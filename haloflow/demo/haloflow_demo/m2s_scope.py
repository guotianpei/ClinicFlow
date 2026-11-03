"""M-2S scope pin and local provenance gate (package v1r2, R-MS-10/§5).

No network access here: every check reads the demo database only.

- Scope pin ``demo_meta['athena_m2s_scope']`` = ``v1;practice=<id>;dept=<id>``.
  Written insert-if-absent by its first writer, under the provenance advisory lock,
  and never rewritten. A missing pin is re-established only while the database holds
  no athena_preview store rows (T-MS-31b); otherwise it refuses.
- Gate: before any sandbox store access, admission or seeding, the stored
  interpretation record (N-TZ Step 3) must exist, parse exactly and match the running
  department and clinic zone; the scope pin must equal the running configuration.

Refusals carry a fixed code only.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import text

from haloflow_demo import provenance
from haloflow_demo.ownership import Ownership

KEY = "athena_m2s_scope"
PROVENANCE_ABSENT = "M2S_PROVENANCE_ABSENT"
SCOPE_CHANGED = "M2S_SCOPE_CHANGED"


def pin_value(practice: str, department_id: str) -> str:
    return f"v1;practice={practice};dept={department_id}"


class Gate(NamedTuple):
    ok: bool
    code: str | None
    zone: str | None          # the stored interpretation zone when ok


async def _read(own: Ownership) -> tuple[str | None, str | None]:
    async with own.fenced() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:c, 0)"),
                           {"c": provenance.PROVENANCE_LOCK_CLASS})
        interp = (await conn.execute(text("SELECT value FROM demo_meta WHERE key = :k"),
                                     {"k": provenance.KEY})).scalar_one_or_none()
        pin = (await conn.execute(text("SELECT value FROM demo_meta WHERE key = :k"),
                                  {"k": KEY})).scalar_one_or_none()
    return interp, pin


async def gate(own: Ownership, *, practice: str, department_id: str,
               clinic_tz: str) -> Gate:
    """R-MS-10 for binding (run, reconcile, baseline). Never writes."""
    interp, pin = await _read(own)
    if interp is None:
        return Gate(False, PROVENANCE_ABSENT, None)
    ctx = provenance.parse(interp)
    if ctx is None:
        return Gate(False, provenance.UNSUPPORTED, None)
    if ctx.dept != department_id or ctx.clinic != clinic_tz:
        return Gate(False, provenance.CHANGED, None)
    if pin != pin_value(practice, department_id):
        return Gate(False, SCOPE_CHANGED, None)
    return Gate(True, None, ctx.zone)


async def establish(own: Ownership, *, practice: str, department_id: str) -> str | None:
    """Before seeding: accept an equal pin; write one only when none exists and no
    athena_preview store rows exist; otherwise a fixed refusal code."""
    want = pin_value(practice, department_id)
    async with own.fenced() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:c, 0)"),
                           {"c": provenance.PROVENANCE_LOCK_CLASS})
        pin = (await conn.execute(text("SELECT value FROM demo_meta WHERE key = :k"),
                                  {"k": KEY})).scalar_one_or_none()
        if pin is not None:
            return None if pin == want else SCOPE_CHANGED
        rows = (await conn.execute(text(
            "SELECT 1 FROM sim_confirmations WHERE source = 'athena_preview' LIMIT 1"
        ))).scalar_one_or_none() is not None
        if rows:
            return SCOPE_CHANGED
        await conn.execute(text(
            "INSERT INTO demo_meta(key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING"),
            {"k": KEY, "v": want})
    return None
