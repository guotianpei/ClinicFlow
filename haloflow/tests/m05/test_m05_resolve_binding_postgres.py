"""B4 apply event binding, races, grants, compatibility and adapters (PostgreSQL 17).

Red review REVISE 4c5b82fb (Codex, 2026-11-01), tests only:

- MR-10   R1: the independent one-field matrix through the synthetic persisted-event
          seam (`resolve_support.synthetic_event`, setup-controlled), a positive control,
          unknown / other-tenant / wrong-type (real producer) events, genuine reuse
          (declared replica-mode state-corruption seam) and reuse precedence.
- RA-R    R2: actor standing races on resolve (the RA-1 / RA-2 pattern).
- G-B4    R2: the exact grant set after B4 on the M05 tables and patient_events, plus
          ME-03 (the M05 owner executes exactly the envelope read among m02_*).
- COMPAT-01a R2: a disposable tenant installed with the frozen pre-B4 plain-string
          units (full old-object installation), then migrated with the current
          registry → MIGRATION_CHECKSUM_DRIFT, ledger and catalogue unchanged.
- COMPAT-01b old-checksum cases for t009/t011 (ruling 4f51be67): a ledger recording
          the pre-B4 checksum → MIGRATION_CHECKSUM_DRIFT, ledger/catalogue unchanged.
- MX-01   R2: zero Notifyre / athenahealth / outbound HTTP during resolve and note
          read, with a positive spy control.

Expected classes/codes are declared in the red gate request v2. RED: the resolve
link type has no resolver row yet (the synthetic seam fails its FK at setup), the
SQL does not accept the resolve payload, the service routes raise NotImplementedError,
and the B4 grants and unit changes do not exist.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import m02_support
import m05_support as m05
import psycopg
import pytest
import resolve_support as rs
import service_support as sv
import staff_support as st
from psycopg import sql
from test_m05_assignee_eligibility_postgres import CHANGES, _admin_open, _change_command

pytestmark = pytest.mark.postgres

OTHER_PATIENT = "00000000-0000-4000-8000-0000000b2f02"


def _other_event(install: st.StaffInstall, item: UUID) -> Any:
    """A callable for `synthetic_event`: an existing event that is not the item's source."""

    def pick(conn: Any) -> str:
        source = rs.item_view(install, item)["source_event_id"]
        row = conn.execute(sql.SQL(
            "SELECT event_id::text FROM {}.patient_events WHERE event_id <> %s"
            " ORDER BY append_sequence LIMIT 1").format(sql.Identifier(install.schema)),
            (source,)).fetchone()
        assert row is not None, "SETUP: no other event"
        return str(row[0])

    return pick


def _link(item: UUID, relationship: str = rs.RELATIONSHIP) -> dict[str, str]:
    return {"link_type": rs.ITEM_LINK, "link_id": str(item), "relationship": relationship}


def _meta(install: st.StaffInstall, item: UUID, **changes: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {"outcome_code": "rescheduled", "assertion_kind": "staff",
                            "item_kind": rs.item_view(install, item)["item_kind"]}
    for k, v in changes.items():
        if v is None:
            meta.pop(k)
        else:
            meta[k] = v
    return meta


def _matrix(install: st.StaffInstall, item: UUID, other: UUID) -> dict[str, tuple[str, Any]]:
    """case → (field, value): exactly one envelope predicate is false in each case."""

    other_kind = ("cancel_request" if rs.item_view(install, item)["item_kind"]
                  != "cancel_request" else "reschedule_request")
    return {
        "type": ("event_type", None),
        "version": ("contract_version", 2),
        "level": ("event_level", "submission"),
        "status": ("status", "accepted"),
        "operation-id": ("operation_id", "NEW"),
        "occurred-at-only": ("occurred_at", "2026-11-01T12:00:00+00:00"),
        "time-quality-only": ("source_time_quality", "estimated"),
        "actor": ("actor_id", m05.S2),
        "patient": ("patient_id", OTHER_PATIENT),
        "causation": ("causation_event_id", _other_event(install, item)),
        "links-none": ("links", []),
        "links-extra": ("links", [_link(item), _link(other)]),
        "links-relationship": ("links", [_link(item, "subject")]),
        "links-other-item": ("links", [_link(other)]),
        "metadata-outcome": ("metadata", _meta(install, item, outcome_code="cancelled_in_ehr")),
        "metadata-assertion": ("metadata", _meta(install, item, assertion_kind="workload")),
        "metadata-item-kind": ("metadata", _meta(install, item, item_kind=other_kind)),
        "metadata-missing-key": ("metadata", _meta(install, item, item_kind=None)),
        "dedupe": ("producer_dedupe_key", "m05r1:" + "0" * 64),
    }


MATRIX_CASES = (
    "type", "version", "level", "status", "operation-id", "occurred-at-only",
    "time-quality-only", "actor", "patient", "causation", "links-none", "links-extra",
    "links-relationship", "links-other-item", "metadata-outcome", "metadata-assertion",
    "metadata-item-kind", "metadata-missing-key", "dedupe",
)


async def test_mr10_control_valid_synthetic_event_applies(staff_install):
    """Positive control: the seam's baseline event is accepted, so every matrix row
    differs from an accepted event in exactly one predicate."""

    item = await rs.claimed(staff_install)
    key = uuid4()
    event_id = await asyncio.to_thread(rs.synthetic_event, staff_install, item, key)
    result = await asyncio.to_thread(st.call, staff_install, "m05_apply_action",
                                     rs.apply_command(item, key, event_id))
    assert result["outcome"] == "applied"
    assert result["snapshot"]["state"] == "resolved"


@pytest.mark.parametrize("case", MATRIX_CASES)
async def test_mr10_one_predicate_false_is_invalid(staff_install, case):
    item, other = await rs.claimed(staff_install), await rs.claimed(staff_install)
    key = uuid4()
    field, value = _matrix(staff_install, item, other)[case]
    event_id = await asyncio.to_thread(rs.synthetic_event, staff_install, item, key,
                                       field=field, value=value)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, key, event_id)))
    assert sv.full_state(staff_install) == before


