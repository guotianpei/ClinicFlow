"""B3a legacy child (TEST ONLY): run the GENUINE e05bc51 synthetic installer in a fresh process.

The installer package is frozen in `fixtures/b2_e05bc51_synthetic.zip`: the 13 `.py` files of
`src/haloflow/synthetic/` at commit e05bc51 (accepted B2), byte-identical. Every file is
verified against its pinned git blob id before use (anyone with history can re-derive the ids:
`git ls-tree e05bc51 src/haloflow/synthetic/`). The frozen package is loaded AS
`haloflow.synthetic`; every other module comes from the installed tree (the F-R1 amendment
changes only `haloflow.synthetic`).

argv[1] = JSON {zip, extract_to, alembic, environ, action in {"init", "status"}}
Prints exactly one `B3LEGACY <json>` line: {"ok": true, "phase"|"status": ...} or the refusal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

# git blob ids at e05bc51 (`git ls-tree e05bc51 src/haloflow/synthetic/`)
E05BC51_BLOBS = {
    "__init__.py": "e5e7c6ce2f813d741715d8f8f913e05f113947bb",
    "__main__.py": "6e3a94a14967dbd79c25fad192e22ef71b15aa87",
    "attest.py": "f1e574995f6e4a66d1dc6c94aaf50e7db6eff0ba",
    "composition.py": "1a38b1741971fff644fb9e6a8b639d6981006b96",
    "dsn.py": "caa40a7db12b3cf2f4d344f5d58abaaee511b4d9",
    "errors.py": "e9bb89856496c08ebaf5c58dce37e6ddf4d49771",
    "guard.py": "7ce251e0138ca019827e194d195f3a968e24830c",
    "hooks.py": "1ef7dbc4172beb0c49cca2e874b36dac5a12cb72",
    "installed_profile.py": "ec3db745c96d21b4443bb8e77d89743f1c44ef21",
    "migrate.py": "1dc50345308792ce6abe3cf88191093c57c6706c",
    "provision.py": "864b43b97132e9f2aadfa065527daaa2880b7f57",
    "state.py": "5d9117b5f604ca92f3cad2add8c041cab7757992",
    "view.py": "bb0272802532f4a6d307a56a60fc21f31468218b",
}


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def extract_verified(zip_path: Path, dest: Path, alembic: Path) -> Path:
    """Extract into the repository's own layout, <dest>/src/haloflow/<pkg>, with <dest>/alembic
    linked to the real migrations: the frozen migrate.py derives its script location as
    parents[3]/alembic, so it runs byte-identical and unmodified."""
    root = dest / "src" / "haloflow"
    root.mkdir(parents=True)
    (dest / "alembic").symlink_to(alembic, target_is_directory=True)
    pkg = root / "synthetic_e05bc51"
    with zipfile.ZipFile(zip_path) as z:
        names = sorted(n.split("/", 1)[1] for n in z.namelist())
        if names != sorted(E05BC51_BLOBS):
            raise SystemExit(f"fixture file set differs: {names}")
        z.extractall(root)
    for name, blob in E05BC51_BLOBS.items():
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
    print("B3LEGACY " + json.dumps(out, default=str), flush=True)


if __name__ == "__main__":
    main()
