"""Eligibility checks E3b: per-tenant migration units t122..t125. SYNTHETIC ONLY.

Governing: architecture v1 section 5, v1r1 R6, v1r2 M4, v1r3 C1-C3; E3b interface pins v1 +
v1r1 (Codex ALIGN 74643295) with correction C-1 (the existing prepare / apply statement keys
are re-bound to v4); E3b red packet v1 + v1r1 (Codex PASS e35a2663).

- t122 (migrator SQL): the six OD-EL6 outcome codes join `m05_outcome_codes` (the item FK
  requires it); `m05_outcome_kinds` (kind x code, 48 rows); `m05_contact_codes` (OD-EL7,
  12 rows); `log_contact` in the history and action-request action CHECKs;
  `m05_contact_entries`; `m05_el_cases.closure_outcome`; the resolution contract v3 (eight
  kinds, W1 + OD-EL6 outcomes) and its allow-list. v1 / v2 rows unchanged.
- t123 / t124 `m05_prepare_action_v4` / `m05_apply_action_v4`: exact-count edits on the v3
  bodies (t090 / t091).
- t125 remove-only closure: runtime EXECUTE leaves t090 / t091.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m05.eligibility_case_units import _EVENT_SCHEMA, EL_KINDS, EVENT_TYPE, METADATA_KEYS
from haloflow.m05.eligibility_labels import CONTACT_LABELS, OUTCOME_LABELS
from haloflow.m05.eligibility_units import _in
from haloflow.m05.writeback_units import ITEM_KINDS

MIGRATION_ID: Final = "t122_m05_el_staff_tables"
PREPARE_V4_ID: Final = "t123_m05_prepare_action_v4"
APPLY_V4_ID: Final = "t124_m05_apply_action_v4"
CLOSURE_ID: Final = "t125_m05_action_v3_closure"
PROJECT_ID: Final = "t126_m05_el_project"
PROJECT_NAME: Final = "m05_el_project"
EL3B_TARGET: Final = 126  # green deviation G-4 (disclosed): + the D-1 projection helper

LEGACY_KINDS: Final = ITEM_KINDS            # the five kinds before E3a
HISTORICAL_KINDS: Final = LEGACY_KINDS[:4]  # resolution contract v1 kinds
ALL_KINDS: Final = (*LEGACY_KINDS, *EL_KINDS)
W1_CODES: Final = ("rescheduled", "cancelled_in_ehr", "reached_no_change",
                   "no_contact_left_message", "no_contact_no_answer", "other")
EL_CODES: Final = tuple(OUTCOME_LABELS)
CONTACT_CODES: Final = tuple(CONTACT_LABELS)
NOTE_REQUIRED_EL: Final = frozenset({"el_other"})

_ACTIONS_V4: Final = ("'claim', 'assign', 'reassign', 'start_progress', 'resolve', "
                      "'claim_and_resolve', 'log_contact'")


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_RESOLUTION_V3_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":["outcome_code","assertion_kind","item_kind"],'
    '"properties":{'
    '"outcome_code":{"enum":[' + ",".join(f'"{c}"' for c in (*W1_CODES, *EL_CODES)) + "]},"
    '"assertion_kind":{"enum":["staff"]},'
    '"item_kind":{"enum":[' + ",".join(f'"{k}"' for k in ALL_KINDS) + "]}"
    "}}"
)

T122_SQL: Final = (
    r"""
-- t122:step:outcome_codes
INSERT INTO {schema}.m05_outcome_codes (outcome_code, display_label, note_required) VALUES
"""
    + ",\n".join(f"    ('{code}', {_sql_text(label)}, "
                 f"{'true' if code in NOTE_REQUIRED_EL else 'false'})"
                 for code, label in OUTCOME_LABELS.items())
    + r""";

-- t122:step:outcome_kinds
CREATE TABLE {schema}.m05_outcome_kinds (
    item_kind varchar(32) NOT NULL CHECK (item_kind IN (@@ALL_KINDS@@)),
    outcome_code varchar(32) NOT NULL
        REFERENCES {schema}.m05_outcome_codes (outcome_code),
    PRIMARY KEY (item_kind, outcome_code)
);
INSERT INTO {schema}.m05_outcome_kinds (item_kind, outcome_code) VALUES
"""
    + ",\n".join(f"    ('{kind}', '{code}')"
                 for kind in ALL_KINDS
                 for code in (EL_CODES if kind in EL_KINDS else W1_CODES))
    + r""";

-- t122:step:contact_codes
CREATE TABLE {schema}.m05_contact_codes (
    item_kind varchar(32) NOT NULL CHECK (item_kind IN (@@EL_KINDS@@)),
    contact_code varchar(32) NOT NULL CHECK (contact_code ~ '^el_[a-z_]{1,29}$'),
    display_label varchar(64) NOT NULL,
    PRIMARY KEY (item_kind, contact_code)
);
INSERT INTO {schema}.m05_contact_codes (item_kind, contact_code, display_label) VALUES
"""
    + ",\n".join(f"    ('{kind}', '{code}', {_sql_text(label)})"
                 for kind in EL_KINDS for code, label in CONTACT_LABELS.items())
    + r""";

-- t122:step:actions
ALTER TABLE {schema}.m05_followup_history DROP CONSTRAINT m05_followup_history_action_check,
    ADD CONSTRAINT m05_followup_history_action_check
        CHECK (action IN ('created', @@ACTIONS@@));
