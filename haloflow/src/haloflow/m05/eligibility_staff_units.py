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
EL3B_TARGET: Final = 125

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


_E4_OLD: Final = (
    "NOT ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '2'::jsonb\n"
    "                OR ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '1'::jsonb\n"
    "                    AND v_row.item_kind <> 'ehr_confirmation_check'))")
_E4_NEW: Final = (
    "NOT (CASE WHEN v_row.item_kind IN (" + _EL_KINDS_SQL + ")\n"
    "                THEN (v_env OPERATOR(pg_catalog.->) 'contract_version') = '3'::jsonb\n"
    "                ELSE ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '2'::jsonb\n"
    "                      OR ((v_env OPERATOR(pg_catalog.->) 'contract_version') = '1'::jsonb\n"
    "                          AND v_row.item_kind <> 'ehr_confirmation_check')) END)")


def _legacy_v4(body: str, mode: str) -> str:
    body = _replace_once(body, _E1_OLD, _E1_NEW)
    body = _replace_once(body, _E2_OLD, _E2_NEW)
    body = _replace_once(body, *_e3(mode))
    if mode == "apply":
        body = _replace_once(body, _E4_OLD, _E4_NEW)
    return body


_PREPARE_V4_LEGACY: Final = _legacy_v4(_PREPARE_V3_BODY, "prepare")
_APPLY_V4_LEGACY: Final = _legacy_v4(_APPLY_V3_BODY, "apply")


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

PREPARE_V4_BODY: Final = _PREPARE_V4_LEGACY
APPLY_V4_BODY: Final = _APPLY_V4_LEGACY

_FUNCTIONS: Final = (
    (PREPARE_V4_ID, "m05_prepare_action_v4", PREPARE_V4_BODY),
    (APPLY_V4_ID, "m05_apply_action_v4", APPLY_V4_BODY),
)

_UNITS: Final[dict[str, str | UnitDefinition]] = {
    MIGRATION_ID: T122_SQL,
    **{mid: _definition(name, "volatile", body, RUNTIME_ROLE) for mid, name, body in _FUNCTIONS},
    CLOSURE_ID: UnitDefinition(template="", kind=ACL_CLOSURE_KIND,
                               execution_role=M05_OWNER_ROLE),
}

EL3B_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_UNITS.items())))
EL3B_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    mid: _profile(name, "volatile", RUNTIME_ROLE) for mid, name, _body in _FUNCTIONS})
_RUNTIME_EXECUTE: Final = ((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),)
EL3B_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = tuple(
    AclAmendment(target_unit=unit, activated_by=CLOSURE_ID, removes=_RUNTIME_EXECUTE)
    for unit in ("t090_m05_prepare_action_v3", "t091_m05_apply_action_v3"))
