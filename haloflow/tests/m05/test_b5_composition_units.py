"""UC1 part B, tranche B5 — composition, startup inputs, keys/producer and the
activation validator (unit rows, no database).

The design is package v2 + v2r1, aligned by Codex 4b0a5448
(codex_b5-v2r1-alignment.md):
- B5-C01..C10: the production composition delta (v2 section 1);
- B5-I01..I08: exact startup input validation before any I/O (v2 2.1, v2r1 5);
- B5-K01..K04: fixed producer and key provider, construction location
  (v2 section 3). These are repository-control evidence only: L-1W stays open;
- B5-V01..V09: activation linkage, then M05-A1, then the existing production
  rules, then routes (v2 section 4, precedence aligned).

These rows retire MB1-S05 and MB1-S07 in test_m05_registry_units.py. Those two
pinned "production unchanged until B5".

Expected values come from b5_support / m05_support / staff_support, never from
the code under test.
"""

from __future__ import annotations

import ast
import asyncio
import enum
import json
from pathlib import Path
from typing import Any

import b5_support as b5
import m05_support as m05
import pytest
import staff_support as st

SRC = Path("src/haloflow")


# ---------------------------------------------------------------------------
# B5-C: composition
# ---------------------------------------------------------------------------


def test_b5_c01_production_statement_set_is_exact() -> None:
    from haloflow import composition

    catalog_keys: dict[str, tuple[str, str]] = {}
    for group in composition.APPROVED_MODULE_STATEMENTS:
        for key, (mode, capability, _query) in group.items():
            assert key not in catalog_keys, key  # registered once
            catalog_keys[key] = (mode.value, capability)
    assert catalog_keys == b5.STATEMENTS


def test_b5_c02_staff_keys_live_in_m01_statements_only() -> None:
    """P-1(a): one canonical mapping. STAFF_STATEMENTS no longer exists."""

    from haloflow.m01 import statements

    assert set(statements.M01_STATEMENTS) == b5.M01_STATEMENT_KEYS
    assert not hasattr(statements, "STAFF_STATEMENTS")


def test_b5_c03_production_registry_units_and_target() -> None:
    from haloflow import composition

    registry = composition.build_production_tenant_migrations()
    assert registry.migration_ids == b5.PRODUCTION_UNIT_IDS
    assert registry.target_version == b5.PRODUCTION_TARGET


def test_b5_c04_execution_roles_are_exact() -> None:
    from haloflow import composition

    assert composition.APPROVED_EXECUTION_ROLES == b5.EXECUTION_ROLES


def test_b5_c05_every_typed_unit_has_a_profile_and_nothing_else_does() -> None:
    from haloflow import composition

    registry = composition.build_production_tenant_migrations()
    typed = {unit.migration_id for unit in registry if unit.is_typed}
    assert set(m05.M05_TYPED_UNITS) <= typed
    assert set(st.STAFF_TYPED_UNITS) <= typed
    assert set(composition.APPROVED_INSTALLED_STATE_PROFILES) == typed


def test_b5_c06_supported_set_is_the_literal_25() -> None:
    from haloflow import composition

    assert composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS == b5.PRODUCTION_SUPPORTED


def _registry_without(dropped: str) -> Any:
    from haloflow import composition
    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    profiles = dict(composition.APPROVED_INSTALLED_STATE_PROFILES)
    del profiles[dropped]
    return build_tenant_migration_registry(
        *composition.APPROVED_TENANT_MIGRATIONS,
        approved_execution_roles=composition.APPROVED_EXECUTION_ROLES,
        installed_state_profiles=profiles,
    )


@pytest.mark.parametrize(
    "dropped", ["t015_m05_prepare_action", "t022_m01_staff_assignable", "t009_m02_append"]
)
def test_b5_c07_generalized_profile_rule_refuses_a_profile_less_typed_unit(dropped: str) -> None:
    from haloflow.composition import require_installed_state_profiles
    from haloflow.m01.errors import MigrationUnitRejected

    registry = _registry_without(dropped)
    with pytest.raises(MigrationUnitRejected) as caught:
        require_installed_state_profiles(registry)
    assert caught.value.reason_code == "INSTALLED_STATE_PROFILE_INVALID"


