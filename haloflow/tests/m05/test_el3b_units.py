"""Eligibility E3b - unit rows, no database (RED BY DESIGN).

Every E3b import is deferred into the row body, so the intended red cause is
ModuleNotFoundError (`haloflow.m05.eligibility_staff`), ImportError (the new names in
`haloflow.m05.eligibility_labels`), TypeError (the `include_el3b` flag) or an assertion on
the route table. Rows marked CONTROL use existing code and pass now. Interface pins: the
el3b_support docstring and the pins documents it cites.

Rows:
- E3b-UC0 CONTROL: the E3a registry and catalogue build; target 121.
- E3b-U01 the six outcome labels (OD-EL6) and four contact-entry labels (OD-EL7),
  byte-exact; every label <= 64 characters; el_other is the only note-required code.
- E3b-U02 composition: include_el3b requires include_el3a (ValueError); the registry adds
  exactly t122..t125 (target 125); the catalogue key set is unchanged over E3a and the
  existing prepare / apply keys are re-bound to the v4 functions (correction C-1); the
  production registry and catalogue are unchanged.
- E3b-U03 service shapes: CaseToken / EligibilityResolveAction / LogContactAction fields
  exactly; StaffServiceV4 extends StaffServiceV3; frozen dataclasses.
- E3b-U04 synthetic routes: `case` GET and `contact` POST added; the six existing routes
  unchanged (F5).
- E3b-U05 no network import in `eligibility_staff`.
"""

from __future__ import annotations

import dataclasses
import pathlib
import uuid

import el3a_support as e3
import el3b_support as e4
import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "haloflow"


# --- E3b-UC0 CONTROL ----------------------------------------------------------------------

def test_el3b_uc0_control_e3a_composition_builds() -> None:
    from haloflow.composition import APPROVED_SUPPORTED_SCHEMA_VERSIONS

    assert e3.el3a_registry().target_version == e3.EL3A_TARGET
    e3.el3a_catalog()
    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS


# --- E3b-U01 labels -----------------------------------------------------------------------

def test_el3b_u01_outcome_and_contact_labels_are_byte_exact() -> None:
    from haloflow.m05.eligibility_labels import CONTACT_LABELS, OUTCOME_LABELS

    assert dict(OUTCOME_LABELS) == e4.OUTCOME_LABELS
    assert dict(CONTACT_LABELS) == e4.CONTACT_LABELS
    for label in (*OUTCOME_LABELS.values(), *CONTACT_LABELS.values()):
        assert len(label) <= 64
    assert not set(OUTCOME_LABELS) & set(CONTACT_LABELS)
    assert not set(OUTCOME_LABELS) & set(e4.W1_CODES)


# --- E3b-U02 composition ------------------------------------------------------------------

def test_el3b_u02_include_el3b_requires_include_el3a() -> None:
    from haloflow.synthetic.composition import (
        build_synthetic_catalog,
        build_synthetic_registry,
    )

    with pytest.raises(ValueError):
        build_synthetic_registry(include_el1=True, include_el2=True, include_el3b=True)
    with pytest.raises(ValueError):
        build_synthetic_catalog(include_el1=True, include_el2=True, include_el3b=True)


def test_el3b_u02_registry_adds_exactly_t122_to_t125() -> None:
    before = set(e3.el3a_registry().migration_ids)
    after = set(e4.el3b_registry().migration_ids)
    assert sorted(after - before) == list(e4.EL3B_UNITS)
    assert before <= after
    assert e4.el3b_registry().target_version == e4.EL3B_TARGET


def test_el3b_u02_catalogue_rebinds_prepare_and_apply_to_v4() -> None:
    before = e3.el3a_catalog().catalog
    after = e4.el3b_catalog().catalog
    assert set(after.keys()) == set(before.keys())
    for key, function in e4.REBOUND_KEYS.items():
        old, new = before.resolve(key), after.resolve(key)
        assert new.required_capability == old.required_capability
        assert new.mode == old.mode
        assert function in new.query and function not in old.query
        assert "_v3(" in old.query
    for key in set(after.keys()) - set(e4.REBOUND_KEYS):
        assert after.resolve(key).query == before.resolve(key).query, key


