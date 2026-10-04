import os
import io
import sqlite3
import secrets
import string
import uuid
import psycopg2
import psycopg2.extras
import cloudinary
import cloudinary.uploader
import cloudinary.utils
from datetime import datetime, date, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify
from dotenv import load_dotenv

# Local dev: read secrets from .env. No-op on Render, where env vars are set in the dashboard.
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vytari-fallback-key-for-local-dev-only")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "vytari-fallback-admin-for-local-dev-only")
app.permanent_session_lifetime = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

DB_PATH = "vytari.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# Photo uploads (doctor/patient profile photos, AI meal-scan photos) default to local
# disk for dev. On Render that disk is ephemeral — wiped on every inactivity restart,
# same root cause the database had. Setting CLOUDINARY_URL switches storage to
# Cloudinary so uploaded photos survive restarts too.
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "").strip()
USE_CLOUDINARY = bool(CLOUDINARY_URL)
CLOUDINARY_FOLDER = "vytari"

GLUCOSE_TAGS = ["fasting", "post-meal", "bedtime", "random"]
INSULIN_TYPES = ["rapid-acting", "long-acting"]
INJECTION_SITES = ["Left Abdomen", "Right Abdomen", "Thigh"]
SYMPTOM_TAGS = ["dizziness", "excessive thirst", "fatigue", "blurred vision", "nausea", "irritability"]
SYMPTOM_ICONS = {
    "dizziness": "cyclone",
    "excessive thirst": "water_full",
    "fatigue": "battery_low",
    "blurred vision": "visibility_off",
    "nausea": "sick",
    "irritability": "mood_bad",
}


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.errorhandler(413)
def handle_file_too_large(e):
    flash("That file is too large (max 5MB).")
    return redirect(request.referrer or url_for("index"))


# ---------- Database ----------
#
# Two dialects are supported: SQLite for local dev (file on disk), and Postgres
# (Neon) in production when DATABASE_URL is set. Render's filesystem is ephemeral —
# a local SQLite file gets wiped every time the service spins down from inactivity
# and restarts, which silently loses all data. Postgres is durable storage instead.
#
# _PgCursor makes the Postgres path a drop-in replacement for sqlite3's behavior so
# none of the ~100 call sites elsewhere in this file need to change:
#   - "?" placeholders are translated to psycopg2's "%s" style
#   - rows come back dict-like (RealDictCursor), matching sqlite3.Row's row["col"] access
#   - datetime/date values are coerced to isoformat strings, matching how SQLite
#     stores/returns TIMESTAMP columns as plain text
#   - INSERT statements get "RETURNING id" appended automatically so cursor.lastrowid
#     works the same as it does under sqlite3


def _normalize_row(row):
    if row is None:
        return None
    for key, value in row.items():
        if isinstance(value, (datetime, date)):
            row[key] = value.isoformat()
    return row


class _PgCursor(psycopg2.extras.RealDictCursor):
    _lastrowid = None

    def execute(self, query, params=None):
        pg_query = query.replace("?", "%s")
        is_insert = pg_query.strip()[:6].upper() == "INSERT" and "RETURNING" not in pg_query.upper()
        if is_insert:
            pg_query = pg_query.rstrip().rstrip(";") + " RETURNING id"
        if params is None:
            super().execute(pg_query)
        else:
            super().execute(pg_query, params)
        self._lastrowid = None
        if is_insert:
            row = super().fetchone()
            self._lastrowid = row["id"] if row else None

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        return _normalize_row(super().fetchone())

    def fetchall(self):
        return [_normalize_row(r) for r in super().fetchall()]


