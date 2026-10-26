"""L-6 CP-3: W5 regression tests, v2 (tiers D and C), for implementation v5. PROPOSED; not frozen.

v2 changes from v1 (61b9c444…1d30, approved by owner record 8aff5824…aa30) only W5-3, for
the two points of Codex `codex_w5-owner-decision-recommendation.md`; both need Rachel's
decision and exact-byte re-approval:
- R-b: `test_w5_3_token_invalid_after_claim_stays_null` is replaced by
  `test_w5_3_token_invalid_after_claim_is_associated`, under the PROPOSED A3 clarification
  (a later RC-05 keeps this attempt's earlier authoritative match; NULL with no earlier
  match, which the frozen v7 TC-A03-e, TC-F03b outer and TC-A03-c continue to assert);
- claim-only coverage: `test_w5_3_fence_lost_after_claim_only_is_associated` is added. It
  uses the PROPOSED seam point `pause_after["CL"]` (after the claim commits, before any
  fence) of the existing `UpgradeTestHooks.pause_after` mapping.

Supplemental to the conditionally frozen packet-v7 module `test_l6_cp3_upgrade_postgres.py`,
whose bytes are unchanged. It reuses that module's harness (fixture `k0_tenant`, the R5
snapshot, the halting helpers) by import, so no harness is duplicated.

Traceability (Codex `codex_impl-v3-full-static-review.md`, W5 findings):
- W5-1 [P1]: architecture v6 r3 §8 ("any other combination is X") and §5 (layer predicates
  consistent with state): an operation's events must be legal for its operation state;
  ledger-step evidence only after `lo_granted` (§7 A2r).
- W5-2 [P1]: architecture v6 r3 §4 and §8 (exact actual-object and privilege shape; an
  unsupported shape is X): routines are inventoried by identity (name, input argument
  types, kind, owner), independently of their ACL rows.
- W5-3 [P2]: IP-14 r3 A3 (`attempt_refused` operation association: set only if this
  attempt has ESTABLISHED an authoritative match, by a claim or a passing fence; never a
  mismatched row) and its TC-F01/F06c mapping ("set only if an earlier step of the same
  attempt established it"), with the PROPOSED clarification (W5 R-b): the earlier match is
  kept whatever the later refusal code, RC-05 included; it is never taken from the refused
  capability, the request or a mismatched row; with no earlier match the association is
  NULL.

Harness mutations (all on the synthetic `k0_tenant` only, removed by its teardown purge):
- `_append_event`: one superuser INSERT of a schema-valid event row into the attempts
  table, bound to the tenant's operation (simulates inconsistent committed history);
- `_extra_routine`: one routine created in the tenant schema, owned by the migrator role.
Data are synthetic (R2). Run only when Rachel authorizes it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import sql

# Reused harness (fixtures and helpers) of the frozen v7 D/C module; imported, not copied.
# Fixtures: the autouse `_record_server_version` is imported by name (pytest registers an
# imported fixture under its module attribute name); `k0_tenant` is imported as
# `_v7_k0_tenant` and exposed to these tests as `w5_tenant` (one wrapper, below).
from test_l6_cp3_upgrade_postgres import (  # noqa: F401 - fixture, registered by name
    ATTEMPTS,
    Tenant,
    _admin,
    _assert_code,
    _assert_one_refusal,
    _assert_r5,
    _claim_tx,
    _classify,
    _close_halted_k,
    _compose,
    _current_attempt,
    _halt_after,
    _k,
    _operation,
    _p_session,
    _r5_snapshot,
    _record_server_version,
    _refused,
    _request,
    _to_state,
)
from test_l6_cp3_upgrade_postgres import k0_tenant as _v7_k0_tenant  # noqa: F401 - fixture

from haloflow.m01.provisioning import MIGRATOR_ROLE

pytestmark = pytest.mark.postgres


@pytest.fixture
def w5_tenant(_v7_k0_tenant: Tenant) -> Tenant:  # noqa: F811 - requests the imported fixture
    """The frozen v7 `k0_tenant` (provision, K0 check, teardown purge), unchanged."""
    return _v7_k0_tenant


def _append_event(admin: str, tenant_id: str, operation_id: UUID, event: str, detail: Any) -> None:
    with _admin(admin) as conn:
        conn.execute(
            f"INSERT INTO {ATTEMPTS} "
            "(attempt_id, maintenance_operation_id, tenant_id, event, detail) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (uuid4(), operation_id, tenant_id, event, json.dumps(detail)),
        )


def _extra_routine(admin: str, schema_key: str, definition: str, revoke_all: bool) -> None:
    """Create `<schema>.<definition>` owned by the migrator; revoke PUBLIC, and with
    `revoke_all` also the owner's own EXECUTE (the ACL then explodes to no rows)."""

    name = definition.split("(", 1)[0]
    args = definition[len(name) :]
    with _admin(admin) as conn:
        target = sql.SQL("{}.{}").format(sql.Identifier(schema_key), sql.Identifier(name))
        conn.execute(
            sql.SQL("CREATE FUNCTION {}{} RETURNS void LANGUAGE sql AS $$ SELECT $$").format(
                target, sql.SQL(args)
            )
        )
        conn.execute(
            sql.SQL("ALTER FUNCTION {}{} OWNER TO {}").format(
                target, sql.SQL(args), sql.Identifier(MIGRATOR_ROLE)
            )
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON FUNCTION {}{} FROM PUBLIC").format(target, sql.SQL(args))
        )
        if revoke_all:
            conn.execute(
                sql.SQL("REVOKE ALL ON FUNCTION {}{} FROM {}").format(
                    target, sql.SQL(args), sql.Identifier(MIGRATOR_ROLE)
                )
            )


