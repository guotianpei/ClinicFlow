"""E4 legacy child (TEST ONLY): run the GENUINE pre-E4 (3a8b884) synthetic installer in a fresh
process (Codex `codex_e4-red-v1-review.md` finding 1; the B3a e05bc51 pattern).

The installer package is frozen in `fixtures/el4_3a8b884_synthetic.zip`: the 32 files of
`src/haloflow/synthetic/` at merged E3b `3a8b884`, byte-identical. Every file is verified
against its pinned git blob id before use
(`git ls-tree -r 3a8b884 haloflow/src/haloflow/synthetic/`).
The frozen package is loaded AS `haloflow.synthetic`; every other module comes from the installed
tree. Its state file and database are therefore written by the pre-E4 WRITER, independent of the
E4 code under test, with the disposable cluster's own generated credentials (nothing committed).

argv[1] = JSON {zip, extract_to, alembic, environ, action in {"init", "status"}}
Prints exactly one `EL4LEGACY <json>` line: {"ok": true, "phase"|"status": ...} or the refusal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

PKG = "synthetic_3a8b884"
# git blob ids at 3a8b884
BLOBS = {
    "__init__.py": "e5e7c6ce2f813d741715d8f8f913e05f113947bb",
    "__main__.py": "40a75dc1ba3cf1b2feb2c2415d90a1bea41da437",
    "app.py": "a40eaca1624c6e3a88558c423834f65ff4f81e08",
    "attest.py": "0cd4229721b8e5f099f5424f76f0e07177e1e57d",
    "composition.py": "8ecf1dc536c64009200d1f39f6e87164fa700196",
    "display_statements.py": "dbf7682f6e4a7fb573f51605dd1dc10c7d59e4c5",
    "display_units.py": "a13314393588611bc790da6d6e643816c99ee5f9",
    "dsn.py": "caa40a7db12b3cf2f4d344f5d58abaaee511b4d9",
    "errors.py": "00de3d08d5f6b687be7dd77e7463d20df335ebbb",
    "gate.py": "1ef4d31085563239d6e8026a77d70d6ce698945c",
    "guard.py": "29b668cfe30ad813e0d3800465636ba1cd616a29",
    "hooks.py": "2fbdedb05034aa9c9219e1811b02bd13af729f12",
    "installed_profile.py": "8cffc61634efdd93361c495fbb8d189962782f35",
    "labels.py": "76f118649b426675783d798534ac543ed032f3e1",
    "lifecycle.py": "abc62c614c6914987b8738e8ca46cbf5f6656e46",
    "migrate.py": "1dc50345308792ce6abe3cf88191093c57c6706c",
    "provision.py": "4247579fe9f6ab83dded85d9dc6ecfcf429f8f28",
    "runtime.py": "3cf699e23b2e0a4a55136f63dcca7bcc69e65a60",
    "scenarios/S1.json": "1fef12eddaed5b2886d67e64b1b0309ca3870366",
    "scenarios/S2.json": "0b5c56ac58b08a39fc17014c7b8e13356bed9b74",
    "scenarios/S3.json": "0a8765ca5b2aa1671de833e689b684752babc8a4",
    "scenarios/S4.json": "5b31b69c87b03247bec83a9cca8d4669866c7c52",
    "scenarios/__init__.py": "e9ee9e73ee4aa97597cdede0fe7f19f0a66404e0",
    "seal_scope.py": "40a546b89a675a05503957126f3d48d4543cdb49",
    "sim_actions.py": "f2ddf85ab707baae7a533258752a301a173ce96f",
    "sim_ui.py": "bb1a237820b58e8ac6f67430bcbaa5f6e3838ba6",
    "simulator.py": "e768af3deb4e024b41a3f93a32e8f94a0c778ca5",
    "staff_routes.py": "f3cfefeecbf446145855c0daeb761279a0c75a0d",
    "state.py": "5d9117b5f604ca92f3cad2add8c041cab7757992",
    "ui.py": "4d937c9895a4bd596ce66fdf4f068b3eef88ae70",
    "view.py": "7c1d1bb7100f0563929b3ac738da4600670250c8",
    "wiring.py": "c91d8d9ca3605f35b0ddd115e8a375a1973f4e76",
}


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def extract_verified(zip_path: Path, dest: Path, alembic: Path) -> Path:
    """Extract into <dest>/src/haloflow/<pkg> with <dest>/alembic linked to the real migrations
    (the frozen migrate.py derives its script location as parents[3]/alembic)."""
    root = dest / "src" / "haloflow"
    root.mkdir(parents=True)
    (dest / "alembic").symlink_to(alembic, target_is_directory=True)
    pkg = root / PKG
    with zipfile.ZipFile(zip_path) as z:
        names = sorted(n.split("/", 1)[1] for n in z.namelist())
        if names != sorted(BLOBS):
            raise SystemExit(f"fixture file set differs: {names}")
        z.extractall(root)
    for name, blob in BLOBS.items():
        if git_blob_id((pkg / name).read_bytes()) != blob:
            raise SystemExit(f"fixture blob mismatch: {name}")
    return pkg


def load_as_haloflow_synthetic(pkg: Path) -> None:
    import haloflow

    spec = importlib.util.spec_from_file_location(
        "haloflow.synthetic", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["haloflow.synthetic"] = module
    spec.loader.exec_module(module)
    haloflow.synthetic = module  # type: ignore[attr-defined]


def main() -> None:
    cfg = json.loads(sys.argv[1])
    out: dict[str, Any]
    try:
        pkg = extract_verified(Path(cfg["zip"]), Path(cfg["extract_to"]), Path(cfg["alembic"]))
        load_as_haloflow_synthetic(pkg)
        provision = importlib.import_module("haloflow.synthetic.provision")
        assert Path(provision.__file__).resolve().parent == pkg.resolve()  # the frozen code
        if cfg["action"] == "init":
            out = {"ok": True, "phase": provision.init(cfg["environ"], attest_flag=True).phase}
        else:
            st = provision.status(cfg["environ"])
            out = {"ok": True, "status": {k: st[k] for k in ("phase", "sealed", "attested")}}
    except Exception as e:  # noqa: BLE001 - reported to the parent row
        out = {"ok": False, "type": type(e).__name__, "code": getattr(e, "code", None)}
    print("EL4LEGACY " + json.dumps(out, default=str), flush=True)


if __name__ == "__main__":
    main()
