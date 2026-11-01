"""UC1 part B, tranche B2 — item creation (`m05_create_item`) on PostgreSQL 17.

Design: claude_uc1-part-b-b2-design-delta-v1 (Δ1–Δ7), ALIGNed with refinements by
Codex 20261031T233740Z (codex_b2-design-delta-v1-disposition). Package rows MC-01..07
plus the aligned refinements; MC-08 (startup) is in test_m05_intake_startup.py.

Every call goes through the intended path: the runtime login shim, SET ROLE runtime,
`app.tenant_id` set transaction-locally, the reviewed signature. Refusals are P0001
with MESSAGE = the code. Every refusal row also asserts ZERO WRITES by comparing the
full CONTENTS of the M05 tables (not counts) and the M02 event/link/outbox counts
before and after the call.

Source events come from the real `m02_append_observation` (runtime) on part A's
synthetic contract, activated by part A's test profile; the M05 intake profile is
test-only (m05_support.B2_INTAKE_PROFILE_SQL). Only the level/status/operation
variants, which the append cannot produce, are inserted directly by the admin.

Rows:
- MC-01  W1 creates for a mapped source: item + `created` history, exact contents,
         SQL-computed digest equal to the independent oracle; result closed.
- MB2-V  the digest oracle against the literal vectors (bytes and preimage).
- MC-02  exact redelivery (other transport fields) -> existing, same id, no new rows.
- MC-03  same source+kind, purpose `treatment` (both purposes allowed) ->
         DEDUPE_CONFLICT, contents unchanged.
- MB2-C  concurrent duplicate: the second blocks on the first, then `existing`
         with the same id; exactly one item and one history row.
- MB2-A  atomicity: a fault injected at the history insert rolls back the item.
- MC-04  ACTOR caller, unmapped W2, a W3-written source, an unknown id, another
         tenant's event: all SOURCE_NOT_ELIGIBLE, identical error shape.
- MC-05  STOP stand-in (`unreviewed`, unmapped), absent discriminator, two matching
         intake rows (never pick one), null patient: SOURCE_NOT_ELIGIBLE; requested
         kind differing from the single matched row: KIND_MISMATCH. Single-match
         control created.
- MC-05b a type/version without an m05 allowlist row (independent tenant, intake row
         present) -> SOURCE_NOT_ELIGIBLE; the envelope itself says ENVELOPE_UNSUPPORTED.
- MB2-L  non-observation level, non-completed status, non-null operation_id ->
         SOURCE_NOT_ELIGIBLE (Δ2 step 4).
- MC-06  closed schema, attribution types/enums/nullability -> COMMAND_MALFORMED.
         Raw duplicate JSON keys are NOT claimed here (jsonb collapses them): a
         service-boundary parser obligation, carried to the service tranche.
- MC-07  a second distinct eligible source for the same patient -> a new item; the
         earlier (admin-resolved) item byte-identical.
- MB2-I  real-row history immutability on a created item: migrator UPDATE/DELETE ->
         0A000 (rejector); M05 owner and runtime -> 42501.
- MB2-G  exact M05-owner grants: explicit ACL entries, then effective privileges.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import m02_support as m02
import m05_support as m05
import part_a_support as part_a
import psycopg
import pytest
from m05_harness import M05Install
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres

M05_TABLES_CONTENT = (
    "m05_followup_items", "m05_followup_history", "m05_followup_actions", "m05_followup_notes",
)
M02_COUNTED = ("patient_events", "patient_event_links", "event_handoff_outbox")
PATIENT = "00000000-0000-4000-8000-0000000b2f01"
PATIENT_2 = "00000000-0000-4000-8000-0000000b2f02"


# --- fixtures and helpers ----------------------------------------------------


@pytest.fixture(scope="module")
def other_install(
    request: pytest.FixtureRequest, m02_ids: m02.Identities, m05_reset: Any
) -> Iterator[M05Install]:
    """MC-05b / MC-04 foreign tenant: an independent M05 test tenant."""

    tenant = m02.tenant_for(f"{request.module.__name__}:m05:other")
    m05_reset(tenant)
    try:
        yield M05Install(m02_ids, tenant)
    finally:
        m05_reset(tenant)


def _uuid() -> str:
    return str(uuid.uuid4())


def _admin(install: M05Install, statement: Any, params: Any = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(install.ids.admin, autocommit=True) as conn:
        cursor = conn.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def _ready(install: M05Install, *, allowlist: bool = True) -> None:
    """Install M05 (in the body), then apply part A's and the B2 test profiles once."""

    install.require()
    schema = install.schema
    present = _admin(
        install,
        "SELECT pg_catalog.to_regclass(%s) IS NOT NULL",
        (f"{schema}.m02_test_link_targets",),
    )
    if present == [(False,)]:
        _admin(install, part_a.TEST_PROFILE_SQL.format(schema=schema))
        profile = m05.B2_INTAKE_PROFILE_SQL if allowlist else m05.B2_NO_ALLOWLIST_PROFILE_SQL
        _admin(install, profile.format(schema=schema))


