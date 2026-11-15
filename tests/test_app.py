import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import create_app


class CareflowAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "clinic.sqlite"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(database),
                "SECRET_KEY": "test-secret",
                "SECRET_KEY_CONFIGURED": True,
                "CLINIC_STAFF_PASSWORD": "staff-password",
                "CLINIC_STAFF_PASSWORD_CONFIGURED": True,
            }
        )
        self.client = self.app.test_client()
        self.doctor_id = self.client.get("/api/doctors").json["doctors"][0]["id"]

    def tearDown(self):
        self.temporary_directory.cleanup()

    def book(self, name):
        return self.client.post(
            "/api/appointments",
            json={
                "doctorId": self.doctor_id,
                "date": (date.today() + timedelta(days=1)).isoformat(),
                "patientName": name,
                "patientPhone": "555 123 4567",
            },
        )

    def staff_login(self):
        return self.client.post(
            "/api/staff/login",
            json={"password": "staff-password"},
        )

    def test_booking_gets_first_token_and_private_tracking_link(self):
        response = self.book("Patient One")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["appointment"]["tokenNumber"], 1)
        token = response.json["trackingToken"]
        self.assertEqual(len(token), 64)
        status = self.client.get(f"/api/patient-bookings/{token}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["appointment"]["status"], "booked")

    def test_cancellation_resequences_next_booking_and_notifies_it(self):
        first = self.book("Patient One").json
        second = self.book("Patient Two").json

        cancelled = self.client.post(
            f"/api/patient-bookings/{first['trackingToken']}/cancel"
        )
        updated = self.client.get(
            f"/api/patient-bookings/{second['trackingToken']}"
        )

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(updated.json["appointment"]["tokenNumber"], 1)
        self.assertEqual(updated.json["notifications"][0]["previousTokenNumber"], 2)
        self.assertEqual(updated.json["notifications"][0]["tokenNumber"], 1)

    def test_staff_routes_require_password_session(self):
        self.assertEqual(
            self.client.get("/api/doctors?includeInactive=1").status_code,
            401,
        )
        self.assertEqual(
            self.staff_login().status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/doctors?includeInactive=1").status_code,
            200,
        )

    def test_staff_can_change_hours_and_patient_receives_time_update(self):
        appointment = self.book("Patient One").json
        self.assertEqual(self.staff_login().status_code, 200)
        doctor = next(
            item
            for item in self.client.get("/api/doctors?includeInactive=1").json["doctors"]
            if item["id"] == self.doctor_id
        )
        doctor["startTime"] = "08:30"

        updated = self.client.patch(
            f"/api/doctors/{self.doctor_id}",
            json=doctor,
        )
        tracking = self.client.get(
            f"/api/patient-bookings/{appointment['trackingToken']}"
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(tracking.json["appointment"]["time"], "08:30")
        self.assertEqual(tracking.json["notifications"][0]["previousTime"], "08:00")
        self.assertEqual(tracking.json["notifications"][0]["time"], "08:30")

    def test_staff_can_book_for_patient_and_share_private_link(self):
        self.assertEqual(self.staff_login().status_code, 200)
        response = self.client.post(
            "/api/clinic/appointments",
            json={
                "doctorId": self.doctor_id,
                "date": (date.today() + timedelta(days=1)).isoformat(),
                "patientName": "Reception Booking",
                "patientPhone": "5551234567",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["appointment"]["tokenNumber"], 1)
        tracking = self.client.get(
            f"/api/patient-bookings/{response.json['trackingToken']}"
        )
        self.assertEqual(tracking.status_code, 200)

    def test_rejects_invalid_phone_and_past_date(self):
        invalid_phone = self.client.post(
            "/api/appointments",
            json={
                "doctorId": self.doctor_id,
                "date": (date.today() + timedelta(days=1)).isoformat(),
                "patientName": "Patient One",
                "patientPhone": "123",
            },
        )
        past_date = self.client.post(
            "/api/appointments",
            json={
                "doctorId": self.doctor_id,
                "date": (date.today() - timedelta(days=1)).isoformat(),
                "patientName": "Patient One",
                "patientPhone": "5551234567",
            },
        )

        self.assertEqual(invalid_phone.status_code, 400)
        self.assertEqual(past_date.status_code, 400)

    def test_patient_booking_is_rate_limited(self):
        for _ in range(self.app.config["PATIENT_BOOKING_RATE_LIMIT"]):
            response = self.client.post(
                "/api/appointments",
                json={},
            )
            self.assertEqual(response.status_code, 400)

        limited = self.client.post(
            "/api/appointments",
            json={},
        )

        self.assertEqual(limited.status_code, 429)

    def test_staff_login_is_rate_limited(self):
        for _ in range(self.app.config["STAFF_LOGIN_RATE_LIMIT"]):
            response = self.client.post(
                "/api/staff/login",
                json={"password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)

        limited = self.client.post(
            "/api/staff/login",
            json={"password": "wrong-password"},
        )

        self.assertEqual(limited.status_code, 429)

    def test_static_server_does_not_expose_database_files(self):
        response = self.client.get("/styles.css")
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.get("/turnstile.js").status_code, 404)
        self.assertEqual(self.client.get("/api/security/config").status_code, 404)
        self.assertEqual(self.client.get("/data/clinic.sqlite").status_code, 404)


if __name__ == "__main__":
    unittest.main()