def test_b5_c07_control_the_production_registry_satisfies_the_rule() -> None:
    from haloflow.composition import (
        build_production_tenant_migrations,
        require_installed_state_profiles,
    )

    require_installed_state_profiles(build_production_tenant_migrations())


def test_b5_c08_provisioning_manifest_changes_exactly_two_values() -> None:
    from m05_harness import production_manifest_document

    profiles = production_manifest_document()["execution_role_profiles"]
    assert {role: p["tenant_schema_privileges"] for role, p in profiles.items()} == (
        b5.TENANT_SCHEMA_PRIVILEGES
    )


def test_b5_c09_statement_catalogue_manifest_pins_the_exact_set() -> None:
    document = json.loads((SRC / "m01/manifests/statement_catalog.json").read_text())
    pinned = {
        key: (entry["mode"], entry["required_capability"])
        for key, entry in document["statements"].items()
    }
    assert pinned == b5.STATEMENTS


def test_b5_c10_ci_and_makefile_cover_startup() -> None:
    workflow = Path("../.github/workflows/m01.yml").read_text().splitlines()
    ruff_line = next(line for line in workflow if "bin/ruff check" in line)
    mypy_line = next(line for line in workflow if "bin/mypy " in line)
    assert b5.STARTUP_PATH in ruff_line.split()
    assert b5.STARTUP_PATH in mypy_line.split()
    makefile = Path("Makefile").read_text().splitlines()
    lint = next(line for line in makefile if line.startswith("LINT_PATHS ="))
    types = next(line for line in makefile if line.startswith("TYPE_PATHS ="))
    assert b5.STARTUP_PATH in lint.split()
    assert b5.STARTUP_PATH in types.split()


# ---------------------------------------------------------------------------
# B5-I: exact inputs, refused before any I/O
# ---------------------------------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        raise AssertionError("no I/O is allowed before input validation")


def _dependencies(spy: _Spy) -> Any:
    from haloflow.m01.runtime import TenantRuntimeDependencies

    sentinel: Any = object()
    return TenantRuntimeDependencies(
        pool=sentinel, control_store=sentinel, migrator_connect=spy, provisioner_connect=spy
    )


def _start(deployment: Any, served: Any, spy: _Spy | None = None) -> Any:
    from haloflow.startup import start_production_services

    spy = spy or _Spy()
    return asyncio.run(
        start_production_services(_dependencies(spy), deployment=deployment, served_tenants=served)
    )


class _Lookalike(enum.Enum):
    PRODUCTION = "production"
    NON_PRODUCTION = "non_production"


class _StrSub(str):
    pass


@pytest.mark.parametrize(
    "deployment",
    ["production", "non_production", True, 1, None, _Lookalike.PRODUCTION, _StrSub("production")],
    ids=["str", "str-nonprod", "bool", "int", "none", "lookalike-enum", "str-subclass"],
)
def test_b5_i01_deployment_must_be_the_exact_enum(deployment: Any) -> None:
    spy = _Spy()
    with pytest.raises(TypeError):
        _start(deployment, ("tenant-a1",), spy)
    assert spy.calls == 0


