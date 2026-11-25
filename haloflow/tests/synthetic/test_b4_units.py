"""Option B B4: unit rows for the synthetic HTML staff screen (no database).

Class A unless stated (b4_support docstring): each row fails in its own body on the missing
`haloflow.synthetic.ui` until it exists.
"""

from __future__ import annotations

import ast
import urllib.parse
from pathlib import Path
from typing import Any

import b4_support as b4
import pytest

DEMO = Path(__file__).resolve().parents[2] / "demo" / "haloflow_demo"


def _demo_constant(module: str, name: str) -> Any:
    tree = ast.parse((DEMO / module).read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            assert node.value is not None
            return ast.literal_eval(node.value)
    raise AssertionError(f"{module}:{name} not found")


# =========================================================================== C: controls

def test_b4_u_lbl1_kind_labels_equal_the_approved_demonstrator_text() -> None:
    """C (v1r1 §5, U-LBL1): the pinned kind labels are byte-for-byte the approved sources:
    the demonstrator STATE_LABELS / INSTRUCTIONS["unclear"] (PR #43) and S-I C5(a)."""
    states = _demo_constant("service.py", "STATE_LABELS")
    instructions = _demo_constant("inbound.py", "INSTRUCTIONS")
    assert b4.KIND_LABELS["no_reply"] == states["NO_RESPONSE"]
    assert b4.KIND_LABELS["reschedule_request"] == states["RESCHEDULE_REQUESTED"]
    assert b4.KIND_LABELS["cancel_request"] == states["CANCEL_REQUESTED"]
    assert b4.KIND_LABELS["unclear_reply"] == instructions["unclear"]
    assert b4.KIND_LABELS["ehr_confirmation_check"] == "EHR confirmation needs checking"


def test_b4_u_rsn0_reason_codes_are_the_s_i_set() -> None:
    """C: the pinned reason labels cover exactly the production S-I reason codes."""
    from haloflow.m05.writeback import REASONS

    assert tuple(b4.REASON_LABELS) == REASONS


# =========================================================================== A: the module

def test_b4_u_lbl2_module_wording_is_exact() -> None:
    """A (R-B4-5, OD-B4-2, B-L1, W1): every label and message the module renders is the
    pinned approved text, with closed key sets."""
    ui = b4.ui()
    assert ui.BANNER == b4.BANNER
    # Eligibility E4 (el4_support pins; declared pin move): the three OD-EL11 titles are added;
    # the five B4 labels are unchanged.
    # Eligibility E5 (declared pin move, 2026-11-24): owner decision 4's doc-check title is added.
    el4 = {"eligibility_inactive", "eligibility_inconclusive", "eligibility_manual",
           "eligibility_doc_check"}
    assert {k: v for k, v in ui.KIND_LABELS.items() if k not in el4} == b4.KIND_LABELS
    assert ui.REASON_LABELS == b4.REASON_LABELS
    assert ui.OUTCOME_LABELS == b4.OUTCOME_LABELS
    assert frozenset({"other"}) == ui.NOTE_REQUIRED
    assert ui.MESSAGES == b4.MESSAGES
    assert ui.UNRECOGNIZED == "Unrecognized"


@pytest.mark.parametrize("value", ["<script>alert(1)</script>", "\"onload=x", "'x'", "a&b",
                                   "</td><td>", "<img src=x onerror=1>"])
def test_b4_u_r1_esc_neutralises_markup(value: str) -> None:
    """A (R-B4-7, B-UI6): esc() leaves no raw `<`, `>`, `"` or `'`, and round-trips."""
    import html

    out = b4.ui().esc(value)
    assert not set("<>\"'") & set(out), out
    assert html.unescape(out) == value


def test_b4_u_r1b_unknown_code_renders_unrecognized() -> None:
    """A (R-B4-5): a code outside a closed map renders the fixed text, never the code."""
    ui = b4.ui()
    assert ui.label(ui.KIND_LABELS, "<b>new_kind</b>") == "Unrecognized"
    assert ui.label(ui.KIND_LABELS, "no_reply") == ui.esc(b4.KIND_LABELS["no_reply"])


def test_b4_u_r3_staff_reported_is_the_w2_form() -> None:
    """A (W2): the resolution line is exactly 'Staff reported: <W1 label> — by <name>, <time>
    (not verified in EHR)', with escaping applied to the actor display."""
    ui = b4.ui()
    got = ui.staff_reported("cancelled_in_ehr", "Synthetic <Staff>", "2026-11-16 10:00")
    assert got == ("Staff reported: Cancelled in EHR — by Synthetic &lt;Staff&gt;,"
                   " 2026-11-16 10:00 (not verified in EHR)")
    assert ui.staff_reported("nope", "A", "t").startswith("Staff reported: Unrecognized —")


def test_b4_u_r4_order_page_groups_locally_and_stably() -> None:
    """A (R-B4-2, v1r1 F2.1): open-unassigned, open-assigned, in progress, resolved; stable by
    the service order within a group; nothing added or dropped."""
    rows = [
        {"item_id": "r1", "state": "resolved", "assignee_id": "s"},
        {"item_id": "p1", "state": "in_progress", "assignee_id": "s"},
        {"item_id": "a1", "state": "open", "assignee_id": "s"},
        {"item_id": "u1", "state": "open", "assignee_id": None},
        {"item_id": "u2", "state": "open", "assignee_id": None},
        {"item_id": "a2", "state": "open", "assignee_id": "s"},
    ]
    got = [r["item_id"] for r in b4.ui().order_page(rows)]
    assert got == ["u1", "u2", "a1", "a2", "p1", "r1"]


def test_b4_u_r6_form_contract_constants() -> None:
    """A (v1r1 F1.1/F1.2/F2.1, v1r2 C1/C2): caps, field sets and endpoint types."""
    ui = b4.ui()
    assert (ui.UI_FORM_MAX_BYTES, ui.UI_FORM_MAX_FIELDS, ui.UI_PAGE_SIZE) == (8192, 6, 100)
    assert {
        "claim": (frozenset({"csrf", "type", "client_action_id", "expected_version"}),
                  frozenset()),
        "resolve": (frozenset({"csrf", "type", "client_action_id", "expected_version",
                               "outcome_code"}), frozenset({"note"})),
    } == ui.FORM_FIELDS
    want_types = {"claim": frozenset({"claim"}),
                  "resolve": frozenset({"resolve", "claim_and_resolve"})}
    assert want_types == ui.ENDPOINT_TYPES


_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.mark.parametrize("target,matches", [
    (f"/synthetic/ui/items/{_ID}/claim", True),
    (f"/synthetic/ui/items/{_ID}/resolve", True),
    (f"/synthetic/ui/items/{_ID.upper()}/claim", False),
    (f"/synthetic/ui/items/{_ID}/claim/", False),
    (f"/synthetic/ui/items/{_ID}%2Fclaim", False),
    (f"/synthetic/ui/items/{_ID}/act", False),
    ("/synthetic/ui/items", False),
    ("/synthetic/ui/x", False),
    (f"/synthetic/v1/followup/items/{_ID}/resolve", False),
    (f"/x/synthetic/ui/items/{_ID}/claim", False),
])
def test_b4_u_frm3_registered_route_pattern_is_exact(target: str, matches: bool) -> None:
    """A (v1r1 F1.1; M63): the form exception's pattern is a full match of the two
    registered routes only."""
    assert (b4.ui().UI_FORM_ROUTE.fullmatch(target) is not None) is matches


def _allowed() -> frozenset[str]:
    req, opt = b4.ui().FORM_FIELDS["resolve"]
    return req | opt


def test_b4_u_frm1_worst_legal_body_fits() -> None:
    """A (v1r1 F1.2, U-FRM1): the largest legal resolve body (a 500-code-point note of 4-byte
    characters, fully percent-encoded, and max-length other values) parses and fits."""
    ui = b4.ui()
    body = urllib.parse.urlencode([
        ("csrf", "x" * 64), ("type", "claim_and_resolve"), ("client_action_id", _ID),
        ("expected_version", "2147483647"), ("outcome_code", "no_contact_left_message"),
        ("note", "\U0001F600" * 500)]).encode("ascii")
    assert len(body) <= ui.UI_FORM_MAX_BYTES, len(body)
    got = ui.parse_form(body, allowed=_allowed())
    assert got["note"] == "\U0001F600" * 500 and len(got) == 6


@pytest.mark.parametrize("raw,ok", [
    (b"a=1&b=2", True),
    (b"note=a+b", True),
    (b"note=a%20b", True),
    (b"note=%E2%80%94", True),
    (b"note=", True),
    (b"note=%", False),
    (b"note=%z1", False),
    (b"note=%1", False),
    (b"note=a b", False),
    (b"note=a;b", False),
    (b'note=a"b', False),
    (b"note=\x80", False),
    (b"a=1&&b=2", False),
    (b"&a=1", False),
    (b"a=1&", False),
    (b"a", False),
    (b"a=1&b", False),
    (b"note=%C3%28", False),
])
def test_b4_u_frm2_raw_validator_table(raw: bytes, ok: bool) -> None:
    """A (v1r2 C3, U-FRM2; M70): byte set, explicit percent triplets, pair structure, then
    strict decoding. A refusal is FormRefused with status 400."""
    ui = b4.ui()
    allowed = frozenset({"a", "b", "note"})
    if ok:
        ui.parse_form(raw, allowed=allowed)
    else:
        with pytest.raises(ui.FormRefused) as e:
            ui.parse_form(raw, allowed=allowed)
        assert e.value.status == 400


@pytest.mark.parametrize("raw", [b"note=1&note=2", b"type=a&type=b", b"zzz=1",
                                 b"a=1&b=1&note=1&type=1&csrf=1&client_action_id=1&x=1"])
def test_b4_u_frm2b_duplicate_unknown_and_count(raw: bytes) -> None:
    """A (v1r2 C4 steps 2-3; M68): a duplicate name, an unknown name or more than six fields
    is refused with 400."""
    ui = b4.ui()
    with pytest.raises(ui.FormRefused) as e:
        ui.parse_form(raw, allowed=_allowed() | {"a", "b"})
    assert e.value.status == 400
