"""CP2-2a pure rows: 2A-U01–U06, 2A-R06, 2A-H01, 2A-H03, 2A-P05.

No database. Placement of P05 here (not in the PostgreSQL module) keeps a
U-layer row out of the `postgres` marker; test cases v4 §4 is otherwise
followed.
"""

import hashlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

REVISION_004 = Path("alembic/versions/004_m02_lock_owner_role.py")


def _load_revision_004() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m02_revision_004", REVISION_004)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 2A-U01 .. U06 ---------------------------------------------------------


def test_2a_u01_production_registry_ids_and_target_version() -> None:
    """R-A3, R-B3, L-1, L-6 (X-1, X-2). The number only; nothing about runtime acceptance."""

    from haloflow.composition import build_production_tenant_migrations

    registry = build_production_tenant_migrations()
    assert registry.migration_ids == (
        "t001_m01_baseline",
        "t002_m02_operation_registry",
        "t003_m02_lock_operation",
    )
    assert registry.target_version == 3


def test_2a_u02_the_unit_set_and_the_one_role_are_approved() -> None:
    """R-B0 (X-3, X-4): the M02 unit set and exactly one execution role are approved."""

    from haloflow.composition import APPROVED_EXECUTION_ROLES, APPROVED_TENANT_MIGRATIONS
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS
    from haloflow.m02.units import (
        M02_TENANT_MIGRATIONS,
        T002_MIGRATION_ID,
        T002_SQL,
        T003_DEFINITION,
        T003_MIGRATION_ID,
    )

    assert APPROVED_TENANT_MIGRATIONS == (TENANT_MIGRATIONS, M02_TENANT_MIGRATIONS)
    assert frozenset({"haloflow_m02_lock_owner"}) == APPROVED_EXECUTION_ROLES
    assert dict(M02_TENANT_MIGRATIONS) == {
        T002_MIGRATION_ID: T002_SQL,
        T003_MIGRATION_ID: T003_DEFINITION,
    }
    assert T002_MIGRATION_ID == "t002_m02_operation_registry"
    assert T003_MIGRATION_ID == "t003_m02_lock_operation"
    with pytest.raises(TypeError):
        cast(Any, M02_TENANT_MIGRATIONS)["t003_x"] = "x"


def test_2a_u03_only_the_gateway_unit_is_role_bearing_and_it_is_typed() -> None:
    """R-A8 (B-beta), re-verified through the CG-4 route classification (X-5).

    No production unit is a role-bearing ORDINARY unit; exactly one unit bears a
    role, and it is the typed gateway unit.
    """

    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m01.provisioning.ordinary_content import requires_content_check

    registry = build_production_tenant_migrations()
    for unit in registry:
        # Role-bearing ordinary units are the only ones CG-4 checks; none exist,
        # so no production unit is both role-bearing and ordinary.
        assert requires_content_check(unit) is False, unit.migration_id
    assert [(unit.migration_id, unit.execution_role, unit.is_typed) for unit in registry] == [
        ("t001_m01_baseline", None, False),
        ("t002_m02_operation_registry", None, False),
        ("t003_m02_lock_operation", "haloflow_m02_lock_owner", True),
    ]


