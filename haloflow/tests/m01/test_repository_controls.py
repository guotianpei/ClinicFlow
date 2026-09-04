import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from haloflow.composition import build_production_catalog

M01_ROOT = Path("src/haloflow/m01")


def test_shared_schema_manifest_explicitly_prohibits_phi() -> None:
    manifest = json.loads((M01_ROOT / "manifests/shared_schema_classification.json").read_text())

    assert manifest["policy"]["phi_allowed"] is False
    allowed = set(manifest["policy"]["allowed_classifications"])
    for table in manifest["tables"].values():
        assert table["classification"] in allowed
        assert set(table["columns"].values()) <= allowed


def test_psycopg_pool_import_is_owned_only_by_m01() -> None:
    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        if path.is_relative_to(M01_ROOT):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "psycopg_pool":
                violations.append(str(path))
            if isinstance(node, ast.Import) and any(
                alias.name == "psycopg_pool" for alias in node.names
            ):
                violations.append(str(path))

    assert violations == []


# Names that application modules may never reach. The public
# `build_statement_catalog` is deliberately absent: it is the supported entry
# point (B2.1), and the whole point of this check is that the difference between
# it and `_build_statement_catalog` is a real boundary rather than a convention.
M01_PACKAGE = "haloflow.m01"
COMPOSITION_ROOT = "src/haloflow/composition.py"
DEFINITION_SET_NAME = re.compile(r"^M\d{2}_STATEMENTS$")
DEFINITION_SET_LOCATION = re.compile(r"^src/haloflow/m\d{2}/statements\.py$")

PROTECTED_MODULES = frozenset({"haloflow.m01.pool"})
PROTECTED_NAMES = frozenset(
    {
        "_build_statement_catalog",
        "_CATALOG_ISSUER",
        "_CONTEXT_ISSUER",
        "issue_tenant_context",
    }
)


def _protected_imports_in(source: str) -> list[str]:
    """Names from the protected set that this source imports, in any form.

    The previous version of this check substring-matched the raw source for
    dotted strings such as "haloflow.m01.statements._build_statement_catalog".
    That form never appears in `from haloflow.m01.statements import
    _build_statement_catalog`, so the check silently missed the import style
    actually used, and could also fire on a mention inside a comment or string.

    Four forms have to be caught, and the first version of the AST rewrite only
    caught two:
      import haloflow.m01.pool
      from haloflow.m01.statements import _build_statement_catalog
      from haloflow.m01 import pool               <- submodule via parent package
      from haloflow.m01.context import *          <- wildcard pulls the issuer in
    """

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_protected_module(alias.name):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_protected_module(module):
                found.append(module)
            for alias in node.names:
                # `from haloflow.m01 import pool` reaches a protected submodule
                # through its parent package.
                if _is_protected_module(f"{module}.{alias.name}") or alias.name in PROTECTED_NAMES:
                    found.append(f"{module}.{alias.name}")
                elif alias.name == "*" and module.startswith(M01_PACKAGE):
                    # A wildcard from any m01 module may pull a protected name in;
                    # it cannot be resolved statically, so it is refused outright.
                    found.append(f"{module}.*")
        elif isinstance(node, ast.Attribute) and node.attr in PROTECTED_NAMES:
            found.append(node.attr)
    return found


def _is_protected_module(dotted: str) -> bool:
    return dotted in PROTECTED_MODULES or any(
        dotted.startswith(f"{module}.") for module in PROTECTED_MODULES
    )


def _composition_violations_in(path: str, source: str) -> list[str]:
    """Production code outside the composition root may not compose a catalogue.

    B2.11. Without this, any module can build its own catalogue and install it,
    and `statement_catalog.json` pins a constant rather than the catalogue the
    application runs -- so the manifest would not be the review gate it claims.
    """

    violations: list[str] = []
    if path == COMPOSITION_ROOT:
        return violations
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "build_statement_catalog":
                    violations.append(f"{path}: imports build_statement_catalog")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name == "build_statement_catalog":
                violations.append(f"{path}: calls build_statement_catalog")
    return violations


