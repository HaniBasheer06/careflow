import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, current_app, g, jsonify, request, send_from_directory, session


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

MIN_BOOKING_LEAD_MINUTES = 15
NO_SHOW_GRACE_MINUTES = 10
DEFAULT_AVG_MINUTES = (
    ("cardio", 10),
    ("pediatr", 8),
    ("dermat", 7),
    ("family", 7),
    ("general", 7),
)


class ApiError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


def local_date():
    return date.today().isoformat()


def local_time():
    return datetime.now().strftime("%H:%M")


def date_is_valid(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def time_is_valid(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is not None


def exact_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None


def time_to_minutes(value):
    hours, minutes = (int(part) for part in value.split(":"))
    return hours * 60 + minutes


def format_time(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def weekday_sunday_first(value):
    return (date.fromisoformat(value).weekday() + 1) % 7


def default_average_minutes(specialty):
    normalized = specialty.lower()
    return next((minutes for pattern, minutes in DEFAULT_AVG_MINUTES if pattern in normalized), 7)


def get_db():
    if "db" not in g:
        connection = sqlite3.connect(
            current_app.config["DATABASE"],
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        g.db = connection
    return g.db


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def table_columns(connection, table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def initialize_database(database_path):
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS doctors (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          specialty TEXT NOT NULL,
          working_days TEXT NOT NULL,
          start_time TEXT NOT NULL,
          end_time TEXT NOT NULL,
          slot_minutes INTEGER NOT NULL,
          avg_minutes INTEGER NOT NULL DEFAULT 7,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS appointments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doctor_id INTEGER NOT NULL REFERENCES doctors(id),
          patient_name TEXT NOT NULL,
          patient_phone TEXT NOT NULL,
          appointment_date TEXT NOT NULL,
          start_time TEXT NOT NULL,
          duration_minutes INTEGER NOT NULL,
          token_number INTEGER NOT NULL,
          tracking_token_hash TEXT,
          status TEXT NOT NULL DEFAULT 'booked'
            CHECK (status IN ('booked', 'cancelled', 'completed', 'no_show')),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS appointment_notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          appointment_id INTEGER NOT NULL REFERENCES appointments(id),
          previous_token_number INTEGER NOT NULL,
          token_number INTEGER NOT NULL,
          previous_time TEXT NOT NULL,
          time TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          acknowledged_at TEXT
        );
        """
    )

    doctor_columns = table_columns(connection, "doctors")
    if "avg_minutes" not in doctor_columns:
        connection.execute("ALTER TABLE doctors ADD COLUMN avg_minutes INTEGER NOT NULL DEFAULT 7")
        for doctor in connection.execute("SELECT id, specialty FROM doctors"):
            connection.execute(
                "UPDATE doctors SET avg_minutes = ? WHERE id = ?",
                (default_average_minutes(doctor["specialty"]), doctor["id"]),
            )

    appointment_columns = table_columns(connection, "appointments")
    token_column_added = "token_number" not in appointment_columns
    if token_column_added:
        connection.execute("ALTER TABLE appointments ADD COLUMN token_number INTEGER")
        counts = {}
        legacy = connection.execute(
            """
            SELECT id, doctor_id, appointment_date
            FROM appointments
            ORDER BY doctor_id, appointment_date, start_time, id
            """
        ).fetchall()
        for appointment in legacy:
            key = (appointment["doctor_id"], appointment["appointment_date"])
            counts[key] = counts.get(key, 0) + 1
            connection.execute(
                "UPDATE appointments SET token_number = ? WHERE id = ?",
                (counts[key], appointment["id"]),
            )

    appointment_columns = table_columns(connection, "appointments")
    if "tracking_token_hash" not in appointment_columns:
        connection.execute("ALTER TABLE appointments ADD COLUMN tracking_token_hash TEXT")

    if token_column_added:
        for appointment in connection.execute(
            """
            SELECT a.id, a.token_number, a.appointment_date, d.start_time, d.avg_minutes
            FROM appointments a
            JOIN doctors d ON d.id = a.doctor_id
            WHERE a.status = 'booked' AND a.appointment_date >= ?
            """,
            (local_date(),),
        ).fetchall():
            expected = time_to_minutes(appointment["start_time"]) + (
                appointment["token_number"] - 1
            ) * appointment["avg_minutes"]
            connection.execute(
                "UPDATE appointments SET start_time = ?, duration_minutes = ? WHERE id = ?",
                (format_time(expected), appointment["avg_minutes"], appointment["id"]),
            )

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS appointments_by_doctor_date
          ON appointments(doctor_id, appointment_date, status, start_time);
        CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_doctor_slot
          ON appointments(doctor_id, appointment_date, start_time)
          WHERE status = 'booked';
        CREATE UNIQUE INDEX IF NOT EXISTS one_booking_per_doctor_token
          ON appointments(doctor_id, appointment_date, token_number)
          WHERE status = 'booked';
        CREATE UNIQUE INDEX IF NOT EXISTS one_tracking_hash_per_appointment
          ON appointments(tracking_token_hash)
          WHERE tracking_token_hash IS NOT NULL;
        """
    )

    if connection.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
        doctors = (
            ("Dr. Avery Morgan", "Family Medicine", [0, 1, 2, 3, 4, 5, 6], "08:00", "16:00", 7),
            ("Dr. Maya Patel", "Pediatrics", [0, 1, 2, 3, 4, 5, 6], "09:00", "17:00", 8),
            ("Dr. Liam Chen", "Cardiology", [0, 1, 2, 3, 4, 5, 6], "08:30", "15:30", 10),
            ("Dr. Sofia Reyes", "Dermatology", [0, 1, 2, 3, 4, 5, 6], "10:00", "18:00", 7),
        )
        connection.executemany(
            """
            INSERT INTO doctors
              (name, specialty, working_days, start_time, end_time, slot_minutes, avg_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (name, specialty, json.dumps(days), start, end, average, average)
                for name, specialty, days, start, end, average in doctors
            ],
        )
    connection.close()


def doctor_by_id(connection, doctor_id):
    return connection.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()


def token_capacity(doctor):
    clinic_minutes = time_to_minutes(doctor["end_time"]) - time_to_minutes(doctor["start_time"])
    return clinic_minutes // doctor["avg_minutes"]


def public_doctor(doctor):
    return {
        "id": doctor["id"],
        "name": doctor["name"],
        "specialty": doctor["specialty"],
        "workingDays": json.loads(doctor["working_days"]),
        "startTime": doctor["start_time"],
        "endTime": doctor["end_time"],
        "avgMinutes": doctor["avg_minutes"],
        "dailyTokenCapacity": token_capacity(doctor),
        "active": bool(doctor["active"]),
    }


def list_doctors(connection, include_inactive=False):
    query = "SELECT * FROM doctors ORDER BY active DESC, name"
    if not include_inactive:
        query = "SELECT * FROM doctors WHERE active = 1 ORDER BY name"
    return [public_doctor(row) for row in connection.execute(query).fetchall()]


def get_next_token(connection, doctor, appointment_date):
    weekday = weekday_sunday_first(appointment_date)
    working_days = json.loads(doctor["working_days"])
    if not doctor["active"] or weekday not in working_days:
        return {"nextToken": None, "remainingTokenCount": 0}

    capacity = token_capacity(doctor)
    assigned = connection.execute(
        """
        SELECT token_number, start_time FROM appointments
        WHERE doctor_id = ? AND appointment_date = ?
          AND status IN ('booked', 'completed', 'no_show')
        ORDER BY token_number DESC
        """,
        (doctor["id"], appointment_date),
    ).fetchall()
    assigned_numbers = {row["token_number"] for row in assigned if row["token_number"] is not None}
    remaining = max(0, capacity - sum(number <= capacity for number in assigned_numbers))
    token_number = 1
    while token_number in assigned_numbers and token_number <= capacity:
        token_number += 1
    if remaining == 0 or token_number > capacity:
        return {"nextToken": None, "remainingTokenCount": remaining}

    opening = time_to_minutes(doctor["start_time"])
    closing = time_to_minutes(doctor["end_time"])
    average = doctor["avg_minutes"]
    first_estimate = opening + (token_number - 1) * average
    previous = next((row for row in assigned if row["token_number"] < token_number), None)
    after_previous = (
        time_to_minutes(previous["start_time"]) + average if previous else opening
    )
    now = datetime.now()
    earliest_today = now.hour * 60 + now.minute + MIN_BOOKING_LEAD_MINUTES
    expected = max(
        first_estimate,
        after_previous,
        earliest_today if appointment_date == local_date() else opening,
    )
    if expected + average > closing:
        return {"nextToken": None, "remainingTokenCount": remaining}
    return {"nextToken": {"tokenNumber": token_number, "expectedTime": format_time(expected)},
            "remainingTokenCount": remaining}


def tracking_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_staff():
    if session.get("role") != "staff":
        raise ApiError(401, "Sign in to the clinic portal to continue.")


def enforce_rate_limit(scope, limit, window_seconds):
    now = time.monotonic()
    client_ip = request.remote_addr or "unknown"
    key = (scope, client_ip)
    buckets = current_app.extensions["rate_limit_buckets"]
    lock = current_app.extensions["rate_limit_lock"]
    retry_after = None

    with lock:
        events = buckets.setdefault(key, deque())
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(window_seconds - (now - events[0])))
        else:
            events.append(now)

        if len(buckets) > 2048:
            for bucket_key, bucket_events in list(buckets.items()):
                while bucket_events and now - bucket_events[0] >= window_seconds:
                    bucket_events.popleft()
                if not bucket_events:
                    buckets.pop(bucket_key, None)

    if retry_after is not None:
        raise ApiError(
            429,
            "Too many requests. Please wait a little before trying again.",
            {"retryAfterSeconds": retry_after},
        )


def request_json():
    body = request.get_json(silent=False)
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    return body


def validate_appointment(body):
    doctor_id = exact_integer(body.get("doctorId"))
    if doctor_id is None or doctor_id < 1 or not date_is_valid(body.get("date")):
        raise ApiError(400, "Choose a doctor and a valid appointment date.")
    appointment_date = body["date"]
    if appointment_date < local_date():
        raise ApiError(400, "Choose today or a future date.")
    name = body.get("patientName") if isinstance(body.get("patientName"), str) else ""
    name = name.strip()
    raw_phone = body.get("patientPhone") if isinstance(body.get("patientPhone"), str) else ""
    phone = re.sub(r"[\s().-]", "", raw_phone.strip())
    if not 2 <= len(name) <= 100:
        raise ApiError(400, "Patient name must be 2–100 characters.")
    if re.fullmatch(r"\+?\d{7,15}", phone) is None:
        raise ApiError(400, "Enter a phone number with 7–15 digits, optionally prefixed with +.")
    return {
        "doctorId": doctor_id,
        "date": appointment_date,
        "patientName": name,
        "patientPhone": phone,
    }


def resequence_booked_appointments(connection, doctor, appointment_date):
    bookings = connection.execute(
        """
        SELECT id, token_number, start_time FROM appointments
        WHERE doctor_id = ? AND appointment_date = ? AND status = 'booked'
        ORDER BY created_at, id
        """,
        (doctor["id"], appointment_date),
    ).fetchall()
    fixed = {
        row["token_number"]
        for row in connection.execute(
            """
            SELECT token_number FROM appointments
            WHERE doctor_id = ? AND appointment_date = ?
              AND status IN ('completed', 'no_show')
            """,
            (doctor["id"], appointment_date),
        ).fetchall()
    }
    if bookings:
        connection.executemany(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ?",
            [(booking["id"],) for booking in bookings],
        )

    opening = time_to_minutes(doctor["start_time"])
    next_number = 1
    changed_count = 0
    average = doctor["avg_minutes"]
    for booking in bookings:
        while next_number in fixed:
            next_number += 1
        number = next_number
        fixed.add(number)
        next_number += 1
        moved = booking["token_number"] - number
        expected = max(opening, time_to_minutes(booking["start_time"]) - moved * average)
        expected_time = format_time(expected)
        connection.execute(
            """
            UPDATE appointments
            SET token_number = ?, start_time = ?, duration_minutes = ?, status = 'booked'
            WHERE id = ?
            """,
            (number, expected_time, average, booking["id"]),
        )
        if booking["token_number"] != number or booking["start_time"] != expected_time:
            connection.execute(
                """
                INSERT INTO appointment_notifications
                  (appointment_id, previous_token_number, token_number, previous_time, time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (booking["id"], booking["token_number"], number, booking["start_time"], expected_time),
            )
            changed_count += 1
    return changed_count


def create_appointment(connection, booking):
    doctor = doctor_by_id(connection, booking["doctorId"])
    if not doctor or not doctor["active"]:
        raise ApiError(404, "That doctor is no longer available.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        resequence_booked_appointments(connection, doctor, booking["date"])
        queue = get_next_token(connection, doctor, booking["date"])
        next_token = queue["nextToken"]
        if not next_token:
            raise ApiError(409, "No more tokens are available for this date. Please choose another date or doctor.")
        token = secrets.token_hex(32)
        cursor = connection.execute(
            """
            INSERT INTO appointments
              (doctor_id, patient_name, patient_phone, appointment_date, start_time,
               duration_minutes, token_number, tracking_token_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                booking["doctorId"],
                booking["patientName"],
                booking["patientPhone"],
                booking["date"],
                next_token["expectedTime"],
                doctor["avg_minutes"],
                next_token["tokenNumber"],
                tracking_hash(token),
            ),
        )
        connection.commit()
        return {
            "trackingToken": token,
            "appointment": {
                "id": cursor.lastrowid,
                "doctorId": doctor["id"],
                "doctorName": doctor["name"],
                "specialty": doctor["specialty"],
                "patientName": booking["patientName"],
                "patientPhone": booking["patientPhone"],
                "date": booking["date"],
                "time": next_token["expectedTime"],
                "tokenNumber": next_token["tokenNumber"],
                "durationMinutes": doctor["avg_minutes"],
                "status": "booked",
            },
        }
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise ApiError(409, "Another patient booked first. Please try again to get the next token.") from error
    except Exception:
        connection.rollback()
        raise


def cancel_and_resequence(connection, appointment_id):
    connection.execute("BEGIN IMMEDIATE")
    try:
        appointment = connection.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if not appointment:
            raise ApiError(404, "Appointment not found.")
        if appointment["status"] != "booked":
            raise ApiError(409, "This appointment has already been updated.")
        doctor = doctor_by_id(connection, appointment["doctor_id"])
        connection.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,)
        )
        count = resequence_booked_appointments(
            connection, doctor, appointment["appointment_date"]
        )
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise


def validate_doctor(body):
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    specialty = body.get("specialty") if isinstance(body.get("specialty"), str) else ""
    name = name.strip()
    specialty = specialty.strip()
    raw_days = body.get("workingDays")
    parsed_days = [exact_integer(day) for day in raw_days] if isinstance(raw_days, list) else []
    days = sorted(set(parsed_days)) if parsed_days and all(day is not None for day in parsed_days) else []
    start = body.get("startTime")
    end = body.get("endTime")
    average = exact_integer(body.get("avgMinutes"))
    if not 2 <= len(name) <= 100:
        raise ApiError(400, "Doctor name must be 2–100 characters.")
    if not 2 <= len(specialty) <= 100:
        raise ApiError(400, "Specialty must be 2–100 characters.")
    if not days or any(day < 0 or day > 6 for day in days):
        raise ApiError(400, "Choose at least one valid working day.")
    if not time_is_valid(start) or not time_is_valid(end) or time_to_minutes(start) >= time_to_minutes(end):
        raise ApiError(400, "Clinic hours must have a valid start before end time.")
    if average is None or average < 5 or average > 60:
        raise ApiError(400, "Average consultation time must be between 5 and 60 minutes.")
    if time_to_minutes(end) - time_to_minutes(start) < average:
        raise ApiError(400, "Clinic hours must fit at least one patient token.")
    return {
        "name": name,
        "specialty": specialty,
        "days": days,
        "startTime": start,
        "endTime": end,
        "avgMinutes": average,
        "active": body.get("active") is not False,
    }


def doctor_schedule_conflicts(connection, doctor, update):
    conflicts = []
    rows = connection.execute(
        """
        SELECT appointment_date, token_number FROM appointments
        WHERE doctor_id = ? AND status = 'booked' AND appointment_date >= ?
        ORDER BY appointment_date, token_number
        """,
        (doctor["id"], local_date()),
    ).fetchall()
    capacity = (time_to_minutes(update["endTime"]) - time_to_minutes(update["startTime"])) // update["avgMinutes"]
    for appointment in rows:
        weekday = weekday_sunday_first(appointment["appointment_date"])
        if (
            not update["active"]
            or weekday not in update["days"]
            or appointment["token_number"] > capacity
        ):
            conflicts.append(
                f"{appointment['appointment_date']} token #{appointment['token_number']}"
            )
    return conflicts


def update_future_estimates(connection, doctor_id, update):
    today = local_date()
    appointments = connection.execute(
        """
        SELECT id, token_number, start_time FROM appointments
        WHERE doctor_id = ? AND appointment_date >= ? AND status = 'booked'
        ORDER BY appointment_date, token_number
        """,
        (doctor_id, today),
    ).fetchall()
    if appointments:
        connection.executemany(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ?",
            [(appointment["id"],) for appointment in appointments],
        )
    opening = time_to_minutes(update["startTime"])
    for appointment in appointments:
        expected = format_time(opening + (appointment["token_number"] - 1) * update["avgMinutes"])
        connection.execute(
            """
            UPDATE appointments
            SET start_time = ?, duration_minutes = ?, status = 'booked'
            WHERE id = ?
            """,
            (expected, update["avgMinutes"], appointment["id"]),
        )
        if appointment["start_time"] != expected:
            connection.execute(
                """
                INSERT INTO appointment_notifications
                  (appointment_id, previous_token_number, token_number, previous_time, time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    appointment["id"],
                    appointment["token_number"],
                    appointment["token_number"],
                    appointment["start_time"],
                    expected,
                ),
            )


def create_app(test_config=None):
    configured_secret = os.getenv("FLASK_SECRET_KEY")
    staff_password = os.getenv("CLINIC_STAFF_PASSWORD", "")
    secret_is_configured = bool(
        configured_secret
        and len(configured_secret) >= 32
        and configured_secret != "replace-with-a-long-random-secret"
    )
    password_is_configured = bool(
        len(staff_password) >= 12
        and staff_password != "replace-with-a-unique-clinic-password"
    )
    app = Flask(__name__, static_folder=None)
    app.config.from_mapping(
        DATABASE=os.getenv("DB_FILE", str(ROOT / "data" / "clinic.sqlite")),
        SECRET_KEY=configured_secret or secrets.token_hex(32),
        SECRET_KEY_CONFIGURED=secret_is_configured,
        CLINIC_STAFF_PASSWORD=staff_password,
        CLINIC_STAFF_PASSWORD_CONFIGURED=password_is_configured,
        STAFF_LOGIN_RATE_LIMIT=10,
        PATIENT_BOOKING_RATE_LIMIT=20,
        CLINIC_BOOKING_RATE_LIMIT=30,
        RATE_LIMIT_WINDOW_SECONDS=3600,
        PORT=int(os.getenv("PORT", "3018")),
        HOST=os.getenv("HOST", "0.0.0.0"),
        COOKIE_SECURE=os.getenv("COOKIE_SECURE", "").lower() == "true",
        MAX_CONTENT_LENGTH=64 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    app.extensions["rate_limit_buckets"] = {}
    app.extensions["rate_limit_lock"] = threading.Lock()
    app.config["SESSION_COOKIE_SECURE"] = app.config["COOKIE_SECURE"]
    app.secret_key = app.config["SECRET_KEY"]
    initialize_database(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    @app.after_request
    def set_response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    @app.errorhandler(ApiError)
    def handle_api_error(error):
        payload = {"error": str(error)}
        if error.details is not None:
            payload["details"] = error.details
        return jsonify(payload), error.status

    @app.errorhandler(sqlite3.Error)
    def handle_database_error(_error):
        app.logger.exception("Database request failed")
        return jsonify({"error": "The server could not complete the request."}), 500

    @app.errorhandler(413)
    def handle_large_request(_error):
        return jsonify({"error": "Request is too large."}), 413

    @app.errorhandler(400)
    @app.errorhandler(415)
    def handle_invalid_json(error):
        return jsonify({"error": "Request body must be valid JSON."}), error.code

    @app.get("/")
    def patient_page():
        return send_from_directory(ROOT, "index.html")

    @app.get("/clinic")
    @app.get("/clinic/")
    def clinic_page():
        return send_from_directory(ROOT, "clinic.html")

    @app.get("/<path:filename>")
    def static_file(filename):
        if filename not in {"styles.css", "app.js", "clinic.js"}:
            abort(404)
        return send_from_directory(ROOT, filename)

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/auth/me")
    def auth_me():
        is_staff = session.get("role") == "staff"
        return jsonify(
            {
                "user": {"name": "Clinic staff", "role": "staff"} if is_staff else None,
                "staffLoginConfigured": bool(
                    app.config["CLINIC_STAFF_PASSWORD_CONFIGURED"]
                    and app.config["SECRET_KEY_CONFIGURED"]
                ),
            }
        )

    @app.post("/api/staff/login")
    def staff_login():
        enforce_rate_limit(
            "staff_login",
            app.config["STAFF_LOGIN_RATE_LIMIT"],
            app.config["RATE_LIMIT_WINDOW_SECONDS"],
        )
        configured_password = app.config["CLINIC_STAFF_PASSWORD"]
        if (
            not app.config["CLINIC_STAFF_PASSWORD_CONFIGURED"]
            or not app.config["SECRET_KEY_CONFIGURED"]
        ):
            raise ApiError(
                503,
                "Clinic login needs a staff password and Flask secret in the environment.",
            )
        body = request_json()
        supplied = body.get("password")
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode("utf-8"), configured_password.encode("utf-8")
        ):
            raise ApiError(401, "The clinic password is incorrect.")
        session.clear()
        session.permanent = True
        session["role"] = "staff"
        return jsonify({"ok": True, "user": {"name": "Clinic staff", "role": "staff"}})

    @app.post("/api/auth/logout")
    def auth_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/doctors")
    def doctors():
        include_inactive = request.args.get("includeInactive") == "1"
        if include_inactive:
            require_staff()
        return jsonify({"doctors": list_doctors(get_db(), include_inactive)})

    @app.get("/api/availability")
    def availability():
        try:
            doctor_id = int(request.args.get("doctorId", ""))
        except ValueError:
            doctor_id = 0
        appointment_date = request.args.get("date")
        if doctor_id < 1 or not date_is_valid(appointment_date):
            raise ApiError(400, "Choose a doctor and a valid appointment date.")
        if appointment_date < local_date():
            raise ApiError(400, "Choose today or a future date.")
        connection = get_db()
        doctor = doctor_by_id(connection, doctor_id)
        if not doctor or not doctor["active"]:
            raise ApiError(404, "That doctor is not available for booking.")
        queue = get_next_token(connection, doctor, appointment_date)
        return jsonify(
            {
                "date": appointment_date,
                "doctorId": doctor_id,
                "openingTime": doctor["start_time"],
                "closingTime": doctor["end_time"],
                "avgMinutes": doctor["avg_minutes"],
                "capacity": token_capacity(doctor),
                "nextTokenEstimate": (
                    queue["nextToken"]["expectedTime"] if queue["nextToken"] else None
                ),
                "remainingTokenCount": queue["remainingTokenCount"],
            }
        )

    @app.post("/api/appointments")
    def patient_booking():
        enforce_rate_limit(
            "patient_booking",
            app.config["PATIENT_BOOKING_RATE_LIMIT"],
            app.config["RATE_LIMIT_WINDOW_SECONDS"],
        )
        body = request_json()
        result = create_appointment(get_db(), validate_appointment(body))
        return jsonify(result), 201

    @app.post("/api/clinic/appointments")
    def staff_booking():
        require_staff()
        enforce_rate_limit(
            "clinic_booking",
            app.config["CLINIC_BOOKING_RATE_LIMIT"],
            app.config["RATE_LIMIT_WINDOW_SECONDS"],
        )
        result = create_appointment(get_db(), validate_appointment(request_json()))
        return jsonify(result), 201

    @app.get("/api/appointments")
    def appointments():
        require_staff()
        appointment_date = request.args.get("date")
        if not date_is_valid(appointment_date):
            raise ApiError(400, "Choose a valid appointment date.")
        rows = get_db().execute(
            """
            SELECT a.id, a.patient_name AS patientName, a.patient_phone AS patientPhone,
              a.appointment_date AS date, a.start_time AS time,
              a.duration_minutes AS durationMinutes, a.token_number AS tokenNumber, a.status,
              d.id AS doctorId, d.name AS doctorName, d.specialty
            FROM appointments a
            JOIN doctors d ON d.id = a.doctor_id
            WHERE a.appointment_date = ?
            ORDER BY a.start_time, d.name
            """,
            (appointment_date,),
        ).fetchall()
        return jsonify({"appointments": [dict(row) for row in rows]})

    @app.get("/api/patient-bookings/<token>")
    def patient_booking_status(token):
        if re.fullmatch(r"[a-f0-9]{64}", token) is None:
            raise ApiError(404, "Booking link not found.")
        connection = get_db()
        appointment = connection.execute(
            """
            SELECT a.id, a.appointment_date AS date, a.start_time AS time,
              a.duration_minutes AS durationMinutes, a.token_number AS tokenNumber, a.status,
              d.name AS doctorName, d.specialty
            FROM appointments a JOIN doctors d ON d.id = a.doctor_id
            WHERE a.tracking_token_hash = ?
            """,
            (tracking_hash(token),),
        ).fetchone()
        if not appointment:
            raise ApiError(404, "Booking link not found.")
        notifications = connection.execute(
            """
            SELECT id, previous_token_number AS previousTokenNumber,
              token_number AS tokenNumber, previous_time AS previousTime, time
            FROM appointment_notifications
            WHERE appointment_id = ? AND acknowledged_at IS NULL
            ORDER BY id
            """,
            (appointment["id"],),
        ).fetchall()
        return jsonify(
            {
                "appointment": {
                    key: appointment[key]
                    for key in (
                        "doctorName",
                        "specialty",
                        "date",
                        "time",
                        "durationMinutes",
                        "tokenNumber",
                        "status",
                    )
                },
                "notifications": [dict(row) for row in notifications],
            }
        )

    @app.post("/api/patient-bookings/<token>/notifications/<int:notification_id>/ack")
    def acknowledge_notification(token, notification_id):
        if re.fullmatch(r"[a-f0-9]{64}", token) is None:
            raise ApiError(404, "Booking link not found.")
        connection = get_db()
        appointment = connection.execute(
            "SELECT id FROM appointments WHERE tracking_token_hash = ?",
            (tracking_hash(token),),
        ).fetchone()
        if not appointment:
            raise ApiError(404, "Booking link not found.")
        result = connection.execute(
            """
            UPDATE appointment_notifications SET acknowledged_at = CURRENT_TIMESTAMP
            WHERE id = ? AND appointment_id = ? AND acknowledged_at IS NULL
            """,
            (notification_id, appointment["id"]),
        )
        if result.rowcount == 0:
            raise ApiError(404, "Booking update not found.")
        return jsonify({"ok": True})

    @app.post("/api/patient-bookings/<token>/cancel")
    def cancel_patient_booking(token):
        if re.fullmatch(r"[a-f0-9]{64}", token) is None:
            raise ApiError(404, "Booking link not found.")
        connection = get_db()
        row = connection.execute(
            "SELECT id FROM appointments WHERE tracking_token_hash = ?",
            (tracking_hash(token),),
        ).fetchone()
        if not row:
            raise ApiError(404, "Booking link not found.")
        count = cancel_and_resequence(connection, row["id"])
        return jsonify({"ok": True, "status": "cancelled", "renumberedCount": count})

    @app.post("/api/doctors")
    def add_doctor():
        require_staff()
        doctor = validate_doctor(request_json())
        connection = get_db()
        cursor = connection.execute(
            """
            INSERT INTO doctors
              (name, specialty, working_days, start_time, end_time, slot_minutes, avg_minutes, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doctor["name"],
                doctor["specialty"],
                json.dumps(doctor["days"]),
                doctor["startTime"],
                doctor["endTime"],
                doctor["avgMinutes"],
                doctor["avgMinutes"],
                int(doctor["active"]),
            ),
        )
        return jsonify({"doctor": public_doctor(doctor_by_id(connection, cursor.lastrowid))}), 201

    @app.patch("/api/doctors/<int:doctor_id>")
    def edit_doctor(doctor_id):
        require_staff()
        update = validate_doctor(request_json())
        connection = get_db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = doctor_by_id(connection, doctor_id)
            if not existing:
                raise ApiError(404, "Doctor not found.")
            conflicts = doctor_schedule_conflicts(connection, existing, update)
            if conflicts:
                raise ApiError(
                    409,
                    "These changes conflict with booked appointments. Cancel or reschedule them first.",
                    {"conflicts": conflicts},
                )
            connection.execute(
                """
                UPDATE doctors SET name = ?, specialty = ?, working_days = ?,
                  start_time = ?, end_time = ?, slot_minutes = ?, avg_minutes = ?, active = ?
                WHERE id = ?
                """,
                (
                    update["name"],
                    update["specialty"],
                    json.dumps(update["days"]),
                    update["startTime"],
                    update["endTime"],
                    update["avgMinutes"],
                    update["avgMinutes"],
                    int(update["active"]),
                    doctor_id,
                ),
            )
            update_future_estimates(connection, doctor_id, update)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return jsonify({"doctor": public_doctor(doctor_by_id(connection, doctor_id))})

    @app.patch("/api/appointments/<int:appointment_id>")
    def update_appointment(appointment_id):
        require_staff()
        body = request_json()
        status = body.get("status")
        if not isinstance(status, str) or status not in {"cancelled", "completed", "no_show"}:
            raise ApiError(400, "Choose a valid appointment status.")
        connection = get_db()
        appointment = connection.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if not appointment:
            raise ApiError(404, "Appointment not found.")
        if appointment["status"] != "booked":
            raise ApiError(409, "This appointment has already been updated.")
        if status == "cancelled":
            count = cancel_and_resequence(connection, appointment_id)
            return jsonify({"ok": True, "status": status, "renumberedCount": count})
        appointment_minutes = time_to_minutes(appointment["start_time"])
        today = local_date()
        if status == "completed" and (
            appointment["appointment_date"] > today
            or (
                appointment["appointment_date"] == today
                and appointment_minutes > time_to_minutes(local_time())
            )
        ):
            raise ApiError(409, "A future appointment cannot be marked completed.")
        if status == "no_show" and (
            appointment["appointment_date"] > today
            or (
                appointment["appointment_date"] == today
                and appointment_minutes + NO_SHOW_GRACE_MINUTES > time_to_minutes(local_time())
            )
        ):
            raise ApiError(
                409,
                f"A no-show can be recorded after the {NO_SHOW_GRACE_MINUTES}-minute grace period.",
            )
        connection.execute(
            "UPDATE appointments SET status = ? WHERE id = ? AND status = 'booked'",
            (status, appointment_id),
        )
        return jsonify({"ok": True, "status": status})

    return app


app = create_app()


if __name__ == "__main__":
    if not app.config["SECRET_KEY_CONFIGURED"] or not app.config["CLINIC_STAFF_PASSWORD_CONFIGURED"]:
        print("Clinic staff login is disabled until FLASK_SECRET_KEY and CLINIC_STAFF_PASSWORD are set in .env.")
    app.run(host=app.config["HOST"], port=app.config["PORT"], debug=False)