async def test_mr10_workload_principal_is_invalid(staff_install):
    item = await rs.claimed(staff_install)
    key = uuid4()
    # actor_id null + workload_id set: one predicate (the attributed actor), two columns.
    event_id = await asyncio.to_thread(_workload_event, staff_install, item, key)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, key, event_id)))
    assert sv.full_state(staff_install) == before


def _workload_event(install: st.StaffInstall, item: UUID, key: UUID) -> UUID:
    """The baseline with the principal swapped to a workload. Built through the seam
    with the actor column cleared; the seam's control then shows {actor_id,
    workload_id} differ, which this wrapper asserts explicitly."""

    import json
    import os

    from psycopg.types.json import Jsonb

    base = rs.baseline_event(install, item, key)
    event_id = str(uuid4())

    with psycopg.connect(install.ids.admin) as conn, conn.transaction():
        conn.execute(sql.SQL(
            "INSERT INTO {}.patient_events (event_id, patient_id, event_type,"
            " contract_version, event_level, status, correlation_id, causation_event_id,"
            " producer_dedupe_key, content_fingerprint, producer_service, actor_id,"
            " workload_id, auth_method, source_time_quality, received_at, metadata)"
            " VALUES (%s, %s, %s, 1, 'observation', 'completed', %s, %s, %s, %s, %s, NULL,"
            " %s, 'test', 'absent', statement_timestamp(), %s)").format(
            sql.Identifier(install.schema)),
            (event_id, base["patient_id"], sv.RESOLUTION_TYPE, str(uuid4()),
             base["causation_event_id"], base["producer_dedupe_key"], os.urandom(32),
             sv.PRODUCER, m05.W1, Jsonb(base["metadata"])))
        conn.execute(sql.SQL(
            "INSERT INTO {}.patient_event_links VALUES (%s, %s, %s, %s)").format(
            sql.Identifier(install.schema)),
            (event_id, rs.ITEM_LINK, str(item), rs.RELATIONSHIP))
        persisted = rs._persisted(conn, install.schema, event_id)
    differs = {f for f in base
               if json.dumps(persisted[f], sort_keys=True) != json.dumps(base[f], sort_keys=True)}
    assert differs == {"actor_id", "workload_id"}, ("SETUP CONTROL", differs)
    return UUID(event_id)


# Contract amendment A-META (Codex REVISE 5ac5b9f9 item 2): apply's metadata predicate
# is on the D6 FILTERED envelope: `metadata_paths` must be exactly the three allowlisted
# paths with the expected values. Completeness of the PERSISTED key set is not observed
# by apply; it is guaranteed by the producer: t009 step 7 refuses any key outside the
# contract's closed schema and any missing required key (COMMAND_MALFORMED), the
# resolution schema closes the set to exactly these three, and patient_events is
# INSERT-only through the append owner and immutable afterwards. X-M1..4 pin that
# guarantee for THIS contract; no new M02 signal, read or grant.

