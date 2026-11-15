import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import ApiError, create_app, verify_turnstile


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
                "TURNSTILE_SITE_KEY": "test-site-key",
                "TURNSTILE_SECRET_KEY": "test-secret-key",
            }
        )
        self.turnstile_patcher = patch("app.verify_turnstile")
        self.verify_turnstile = self.turnstile_patcher.start()
        self.addCleanup(self.turnstile_patcher.stop)
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
                "turnstileToken": "valid-test-token",
            },
        )

    def staff_login(self):
        return self.client.post(
            "/api/staff/login",
            json={"password": "staff-password", "turnstileToken": "valid-test-token"},
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
        self.verify_turnstile.assert_called_with("valid-test-token", "patient_booking")

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
        self.verify_turnstile.assert_called_with("valid-test-token", "staff_login")

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
                "turnstileToken": "valid-test-token",
            },
        )
        past_date = self.client.post(
            "/api/appointments",
            json={
                "doctorId": self.doctor_id,
                "date": (date.today() - timedelta(days=1)).isoformat(),
                "patientName": "Patient One",
                "patientPhone": "5551234567",
                "turnstileToken": "valid-test-token",
            },
        )

        self.assertEqual(invalid_phone.status_code, 400)
        self.assertEqual(past_date.status_code, 400)

    def test_rejects_failed_turnstile_verification(self):
        self.verify_turnstile.side_effect = ApiError(400, "Security check was rejected.")

        response = self.book("Patient One")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Security check was rejected.")

    def test_security_config_exposes_only_the_public_site_key(self):
        response = self.client.get("/api/security/config")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["turnstileConfigured"])
        self.assertEqual(response.json["turnstileSiteKey"], "test-site-key")
        self.assertNotIn("TURNSTILE_SECRET_KEY", response.json)
        self.assertNotIn("test-secret-key", response.get_data(as_text=True))

    def test_turnstile_example_placeholders_do_not_enable_protection(self):
        placeholder_app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(self.temporary_directory.name) / "placeholder.sqlite"),
                "TURNSTILE_SITE_KEY": "replace-with-cloudflare-turnstile-site-key",
                "TURNSTILE_SECRET_KEY": "replace-with-cloudflare-turnstile-secret-key",
            }
        )

        config = placeholder_app.test_client().get("/api/security/config")

        self.assertFalse(config.json["turnstileConfigured"])
        self.assertEqual(config.json["turnstileSiteKey"], "")

    def test_staff_login_requires_turnstile_configuration(self):
        unconfigured_app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(self.temporary_directory.name) / "unconfigured.sqlite"),
                "SECRET_KEY": "test-secret",
                "SECRET_KEY_CONFIGURED": True,
                "CLINIC_STAFF_PASSWORD": "staff-password",
                "CLINIC_STAFF_PASSWORD_CONFIGURED": True,
                "TURNSTILE_SITE_KEY": "",
                "TURNSTILE_SECRET_KEY": "",
            }
        )
        response = unconfigured_app.test_client().post(
            "/api/staff/login",
            json={"password": "staff-password", "turnstileToken": "valid-test-token"},
        )

        self.assertEqual(response.status_code, 503)

    def test_patient_booking_requires_turnstile_configuration(self):
        self.verify_turnstile.side_effect = verify_turnstile
        unconfigured_app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(self.temporary_directory.name) / "patient-unconfigured.sqlite"),
                "TURNSTILE_SITE_KEY": "",
                "TURNSTILE_SECRET_KEY": "",
            }
        )
        response = unconfigured_app.test_client().post(
            "/api/appointments",
            json={"turnstileToken": "valid-test-token"},
        )

        self.assertEqual(response.status_code, 503)

    def test_turnstile_verification_checks_expected_action(self):
        valid_payload = b'{"success": true, "action": "patient_booking"}'
        with self.app.test_request_context("/api/appointments"):
            with patch("app.urlopen", return_value=io.BytesIO(valid_payload)) as send:
                verify_turnstile("valid-test-token", "patient_booking")

            request_sent = send.call_args.args[0]
            self.assertIn(b"secret=test-secret-key", request_sent.data)
            self.assertEqual(request_sent.get_method(), "POST")

        wrong_action = b'{"success": true, "action": "staff_login"}'
        with self.app.test_request_context("/api/appointments"):
            with patch("app.urlopen", return_value=io.BytesIO(wrong_action)):
                with self.assertRaises(ApiError):
                    verify_turnstile("valid-test-token", "patient_booking")

    def test_patient_booking_is_rate_limited(self):
        for _ in range(self.app.config["PATIENT_BOOKING_RATE_LIMIT"]):
            response = self.client.post(
                "/api/appointments",
                json={"turnstileToken": "valid-test-token"},
            )
            self.assertEqual(response.status_code, 400)

        limited = self.client.post(
            "/api/appointments",
            json={"turnstileToken": "valid-test-token"},
        )

        self.assertEqual(limited.status_code, 429)

    def test_staff_login_is_rate_limited(self):
        for _ in range(self.app.config["STAFF_LOGIN_RATE_LIMIT"]):
            response = self.client.post(
                "/api/staff/login",
                json={"password": "wrong-password", "turnstileToken": "valid-test-token"},
            )
            self.assertEqual(response.status_code, 401)

        limited = self.client.post(
            "/api/staff/login",
            json={"password": "wrong-password", "turnstileToken": "valid-test-token"},
        )

        self.assertEqual(limited.status_code, 429)

    def test_static_server_does_not_expose_database_files(self):
        response = self.client.get("/styles.css")
        self.assertEqual(response.status_code, 200)
        response.close()
        script = self.client.get("/turnstile.js")
        self.assertEqual(script.status_code, 200)
        script.close()
        self.assertEqual(self.client.get("/data/clinic.sqlite").status_code, 404)


if __name__ == "__main__":
    unittest.main()
