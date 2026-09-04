"""Checksum v2: a versioned canonical payload, not a concatenation (R-P4.1-.7).

v1 digested ``" ".join(template.split())`` -- one field, so there was nothing to
separate. v2 must cover the execution role and the verification specification as
well, and the moment there is more than one field, concatenation becomes
ambiguous: ``role="a", template="b"`` and ``role="ab", template=""`` are the
same byte string and would digest alike. A checksum whose collisions are
reachable by editing a migration is not a drift control. The payload is
therefore canonical JSON with a ``checksum_version`` discriminator, and the
alternative -- a length-prefixed or delimiter-joined concatenation -- was
rejected because every delimiter is a character some future field may contain.

**Two normalizations, deliberately not merged** (R-P4.6). A migration *template*
collapses whitespace, so reindenting a migration is not drift. A function *body*
does not, because it is compared against ``prosrc``, where drift is meant to be
exact. A single ``normalize(s, collapse=False)`` is how these get merged by
accident in a later edit, so they are two functions with two names and one
shared core. Merging them would let a reindented body checksum equal while the
verifier found it different -- the checksum and the verifier disagreeing about
one string, which is exactly what A6 exists to prevent.

**Every collection is ordered by a stable semantic key** (R-P4.5), including the
nested ones, so two equivalent specifications cannot digest differently and no
collection is left to its declaration order. Ordering resolves presentation, not
contradiction: duplicate function identities, duplicate ACL grantees and
repeated ``proconfig`` keys are *refused* rather than silently ordered, because
sorting a contradiction only hides which of the two the digest committed to.

**Canonicalization fails closed rather than tidying** (Codex note-07). Ordering may remove a
presentation difference; it may never remove supplied data. The first cut filtered
a recognized collection to the members it knew how to order, which meant
``config=["a=1", 42]`` and ``config=["a=1"]`` digested alike -- a malformed
specification quietly becoming a valid one, which is precisely the collision this
module exists to prevent, reached from the inside. A member of the wrong type is
now refused, and so are two mapping keys that become one under NFC.

The collections belong to the verification specification, which a later
checkpoint builds. The ordering lives here from the start so that specification
is supplied to an already-ordered structure rather than having ordering added to
a payload that shipped without it -- which would move every checksum a second
time. For the same reason ``execution_role`` and ``verification`` are present as
``null`` while neither field yet exists on a unit: the payload shape is final
now, so the churn R-P4.4 gates happens once.
"""

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.codes import PreconditionCode

CHECKSUM_VERSION: Final = 2

# Mappings carrying either of these keys have that collection canonically
# ordered wherever it appears in the payload, at any depth. Matching on the key
# rather than on a fixed path means the verification specification is ordered
# whether its `config` entries sit on the specification or on each function.
_FUNCTIONS_KEY: Final = "functions"
_ACL_KEY: Final = "acl"
_CONFIG_KEY: Final = "config"
_PRIVILEGES_KEY: Final = "privileges"

_JSON_SEPARATORS: Final = (",", ":")


def _common(text: str) -> str:
    """Newline and Unicode normalization, shared by both rules.

    Newlines first: CRLF and a lone CR both become ``\\n`` so a file's line
    endings are not part of any digest. NFC second, so a composed and a
    decomposed spelling of the same text are one string. Neither step touches
    interior spacing -- that difference is what separates the two rules below.
    """

    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def normalize_template(template: str) -> str:
    """Migration templates collapse whitespace: reindenting is not drift.

    This is v1's exact behaviour, preserved so the *reason* a template's digest
    is stable does not change under v2 -- only what else the digest covers.
    """

    return " ".join(_common(template).split())


def normalize_body(body: str) -> str:
    """Function bodies do not collapse whitespace: ``prosrc`` drift is exact.

    R-P4.7: this is the same value the verifier renders and digests, so the
    checksum and the verifier can never disagree about one string.
    """

    return _common(body)


