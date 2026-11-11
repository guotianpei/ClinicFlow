"""Option B B1 mutation controls, group family ``b1`` (B1-M1..B1-M10).

Focused on the B1 SAFETY criteria (Codex Option B v1 review, Q5): the exact second
composition root, the transitive production fence, and the synthetic composition's
equality to the frozen pre-move oracle. Four groups, one target and one test module each
(the shared part-A checker binds a mutant to exactly one module):

- ``root``: the second-root allowance in the repository-control checker, judged by the
  B1-R rows beside it (the checker IS the guard; its rows are the oracle).
- ``fence``: the import-closure walker, judged by the B1-C1/C1f rows.
- ``compose``: `haloflow.synthetic.composition`, judged by the B1-E rows against the frozen
  manifest (never by the new builder).
- ``prod``: a production entry point that imports the synthetic package, judged by the
  B1-C1 (static) and B1-C1r (runtime) rows.

Kill sets are MEASURED (scripts/b1_mutant_kills.json): every node a mutant may fail; a
mutant is killed only if it fails and every failure is in its set (the part-A rule). A
survivor is reported, never silently dropped.

Usage (from haloflow/):
    python3 -I scripts/b1_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/b1-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONTROLS = Path("tests/m01/test_repository_controls.py")
FENCE = Path("tests/synthetic/fence.py")
COMPOSE = Path("src/haloflow/synthetic/composition.py")
PROD = Path("src/haloflow/platform/app.py")
CONTROL_TESTS = "tests/m01/test_repository_controls.py"
B1_TESTS = "tests/synthetic/test_b1_units.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "b1_mutant_kills.json"

# (id, description, target, old, new); kill sets in b1_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    # -- the second-root allowance ------------------------------------------------------
    ("B1-M1", "second root: the builder may be called from any function", CONTROLS,
     "            if not (callee and scope == function and isinstance(node.ctx, ast.Load)):\n",
     "            if not (callee and isinstance(node.ctx, ast.Load)):\n"),
    ("B1-M2", "second root: an aliased builder import is accepted", CONTROLS,
     "                        and alias.asname is None and node.level == 0):\n",
     "                        and node.level == 0):\n"),
    ("B1-M3", "second root: any synthetic module may compose a catalogue", CONTROLS,
     "    if path == SECOND_ROOT:\n"
     '        return _second_root_violations(path, source, "build_statement_catalog",\n',
     '    if path.startswith("src/haloflow/synthetic/"):\n'
     '        return _second_root_violations(path, source, "build_statement_catalog",\n'),
    # -- the production fence walker ----------------------------------------------------
    ("B1-M4", "fence: importer aliases are not resolved", FENCE,
     "                    importers.add(alias.asname or alias.name)\n",
     "                    pass\n"),
    ("B1-M5", "fence: escaped loader references are not reported", FENCE,
     "                node.id in importers or node.id in loaders):\n",
     "                False):\n"),
    ("B1-M6", "fence: sys.path / meta_path / path_hooks references are ignored", FENCE,
     "                    and node.attr in SYS_HOOKS):\n",
     "                    and node.attr in ()):\n"),
    # -- the synthetic composition ------------------------------------------------------
    ("B1-M7", "compose: the S-I ACL amendments are dropped", COMPOSE,
     "        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *SH_ACL_AMENDMENTS, *SH2_ACL_AMENDMENTS,\n"
     "                        *SI_ACL_AMENDMENTS),\n",
     "        acl_amendments=(*APPROVED_ACL_AMENDMENTS, *SH_ACL_AMENDMENTS,\n"
     "                        *SH2_ACL_AMENDMENTS),\n"),
    ("B1-M8", "compose: the S-I intake rows are marked active in production", COMPOSE,
     "'{ITEM_KIND_CHECK}', '{W_DISPATCH}', false)\"\n",
     "'{ITEM_KIND_CHECK}', '{W_DISPATCH}', true)\"\n"),
    ("B1-M9", "compose: the S-I statements leave the activation-protected set", COMPOSE,
     "    return h2 | frozenset(SH3_PROTECTED) | frozenset(SI_PROTECTED)\n",
     "    return h2 | frozenset(SH3_PROTECTED)\n"),
    # -- a production entry point reaching the synthetic package ------------------------
    ("B1-M10", "production: platform.app imports haloflow.synthetic", PROD,
     "from haloflow.platform.routes import install as install_routes\n",
     "from haloflow.platform.routes import install as install_routes\n"
     "import haloflow.synthetic.composition  # noqa: F401\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("root", CONTROLS, CONTROL_TESTS),
    ("fence", FENCE, B1_TESTS),
    ("compose", COMPOSE, B1_TESTS),
    ("prod", PROD, B1_TESTS),
)
GROUP_OF: dict[str, str] = {
    **{m: "root" for m in ("B1-M1", "B1-M2", "B1-M3")},
    **{m: "fence" for m in ("B1-M4", "B1-M5", "B1-M6")},
    **{m: "compose" for m in ("B1-M7", "B1-M8", "B1-M9")},
    "B1-M10": "prod",
}


def bytecode_env(empty_dir: str) -> dict[str, str]:
    """The W4B-CACHE-1 guard: compile the source on disk for every run."""

    return {"PYTHONPYCACHEPREFIX": empty_dir, "PYTHONDONTWRITEBYTECODE": "1"}


def kill_sets() -> dict[str, frozenset[str]]:
    if not KILLS_FILE.exists():
        return {}
    raw = json.loads(KILLS_FILE.read_text(encoding="utf-8"))
    return {mid: frozenset(nodes) for mid, nodes in raw.items()}


def specs() -> tuple[tuple[str, str, Path, str, str, frozenset[str]], ...]:
    kills = kill_sets()
    return tuple((*edit, kills.get(edit[0], frozenset())) for edit in EDITS)


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_m02_observation_mutants", HERE / "m02_observation_mutants.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def in_group(label: str, spec: tuple[Any, ...]) -> bool:
    return GROUP_OF.get(spec[0]) == label


def build_mutants(checker: Any, label: str) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(spec[0], spec[1], spec[3], spec[4], spec[5])
        for spec in specs()
        if in_group(label, spec)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--group", action="append", default=None,
                        help="run only the named group(s) (local measurement)")
    parser.add_argument("--mutant", action="append", default=None,
                        help="run only the named mutant(s) (local re-measurement)")
    args = parser.parse_args(argv)
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="b1-pycache-")))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        if args.group is not None and label not in args.group:
            continue
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = tuple(m for m in build_mutants(checker, label)
                                if args.mutant is None or m.id in args.mutant)
        if not checker.MUTANTS:
            continue
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=b1-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
