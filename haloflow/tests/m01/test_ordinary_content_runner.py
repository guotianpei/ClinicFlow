"""CG-4 -- I layer: the role-bearing ordinary content check through the REAL runner.

Traceability: requirements v5 (CG4-R0 to R9, R1b, R8a), architecture v2 section 5-6,
owner rulings CL-1 / CL-2 / AQ-3 option E, test cases v1 as amended by v2
(I-01, I-03 to I-19; I-02 is the edited `test_declared_role_switch_sequence` in
`test_migration_plan_binding.py`).

HOW THE RUNNER IS REACHED
-------------------------
`TenantMigrationRunner(connect=..., manifest=...)` over `TypedConnection`, via the
existing `typed_driver` fixture -- production constructor parameters only.

THE BASELINE-COMPATIBLE SPY (test cases v2 section 1.1)
-------------------------------------------------------
`ordinary_content_spy` attaches NOTHING while `ordinary_content` is absent: no fake
module, no `sys.modules` entry, the runner untouched -- so the real, unchanged
runner executes. Once the module exists it wraps `check_rendered` ON THAT MODULE
with a forwarding wrapper that calls the REAL function and records what it did.

CODES are compared as approved literal strings (section 1.2), so a baseline run
fails on the wrong OUTCOME, not on an import.

PRE-CHANGE CLASSIFICATION (hypotheses, confirmed by the pre-change run):
  behavioural red : I-01, I-03, I-04, I-05, I-06, I-07 (timing), I-15a, I-19
  green/baseline  : I-07 (count), I-09, I-10 to I-14, I-15b, I-16 (all, measured)
  interface       : I-08, I-17, I-18

WHAT THIS CANNOT ESTABLISH: anything about PostgreSQL. The recording harness
records calls; catalogue, privilege and rollback facts are D-layer.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.metadata
import importlib.util
from collections.abc import Callable
from typing import Any

import pglast
import pytest

from haloflow.m01.errors import MigrationUnitRejected, TenantMigrationFailed
from haloflow.m01.provisioning import runner as runner_module
from haloflow.m01.provisioning.codes import PreconditionCode, SanitizedErrorCode
from haloflow.m01.provisioning.manifest import (
    ExecutionRoleProfile,
    ProvisioningManifest,
    load_provisioning_manifest,
)
from haloflow.m01.provisioning.units import (
    TenantMigrationRegistry,
    TenantMigrationUnit,
    UnitDefinition,
    build_tenant_migration_registry,
)

TENANT = "clinic-a"
SCHEMA = "tenant_aaaaaaaa"
BAD_SCHEMA = "Tenant Not Valid"
OWNER = "haloflow_m02_owner"
ROLES = frozenset({OWNER, "haloflow_m02_annex"})
MODULE = "haloflow.m01.provisioning.ordinary_content"

# Approved literal codes (test cases v2 section 1.2).
PROHIBITED = "ORDINARY_ROLE_CONTENT_PROHIBITED"
UNPARSEABLE = "ORDINARY_ROLE_CONTENT_UNPARSEABLE"
SCHEMA_KEY_INVALID = PreconditionCode.SCHEMA_KEY_INVALID.value
VERSION_MISMATCH = PreconditionCode.INSTALL_PARSER_VERSION_MISMATCH.value

ORDINARY_SQL = "CREATE TABLE {schema}.cg4_probe (id int);"
SECOND_SQL = "CREATE TABLE {schema}.cg4_second (id int);"
FUNCTION_SQL = (
    "CREATE FUNCTION {schema}.cp2_probe_fn() RETURNS int "
    "LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$;"
)
DO_SQL = "DO $$ BEGIN NULL; END $$; -- {schema}"
NUL_SQL = "CREATE TABLE {schema}.cg4_probe (id int);\x00 DROP TABLE {schema}.cg4_other;"
COMMENT_ONLY_SQL = "-- {schema}: a comment and nothing else\n"


# ---------------------------------------------------------------------------
# Construction.
# ---------------------------------------------------------------------------


def unit_def(template: str, role: str | None = OWNER) -> UnitDefinition:
    return UnitDefinition(template, execution_role=role)


def build(definitions: dict[str, Any]) -> TenantMigrationRegistry:
    return build_tenant_migration_registry(
        definitions, approved_execution_roles=ROLES, allow_test_units=True
    )


def manifest_declaring(*roles: str) -> ProvisioningManifest:
    profile = ExecutionRoleProfile(
        login=False,
        superuser=False,
        createdb=False,
        createrole=False,
        replication=False,
        bypassrls=False,
        tenant_schema_privileges=("USAGE",),
    )
    return dataclasses.replace(
        load_provisioning_manifest(),
        execution_role_profiles={role: profile for role in roles},
    )


def role_answers(harness: Any) -> tuple[Any, ...]:
    """Stage 1's per-role reads, answered SAFE. Assumptions supplied, not observed."""

    return (
        harness.Answer(markers=("select rolcanlogin", "pg_roles"), rows=((False,) * 6,)),
        harness.Answer(markers=("pg_has_role",), rows=((True,),)),
    )


