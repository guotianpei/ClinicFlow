"""L-1S package S-C mutation controls, group family ``sc_activation`` (SC1..SC13).

The ten mutation targets were predeclared in the S-C red package v1 (red gate PASS,
Codex 20261106T214654Z): probe skipped for a protected key; absent treated active;
unreadable treated active; probe after the statement; success cached across handles;
refusal not poisoning; concurrent call bypassing a pending probe; isolation unchecked;
production default NOT_ENFORCED; CancelledError translated. Some targets have two
mutants (handle and gateway side, or envelope and database-error side); the kill sets
are declared in scripts/sc_activation_mutant_kills.json before the CI run.

Judged by the shared part-A checker (scripts/m02_observation_mutants.py) exactly as
scripts/sb_due_mutants.py: each group's control run must pass completely; a mutant is
KILLED only by a completed run with at least one assertion-level failure, no
setup/teardown error, and every failed node inside its PREDECLARED kill set; the target
is restored after every run and its SHA-256 re-checked. Stale-bytecode guard as W4b.

Usage (from haloflow/):
    python3 -I scripts/sc_activation_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/sc-activation-mutation"
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
GATEWAY = Path("src/haloflow/m01/gateway.py")
STARTUP = Path("src/haloflow/startup.py")
UNITS = Path("src/haloflow/m05/activation_units.py")
STATIC_TESTS = "tests/m05/test_sc_static_units.py"
PG_TESTS = "tests/m05/test_sc_activation_postgres.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "sc_activation_mutant_kills.json"

# (id, description, target, old, new); kill sets in sc_activation_mutant_kills.json.
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("SC1", "the probe is skipped for one protected key (the S-B selector)", GATEWAY,
     "                and statement.key in self.__protected and not self.__authorized):\n",
     "                and statement.key in self.__protected - {\"m05.intake.due_windows\"}\n"
     "                and not self.__authorized):\n"),
    ("SC2", "an inactive (absent or revoked) answer is treated as active", GATEWAY,
     '    return None if value["active"] else "M05_NOT_ACTIVATED"\n',
     "    return None\n"),
    ("SC3", "an unreadable envelope is treated as active", GATEWAY,
     '            or type(value["active"]) is not bool):\n'
     '        return "M05_ACTIVATION_UNREADABLE"\n',
     '            or type(value["active"]) is not bool):\n'
     "        return None\n"),
    ("SC4", "a probe database error is treated as active", GATEWAY,
     '                self._refuse("M05_ACTIVATION_UNREADABLE")\n'
     "            code = _probe_outcome(rows)\n",
     "                self.__authorized = True\n"
     "                return\n"
     "            code = _probe_outcome(rows)\n"),
    ("SC5", "the probe runs after the statement (fetch_all)", GATEWAY,
     "        statement = await self._authorized_statement(statement_key)\n"
     "        cursor = await self._connection().execute(statement.query, params)\n"
     "        return await cursor.fetchall()\n",
     "        statement = self._resolve_statement(statement_key)\n"
     "        cursor = await self._connection().execute(statement.query, params)\n"
     "        await self._authorized_statement(statement_key)\n"
     "        return await cursor.fetchall()\n"),
    ("SC6", "a successful probe is cached across handles (process-wide)", GATEWAY,
     "            if self.__authorized:\n"
     "                return\n"
     "            try:\n"
     "                cursor = await self._connection().execute(PROBE_SQL)\n"
     "                rows = await cursor.fetchall()\n"
     "            except Exception:  # noqa: BLE001 - CancelledError is BaseException:"
     " not caught\n"
     "                self._refuse(\"M05_ACTIVATION_UNREADABLE\")\n"
     "            code = _probe_outcome(rows)\n"
     "            if code is not None:\n"
     "                self._refuse(code)\n"
     "            self.__authorized = True\n",
     "            if self.__authorized or globals().get(\"_SC6_CACHE\"):\n"
     "                self.__authorized = True\n"
     "                return\n"
     "            try:\n"
     "                cursor = await self._connection().execute(PROBE_SQL)\n"
     "                rows = await cursor.fetchall()\n"
     "            except Exception:  # noqa: BLE001 - CancelledError is BaseException:"
     " not caught\n"
     "                self._refuse(\"M05_ACTIVATION_UNREADABLE\")\n"
     "            code = _probe_outcome(rows)\n"
     "            if code is not None:\n"
     "                self._refuse(code)\n"
     "            self.__authorized = True\n"
     "            globals()[\"_SC6_CACHE\"] = True\n"),
    ("SC7", "a refusal does not poison the handle", GATEWAY,
     "        self.__refused = code\n"
     "        raise ActivationRefused(reason_code=code)\n",
     "        raise ActivationRefused(reason_code=code)\n"),
    ("SC8", "concurrent first calls do not share one probe (no lock)", GATEWAY,
     "        async with self.__probe_lock:\n",
     "        if True:\n"),
    ("SC9", "the transaction isolation is not checked", GATEWAY,
     '    if isolation != "read committed":\n',
     "    if False:\n"),
    ("SC10", "CancelledError during the probe is translated to UNREADABLE", GATEWAY,
     "            except Exception:  # noqa: BLE001 - CancelledError is BaseException:"
     " not caught\n",
     "            except BaseException:  # noqa: BLE001 - CancelledError is BaseException:"
     " not caught\n"),
    ("SC11", "a refusal the callback caught does not abort the transaction", GATEWAY,
     "                    if handle.activation_refused is not None:\n"
     "                        raise ActivationRefused(reason_code=handle.activation_refused)\n",
     ""),
    ("SC12", "production startup composes with NOT_ENFORCED", STARTUP,
     "        policy = (ActivationPolicy.ENFORCE if deployment is Deployment.PRODUCTION\n",
     "        policy = (ActivationPolicy.NOT_ENFORCED if deployment is Deployment.PRODUCTION\n"),
    ("SC13", "t046 treats a tenant with no activation record as active", UNITS,
     "'active', COALESCE(v_change OPERATOR(pg_catalog.=) 'grant', false));",
     "'active', COALESCE(v_change OPERATOR(pg_catalog.=) 'grant', true));"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("handle", GATEWAY, STATIC_TESTS),
    ("gateway", GATEWAY, PG_TESTS),
    ("startup", STARTUP, STATIC_TESTS),
    ("probe-unit", UNITS, PG_TESTS),
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
    os.environ.update(bytecode_env(tempfile.mkdtemp(prefix="sc-activation-pycache-")))
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
                                                   f"title=sc-activation-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
