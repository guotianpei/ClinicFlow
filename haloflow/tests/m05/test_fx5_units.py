"""Incoming fax routing FX-IN, package FX5 — unit rows (RED BY DESIGN).
Governing: see `fx5_support`.
Classes: C control, A absence-red.

- FX5-C01 CONTROL: the FX4 registry (149); the historical FX4 catalogue READ_KEY (t149, read
          capability); a read-only holder resolves the follow-up `list` requirement (unchanged)
- FX5-U01 platform: ROUTE_REQUIREMENTS["fax_read"] == ({read, prepare, apply},); an explicit EMPTY
          ROUTE_OPTIONAL["fax_read"]; route_capabilities("fax_read", {read}) is None, with
          {read, prepare, apply} (and + supervise) exactly {read, prepare, apply}; list / get /
          history / act requirements and optionals unchanged (follow-up routes, R3)
- FX5-U02 staff routes: list / get / history / open / queues use "fax_read"; act stays "act"
- FX5-U03 registry ids + target 153; include_fx5 chains to include_fx4 ("FX5" in the error)
- FX5-U04 catalogue: READ / OPEN / ACT / QUEUES rebound to the v3 / v2 functions with capability
          m05.followup.apply and their modes; OPEN keeps the constant right-hand open; RECEIVE /
          LINES unchanged; FX4 catalogue unchanged; include_fx5 needs include_fx4
"""

from __future__ import annotations

import fx1_support as fx
import fx3a_support as f3
import fx4_support as f4
import fx5_support as f5
import pytest

READ = "m05.followup.read"
PREPARE = "m05.followup.prepare"
APPLY = "m05.followup.apply"
SUPERVISE = "m05.followup.supervise"


def test_fx5_c01_controls() -> None:
    """C."""
    from haloflow.platform.capabilities import route_capabilities

    assert f4.fx4_registry().target_version == 149
    # the historical FX4 composition (not the live route table, which U02 re-pins)
    st = f3.m("fax_statements")
    read = f4.fx4_catalog().catalog.resolve(st.READ_KEY)
    assert (read.query, read.required_capability) == (
        "SELECT m05_fx_staff_read_v2(%(command)s)", READ)
    assert route_capabilities("list", frozenset({READ})) == frozenset({READ})


def test_fx5_u01_fax_read_requirement() -> None:
    """A."""
    from haloflow.platform import capabilities as cap

    full = frozenset({READ, PREPARE, APPLY})
    assert cap.ROUTE_REQUIREMENTS["fax_read"] == (full,)
    assert "fax_read" in cap.ROUTE_OPTIONAL and cap.ROUTE_OPTIONAL["fax_read"] == frozenset()
    assert cap.route_capabilities("fax_read", frozenset({READ})) is None
    assert cap.route_capabilities("fax_read", frozenset({PREPARE, APPLY})) is None
    assert cap.route_capabilities("fax_read", full) == full
    assert cap.route_capabilities("fax_read", full | {SUPERVISE}) == full
    # the follow-up routes keep today's shape (R3)
    assert cap.ROUTE_REQUIREMENTS["list"] == (frozenset({READ}),)
    assert cap.ROUTE_REQUIREMENTS["get"] == (frozenset({READ}),)
    assert cap.ROUTE_REQUIREMENTS["history"] == (frozenset({READ}),)
    assert cap.ROUTE_REQUIREMENTS["act"] == (frozenset({PREPARE, APPLY}),)
    assert cap.ROUTE_OPTIONAL["act"] == frozenset({READ, SUPERVISE})


def test_fx5_u02_fax_routes_use_fax_read() -> None:
    """A."""
    routes = f3.m("fax_staff_routes", package="haloflow.synthetic")
    p = routes.PREFIX
    assert {
        "list": ("fax_read", "GET", p + "/faxes"),
        "get": ("fax_read", "GET", p + "/faxes/{fax_id}"),
        "history": ("fax_read", "GET", p + "/faxes/{fax_id}/history"),
        "open": ("fax_read", "POST", p + "/faxes/{fax_id}/open"),
        "act": ("act", "POST", p + "/faxes/{fax_id}/actions"),
        "queues": ("fax_read", "GET", p + "/queues"),
    } == routes.ROUTES


def test_fx5_u03_registry() -> None:
    """A."""
    from haloflow.synthetic.composition import build_synthetic_registry

    registry = f5.fx5_registry()
    assert registry.target_version == f5.FX5_TARGET == 153
    assert registry.migration_ids == (*f4.fx4_registry().migration_ids, *f5.FX5_IDS)
    with pytest.raises(ValueError, match="FX5"):
        build_synthetic_registry(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                 include_fx2=True, include_fx5=True)


def test_fx5_u04_catalogue_rebinds() -> None:
    """A (Codex 273a6baf item 3: capability, mode and target per rebound key; constant open)."""
    from haloflow.m01.statements import StatementMode
    from haloflow.synthetic.composition import build_synthetic_catalog

    st = f3.m("fax_statements")
    expected = {
        st.READ_KEY: (StatementMode.READ, APPLY, "SELECT m05_fx_staff_read_v3(%(command)s)"),
        st.OPEN_KEY: (StatementMode.WRITE, APPLY, f5.OPEN_SQL),
        st.ACT_KEY: (StatementMode.WRITE, APPLY, "SELECT m05_fx_staff_action_v2(%(command)s)"),
        st.QUEUES_KEY: (StatementMode.READ, APPLY, "SELECT m05_fx_staff_queues_v2(%(command)s)"),
    }
    assert dict(st.FX5_REBOUND) == expected
    catalog = f5.fx5_catalog().catalog
    for key, (mode, capability, query) in expected.items():
        got = catalog.resolve(key)
        assert (got.mode, got.required_capability, got.query) == (mode, capability, query), key
    assert catalog.resolve(st.RECEIVE_KEY).query == "SELECT m05_fx_receive_v2(%(command)s)"
    assert catalog.resolve(st.LINES_KEY).query == "SELECT m05_fx_lines_snapshot(%(command)s)"
    fx4 = f4.fx4_catalog().catalog                     # FX4 composition unchanged
    assert fx4.resolve(st.READ_KEY).query == "SELECT m05_fx_staff_read_v2(%(command)s)"
    assert fx4.resolve(st.READ_KEY).required_capability == READ
    with pytest.raises(ValueError, match="FX5"):
        build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                include_fx2=True, include_fx3=True, include_fx5=True)
