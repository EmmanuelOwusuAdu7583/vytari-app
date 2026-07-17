"""Seeds demo doctor + patient accounts with sample data for the pilot demo.

Run with: python seed.py
Safe to re-run — skips creation if the demo doctor code already exists.
"""
from datetime import datetime, timedelta

import app as vytari

DEMO_DOCTOR_NAME = "Dr. Ama Boateng"
DEMO_DOCTOR_CODE = "DR-DEMO01"
DEMO_DOCTOR_PASSWORD = "DemoDoctor123"

DEMO_PATIENTS = [
    {"code": "PT-DEMO01", "password": "DemoPatient123", "name": "Kwame Asante", "diabetes_type": "Type 1", "unit": "mg/dL"},
    {"code": "PT-DEMO02", "password": "DemoPatient456", "name": "Efua Owusu", "diabetes_type": "Type 2", "unit": "mmol/L"},
]


def iso(days_ago, hour=8, minute=0):
    return (datetime.now() - timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def main():
    conn = vytari.get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM doctors WHERE doctor_code = ?", (DEMO_DOCTOR_CODE,))
    existing = cursor.fetchone()
    if existing:
        print(f"Demo doctor {DEMO_DOCTOR_CODE} already exists — skipping seed.")
        conn.close()
        return

    cursor.execute("""
        INSERT INTO doctors (doctor_code, name, password, phone_number) VALUES (?, ?, ?, ?)
    """, (DEMO_DOCTOR_CODE, DEMO_DOCTOR_NAME, DEMO_DOCTOR_PASSWORD, "+233-20-555-0142"))
    doctor_id = cursor.lastrowid
    conn.commit()
    print(f"Created doctor: {DEMO_DOCTOR_CODE} / {DEMO_DOCTOR_PASSWORD} ({DEMO_DOCTOR_NAME})")

    created_patient_ids = []
    for p in DEMO_PATIENTS:
        cursor.execute("""
            INSERT INTO patients (patient_code, doctor_id, name, password, diabetes_type, preferred_unit)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (p["code"], doctor_id, p["name"], p["password"], p["diabetes_type"], p["unit"]))
        patient_id = cursor.lastrowid
        created_patient_ids.append(patient_id)
        print(f"Created patient: {p['code']} / {p['password']} ({p['name']})")

        cursor.execute("""
            INSERT INTO care_plans (
                patient_id, doctor_id, target_min, target_max, unit,
                basal_name, basal_description, basal_units, basal_frequency,
                bolus_name, bolus_description, bolus_ratio,
                instructions, version, is_current
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
        """, (
            patient_id, doctor_id,
            70 if p["unit"] == "mg/dL" else 4.0,
            140 if p["unit"] == "mg/dL" else 7.8,
            p["unit"],
            "Lantus (Basal)", "Long-acting bedtime", "12u", "Daily",
            "Humalog (Bolus)", "Rapid-acting w/ meals", "1u : 15g",
            "Check glucose before each meal and at bedtime. Contact clinic if fasting readings exceed target for 2+ days in a row.",
        ))

        for days_ago, tag, value in [
            (6, "fasting", 95 if p["unit"] == "mg/dL" else 5.3),
            (5, "post-meal", 145 if p["unit"] == "mg/dL" else 8.1),
            (4, "fasting", 88 if p["unit"] == "mg/dL" else 4.9),
            (3, "bedtime", 110 if p["unit"] == "mg/dL" else 6.1),
            (2, "fasting", 102 if p["unit"] == "mg/dL" else 5.7),
            (1, "post-meal", 168 if p["unit"] == "mg/dL" else 9.3),
            (0, "fasting", 91 if p["unit"] == "mg/dL" else 5.1),
        ]:
            cursor.execute("""
                INSERT INTO glucose_readings (patient_id, value, unit, tag, reading_at)
                VALUES (?, ?, ?, ?, ?)
            """, (patient_id, value, p["unit"], tag, iso(days_ago)))

        for days_ago in [5, 3, 1]:
            cursor.execute("""
                INSERT INTO insulin_logs (patient_id, insulin_type, units, dose_at)
                VALUES (?, 'long-acting', 12, ?)
            """, (patient_id, iso(days_ago, hour=22)))

        cursor.execute("""
            INSERT INTO meal_logs (patient_id, patient_confirmed_carbs, entry_method, description, logged_at)
            VALUES (?, 55, 'manual', 'Jollof rice with chicken', ?)
        """, (patient_id, iso(2, hour=13)))

        cursor.execute("""
            INSERT INTO doctor_notes (patient_id, doctor_id, note_text)
            VALUES (?, ?, 'Reviewed at last visit. Adherence looks reasonable, continue current regimen.')
        """, (patient_id, doctor_id))

    conn.commit()
    conn.close()

    with vytari.app.test_request_context():
        for patient_id in created_patient_ids:
            vytari.check_and_create_flags(patient_id)

    print("\nSeed complete. Sign in with the codes above at /doctor/login and /patient/login.")


if __name__ == "__main__":
    main()
