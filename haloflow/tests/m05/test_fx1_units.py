"""Incoming fax routing FX-IN, package FX1 — pure and composition rows (RED BY DESIGN).

Governing: see `fx1_support`. Classes: C control (green on 4529d15), A absence-red.

Rows (test-case ids v1; trace in brackets):
- FX1-U01  canonical_e164 vectors; malformed -> ValueError(INVALID_NUMBER) [FR-1, arch 3.4]
- FX1-U02  decide(): L1 beats L2 beats L3; sender None skips L1-L2; distinct destinations;
           no match -> (None, {}) [FR-4, FR-5, FR-6, OD-FX5]
- FX1-U02b decide() discriminates rule count from destination count (two rules, one queue at
           the deciding level -> one destination) [FR-5; kills M-FX1-2 at the pure level]
- FX1-U03  meta_digest: fixed vector, 32 bytes, each field changes it, sender None != "" mapping
           is the only null mapping (sender is never "") [FR-7, arch 4]
- FX1-C01  CONTROL: the eligibility registry target is 135 and its ids end with t135
- FX1-C02  CONTROL: the default registry target is 97; profiles TARGETS unchanged
- FX1-A01  include_fx1 registry: target 139; ids = eligibility ids + exactly FX1_IDS in order
- FX1-A02  include_fx1 catalogue builds; include_fx1 without include_el5 -> ValueError
- FX1-A03  production registry and supported set {36} unchanged by FX1 (no fax unit leaks)
"""

from __future__ import annotations

import hashlib
from typing import Any

import fx1_support as fx
import pytest

# --- pure: fax_rules ------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "canonical"), [
    ("(540) 555-0101", "+15405550101"),
    ("540-555-0101", "+15405550101"),
    ("5405550101", "+15405550101"),
    ("15405550101", "+15405550101"),
    ("+1 540 555 0101", "+15405550101"),
    ("+44 20 7946 0018", "+442079460018"),
])
def test_fx1_u01_canonical_e164(raw: str, canonical: str) -> None:
    """A: fax_rules does not exist on the baseline."""
    from haloflow.m05 import fax_rules

    assert fax_rules.canonical_e164(raw) == canonical


@pytest.mark.parametrize("raw", ["", "abc", "555-0101", "+0123456789", "12345678901234567",
                                 "+1 540 555 01O1", "25405550101", "+"])
def test_fx1_u01b_canonical_e164_refuses(raw: str) -> None:
    """A."""
    from haloflow.m05 import fax_rules

    with pytest.raises(ValueError, match="INVALID_NUMBER"):
        fax_rules.canonical_e164(raw)


def _rules(*specs: tuple[str | None, str | None, str]) -> list[Any]:
    from haloflow.m05 import fax_rules

    return [fax_rules.Rule(sender=s, line=line, queue_id=q) for s, line, q in specs]


S, L = fx.SENDER, fx.LINE1


@pytest.mark.parametrize(("specs", "sender", "level", "queues"), [
    # L1 beats L2 beats L3
    (((S, L, "labs"), (S, None, "imaging"), (None, L, "general")), S, 1, {"labs"}),
    (((S, None, "imaging"), (None, L, "general")), S, 2, {"imaging"}),
    (((None, L, "general"),), S, 3, {"general"}),
    # a rule for another line or sender never matches
    (((S, fx.LINE2, "labs"), (fx.SENDER2, None, "imaging")), S, None, set()),
    # sender hidden: L1 and L2 skipped, L3 still applies (OD-FX5)
    (((S, L, "labs"), (S, None, "imaging"), (None, L, "general")), None, 3, {"general"}),
    (((S, L, "labs"),), None, None, set()),
    # conflict at the deciding level: two distinct destinations
    (((S, None, "labs"), (S, None, "imaging"), (None, L, "general")), S, 2,
     {"labs", "imaging"}),
    # a conflict only at a LOWER level is irrelevant
    (((S, None, "labs"), (None, L, "general"), (None, L, "imaging")), S, 2, {"labs"}),
    # no rules
    ((), S, None, set()),
], ids=["l1", "l2", "l3", "other", "hidden-l3", "hidden-none", "conflict", "lower-conflict",
        "empty"])
