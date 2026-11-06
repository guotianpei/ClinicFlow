"""L-1S package S-B — selector plan evidence (SB-P01..P03), own tenants.

Red-package delta D3: the plan is taken on the source-tied `due_units.DUE_QUERIES[page]`
(each appears exactly once in the t045 body, SB-S04) with the selector's PL/pgSQL
variables as typed parameters, as the M05 owner, under plan_cache_mode
force_custom_plan AND force_generic_plan, with EXPLAIN (ANALYZE).

Acceptance — AMENDED by owner decision (Rachel, 2026-11-06 ~12:54 PM ET, relayed by Codex
20261106T175452Z; investigation v2 sha256 281528da…): synthetic, fixture-specific TEST
acceptance only, not a universal examined-work bound or production assurance; the
medium-backlog (120..6 000 due) and production-table-state gaps are carried.
- Both pinned fixtures, both plan modes, all four page shapes: m05_reminder_windows is
  reached only through m05_reminder_windows_due (Index Scan, Index Only Scan, or Bitmap
  Index Scan + Bitmap Heap Scan), with an Index Cond on deadline and no Seq Scan.
- Large pinned fixture (30 000 windows; 8 000 awaiting; 6 000 due): every page is an
  ordered Index Scan / Index Only Scan (no bitmap, no Sort) whose scan emits <= L rows.
- Small pinned fixture (3 000 windows; 300 awaiting; 120 due; 30 distinct deadlines): a
  bitmap page may read the whole qualifying range.
Previously (architecture v1 section 5): Index Scan or Index Only Scan only.
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


FIXTURES = {"small": (3_000, 2_700, 2_820), "large": (30_000, 22_000, 28_000)}
# (total windows, first N closed, first N with a past deadline)


def _backlog(install: sb.SBInstall, fixture: str) -> list[tuple[Any, str]]:
    """The pinned synthetic backlog for `fixture`, built once per tenant."""

    total, closed, past = FIXTURES[fixture]
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
                   CASE WHEN g <= %(closed)s THEN 'replied' ELSE 'awaiting' END,
                   '\\x5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b'::bytea,
                   gen_random_uuid(), NULL, NULL
              FROM generate_series(1, %(total)s) AS g,
                   LATERAL (SELECT CASE WHEN g <= %(past)s
                                        THEN now() - make_interval(hours => (g %% 30) + 1)
                                        ELSE now() + make_interval(hours => (g %% 30) + 1)
                                   END AS d) AS t"""),
                 {"closed": closed, "total": total, "past": past})
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
        row = conn.execute(sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE sb_plan({})").format(
            literals)).fetchone()
        conn.execute("DEALLOCATE sb_plan")
    assert row is not None
    value = row[0]
    return value if not isinstance(value, str) else json.loads(value)


ORDERED = frozenset({"Index Scan", "Index Only Scan"})
INDEX_ACCESS = ORDERED | {"Bitmap Heap Scan"}


@pytest.mark.parametrize("mode", ("force_custom_plan", "force_generic_plan"))
@pytest.mark.parametrize("page", ("first", "after", "until", "after_until"))
@pytest.mark.parametrize("fixture", ("small", "large"))
def test_sb_p01_p03_index_plans(sb_install: sb.SBInstall, sb_other_install: sb.SBInstall,
                                fixture: str, page: str, mode: str) -> None:
    install = sb_install if fixture == "small" else sb_other_install
    install.require()
    from haloflow.m05.due_units import DUE_QUERIES

    due = _backlog(install, fixture)
    quarter, middle, three = due[len(due) // 4], due[len(due) // 2], due[3 * len(due) // 4]
    after = quarter if page == "after_until" else middle
    until = three if page == "after_until" else middle
    values = {"v_cutoff": due[-1][0] + sb.MICRO,
              "v_after_deadline": after[0], "v_after_id": after[1],
              "v_until_deadline": until[0], "v_until_id": until[1], "v_limit": 100}
    plan = _explain(install, DUE_QUERIES[page], values, mode)
    nodes = _plan_nodes(plan[0]["Plan"])
    scans = [n for n in nodes if n.get("Relation Name") == "m05_reminder_windows"]
    indexed = [n for n in nodes if "Index Name" in n]
    # every pinned fixture, mode and page shape: partial-index access only
    assert scans, nodes
    assert all(n["Node Type"] in INDEX_ACCESS for n in scans), scans
    assert indexed and all(n["Index Name"] == sb.INDEX_NAME for n in indexed), indexed
    assert any("deadline" in n.get("Index Cond", "") for n in indexed), indexed
    assert not [n for n in nodes if n["Node Type"] == "Seq Scan"], nodes
    if fixture == "large":
        # ordered traversal bounded by L on the large pinned fixture
        assert all(n["Node Type"] in ORDERED for n in scans), scans
        assert not [n for n in nodes if "Sort" in n["Node Type"]
                    or n["Node Type"].startswith("Bitmap")], nodes
        assert all(n["Actual Rows"] <= 100 for n in scans), scans
