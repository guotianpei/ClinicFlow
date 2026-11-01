"""B4 resolution notes (package v2 + v2r1, Claude/Codex aligned 2026-11-01; owner O-1
2026-11-01: all active same-clinic follow-up viewers may read notes, through the
separate m05.followup.note.read capability).

- D11 normalization: CRLF and bare CR -> LF; NFC; trim; refuse remaining C0/C1
  controls (other than LF) and U+2028/U+2029; 1..500 code points; empty after trim is
  absent (None).
- note_hmac = HMAC-SHA256(key[version], "m05.note/v1\\n" + tenant + "\\n" + text), hex.
- AEAD = AES-256-GCM, random 96-bit nonce, AAD = compact JSON
  ["m05.note/v1", tenant, item_id, client_action_id, note_id, enc_key_version].
- Keys come from a NoteKeyProvider. Production custody is the OD-D2 release gap: the
  production provider fails closed; tests use TestNoteKeys.

RED TRANCHE: the functions raise NotImplementedError; the types fix the interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class KeyUnavailable(Exception):
    """The requested key version is absent, retired or the provider is closed."""

    def __init__(self) -> None:
        super().__init__("note key unavailable")


class NoteIntegrityFailed(Exception):
    def __init__(self) -> None:
        super().__init__("note integrity failed")


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


def normalize_note(text: str | None) -> str | None:
    raise NotImplementedError("B4 note normalization")


def note_hmac(key: bytes, tenant_id: str, text: str) -> str:
    raise NotImplementedError("B4 note HMAC")


def note_aad(tenant_id: str, item_id: UUID, client_action_id: UUID, note_id: UUID,
             enc_key_version: int) -> bytes:
    raise NotImplementedError("B4 note AAD")


def encrypt_note(key: bytes, version: int, tenant_id: str, item_id: UUID,
                 client_action_id: UUID, text: str) -> EncryptedNote:
    raise NotImplementedError("B4 note encryption")


def decrypt_note(key: bytes, tenant_id: str, item_id: UUID, client_action_id: UUID,
                 note: EncryptedNote) -> str:
    raise NotImplementedError("B4 note decryption")


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