RESOLUTION_METADATA_KEYS = ("assertion_kind", "item_kind", "outcome_code")


def test_xm1_resolution_contract_schema_and_allowlist_are_closed(staff_install):
    from test_m05_create_postgres import _ready

    _ready(staff_install)
    schema = sql.Identifier(staff_install.schema)
    ((meta_schema,),) = st.admin_rows(staff_install, sql.SQL(
        "SELECT metadata_json_schema FROM {}.event_contracts WHERE event_type = %s"
        " AND version = 1").format(schema), (sv.RESOLUTION_TYPE,))
    assert meta_schema["additionalProperties"] is False
    assert sorted(meta_schema["required"]) == list(RESOLUTION_METADATA_KEYS)
    assert sorted(meta_schema["properties"]) == list(RESOLUTION_METADATA_KEYS)
    paths = st.admin_rows(staff_install, sql.SQL(
        "SELECT json_path FROM {}.m02_envelope_path_allowlist WHERE consumer_module = 'm05'"
        " AND event_type = %s AND contract_version = 1 ORDER BY json_path").format(schema),
        (sv.RESOLUTION_TYPE,))
    assert [p for (p,) in paths] == list(RESOLUTION_METADATA_KEYS)


@pytest.mark.parametrize("case", ["extra-key", "missing-key"])
async def test_xm2_xm3_producer_refuses_non_closed_metadata(staff_install, case):
    """The persisted-key-set guarantee, on the real producer for this contract."""

    item = await rs.claimed(staff_install)
    meta = _meta(staff_install, item, **({"note": "x"} if case == "extra-key"
                                         else {"item_kind": None}))
    before = sv.full_state(staff_install)
    with pytest.raises(rs.obs().AppendFailed) as caught:
        await rs.append_resolution(staff_install, item, uuid4(), metadata=meta)
    assert caught.value.reason_code == "COMMAND_MALFORMED"
    assert sv.full_state(staff_install) == before


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
@pytest.mark.parametrize("table", ["patient_events", "patient_event_links"])
def test_xm4_event_evidence_is_immutable(staff_install, table, operation):
    """Fact 4 of A-META-2, pinned here (no existing part A row covers patient_events):
    even the table owner's UPDATE/DELETE is refused 0A000 by m02_evidence_reject."""

    from test_m05_create_postgres import _ready, _source

    _ready(staff_install)
    event_id = _source(staff_install)
    target = sql.SQL("{}.{}").format(sql.Identifier(staff_install.schema),
                                     sql.Identifier(table))
    statement = (sql.SQL("UPDATE {} SET event_id = event_id WHERE event_id = %s")
                 if operation == "UPDATE" else sql.SQL("DELETE FROM {} WHERE event_id = %s"))
    with psycopg.connect(staff_install.ids.admin) as conn, \
            pytest.raises(psycopg.Error) as caught, conn.transaction():
        conn.execute(statement.format(target), (event_id,))
    assert caught.value.sqlstate == "0A000"
    assert caught.value.diag.message_primary == "event evidence is immutable"


async def test_mr10_unknown_event_is_invalid(staff_install):
    item = await rs.claimed(staff_install)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, uuid4(), uuid4())))
    assert sv.full_state(staff_install) == before


async def test_mr10_other_tenant_event_identical_to_unknown(staff_install, staff_foreign_install):
    foreign_item = await rs.claimed(staff_foreign_install)
    key = uuid4()
    foreign_event = await asyncio.to_thread(rs.synthetic_event, staff_foreign_install,
                                            foreign_item, key)
    item = await rs.claimed(staff_install)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, key, foreign_event)))
    assert sv.full_state(staff_install) == before


async def test_mr10_real_other_type_event_is_invalid(staff_install):
    from test_m05_create_postgres import _source

    item = await rs.claimed(staff_install)
    other = await asyncio.to_thread(_source, staff_install)  # a real M02 test observation
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, uuid4(), UUID(other))))
    assert sv.full_state(staff_install) == before


