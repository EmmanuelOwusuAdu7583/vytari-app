# GlucoTrack — Full Build Task for Claude Code

Build a diabetes monitoring and doctor-patient care coordination app called GlucoTrack, following the same architecture and code patterns as the existing Healio/Ma Nkwa project in this workspace (Flask, Jinja templates, vanilla CSS, SQLite locally → PostgreSQL on Render, session-based auth, closed-pilot admin-onboards-doctors model). Read this entire document before starting.

---

## CORE SAFETY PRINCIPLE (read first, follow throughout)

This app logs, visualizes, and communicates blood glucose and insulin data between patient and doctor. **It must never calculate or recommend insulin dosages.** Any dosing guidance comes only from the doctor via the care plan feature, never from app logic. State this boundary visibly in the UI near logging screens (e.g., "This app does not calculate insulin doses. Always follow your doctor's prescribed regimen."). The app surfaces patterns; the doctor interprets and acts. Do not build any feature, now or later, that suggests a dose or auto-adjusts a care plan based on data patterns.

---

## Concept Summary

GlucoTrack helps diabetic patients track blood glucose readings, insulin doses taken, meals, and symptoms, while giving their doctor a pattern-aware view between appointments, enabling earlier intervention without requiring constant in-person visits.

## User Roles

- **Patient** — logs glucose readings, insulin doses taken, meals/carbs (via photo or manual entry), symptoms; views care plan (read-only); messages doctor
- **Doctor** — reviews patient data/trends, sets/updates care plans, responds to flags, messages patients
- **Admin** — onboards doctors via a password-protected route (same pattern as Healio's `/admin/doctors`, using `ADMIN_PASSWORD` env var)

---

## PHASE 1 — Core Skeleton

- Flask app structure, database, both login flows (Doctor, Patient)
- Admin route to add doctor accounts (auto-generates Doctor Code + password, shown once)
- Doctor can create a patient (auto-generates Patient Code + password)
- Environment variables for `SECRET_KEY` and `ADMIN_PASSWORD` from the start — never hardcode secrets (this was a real mistake made and fixed in the earlier Healio project; do not repeat it)

## PHASE 2 — Patient-Side Logging

1. **Blood glucose logging** — manual entry, support both mg/dL and mmol/L, timestamp, optional tag (fasting/post-meal/bedtime/random)
2. **Insulin dose logging** — type (rapid-acting/long-acting/etc.) and units taken, timestamp. This logs what the patient did — never suggests what to take.
3. **Meal/carb logging — photo-based, accessibility-first design:**
   - Patient uploads/takes a photo (camera or gallery)
   - AI vision model estimates carbs from the photo, phrased in plain terms (e.g., "This looks like a medium portion of rice with stew — roughly 50g of carbs")
   - Confirmation UI uses three large buttons: **[Smaller than this] [About right] [Larger than this]** — never ask the patient to type/edit an exact gram number, since the target population may not have the nutritional literacy to judge that
   - Store `photo_url`, `ai_estimated_carbs`, `patient_confirmed_carbs` (adjusted proportionally from the button response), and `entry_method` (photo-AI vs. manual) — keep the AI's raw estimate separate from the confirmed value for transparency
   - Also support plain manual carb entry as a fallback/alternative for patients who prefer it
4. **Symptom logging** — quick-tap tags (dizziness, excessive thirst, fatigue, blurred vision, etc.)
5. **View care plan** — read-only display of doctor-set target glucose range, insulin regimen, instructions
6. **Trend view** — simple charts of glucose over time, visible to the patient themselves
7. **Static emergency information screen** — doctor-approved, pre-written recognition guidance for hypo/hyperglycemia symptoms and when to seek emergency care. Static content only — never dynamically generated from the patient's own data, to avoid appearing to give personalized medical judgment.

## PHASE 3 — Doctor-Side Monitoring

1. **Patient dashboard** — list of patients, flagged patients highlighted (mirror Ma Nkwa's dashboard pattern: stat cards, search, entry cards with colored left border for status)
2. **Automatic pattern flagging** (data surfacing only, never decision-making):
   - 3+ hypoglycemic readings below doctor-set threshold in 7 days
   - Repeated hyperglycemic readings above threshold
   - 2+ consecutive days of missed insulin doses
   - Wide glucose variability (large swings between readings)
   - Store the specific rule/threshold that triggered each flag, not just a boolean — for doctor review speed and auditability
3. **Care plan editor** — doctor sets/updates target glucose range, insulin regimen, instructions. **Version this table** (keep history, not just current state) so a doctor can see how a patient's regimen changed over time.
4. **Trend visualization** — same data as patient view, plus doctor-level detail including **time-in-range (TIR) as the headline metric** (percentage of readings within target range — a real, standard clinical metric, use this rather than inventing a custom score)
5. **Notes** — same pattern as Ma Nkwa's doctor notes

## PHASE 4 — Live Chat with Doctor (replaces any AI chatbot concept — do not build an AI chatbot for this app)

- Bidirectional messaging: extend a `messages` table (`id`, `patient_id`, `doctor_id`, `sender_type`, `message_text`, `created_at`, `read_at`)
- Per-patient conversation thread, not a generic inbox
- Unread indicators on doctor dashboard, same visual pattern as flags
- Simplest implementation: page-refresh/polling, consistent with the rest of this Flask app's architecture — no WebSockets needed for v1
- **UI must clearly state this is not real-time/instant** (e.g., "Your doctor typically responds within 24 hours")
- **Keep visually and functionally separate from the emergency alert feature** — add a visible prompt near the chat input like "Is this urgent? Use Emergency Alert instead"

## PHASE 5 — Emergency Alert Feature

- A distinct, clearly-separate-from-chat signal a patient can raise for genuinely urgent situations (e.g., severe hypoglycemia)
- Alerts the doctor immediately (and optionally a designated emergency contact) — push notification via Firebase Cloud Messaging (free tier) is sufficient; SMS via Africa's Talking or similar as a stretch goal
- This is NOT a chat message — it's a distinct, urgent-only action with its own clear UI

## PHASE 6 — Polish & Deploy

- Apply the design system below throughout
- PWA setup (manifest.json, service worker, icons) — same pattern as Healio
- Deploy to Render, environment variables set in dashboard, not code
- Seed 1-2 test doctor accounts + test patients for demo purposes

---

## Data Model Summary

Tables: `doctors`, `patients`, `glucose_readings`, `insulin_logs`, `meal_logs` (with photo/AI fields above), `symptom_logs`, `care_plans` (versioned), `flags` (with trigger reason stored), `messages`, `doctor_notes`

---

## Deliberately Out of Scope (do not build, now or as "future work")

- Any dosing calculator or dose-suggestion feature
- Auto-adjusting care plans based on detected patterns
- AI chatbot for open-ended health questions (replaced by live doctor chat instead)
- CGM device integration (Dexcom/Libre APIs) — genuine future work, not this build
- Caregiver/family access role — future consideration, not this build

---

## DESIGN SYSTEM — Apply Throughout

Use these tokens and component patterns consistently. Do not invent new colors/spacing ad hoc — reference these variables everywhere, the same discipline used in the Healio project.

### Color tokens (adapt hex values to a health-appropriate palette, keep the same structure)
```css
:root {
  --primary: #0052CC;
  --primary-dark: #003D99;
  --primary-light: #EAF1FF;
  --secondary: #DC3223;
  --secondary-light: #FDECEA;
  --tertiary: #A33500;
  --tertiary-light: #FDF1E8;
  --success: #16A34A;
  --success-light: #F0FDF4;
  --neutral-900: #1A2332;
  --neutral-600: #425266;
  --neutral-400: #8A94A6;
  --neutral-200: #E4E8EF;
  --neutral-100: #F4F6F9;
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;
}
```

### Layered shadow (use for all cards)
```css
.card {
  box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.06);
  border-radius: 14px;
}
```

### Gradient auth header
```css
.auth-header {
  background: linear-gradient(180deg, var(--primary-light) 0%, rgba(234,241,255,0) 100%);
}
```

### Button press feedback
```css
.btn { transition: transform 0.1s ease; }
.btn:active { transform: scale(0.97); }
```

### Toggle switch with sliding knob
```css
.toggle-knob { transition: left 0.15s ease; }
.toggle-switch input:checked ~ .toggle-knob { left: 20px; }
```

### Skeleton loader (use for any async content load)
```css
.skeleton {
  background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
  height: 16px;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
```

### Left-border status card (use for flagged patients, entries)
```css
.entry-card { border-left: 3px solid transparent; }
.entry-card.status-flagged { border-left-color: var(--secondary); }
.entry-card.status-good { border-left-color: var(--success); }
```

### Tinted status badge
```css
.badge {
  background: var(--secondary-light);
  color: var(--secondary);
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 700;
}
```

### Eyebrow label
```css
.eyebrow {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--neutral-400);
  font-weight: 700;
}
```

### Icon-in-tinted-circle (use for all icon usage, SVG only, never emoji — see icon rules below)
```css
.icon-circle {
  width: 34px; height: 34px;
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  background: var(--primary-light);
  color: var(--primary);
}
```

### Glass-lite (use ONLY on modals/overlays — e.g., the meal-confirmation modal, emergency alert confirmation — never on dashboards, data tables, or dense-text areas)
```css
.glass-panel {
  background: rgba(255, 255, 255, 0.95);
  border-radius: 20px;
  border: 1px solid rgba(255, 255, 255, 0.3);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
}
@supports (backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)) {
  .glass-panel {
    background: rgba(255, 255, 255, 0.75);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
}
.glass-panel .content-inner {
  background: rgba(255, 255, 255, 0.98);
  border-radius: 14px;
  padding: 16px;
}
```
Always verify text contrast inside glass panels meets accessibility standards — put text in the near-solid `.content-inner` container, never directly on the blurred surface.

### Reduced motion (include globally, always)
```css
@media (prefers-reduced-motion: reduce) {
  * { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
```

### Explicit rules, no exceptions
- No emoji as structural/functional icons — SVG only, `stroke="currentColor"`
- Minimum 44×44px touch targets on all icon buttons
- `aria-label` on every icon-only button
- Visible focus rings on all interactive elements (never `outline: none` without a replacement)
- No 3D rendering/WebGL — performance and clarity concerns for this user base outweigh visual novelty
- Consistent type scale — pick 5-6 font sizes total, do not deviate

---

## Testing Expectation

Before considering any phase done: actually run the app, walk through the affected flow end-to-end, confirm no console/server errors. Test the full flow: admin creates doctor → doctor creates patient → patient logs data (including photo-based meal logging) → flags trigger correctly → doctor sees flags and can message patient → patient sees message and care plan.
