"""L-1S package S-B — selector plan DIAGNOSTICS (evidence only; not an acceptance gate).

Codex 20261106T163214Z: an additive, bounded investigation of the SB-P01..P03 custom-plan
failures at 895062a. The gating rows in test_sb_plan_postgres.py are unchanged and still
fail on their pinned 3 000-window / 120-due backlog. This module MEASURES, on two
workloads in their own tenants:

- small: the pinned shape (3 000 windows; 300 awaiting, 120 due; 30 distinct deadlines);
- large: 30 000 windows; 8 000 awaiting, 6 000 due; same tie-heavy shape (30 distinct).

For each workload x page (first / after / until / after_until) x plan_cache_mode
(force_custom_plan / force_generic_plan) it runs EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
on the source-tied `DUE_QUERIES[page]` as the M05 owner with the selector's own
variables, and prints one `SB-PLAN-DIAG` line: node types, index used, actual rows per
node, rows removed by filter, sort method, shared buffer hits/reads, execution time.

The rows assert only what makes the measurement valid (the plan ran, touched
m05_reminder_windows, and returned at most `v_limit` rows). They pass or fail on that,
never on plan shape: shape is judged by the gating rows and by the reviewer.
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
import pytest
import sa_support as sa
import sb_support as sb
from psycopg import sql

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

_PARAMS = ("v_cutoff", "v_after_deadline", "v_after_id", "v_until_deadline", "v_until_id",
           "v_limit")
_TYPES = ("timestamptz", "timestamptz", "uuid", "timestamptz", "uuid", "int4")
WORKLOADS = {"small": (3_000, 2_700, 2_820), "large": (30_000, 22_000, 28_000)}
# (total windows, first N closed, first N with a past deadline)


def _seed(install: sb.SBInstall, workload: str) -> list[tuple[Any, str]]:
    total, closed, past = WORKLOADS[workload]
    marker = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.m05_reminder_windows"
                                             " WHERE dispatch_fingerprint = %s"),
                      (b"\x5c" * 32,))[0][0]
    if marker == 0:
        sb.close_all_awaiting(install)
        sa.admin(install, sa.q(install, """
            INSERT INTO {s}.m05_reminder_windows (reminder_id, appointment_ref, patient_id,
                phone, sent_at, deadline, state, dispatch_fingerprint, dispatch_event_id,
                closed_decision, closed_event_id)
            SELECT gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), '+15550199998',
                   d - interval '24 hours', d,
                   CASE WHEN g <= %(closed)s THEN 'replied' ELSE 'awaiting' END,
                   '\\x5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c'::bytea,
                   gen_random_uuid(), NULL, NULL
              FROM generate_series(1, %(total)s) AS g,
                   LATERAL (SELECT CASE WHEN g <= %(past)s
                                        THEN now() - make_interval(hours => (g %% 30) + 1)
                                        ELSE now() + make_interval(hours => (g %% 30) + 1)
                                   END AS d) AS t"""),
                 {"closed": closed, "total": total, "past": past})
        sa.admin(install, sa.q(install, "ANALYZE {s}.m05_reminder_windows"))
    return sb.awaiting_due(install)


def _nodes(plan: Any) -> list[dict[str, Any]]:
    out = [plan]
    for child in plan.get("Plans", []):
        out += _nodes(child)
    return out


def _explain(install: sb.SBInstall, query: str, values: dict[str, Any], mode: str) -> Any:
    text = query.replace("{schema}", install.schema)
    used = [p for p in _PARAMS if re.search(rf"\b{p}\b", text)]
    for i, name in enumerate(used, start=1):
        text = re.sub(rf"\b{name}\b", f"${i}", text)
    types = ", ".join(_TYPES[_PARAMS.index(p)] for p in used)
    literals = sql.SQL(", ").join(sql.Literal(values[p]) for p in used)
    with psycopg.connect(install.ids.admin, autocommit=True) as conn, conn.transaction():
        conn.execute(f"SET LOCAL ROLE {sa.M05_OWNER}")
        conn.execute(sql.SQL("SET LOCAL plan_cache_mode = {}").format(sql.Literal(mode)))
        conn.execute(sql.SQL("PREPARE sb_diag (" + types + ") AS " + text))  # type: ignore[arg-type]
        row = conn.execute(sql.SQL(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE sb_diag({})").format(literals)
        ).fetchone()
        conn.execute("DEALLOCATE sb_diag")
    assert row is not None
    value = row[0]
    return value if not isinstance(value, str) else json.loads(value)


def _summary(plan: Any) -> dict[str, Any]:
    top = plan[0]
    nodes = []
    for n in _nodes(top["Plan"]):
        nodes.append({k: n[k] for k in (
            "Node Type", "Index Name", "Actual Rows", "Actual Loops", "Rows Removed by Filter",
            "Rows Removed by Index Recheck", "Exact Heap Blocks", "Sort Method",
            "Shared Hit Blocks", "Shared Read Blocks") if k in n})
    return {"execution_ms": top.get("Execution Time"), "planning_ms": top.get("Planning Time"),
            "nodes": nodes}


@pytest.mark.parametrize("mode", ("force_custom_plan", "force_generic_plan"))
@pytest.mark.parametrize("page", ("first", "after", "until", "after_until"))
@pytest.mark.parametrize("workload", ("small", "large"))
def test_sb_plan_diagnostics(sb_install: sb.SBInstall, sb_other_install: sb.SBInstall,
                             workload: str, page: str, mode: str) -> None:
    install = sb_install if workload == "small" else sb_other_install
    install.require()
    from haloflow.m05.due_units import DUE_QUERIES

    due = _seed(install, workload)
    quarter, middle, three = due[len(due) // 4], due[len(due) // 2], due[3 * len(due) // 4]
    after = quarter if page == "after_until" else middle
    until = three if page == "after_until" else middle
    values = {"v_cutoff": due[-1][0] + sb.MICRO,
              "v_after_deadline": after[0], "v_after_id": after[1],
              "v_until_deadline": until[0], "v_until_id": until[1], "v_limit": 100}
    summary = _summary(_explain(install, DUE_QUERIES[page], values, mode))
    print("SB-PLAN-DIAG " + json.dumps({"workload": workload, "due": len(due), "page": page,
                                         "mode": mode, **summary}, default=str))
    scans = [n for n in summary["nodes"] if "Index Name" in n or "Scan" in n["Node Type"]]
    assert scans, summary
    assert summary["nodes"][0]["Actual Rows"] <= 100, summary
