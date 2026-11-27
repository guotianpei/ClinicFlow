"""Incoming fax routing FX-IN, package FX3b — fax staff screen UNIT rows (no database). RED BY
DESIGN. Governing: `fx3b_support.py` (interface pin, approved copy).

- FX3b-C01 CONTROL: the approved copy in the support module equals the owner records (OD-FX7,
           OD-FX12, OD-FX13), and the reason enum it covers is the installed one
- FX3b-U01 labels and copy: the module's REASON_LABELS / notices / fallbacks equal the approved
           text exactly; REASON_LABELS is total over m05.fax_units.REASONS
- FX3b-U02 From column (OD-FX10): saved, as sent, neither, withheld
- FX3b-U03 time: UTC -> clinic time "Nov 24, 5:12 PM" (DST on both sides; no leading zeros);
           unparseable -> "Unrecognized"
- FX3b-U04 conflict cases 0-4: exactly one state change since the form, the latest state row
           of the expected kind, summary agrees -> the named notice (actor name, queue, time)
- FX3b-U05 conflict case 5 (neutral): two changes since the form; a later `opened` row is
           ignored (and alone is never a change); summary disagrees with the row; system actor;
           unknown kind -> "Check the details below." with no name, queue or time
- FX3b-U06 escaping: names, queue names and sender text are HTML-escaped where rendered by the
           module's builders (`esc`), never double-escaped in the notice text
"""

from __future__ import annotations

from typing import Any

import fx3b_support as f
import pytest

ME = "11111111-1111-4111-8111-111111111111"
DANA = "22222222-2222-4222-8222-222222222222"
CHRIS = "33333333-3333-4333-8333-333333333333"
X = "aaaaaaaa-0000-4000-8000-000000000001"   # attempted queue
Y = "aaaaaaaa-0000-4000-8000-000000000002"   # actual queue
Z = "aaaaaaaa-0000-4000-8000-000000000003"   # original queue
NAMES = {DANA: "Dana Reyes", CHRIS: "Chris Ode", ME: "Demo Clinic Admin"}
QUEUES = {X: "Labs/Results", Y: "Imaging", Z: "General"}
T_RECEIVED = "2026-11-24T21:00:00.000000Z"
T_CHANGE = "2026-11-24T22:12:00.000000Z"    # 5:12 PM EST
T_OPEN = "2026-11-24T22:20:00.000000Z"
TIME = "Nov 24, 5:12 PM"


def _row(seq: int, action: str, actor: str | None, frm: str | None, to: str | None,
         at: str) -> dict[str, Any]:
    return {"seq": seq, "action": action, "actor": actor, "from_queue_id": frm,
            "to_queue_id": to, "at": at}


def _fax(state: str, queue: str | None, version: int) -> dict[str, Any]:
    return {"fax_id": "ffffffff-0000-4000-8000-000000000001", "state": state, "queue_id": queue,
            "version": version, "reason": None, "pages": 1, "received_at": T_RECEIVED,
            "sender_last4": "0101", "sender_display": {"text": None, "source": "none"}}


def _notice(attempt: str, attempted: str | None, expected: int, fax: dict[str, Any],
            rows: list[dict[str, Any]]) -> str:
    out: str = f.fax_ui().conflict_notice(attempt, attempted, expected, fax, rows, NAMES, QUEUES,
                                          f.CLINIC_TZ)
    return out


def test_fx3b_c01_copy_controls() -> None:
    """C."""
    from haloflow.m05 import fax_units

    assert tuple(f.REASON_LABELS) == fax_units.REASONS
    assert f.CONFLICT_LEAD.startswith("Nothing was changed.")
    assert set(f.CASES) == {0, 1, 2, 3, 4, 5}
    assert f.clinic_time(__import__("datetime").datetime.fromisoformat(
        T_CHANGE.replace("Z", "+00:00"))) == TIME