def _normalize_recursively(value: object) -> object:
    """Apply the shared normalization to every string in the payload (R-P4.7).

    Mapping keys are normalized too, which is why they must be strings and must
    not collide once normalized. Two keys that differ only by Unicode form are
    one key after NFC; building the new mapping without checking would let the
    second value overwrite the first and hand two distinct specifications the
    same digest. So the collision is refused rather than resolved.
    """

    if isinstance(value, str):
        return _common(value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value
                )
            normalized_key = _common(key)
            if normalized_key in normalized:
                raise MigrationUnitRejected(
                    reason_code=PreconditionCode.DUPLICATE_PAYLOAD_KEY.value
                )
            normalized[normalized_key] = _normalize_recursively(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_normalize_recursively(item) for item in value]
    return value


def _text_sequence(value: object) -> tuple[str, ...]:
    """A sequence of strings, or a refusal. Never a filtered subset.

    Filtering was the original defect (Codex note-07, finding 1): dropping a
    member that is not a string made ``["a=1", 42]`` and ``["a=1"]`` digest
    alike, so a malformed specification silently became a valid one. An absent
    collection is still absent -- ``None`` is empty, not malformed -- but a
    present collection must be a list of strings all the way down.

    **The container is checked before it is iterated** (Codex note-09). A bare
    string is iterable and yields strings, so materializing first made
    ``ordered_config("ab")`` return ``("a", "b")`` -- a malformed container
    silently becoming two well-formed entries. ``bytes`` and ``Mapping`` are
    refused for the same reason; any other iterable is accepted, so a generator
    or a tuple still works.
    """

    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise MigrationUnitRejected(
            reason_code=PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value
        )
    # Materialize once: iterating to validate and again to return would consume
    # a generator and hand back an empty tuple.
    members = tuple(value)
    for item in members:
        if not isinstance(item, str):
            raise MigrationUnitRejected(
                reason_code=PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value
            )
    return members


