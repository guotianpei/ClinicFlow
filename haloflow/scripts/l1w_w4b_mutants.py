"""L-1W W4b security mutation controls (pins v1 section 2, v1r1; red gate ACCEPT
9cca47b; Codex: "traced mutation controls and restored sources").

Three groups, judged by the shared part-A checker (scripts/m02_observation_mutants.py)
exactly as scripts/l1w_w4a_mutants.py: the control run must pass completely; a mutant
is KILLED only by a completed run whose every failure is in its PREDECLARED kill set;
the target is restored after every run and its SHA-256 re-checked.

- capabilities (``w4b_capabilities``): haloflow.platform.capabilities against the
  R-01 unit rows (a matrix row dropped, supervise added to act, admin added to read,
  act without read, a route requirement drifted, the display alternatives reordered,
  an unheld optional capability added).
- routes (``w4b_routes``): haloflow.platform.routes against the boundary unit rows
  (the standing/route check skipped, a body item_id accepted, unknown keys accepted
  so a body tenant key reaches the request, an unknown key reflected, the client
  correlation header trusted, the self-standing step asking for the route set).
- late_reader (W4B-MUT-1): the T3 reader revalidation in haloflow.m05.service
  against the genuine-bundle R-Q1 rows (PostgreSQL 17; measured in CI only, the
  local environment has no PostgreSQL 17). RT-TENANT (routes) is the compound
  tenant-from-body mutant; RT-NOCHECK remains the early HTTP decision control.
- http (``w4b_routes``): the closed status map in haloflow.platform.http against the
  boundary unit rows (a refusal turned into 200, NOT_AUTHORIZED as 409, an unlisted
  failure reason echoed, an M01 error as 500).

Scope (checked before measuring, per the 11-04 close note): every group runs ONE
unit module; none runs tests/platform as a whole (W2 does, and its kill sets name only
W2/W4a nodes, so the W4b modules add passing nodes to its control, not failures).

Kill sets were measured locally on the locked Python 3.12 venv on implementation head
dfba146 + this harness, and are confirmed or reported by the PG17 CI run; a CI
difference is reported, never edited away silently.

Usage (from haloflow/):
    python3 -I scripts/l1w_w4b_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/l1w-w4b-mutation"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CAPS = Path("src/haloflow/platform/capabilities.py")
ROUTES = Path("src/haloflow/platform/routes.py")
HTTP = Path("src/haloflow/platform/http.py")
M05_SERVICE = Path("src/haloflow/m05/service.py")
CAPS_TESTS = "tests/platform/test_l1w_w4b_capabilities_units.py"
ROUTES_PG_TESTS = "tests/m05/test_l1w_w4b_routes_postgres.py"
BOUNDARY_TESTS = "tests/platform/test_l1w_w4b_boundary_units.py"
TITLE_FROM = "m02-observation-mutants"
KILLS_FILE = HERE / "l1w_w4b_mutant_kills.json"

# (id, description, target, old, new); kill sets live in l1w_w4b_mutant_kills.json
EDITS: tuple[tuple[str, str, Path, str, str], ...] = (
    ("CP-DROPROW", "supervisor matrix row never granted", CAPS,
     '    if "supervisor" in standing.roles:\n        granted |= CAPABILITY_MATRIX["supervisor"]\n',
     ""),
    ("CP-SUPACT", "supervise added to the act row", CAPS,
     '    "act": frozenset({_PREPARE, _APPLY, _APPEND}),\n',
     '    "act": frozenset({_PREPARE, _APPLY, _APPEND, _SUPERVISE}),\n'),
    ("CP-ADMINREAD", "admin added to the read row", CAPS,
     '    "read": frozenset({_READ, _NOTE_READ, _DISPLAY}),\n',
     '    "read": frozenset({_READ, _NOTE_READ, _DISPLAY, _ADMIN}),\n'),
    ("CP-ACTNOREAD", "act granted without read", CAPS,
     '        if "act" in standing.access:\n            granted |= CAPABILITY_MATRIX["act"]\n',
     '    if "act" in standing.access:\n        granted |= CAPABILITY_MATRIX["act"]\n'),
    ("CP-DRIFT", "act route requirement drops apply", CAPS,
     '    "act": (frozenset({_PREPARE, _APPLY}),),\n',
     '    "act": (frozenset({_PREPARE}),),\n'),
    ("CP-DISPLAYORDER", "display alternatives reordered", CAPS,
     '    "display": (frozenset({_DISPLAY, _READ}), frozenset({_DISPLAY, _ADMIN})),\n',
     '    "display": (frozenset({_DISPLAY, _ADMIN}), frozenset({_DISPLAY, _READ})),\n'),
    ("CP-UNHELD", "optional capabilities added without being held", CAPS,
     "            return alternative | (ROUTE_OPTIONAL[route] & held)\n",
     "            return alternative | ROUTE_OPTIONAL[route]\n"),
    ("RT-NOCHECK", "standing/route check skipped (R-Q1 reader check)", ROUTES,
     "        requested = route_capabilities(route, held)\n        if requested is None:\n",
     "        requested = route_capabilities(route, held) or frozenset()\n        if False:\n"),
    ("RT-ITEMID", "body item_id accepted on act", ROUTES,
     '                     "assignee_id": False}\n',
     '                     "assignee_id": False, "item_id": False}\n'),
    ("RT-UNKNOWN", "undeclared body keys accepted (a body tenant key reaches the request)",
     ROUTES,
     "    if any(key not in declared for key in value):\n",
     "    if False:\n"),
    ("RT-ECHO", "unknown key reflected in the field label", ROUTES,
     '        raise _Malformed("unknown_field")\n',
     "        raise _Malformed(next(k for k in value if k not in declared))\n"),
    ("RT-CORR", "client correlation header trusted", ROUTES,
     "    ids = _Ids(uuid.uuid4(), uuid.uuid4())\n",
     '    ids = _Ids(uuid.uuid4(), uuid.UUID(request.headers["x-correlation-id"])'
     ' if "x-correlation-id" in request.headers else uuid.uuid4())\n'),
    # W4B-MUT-1 (Codex 20261105T134723Z): a compound parser + selection mutant, so a
    # body tenant key is stripped from the body AND becomes the admitted tenant.
    ("RT-TENANT", "tenant taken from the body (parser accepts it, selection uses it)",
     ROUTES,
     "        call = await parse(request, admitted)\n",
     '        raw = await request.body() if request.method == "POST" else b""\n'
     "        try:\n"
     "            sent = json.loads(raw) if raw else {}\n"
     "        except ValueError:\n"
     "            sent = {}\n"
     '        if type(sent) is dict and type(sent.get("tenant_id")) is str:\n'
     '            admitted = type(admitted)(tenant_id=sent.pop("tenant_id"),\n'
     "                                      staff_id=admitted.staff_id,\n"
     "                                      identity=admitted.identity)\n"
     "            request._body = json.dumps(sent).encode()\n"
     "        call = await parse(request, admitted)\n"),
    ("RT-STEP1", "self-standing step asks for more than its capability", ROUTES,
     "        own = frozenset({SELF_STANDING})\n",
     '        own = frozenset({SELF_STANDING, "m05.followup.read"})\n'),
    # W4B-MUT-1: the LATE reader revalidation (T3, inside the gateway transaction),
    # judged by the genuine-bundle R-Q1 rows on PostgreSQL 17 (CI only).
    ("LR-READ", "reader revalidation skipped on list/get/history", M05_SERVICE,
     '            await check_reader(handle, attribution["principal_id"])\n'
     "            return bind(await fetch_rows(handle, key, params, read_error))\n",
     "            return bind(await fetch_rows(handle, key, params, read_error))\n"),
    ("LR-NOTE", "reader revalidation skipped on read_note", M05_SERVICE,
     '            await check_reader(handle, attribution["principal_id"])\n'
     "            envelope = _bind_note(\n",
     "            envelope = _bind_note(\n"),
    ("HT-REFUSAL200", "domain refusal returned as 200", HTTP,
     '            return JSONResponse({"code": code}, status_code=409)\n',
     '            return JSONResponse({"code": code}, status_code=200)\n'),
    ("HT-NOTAUTH", "NOT_AUTHORIZED returned as 409", HTTP,
     '        if code == "NOT_AUTHORIZED":\n            return body("FORBIDDEN")\n',
     ""),
    ("HT-ECHOREASON", "unlisted failure reason echoed", HTTP,
     '        return body("UNAVAILABLE")\n    if isinstance(error, (ActionCommitUnknown',
     '        return JSONResponse({"code": str(reason)}, status_code=503)\n'
     "    if isinstance(error, (ActionCommitUnknown"),
    ("HT-M01", "M01 error mapped to 500", HTTP,
     "    if isinstance(error, (M01Error, TenantNotServed)):\n",
     "    if isinstance(error, TenantNotServed):\n"),
)

GROUPS: tuple[tuple[str, Path, str], ...] = (
    ("capabilities", CAPS, CAPS_TESTS),
    ("routes", ROUTES, BOUNDARY_TESTS),
    ("http", HTTP, BOUNDARY_TESTS),
    ("late_reader", M05_SERVICE, ROUTES_PG_TESTS),  # PostgreSQL 17 rows (CI)
)


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
    args = parser.parse_args(argv)
    evidence = Path(args.evidence)
    status = 0
    for label, target, tests in GROUPS:
        checker = _load_checker()
        checker.TARGET = target
        checker.TESTS = tests
        checker.MUTANTS = build_mutants(checker, target, tests)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(checker.main(["--pytest", args.pytest,
                                     "--evidence", str(evidence / label)]))
        sys.stdout.write(buffer.getvalue().replace(f"title={TITLE_FROM}",
                                                   f"title=l1w-w4b-mutants {label}"))
        status = status or code
    return status


if __name__ == "__main__":
    raise SystemExit(main())