def test_2a_u04_shipped_manifest_declares_the_lock_owner_and_its_n1_edge() -> None:
    """R-A1, AQ-1, R-B10 (X-6): all-false profile, schema USAGE and CREATE, exactly the N1 edge."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    manifest = load_provisioning_manifest()
    assert "haloflow_m02_lock_owner" in manifest.execution_role_profiles
    profile = manifest.execution_role_profiles["haloflow_m02_lock_owner"]
    assert (
        profile.login,
        profile.superuser,
        profile.createdb,
        profile.createrole,
        profile.replication,
        profile.bypassrls,
    ) == (False,) * 6
    assert profile.tenant_schema_privileges == ("CREATE", "USAGE")
    edges = [e for e in manifest.role_memberships if e.role == "haloflow_m02_lock_owner"]
    assert [(e.member, e.set, e.inherit, e.admin) for e in edges] == [
        ("haloflow_migrator", True, False, False)
    ]


def test_2a_u05_the_lock_owner_name_stays_out_of_m01_provisioning() -> None:
    """M-8: the module-role literal scan scope stays clean."""

    offenders = [
        str(path)
        for path in Path("src/haloflow/m01/provisioning").rglob("*.py")
        if "haloflow_m02_lock_owner" in path.read_text()
    ]
    assert offenders == []


def test_2b_u_module_scope() -> None:
    """X-11 (O-5), replacing 2A-U06: exact M02 module set, exact top-level `LOCK_*`
    bindings per module, exact refusal-code vocabulary, and the gateway function
    name confined to the three modules that must use it."""

    from haloflow.m02.codes import LockRefusalCode

    package = Path("src/haloflow/m02")
    sources = {path.name: path.read_text() for path in package.glob("*.py")}
    assert set(sources) == {
        "__init__.py",
        "roles.py",
        "units.py",
        "gateway_profile.py",
        "codes.py",
        "lock.py",
    }
    bound = {
        name: set(re.findall(r"^(LOCK_[A-Z0-9_]*)\s*[:=]", source, re.MULTILINE))
        for name, source in sources.items()
    }
    assert bound == {
        "__init__.py": set(),
        "roles.py": {"LOCK_OWNER_ROLE"},
        "units.py": set(),
        "gateway_profile.py": {"LOCK_OPERATION_PROFILE"},
        "codes.py": set(),
        "lock.py": set(),
    }
    assert {member.name for member in LockRefusalCode} == {
        "LOCK_OPERATION_ID_REQUIRED",
        "LOCK_OPERATION_NOT_FOUND",
        "LOCK_TENANT_CONTEXT_INVALID",
    }
    mentions = {name for name, source in sources.items() if "m02_lock_operation" in source}
    assert mentions == {"units.py", "gateway_profile.py", "lock.py"}


def test_2a_u_role_constant() -> None:
    """ET-2 I-1 (binding used by every row)."""

    from haloflow.m02.roles import LOCK_OWNER_ROLE

    assert LOCK_OWNER_ROLE == "haloflow_m02_lock_owner"


# --- 2A-R06 ----------------------------------------------------------------


def test_2a_r06_downgrade_raises_before_any_op_call() -> None:
    """Architecture §3: as `001`–`003`, `downgrade()` raises and touches nothing."""

    module = _load_revision_004()
    assert (module.revision, module.down_revision) == ("004", "003")

    calls: list[tuple[str, Any]] = []

    class _RecordingOp:
        def __getattr__(self, name: str) -> Any:
            def _record(*args: Any, **kwargs: Any) -> None:
                calls.append((name, args))

            return _record

    setattr(module, "op", _RecordingOp())  # noqa: B010 - module attribute swap
    with pytest.raises(RuntimeError):
        module.downgrade()
    assert calls == []


def test_2a_r_upgrade_executes_exactly_the_reviewed_constant() -> None:
    """ET-2 I-7: `upgrade()` runs `LOCK_OWNER_ROLE_SQL`, which R05 reuses."""

    module = _load_revision_004()
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class _RecordingOp:
        def execute(self, *args: Any) -> None:
            calls.append(("execute", args))

    setattr(module, "op", _RecordingOp())  # noqa: B010 - module attribute swap
    module.upgrade()
    assert calls == [("execute", (module.LOCK_OWNER_ROLE_SQL,))]
    sql_text = " ".join(module.LOCK_OWNER_ROLE_SQL.split())
    assert "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in sql_text
    assert (
        "GRANT haloflow_m02_lock_owner TO haloflow_migrator "
        "WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;"
    ) in sql_text


# --- 2A-H01, H03 -----------------------------------------------------------


def test_2a_h01_emitted_rejector_body_matches_the_reviewed_literal(m02: ModuleType) -> None:
    from haloflow.m02.units import T002_SQL

    body = m02.rejector_body(T002_SQL)
    assert body == m02.REJECTOR_BODY
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == m02.REJECTOR_BODY_SHA256


def test_2a_h03_guard_embeds_the_same_reviewed_literal(m02: ModuleType) -> None:
    from haloflow.m02.units import T002_SQL

    guard = m02.Template.parse(T002_SQL).steps["guard"]
    assert guard.count(m02.REJECTOR_BODY_SHA256) == 1
    assert T002_SQL.count(m02.REJECTOR_BODY_SHA256) == 1


def test_template_handles_parse_and_every_mutant_builds(m02: ModuleType) -> None:
    """ET-2 I-3..I-6 precondition for the X/O/Q/T04c/S rows.

    This is not a matrix row. It makes a handle defect fail here, once and
    explicitly, instead of as a setup error in every PostgreSQL row.
    """

    from haloflow.m02.units import REJECTOR_FUNCTION_NAME, REJECTOR_TRIGGER_NAME, T002_SQL

    assert (REJECTOR_FUNCTION_NAME, REJECTOR_TRIGGER_NAME) == (
        m02.REJECTOR_FUNCTION,
        m02.REJECTOR_TRIGGER,
    )
    template = m02.Template.parse(T002_SQL)
    assert template.join() == T002_SQL
    assert template.steps["lock_owner_grants"].count(m02.LOCK_OWNER_GRANT) == 1
    m02.guard_group_bounds(template.steps["guard"])
    for name in m02.MUTANTS:
        mutated = m02.mutant_template(T002_SQL, name)
        assert mutated != T002_SQL, name
        assert "{schema}" in mutated, name


# --- 2A-P05 ----------------------------------------------------------------


def test_2a_p05_runtime_override_is_select_only() -> None:
    """R-A4 + RQ-2: the declaration is `("SELECT",)`."""

    from haloflow.m01.provisioning.manifest import load_provisioning_manifest

    overrides = [
        o
        for o in load_provisioning_manifest().tenant_table_overrides
        if (o.role, o.table) == ("haloflow_runtime", "operation_registry")
    ]
    assert len(overrides) == 1
    assert overrides[0].privileges == ("SELECT",)
