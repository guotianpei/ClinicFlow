import ast
import json
from pathlib import Path

M01_ROOT = Path("src/haloflow/m01")


def test_shared_schema_manifest_explicitly_prohibits_phi() -> None:
    manifest = json.loads((M01_ROOT / "manifests/shared_schema_classification.json").read_text())

    assert manifest["policy"]["phi_allowed"] is False
    assert set(manifest["tables"].values()) <= {
        "control-plane",
        "pseudonymous-id",
    }


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


def test_m01_pool_and_context_issuer_are_not_imported_by_application_modules() -> None:
    protected_imports = {
        "haloflow.m01.pool",
        "haloflow.m01.context.issue_tenant_context",
    }
    violations: list[str] = []
    for path in Path("src/haloflow").rglob("*.py"):
        if path.is_relative_to(M01_ROOT):
            continue
        source = path.read_text()
        if any(protected in source for protected in protected_imports):
            violations.append(str(path))

    assert violations == []


def test_runtime_role_has_no_global_audit_write_in_permissions_manifest() -> None:
    manifest = json.loads((M01_ROOT / "manifests/permissions.json").read_text())

    runtime = manifest["haloflow_runtime"]
    assert "shared.access_audit_log:insert,update,delete,truncate" in runtime["deny"]