def test_b5_i02_deployment_has_no_default() -> None:
    import inspect

    from haloflow.startup import start_production_services

    parameter = inspect.signature(start_production_services).parameters["deployment"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_b5_i03_signature_takes_no_override_parameters() -> None:
    import inspect

    from haloflow.startup import start_production_services

    assert list(inspect.signature(start_production_services).parameters) == [
        "dependencies", "deployment", "served_tenants",
    ]


def test_b5_i04_empty_tenant_set_refused() -> None:
    from haloflow.startup import Deployment, StartupRefused

    spy = _Spy()
    for served in ((), []):
        with pytest.raises(StartupRefused) as caught:
            _start(Deployment.PRODUCTION, served, spy)
        assert caught.value.reason_code == "STARTUP_NO_TENANTS"
        assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert spy.calls == 0


def test_b5_i05_duplicate_tenant_refused_not_deduplicated() -> None:
    from haloflow.startup import Deployment, StartupRefused

    spy = _Spy()
    with pytest.raises(StartupRefused) as caught:
        _start(Deployment.PRODUCTION, ("tenant-a1", "tenant-b2", "tenant-a1"), spy)
    assert caught.value.reason_code == "STARTUP_TENANT_DUPLICATE"
    assert caught.value.tenant_index == 2
    assert spy.calls == 0


@pytest.mark.parametrize(
    "served",
    [("tenant-a1", 7), ("tenant-a1", None), (_StrSub("tenant-a1"),), "tenant-a1", {"tenant-a1"}],
    ids=["int-element", "none-element", "str-subclass-element", "bare-str", "set"],
)
def test_b5_i06_tenant_collection_and_elements_must_be_exact(served: Any) -> None:
    from haloflow.startup import Deployment

    spy = _Spy()
    with pytest.raises(TypeError):
        _start(Deployment.PRODUCTION, served, spy)
    assert spy.calls == 0


@pytest.mark.parametrize("bad", ["Tenant-A1", "a", "-tenant", "tenant_a1", "x" * 70])
def test_b5_i07_tenant_id_grammar_enforced(bad: str) -> None:
    from haloflow.startup import Deployment

    spy = _Spy()
    with pytest.raises(ValueError):
        _start(Deployment.PRODUCTION, ("tenant-a1", bad), spy)
    assert spy.calls == 0


def test_b5_i08_dependencies_must_be_the_exact_dataclass() -> None:
    from haloflow.startup import Deployment, start_production_services

    spy = _Spy()

    class Lookalike:
        pool = control_store = None
        migrator_connect = provisioner_connect = spy

    with pytest.raises(TypeError):
        asyncio.run(start_production_services(
            Lookalike(), deployment=Deployment.PRODUCTION, served_tenants=("tenant-a1",)
        ))
    assert spy.calls == 0


# ---------------------------------------------------------------------------
# B5-K: producer, keys, construction location (repository control)
# ---------------------------------------------------------------------------


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            label = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            if label == name:
                found.append(node)
    return found


def _src_trees() -> dict[Path, ast.AST]:
    return {path: ast.parse(path.read_text()) for path in SRC.rglob("*.py")}


def test_b5_k01_services_are_constructed_only_in_startup() -> None:
    trees = _src_trees()
    for name in ("M05Service", "StaffService", "EventFoundation"):
        sites = {path for path, tree in trees.items() if _calls(tree, name)}
        assert sites == {Path(b5.STARTUP_PATH)}, (name, sites)


def test_b5_k02_startup_fixes_the_producer_literal() -> None:
    tree = ast.parse(Path(b5.STARTUP_PATH).read_text())
    (call,) = _calls(tree, "EventFoundation")
    keywords = {kw.arg: kw.value for kw in call.keywords}
    value = keywords["producer_service"]
    assert isinstance(value, ast.Constant) and value.value == b5.PRODUCER


def test_b5_k03_startup_wires_only_the_fail_closed_key_provider() -> None:
    tree = ast.parse(Path(b5.STARTUP_PATH).read_text())
    (call,) = _calls(tree, "M05Service")
    keys = {kw.arg: kw.value for kw in call.keywords}["keys"]
    assert isinstance(keys, ast.Call) and isinstance(keys.func, ast.Name)
    assert keys.func.id == "ProductionNoteKeys"
    providers = {
        node.name
        for path, tree in _src_trees().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        # Exactly the known structural interface, m05/notes.py `class
        # NoteKeyProvider(Protocol)`, is excluded (Codex 6c4c51f7). Any other
        # definition, a Protocol included, still counts as a provider.
        and not (
            (path, node.name) == (SRC / "m05/notes.py", "NoteKeyProvider")
            and [ast.unparse(base) for base in node.bases] == ["Protocol"]
        )
        and {"current_hmac", "current_encryption"}
        <= {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    }
    assert providers == {"ProductionNoteKeys"}


def test_b5_k04_startup_and_composition_import_nothing_from_tests() -> None:
    for path in (Path(b5.STARTUP_PATH), SRC / "composition.py"):
        tree = ast.parse(path.read_text())
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        forbidden = ("tests", "conftest", "_support", "_harness", "service_support")
        assert not [m for m in modules if m.startswith("tests") or m.endswith(forbidden)], path
    # The production provider is the one the startup factory imports.
    startup = Path(b5.STARTUP_PATH).read_text()
    assert "from haloflow.m05.notes import" in startup and "ProductionNoteKeys" in startup


# ---------------------------------------------------------------------------
# B5-V: activation linkage, M05-A1, then the existing rules
# ---------------------------------------------------------------------------


def _v() -> Any:
    from haloflow.m02 import registry_validation

    return registry_validation


def _m05_contract(v: Any, active: bool) -> Any:
    return v.ContractRow(
        event_type=b5.M05_TYPE, version=1, event_level="observation", is_active=active,
        projection_rule_id=None,
        signal_rule={"handoff_kind": "event_signal", "signal_type": "m02.event.appended"},
        value_constraints={"operation_id": {"must_be_null": True}},
    )


def _generation(v: Any, family: bool, contract: bool, resolver: bool, *,
                test_family: bool = False, synthetic: bool = False) -> Any:
    contracts = [_m05_contract(v, contract)]
    if test_family:
        contracts.append(v.ContractRow(
            event_type="m02_test_observation_received", version=1, event_level="observation",
            is_active=True, projection_rule_id=None,
            signal_rule={"handoff_kind": "event_signal", "signal_type": "m02.event.appended"},
            value_constraints={"operation_id": {"must_be_null": True}},
        ))
    resolvers = [v.ResolverRow(b5.M05_RESOLVER, "module_owned", resolver)]
    if synthetic:
        resolvers.append(v.ResolverRow("m02_test_subject_v1", "synthetic_fixture", True))
    return v.LoadedGeneration(
        contracts=tuple(contracts), resolvers=tuple(resolvers), test_targets_present=False,
        families=(v.FamilyRow(b5.M05_FAMILY, family),),
    )


def _refused(v: Any, generation: Any, deployment: str) -> str:
    with pytest.raises(v.RegistryRefused) as caught:
        v.validate_loaded_generation(generation, deployment=deployment)
    return str(caught.value.reason_code)


def test_b5_v01_full_activation_in_production_refused() -> None:
    v = _v()
    assert _refused(v, _generation(v, True, True, True), "production") == (
        "M05_ACTIVATION_NOT_AUTHORIZED"
    )


def test_b5_v02_full_activation_in_non_production_accepted() -> None:
    v = _v()
    assert v.validate_loaded_generation(
        _generation(v, True, True, True), deployment="non_production"
    ) is None


PARTIAL = [
    (True, False, False), (False, True, False), (False, False, True),
    (True, True, False), (True, False, True), (False, True, True),
]


@pytest.mark.parametrize("flags", PARTIAL, ids=lambda f: "".join("1" if x else "0" for x in f))
@pytest.mark.parametrize("deployment", ["production", "non_production"])
def test_b5_v03_partial_activation_is_route_not_installed(flags: Any, deployment: str) -> None:
    v = _v()
    assert _refused(v, _generation(v, *flags), deployment) == "ROUTE_NOT_INSTALLED"


def test_b5_v04_partial_with_test_family_in_production_is_linkage_first() -> None:
    v = _v()
    generation = _generation(v, True, True, False, test_family=True)
    assert _refused(v, generation, "production") == "ROUTE_NOT_INSTALLED"


def test_b5_v05_full_with_test_family_in_production_is_m05_a1_first() -> None:
    v = _v()
    generation = _generation(v, True, True, True, test_family=True)
    assert _refused(v, generation, "production") == "M05_ACTIVATION_NOT_AUTHORIZED"


def test_b5_v06_full_with_synthetic_resolver_in_production_is_m05_a1_first() -> None:
    v = _v()
    generation = _generation(v, True, True, True, synthetic=True)
    assert _refused(v, generation, "production") == "M05_ACTIVATION_NOT_AUTHORIZED"


def test_b5_v07_all_inactive_in_production_accepted() -> None:
    v = _v()
    assert v.validate_loaded_generation(
        _generation(v, False, False, False), deployment="production"
    ) is None


def test_b5_v08_part_a_fixture_without_m05_rows_keeps_its_outcome() -> None:
    """A generation with no M05 family, type or resolver (Part A only) is not judged
    by the linkage rule; the existing constructor still means 'no families'."""

    v = _v()
    generation = v.LoadedGeneration(contracts=(), resolvers=(), test_targets_present=False)
    assert generation.families == ()
    assert v.validate_loaded_generation(generation, deployment="production") is None


def test_b5_v09_inactive_m05_with_test_family_keeps_the_existing_refusal() -> None:
    v = _v()
    generation = _generation(v, False, False, False, test_family=True)
    assert _refused(v, generation, "production") == "TEST_FAMILY_ACTIVE_IN_PRODUCTION"
