"""UC1 part A (M02 minimal observation append): frozen test manifest and oracles.

Test-only code. It is exposed to test modules only through the `part_a` fixture
in `tests/m02/conftest.py`. It imports nothing from `haloflow` at module import
time, so the manifest and the oracle stay independent of the code under test.

Governing documents (Claude/Codex aligned, 2026-10-30):
- package v1 `f879df48...`, delta v1r1 `39c12d87...`;
- tenant-binding/canonicalization decision v1 `8ddd1fc1...`;
- Codex dispositions `...a970184c`, `...b84b2532`, `...95ae5bfd`.

Every literal below is FROZEN for review. A test that disagrees with the
implementation is reported, never silently edited to match it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# Roles (delta v1r1 Δ4; decision TB-3). Created by shared Alembic revision 005.
# ---------------------------------------------------------------------------

MIGRATOR: Final = "haloflow_migrator"
RUNTIME: Final = "haloflow_runtime"
APPEND_OWNER: Final = "haloflow_m02_append_owner"
PROJECTION_OWNER: Final = "haloflow_m02_projection_owner"
READ_OWNER: Final = "haloflow_m02_read_owner"
M05_OWNER: Final = "haloflow_m05_owner"
PART_A_ROLES: Final = (APPEND_OWNER, PROJECTION_OWNER, READ_OWNER, M05_OWNER)
REVISION_005: Final = "005_m02_part_a_roles"

# Migrator membership options for every part-A role (004 pattern).
MEMBERSHIP: Final = {"inherit": False, "set": True, "admin": False}

# ---------------------------------------------------------------------------
# Tenant units, in installation order (Δ4).
# ---------------------------------------------------------------------------

UNIT_ORDER: Final = (
    "t004_m02_reference",
    "t005_m02_events",
    "t006_m02_outbox",
    "t007_m02_envelope_allowlist",
    "t008_m02_part_a_gateways",
)

# ---------------------------------------------------------------------------
# Functions and EXECUTE ACL (package v1 §1; decision CN-5 + Codex 95ae5bfd).
# `executors` is the exact set of roles holding EXECUTE; PUBLIC is never one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    args: str
    owner: str
    executors: frozenset[str]
    security_definer: bool


FUNCTIONS: Final = (
    FunctionSpec("m02_append_observation", "jsonb", APPEND_OWNER, frozenset({RUNTIME}), True),
    FunctionSpec(
        "m02_enqueue_event_handoffs",
        "uuid, jsonb",
        PROJECTION_OWNER,
        frozenset({APPEND_OWNER}),
        True,
    ),
    FunctionSpec(
        "m02_get_observation_envelope",
        "uuid",
        READ_OWNER,
        frozenset({M05_OWNER}),
        True,
    ),
    # Call graph (Codex 95ae5bfd): the append body computes the content fingerprint
    # and the enqueue body computes the M11 key, so the private serializer is
    # executable by exactly those two owners. Owned by the append owner.
    FunctionSpec(
        "m02_canon_v1",
        "jsonb",
        APPEND_OWNER,
        frozenset({APPEND_OWNER, PROJECTION_OWNER}),
        False,
    ),
)
SEARCH_PATH_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"

# ---------------------------------------------------------------------------
# Tables and exact column grants.
# ---------------------------------------------------------------------------

# Physical batch v2 line 150, evidence INSERT column set E.
EVENT_INSERT_SET_E: Final = (
    "event_id",
    "operation_id",
    "patient_id",
    "event_type",
    "contract_version",
    "event_level",
    "status",
    "channel",
    "direction",
    "correlation_id",
    "causation_event_id",
    "provider",
    "provider_account",
    "external_id",
    "provider_event_id",
    "source_namespace",
    "source_event_id",
    "producer_dedupe_key",
    "content_fingerprint",
    "producer_service",
    "producer_version",
    "actor_id",
    "workload_id",
    "auth_method",
    "occurred_at",
    "source_time_quality",
    "received_at",
    "corrected_event_id",
    "correction_reason",
    "correction_scope",
    "failure_reason_code",
    "resolution_path_code",
    "metadata",
)
LINK_COLUMNS: Final = ("event_id", "link_type", "link_id", "relationship")

# Physical batch v2 §4 outbox INSERT set.
OUTBOX_INSERT_SET: Final = (
    "handoff_id",
    "tenant_id",
    "operation_id",
    "source_event_id",
    "handoff_kind",
    "dedupe_key",
    "payload",
    "state",
    "attempts",
    "available_at",
)

# Frozen reference/registry physical names (Codex 95ae5bfd: frozen here, not
# inferred from a not-yet-written t004). Type strings are pg format_type output.
REFERENCE_TABLES: Final[Mapping[str, tuple[tuple[str, str, bool], ...]]] = {
    "ref_event_levels": (
        ("code", "character varying(32)", True),
        ("ordinal", "smallint", True),
        ("is_active", "boolean", True),
    ),
    "ref_event_statuses": (
        ("code", "character varying(64)", True),
        ("status_scope", "character varying(16)", True),
        ("is_active", "boolean", True),
    ),
    "event_types": (
        ("event_type", "character varying(128)", True),
        ("action_family", "character varying(64)", True),
        ("is_active", "boolean", True),
    ),
}
# OI-007 §3 contract row plus the two reviewed physical extensions.
EVENT_CONTRACT_COLUMNS: Final = (
    "event_type",
    "version",
    "action_family",
    "event_level",
    "allowed_statuses",
    "terminality",
    "compatible_outcome_class",
    "precedence_rank",
    "required_fields",
    "required_links",
    "optional_links",
    "metadata_json_schema",
    "metadata_max_bytes",
    "authorized_producers",
    "required_provenance",
    "correction_policy",
    "projection_rule_id",
    "effective_from",
    "retired_at",
    "signal_rule",
    "value_constraints",
)
# Extension schemas (frozen; shapes follow synthetic-descriptors-v3 exactly):
# signal_rule jsonb NULL, else exactly {handoff_kind, signal_type} from the closed
# map; value_constraints jsonb NOT NULL, a closed object whose only permitted keys
# are operation_id:{must_be_null:bool} and synthetic_intent:{forbidden:bool}.
SIGNAL_RULE_KEYS: Final = ("handoff_kind", "signal_type")
SIGNAL_RULE_ALLOWED: Final = (("event_signal", "m02.event.appended"),)
VALUE_CONSTRAINT_SHAPE: Final[Mapping[str, tuple[str, ...]]] = {
    "operation_id": ("must_be_null",),
    "synthetic_intent": ("forbidden",),
}
FROZEN_CONTRACT_COLUMN_TYPES: Final = {
    "authorized_producers": ("character varying(64)[]", True),
    "signal_rule": ("jsonb", False),
    "value_constraints": ("jsonb", True),
}

APPEND_OWNER_SELECT: Final[Mapping[str, tuple[str, ...]]] = {
    "event_contracts": (
        "event_type",
        "version",
        "action_family",
        "event_level",
        "allowed_statuses",
        "required_fields",
        "required_links",
        "optional_links",
        "metadata_json_schema",
        "metadata_max_bytes",
        "authorized_producers",
        "projection_rule_id",
        "effective_from",
        "retired_at",
        "signal_rule",
        "value_constraints",
    ),
    "event_types": ("event_type", "is_active"),
    "ref_event_levels": ("code", "ordinal", "is_active"),
    "ref_event_statuses": ("code", "status_scope", "is_active"),
    "patient_events": (
        "event_id",
        "producer_service",
        "producer_dedupe_key",
        "content_fingerprint",
    ),
}
READ_OWNER_SELECT: Final[Mapping[str, tuple[str, ...]]] = {
    "patient_events": (
        "event_id",
        "event_type",
        "contract_version",
        "event_level",
        "status",
        "operation_id",
        "patient_id",
        "actor_id",
        "workload_id",
        "causation_event_id",
        "producer_dedupe_key",
        "metadata",
    ),
    "patient_event_links": LINK_COLUMNS,
    "m02_envelope_path_allowlist": (
        "consumer_module",
        "event_type",
        "contract_version",
        "json_path",
    ),
    "event_types": ("event_type", "is_active"),
}
PROJECTION_OWNER_OUTBOX_SELECT: Final = (
    "dedupe_key",
    "tenant_id",
    "operation_id",
    "source_event_id",
    "handoff_kind",
    "payload",
)
ALLOWLIST_COLUMNS: Final = ("consumer_module", "event_type", "contract_version", "json_path")

# Decision TB-3: shared grants to the projection owner only.
SHARED_TENANTS_COLUMNS_ALL: Final = (
    "tenant_id",
    "schema_key",
    "lifecycle_state",
    "schema_version",
    "display_reference",
    "created_at",
    "updated_at",
)
SHARED_TENANTS_GRANTED: Final = ("tenant_id", "schema_key", "lifecycle_state", "schema_version")
LIFECYCLE_STATES: Final = (
    "provisioning",
    "active",
    "suspended",
    "archival_pending",
    "archived",
    "decommissioned",
)
MSG_BINDING_ABSENT: Final = "tenant binding absent"
MSG_BINDING_INACTIVE: Final = "tenant binding inactive"
MSG_BINDING_MISMATCH: Final = "tenant binding mismatch"

# ---------------------------------------------------------------------------
# m02.canon/v1 oracle (decision CN-1..CN-6). Independent implementation; it
# deliberately does not use json.dumps.
# ---------------------------------------------------------------------------

MAX_SAFE_INT: Final = 2**53 - 1


class CanonReject(ValueError):
    """A value outside the m02.canon/v1 domain."""


_SHORT_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _canon_string(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if code == 0:
            raise CanonReject("U+0000")
        if 0xD800 <= code <= 0xDFFF:
            raise CanonReject("unpaired surrogate")
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


def _canon(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise CanonReject("integer out of range")
        return str(value)
    if isinstance(value, float):
        raise CanonReject("non-integral number")
    if isinstance(value, str):
        return _canon_string(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(_canon(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonReject("non-string key")
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(_canon_string(k) + ":" + _canon(value[k]) for k in keys) + "}"
    raise CanonReject(f"unsupported type {type(value).__name__}")


def canon_bytes(value: Any) -> bytes:
    return _canon(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Decision CN-6 vectors. `logical` is the Python input; `hex` and `sha256` are the
# independently authored expected bytes, which are the primary oracle.
@dataclass(frozen=True)
class Vector:
    name: str
    logical: Any
    hex: str
    sha256: str


CANON_VECTORS: Final = (
    Vector(
        "CV1",
        {"\ue000": 1, "\U0001f600": 2, "a": 3},
        "7b2261223a332c22f09f9880223a322c22ee8080223a317d",
        "84495633024b0992798250783e7ccb01ab2875649ab22f353f14c0d6a8389fc2",
    ),
    Vector(
        "CV2",
        {"t": 'a"b\\c/d\x01\x1f\x7f\b\t\n\f\r'},
        "7b2274223a22615c22625c5c632f645c75303030315c75303031667f5c625c745c6e5c665c72227d",
        "b7fac87da8eef6ed05357f9a79b742c6d18b45f86e5a49f28650d391b7ff927a",
    ),
    Vector(
        "CV3",
        {"n": ["\u00e9", "e\u0301"]},
        "7b226e223a5b22c3a9222c2265cc81225d7d",
        "95289fd5354f134113ba505e45ba37c52e9fda1fb0867f8e30aff86512ced307",
    ),
    Vector(
        "CV4",
        {"max": 9007199254740991, "min": -9007199254740991, "zero": 0},
        "7b226d6178223a393030373139393235343734303939312c226d696e223a2d39303037313939"
        "3235343734303939312c227a65726f223a307d",
        "b7b2401ddca2165824e98c61890c0aaec470258d3119dd265d02be9438bf47e6",
    ),
)
# Corrected CV0 (Codex 95ae5bfd): the SAME input as CV1 in code-point order,
# which a sort_keys regression would produce. Must differ from CV1.
CV0_CODEPOINT_ORDER_HEX: Final = "7b2261223a332c22ee8080223a312c22f09f9880223a327d"

# Additional type-coverage vectors (Codex 95ae5bfd: arrays/objects/scalars).
TYPE_VECTORS: Final = (
    Vector(
        "CT1",
        [None, True, False, 0, -1, "", [], {}],
        "5b6e756c6c2c747275652c66616c73652c302c2d312c22222c5b5d2c7b7d5d",
        "23ab67232ef55934f3798bd9536a8716e5594913aeed80bf142cc7d1731230a1",
    ),
    Vector(
        "CT2",
        {"b": {"d": [1, {"f": None}], "c": "x"}, "a": []},
        "7b2261223a5b5d2c2262223a7b2263223a2278222c2264223a5b312c7b2266223a6e756c6c7d5d7d7d",
        "517beec14b9de0b08e2f6e3759506af5ee13e57d1534956886d564a5f17c13e3",
    ),
)

# Reject vectors at the oracle/service layer (CN-1, CN-5).
CANON_REJECTS: Final = (
    ("int_above", 2**53),
    ("int_below", -(2**53)),
    ("fraction", 1.5),
    ("integral_float", 1.0),
    ("surrogate", "\ud800"),
    ("nul", "\x00"),
)
# Raw JSON texts the service raw-input layer must refuse before JSONB conversion.
RAW_INPUT_REJECTS: Final = (
    ("negative_zero", '{"x":-0}'),
    ("negative_zero_fraction", '{"x":-0.0}'),
    ("exponent", '{"x":1e2}'),
    ("fraction", '{"x":1.5}'),
    ("duplicate_key", '{"a":1,"a":2}'),
    # Codex 2a872210 point 4: raw-layer forms of the value rejects.
    ("integral_fraction", '{"x":1.0}'),
    ("surrogate_escape", '{"x":"' + chr(92) + 'ud800"}'),
    ("nul_escape", '{"x":"' + chr(92) + 'u0000"}'),
    ("duplicate_nested_key", '{"o":{"k":1,"k":1}}'),
)

# ---------------------------------------------------------------------------
# Key and fingerprint vectors (delta v1r1 Δ1-S, Δ2-F; decision CN).
# ---------------------------------------------------------------------------

M11_V1_PREIMAGE: Final = (
    "m02.handoff/v1",
    "M11",
    "pilot-clinic-01",
    "00000000-0000-4000-8000-000000000101",
    "event_signal",
    "m02.event.appended",
)
M11_V1_KEY: Final = "m02h1:b01fd55085c30e48af198da6df807dc1d513442edafeb52e3c6e8517f2eb16fd"
M11_V0_KEY_SUPERSEDED: Final = (
    "m02h1:cdffdba90c204f776dc72e6aff81f2dbcb280ba05fb8bd9278b58d8b48d862a0"
)
MK_V1_PREIMAGE: Final = (
    "m05.resolve/v1",
    "pilot-clinic-01",
    "00000000-0000-4000-8000-000000000201",
    "00000000-0000-4000-8000-000000000301",
)
MK_V1_KEY: Final = "m05r1:c1d4792e14070a96dca1f76be434c5105cd5a5ccd81cbd0c57d88d3223621253"
FP_V1_ARRAY: Final = (
    "m02.event-content/v1",
    "m05_followup_resolution_asserted",
    "1",
    "observation",
    "completed",
    None,
    "00000000-0000-4000-8000-000000000401",
    "00000000-0000-4000-8000-000000000101",
    "m05",
    "00000000-0000-4000-8000-000000000501",
    None,
    None,
    "absent",
    [["m05_followup_item_v1", "00000000-0000-4000-8000-000000000201", "case"]],
    # Deliberately NOT in canonical key order (Δ2 PA-26 permutation check).
    {"outcome_code": "cancelled_in_ehr", "item_kind": "cancel_request", "assertion_kind": "staff"},
)
FP_V1_SHA256: Final = "380ab482bbfb47914e8f6e72ec6fdde38ac7ea5c03ae81f55d84cfc7cdf3726a"

# Fingerprint exclusions (Δ2-F): attempt/transport fields.
FINGERPRINT_EXCLUDED: Final = (
    "event_id",
    "append_sequence",
    "created_at",
    "received_at",
    "correlation_id",
    "execution_id",
    "request_id",
    "auth_method",
    "producer_version",
    "producer_dedupe_key",
)


def m11_key(tenant_id: str, source_event_id: str, handoff_kind: str, signal_type: str) -> str:
    preimage = ["m02.handoff/v1", "M11", tenant_id, source_event_id, handoff_kind, signal_type]
    return "m02h1:" + sha256_hex(canon_bytes(preimage))


# ---------------------------------------------------------------------------
# Small catalogue helpers (admin connection string in, plain values out).
# ---------------------------------------------------------------------------


def role_exists(conn: Any, role: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,)
    ).fetchone()
    return bool(row[0])


def column_privilege(conn: Any, role: str, table: str, column: str, privilege: str) -> bool:
    row = conn.execute(
        "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s)", (role, table, column, privilege)
    ).fetchone()
    return bool(row[0])


def regclass_exists(conn: Any, qualified: str) -> bool:
    row = conn.execute("SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (qualified,)).fetchone()
    return bool(row[0])


# ---------------------------------------------------------------------------
# Pure ACL oracle (Codex 2a872210 point 1). Positive and negative controls in
# test_m02_part_a_canon_units.py exercise it on constructed ACL lists.
# ---------------------------------------------------------------------------


def function_acl_problems(
    acl: list[str] | None, owner: str, executors: frozenset[str]
) -> list[str]:
    """Problems with a pg_proc.proacl text[] against the intended EXECUTE set.

    `executors` may include `owner` (the manifest lists every role that must be
    able to execute). The non-owner entries must be exactly `executors - {owner}`,
    each `X` with no grant option; PUBLIC (empty grantee) must be absent; a NULL
    ACL (default PUBLIC EXECUTE) is a problem.
    """

    if acl is None:
        return ["NULL ACL grants PUBLIC EXECUTE by default"]
    problems: list[str] = []
    seen: set[str] = set()
    for entry in acl:
        grantee, _, rest = entry.partition("=")
        privileges = rest.split("/", 1)[0]
        if grantee == "":
            problems.append("PUBLIC holds a privilege")
            continue
        if grantee == owner:
            continue
        seen.add(grantee)
        if privileges != "X":
            problems.append(f"{grantee} has {privileges!r}, expected 'X'")
    expected = set(executors) - {owner}
    if seen != expected:
        problems.append(f"non-owner grantees {sorted(seen)} != {sorted(expected)}")
    return problems


# ---------------------------------------------------------------------------
# Tranche 2: frozen refusal contract, test profile and command builder.
# ---------------------------------------------------------------------------

# Every part-A gateway refusal is SQLSTATE P0001 with MESSAGE exactly the code
# (the t003 fixed-message pattern); 22023 is reserved for the tenant setting.
REFUSAL_SQLSTATE: Final = "P0001"
TENANT_SETTING_SQLSTATE: Final = "22023"
REFUSALS: Final = (
    "COMMAND_MALFORMED",
    "CONTRACT_UNSUPPORTED",
    "PRODUCER_NOT_AUTHORIZED",
    "ATTRIBUTION_INVALID",
    "REQUIRED_LINK_MISSING",
    "LINK_INVALID",
    "LINK_UNSUPPORTED",
    "DEDUPE_CONFLICT",
    "ENVELOPE_UNSUPPORTED",
    "HANDOFF_KEY_COLLISION",
    MSG_BINDING_ABSENT,
    MSG_BINDING_INACTIVE,
    MSG_BINDING_MISMATCH,
)

OBSERVATION_TYPE: Final = "m02_test_observation_received"
SUBJECT_RESOLVER: Final = "m02_test_subject_v1"
SOURCE_RESOLVER: Final = "m02_test_source_v1"
INACTIVE_RESOLVERS: Final = (
    "m02_test_provider_v1",
    "m02_test_business_v1",
    "m02_test_evidence_v1",
    "m02_test_case_v1",
)
TEST_PRODUCER: Final = "m02_test_producer"
TEST_PRODUCER_B: Final = "m02_test_producer_b"
# Allowlist primary key (frozen): all four columns.
ALLOWLIST_PRIMARY_KEY: Final = ALLOWLIST_COLUMNS
SYNTHETIC_SUBJECT_KIND: Final = "patient"
SYNTHETIC_SOURCE_CLASS: Final = "synthetic_inbound"

# Test-owned non-production profile (G1): applied by ADMIN after production
# provisioning; never part of src. Rendered with the tenant schema.
TEST_PROFILE_SQL: Final = """
CREATE TABLE {schema}.m02_test_link_targets (
    resolver_id varchar(64) NOT NULL,
    link_id uuid NOT NULL,
    subject_kind varchar(32),
    authorized_principal uuid,
    source_class varchar(32),
    PRIMARY KEY (resolver_id, link_id),
    CHECK ((subject_kind IS NOT NULL AND source_class IS NULL)
        OR (subject_kind IS NULL AND source_class IS NOT NULL))
);
ALTER TABLE {schema}.m02_test_link_targets OWNER TO haloflow_migrator;
GRANT SELECT (resolver_id, link_id, subject_kind, authorized_principal, source_class)
    ON {schema}.m02_test_link_targets TO haloflow_m02_append_owner;