def _corrupt_back_to_claimed(install: st.StaffInstall, item: UUID, key: UUID) -> None:
    """DECLARED STATE-CORRUPTION SEAM (replica mode; the PA precedent at
    test_m02_part_a_service_postgres.py L682): remove the action record and return the
    item to its claimed state, KEEPING the history row that references the event. Only
    a corrupted (or racing) state can reach the reuse check with every binding true.

    Narrow scope (ruling 4f51be67): `SET LOCAL` inside one transaction, exactly one
    action row and one item row, preconditions asserted first, and the setting proven
    restored on the same session afterwards."""

    schema = sql.Identifier(install.schema)
    with psycopg.connect(install.ids.admin) as conn:
        with conn.transaction():
            state = conn.execute(sql.SQL(
                "SELECT state, version FROM {}.m05_followup_items WHERE item_id = %s").format(
                schema), (str(item),)).fetchone()
            assert state == ("resolved", 3), ("PRECONDITION", state)
            conn.execute("SET LOCAL session_replication_role = replica")
            deleted = conn.execute(sql.SQL(
                "DELETE FROM {}.m05_followup_actions WHERE item_id = %s"
                " AND client_action_id = %s").format(schema), (str(item), str(key))).rowcount
            updated = conn.execute(sql.SQL(
                "UPDATE {}.m05_followup_items SET state = 'open', outcome_code = NULL,"
                " version = 2 WHERE item_id = %s").format(schema), (str(item),)).rowcount
            assert (deleted, updated) == (1, 1), ("PRECONDITION", deleted, updated)
        restored = conn.execute("SHOW session_replication_role").fetchone()
        assert restored == ("origin",), ("RESTORATION", restored)
    history = rs.rows(install, "m05_followup_history", item)
    assert [h["action"] for h in history].count("resolve") == 1, "PRECONDITION: history kept"


async def test_mr10_genuine_reuse_is_reused(staff_install):
    item = await rs.claimed(staff_install)
    key = uuid4()
    first = await rs.resolve_once(staff_install, sv.TestNoteKeys(),
                                  rs.resolve_action(item, 2, key=key))
    assert first.outcome == "applied"
    (event,) = rs.events_for(staff_install, item)
    await asyncio.to_thread(_corrupt_back_to_claimed, staff_install, item, key)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_REUSED",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, key, UUID(event["event_id"]))))
    assert sv.full_state(staff_install) == before


async def test_mr10_precedence_binding_before_reuse(staff_install):
    """Another item's already-referenced event: binding fails first → INVALID, not
    REUSED (declared precedence: every §3 predicate, then reuse)."""

    used_item = await rs.claimed(staff_install)
    result = await rs.resolve_once(staff_install, sv.TestNoteKeys(),
                                   rs.resolve_action(used_item, 2))
    assert result.outcome == "applied"
    (event,) = rs.events_for(staff_install, used_item)
    item = await rs.claimed(staff_install)
    before = sv.full_state(staff_install)
    st.expect_code("RESOLUTION_EVENT_INVALID",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, uuid4(), UUID(event["event_id"]))))
    assert sv.full_state(staff_install) == before


# ---------------------------------------------------------------------------
# RA-R: actor standing races on resolve (RA-1 / RA-2 pattern)
# ---------------------------------------------------------------------------


def _actor_with_claim(install: st.StaffInstall) -> tuple[str, UUID]:
    from test_m05_actions_postgres import _cmd
    from test_m05_service_postgres import _item

    actor = st.new_uuid()
    st.seed_staff(install, actor, name="Resolver")
    item = _item(install)
    st.call(install, "m05_apply_action", _cmd(str(item), 1, "claim", actor=actor))
    return actor, item


@pytest.mark.parametrize("change", CHANGES)
async def test_rar1_actor_change_committed_before_resolve(staff_install, change):
    actor, item = await asyncio.to_thread(_actor_with_claim, staff_install)
    key = uuid4()
    event_id = await asyncio.to_thread(rs.synthetic_event, staff_install, item, key,
                                       actor=actor)
    await asyncio.to_thread(st.admin_op, staff_install, _change_command(change, actor))
    before = sv.full_state(staff_install)
    st.expect_code("NOT_AUTHORIZED",
                   lambda: st.call(staff_install, "m05_apply_action",
                                   rs.apply_command(item, key, event_id, actor=actor)))
    assert sv.full_state(staff_install) == before


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
@pytest.mark.parametrize("change", CHANGES)
async def test_rar2_actor_change_uncommitted_resolve_waits(staff_install, change, rollback):
    actor, item = await asyncio.to_thread(_actor_with_claim, staff_install)
    key = uuid4()
    event_id = await asyncio.to_thread(rs.synthetic_event, staff_install, item, key,
                                       actor=actor)
    command = rs.apply_command(item, key, event_id, actor=actor)

    def race() -> dict[str, Any]:
        admin_conn, _ = _admin_open(staff_install, _change_command(change, actor))
        apply_conn = st.runtime_connection(staff_install)
        try:
            st.set_tenant(apply_conn, staff_install)
            with st.background(lambda: st.call_on(apply_conn, staff_install,
                                                  "m05_apply_action", command)) as box:
                st.wait_until_blocked_by(staff_install, apply_conn.info.backend_pid,
                                         admin_conn.info.backend_pid)
                (admin_conn.rollback if rollback else admin_conn.commit)()
            (apply_conn.rollback if "error" in box else apply_conn.commit)()
        finally:
            admin_conn.close()
            apply_conn.close()
        return box

    box = await asyncio.to_thread(race)
    if rollback:
        assert "error" not in box, box.get("error")
        assert box["result"]["outcome"] == "applied"
        return
    error = box.get("error")
    assert isinstance(error, psycopg.Error) and error.sqlstate == "P0001"
    assert error.diag.message_primary == "NOT_AUTHORIZED"
    assert rs.rows(staff_install, "m05_followup_history", item)[-1]["action"] != "resolve"


