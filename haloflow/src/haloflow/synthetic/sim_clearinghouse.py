"""Simulator controls for the SYNTHETIC clearinghouse (eligibility E4, v1 D-E4-4).
SYNTHETIC ONLY: eligibility install profile only.

`POST /synthetic/simulator/clearinghouse` (JSON, CSRF header) and its form twin
`POST /synthetic/simulator/ui/clearinghouse` set the in-memory scenario table of the composed
`SimulatedClearinghouse` (`lifecycle.clearinghouse`). A body is exactly one of

    {"member": {"member_id": "SYN-...", "status": "active"|"inactive"|"error",
                "error": <E2 error code, only with status "error">}}
    {"fail_next": n}     {"malformed_next": n}     n an int (never a bool), 0..10

and anything else is `ControlMalformed` -> 422 MALFORMED before any state change. Member ids
are synthetic only (`SYN-...`). Logs carry the status, the closed code and the operation
name only (never a member id or a supplied value).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

__all__ = ["ERRORS", "MAX_N", "MEMBER_ID", "OPS", "STATUSES", "ControlMalformed", "parse"]

LOGGER: Final = logging.getLogger("haloflow.synthetic.sim_clearinghouse")
MEMBER_ID: Final = re.compile(r"SYN-[A-Z0-9]{1,12}(?:-[A-Z0-9]{1,8})?")
STATUSES: Final = ("active", "inactive", "error")
# The E2 SimulatedClearinghouse error vocabulary (simulated_clearinghouse._ERRORS).
ERRORS: Final = ("subscriber_not_found", "name_dob_mismatch", "member_id_not_recognized",
                 "service_type_unsupported")
OPS: Final = ("member", "fail_next", "malformed_next")
MAX_N: Final = 10


class ControlMalformed(ValueError):
    """A clearinghouse control body outside the pinned shapes (carries no value)."""


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_N:
        raise ControlMalformed("n")
    return value


def _member(value: object) -> tuple[str, str, str | None]:
    if type(value) is not dict:
        raise ControlMalformed("member")
    keys = set(value)
    if keys not in ({"member_id", "status"}, {"member_id", "status", "error"}):
        raise ControlMalformed("member")
    member_id, status = value["member_id"], value["status"]
    if type(member_id) is not str or not MEMBER_ID.fullmatch(member_id):
        raise ControlMalformed("member_id")
    if type(status) is not str or status not in STATUSES:
        raise ControlMalformed("status")
    error = value.get("error")
    if (status == "error") != ("error" in value):
        raise ControlMalformed("error")
    if error is not None and (type(error) is not str or error not in ERRORS):
        raise ControlMalformed("error")
    return member_id, status, error


def parse(value: object) -> tuple[str, Any]:
    """("member", (member_id, status, error)) | ("fail_next", n) | ("malformed_next", n)."""
    if type(value) is not dict or len(value) != 1:
        raise ControlMalformed("top")
    ((op, arg),) = value.items()
    if op == "member":
        return "member", _member(arg)
    if op in ("fail_next", "malformed_next"):
        return op, _count(arg)
    raise ControlMalformed("op")


def apply(clearinghouse: Any, control: tuple[str, Any]) -> None:
    """Apply one parsed control to the composed SimulatedClearinghouse."""
    op, arg = control
    if op == "member":
        member_id, status, error = arg
        clearinghouse.set_member(member_id, status, error=error)
    elif op == "fail_next":
        clearinghouse.fail_next(arg)
    else:
        clearinghouse.malformed_next(arg)