def _definition_set_violations_in(path: str, source: str) -> list[str]:
    """Production definition sets live only in `haloflow/mNN/statements.py`."""

    violations: list[str] = []
    if DEFINITION_SET_LOCATION.fullmatch(path):
        return violations
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and DEFINITION_SET_NAME.fullmatch(target.id):
                violations.append(f"{path}: defines {target.id}")
    return violations


def test_m01_private_construction_paths_are_not_reachable_from_application_modules() -> None:
    """TC-D10. Scoped to the production boundary; tests keep their private imports."""

    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        if path.is_relative_to(M01_ROOT):
            continue
        for name in _protected_imports_in(path.read_text()):
            violations.append(f"{path}: {name}")

    assert violations == []


def test_protected_import_check_catches_every_import_form() -> None:
    """TC-D9. Four forms; the first AST rewrite caught only two (finding F-4)."""

    forms = {
        "dotted member": "from haloflow.m01.statements import _build_statement_catalog\n",
        "aliased member": "from haloflow.m01.statements import _build_statement_catalog as b\n",
        "module": "import haloflow.m01.pool\n",
        "module aliased": "import haloflow.m01.pool as p\n",
        "submodule via parent": "from haloflow.m01 import pool\n",
        "submodule aliased": "from haloflow.m01 import pool as p\n",
        "wildcard": "from haloflow.m01.context import *\n",
        "attribute": "import haloflow\nhaloflow.m01.context.issue_tenant_context()\n",
    }
    missed = [name for name, source in forms.items() if not _protected_imports_in(source)]
    assert missed == []


def test_protected_import_check_permits_the_public_builder() -> None:
    public = "from haloflow.m01.statements import build_statement_catalog\n"
    assert _protected_imports_in(public) == []
    assert _protected_imports_in("from haloflow.m01 import build_statement_catalog\n") == []


def test_protected_import_check_does_not_fire_on_comments_or_strings() -> None:
    """TC-D11. The false-positive class the substring version had."""

    commented = "# do not use _build_statement_catalog here\nx = 1\n"
    stringified = 'DOC = "never import haloflow.m01.pool"\n'

    assert _protected_imports_in(commented) == []
    assert _protected_imports_in(stringified) == []


def test_legacy_database_bypass_allowlist_cannot_grow() -> None:
    expected = {
        "src/haloflow/database.py",
        "src/haloflow/modules/care_gaps/models.py",
        "src/haloflow/modules/care_gaps/router.py",
        "src/haloflow/modules/care_gaps/service.py",
        "src/haloflow/modules/eligibility/models.py",
        "src/haloflow/modules/eligibility/router.py",
        "src/haloflow/modules/eligibility/service.py",
        "src/haloflow/modules/fax/models.py",
        "src/haloflow/modules/fax/router.py",
        "src/haloflow/modules/fax/service.py",
        "src/haloflow/modules/reminders/models.py",
        "src/haloflow/modules/reminders/router.py",
        "src/haloflow/modules/reminders/service.py",
        "src/haloflow/scheduler.py",
    }
    actual: set[str] = set()
    for path in Path("src/haloflow").rglob("*.py"):
        if path.is_relative_to(M01_ROOT):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if any(
                module == "haloflow.database"
                or module == "sqlalchemy"
                or module.startswith("sqlalchemy.")
                or module == "asyncpg"
                or module.startswith("asyncpg.")
                for module in modules
            ):
                actual.add(str(path))

    assert actual == expected


def test_runtime_role_has_no_global_audit_write_in_permissions_manifest() -> None:
    manifest = json.loads((M01_ROOT / "manifests/permissions.json").read_text())

    runtime = manifest["haloflow_runtime"]
    assert "shared.access_audit_log:insert,update,delete,truncate" in runtime["deny"]