-- Codex 36155328 point 4: activate ONLY the scoped observation type; every other
-- m02_test_ type stays inactive (D3 excluded routes).
UPDATE {schema}.event_types SET is_active = true
    WHERE event_type = 'm02_test_observation_received';
UPDATE {schema}.event_contracts
    SET authorized_producers = ARRAY['m02_test_producer', 'm02_test_producer_b']
    WHERE event_type = 'm02_test_observation_received';
UPDATE {schema}.m02_link_resolvers SET is_active = true
    WHERE resolver_id IN ('m02_test_subject_v1', 'm02_test_source_v1');
"""


def subject_target(
    link_id: str, *, kind: str = SYNTHETIC_SUBJECT_KIND, principal: str | None = None
) -> tuple[str, str, str, str | None, None]:
    return (SUBJECT_RESOLVER, link_id, kind, principal, None)


def source_target(
    link_id: str, *, source_class: str = SYNTHETIC_SOURCE_CLASS
) -> tuple[str, str, None, None, str]:
    return (SOURCE_RESOLVER, link_id, None, None, source_class)


def attribution(principal_id: str, *, kind: str = "workload") -> dict[str, Any]:
    return {
        "principal_kind": kind,
        "principal_id": principal_id,
        "auth_method": "test",
        "execution_id": "00000000-0000-4000-8000-00000000e001",
        "request_id": None,
        "purpose_code": "operations",
    }


def observation_command(
    *,
    event_id: str,
    dedupe_key: str,
    subject_id: str,
    source_id: str,
    principal_id: str,
    metadata: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Package v1 section 1.1 input for the synthetic observation contract."""

    command: dict[str, Any] = {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": OBSERVATION_TYPE,
        "contract_version": 1,
        "operation_id": None,
        "patient_id": None,
        "correlation_id": "00000000-0000-4000-8000-00000000c001",
        "causation_event_id": None,
        "producer_dedupe_key": dedupe_key,
        # Service-derived from the producer route, never caller payload (Delta2).
        "producer_service": TEST_PRODUCER,
        "occurred_at": None,
        "source_time_quality": "absent",
        "links": [
            {"link_type": SUBJECT_RESOLVER, "link_id": subject_id, "relationship": "subject"},
            {"link_type": SOURCE_RESOLVER, "link_id": source_id, "relationship": "source"},
        ],
        "metadata": metadata if metadata is not None else {"label_code": "reviewed"},
        "attribution": attribution(principal_id),
    }
    command.update(overrides)
    return command


COUNTED_TABLES: Final = ("patient_events", "patient_event_links", "event_handoff_outbox")
