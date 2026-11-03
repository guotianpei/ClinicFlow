"""No-response handling (plan v3.1 Day 6; package v1r1, tests frozen at b3e95a1).

Owner decisions (2026-10-26):
- B-1: a SUBMITTED reminder with no reply 24 elapsed hours after submission
  becomes NO_RESPONSE and gets one staff item ("call patient").
- B-2: a reply arriving after that is staff-only. That needs nothing here: the
  reply's effect finds the reminder no longer SUBMITTED and records
  ``reply_after_resolution`` (inbound ``_decide``), with no write-back task.

Submission time is ``reminder_records.updated_at`` while the row is SUBMITTED:
only ``_mark`` moves a row into SUBMITTED, and no writer touches a SUBMITTED row
except the guarded transition out of it (inventory pinned by NR-T0). The value
lies between the provider accepting the message and the outcome commit.

Ordering is the established one: ownership fence, then ``demo_match_lock``, then
reminder rows. Reply receipt and reply effects take the same lock, so the
"answered" check below cannot race a receipt: whichever commits first wins, and
the other sees its result.

No external call is made here. Synthetic demonstrator only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final

from sqlalchemy import text

from haloflow_demo.ownership import Ownership

ITEM_KIND: Final = "no_response"
DETAIL: Final = "No reply 24 hours after the reminder was sent — call patient."
DETAIL_OPTED_OUT: Final = DETAIL + " Phone opted out of SMS — call only."

Hook = Callable[[str], Awaitable[None]]

# A reminder is answered when a reply of the same provenance is explicitly
# correlated to it, or is uncorrelated with exactly one candidate and that
# candidate is this reminder. A reply correlated to another reminder, an
# ambiguous reply (two or more candidates), another provenance or a no-match
# reply does not answer it. Any class counts, UNCLEAR and STOP included.
_FLAG_SQL: Final = (
    "UPDATE reminder_records r SET state = 'NO_RESPONSE', updated_at = :t"
    " WHERE r.state = 'SUBMITTED'"
    " AND r.updated_at + interval '24 hours' <= :t"
    " AND r.appt_start > :t"
    " AND NOT EXISTS (SELECT 1 FROM inbound_messages m"
    " WHERE m.provenance = r.mode"
    " AND (m.simulated_correlation = r.id"
    " OR (m.simulated_correlation IS NULL"
    " AND (SELECT count(*) FROM inbound_candidates c WHERE c.inbound_id = m.id) = 1"
    " AND EXISTS (SELECT 1 FROM inbound_candidates c"
    " WHERE c.inbound_id = m.id AND c.reminder_id = r.id))))"
    " RETURNING r.id, r.phone_key"
)


async def flag_no_response(own: Ownership, now: Callable[[], datetime], *,
                           hook: Hook | None = None) -> list[int]:
    """Flag every overdue unanswered reminder; return the reminder ids flagged.

    Returns ``[]`` and changes nothing if closing or ownership loss is seen after
    the match-lock wait. Any exception rolls back the state change and the items
    together.
    """
    async with own.fenced() as conn:
        await conn.execute(text(
            "SELECT id FROM demo_match_lock WHERE id = 1 FOR UPDATE /* match:noresponse */"))
        if hook is not None:
            await hook("locked")
        if own.owner_lost or own.closing:
            return []
        t = now()
        rows = (await conn.execute(text(_FLAG_SQL), {"t": t})).all()
        if not rows:
            return []
        if hook is not None:
            await hook("flagged")
        flagged: list[int] = []
        for rid, phone_key in rows:
            opted = phone_key is not None and (await conn.execute(
                text("SELECT 1 FROM opt_outs WHERE phone_key = :k"), {"k": phone_key}
            )).scalar_one_or_none() is not None
            await conn.execute(text(
                "INSERT INTO follow_up_items(source_key, kind, reminder_id, detail)"
                " VALUES (:s, :k, :r, :d)"
                " ON CONFLICT ON CONSTRAINT uq_follow_up_source_kind DO NOTHING"
            ), {"s": f"reminder:{int(rid)}", "k": ITEM_KIND, "r": int(rid),
                "d": DETAIL_OPTED_OUT if opted else DETAIL})
            flagged.append(int(rid))
        return flagged
