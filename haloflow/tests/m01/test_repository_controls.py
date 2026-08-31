import ast
import hashlib
import json
from pathlib import Path

from haloflow.m01.statements import (
    M01_STATEMENTS,
    StatementMode,
    build_statement_catalog,
)

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
    """Names from the protected set that this source imports, in either form.

    The previous version of this check substring-matched the raw source for
    dotted strings such as "haloflow.m01.statements._build_statement_catalog".
    That form never appears in `from haloflow.m01.statements import
    _build_statement_catalog`, so the check silently missed the import style
    actually used, and could also fire on a mention inside a comment or string.
    """

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in PROTECTED_MODULES or any(
                    alias.name.startswith(f"{module}.") for module in PROTECTED_MODULES
                ):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in PROTECTED_MODULES or any(
                module.startswith(f"{protected}.") for protected in PROTECTED_MODULES
            ):
                found.append(module)
            for alias in node.names:
                if alias.name in PROTECTED_NAMES:
                    found.append(f"{module}.{alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in PROTECTED_NAMES:
            found.append(node.attr)
    return found


def test_m01_private_construction_paths_are_not_reachable_from_application_modules() -> None:
    """TC-D10. Scoped to the production boundary; tests keep their private imports."""

    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        if path.is_relative_to(M01_ROOT):
            continue
        for name in _protected_imports_in(path.read_text()):
            violations.append(f"{path}: {name}")

    assert violations == []


def test_protected_import_check_catches_both_import_forms() -> None:
    """TC-D9. Both `import x.y.z` and `from x.y import z`, including aliases."""

    dotted = "from haloflow.m01.statements import _build_statement_catalog\n"
    aliased = "from haloflow.m01.statements import _build_statement_catalog as b\n"
    module_import = "import haloflow.m01.pool\n"
    attribute = "import haloflow\nhaloflow.m01.context.issue_tenant_context()\n"

    assert _protected_imports_in(dotted)
    assert _protected_imports_in(aliased)
    assert _protected_imports_in(module_import)
    assert _protected_imports_in(attribute)


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


def test_statement_catalog_manifest_pins_the_composed_production_catalogue() -> None:
    """TC-D8. CI control for B2.10: keys and digests are pinned, not checked at runtime."""

    manifest = json.loads((M01_ROOT / "manifests/statement_catalog.json").read_text())
    compiled = build_statement_catalog(M01_STATEMENTS)

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


def test_derived_write_capabilities_match_the_catalogue_write_statements() -> None:
    """TC-D6, over the composed production catalogue."""

    compiled = build_statement_catalog(M01_STATEMENTS)

    assert compiled.write_capabilities == frozenset(
        statement.required_capability
        for statement in compiled.catalog.statements()
        if statement.mode is StatementMode.WRITE
    )


def test_resolver_contains_no_uuid_generation_path() -> None:
    """TC-B4. FR-031 puts generation at the entry point.

    The strongest available proof is that the capability is absent from the
    module: no uuid4/uuid1, no secrets, no os.urandom anywhere in resolver.py.
    """

    source = Path("src/haloflow/m01/resolver.py").read_text()
    tree = ast.parse(source)

    generators = {"uuid4", "uuid1", "uuid3", "urandom", "token_bytes", "token_hex"}
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name in generators:
                called.append(name)
        elif isinstance(node, ast.Import):
            called.extend(a.name for a in node.names if a.name in {"secrets", "random"})
        elif isinstance(node, ast.ImportFrom) and node.module in {"secrets", "random", "os"}:
            called.extend(a.name for a in node.names if a.name in generators)

    assert called == []


def test_classification_manifest_tracks_the_renamed_execution_id_column() -> None:
    """TC-A11. The manifest must move with migration 002, not lag behind it."""

    manifest = json.loads((M01_ROOT / "manifests/shared_schema_classification.json").read_text())

    renamed = {"tenant_state_history", "access_audit_log", "isolation_alerts"}
    for name in renamed:
        columns = manifest["tables"][name]["columns"]
        assert "execution_id" in columns, name
        assert "operation_id" not in columns, name
