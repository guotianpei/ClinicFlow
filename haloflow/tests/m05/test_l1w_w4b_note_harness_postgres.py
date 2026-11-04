"""L-1W W4b G-04: note text over HTTP — LABELLED HARNESS EVIDENCE (seam (a)).

Architecture v2 A9 proposal (a), plan v1 R-W4-10, Codex 5cca1a17 (keep G-03 genuine
refusal separate from the positive-note harness). PostgreSQL 17.

HARNESS (labelled; not genuine-bundle evidence): the app is the real ``create_app``
with the real tenant pool, ``start_production_services``, admission pool, verifier
(test issuer keys) and routes; only the published ``m05`` service is rebuilt by this
test as ``M05Service(<the genuine bundle's gateway>, foundation=<its foundation>,
keys=TestNoteKeys())``, the same construction the B4 rows use. The private
attributes ``_gateway`` and ``_foundation`` are read here, in test code only.

Rows:
- G-04a: a note-bearing ``claim_and_resolve`` by an ACT member returns 200; the
  history row carries a note id; an authorized reader (READ standing) gets exactly the
  note text from ``GET /v1/followup/notes/{note_id}``.
- G-04b: an admin-only member (no follow-up access) and an inactive member get 403
  and no text.
- G-03b (genuine): the GENUINE bundle (ProductionNoteKeys) reading that stored note
  returns 503 ``NOTE_KEY_UNAVAILABLE`` and no text.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import service_support as sv
import test_l1w_w4b_routes_postgres as rp
from psycopg import sql
from test_l1w_w4b_routes_postgres import clinic, env  # noqa: F401 - module fixtures

pytestmark = pytest.mark.postgres

KEYS = sv.TestNoteKeys()
TEXT = "Synthetic follow-up note: left a voicemail."


def _harness_m05(real: Any) -> Any:
    from haloflow.m05.service import M05Service

    return M05Service(real._gateway, foundation=real._foundation, keys=KEYS)  # HARNESS


async def _stored_note(c: rp.Clinic) -> tuple[str, str]:
    member = c.members["ACT"]
    item = rp._item(c)
    async with rp._client(c, wrap_m05=_harness_m05) as client:
        response = await client.post(
            f"/v1/followup/items/{item}/resolve", headers=rp._headers(member),
            json={"client_action_id": str(uuid.uuid4()), "expected_version": 1,
                  "type": "claim_and_resolve", "outcome_code": "other", "note": TEXT})
    assert response.status_code == 200, response.content
    rows = rp.st.admin_rows(c.install, sql.SQL(
        "SELECT note_id::text FROM {}.m05_followup_notes WHERE item_id = %s").format(
        sql.Identifier(c.install.schema)), (item,))
    assert len(rows) == 1
    return item, rows[0][0]


async def test_g04a_authorized_reader_gets_the_note_text(clinic: rp.Clinic) -> None:  # noqa: F811
    item, note_id = await _stored_note(clinic)
    async with rp._client(clinic, wrap_m05=_harness_m05) as client:
        response = await client.get(f"/v1/followup/notes/{note_id}",
                                    headers=rp._headers(clinic.members["READ"]))
    assert response.status_code == 200
    assert response.json() == {"note_id": note_id, "item_id": item, "text": TEXT}


@pytest.mark.parametrize("standing", ["ADMIN-ONLY", "INACTIVE"])
async def test_g04b_unauthorized_reader_gets_no_text(
    clinic: rp.Clinic, standing: str,  # noqa: F811
) -> None:
    _item, note_id = await _stored_note(clinic)
    async with rp._client(clinic, wrap_m05=_harness_m05) as client:
        response = await client.get(f"/v1/followup/notes/{note_id}",
                                    headers=rp._headers(clinic.members[standing]))
    assert (response.status_code, response.json()) == (403, {"code": "FORBIDDEN"})
    assert TEXT.encode() not in response.content


async def test_g03b_genuine_bundle_cannot_read_a_stored_note(clinic: rp.Clinic) -> None:  # noqa: F811
    _item, note_id = await _stored_note(clinic)
    async with rp._client(clinic) as client:  # GENUINE: ProductionNoteKeys
        response = await client.get(f"/v1/followup/notes/{note_id}",
                                    headers=rp._headers(clinic.members["READ"]))
    assert (response.status_code, response.json()) == (503, {"code": "NOTE_KEY_UNAVAILABLE"})
    assert TEXT.encode() not in response.content

