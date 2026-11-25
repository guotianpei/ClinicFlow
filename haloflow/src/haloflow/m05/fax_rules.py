"""Incoming fax routing FX-IN: pure rules (FX1). SYNTHETIC ONLY.

Governing: FX-IN requirements v1r1 (Codex ALIGN 8e064b80), architecture v1r2 (Codex ALIGN
469d7802), test cases v1 / FX1 red packet v1 (branch feat/fx-in-fx1), owner decisions OD-FX1..FX9.

The routing authority is the SQL gateway `m05_fx_receive` (t139). `decide` mirrors its rule for
tests and the simulator; it is never the authority. Levels (OD-FX5, sender wins):
1 = exact sender AND receiving line, 2 = exact sender only, 3 = receiving line only. A hidden
sender skips levels 1-2. The deciding level is the highest level with any rule; its DISTINCT
destinations decide: one -> routed once, two or more -> "Fax needs sorting" (rules_conflict).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

INVALID_NUMBER: Final = "INVALID_NUMBER"
META_TAG: Final = "m05.fx.meta/v1"
_SEP: Final = "\x1f"
E164: Final = re.compile(r"^\+[1-9][0-9]{7,14}$")
_PUNCT: Final = re.compile(r"[\s().\-]")

# The install-wide advisory lock key for receiving-line provisioning (architecture v1r2 §3.2,
# test cases v1r1 §0): the provisioner takes it exclusively, startup and every receipt shared.
# A fixed 63-bit value derived once from "haloflow.m05.fx.lines" (sha256, first 8 bytes, >> 1).
K_FX_LINES: Final = int.from_bytes(
    hashlib.sha256(b"haloflow.m05.fx.lines").digest()[:8], "big") >> 1


def canonical_e164(raw: str) -> str:
    """A fax number in canonical E.164. US/NANP numbers may be given as 10 digits or as 11 digits
    starting with 1, with spaces, dots, dashes or parentheses; a leading "+" keeps its country
    code. Anything else raises ValueError(INVALID_NUMBER)."""

    text = _PUNCT.sub("", raw)
    if text.startswith("+"):
        candidate = text
    elif text.isascii() and text.isdigit() and len(text) == 10:
        candidate = "+1" + text
    elif text.isascii() and text.isdigit() and len(text) == 11 and text.startswith("1"):
        candidate = "+" + text
    else:
        raise ValueError(INVALID_NUMBER)
    if not E164.fullmatch(candidate):
        raise ValueError(INVALID_NUMBER)
    return candidate


@dataclass(frozen=True, slots=True)
class Rule:
    sender: str | None
    line: str | None
    queue_id: str


@dataclass(frozen=True, slots=True)
class Decision:
    level: int | None
    queue_ids: frozenset[str]


def level_of(rule: Rule) -> int:
    if rule.sender is not None and rule.line is not None:
        return 1
    if rule.sender is not None:
        return 2
    if rule.line is not None:
        return 3
    raise ValueError("a rule needs a sender or a line")


def _matches(rule: Rule, *, sender: str | None, line: str) -> bool:
    level = level_of(rule)
    if level == 1:
        return sender is not None and rule.sender == sender and rule.line == line
    if level == 2:
        return sender is not None and rule.sender == sender
    return rule.line == line


def decide(rules: Iterable[Rule], *, sender: str | None, line: str) -> Decision:
    matched: dict[int, set[str]] = {}
    for rule in rules:
        if _matches(rule, sender=sender, line=line):
            matched.setdefault(level_of(rule), set()).add(rule.queue_id)
    for level in (1, 2, 3):
        if level in matched:
            return Decision(level, frozenset(matched[level]))
    return Decision(None, frozenset())


def meta_digest(*, line: str, sender: str | None, pages: int, received_at: str,
                doc_ref: str) -> bytes:
    """The immutable-metadata digest (FR-7). SQL (t139) computes the same bytes."""

    parts = (META_TAG, line, sender or "", str(pages), received_at, doc_ref)
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).digest()