ALTER TABLE {schema}.m05_followup_actions
    DROP CONSTRAINT m05_followup_actions_requested_action_check,
    ADD CONSTRAINT m05_followup_actions_requested_action_check
        CHECK (requested_action IN (@@ACTIONS@@));
CREATE TABLE {schema}.m05_contact_entries (
    history_id uuid PRIMARY KEY REFERENCES {schema}.m05_followup_history (history_id),
    contact_code varchar(32) NOT NULL CHECK (contact_code ~ '^el_[a-z_]{1,29}$')
);

-- t122:step:case_closure
ALTER TABLE {schema}.m05_el_cases
    ADD COLUMN closure_outcome varchar(32)
        REFERENCES {schema}.m05_outcome_codes (outcome_code),
    ADD CONSTRAINT m05_el_cases_closure_outcome CHECK (
        (state = 'closed') = (closure_outcome IS NOT NULL));

-- t122:step:resolution_contract_v3
INSERT INTO {schema}.event_contracts (
    event_type, version, action_family, event_level, allowed_statuses, terminality,
    compatible_outcome_class, precedence_rank, required_fields, required_links,
    optional_links, metadata_json_schema, metadata_max_bytes, authorized_producers,
    required_provenance, correction_policy, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints
) VALUES (
    'm05_followup_resolution_asserted', 3, 'm05_followup', 'observation',
    ARRAY['completed']::varchar(64)[], true, 'observation', 100,
    ARRAY['correlation_id']::varchar(64)[],
    '[["m05_followup_item_v1"]]'::jsonb,
    '[]'::jsonb,
    '@@RESOLUTION_SCHEMA@@'::jsonb,
    1024,
    ARRAY['m05_followup']::varchar(64)[],
    ARRAY[]::varchar(64)[],
    NULL, NULL, '2026-11-20T00:00:00Z', NULL,
    '{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}'::jsonb,
    '{"operation_id":{"must_be_null":true}}'::jsonb
);

-- t122:step:resolution_allowlist_v3
INSERT INTO {schema}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
    ('m05', 'm05_followup_resolution_asserted', 3, 'outcome_code'),
    ('m05', 'm05_followup_resolution_asserted', 3, 'assertion_kind'),
    ('m05', 'm05_followup_resolution_asserted', 3, 'item_kind');

-- t122:step:case_event_contract_v2
-- D-1 B1-b (Codex 85e37979): a synthetic-only contract version of the case evidence event
-- whose producer is the staff follow-up service, for the events a staff action projects. v1
-- (producer m05_intake, the timer path) is unchanged; metadata schema and allow-list as v1.
INSERT INTO {schema}.event_contracts (
    event_type, version, action_family, event_level, allowed_statuses, terminality,
    compatible_outcome_class, precedence_rank, required_fields, required_links,
    optional_links, metadata_json_schema, metadata_max_bytes, authorized_producers,
    required_provenance, correction_policy, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints
) VALUES (
    '@@CASE_EVENT@@', 2, 'm05_intake', 'observation',
    ARRAY['completed']::varchar(64)[], true, 'observation', 100,
    ARRAY['correlation_id']::varchar(64)[], '[]'::jsonb, '[]'::jsonb,
    '@@CASE_EVENT_SCHEMA@@'::jsonb, 1024, ARRAY['m05_followup']::varchar(64)[],
    ARRAY[]::varchar(64)[], NULL, NULL, '2026-11-20T00:00:00Z', NULL,
    '{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}'::jsonb,
    '{"operation_id":{"must_be_null":true}}'::jsonb
);
INSERT INTO {schema}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
@@CASE_EVENT_ALLOW@@;

-- t122:step:runtime_privileges
REVOKE ALL ON {schema}.m05_outcome_kinds, {schema}.m05_contact_codes,
    {schema}.m05_contact_entries FROM haloflow_runtime;

-- t122:step:grants
GRANT SELECT ON {schema}.m05_outcome_kinds, {schema}.m05_contact_codes
    TO haloflow_m05_owner;
GRANT SELECT, INSERT ON {schema}.m05_contact_entries TO haloflow_m05_owner;
GRANT UPDATE (closure_outcome) ON {schema}.m05_el_cases TO haloflow_m05_owner;
GRANT SELECT (outcome_code, note_required, display_label) ON {schema}.m05_outcome_codes
    TO haloflow_m05_owner;
