"""Incoming fax routing FX-IN, package FX3a — statement, catalogue and ingress unit rows (RED BY
DESIGN). Governing: see `fx3a_support`. Classes: C control, A absence-red.

- FX3-C01 CONTROL: the FX2 catalogue builds; include_fx2 unchanged
- FX3-U01 FX3_STATEMENTS: exactly the five keys, their modes, capabilities and SQL (OPEN_KEY fixes
          the action to open on the SQL side; ACT_KEY needs m05.followup.apply; RECEIVE / LINES keys
          have their own capabilities)
- FX3-U02 include_fx3 catalogue resolves the five keys; include_fx3 without include_fx2 ->
          ValueError mentioning FX3; the default and eligibility catalogues have no fax key
- FX3-U03 FaxIngressConfig: a secret of 32..505 printable bytes; shorter / longer / non-printable
          / non-str -> FaxIngressConfigInvalid
- FX3-U04 FaxLineDirectory.build: lookup by canonical line; the same line under two tenants ->
          LineDirectoryInvalid LINE_DUPLICATE; a non-canonical line -> LineDirectoryInvalid
          LINE_INVALID; an unknown line -> None
- FX3-U05 InboundFax / FaxAction are frozen records; FaxAction.action outside move/file/sort ->
          ValueError
"""

from __future__ import annotations

import uuid
from typing import Any

import fx1_support as fx
import fx3a_support as f3
import pytest


def test_fx3_c01_fx2_catalogue() -> None:
    """C."""
    from haloflow.synthetic.composition import build_synthetic_catalog

    assert build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,
                                   include_fx2=True) is not None


def test_fx3_u01_statements() -> None:
    """A."""
    from haloflow.m01.statements import StatementMode

    st = f3.m("fax_statements")
    defs = dict(st.FX3_STATEMENTS)
    assert set(defs) == {st.RECEIVE_KEY, st.LINES_KEY, st.READ_KEY, st.OPEN_KEY, st.ACT_KEY}
    assert (st.RECEIVE_KEY, st.LINES_KEY, st.READ_KEY, st.OPEN_KEY, st.ACT_KEY) == (
        "m05.fax.receive", "m05.fax.lines", "m05.fax.read", "m05.fax.open", "m05.fax.act")
    assert defs[st.RECEIVE_KEY] == (StatementMode.WRITE, "m05.fax.ingress",
                                    "SELECT m05_fx_receive(%(command)s)")
    assert defs[st.LINES_KEY] == (StatementMode.READ, "m05.fax.lines",
                                  "SELECT m05_fx_lines_snapshot(%(command)s)")
    assert defs[st.READ_KEY] == (StatementMode.READ, "m05.followup.read",
                                 "SELECT m05_fx_staff_read(%(command)s)")
    assert defs[st.OPEN_KEY] == (
        StatementMode.WRITE, "m05.followup.read",
        "SELECT m05_fx_staff_action(%(command)s OPERATOR(pg_catalog.||)"
        " '{\"action\": \"open\"}'::jsonb)")
    assert defs[st.ACT_KEY] == (StatementMode.WRITE, "m05.followup.apply",
                                "SELECT m05_fx_staff_action(%(command)s)")


def _fax_keys(compiled: Any) -> set[str]:
    registered: tuple[str, ...] = compiled.catalog.keys()
    return {key for key in registered if key.startswith("m05.fax.")}


def test_fx3_u02_catalogue_chain() -> None:
    """A."""
    from haloflow.synthetic.composition import build_synthetic_catalog

    st = f3.m("fax_statements")
    compiled = f3.fx3_catalog()
    assert _fax_keys(compiled) == {st.RECEIVE_KEY, st.LINES_KEY, st.READ_KEY, st.OPEN_KEY,
                                   st.ACT_KEY}
    assert compiled.catalog.resolve(st.OPEN_KEY).required_capability == "m05.followup.read"
    with pytest.raises(ValueError, match="FX3"):
        build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,
                                include_fx3=True)  # type: ignore[call-arg]
    for other in (build_synthetic_catalog(), build_synthetic_catalog(**fx.eligibility_flags()),
                  build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,
                                          include_fx2=True)):
        assert _fax_keys(other) == set()


@pytest.mark.parametrize("secret", ["s" * 32, "s" * 505, "!" * 40])
def test_fx3_u03_config_ok(secret: str) -> None:
    """A."""
    ing = f3.m("fax_ingress")
    assert ing.FaxIngressConfig.build(secret) is not None


@pytest.mark.parametrize("secret", ["s" * 31, "s" * 506, "s" * 39 + " ", "s" * 39 + "é",
                                    None, 42])
def test_fx3_u03_config_refused(secret: Any) -> None:
    """A."""
    ing = f3.m("fax_ingress")
    with pytest.raises(ing.FaxIngressConfigInvalid):
        ing.FaxIngressConfig.build(secret)


def test_fx3_u04_directory() -> None:
    """A."""
    ing = f3.m("fax_ingress")
    directory = ing.FaxLineDirectory.build({"tenant-a": [fx.LINE1], "tenant-b": [fx.LINE2]})
    assert (directory.lookup(fx.LINE1), directory.lookup(fx.LINE2),
            directory.lookup("+15405559999")) == ("tenant-a", "tenant-b", None)
    with pytest.raises(ing.LineDirectoryInvalid) as dup:
        ing.FaxLineDirectory.build({"tenant-a": [fx.LINE1], "tenant-b": [fx.LINE1]})
    assert dup.value.code == "LINE_DUPLICATE"
    with pytest.raises(ing.LineDirectoryInvalid) as bad:
        ing.FaxLineDirectory.build({"tenant-a": ["5405550101"]})
    assert bad.value.code == "LINE_INVALID"


def test_fx3_u05_records() -> None:
    """A."""
    fax = f3.m("fax")
    record = fax.InboundFax(provider="simfax", provider_fax_id="FX-1", line=fx.LINE1,
                            sender=None, pages=1, received_at=fx.received_at(),
                            doc_ref="syn-doc-1")
    with pytest.raises(AttributeError):
        record.pages = 2  # type: ignore[misc]
    action = fax.FaxAction("move", uuid.uuid4(), 1, uuid.uuid4())
    with pytest.raises(AttributeError):
        action.action = "file"  # type: ignore[misc]
    with pytest.raises(ValueError):
        fax.FaxAction("open", uuid.uuid4(), 1, None)
