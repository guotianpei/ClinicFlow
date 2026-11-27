"""Synthetic install profiles (eligibility E4, v1 D-E4-1). SYNTHETIC ONLY.

A synthetic install is either the `default` profile (the B1-B5 registry, target 97, every
B-series pin unchanged) or the `eligibility` profile (the same registry plus eligibility
E1..E3b, the OD-EL12 t127 read and the E5 t128..t134 chart documentation units, target
134). The profile is chosen at `init`, recorded in `state.json` under the
top-level key `install_profile` (only for the eligibility profile, so a default install keeps
the pre-E4 key set exactly; `seal.profile` is a different thing, the installed-state digest
id), and must match the startup request; a mismatch is refused before any pool opens.

FX-IN FX3a adds the `fax` profile: the eligibility profile plus the fax units FX1/FX2/FX4/FX5
(t136..t153, target 153; FX4 adds the queue read and sender display; FX5 limits fax access to
staff who can act, OD-FX11), the fax statement keys,
the fax ingress workload and one synthetic receiving line. Everything the eligibility profile
composes, the fax profile composes too (`has_eligibility`).
"""

from __future__ import annotations

import json
from typing import Any, Final

from haloflow.synthetic.errors import SyntheticRefused

DEFAULT: Final = "default"
ELIGIBILITY: Final = "eligibility"
FAX: Final = "fax"
PROFILES: Final = (DEFAULT, ELIGIBILITY, FAX)
STATE_FIELD: Final = "install_profile"
# OD-EL12 (Rachel 2026-11-22): the eligibility profile adds the t127 contact-history read;
# E5 (EL-12) adds t128..t134, the simulated chart documentation units.
TARGETS: Final = {DEFAULT: 97, ELIGIBILITY: 135, FAX: 153}    # E5: t128..t135; FX5: ..t153
B5_SCHEDULERS: Final = ("due", "reminder", "writeback")
_SCHEDULERS: Final = {DEFAULT: B5_SCHEDULERS, ELIGIBILITY: (*B5_SCHEDULERS, "eligibility"),
                      FAX: (*B5_SCHEDULERS, "eligibility")}
_EL_FLAGS: Final = {"include_el1": True, "include_el2": True, "include_el3a": True,
                    "include_el3b": True, "include_el4": True, "include_el5": True}


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


def has_eligibility(profile: str) -> bool:
    """True for the profiles that compose the eligibility package (eligibility, fax). Never
    raises (an unknown value is simply not one of them)."""
    return type(profile) is str and profile in (ELIGIBILITY, FAX)


def registry(profile: str) -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    if valid(profile) == DEFAULT:
        return build_synthetic_registry()
    if profile == FAX:
        return build_synthetic_registry(**_EL_FLAGS, include_fx1=True, include_fx2=True,
                                        include_fx4=True, include_fx5=True)
    return build_synthetic_registry(**_EL_FLAGS)


def catalog(profile: str) -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    if valid(profile) == DEFAULT:
        return build_synthetic_catalog()
    if profile == FAX:
        return build_synthetic_catalog(**_EL_FLAGS, include_fx1=True, include_fx2=True,
                                       include_fx3=True, include_fx4=True, include_fx5=True)
    return build_synthetic_catalog(**_EL_FLAGS)


def schedulers(profile: str) -> tuple[str, ...]:
    return _SCHEDULERS[valid(profile)]