def test_statement_catalog_manifest_pins_the_production_composition_root() -> None:
    """TC-D8. The manifest gates what the application actually composes.

    Composed through `build_production_catalog`, not a hard-coded constant, so
    any addition, removal, mode or capability change, or query edit fails here
    unless the manifest changes in the same commit.
    """

    manifest = json.loads((M01_ROOT / "manifests/statement_catalog.json").read_text())
    compiled = build_production_catalog()

    expected = {
        statement.key: {
            "mode": statement.mode.value,
            "required_capability": statement.required_capability,
            "sha256": hashlib.sha256(
                " ".join(statement.query.split()).encode("utf-8")
            ).hexdigest(),
        }
        for statement in compiled.catalog.statements()
    }

    assert manifest["statements"] == expected
    assert manifest["composition_root"] == COMPOSITION_ROOT


def test_derived_write_capabilities_match_the_production_catalogue() -> None:
    """TC-D6, over the real composition root."""

    compiled = build_production_catalog()

    assert compiled.write_capabilities == frozenset(
        statement.required_capability
        for statement in compiled.catalog.statements()
        if statement.mode.value == "write"
    )


def test_only_the_composition_root_composes_a_statement_catalogue() -> None:
    """F-1. B2.11: composition is restricted, so the manifest can be a real gate."""

    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        violations.extend(_composition_violations_in(str(path), path.read_text()))

    assert violations == []


def test_production_definition_sets_live_only_in_module_statement_files() -> None:
    """F-1. An unpinned definition set defined elsewhere would bypass the manifest."""

    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        violations.extend(_definition_set_violations_in(str(path), path.read_text()))

    assert violations == []


def test_an_application_module_cannot_compose_or_define_an_unpinned_catalogue() -> None:
    """F-1 negative control: the checks above fail when they should."""

    rogue_import = "from haloflow.m01.statements import build_statement_catalog\n"
    rogue_call = "catalog = build_statement_catalog({})\n"
    rogue_qualified = "import haloflow\nx = haloflow.m01.statements.build_statement_catalog({})\n"
    rogue_definitions = 'M07_STATEMENTS = {"m07.read": (None, "c", "SELECT 1")}\n'

    app = "src/haloflow/modules/rogue/service.py"
    assert _composition_violations_in(app, rogue_import)
    assert _composition_violations_in(app, rogue_call)
    assert _composition_violations_in(app, rogue_qualified)
    assert _definition_set_violations_in(app, rogue_definitions)

    # ...and permit the same things in their approved locations.
    assert _composition_violations_in(COMPOSITION_ROOT, rogue_import) == []
    assert _composition_violations_in(COMPOSITION_ROOT, rogue_call) == []
    assert _definition_set_violations_in("src/haloflow/m07/statements.py", rogue_definitions) == []


def test_compiled_catalog_write_capabilities_cannot_be_supplied_or_drift() -> None:
    """F-2. Derivation is true by construction: there is no field to disagree with."""

    from haloflow.m01.statements import CompiledCatalog

    compiled = build_production_catalog()

    # No independent field exists to supply.
    with pytest.raises(TypeError):
        CompiledCatalog(  # type: ignore[call-arg]
            catalog=compiled.catalog,
            write_capabilities=frozenset({"forged:write"}),
        )
    # And the derived value cannot be rebound on an instance. A frozen dataclass
    # with __slots__ and a property refuses this as TypeError rather than the
    # usual FrozenInstanceError, so both are accepted -- the property under test
    # is that it is refused, not which exception carries the refusal.
    with pytest.raises((AttributeError, TypeError)):
        compiled.write_capabilities = frozenset({"forged:write"})  # type: ignore[misc]


def test_classification_manifest_tracks_the_renamed_execution_id_column() -> None:
    """TC-A11. The manifest must move with migration 002, not lag behind it."""

    manifest = json.loads((M01_ROOT / "manifests/shared_schema_classification.json").read_text())

    renamed = {"tenant_state_history", "access_audit_log", "isolation_alerts"}
    for name in renamed:
        columns = manifest["tables"][name]["columns"]
        assert "execution_id" in columns, name
        assert "operation_id" not in columns, name


