#!/usr/bin/env python3
"""Measure the B2 approved installed-state profile and (re)generate
src/haloflow/synthetic/installed_profile.py. SYNTHETIC ONLY: per-case PostgreSQL clusters.

One uninterrupted install on a fresh per-case cluster; at EVERY committed boundary (the
reservation, the start of each guarded tenant transaction = the state after the previous one
committed, each phase / staff sub-step `after_commit`, and `ready`) the owned-state view is
captured with `haloflow.synthetic.view.capture` and its normalised `profile_digest` recorded.
Run twice by default; the two traces must be identical (determinism), else nothing is written.

    python scripts/b2_measure_profile.py [--check]   # --check: compare with the pinned module

Requires HALOFLOW_SYNTHETIC_PG_BIN (PostgreSQL 17 binaries).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HALOFLOW = HERE.parent
sys.path[:0] = [str(HALOFLOW / "tests" / "synthetic"), str(HALOFLOW / "src")]

import b2_support as s  # noqa: E402

from haloflow.synthetic import guard, hooks, provision, view  # noqa: E402
from haloflow.synthetic.attest import admin_connect  # noqa: E402

TARGET = HALOFLOW / "src" / "haloflow" / "synthetic" / "installed_profile.py"
COMPLETION_POINTS = {
    "intent": "reserve.after_commit", "stamped": "phase.stamped.after_commit",
    "migrated": "phase.migrated.after_commit", "logins": "phase.logins.after_commit",
    "tenant": "phase.tenant.after_commit", "profile": "phase.profile.after_commit",
    "activated": "phase.activated.after_commit", "settings": "phase.settings.after_commit",
    "staff.t1": "phase.staff.t1.after_commit", "staff.t2": "phase.staff.t2.after_commit",
    "staff.t3a": "phase.staff.t3a.after_commit", "staff.t3b": "phase.staff.t3b.after_commit",
    "ready": "ready",
}


_FAKE_ORDER = ("intent", "stamped", "migrated", "logins", "tenant", "profile", "activated",
               "settings", "staff.t1", "staff.t2", "staff.t3a", "staff.t3b", "ready")


def _measurement_mode() -> None:
    """MEASUREMENT ONLY (this script's process): the profile being generated cannot validate
    the run that measures it, so reconciliation is replaced by a capture that reports the
    expected index of an uninterrupted install (fake consecutive indices). Never used by src."""
    order = {name: i for i, name in enumerate(_FAKE_ORDER)}
    provision.COMPLETION = order  # type: ignore[misc]
    calls = [0]

    def reconcile(ctx: Any, conn: Any) -> tuple[int, Any]:
        cap = view.capture(conn, ctx.state, provision.GROUP_ROLES,
                           provision.intent_verifier_digests(ctx.state))
        calls[0] += 1
        if calls[0] == 1:
            return 0, cap
        phases = provision.SEGMENT_PHASES
        nxt = phases[phases.index(str(ctx.state["phase"])) + 1]
        return provision._completion(nxt), cap

    provision._reconcile = reconcile  # type: ignore[assignment]


def trace(tmp: Path) -> list[tuple[str, str, dict[str, frozenset[str]]]]:
    points: list[tuple[str, str, dict[str, frozenset[str]]]] = []
    with s.cluster(tmp) as c:
        st_dir = s.state_dir(tmp)
        env = s.environ_for(c, st_dir)
        endpoint = provision.parse_admin_dsn(env["HALOFLOW_SYNTHETIC_ADMIN_DSN"], env)

        def snap(label: str) -> None:
            state = json.loads((st_dir / "state.json").read_text())
            with admin_connect(endpoint, endpoint.target) as conn:
                cap = view.capture(conn, state, provision.GROUP_ROLES,
                                   provision.intent_verifier_digests(state))
            names = view.identity_names(cap.identities, state)
            by_name: dict[str, set[str]] = {}
            for c, o, _n in cap.identities:
                by_name.setdefault(names[f"{c}|{o}"], set()).add(o)
            points.append((label, cap.profile_digest,
                           {k: frozenset(v) for k, v in by_name.items()}))

        def hook(point: str, info: dict[str, Any]) -> None:
            if point == "guarded_txn":
                snap(f"guarded_txn#{info['n']}")
            elif point == "reserve.after_commit" or point.endswith(".after_commit"):
                snap(point)

        guard._TXN_COUNT[0] = 0
        hooks.FAULT = hook
        _measurement_mode()
        try:
            assert provision.init(env, attest_flag=True).phase == "ready"
        finally:
            hooks.FAULT = None
        snap("ready")
    return points


def replaced(points: list[tuple[str, str, dict[str, frozenset[str]]]]) -> dict[int, list[str]]:
    """Per transition i (point i-1 -> i): owned objects whose identity was dropped or got a new
    OID (an object replaced by a later unit). Only these may leave a sealed identity set."""
    out: dict[int, list[str]] = {}
    for i in range(1, len(points)):
        before, after = points[i - 1][2], points[i][2]
        gone = sorted(n for n, oids in before.items() if not oids <= after.get(n, frozenset()))
        if gone:
            out[i] = gone
    return out


def render(points: list[tuple[str, str, dict[str, frozenset[str]]]]) -> str:
    labels = [p for p, _, _ in points]
    completion = {k: labels.index(v) for k, v in COMPLETION_POINTS.items()}
    import hashlib

    profile_id = hashlib.sha256(json.dumps([d for _, d, _ in points]).encode()).hexdigest()
    moves = replaced(points)
    lines = [
        '"""GENERATED by scripts/b2_measure_profile.py -- do not edit. SYNTHETIC ONLY.',
        "",
        "The approved installed-state profile of a synthetic install (Option B B2 S1/S2): the",
        "normalised owned-state digest (`view.capture(...).profile_digest`) of EVERY legitimate",
        "committed state of one install, in order, and the index of each phase's completion",
        "point. Pinned to PostgreSQL 17, the synthetic registry (target 94) and Alembic head 009;",
        "control B2-P1 re-measures and must equal this module.",
        '"""',
        "",
        "# ruff: noqa: E501",
        "from typing import Final",
        "",
        f'PROFILE_ID: Final = "{profile_id}"',
        "POINTS: Final[tuple[str, ...]] = (",
    ]
    lines += [f'    "{d}",' for _, d, _ in points]
    lines += [")", "LABELS: Final[tuple[str, ...]] = ("]
    lines += [f'    "{label}",' for label, _, _ in points]
    lines += [")", "# transition i (point i-1 -> i): owned identities dropped or re-created",
              "REPLACED: Final[dict[int, tuple[str, ...]]] = {"]
    for i, names in moves.items():
        lines.append(f"    {i}: (")
        lines += [f"        {json.dumps(n)}," for n in names]
        lines.append("    ),")
    lines += ["}"]
    lines += ["COMPLETION: Final[dict[str, int]] = {"]
    lines += [f'    "{k}": {v},' for k, v in completion.items()]
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    first = trace(Path(tempfile.mkdtemp(prefix="b2prof-a-")))
    second = trace(Path(tempfile.mkdtemp(prefix="b2prof-b-")))
    if [(a, b) for a, b, _ in first] != [(a, b) for a, b, _ in second] \
            or replaced(first) != replaced(second):
        diff = [i for i, (a, b) in enumerate(zip(first, second, strict=False)) if a[:2] != b[:2]]
        print(f"NOT DETERMINISTIC: {len(first)} vs {len(second)} points, first diff {diff[:5]}")
        ra, rb = replaced(first), replaced(second)
        for i in sorted(set(ra) | set(rb)):
            if ra.get(i) != rb.get(i):
                print("  replaced differs at", i, ra.get(i), rb.get(i))
        return 1
    text = render(first)
    if args.check:
        current = TARGET.read_text() if TARGET.exists() else ""
        print("profile pin", "MATCHES" if current == text else "DIFFERS")
        return 0 if current == text else 1
    TARGET.write_text(text)
    print(f"wrote {TARGET} ({len(first)} points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
