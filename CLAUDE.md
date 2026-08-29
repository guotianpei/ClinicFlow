# ClinicFlow / HaloFlow — working notes for Claude

## Repository shape

- `haloflow/` — the only Python package. **Run every command from `haloflow/`**, not the repo root.
  `alembic.ini` resolves `script_location` relative to the CWD, and the M01 test harness builds
  `Config("alembic.ini")` from a relative path.
- `documentation/` — requirements, architecture, ADRs, session status. **Read this first**; check the
  newest `track-b-session-*.md` for current status before starting work.
- `.github/workflows/m01.yml` — the CI gate. It is the source of truth for lint/type/test scope.

## Process — follow this before writing code

For any new module or feature: **requirements review → architecture design → unit test cases →
alignment → code.** Do not start implementing before those first three are reviewed together.
Architecture decisions are recorded in `documentation/architecture/track-b-architecture-decisions.md`.

## Local environment

```
cd haloflow
source .venv/bin/activate
set -a; . ./.env; set +a
```

The `set -a` line matters: pydantic-settings reads `.env` for `Settings` fields but does **not**
export it to the process environment. `alembic/env.py` and `tests/m01/` use `os.getenv`, so
`HALOFLOW_MIGRATION_DATABASE_URL` and `HALOFLOW_TEST_DATABASE_URL` are only visible after sourcing.

Three connection strings, three consumers:

| Variable | Form | Read by |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://` | `config.py` → `database.py` (the app) |
| `HALOFLOW_MIGRATION_DATABASE_URL` | `postgresql://` | `alembic/env.py` (rewritten to `+psycopg`) |
| `HALOFLOW_TEST_DATABASE_URL` | `postgresql://` | `tests/m01/test_gateway_postgres.py` |

Full setup: `documentation/local-dev-setup.md`.

## Commands

```
make check      # lint + type + test, the full CI gate
make test       # pytest
make test-unit  # pytest -m "not postgres", no database needed
make run        # uvicorn on :8000
make migrate    # alembic upgrade head
```

Equivalently, and this is exactly what CI runs:

```
ruff check src/haloflow/m01 tests/m01 alembic
mypy src/haloflow/m01
pytest tests/m01 -q
```

## Constraints that will bite

- **PostgreSQL 17+ is required.** The M01 suite fails (not skips) below server version 170000.
- The test database name **must start with `haloflow_test`**. The harness refuses anything else,
  because it drops schemas and roles inside it on every run.
- The migration issues `CREATE ROLE`, so the migrating role needs `CREATEROLE`.
- `ruff` and `mypy` currently cover **only** `src/haloflow/m01`, `tests/m01`, and `alembic`. Code
  outside that scope — including `config.py` and everything under `src/haloflow/modules/` — is not
  checked by CI. Run the tools manually on files you touch there.
- `mypy` runs in `strict` mode.
- `PRIORITY_PAYER_IDS` is comma-separated, not JSON. The field is annotated
  `Annotated[list[str], NoDecode]` so pydantic-settings hands the raw string to `parse_payer_ids`.

## Data and secrets

- **No PHI in the local database.** `haloflow_dev` is synthetic data only.
- `.env` is gitignored. Never write real secrets into docs, commit messages, or session notes.
- Vendor credentials (athenahealth, Notifyre, Stedi) default to empty strings; leave them blank
  unless deliberately exercising an integration.
