"""F-INT1: the RUNTIME installed-state verifier for t008-t011, PostgreSQL 17.

Codex ALIGN 20261031T142215Z, item 3. The static gate (`check_profile_agreement`,
test_m02_part_a_profiles PR4/PR6) refuses a profile that disagrees with its frozen
declaration before any DDL, so a mutated profile can never reach the runtime
verifier. These rows reach it through a bounded test seam instead: composition and
DDL are the real, valid ones; the seam wraps `installed_state.compare_installed_function`
(the module attribute the runner calls, I-B11) and, for ONE target function only,
alters the rows the real `INSTALLED_FUNCTION_QUERY` observed before the REAL
comparator judges them. Expected values (the profile) are never touched, so expected
and observed are never changed together into a passing oracle.

Each row proves: the real comparator ran for the target and raised
`VerificationMismatch`; every earlier unit verified and is `applied`; the run fails
`VERIFICATION_FAILED` with the target's ledger row `failed`; no later unit ran; and
the target function is absent (its DDL rolled back) while earlier functions remain.
The control row runs the same seam with no alteration and installs all eleven units.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import m02_gateway_support as gw
import m02_support as m02
import pytest

pytestmark = pytest.mark.postgres

VERIFICATION_FAILED = "VERIFICATION_FAILED"
ARGTYPES_MATCH, RETTYPE_MATCHES = 3, 7

ORDER = (
    "t001_m01_baseline", "t002_m02_operation_registry", "t003_m02_lock_operation",
    "t004_m02_reference", "t005_m02_events", "t006_m02_outbox",
    "t007_m02_envelope_allowlist", "t008_m02_canon", "t009_m02_append",
    "t010_m02_enqueue", "t011_m02_envelope_read",
    # UC1 part B B5 (H5): the production registry continues through t025.
    "t012_m05_tables", "t013_m05_seed", "t014_m05_create_item",
    "t015_m05_prepare_action", "t016_m05_apply_action", "t017_m05_get_item",
    "t018_m05_list_items", "t019_m05_get_history", "t020_m05_read_note",
    "t021_m01_staff_directory", "t022_m01_staff_assignable", "t023_m01_staff_admin",
    "t024_m01_staff_display", "t025_m01_staff_followup_reader",
    # L-1W W3a: t026-t032.
    "t026_m01_staff_roles", "t027_m01_staff_self_standing", "t028_m01_staff_member_standing",
    "t029_m01_staff_admin_v2", "t030_m05_prepare_action_v2", "t031_m05_apply_action_v2",
    "t032_m01_staff_bootstrap_admin",
    # L-1W W3b: t033-t036.
    "t033_m01_staff_bootstrap_first_admin_schema", "t034_m01_staff_bootstrap_first_admin",
    "t035_m05_v1_runtime_closure", "t036_m01_staff_v1_runtime_closure",
)
# B5 (H5): the typed M05 and staff functions the production registry verifies
# after t011, in install order (frozen, not read from the profiles).
B5_VERIFIED = (
    "m05_create_item", "m05_prepare_action", "m05_apply_action", "m05_get_item",
    "m05_list_items", "m05_get_history", "m05_read_note",
    "m01_staff_assignable", "m01_staff_admin", "m01_staff_display",
    "m01_staff_followup_reader",
    # L-1W W3a, in install order.
    "m01_staff_self_standing", "m01_staff_member_standing", "m01_staff_admin_v2",
    "m05_prepare_action_v2", "m05_apply_action_v2", "m01_staff_bootstrap_admin",
    # L-1W W3b: t034, then the closure units re-verify their targets against the
    # amended profiles (t035: prepare, apply; t036: the v1 admin gateway).
    "m01_staff_bootstrap_first_admin", "m05_prepare_action", "m05_apply_action",
    "m01_staff_admin",
)
# Frozen from the part-A manifest (amendment C1), not read from the profiles.
TYPED = {
    "t008_m02_canon": ("m02_canon_v1", "jsonb"),
    "t009_m02_append": ("m02_append_observation", "jsonb"),
    "t010_m02_enqueue": ("m02_enqueue_event_handoffs", "uuid, jsonb"),
    "t011_m02_envelope_read": ("m02_get_observation_envelope", "uuid"),
}


@contextmanager
def observed_seam(
    monkeypatch: Any, target: str | None, column: int | None
) -> Iterator[list[tuple[str, str]]]:
    """Wrap the real comparator. For `target` only, set `column` false on every
    observed row, then call the real comparator. Records (function, outcome)."""

    from haloflow.m01.provisioning import installed_state
    from haloflow.m01.provisioning.verification import VerificationMismatch

    record: list[tuple[str, str]] = []
    real: Callable[..., None] = installed_state.compare_installed_function

    def seam(profile: Any, **kwargs: Any) -> None:
        rows: Sequence[Sequence[Any]] = kwargs["rows"]
        if profile.function_name == target and column is not None:
            assert rows, "the real query observed no row for the target"
            kwargs["rows"] = [
                tuple(False if i == column else value for i, value in enumerate(row))
                for row in rows
            ]
        try:
            real(profile, **kwargs)
        except VerificationMismatch:
            record.append((profile.function_name, "mismatch"))
            raise
        record.append((profile.function_name, "verified"))

    monkeypatch.setattr(installed_state, "compare_installed_function", seam)
    yield record


def _absent(ids: Any, schema: str, name: str, arguments: str) -> bool:
    return bool(m02.admin_one(
        ids, "SELECT pg_catalog.to_regprocedure(%s) IS NULL",
        (f'"{schema}".{name}({arguments})',),
    )[0])


@pytest.mark.parametrize("column", [ARGTYPES_MATCH, RETTYPE_MATCHES], ids=["args", "return"])
@pytest.mark.parametrize("target", sorted(TYPED))
def test_fint1_m1_observed_mismatch_reaches_the_runtime_verifier_and_rolls_back(
    m02_ids: Any, m02_tenant: tuple[str, str], monkeypatch: Any, target: str, column: int
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed

    tenant_id, schema = m02_tenant
    name, arguments = TYPED[target]
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    with (
        observed_seam(monkeypatch, name, column) as record,
        pytest.raises(TenantMigrationFailed) as caught,
    ):
        gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)

    assert caught.value.reason_code == VERIFICATION_FAILED
    # The runtime verifier was reached: every earlier profiled unit verified, then
    # the real comparator refused the target.
    earlier_typed = [TYPED[u][0] for u in sorted(TYPED) if u < target]
    assert record == [
        ("m02_lock_operation", "verified"),
        *[(fn, "verified") for fn in earlier_typed],
        (name, "mismatch"),
    ]
    rows = m02.ledger(m02_ids, tenant_id)
    position = ORDER.index(target)
    for unit in ORDER[:position]:
        assert rows[unit][0] == "applied", unit
    assert rows[target] == ("failed", VERIFICATION_FAILED, True)
    for unit in ORDER[position + 1:]:
        assert unit not in rows, unit
    # Rollback: the target's function is absent; earlier ones are installed.
    assert _absent(m02_ids, schema, name, arguments)
    for unit in sorted(TYPED):
        if unit < target:
            assert not _absent(m02_ids, schema, *TYPED[unit]), unit


def test_fint1_m1_control_the_unaltered_seam_installs_every_unit(
    m02_ids: Any, m02_tenant: tuple[str, str], monkeypatch: Any
) -> None:
    tenant_id, schema = m02_tenant
    gw.prepare_bare_schema(m02_ids, m02_tenant)
    with observed_seam(monkeypatch, None, None) as record:
        outcomes = gw.run_migrations(m02_ids, m02.production_registry(), m02_tenant)
    assert [o.migration_id for o in outcomes if o.applied] == list(ORDER)
    assert record == [
        ("m02_lock_operation", "verified"),
        *[(TYPED[u][0], "verified") for u in sorted(TYPED)],
        *[(name, "verified") for name in B5_VERIFIED],
    ]
    for unit in sorted(TYPED):
        assert not _absent(m02_ids, schema, *TYPED[unit]), unit
