# HaloFlow Track B — Session Status
*Date: July 31, 2026*
*Pick this up at the start of the next session.*

---

## What Was Completed This Session

### 1. Tier 2 Codebase — Built from Scratch
Full Python/FastAPI project created in `/haloflow` folder, pushed to `ClinicFlow` repo.

**Structure:**
```
haloflow/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/001_initial_schema.py   ← full DB schema + seeded care gap measures
└── src/haloflow/
    ├── config.py
    ├── database.py
    ├── main.py
    ├── scheduler.py
    ├── ehr/
    │   ├── base.py                      ← abstract EHRAdapter (EMR-agnostic interface)
    │   └── athenahealth.py              ← athenahealth REST API implementation
    ├── integrations/
    │   ├── telnyx.py                    ← ⚠️ NEEDS REPLACEMENT (see below)
    │   ├── stedi.py                     ← eligibility EDI 270/271
    │   └── fax.py                       ← ⚠️ NEEDS REPLACEMENT (see below)
    └── modules/
        ├── reminders/                   ← SMS reminders, no-show rebooking, EMR writeback
        ├── eligibility/                 ← pre-visit insurance checks, alert queue
        ├── fax/                         ← inbound routing rules, outbound cover sheet
        └── care_gaps/                   ← EMR due-date + payer list upload outreach
```

### 2. athenahealth Sandbox — Registered
- App name: **HaloVox Clinic Flow**
- Auth type: 2-legged OAuth, Secret
- Environment: Preview
- Client ID and Secret: in hand (stored securely — do NOT commit to repo)
- Sandbox practice ID: **195900** (ambulatory testing)
- Scope to use: `athena/service/Athenanet.MDP.*` (only this — no FHIR SMART scopes)
- Scope bug fixed: `athenahealth.py` updated from wrong scope string to correct one

### 3. GCP Project — Created
- Project name: `halovox-clinicflow`
- Project ID: `portal-project-1538058727457`
- APIs enabled: Cloud SQL Admin, Secret Manager, Cloud Run
- Region decision: `us-east1` (closest to pilot clinic in Virginia)

### 4. Cloud SQL — Provisioning Started
- Instance ID: `haloflow-db`
- Engine: PostgreSQL
- Region: `us-east1`
- Status: provisioning at end of session — confirm it shows **Running** at start of next session

---

## What Needs to Happen Next Session

### Priority 1 — Verify Cloud SQL is Running
Go to: https://console.cloud.google.com/sql/instances?project=portal-project-1538058727457
Confirm `haloflow-db` shows status **Running**.

### Priority 2 — Replace Telnyx + SRFax with Notifyre
Per `track-b-vendor-baa-status.md`:
- **Telnyx is disqualified** (contradictory BAA story)
- **SRFax is not selected** — Notifyre handles both SMS and fax under one free BAA
- Need to: delete `integrations/telnyx.py`, rewrite `integrations/fax.py` → merge into `integrations/notifyre.py`
- Notifyre API docs: https://docs.notifyre.com

### Priority 3 — Connect Codespaces to Cloud SQL
1. Install gcloud CLI in Codespaces:
   ```bash
   curl https://sdk.cloud.google.com | bash -s -- --disable-prompts
   exec -l $SHELL
   gcloud auth login --no-launch-browser
   gcloud config set project portal-project-1538058727457
   ```
2. Install Cloud SQL Auth Proxy (secure connection from Codespaces to Cloud SQL)
3. Create the `haloflow` database and user in Cloud SQL
4. Store credentials in GCP Secret Manager (not in `.env`)
5. Run `alembic upgrade head` to create schema

### Priority 4 — Create .env in Codespaces
Once Cloud SQL and Secret Manager are set up:
```
DATABASE_URL=postgresql+asyncpg://haloflow:<password>@localhost:5432/haloflow
ATHENA_CLIENT_ID=<from athenahealth app>
ATHENA_CLIENT_SECRET=<from athenahealth app>
ATHENA_PRACTICE_ID=195900
ATHENA_BASE_URL=https://api.preview.platform.athenahealth.com
APP_ENV=development
```

### Priority 5 — First smoke test
```bash
cd /workspaces/ClinicFlow/haloflow
uvicorn haloflow.main:app --reload
```
Hit `GET /health` → should return `{"status": "ok"}`

---

## Open Vendor Items (from track-b-vendor-baa-status.md)
| Item | Status |
|---|---|
| Notifyre — SMS + Fax | 🟢 Locked (BAA free, one vendor) |
| Stedi — Eligibility | 🟡 Pending (waiting on BAA + payer connectivity reply) |
| GCP — Hosting | 🟢 Locked |
| Google Vertex AI — Tier 3 LLM | 🟢 Locked |
| Retell AI — Tier 4 Voice | 🟢 Locked |

---

## Code Change Needed (do at start of next session)
`integrations/telnyx.py` → replace with `integrations/notifyre.py`
`integrations/fax.py` → merge into Notifyre client (one API for both SMS + fax)
All references to `TelnyxSMSClient` and `SRFaxClient` in service/router files need updating.

---

*Next session: start by confirming Cloud SQL is Running, then Priority 2 (Notifyre rewrite).*