def drive(
    typed_driver: Any, harness: Any, registry: TenantMigrationRegistry, ledger: Any
) -> tuple[Any, Any]:
    runner, (connection,) = typed_driver(
        registry,
        (*role_answers(harness), ledger),
        manifest=manifest_declaring(OWNER),
        names=("work",),
    )
    return runner, connection


async def refusal(runner: Any, schema_key: str = SCHEMA) -> str:
    with pytest.raises(TenantMigrationFailed) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=schema_key)
    return caught.value.reason_code


def rendered_text(template: str, schema_key: str = SCHEMA) -> str:
    """The rendered text, computed WITHOUT calling `unit.render` (which may be spied)."""

    return template.replace("{schema}", schema_key)


def ledger_writes(connection: Any) -> tuple[Any, ...]:
    return tuple(
        entry
        for entry in connection.statements
        if "shared.schema_migrations" in entry.fingerprint
        and not entry.fingerprint.startswith("select")
    )


_INSTALL_PREFIXES = ("create", "do", "insert", "grant", "revoke", "drop", "set local role")


def assert_no_side_effects(connection: Any, *templates: str) -> None:
    """CG4-R4 (i)-(iii) at the I layer."""

    assert ledger_writes(connection) == ()
    for template in templates:
        assert rendered_text(template) not in connection.texts
    assert not [
        text for text in connection.texts if text.lstrip().casefold().startswith(_INSTALL_PREFIXES)
    ]
    assert "txn-begin" not in connection.kinds


# ---------------------------------------------------------------------------
# Spies (test cases v2 section 1.1).
# ---------------------------------------------------------------------------

_FORWARD = object()


@dataclasses.dataclass
class ContentSpy:
    present: bool = False
    calls: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    returned: list[object] = dataclasses.field(default_factory=list)
    raised: list[str] = dataclasses.field(default_factory=list)


@pytest.fixture
def ordinary_content_spy(monkeypatch: pytest.MonkeyPatch) -> Callable[..., ContentSpy]:
    def attach(*, returning: object = _FORWARD) -> ContentSpy:
        spy = ContentSpy()
        if importlib.util.find_spec(MODULE) is None:
            return spy  # baseline: attach nothing, runner untouched
        module = importlib.import_module(MODULE)
        real = module.check_rendered

        def check_rendered(*, unit: TenantMigrationUnit, rendered: str) -> object:
            spy.calls.append((unit.migration_id, rendered))
            if returning is not _FORWARD:
                spy.returned.append(returning)
                return returning
            try:
                result = real(unit=unit, rendered=rendered)
            except MigrationUnitRejected as error:
                spy.raised.append(error.reason_code)
                raise
            spy.returned.append(result)
            return result

        monkeypatch.setattr(module, "check_rendered", check_rendered)
        spy.present = True
        return spy

    return attach


