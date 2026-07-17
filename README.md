# Vytari

Diabetes monitoring and doctor-patient care coordination. Patients log blood glucose, insulin doses, meals (including photo-based carb estimation), and symptoms; doctors monitor patients through a dashboard with automatic pattern flagging, a versioned care plan, and live chat.

**Vytari never calculates or recommends insulin doses.** It logs what the patient did and surfaces patterns in the data; only the doctor interprets that data and sets or adjusts the care plan.

## Tech Stack

- Python 3 / Flask
- SQLite (`vytari.db`)
- Server-rendered Jinja templates, vanilla CSS (design tokens in `static/css/style.css`)
- No JS framework — plain `fetch` polling for chat

## Running Locally

```
python -m venv .venv
.venv\Scripts\activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python app.py
```

The app runs at `http://127.0.0.1:5000`. On first run it creates `vytari.db` and a `static/uploads/` folder automatically.

Seed a demo doctor + two demo patients with sample data:

```
python seed.py
```

### Environment Variables

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing key |
| `ADMIN_PASSWORD` | Password for the admin login (`/admin/login`), which onboards doctors |
| `ANTHROPIC_API_KEY` | Optional. Enables real AI photo-based carb estimation (Claude vision) for meal logging. Without it, the photo flow still works end-to-end using a labeled placeholder estimate the patient can adjust. |
| `FCM_SERVER_KEY` | Optional, stretch goal. Hook point for Firebase Cloud Messaging push on Emergency Alerts. In-app notifications (via polling) are the reliable delivery path already implemented; push requires this key plus per-doctor device token registration, which isn't built yet. |

Local-dev fallback values are used for `SECRET_KEY`/`ADMIN_PASSWORD` if unset — never rely on the fallbacks in production.

## User Roles

- **Admin** — onboards doctor accounts via `/admin/doctors` (password-protected, no public sign-up)
- **Doctor** — pre-registered by admin; creates patients, sets/versions care plans, monitors flags, messages patients, adds notes
- **Patient** — given a Patient Code + Doctor Code by their doctor; logs glucose/insulin/meals/symptoms, views their read-only care plan and trends, messages their doctor, and can raise an Emergency Alert

## Data Model

`doctors`, `patients`, `glucose_readings`, `insulin_logs`, `meal_logs` (photo + AI estimate kept separate from the patient-confirmed value), `symptom_logs`, `care_plans` (versioned, full history retained), `flags` (stores the specific rule/threshold that triggered each one), `messages`, `emergency_alerts`, `doctor_notes`, `notifications`.

## Deploying (Render)

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app` (see `Procfile`)
- Set `SECRET_KEY`, `ADMIN_PASSWORD`, and optionally `ANTHROPIC_API_KEY` / `FCM_SERVER_KEY` in the Render dashboard — never in code
- SQLite (`vytari.db`) needs a persistent disk mounted in Render to survive deploys/restarts; for a pilot beyond a handful of concurrent users, migrating to Render's managed PostgreSQL (via `DATABASE_URL` + `psycopg2`) is the natural next step but is not yet implemented — this build follows the same SQLite-first pattern as the reference Healio project.
