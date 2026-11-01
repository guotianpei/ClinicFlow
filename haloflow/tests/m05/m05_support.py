"""UC1 part B (M05 staff follow-up): frozen manifest for the red tests.

Sources (aligned; nothing here is inferred from source under test):
- test/interface package v1 §2.1 (tables), §2.3 (functions), §3.2–3.6 (test ids);
- A-INV boundary decisions v1, function table (exact signatures, owners, executors,
  capabilities) and notes section (HMAC/AEAD columns);
- Part B kickoff v1 + Codex disposition 20261031T190524Z (B1 scope: ME-01..05,
  MX-02 catalogue half; resolver (a); production target advances only when every
  M05 route is installed, so B1..B4 install M05 through a TEST registry).

Expected values are literals. A test that disagrees with the implementation is a
finding for review, not something to edit to match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

M05_OWNER: Final = "haloflow_m05_owner"
MIGRATOR: Final = "haloflow_migrator"
RUNTIME: Final = "haloflow_runtime"
SEARCH_PATH_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    args: str  # regprocedure argument list
    returns: str
    capability: str
    mode: str  # statement mode: "write" | "read"


# A-INV decisions v1 function table. Owner haloflow_m05_owner, SECURITY DEFINER,
# EXECUTE to the runtime only, no PUBLIC; each is its own registered statement.
FUNCTIONS: Final = (
    FunctionSpec("m05_create_item", "jsonb", "jsonb", "m05.followup.create", "write"),
    FunctionSpec("m05_prepare_action", "jsonb", "jsonb", "m05.followup.prepare", "write"),
    FunctionSpec("m05_apply_action", "jsonb", "jsonb", "m05.followup.apply", "write"),
    FunctionSpec("m05_get_item", "uuid", "jsonb", "m05.followup.read", "read"),
    FunctionSpec("m05_list_items", "jsonb", "jsonb", "m05.followup.read", "read"),
    FunctionSpec("m05_get_history", "uuid", "jsonb", "m05.followup.read", "read"),
    FunctionSpec("m05_read_note", "uuid", "jsonb", "m05.followup.note.read", "read"),
)

# Package §2.1: owned by the migrator; no runtime DML or SELECT.
TABLES: Final = (
    "m05_followup_items",
    "m05_followup_history",
    "m05_followup_actions",
    "m05_followup_notes",
    "m05_intake_map",
    "m05_outcome_codes",
)

# Package §2.1 column sets (names only; types are asserted where the package fixes
# them). `create_digest`, `digest` are bytea(32); notes carry nonce/ciphertext and
# the AEAD key version (A-INV notes section).
COLUMNS: Final = {
    "m05_followup_items": (
        "item_id", "source_event_id", "item_kind", "patient_id", "state", "assignee_id",
        "outcome_code", "version", "create_digest", "created_at",
    ),
    "m05_followup_history": (
        "history_id", "item_id", "seq", "action", "actor_id", "before", "after",
        "resolution_event_id", "assertion_kind", "created_at",
    ),
    "m05_followup_actions": (
        "item_id", "client_action_id", "actor_id", "requested_action", "digest",
        "hmac_key_version", "event_id", "result_snapshot", "created_at",
    ),
    "m05_followup_notes": (
        "note_id", "item_id", "client_action_id", "nonce", "ciphertext", "enc_key_version",
        "created_at",
    ),
}

ITEM_KINDS: Final = ("no_reply", "reschedule_request", "cancel_request", "unclear_reply")
STATES: Final = ("open", "in_progress", "resolved")
HISTORY_ACTIONS: Final = (
    "created", "claim", "assign", "reassign", "start_progress", "resolve", "claim_and_resolve",
)

# W1 (owner decision): exact codes and display wording; `other` requires a note.
OUTCOME_CODES: Final = (
    ("rescheduled", "Rescheduled", False),
    ("cancelled_in_ehr", "Cancelled in EHR", False),
    ("reached_no_change", "Reached – no change", False),
    ("no_contact_left_message", "Couldn't reach – left message", False),
    ("no_contact_no_answer", "Couldn't reach – no answer", False),
    ("other", "Other", True),
)

# R10 / MX-02 (catalogue half): M05 stores no EHR state. A column whose name says it
# holds EHR or appointment state would contradict "staff assertion, never verified".
FORBIDDEN_COLUMN_FRAGMENTS: Final = ("ehr", "appointment_status", "emr")

# The M02 functions that exist after part A; M05's owner executes exactly one.
M02_FUNCTIONS: Final = (
    "m02_append_observation(jsonb)",
    "m02_enqueue_event_handoffs(uuid, jsonb)",
    "m02_get_observation_envelope(uuid)",
    "m02_canon_v1(jsonb)",
    "m02_lock_operation(uuid)",
)
M05_EXECUTABLE_M02: Final = ("m02_get_observation_envelope(uuid)",)

# Unit layout (Part B kickoff v1 delta 4): tables, seed, then one typed unit per
# function, all after part A's t011. Version = number of units.
M05_UNIT_IDS: Final = (
    "t012_m05_tables",
    "t013_m05_seed",
    "t014_m05_create_item",
    "t015_m05_prepare_action",
    "t016_m05_apply_action",
    "t017_m05_get_item",
    "t018_m05_list_items",
    "t019_m05_get_history",
    "t020_m05_read_note",
)
M05_TYPED_UNITS: Final = {
    "t014_m05_create_item": "m05_create_item",
    "t015_m05_prepare_action": "m05_prepare_action",
    "t016_m05_apply_action": "m05_apply_action",
    "t017_m05_get_item": "m05_get_item",
    "t018_m05_list_items": "m05_list_items",
    "t019_m05_get_history": "m05_get_history",
    "t020_m05_read_note": "m05_read_note",
}
M05_TEST_TARGET: Final = 20
M05_TEST_SUPPORTED: Final = range(M05_TEST_TARGET, M05_TEST_TARGET + 1)
# The production target is NOT advanced in B1..B4 (Codex 20261031T190524Z).
PRODUCTION_TARGET_UNTIL_B5: Final = 11

# Installed-state profile values for the typed M05 units (B1 proposal, Codex gate):
# plpgsql, SECURITY DEFINER, not strict, not set-returning, PARALLEL UNSAFE; the
# three write functions VOLATILE, the four read functions STABLE (as part A's t011
# envelope read). Exact ACL: the owner's own entry plus the runtime, both granted
# by the owner, neither grantable.
FUNCTION_VOLATILITY: Final = {
    "m05_create_item": "v",
    "m05_prepare_action": "v",
    "m05_apply_action": "v",
    "m05_get_item": "s",
    "m05_list_items": "s",
    "m05_get_history": "s",
    "m05_read_note": "s",
}
FUNCTION_ACL: Final = (
    (M05_OWNER, "EXECUTE", M05_OWNER, False),
    (RUNTIME, "EXECUTE", M05_OWNER, False),
)

# Statements (kickoff delta 5; key names are a B1 proposal for the Codex gate). One
# statement per function; keys unique; the three reads share m05.followup.read.
# Composed into production only in B5, with the target (Codex: no incomplete routes).
STATEMENTS: Final = {
    "m05.followup.create": ("write", "m05.followup.create", "SELECT m05_create_item(%(command)s)"),
    "m05.followup.prepare": (
        "write", "m05.followup.prepare", "SELECT m05_prepare_action(%(command)s)",
    ),
    "m05.followup.apply": ("write", "m05.followup.apply", "SELECT m05_apply_action(%(command)s)"),
    "m05.followup.get": ("read", "m05.followup.read", "SELECT m05_get_item(%(item_id)s)"),
    "m05.followup.list": ("read", "m05.followup.read", "SELECT m05_list_items(%(query)s)"),
    "m05.followup.history": ("read", "m05.followup.read", "SELECT m05_get_history(%(item_id)s)"),
    "m05.followup.note.read": (
        "read", "m05.followup.note.read", "SELECT m05_read_note(%(note_id)s)",
    ),
}

# m05_outcome_codes column names (B1 proposal; package §2.1 fixes the rows, not the
# column names).
OUTCOME_CODE_COLUMNS: Final = ("outcome_code", "display_label", "note_required")

# Part A production checksums. Pre-B4 (main 2b18bf3 / 6a81464) they were t004
# 744f45e3...7515 and t009 e00a8128...544c (kept in pre_b4_units.py). B4 resolver (a)
# changes both; this commit changes the pins together with the retained-installation
# cases COMPAT-01a/01b (Codex 20261031T190524Z condition; measured 2026-11-01).
PART_A_CHECKSUMS: Final = {
    "t004_m02_reference": "2c8e09c6f88bb771d1eb8f2065c531ae85e2fd443b3fa1083d50f3cbac432e4d",
    "t009_m02_append": "4ce106caba2b1b65c09ce6f20a96eb3135f603f7ec63e75b1e7fe66775fd81ad",
}
PART_A_UNIT_IDS: Final = (
    "t001_m01_baseline",
    "t002_m02_operation_registry",
    "t003_m02_lock_operation",
    "t004_m02_reference",
    "t005_m02_events",
    "t006_m02_outbox",
    "t007_m02_envelope_allowlist",
    "t008_m02_canon",
    "t009_m02_append",
    "t010_m02_enqueue",
    "t011_m02_envelope_read",
)

# B1 fail-closed stub refusal (Codex 20261031T192944Z item 5 / F-B1-02). Temporary:
# replaced function by function by behaviour rows in B2..B4.
STUB_SQLSTATE: Final = "P0001"
STUB_MESSAGE: Final = "M05_NOT_IMPLEMENTED"


# ---------------------------------------------------------------------------
# B2 (item creation). Codex ALIGN 20261031T233740Z on design delta v1
# (claude_uc1-part-b-b2-design-delta-v1, disposition codex_b2-design-delta-v1).
# ---------------------------------------------------------------------------

CREATE_ERROR_CODES: Final = (
    "COMMAND_MALFORMED", "SOURCE_NOT_ELIGIBLE", "KIND_MISMATCH", "DEDUPE_CONFLICT",
)

# Synthetic workload principals (Δ6). W1 is the mapped producer; W2 is unmapped;
# W3 is a producer the M02 test contract authorizes but no intake row names.
W1: Final = "00000000-0000-4000-8000-0000000b2001"
W2: Final = "00000000-0000-4000-8000-0000000b2002"
W3: Final = "00000000-0000-4000-8000-0000000b2003"
ACTOR: Final = "00000000-0000-4000-8000-0000000b2a01"

SOURCE_TYPE: Final = "m02_test_observation_received"

# Test-only intake profile, applied by the admin AFTER part A's TEST_PROFILE_SQL
# (which activates SOURCE_TYPE v1 and its two resolvers). Never in src; production
# intake stays empty. Two discriminator paths so a source can match two rows:
#   label_code  = 'reviewed' -> reschedule_request (W1)
#   channel_code = 'sms'     -> cancel_request     (W1)
# `label_code = 'unreviewed'` has NO row: the explicitly labelled STOP / unmapped
# stand-in (Part A has no STOP contract; this is not a STOP integration).
_SOURCE_SCHEMA_JSON: Final = (
    '{"type":"object","additionalProperties":false,"properties":{'
    '"label_code":{"enum":["reviewed","unreviewed"]},'
    '"channel_code":{"enum":["sms","voice"]}}}'
)
# The profile is rendered with str.format(schema=...): braces are doubled here.
_SOURCE_SCHEMA_TEMPLATE: Final = _SOURCE_SCHEMA_JSON.replace("{", "{{").replace("}", "}}")

B2_INTAKE_PROFILE_SQL: Final = f"""
UPDATE {{schema}}.event_contracts
   SET metadata_json_schema = '{_SOURCE_SCHEMA_TEMPLATE}'::jsonb
 WHERE event_type = '{SOURCE_TYPE}' AND version = 1;
