"""The boundary between validating a typed unit and executing it (CP2-1).

WHAT THIS MODULE IS FOR
-----------------------
Before CP2-1, the runner rendered a template into a string and executed that
string. Nothing tied the bytes a checker blessed to the bytes that ran: the
frozen CP1 checker had zero call sites in `src/`, and its `sql_bytes` were never
consulted. This module is that tie.

An **authorized plan** is issued by `issue_plan` for one operation, one unit and
one tenant schema. It carries the exact bytes the checker bound, a digest of
those bytes, and the identity they were bound under. `consume_plan` verifies it
and hands back the bytes to execute -- and those bytes are the only thing the
runner may execute. There is no second render.

THREE PHASES, IN THIS ORDER (contract v5.1, 3.7)
------------------------------------------------
1. **provenance** -- was this envelope issued by this operation? A record the
   caller built, or one left over from a completed operation, is refused here.
   `INSTALL_PLAN_INVALID`.
2. **NUL** -- a NUL in the bound bytes. `INSTALL_NUL_FORBIDDEN`. Checked BEFORE
   the digest, because a NUL also changes the bytes, so a digest-first order
   would report a binding mismatch and hide the NUL.
3. **binding** -- do the bound identity, declaration checksum and byte digest
   match what the operation independently expects? `INSTALL_PLAN_INVALID`.

Phases 1 and 3 share one public code on purpose: which phase refused is an
internal distinction, not a tenant-facing one. They are separate functions so
that the distinction is observable where it matters -- in tests and in a
debugger -- without widening the public vocabulary.

WHAT THE TRUSTED STORE IS
-------------------------
`OperationExpectations` is created by the runner for one `apply` and never
escapes it. Expectations are recorded at issuance **from the unit and the
registry**, not from the envelope: an envelope that supplied its own
expectations would be checking itself (TP-R10a). The issuance token is a fresh
object compared by identity, so neither a copied record nor a replayed one
passes -- equal field values are not provenance (TP-R12a).

WHAT THIS MODULE DOES NOT DO
----------------------------
No database access, no rendering of its own, and no policy judgement: the frozen
CP1 checker remains the only thing that decides whether a declaration is
admissible. Nothing here mutates an envelope, and nothing here provides a way for
a caller to build one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.codes import PreconditionCode
from haloflow.m01.provisioning.function_policy import validate_function_installation
from haloflow.m01.provisioning.units import TenantMigrationRegistry, TenantMigrationUnit

__all__ = [
    "AuthorizedPlan",
    "OperationExpectations",
    "consume_plan",
    "issue_plan",
    "plan_digest",
    "verify_binding",
    "verify_provenance",
]


def _reject(code: PreconditionCode) -> MigrationUnitRejected:
    return MigrationUnitRejected(reason_code=code.value)


def plan_digest(sql_bytes: bytes) -> str:
    """SHA-256 over exactly these bytes, lowercase hex."""

    return hashlib.sha256(sql_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizedPlan:
    """Bytes authorized for one unit, in one schema, in one operation.

    Provenance is NOT the type: this record can be constructed, and being frozen
    or private would not change that. What cannot be forged is `token`, a fresh
    object the issuer keeps only in the operation's store and which provenance
    compares by identity. A caller-built record carries a token that store never
    issued, so it is refused -- which is why no construction sentinel is used
    here and none would add anything.
    """

    registry: TenantMigrationRegistry
    migration_id: str
    schema_key: str
    execution_role: str
    policy_version: int
    declaration_checksum: str
    byte_digest: str
    sql_bytes: bytes = field(repr=False)
    token: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.token is None:
            raise _reject(PreconditionCode.INSTALL_PLAN_INVALID)


@dataclass(slots=True)
class _Expectation:
    """What the operation independently expects for one unit."""

    registry: TenantMigrationRegistry
    migration_id: str
    schema_key: str
    execution_role: str
    policy_version: int
    declaration_checksum: str
    byte_digest: str
    token: object


@dataclass(slots=True)
class OperationExpectations:
    """One operation's trusted, operation-local store. Never handed to a caller."""

    _issued: list[_Expectation] = field(default_factory=list)

    def record(self, expectation: _Expectation) -> None:
        self._issued.append(expectation)

    def issued(self, token: object) -> _Expectation | None:
        """The expectation recorded for this exact token, by IDENTITY.

        Keyed by the token rather than by the migration id, because the id is one
        of the fields binding has to check: looking the expectation up by a field
        the envelope supplies would let a mutated id go unnoticed -- it would
        simply find nothing, and the wrong phase would refuse it.
        """

        for expectation in self._issued:
            if expectation.token is token:
                return expectation
        return None


