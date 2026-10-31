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
from typing import Final

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

# Part A production checksums at main 2b18bf3 (measured 2026-10-31). Resolver (a)
# will change t004 and t009 in B4; that commit must change these pins together with
# the retained-installation compatibility case (Codex 20261031T190524Z condition).
PART_A_CHECKSUMS: Final = {
    "t004_m02_reference": "744f45e3c55ac386f2b32735e3ae22d6172bcf1466c838b90f7e8444aad37515",
    "t009_m02_append": "e00a81282bff7cd48721a8e70037a0c3b77c68c92c5d3305663b962628f2544c",
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
