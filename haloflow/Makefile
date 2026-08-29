# HaloFlow — local development helpers.
# Run every target from the haloflow/ directory.

SHELL := /bin/bash
VENV  := .venv
PY    := python3.12

# alembic/env.py and tests/m01 read os.environ, and pydantic-settings does not
# export .env — so source it explicitly before those commands.
LOAD_ENV = set -a; [ -f .env ] && . ./.env; set +a;

# Lint/type scope currently mirrors .github/workflows/m01.yml.
LINT_PATHS = src/haloflow/m01 tests/m01 alembic
TYPE_PATHS = src/haloflow/m01

.PHONY: help venv install run migrate revision test test-unit test-pg lint type check clean

help:
	@echo "make install    create .venv (Python 3.12) and install the package + dev extras"
	@echo "make run        start the FastAPI app on http://localhost:8000"
	@echo "make migrate    alembic upgrade head against HALOFLOW_MIGRATION_DATABASE_URL"
	@echo "make test       full pytest suite (Postgres tests skip if unconfigured)"
	@echo "make test-unit  pytest without the Postgres-backed M01 tests"
	@echo "make test-pg    only the Postgres-backed M01 isolation tests"
	@echo "make lint       ruff check"
	@echo "make type       mypy (strict)"
	@echo "make check      lint + type + test, i.e. what CI runs"
	@echo "make clean      remove .venv and tool caches"

$(VENV)/bin/python:
	$(PY) -m venv $(VENV)

venv: $(VENV)/bin/python

install: venv
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/python -m pip install -e '.[dev]'

run:
	$(LOAD_ENV) $(VENV)/bin/uvicorn haloflow.main:app --reload --port 8000

migrate:
	$(LOAD_ENV) $(VENV)/bin/alembic upgrade head

# usage: make revision m="add work_queue"
revision:
	$(LOAD_ENV) $(VENV)/bin/alembic revision -m "$(m)"

test:
	$(LOAD_ENV) $(VENV)/bin/pytest -q

test-unit:
	$(LOAD_ENV) $(VENV)/bin/pytest -q -m "not postgres"

test-pg:
	$(LOAD_ENV) $(VENV)/bin/pytest -q -m postgres

lint:
	$(VENV)/bin/ruff check $(LINT_PATHS)

type:
	$(VENV)/bin/mypy $(TYPE_PATHS)

check: lint type test

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