def _source(
    install: M05Install,
    *,
    workload: str = m05.W1,
    patient: str | None = PATIENT,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append one source event through the real M02 append; return its event_id."""

    schema = install.schema
    subject, source = _uuid(), _uuid()
    for target in (part_a.subject_target(subject), part_a.source_target(source)):
        _admin(
            install,
            sql.SQL(
                "INSERT INTO {}.m02_test_link_targets VALUES (%s, %s, %s, %s, %s)"
            ).format(sql.Identifier(schema)),
            target,
        )
    event_id = _uuid()
    command = part_a.observation_command(
        event_id=event_id,
        dedupe_key=f"mb2:{uuid.uuid4().hex}",
        subject_id=subject,
        source_id=source,
        principal_id=workload,
        metadata={"label_code": "reviewed"} if metadata is None else metadata,
        patient_id=patient,
    )
    with psycopg.connect(install.ids.logins[m05.RUNTIME], autocommit=True) as conn:
        conn.execute(f"SET ROLE {m05.RUNTIME}")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
            row = conn.execute(
                sql.SQL("SELECT {}.m02_append_observation(%s)").format(sql.Identifier(schema)),
                (Jsonb(command),),
            ).fetchone()
    assert row is not None and row[0]["appended_or_existing"] == "appended", row
    return event_id


def _direct_source(install: M05Install, **overrides: Any) -> str:
    """An event the append cannot produce (level/status/operation variants), inserted
    by the admin with otherwise-eligible fields."""

    event_id = _uuid()
    values: dict[str, Any] = {
        "event_id": event_id,
        "operation_id": None,
        "patient_id": PATIENT,
        "event_type": m05.SOURCE_TYPE,
        "contract_version": 1,
        "event_level": "observation",
        "status": "completed",
        "correlation_id": _uuid(),
        "content_fingerprint": bytes(32),
        "producer_service": part_a.TEST_PRODUCER,
        "workload_id": m05.W1,
        "auth_method": "test",
        "source_time_quality": "absent",
        "metadata": Jsonb({"label_code": "reviewed"}),
    }
    values.update(overrides)
    columns = list(values)
    _admin(
        install,
        sql.SQL("INSERT INTO {}.patient_events ({}, received_at) VALUES ({}, now())").format(
            sql.Identifier(install.schema),
            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
        ),
        values,
    )
    return event_id


def _attribution(
    principal: str = m05.W1, *, kind: str = "workload", purpose: str = "operations", **extra: Any
) -> dict[str, Any]:
    attribution = part_a.attribution(principal, kind=kind)
    attribution["purpose_code"] = purpose
    attribution.update(extra)
    return attribution


def _command(
    source_event_id: str, item_kind: str = "reschedule_request", **attribution: Any
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_event_id": source_event_id,
        "item_kind": item_kind,
        "attribution": _attribution(**attribution),
    }


def _create(install: M05Install, command: Any) -> dict[str, Any]:
    with psycopg.connect(install.ids.logins[m05.RUNTIME], autocommit=True) as conn:
        conn.execute(f"SET ROLE {m05.RUNTIME}")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
            row = conn.execute(
                sql.SQL("SELECT {}.m05_create_item(%s)").format(sql.Identifier(install.schema)),
                (Jsonb(command),),
            ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def _snapshot(install: M05Install) -> dict[str, Any]:
    """Full contents of the M05 tables (sorted JSON rows) plus M02 counts."""

    out: dict[str, Any] = {}
    for table in M05_TABLES_CONTENT:
        out[table] = sorted(
            row[0]
            for row in _admin(
                install,
                sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} AS t").format(
                    sql.Identifier(install.schema), sql.Identifier(table)
                ),
            )
        )
    for table in M02_COUNTED:
        out[table] = _admin(
            install,
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(install.schema), sql.Identifier(table)
            ),
        )[0][0]
    return out


def _refused(install: M05Install, command: Any, code: str) -> psycopg.Error:
    before = _snapshot(install)
    with pytest.raises(psycopg.Error) as caught:
        _create(install, command)
    assert (caught.value.sqlstate, caught.value.diag.message_primary) == ("P0001", code)
    assert _snapshot(install) == before, "a refused create wrote something"
    return caught.value


def _item(install: M05Install, item_id: str) -> dict[str, Any]:
    rows = _admin(
        install,
        sql.SQL("SELECT to_jsonb(t) FROM {}.m05_followup_items AS t WHERE item_id = %s").format(
            sql.Identifier(install.schema)
        ),
        (item_id,),
    )
    assert len(rows) == 1, rows
    item: dict[str, Any] = rows[0][0]
    return item


def _history(install: M05Install, item_id: str) -> list[dict[str, Any]]:
    rows = _admin(
        install,
        sql.SQL(
            "SELECT to_jsonb(t) FROM {}.m05_followup_history AS t WHERE item_id = %s ORDER BY seq"
        ).format(sql.Identifier(install.schema)),
        (item_id,),
    )
    return [row[0] for row in rows]


def _digest_hex(install: M05Install, item_id: str) -> str:
    rows = _admin(
        install,
        sql.SQL("SELECT encode(create_digest, 'hex') FROM {}.m05_followup_items"
                " WHERE item_id = %s").format(sql.Identifier(install.schema)),
        (item_id,),
    )
    return str(rows[0][0])


def _assert_result(result: dict[str, Any], outcome: str) -> str:
    assert sorted(result) == sorted(m05.CREATE_RESULT_KEYS), result
    assert result["schema_version"] == 1
    assert result["created_or_existing"] == outcome
    item_id = str(result["item_id"])
    assert str(uuid.UUID(item_id)) == item_id
    return item_id


# --- MB2-V: digest oracle against independent literals -----------------------


def test_mb2_v_create_digest_vectors() -> None:
    for parts, preimage, digest in m05.CREATE_DIGEST_VECTORS:
        assert m05.create_digest_preimage(*parts) == preimage
        assert m05.create_digest(*parts).hex() == digest


# --- MC-01 / MC-02 / MC-03 ---------------------------------------------------


def test_mc01_mapped_source_creates_item_and_history(m05_install: M05Install) -> None:
    _ready(m05_install)
    source = _source(m05_install)
    item_id = _assert_result(_create(m05_install, _command(source)), "created")
    item = _item(m05_install, item_id)
    assert {k: item[k] for k in item if k not in ("create_digest", "created_at")} == {
        "item_id": item_id,
        "source_event_id": source,
        "item_kind": "reschedule_request",
        "patient_id": PATIENT,
        "state": "open",
        "assignee_id": None,
        "outcome_code": None,
        "version": 1,
    }
    assert item["created_at"] is not None
    expected = m05.create_digest(
        m05_install.tenant[0], source, "reschedule_request", m05.W1, "operations"
    )
    assert _digest_hex(m05_install, item_id) == expected.hex()
    history = _history(m05_install, item_id)
    assert len(history) == 1
    entry = history[0]
    assert str(uuid.UUID(entry["history_id"])) == entry["history_id"]
    assert {k: entry[k] for k in entry if k not in ("history_id", "created_at")} == {
        "item_id": item_id,
        "seq": 1,
        "action": "created",
        "actor_id": m05.W1,
        "before": None,
        "after": m05.CREATED_AFTER,
        "resolution_event_id": None,
        "assertion_kind": None,
    }


def test_mc02_exact_redelivery_is_existing_with_no_new_rows(m05_install: M05Install) -> None:
    _ready(m05_install)
    source = _source(m05_install)
    first = _assert_result(_create(m05_install, _command(source)), "created")
    before = _snapshot(m05_install)
    # Transport fields differ on an honest retry; they are outside the digest.
    retry = _command(source, execution_id=_uuid(), request_id="retry-1", auth_method="other")
    second = _assert_result(_create(m05_install, retry), "existing")
    assert second == first
    assert _snapshot(m05_install) == before


def test_mc03_same_source_and_kind_other_purpose_is_dedupe_conflict(
    m05_install: M05Install,
) -> None:
    _ready(m05_install)
    source = _source(m05_install)
    _assert_result(_create(m05_install, _command(source)), "created")
    _refused(m05_install, _command(source, purpose="treatment"), "DEDUPE_CONFLICT")


# --- MB2-C: concurrent duplicate ---------------------------------------------


def test_mb2_c_concurrent_duplicate_converges(m05_install: M05Install) -> None:
    _ready(m05_install)
    source = _source(m05_install)
    command = _command(source)
    call = sql.SQL("SELECT {}.m05_create_item(%s)").format(sql.Identifier(m05_install.schema))
    outcome: list[Any] = []
    holder = psycopg.connect(m05_install.ids.logins[m05.RUNTIME])
    waiter = psycopg.connect(m05_install.ids.logins[m05.RUNTIME])
    thread: threading.Thread | None = None
    try:
        for conn in (holder, waiter):
            conn.execute(f"SET ROLE {m05.RUNTIME}")
            conn.execute("SET statement_timeout = '20s'")
            conn.execute("SET lock_timeout = '15s'")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (m05_install.tenant[0],))
        first_row = holder.execute(call, (Jsonb(command),)).fetchone()  # open, uncommitted
        assert first_row is not None
        first = _assert_result(first_row[0], "created")
        waiter_pid = waiter.execute("SELECT pg_backend_pid()").fetchone()[0]  # type: ignore[index]

        def run() -> None:
            try:
                row = waiter.execute(call, (Jsonb(command),)).fetchone()
                waiter.commit()
                outcome.append(row[0] if row else None)
            except Exception as error:  # noqa: BLE001 - asserted below
                outcome.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        blocked = False
        for _ in range(100):
            rows = _admin(
                m05_install,
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                (waiter_pid,),
            )
            if rows == [("Lock",)]:
                blocked = True
                break
            threading.Event().wait(0.05)
        assert blocked, "the second create was not observed waiting on the first"
        assert outcome == [], "the second create finished before the first committed"
        holder.commit()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], dict), outcome
        assert _assert_result(outcome[0], "existing") == first
        items = _admin(
            m05_install,
            sql.SQL("SELECT item_id::text FROM {}.m05_followup_items WHERE source_event_id = %s")
            .format(sql.Identifier(m05_install.schema)),
            (source,),
        )
        assert items == [(first,)]
        assert len(_history(m05_install, first)) == 1
    finally:
        for conn in (holder, waiter):
            try:
                conn.rollback()
            finally:
                conn.close()
        if thread is not None:
            thread.join(timeout=5)


# --- MB2-A: history-stage fault rolls back the item --------------------------


def test_mb2_a_history_fault_rolls_back_the_item(m05_install: M05Install) -> None:
    _ready(m05_install)
    source = _source(m05_install)
    schema = sql.Identifier(m05_install.schema)
    _admin(
        m05_install,
        sql.SQL(
            "CREATE FUNCTION {}.mb2_fault() RETURNS trigger LANGUAGE plpgsql AS"
            " $f$BEGIN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'MB2_FAULT';"
            " END$f$"
        ).format(schema),
    )
    try:
        _admin(
            m05_install,
            sql.SQL(
                "CREATE TRIGGER mb2_fault BEFORE INSERT ON {}.m05_followup_history"
                " FOR EACH ROW EXECUTE FUNCTION {}.mb2_fault()"
            ).format(schema, schema),
        )
        before = _snapshot(m05_install)
        with pytest.raises(psycopg.Error) as caught:
            _create(m05_install, _command(source))
        assert (caught.value.sqlstate, caught.value.diag.message_primary) == (
            "P0001",
            "MB2_FAULT",
        )
        assert _snapshot(m05_install) == before
    finally:
        _admin(
            m05_install,
            sql.SQL("DROP TRIGGER IF EXISTS mb2_fault ON {}.m05_followup_history").format(schema),
        )
        _admin(m05_install, sql.SQL("DROP FUNCTION IF EXISTS {}.mb2_fault()").format(schema))
    # Control: the same command succeeds once the fault is removed.
    _assert_result(_create(m05_install, _command(source)), "created")


# --- MC-04 -------------------------------------------------------------------


def test_mc04_ineligible_callers_and_sources_share_one_shape(
    m05_install: M05Install, other_install: M05Install
) -> None:
    _ready(m05_install)
    _ready(other_install)
    mine = _source(m05_install)
    by_w3 = _source(m05_install, workload=m05.W3)
    foreign = _source(other_install)
    cases = {
        "actor": _command(mine, principal=m05.ACTOR, kind="actor"),
        "unmapped-workload": _command(mine, principal=m05.W2),
        "other-producer-source": _command(by_w3),
        "w3-own-source": _command(by_w3, principal=m05.W3),
        "unknown-source": _command(_uuid()),
        "foreign-tenant-source": _command(foreign),
    }
    shapes = {}
    for name, command in cases.items():
        error = _refused(m05_install, command, "SOURCE_NOT_ELIGIBLE")
        shapes[name] = (
            error.sqlstate,
            error.diag.message_primary,
            error.diag.message_detail,
            error.diag.message_hint,
        )
    assert len(set(shapes.values())) == 1, shapes


# --- MC-05 / MC-05b / level-status-operation ----------------------------------


def test_mc05_unmapped_ambiguous_absent_and_null_patient_refused(
    m05_install: M05Install,
) -> None:
    _ready(m05_install)
    stop_stand_in = _source(m05_install, metadata={"label_code": "unreviewed"})
    absent = _source(m05_install, metadata={})
    ambiguous = _source(m05_install, metadata={"label_code": "reviewed", "channel_code": "sms"})
    null_patient = _source(m05_install, patient=None)
    for source in (stop_stand_in, absent, ambiguous, null_patient):
        _refused(m05_install, _command(source), "SOURCE_NOT_ELIGIBLE")
    # The two-row source is refused for every kind it could match: no arbitrary pick.
    _refused(m05_install, _command(ambiguous, "cancel_request"), "SOURCE_NOT_ELIGIBLE")


def test_mc05_kind_mismatch_and_single_match_controls(m05_install: M05Install) -> None:
    _ready(m05_install)
    reviewed = _source(m05_install)
    _refused(m05_install, _command(reviewed, "no_reply"), "KIND_MISMATCH")
    # Single-match controls: each discriminator alone maps to exactly one row.
    voice = _source(m05_install, metadata={"label_code": "reviewed", "channel_code": "voice"})
    _assert_result(_create(m05_install, _command(voice, "reschedule_request")), "created")
    sms = _source(m05_install, metadata={"channel_code": "sms"})
    _assert_result(_create(m05_install, _command(sms, "cancel_request")), "created")


@pytest.fixture(scope="module")
def no_allowlist_install(
    request: pytest.FixtureRequest, m02_ids: m02.Identities, m05_reset: Any
) -> Iterator[M05Install]:
    tenant = m02.tenant_for(f"{request.module.__name__}:m05:no-allowlist")
    m05_reset(tenant)
    try:
        yield M05Install(m02_ids, tenant)
    finally:
        m05_reset(tenant)


def test_mc05b_type_without_m05_allowlist_is_not_eligible(
    no_allowlist_install: M05Install,
) -> None:
    """Independent tenant: the intake row exists, the m05 allowlist row does not, so
    only the envelope's refusal can make the source ineligible."""

    install = no_allowlist_install
    _ready(install, allowlist=False)
    source = _source(install)
    with psycopg.connect(install.ids.logins[m05.MIGRATOR], autocommit=True) as conn:
        conn.execute(f"SET ROLE {m05.MIGRATOR}")
        conn.execute(f"SET ROLE {m05.M05_OWNER}")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (install.tenant[0],))
            row = conn.execute(
                sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(
                    sql.Identifier(install.schema)
                ),
                (source,),
            ).fetchone()
    assert row == ({"error": "ENVELOPE_UNSUPPORTED"},)
    _refused(install, _command(source), "SOURCE_NOT_ELIGIBLE")