def test_el3b_u02_production_composition_unchanged() -> None:
    from haloflow.composition import APPROVED_SUPPORTED_SCHEMA_VERSIONS
    from haloflow.synthetic.composition import build_synthetic_registry

    assert frozenset({36}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    plain = build_synthetic_registry()
    assert not set(e4.EL3B_UNITS) & set(plain.migration_ids)


# --- E3b-U03 service shapes ---------------------------------------------------------------

@pytest.mark.parametrize(("name", "fields"), [
    ("CaseToken", ("case_id", "item_id", "case_version")),
    ("EligibilityResolveAction", ("item_id", "client_action_id", "expected_version", "type",
                                  "outcome_code", "note", "token")),
    ("LogContactAction", ("item_id", "client_action_id", "expected_version", "contact_code",
                          "token")),
])
def test_el3b_u03_action_shapes(name: str, fields: tuple[str, ...]) -> None:
    from haloflow.m05 import eligibility_staff as es

    cls = getattr(es, name)
    assert dataclasses.is_dataclass(cls)
    assert tuple(f.name for f in dataclasses.fields(cls)) == fields
    token = es.CaseToken(case_id=uuid.uuid4(), item_id=uuid.uuid4(), case_version=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.case_version = 2  # type: ignore[misc]


def test_el3b_u03_staff_service_v4_extends_v3() -> None:
    from haloflow.m05.eligibility_staff import StaffServiceV4
    from haloflow.m05.writeback import StaffServiceV3

    assert issubclass(StaffServiceV4, StaffServiceV3)
    assert callable(StaffServiceV4.case) and callable(StaffServiceV4.act)


# --- E3b-U04 synthetic routes (F5) --------------------------------------------------------

def test_el3b_u04_case_and_contact_routes_added_existing_unchanged() -> None:
    from haloflow.synthetic.staff_routes import PREFIX, ROUTES

    existing = {"list": ("GET", PREFIX), "get": ("GET", PREFIX + "/{item_id}"),
                "history": ("GET", PREFIX + "/{item_id}/history"),
                "reason": ("GET", PREFIX + "/{item_id}/reason"),
                "act": ("POST", PREFIX + "/{item_id}/actions"),
                "resolve": ("POST", PREFIX + "/{item_id}/resolve")}
    assert dict(ROUTES) == {**existing, "case": ("GET", PREFIX + "/{item_id}/case"),
                            "contact": ("POST", PREFIX + "/{item_id}/contact")}


def test_el3b_u04_production_resolve_fields_unchanged() -> None:
    from haloflow.platform.routes import RESOLVE_FIELDS

    assert "case_token" not in RESOLVE_FIELDS


# --- E3b-U05 ------------------------------------------------------------------------------

def test_el3b_u05_no_network_import() -> None:
    text = (SRC / "m05" / "eligibility_staff.py").read_text()
    for name in ("socket", "http.client", "urllib", "requests", "httpx", "aiohttp"):
        assert f"import {name}" not in text and f"from {name}" not in text


# --- E3b-U06 contact entitlement parity (Codex D-1 B2, 85e37979) --------------------------

def test_el3b_u06_contact_resolve_requirement_admits_exactly_the_act_route_actors() -> None:
    """The `contact` route uses the `resolve` requirement (it may append evidence for projected
    case events). Parity: over EVERY standing (status x access x roles), the `resolve`
    requirement is met exactly when the `act` requirement is met - the capability matrix grants
    prepare, apply and append together in one row - so no actor gains or loses the contact
    action through the choice. Item-level resolution authority stays the gateway's (standing,
    assignment, supervise), unchanged."""
    from itertools import product

    from haloflow.m01.staff.service import Standing
    from haloflow.platform.capabilities import capabilities_for, route_capabilities

    accesses = [frozenset(a) for a in ((), ("read",), ("act",), ("read", "act"))]
    roles = [frozenset(r) for r in ((), ("supervisor",), ("clinic_admin",),
                                    ("supervisor", "clinic_admin"))]
    checked = 0
    admitted: set[tuple[str, frozenset[str], frozenset[str]]] = set()
    for status, access, role in product(("active", "inactive"), accesses, roles):
        held = capabilities_for(Standing(status=status, access=tuple(sorted(access)),
                                         roles=tuple(sorted(role))))
        act = route_capabilities("act", held)
        res = route_capabilities("resolve", held)
        assert (act is None) == (res is None), (status, access, role)
        if res is not None:
            admitted.add((status, access, role))
        checked += 1
    assert checked == 32
    # non-vacuous: exactly the active members with read AND act are admitted (4 role sets)
    assert {(s, a) for s, a, _r in admitted} == {("active", frozenset({"read", "act"}))}
    assert len(admitted) == 4


# --- E3b-U07 the CASE_CHANGED case view is the canonical t121 shape (Codex green v1 review) --

def _view(item: uuid.UUID) -> dict[str, object]:
    return {"schema_version": 1, "item_id": str(item), "item_kind": "eligibility_inactive",
            "case_id": str(uuid.uuid4()), "case_version": 2, "case_state": "open",
            "class": "inactive", "reason": "coverage_inactive",
            "summary": {"payer_name": "Synthetic Payer", "copay": 25,
                        "checked_at": "2026-11-20T12:00:00.000000Z"},
            "notes": [{"seq": 1, "kind": "open_or_note", "class": "inactive",
                       "reason": "coverage_inactive", "at": "2026-11-20T12:00:00.000000Z"},
                      {"seq": 2, "kind": "note_only", "class": None,
                       "reason": "appointment_cancelled", "at": "2026-11-20T12:01:00.000000Z"}]}


def _changed(item: uuid.UUID, case: object) -> list[tuple[object]]:
    return [({"schema_version": 1, "decision": "CASE_CHANGED", "case": case,
              "evidence": []},)]


def _contact_action(item: uuid.UUID) -> object:
    from haloflow.m05.eligibility_staff import LogContactAction

    return LogContactAction(item_id=item, client_action_id=uuid.uuid4(), expected_version=1,
                            contact_code="el_left_message", token=None)


def _mutate(view: dict[str, object], how: str) -> dict[str, object]:
    bad = {**view}
    notes = [dict(n) for n in view["notes"]]  # type: ignore[attr-defined]
    if how == "extra_key":
        bad["coinsurance"] = 20
    elif how == "missing_key":
        del bad["reason"]
    elif how == "other_item":
        bad["item_id"] = str(uuid.uuid4())
    elif how == "summary_forbidden_field":
        bad["summary"] = {**view["summary"], "coinsurance": 20}  # type: ignore[dict-item]
    elif how == "summary_nested":
        bad["summary"] = {"plan": {"raw": "response"}}
    elif how == "note_extra_key":
        notes[0]["raw"] = "x"
        bad["notes"] = notes
    elif how == "note_bad_time":
        notes[1]["at"] = "yesterday"
        bad["notes"] = notes
    elif how == "bad_state":
        bad["case_state"] = "deleted"
    elif how == "version_text":
        bad["case_version"] = "2"
    return bad


@pytest.mark.parametrize("how", ["extra_key", "missing_key", "other_item",
                                 "summary_forbidden_field", "summary_nested",
                                 "note_extra_key", "note_bad_time", "bad_state",
                                 "version_text"])
def test_el3b_u07_case_changed_refuses_a_malformed_case_view(how: str) -> None:
    from haloflow.m05.eligibility_staff import _el_decision
    from haloflow.m05.service import ActionFailed

    item = uuid.uuid4()
    with pytest.raises(ActionFailed) as failed:
        _el_decision(_changed(item, _mutate(_view(item), how)), _contact_action(item),
                     str(uuid.uuid4()), second=False)  # type: ignore[arg-type]
    assert failed.value.reason_code == "ACTION_RESULT_INVALID"


def test_el3b_u07_case_changed_returns_exactly_the_canonical_view() -> None:
    from haloflow.m05.eligibility_staff import CASE_VIEW_KEYS, _el_decision

    item = uuid.uuid4()
    view = _view(item)
    decision, detail = _el_decision(_changed(item, view), _contact_action(item),
                                    str(uuid.uuid4()), second=False)  # type: ignore[arg-type]
    assert decision == "CASE_CHANGED" and detail["evidence"] == []
    assert detail["case"] == view and set(detail["case"]) == CASE_VIEW_KEYS
    assert detail["case"] is not view  # rebuilt, not forwarded