"""
).replace("@@ALL_KINDS@@", _in(ALL_KINDS)).replace("@@EL_KINDS@@", _in(EL_KINDS)).replace(
    "@@ACTIONS@@", _ACTIONS_V4).replace("@@RESOLUTION_SCHEMA@@", _RESOLUTION_V3_SCHEMA).replace(
    "@@CASE_EVENT_SCHEMA@@", _EVENT_SCHEMA).replace("@@CASE_EVENT@@", EVENT_TYPE).replace(
    "@@CASE_EVENT_ALLOW@@", ",\n".join(f"    ('m05', '{EVENT_TYPE}', 2, '{k}')"
                                      for k in METADATA_KEYS))



# --- t123 / t124 v4 action gateways (exact-count edits on the v3 bodies) ------------------
#
# Legacy part (F1, F2; pins v1 section 3): for the five legacy kinds v4 behaves exactly as v3.
#   E1  pre-lock: the outcome code must exist in m05_outcome_codes (replaces the closed W1
#       IN list; the same COMMAND_MALFORMED);
#   E2  pre-lock: the note rule stays pre-lock ONLY for the W1 codes (v3 precedence);
#   E3  under the item lock: (item_kind, outcome_code) must be in m05_outcome_kinds
#       (COMMAND_MALFORMED), THEN the deferred note rule for the OD-EL6 codes
#       (NOTE_REQUIRED) - so legacy item + el_other without a note is COMMAND_MALFORMED;
#   E4  apply: resolution contract v1 only for the four historical kinds, v2 for the five
#       legacy kinds, v3 only for the eligibility kinds.

from haloflow.m05.gateways import _REFUSE_APPLY, _REFUSE_PREPARE  # noqa: E402
from haloflow.m05.writeback_units import (  # noqa: E402
    _APPLY_V3_BODY,
    _PREPARE_V3_BODY,
    _V3_CONTRACT_OK,
    _replace_once,
)

_W1_SQL: Final = _in(W1_CODES)
_EL_KINDS_SQL: Final = _in(EL_KINDS)


def _refuse(mode: str, code: str) -> str:
    return (_REFUSE_APPLY if mode == "apply" else _REFUSE_PREPARE).format(code=code)


_E1_OLD: Final = (
    "(v_action OPERATOR(pg_catalog.->>) 'outcome_code') NOT IN ('rescheduled',\n"
    "              'cancelled_in_ehr', 'reached_no_change', 'no_contact_left_message',\n"
    "              'no_contact_no_answer', 'other') THEN")
_E1_NEW: Final = (
    "NOT EXISTS (SELECT 1 FROM {schema}.m05_outcome_codes AS oc\n"
    "               WHERE oc.outcome_code OPERATOR(pg_catalog.=)\n"
    "                     (v_action OPERATOR(pg_catalog.->>) 'outcome_code')) THEN")
_E2_OLD: Final = "        IF v_note_required AND NOT v_note_present THEN"
_E2_NEW: Final = ("        -- E3b F1: pre-lock only for the W1 codes (v3 precedence).\n"
                  "        IF v_note_required AND NOT v_note_present\n"
                  "           AND v_outcome IN (" + _W1_SQL + ") THEN")


def _e3(mode: str) -> tuple[str, str]:
    old = ("     WHERE i.item_id = v_item\n"
           "       FOR UPDATE;\n"
           "    IF NOT FOUND THEN\n"
           "        " + _refuse(mode, "ITEM_NOT_FOUND") + "\n"
           "    END IF;\n")
    new = old + (
        "    -- E3b F1: the kind / code pair under the item lock, then the deferred note rule\n"
        "    -- of the OD-EL6 codes (a legacy item with el_other is COMMAND_MALFORMED).\n"
        "    IF v_type IN ('resolve', 'claim_and_resolve') THEN\n"
        "        PERFORM 1 FROM {schema}.m05_outcome_kinds AS ok\n"
        "         WHERE ok.item_kind OPERATOR(pg_catalog.=) v_row.item_kind\n"
        "           AND ok.outcome_code OPERATOR(pg_catalog.=) v_outcome;\n"
        "        IF NOT FOUND THEN\n"
        "            " + _refuse(mode, "COMMAND_MALFORMED") + "\n"
        "        END IF;\n"
        "        IF v_note_required AND NOT v_note_present THEN\n"
        "            " + _refuse(mode, "NOTE_REQUIRED") + "\n"
        "        END IF;\n"
        "    END IF;\n")
    return old, new


# E4 wraps the v3 predicate (writeback_units._V3_CONTRACT_OK) rather than restating it, so a
# change to v3 flows into the legacy branch instead of breaking this anchor (si V7, CI
# 44850997105). The ELSE branch is the v3 predicate re-indented by six spaces.
_E4_OLD: Final = "NOT " + _V3_CONTRACT_OK
_E4_NEW: Final = (
    "NOT (CASE WHEN v_row.item_kind IN (" + _EL_KINDS_SQL + ")\n"
    "                THEN (v_env OPERATOR(pg_catalog.->) 'contract_version') = '3'::jsonb\n"
    "                ELSE " + _V3_CONTRACT_OK.replace("\n", "\n      ") + " END)")


def _legacy_v4(body: str, mode: str) -> str:
    body = _replace_once(body, _E1_OLD, _E1_NEW)
    body = _replace_once(body, _E2_OLD, _E2_NEW)
    body = _replace_once(body, *_e3(mode))
    if mode == "apply":
        body = _replace_once(body, _E4_OLD, _E4_NEW)
    return body


_PREPARE_V4_LEGACY: Final = _legacy_v4(_PREPARE_V3_BODY, "prepare")
_APPLY_V4_LEGACY: Final = _legacy_v4(_APPLY_V3_BODY, "apply")


# --- E3b eligibility branch (D-1 option A; Codex 85e37979 / design ruling D-1, B1-b, B2) ----
#
# For the three eligibility kinds, on top of the legacy edits (pins v1 section 3 + v1r1 C1-C3):
#   G1  shape: resolve / claim_and_resolve / log_contact may carry `case_token`
#       {case_id, item_id, case_version} (closed shape, pre-lock); log_contact is
#       {type, contact_code} with a code that exists in m05_contact_codes (pre-lock).
#   G2  locator (F4 / C1): a tenant-scoped NON-LOCKING read of the item's kind and its OWN
#       case (from the item, never from the token); for an eligibility item the appointment
#       key, then that case, are locked BEFORE the item (membership was locked at 2b).
#   G3  under the item lock: the binding re-check (item <-> case <-> appointment; a broken
#       binding is an integrity fault, raised); token required for the three actions on an
#       eligibility item, refused on a legacy item; log_contact refused on a legacy kind
#       (M06) and kind-scoped against m05_contact_codes.
#   G4  replay digest m05.action/v3 for the bound actions (code, note, version and token).
#   G5  NEW-only: log_contact on open / in_progress items by an actor who may work the item
#       (the start_progress standing rule: unassigned, the assignee, or a supervisor).
#   G6  AFTER every refusal and the replay branch: the owner-only projection helper applies
#       every unapplied case event of the locked appointment (t120 rules) and returns their
#       evidence; then the token comparison (case_id, item_id, case_version after
#       projection). Prepare returns CASE_CHANGED with the refreshed t121 view and the
#       evidence (a committed business response; the service appends the evidence, then
#       commits); NEW carries the evidence too. Apply refuses CASE_CHANGED (defensive).
#   G7  apply: every applied case event of the appointment has its M02 evidence (else
#       CASE_EVIDENCE_MISSING, the transaction rolls back); close = case closed with
#       closure_outcome, key closure_watermark = next_seq - 1 (C2); log_contact = one history
#       row (before = after), one contact entry, item version and case_version unchanged.

_EL_TYPES_SQL: Final = "'resolve', 'claim_and_resolve', 'log_contact'"


def _raise(code: str) -> str:
    return f"RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{code}';"


_G0_OLD: Final = "    v_supervise boolean;\nBEGIN"
_G0_NEW: Final = (
    "    v_supervise boolean;\n"
    "    v_tok jsonb;\n"
    "    v_eltok boolean := false;\n"
    "    v_contact text;\n"
    "    v_loc record;\n"
    "    v_case_id uuid;\n"
    "    v_case_ref uuid;\n"
    "    v_crow record;\n"
    "    v_proj jsonb;\n"
    "    v_evidence jsonb := '[]'::jsonb;\n"
    "    v_case_version integer;\n"
    "BEGIN")

_G1A_OLD: Final = (
    "    v_type := v_action OPERATOR(pg_catalog.->>) 'type';\n"
    "    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys\n"
    "      FROM pg_catalog.jsonb_object_keys(v_action) AS k;\n")


def _g1a(mode: str) -> str:
    malformed = _refuse(mode, "COMMAND_MALFORMED")
    return (
        "    v_type := v_action OPERATOR(pg_catalog.->>) 'type';\n"
        "    -- E3b G1: the optional case_token of the bound actions (closed shape); its\n"
        "    -- presence is decided under the item lock, by the item's kind.\n"
        "    v_tok := NULL;\n"
        "    IF v_type IN (" + _EL_TYPES_SQL + ")\n"
        "       AND (v_action OPERATOR(pg_catalog.?) 'case_token') THEN\n"
        "        v_tok := v_action OPERATOR(pg_catalog.->) 'case_token';\n"
        "        IF pg_catalog.jsonb_typeof(v_tok) IS DISTINCT FROM 'object'\n"
        "           OR (SELECT pg_catalog.array_agg(k ORDER BY k)\n"
        "                 FROM pg_catalog.jsonb_object_keys(v_tok) AS k)\n"
        "              IS DISTINCT FROM ARRAY['case_id', 'case_version', 'item_id']::text[]\n"
        "           OR pg_catalog.jsonb_typeof(v_tok OPERATOR(pg_catalog.->) 'case_id')\n"
        "              IS DISTINCT FROM 'string'\n"
        "           OR (v_tok OPERATOR(pg_catalog.->>) 'case_id') OPERATOR(pg_catalog.!~) c_uuid\n"
        "           OR pg_catalog.jsonb_typeof(v_tok OPERATOR(pg_catalog.->) 'item_id')\n"
        "              IS DISTINCT FROM 'string'\n"
        "           OR (v_tok OPERATOR(pg_catalog.->>) 'item_id') OPERATOR(pg_catalog.!~) c_uuid\n"
        "           OR pg_catalog.jsonb_typeof(v_tok OPERATOR(pg_catalog.->) 'case_version')\n"
        "              IS DISTINCT FROM 'number'\n"
        "           OR (v_tok OPERATOR(pg_catalog.->) 'case_version')::text\n"
        "              OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,9}$'\n"
        "           OR ((v_tok OPERATOR(pg_catalog.->) 'case_version')::text)::bigint\n"
        "              > 2147483647 THEN\n"
        "            " + malformed + "\n"
        "        END IF;\n"
        "        v_action := v_action OPERATOR(pg_catalog.-) 'case_token'::text;\n"
        "    END IF;\n"
        "    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys\n"
        "      FROM pg_catalog.jsonb_object_keys(v_action) AS k;\n")


def _g1b(mode: str) -> tuple[str, str]:
    malformed = _refuse(mode, "COMMAND_MALFORMED")
    old = ("    ELSE\n        " + malformed + "\n    END IF;\n"
           "    v_perm := p_command OPERATOR(pg_catalog.->) 'permissions';\n")
    new = (
        "    ELSIF v_type = 'log_contact' THEN\n"
        "        -- E3b G1 (OD-EL7): {type, contact_code}; no note, no payload.\n"
        "        IF v_keys IS DISTINCT FROM ARRAY['contact_code', 'type']::text[]\n"
        "           OR pg_catalog.jsonb_typeof(v_action OPERATOR(pg_catalog.->) 'contact_code')\n"
        "              IS DISTINCT FROM 'string'\n"
        "           OR NOT EXISTS (SELECT 1 FROM {schema}.m05_contact_codes AS cc\n"
        "               WHERE cc.contact_code OPERATOR(pg_catalog.=)\n"
        "                     (v_action OPERATOR(pg_catalog.->>) 'contact_code')) THEN\n"
        "            " + malformed + "\n"
        "        END IF;\n"
        "        v_contact := v_action OPERATOR(pg_catalog.->>) 'contact_code';\n"
        + old)
    return old, new


_G2_OLD: Final = ("    -- 3. The item lock; unknown and other-tenant ids are identical"
                  " (schema-local).\n")
_G2_NEW: Final = (
    "    -- E3b G2 (F4 / v1r1 C1): the locator - a tenant-scoped NON-LOCKING read of the\n"
    "    -- item's kind and its OWN case, from the item (never from the token), used only to\n"
    "    -- choose locks. Lock order: membership (2b) -> appointment key -> that case -> item.\n"
    "    SELECT i.item_kind, c.case_id, c.appointment_ref INTO v_loc\n"
    "      FROM {schema}.m05_followup_items AS i\n"
    "      LEFT JOIN {schema}.m05_el_cases AS c ON c.item_id = i.item_id\n"
    "     WHERE i.item_id = v_item;\n"
    "    IF FOUND AND v_loc.item_kind IN (" + _EL_KINDS_SQL + ")\n"
    "       AND v_loc.case_id IS NOT NULL THEN\n"
    "        v_case_id := v_loc.case_id;\n"
    "        v_case_ref := v_loc.appointment_ref;\n"
    "        PERFORM 1 FROM {schema}.m05_el_appointments AS a\n"
    "         WHERE a.appointment_ref = v_case_ref\n"
    "           FOR UPDATE;\n"
    "        PERFORM 1 FROM {schema}.m05_el_cases AS c\n"
    "         WHERE c.case_id = v_case_id\n"
    "           FOR UPDATE;\n"
    "    END IF;\n\n" + _G2_OLD)


def _g3(mode: str) -> tuple[str, str]:
    malformed = _refuse(mode, "COMMAND_MALFORMED")
    old = "    -- 4. Current read+act, actor principals only (defence in depth; not\n"
    new = (
        "    -- E3b G3: the eligibility binding re-check under the locks (an integrity fault if\n"
        "    -- broken - the binding is immutable), then the token presence rule and the\n"
        "    -- kind-scoped contact code; on a legacy item a token or log_contact is malformed.\n"
        "    IF v_row.item_kind IN (" + _EL_KINDS_SQL + ") THEN\n"
        "        SELECT c.case_id, c.appointment_ref, c.item_id INTO v_crow\n"
        "          FROM {schema}.m05_el_cases AS c\n"
        "         WHERE c.case_id = v_case_id;\n"
        "        IF v_case_id IS NULL OR NOT FOUND\n"
        "           OR v_crow.item_id IS DISTINCT FROM v_item\n"
        "           OR v_crow.appointment_ref IS DISTINCT FROM v_case_ref THEN\n"
        "            " + _raise("CASE_BINDING_INVALID") + "\n"
        "        END IF;\n"
        "        IF v_type IN (" + _EL_TYPES_SQL + ") THEN\n"
        "            IF v_tok IS NULL THEN\n"
        "                " + malformed + "\n"
        "            END IF;\n"
        "            v_eltok := true;\n"
        "        END IF;\n"
        "        IF v_type = 'log_contact' THEN\n"
        "            PERFORM 1 FROM {schema}.m05_contact_codes AS cc\n"
        "             WHERE cc.item_kind OPERATOR(pg_catalog.=) v_row.item_kind\n"
        "               AND cc.contact_code OPERATOR(pg_catalog.=) v_contact;\n"
        "            IF NOT FOUND THEN\n"
        "                " + malformed + "\n"
        "            END IF;\n"
        "        END IF;\n"
        "    ELSIF v_tok IS NOT NULL OR v_type = 'log_contact' THEN\n"
        "        " + malformed + "\n"
        "    END IF;\n\n" + old)
    return old, new


_G4_OLD: Final = (
    "    IF v_type IN ('resolve', 'claim_and_resolve') THEN\n"
    "        -- Digest m05.action/v2 (package v2 2.1).\n")
_G4_NEW: Final = (
    "    IF v_eltok THEN\n"
    "        -- E3b G4: digest m05.action/v3 for the bound actions (outcome or contact code,\n"
    "        -- note, version and the token).\n"
    "        v_digest := pg_catalog.sha256(pg_catalog.convert_to(\n"
    "            '[\"m05.action/v3\",' || pg_catalog.to_json(v_setting)::text\n"
    "            || ',' || pg_catalog.to_json(v_item::text)::text\n"
    "            || ',' || pg_catalog.to_json(v_key::text)::text\n"
    "            || ',' || pg_catalog.to_json(v_actor::text)::text\n"
    "            || ',' || pg_catalog.to_json(v_type)::text\n"
    "            || ',' || COALESCE(pg_catalog.to_json(v_outcome)::text, 'null')\n"
    "            || ',' || COALESCE(pg_catalog.to_json(v_contact)::text, 'null')\n"
    "            || ',' || COALESCE(pg_catalog.to_json(v_note_hmac)::text, 'null')\n"
    "            || ',' || COALESCE(v_hmac_version::text, 'null')\n"
    "            || ',' || v_version::text\n"
    "            || ',' || pg_catalog.to_json(v_tok OPERATOR(pg_catalog.->>) 'case_id')::text\n"
    "            || ',' || pg_catalog.to_json(v_tok OPERATOR(pg_catalog.->>) 'item_id')::text\n"
    "            || ',' || (v_tok OPERATOR(pg_catalog.->) 'case_version')::text || ']',\n"
    "            'UTF8'));\n"
    "    ELSIF v_type IN ('resolve', 'claim_and_resolve') THEN\n"
    "        -- Digest m05.action/v2 (package v2 2.1).\n")


def _g5(mode: str) -> tuple[str, str]:
    old = "    ELSIF v_type = 'claim_and_resolve' THEN\n"
    new = (
        "    ELSIF v_type = 'log_contact' THEN\n"
        "        -- E3b G5 (v1r2 M4): open / in_progress (resolved was refused above), by an\n"
        "        -- actor who may work the item: unassigned, the assignee, or a supervisor.\n"
        "        IF v_row.state NOT IN ('open', 'in_progress') THEN\n"
        "            " + _refuse(mode, "PRECONDITION_FAILED") + "\n"
        "        END IF;\n"
        "        IF v_row.assignee_id IS NOT NULL AND v_row.assignee_id <> v_actor\n"
        "           AND NOT v_supervise THEN\n"
        "            " + _refuse(mode, "NOT_AUTHORIZED") + "\n"
        "        END IF;\n" + old)
    return old, new


def _g6(mode: str) -> tuple[str, str]:
    old = ("            'assignable')::boolean, false) THEN\n"
           "        " + _refuse(mode, "ASSIGNEE_NOT_ELIGIBLE") + "\n"
           "    END IF;\n")
    if mode == "prepare":
        changed = (
            "            RETURN pg_catalog.jsonb_build_object('schema_version', 1,\n"
            "                'decision', 'CASE_CHANGED',\n"
            "                'case', {schema}.m05_el_case_view(v_item),\n"
            "                'evidence', v_evidence);\n")
    else:
        changed = "            " + _raise("CASE_CHANGED") + "\n"
    new = old + (
        "\n"
        "    -- E3b G6: after every refusal and the replay branch - project, then compare.\n"
        "    IF v_eltok THEN\n"
        "        v_proj := {schema}.m05_el_project(pg_catalog.jsonb_build_object(\n"
        "            'appointment_ref', v_case_ref::text, 'case_id', v_case_id::text));\n"
        "        v_evidence := v_proj OPERATOR(pg_catalog.->) 'evidence';\n"
        "        SELECT c.case_version INTO v_case_version FROM {schema}.m05_el_cases AS c\n"
        "         WHERE c.case_id = v_case_id;\n"
        "        IF (v_tok OPERATOR(pg_catalog.->>) 'case_id')::uuid <> v_case_id\n"
        "           OR (v_tok OPERATOR(pg_catalog.->>) 'item_id')::uuid <> v_item\n"
        "           OR ((v_tok OPERATOR(pg_catalog.->) 'case_version')::text)::bigint\n"
        "              <> v_case_version THEN\n"
        + changed +
        "        END IF;\n"
        "    END IF;\n")
    return old, new


# prepare: NEW carries the evidence of the projected events (possibly none).
_G6P_RESOLVE_OLD: Final = (
    "                'source_event_id', v_row.source_event_id::text));\n"
    "    END IF;\n"
    "    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'NEW');\n")
_G6P_RESOLVE_NEW: Final = (
    "                'source_event_id', v_row.source_event_id::text))\n"
    "            OPERATOR(pg_catalog.||) CASE WHEN v_eltok\n"
    "                THEN pg_catalog.jsonb_build_object('evidence', v_evidence)\n"
    "                ELSE '{}'::jsonb END;\n"
    "    END IF;\n"
    "    IF v_eltok THEN\n"
    "        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'NEW',\n"
    "            'evidence', v_evidence);\n"
    "    END IF;\n"
    "    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'decision', 'NEW');\n")

# apply G7: the evidence check, the closure, the log_contact write.
_G7_EVIDENCE: Final = (
    "    IF v_eltok AND EXISTS (\n"
    "        SELECT 1 FROM {schema}.m05_el_case_events AS e\n"
    "         WHERE e.appointment_ref = v_case_ref AND e.applied_at IS NOT NULL\n"
    "           AND {schema}.m02_get_observation_envelope(e.evidence_event_id) IS NULL) THEN\n"
    "        " + _raise("CASE_EVIDENCE_MISSING") + "\n"
    "    END IF;\n")
_G7A_OLD: Final = (
    "    IF v_type IN ('resolve', 'claim_and_resolve') THEN\n"
    "        IF v_payload IS NULL THEN\n")
_G7A_NEW: Final = (
    "    -- E3b G7: every applied case event of the appointment has its M02 evidence.\n"
    + _G7_EVIDENCE + _G7A_OLD)
_G7B_OLD: Final = "        IF v_note_row IS NOT NULL THEN\n"
_G7B_NEW: Final = (
    "        IF v_eltok THEN\n"
    "            -- E3b G7: the close (C2: watermark = next_seq - 1 under the key lock).\n"
    "            UPDATE {schema}.m05_el_cases AS c\n"
    "               SET state = 'closed', closure_outcome = v_outcome,\n"
    "                   closed_at = pg_catalog.statement_timestamp()\n"
    "             WHERE c.case_id = v_case_id;\n"
    "            UPDATE {schema}.m05_el_appointments AS a\n"
    "               SET closure_watermark = a.next_seq OPERATOR(pg_catalog.-) 1\n"
    "             WHERE a.appointment_ref = v_case_ref;\n"
    "        END IF;\n" + _G7B_OLD)
_G7C_OLD: Final = "    -- 7. Writes: item, history (seq under the item lock), action record.\n"
_G7C_NEW: Final = (
    "    IF v_type = 'log_contact' THEN\n"
    "        -- E3b G7 (v1r2 M4): one history row (before = after), one contact entry; the\n"
    "        -- item version and the case_version are unchanged.\n"
    "        SELECT pg_catalog.max(h.seq) INTO v_seq\n"
    "          FROM {schema}.m05_followup_history AS h\n"
    "         WHERE h.item_id = v_item;\n"
    "        v_event := pg_catalog.gen_random_uuid();\n"
    "        INSERT INTO {schema}.m05_followup_history (history_id, item_id, seq, action,\n"
    "            actor_id, before, after, resolution_event_id, assertion_kind, created_at)\n"
    "        VALUES (v_event, v_item, COALESCE(v_seq, 0) + 1, v_type, v_actor,\n"
    "            pg_catalog.jsonb_build_object('state', v_row.state,\n"
    "                'assignee_id', v_row.assignee_id::text, 'outcome_code', v_row.outcome_code),\n"
    "            pg_catalog.jsonb_build_object('state', v_row.state,\n"
    "                'assignee_id', v_row.assignee_id::text, 'outcome_code', v_row.outcome_code),\n"
    "            NULL, NULL, pg_catalog.statement_timestamp());\n"
    "        INSERT INTO {schema}.m05_contact_entries (history_id, contact_code)\n"
    "        VALUES (v_event, v_contact);\n"
    "        v_snapshot := pg_catalog.jsonb_build_object('schema_version', 1,\n"
    "            'item_id', v_item::text, 'client_action_id', v_key::text, 'action', v_type,\n"
    "            'version', v_row.version, 'state', v_row.state,\n"
    "            'assignee_id', v_row.assignee_id::text, 'outcome_code', v_row.outcome_code,\n"
    "            'contact_code', v_contact);\n"
    "        INSERT INTO {schema}.m05_followup_actions (item_id, client_action_id, actor_id,\n"
    "            requested_action, digest, hmac_key_version, event_id, result_snapshot,\n"
    "            created_at)\n"
    "        VALUES (v_item, v_key, v_actor, v_type, v_digest, NULL, NULL, v_snapshot,\n"
    "            pg_catalog.statement_timestamp());\n"
    "        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',\n"
    "            'snapshot', v_snapshot);\n"
    "    END IF;\n" + _G7C_OLD)


def _eligibility_v4(body: str, mode: str) -> str:
    body = _replace_once(body, _G0_OLD, _G0_NEW)
    body = _replace_once(body, _G1A_OLD, _g1a(mode))
    body = _replace_once(body, *_g1b(mode))
    body = _replace_once(body, _G2_OLD, _G2_NEW)
    body = _replace_once(body, *_g3(mode))
    body = _replace_once(body, _G4_OLD, _G4_NEW)
    body = _replace_once(body, *_g5(mode))
    body = _replace_once(body, *_g6(mode))
    if mode == "prepare":
        body = _replace_once(body, _G6P_RESOLVE_OLD, _G6P_RESOLVE_NEW)
    else:
        body = _replace_once(body, _G7A_OLD, _G7A_NEW)
        body = _replace_once(body, _G7B_OLD, _G7B_NEW)
        body = _replace_once(body, _G7C_OLD, _G7C_NEW)
    return body


# --- t126 m05_el_project: the owner-only projection helper (D-1 option A) -------------------
#
# Called only from the v4 gateways (security definer, owner) with the appointment key and the
# target case already locked. Applies EVERY unapplied case event of the appointment in seq
# order by the t120 rules - the rule block below is t120's text verbatim (asserted at import)
# plus one inserted guard: before consuming an event, the appointment's open case must be the
# target case (the open-case invariant); otherwise CASE_INVARIANT_FAILED is raised before the
# event is consumed and the whole transaction fails. Returns the evidence of every projected
# event (incl. effect none) in the t120 evidence shape; the caller's service appends it.

from haloflow.m05 import gateways as _gw  # noqa: E402
from haloflow.m05.eligibility_case_units import _APPLY_BODY as _T120_BODY  # noqa: E402
from haloflow.m05.eligibility_case_units import _UTC_ISO  # noqa: E402

_RULES_START: Final = ("    SELECT * INTO v_case FROM {schema}.m05_el_cases AS c\n"
                       "     WHERE c.appointment_ref = v_ref AND c.state = 'open'\n"
                       "       FOR UPDATE;\n"
                       "    v_has_case := FOUND;\n")
_RULES_END: Final = ("    UPDATE {schema}.m05_el_case_events AS e\n"
                     "       SET applied_at = v_now, effect = v_effect,"
                     " evidence_event_id = v_event\n"
                     "     WHERE e.event_id = v_e.event_id;\n")


def _t120_rules() -> str:
    start = _T120_BODY.index(_RULES_START)
    end = _T120_BODY.index(_RULES_END) + len(_RULES_END)
    if _T120_BODY.count(_RULES_START) != 1 or _T120_BODY.count(_RULES_END) != 1:
        raise RuntimeError("t120 rule block anchors not unique")  # pragma: no cover
    return _T120_BODY[start:end]


_EVIDENCE_EXPR: Final = (
    "pg_catalog.jsonb_build_object('event_id', v_event::text, 'patient_id',"
    " v_k.patient_id::text, 'occurred_at', " + _UTC_ISO.format(x="v_e.created_at")
    + ", 'metadata', pg_catalog.jsonb_build_object("
    "'appointment_ref', v_ref::text, 'case_event_id', v_e.event_id::text,"
    " 'verification_id', v_vid::text, 'seq', v_e.seq,"
    " 'coverage_version', v_e.coverage_version, 'class', v_e.class,"
    " 'reason', v_e.reason, 'effect', v_effect,"
    " 'open_kind', COALESCE(v_kind, 'none'), 'record', v_record))")
if _EVIDENCE_EXPR not in _T120_BODY:  # pragma: no cover - parity guard (settlement identity)
    raise RuntimeError("projection evidence differs from t120")

_INVARIANT: Final = (
    "    -- D-1: the open-case invariant, before the event is consumed.\n"
    "    IF NOT v_has_case OR v_case.case_id <> v_target THEN\n"
    "        " + _raise("CASE_INVARIANT_FAILED") + "\n"
    "    END IF;\n")

PROJECT_BODY: Final = (
    r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_now timestamptz;
    v_ref uuid;
    v_target uuid;
    v_k record;
    v_e record;
    v_case record;
    v_has_case boolean;
    v_effect text;
    v_record text;
    v_vid uuid;
    v_event uuid;
    v_case_id uuid;
    v_kind text;
    v_out jsonb := '[]'::jsonb;
BEGIN""" + _gw._TENANT_CHECK + r"""
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.array_agg(k ORDER BY k)
             FROM pg_catalog.jsonb_object_keys(p_command) AS k)
          IS DISTINCT FROM ARRAY['appointment_ref', 'case_id']::text[]
       OR (p_command OPERATOR(pg_catalog.->>) 'appointment_ref') OPERATOR(pg_catalog.!~) c_uuid
       OR (p_command OPERATOR(pg_catalog.->>) 'case_id') OPERATOR(pg_catalog.!~) c_uuid THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    v_target := (p_command OPERATOR(pg_catalog.->>) 'case_id')::uuid;
    v_now := pg_catalog.clock_timestamp();
    -- The caller holds the key lock (re-taking it is a no-op in its transaction).
    SELECT * INTO v_k FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = v_ref
       FOR UPDATE;
    IF NOT FOUND THEN
        """ + _raise("CASE_BINDING_INVALID") + r"""
    END IF;
    LOOP
        SELECT * INTO v_e FROM {schema}.m05_el_case_events AS e
         WHERE e.appointment_ref = v_ref AND e.applied_at IS NULL
         ORDER BY e.seq
         LIMIT 1
           FOR UPDATE;
        EXIT WHEN NOT FOUND;
"""
    + _t120_rules().replace(_RULES_START, _RULES_START + _INVARIANT)
    + r"""
        IF v_effect = 'opened' THEN
            """ + _raise("CASE_INVARIANT_FAILED") + r"""
        END IF;
        v_out := v_out OPERATOR(pg_catalog.||) pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object('case_event_id', v_e.event_id::text,
                'effect', v_effect, 'evidence', """ + _EVIDENCE_EXPR + r"""));
    END LOOP;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'evidence', v_out);
END;
"""
)