def test_fx1_u02_decide(specs: tuple[tuple[str | None, str | None, str], ...],
                        sender: str | None, level: int | None, queues: set[str]) -> None:
    """A."""
    from haloflow.m05 import fax_rules

    decision = fax_rules.decide(_rules(*specs), sender=sender, line=L)
    assert (decision.level, set(decision.queue_ids)) == (level, queues)


def test_fx1_u02b_decide_counts_destinations_not_rules() -> None:
    """A: two rules at the deciding level pointing to ONE queue -> one destination (route once).
    A row-counting implementation would report a conflict (M-FX1-2)."""
    from haloflow.m05 import fax_rules

    decision = fax_rules.decide(_rules((S, None, "labs"), (S, None, "labs")), sender=S, line=L)
    assert (decision.level, set(decision.queue_ids)) == (2, {"labs"})


def test_fx1_u02c_level_of() -> None:
    """A."""
    from haloflow.m05 import fax_rules

    assert [fax_rules.level_of(r) for r in _rules((S, L, "q"), (S, None, "q"), (None, L, "q"))
            ] == [1, 2, 3]


def _expected_digest(line: str, sender: str | None, pages: int, received: str, doc: str) -> bytes:
    parts = ["m05.fx.meta/v1", line, sender or "", str(pages), received, doc]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()


def test_fx1_u03_meta_digest() -> None:
    """A: the digest is the pinned construction; every immutable field changes it."""
    from haloflow.m05 import fax_rules

    base = {"line": L, "sender": S, "pages": 2, "received_at": "2026-11-25T13:00:00.000000Z",
            "doc_ref": "syn-doc-0001"}
    digest = fax_rules.meta_digest(**base)
    assert len(digest) == 32
    assert digest == _expected_digest(L, S, 2, base["received_at"], "syn-doc-0001")
    assert fax_rules.META_TAG == "m05.fx.meta/v1"
    changed = {"line": fx.LINE2, "sender": None, "pages": 3,
               "received_at": "2026-11-25T13:00:00.000001Z", "doc_ref": "syn-doc-0002"}
    for field, value in changed.items():
        assert fax_rules.meta_digest(**{**base, field: value}) != digest, field


# --- composition ----------------------------------------------------------------------------


def test_fx1_c01_eligibility_target_unchanged() -> None:
    """C."""
    from haloflow.synthetic.composition import build_synthetic_registry

    registry = build_synthetic_registry(**fx.eligibility_flags())
    assert registry.target_version == fx.ELIGIBILITY_TARGET
    assert registry.migration_ids[-1].startswith("t135_")


def test_fx1_c02_default_target_and_profiles_unchanged() -> None:
    """C."""
    from haloflow.synthetic import profiles
    from haloflow.synthetic.composition import build_synthetic_registry

    assert build_synthetic_registry().target_version == 97
    # FX3a adds the fax profile (144); the default and eligibility targets stay unchanged
    assert (profiles.TARGETS["default"], profiles.TARGETS["eligibility"]) == (97, 135)


def test_fx1_a01_fx1_registry() -> None:
    """A: build_synthetic_registry has no include_fx1 on the baseline (TypeError)."""
    from haloflow.synthetic.composition import build_synthetic_registry

    base = build_synthetic_registry(**fx.eligibility_flags()).migration_ids
    registry = fx.fx1_registry()
    assert registry.target_version == fx.FX1_TARGET
    assert registry.migration_ids == (*base, *fx.FX1_IDS)


def test_fx1_a02_catalog_and_chain() -> None:
    """A."""
    from haloflow.synthetic.composition import build_synthetic_registry

    assert fx.fx1_catalog() is not None
    flags = {**fx.eligibility_flags(), "include_el5": False}
    with pytest.raises(ValueError, match="FX1"):
        build_synthetic_registry(**flags, include_fx1=True)  # type: ignore[call-arg]


def test_fx1_a03_production_unchanged() -> None:
    """A (its first assertion is a control; the import of fax_rules is the absence)."""
    from haloflow import composition
    from haloflow.m05 import fax_rules

    assert frozenset({36}) == composition.APPROVED_SUPPORTED_SCHEMA_VERSIONS
    production_ids = composition.build_production_tenant_migrations().migration_ids
    assert not [m for m in production_ids if "_fx_" in m]
    assert isinstance(fax_rules.K_FX_LINES, int)
