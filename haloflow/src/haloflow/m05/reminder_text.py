"""L-1S package S-H, checkpoint H2: the reminder text (Q-B4) and the name rule (D10r1).

`reminder_text` is the owner-approved Q-B4 wording exactly as the demonstrator renders it
(`demo/haloflow_demo/sending.py::reminder_text`, Rachel 2026-10-25): date and time in the
resolved zone, no zone suffix, the day zero-padded ("Nov 02") as the demonstrator does. The
clinic name is a parameter here (the frozen `clinic_display_name`).

The name rule is shared with SQL: `NAME_FORBIDDEN_RANGES` is ONE committed literal (Unicode
general categories Cc, Cf, Co, Cs, Zl, Zp from the Unicode 15.0 and 15.1 tables, plus the
emoji ranges U+2600-27BF, U+FE00-FE0F, U+1F000-1FAFF and U+E0100-E01EF). `reminder_units`
builds its SQL character class from the same table (`sql_forbidden_class`).

Privacy: nothing here logs.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

__all__ = [
    "FIRST_NAME_MAX",
    "CLINIC_NAME_MAX",
    "NAME_FORBIDDEN_RANGES",
    "NAME_WHITESPACE",
    "clinic_display_name_valid",
    "first_name_valid",
    "reminder_text",
    "sql_forbidden_class",
    "sql_whitespace_class",
]

FIRST_NAME_MAX: Final = 40
CLINIC_NAME_MAX: Final = 60

NAME_FORBIDDEN_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0000, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x0600, 0x0605), (0x061C, 0x061C),
    (0x06DD, 0x06DD), (0x070F, 0x070F), (0x0890, 0x0891), (0x08E2, 0x08E2), (0x180E, 0x180E),
    (0x200B, 0x200F), (0x2028, 0x202E), (0x2060, 0x2064), (0x2066, 0x206F), (0x2600, 0x27BF),
    (0xD800, 0xF8FF), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x110BD, 0x110BD),
    (0x110CD, 0x110CD), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0x1F000, 0x1FAFF), (0xE0001, 0xE0001), (0xE0020, 0xE007F), (0xE0100, 0xE01EF),
    (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD),
)

# A name may not start or end with one of these (the L-1W display-name whitespace set).
NAME_WHITESPACE: Final[tuple[tuple[int, int], ...]] = (
    (0x0009, 0x000D), (0x0020, 0x0020), (0x0085, 0x0085), (0x00A0, 0x00A0), (0x1680, 0x1680),
    (0x2000, 0x200A), (0x2028, 0x2029), (0x202F, 0x202F), (0x205F, 0x205F), (0x3000, 0x3000),
)


def _in(ranges: tuple[tuple[int, int], ...], cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def _name_valid(value: object, maximum: int) -> bool:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        return False
    if unicodedata.normalize("NFC", value) != value:
        return False
    if _in(NAME_WHITESPACE, ord(value[0])) or _in(NAME_WHITESPACE, ord(value[-1])):
        return False
    return not any(_in(NAME_FORBIDDEN_RANGES, ord(ch)) for ch in value)


def first_name_valid(value: object) -> bool:
    """R-H2-05 / D10r1: NFC, 1-40 characters, no edge whitespace, no forbidden code point."""

    return _name_valid(value, FIRST_NAME_MAX)


def clinic_display_name_valid(value: object) -> bool:
    """R-H2-03: the same rule, 1-60 characters."""

    return _name_valid(value, CLINIC_NAME_MAX)


def _class(ranges: tuple[tuple[int, int], ...]) -> str:
    parts = []
    for lo, hi in ranges:
        lo = max(lo, 0x0001)  # NUL cannot occur in PostgreSQL text
        if hi < lo:
            continue
        # Surrogates cannot occur in PostgreSQL text and are invalid in a regex escape.
        pieces = [(lo, hi)]
        if lo <= 0xDFFF and hi >= 0xD800:
            pieces = [(a, b) for a, b in ((lo, 0xD7FF), (0xE000, hi)) if a <= b]
        for a, b in pieces:
            parts.append(_escape(a) if a == b else f"{_escape(a)}-{_escape(b)}")
    return "[" + "".join(parts) + "]"


def _escape(cp: int) -> str:
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}"


def sql_forbidden_class() -> str:
    """The PostgreSQL ARE character class of NAME_FORBIDDEN_RANGES."""

    return _class(NAME_FORBIDDEN_RANGES)


def sql_whitespace_class() -> str:
    return _class(NAME_WHITESPACE)


def reminder_text(first_name: str, start_utc: datetime, zone: str,
                  clinic_display_name: str) -> str:
    """Exact owner-approved text (Q-B4). Date/time in the resolved zone, no zone suffix."""

    local = start_utc.astimezone(ZoneInfo(zone))
    hour = local.strftime("%I").lstrip("0") or "12"
    when = f"{local:%A, %b %d} at {hour}:{local:%M %p}"
    return (
        f"Hi {first_name}, this is {clinic_display_name}. You have an appointment on {when}. "
        "Reply 1 to confirm, 2 to reschedule, or 3 to cancel. Reply STOP to opt out."
    )
