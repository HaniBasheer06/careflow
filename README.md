# Careflow clinic booking

Careflow is a Python Flask website with separate patient booking and clinic staff portals. Both use a shared SQLite database and the existing HTML, CSS, and browser JavaScript interface.

## Run on Windows

Python 3.10 or later is recommended. From the project folder:

```powershell
Copy-Item .env.example .env
notepad .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

In `.env`, replace `FLASK_SECRET_KEY` with a random value of at least 32 characters and `CLINIC_STAFF_PASSWORD` with a unique password of at least 12 characters. Set `OPENAI_API_KEY` to enable the AI doctor finder locally. Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`. Keep `.env` private; it is ignored by Git. Local `.env` values fill in environment variables that are not already set. Restart `python app.py` after changing `.env`. The app listens at `http://localhost:3018`; open `/` for patient bookings and `/clinic` for staff.

## Abuse protection and staff sign-in

Patient bookings, staff sign-ins, and AI chats use server-side validation and IP-based rate limits. These in-memory limits reset when the server restarts, so public deployments should also configure edge-level rate limiting.

The app does not use a CAPTCHA service. Staff sign-in requires the configured password, and staff sessions expire after 12 hours. Patients do not need an account; each confirmed appointment gets a private tracking link. Rate limits help reduce abuse but do not establish a patient's identity or guarantee that all bots are blocked.

## Current features

- Patients can browse active doctors, check date-specific availability, and book the next token.
- Token assignment is serialized and follows booking order. Estimates use each doctor's hours and average consultation duration.
- Patients can cancel from their private tracking link. Remaining active bookings are resequenced and receive in-app notices on their tracking links.
- Clinic staff can add, edit, list, and unlist doctors; change working days, clinic hours, and consultation estimates; book appointments for patients; and update appointment status.
- Staff can start and finish consultations in token order. The public `/waiting-room` display shows the current and waiting token numbers only; it does not expose patient names or contact details.
- The optional AI doctor finder uses the OpenAI Responses API to suggest a specialty and only doctors with available tokens on the selected date. It is not a diagnosis or treatment tool. The patient must consent before sending a chat; messages are not saved by this app, but are processed by OpenAI under its API terms. Do not enter identifying information. OpenAI API usage may incur charges.
- The database is stored at `data/clinic.sqlite`. The Python app uses the existing database file and preserves its records.

## Local-network use and privacy

To open the app on a phone connected to the same trusted Wi-Fi, find the computer's local IPv4 address with `ipconfig` and visit `http://<computer-ip>:3018`. The computer firewall may need to allow Python on the private network.

This is a local-network preview, not a production medical booking service. Use test data until you have appropriate HTTPS, session protection, backups, privacy controls, and a secure deployment. Do not expose the Flask development server directly to the public internet.

## Deployment

Deploy on a Linux Python host that supports a `Procfile`. Install dependencies with `pip install -r requirements.txt`; the included `Procfile` starts the app with Gunicorn and binds to the host-provided `PORT`. Set these environment variables in the host's secret/configuration settings:

- `FLASK_SECRET_KEY`: a random value of at least 32 characters.
- `CLINIC_STAFF_PASSWORD`: a unique password of at least 12 characters.
- `OPENAI_API_KEY`: an OpenAI API key, required to enable the AI doctor finder. Keep it in the host's secret manager, never in frontend code or Git.
- `OPENAI_MODEL`: optional OpenAI model name (defaults to `gpt-4o-mini`).
- `DB_FILE`: an absolute path on persistent, writable storage (for example, `/var/data/clinic.sqlite`).
- `COOKIE_SECURE=true` when serving the app over HTTPS.

The host's environment variables take precedence over `.env`. Without persistent storage configured for `DB_FILE`, SQLite data may be lost when the host replaces the app instance. Booking and staff sign-in do not depend on third-party CAPTCHA services. For a public deployment, configure edge-level rate limiting because the app's in-memory limits reset on restarts and are not shared across multiple workers.

### Fly.io persistence

The included `fly.toml` mounts a Fly volume at `/data` and points `DB_FILE` to `/data/clinic.sqlite`. SQLite is intended to run on one Fly machine with this setup.

Before deploying this configuration, create a 1 GB volume in the app's `ams` region named `clinic_data` if one does not already exist:

```powershell
fly volumes list --app careflow-aqhzzw
fly volumes create clinic_data --region ams --size 1 --app careflow-aqhzzw
fly scale count 1 --app careflow-aqhzzw
```

The Fly app must also have `FLASK_SECRET_KEY` and `CLINIC_STAFF_PASSWORD` set as Fly secrets. A newly created volume starts with a fresh database. If the current machine still has records you need, back up or migrate its database before deploying this configuration; do not assume the temporary machine disk will carry over.

To enable the AI doctor finder, add `OPENAI_API_KEY` as a Fly secret. The key must never be placed in `.env.example`, browser JavaScript, or a committed file. The AI chat is disabled until this secret is configured.
# careflow