def test_mb2_l_level_status_and_operation_ineligible(m05_install: M05Install) -> None:
    _ready(m05_install)
    operation = m02.seeded_row(f"mb2-l-{uuid.uuid4().hex}")
    _admin(m05_install, m02.insert_statement(m05_install.schema), operation)
    for overrides in (
        {"event_level": "intent", "status": "requested"},
        {"status": "accepted"},
        {"operation_id": operation["operation_id"]},
    ):
        source = _direct_source(m05_install, **overrides)
        _refused(m05_install, _command(source), "SOURCE_NOT_ELIGIBLE")
    # Control: the same direct construction with eligible fields is accepted.
    _assert_result(_create(m05_install, _command(_direct_source(m05_install))), "created")


# --- MC-06 -------------------------------------------------------------------


def _malformed_commands(source: str) -> dict[str, Any]:
    base = _command(source)
    attribution = base["attribution"]

    def with_attr(**changes: Any) -> dict[str, Any]:
        return {**base, "attribution": {**attribution, **changes}}

    def without(key: str) -> dict[str, Any]:
        return {k: v for k, v in base.items() if k != key}

    def without_attr(key: str) -> dict[str, Any]:
        return {**base, "attribution": {k: v for k, v in attribution.items() if k != key}}

    cases: dict[str, Any] = {
        "not-an-object": [base],
        "extra-key": {**base, "note": "x"},
        "patient_id": {**base, "patient_id": PATIENT},
        "subject": {**base, "subject": PATIENT},
        "item_id": {**base, "item_id": _uuid()},
        "state": {**base, "state": "open"},
        "schema_version-bool": {**base, "schema_version": True},
        "schema_version-2": {**base, "schema_version": 2},
        "schema_version-string": {**base, "schema_version": "1"},
        "source-uppercase": {**base, "source_event_id": source.upper()},
        "source-not-uuid": {**base, "source_event_id": "not-a-uuid"},
        "source-null": {**base, "source_event_id": None},
        "kind-unknown": {**base, "item_kind": "stop_request"},
        "attribution-not-object": {**base, "attribution": "w1"},
        "attr-extra": with_attr(actor_id=m05.W1),
        "attr-kind-unknown": with_attr(principal_kind="robot"),
        "attr-principal-not-uuid": with_attr(principal_id="w1"),
        "attr-principal-uppercase": with_attr(principal_id=m05.W1.upper()),
        "attr-execution-not-uuid": with_attr(execution_id="exec"),
        "attr-auth-method-pattern": with_attr(auth_method="Bad Method"),
        "attr-purpose-unknown": with_attr(purpose_code="research"),
        "attr-request-number": with_attr(request_id=7),
        "attr-request-empty": with_attr(request_id=""),
    }
    for key in ("schema_version", "source_event_id", "item_kind", "attribution"):
        cases[f"missing-{key}"] = without(key)
    for key in ("principal_kind", "principal_id", "auth_method", "execution_id",
                "request_id", "purpose_code"):
        cases[f"attr-missing-{key}"] = without_attr(key)
    return cases