def _mapping_members(entries: object) -> tuple[Mapping[str, object], ...]:
    """Every member of a recognized collection, each required to be a mapping.

    The counterpart to ``_text_sequence`` for ``functions`` and ``acl``, and for
    the same reason: a member that cannot be ordered must fail construction, not
    disappear from the payload.
    """

    if isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Iterable):
        raise MigrationUnitRejected(
            reason_code=PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value
        )
    members: list[Mapping[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise MigrationUnitRejected(
                reason_code=PreconditionCode.CHECKSUM_PAYLOAD_MALFORMED.value
            )
        members.append(entry)
    return tuple(members)


def _parsed_config_key(entry: str) -> str:
    """The key of a ``key=value`` ``proconfig`` entry.

    Sorting the raw strings would order by the value whenever two keys share a
    prefix, so the sort key is the text before the first ``=``. An entry with no
    ``=`` is its own key: it is malformed, and inventing a parse for it here
    would hide that from whoever supplied it.
    """

    return entry.split("=", 1)[0]


def ordered_functions(
    entries: Iterable[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """Functions ordered by ``(name, argument_types)``; duplicate identities refused.

    ``_text_sequence`` refuses a malformed ``argument_types`` rather than
    filtering it, which also keeps the identity key honest: filtering made
    ``["uuid", 99]`` and ``["uuid", 100]`` one identity and rejected the pair as
    duplicates, so the earlier defect could reject valid input as well as accept
    invalid input.
    """

    ordered = sorted(
        _mapping_members(entries),
        key=lambda entry: (str(entry.get("name", "")), _text_sequence(entry.get("argument_types"))),
    )
    identities = [
        (str(entry.get("name", "")), _text_sequence(entry.get("argument_types")))
        for entry in ordered
    ]
    if len(set(identities)) != len(identities):
        raise MigrationUnitRejected(
            reason_code=PreconditionCode.DUPLICATE_FUNCTION_IDENTITY.value
        )
    return tuple(ordered)


def ordered_acl(entries: Iterable[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    """ACL entries ordered by grantee, each entry's privileges ordered within it.

    A grantee appearing twice is refused rather than merged: two entries for one
    grantee are two claims about the same privilege set, and ordering them would
    commit the digest to whichever sorted first.
    """

    ordered = sorted(_mapping_members(entries), key=lambda entry: str(entry.get("grantee", "")))
    grantees = [str(entry.get("grantee", "")) for entry in ordered]
    if len(set(grantees)) != len(grantees):
        raise MigrationUnitRejected(reason_code=PreconditionCode.DUPLICATE_ACL_ENTRY.value)

    canonical: list[Mapping[str, object]] = []
    for entry in ordered:
        if _PRIVILEGES_KEY in entry:
            promoted = dict(entry)
            promoted[_PRIVILEGES_KEY] = tuple(sorted(_text_sequence(entry[_PRIVILEGES_KEY])))
            canonical.append(promoted)
        else:
            canonical.append(entry)
    return tuple(canonical)


def ordered_config(entries: Iterable[str]) -> tuple[str, ...]:
    """``proconfig`` entries ordered by parsed key; a repeated key is refused.

    PostgreSQL cannot hold one ``proconfig`` key twice, so a repeat is malformed
    whether or not the two values agree -- an identical repeat is as much a
    defect in the specification as a contradictory one, and neither should be
    resolved by sorting.
    """

    ordered = tuple(sorted(_text_sequence(entries), key=_parsed_config_key))
    keys = [_parsed_config_key(entry) for entry in ordered]
    if len(set(keys)) != len(keys):
        raise MigrationUnitRejected(reason_code=PreconditionCode.CONFLICTING_CONFIG_KEY.value)
    return ordered


def _order_known_collections(value: object) -> object:
    """Order every recognized collection wherever it appears, at any depth."""

    if isinstance(value, Mapping):
        ordered: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if name == _FUNCTIONS_KEY:
                ordered[name] = tuple(
                    _order_known_collections(entry)
                    for entry in ordered_functions(_mapping_members(item))
                )
            elif name == _ACL_KEY:
                ordered[name] = tuple(
                    _order_known_collections(entry)
                    for entry in ordered_acl(_mapping_members(item))
                )
            elif name == _CONFIG_KEY:
                ordered[name] = ordered_config(_text_sequence(item))
            else:
                ordered[name] = _order_known_collections(item)
        return ordered
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_order_known_collections(item) for item in value]
    return value


def unit_payload(
    *,
    migration_id: str,
    template: str,
    execution_role: str | None = None,
    verification: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The A6 payload: normalized, canonically ordered, ready to digest.

    ``execution_role`` and ``verification`` are in the shape now, as ``null``,
    though no unit carries either field yet. Later checkpoints populate them
    without changing the shape, so every checksum moves once rather than three
    times.
    """

    payload: dict[str, object] = {
        "checksum_version": CHECKSUM_VERSION,
        "execution_role": execution_role,
        "migration_id": migration_id,
        "template": normalize_template(template),
        "verification": verification,
    }
    normalized = _normalize_recursively(payload)
    ordered = _order_known_collections(normalized)
    if not isinstance(ordered, dict):  # pragma: no cover - a mapping in, a mapping out
        raise MigrationUnitRejected(reason_code=PreconditionCode.UNTRUSTED_MIGRATION_UNIT.value)
    return ordered


def canonical_json(payload: Mapping[str, object]) -> str:
    """Sorted keys, ``(",",":")`` separators, real characters rather than escapes.

    ``ensure_ascii`` is off because the digest is over the UTF-8 encoding of NFC
    text (R-P4.2). Escaping to ASCII would make the NFC normalization invisible
    in the encoded form without changing what it protects, and would tie the
    digest to json's escaping rules as well as to the payload.
    """

    return json.dumps(payload, sort_keys=True, separators=_JSON_SEPARATORS, ensure_ascii=False)


def digest(payload: Mapping[str, object]) -> str:
    """SHA-256 over the canonical JSON encoding."""

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def unit_checksum(
    *,
    migration_id: str,
    template: str,
    execution_role: str | None = None,
    verification: Mapping[str, object] | None = None,
) -> str:
    """The checksum of one migration unit."""

    return digest(
        unit_payload(
            migration_id=migration_id,
            template=template,
            execution_role=execution_role,
            verification=verification,
        )
    )
