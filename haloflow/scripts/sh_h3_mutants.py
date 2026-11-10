"""L-1S package S-H H3 mutation controls, group family ``sh_h3`` (X1..X19).

Predeclared from the H3 implementation checklist and mutation-to-assertion map v1 (Work
Session 2026-11-09, Codex ALIGN with pins) at source a271a62. Three groups, one test
module each (the shared checker binds a mutant to exactly one module):

- ``scheduler-units``: scheduler defects judged by the fast unit rows; kill sets
  MEASURED locally before the CI run (no database needed).
- ``scheduler-postgres``: X1p..X3p repeat X1..X3 against the PostgreSQL rows (the map's
  primary assertions, P04 etc.); kill sets PREDICTED with "may" nodes, measured in CI.
- ``sql``: t075 defects judged by the PostgreSQL rows; kill sets PREDICTED, measured in CI.

Disclosed: X4 replaces the obsolete batch-subtraction design (the runner owns its batch);
X5 removes BOTH per-tenant cancellation checks (either alone is redundant with the other);
X7 narrows the context handler so an unexpected failure escapes the tick; X10 narrows the
per-item handler; X17 removes the impossible-date handler: the canonical regex and the
round-trip check are mutually redundant for every tested input, so removing either alone
is an equivalent mutant and is not run. Judged by the shared part-A checker exactly as
sh_h2_mutants; a kill set lists every node the mutant may fail.

Usage (from haloflow/):
    python3 -I scripts/sh_h3_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sh-h3-mutation"
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
SCHED = Path("src/haloflow/m05/reminder_scheduler.py")
SQL = Path("src/haloflow/m05/reminder_scheduler_units.py")
UNIT_TESTS = "tests/m05/test_sh_h3_units.py"
PG_TESTS = "tests/m05/test_sh_h3_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sh_h3_mutant_kills.json"

_RECOVER_CALL = "            result = await self._worker.recover(context, op_id)\n"
_SKIP_EXPIRE = ("        expire = await self._expire_phase(context)\n",
                '        expire = PhaseReport(OK, {"expired": 0})\n')
_SKIP_RECOVERY = ("        recovery = await self._recovery_phase(tenant, context)\n",
                  "        recovery = PhaseReport(OK, _zero(RECOVERY_KEYS))\n")
_RECOVER_AS_READY = (_RECOVER_CALL,
                     "            result = await self._runner.run_once(context)\n")
_READY = "        ready = await self._ready_phase(context)\n"

# (id, description, target, old, new); kill sets in sh_h3_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    # -- scheduler, judged by the unit rows (kill sets measured before the CI run) --------
    ("X1", "the expire phase is skipped", SCHED, *_SKIP_EXPIRE),
    ("X2", "accepted recovery is skipped", SCHED, *_SKIP_RECOVERY),
    ("X3", "recovery runs a second ready pass instead of registration", SCHED,
     *_RECOVER_AS_READY),
    ("X4", "the ready phase is skipped when the recovery page was full (starvation)", SCHED,
     _READY,
     "        ready = (_READY_NOT_RUN if sum(recovery.counts.values()) >= self._limit\n"
     "                 else await self._ready_phase(context))\n"),
    ("X5", "no cancellation check before or after a tenant", SCHED,
     "                _cancel_pending()\n"
     "                reports.append(await self._one(tenant))\n"
     "                _cancel_pending()  # a collaborator that swallowed cancellation\n",
     "                reports.append(await self._one(tenant))\n"),
    ("X6", "lateness measured before the tick completes (catch-up burst)", SCHED,
     "            result = await self._run_tick()\n            end = self._clock()\n",
     "            pending = asyncio.ensure_future(self._run_tick())\n"
     "            end = self._clock()\n            result = await pending\n"),
    ("X7", "an unexpected context failure escapes the tenant and aborts the tick", SCHED,
     "        except Exception as error:  # noqa: BLE001 - classified by type only, no text kept\n",
     "        except (CapabilityDenied, TenantDenied) as error:\n"),
    ("X8", "exception text is logged", SCHED,
     "        except Exception as error:  # noqa: BLE001 - classified by type only, no text kept\n",
     "        except Exception as error:  # noqa: BLE001 - classified by type only, no text kept\n"
     '            LOGGER.warning("reminder_scheduler context failed: %s", error)\n'),
    ("X9", "stop() waits without cancelling", SCHED,
     "            self._cancel_sent = True\n            task.cancel()\n",
     "            self._cancel_sent = True\n"),
    ("X10", "a recovery item's exception is not isolated", SCHED,
     "        except Exception as error:  # noqa: BLE001\n            return _item_count(error)\n",
     "        except OutboundCommitUnknown as error:\n            return _item_count(error)\n"),
    ("X11", "the cursor is not advanced past a failed item", SCHED,
     "            counts[await self._recover_item(context, key[1])] += 1\n"
     "            self._cursors[tenant] = key  # past every row, whatever its outcome\n",
     "            outcome = await self._recover_item(context, key[1])\n"
     "            counts[outcome] += 1\n"
     "            if outcome in _BENIGN:\n"
     "                self._cursors[tenant] = key\n"),
    ("X12", "the ready phase is skipped when recovery is not ok", SCHED, _READY,
     "        ready = (_READY_NOT_RUN if recovery.status != OK\n"
     "                 else await self._ready_phase(context))\n"),
    ("X14", "no wrap on an empty page with a cursor set", SCHED,
     "            if not page and cursor is not None:  # bounded wrap: once, from the start\n",
     "            if False:  # bounded wrap removed\n"),
    ("X15", "failure counts left out of DEGRADED_ITEMS", SCHED,
     "        return PhaseReport(DEGRADED_ITEMS if failed else OK, counts)\n",
     "        return PhaseReport(OK, counts)\n"),
    ("X19", "STOP_FAILED releases ownership (restart allowed)", SCHED,
     "            self._state = SchedulerState.STOP_FAILED\n",
     "            self._task = None\n            self._state = SchedulerState.NEW\n"),
    # -- scheduler, judged by the PostgreSQL rows (the same defects, real collaborators) --
    ("X1p", "the expire phase is skipped (PostgreSQL rows)", SCHED, *_SKIP_EXPIRE),
    ("X2p", "accepted recovery is skipped (PostgreSQL rows)", SCHED, *_SKIP_RECOVERY),
    ("X3p", "recovery runs a second ready pass instead of registration (PostgreSQL rows)",
     SCHED, *_RECOVER_AS_READY),
    # -- t075 SQL, judged by the PostgreSQL rows ------------------------------------------
    ("X13", "t075 also selects accepted_unregistered", SQL,
     "             WHERE o.state = 'accepted'\n",
     "             WHERE o.state IN ('accepted', 'accepted_unregistered')\n"),
    ("X16", "t075 cursor is not strict on op_id (>=)", SQL,
     "                        AND o.op_id OPERATOR(pg_catalog.>) v_op))\n",
     "                        AND o.op_id OPERATOR(pg_catalog.>=) v_op))\n"),
    ("X17", "an impossible cursor date is not normalized (handler removed)", SQL,
     "        EXCEPTION WHEN data_exception THEN\n"
     "            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';\n"
     "        END;\n",
     "        END;\n"),
    ("X18", "t075 output tie order by op_id descending", SQL,
     "               ORDER BY p.updated_at, p.op_id), '[]'::jsonb)\n",
     "               ORDER BY p.updated_at, p.op_id DESC), '[]'::jsonb)\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("scheduler-units", SCHED, UNIT_TESTS),
    ("scheduler-postgres", SCHED, PG_TESTS),
    ("sql", SQL, PG_TESTS),
)


def bytecode_env(empty_dir: str) -> dict[str, str]:
    """The W4B-CACHE-1 guard: compile the source on disk for every run."""

    return {"PYTHONPYCACHEPREFIX": empty_dir, "PYTHONDONTWRITEBYTECODE": "1"}


def kill_sets() -> dict[str, frozenset[str]]:
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


def in_group(target: Path, tests: str, spec: tuple[Any, ...]) -> bool:
    return bool(spec[2] == target and spec[5]
                and all(n.startswith(tests + "::") for n in spec[5]))


def build_mutants(checker: Any, target: Path, tests: str) -> tuple[Any, ...]:
    return tuple(
        checker.Mutant(spec[0], spec[1], spec[3], spec[4], spec[5])
        for spec in specs()
        if in_group(target, tests, spec)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--group", action="append", default=None,
                        help="run only the named group(s) (local measurement)")
    args = parser.parse_args(argv)
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sh-h3-pycache-")))
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        if args.group is not None and label not in args.group:
            continue
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = build_mutants(checker, target, tests)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=sh-h3-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