# --- unit construction ---------------------------------------------------------------------

from haloflow.m01.provisioning.installed_state import AclAmendment  # noqa: E402
from haloflow.m01.provisioning.roles import RUNTIME_ROLE  # noqa: E402
from haloflow.m01.provisioning.units import (  # noqa: E402
    ACL_CLOSURE_KIND,
    UnitDefinition,
    UnitDefinitions,
)
from haloflow.m02.roles import M05_OWNER_ROLE  # noqa: E402
from haloflow.m05.reminder_units import _definition, _profile  # noqa: E402

PREPARE_V4_BODY: Final = _eligibility_v4(_PREPARE_V4_LEGACY, "prepare")
APPLY_V4_BODY: Final = _eligibility_v4(_APPLY_V4_LEGACY, "apply")

_FUNCTIONS: Final = (
    (PREPARE_V4_ID, "m05_prepare_action_v4", PREPARE_V4_BODY),
    (APPLY_V4_ID, "m05_apply_action_v4", APPLY_V4_BODY),
)

_UNITS: Final[dict[str, str | UnitDefinition]] = {
    MIGRATION_ID: T122_SQL,
    **{mid: _definition(name, "volatile", body, RUNTIME_ROLE) for mid, name, body in _FUNCTIONS},
    CLOSURE_ID: UnitDefinition(template="", kind=ACL_CLOSURE_KIND,
                               execution_role=M05_OWNER_ROLE),
    # D-1 option A: the owner-only projection helper (no runtime / PUBLIC EXECUTE).
    PROJECT_ID: _definition(PROJECT_NAME, "volatile", PROJECT_BODY, None),
}

EL3B_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_UNITS.items())))
EL3B_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _profile(name, "volatile", RUNTIME_ROLE) for mid, name, _body in _FUNCTIONS},
    PROJECT_ID: _profile(PROJECT_NAME, "volatile", None)})
_RUNTIME_EXECUTE: Final = ((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),)
EL3B_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = tuple(
    AclAmendment(target_unit=unit, activated_by=CLOSURE_ID, removes=_RUNTIME_EXECUTE)
    for unit in ("t090_m05_prepare_action_v3", "t091_m05_apply_action_v3"))
