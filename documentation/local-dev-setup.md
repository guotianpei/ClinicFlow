# HaloFlow — Local Development Setup (macOS)

Replaces the GitHub Codespaces workflow. Target machine: the Mac mini (Apple Silicon, macOS 13+).
Everything below runs against `~/Documents/GitHub/ClinicFlow`.

**Source of truth:** this file. If a command here disagrees with `.github/workflows/m01.yml`,
CI wins — update this doc.

---

## 0. What the stack actually is

Determined from `haloflow/pyproject.toml`, `haloflow/alembic/env.py`, `haloflow/src/haloflow/config.py`,
`haloflow/tests/m01/`, and `.github/workflows/m01.yml`:

| Piece | Requirement | Where it comes from |
|---|---|---|
| Language | Python **>= 3.11**; CI pins **3.12** | `pyproject.toml` `requires-python`, `m01.yml` |
| Build backend | hatchling, editable install | `pyproject.toml` |
| Web | FastAPI + uvicorn | `src/haloflow/main.py` |
| DB driver (app) | SQLAlchemy 2 async + asyncpg | `src/haloflow/database.py` |
| DB driver (M01) | psycopg 3 (`psycopg[binary]`, `psycopg_pool`) | `src/haloflow/m01/pool.py` |
| Migrations | Alembic, privileged path | `alembic/env.py` |
| Database | **PostgreSQL 17+** — hard-checked by the M01 tests | `tests/m01/test_gateway_postgres.py` |
| Scheduler | APScheduler, started in the FastAPI lifespan | `src/haloflow/scheduler.py` |
| Lint / types | ruff, mypy `strict` | `pyproject.toml` |

There is **no** Node/frontend, **no** Dockerfile, and **no** devcontainer in this repo — it is a
single Python package under `haloflow/`. All commands below are run from `haloflow/`, not the repo root.

---

## 1. Base toolchain

> **Paste one command at a time.** zsh on macOS does not enable `interactive_comments` by
> default, so a `#` comment typed or pasted after a command is passed to it as arguments
> (`createdb haloflow_test_m01  # note` fails with "too many command-line arguments").
> Every code block below is therefore comment-free. Run `setopt interactive_comments`
> if you want trailing comments to work in your shell.

### 1.1 Xcode Command Line Tools

```bash
xcode-select -p || xcode-select --install
```

### 1.2 Homebrew

```bash
which brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

On Apple Silicon Homebrew installs to `/opt/homebrew`. If `brew` is not on your PATH afterwards:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 1.3 Python 3.12 and git

```bash
brew install python@3.12 git
python3.12 --version
```

Expect `3.12.x`. Use 3.12 to match CI. 3.11 and 3.13 satisfy `requires-python`, but CI only proves 3.12.

---

## 2. PostgreSQL 17

The M01 isolation tests **fail** (not skip) on anything below server version 170000, and the
migration creates database roles, so the connecting role needs `CREATEROLE` — a superuser is simplest.

```bash
brew install postgresql@17
```

`postgresql@17` is keg-only, so its binaries are not linked into `/opt/homebrew/bin`. Add them:

```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zshrc
exec zsh -l
psql --version
```

Expect `psql (PostgreSQL) 17.x`.

Start it as a background service and create the two databases:

```bash
brew services start postgresql@17
createdb haloflow_dev
createdb haloflow_test_m01
psql -l
```

`psql -l` should list both new databases. The test database name must start with `haloflow_test`.

Homebrew's `initdb` creates a superuser role named after your macOS account and configures
`trust` auth for local connections, so no password is needed. Confirm your role name:

```bash
whoami
psql -d postgres -c '\du'
```

`whoami` is the value for `YOURUSER` in `.env`; your role should show `Superuser` in `\du`.

> **Why the test database name matters:** the harness refuses to initialize any database whose name
> does not start with `haloflow_test`, and it drops schemas and roles inside it on every run. Never
> point `HALOFLOW_TEST_DATABASE_URL` at `haloflow_dev` or a shared/cloud database.

### Optional: Postgres in Docker instead

If you would rather not run Postgres natively:

```bash
docker run -d --name haloflow-pg17 -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=haloflow_test_m01 postgres:17
```

Then use `postgresql://postgres:postgres@localhost:5432/haloflow_test_m01`, which mirrors CI exactly.
This requires Docker Desktop (check its licence terms for business use).

---

## 3. Get the code

Already cloned at `~/Documents/GitHub/ClinicFlow`. To refresh:

```bash
cd ~/Documents/GitHub/ClinicFlow
git pull
```

Fresh machine:

```bash
mkdir -p ~/Documents/GitHub && cd ~/Documents/GitHub
git clone https://github.com/guotianpei/ClinicFlow.git
```

Authenticate pushes with the GitHub CLI (`brew install gh && gh auth login`) or an SSH key.

---

## 4. Virtual environment and dependencies

