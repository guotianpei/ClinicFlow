"""UC1 part A tranche 2a — SQL-layer append, dedupe, links, binding, envelope (PostgreSQL 17).

Rows: PA-01 (SQL half), PA-07, PA-08, PA-09, PA-10a-i, PA-11, PA-12, PA-16, PA-17,
PA-19 (SQL key), PA-20, PA-22, PA-23, PA-24, PA-25, PA-26 (SQL fingerprint), PA-29,
PA-33..PA-38, PA-45, plus Codex-carried rows XB-1 (existing-branch binding) and
AL-H (historical evidence unchanged when a type is deactivated).

Calls are made as the runtime role (login shim RT) with `app.tenant_id` set by
`set_config(..., true)` inside the transaction, which is what the M01 gateway does.
The envelope is called as `haloflow_m05_owner` through the migrator's SET-only
membership. The non-production profile (G1) is test-owned SQL applied by ADMIN.

Baseline: every row first asserts the part-A objects are installed (`_ready`), so
at the pre-change baseline each row fails in its body (class B), never in a fixture.
Service-layer rows (PA-02..06, PA-13, PA-18, PA-30, PA-50, handle/context
mismatch) are tranche 2b.
"""

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.postgres


# --- helpers ----------------------------------------------------------------


def _uuid() -> str:
    return str(uuid.uuid4())


def _ready(part_a: ModuleType, m02_ids: Any, schema: str) -> None:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        for table in ("patient_events", "event_handoff_outbox", "m02_link_resolvers"):
            assert part_a.regclass_exists(conn, f"{schema}.{table}"), (
                f"{schema}.{table} is not installed"
            )
        if not part_a.regclass_exists(conn, f"{schema}.m02_test_link_targets"):
            conn.execute(part_a.TEST_PROFILE_SQL.format(schema=schema))


def _targets(m02_ids: Any, schema: str, *rows: tuple[Any, ...]) -> None:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        for row in rows:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.m02_test_link_targets VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING"
                ).format(sql.Identifier(schema)),
                row,
            )


def _fixture_ids(part_a: ModuleType, m02_ids: Any, schema: str) -> tuple[str, str]:
    subject, source = _uuid(), _uuid()
    _targets(m02_ids, schema, part_a.subject_target(subject), part_a.source_target(source))
    return subject, source


def _command(part_a: ModuleType, m02_ids: Any, schema: str, **overrides: Any) -> dict[str, Any]:
    subject, source = _fixture_ids(part_a, m02_ids, schema)
    defaults: dict[str, Any] = {
        "event_id": _uuid(),
        "dedupe_key": f"pa:{uuid.uuid4().hex}",
        "subject_id": subject,
        "source_id": source,
        "principal_id": _uuid(),
    }
    named = {k: overrides.pop(k) for k in list(overrides) if k in defaults}
    defaults.update(named)
    return part_a.observation_command(**defaults, **overrides)


def _append(
    m02_ids: Any, tenant: tuple[str, str], command: dict[str, Any], *, setting: str | None = None
) -> dict[str, Any]:
    tenant_id, schema = tenant
    with psycopg.connect(m02_ids.logins["haloflow_runtime"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_runtime")
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                (tenant_id if setting is None else setting,),
            )
            row = conn.execute(
                sql.SQL("SELECT {}.m02_append_observation(%s)").format(sql.Identifier(schema)),
                (Jsonb(command),),
            ).fetchone()
    assert row is not None
    result: dict[str, Any] = row[0]
    return result


def _refused(
    m02_ids: Any,
    tenant: tuple[str, str],
    command: dict[str, Any],
    code: str,
    *,
    sqlstate: str = "P0001",
    setting: str | None = None,
) -> None:
    with pytest.raises(psycopg.Error) as caught:
        _append(m02_ids, tenant, command, setting=setting)
    assert caught.value.sqlstate == sqlstate, caught.value.sqlstate
    if sqlstate == "P0001":
        assert caught.value.diag.message_primary == code


def _counts(part_a: ModuleType, m02_ids: Any, schema: str) -> tuple[int, ...]:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        out = []
        for table in part_a.COUNTED_TABLES:
            row = conn.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            ).fetchone()
            assert row is not None
            out.append(int(row[0]))
    return tuple(out)


