"""Incoming fax routing FX-IN, package FX2 — composition and provisioner-interface rows (RED BY
DESIGN). Governing: see `fx2_support`. Classes: C control, A absence-red.

- FX2-C01 CONTROL: the FX1 registry (target 140) is unchanged
- FX2-U01 include_fx2 registry: target 144; ids = FX1 ids + exactly FX2_IDS in order
- FX2-U02 include_fx2 without include_fx1 -> ValueError mentioning FX2; the catalogue builds
- FX2-U03 haloflow.synthetic.fax_lines: LineOp is a frozen record; LineProvisionRefused carries
          `code`; an op outside add/remove or a non-canonical line is refused before any database
          contact (`OP_INVALID` / `LINE_INVALID`, conninfo never opened)
"""

from __future__ import annotations

import fx1_support as fx
import fx2_support as f2
import pytest


def test_fx2_c01_fx1_registry_unchanged() -> None:
    """C."""
    registry = fx.fx1_registry()
    assert registry.target_version == fx.FX1_TARGET
    assert registry.migration_ids[-len(fx.FX1_IDS):] == fx.FX1_IDS


def test_fx2_u01_registry() -> None:
    """A."""
    base = fx.fx1_registry().migration_ids
    registry = f2.fx2_registry()
    assert registry.target_version == f2.FX2_TARGET
    assert registry.migration_ids == (*base, *f2.FX2_IDS)


def test_fx2_u02_chain_and_catalog() -> None:
    """A."""
    from haloflow.synthetic.composition import build_synthetic_catalog, build_synthetic_registry

    with pytest.raises(ValueError, match="FX2"):
        build_synthetic_registry(**fx.eligibility_flags(), include_fx2=True)  # type: ignore[call-arg]
    assert build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,
                                   include_fx2=True) is not None  # type: ignore[call-arg]


@pytest.mark.parametrize(("op", "line", "code"), [
    ("move", fx.LINE1, "OP_INVALID"),
    ("add", "5405550101", "LINE_INVALID"),
    ("remove", "+0123", "LINE_INVALID"),
])
def test_fx2_u03_provisioner_validates_before_connecting(op: str, line: str, code: str) -> None:
    """A: the conninfo is unusable, so any database contact would raise a different error."""
    from haloflow.synthetic import fax_lines

    record = fax_lines.LineOp(op=op, tenant_id="t", line=line)
    with pytest.raises(AttributeError):
        record.op = "add"  # type: ignore[misc]
    with pytest.raises(fax_lines.LineProvisionRefused) as refused:
        fax_lines.provision_lines("host=/nonexistent dbname=none connect_timeout=1", [record])
    assert refused.value.code == code
