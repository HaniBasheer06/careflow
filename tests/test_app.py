import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

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
                "OPENAI_API_KEY": "",
                "OPENAI_MODEL": "gpt-4o-mini",
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

    def test_waiting_room_shows_token_numbers_and_staff_can_advance_consultations(self):
        appointment_date = date.today().isoformat()
        self.assertEqual(self.staff_login().status_code, 200)
        with patch("app.local_date", return_value=appointment_date), patch(
            "app.datetime"
        ) as mocked_datetime:
            mocked_datetime.now.return_value = datetime.combine(
                date.today(), time(9, 30)
            )
            first = self.client.post(
                "/api/clinic/appointments",
                json={
                    "doctorId": self.doctor_id,
                    "date": appointment_date,
                    "patientName": "Private Patient One",
                    "patientPhone": "5551234567",
                },
            ).json["appointment"]
            second = self.client.post(
                "/api/clinic/appointments",
                json={
                    "doctorId": self.doctor_id,
                    "date": appointment_date,
                    "patientName": "Private Patient Two",
                    "patientPhone": "5551234568",
                },
            ).json["appointment"]
            mocked_datetime.now.return_value = datetime.combine(
                date.today(), time(10, 0)
            )

            started = self.client.post(
                f"/api/appointments/{first['id']}/consultation",
                json={"action": "start"},
            )
            cancel_active = self.client.patch(
                f"/api/appointments/{first['id']}",
                json={"status": "cancelled"},
            )
            blocked = self.client.post(
                f"/api/appointments/{second['id']}/consultation",
                json={"action": "start"},
            )
            board = self.client.get(f"/api/waiting-room?date={appointment_date}")
            self.assertEqual(started.status_code, 200)
            self.assertEqual(cancel_active.status_code, 409)
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(board.status_code, 200)
            doctor = next(
                item for item in board.json["doctors"]
                if item["doctorName"]
            )
            self.assertEqual(doctor["currentToken"], first["tokenNumber"])
            self.assertEqual(doctor["waitingTokens"], [second["tokenNumber"]])
            self.assertNotIn("patientName", str(board.json))
            self.assertNotIn("patientPhone", str(board.json))
            self.assertNotIn("Private Patient", str(board.json))

            finished = self.client.post(
                f"/api/appointments/{first['id']}/consultation",
                json={"action": "finish"},
            )
            started_next = self.client.post(
                f"/api/appointments/{second['id']}/consultation",
                json={"action": "start"},
            )
            next_board = self.client.get(f"/api/waiting-room?date={appointment_date}")

        self.assertEqual(finished.status_code, 200)
        self.assertEqual(started_next.status_code, 200)
        self.assertEqual(next_board.json["doctors"][0]["currentToken"], second["tokenNumber"])
        self.assertEqual(next_board.json["doctors"][0]["waitingTokens"], [])

    def test_ai_chat_requires_consent_and_configured_provider_key(self):
        response = self.client.post(
            "/api/ai/doctor-guide",
            json={
                "consent": False,
                "date": (date.today() + timedelta(days=1)).isoformat(),
                "messages": [{"role": "user", "content": "skin rash"}],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.client.get("/api/ai/config").json["enabled"])

        response = self.client.post(
            "/api/ai/doctor-guide",
            json={
                "consent": True,
                "date": (date.today() + timedelta(days=1)).isoformat(),
                "messages": [{"role": "user", "content": "skin rash"}],
            },
        )
        self.assertEqual(response.status_code, 503)

    def test_ai_provider_errors_are_actionable_without_exposing_provider_details(self):
        self.app.config["OPENAI_API_KEY"] = "test-api-key"
        request_body = {
            "consent": True,
            "date": (date.today() + timedelta(days=1)).isoformat(),
            "messages": [{"role": "user", "content": "cough"}],
        }
        cases = [
            (
                401,
                502,
                "OpenAI rejected the configured API key or project access.",
            ),
            (404, 502, "The configured OpenAI model was not found or is unavailable."),
            (429, 503, "OpenAI rate or usage limit reached."),
            (500, 502, "The AI provider request failed (HTTP 500)."),
        ]

        for provider_status, expected_status, expected_message in cases:
            with self.subTest(provider_status=provider_status):
                provider_error = HTTPError(
                    "https://api.openai.com/v1/responses",
                    provider_status,
                    "provider error",
                    None,
                    BytesIO(b"private provider response details"),
                )
                with patch("app.urlopen", side_effect=provider_error):
                    response = self.client.post("/api/ai/doctor-guide", json=request_body)

                self.assertEqual(response.status_code, expected_status)
                self.assertIn(expected_message, response.json["error"])
                self.assertNotIn("private provider response details", response.json["error"])

    def test_ai_chat_redacts_contact_details_and_only_recommends_available_doctors(self):
        self.app.config["OPENAI_API_KEY"] = "test-api-key"
        self.assertTrue(self.client.get("/api/ai/config").json["enabled"])
        appointment_date = (date.today() + timedelta(days=1)).isoformat()
        model_output = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "reply": "A dermatologist may be a useful starting point.",
                                    "specialty": "Dermatology",
                                    "urgent": False,
                                }
                            ),
                        }
                    ],
                }
            ],
        }
        with patch("app.urlopen", return_value=BytesIO(json.dumps(model_output).encode())) as mocked:
            response = self.client.post(
                "/api/ai/doctor-guide",
                json={
                    "consent": True,
                    "date": appointment_date,
                    "messages": [
                        {
                            "role": "user",
                            "content": "I have a rash; call 555 123 4567 or a.person@example.com",
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["specialty"], "Dermatology")
        self.assertEqual(len(response.json["doctors"]), 1)
        self.assertEqual(response.json["doctors"][0]["specialty"], "Dermatology")
        outbound_request = mocked.call_args.args[0]
        outbound = json.loads(outbound_request.data.decode("utf-8"))
        serialized_input = json.dumps(outbound["input"])
        self.assertFalse(outbound["store"])
        self.assertNotIn("555 123 4567", serialized_input)
        self.assertNotIn("a.person@example.com", serialized_input)
        self.assertNotIn("Dr. Sofia Reyes", serialized_input)
        self.assertNotIn(appointment_date, serialized_input)

    def test_ai_urgent_response_never_recommends_a_doctor(self):
        self.app.config["OPENAI_API_KEY"] = "test-api-key"
        model_output = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "reply": "Please get help.",
                                    "specialty": "Cardiology",
                                    "urgent": True,
                                }
                            ),
                        }
                    ],
                }
            ],
        }
        with patch("app.urlopen", return_value=BytesIO(json.dumps(model_output).encode())):
            response = self.client.post(
                "/api/ai/doctor-guide",
                json={
                    "consent": True,
                    "date": (date.today() + timedelta(days=1)).isoformat(),
                    "messages": [{"role": "user", "content": "I feel very unwell"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["urgent"])
        self.assertIsNone(response.json["specialty"])
        self.assertEqual(response.json["doctors"], [])
        self.assertIn("emergency services", response.json["reply"])

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
        self.assertEqual(self.client.get("/waiting-room").status_code, 200)
        self.assertEqual(self.client.get("/waiting-room.js").status_code, 200)
        self.assertEqual(self.client.get("/turnstile.js").status_code, 404)
        self.assertEqual(self.client.get("/api/security/config").status_code, 404)
        self.assertEqual(self.client.get("/data/clinic.sqlite").status_code, 404)


if __name__ == "__main__":
    unittest.main()