INSERT INTO {{schema}}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
    ('m05', '{SOURCE_TYPE}', 1, 'label_code'),
    ('m05', '{SOURCE_TYPE}', 1, 'channel_code');
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
    ('{SOURCE_TYPE}', 1, 'label_code', 'reviewed', 'reschedule_request', '{W1}', false),
    ('{SOURCE_TYPE}', 1, 'channel_code', 'sms', 'cancel_request', '{W1}', false);
"""

# B4 test generation (package v2 5; red-gate PASS 397f5f9e, deviation D-5 reported):
# the units keep the resolution family, type and resolver (a) INACTIVE (production
# activation is B5); test tenants activate all three here.
B4_RESOLUTION_PROFILE_SQL: Final = """
UPDATE {schema}.ref_action_families SET is_active = true WHERE action_family = 'm05_followup';
UPDATE {schema}.event_types SET is_active = true
 WHERE event_type = 'm05_followup_resolution_asserted';
UPDATE {schema}.m02_link_resolvers SET is_active = true
 WHERE resolver_id = 'm05_followup_item_v1';
"""

# MC-05b tenant: the intake row exists but NO m05 allowlist row, so only the
# envelope's ENVELOPE_UNSUPPORTED can refuse (Codex: an independent tenant, not an
# uncommitted DELETE invisible to the runtime connection).
B2_NO_ALLOWLIST_PROFILE_SQL: Final = f"""
INSERT INTO {{schema}}.m05_intake_map (source_event_type, source_contract_version,
    discriminator_path, discriminator_value, item_kind, producer_workload_id,
    active_in_production) VALUES
    ('{SOURCE_TYPE}', 1, 'label_code', 'reviewed', 'reschedule_request', '{W1}', false);
