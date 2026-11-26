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


# --- FX4 (OD-FX10): sender names ------------------------------------------------------------
#
# The SQL gateways (t147 saved names, t148 sent names) are the authority; these functions mirror
# them for tests, the simulator and the ingress pre-check. Order (Codex 8f518e20): NFKC ->
# whitespace to ' ' -> drop the remaining Cc/Cf and '<' '>' -> collapse spaces, trim -> mask each
# number run (>= 5 Unicode digits joined by separators) to MASK + its last four digits -> cut to
# SENDER_NAME_MAX -> trim; empty is None.

SENDER_NAME_RAW_MAX: Final = 256
SENDER_NAME_MAX: Final = 40
SAVED_NAME_MAX: Final = 64
MASK: Final = "•••"
RUN_DIGITS: Final = 5
_SEPARATORS: Final = frozenset(".()+/−")


def _is_digit(char: str) -> bool:
    import unicodedata

    return unicodedata.category(char) == "Nd"


def _is_separator(char: str) -> bool:
    import unicodedata

    return char in _SEPARATORS or unicodedata.category(char) in ("Zs", "Pd")


def _clean(raw: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKC", raw)
    text = "".join(" " if char.isspace() else char for char in text)
    text = "".join(char for char in text
                   if unicodedata.category(char) not in ("Cc", "Cf") and char not in "<>")
    return " ".join(text.split())


def _runs(text: str) -> list[tuple[int, int, str]]:
    """(start, end, ASCII digits) of every maximal digit/separator run that starts and ends with
    a digit."""
    import unicodedata

    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        if not _is_digit(text[i]):
            i += 1
            continue
        j, last = i, i
        while j < len(text) and (_is_digit(text[j]) or _is_separator(text[j])):
            if _is_digit(text[j]):
                last = j
            j += 1
        digits = "".join(str(unicodedata.decimal(c)) for c in text[i:last + 1] if _is_digit(c))
        out.append((i, last + 1, digits))
        i = last + 1
    return out


def number_runs(text: str) -> list[str]:
    """The ASCII digit string of every digit run in `text` (after the same cleaning)."""
    return [digits for _start, _end, digits in _runs(_clean(text))]


def normalise_sender_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    if type(raw) is not str or len(raw) > SENDER_NAME_RAW_MAX:
        raise ValueError("SENDER_NAME_INVALID")
    text = _clean(raw)
    parts: list[str] = []
    position = 0
    for start, end, digits in _runs(text):
        if len(digits) >= RUN_DIGITS:
            parts += [text[position:start], MASK + digits[-4:]]
            position = end
    parts.append(text[position:])
    result = "".join(parts)[:SENDER_NAME_MAX].strip()
    return result or None


def saved_name_valid(name: str) -> bool:
    import unicodedata

    if type(name) is not str:
        return False
    text = " ".join("".join(" " if c.isspace() else c
                            for c in unicodedata.normalize("NFKC", name)).split())
    if not 1 <= len(text) <= SAVED_NAME_MAX:
        return False
    if any(unicodedata.category(c) in ("Cc", "Cf") or c in "<>" for c in text):
        return False
    return not any(len(digits) >= RUN_DIGITS for _s, _e, digits in _runs(text))


def saved_name_stored(name: str) -> str:
    """The stored form of a valid saved name: NFKC, whitespace collapsed, trimmed."""
    import unicodedata

    return " ".join("".join(" " if c.isspace() else c
                            for c in unicodedata.normalize("NFKC", name)).split())