# ---------------------------------------------------------------------------
# G-B4: the exact grant set after B4, and ME-03
# ---------------------------------------------------------------------------

_ITEM_COLUMNS = ("item_id", "source_event_id", "item_kind", "patient_id", "state",
                 "assignee_id", "outcome_code", "version", "create_digest", "created_at")
_HISTORY_COLUMNS = ("history_id", "item_id", "seq", "action", "actor_id", "before", "after",
                    "assertion_kind", "created_at", "resolution_event_id")
_ACTION_COLUMNS = ("item_id", "client_action_id", "actor_id", "requested_action", "digest",
                   "hmac_key_version", "event_id", "result_snapshot", "created_at")
_NOTE_COLUMNS = ("note_id", "item_id", "client_action_id", "nonce", "ciphertext",
                 "enc_key_version", "created_at")
_INTAKE_COLUMNS = ("source_event_type", "source_contract_version", "discriminator_path",
                   "discriminator_value", "item_kind", "producer_workload_id",
                   "active_in_production")
_READ_OWNER_EVENT_COLUMNS = ("event_id", "event_type", "contract_version", "event_level",
                             "status", "operation_id", "patient_id", "actor_id", "workload_id",
                             "causation_event_id", "producer_dedupe_key", "metadata",
                             "occurred_at", "source_time_quality")
APPEND_OWNER = "haloflow_m02_append_owner"
READ_OWNER = "haloflow_m02_read_owner"
GB4_ROLES = (m05.M05_OWNER, APPEND_OWNER, READ_OWNER, m05.RUNTIME)
GB4_M05_TABLES = ("m05_outcome_codes", "m05_intake_map", "m05_followup_items",
                  "m05_followup_history", "m05_followup_actions", "m05_followup_notes")


def _cols(role: str, table: str, privilege: str, columns: tuple[str, ...]) -> set[tuple[Any, ...]]:
    return {(role, table, c, privilege) for c in columns}


EXPECTED_GB4: set[tuple[Any, ...]] = (
    {(m05.M05_OWNER, "m05_followup_history", None, "INSERT")}
    | _cols(m05.M05_OWNER, "m05_intake_map", "SELECT", _INTAKE_COLUMNS)
    | _cols(m05.M05_OWNER, "m05_outcome_codes", "SELECT", ("outcome_code", "note_required"))
    | _cols(m05.M05_OWNER, "m05_followup_items", "INSERT", _ITEM_COLUMNS)
    | _cols(m05.M05_OWNER, "m05_followup_items", "SELECT", _ITEM_COLUMNS)
    | _cols(m05.M05_OWNER, "m05_followup_items", "UPDATE",
            ("state", "assignee_id", "version", "outcome_code"))
    | _cols(m05.M05_OWNER, "m05_followup_history", "SELECT", _HISTORY_COLUMNS)
    | _cols(m05.M05_OWNER, "m05_followup_actions", "INSERT", _ACTION_COLUMNS)
    | _cols(m05.M05_OWNER, "m05_followup_actions", "SELECT",
            ("item_id", "client_action_id", "actor_id", "digest", "result_snapshot",
             "event_id", "hmac_key_version"))
    | _cols(m05.M05_OWNER, "m05_followup_notes", "INSERT", _NOTE_COLUMNS)
    | _cols(m05.M05_OWNER, "m05_followup_notes", "SELECT", _NOTE_COLUMNS)
    | _cols(APPEND_OWNER, "m05_followup_items", "SELECT", ("item_id", "patient_id"))
    | _cols(READ_OWNER, "patient_events", "SELECT", _READ_OWNER_EVENT_COLUMNS)
)