def test_ci_workflow_covers_every_checked_production_path() -> None:
    """TC-R3, asserted rather than asserted-about.

    Adding `composition.py` outside `src/haloflow/m01` silently escaped the CI
    gate until this test existed. A new production file that lint and mypy do
    not see is exactly the failure this is here to catch.
    """

    workflow = Path("../.github/workflows/m01.yml").read_text()
    ruff_line = next(line for line in workflow.splitlines() if "ruff check" in line)
    mypy_line = next(line for line in workflow.splitlines() if "mypy " in line)

    checked = {"src/haloflow/m01", "src/haloflow/composition.py"}
    for required in checked:
        assert required in ruff_line, f"{required} not linted by CI"
        assert required in mypy_line, f"{required} not type-checked by CI"

    # Every production module reachable from the composition root must sit under
    # a path CI actually checks.
    for path in Path("src/haloflow").rglob("*.py"):
        text = path.read_text()
        if "haloflow.m01" not in text and "composition" not in str(path):
            continue
        if str(path).startswith("src/haloflow/modules/"):
            continue  # legacy bypass modules, tracked separately
        assert any(str(path).startswith(prefix.rstrip(".py")) for prefix in checked), path


# --- per-tenant migration registry controls (R-E12) ------------------------


def _migration_composition_violations_in(path: str, source: str) -> list[str]:
    """Production code outside the composition root may not compose a registry.

    The same rule as the statement catalogue, for the same reason: a second
    composition path would mean the production registry is not reviewable in one
    place, and a test-only unit could reach a real tenant schema through it.
    """

    violations: list[str] = []
    if path == COMPOSITION_ROOT:
        return violations
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "build_tenant_migration_registry":
                    violations.append(f"{path}: imports build_tenant_migration_registry")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name == "build_tenant_migration_registry":
                violations.append(f"{path}: calls build_tenant_migration_registry")
    return violations


def _test_unit_escape_violations_in(path: str, source: str) -> list[str]:
    """R-E12: no production module may switch the test-unit gate off."""

    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "allow_test_units":
            violations.append(f"{path}: passes allow_test_units")
    return violations


def test_only_the_composition_root_composes_a_tenant_migration_registry() -> None:
    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        # The package that defines the builder necessarily names it; the rule is
        # about *composing* a registry, which is a call or an import elsewhere.
        if str(path) == "src/haloflow/m01/provisioning/units.py":
            continue
        violations.extend(_migration_composition_violations_in(str(path), path.read_text()))

    assert violations == []


def test_no_production_module_allows_test_migration_units() -> None:
    """R-E12. A test-only unit cannot enter the production registry."""

    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        if str(path) == "src/haloflow/m01/provisioning/units.py":
            continue  # defines the parameter; does not pass it
        violations.extend(_test_unit_escape_violations_in(str(path), path.read_text()))

    assert violations == []


def test_the_test_unit_controls_fail_when_they_should() -> None:
    """Negative control: both checks above catch the thing they name."""

    app = "src/haloflow/modules/rogue/service.py"
    assert _migration_composition_violations_in(
        app, "from haloflow.m01.provisioning import build_tenant_migration_registry\n"
    )
    assert _migration_composition_violations_in(
        app, "registry = build_tenant_migration_registry({})\n"
    )
    assert _test_unit_escape_violations_in(
        app, "registry = build_tenant_migration_registry({}, allow_test_units=True)\n"
    )
    assert _migration_composition_violations_in(
        COMPOSITION_ROOT, "registry = build_tenant_migration_registry({})\n"
    ) == []


