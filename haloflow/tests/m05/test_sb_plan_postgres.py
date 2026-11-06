"""L-1S package S-B — selector plan evidence (SB-P01..P03), own tenant.

Red-package delta D3: the plan is taken on the source-tied `due_units.DUE_QUERIES[page]`
(each appears exactly once in the t045 body, SB-S04) with the selector's PL/pgSQL
variables as typed parameters, as the M05 owner, under plan_cache_mode
force_custom_plan AND force_generic_plan. Acceptance (architecture v1 section 5): an Index
Scan or Index Only Scan using m05_reminder_windows_due, an Index Cond on deadline, and no
Seq Scan node — for the first, after-key and until-key pages on a representative,
tie-heavy backlog (3 000 windows; 2 700 closed; 300 awaiting, 120 due; 30 distinct
deadlines). No rows-examined metric is claimed.
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


# --- SB-P01..P03 ------------------------------------------------------------------------------


def _backlog(install: sb.SBInstall) -> list[tuple[Any, str]]:
    """Representative synthetic backlog, built once per module: 3 000 windows, 2 700
    closed, 300 awaiting of which 120 due, deadlines tie-heavy (30 distinct)."""

    marker = sa.admin(install, sa.q(install, "SELECT count(*) FROM {s}.m05_reminder_windows"
                                             " WHERE dispatch_fingerprint = %s"),
                      (b"\x5b" * 32,))[0][0]
    if marker == 0:
        sa.admin(install, sa.q(install, """
            INSERT INTO {s}.m05_reminder_windows (reminder_id, appointment_ref, patient_id,
                phone, sent_at, deadline, state, dispatch_fingerprint, dispatch_event_id,
                closed_decision, closed_event_id)
            SELECT gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), '+15550199999',
                   d - interval '24 hours', d,
                   CASE WHEN g <= 2700 THEN 'replied' ELSE 'awaiting' END,
                   '\\x5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b'::bytea,
                   gen_random_uuid(), NULL, NULL
              FROM generate_series(1, 3000) AS g,
                   LATERAL (SELECT CASE WHEN g <= 2820
                                        THEN now() - make_interval(hours => (g % 30) + 1)
                                        ELSE now() + make_interval(hours => (g % 30) + 1)
                                   END AS d) AS t"""))
        sa.admin(install, sa.q(install, "ANALYZE {s}.m05_reminder_windows"))
    return sb.awaiting_due(install)


def _plan_nodes(plan: Any) -> list[dict[str, Any]]:
    out = [plan]
    for child in plan.get("Plans", []):
        out += _plan_nodes(child)
    return out


_PARAMS = ("v_cutoff", "v_after_deadline", "v_after_id", "v_until_deadline", "v_until_id",
           "v_limit")
_TYPES = ("timestamptz", "timestamptz", "uuid", "timestamptz", "uuid", "int4")


def _explain(install: sb.SBInstall, query: str, values: dict[str, Any], mode: str) -> Any:
    """EXPLAIN the source-tied query with the selector's PL/pgSQL variables as typed
    parameters (the plan PL/pgSQL itself prepares), as the M05 owner."""

    text = query.replace("{schema}", install.schema)
    used = [p for p in _PARAMS if re.search(rf"\b{p}\b", text)]
    for i, name in enumerate(used, start=1):
        text = re.sub(rf"\b{name}\b", f"${i}", text)
    types = ", ".join(_TYPES[_PARAMS.index(p)] for p in used)
    args = [values[p] for p in used]
    with psycopg.connect(install.ids.admin, autocommit=True) as conn, conn.transaction():
        conn.execute(f"SET LOCAL ROLE {sa.M05_OWNER}")
        conn.execute(sql.SQL("SET LOCAL plan_cache_mode = {}").format(sql.Literal(mode)))
        conn.execute(sql.SQL("PREPARE sb_plan (" + types + ") AS " + text))  # type: ignore[arg-type]
        # A utility statement takes no bind parameters: the arguments go in as literals.
        literals = sql.SQL(", ").join(sql.Literal(a) for a in args)
        row = conn.execute(sql.SQL("EXPLAIN (FORMAT JSON) EXECUTE sb_plan({})").format(
            literals)).fetchone()
        conn.execute("DEALLOCATE sb_plan")
    assert row is not None
    value = row[0]
    return value if not isinstance(value, str) else json.loads(value)


@pytest.mark.parametrize("mode", ("force_custom_plan", "force_generic_plan"))
@pytest.mark.parametrize("page", ("first", "after", "until"))
def test_sb_p01_p03_index_plans(sb_install: sb.SBInstall, page: str, mode: str) -> None:
    sb_install.require()
    from haloflow.m05.due_units import DUE_QUERIES

    due = _backlog(sb_install)
    middle = due[len(due) // 2]
    values = {"v_cutoff": due[-1][0] + sb.MICRO,
              "v_after_deadline": middle[0], "v_after_id": middle[1],
              "v_until_deadline": middle[0], "v_until_id": middle[1], "v_limit": 100}
    plan = _explain(sb_install, DUE_QUERIES[page], values, mode)
    nodes = _plan_nodes(plan[0]["Plan"])
    scans = [n for n in nodes if n.get("Relation Name") == "m05_reminder_windows"]
    assert scans, nodes
    assert all(n["Node Type"] in {"Index Scan", "Index Only Scan"} for n in scans), scans
    assert all(n.get("Index Name") == sb.INDEX_NAME for n in scans), scans
    assert any("deadline" in n.get("Index Cond", "") for n in scans), scans
    assert not [n for n in nodes if n["Node Type"] == "Seq Scan"], nodes
