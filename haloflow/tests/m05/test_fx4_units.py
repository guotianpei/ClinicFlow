"""Incoming fax routing FX-IN, package FX4 — unit rows (RED BY DESIGN). Governing: see
`fx4_support`.
Classes: C control, A absence-red.

- FX4-C01 CONTROL: the FX2 registry (144) and FX3 catalogue build; include_fx4 absent today
- FX4-U01 normalise_sender_name over the corpus (NFKC first, whitespace, Cc/Cf, brackets incl.
          fullwidth, cap 40)
- FX4-U02 number masking over the corpus (Nd digits incl. fullwidth and Arabic-Indic; separators Zs,
          Pd, U+2212, . ( ) + /; >= 5 digits; 4 kept); number_runs returns ASCII digit strings
- FX4-U03 raw limit: 256 accepted, 257 -> ValueError; non-str -> ValueError
- FX4-U04 saved_name_valid: valid / invalid corpus (same detector)
- FX4-U05 InboundFax.sender_name defaults to None; QUEUES_KEY statement pinned; registry ids and
          target 149; include_fx4 chains (registry needs FX2, catalogue needs FX3) with "FX4" in the
          error
- FX4-U06 ingress body: sender_name absent / null / string accepted; number -> 400 MALFORMED
          sender_name; 257 characters -> 400 MALFORMED sender_name (no context / receive call)
- FX4-U07 staff routes: ROUTES["queues"] == ("list", "GET", PREFIX + "/queues")

"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fx1_support as fx
import fx2_support as f2
import fx3a_support as f3
import fx4_support as f4
import pytest


def test_fx4_c01_controls() -> None:
    """C."""
    assert f2.fx2_registry().target_version == 144
    assert f3.fx3_catalog() is not None


@pytest.mark.parametrize(("raw", "expected"), f4.CORPUS,
                         ids=[str(i) for i in range(len(f4.CORPUS))])
def test_fx4_u01_u02_normalise(raw: str | None, expected: str | None) -> None:
    """A."""
    from haloflow.m05 import fax_rules

    assert fax_rules.normalise_sender_name(raw) == expected
    assert (fax_rules.SENDER_NAME_RAW_MAX, fax_rules.SENDER_NAME_MAX, fax_rules.SAVED_NAME_MAX,
            fax_rules.MASK) == (256, 40, 64, f4.MASK)


def test_fx4_u02_number_runs() -> None:
    """A."""
    from haloflow.m05 import fax_rules

    assert fax_rules.number_runs("A 540-555-0100 B 804 555 0199") == ["5405550100", "8045550199"]
    assert fax_rules.number_runs("Lab ٥٤٠٥٥") == ["54055"]
    assert fax_rules.number_runs("Lab 540−555−0100") == ["5405550100"]
    assert fax_rules.number_runs("Suite 1200") == ["1200"]
    assert fax_rules.number_runs("no digits") == []


def test_fx4_u03_raw_limit() -> None:
    """A."""
    from haloflow.m05 import fax_rules

    assert fax_rules.normalise_sender_name("a" * 256) == "a" * 40
    for bad in ("a" * 257, 42, b"Quest", ["Quest"]):
        with pytest.raises(ValueError):
            fax_rules.normalise_sender_name(bad)  # type: ignore[arg-type]


def test_fx4_u04_saved_names() -> None:
    """A."""
    from haloflow.m05 import fax_rules

    assert [n for n in f4.SAVED_VALID if not fax_rules.saved_name_valid(n)] == []
    assert [n for n in f4.SAVED_INVALID if fax_rules.saved_name_valid(n)] == []


def test_fx4_u05_records_statements_registry() -> None:
    """A."""
    import uuid

    from haloflow.m01.statements import StatementMode
    from haloflow.synthetic.composition import build_synthetic_catalog, build_synthetic_registry

    fax = f3.m("fax")
    record = fax.InboundFax(provider="simfax", provider_fax_id="FX-1", line=fx.LINE1, sender=None,
                            pages=1, received_at=fx.received_at(), doc_ref="syn-doc-1")
    assert record.sender_name is None
    named = fax.InboundFax(provider="simfax", provider_fax_id="FX-2", line=fx.LINE1, sender=None,
                           pages=1, received_at=fx.received_at(), doc_ref="syn-doc-2",
                           sender_name="Quest")
    assert named.sender_name == "Quest"
    st = f3.m("fax_statements")
    assert st.QUEUES_KEY == "m05.fax.queues"
    assert dict(st.FX4_STATEMENTS) == {st.QUEUES_KEY: (
        StatementMode.READ, "m05.followup.read", "SELECT m05_fx_staff_queues(%(command)s)")}
    registry = f4.fx4_registry()
    assert registry.target_version == f4.FX4_TARGET
    assert registry.migration_ids == (*f2.fx2_registry().migration_ids, *f4.FX4_IDS)
    with pytest.raises(ValueError, match="FX4"):
        build_synthetic_registry(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                 include_fx4=True)
    with pytest.raises(ValueError, match="FX4"):
        build_synthetic_catalog(**fx.eligibility_flags(), include_fx1=True,  # type: ignore[call-arg]
                                include_fx2=True, include_fx4=True)
    compiled = f4.fx4_catalog()
    assert compiled.catalog.resolve(st.QUEUES_KEY).required_capability == "m05.followup.read"
    assert str(uuid.uuid4())  # keeps the import local (class-A rule)


class _Calls:
    def __init__(self) -> None:
        self.receives: list[Any] = []


def _app(calls: _Calls) -> Any:
    from fastapi import FastAPI

    ing = f3.m("fax_ingress")

    class _Fence:
        alive = True

        async def ensure(self) -> bool:
            return True

    async def context_for(_tenant: str) -> Any:
        return object()

    async def receive(_ctx: Any, fax: Any) -> Any:
        calls.receives.append(fax)
        return {"outcome": "needs_sorting", "fax_id": "00000000-0000-4000-8000-000000000001"}

    directory = ing.FaxLineDirectory.build({"tenant-a": [fx.LINE1]})
    app = FastAPI()
    app.include_router(ing.create_fax_ingress_router(
        ing.FaxIngressConfig.build(f3.SECRET), directory, _Fence(), context_for, receive))
    return app


@pytest.mark.parametrize(("value", "status"), [
    ("absent", 200), (None, 200), ("Quest Diagnostics", 200), (42, 400), ("q" * 257, 400)],
    ids=["absent", "null", "string", "number", "raw-257"])
def test_fx4_u06_ingress_sender_name(value: Any, status: int) -> None:
    """A."""
    calls = _Calls()
    body = f3.body()
    if value != "absent":
        body["sender_name"] = value
    got = asyncio.run(f3.request(_app(calls), json.dumps(body).encode()))
    assert got[0] == status, got
    if status == 400:
        assert got[1] == {"code": "MALFORMED", "field": "sender_name"}
        assert calls.receives == []
    else:
        assert calls.receives[0].sender_name == (None if value == "absent" else value)


def test_fx4_u07_queues_route_pinned() -> None:
    """A."""
    routes = f3.m("fax_staff_routes", package="haloflow.synthetic")
    assert routes.ROUTES["queues"] == ("list", "GET", routes.PREFIX + "/queues")