@dataclasses.dataclass
class RenderSpy:
    calls: list[tuple[str, int, str]] = dataclasses.field(default_factory=list)

    def count(self, migration_id: str) -> int:
        return sum(1 for call in self.calls if call[0] == migration_id)


def spy_on_render(
    monkeypatch: pytest.MonkeyPatch,
    connection: Any,
    *,
    second: Callable[[str], str] | None = None,
) -> RenderSpy:
    """Class-level forwarding spy on `TenantMigrationUnit.render`.

    Records (migration id, trace length at the call, text returned). With `second`,
    any SECOND render of the same unit returns `second(text)` -- the controlled
    re-render mutant of architecture v2 section 6(c).
    """

    spy = RenderSpy()
    real = TenantMigrationUnit.render

    def render(self: TenantMigrationUnit, schema_key: str) -> str:
        text = real(self, schema_key)
        if second is not None and spy.count(self.migration_id) >= 1:
            text = second(text)
        spy.calls.append((self.migration_id, len(connection.trace), text))
        return text

    monkeypatch.setattr(TenantMigrationUnit, "render", render)
    return spy


class ParseSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self._real = pglast.parse_sql
        monkeypatch.setattr(pglast, "parse_sql", self)

    def __call__(self, text: str) -> Any:
        self.calls.append(text)
        return self._real(text)


# ===========================================================================
# Refusals: CG4-R1 / R1b / R4 / R6 (behavioural red at baseline).
# ===========================================================================


async def test_i01_rf5_a_role_bearing_function_template_is_refused(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any
) -> None:
    """I-01 = RF-5. The I-1 original (`FUNCTION_SQL` under a module role) is refused."""

    spy = ordinary_content_spy()
    runner, connection = drive(
        typed_driver, harness, build({"t001_test_cg4_fn": unit_def(FUNCTION_SQL)}),
        harness.ledger_absent(),
    )

    assert await refusal(runner) == PROHIBITED
    assert_no_side_effects(connection, FUNCTION_SQL)
    assert spy.present and spy.raised == [PROHIBITED]


async def test_i03_whole_plan_an_earlier_migrator_unit_is_not_installed(
    typed_driver: Any, harness: Any
) -> None:
    """I-03. CG4-R4 (iii): the refusal is whole-plan, before ANY unit installs."""

    registry = build(
        {
            "t001_test_cg4_migrator": unit_def(ORDINARY_SQL, role=None),
            "t002_test_cg4_fn": unit_def(FUNCTION_SQL),
        }
    )
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())

    assert await refusal(runner) == PROHIBITED
    assert_no_side_effects(connection, ORDINARY_SQL, FUNCTION_SQL)


@pytest.mark.parametrize(
    ("template", "code"),
    [(DO_SQL, PROHIBITED), (NUL_SQL, UNPARSEABLE), (COMMENT_ONLY_SQL, PROHIBITED)],
    ids=["i04-do-block", "i05-nul", "i06-zero-statements"],
)
async def test_i04_to_i06_refusals_leave_no_side_effects(
    typed_driver: Any, harness: Any, template: str, code: str
) -> None:
    """I-04 (CG4-R1), I-05 (CG4-R6), I-06 (CG4-R1b)."""

    runner, connection = drive(
        typed_driver, harness, build({"t001_test_cg4_x": unit_def(template)}),
        harness.ledger_absent(),
    )

    assert await refusal(runner) == code
    assert_no_side_effects(connection, template)


# ===========================================================================
# Render once, checked text executes: architecture v2 section 6(a)-(c).
# ===========================================================================


