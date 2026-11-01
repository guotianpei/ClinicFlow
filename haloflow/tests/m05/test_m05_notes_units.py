"""B4 note rows that need no database (package v2 + v2r1; D11; owner O-1).

- D11-1..7  normalization, one rule per row, exact.
- NH-V      note HMAC literal vectors (computed with hmac/hashlib, 2026-11-01).
- NA-V      AAD literal vector.
- NE-1..3   AES-256-GCM round trip; swapped AAD/ciphertext/nonce -> NoteIntegrityFailed.
- KP-0      the production key provider fails closed (OD-D2 open).
- RV-1      ResolveAction validation (outcome set, `other` requires a note, types).

RED TRANCHE: everything except KP-0 fails until the implementation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

KEY = bytes([0x11]) * 32
TENANT = "pilot-clinic-01"
ITEM = uuid.UUID("00000000-0000-4000-8000-000000000201")
ACTION_KEY = uuid.UUID("00000000-0000-4000-8000-000000000301")
NOTE_ID = uuid.UUID("00000000-0000-4000-8000-000000000401")


def _n() -> Any:
    import haloflow.m05.notes as notes

    return notes


NORMALIZE = {
    "crlf": ("a\r\nb", "a\nb"),
    "bare-cr": ("a\rb", "a\nb"),
    "nfc": ("Café", "Café"),
    "trim": ("  \n hello \t ", "hello"),
    "empty-after-trim": ("   \n\t ", None),
    "none": (None, None),
    "500-code-points": ("x" * 500, "x" * 500),
}


@pytest.mark.parametrize("case", sorted(NORMALIZE))
def test_d11_normalization(case: str) -> None:
    raw, expected = NORMALIZE[case]
    assert _n().normalize_note(raw) == expected


REFUSED = {
    "501-code-points": "x" * 501,
    "c0-bell": "a\u0007b",
    "c0-tab-inside": "a\tb",
    "c1-nel": "a\u0085b",
    "u2028": "a b",
    "u2029": "a b",
    "not-str": 5,
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_d11_refusals(case: str) -> None:
    from haloflow.m05.service import CommandMalformed

    with pytest.raises((ValueError, CommandMalformed)):
        _n().normalize_note(REFUSED[case])


def test_nh_v_hmac_literal_vectors() -> None:
    assert _n().note_hmac(KEY, TENANT, "Called; left voicemail.") == (
        "1eba756bd4029327ca44baa909eb003f0ced26a413b5ef2c7adbefa7161c1f63"
    )
    two_lines = "Patient asked to call back after 5pm.\nNo answer at 2pm."
    assert _n().note_hmac(KEY, TENANT, two_lines) == (
        "03fa78a20a513709b12f902cdb42ffdc94abc8eb4eaed3d6945f7b4014b8e2cd"
    )


def test_nh_variants_share_one_digest_after_normalization() -> None:
    notes = _n()
    variants = ["Café call\r\n", "Café call\n", "  Café call  "]
    digests = {notes.note_hmac(KEY, TENANT, notes.normalize_note(v)) for v in variants}
    assert len(digests) == 1


def test_na_v_aad_literal_vector() -> None:
    assert _n().note_aad(TENANT, ITEM, ACTION_KEY, NOTE_ID, 1) == (
        b'["m05.note/v1","pilot-clinic-01","00000000-0000-4000-8000-000000000201",'
        b'"00000000-0000-4000-8000-000000000301","00000000-0000-4000-8000-000000000401",1]'
    )


def test_ne1_round_trip_and_fresh_nonce() -> None:
    notes = _n()
    a = notes.encrypt_note(KEY, 1, TENANT, ITEM, ACTION_KEY, "hello")
    b = notes.encrypt_note(KEY, 1, TENANT, ITEM, ACTION_KEY, "hello")
    assert len(a.nonce) == 12 and len(a.ciphertext) >= 16 and a.enc_key_version == 1
    assert a.nonce != b.nonce and a.note_id != b.note_id
    assert notes.decrypt_note(KEY, TENANT, ITEM, ACTION_KEY, a) == "hello"


@pytest.mark.parametrize("swap", ["item", "action-key", "note-id", "version", "nonce", "tenant",
                                  "ciphertext"])
def test_ne2_any_binding_change_fails_integrity(swap: str) -> None:
    from dataclasses import replace

    notes = _n()
    note = notes.encrypt_note(KEY, 1, TENANT, ITEM, ACTION_KEY, "hello")
    tenant, item, key = TENANT, ITEM, ACTION_KEY
    if swap == "item":
        item = uuid.uuid4()
    elif swap == "action-key":
        key = uuid.uuid4()
    elif swap == "tenant":
        tenant = "other-clinic-02"
    elif swap == "note-id":
        note = replace(note, note_id=uuid.uuid4())
    elif swap == "version":
        note = replace(note, enc_key_version=2)
    elif swap == "nonce":
        note = replace(note, nonce=bytes(12))
    else:
        other = notes.encrypt_note(KEY, 1, TENANT, ITEM, ACTION_KEY, "other")
        note = replace(note, ciphertext=other.ciphertext)
    with pytest.raises(notes.NoteIntegrityFailed) as caught:
        notes.decrypt_note(KEY, tenant, item, key, note)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_ne3_wrong_key_fails_integrity() -> None:
    notes = _n()
    note = notes.encrypt_note(KEY, 1, TENANT, ITEM, ACTION_KEY, "hello")
    with pytest.raises(notes.NoteIntegrityFailed):
        notes.decrypt_note(bytes(32), TENANT, ITEM, ACTION_KEY, note)


def test_kp0_production_provider_fails_closed() -> None:
    notes = _n()
    provider = notes.ProductionNoteKeys()
    for call in (provider.current_hmac, provider.current_encryption,
                 lambda: provider.hmac_key(1), lambda: provider.encryption_key(1)):
        with pytest.raises(notes.KeyUnavailable):
            call()


def _resolve(**overrides: Any) -> Any:
    from haloflow.m05.service import ResolveAction

    values: dict[str, Any] = {"item_id": ITEM, "client_action_id": ACTION_KEY,
                              "expected_version": 1, "type": "resolve",
                              "outcome_code": "rescheduled", "note": None}
    values.update(overrides)
    return ResolveAction(**values)


RESOLVE_CASES = {
    "item-str": {"item_id": str(ITEM)},
    "version-bool": {"expected_version": True},
    "type-claim": {"type": "claim"},
    "outcome-unknown": {"outcome_code": "closed"},
    "outcome-list": {"outcome_code": []},
    "other-without-note": {"outcome_code": "other"},
    "other-empty-note": {"outcome_code": "other", "note": "   "},
    "note-too-long": {"note": "x" * 501},
    "note-control": {"note": "a\u0007b"},
    "note-not-str": {"note": 5},
}


@pytest.mark.parametrize("case", sorted(RESOLVE_CASES))
def test_rv1_resolve_action_malformed(case: str) -> None:
    from haloflow.m05.service import CommandMalformed

    with pytest.raises(CommandMalformed):
        _resolve(**RESOLVE_CASES[case])


def test_rv1_resolve_action_controls() -> None:
    _resolve()
    _resolve(type="claim_and_resolve", outcome_code="no_contact_no_answer")
    _resolve(outcome_code="other", note="Patient will call the front desk.")
    assert _resolve(note="  spaced  ").note == "spaced"  # stored normalized
    assert _resolve(note="   ").note is None  # empty after trim is absent
