"""L-1W W4b: the capability matrix and route requirements (R-01).

Architecture v2 A5.2, addendum 1 pin 3, interface pins v1 1.1 and v1r1 correction 1.
This module is the ONLY place that turns a caller's standing into capabilities; any
other code path that grants one is a defect (the ``w4b_capabilities`` mutant group).

- ``capabilities_for(standing)``: the union of the matrix rows the standing holds.
  Inactive or unknown status grants nothing; ``act`` without ``read`` grants no ``act``
  row (it is granted only together with ``read``).
- ``route_capabilities(route, held)``: the first alternative of
  ``ROUTE_REQUIREMENTS[route]`` fully contained in ``held``, unioned with
  ``ROUTE_OPTIONAL[route] & held``; None when no alternative is held (403). Nothing
  outside ``held`` is ever added and no request input changes the set.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from haloflow.m01.staff.service import SELF_STANDING_CAPABILITY, Standing

SELF_STANDING: Final = SELF_STANDING_CAPABILITY

_READ: Final = "m05.followup.read"
_NOTE_READ: Final = "m05.followup.note.read"
_DISPLAY: Final = "m01.staff.display"
_PREPARE: Final = "m05.followup.prepare"
_APPLY: Final = "m05.followup.apply"
_APPEND: Final = "m02.observation.append"
_SUPERVISE: Final = "m05.followup.supervise"
_ADMIN: Final = "m01.staff.admin"

CAPABILITY_MATRIX: Final[Mapping[str, frozenset[str]]] = MappingProxyType({
    "read": frozenset({_READ, _NOTE_READ, _DISPLAY}),
    "act": frozenset({_PREPARE, _APPLY, _APPEND}),
    "supervisor": frozenset({_SUPERVISE}),
    "clinic_admin": frozenset({_ADMIN, _DISPLAY}),
})

ROUTE_REQUIREMENTS: Final[Mapping[str, tuple[frozenset[str], ...]]] = MappingProxyType({
    "list": (frozenset({_READ}),),
    "get": (frozenset({_READ}),),
    "history": (frozenset({_READ}),),
    "act": (frozenset({_PREPARE, _APPLY}),),
    "resolve": (frozenset({_PREPARE, _APPLY, _APPEND}),),
    "read_note": (frozenset({_READ, _NOTE_READ}),),
    "display": (frozenset({_DISPLAY, _READ}), frozenset({_DISPLAY, _ADMIN})),
    "admin": (frozenset({_ADMIN}),),
})

# v1r1 correction 1: the M05 service derives the SQL permissions {read, act, supervise}
# from the context, so the write routes carry read and supervise when they are held.
ROUTE_OPTIONAL: Final[Mapping[str, frozenset[str]]] = MappingProxyType({
    "list": frozenset(),
    "get": frozenset(),
    "history": frozenset(),
    "act": frozenset({_READ, _SUPERVISE}),
    "resolve": frozenset({_READ, _SUPERVISE}),
    "read_note": frozenset(),
    "display": frozenset(),
    "admin": frozenset(),
})


def capabilities_for(standing: Standing) -> frozenset[str]:
    if standing.status != "active":
        return frozenset()
    granted: set[str] = set()
    if "read" in standing.access:
        granted |= CAPABILITY_MATRIX["read"]
        if "act" in standing.access:
            granted |= CAPABILITY_MATRIX["act"]
    if "supervisor" in standing.roles:
        granted |= CAPABILITY_MATRIX["supervisor"]
    if "clinic_admin" in standing.roles:
        granted |= CAPABILITY_MATRIX["clinic_admin"]
    return frozenset(granted)


def route_capabilities(route: str, held: frozenset[str]) -> frozenset[str] | None:
    alternatives = ROUTE_REQUIREMENTS[route]  # KeyError for an unknown route
    for alternative in alternatives:
        if alternative <= held:
            return alternative | (ROUTE_OPTIONAL[route] & held)
    return None
