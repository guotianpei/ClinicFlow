"""Synthetic install profiles (eligibility E4, v1 D-E4-1). SYNTHETIC ONLY.

A synthetic install is either the `default` profile (the B1-B5 registry, target 97, every
B-series pin unchanged) or the `eligibility` profile (the same registry plus eligibility
E1..E3b, target 126). The profile is chosen at `init`, recorded in `state.json` under the
top-level key `install_profile` (only for the eligibility profile, so a default install keeps
the pre-E4 key set exactly; `seal.profile` is a different thing, the installed-state digest
id), and must match the startup request; a mismatch is refused before any pool opens.
"""

from __future__ import annotations

import json
from typing import Any, Final

from haloflow.synthetic.errors import SyntheticRefused

DEFAULT: Final = "default"
ELIGIBILITY: Final = "eligibility"
PROFILES: Final = (DEFAULT, ELIGIBILITY)
STATE_FIELD: Final = "install_profile"
TARGETS: Final = {DEFAULT: 97, ELIGIBILITY: 126}
B5_SCHEDULERS: Final = ("due", "reminder", "writeback")
_SCHEDULERS: Final = {DEFAULT: B5_SCHEDULERS, ELIGIBILITY: (*B5_SCHEDULERS, "eligibility")}


def _no_duplicate_field(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key == STATE_FIELD and key in out:
            raise SyntheticRefused("PROFILE_INVALID")
        out[key] = value
    return out


def valid(profile: object) -> str:
    """The profile itself when it is one of PROFILES (exact str), else PROFILE_INVALID."""
    if type(profile) is not str or profile not in PROFILES:
        raise SyntheticRefused("PROFILE_INVALID")
    return profile


def from_state(state: dict[str, Any]) -> str:
    """The recorded profile of a parsed state: absent -> default; present -> validated."""
    if STATE_FIELD not in state:
        return DEFAULT
    return valid(state[STATE_FIELD])


def read_profile(raw: bytes) -> str:
    """The profile recorded in the state file bytes. Absent -> `default`; present -> its value
    when in PROFILES; an unknown value, a non-string, or the field given twice in the JSON
    object -> PROFILE_INVALID. Never carries the value."""
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_field)
    except SyntheticRefused:
        raise
    except (UnicodeDecodeError, ValueError):
        raise SyntheticRefused("STATE_CORRUPT") from None
    if not isinstance(value, dict):
        raise SyntheticRefused("STATE_CORRUPT")
    return from_state(value)


def check_request(installed: str, requested: str) -> None:
    """The startup request must name the installed profile."""
    if valid(installed) != valid(requested):
        raise SyntheticRefused("PROFILE_MISMATCH")


def registry(profile: str) -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    if valid(profile) == DEFAULT:
        return build_synthetic_registry()
    return build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True,
                                    include_el3b=True)


def catalog(profile: str) -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    if valid(profile) == DEFAULT:
        return build_synthetic_catalog()
    return build_synthetic_catalog(include_el1=True, include_el2=True, include_el3a=True,
                                   include_el3b=True)


def schedulers(profile: str) -> tuple[str, ...]:
    return _SCHEDULERS[valid(profile)]
