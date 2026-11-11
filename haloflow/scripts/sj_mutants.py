"""L-1S package S-J mutation controls, group family ``sj`` (J-M1..J-M11).

Predeclared from the S-J red packet v1 §6 map (Work Session 2026-11-10; Codex ALIGN of the
map direction 20261110T225403Z, RED GATE PASS 20261110T231155Z). Three groups, one target
and one test module each (the shared part-A checker binds a mutant to exactly one module):

- ``sched-units``: WritebackScheduler defects judged by the unit rows.
- ``sched-pg``: WritebackScheduler defects judged by the combined-lifespan PostgreSQL rows.
- ``harness-pg``: TEST-ONLY harness orchestration defects (shutdown order, staff-route
  capability) judged by the PostgreSQL rows. Codex pin 3: each mutates the real
  orchestration seam, never an assertion, oracle or recorder.

Kill sets are MEASURED (scripts/sj_mutant_kills.json): every node a mutant may fail; a
mutant is killed only if it fails and every failure is in its set (the part-A rule). A
survivor is reported, never silently dropped, and no S-I defence is bypassed to obtain a
kill (Codex 20261110T225403Z on J-M7).

Usage (from haloflow/):
    python3 -I scripts/sj_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sj-mutation"
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
SCHED = Path("src/haloflow/m05/writeback_scheduler.py")
HARNESS = Path("tests/m05/sj_harness.py")
UNIT_TESTS = "tests/m05/test_sj_units.py"
PG_TESTS = "tests/m05/test_sj_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sj_mutant_kills.json"

_RUN_ONE = '                outcome = await self._worker.run_one(op_context, row["wb_op_id"])\n'

# (id, description, target, old, new); kill sets in sj_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    # -- WritebackScheduler, unit rows --------------------------------------------------
    ("J-M1", "run_one reuses the pass context", SCHED, _RUN_ONE,
     '                outcome = await self._worker.run_one(context, row["wb_op_id"])\n'),
    ("J-M2", "a manual tick is allowed while RUNNING", SCHED,
     "        if self._state is SchedulerState.RUNNING:\n"
     '            raise WritebackSchedulerRefused("LOOP_OWNS_TICKS")\n', ""),
    ("J-M3", "a pass-context failure stops the tenant loop", SCHED,
     "            return TenantReport(tenant, DEGRADED, _failure_class(error), "
     "_DISCOVER_NOT_RUN,\n                                _RUN_NOT_RUN)\n",
     "            raise\n"),
    ("J-M4", "batch_limit is ignored on the run phase", SCHED,
     "        for row in rows[:self._limit]:\n", "        for row in rows:\n"),
    ("J-M5", "stop() waits without a timeout", SCHED,
     "        done, _ = await asyncio.wait({task}, timeout=self._timeout)\n",
     "        done, _ = await asyncio.wait({task})\n"),
    ("J-M9", "a per-op context failure aborts the pass", SCHED,
     '                counts["context_failed"] += 1\n                continue\n',
     '                counts["context_failed"] += 1\n                break\n'),
    ("J-M10", "a discover failure skips the run phase", SCHED,
     "        run = await self._run_phase(tenant, context)\n",
     "        run = (await self._run_phase(tenant, context) if discover.status == OK\n"
     "               else _RUN_NOT_RUN)\n"),
    ("J-M11", "an op failure's exception text is logged", SCHED,
     "            except Exception:  # noqa: BLE001\n"
     '                counts["failed"] += 1\n',
     "            except Exception as error:  # noqa: BLE001\n"
     '                LOGGER.info("writeback op failed %s", error)\n'
     '                counts["failed"] += 1\n'),
    # -- WritebackScheduler, combined-lifespan rows -------------------------------------
    ("J-M7", "each due op is dispatched twice (a re-dispatch at the real seam)", SCHED,
     _RUN_ONE, _RUN_ONE + _RUN_ONE),
    # -- TEST-ONLY harness orchestration ------------------------------------------------
    ("J-M6a", "harness: shared resources close before the producers stop", HARNESS,
     "        failed: dict[str, str] = {}\n        for name in PRODUCERS:\n",
     "        failed: dict[str, str] = {}\n"
     "        if not self.closed:\n"
     "            self.port.close()\n"
     "            await self.runtime.close()\n"
     "            self.closed = True\n"
     "        for name in PRODUCERS:\n"),
    ("J-M6b", "harness: shared resources close even after a failed stop", HARNESS,
     "        if failed:\n            return failed\n", ""),
    ("J-M8", "harness: the test staff router ignores the capability header", HARNESS,
     '        caps = CAPS[request.headers.get(CAPS_HEADER, "resolve")]\n',
     '        caps = CAPS["resolve"]\n'),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("sched-units", SCHED, UNIT_TESTS),
    ("sched-pg", SCHED, PG_TESTS),
    ("harness-pg", HARNESS, PG_TESTS),
)
GROUP_OF: dict[str, str] = {
    **{m: "sched-units" for m in ("J-M1", "J-M2", "J-M3", "J-M4", "J-M5", "J-M9", "J-M10",
                                  "J-M11")},
    "J-M7": "sched-pg",
    **{m: "harness-pg" for m in ("J-M6a", "J-M6b", "J-M8")},
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sj-pycache-")))
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
                                                   f"title=sj-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
