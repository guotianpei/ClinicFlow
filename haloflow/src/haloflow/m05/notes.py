"""B4 resolution notes (package v2 + v2r1, Claude/Codex aligned 2026-11-01; owner O-1
2026-11-01: all active same-clinic follow-up viewers may read notes, through the
separate m05.followup.note.read capability; red gate PASS at b8258d4).

- D11 normalization: CRLF and bare CR -> LF; NFC; trim; refuse remaining C0/C1
  controls (other than LF) and U+2028/U+2029; 1..500 code points; empty after trim is
  absent (None). No case folding.
- note_hmac = HMAC-SHA256(key[version], "m05.note/v1\\n" + tenant + "\\n" + text), hex.
- AEAD = AES-256-GCM, random 96-bit nonce, AAD = compact JSON
  ["m05.note/v1", tenant, item_id, client_action_id, note_id, enc_key_version].
- Keys come from a NoteKeyProvider. Production custody is the OD-D2 release gap: the
  production provider fails closed; tests use TestNoteKeys.

Every failure raised here carries a fixed message and neither cause nor context, so no
plaintext, key or library text can reach an exception chain.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

MAX_NOTE_CODE_POINTS = 500
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_CIPHERTEXT_BYTES = MAX_NOTE_CODE_POINTS * 4 + TAG_BYTES
_DOMAIN = "m05.note/v1"


class KeyUnavailable(Exception):
    """The requested key version is absent, retired or the provider is closed."""

    def __init__(self) -> None:
        super().__init__("note key unavailable")


class NoteIntegrityFailed(Exception):
    def __init__(self) -> None:
        super().__init__("note integrity failed")


class NoteInvalid(ValueError):
    """A note that violates D11 (fixed message; never carries the text)."""

    def __init__(self) -> None:
        super().__init__("note invalid")


class NoteKeyProvider(Protocol):
    def current_hmac(self) -> tuple[int, bytes]: ...
    def hmac_key(self, version: int) -> bytes: ...
    def current_encryption(self) -> tuple[int, bytes]: ...
    def encryption_key(self, version: int) -> bytes: ...


@dataclass(frozen=True)
class EncryptedNote:
    note_id: UUID
    nonce: bytes
    ciphertext: bytes
    enc_key_version: int


def _refused_char(ch: str) -> bool:
    code = ord(ch)
    if ch == "\n":
        return False
    return code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029)


def normalize_note(text: str | None) -> str | None:
    """D11. Returns the normalized note, or None when absent; raises NoteInvalid."""

    if text is None:
        return None
    if type(text) is not str:
        raise NoteInvalid()
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = unicodedata.normalize("NFC", value)
    value = value.strip()
    if value == "":
        return None
    if any(_refused_char(ch) for ch in value) or len(value) > MAX_NOTE_CODE_POINTS:
        raise NoteInvalid()
    return value


def is_normal(text: object) -> bool:
    """True when `text` is a present, D11-normal note (normalization is a fixpoint)."""

    if type(text) is not str:
        return False
    try:
        return normalize_note(text) == text
    except NoteInvalid:
        return False


def note_hmac(key: bytes, tenant_id: str, text: str) -> str:
    message = f"{_DOMAIN}\n{tenant_id}\n{text}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def note_aad(tenant_id: str, item_id: UUID, client_action_id: UUID, note_id: UUID,
             enc_key_version: int) -> bytes:
    return json.dumps(
        [_DOMAIN, tenant_id, str(item_id), str(client_action_id), str(note_id),
         enc_key_version],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def encrypt_note(key: bytes, version: int, tenant_id: str, item_id: UUID,
                 client_action_id: UUID, text: str) -> EncryptedNote:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    note_id = uuid.uuid4()
    nonce = os.urandom(NONCE_BYTES)
    aad = note_aad(tenant_id, item_id, client_action_id, note_id, version)
    ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), aad)
    return EncryptedNote(note_id=note_id, nonce=nonce, ciphertext=ciphertext,
                         enc_key_version=version)


def _decrypt(key: bytes, tenant_id: str, item_id: UUID, client_action_id: UUID,
             note: EncryptedNote) -> str | None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        aad = note_aad(tenant_id, item_id, client_action_id, note.note_id,
                       note.enc_key_version)
        plain = AESGCM(key).decrypt(note.nonce, note.ciphertext, aad)
        return plain.decode("utf-8")
    except (InvalidTag, ValueError, TypeError, UnicodeDecodeError):
        return None


def decrypt_note(key: bytes, tenant_id: str, item_id: UUID, client_action_id: UUID,
                 note: EncryptedNote) -> str:
    """AEAD decrypt with the AAD rebuilt from the binding, then the D11 re-check.
    Any failure is NoteIntegrityFailed with no cause or context."""

    text = _decrypt(key, tenant_id, item_id, client_action_id, note)
    if text is None or not is_normal(text):
        raise NoteIntegrityFailed()
    return text


class ProductionNoteKeys:
    """OD-D2 is open: production key custody does not exist, so every call fails
    closed with KeyUnavailable."""

    def current_hmac(self) -> tuple[int, bytes]:
        raise KeyUnavailable()

    def hmac_key(self, version: int) -> bytes:
        raise KeyUnavailable()

    def current_encryption(self) -> tuple[int, bytes]:
        raise KeyUnavailable()

    def encryption_key(self, version: int) -> bytes:
        raise KeyUnavailable()