def test_mc06_closed_schema_refused_as_malformed(m05_install: M05Install) -> None:
    _ready(m05_install)
    source = _source(m05_install)
    for name, command in _malformed_commands(source).items():
        try:
            _refused(m05_install, command, "COMMAND_MALFORMED")
        except AssertionError as error:
            raise AssertionError(f"case {name}: {error}") from error
    # Control: the well-formed base command is accepted.
    _assert_result(_create(m05_install, _command(source)), "created")


# --- MC-07 -------------------------------------------------------------------


def test_mc07_second_distinct_source_creates_new_item_first_untouched(
    m05_install: M05Install,
) -> None:
    _ready(m05_install)
    first_source = _source(m05_install, patient=PATIENT_2)
    first = _assert_result(_create(m05_install, _command(first_source)), "created")
    # Resolve the first item by direct admin fixture (resolve itself is B4).
    _admin(
        m05_install,
        sql.SQL(
            "UPDATE {}.m05_followup_items SET state = 'resolved',"
            " outcome_code = 'reached_no_change', version = 2 WHERE item_id = %s"
        ).format(sql.Identifier(m05_install.schema)),
        (first,),
    )
    first_before = (_item(m05_install, first), _history(m05_install, first))
    second_source = _source(m05_install, patient=PATIENT_2)
    second = _assert_result(_create(m05_install, _command(second_source)), "created")
    assert second != first
    assert _item(m05_install, second)["patient_id"] == PATIENT_2
    assert (_item(m05_install, first), _history(m05_install, first)) == first_before


