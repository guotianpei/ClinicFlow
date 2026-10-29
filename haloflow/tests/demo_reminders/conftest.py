"""Fixtures for the reminder demonstrator suite (tests/demo_reminders).

Layers (package v2 §3): U = unit, no database; D = PostgreSQL on a throwaway
``*_demo`` database. D tests need ``DEMO_TEST_ADMIN_URL``: a ``postgresql://``
URL for a local role that may CREATE/DROP DATABASE. Each D test gets a fresh
database named ``haloflow_t<random>_demo``, dropped afterwards. Dropping here is
test harness behaviour only; demo code never issues DROP (R-8, T13).

Evidence from Claude's container is PG16 development evidence only; it does not
establish PostgreSQL 17 behaviour (package v2 §4).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from .harness import ensure_demo_on_path

ensure_demo_on_path()

DEMO_ENV_KEYS = (
    "DATABASE_URL",
    "HALOFLOW_TEST_DATABASE_URL",
    "DEMO_DATABASE_URL",
    "DEMO_SMS_MODE",
    "DEMO_SMS_DAILY_CAP",
    "DEMO_SMS_TEST_NUMBER",
    "DEMO_SMS_SENDER",
    "DEMO_LIVE_TEST",
    "DEMO_CALLBACK_MODE",
    "DEMO_CALLBACK_SECRET",
    "DEMO_CALLBACK_BIND",
    "DEMO_STAFF_BIND",
    "DEMO_DISPATCHER",
    "DEMO_EHR_WRITE",
    "CLINIC_TZ",
)


def _admin_url() -> str:
    url = os.environ.get("DEMO_TEST_ADMIN_URL", "")
    if not url:
        pytest.skip("DEMO_TEST_ADMIN_URL is not configured (D-layer test)")
    return url


def _with_dbname(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + dbname, parts.query, ""))


def _new_fixture_db_name() -> str:
    """The name ``demo_db_url`` gives each throwaway database."""
    return f"haloflow_t{secrets.token_hex(4)}_demo"


def _drop_fixture_db(conn: psycopg.Connection[Any], name: str) -> None:
    """Teardown of ``demo_db_url``: drop the fixture's database on ``conn``
    (an autocommit admin connection)."""
    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def demo_db_url() -> Iterator[str]:
    """A fresh, empty ``*_demo`` database; yields its ``postgresql://`` URL."""
    admin = _admin_url()
    name = _new_fixture_db_name()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_dbname(admin, name)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            _drop_fixture_db(conn, name)


@pytest.fixture
def demo_env(demo_db_url: str) -> dict[str, str]:
    """A minimal valid demo environment pointing at the fresh database."""
    return {
        "DEMO_DATABASE_URL": demo_db_url,
        "DATABASE_URL": "postgresql+asyncpg://legacy@127.0.0.1:5433/haloflow_dev",
        "CLINIC_TZ": "America/New_York",
    }