```bash
cd ~/Documents/GitHub/ClinicFlow/haloflow
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

`.venv/` is already gitignored. `-e '.[dev]'` installs the runtime dependencies plus
pytest, pytest-asyncio, pytest-httpx, ruff and mypy — the same line CI runs.

Or, with the Makefile added alongside this guide: `make install`.

---

## 5. Environment variables

Copy the template and edit it:

```bash
cd ~/Documents/GitHub/ClinicFlow/haloflow
cp .env.example .env
```

Then edit `.env` and replace every `YOURUSER` with the output of `whoami`.

Three connection strings, three different consumers — this is the part that most often goes wrong:

| Variable | Form | Read by |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://…` | `config.py` → `database.py` (the running app) |
| `HALOFLOW_MIGRATION_DATABASE_URL` | `postgresql://…` | `alembic/env.py` via `os.getenv` (rewritten to `+psycopg`) |
| `HALOFLOW_TEST_DATABASE_URL` | `postgresql://…` | `tests/m01/test_gateway_postgres.py` via `os.getenv` |

**Important:** pydantic-settings reads `.env` for `Settings` fields but does **not** export it into the
process environment. Alembic and the M01 tests use `os.getenv`, so putting those two variables in
`.env` alone is not enough — source the file first:

```bash
set -a; . ./.env; set +a
```

`make migrate` / `make test` do this for you.

`DATABASE_URL` is the only field with no default: importing `haloflow.main` fails without it.
The M01 tests do not import `config.py`, so they run without a `.env` as long as
`HALOFLOW_TEST_DATABASE_URL` is exported.

Vendor keys (athenahealth, Notifyre, Stedi) default to empty strings and are only needed for the
code paths that call those APIs — leave them blank for M01 work.

**`PRIORITY_PAYER_IDS` is comma-separated**, e.g. `ANTHM,UHC,SNTARA` — not a JSON array. Commenting
the line out is also fine; the code default is the same three payers.

> **Background.** pydantic-settings classifies a `list[str]` field as complex and runs `json.loads()`
> on the raw env value *before* any validator, which made the comma form raise
> `SettingsError: error parsing value for field "priority_payer_ids"` and left the
> `parse_payer_ids(mode="before")` validator unreachable. `config.py` now annotates the field
> `Annotated[list[str], NoDecode]`, which suppresses that pre-decode for this field only, so the
> validator receives the raw string and splits it. Because the value is no longer JSON-decoded, a
> JSON array in `.env` would be split on its own commas and silently yield garbage — use the comma
> form.

---

## 6. Run the migrations

```bash
cd ~/Documents/GitHub/ClinicFlow/haloflow
source .venv/bin/activate
set -a; . ./.env; set +a
alembic upgrade head
```

Run this from `haloflow/` — `alembic.ini` uses `script_location = alembic` relative to the CWD.
`sqlalchemy.url` in `alembic.ini` is a placeholder and is ignored; the URL always comes from
`HALOFLOW_MIGRATION_DATABASE_URL`.

Verify:

```bash
psql -d haloflow_dev -c '\dn'
psql -d haloflow_dev -c '\dt shared.*'
psql -d haloflow_dev -c "\du haloflow_*"
```

Expect a `shared` schema, its tables, and the nine `haloflow_*` roles.

You do **not** need to migrate `haloflow_test_m01` by hand — the pytest session fixture applies
`alembic upgrade head` to it itself.

---

## 7. Run the app

```bash
cd ~/Documents/GitHub/ClinicFlow/haloflow
source .venv/bin/activate
set -a; . ./.env; set +a
uvicorn haloflow.main:app --reload --port 8000
```

- Health: <http://localhost:8000/health> → `{"status":"ok","tenant":"pilot-clinic-1","env":"development"}`
- OpenAPI docs: <http://localhost:8000/docs>

Startup also starts APScheduler and logs `Scheduler started with N jobs`. The daily jobs
(reminders 08:00, eligibility 08:05, rebook 09:00, no-response 20:00) and the interval jobs
(inbound fax every 15 min, delivery confirmation every 30 min) will fire against whatever vendor
credentials are in `.env` — keep those blank locally unless you are deliberately testing an integration.

Or: `make run`.

---

## 8. Tests

```bash
cd ~/Documents/GitHub/ClinicFlow/haloflow
source .venv/bin/activate
set -a; . ./.env; set +a

pytest -q
pytest -q -m "not postgres"
pytest -q -m postgres
pytest tests/m01 -q
```

In order: everything; unit tests only, no database needed; the M01 isolation suite only; exactly what CI runs.

Run pytest **from `haloflow/`** — the Postgres fixture builds `Config("alembic.ini")` from a
relative path.

Behaviour to expect:

- `HALOFLOW_TEST_DATABASE_URL` unset → the Postgres tests **skip**; the rest still run.
- Server older than PostgreSQL 17 → the suite **fails** with `M01 tests require PostgreSQL 17+`.
- Database not named `haloflow_test*` → **fails** with `Refusing to initialize a database not named haloflow_test*`.
- Role cannot create roles/schemas → `InsufficientPrivilege` during setup.

Each session drops and recreates `tenant_aaaaaaaa`, `tenant_bbbbbbbb`, and the
`haloflow_test_runtime_login` role inside the test database. That is expected and is why the
name guard exists.

Or: `make test`, `make test-unit`, `make test-pg`.

---

## 9. Lint and type checks (the rest of CI)

```bash
ruff check src/haloflow/m01 tests/m01 alembic
mypy src/haloflow/m01
```

These are the exact scopes in `.github/workflows/m01.yml`. mypy runs in `strict` mode with
`ignore_missing_imports = true`; ruff is line-length 100, target py311, rules `E,F,I,UP,B,SIM`.

`make check` runs lint + type + tests, i.e. the full CI gate, before you push.

---

## 10. Editor setup (VS Code)

```bash
brew install --cask visual-studio-code
code ~/Documents/GitHub/ClinicFlow
```

Extensions: **Python** and **Pylance** (Microsoft), **Ruff** (Astral), optionally **Mypy Type Checker**.

Select the interpreter: `Cmd+Shift+P` → *Python: Select Interpreter* →
`~/Documents/GitHub/ClinicFlow/haloflow/.venv/bin/python`.

Suggested `.vscode/settings.json` at the repo root (create it if you want it; it is not committed today):

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/haloflow/.venv/bin/python",
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["tests"],
  "python.testing.cwd": "${workspaceFolder}/haloflow",
  "python.envFile": "${workspaceFolder}/haloflow/.env",
  "[python]": { "editor.defaultFormatter": "charliermarsh.ruff" }
}
```

`python.envFile` makes VS Code's test runner see `HALOFLOW_TEST_DATABASE_URL`, which a plain
terminal run would need `set -a; . ./.env; set +a` for.

---

## 11. Claude Code on the local checkout

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
cd ~/Documents/GitHub/ClinicFlow && claude
```

Native installs auto-update. Homebrew is the alternative (`brew install --cask claude-code`), but
does not auto-update. Requires a Pro, Max, Team, Enterprise, or Console account; log in through the
browser prompt on first run.

Consider adding a `CLAUDE.md` at the repo root capturing the conventions this project already
follows — commands are run from `haloflow/`, requirements → architecture → unit tests → code before
any new module, project docs live in `documentation/` — so every session starts with them.

---

## 12. Daily loop

```bash
cd ~/Documents/GitHub/ClinicFlow/haloflow
source .venv/bin/activate
set -a; . ./.env; set +a
git pull
make check
make run
```

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `RuntimeError: HALOFLOW_MIGRATION_DATABASE_URL is required` | `.env` not sourced into the environment | `set -a; . ./.env; set +a` |
| `pydantic_core.ValidationError: database_url Field required` | no `.env`, or you ran from outside `haloflow/` | `cp .env.example .env`; `cd haloflow` |
| `SettingsError: error parsing value for field "priority_payer_ids"` | the `NoDecode` annotation is missing from `config.py` | see section 5; use the comma form `ANTHM,UHC,SNTARA` |
| Postgres tests silently skipped | `HALOFLOW_TEST_DATABASE_URL` unset | export it (see above) |
| `M01 tests require PostgreSQL 17+` | an older Postgres is first on PATH | `psql --version`; put `/opt/homebrew/opt/postgresql@17/bin` ahead in PATH |
| `Refusing to initialize a database not named haloflow_test*` | test URL points at the wrong database | point it at `haloflow_test_m01` |
| `InsufficientPrivilege` during test setup | role lacks CREATEROLE | connect as your Homebrew superuser role, or `createuser -s <role>` |
| `psql: could not connect to server` | service not running | `brew services list`; `brew services restart postgresql@17` |
| `alembic: command not found` | venv not activated | `source .venv/bin/activate` |
| `FileNotFoundError: alembic.ini` | wrong CWD | run alembic and pytest from `haloflow/` |
| Port 8000 in use | another uvicorn | `lsof -ti:8000 \| xargs kill` or `--port 8001` |

---

## 14. Local safety rules

- **No PHI locally.** `haloflow_dev` is for synthetic data only.
- `.env` is gitignored — keep it that way; never paste real secrets into docs, screenshots, or chats.
- The carried-forward item to **rotate the athenahealth client secret** (it appeared in screenshots in
  an earlier session) still stands; rotate before putting a real value in `.env`.
- The local Postgres is a dev instance with `trust` auth: do not expose port 5432 beyond localhost.
- `.DS_Store` files are currently tracked in this repo and show up as dirty on every `git status`.
  Worth adding `.DS_Store` to the root `.gitignore` and `git rm --cached` the tracked ones.
