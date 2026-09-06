"""Closed, immutable M01 verification values (R-P2, A2).

Expected state, fixed catalogue query, and pure comparison. The runner owns
database execution. Argument types are identity data: neither rendering nor
serialization interprets them as SQL or replaces their schema sentinels.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

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


# Executed only by the runner, after SET LOCAL search_path = pg_catalog, pg_temp.
# format_type spelling is path-sensitive even when its call is qualified.
# proargtypes includes input/INOUT/variadic types in order, excluding OUT-only
# arguments. Canonical spellings (integer, text[], tenant_x.my_type) are compared
# literally: aliases such as int4 and pg_catalog.int4 do not match integer.
# Supplied argument strings never enter SQL resolution, casts or interpolation.
FUNCTION_METADATA_QUERY = """
    -- M01 function metadata verification
    SELECT p.proname,
           ARRAY(SELECT pg_catalog.format_type(a.type_oid, NULL)
                   FROM pg_catalog.unnest(p.proargtypes) WITH ORDINALITY a(type_oid, position)
                  ORDER BY a.position),
           owner.rolname, p.prosecdef, p.proconfig, p.proacl IS NULL, p.prosrc,
           COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(
                                acl.grantee, grantee.rolname,
                                acl.privilege_type, acl.is_grantable))
                       FROM pg_catalog.aclexplode(p.proacl) acl
                       LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee),
                    '[]'::pg_catalog.jsonb)
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace namespace ON namespace.oid = p.pronamespace
      JOIN pg_catalog.pg_roles owner ON owner.oid = p.proowner
     WHERE namespace.nspname = %s AND p.prokind = 'f'
"""


class VerificationMismatch(Exception):
    """Internal, content-free signal: runner rolls back before recording failure."""


def compare_function_metadata(
    verification: Verification, schema_key: str, rows: Sequence[Sequence[Any]]
) -> None:
    """Compare declared identities only; no database calls or module callbacks.

    NULL proacl is deliberately different from an explicit empty ACL. PUBLIC's
    OID 0 survives the query's LEFT JOIN and is rejected before set comparison.
    The expected model declares ordinary EXECUTE, never delegation rights.
    Grantor is not an expected dimension; all grantee/privilege entries count.
    """
    _schema(schema_key)
    actual = {(row[0], tuple(row[1])): row for row in rows}
    for expected in verification.functions:
        row = actual.get((expected.name, expected.argument_types))
        if row is None or row[5]:
            raise VerificationMismatch
        if row[2] != expected.owner or row[3] != expected.security_definer:
            raise VerificationMismatch
        if {normalize_body(entry) for entry in row[4] or ()} != set(
            expected.render_config(schema_key)
        ):
            raise VerificationMismatch
        acl = row[7]
        if any(entry[0] == 0 or entry[1] is None or entry[3] for entry in acl):
            raise VerificationMismatch
        actual_acl = {(entry[1], entry[2]) for entry in acl}
        expected_acl = {(entry.grantee, privilege)
                        for entry in expected.acl for privilege in entry.privileges}
        if actual_acl != expected_acl:
            raise VerificationMismatch
        actual_digest = hashlib.sha256(normalize_body(row[6]).encode("utf-8")).digest()
        expected_digest = hashlib.sha256(expected.render_body(schema_key).encode("utf-8")).digest()
        if actual_digest != expected_digest:
            raise VerificationMismatch