def test_fx3b_u01_labels_and_copy() -> None:
    """A."""
    from haloflow.m05 import fax_units

    ui = f.fax_ui()
    assert ui.UI_PREFIX == f.UI
    assert ui.REASON_LABELS == f.REASON_LABELS
    assert set(ui.REASON_LABELS) == set(fax_units.REASONS)
    assert (ui.ENTRY_LABEL, ui.SORTING_HEADING) == (f.ENTRY_LABEL, f.SORTING_HEADING)
    assert (ui.UNKNOWN_SENDER, ui.SENDER_HIDDEN) == (f.UNKNOWN_SENDER, f.SENDER_HIDDEN)
    assert (ui.SYSTEM_ACTOR, ui.UNKNOWN_STAFF) == (f.SYSTEM_ACTOR, f.UNKNOWN_STAFF)
    assert (ui.FILED_NOTICE, ui.CONFLICT_LEAD, ui.REFRESH_LABEL) == (
        f.FILED_NOTICE, f.CONFLICT_LEAD, f.REFRESH_LABEL)


@pytest.mark.parametrize(("display", "last4", "expected"), [
    ({"text": "Valley Radiology", "source": "saved"}, "0199", "Valley Radiology · ending 0199"),
    ({"text": "Valley Radiology •••0199", "source": "as_sent"}, "0199",
     "Valley Radiology •••0199 (as sent) · ending 0199"),
    ({"text": None, "source": "none"}, "0199", "Unknown sender · ending 0199"),
    ({"text": None, "source": "none"}, None, "Sender number not shown"),
    ({"text": "Valley Radiology", "source": "saved"}, None, "Valley Radiology"),
], ids=["saved", "as-sent", "neither", "withheld", "saved-no-number"])
def test_fx3b_u02_from_text(display: dict[str, Any], last4: str | None, expected: str) -> None:
    """A."""
    fax = _fax("routed", Y, 1) | {"sender_display": display, "sender_last4": last4}
    assert f.fax_ui().from_text(fax) == expected


@pytest.mark.parametrize(("at", "expected"), [
    (T_CHANGE, TIME),                                  # EST (UTC-5)
    ("2026-07-04T13:05:00.000000Z", "Jul 4, 9:05 AM"),  # EDT (UTC-4)
    ("2026-11-25T04:59:59.000000Z", "Nov 24, 11:59 PM"),
    ("2026-11-25T05:00:00.000000Z", "Nov 25, 12:00 AM"),
    ("2026-11-25T17:00:00.000000Z", "Nov 25, 12:00 PM"),
    ("not a time", "Unrecognized"),
])
def test_fx3b_u03_when_text(at: str, expected: str) -> None:
    """A."""
    assert f.fax_ui().when_text(at, f.CLINIC_TZ) == expected


def _received(queue: str | None = Z) -> dict[str, Any]:
    if queue is None:
        return _row(1, "received_needs_sorting", None, None, None, T_RECEIVED)
    return _row(1, "received_routed", None, None, queue, T_RECEIVED)


NAMED = [
    # case 0: A moves to X; Dana moved it to Y first; Chris opened it later (ignored)
    pytest.param("move", X, 1, _fax("routed", Y, 2),
                 [_received(), _row(2, "moved", DANA, Z, Y, T_CHANGE),
                  _row(3, "opened", CHRIS, None, None, T_OPEN)],
                 f.case_text(0, x="Labs/Results", y="Imaging", name="Dana Reyes", time=TIME),
                 id="case0-move-after-move-later-open"),
    # case 1: A moves to X; Dana filed it
    pytest.param("move", X, 1, _fax("filed", Z, 2),
                 [_received(), _row(2, "filed", DANA, Z, Z, T_CHANGE)],
                 f.case_text(1, x="Labs/Results", name="Dana Reyes", time=TIME),
                 id="case1-move-after-file"),
    # case 2: A files; Dana moved it to Y
    pytest.param("file", None, 1, _fax("routed", Y, 2),
                 [_received(), _row(2, "moved", DANA, Z, Y, T_CHANGE)],
                 f.case_text(2, y="Imaging", name="Dana Reyes", time=TIME),
                 id="case2-file-after-move"),
    # case 3: A files; Dana filed it
    pytest.param("file", None, 1, _fax("filed", Z, 2),
                 [_received(), _row(2, "filed", DANA, Z, Z, T_CHANGE)],
                 f.case_text(3, name="Dana Reyes", time=TIME),
                 id="case3-file-after-file"),
    # case 4: A sorts into X; Dana sorted it into Y
    pytest.param("sort", X, 1, _fax("routed", Y, 2),
                 [_received(None), _row(2, "sorted", DANA, None, Y, T_CHANGE)],
                 f.case_text(4, x="Labs/Results", y="Imaging", name="Dana Reyes", time=TIME),
                 id="case4-sort-after-sort"),
]