def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=_PgCursor)
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database():
    conn = get_db()
    cursor = conn.cursor()

    # Postgres has no AUTOINCREMENT keyword (SERIAL is its equivalent) and requires
    # real boolean defaults to be TRUE/FALSE — but is_current/resolved are compared
    # against literal 0/1 all over this file (SQLite's boolean storage class), so on
    # Postgres they're kept as plain INTEGER to match that behavior exactly.
    id_col = "id SERIAL PRIMARY KEY" if USE_POSTGRES else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    bool_type = "INTEGER" if USE_POSTGRES else "BOOLEAN"

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS doctors (
            {id_col},
            doctor_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password TEXT NOT NULL,
            photo_filename TEXT,
            phone_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS patients (
            {id_col},
            patient_code TEXT UNIQUE NOT NULL,
            doctor_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            password TEXT NOT NULL,
            diabetes_type TEXT,
            age INTEGER,
            preferred_unit TEXT NOT NULL DEFAULT 'mg/dL',
            photo_filename TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS glucose_readings (
            {id_col},
            patient_id INTEGER NOT NULL,
            value REAL NOT NULL,
            unit TEXT NOT NULL,
            tag TEXT,
            reading_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS insulin_logs (
            {id_col},
            patient_id INTEGER NOT NULL,
            insulin_type TEXT NOT NULL,
            units REAL NOT NULL,
            injection_site TEXT,
            dose_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS meal_logs (
            {id_col},
            patient_id INTEGER NOT NULL,
            photo_filename TEXT,
            ai_estimated_carbs REAL,
            ai_description TEXT,
            patient_confirmed_carbs REAL,
            entry_method TEXT NOT NULL,
            description TEXT,
            logged_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS symptom_logs (
            {id_col},
            patient_id INTEGER NOT NULL,
            symptoms TEXT NOT NULL,
            notes TEXT,
            logged_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS care_plans (
            {id_col},
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            target_min REAL NOT NULL,
            target_max REAL NOT NULL,
            unit TEXT NOT NULL,
            insulin_regimen TEXT,
            basal_name TEXT,
            basal_description TEXT,
            basal_units TEXT,
            basal_frequency TEXT,
            bolus_name TEXT,
            bolus_description TEXT,
            bolus_ratio TEXT,
            instructions TEXT,
            version INTEGER NOT NULL,
            is_current {bool_type} NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS flags (
            {id_col},
            patient_id INTEGER NOT NULL,
            flag_type TEXT NOT NULL,
            flag_reason TEXT NOT NULL,
            resolved {bool_type} DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS messages (
            {id_col},
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            sender_type TEXT NOT NULL,
            message_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS emergency_alerts (
            {id_col},
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            message TEXT,
            latitude REAL,
            longitude REAL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            acknowledged_at TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS doctor_notes (
            {id_col},
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            note_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS notifications (
            {id_col},
            recipient_type TEXT NOT NULL,
            recipient_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            link TEXT,
            read_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def migrate_database():
    conn = get_db()
    cursor = conn.cursor()

    if USE_POSTGRES:
        # Postgres supports idempotent "IF NOT EXISTS" on ADD COLUMN directly, so no
        # need for SQLite's introspect-then-conditionally-ALTER dance below.
        cursor.execute("ALTER TABLE insulin_logs ADD COLUMN IF NOT EXISTS injection_site TEXT")
        for col in ("basal_name", "basal_description", "basal_units", "basal_frequency", "bolus_name", "bolus_description", "bolus_ratio"):
            cursor.execute(f"ALTER TABLE care_plans ADD COLUMN IF NOT EXISTS {col} TEXT")
        cursor.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS age INTEGER")
        cursor.execute("ALTER TABLE doctors ADD COLUMN IF NOT EXISTS phone_number TEXT")
        for col in ("latitude", "longitude"):
            cursor.execute(f"ALTER TABLE emergency_alerts ADD COLUMN IF NOT EXISTS {col} REAL")
        conn.commit()
        conn.close()
        return

    cursor.execute("PRAGMA table_info(insulin_logs)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "injection_site" not in columns:
        cursor.execute("ALTER TABLE insulin_logs ADD COLUMN injection_site TEXT")

    cursor.execute("PRAGMA table_info(care_plans)")
    cp_columns = {row["name"] for row in cursor.fetchall()}
    for col in ("basal_name", "basal_description", "basal_units", "basal_frequency", "bolus_name", "bolus_description", "bolus_ratio"):
        if col not in cp_columns:
            cursor.execute(f"ALTER TABLE care_plans ADD COLUMN {col} TEXT")

    cursor.execute("PRAGMA table_info(patients)")
    patient_columns = {row["name"] for row in cursor.fetchall()}
    if "age" not in patient_columns:
        cursor.execute("ALTER TABLE patients ADD COLUMN age INTEGER")

    cursor.execute("PRAGMA table_info(doctors)")
    doctor_columns = {row["name"] for row in cursor.fetchall()}
    if "phone_number" not in doctor_columns:
        cursor.execute("ALTER TABLE doctors ADD COLUMN phone_number TEXT")

    cursor.execute("PRAGMA table_info(emergency_alerts)")
    alert_columns = {row["name"] for row in cursor.fetchall()}
    for col in ("latitude", "longitude"):
        if col not in alert_columns:
            cursor.execute(f"ALTER TABLE emergency_alerts ADD COLUMN {col} REAL")

    conn.commit()
    conn.close()


# ---------- Helpers ----------

def generate_code(prefix, length=6):
    chars = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(chars) for _ in range(length))


def generate_password(length=10):
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def relative_time(dt_str):
    """Short 'Xh ago' style label. dt_str is a naive local-time ISO string."""
    if not dt_str:
        return None
    dt = datetime.fromisoformat(dt_str)
    seconds = (datetime.now() - dt).total_seconds()
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def format_chat_messages(messages):
    """Attaches display-friendly time/date-divider info to a chronological message list.
    messages.created_at is SQLite's CURRENT_TIMESTAMP (UTC), so "today"/"yesterday" are
    computed in UTC too — comparing against local date() would misclassify messages sent
    near local midnight when the UTC offset crosses a day boundary."""
    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    formatted = []
    last_date = None
    for m in messages:
        row = dict(m)
        dt = datetime.fromisoformat(row["created_at"])
        row["time_display"] = dt.strftime("%I:%M %p").lstrip("0")
        msg_date = dt.date()
        if msg_date != last_date:
            row["date_label"] = "Today" if msg_date == today else ("Yesterday" if msg_date == yesterday else f"{dt.strftime('%B')} {dt.day}, {dt.year}")
            last_date = msg_date
        else:
            row["date_label"] = None
        formatted.append(row)
    return formatted


def mg_dl(value, unit):
    """Convert a glucose value to mg/dL for internal comparisons (flagging, TIR)."""
    return value * 18.0182 if unit == "mmol/L" else value


def compute_gmi(avg_mgdl):
    """Glucose Management Indicator — a standard, published estimated-A1c formula
    (Bergenstal et al. 2018): GMI% = 3.31 + 0.02392 x mean glucose (mg/dL)."""
    if avg_mgdl is None:
        return None
    return round(3.31 + 0.02392 * avg_mgdl, 1)


def build_gauge(pct, radius=45):
    """Stroke-dashoffset for an SVG circular progress ring."""
    if pct is None:
        return None
    circumference = round(2 * 3.14159265 * radius, 1)
    offset = round(circumference * (1 - max(0, min(100, pct)) / 100), 1)
    return {"circumference": circumference, "offset": offset}


def build_bar_chart(readings, care_plan, bucket_count, span_hours):
    """Buckets readings into evenly-spaced time segments for a simple bar chart,
    scaled against a fixed 40-300 mg/dL clinical display range."""
    cutoff = datetime.now() - timedelta(hours=span_hours)
    in_window = [r for r in readings if datetime.fromisoformat(r["reading_at"]) >= cutoff]
    if not in_window:
        return None

    target_min_mgdl = mg_dl(care_plan["target_min"], care_plan["unit"]) if care_plan else None
    target_max_mgdl = mg_dl(care_plan["target_max"], care_plan["unit"]) if care_plan else None

    bucket_seconds = span_hours * 3600 / bucket_count
    buckets = [[] for _ in range(bucket_count)]
    now = datetime.now()
    for r in in_window:
        age_seconds = max(0, (now - datetime.fromisoformat(r["reading_at"])).total_seconds())
        idx = bucket_count - 1 - min(bucket_count - 1, int(age_seconds // bucket_seconds))
        idx = max(0, min(bucket_count - 1, idx))
        buckets[idx].append(mg_dl(r["value"], r["unit"]))

    display_min, display_max = 40, 300
    bars = []
    for vals in buckets:
        if not vals:
            bars.append(None)
            continue
        avg = sum(vals) / len(vals)
        height_pct = round(max(8, min(100, (avg - display_min) / (display_max - display_min) * 100)), 1)
        in_range = (target_min_mgdl <= avg <= target_max_mgdl) if target_min_mgdl is not None else True
        bars.append({"height_pct": height_pct, "in_range": in_range, "value": round(avg)})
    return bars


def build_range_bar(care_plan):
    """Proportions a 3-segment low/target/high bar between fixed clinical
    critical thresholds (55-250 mg/dL) around the doctor-set target range."""
    if not care_plan:
        return None
    critical_low_mgdl, critical_high_mgdl = 55.0, 250.0
    target_min_mgdl = mg_dl(care_plan["target_min"], care_plan["unit"])
    target_max_mgdl = mg_dl(care_plan["target_max"], care_plan["unit"])
    target_min_mgdl = max(target_min_mgdl, critical_low_mgdl)
    target_max_mgdl = min(target_max_mgdl, critical_high_mgdl)

    span = critical_high_mgdl - critical_low_mgdl
    low_pct = round((target_min_mgdl - critical_low_mgdl) / span * 100, 1)
    mid_pct = round((target_max_mgdl - target_min_mgdl) / span * 100, 1)
    high_pct = round(100 - low_pct - mid_pct, 1)

    if care_plan["unit"] == "mmol/L":
        critical_low_display, critical_high_display = round(critical_low_mgdl / 18.0182, 1), round(critical_high_mgdl / 18.0182, 1)
    else:
        critical_low_display, critical_high_display = round(critical_low_mgdl), round(critical_high_mgdl)

    return {
        "low_pct": low_pct, "mid_pct": mid_pct, "high_pct": high_pct,
        "critical_low": critical_low_display, "critical_high": critical_high_display,
    }


def build_glucose_chart(readings, care_plan, width=800, height=200, pad=20):
    """Builds coordinates for a simple index-spaced SVG line chart, converting
    all values to mg/dL so mixed-unit readings plot on one consistent scale."""
    if not readings:
        return None

    values_mgdl = [mg_dl(r["value"], r["unit"]) for r in readings]
    band = None
    if care_plan:
        band = (mg_dl(care_plan["target_min"], care_plan["unit"]), mg_dl(care_plan["target_max"], care_plan["unit"]))

    all_vals = values_mgdl + ([band[0], band[1]] if band else [])
    y_min = min(all_vals) - 20
    y_max = max(all_vals) + 20
    if y_max - y_min < 40:
        y_max = y_min + 40

    n = len(readings)

    def x_at(i):
        return pad if n == 1 else pad + i * (width - 2 * pad) / (n - 1)

    def y_at(v):
        return height - pad - (v - y_min) / (y_max - y_min) * (height - 2 * pad)

    points = []
    for i, (r, v) in enumerate(zip(readings, values_mgdl)):
        points.append({
            "x": round(x_at(i), 1),
            "y": round(y_at(v), 1),
            "value": r["value"],
            "unit": r["unit"],
            "in_range": (band[0] <= v <= band[1]) if band else None,
            "reading_at": r["reading_at"],
        })

    band_rect = None
    if band:
        band_rect = {"y": round(y_at(band[1]), 1), "height": round(y_at(band[0]) - y_at(band[1]), 1)}

    polyline = " ".join(f"{p['x']},{p['y']}" for p in points)
    area_path = f"M{points[0]['x']},{points[0]['y']} L" + " L".join(f"{p['x']},{p['y']}" for p in points[1:]) + f" L{points[-1]['x']},{height} L{points[0]['x']},{height} Z"

    return {
        "width": width,
        "height": height,
        "points": points,
        "polyline": polyline,
        "area_path": area_path,
        "band": band_rect,
    }


def build_sparkline(readings, width=100, height=30):
    """Small linear sparkline (points string) for the last few glucose readings, in mg/dL."""
    if not readings:
        return None
    values = [mg_dl(r["value"], r["unit"]) for r in readings]
    y_min, y_max = min(values), max(values)
    if y_max - y_min < 20:
        y_max = y_min + 20
    n = len(values)

    def x_at(i):
        return 0 if n == 1 else i * width / (n - 1)

    def y_at(v):
        return height - (v - y_min) / (y_max - y_min) * height

    return " ".join(f"{round(x_at(i), 1)},{round(y_at(v), 1)}" for i, v in enumerate(values))


def estimate_carbs_from_photo(image_bytes, ext):
    """Returns (food_description, estimated_carbs_grams) for a meal photo — just
    the food phrase (e.g. "a medium portion of rice with stew"), not a full
    sentence, so the caller can build the exact "This looks like <food> —
    roughly <N>g of carbs" display format reliably instead of guessing.

    Takes raw image bytes (not a path) so it works the same whether the photo
    ends up on local disk or in Cloudinary — the caller reads the upload once
    and reuses those bytes for both storage and this vision call.

    Uses Claude vision when ANTHROPIC_API_KEY is set; otherwise falls back to a
    clearly-labeled placeholder estimate so the photo-logging flow still works
    end-to-end without an API key configured.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic
            import base64
            import json

            image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
            # Trust the bytes over the filename — phones often save a PNG/WEBP with a
            # .jpg name, and the API rejects a mismatched media type.
            if image_bytes[:3] == b"\xff\xd8\xff":
                media_type = "image/jpeg"
            elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                media_type = "image/png"
            elif image_bytes[:4] == b"GIF8":
                media_type = "image/gif"
            elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
                media_type = "image/webp"
            else:
                media_type = f"image/{'jpeg' if ext == 'jpg' else ext}"

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                        {"type": "text", "text": (
                            "You are helping a diabetes patient log a meal. Look at this food photo and "
                            "respond with ONLY JSON, no other text: "
                            '{"food_description": "<a short plain-language noun phrase for the food and '
                            'portion size, e.g. \'a medium portion of rice with stew\' — this will be '
                            'slotted into the sentence \'This looks like <food_description> — roughly Ng '
                            'of carbs.\', so phrase it to fit grammatically>", "estimated_carbs_grams": '
                            "<number>}. Keep language simple and non-technical. This is a rough estimate "
                            "only, not medical advice."
                        )},
                    ],
                }],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.strip("`").split("\n", 1)[-1]
            data = json.loads(text)
            return data["food_description"], round(float(data["estimated_carbs_grams"]), 1)
        except Exception:
            # Log why and say so in the placeholder — a bad key or API
            # error otherwise looks identical to "no key configured".
            app.logger.exception("Claude carb estimate failed")
            return (
                "a meal (the AI photo estimate didn't work this time — please enter the carbs yourself)",
                45.0,
            )

    return (
        "a meal (AI photo estimate isn't connected yet — set ANTHROPIC_API_KEY to enable it)",
        45.0,
    )


def save_uploaded_photo(file_storage, old_filename=None):
    """Returns (stored_key, error, raw_bytes, ext). stored_key is a Cloudinary
    public_id when USE_CLOUDINARY, else a local filename under UPLOAD_FOLDER —
    photo_url() and this function are the only places that need to know which."""
    if not file_storage or not file_storage.filename:
        return None, "No file selected.", None, None

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_PHOTO_EXTENSIONS:
        return None, "Unsupported file type. Use PNG, JPG, GIF, or WEBP.", None, None

    raw_bytes = file_storage.read()
    key = uuid.uuid4().hex

    if USE_CLOUDINARY:
        public_id = f"{CLOUDINARY_FOLDER}/{key}"
        cloudinary.uploader.upload(io.BytesIO(raw_bytes), public_id=public_id, resource_type="image", overwrite=True)
        if old_filename:
            try:
                cloudinary.uploader.destroy(old_filename, resource_type="image")
            except Exception:
                pass
        return public_id, None, raw_bytes, ext

    filename = secure_filename(f"{key}.{ext}")
    with open(os.path.join(UPLOAD_FOLDER, filename), "wb") as f:
        f.write(raw_bytes)

    if old_filename:
        old_path = os.path.join(UPLOAD_FOLDER, old_filename)
        if os.path.exists(old_path):
            os.remove(old_path)

    return filename, None, raw_bytes, ext


def photo_url(stored_key):
    """Builds a displayable URL from whatever save_uploaded_photo() returned as
    stored_key — a Cloudinary public_id or a local uploads/ filename."""
    if not stored_key:
        return None
    if USE_CLOUDINARY:
        url, _ = cloudinary.utils.cloudinary_url(stored_key, secure=True)
        return url
    return url_for("static", filename="uploads/" + stored_key)


app.jinja_env.globals["photo_url"] = photo_url


def login_required_doctor(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "doctor_id" not in session:
            return redirect(url_for("doctor_login"))
        return f(*args, **kwargs)
    return decorated


def login_required_patient(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "patient_id" not in session:
            return redirect(url_for("patient_login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def send_push_notification(doctor_id, title, body):
    """Hook point for Firebase Cloud Messaging push delivery.

    Sending a real push requires an FCM server key plus a registered device
    token per doctor (collected via a browser/mobile push-subscription flow),
    neither of which exist yet in this build. When FCM_SERVER_KEY is set this
    still no-ops safely — wiring up device token registration is the
    remaining piece to make this a real push. In-app notifications (which are
    real right now, via the notifications table + polling) are the reliable
    delivery path until then.
    """
    if not os.environ.get("FCM_SERVER_KEY"):
        return
    # TODO: once device tokens are collected, POST to FCM here.


def create_notification(recipient_type, recipient_id, message, link=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO notifications (recipient_type, recipient_id, message, link)
        VALUES (?, ?, ?, ?)
    """, (recipient_type, recipient_id, message, link))
    conn.commit()
    conn.close()


def check_and_create_flags(patient_id):
    """Surfaces patterns in a patient's recent data for the doctor to review.
    Never decides or suggests treatment — only flags, with the specific rule
    and numbers that triggered it, so the doctor can judge quickly."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT name, doctor_id FROM patients WHERE id = ?", (patient_id,))
    patient_row = cursor.fetchone()
    patient_name, doctor_id = patient_row["name"], patient_row["doctor_id"]

    cursor.execute("""
        SELECT * FROM care_plans WHERE patient_id = ? AND is_current = 1 ORDER BY version DESC LIMIT 1
    """, (patient_id,))
    care_plan = cursor.fetchone()

    def raise_flag(flag_type, reason):
        cursor.execute("""
            SELECT id FROM flags WHERE patient_id = ? AND flag_type = ? AND resolved = 0
        """, (patient_id, flag_type))
        if cursor.fetchone():
            return
        cursor.execute("""
            INSERT INTO flags (patient_id, flag_type, flag_reason) VALUES (?, ?, ?)
        """, (patient_id, flag_type, reason))
        cursor.execute("""
            INSERT INTO notifications (recipient_type, recipient_id, message, link)
            VALUES ('doctor', ?, ?, ?)
        """, (doctor_id, f"New flag for {patient_name}: {reason}", url_for("patient_detail", patient_id=patient_id)))

    since = (datetime.now() - timedelta(days=7)).isoformat()

    if care_plan:
        target_min_mgdl = mg_dl(care_plan["target_min"], care_plan["unit"])
        target_max_mgdl = mg_dl(care_plan["target_max"], care_plan["unit"])

        cursor.execute("""
            SELECT value, unit, reading_at FROM glucose_readings
            WHERE patient_id = ? AND reading_at >= ? ORDER BY reading_at ASC
        """, (patient_id, since))
        recent_readings = cursor.fetchall()

        hypo = [r for r in recent_readings if mg_dl(r["value"], r["unit"]) < target_min_mgdl]
        if len(hypo) >= 3:
            detail = ", ".join(f"{r['value']} {r['unit']} on {r['reading_at']}" for r in hypo[-3:])
            raise_flag("hypoglycemia", f"{len(hypo)} readings below the target minimum of {care_plan['target_min']} {care_plan['unit']} in the last 7 days: {detail}")

        hyper = [r for r in recent_readings if mg_dl(r["value"], r["unit"]) > target_max_mgdl]
        if len(hyper) >= 3:
            detail = ", ".join(f"{r['value']} {r['unit']} on {r['reading_at']}" for r in hyper[-3:])
            raise_flag("hyperglycemia", f"{len(hyper)} readings above the target maximum of {care_plan['target_max']} {care_plan['unit']} in the last 7 days: {detail}")

        if len(recent_readings) >= 2:
            max_swing = 0
            swing_pair = None
            for a, b in zip(recent_readings, recent_readings[1:]):
                swing = abs(mg_dl(b["value"], b["unit"]) - mg_dl(a["value"], a["unit"]))
                if swing > max_swing:
                    max_swing, swing_pair = swing, (a, b)
            if max_swing >= 100:
                a, b = swing_pair
                raise_flag("variability", f"Wide swing of {round(max_swing)} mg/dL between consecutive readings: {a['value']} {a['unit']} on {a['reading_at']} then {b['value']} {b['unit']} on {b['reading_at']}")

        if care_plan["insulin_regimen"]:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            day_before = (date.today() - timedelta(days=2)).isoformat()
            cursor.execute("""
                SELECT DISTINCT date(dose_at) as d FROM insulin_logs
                WHERE patient_id = ? AND date(dose_at) IN (?, ?)
            """, (patient_id, yesterday, day_before))
            logged_days = {row["d"] for row in cursor.fetchall()}
            if yesterday not in logged_days and day_before not in logged_days:
                raise_flag("missed_insulin", f"No insulin doses logged for 2 consecutive days ({day_before}, {yesterday}) despite an active insulin regimen")

    conn.commit()
    conn.close()


@app.context_processor
def inject_notifications():
    recipient_type = recipient_id = None
    if "doctor_id" in session:
        recipient_type, recipient_id = "doctor", session["doctor_id"]
    elif "patient_id" in session:
        recipient_type, recipient_id = "patient", session["patient_id"]

    if not recipient_type:
        return {"unread_notifications_count": 0, "unread_messages_count": 0}

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) as c FROM notifications
        WHERE recipient_type = ? AND recipient_id = ? AND read_at IS NULL
    """, (recipient_type, recipient_id))
    notif_count = cursor.fetchone()["c"]

    if recipient_type == "doctor":
        cursor.execute("""
            SELECT COUNT(*) as c FROM messages m
            JOIN patients p ON m.patient_id = p.id
            WHERE p.doctor_id = ? AND m.sender_type = 'patient' AND m.read_at IS NULL
        """, (recipient_id,))
    else:
        cursor.execute("""
            SELECT COUNT(*) as c FROM messages
            WHERE patient_id = ? AND sender_type = 'doctor' AND read_at IS NULL
        """, (recipient_id,))
    msg_count = cursor.fetchone()["c"]
    conn.close()
    return {"unread_notifications_count": notif_count, "unread_messages_count": msg_count}


# ---------- Public / Welcome ----------

@app.route("/")
def index():
    return render_template("welcome.html")


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(app.static_folder, "service-worker.js")


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/emergency-info")
def emergency_info():
    return render_template("emergency_info.html")


# ---------- Admin ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_doctors"))
        flash("Incorrect admin password.")
    return render_template("admin_login.html")


@app.route("/admin/doctors", methods=["GET", "POST"])
@admin_required
def admin_doctors():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        if name:
            doctor_code = generate_code("DR-")
            password = generate_password()
            cursor.execute("""
                INSERT INTO doctors (doctor_code, name, password, phone_number) VALUES (?, ?, ?, ?)
            """, (doctor_code, name, password, phone_number or None))
            conn.commit()
            flash(f"Doctor added — Code: {doctor_code} | Password: {password} (save this, it won't be shown again in full)")

    cursor.execute("SELECT id, doctor_code, name, phone_number, created_at FROM doctors ORDER BY created_at DESC")
    doctors = cursor.fetchall()
    conn.close()
    return render_template("admin_doctors.html", doctors=doctors)


# ---------- Doctor: auth & core ----------

@app.route("/doctor/login", methods=["GET", "POST"])
def doctor_login():
    if request.method == "POST":
        doctor_code = request.form.get("doctor_code", "").strip().upper()
        password = request.form.get("password", "")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM doctors WHERE doctor_code = ?", (doctor_code,))
        doctor = cursor.fetchone()
        conn.close()

        if doctor and doctor["password"] == password:
            session.permanent = request.form.get("keep_signed_in") == "yes"
            session["doctor_id"] = doctor["id"]
            session["doctor_name"] = doctor["name"]
            session["doctor_photo"] = doctor["photo_filename"]
            return redirect(url_for("doctor_dashboard"))
        flash("Invalid doctor code or password.")
    return render_template("doctor_login.html")


@app.route("/doctor/dashboard")
@login_required_doctor
def doctor_dashboard():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.patient_code, p.name, p.diabetes_type, p.photo_filename,
        (SELECT COUNT(*) FROM flags f WHERE f.patient_id = p.id AND f.resolved = 0) as active_flags,
        (SELECT value FROM glucose_readings g WHERE g.patient_id = p.id ORDER BY g.reading_at DESC LIMIT 1) as latest_value,
        (SELECT unit FROM glucose_readings g WHERE g.patient_id = p.id ORDER BY g.reading_at DESC LIMIT 1) as latest_unit,
        (SELECT reading_at FROM glucose_readings g WHERE g.patient_id = p.id ORDER BY g.reading_at DESC LIMIT 1) as latest_reading_at
        FROM patients p WHERE p.doctor_id = ?
        ORDER BY active_flags DESC, latest_reading_at DESC
    """, (session["doctor_id"],))
    patients = [dict(p) for p in cursor.fetchall()]
    for p in patients:
        p["last_log_display"] = relative_time(p["latest_reading_at"])

    cursor.execute("""
        SELECT ea.*, p.name as patient_name, p.photo_filename as patient_photo FROM emergency_alerts ea
        JOIN patients p ON ea.patient_id = p.id
        WHERE ea.doctor_id = ? AND ea.status = 'active'
        ORDER BY ea.created_at DESC
    """, (session["doctor_id"],))
    active_alerts = cursor.fetchall()

    since_this_week = (datetime.now() - timedelta(days=7)).isoformat()
    since_last_week = (datetime.now() - timedelta(days=14)).isoformat()
    this_week_hypo, last_week_hypo = 0, 0
    for p in patients:
        cursor.execute("""
            SELECT * FROM care_plans WHERE patient_id = ? AND is_current = 1 ORDER BY version DESC LIMIT 1
        """, (p["id"],))
        cp = cursor.fetchone()
        if not cp:
            continue
        target_min_mgdl = mg_dl(cp["target_min"], cp["unit"])
        cursor.execute("""
            SELECT value, unit FROM glucose_readings WHERE patient_id = ? AND reading_at >= ? AND reading_at < ?
        """, (p["id"], since_this_week, datetime.now().isoformat()))
        this_week_hypo += sum(1 for r in cursor.fetchall() if mg_dl(r["value"], r["unit"]) < target_min_mgdl)
        cursor.execute("""
            SELECT value, unit FROM glucose_readings WHERE patient_id = ? AND reading_at >= ? AND reading_at < ?
        """, (p["id"], since_last_week, since_this_week))
        last_week_hypo += sum(1 for r in cursor.fetchall() if mg_dl(r["value"], r["unit"]) < target_min_mgdl)
    conn.close()

    trend_insight = None
    if last_week_hypo > 0:
        pct_change = round(abs(this_week_hypo - last_week_hypo) / last_week_hypo * 100)
        if pct_change >= 3:
            direction = "decrease" if this_week_hypo < last_week_hypo else "increase"
            trend_insight = f"There's been a {pct_change}% {direction} in hypoglycemic readings across your patient base this week, compared to last week."

    total_patients = len(patients)
    flagged_count = sum(1 for p in patients if p["active_flags"] > 0)

    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
    initials = "".join(part[0].upper() for part in session["doctor_name"].replace("Dr.", "").split()[:2])

    return render_template(
        "doctor_dashboard.html",
        patients=patients,
        total_patients=total_patients,
        flagged_count=flagged_count,
        active_alerts=active_alerts,
        trend_insight=trend_insight,
        initials=initials,
        greeting=greeting,
        active="home",
    )


@app.route("/doctor/emergency-alerts/<int:alert_id>/acknowledge", methods=["POST"])
@login_required_doctor
def acknowledge_alert(alert_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE emergency_alerts SET status = 'acknowledged', acknowledged_at = CURRENT_TIMESTAMP
        WHERE id = ? AND doctor_id = ?
    """, (alert_id, session["doctor_id"]))
    conn.commit()
    conn.close()
    flash("Alert acknowledged.")
    return redirect(url_for("doctor_dashboard"))


@app.route("/doctor/patients")
@login_required_doctor
def doctor_patients():
    conn = get_db()
    cursor = conn.cursor()
    q = request.args.get("q", "").strip()
    if q:
        cursor.execute("""
            SELECT p.id, p.patient_code, p.name, p.diabetes_type, p.photo_filename,
            (SELECT COUNT(*) FROM flags f WHERE f.patient_id = p.id AND f.resolved = 0) as active_flags
            FROM patients p WHERE p.doctor_id = ? AND (p.name LIKE ? OR p.patient_code LIKE ?)
            ORDER BY active_flags DESC, p.created_at DESC
        """, (session["doctor_id"], f"%{q}%", f"%{q}%"))
    else:
        cursor.execute("""
            SELECT p.id, p.patient_code, p.name, p.diabetes_type, p.photo_filename,
            (SELECT COUNT(*) FROM flags f WHERE f.patient_id = p.id AND f.resolved = 0) as active_flags
            FROM patients p WHERE p.doctor_id = ?
            ORDER BY active_flags DESC, p.created_at DESC
        """, (session["doctor_id"],))
    patients = cursor.fetchall()
    conn.close()
    return render_template("doctor_patients.html", patients=patients, q=q, active="patients")


@app.route("/doctor/patients/<int:patient_id>", methods=["GET", "POST"])
@login_required_doctor
def patient_detail(patient_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    if request.method == "POST":
        note_text = request.form.get("note_text", "").strip()
        if note_text:
            cursor.execute("""
                INSERT INTO doctor_notes (patient_id, doctor_id, note_text) VALUES (?, ?, ?)
            """, (patient_id, session["doctor_id"], note_text))
            conn.commit()
            create_notification(
                "patient", patient_id,
                f"{session['doctor_name']} added a new note to your file",
                url_for("patient_dashboard"),
            )

    cursor.execute("""
        SELECT * FROM flags WHERE patient_id = ? AND resolved = 0 ORDER BY created_at DESC
    """, (patient_id,))
    active_flags = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM care_plans WHERE patient_id = ? ORDER BY version DESC
    """, (patient_id,))
    care_plans = cursor.fetchall()
    care_plan = care_plans[0] if care_plans else None

    cursor.execute("""
        SELECT * FROM glucose_readings WHERE patient_id = ? ORDER BY reading_at ASC LIMIT 200
    """, (patient_id,))
    readings = cursor.fetchall()

    cursor.execute("""
        SELECT dn.*, d.name as doctor_name FROM doctor_notes dn
        JOIN doctors d ON dn.doctor_id = d.id
        WHERE dn.patient_id = ? ORDER BY dn.created_at DESC
    """, (patient_id,))
    notes = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM insulin_logs WHERE patient_id = ? ORDER BY dose_at DESC LIMIT 5
    """, (patient_id,))
    recent_insulin = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM meal_logs WHERE patient_id = ? ORDER BY logged_at DESC LIMIT 5
    """, (patient_id,))
    recent_meals = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM symptom_logs WHERE patient_id = ? ORDER BY logged_at DESC LIMIT 5
    """, (patient_id,))
    recent_symptoms = cursor.fetchall()

    conn.close()

    chart = build_glucose_chart(readings, care_plan)
    tir_pct = None
    avg_glucose_display = None
    gmi = None
    if readings:
        values_mgdl = [mg_dl(r["value"], r["unit"]) for r in readings]
        avg_mgdl = sum(values_mgdl) / len(values_mgdl)
        avg_glucose_display = round(avg_mgdl)
        gmi = compute_gmi(avg_mgdl)
        if care_plan:
            target_min_mgdl = mg_dl(care_plan["target_min"], care_plan["unit"])
            target_max_mgdl = mg_dl(care_plan["target_max"], care_plan["unit"])
            in_range = sum(1 for v in values_mgdl if target_min_mgdl <= v <= target_max_mgdl)
            tir_pct = round(100 * in_range / len(readings))
    gauge = build_gauge(tir_pct)

    view = request.args.get("view", "24H")
    if view not in ("24H", "7D"):
        view = "24H"
    span_hours = 24 if view == "24H" else 24 * 7
    bar_chart = build_bar_chart(readings, care_plan, bucket_count=8, span_hours=span_hours)
    if view == "24H":
        bar_labels = ["00:00", "06:00", "12:00", "18:00", "23:59"]
    else:
        bar_labels = [(date.today() - timedelta(days=d)).strftime("%a").upper() for d in range(6, -1, -1)]

    insulin_history = []
    for i in recent_insulin:
        row = dict(i)
        row["time_display"] = datetime.fromisoformat(row["dose_at"]).strftime("%I:%M %p").lstrip("0")
        row["category"] = "Basal" if row["insulin_type"] == "long-acting" else "Bolus"
        insulin_history.append(row)

    return render_template(
        "patient_detail.html",
        patient=patient,
        active_flags=active_flags,
        care_plan=care_plan,
        chart=chart,
        tir_pct=tir_pct,
        gauge=gauge,
        avg_glucose_display=avg_glucose_display,
        gmi=gmi,
        view=view,
        bar_chart=bar_chart,
        bar_labels=bar_labels,
        notes=notes,
        recent_insulin=insulin_history,
        recent_meals=recent_meals,
        recent_symptoms=recent_symptoms,
        active="patients",
    )


@app.route("/doctor/flags/<int:flag_id>/resolve", methods=["POST"])
@login_required_doctor
def resolve_flag(flag_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE flags SET resolved = 1 WHERE id = ?
        AND patient_id IN (SELECT id FROM patients WHERE doctor_id = ?)
    """, (flag_id, session["doctor_id"]))
    conn.commit()
    patient_id = request.form.get("patient_id")
    conn.close()
    flash("Flag marked resolved.")
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/doctor/patients/<int:patient_id>/care-plan/edit", methods=["GET", "POST"])
@login_required_doctor
def edit_care_plan(patient_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    cursor.execute("""
        SELECT * FROM care_plans WHERE patient_id = ? ORDER BY version DESC LIMIT 1
    """, (patient_id,))
    current = cursor.fetchone()

    if request.method == "POST":
        target_min = request.form.get("target_min", "").strip()
        target_max = request.form.get("target_max", "").strip()
        unit = request.form.get("unit", "mg/dL")
        basal_name = request.form.get("basal_name", "").strip()
        basal_description = request.form.get("basal_description", "").strip()
        basal_units = request.form.get("basal_units", "").strip()
        basal_frequency = request.form.get("basal_frequency", "").strip()
        bolus_name = request.form.get("bolus_name", "").strip()
        bolus_description = request.form.get("bolus_description", "").strip()
        bolus_ratio = request.form.get("bolus_ratio", "").strip()
        instructions = request.form.get("instructions", "").strip()

        error = None
        try:
            target_min = float(target_min)
            target_max = float(target_max)
            if target_min <= 0 or target_max <= 0 or target_min >= target_max:
                error = "Enter a valid range where the minimum is less than the maximum."
        except ValueError:
            error = "Enter valid numbers for the target range."
        if unit not in ("mg/dL", "mmol/L"):
            unit = "mg/dL"

        if error:
            flash(error)
        else:
            next_version = (current["version"] + 1) if current else 1
            cursor.execute("UPDATE care_plans SET is_current = 0 WHERE patient_id = ?", (patient_id,))
            cursor.execute("""
                INSERT INTO care_plans (
                    patient_id, doctor_id, target_min, target_max, unit,
                    basal_name, basal_description, basal_units, basal_frequency,
                    bolus_name, bolus_description, bolus_ratio,
                    instructions, version, is_current
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                patient_id, session["doctor_id"], target_min, target_max, unit,
                basal_name, basal_description, basal_units, basal_frequency,
                bolus_name, bolus_description, bolus_ratio,
                instructions, next_version,
            ))
            conn.commit()
            conn.close()
            create_notification(
                "patient", patient_id,
                f"{session['doctor_name']} updated your care plan",
                url_for("patient_dashboard"),
            )
            flash("Care plan updated.")
            return redirect(url_for("patient_detail", patient_id=patient_id))

    conn.close()
    return render_template("edit_care_plan.html", patient=patient, current=current, active="patients")


@app.route("/doctor/patients/new", methods=["GET", "POST"])
@login_required_doctor
def new_patient():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        diabetes_type = request.form.get("diabetes_type", "").strip()
        age = request.form.get("age", "").strip()
        age = int(age) if age.isdigit() and 0 < int(age) < 130 else None
        preferred_unit = request.form.get("preferred_unit", "mg/dL")
        if preferred_unit not in ("mg/dL", "mmol/L"):
            preferred_unit = "mg/dL"

        if name:
            patient_code = generate_code("PT-")
            password = generate_password()

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO patients (patient_code, doctor_id, name, password, diabetes_type, age, preferred_unit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (patient_code, session["doctor_id"], name, password, diabetes_type, age, preferred_unit))
            conn.commit()
            conn.close()

            flash(f"Patient added — Code: {patient_code} | Password: {password} (share these with the patient securely)")
            return redirect(url_for("doctor_dashboard"))

    return render_template("new_patient.html", active="add")


@app.route("/doctor/profile")
@login_required_doctor
def doctor_profile():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM doctors WHERE id = ?", (session["doctor_id"],))
    doctor = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) as c FROM patients WHERE doctor_id = ?", (session["doctor_id"],))
    patient_count = cursor.fetchone()["c"]
    conn.close()
    return render_template("doctor_profile.html", doctor=doctor, patient_count=patient_count, active="profile")


@app.route("/doctor/profile/photo", methods=["POST"])
@login_required_doctor
def doctor_profile_photo():
    filename, error, _, _ = save_uploaded_photo(request.files.get("photo"), session.get("doctor_photo"))
    if error:
        flash(error)
    else:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE doctors SET photo_filename = ? WHERE id = ?", (filename, session["doctor_id"]))
        conn.commit()
        conn.close()
        session["doctor_photo"] = filename
        flash("Profile photo updated.")
    return redirect(url_for("doctor_profile"))


@app.route("/doctor/notifications")
@login_required_doctor
def doctor_notifications():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM notifications WHERE recipient_type = 'doctor' AND recipient_id = ?
        ORDER BY created_at DESC
    """, (session["doctor_id"],))
    notifications = cursor.fetchall()
    cursor.execute("""
        UPDATE notifications SET read_at = CURRENT_TIMESTAMP
        WHERE recipient_type = 'doctor' AND recipient_id = ? AND read_at IS NULL
    """, (session["doctor_id"],))
    conn.commit()
    conn.close()
    return render_template("doctor_notifications.html", notifications=notifications)


# ---------- Patient: auth & core ----------

@app.route("/patient/login", methods=["GET", "POST"])
def patient_login():
    if request.method == "POST":
        patient_code = request.form.get("patient_code", "").strip().upper()
        password = request.form.get("password", "")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM patients WHERE patient_code = ?", (patient_code,))
        patient = cursor.fetchone()
        conn.close()

        if patient and patient["password"] == password:
            session.permanent = request.form.get("remember_device") == "yes"
            session["patient_id"] = patient["id"]
            session["patient_name"] = patient["name"]
            session["patient_photo"] = patient["photo_filename"]
            return redirect(url_for("patient_dashboard"))
        flash("Invalid patient code or password.")
    return render_template("patient_login.html")


@app.route("/patient/dashboard")
@login_required_patient
def patient_dashboard():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE id = ?", (session["patient_id"],))
    patient = cursor.fetchone()

    cursor.execute("""
        SELECT * FROM glucose_readings WHERE patient_id = ? ORDER BY reading_at DESC LIMIT 3
    """, (session["patient_id"],))
    recent_readings = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM care_plans WHERE patient_id = ? AND is_current = 1 ORDER BY version DESC LIMIT 1
    """, (session["patient_id"],))
    care_plan = cursor.fetchone()

    conn.close()

    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")

    return render_template(
        "patient_dashboard.html",
        patient=patient,
        recent_readings=recent_readings,
        care_plan=care_plan,
        greeting=greeting,
        active="home",
    )


@app.route("/patient/log")
@login_required_patient
def log_menu():
    return render_template("log_menu.html", active="log")


@app.route("/patient/log/glucose", methods=["GET", "POST"])
@login_required_patient
def log_glucose():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT preferred_unit FROM patients WHERE id = ?", (session["patient_id"],))
    preferred_unit = cursor.fetchone()["preferred_unit"]

    if request.method == "POST":
        value = request.form.get("value", "").strip()
        unit = request.form.get("unit", preferred_unit)
        tag = request.form.get("tag") or None
        reading_at = request.form.get("reading_at", "").strip()

        if unit not in ("mg/dL", "mmol/L"):
            unit = preferred_unit
        if tag and tag not in GLUCOSE_TAGS:
            tag = None
        if not reading_at:
            reading_at = datetime.now().strftime("%Y-%m-%dT%H:%M")

        error = None
        try:
            value = float(value)
            if value <= 0 or value > 1000:
                error = "Enter a realistic glucose value."
        except ValueError:
            error = "Enter a valid number for glucose value."

        if error:
            flash(error)
        else:
            cursor.execute("""
                INSERT INTO glucose_readings (patient_id, value, unit, tag, reading_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session["patient_id"], value, unit, tag, reading_at))
            conn.commit()
            conn.close()
            check_and_create_flags(session["patient_id"])
            flash("Glucose reading logged.")
            return redirect(url_for("patient_dashboard"))

    cursor.execute("""
        SELECT * FROM glucose_readings WHERE patient_id = ? ORDER BY reading_at DESC LIMIT 7
    """, (session["patient_id"],))
    recent = list(reversed(cursor.fetchall()))
    conn.close()

    sparkline_points = build_sparkline(recent)
    avg_display = None
    if recent:
        avg_mgdl = sum(mg_dl(r["value"], r["unit"]) for r in recent) / len(recent)
        avg_display = round(avg_mgdl) if preferred_unit == "mg/dL" else round(avg_mgdl / 18.0182, 1)

    return render_template(
        "log_glucose.html",
        active="log",
        preferred_unit=preferred_unit,
        glucose_tags=GLUCOSE_TAGS,
        now=datetime.now().strftime("%Y-%m-%dT%H:%M"),
        now_display=datetime.now().strftime("%I:%M %p").lstrip("0"),
        sparkline_points=sparkline_points,
        avg_display=avg_display,
    )


@app.route("/patient/log/insulin", methods=["GET", "POST"])
@login_required_patient
def log_insulin():
    if request.method == "POST":
        insulin_type = request.form.get("insulin_type", "")
        units = request.form.get("units", "").strip()
        injection_site = request.form.get("injection_site", "").strip()
        dose_at = request.form.get("dose_at", "").strip()

        if insulin_type not in INSULIN_TYPES:
            insulin_type = INSULIN_TYPES[0]
        if injection_site not in INJECTION_SITES:
            injection_site = None
        if not dose_at:
            dose_at = datetime.now().strftime("%Y-%m-%dT%H:%M")

        error = None
        try:
            units = float(units)
            if units <= 0 or units > 200:
                error = "Enter a realistic number of units."
        except ValueError:
            error = "Enter a valid number of units."

        if error:
            flash(error)
        else:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO insulin_logs (patient_id, insulin_type, units, injection_site, dose_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session["patient_id"], insulin_type, units, injection_site, dose_at))
            conn.commit()
            conn.close()
            check_and_create_flags(session["patient_id"])
            flash("Insulin dose logged.")
            return redirect(url_for("patient_dashboard"))

    return render_template(
        "log_insulin.html",
        active="log",
        insulin_types=INSULIN_TYPES,
        injection_sites=INJECTION_SITES,
        now=datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )


@app.route("/patient/log/meal", methods=["GET", "POST"])
@login_required_patient
def log_meal():
    conn = get_db()
    cursor = conn.cursor()

    if request.method == "POST" and "photo" in request.files:
        photo = request.files.get("photo")
        if not photo or not photo.filename:
            flash("Choose or take a photo of your meal.")
            conn.close()
            return redirect(url_for("log_meal"))

        filename, error, raw_bytes, ext = save_uploaded_photo(photo)
        if error:
            flash(error)
            conn.close()
            return redirect(url_for("log_meal"))

        description, estimated_carbs = estimate_carbs_from_photo(raw_bytes, ext)
        cursor.execute("""
            INSERT INTO meal_logs (patient_id, photo_filename, ai_estimated_carbs, ai_description, entry_method, logged_at)
            VALUES (?, ?, ?, ?, 'photo-ai', ?)
        """, (session["patient_id"], filename, estimated_carbs, description, datetime.now().isoformat(timespec="minutes")))
        conn.commit()
        conn.close()
        return redirect(url_for("log_meal"))

    if request.method == "POST" and "choice" in request.form:
        cursor.execute("""
            SELECT * FROM meal_logs WHERE patient_id = ? AND entry_method = 'photo-ai' AND patient_confirmed_carbs IS NULL
            ORDER BY id DESC LIMIT 1
        """, (session["patient_id"],))
        pending = cursor.fetchone()
        multiplier = {"smaller": 0.7, "about_right": 1.0, "larger": 1.3}.get(request.form.get("choice"))
        if pending and multiplier is not None:
            confirmed = round((pending["ai_estimated_carbs"] or 0) * multiplier)
            cursor.execute("UPDATE meal_logs SET patient_confirmed_carbs = ? WHERE id = ?", (confirmed, pending["id"]))
            conn.commit()
            conn.close()
            check_and_create_flags(session["patient_id"])
            flash("Meal logged.")
            return redirect(url_for("patient_dashboard"))
        conn.close()
        return redirect(url_for("log_meal"))

    cursor.execute("""
        SELECT * FROM meal_logs WHERE patient_id = ? AND entry_method = 'photo-ai' AND patient_confirmed_carbs IS NULL
        ORDER BY id DESC LIMIT 1
    """, (session["patient_id"],))
    pending_meal = cursor.fetchone()

    display_meal = pending_meal
    if not display_meal:
        cursor.execute("""
            SELECT * FROM meal_logs WHERE patient_id = ? ORDER BY id DESC LIMIT 1
        """, (session["patient_id"],))
        display_meal = cursor.fetchone()
    conn.close()

    return render_template(
        "log_meal.html", active="log",
        pending_meal=pending_meal, display_meal=display_meal,
    )


@app.route("/patient/log/meal/manual", methods=["GET", "POST"])
@login_required_patient
def log_meal_manual():
    if request.method == "POST":
        carbs = request.form.get("carbs", "").strip()
        description = request.form.get("description", "").strip()
        logged_at = request.form.get("logged_at", "").strip() or datetime.now().strftime("%Y-%m-%dT%H:%M")

        error = None
        try:
            carbs = float(carbs)
            if carbs < 0 or carbs > 500:
                error = "Enter a realistic carb amount."
        except ValueError:
            error = "Enter a valid number of carb grams."

        if error:
            flash(error)
        else:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO meal_logs (patient_id, patient_confirmed_carbs, entry_method, description, logged_at)
                VALUES (?, ?, 'manual', ?, ?)
            """, (session["patient_id"], carbs, description, logged_at))
            conn.commit()
            conn.close()
            check_and_create_flags(session["patient_id"])
            flash("Meal logged.")
            return redirect(url_for("patient_dashboard"))

    return render_template("log_meal_manual.html", active="log", now=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@app.route("/patient/log/symptoms", methods=["GET", "POST"])
@login_required_patient
def log_symptoms():
    if request.method == "POST":
        selected = [s for s in request.form.getlist("symptoms") if s in SYMPTOM_TAGS]
        notes = request.form.get("notes", "").strip()
        logged_at = request.form.get("logged_at", "").strip() or datetime.now().strftime("%Y-%m-%dT%H:%M")

        if not selected:
            flash("Select at least one symptom.")
        else:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO symptom_logs (patient_id, symptoms, notes, logged_at)
                VALUES (?, ?, ?, ?)
            """, (session["patient_id"], ", ".join(selected), notes, logged_at))
            conn.commit()
            conn.close()
            check_and_create_flags(session["patient_id"])
            flash("Symptoms logged.")
            return redirect(url_for("patient_dashboard"))

    return render_template(
        "log_symptoms.html",
        active="log",
        symptom_tags=SYMPTOM_TAGS,
        symptom_icons=SYMPTOM_ICONS,
        now=datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )


@app.route("/patient/care-plan")
@login_required_patient
def view_care_plan():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT cp.*, d.name as doctor_name FROM care_plans cp
        JOIN doctors d ON cp.doctor_id = d.id
        WHERE cp.patient_id = ? ORDER BY cp.version DESC
    """, (session["patient_id"],))
    plans = cursor.fetchall()
    conn.close()
    current = plans[0] if plans else None
    history = plans[1:] if len(plans) > 1 else []
    range_bar = build_range_bar(current)
    return render_template("view_care_plan.html", current=current, history=history, range_bar=range_bar, active="home")


@app.route("/patient/emergency-alert", methods=["GET", "POST"])
@login_required_patient
def emergency_alert():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.id as doctor_id, d.name as doctor_name, d.photo_filename as doctor_photo, d.phone_number as doctor_phone
        FROM patients p JOIN doctors d ON p.doctor_id = d.id WHERE p.id = ?
    """, (session["patient_id"],))
    doctor = cursor.fetchone()

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        latitude = request.form.get("latitude", "").strip()
        longitude = request.form.get("longitude", "").strip()
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except ValueError:
            latitude = longitude = None

        cursor.execute("""
            INSERT INTO emergency_alerts (patient_id, doctor_id, message, latitude, longitude) VALUES (?, ?, ?, ?, ?)
        """, (session["patient_id"], doctor["doctor_id"], message, latitude, longitude))
        alert_id = cursor.lastrowid
        conn.commit()
        conn.close()

        create_notification(
            "doctor", doctor["doctor_id"],
            f"EMERGENCY ALERT from {session['patient_name']}" + (f": {message}" if message else ""),
            url_for("doctor_dashboard"),
        )
        send_push_notification(doctor["doctor_id"], "Vytari Emergency Alert", f"{session['patient_name']} needs urgent attention")

        return redirect(url_for("emergency_alert_sent", alert_id=alert_id))

    conn.close()
    return render_template(
        "emergency_alert.html", active="home",
        doctor_name=doctor["doctor_name"], doctor_photo=doctor["doctor_photo"], doctor_phone=doctor["doctor_phone"],
    )


@app.route("/patient/emergency-alert/<int:alert_id>/sent")
@login_required_patient
def emergency_alert_sent(alert_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM emergency_alerts WHERE id = ? AND patient_id = ?", (alert_id, session["patient_id"]))
    alert = cursor.fetchone()
    conn.close()
    if not alert:
        return redirect(url_for("patient_dashboard"))
    return render_template("emergency_alert_sent.html", alert=alert, active="home")


@app.route("/patient/profile")
@login_required_patient
def patient_profile():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, d.name as doctor_name, d.doctor_code FROM patients p
        JOIN doctors d ON p.doctor_id = d.id
        WHERE p.id = ?
    """, (session["patient_id"],))
    patient = cursor.fetchone()
    conn.close()
    return render_template("patient_profile.html", patient=patient, active="profile")


@app.route("/patient/profile/photo", methods=["POST"])
@login_required_patient
def patient_profile_photo():
    filename, error, _, _ = save_uploaded_photo(request.files.get("photo"), session.get("patient_photo"))
    if error:
        flash(error)
    else:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE patients SET photo_filename = ? WHERE id = ?", (filename, session["patient_id"]))
        conn.commit()
        conn.close()
        session["patient_photo"] = filename
        flash("Profile photo updated.")
    return redirect(url_for("patient_profile"))


@app.route("/patient/profile/unit", methods=["POST"])
@login_required_patient
def patient_profile_unit():
    unit = request.form.get("preferred_unit", "mg/dL")
    if unit not in ("mg/dL", "mmol/L"):
        unit = "mg/dL"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE patients SET preferred_unit = ? WHERE id = ?", (unit, session["patient_id"]))
    conn.commit()
    conn.close()
    flash("Preferred unit updated.")
    return redirect(url_for("patient_profile"))


@app.route("/patient/notifications")
@login_required_patient
def patient_notifications():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM notifications WHERE recipient_type = 'patient' AND recipient_id = ?
        ORDER BY created_at DESC
    """, (session["patient_id"],))
    notifications = cursor.fetchall()
    cursor.execute("""
        UPDATE notifications SET read_at = CURRENT_TIMESTAMP
        WHERE recipient_type = 'patient' AND recipient_id = ? AND read_at IS NULL
    """, (session["patient_id"],))
    conn.commit()
    conn.close()
    return render_template("patient_notifications.html", notifications=notifications)


@app.route("/patient/trends")
@login_required_patient
def patient_trends():
    days = request.args.get("days", 7, type=int)
    if days not in (7, 30):
        days = 7

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT preferred_unit FROM patients WHERE id = ?", (session["patient_id"],))
    preferred_unit = cursor.fetchone()["preferred_unit"]

    period_start = datetime.now() - timedelta(days=days)
    cursor.execute("""
        SELECT * FROM glucose_readings WHERE patient_id = ? AND reading_at >= ?
        ORDER BY reading_at ASC LIMIT 200
    """, (session["patient_id"], period_start.isoformat()))
    readings = cursor.fetchall()

    prior_start = datetime.now() - timedelta(days=days * 2)
    cursor.execute("""
        SELECT value, unit FROM glucose_readings WHERE patient_id = ? AND reading_at >= ? AND reading_at < ?
    """, (session["patient_id"], prior_start.isoformat(), period_start.isoformat()))
    prior_readings = cursor.fetchall()

    cursor.execute("""
        SELECT * FROM care_plans WHERE patient_id = ? AND is_current = 1 ORDER BY version DESC LIMIT 1
    """, (session["patient_id"],))
    care_plan = cursor.fetchone()
    conn.close()

    chart = build_glucose_chart(readings, care_plan)
    if chart:
        for i, r in enumerate(readings):
            chart["points"][i]["day_label"] = datetime.fromisoformat(r["reading_at"]).strftime("%a").upper()
        if chart["band"]:
            chart["zone_high_pct"] = round(chart["band"]["y"] / chart["height"] * 100, 1)
            chart["zone_target_pct"] = round(chart["band"]["height"] / chart["height"] * 100, 1)
            chart["zone_low_pct"] = round(100 - chart["zone_high_pct"] - chart["zone_target_pct"], 1)

    in_range_pct = None
    avg_display = None
    if readings:
        values_mgdl = [mg_dl(r["value"], r["unit"]) for r in readings]
        avg_mgdl = sum(values_mgdl) / len(values_mgdl)
        avg_display = round(avg_mgdl) if preferred_unit == "mg/dL" else round(avg_mgdl / 18.0182, 1)
        if care_plan:
            target_min_mgdl = mg_dl(care_plan["target_min"], care_plan["unit"])
            target_max_mgdl = mg_dl(care_plan["target_max"], care_plan["unit"])
            in_range = sum(1 for v in values_mgdl if target_min_mgdl <= v <= target_max_mgdl)
            in_range_pct = round(100 * in_range / len(readings))

    insights = []
    if readings and prior_readings:
        this_avg = sum(mg_dl(r["value"], r["unit"]) for r in readings) / len(readings)
        prior_avg = sum(mg_dl(r["value"], r["unit"]) for r in prior_readings) / len(prior_readings)
        if prior_avg > 0:
            pct_change = round(abs(this_avg - prior_avg) / prior_avg * 100)
            if pct_change >= 3:
                direction = "lower" if this_avg < prior_avg else "higher"
                insights.append({
                    "title": "Trending " + direction.capitalize(),
                    "text": f"Your average glucose has been {pct_change}% {direction} than the previous {days}-day period.",
                    "icon": "trending_down" if direction == "lower" else "trending_up",
                    "highlight": direction == "lower",
                })

    tag_values = {}
    for r in readings:
        if r["tag"] in ("fasting", "post-meal", "bedtime"):
            tag_values.setdefault(r["tag"], []).append(mg_dl(r["value"], r["unit"]))
    if readings:
        overall_avg = sum(mg_dl(r["value"], r["unit"]) for r in readings) / len(readings)
        best_tag, best_avg = None, 0
        for tag, vals in tag_values.items():
            if len(vals) >= 2:
                tag_avg = sum(vals) / len(vals)
                if tag_avg > best_avg:
                    best_tag, best_avg = tag, tag_avg
        if best_tag and (best_avg - overall_avg) > 15:
            insights.append({
                "title": "Timing Pattern",
                "text": f"Your {best_tag} readings have tended to run higher than other times — worth mentioning at your next visit.",
                "icon": "restaurant",
                "highlight": False,
            })

    return render_template(
        "patient_trends.html",
        active="trends",
        readings=list(reversed(readings))[:20],
        chart=chart,
        care_plan=care_plan,
        in_range_pct=in_range_pct,
        avg_display=avg_display,
        preferred_unit=preferred_unit,
        insights=insights,
        days=days,
    )


@app.route("/patient/chat", methods=["GET", "POST"])
@login_required_patient
def patient_chat():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.id as doctor_id, d.name as doctor_name, d.photo_filename as doctor_photo FROM patients p
        JOIN doctors d ON p.doctor_id = d.id WHERE p.id = ?
    """, (session["patient_id"],))
    doctor = cursor.fetchone()

    if request.method == "POST":
        message_text = request.form.get("message_text", "").strip()
        if message_text:
            cursor.execute("""
                INSERT INTO messages (patient_id, doctor_id, sender_type, message_text)
                VALUES (?, ?, 'patient', ?)
            """, (session["patient_id"], doctor["doctor_id"], message_text))
            conn.commit()
            create_notification(
                "doctor", doctor["doctor_id"],
                f"New message from {session['patient_name']}",
                url_for("doctor_patient_chat", patient_id=session["patient_id"]),
            )
        conn.close()
        return redirect(url_for("patient_chat"))

    cursor.execute("""
        UPDATE messages SET read_at = CURRENT_TIMESTAMP
        WHERE patient_id = ? AND sender_type = 'doctor' AND read_at IS NULL
    """, (session["patient_id"],))
    conn.commit()

    cursor.execute("""
        SELECT * FROM messages WHERE patient_id = ? ORDER BY created_at ASC
    """, (session["patient_id"],))
    messages = format_chat_messages(cursor.fetchall())
    conn.close()

    return render_template(
        "chat.html", active="chat", messages=messages,
        doctor_name=doctor["doctor_name"], doctor_photo=doctor["doctor_photo"],
    )


@app.route("/patient/chat/poll")
@login_required_patient
def patient_chat_poll():
    after_id = request.args.get("after_id", 0, type=int)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE messages SET read_at = CURRENT_TIMESTAMP
        WHERE patient_id = ? AND sender_type = 'doctor' AND read_at IS NULL
    """, (session["patient_id"],))
    conn.commit()
    cursor.execute("""
        SELECT id, sender_type, message_text, created_at FROM messages
        WHERE patient_id = ? AND id > ? ORDER BY created_at ASC
    """, (session["patient_id"], after_id))
    rows = format_chat_messages(cursor.fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/doctor/patients/<int:patient_id>/chat", methods=["GET", "POST"])
@login_required_doctor
def doctor_patient_chat(patient_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    patient = cursor.fetchone()
    if not patient:
        conn.close()
        return redirect(url_for("doctor_dashboard"))

    if request.method == "POST":
        message_text = request.form.get("message_text", "").strip()
        if message_text:
            cursor.execute("""
                INSERT INTO messages (patient_id, doctor_id, sender_type, message_text)
                VALUES (?, ?, 'doctor', ?)
            """, (patient_id, session["doctor_id"], message_text))
            conn.commit()
            create_notification(
                "patient", patient_id,
                f"New message from {session['doctor_name']}",
                url_for("patient_chat"),
            )
        conn.close()
        return redirect(url_for("doctor_patient_chat", patient_id=patient_id))

    cursor.execute("""
        UPDATE messages SET read_at = CURRENT_TIMESTAMP
        WHERE patient_id = ? AND sender_type = 'patient' AND read_at IS NULL
    """, (patient_id,))
    conn.commit()

    cursor.execute("""
        SELECT * FROM messages WHERE patient_id = ? ORDER BY created_at ASC
    """, (patient_id,))
    messages = format_chat_messages(cursor.fetchall())
    conn.close()

    return render_template("doctor_chat.html", active="messages", messages=messages, patient=patient)


@app.route("/doctor/patients/<int:patient_id>/chat/poll")
@login_required_doctor
def doctor_patient_chat_poll(patient_id):
    after_id = request.args.get("after_id", 0, type=int)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM patients WHERE id = ? AND doctor_id = ?", (patient_id, session["doctor_id"]))
    if not cursor.fetchone():
        conn.close()
        return jsonify([])
    cursor.execute("""
        UPDATE messages SET read_at = CURRENT_TIMESTAMP
        WHERE patient_id = ? AND sender_type = 'patient' AND read_at IS NULL
    """, (patient_id,))
    conn.commit()
    cursor.execute("""
        SELECT id, sender_type, message_text, created_at FROM messages
        WHERE patient_id = ? AND id > ? ORDER BY created_at ASC
    """, (patient_id, after_id))
    rows = format_chat_messages(cursor.fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/doctor/messages")
@login_required_doctor
def doctor_messages():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.name, p.patient_code, p.photo_filename,
        (SELECT COUNT(*) FROM messages m WHERE m.patient_id = p.id AND m.sender_type = 'patient' AND m.read_at IS NULL) as unread_count,
        (SELECT message_text FROM messages m WHERE m.patient_id = p.id ORDER BY m.created_at DESC LIMIT 1) as last_message,
        (SELECT created_at FROM messages m WHERE m.patient_id = p.id ORDER BY m.created_at DESC LIMIT 1) as last_message_at
        FROM patients p WHERE p.doctor_id = ?
        ORDER BY unread_count DESC, last_message_at DESC
    """, (session["doctor_id"],))
    conversations = cursor.fetchall()
    conn.close()
    return render_template("doctor_messages.html", conversations=conversations, active="messages")


# ---------- Static info pages ----------

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


# ---------- App startup ----------

create_database()
migrate_database()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

if __name__ == "__main__":
    app.run(debug=True)