async def test_i07_each_in_scope_unit_renders_once_and_before_any_ledger_write(
    typed_driver: Any, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-07. Count (baseline-green) AND timing (behavioural red at baseline).

    Today unit 2 renders in pass 3, after unit 1's `running`/`applied` writes.
    Under the approved design both render in pass 2, before any ledger write.
    """

    registry = build(
        {"t001_test_cg4_a": unit_def(ORDINARY_SQL), "t003_test_cg4_b": unit_def(SECOND_SQL)}
    )
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())
    spy = spy_on_render(monkeypatch, connection)

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [True, True]
    assert spy.count("t001_test_cg4_a") == 1
    assert spy.count("t003_test_cg4_b") == 1
    first_write = next(
        index
        for index, entry in enumerate(connection.trace)
        if entry.kind == "statement"
        and "shared.schema_migrations" in entry.fingerprint
        and not entry.fingerprint.startswith("select")
    )
    assert all(position <= first_write for _, position, _ in spy.calls)


async def test_i08_the_executed_text_is_the_value_the_check_returned(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any
) -> None:
    """I-08. Regression oracle; needs the spy, so interface-dependent at baseline."""

    spy = ordinary_content_spy()
    runner, connection = drive(
        typed_driver, harness, build({"t001_test_cg4_a": unit_def(ORDINARY_SQL)}),
        harness.ledger_absent(),
    )

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert spy.present
    (returned,) = spy.returned
    assert isinstance(returned, str)
    assert connection.texts.count(returned) == 1


async def test_i09_a_second_render_would_not_be_what_executes(
    typed_driver: Any, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-09. Controlled re-render mutant: a second render yields DIFFERENT text.

    Regression oracle. Green at baseline (one render today); fails any
    implementation that renders again after the check.
    """

    runner, connection = drive(
        typed_driver, harness, build({"t001_test_cg4_a": unit_def(ORDINARY_SQL)}),
        harness.ledger_absent(),
    )
    spy_on_render(
        monkeypatch, connection, second=lambda text: text.replace("cg4_probe", "cg4_rerender")
    )

    await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert connection.texts.count(rendered_text(ORDINARY_SQL)) == 1
    assert not [text for text in connection.texts if "cg4_rerender" in text]


# ===========================================================================
# Excluded paths and refusal order: CG4-R8 / architecture v2 section 5.3.
# ===========================================================================


async def test_i10_a_migrator_owned_unit_is_never_checked_or_parsed(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-10. CG4-R0 / R8: migrator-owned ordinary units stay unparsed (OD-04)."""

    spy = ordinary_content_spy()
    parse = ParseSpy(monkeypatch)
    registry = build({"t001_test_cg4_m": unit_def(ORDINARY_SQL, role=None)})
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())
    renders = spy_on_render(monkeypatch, connection)

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [True]
    assert spy.calls == []
    assert parse.calls == []
    assert renders.count("t001_test_cg4_m") == 1


async def test_i11_a_typed_unit_is_never_content_checked(
    seam: Any, typed_driver: Any, harness: Any, ordinary_content_spy: Any,
    typed_payloads: dict[str, Any],
) -> None:
    """I-11. The typed route is unchanged and never reaches `check_rendered`."""

    spy = ordinary_content_spy()
    payload = typed_payloads["first"]
    registry = build({payload["migration_id"]: seam.typed_definition(payload)})
    runner, _ = drive(typed_driver, harness, registry, harness.ledger_absent())

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [True]
    assert spy.calls == []


async def test_i12_a_skipped_applied_unit_is_not_checked_retroactively(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any
) -> None:
    """I-12. Architecture v2 section 7: no retroactive attestation."""

    spy = ordinary_content_spy()
    registry = build({"t001_test_cg4_fn": unit_def(FUNCTION_SQL)})
    runner, connection = drive(
        typed_driver, harness, registry,
        harness.ledger_row("applied", registry.units[0].checksum),
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [False]
    assert spy.calls == []
    assert_no_side_effects(connection, FUNCTION_SQL)


async def test_i13_drift_is_reported_before_any_content_check(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any
) -> None:
    """I-13. Pass 1 drift precedes pass 2."""

    spy = ordinary_content_spy()
    runner, _ = drive(
        typed_driver, harness, build({"t001_test_cg4_fn": unit_def(FUNCTION_SQL)}),
        harness.ledger_row("applied", "0" * 64),
    )

    assert await refusal(runner) == SanitizedErrorCode.MIGRATION_CHECKSUM_DRIFT.value
    assert spy.calls == []


async def test_i14_stage_one_refuses_before_any_content_check(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any
) -> None:
    """I-14. The shipped manifest does not describe the role: stage 1 refuses first."""

    spy = ordinary_content_spy()
    runner, (connection,) = typed_driver(
        build({"t001_test_cg4_fn": unit_def(FUNCTION_SQL)}),
        (harness.ledger_absent(),),
        names=("work",),
    )

    assert await refusal(runner) == PreconditionCode.EXECUTION_ROLE_UNAVAILABLE.value
    assert spy.calls == []
    assert_no_side_effects(connection, FUNCTION_SQL)


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("a-role-bearing-first", PROHIBITED),
        ("b-typed-first", PreconditionCode.INSTALL_BODY_MISMATCH.value),
    ],
)
async def test_i15_registry_order_decides_which_pass_two_refusal_is_reported(
    seam: Any, typed_driver: Any, harness: Any, typed_payloads: dict[str, Any],
    order: str, expected: str,
) -> None:
    """I-15. (a) behavioural red at baseline; (b) green/baseline."""

    drift = typed_payloads["body_drift"]  # migration id t002_annex_probe
    role_id = "t001_test_cg4_fn" if order.startswith("a") else "t003_test_cg4_fn"
    registry = build(
        {role_id: unit_def(FUNCTION_SQL), drift["migration_id"]: seam.typed_definition(drift)}
    )
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())

    assert await refusal(runner) == expected
    assert_no_side_effects(connection, FUNCTION_SQL)


# ===========================================================================
# I-16  AQ-3 option E: an invalid schema key keeps today's type, code and order.
#
# Every row is GREEN/BASELINE and its expectation is the one APPROVED in test
# cases v1/v2. The pre-change run MEASURES each at 6bb10452; any mismatch is a
# finding for Rachel, never a silent edit of these expectations.
# ===========================================================================


def _i16_registry(seam: Any, typed_payloads: dict[str, Any], case: str) -> TenantMigrationRegistry:
    first, nul = typed_payloads["first"], typed_payloads["payload_nul"]
    role = unit_def(ORDINARY_SQL)
    cases: dict[str, dict[str, Any]] = {
        "a-role-only": {"t001_test_cg4_a": role},
        "b-role-then-typed": {
            "t001_test_cg4_a": role,
            first["migration_id"]: seam.typed_definition(first),
        },
        "c-typed-then-role": {
            first["migration_id"]: seam.typed_definition(first),
            "t003_test_cg4_a": role,
        },
        "d1-policy-error-typed-then-role": {
            nul["migration_id"]: seam.typed_definition(nul),
            "t003_test_cg4_a": role,
        },
        "d2-role-then-policy-error-typed": {
            "t001_test_cg4_a": role,
            nul["migration_id"]: seam.typed_definition(nul),
        },
        "f-migrator-only": {"t001_test_cg4_m": unit_def(ORDINARY_SQL, role=None)},
    }
    return build(cases[case])


@pytest.mark.parametrize(
    ("case", "error_type", "code"),
    [
        ("a-role-only", MigrationUnitRejected, SCHEMA_KEY_INVALID),
        ("b-role-then-typed", TenantMigrationFailed, SCHEMA_KEY_INVALID),
        ("c-typed-then-role", TenantMigrationFailed, SCHEMA_KEY_INVALID),
        (
            "d1-policy-error-typed-then-role",
            TenantMigrationFailed,
            PreconditionCode.INSTALL_NUL_FORBIDDEN.value,
        ),
        (
            "d2-role-then-policy-error-typed",
            TenantMigrationFailed,
            PreconditionCode.INSTALL_NUL_FORBIDDEN.value,
        ),
        ("f-migrator-only", MigrationUnitRejected, SCHEMA_KEY_INVALID),
    ],
)
async def test_i16_an_invalid_schema_key_keeps_todays_type_code_and_order(
    seam: Any, typed_driver: Any, harness: Any, typed_payloads: dict[str, Any],
    case: str, error_type: type[Exception], code: str,
) -> None:
    """I-16 (a)-(d2), (f). Exact exception TYPE, not a subclass match."""

    registry = _i16_registry(seam, typed_payloads, case)
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())

    with pytest.raises((MigrationUnitRejected, TenantMigrationFailed)) as caught:
        await runner.apply_within_lock(tenant_id=TENANT, schema_key=BAD_SCHEMA)

    assert type(caught.value) is error_type
    assert caught.value.reason_code == code  # type: ignore[attr-defined]
    assert ledger_writes(connection) == ()
    assert "txn-begin" not in connection.kinds


async def test_i16e_nothing_pending_means_no_render_and_no_error(
    typed_driver: Any, harness: Any
) -> None:
    """I-16 (e). Every unit applied at an equal checksum: nothing renders, nothing raises."""

    registry = build({"t001_test_cg4_a": unit_def(ORDINARY_SQL)})
    runner, _ = drive(
        typed_driver, harness, registry,
        harness.ledger_row("applied", registry.units[0].checksum),
    )

    outcomes = await runner.apply_within_lock(tenant_id=TENANT, schema_key=BAD_SCHEMA)

    assert [outcome.applied for outcome in outcomes] == [False]


# ===========================================================================
# CL-2: the completeness invariant and the pass-3 fallback (interface).
# ===========================================================================


async def test_i17_a_present_but_none_checked_value_is_refused_before_any_install(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any
) -> None:
    """I-17. Completeness is by VALUE: a key holding None fails the REAL section 5.2 check.

    An EARLIER pending, migrator-owned, allowed unit precedes the None-valued
    role-bearing unit (Codex packet-v1 assertion review). Only the pass-2
    completeness check refuses before that earlier unit installs: if it were
    missing or a no-op, the pass-3 fallback would refuse only after t001 had
    installed and written its ledger rows, and this test would fail. The real
    `_require_checked_complete` is NOT patched here; I-18 covers the disabled case.
    """

    spy = ordinary_content_spy(returning=None)
    registry = build(
        {
            "t001_test_cg4_migrator": unit_def(ORDINARY_SQL, role=None),
            "t002_test_cg4_role": unit_def(SECOND_SQL),
        }
    )
    runner, connection = drive(typed_driver, harness, registry, harness.ledger_absent())

    assert spy.present
    assert await refusal(runner) == PROHIBITED
    assert_no_side_effects(connection, ORDINARY_SQL, SECOND_SQL)


async def test_i18_the_pass_three_fallback_refuses_with_the_cl2_code(
    typed_driver: Any, harness: Any, ordinary_content_spy: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-18. With the section 5.2 seam disabled, the pass-3 guard still refuses.

    Only the CODE is asserted. No whole-plan claim (CL-2).
    """

    monkeypatch.setattr(runner_module, "_require_checked_complete", lambda *a, **k: None)
    spy = ordinary_content_spy(returning=None)
    runner, _ = drive(
        typed_driver, harness, build({"t001_test_cg4_a": unit_def(ORDINARY_SQL)}),
        harness.ledger_absent(),
    )

    assert spy.present
    assert await refusal(runner) == PROHIBITED


# ===========================================================================
# CL-1: a loader fault through the runner (behavioural red at baseline).
# ===========================================================================


async def test_i19_a_parser_version_mismatch_refuses_with_no_side_effects(
    typed_driver: Any, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-19. Environment fault -> existing code, raised in pass 2 (CL-1)."""

    real = importlib.metadata.version

    def version(name: str) -> str:
        return "7.16" if name == "pglast" else real(name)

    monkeypatch.setattr(importlib.metadata, "version", version)
    runner, connection = drive(
        typed_driver, harness, build({"t001_test_cg4_a": unit_def(ORDINARY_SQL)}),
        harness.ledger_absent(),
    )

    assert await refusal(runner) == VERSION_MISMATCH
    assert_no_side_effects(connection, ORDINARY_SQL)