@pytest.mark.parametrize(("attempt", "attempted", "expected_version", "fax", "rows", "want"),
                         NAMED)
def test_fx3b_u04_named_cases(attempt: str, attempted: str | None, expected_version: int,
                              fax: dict[str, Any], rows: list[dict[str, Any]], want: str) -> None:
    """A."""
    assert _notice(attempt, attempted, expected_version, fax, rows) == want


NEUTRAL = [
    # Codex case 5: A sorts; Dana sorted into Y, then Chris moved it to X (two changes)
    pytest.param("sort", X, 1, _fax("routed", X, 3),
                 [_received(None), _row(2, "sorted", DANA, None, Y, T_CHANGE),
                  _row(3, "moved", CHRIS, Y, X, T_OPEN)], id="sort-then-move"),
    # A sorts; Dana sorted, then filed (two changes)
    pytest.param("sort", X, 1, _fax("filed", Y, 3),
                 [_received(None), _row(2, "sorted", DANA, None, Y, T_CHANGE),
                  _row(3, "filed", DANA, Y, Y, T_OPEN)], id="sort-then-file"),
    # A moves; two moves since the form
    pytest.param("move", X, 1, _fax("routed", Y, 3),
                 [_received(), _row(2, "moved", DANA, Z, X, T_CHANGE),
                  _row(3, "moved", CHRIS, X, Y, T_OPEN)], id="two-moves"),
    # the summary disagrees with the latest state row (queue)
    pytest.param("move", X, 1, _fax("routed", Z, 2),
                 [_received(), _row(2, "moved", DANA, Z, Y, T_CHANGE)], id="summary-disagrees"),
    # the summary disagrees with the latest state row (state)
    pytest.param("file", None, 1, _fax("routed", Z, 2),
                 [_received(), _row(2, "filed", DANA, Z, Z, T_CHANGE)], id="state-disagrees"),
    # only an `opened` row since the form: a refusal without a state change names no one
    pytest.param("move", X, 1, _fax("routed", Z, 1),
                 [_received(), _row(2, "opened", CHRIS, None, None, T_OPEN)], id="open-only"),
    # the change kind does not match the case (file attempt, latest row is a sort)
    pytest.param("file", None, 1, _fax("routed", Y, 2),
                 [_received(None), _row(2, "sorted", DANA, None, Y, T_CHANGE)],
                 id="kind-mismatch"),
    # a state row with no staff actor
    pytest.param("move", X, 1, _fax("routed", Y, 2),
                 [_received(), _row(2, "moved", None, Z, Y, T_CHANGE)], id="system-actor"),
]


@pytest.mark.parametrize(("attempt", "attempted", "expected_version", "fax", "rows"), NEUTRAL)
def test_fx3b_u05_neutral_case(attempt: str, attempted: str | None, expected_version: int,
                               fax: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """A."""
    got = _notice(attempt, attempted, expected_version, fax, rows)
    assert got == f.case_text(5)
    for leaked in ("Dana Reyes", "Chris Ode", "Imaging", "Labs/Results", "General", "PM", "AM"):
        assert leaked not in got, leaked


def test_fx3b_u05b_unnamed_actor_is_neutral() -> None:
    """A: a staff actor with no display name never yields a named notice."""
    fax = _fax("routed", Y, 2)
    rows = [_received(), _row(2, "moved", "44444444-4444-4444-8444-444444444444", Z, Y,
                              T_CHANGE)]
    assert _notice("move", X, 1, fax, rows) == f.case_text(5)


def test_fx3b_u06_escaping() -> None:
    """A."""
    ui = f.fax_ui()
    assert ui.esc('<b>"R&D" Fax</b>') == "&lt;b&gt;&quot;R&amp;D&quot; Fax&lt;/b&gt;"
    fax = _fax("routed", Y, 1) | {"sender_display": {"text": "A&B <Labs>", "source": "saved"}}
    assert ui.from_text(fax) == "A&B <Labs> · ending 0101"   # plain text; escaped once at render
    names = {DANA: "Dana <Reyes>"}
    got = ui.conflict_notice("move", X, 1, _fax("routed", Y, 2),
                             [_received(), _row(2, "moved", DANA, Z, Y, T_CHANGE)],
                             names, QUEUES, f.CLINIC_TZ)
    assert "Dana <Reyes>" in got and "&lt;" not in got