# --- W5-1: event histories inconsistent with the operation state -----------

_PREMATURE = {
    "completed": {},
    "released": {},
    "abandoned": {},
    "activated": {"verified_event_id": "00000000-0000-4000-8000-000000000001"},
    "l1_restored": {},
    "l2_l2b_restored": {},
    "apply_running": {"ledger_attempt": 1},
}


@pytest.mark.parametrize("event", sorted(_PREMATURE))
async def test_w5_1_k1_premature_event_is_x_and_cl_refused(
    event: str,
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-1 (establishing): K1 plus one event that is illegal while `establishing`
    classifies X, and a direct CL is refused RC-08 with nothing written (component)."""

    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, w5_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.K1, "W5-1 setup K1"
    _append_event(admin, t.tenant_id, op, event, _PREMATURE[event])
    case = f"W5-1 K1+{event}"
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.X, f"{case}: X"
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        error = await _refused(
            _claim_tx(
                conn, form="CL", tenant_id=t.tenant_id, operation_id=op, new_attempt_id=uuid4(), k=k
            ),
            case,
        )
    _assert_code(error, "MAINTENANCE_CLAIM_REFUSED", "claim", case)
    _assert_r5(before, _r5_snapshot(admin, t), case, new_events=[])


@pytest.mark.parametrize("event", ["completed", "abandoned", "released"])
async def test_w5_1_k2_premature_event_is_x(
    event: str,
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-1 (establishing, L2 withheld): K2 plus a terminal or release event is X."""

    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, w5_tenant
    op = await _to_state(role_logins, admin, t, "K2")
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.K2, "W5-1 setup K2"
    _append_event(admin, t.tenant_id, op, event, {})
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.X, f"W5-1 K2+{event}: X"


@pytest.mark.parametrize("event", ["completed", "abandoned"])
async def test_w5_1_k3_terminal_event_is_x(
    event: str,
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-1 (entered): K3 plus `completed` or `abandoned` classifies X (Codex: the entered
    branch omitted both)."""

    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, w5_tenant
    op = await _to_state(role_logins, admin, t, "K3")
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.K3, "W5-1 setup K3"
    _append_event(admin, t.tenant_id, op, event, {})
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.X, f"W5-1 K3+{event}: X"


async def test_w5_1_k3_apply_evidence_before_lo_is_x(
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-1 (§7 A2r): ledger-step evidence (`apply_running`) without an earlier
    `lo_granted` is X, even in `entered`."""

    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, w5_tenant
    op = await _to_state(role_logins, admin, t, "K3")
    _append_event(admin, t.tenant_id, op, "apply_running", {"ledger_attempt": 1})
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.X, (
        "W5-1 K3+apply_running: X"
    )


async def test_w5_1_outer_k1_completed_refused_nothing_protected(
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-1 (outer): an attempt on K1 + `completed` refuses RC-06 at classify; only
    `started` and `attempt_refused` are added; no claim, no protected write (R5)."""

    from haloflow.m01.provisioning.upgrade import TenantSchemaUpgrade  # noqa: F401 - D-sym

    admin, t = migrated_database, w5_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    _append_event(admin, t.tenant_id, op, "completed", {})
    before = _r5_snapshot(admin, t)
    error = await _refused(_compose(role_logins).run(_request(t, op)), "W5-1 outer")
    _assert_code(error, "MAINTENANCE_CASE_REFUSED", "classify", "W5-1 outer")
    _assert_r5(
        before, _r5_snapshot(admin, t), "W5-1 outer", new_events=["started", "attempt_refused"]
    )


# --- W5-2: routine identity in the exact Φ comparison -----------------------


@pytest.mark.parametrize(
    ("definition", "revoke_all"),
    [
        ("operation_registry_reject(integer)", False),  # an overload, identical ACL tuple
        ("w5_zero_acl_routine()", True),  # ACL explodes to no rows
    ],
    ids=["overload", "zero_acl"],
)
async def test_w5_2_k0_extra_routine_is_x_and_no_c1(
    definition: str,
    revoke_all: bool,
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-2: an extra routine in the tenant schema (an overload of the rejector with the
    same name, owner and ACL tuple; or a routine whose ACL is empty) makes K0 X; the
    outer attempt refuses at classify and C1 does not run."""

    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, w5_tenant
    assert (await _classify(role_logins, t.tenant_id, None)).state == KState.K0, "W5-2 setup K0"
    _extra_routine(admin, t.schema_key, definition, revoke_all)
    case = f"W5-2 {definition}"
    assert (await _classify(role_logins, t.tenant_id, None)).state == KState.X, f"{case}: X"
    before = _r5_snapshot(admin, t)
    error = await _refused(_compose(role_logins).run(_request(t, None)), case)
    _assert_code(error, "MAINTENANCE_CASE_REFUSED", "classify", case)
    _assert_r5(before, _r5_snapshot(admin, t), case, new_events=["started", "attempt_refused"])
    assert _operation(admin, t.tenant_id) is None, f"{case}: no operation created"


# --- W5-3: refusal association after an established match -------------------


async def _stale_after(
    role_logins: dict[str, str], admin: str, t: Tenant, start: str, pause: str = "C2"
) -> tuple[Any, UUID, UUID, UUID]:
    """Attempt A starts at `start` (K0: C1, C2; K1: CL, C2), pauses after `pause` (C2; or,
    from K1, CL: after the claim committed and before any fence), loses K (identity-checked
    harness H5), and B claims (CL) with its own K. A's next fenced step (C3 after a C2
    pause; C2 after a CL pause) then fails statement 2 (RC-01)."""

    if start == "K1":
        op = await _to_state(role_logins, admin, t, "K1")
        a = await _halt_after(role_logins, admin, pause, _request(t, op))
    else:
        a = await _halt_after(role_logins, admin, "C2", _request(t, None))
        row = _operation(admin, t.tenant_id)
        assert row is not None
        op = UUID(str(row[0]))
    att_a = _current_attempt(admin, op)
    await _close_halted_k(admin, a, role_logins)
    att_b = uuid4()
    async with _k(role_logins, t.tenant_id) as k_b, _p_session(role_logins) as conn:
        await _claim_tx(
            conn, form="CL", tenant_id=t.tenant_id, operation_id=op, new_attempt_id=att_b, k=k_b
        )
    assert _current_attempt(admin, op) == att_b, "W5-3 setup: B's claim committed"
    return a, op, att_a, att_b


@pytest.mark.parametrize("start", ["K1", "K0"], ids=["claim_then_fence", "fence_only"])
async def test_w5_3_fence_lost_after_established_match_is_associated(
    start: str,
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-3: A established a match earlier in the same attempt (K1: a committed CL and a
    passing C2 fence; K0: a passing C2 fence), so its later RC-01 at C3 is associated with
    the operation. Nothing protected is written by the refusal (R5)."""

    from haloflow.m01.provisioning.upgrade import UpgradeRequest  # noqa: F401 - D-sym site

    admin, t = migrated_database, w5_tenant
    a, op, att_a, att_b = await _stale_after(role_logins, admin, t, start)
    case = f"W5-3 {start}"
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), case)
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", case)
    _assert_one_refusal(admin, t.tenant_id, att_a, "MAINTENANCE_FENCE_LOST", "fence", op, case)
    _assert_r5(before, _r5_snapshot(admin, t), case, new_events=["attempt_refused"])
    assert _current_attempt(admin, op) == att_b, f"{case}: B remains current"


async def test_w5_3_fence_lost_after_claim_only_is_associated(
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-3 claim-only provenance: from K1, A's CL commits (the only match A establishes)
    and A pauses before any fence (proposed seam `pause_after["CL"]`); A loses K and B
    claims. A's C2 fence then fails statement 2 (RC-01), so the refusal carries no fence
    match of its own: its association with the operation can come only from A's committed
    claim. Nothing protected is written by the refusal (R5); B stays current."""

    from haloflow.m01.provisioning.upgrade import UpgradeRequest  # noqa: F401 - D-sym site

    admin, t = migrated_database, w5_tenant
    a, op, att_a, att_b = await _stale_after(role_logins, admin, t, "K1", pause="CL")
    case = "W5-3 claim_only"
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), case)
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", case)
    _assert_one_refusal(admin, t.tenant_id, att_a, "MAINTENANCE_FENCE_LOST", "fence", op, case)
    _assert_r5(before, _r5_snapshot(admin, t), case, new_events=["attempt_refused"])
    assert _current_attempt(admin, op) == att_b, f"{case}: B remains current"


async def test_w5_3_token_invalid_after_claim_is_associated(
    w5_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """W5-3 under the PROPOSED A3 clarification (R-b): from K1, A's CL commits (an
    established match), then C2 runs with a tenant-faulted capability and the fence
    refuses RC-05 after statement 2. The refusal keeps A's earlier match, so it is
    associated with the operation; it names the trusted tenant, and nothing is appended
    under the capability's tenant. (No earlier match -> NULL: frozen v7 TC-A03-e.)"""

    from haloflow.m01.provisioning.upgrade import UpgradeTestHooks

    admin, t = migrated_database, w5_tenant
    op = await _to_state(role_logins, admin, t, "K1")

    def _foreign_rows() -> list[tuple[Any, ...]]:  # any tenant other than this one
        with _admin(admin) as conn:
            return conn.execute(
                f"SELECT event_id FROM {ATTEMPTS} WHERE tenant_id <> %s ORDER BY 1",
                (t.tenant_id,),
            ).fetchall()

    foreign_before = _foreign_rows()
    before = _r5_snapshot(admin, t)
    upgrade = _compose(role_logins, UpgradeTestHooks(capability_fault="tenant"))
    error = await _refused(upgrade.run(_request(t, op)), "W5-3 RC-05")
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "fence", "W5-3 RC-05")
    att = _current_attempt(admin, op)  # A's committed claim made it current
    _assert_one_refusal(
        admin, t.tenant_id, att, "MAINTENANCE_TOKEN_INVALID", "fence", op, "W5-3 RC-05"
    )
    added = _assert_r5(
        {k: v for k, v in before.items() if k != "operation"},
        {k: v for k, v in _r5_snapshot(admin, t).items() if k != "operation"},
        "W5-3 RC-05",
        new_events=["started", "claimed", "attempt_refused"],
    )
    assert added, "W5-3 RC-05: evidence appended"
    assert _foreign_rows() == foreign_before, (
        "W5-3 RC-05: nothing appended under the capability's tenant"
    )
