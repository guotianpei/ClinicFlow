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
