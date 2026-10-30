"""UC1 part A — PA-04 staged SQL faults and PA-05(a) enqueue-omission mutant (PostgreSQL 17).

PA-04 (TC07): a test-owned trigger in a FRESH per-test tenant injects a failure at
one stage of `m02_append_observation`:
  after_event_insert  AFTER INSERT ON patient_events
  after_links         AFTER INSERT ON patient_event_links
  inside_enqueue      BEFORE INSERT ON event_handoff_outbox
  at_commit           DEFERRABLE INITIALLY DEFERRED constraint trigger on patient_events
Each stage: the append raises, every counted table is unchanged, and a previously
committed event in the same tenant is byte-identical (prior evidence intact).

PA-05(a) (TC08): in a fresh tenant ADMIN replaces the installed append function with
a mutant whose enqueue call is removed. The removal is located by the frozen marker
`-- t008:enqueue-call` (part_a.T008_ENQUEUE_MARKER) in the installed body and ends
at the first following ';'. The required-outbox oracle reports the appended event as
missing its signal row for the mutant and reports nothing for the reviewed body.
"""

import re
import uuid
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres

_STAGES = {
    "after_event_insert": ("patient_events", "AFTER INSERT", False),
    "after_links": ("patient_event_links", "AFTER INSERT", False),
    "inside_enqueue": ("event_handoff_outbox", "BEFORE INSERT", False),
    "at_commit": ("patient_events", "AFTER INSERT", True),
}


def _prepare(part_a: ModuleType, m02: ModuleType, m02_ids: Any, fresh: tuple[str, str]) -> None:
    m02.provision_sync(m02_ids, m02.production_registry(), fresh)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        assert part_a.regclass_exists(conn, f"{fresh[1]}.patient_events"), (
            f"{fresh[1]}.patient_events is not installed"
        )
        conn.execute(part_a.TEST_PROFILE_SQL.format(schema=fresh[1]))


def _command(part_a: ModuleType, m02_ids: Any, schema: str) -> dict[str, Any]:
    subject, source = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        for row in (part_a.subject_target(subject), part_a.source_target(source)):
            conn.execute(
                sql.SQL("INSERT INTO {}.m02_test_link_targets VALUES (%s,%s,%s,%s,%s)").format(
                    sql.Identifier(schema)
                ),
                row,
            )
    return part_a.observation_command(
        event_id=str(uuid.uuid4()),
        dedupe_key=f"pa04:{uuid.uuid4().hex}",
        subject_id=subject,
        source_id=source,
        principal_id=str(uuid.uuid4()),
    )