def _payload_bytes(unit: TenantMigrationUnit) -> bytes:
    try:
        return json.dumps(
            unit.declaration_payload, ensure_ascii=False, allow_nan=False, sort_keys=True
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise _reject(PreconditionCode.INSTALL_POLICY_INVALID) from error


def validate_declaration(*, unit: TenantMigrationUnit, schema_key: str) -> Any:
    """Run the frozen CP1 checker for this typed unit and this schema.

    Separate from issuance on purpose: a declaration the policy refuses must
    never reach the issuer, so a policy refusal can never be mistaken for a plan
    refusal. The checker is reached through this module's own global, so one spy
    installed here sees every call -- from the runner, and from composition if it
    ever made one, which TP-R13 says it must not. Called once per pending typed
    unit; a skipped entry never reaches it (3.6).
    """

    if not unit.is_typed:
        raise _reject(PreconditionCode.INSTALL_POLICY_INVALID)
    return validate_function_installation(_payload_bytes(unit), schema_key=schema_key)


def issue_plan(
    *,
    unit: TenantMigrationUnit,
    schema_key: str,
    registry: TenantMigrationRegistry,
    store: OperationExpectations,
    result: Any,
) -> AuthorizedPlan:
    """Bind the checker's exact bytes to this operation, unit and schema.

    `result` is the checker's own result for this unit, from
    `validate_declaration`. Issuance records what the operation expects -- taken
    from the unit, the registry and that result -- and hands back an envelope
    carrying a token only this operation's store holds.
    """

    token = object()
    digest = plan_digest(result.sql_bytes)
    # Recorded from the unit, the registry and the checker's own result -- never
    # from the envelope, which is what the envelope is checked against.
    store.record(
        _Expectation(
            registry=registry,
            migration_id=unit.migration_id,
            schema_key=schema_key,
            execution_role=unit.execution_role or "",
            policy_version=result.policy_version,
            declaration_checksum=unit.checksum,
            byte_digest=digest,
            token=token,
        )
    )
    return AuthorizedPlan(
        registry=registry,
        migration_id=unit.migration_id,
        schema_key=schema_key,
        execution_role=unit.execution_role or "",
        policy_version=result.policy_version,
        declaration_checksum=unit.checksum,
        byte_digest=digest,
        sql_bytes=result.sql_bytes,
        token=token,
    )


def verify_provenance(plan: AuthorizedPlan, store: OperationExpectations) -> None:
    """Phase 1. Was this envelope issued, by this operation, for this unit?

    Compared by IDENTITY against the operation's own token. A fabricated record
    with identical fields fails here, and so does a genuine envelope from a
    completed operation.
    """

    if store.issued(plan.token) is None:
        raise _reject(PreconditionCode.INSTALL_PLAN_INVALID)


def verify_binding(plan: AuthorizedPlan, store: OperationExpectations) -> None:
    """Phase 3. Identity, declaration checksum and byte digest, against the store.

    The digest is recomputed from the bytes AND compared with the digest the
    store retained independently. Checking only that the envelope agrees with
    itself would accept bytes and digest substituted together.
    """

    expectation = store.issued(plan.token)
    if expectation is None:
        raise _reject(PreconditionCode.INSTALL_PLAN_INVALID)
    bound: tuple[Any, ...] = (
        id(plan.registry),
        plan.migration_id,
        plan.schema_key,
        plan.execution_role,
        plan.policy_version,
        plan.declaration_checksum,
        plan.byte_digest,
    )
    expected: tuple[Any, ...] = (
        id(expectation.registry),
        expectation.migration_id,
        expectation.schema_key,
        expectation.execution_role,
        expectation.policy_version,
        expectation.declaration_checksum,
        expectation.byte_digest,
    )
    if bound != expected or plan_digest(plan.sql_bytes) != expectation.byte_digest:
        raise _reject(PreconditionCode.INSTALL_PLAN_INVALID)


def consume_plan(plan: AuthorizedPlan, store: OperationExpectations) -> bytes:
    """Verify a plan and return the bytes to execute. The ONLY way to get them.

    The bytes are captured into a local before anything else, and every check
    below runs against that capture, so a source record mutated later -- across
    an await, after this returned -- cannot change what executes (TP-R9a). This
    function performs no I/O and no await of its own, and the runner calls it
    before it writes `running` (TP-R6).
    """

    sql_bytes = plan.sql_bytes
    verify_provenance(plan, store)
    if b"\x00" in sql_bytes:
        raise _reject(PreconditionCode.INSTALL_NUL_FORBIDDEN)
    verify_binding(plan, store)
    return sql_bytes