def _grants(install: st.StaffInstall) -> set[tuple[Any, ...]]:
    tables = list(GB4_M05_TABLES)
    found = st.admin_rows(install, """
        SELECT r.rolname, c.relname, NULL::text, a.privilege_type
          FROM pg_catalog.pg_class c
         CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
          JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
         WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname = ANY(%(tables)s)
           AND r.rolname = ANY(%(roles)s)
        UNION ALL
        SELECT r.rolname, c.relname, t.attname::text, a.privilege_type
          FROM pg_catalog.pg_attribute t
          JOIN pg_catalog.pg_class c ON c.oid = t.attrelid
         CROSS JOIN LATERAL pg_catalog.aclexplode(t.attacl) a
          JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
         WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname = ANY(%(tables)s)
           AND t.attacl IS NOT NULL AND r.rolname = ANY(%(roles)s)
        UNION ALL
        SELECT r.rolname, c.relname, t.attname::text, a.privilege_type
          FROM pg_catalog.pg_attribute t
          JOIN pg_catalog.pg_class c ON c.oid = t.attrelid
         CROSS JOIN LATERAL pg_catalog.aclexplode(t.attacl) a
          JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
         WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname = 'patient_events'
           AND t.attacl IS NOT NULL AND r.rolname = ANY(%(event_roles)s)
        UNION ALL
        SELECT r.rolname, c.relname, NULL::text, a.privilege_type
          FROM pg_catalog.pg_class c
         CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
          JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
         WHERE c.relnamespace = %(schema)s::regnamespace AND c.relname = 'patient_events'
           AND r.rolname = ANY(%(event_roles)s)
        """, {"schema": install.schema, "tables": tables, "roles": list(GB4_ROLES),
              "event_roles": [m05.M05_OWNER, READ_OWNER, m05.RUNTIME]})
    return {tuple(row) for row in found}


def test_gb4_exact_grant_set(staff_install):
    """Exact on the six M05 tables for the M05 owner, append owner, read owner and
    runtime; exact on patient_events for the M05 owner, read owner and runtime (the
    append owner's patient_events grants are part A's, pinned by part A's rows)."""

    from test_m05_create_postgres import _ready

    _ready(staff_install)
    actual = _grants(staff_install)
    assert actual - EXPECTED_GB4 == set(), sorted(actual - EXPECTED_GB4, key=str)
    assert EXPECTED_GB4 - actual == set(), sorted(EXPECTED_GB4 - actual, key=str)


def test_gb4_me03_and_resolver_registration(staff_install):
    from test_m05_create_postgres import _ready

    _ready(staff_install)
    executable = [s for s in m05.M02_FUNCTIONS if st.admin_rows(
        staff_install, "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE')",
        (m05.M05_OWNER, f"{staff_install.schema}.{s}"))[0][0]]
    assert tuple(executable) == m05.M05_EXECUTABLE_M02
    resolver = st.admin_rows(staff_install, sql.SQL(
        "SELECT resolver_id, relationship, resolver_kind, is_active FROM {}.m02_link_resolvers"
        " WHERE resolver_id = %s").format(sql.Identifier(staff_install.schema)),
        (rs.ITEM_LINK,))
    assert resolver == [(rs.ITEM_LINK, rs.RELATIONSHIP, "module_owned", True)]


# ---------------------------------------------------------------------------
# COMPAT-01: a retained pre-B4 installation is refused by the drift guard
# ---------------------------------------------------------------------------


def _pre_b4_registry() -> Any:
    import pre_b4_units as pre

    from haloflow.composition import (
        APPROVED_EXECUTION_ROLES,
        APPROVED_INSTALLED_STATE_PROFILES,
        APPROVED_TENANT_MIGRATIONS,
    )
    from haloflow.m01.provisioning.units import build_tenant_migration_registry
    from haloflow.m01.staff.units import STAFF_GATEWAY_PROFILES, STAFF_TENANT_MIGRATIONS
    from haloflow.m05.gateways import M05_GATEWAY_PROFILES
    from haloflow.m05.units import M05_TENANT_MIGRATIONS

    def frozen(group: Any) -> dict[str, Any]:
        return {k: pre.PRE_B4_UNITS.get(k, v) for k, v in group.items()}

    return build_tenant_migration_registry(
        *(frozen(g) for g in APPROVED_TENANT_MIGRATIONS),
        frozen(M05_TENANT_MIGRATIONS),
        STAFF_TENANT_MIGRATIONS,
        approved_execution_roles=APPROVED_EXECUTION_ROLES | {m05.M05_OWNER, st.STAFF_OWNER},
        installed_state_profiles={**APPROVED_INSTALLED_STATE_PROFILES,
                                  **M05_GATEWAY_PROFILES, **STAFF_GATEWAY_PROFILES},
    )