def _append(m02_ids: Any, tenant: tuple[str, str], command: dict[str, Any]) -> dict[str, Any]:
    with psycopg.connect(m02_ids.logins["haloflow_runtime"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_runtime")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant[0],))
            row = conn.execute(
                sql.SQL("SELECT {}.m02_append_observation(%s)").format(sql.Identifier(tenant[1])),
                (Jsonb(command),),
            ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def _snapshot(part_a: ModuleType, m02_ids: Any, schema: str) -> tuple[Any, ...]:
    ident = sql.Identifier(schema)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        counts = tuple(
            conn.execute(  # type: ignore[index]
                sql.SQL("SELECT count(*) FROM {}.{}").format(ident, sql.Identifier(t))
            ).fetchone()[0]
            for t in part_a.COUNTED_TABLES
        )
        rows = conn.execute(
            sql.SQL(
                "SELECT event_id, content_fingerprint, metadata FROM {}.patient_events"
                " ORDER BY event_id"
            ).format(ident)
        ).fetchall()
    return counts, [tuple(r) for r in rows]


@pytest.mark.parametrize("stage", list(_STAGES))
def test_pa04_stage_fault_rolls_back_and_prior_evidence_intact(
    part_a: ModuleType, m02: ModuleType, m02_ids: Any, m02_tenant: tuple[str, str], stage: str
) -> None:
    _prepare(part_a, m02, m02_ids, m02_tenant)
    schema = m02_tenant[1]
    prior = _command(part_a, m02_ids, schema)
    assert _append(m02_ids, m02_tenant, prior)["appended_or_existing"] == "appended"
    table, timing, deferred = _STAGES[stage]
    ident = sql.Identifier(schema)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE FUNCTION {}.pa04_fault() RETURNS trigger LANGUAGE plpgsql AS"
                " $f$BEGIN RAISE EXCEPTION USING ERRCODE = 'P0001',"
                " MESSAGE = 'pa04 injected fault'; END$f$"
            ).format(ident)
        )
        if deferred:
            conn.execute(
                sql.SQL(
                    "CREATE CONSTRAINT TRIGGER pa04_fault AFTER INSERT ON {}.{}"
                    " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {}.pa04_fault()"
                ).format(ident, sql.Identifier(table), ident)
            )
        else:
            conn.execute(
                sql.SQL(
                    "CREATE TRIGGER pa04_fault " + timing + " ON {}.{}"
                    " FOR EACH ROW EXECUTE FUNCTION {}.pa04_fault()"
                ).format(ident, sql.Identifier(table), ident)
            )
    before = _snapshot(part_a, m02_ids, schema)
    command = _command(part_a, m02_ids, schema)
    with pytest.raises(psycopg.Error) as caught:
        _append(m02_ids, m02_tenant, command)
    assert caught.value.diag.message_primary == "pa04 injected fault"
    assert _snapshot(part_a, m02_ids, schema) == before


def _installed_append_body(m02_ids: Any, schema: str) -> str:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pg_get_functiondef(to_regprocedure(%s))",
            (f"{schema}.m02_append_observation(jsonb)",),
        ).fetchone()
    assert row is not None and row[0] is not None, "m02_append_observation is not installed"
    return str(row[0])


def _outbox_oracle(
    part_a: ModuleType, m02_ids: Any, schema: str, event_ids: list[str]
) -> list[str]:
    """Appended events with no required event_signal row (the TC08 oracle)."""

    missing = []
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        for event_id in event_ids:
            row = conn.execute(
                sql.SQL(
                    "SELECT count(*) FROM {}.event_handoff_outbox WHERE source_event_id = %s"
                    " AND handoff_kind = 'event_signal'"
                ).format(sql.Identifier(schema)),
                (event_id,),
            ).fetchone()
            if row is None or row[0] != 1:
                missing.append(event_id)
    return missing


@pytest.mark.parametrize("variant", ["reviewed", "mutant_no_enqueue"])
def test_pa05a_enqueue_omission_detected_by_outbox_oracle(
    part_a: ModuleType,
    m02: ModuleType,
    m02_ids: Any,
    m02_tenant: tuple[str, str],
    variant: str,
) -> None:
    _prepare(part_a, m02, m02_ids, m02_tenant)
    schema = m02_tenant[1]
    body = _installed_append_body(m02_ids, schema)
    assert body.count(part_a.T008_ENQUEUE_MARKER) == 1, "enqueue marker missing or duplicated"
    if variant == "mutant_no_enqueue":
        pattern = re.escape(part_a.T008_ENQUEUE_MARKER) + r"[^;]*;"
        mutant, replaced = re.subn(pattern, "-- pa05a mutant: enqueue removed", body, count=1)
        assert replaced == 1
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute(mutant)  # CREATE OR REPLACE FUNCTION ... (definer, owner kept)
    command = _command(part_a, m02_ids, schema)
    result = _append(m02_ids, m02_tenant, command)
    assert result["appended_or_existing"] == "appended"
    missing = _outbox_oracle(part_a, m02_ids, schema, [command["event_id"]])
    if variant == "reviewed":
        assert missing == []
    else:
        assert missing == [command["event_id"]]  # the oracle detects the mutant
