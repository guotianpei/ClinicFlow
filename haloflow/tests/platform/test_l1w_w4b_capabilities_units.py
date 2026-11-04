"""L-1W W4b: the capability matrix and route requirements (unit tier, no database).

Plan v1 R-W4-4, addendum 1 pin 3, architecture v2 A5.2, interface pins v1 1.1. Row:
R-01 (the literal, ``capabilities_for`` and the route-alternative choice).

PINNED INTERFACE (interface pins v1 1.1):
- ``haloflow.platform.capabilities.SELF_STANDING == "m01.staff.self_standing"``.
- ``CAPABILITY_MATRIX: Mapping[str, frozenset[str]]`` with exactly the keys
  ``read``, ``act``, ``supervisor``, ``clinic_admin`` and the A5.2 values.
- ``ROUTE_REQUIREMENTS: Mapping[str, tuple[frozenset[str], ...]]`` with exactly the
  eight W4b tenant routes; each value is an ordered tuple of alternatives.
- ``capabilities_for(standing) -> frozenset[str]`` over
  ``haloflow.m01.staff.service.Standing(status, access, roles)``.
- ``ROUTE_OPTIONAL: Mapping[str, frozenset[str]]`` (pins v1r1, Codex 264b3e82 #1):
  capabilities a route's context carries IN ADDITION when the standing holds them,
  because the service derives SQL permissions from the context (M05 service.py
  590-591, 660-661): ``act`` and ``resolve`` -> {read, supervise}; every other route
  -> empty. Nothing outside ``held`` is ever added.
- ``route_capabilities(route, held) -> frozenset[str] | None``: the first alternative
  fully contained in ``held``, unioned with ``ROUTE_OPTIONAL[route] & held``; None if
  no alternative is held (the route is denied, 403).

The expected values below are written out, not derived from the module under test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

READ = "m05.followup.read"
NOTE_READ = "m05.followup.note.read"
DISPLAY = "m01.staff.display"
PREPARE = "m05.followup.prepare"
APPLY = "m05.followup.apply"
APPEND = "m02.observation.append"
SUPERVISE = "m05.followup.supervise"
ADMIN = "m01.staff.admin"

EXPECTED_MATRIX: dict[str, frozenset[str]] = {
    "read": frozenset({READ, NOTE_READ, DISPLAY}),
    "act": frozenset({PREPARE, APPLY, APPEND}),
    "supervisor": frozenset({SUPERVISE}),
    "clinic_admin": frozenset({ADMIN, DISPLAY}),
}

EXPECTED_ROUTES: dict[str, tuple[frozenset[str], ...]] = {
    "list": (frozenset({READ}),),
    "get": (frozenset({READ}),),
    "history": (frozenset({READ}),),
    "act": (frozenset({PREPARE, APPLY}),),
    "resolve": (frozenset({PREPARE, APPLY, APPEND}),),
    "read_note": (frozenset({READ, NOTE_READ}),),
    "display": (frozenset({DISPLAY, READ}), frozenset({DISPLAY, ADMIN})),
    "admin": (frozenset({ADMIN}),),
}

EXPECTED_OPTIONAL: dict[str, frozenset[str]] = {
    "list": frozenset(), "get": frozenset(), "history": frozenset(),
    "act": frozenset({READ, SUPERVISE}), "resolve": frozenset({READ, SUPERVISE}),
    "read_note": frozenset(), "display": frozenset(), "admin": frozenset(),
}

READ_CAPS = EXPECTED_MATRIX["read"]
ACT_CAPS = EXPECTED_MATRIX["read"] | EXPECTED_MATRIX["act"]

# (status, access, roles) -> granted capabilities, written out per row.
STANDINGS: dict[str, tuple[tuple[str, tuple[str, ...], tuple[str, ...]], frozenset[str]]] = {
    "unknown": (("unknown", (), ()), frozenset()),
    "inactive-with-everything": (("inactive", ("act", "read"), ("clinic_admin", "supervisor")),
                                 frozenset()),
    "active-nothing": (("active", (), ()), frozenset()),
    "read": (("active", ("read",), ()), READ_CAPS),
    "act-without-read": (("active", ("act",), ()), frozenset()),
    "act": (("active", ("act", "read"), ()), ACT_CAPS),
    "sup-no-act": (("active", ("read",), ("supervisor",)), READ_CAPS | {SUPERVISE}),
    "sup-act": (("active", ("act", "read"), ("supervisor",)), ACT_CAPS | {SUPERVISE}),
    "sup-only": (("active", (), ("supervisor",)), frozenset({SUPERVISE})),
    "admin-only": (("active", (), ("clinic_admin",)), frozenset({ADMIN, DISPLAY})),
    "admin-act": (("active", ("act", "read"), ("clinic_admin",)), ACT_CAPS | {ADMIN}),
    "everything": (("active", ("act", "read"), ("clinic_admin", "supervisor")),
                   ACT_CAPS | {ADMIN, SUPERVISE}),
}


def _standing(status: str, access: tuple[str, ...], roles: tuple[str, ...]) -> Any:
    from haloflow.m01.staff.service import Standing

    return Standing(status=status, access=access, roles=roles)


def test_r01_self_standing_capability_literal() -> None:
    from haloflow.platform.capabilities import SELF_STANDING

    assert SELF_STANDING == "m01.staff.self_standing"


def test_r01_matrix_is_exactly_the_a52_literal() -> None:
    from haloflow.platform.capabilities import CAPABILITY_MATRIX

    assert isinstance(CAPABILITY_MATRIX, Mapping)
    assert dict(CAPABILITY_MATRIX) == EXPECTED_MATRIX
    assert all(type(v) is frozenset for v in CAPABILITY_MATRIX.values())


def test_r01_matrix_is_read_only() -> None:
    from haloflow.platform.capabilities import CAPABILITY_MATRIX

    with pytest.raises(TypeError):
        CAPABILITY_MATRIX["read"] = frozenset()  # type: ignore[index]


def test_r01_route_requirements_are_exactly_the_literal() -> None:
    from haloflow.platform.capabilities import ROUTE_REQUIREMENTS

    assert isinstance(ROUTE_REQUIREMENTS, Mapping)
    assert dict(ROUTE_REQUIREMENTS) == EXPECTED_ROUTES
    for alternatives in ROUTE_REQUIREMENTS.values():
        assert type(alternatives) is tuple
        assert all(type(a) is frozenset for a in alternatives)


def test_r01_route_optional_is_exactly_the_literal() -> None:
    from haloflow.platform.capabilities import ROUTE_OPTIONAL

    assert isinstance(ROUTE_OPTIONAL, Mapping)
    assert dict(ROUTE_OPTIONAL) == EXPECTED_OPTIONAL
    with pytest.raises(TypeError):
        ROUTE_OPTIONAL["act"] = frozenset()  # type: ignore[index]


def test_r01_route_requirements_are_read_only() -> None:
    from haloflow.platform.capabilities import ROUTE_REQUIREMENTS

    with pytest.raises(TypeError):
        ROUTE_REQUIREMENTS["admin"] = ()  # type: ignore[index]


def test_r01_no_matrix_row_grants_self_standing() -> None:
    from haloflow.platform.capabilities import CAPABILITY_MATRIX, SELF_STANDING

    assert all(SELF_STANDING not in caps for caps in CAPABILITY_MATRIX.values())


@pytest.mark.parametrize("case", sorted(STANDINGS))
def test_r01_capabilities_for_each_standing(case: str) -> None:
    from haloflow.platform.capabilities import capabilities_for

    (status, access, roles), expected = STANDINGS[case]
    granted = capabilities_for(_standing(status, access, roles))
    assert type(granted) is frozenset
    assert granted == expected


def test_r01_supervisor_without_act_cannot_act_or_resolve() -> None:
    from haloflow.platform.capabilities import capabilities_for, route_capabilities

    held = capabilities_for(_standing("active", ("read",), ("supervisor",)))
    assert route_capabilities("act", held) is None
    assert route_capabilities("resolve", held) is None
    assert route_capabilities("list", held) == frozenset({READ})


def test_r01_admin_only_standing_is_route_specific() -> None:
    # addendum 1 pin 3: the OD-FM1 first-admin shape
    from haloflow.platform.capabilities import capabilities_for, route_capabilities

    held = capabilities_for(_standing("active", (), ("clinic_admin",)))
    assert route_capabilities("admin", held) == frozenset({ADMIN})
    assert route_capabilities("display", held) == frozenset({DISPLAY, ADMIN})
    for route in ("list", "get", "history", "act", "resolve", "read_note"):
        assert route_capabilities(route, held) is None, route


def test_r01_display_prefers_the_read_alternative_when_both_are_held() -> None:
    from haloflow.platform.capabilities import route_capabilities

    held = ACT_CAPS | {ADMIN}
    assert route_capabilities("display", held) == frozenset({DISPLAY, READ})


@pytest.mark.parametrize("route", sorted(EXPECTED_ROUTES))
def test_r01_route_capabilities_with_everything_held(route: str) -> None:
    from haloflow.platform.capabilities import route_capabilities

    everything = frozenset().union(*EXPECTED_MATRIX.values())
    assert route_capabilities(route, everything) == (
        EXPECTED_ROUTES[route][0] | EXPECTED_OPTIONAL[route])
    assert route_capabilities(route, frozenset()) is None


# Bounded service sets per standing for the two write routes (Codex 264b3e82 #1).
WRITE_SETS: dict[str, tuple[frozenset[str] | None, frozenset[str] | None]] = {
    # standing -> (act, resolve)
    "act": (frozenset({PREPARE, APPLY, READ}), frozenset({PREPARE, APPLY, APPEND, READ})),
    "sup-act": (frozenset({PREPARE, APPLY, READ, SUPERVISE}),
                frozenset({PREPARE, APPLY, APPEND, READ, SUPERVISE})),
    "sup-no-act": (None, None),
    "read": (None, None),
    "admin-only": (None, None),
    "admin-act": (frozenset({PREPARE, APPLY, READ}), frozenset({PREPARE, APPLY, APPEND, READ})),
    "everything": (frozenset({PREPARE, APPLY, READ, SUPERVISE}),
                   frozenset({PREPARE, APPLY, APPEND, READ, SUPERVISE})),
}


@pytest.mark.parametrize("case", sorted(WRITE_SETS))
def test_r01_write_routes_carry_read_and_held_supervise(case: str) -> None:
    from haloflow.platform.capabilities import capabilities_for, route_capabilities

    (status, access, roles), _ = STANDINGS[case]
    held = capabilities_for(_standing(status, access, roles))
    act, resolve = WRITE_SETS[case]
    assert route_capabilities("act", held) == act
    assert route_capabilities("resolve", held) == resolve


def test_r01_optional_never_adds_an_unheld_capability() -> None:
    from haloflow.platform.capabilities import route_capabilities

    held = frozenset({PREPARE, APPLY})  # not reachable from the matrix; bound check
    assert route_capabilities("act", held) == frozenset({PREPARE, APPLY})


@pytest.mark.parametrize("route", sorted(EXPECTED_ROUTES))
def test_r01_route_denied_when_one_capability_is_missing(route: str) -> None:
    from haloflow.platform.capabilities import route_capabilities

    for alternative in EXPECTED_ROUTES[route]:
        for missing in alternative:
            held = alternative - {missing}
            others = [a for a in EXPECTED_ROUTES[route] if a <= held]
            expected = (others[0] | (EXPECTED_OPTIONAL[route] & held)) if others else None
            assert route_capabilities(route, held) == expected


def test_r01_unknown_route_is_refused() -> None:
    from haloflow.platform.capabilities import route_capabilities

    with pytest.raises(KeyError):
        route_capabilities("support", frozenset({ADMIN}))


@pytest.mark.parametrize("bad", [
    ("active", ("write",), ()), ("active", (), ("owner",)), ("enabled", (), ()),
])
def test_r01_standing_outside_the_closed_vocabulary_is_refused(bad: Any) -> None:
    from haloflow.m01.staff.service import ReadFailed

    with pytest.raises(ReadFailed) as caught:
        _standing(*bad)
    assert caught.value.reason_code == "READ_RESULT_INVALID"