"""

CREATE_RESULT_KEYS: Final = ("created_or_existing", "item_id", "schema_version")
CREATED_AFTER: Final = {"state": "open", "assignee_id": None, "outcome_code": None}

# Δ3 create digest: sha256 over compact UTF-8 JSON (no whitespace; separators ","
# and ":"; non-ASCII emitted as UTF-8, not \\u escapes) of
#   ["m05.create/v1", tenant_id, source_event_id, item_kind, principal_id, purpose_code]
# Independent literal vectors, computed with json.dumps(..., separators=(",", ":"),
# ensure_ascii=False) + hashlib.sha256 on 2026-10-31:
CREATE_DIGEST_VECTORS: Final = (
    (
        ("pilot-clinic-01", "00000000-0000-4000-8000-000000000101", "reschedule_request",
         "00000000-0000-4000-8000-000000000501", "operations"),
        '["m05.create/v1","pilot-clinic-01","00000000-0000-4000-8000-000000000101",'
        '"reschedule_request","00000000-0000-4000-8000-000000000501","operations"]',
        "da36bd27a59e06bea04467fa496709053f48d54e79c70101be3d243b02ae3717",
    ),
    (
        ("pilot-clinic-01", "00000000-0000-4000-8000-000000000101", "reschedule_request",
         "00000000-0000-4000-8000-000000000501", "treatment"),
        '["m05.create/v1","pilot-clinic-01","00000000-0000-4000-8000-000000000101",'
        '"reschedule_request","00000000-0000-4000-8000-000000000501","treatment"]',
        "6598b4e71bd4250dca32bf045aad090f6bda1ed3b1cd0f3499e863a60f1c132b",
    ),
)


def create_digest_preimage(
    tenant_id: str, source_event_id: str, item_kind: str, principal_id: str, purpose: str
) -> str:
    import json

    return json.dumps(
        ["m05.create/v1", tenant_id, source_event_id, item_kind, principal_id, purpose],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def create_digest(*parts: str) -> bytes:
    import hashlib

    return hashlib.sha256(create_digest_preimage(*parts).encode("utf-8")).digest()


# Δ5 exact grants to the M05 owner after B2. EXPLICIT ACL entries (relacl / attacl),
# distinct from EFFECTIVE privileges (a table-level INSERT makes every column
# effectively insertable without any attacl entry).
B2_TABLE_ACL: Final = {"m05_followup_history": ("INSERT",)}
B2_COLUMN_ACL: Final = {
    "m05_intake_map": {
        "SELECT": (
            "source_event_type", "source_contract_version", "discriminator_path",
            "discriminator_value", "item_kind", "producer_workload_id", "active_in_production",
        ),
    },
    "m05_followup_items": {
        "INSERT": COLUMNS["m05_followup_items"],
        "SELECT": ("item_id", "source_event_id", "item_kind", "create_digest"),
    },
}


# ---------------------------------------------------------------------------
# B3a (staff actions and reads, SQL). Design delta v1 (Δ1–Δ8), partial ALIGN by
# Codex 20261101T010812Z (codex_b3-design-delta-v1-disposition). MA-10's
# active/same-tenant staff check is an OPEN owner decision (Codex asked Rachel):
# NOT tested, NOT replaced and NOT claimed here.
# ---------------------------------------------------------------------------

ACTION_ERROR_CODES: Final = (
    "COMMAND_MALFORMED", "ITEM_NOT_FOUND", "NOT_AUTHORIZED", "ACTION_KEY_ACTOR_MISMATCH",
    "ACTION_KEY_CONFLICT", "ITEM_RESOLVED", "STALE_VERSION", "PRECONDITION_FAILED",
    "ACTION_NOT_IMPLEMENTED",
)
NON_RESOLVE_ACTIONS: Final = ("claim", "start_progress", "assign", "reassign")
# Reserved for B4. Canonical B3 shape: {"type": <one of these>, "outcome_code": <W1
# code>} exactly; anything else is COMMAND_MALFORMED before ACTION_NOT_IMPLEMENTED.
# B4 changes this shape under its own reviewed delta.
RESERVED_ACTIONS: Final = ("resolve", "claim_and_resolve")

# Synthetic staff principals (ACTOR) and their trusted permission sets.
S1: Final = "00000000-0000-4000-8000-0000000b3001"
S2: Final = "00000000-0000-4000-8000-0000000b3002"
SUP: Final = "00000000-0000-4000-8000-0000000b3003"
STAFF_PERMISSIONS: Final = {"read": True, "act": True, "supervise": False}
SUPERVISOR_PERMISSIONS: Final = {"read": True, "act": True, "supervise": True}

PREPARE_KEYS: Final = {
    "NEW": ("decision", "schema_version"),
    "REPLAY": ("decision", "schema_version", "snapshot"),
    "REFUSED": ("code", "decision", "schema_version"),
}
APPLY_KEYS: Final = ("outcome", "schema_version", "snapshot")
SNAPSHOT_KEYS: Final = (
    "action", "assignee_id", "client_action_id", "item_id", "outcome_code", "schema_version",
    "state", "version",
)
ITEM_VIEW_KEYS: Final = (
    "assignee_id", "created_at", "item_id", "item_kind", "outcome_code", "patient_id",
    "schema_version", "source_event_id", "state", "version",
)
HISTORY_ENTRY_KEYS: Final = (
    "action", "actor_id", "after", "assertion_kind", "before", "created_at", "note_id",
    "seq",
)  # B4: note_id on every entry (v2r1 aligned extension)
LIST_RESULT_KEYS: Final = ("items", "next", "schema_version")
# Timestamps leave M05 as UTC with microseconds and a literal Z.
TIMESTAMP_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
MAX_VERSION: Final = 2147483647
LIST_LIMIT_MAX: Final = 100

# Δ4 action digest: sha256 of compact UTF-8 JSON
#   ["m05.action/v1", tenant, item_id, client_action_id, actor_id, type,
#    assignee_id | null, expected_version (JSON integer)]
ACTION_DIGEST_VECTORS: Final = (
    (
        ("pilot-clinic-01", "00000000-0000-4000-8000-000000000201",
         "00000000-0000-4000-8000-000000000301", "00000000-0000-4000-8000-000000000501",
         "claim", None, 1),
        '["m05.action/v1","pilot-clinic-01","00000000-0000-4000-8000-000000000201",'
        '"00000000-0000-4000-8000-000000000301","00000000-0000-4000-8000-000000000501",'
        '"claim",null,1]',
        "24940bb25ae4ab01ee8dc4766ec5231a6be020a24ae31e6902f1cde13b732d7f",
    ),
    (
        ("pilot-clinic-01", "00000000-0000-4000-8000-000000000201",
         "00000000-0000-4000-8000-000000000302", "00000000-0000-4000-8000-000000000502",
         "reassign", "00000000-0000-4000-8000-000000000503", 3),
        '["m05.action/v1","pilot-clinic-01","00000000-0000-4000-8000-000000000201",'
        '"00000000-0000-4000-8000-000000000302","00000000-0000-4000-8000-000000000502",'
        '"reassign","00000000-0000-4000-8000-000000000503",3]',
        "0e92515105c59ccdafec0341c1900bffc6ca3654e1aa9a9fbb28f3e4cff40b0f",
    ),
)


def action_digest_preimage(
    tenant_id: str, item_id: str, client_action_id: str, actor_id: str, action_type: str,
    assignee_id: str | None, expected_version: int,
) -> str:
    import json

    return json.dumps(
        ["m05.action/v1", tenant_id, item_id, client_action_id, actor_id, action_type,
         assignee_id, expected_version],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def action_digest(*parts: Any) -> bytes:
    import hashlib

    return hashlib.sha256(action_digest_preimage(*parts).encode("utf-8")).digest()


# Δ6 (with Codex's least-privilege refinement: no UPDATE(outcome_code) until B4).
# These supersede the B2 sets: the grant rows assert CURRENT_* from B3a on.
B3_TABLE_ACL: Final = {"m05_followup_history": ("INSERT",)}
B3_COLUMN_ACL: Final = {
    "m05_intake_map": B2_COLUMN_ACL["m05_intake_map"],
    "m05_followup_items": {
        "INSERT": COLUMNS["m05_followup_items"],
        "SELECT": COLUMNS["m05_followup_items"],
        "UPDATE": ("state", "assignee_id", "version"),
    },
    "m05_followup_history": {
        "SELECT": tuple(c for c in COLUMNS["m05_followup_history"] if c != "resolution_event_id"),
    },
    "m05_followup_actions": {
        "INSERT": COLUMNS["m05_followup_actions"],
        "SELECT": ("item_id", "client_action_id", "actor_id", "digest", "result_snapshot"),
    },
}
# B4 (package v2 3.5 + rulings 4f51be67 / cc359617): the accepted grant delta.
B4_TABLE_ACL: Final = B3_TABLE_ACL
B4_COLUMN_ACL: Final = {
    "m05_outcome_codes": {"SELECT": ("outcome_code", "note_required")},
    "m05_intake_map": B2_COLUMN_ACL["m05_intake_map"],
    "m05_followup_items": {
        "INSERT": COLUMNS["m05_followup_items"],
        "SELECT": COLUMNS["m05_followup_items"],
        "UPDATE": ("state", "assignee_id", "version", "outcome_code"),
    },
    "m05_followup_history": {"SELECT": COLUMNS["m05_followup_history"]},
    "m05_followup_actions": {
        "INSERT": COLUMNS["m05_followup_actions"],
        "SELECT": ("item_id", "client_action_id", "actor_id", "digest", "result_snapshot",
                   "event_id", "hmac_key_version"),
    },
    "m05_followup_notes": {
        "INSERT": COLUMNS["m05_followup_notes"],
        "SELECT": COLUMNS["m05_followup_notes"],
    },
}
CURRENT_TABLE_ACL: Final = B4_TABLE_ACL
CURRENT_COLUMN_ACL: Final = B4_COLUMN_ACL

# Interim read-slice stage (Codex 20261101T013809Z): exactly B2 plus the read
# SELECT grants; NO items UPDATE and NO action-record grants. Asserted by the MB3-GI
# rows while assignment waits on the directory-first decision (Rachel, MA-10). When
# the action slice lands, those rows are REPLACED (declared), and CURRENT_* (the
# full B3 set above) must hold instead.
READ_SLICE_TABLE_ACL: Final = {"m05_followup_history": ("INSERT",)}
READ_SLICE_COLUMN_ACL: Final = {
    "m05_intake_map": B2_COLUMN_ACL["m05_intake_map"],
    "m05_followup_items": {
        "INSERT": COLUMNS["m05_followup_items"],
        "SELECT": COLUMNS["m05_followup_items"],
    },
    "m05_followup_history": {
        "SELECT": tuple(c for c in COLUMNS["m05_followup_history"] if c != "resolution_event_id"),
    },
}
