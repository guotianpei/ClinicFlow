"""Atomic post-install verification of a typed unit's installed function (CP2-2b).

Architecture v3 section 5 (the O-2 adapter; C-4, C-5, C-6; R-B9). This module
is generic M01: it names no module role and no module function. A module supplies
an `InstalledStateProfile` at composition, keyed by migration id, and the
composition root passes it to `build_tenant_migration_registry`.

WHAT IS HERE
------------
- `InstalledStateProfile`: the fixed, frozen facts about ONE installed function
  that a typed declaration cannot express (argument modes, return-set, owner,
  grantor and grantability of every ACL entry).
- `profile_digest`: SHA-256 over the profile's canonical JSON, computed once at
  composition.
- `InstalledStateRequirement`: the registry's immutable per-unit descriptor.
- `VerifiedInstalledState`: what consumption hands the runner.
- `check_profile_agreement`: the static agreement check between a profile and a
  unit's declaration (section 5.3). Neither overrides the other.
- `INSTALLED_FUNCTION_QUERY` and `compare_installed_function`: the observation.
  Separate from the legacy `FUNCTION_METADATA_QUERY` / `compare_function_metadata`,
  which stay byte-unchanged (R-B9.6).

WHAT IS NOT HERE
----------------
No database execution (the runner owns it), no rendering of templates, and no
expected value taken from observed state (R-B9.3).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.checksum import normalize_body
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.verification import VerificationMismatch
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN

__all__ = [
    "INSTALLED_FUNCTION_QUERY",
    "InstalledStateProfile",
    "InstalledStateRequirement",
    "VerifiedInstalledState",
    "check_profile_agreement",
    "compare_installed_function",
    "profile_digest",
]

_SCHEMA_PLACEHOLDER: Final = "{schema}"
# The declaration's spelling of a catalogue code (function_policy.py admits these
# words; pg_proc stores the letters).
_VOLATILITY: Final = {"volatile": "v", "stable": "s", "immutable": "i"}
_PARALLEL: Final = {"unsafe": "u", "restricted": "r", "safe": "s"}


def _invalid() -> MigrationUnitRejected:
    return MigrationUnitRejected(
        reason_code=PreconditionCode.INSTALLED_STATE_PROFILE_INVALID.value
    )


# ---------------------------------------------------------------------------
# The profile and the registry descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstalledStateProfile:
    """Fixed expectations for one installed function. Primitives and tuples only.

    `installed_acl` is the EXACT expected ACL: a tuple of
    `(grantee, privilege, grantor, is_grantable)`, including the owner's own
    entry, which a Form A declaration does not declare (E6).
    """

    function_name: str
    argument_types: tuple[str, ...]
    owner: str
    execution_role: str
    prokind: str
    return_type: str
    returns_set: bool
    language: str
    security_definer: bool
    volatility: str
    parallel: str
    strict: bool
    installed_acl: tuple[tuple[str, str, str, bool], ...]

    def __post_init__(self) -> None:
        for text in (
            self.function_name, self.owner, self.execution_role, self.prokind,
            self.return_type, self.language, self.volatility, self.parallel,
        ):
            if type(text) is not str or not text:
                raise _invalid()
        for flag in (self.returns_set, self.security_definer, self.strict):
            if type(flag) is not bool:
                raise _invalid()
        if type(self.argument_types) is not tuple or any(
            type(argument) is not str or not argument for argument in self.argument_types
        ):
            raise _invalid()
        if type(self.installed_acl) is not tuple or not self.installed_acl:
            raise _invalid()
        for entry in self.installed_acl:
            if (
                type(entry) is not tuple
                or len(entry) != 4
                or any(type(part) is not str or not part for part in entry[:3])
                or type(entry[3]) is not bool
            ):
                raise _invalid()
        if len(set(self.installed_acl)) != len(self.installed_acl):
            raise _invalid()


def profile_digest(profile: InstalledStateProfile) -> str:
    """SHA-256 over the profile's canonical JSON, lowercase hex."""

    if type(profile) is not InstalledStateProfile:
        raise _invalid()
    canonical = json.dumps(
        asdict(profile), ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InstalledStateRequirement:
    """The registry's immutable descriptor for one unit (architecture v3 5.1(2)).

    `required` is true if and only if a profile was supplied for the unit. The
    descriptor is owned by the registry. It is not part of the unit, the
    declaration, the checksum or the ledger identity (R-B9.6).
    """

    required: bool
    profile: InstalledStateProfile | None
    profile_digest: str | None


NOT_REQUIRED: Final = InstalledStateRequirement(required=False, profile=None, profile_digest=None)


def requirement_for(profile: InstalledStateProfile) -> InstalledStateRequirement:
    """The descriptor for a profiled unit. The digest is computed here, once."""

    return InstalledStateRequirement(
        required=True, profile=profile, profile_digest=profile_digest(profile)
    )


@dataclass(frozen=True, slots=True)
class VerifiedInstalledState:
    """What `consume_installed_state` returns: built from the registry's values."""

    migration_id: str
    required: bool
    profile: InstalledStateProfile | None


# ---------------------------------------------------------------------------
# The static agreement check (architecture v3 section 5.3)
# ---------------------------------------------------------------------------


def _only(block: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The single function a profiled declaration may declare."""

    if not isinstance(block, Mapping):
        raise _invalid()
    functions = block.get("functions")
    if not isinstance(functions, Sequence) or len(functions) != 1:
        raise _invalid()
    function = functions[0]
    if not isinstance(function, Mapping):
        raise _invalid()
    return function


def _acl_pairs(acl: Any) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for entry in acl:
        for privilege in entry["privileges"]:
            pairs.add((entry["grantee"], privilege))
    return pairs


def check_profile_agreement(
    profile: InstalledStateProfile,
    *,
    execution_role: str | None,
    policy: Mapping[str, Any] | None,
    policy_verification: Mapping[str, Any] | None,
) -> None:
    """Every "agree" cell of section 5.3 must hold, or `INSTALLED_STATE_PROFILE_INVALID`.

    Pure and static: no database, no rendering, no observed state. Run at
    composition over the unit's OWN frozen declaration, so a caller mutating its
    inputs afterwards cannot change what was checked (D32).
    """

    if type(profile) is not InstalledStateProfile:
        raise _invalid()
    try:
        declared = _only(policy)
        verified = _only(policy_verification)
        declared_acl = _acl_pairs(declared["acl"])
        verified_acl = _acl_pairs(verified["acl"])
        cells = (
            # identity: namespace, name, kind, arguments
            declared["schema"] == _SCHEMA_PLACEHOLDER,
            declared["name"] == profile.function_name,
            verified["name"] == profile.function_name,
            profile.prokind == "f" and declared["is_procedure"] is False,
            [parameter["type"] for parameter in declared["inputs"]]
            == list(profile.argument_types),
            list(verified["argument_types"]) == list(profile.argument_types),
            # result shape
            declared["return_type"] == profile.return_type,
            (len(declared["outputs"]) == 0) is (not profile.returns_set),
            declared["language"] == profile.language,
            # owner: the declared owner and the unit's execution role
            verified["owner"] == profile.owner,
            execution_role == profile.execution_role == profile.owner,
            # properties
            declared["security_definer"] is profile.security_definer,
            verified["security_definer"] is profile.security_definer,
            _VOLATILITY.get(declared["volatility"]) == profile.volatility,
            _PARALLEL.get(declared["parallel"]) == profile.parallel,
            declared["strict"] is profile.strict,
            # snapshot values the profile does not hold: the two declarations agree
            list(declared["config"]) == list(verified["config"]),
            declared["body_sha256"]
            == hashlib.sha256(normalize_body(verified["body"]).encode("utf-8")).hexdigest(),
            # ACL: the declared (Form A) entries are exactly the profile's
            # non-owner entries; the owner entry is profile-only.
            declared_acl == verified_acl,
            declared_acl
            == {
                (grantee, privilege)
                for grantee, privilege, _grantor, _grantable in profile.installed_acl
                if grantee != profile.owner
            },
        )
    except (KeyError, TypeError, AttributeError) as error:
        raise _invalid() from error
    if not all(cell is True for cell in cells):
        raise _invalid()


# ---------------------------------------------------------------------------
# Observation (architecture v3 section 5.2)
# ---------------------------------------------------------------------------

# Executed only by the runner, after `SET LOCAL search_path = pg_catalog, pg_temp`,
# with parameters `(schema_key, function_name)`. One row per pg_proc entry with
# that namespace and name, over EVERY prokind. Types and modes are resolved to
# booleans here, so the comparator never compares an OID with a text literal.
# Supplied strings never enter SQL resolution: both are bound parameters.
#
# The ACL grantee OID is cast to int8 before it enters JSON. `oid` is not a
# numeric type to jsonb, so an uncast OID becomes a JSON STRING, which the strict
# parser below would (correctly) refuse as malformed. Measured in CI on
# postgres:17, candidate v2: every real install failed VERIFICATION_FAILED.
#
# Columns (19, in this order): oid, prokind, pronargs, arg_is_uuid,
# allargtypes_is_null, argmodes_all_in, nargdefaults_zero, rettype_is_uuid,
# proretset, lang_is_plpgsql, owner, prosecdef, provolatile, proparallel,
# proisstrict, proconfig, prosrc, proacl_is_null, acl.
INSTALLED_FUNCTION_QUERY: Final = """-- M01 installed function verification
SELECT p.oid,
       p.prokind,
       p.pronargs,
       (p.pronargs = 1
        AND p.proargtypes[0] = 'pg_catalog.uuid'::pg_catalog.regtype) IS TRUE,
       p.proallargtypes IS NULL,
       (p.proargmodes IS NULL OR p.proargmodes = ARRAY['i']::pg_catalog."char"[]) IS TRUE,
       p.pronargdefaults = 0,
       p.prorettype = 'pg_catalog.uuid'::pg_catalog.regtype,
       p.proretset,
       (p.prolang = (SELECT l.oid
                       FROM pg_catalog.pg_language l
                      WHERE l.lanname = 'plpgsql')) IS TRUE,
       owner.rolname,
       p.prosecdef,
       p.provolatile,
       p.proparallel,
       p.proisstrict,
       p.proconfig,
       p.prosrc,
       p.proacl IS NULL,
       COALESCE((SELECT pg_catalog.jsonb_agg(
                            pg_catalog.jsonb_build_array(
                                acl.grantee::pg_catalog.int8, grantee.rolname,
                                grantor.rolname,
                                acl.privilege_type, acl.is_grantable)
                            ORDER BY acl.grantee, acl.grantor, acl.privilege_type)
                   FROM pg_catalog.aclexplode(p.proacl) acl
                   LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid = acl.grantee
                   LEFT JOIN pg_catalog.pg_roles grantor ON grantor.oid = acl.grantor),
                '[]'::pg_catalog.jsonb)
  FROM pg_catalog.pg_proc p
  JOIN pg_catalog.pg_namespace namespace ON namespace.oid = p.pronamespace
  LEFT JOIN pg_catalog.pg_roles owner ON owner.oid = p.proowner
 WHERE namespace.nspname = %s
   AND p.proname = %s
 ORDER BY p.oid
"""

_COLUMN_COUNT: Final = 19


@dataclass(frozen=True, slots=True)
class _Observed:
    oid: int
    prokind: str
    pronargs: int
    arg_is_uuid: bool
    allargtypes_is_null: bool
    argmodes_all_in: bool
    nargdefaults_zero: bool
    rettype_is_uuid: bool
    proretset: bool
    lang_is_plpgsql: bool
    owner: str
    prosecdef: bool
    provolatile: str
    proparallel: str
    proisstrict: bool
    proconfig: tuple[str, ...] | None
    prosrc: str
    proacl_is_null: bool
    acl: tuple[tuple[int, str | None, str | None, str, bool], ...]


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _is_str(value: object) -> bool:
    return type(value) is str


def _parse_acl(value: object) -> tuple[tuple[int, str | None, str | None, str, bool], ...]:
    if type(value) is not list:
        raise VerificationMismatch
    entries = []
    for entry in value:
        if type(entry) not in (list, tuple) or len(entry) != 5:
            raise VerificationMismatch
        grantee_oid, grantee, grantor, privilege, grantable = entry
        if not (
            _is_int(grantee_oid)
            and (grantee is None or _is_str(grantee))
            and (grantor is None or _is_str(grantor))
            and _is_str(privilege)
            and _is_bool(grantable)
        ):
            raise VerificationMismatch
        entries.append((grantee_oid, grantee, grantor, privilege, grantable))
    # A duplicate identical entry is malformed on ANY row, whatever its identity
    # (architecture v3 5.2 step 3), so a malformed overload cannot slip through
    # because only the identity row reaches the ACL comparison.
    if len(set(entries)) != len(entries):
        raise VerificationMismatch
    return tuple(entries)


def _parse_row(row: object) -> _Observed:
    """Fail-closed parsing of EVERY row, whatever its identity (step 0)."""

    if not isinstance(row, Sequence) or isinstance(row, str | bytes):
        raise VerificationMismatch
    if len(row) != _COLUMN_COUNT:
        raise VerificationMismatch
    (oid, prokind, pronargs, arg_is_uuid, allargtypes_is_null, argmodes_all_in,
     nargdefaults_zero, rettype_is_uuid, proretset, lang_is_plpgsql, owner, prosecdef,
     provolatile, proparallel, proisstrict, proconfig, prosrc, proacl_is_null, acl) = row
    booleans = (arg_is_uuid, allargtypes_is_null, argmodes_all_in, nargdefaults_zero,
                rettype_is_uuid, proretset, lang_is_plpgsql, prosecdef, proisstrict,
                proacl_is_null)
    if not (
        _is_int(oid)
        and _is_str(prokind)
        and _is_int(pronargs)
        and all(_is_bool(flag) for flag in booleans)
        and _is_str(owner)
        and _is_str(provolatile)
        and _is_str(proparallel)
        and _is_str(prosrc)
    ):
        raise VerificationMismatch
    if proconfig is not None and (
        type(proconfig) is not list or not all(_is_str(entry) for entry in proconfig)
    ):
        raise VerificationMismatch
    return _Observed(
        oid=oid, prokind=prokind, pronargs=pronargs, arg_is_uuid=arg_is_uuid,
        allargtypes_is_null=allargtypes_is_null, argmodes_all_in=argmodes_all_in,
        nargdefaults_zero=nargdefaults_zero, rettype_is_uuid=rettype_is_uuid,
        proretset=proretset, lang_is_plpgsql=lang_is_plpgsql, owner=owner,
        prosecdef=prosecdef, provolatile=provolatile, proparallel=proparallel,
        proisstrict=proisstrict,
        proconfig=None if proconfig is None else tuple(proconfig),
        prosrc=prosrc, proacl_is_null=proacl_is_null, acl=_parse_acl(acl),
    )


def _body_digest(body: str) -> bytes:
    return hashlib.sha256(normalize_body(body).encode("utf-8")).digest()


def compare_installed_function(
    profile: InstalledStateProfile,
    *,
    schema_key: str,
    expected_config: Sequence[str],
    expected_body: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    """Pure comparison: no database access, no callbacks. Raises `VerificationMismatch`.

    `expected_config` and `expected_body` come from the unit's snapshot, rendered
    for `schema_key`. Everything else expected comes from `profile`.

    0. Every row is parsed strictly; a malformed row of ANY identity is a mismatch.
    1. Identity is `prokind == 'f'`, `pronargs == 1` and `arg_is_uuid`. Exactly one
       row must have it. Well-formed rows of another identity are ignored: there is
       no name-wide overload ban (R-B9.2(a)).
    2. Every other column of that row is asserted, including every selected boolean.
    """

    if type(profile) is not InstalledStateProfile:
        raise VerificationMismatch
    if type(schema_key) is not str or not SCHEMA_KEY_PATTERN.fullmatch(schema_key):
        raise VerificationMismatch
    if (
        type(expected_body) is not str
        or type(expected_config) not in (list, tuple)
        or not all(_is_str(entry) for entry in expected_config)
    ):
        raise VerificationMismatch
    # The query resolves exactly these to booleans; a profile expecting anything
    # else cannot be verified by it, so it fails closed.
    if (profile.prokind, profile.argument_types, profile.return_type, profile.language) != (
        "f", ("uuid",), "uuid", "plpgsql"
    ):
        raise VerificationMismatch

    parsed = [_parse_row(row) for row in rows]
    identity = [
        row for row in parsed
        if row.prokind == "f" and row.pronargs == 1 and row.arg_is_uuid
    ]
    if len(identity) != 1:
        raise VerificationMismatch
    row = identity[0]

    if not (
        row.allargtypes_is_null
        and row.argmodes_all_in
        and row.nargdefaults_zero
        and row.rettype_is_uuid
        and row.lang_is_plpgsql
    ):
        raise VerificationMismatch
    if (
        row.proretset is not profile.returns_set
        or row.owner != profile.owner
        or row.prosecdef is not profile.security_definer
        or row.provolatile != profile.volatility
        or row.proparallel != profile.parallel
        or row.proisstrict is not profile.strict
    ):
        raise VerificationMismatch
    if row.proconfig is None or row.proconfig != tuple(expected_config):
        raise VerificationMismatch
    if _body_digest(row.prosrc) != _body_digest(expected_body):
        raise VerificationMismatch
    if row.proacl_is_null:
        raise VerificationMismatch
    observed_acl = []
    for grantee_oid, grantee, grantor, privilege, grantable in row.acl:
        if grantee_oid == 0 or grantee is None or grantor is None:
            raise VerificationMismatch
        observed_acl.append((grantee, privilege, grantor, grantable))
    if sorted(observed_acl) != sorted(profile.installed_acl):
        raise VerificationMismatch