# --- no privileged module callback in the provisioning path ---------------
#
# Defense in depth, NOT proof of confinement. The review that asked for this
# check was right that it is a heuristic: it recognizes connection-taking
# `Protocol` methods, connection-taking `Callable` annotations, and constructor
# parameters named like a callback. A callback hidden behind an opaque alias, or
# a parameter named something innocuous and untyped, would still get past it.
# What it does buy is that the specific contract PR-2 removed cannot come back
# unnoticed, and that the obvious ways of reintroducing one are noisy.

PROVISIONING_ROOT = Path("src/haloflow/m01/provisioning")
CALLBACK_PARAMETER_NAMES = re.compile(
    r"(installer|hook|callback|plugin|extension)s?$", re.IGNORECASE
)


def _takes_a_connection(annotation: ast.expr | None) -> bool:
    """True when this annotation describes something *given* a connection.

    A `Callable[[AsyncConnection], ...]` parameter is a callback handed a live
    connection, which is the contract under prohibition. `ConnectionFactory =
    Callable[[], Awaitable[AsyncConnection]]` *returns* one, which is the
    mechanism this whole package is built on, so only the parameter positions of
    a callable are inspected.
    """

    if annotation is None:
        return False
    if isinstance(annotation, ast.Subscript):
        value = ast.unparse(annotation.value)
        if value.split(".")[-1] in {"Callable", "Coroutine", "Awaitable"}:
            arguments = annotation.slice
            if isinstance(arguments, ast.Tuple) and arguments.elts:
                parameters = arguments.elts[0]
                return "Connection" in ast.unparse(parameters)
            return False
        return any(
            _takes_a_connection(element)
            for element in (
                annotation.slice.elts if isinstance(annotation.slice, ast.Tuple)
                else [annotation.slice]
            )
        )
    return False


def _connection_callback_violations_in(path: str, source: str) -> list[str]:
    """No module-supplied callback may be handed a live database connection.

    PR-2 removed `TenantObjectInstaller`, which received the provisioner-role
    connection — a role that owns every tenant schema and can write the tenant
    registry — with nothing confining an installer to the schema it was handed.
    Per-tenant objects are contributed as migration units instead, and M02 will
    define whatever narrow mechanism its SECURITY DEFINER functions actually
    need. This check exists so that contract cannot quietly come back.
    """

    violations: list[str] = []
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_protocol = any(
                (isinstance(base, ast.Name) and base.id == "Protocol")
                or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
                for base in node.bases
            )
            if not is_protocol:
                continue
            for member in node.body:
                if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                arguments = [*member.args.args, *member.args.kwonlyargs, *member.args.posonlyargs]
                for argument in arguments:
                    annotation = ast.unparse(argument.annotation) if argument.annotation else ""
                    if "Connection" in annotation:
                        violations.append(
                            f"{path}: Protocol {node.name}.{member.name} takes a connection"
                        )

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "__init__":
            arguments = [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]
            for argument in arguments:
                if CALLBACK_PARAMETER_NAMES.search(argument.arg):
                    violations.append(f"{path}: __init__ accepts module callback {argument.arg!r}")
                elif _takes_a_connection(argument.annotation):
                    violations.append(
                        f"{path}: __init__ parameter {argument.arg!r} is a callback "
                        "annotated to receive a connection"
                    )

        # A callable type alias is the obvious way to launder the annotation above.
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if isinstance(target, ast.Name) and _takes_a_connection(value):
                    violations.append(
                        f"{path}: type alias {target.id!r} describes a callback "
                        "that receives a connection"
                    )
    return violations


def test_no_module_callback_receives_a_privileged_connection() -> None:
    """Requested in the PR-2 review disposition, 2026-09-01. Defense in depth."""

    violations: list[str] = []
    for path in PROVISIONING_ROOT.rglob("*.py"):
        violations.extend(_connection_callback_violations_in(str(path), path.read_text()))

    assert violations == []


