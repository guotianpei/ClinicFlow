"""CP2-1 typed-plan test seam -- the ONE place a test names a production symbol
that does not exist yet.

WHY THIS FILE EXISTS
--------------------
v11 declined to write the `B-*` rows "as imports of an absent module", and that
was right: an `ImportError` at collection takes every other case in the module
down with it and says nothing about which requirement is unmet. It also freezes a
guessed production name into dozens of assertions.

So the typed-plan cases are written against the VOCABULARY in this file, not
against production names. Every entry below states the capability a case needs.
Until the typed path exists, each one raises `InterfaceAbsent`, so every typed
case fails individually, at its own first use of the seam, naming the capability
it lacks. That is the case's pre-change state: RED / INTERFACE. It is recorded as
such and never read as a test verdict.

`support/serializer_adapter.py` is the precedent in this directory: a thin
dispatch layer, no oracle of its own.

TWO KINDS OF ENTRY -- amended per Codex's v12 review (item 5, Q-1)
------------------------------------------------------------------
v12 promised that every entry would later become "a direct production call or
attribute read". Codex: insufficient, because `alter`, `fabricate` and
`force_set` have no honest production counterpart, and adding production
mutation/fabrication APIs to make that promise true would create bypasses and
force the security design around freely mutable records. So the entries are
split, and each kind has its own rule.

(A) THIN PRODUCTION DISPATCH. `checker_seam`, `issuer_seam`, `consumer_seam`,
    `provenance_seam`, `binding_seam`, `is_typed`, `read`, `registry_identity`,
    and the constructor call inside `typed_definition`. At implementation each
    body becomes ONLY a direct call or attribute read of a production name. No
    branching, no computation, no expected values.

(B) REVIEWED TEST-ONLY OPERATIONS. `alter`, `fabricate`, `force_set`, the
    ABSENT/override translation inside `typed_definition`, and `digest_of`.
    Implemented HERE, in the test adapter, with generic Python mechanisms
    (`dataclasses.replace`, constructing a record with a fresh caller-made token,
    `object.__setattr__`, `hashlib`). They require NO production API; nothing is
    added to production code to make them possible. Their semantics are fixed
    below and frozen with the tests; their eventual bodies are reviewed with the
    binding diff, separately from the dispatch bindings.

Neither kind may contain a policy or validation oracle or an expected outcome. An
adapter that computed the right answer would let an implementation and its tests
agree by construction.

FROZEN SEMANTICS OF THE (B) OPERATIONS
--------------------------------------
typed_definition(payload, **overrides)
    Builds the production typed definition from the CP1 v3 payload's `template`,
    `execution_role`, `verification` and `policy`, declared typed. `migration_id`
    is the registry key and is not passed; `checksum_version` is the constant 3
    and is not passed. For each override `field=value`:
      value is ABSENT -> the field is OMITTED from the construction call;
      value is None   -> the field is passed as None;
      otherwise       -> the field is passed as `value`, unchanged.
    `kind=value` passes an explicit declared kind of exactly `value`. No other
    field is touched, so each mutation case is a single mutation.

alter(envelope, **fields)
    Returns a NEW envelope equal to `envelope` except in the named fields, with
    the SAME issuance identity (provenance preserved). `envelope` itself is not
    modified -- the binding cases assert this.

fabricate(envelope)
    Returns a NEW envelope whose visible fields all equal `envelope`'s, carrying
    an issuance identity the issuer never produced (a fresh caller-made object).
    It must therefore fail provenance. It never borrows the issuer's token.

force_set(envelope, field, value)
    Sets one field of an ISSUED envelope in place, bypassing immutability, to
    model a source changed after the executor's snapshot. It does not re-issue.

digest_of(sql_bytes)
    SHA-256 over exactly `sql_bytes`, lowercase hex. Independently specified
    here, never read from the implementation. It also states the representation
    the envelope's `byte_digest` is expected to use (README Q-7).

WHAT THE SEAM REQUIRES OF THE IMPLEMENTATION -- proposed, for review
--------------------------------------------------------------------
These are testability constraints on the design. They are listed here so they are
reviewed as architecture rather than discovered at implementation time.

  S-1  The CP1 checker is reached through ONE module attribute the runner looks
       up at call time, so a spy installed there observes every typed call.
  S-2  Issuance and consumption are separable callables the runner looks up at
       call time. A test intercepts the envelope between them. This is the same
       private-seam pattern `test_function_policy.py` uses on
       `_validate_local_statements`; it adds no production parameter.
  S-3  Provenance and binding verification are distinct callables, so the
       "internal phase oracle" v5.1 section 3.7 requires is observable: which one
       raised, not merely which public code came out.
  S-4  AMENDED. The TESTS must be able to replace an envelope field while
       preserving provenance, and to fabricate an envelope with identical visible
       fields and a caller-made token -- otherwise `B-checksum` and
       `B-provenance` cannot be told apart. This is met by the (B) operations
       above, NOT by any production API. The implementation is not required to
       expose any way to replace, fabricate or mutate an envelope; if generic
       mechanisms cannot reach its envelope, the (B) bodies are adapted and
       re-reviewed, and production code is not changed to accommodate them.
  S-5  Rendering goes through the renderer(s) named in `renderer_seams()`, so
       `B-rerender` can count calls.

Nothing in this file is imported by production code.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Final, NoReturn

from haloflow.m01.provisioning import function_policy, typed_plan, units

__all__ = [
    "ABSENT",
    "PLAN_FIELDS",
    "InterfaceAbsent",
    "alter",
    "binding_seam",
    "checker_seam",
    "consumer_seam",
    "digest_of",
    "fabricate",
    "force_set",
    "is_typed",
    "issuer_seam",
    "ordinary_definition",
    "provenance_seam",
    "read",
    "registry_identity",
    "renderer_seams",
    "typed_definition",
]


class InterfaceAbsent(Exception):
    """The typed-plan capability a case needs does not exist yet.

    Deliberately NOT an `AssertionError`: a case failing with this is RED /
    INTERFACE, which is a different pre-change state from RED / MISSING (the
    interface exists and the behaviour is wrong).
    """


def _absent(capability: str) -> NoReturn:
    raise InterfaceAbsent(f"typed-plan interface absent: {capability}")


class _Absent:
    """Sentinel: a declaration field left out entirely, as opposed to `None`."""

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _Absent()

# The logical envelope fields the binding cases name. v5.1 section 3.2, TP-R10 and
# TP-R11: registry, unit, schema, role and policy identity, the declaration
# checksum, the independently held byte digest, and the bytes themselves.
PLAN_FIELDS: Final = (
    "registry",
    "migration_id",
    "schema_key",
    "execution_role",
    "policy_version",
    "declaration_checksum",
    "byte_digest",
    "sql_bytes",
)


# ---------------------------------------------------------------------------
# Declaration and classification -- v5.1 section 3.1.
# ---------------------------------------------------------------------------


def typed_definition(payload: dict[str, Any], **overrides: Any) -> Any:
    """A definition that DECLARES itself typed, from a CP1 v3 payload.

    `payload` is the frozen CP1 shape (`migration_id` is the registry key and is
    not part of the definition). `overrides` replaces one declaration field for
    a mutation case: `execution_role=None` (TP-03), `policy=ABSENT` / `None` / a
    malformed value (TP-06/07/08), `kind=<value>` (TP-09). Every other field is
    carried unchanged, so each case is a single mutation.
    """

    fields: dict[str, Any] = {
        "template": payload["template"],
        "execution_role": payload["execution_role"],
        "policy_verification": payload["verification"],
        "policy": payload["policy"],
        "kind": units.TYPED_FUNCTION_KIND,
    }
    for name, value in overrides.items():
        if value is ABSENT:
            fields.pop(name, None)
        else:
            fields[name] = value
    return units.UnitDefinition(**fields)


def ordinary_definition(template: str, **typed_only: Any) -> Any:
    """An ordinary definition, optionally carrying typed-only fields (TP-09a).

    With no `typed_only` fields this is today's `UnitDefinition(template)` and is
    bound now: the ordinary route exists. With any typed-only field it needs the
    typed vocabulary to exist first.
    """

    if not typed_only:
        return units.UnitDefinition(template)
    return units.UnitDefinition(template, **typed_only)


def is_typed(unit: Any) -> bool:
    """Whether a COMPOSED unit is classified typed (TP-R2)."""

    # Codex, v15: coercion sits outside the thin attribute-read rule and would
    # hide a non-boolean from the frozen `is True` / `is False` assertions.
    return unit.is_typed


# ---------------------------------------------------------------------------
# Runner seams -- S-1, S-2, S-3, S-5.
# Each returns `(owner, attribute_name)` for `monkeypatch.setattr`.
# ---------------------------------------------------------------------------


def checker_seam() -> tuple[Any, str]:
    """Where the typed runner path looks up `validate_function_installation` (S-1).

    The function exists today; the runner does not call it (zero `src/` call
    sites, measured at `abc86e3` and unchanged at `621cba7`). The location the
    runner will call it through is the implementation's choice, so it is not
    bound here.
    """

    return (typed_plan, "validate_function_installation")


def issuer_seam() -> tuple[Any, str]:
    """The callable that issues an operation-local envelope (S-2, TP-R9)."""

    return (typed_plan, "issue_plan")


def consumer_seam() -> tuple[Any, str]:
    """The callable that verifies an envelope and yields bytes to execute (S-2)."""

    return (typed_plan, "consume_plan")


def provenance_seam() -> tuple[Any, str]:
    """The provenance check, phase 1 of v5.1 section 3.7 (S-3)."""

    return (typed_plan, "verify_provenance")


def binding_seam() -> tuple[Any, str]:
    """The binding / digest check, phase 3 of v5.1 section 3.7 (S-3)."""

    return (typed_plan, "verify_binding")


def renderer_seams() -> tuple[tuple[Any, str], ...]:
    """Every renderer a re-render could go through (S-5).

    The two that exist today are listed. The implementation may add one; if it
    does, it is added here, never removed.
    """

    return (
        (units.TenantMigrationUnit, "render"),
        (function_policy, "_render_exact_sql"),
    )


# ---------------------------------------------------------------------------
# Envelope access -- S-4. `field` is always one of PLAN_FIELDS.
# ---------------------------------------------------------------------------


def read(envelope: Any, field: str) -> Any:
    """One logical field of an issued envelope."""

    return getattr(envelope, field)


def alter(envelope: Any, **fields: Any) -> Any:
    """A copy with fields replaced and provenance PRESERVED (S-4).

    This is how a single binding mutation reaches the binding check without the
    provenance check refusing it first -- v5.1 section 5: "each mutation case
    leaves earlier provenance and NUL checks valid".
    """

    # (B) `dataclasses.replace` carries `token` across unchanged, so the copy
    # keeps the issuance identity and the case reaches the binding phase.
    return dataclasses.replace(envelope, **fields)


def fabricate(envelope: Any) -> Any:
    """A caller-constructed envelope with identical visible fields (S-4, TP-R12a)."""

    # (B) Same visible fields, a caller-made token the issuer never recorded.
    return dataclasses.replace(envelope, token=object())


def force_set(envelope: Any, field: str, value: Any) -> None:
    """Mutate an issued envelope IN PLACE, bypassing immutability (B-after-await).

    Models a source object changed after the executor took its snapshot. It is a
    test action, not something production code could do by accident.
    """

    # (B) Frozen is not immutable against a test that means it. No production
    # code path can do this, and none is added to allow it.
    object.__setattr__(envelope, field, value)


def digest_of(sql_bytes: bytes) -> str:
    """(B) SHA-256 over exactly `sql_bytes`, lowercase hex. Independently specified."""

    return hashlib.sha256(sql_bytes).hexdigest()


def registry_identity(registry: Any) -> Any:
    """The registry identity an envelope binds (TP-R10, `B-registry`)."""

    return registry
