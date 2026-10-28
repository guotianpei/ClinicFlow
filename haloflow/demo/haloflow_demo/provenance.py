"""N-TZ Step 3 — interpretation provenance for sandbox (``ath-…``) reminders.

Package claude_n-tz-step3-provenance-package-2026-10-28-v2.md (Codex ALIGN).

Invariant: every retained sandbox reminder was claimed under the same
interpretation context — (department id, effective zone, basis, clinic zone) —
that every later snapshot of this database uses. The F-R1-1 slot dedupe key
(tenant, patient, start instant) is stable exactly while that context is.

The record is one ``demo_meta`` row (no schema change) with the exact value
``v1;dept=<id>;zone=<IANA>;basis=<DEPARTMENT|FALLBACK>;clinic=<IANA>``. All reads
and writes run in one fenced transaction under a transaction-scoped advisory
lock. Nothing here ever deletes or rewrites reminders, timestamps or contact
history. Synthetic mode never calls this module.
"""

from __future__ import annotations

import re
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from haloflow_demo.ownership import Ownership

KEY = "athena_tz_interpretation"
PROVENANCE_LOCK_CLASS = 0x545A5056  # "TZPV"
LEGACY = "LEGACY_ATHENA_REMINDERS"
UNSUPPORTED = "TZ_PROVENANCE_UNSUPPORTED"
CHANGED = "TZ_CONTEXT_CHANGED"
_VALUE_RE = re.compile(
    r"^v1;dept=([1-9][0-9]{0,11});zone=([A-Za-z0-9_/+\-]{1,64});"
    r"basis=(DEPARTMENT|FALLBACK);clinic=([A-Za-z0-9_/+\-]{1,64})$")


class Context(NamedTuple):
    dept: str
    zone: str
    basis: str
    clinic: str

    def value(self) -> str:
        return f"v1;dept={self.dept};zone={self.zone};basis={self.basis};clinic={self.clinic}"


class Conflict(NamedTuple):
    code: str
    stored: Context | None


def _iana(name: str) -> bool:
    if name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse(value: str) -> Context | None:
    """The stored context, or None when the value is not exactly supported."""
    m = _VALUE_RE.match(value)
    if m is None or not _iana(m.group(2)) or not _iana(m.group(4)):
        return None
    return Context(m.group(1), m.group(2), m.group(3), m.group(4))


async def _locked(conn: AsyncConnection) -> tuple[str | None, bool]:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:c, 0)"),
                       {"c": PROVENANCE_LOCK_CLASS})
    stored = (await conn.execute(
        text("SELECT value FROM demo_meta WHERE key = :k"), {"k": KEY})).scalar_one_or_none()
    retained = (await conn.execute(text(
        "SELECT 1 FROM reminder_records WHERE emr_appt_id LIKE 'ath-%' LIMIT 1"
    ))).scalar_one_or_none() is not None
    return stored, retained


async def check_stored(own: Ownership) -> str | None:
    """P0 (before any athena request): a fixed refusal code, or None."""
    async with own.fenced() as conn:
        stored, retained = await _locked(conn)
    if stored is not None:
        return None if parse(stored) is not None else UNSUPPORTED
    return LEGACY if retained else None


async def establish(own: Ownership, ctx: Context) -> Conflict | None:
    """P1 (after the department GET, before any booked GET), at most once per
    process: accept an equal record, write one where none is retained, else refuse."""
    async with own.fenced() as conn:
        stored, retained = await _locked(conn)
        if stored == ctx.value():
            return None
        old = parse(stored) if stored is not None else None
        if stored is not None and old is None:
            return Conflict(UNSUPPORTED, None)
        if retained:
            return Conflict(CHANGED if old is not None else LEGACY, old)
        await conn.execute(text(
            "INSERT INTO demo_meta(key, value) VALUES (:k, :v) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ), {"k": KEY, "v": ctx.value()})
    return None
