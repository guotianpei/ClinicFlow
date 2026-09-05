"""Closed, immutable M01 verification values (R-P2, A2).

This checkpoint defines expected state and literal rendering only. Database
evaluation belongs to CP-7b. Argument types are identity data: neither rendering
nor serialization interprets them as SQL or replaces their schema sentinels.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.checksum import normalize_body, ordered_config
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_CONFIG_KEY = re.compile(r"[a-z_][a-z0-9_.]*")


def _reject(code: PreconditionCode) -> MigrationUnitRejected:
    return MigrationUnitRejected(reason_code=code.value)


def _identifier(value: str) -> None:
    # Deliberately narrow unquoted identifiers, with PostgreSQL's 63-byte limit.
    # PUBLIC is an ACL pseudo-grantee, not an expected role (R-P2.6).
    if type(value) is not str or not _IDENTIFIER.fullmatch(value) or value == "public":
        raise _reject(PreconditionCode.VERIFICATION_IDENTIFIER_INVALID)


def _text(value: str) -> str:
    if type(value) is not str or "\x00" in value:
        raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
    return normalize_body(value)


def _texts(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
    return tuple(_text(value) for value in values)


def _schema(schema_key: str) -> None:
    if type(schema_key) is not str or not SCHEMA_KEY_PATTERN.fullmatch(schema_key):
        raise _reject(PreconditionCode.SCHEMA_KEY_INVALID)


@dataclass(frozen=True, slots=True)
class AclEntry:
    """One function grantee's exact privilege set; functions only have EXECUTE.

    ALL is SQL shorthand, not a privilege name returned by aclexplode. Empty
    sets have no ACL entry and are expressed by omitting the grantee entirely.
    """

    grantee: str
    privileges: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.grantee)
        if type(self.privileges) is not tuple or self.privileges != ("EXECUTE",):
            raise _reject(PreconditionCode.VERIFICATION_ACL_INVALID)


@dataclass(frozen=True, slots=True)
class FunctionExpectation:
    """Expected metadata for one tenant-relative (name, argument_types) identity."""

    name: str
    argument_types: tuple[str, ...]
    owner: str
    security_definer: bool
    config: tuple[str, ...]
    acl: tuple[AclEntry, ...]
    body: str = field(repr=False)

    def __post_init__(self) -> None:
        _identifier(self.name)
        _identifier(self.owner)
        if type(self.security_definer) is not bool:
            raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
        arguments = _texts(self.argument_types)
        if any(not argument.strip() for argument in arguments):
            raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
        config = _texts(self.config)
        for entry in config:
            key, separator, _ = entry.partition("=")
            if not separator or not _CONFIG_KEY.fullmatch(key):
                raise _reject(PreconditionCode.VERIFICATION_CONFIG_INVALID)
        if type(self.acl) is not tuple or any(type(entry) is not AclEntry for entry in self.acl):
            raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
        if len({entry.grantee for entry in self.acl}) != len(self.acl):
            raise _reject(PreconditionCode.DUPLICATE_ACL_ENTRY)
        object.__setattr__(self, "argument_types", arguments)
        object.__setattr__(self, "config", ordered_config(config))
        object.__setattr__(self, "acl", tuple(sorted(self.acl, key=lambda entry: entry.grantee)))
        object.__setattr__(self, "body", _text(self.body))

    def render_body(self, schema_key: str) -> str:
        """Render the same normalized body that is serialized into the checksum."""
        _schema(schema_key)
        return self.body.replace("{schema}", schema_key)

    def render_config(self, schema_key: str) -> tuple[str, ...]:
        """Render values only after validation, with no formatting language."""
        _schema(schema_key)
        return tuple(entry.replace("{schema}", schema_key) for entry in self.config)


@dataclass(frozen=True, slots=True)
class FunctionMetadataVerification:
    """The sole PR-3 discriminator; only declared identities are in scope."""

    functions: tuple[FunctionExpectation, ...]
    kind: Literal["function_metadata"] = field(default="function_metadata", kw_only=True)

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind != "function_metadata":
            raise _reject(PreconditionCode.VERIFICATION_KIND_UNKNOWN)
        if type(self.functions) is not tuple or any(
            type(function) is not FunctionExpectation for function in self.functions
        ):
            raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
        identities = [(function.name, function.argument_types) for function in self.functions]
        if len(set(identities)) != len(identities):
            raise _reject(PreconditionCode.DUPLICATE_FUNCTION_IDENTITY)
        object.__setattr__(self, "functions", tuple(sorted(
            self.functions, key=lambda function: (function.name, function.argument_types)
        )))


Verification: TypeAlias = FunctionMetadataVerification


def validate_verification(value: object) -> None:
    """Refuse executable/untyped specifications at both definition and unit construction.

    Mappings are inspected only to distinguish refusal causes, never parsed
    into a verifier. An exact owned type is required; subclasses cannot add a
    verifier hook. Modules provide expected values through these dataclasses.
    """
    if value is None or type(value) is FunctionMetadataVerification:
        return
    if isinstance(value, str):
        raise _reject(PreconditionCode.VERIFICATION_SQL_FORBIDDEN)
    if callable(value):
        raise _reject(PreconditionCode.VERIFICATION_CALL_FORBIDDEN)
    if isinstance(value, Mapping):
        if "sql" in value:
            raise _reject(PreconditionCode.VERIFICATION_SQL_FORBIDDEN)
        if "function" in value or "callback" in value:
            raise _reject(PreconditionCode.VERIFICATION_CALL_FORBIDDEN)
        if "query" in value or "catalogue_query" in value:
            raise _reject(PreconditionCode.VERIFICATION_QUERY_FORBIDDEN)
        if value.get("kind") != "function_metadata":
            raise _reject(PreconditionCode.VERIFICATION_KIND_UNKNOWN)
        raise _reject(PreconditionCode.VERIFICATION_SPEC_INVALID)
    raise _reject(PreconditionCode.VERIFICATION_KIND_UNKNOWN)


def verification_payload(value: Verification) -> dict[str, object]:
    """Serialize the closed concrete structure, with no module-provided hooks.

    Constructors canonicalize functions, config and ACLs by their semantic
    keys. Argument order is significant and stays intact. Every ACL's sole
    legal privilege is EXECUTE, so its collection is already canonical.
    """
    validate_verification(value)
    return {"kind": value.kind, "functions": [
        {"name": f.name, "argument_types": list(f.argument_types), "owner": f.owner,
         "security_definer": f.security_definer, "config": list(f.config),
         "acl": [{"grantee": a.grantee, "privileges": list(a.privileges)} for a in f.acl],
         "body": f.body}
        for f in value.functions
    ]}