@pytest.fixture
def compat_tenant(m02_ids, m05_reset):
    tenant = m02_support.tenant_for("b4:compat01:disposable")
    m05_reset(tenant)
    try:
        yield tenant
    finally:
        m05_reset(tenant)


def _catalogue(ids: Any, schema: str) -> list[tuple[Any, ...]]:
    return m02_support.admin_all(ids, """
        SELECT 'class', c.relname, c.relkind::text, pg_catalog.md5(coalesce(c.relacl::text, ''))
          FROM pg_catalog.pg_class c WHERE c.relnamespace = %(s)s::regnamespace
        UNION ALL
        SELECT 'proc', p.proname, pg_catalog.pg_get_function_identity_arguments(p.oid),
               pg_catalog.md5(p.prosrc || coalesce(p.proacl::text, ''))
          FROM pg_catalog.pg_proc p WHERE p.pronamespace = %(s)s::regnamespace
        UNION ALL
        SELECT 'constraint', n.conname, n.contype::text,
               pg_catalog.md5(pg_catalog.pg_get_constraintdef(n.oid))
          FROM pg_catalog.pg_constraint n WHERE n.connamespace = %(s)s::regnamespace
        ORDER BY 1, 2, 3, 4
        """, {"s": schema})


LEDGER = ("SELECT migration_id, state, attempt, checksum, sanitized_error_code, completed_at"
          " FROM shared.schema_migrations WHERE tenant_id = %s ORDER BY migration_id")


def test_compat01a_retained_pre_b4_install_refused_as_drift(m02_ids, compat_tenant):
    import hashlib

    import pre_b4_units as pre

    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning import MIGRATOR_ROLE, TenantMigrationRunner
    from haloflow.m01.provisioning.codes import SanitizedErrorCode

    # Control: the frozen text really is the pre-change unit.
    pre_registry = _pre_b4_registry()
    pre_units = {u.migration_id: u for u in pre_registry}
    for migration_id, text in pre.PRE_B4_UNITS.items():
        assert hashlib.sha256(text.encode()).hexdigest() == pre.PRE_B4_TEXT_SHA256[migration_id]
        assert pre_units[migration_id].checksum == pre.PRE_B4_CHECKSUMS[migration_id]

    m02_support.provision_sync(m02_ids, pre_registry, compat_tenant,
                               supported=st.STAFF_TEST_SUPPORTED, manifest=st.staff_manifest())
    ledger = m02_support.admin_all(m02_ids, LEDGER, (compat_tenant[0],))
    catalogue = _catalogue(m02_ids, compat_tenant[1])

    current = st.staff_registry()
    changed = {u.migration_id for u in current
               if u.migration_id in pre.PRE_B4_CHECKSUMS
               and u.checksum != pre.PRE_B4_CHECKSUMS[u.migration_id]}
    assert changed == set(pre.PRE_B4_UNITS), changed  # B4 changes all four units

    runner = TenantMigrationRunner(m02_support._connection_factory(m02_ids.logins[MIGRATOR_ROLE]),
                                   current, manifest=st.staff_manifest())
    with pytest.raises(TenantMigrationFailed) as drift:
        asyncio.run(runner.apply(tenant_id=compat_tenant[0], schema_key=compat_tenant[1]))
    assert drift.value.reason_code == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert m02_support.admin_all(m02_ids, LEDGER, (compat_tenant[0],)) == ledger
    assert _catalogue(m02_ids, compat_tenant[1]) == catalogue