# --- MB2-I: real-row history immutability ------------------------------------


def test_mb2_i_created_history_row_is_immutable(m05_install: M05Install) -> None:
    _ready(m05_install)
    item_id = _assert_result(_create(m05_install, _command(_source(m05_install))), "created")
    table = sql.SQL("{}.m05_followup_history").format(sql.Identifier(m05_install.schema))
    statements = {
        "UPDATE": sql.SQL("UPDATE {} SET seq = seq WHERE item_id = %s").format(table),
        "DELETE": sql.SQL("DELETE FROM {} WHERE item_id = %s").format(table),
    }
    before = _history(m05_install, item_id)
    assert len(before) == 1
    identities = {
        m05.MIGRATOR: (m05.MIGRATOR, (m05.MIGRATOR,), "0A000"),
        m05.M05_OWNER: (m05.MIGRATOR, (m05.MIGRATOR, m05.M05_OWNER), "42501"),
        m05.RUNTIME: (m05.RUNTIME, (m05.RUNTIME,), "42501"),
    }
    for name, (login, roles, sqlstate) in identities.items():
        for verb, statement in statements.items():
            with psycopg.connect(m05_install.ids.logins[login], autocommit=True) as conn:
                for role in roles:
                    conn.execute(f"SET ROLE {role}")
                with pytest.raises(psycopg.Error) as caught, conn.transaction():
                    conn.execute(statement, (item_id,))
            assert caught.value.sqlstate == sqlstate, (name, verb, caught.value.sqlstate)
    assert _history(m05_install, item_id) == before