def _admin_one(m02_ids: Any, query: sql.Composable, params: Any = None) -> tuple[Any, ...] | None:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
    return None if row is None else tuple(row)


@contextmanager
def _as_m05_owner(m02_ids: Any, tenant_id: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(m02_ids.logins["haloflow_migrator"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_migrator")
        conn.execute("SET ROLE haloflow_m05_owner")
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield conn


@pytest.fixture
def tenant(production_tenant: tuple[str, str]) -> tuple[str, str]:
    return production_tenant


# --- PA-01 / PA-19 / PA-26: happy path ---------------------------------------


def test_pa01_append_writes_event_links_and_one_signal_row(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    before = _counts(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    result = _append(m02_ids, tenant, command)
    assert result == {
        "schema_version": 1,
        "event_id": command["event_id"],
        "appended_or_existing": "appended",
        "handoff_state": "enqueued",
    }
    after = _counts(part_a, m02_ids, tenant[1])
    assert tuple(a - b for a, b in zip(after, before, strict=True)) == (1, 2, 1)
    expected_key = part_a.m11_key(
        tenant[0], command["event_id"], "event_signal", "m02.event.appended"
    )
    row = _admin_one(
        m02_ids,
        sql.SQL(
            "SELECT tenant_id, operation_id, handoff_kind, state, attempts, payload"
            " FROM {}.event_handoff_outbox WHERE source_event_id = %s"
        ).format(sql.Identifier(tenant[1])),
        (command["event_id"],),
    )
    assert row is not None
    assert row[:5] == (tenant[0], None, "event_signal", "pending", 0)
    assert row[5] == {
        "schema_version": 1,
        "tenant_id": tenant[0],
        "operation_id": None,
        "source_event_id": command["event_id"],
        "handoff_kind": "event_signal",
        "signal_type": "m02.event.appended",
    }
    key = _admin_one(
        m02_ids,
        sql.SQL("SELECT dedupe_key FROM {}.event_handoff_outbox WHERE source_event_id = %s").format(
            sql.Identifier(tenant[1])
        ),
        (command["event_id"],),
    )
    assert key == (expected_key,)  # PA-19 SQL half: kind-inclusive key


def test_pa26_sql_fingerprint_matches_oracle(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    attribution = command["attribution"]
    links = sorted(
        ([lk["link_type"], lk["link_id"], lk["relationship"]] for lk in command["links"]),
        key=lambda item: part_a.canon_bytes(item),
    )
    preimage = [
        "m02.event-content/v1",
        command["event_type"],
        str(command["contract_version"]),
        "observation",
        "completed",
        None,
        None,
        None,
        command["producer_service"],
        None,
        attribution["principal_id"],
        None,
        "absent",
        links,
        command["metadata"],
    ]
    row = _admin_one(
        m02_ids,
        sql.SQL(
            "SELECT encode(content_fingerprint, 'hex') FROM {}.patient_events WHERE event_id = %s"
        ).format(sql.Identifier(tenant[1])),
        (command["event_id"],),
    )
    assert row == (part_a.sha256_hex(part_a.canon_bytes(preimage)),)


# --- PA-07 / PA-08 / PA-22 / PA-24 / PA-25: dedupe ------------------------------


def test_pa07_equivalent_duplicate_returns_original(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    before = _counts(part_a, m02_ids, tenant[1])
    again = _append(m02_ids, tenant, command)
    assert again["appended_or_existing"] == "existing"
    assert again["event_id"] == command["event_id"]
    assert _counts(part_a, m02_ids, tenant[1]) == before


def test_pa22_retry_with_fresh_event_id_and_transport_fields_is_existing(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    before = _counts(part_a, m02_ids, tenant[1])
    retry = {**command, "event_id": _uuid(), "correlation_id": _uuid()}
    retry["attribution"] = {
        **command["attribution"],
        "execution_id": _uuid(),
        "request_id": "retry-1",
    }
    result = _append(m02_ids, tenant, retry)
    assert result["appended_or_existing"] == "existing"
    assert result["event_id"] == command["event_id"]
    assert _counts(part_a, m02_ids, tenant[1]) == before


@pytest.mark.parametrize("change", ["metadata", "subject_link", "principal"])
def test_pa08_pa24_same_key_changed_content_conflicts(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], change: str
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    before = _counts(part_a, m02_ids, tenant[1])
    changed = {**command, "event_id": _uuid()}
    if change == "metadata":
        changed["metadata"] = {"label_code": "unreviewed"}
    elif change == "subject_link":
        other = _uuid()
        _targets(m02_ids, tenant[1], part_a.subject_target(other))
        changed["links"] = [
            {"link_type": part_a.SUBJECT_RESOLVER, "link_id": other, "relationship": "subject"},
            command["links"][1],
        ]
    else:
        changed["attribution"] = {**command["attribution"], "principal_id": _uuid()}
    _refused(m02_ids, tenant, changed, "DEDUPE_CONFLICT")
    assert _counts(part_a, m02_ids, tenant[1]) == before


def test_pa25_unauthorized_producer_refused_before_dedupe(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1], producer_service="not_registered")
    _refused(m02_ids, tenant, command, "PRODUCER_NOT_AUTHORIZED")


# --- PA-23: concurrent identical appends ----------------------------------------


def test_pa23_concurrent_identical_appends_converge(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    before = _counts(part_a, m02_ids, tenant[1])
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(_append(m02_ids, tenant, command))
        except Exception as error:  # noqa: BLE001 - recorded and asserted below
            results.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(isinstance(r, dict) for r in results), results
    assert sorted(r["appended_or_existing"] for r in results) == ["appended", "existing"]
    assert {r["event_id"] for r in results} == {command["event_id"]}
    after = _counts(part_a, m02_ids, tenant[1])
    assert tuple(a - b for a, b in zip(after, before, strict=True)) == (1, 2, 1)


# --- PA-09 / PA-11: input validation --------------------------------------------


@pytest.mark.parametrize(
    ("label", "patch", "code"),
    [
        (
            "operation_id",
            {"operation_id": "00000000-0000-4000-8000-0000000000aa"},
            "COMMAND_MALFORMED",
        ),
        ("unknown_type", {"event_type": "m02_test_unknown"}, "CONTRACT_UNSUPPORTED"),
        ("wrong_level_type", {"event_type": "m02_test_requested"}, "CONTRACT_UNSUPPORTED"),
        ("extra_key", {"unexpected": 1}, "COMMAND_MALFORMED"),
        (
            "uppercase_uuid",
            {"event_id": "00000000-0000-4000-8000-0000000000AB"},
            "COMMAND_MALFORMED",
        ),
        ("metadata_extra", {"metadata": {"label_code": "reviewed", "x": 1}}, "COMMAND_MALFORMED"),
        ("metadata_enum", {"metadata": {"label_code": "other"}}, "COMMAND_MALFORMED"),
        ("missing_correlation", {"correlation_id": None}, "COMMAND_MALFORMED"),
        ("actor_field", {"actor_id": "00000000-0000-4000-8000-0000000000ac"}, "COMMAND_MALFORMED"),
    ],
)
def test_pa09_input_refusals(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    label: str,
    patch: dict[str, Any],
    code: str,
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    before = _counts(part_a, m02_ids, tenant[1])
    command = {**_command(part_a, m02_ids, tenant[1]), **patch}
    _refused(m02_ids, tenant, command, code)
    assert _counts(part_a, m02_ids, tenant[1]) == before, label


def test_pa09_inactive_type_refused(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    ident = sql.Identifier(tenant[1])
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("UPDATE {}.event_types SET is_active = false WHERE event_type = %s").format(
                ident
            ),
            (part_a.OBSERVATION_TYPE,),
        )
    try:
        _refused(m02_ids, tenant, command, "CONTRACT_UNSUPPORTED")
    finally:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("UPDATE {}.event_types SET is_active = true WHERE event_type = %s").format(
                    ident
                ),
                (part_a.OBSERVATION_TYPE,),
            )


@pytest.mark.parametrize(
    ("label", "patch"),
    [
        ("non_uuid_principal", {"principal_id": "user-17"}),
        ("bad_purpose", {"purpose_code": "marketing"}),
        ("bad_kind", {"principal_kind": "admin"}),
        ("long_request_id", {"request_id": "r" * 129}),
        ("extra_key", {"tenant_id": "x"}),
    ],
)
def test_pa11_attribution_refusals(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], label: str, patch: dict[str, Any]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    command["attribution"] = {**command["attribution"], **patch}
    _refused(m02_ids, tenant, command, "ATTRIBUTION_INVALID")


# --- PA-10a-i: links (G1) ---------------------------------------------------------


def _links(subject: str | None, source: str | None, **extra: str) -> list[dict[str, str]]:
    out = []
    if subject is not None:
        out.append(
            {
                "link_type": "m02_test_subject_v1",
                "link_id": subject,
                "relationship": extra.get("subject_rel", "subject"),
            }
        )
    if source is not None:
        out.append(
            {
                "link_type": "m02_test_source_v1",
                "link_id": source,
                "relationship": extra.get("source_rel", "source"),
            }
        )
    return out


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "subject_kind",
        "principal",
        "source_class",
        "relationship",
        "inactive",
        "group_unsatisfied",
    ],
)
def test_pa10_link_refusals(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], case: str
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    principal = command["attribution"]["principal_id"]
    good_source = command["links"][1]["link_id"]
    code = "LINK_INVALID"
    if case == "missing":
        command["links"] = _links(_uuid(), good_source)
    elif case == "subject_kind":
        target = _uuid()
        _targets(m02_ids, tenant[1], part_a.subject_target(target, kind="practice"))
        command["links"] = _links(target, good_source)
    elif case == "principal":
        target = _uuid()
        _targets(m02_ids, tenant[1], part_a.subject_target(target, principal=_uuid()))
        assert principal is not None
        command["links"] = _links(target, good_source)
    elif case == "source_class":
        target = _uuid()
        _targets(m02_ids, tenant[1], part_a.source_target(target, source_class="unapproved"))
        command["links"] = _links(None, target)
    elif case == "relationship":
        command["links"] = _links(command["links"][0]["link_id"], good_source, subject_rel="source")
        code = "LINK_UNSUPPORTED"
    elif case == "inactive":
        command["links"] = [
            *command["links"],
            {
                "link_type": part_a.INACTIVE_RESOLVERS[0],
                "link_id": _uuid(),
                "relationship": "provider",
            },
        ]
        code = "LINK_UNSUPPORTED"
    else:
        command["links"] = []
        code = "REQUIRED_LINK_MISSING"
    before = _counts(part_a, m02_ids, tenant[1])
    _refused(m02_ids, tenant, command, code)
    assert _counts(part_a, m02_ids, tenant[1]) == before


def test_pa10b_other_tenants_target_is_absent(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    m02_tenant: tuple[str, str],
    m02: ModuleType,
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    m02.provision_sync(m02_ids, m02.production_registry(), m02_tenant)
    _ready(part_a, m02_ids, m02_tenant[1])
    foreign_subject, _ = _fixture_ids(part_a, m02_ids, m02_tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    command["links"] = _links(foreign_subject, command["links"][1]["link_id"])
    _refused(m02_ids, tenant, command, "LINK_INVALID")


def test_pa10_principal_bound_target_accepts_matching_principal(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    principal = _uuid()
    target = _uuid()
    _targets(m02_ids, tenant[1], part_a.subject_target(target, principal=principal))
    command = _command(part_a, m02_ids, tenant[1], principal_id=principal)
    command["links"] = _links(target, command["links"][1]["link_id"])
    assert _append(m02_ids, tenant, command)["appended_or_existing"] == "appended"


# --- PA-29 / PA-33..PA-37: tenant setting and registry binding (TB) --------------


@pytest.mark.parametrize("setting", ["", "Bad Tenant", "x"])
def test_pa29_malformed_tenant_setting(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], setting: str
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _refused(m02_ids, tenant, command, "", sqlstate="22023", setting=setting)


def test_pa34_valid_looking_foreign_tenant_setting_is_mismatch(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    m02_tenant: tuple[str, str],
    m02: ModuleType,
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    m02.provision_sync(m02_ids, m02.production_registry(), m02_tenant)
    before = _counts(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _refused(m02_ids, tenant, command, part_a.MSG_BINDING_MISMATCH, setting=m02_tenant[0])
    assert _counts(part_a, m02_ids, tenant[1]) == before  # whole append rolled back


@pytest.mark.parametrize(
    "state", ["provisioning", "suspended", "archival_pending", "archived", "decommissioned"]
)
def test_pa36_inactive_registry_row(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], state: str
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    before = _counts(part_a, m02_ids, tenant[1])
    with _registry_state(m02_ids, tenant[0], state):
        _refused(m02_ids, tenant, command, part_a.MSG_BINDING_INACTIVE)
    assert _counts(part_a, m02_ids, tenant[1]) == before


@contextmanager
def _registry_state(m02_ids: Any, tenant_id: str, state: str) -> Iterator[None]:
    """ADMIN sets lifecycle_state directly (bypassing M01 transition triggers in a
    session with session_replication_role = replica) and restores it."""

    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
        assert row is not None
        original = row[0]
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "UPDATE shared.tenants SET lifecycle_state = %s WHERE tenant_id = %s",
            (state, tenant_id),
        )
    try:
        yield
    finally:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute("SET session_replication_role = replica")
            conn.execute(
                "UPDATE shared.tenants SET lifecycle_state = %s WHERE tenant_id = %s",
                (original, tenant_id),
            )


def test_xb1_existing_branch_retry_is_binding_checked(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    """Codex 95ae5bfd: an equivalent retry (existing branch) under a suspended tenant
    or a mismatched setting is refused, not answered from the stored row."""

    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    with _registry_state(m02_ids, tenant[0], "suspended"):
        _refused(m02_ids, tenant, command, part_a.MSG_BINDING_INACTIVE)


def test_pa38_direct_enqueue_and_canon_denied_to_runtime(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    ident = sql.Identifier(tenant[1])
    with psycopg.connect(m02_ids.logins["haloflow_runtime"], autocommit=True) as conn:
        conn.execute("SET ROLE haloflow_runtime")
        for statement, params in (
            (
                sql.SQL("SELECT {}.m02_enqueue_event_handoffs(%s, %s)").format(ident),
                (_uuid(), Jsonb([])),
            ),
            (sql.SQL("SELECT {}.m02_canon_v1(%s)").format(ident), (Jsonb({}),)),
            (sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(ident), (_uuid(),)),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement, params)


# --- PA-20: outbox logical-collision guard ------------------------------------------


def test_pa20_incompatible_logical_collision_is_integrity_error(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    key = part_a.m11_key(tenant[0], command["event_id"], "event_signal", "m02.event.appended")
    # Seed an incompatible row under the key this append will derive: it names a
    # different (pre-existing) source event.
    seed = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, seed)
    ident = sql.Identifier(tenant[1])
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "UPDATE {}.event_handoff_outbox SET dedupe_key = %s WHERE source_event_id = %s"
            ).format(ident),
            (key, seed["event_id"]),
        )
    before = _counts(part_a, m02_ids, tenant[1])
    with pytest.raises(psycopg.Error):
        _append(m02_ids, tenant, command)
    assert _counts(part_a, m02_ids, tenant[1]) == before


# --- PA-16 / PA-17 / PA-45 / AL-H: envelope -------------------------------------------


def test_pa16_envelope_closed_keys_no_metadata_leak(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    ident = sql.Identifier(tenant[1])
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.m02_envelope_path_allowlist VALUES ('m05', %s, 1, 'label_code')"
                " ON CONFLICT DO NOTHING"
            ).format(ident),
            (part_a.OBSERVATION_TYPE,),
        )
    with _as_m05_owner(m02_ids, tenant[0]) as conn:
        row = conn.execute(
            sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(ident),
            (command["event_id"],),
        ).fetchone()
    assert row is not None
    envelope = row[0]
    assert set(envelope) == {
        "schema_version",
        "event_id",
        "event_type",
        "contract_version",
        "event_level",
        "status",
        "operation_id",
        "patient_id",
        "actor_id",
        "workload_id",
        "causation_event_id",
        "producer_dedupe_key",
        "links",
        "metadata_paths",
    }
    assert envelope["event_level"] == "observation"
    assert envelope["status"] == "completed"
    assert envelope["operation_id"] is None
    assert envelope["metadata_paths"] == {"label_code": "reviewed"}


def test_pa17_envelope_unknown_id_is_null(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    with _as_m05_owner(m02_ids, tenant[0]) as conn:
        row = conn.execute(
            sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(sql.Identifier(tenant[1])),
            (_uuid(),),
        ).fetchone()
    assert row == (None,)


def test_pa17_pa45_alh_inactive_type_unsupported_history_untouched(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    """AL: a type not active in the loaded generation is ENVELOPE_UNSUPPORTED, and the
    stored event and its fingerprint are unchanged (no rewrite or deletion)."""

    _ready(part_a, m02_ids, tenant[1])
    command = _command(part_a, m02_ids, tenant[1])
    _append(m02_ids, tenant, command)
    ident = sql.Identifier(tenant[1])
    stored = sql.SQL(
        "SELECT event_id, content_fingerprint, metadata FROM {}.patient_events WHERE event_id = %s"
    ).format(ident)
    before = _admin_one(m02_ids, stored, (command["event_id"],))
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("UPDATE {}.event_types SET is_active = false WHERE event_type = %s").format(
                ident
            ),
            (part_a.OBSERVATION_TYPE,),
        )
    try:
        with _as_m05_owner(m02_ids, tenant[0]) as conn:
            row = conn.execute(
                sql.SQL("SELECT {}.m02_get_observation_envelope(%s)").format(ident),
                (command["event_id"],),
            ).fetchone()
        assert row == ({"error": "ENVELOPE_UNSUPPORTED"},)
        assert _admin_one(m02_ids, stored, (command["event_id"],)) == before
    finally:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("UPDATE {}.event_types SET is_active = true WHERE event_type = %s").format(
                    ident
                ),
                (part_a.OBSERVATION_TYPE,),
            )


# --- PA-12: tenant separation --------------------------------------------------------


def test_pa12_same_event_id_in_two_tenants_is_independent(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    m02_tenant: tuple[str, str],
    m02: ModuleType,
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    m02.provision_sync(m02_ids, m02.production_registry(), m02_tenant)
    _ready(part_a, m02_ids, m02_tenant[1])
    shared_id = _uuid()
    first = _command(part_a, m02_ids, tenant[1], event_id=shared_id)
    second = _command(part_a, m02_ids, m02_tenant[1], event_id=shared_id)
    assert _append(m02_ids, tenant, first)["appended_or_existing"] == "appended"
    assert _append(m02_ids, m02_tenant, second)["appended_or_existing"] == "appended"
