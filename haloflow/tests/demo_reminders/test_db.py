"""D-layer: T13 (R-8), P-ID (connected identity), P-BOOT (bootstrap under ownership).

PG16 development evidence only (package v2 §4).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from haloflow_demo.db import ConnectedIdentity, check_identity, make_demo_engine
from haloflow_demo.models import DEMO_TABLES, create_demo_tables
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url
from sqlalchemy import event

EXPECTED_TABLES = {
    "reminder_records",
    "send_quota",
    "demo_settings",
    "opt_outs",
    "inbound_messages",
    "follow_up_items",
    "writeback_tasks",
    "demo_meta",
    # Day 5 (package v1r2): durable contact history, receipt snapshot, match lock
    "contact_history",
    "inbound_candidates",
    "demo_match_lock",
}


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
    return {r[0] for r in rows}


async def test_t13_create_twice_exactly_eight_tables_no_destructive_sql(
    demo_env: dict[str, str], demo_db_url: str
) -> None:
    assert set(DEMO_TABLES) == EXPECTED_TABLES
    target = validate_demo_database_url(DemoSettings.from_env(demo_env))
    engine = make_demo_engine(target)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _log(conn: Any, cursor: Any, statement: str, *a: Any) -> None:
        statements.append(statement)

    try:
        await create_demo_tables(engine, target)
        assert _tables(demo_db_url) == EXPECTED_TABLES
        first_count = len(statements)
        await create_demo_tables(engine, target)
        assert _tables(demo_db_url) == EXPECTED_TABLES
        second = statements[first_count:]
        assert not any(s.lstrip().upper().startswith("CREATE") for s in second), second
    finally:
        await engine.dispose()
    joined = "\n".join(statements).upper()
    for word in ("DROP", "TRUNCATE", "DELETE FROM"):
        assert word not in joined


async def test_t13_rebook_prompts_not_created(demo_env: dict[str, str], demo_db_url: str) -> None:
    target = validate_demo_database_url(DemoSettings.from_env(demo_env))
    engine = make_demo_engine(target)
    try:
        await create_demo_tables(engine, target)
    finally:
        await engine.dispose()
    assert "rebook_prompts" not in _tables(demo_db_url)


async def test_t13_send_quota_check_constraint(demo_env: dict[str, str], demo_db_url: str) -> None:
    target = validate_demo_database_url(DemoSettings.from_env(demo_env))
    engine = make_demo_engine(target)
    try:
        await create_demo_tables(engine, target)
    finally:
        await engine.dispose()
    with psycopg.connect(demo_db_url) as conn:
        for bad in (0, 11):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO send_quota(clinic_day, used) VALUES ('2026-10-28', %s)",
                             (bad,))
            conn.rollback()


def test_make_demo_engine_requires_validated_target() -> None:
    with pytest.raises(TypeError):
        make_demo_engine("postgresql://u@127.0.0.1/haloflow_reminders_demo")  # type: ignore[arg-type]


# ── P-ID: identity after connecting ───────────────────────────────────────────


def test_p_id_check_identity_refuses_mismatched_database() -> None:
    target = validate_demo_database_url(
        DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@127.0.0.1/haloflow_x_demo"})
    )
    with pytest.raises(StartupRefused) as exc:
        check_identity(ConnectedIdentity(database="haloflow_dev", server_addr=None), target)
    assert exc.value.reason == "demo_db_identity"


def test_p_id_check_identity_refuses_non_loopback_server() -> None:
    target = validate_demo_database_url(
        DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@127.0.0.1/haloflow_x_demo"})
    )
    with pytest.raises(StartupRefused) as exc:
        check_identity(ConnectedIdentity(database="haloflow_x_demo", server_addr="10.1.2.3"),
                       target)
    assert exc.value.reason == "demo_db_identity"


def test_p_id_check_identity_accepts_loopback_and_socket() -> None:
    target = validate_demo_database_url(
        DemoSettings.from_env({"DEMO_DATABASE_URL": "postgresql://u@127.0.0.1/haloflow_x_demo"})
    )
    check_identity(ConnectedIdentity(database="haloflow_x_demo", server_addr="127.0.0.1"), target)
    check_identity(ConnectedIdentity(database="haloflow_x_demo", server_addr="::1"), target)
    check_identity(ConnectedIdentity(database="haloflow_x_demo", server_addr=None), target)


async def test_p_id_create_tables_checks_connected_identity(
    demo_env: dict[str, str], demo_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import haloflow_demo.models as models

    target = validate_demo_database_url(DemoSettings.from_env(demo_env))
    engine = make_demo_engine(target)

    async def _fake_identity(conn: Any) -> ConnectedIdentity:
        return ConnectedIdentity(database="haloflow_dev", server_addr=None)

    monkeypatch.setattr(models, "read_identity", _fake_identity)
    try:
        with pytest.raises(StartupRefused) as exc:
            await create_demo_tables(engine, target)
    finally:
        await engine.dispose()
    assert exc.value.reason == "demo_db_identity"
    assert _tables(demo_db_url) == set()