# --- MB2-G: exact grants ------------------------------------------------------
#
# The grant rows assert m05.CURRENT_* (B3a: m05.B3_*), not the B2 sets: each tranche
# widens the owner's exact grants, and these rows always pin the current reviewed set.


def test_mb2_g_m05_owner_explicit_acl_entries(m05_install: M05Install) -> None:
    m05_install.require()
    schema = m05_install.schema
    table_acl = _admin(
        m05_install,
        "SELECT c.relname, a.privilege_type, a.is_grantable FROM pg_catalog.pg_class c,"
        " LATERAL pg_catalog.aclexplode(c.relacl) a"
        " WHERE c.relnamespace = %s::regnamespace AND c.relname LIKE 'm05\\_%%'"
        " AND a.grantee = %s::regrole",
        (schema, m05.M05_OWNER),
    )
    assert sorted(table_acl) == sorted(
        (table, privilege, False)
        for table, privileges in m05.CURRENT_TABLE_ACL.items()
        for privilege in privileges
    )
    column_acl = _admin(
        m05_install,
        "SELECT c.relname, att.attname, a.privilege_type, a.is_grantable"
        " FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute att ON att.attrelid = c.oid,"
        " LATERAL pg_catalog.aclexplode(att.attacl) a"
        " WHERE c.relnamespace = %s::regnamespace AND c.relname LIKE 'm05\\_%%'"
        " AND a.grantee = %s::regrole",
        (schema, m05.M05_OWNER),
    )
    assert sorted(column_acl) == sorted(
        (table, column, privilege, False)
        for table, by_privilege in m05.CURRENT_COLUMN_ACL.items()
        for privilege, columns in by_privilege.items()
        for column in columns
    )


def test_mb2_g_m05_owner_effective_privileges(m05_install: M05Install) -> None:
    m05_install.require()
    schema = m05_install.schema
    for table in m05.TABLES:
        qualified = f"{schema}.{table}"
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                          "TRIGGER", "MAINTAIN"):
            expected = privilege in m05.CURRENT_TABLE_ACL.get(table, ())
            assert _admin(
                m05_install,
                "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
                (m05.M05_OWNER, qualified, privilege),
            ) == [(expected,)], (table, privilege)
        columns = [
            row[0]
            for row in _admin(
                m05_install,
                "SELECT attname FROM pg_catalog.pg_attribute WHERE attrelid = %s::regclass"
                " AND attnum > 0 AND NOT attisdropped",
                (qualified,),
            )
        ]
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                expected = privilege in m05.CURRENT_TABLE_ACL.get(table, ()) or column in (
                    m05.CURRENT_COLUMN_ACL.get(table, {}).get(privilege, ())
                )
                assert _admin(
                    m05_install,
                    "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s)",
                    (m05.M05_OWNER, qualified, column, privilege),
                ) == [(expected,)], (table, column, privilege)