def test_the_module_callback_control_fails_when_it_should() -> None:
    """Negative control: each half catches a way of reintroducing the contract."""

    path = "src/haloflow/m01/provisioning/provisioner.py"
    protocol_form = (
        "from typing import Protocol\n"
        "from psycopg import AsyncConnection\n"
        "class TenantObjectInstaller(Protocol):\n"
        "    def install(self, connection: AsyncConnection, *, schema_key: str) -> None: ...\n"
    )
    constructor_form = (
        "class TenantProvisioner:\n"
        "    def __init__(self, connect, runner, *, object_installers=()) -> None:\n"
        "        self._object_installers = object_installers\n"
    )
    # The evasions the review named: an innocuous parameter name, and an alias.
    annotated_form = (
        "from collections.abc import Awaitable, Callable\n"
        "from psycopg import AsyncConnection\n"
        "class TenantProvisioner:\n"
        "    def __init__(self, connect, *, operations: "
        "tuple[Callable[[AsyncConnection, str], Awaitable[None]], ...] = ()) -> None: ...\n"
    )
    alias_form = (
        "from collections.abc import Awaitable, Callable\n"
        "from psycopg import AsyncConnection\n"
        "ModuleOperation = Callable[[AsyncConnection, str], Awaitable[None]]\n"
    )

    assert _connection_callback_violations_in(path, protocol_form)
    assert _connection_callback_violations_in(path, constructor_form)
    assert _connection_callback_violations_in(path, annotated_form)
    assert _connection_callback_violations_in(path, alias_form)

    # ...and the legitimate factory, which RETURNS a connection, must not fire.
    factory_alias = (
        "from collections.abc import Awaitable, Callable\n"
        "from psycopg import AsyncConnection\n"
        "ConnectionFactory = Callable[[], Awaitable[AsyncConnection]]\n"
        "class TenantProvisioner:\n"
        "    def __init__(self, connect: ConnectionFactory) -> None: ...\n"
    )
    assert _connection_callback_violations_in(path, factory_alias) == []

    # ...and every shipped file passes.
    for shipped in PROVISIONING_ROOT.rglob("*.py"):
        assert _connection_callback_violations_in(str(shipped), shipped.read_text()) == []


# --- CP-2: M01 embeds no module execution role name (R-P1.2, R-P1B.1) ------


def _module_role_literals_in(source: str) -> set[str]:
    """Every `haloflow_*` string literal that is not part of the fixed vocabulary.

    Read as literals rather than by grep so a name inside a comment or a
    docstring does not trip the control -- the rule is about what the code
    *embeds*, not what it discusses.
    """

    from haloflow.m01.provisioning.roles import PROVISIONING_ROLES

    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value.startswith("haloflow_") and value not in PROVISIONING_ROLES:
                found.add(value)
    return found


def test_m01_embeds_no_module_execution_role_name() -> None:
    """R-P1.2 and R-P1B.1. The approved set arrives from composition, not from M01.

    A module role name compiled into M01 would make the allow-list decorative:
    the point of supplying it at composition time is that adding a module is a
    reviewable change in one file, and that M01 cannot grant a module something
    the composition root never approved. M01 also creates no execution role and
    no membership -- those are an Alembic revision's work, under the deploy
    identity (R-P1B.2).
    """

    embedded: dict[str, set[str]] = {}
    for path in list(Path("src/haloflow/m01/provisioning").rglob("*.py")) + [
        Path("src/haloflow/composition.py")
    ]:
        names = _module_role_literals_in(path.read_text())
        if names:
            embedded[str(path)] = names

    assert embedded == {}, f"module role names embedded in M01: {embedded}"


def test_the_embedded_role_control_fails_when_it_should() -> None:
    """Negative control: the check above catches the thing it names."""

    assert _module_role_literals_in('ROLE = "haloflow_m02_migrator"') == {
        "haloflow_m02_migrator"
    }
    # The fixed vocabulary is not a violation, and neither is an unrelated string.
    assert _module_role_literals_in('R = "haloflow_migrator"\nS = "tenant_abcdefgh"') == set()