@pytest.mark.parametrize("migration_id", ["t009_m02_append", "t011_m02_envelope_read"])
def test_compat01b_old_checksum_ledger_refused_as_drift(m02_ids, compat_tenant, migration_id):
    """OLD-CHECKSUM compatibility case (ruling 4f51be67), distinct from a full
    old-object installation: a disposable tenant installed with the current registry
    whose ledger records the pre-B4 checksum of `migration_id` (verified at 6a81464),
    as a retained pre-B4 tenant's ledger would. Entry point: the migration runner, as
    for any installed tenant (the provisioner refuses an active tenant by design,
    provisioner.py L192). Expect MIGRATION_CHECKSUM_DRIFT, ledger and catalogue
    unchanged."""

    import pre_b4_units as pre

    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning import MIGRATOR_ROLE, TenantMigrationRunner
    from haloflow.m01.provisioning.codes import SanitizedErrorCode

    current = st.staff_registry()
    current_checksum = {u.migration_id: u.checksum for u in current}[migration_id]
    old_checksum = pre.PRE_B4_DEFINITION_CHECKSUMS[migration_id]
    assert current_checksum != old_checksum, "B4 must change this unit's checksum"

    m02_support.provision_sync(m02_ids, current, compat_tenant,
                               supported=st.STAFF_TEST_SUPPORTED, manifest=st.staff_manifest())
    with psycopg.connect(m02_ids.admin) as conn, conn.transaction():
        recorded = conn.execute("SELECT checksum FROM shared.schema_migrations WHERE"
                                " tenant_id = %s AND migration_id = %s",
                                (compat_tenant[0], migration_id)).fetchone()
        assert recorded == (current_checksum,), ("PRECONDITION", recorded)
        changed = conn.execute("UPDATE shared.schema_migrations SET checksum = %s WHERE"
                               " tenant_id = %s AND migration_id = %s",
                               (old_checksum, compat_tenant[0], migration_id)).rowcount
        assert changed == 1
    ledger = m02_support.admin_all(m02_ids, LEDGER, (compat_tenant[0],))
    catalogue = _catalogue(m02_ids, compat_tenant[1])

    runner = TenantMigrationRunner(m02_support._connection_factory(m02_ids.logins[MIGRATOR_ROLE]),
                                   current, manifest=st.staff_manifest())
    with pytest.raises(TenantMigrationFailed) as drift:
        asyncio.run(runner.apply(tenant_id=compat_tenant[0], schema_key=compat_tenant[1]))
    assert drift.value.reason_code == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert m02_support.admin_all(m02_ids, LEDGER, (compat_tenant[0],)) == ledger
    assert _catalogue(m02_ids, compat_tenant[1]) == catalogue


# ---------------------------------------------------------------------------
# MX-01: zero vendor adapter / outbound HTTP calls
# ---------------------------------------------------------------------------


VENDOR_PREFIXES = ("haloflow.integrations", "haloflow.ehr")


class _VendorSpy:
    """MX-01 witness without importing the adapters. They read Settings at import
    (athenahealth.py L37, get_settings()), so the spy is an import blocker: any import
    of haloflow.integrations* / haloflow.ehr* during the row is recorded and refused,
    and every outbound httpx send is recorded and refused. The vendor modules must not
    be loaded already, so a cached import cannot bypass the blocker."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.abc
        import sys

        import httpx

        self.calls: list[str] = []
        loaded = sorted(m for m in sys.modules if m.startswith(VENDOR_PREFIXES))
        assert loaded == [], ("PRECONDITION: vendor modules already imported", loaded)
        spy = self

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
                if name.startswith(VENDOR_PREFIXES):
                    spy.calls.append(f"import {name}")
                    raise ImportError(f"MX-01: {name} imported")
                return None

        monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])

        def record(name: str) -> Any:
            def refuse(*args: Any, **kwargs: Any) -> Any:
                self.calls.append(name)
                raise AssertionError(f"MX-01: {name} called")

            return refuse

        monkeypatch.setattr(httpx.AsyncClient, "send", record("httpx.AsyncClient.send"))
        monkeypatch.setattr(httpx.Client, "send", record("httpx.Client.send"))


def test_mx01_spy_control(monkeypatch):
    """The spy is live: a vendor import and an httpx send are each recorded and
    refused."""

    import importlib

    import httpx

    spy = _VendorSpy(monkeypatch)
    with pytest.raises(ImportError):
        importlib.import_module("haloflow.integrations.notifyre")
    with pytest.raises(AssertionError):
        httpx.Client().send(httpx.Request("GET", "http://127.0.0.1:9/"))
    assert spy.calls == ["import haloflow.integrations", "httpx.Client.send"]


async def test_mx01_resolve_and_note_read_make_no_vendor_call(staff_install, monkeypatch):
    spy = _VendorSpy(monkeypatch)
    keys = sv.TestNoteKeys()
    item = await rs.claimed(staff_install)
    result = await rs.resolve_once(staff_install, keys,
                                   rs.resolve_action(item, 2, note="Vendor spy note."))
    assert result.outcome == "applied"
    history = await asyncio.to_thread(st.call, staff_install, "m05_get_history", str(item))
    (note_id,) = [e["note_id"] for e in history["entries"] if e["note_id"] is not None]
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.NOTE_READ}))
        note = await rs.service(rt, keys).read_note(ctx, UUID(note_id))
    assert note["text"] == "Vendor spy note."
    assert spy.calls == []
