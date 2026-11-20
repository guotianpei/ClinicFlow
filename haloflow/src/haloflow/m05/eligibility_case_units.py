"""Eligibility checks E3a: per-tenant migration units t118..t121. SYNTHETIC ONLY.

Composed ONLY into the synthetic registry behind `include_el3a` (with `include_el2`); the
production registry {36} and catalogue are unchanged. Governing: eligibility requirements
v1r1 (EL-10, EL-11, EL-13, EL-14, EL-14a); architecture v1 section 5, v1r1 R3/R4, v1r2 M2,
v1r3 C1; E3 sequencing v1 + Codex ALIGN (relay f14a103a); E3a red packet v1r1 (Codex red
gate PASS e4e7aadd, relay 20261120T013823Z).

- t118 (migrator SQL): the eight item kinds; the evidence event type
  `m05_eligibility_case_event` v1 (inactive until the E3a test profile) with its contract and
  allow-list; `m05_el_cases`; `m05_el_case_notes`; `evidence_event_id` / `effect` on
  `m05_el_case_events`.
- t119 `m05_create_item_v3`: the t089 body with the three eligibility kinds; an eligibility
  item must come from the opening evidence of an OPEN case of its kind and is recorded on that
  case in the same transaction.
- t120 `m05_el_apply` (timer workload): applies the lowest unapplied case event of one
  appointment (SKIP LOCKED on the key row) by the M2 / C1 rules and mints its evidence id.
- t121 `m05_el_case_view` (STABLE read): the case, its notes and the OD-EL4 summary only.

Lock order (extends E1/E2): the appointment KEY row, then its open case (t120), then the item
(t119 runs in the same transaction after the case exists). No other writer locks a case.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinition, UnitDefinitions
from haloflow.m05 import gateways as _gw
from haloflow.m05.eligibility_units import MANUAL_REASONS, _in
from haloflow.m05.eligibility_worker_units import _head, _out
from haloflow.m05.intake_units import _contract
from haloflow.m05.reminder_units import _definition, _profile
from haloflow.m05.writeback_units import _CREATE_V2_BODY, _KINDS_SQL, _replace_once

MIGRATION_ID: Final = "t118_m05_el_case_tables"
CREATE_V3_ID: Final = "t119_m05_create_item_v3"
APPLY_ID: Final = "t120_m05_el_apply"
VIEW_ID: Final = "t121_m05_el_case_view"
EL3A_TARGET: Final = 121

EVENT_TYPE: Final = "m05_eligibility_case_event"
EL_KINDS: Final = ("eligibility_inactive", "eligibility_inconclusive", "eligibility_manual")
KIND_BY_CLASS: Final = (("inactive", "eligibility_inactive"),
                        ("inconclusive", "eligibility_inconclusive"),
                        ("manual_required", "eligibility_manual"))
UNRESOLVED: Final = ("inactive", "inconclusive", "manual_required")
EFFECTS: Final = ("opened", "noted", "none")
RECORDS: Final = ("settlement", "finding", "note")
METADATA_KEYS: Final = ("appointment_ref", "case_event_id", "class", "coverage_version",
                        "effect", "open_kind", "reason", "record", "seq", "verification_id")
SUMMARY_FIELDS: Final = ("payer_name", "plan", "copay", "deductible_remaining",
                         "referral_required", "auth_required")

_ALL_KINDS_SQL: Final = _KINDS_SQL + ", " + _in(EL_KINDS)

# --- t118 migrator ---------------------------------------------------------------------

_EVENT_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"required":[' + ",".join(f'"{k}"' for k in METADATA_KEYS) + '],'
    '"properties":{'
    '"appointment_ref":{"type":"string"},'
    '"case_event_id":{"type":"string"},'
    '"verification_id":{},'
    '"seq":{"type":"integer"},'
    '"coverage_version":{"type":"integer"},'
    '"class":{"enum":["active","inactive","inconclusive","manual_required",null]},'
    '"reason":{"type":"string"},'
    '"effect":{"enum":[' + ",".join(f'"{e}"' for e in EFFECTS) + ']},'
    '"open_kind":{"enum":[' + ",".join(f'"{k}"' for k in (*EL_KINDS, "none")) + ']},'
    '"record":{"enum":[' + ",".join(f'"{r}"' for r in RECORDS) + ']}'
    '}}'
)

T118_SQL: Final = (
    r"""
-- t118:step:item_kinds
ALTER TABLE {schema}.m05_intake_map DROP CONSTRAINT m05_intake_map_item_kind_check,
    ADD CONSTRAINT m05_intake_map_item_kind_check CHECK (item_kind IN (@@KINDS@@));
ALTER TABLE {schema}.m05_followup_items DROP CONSTRAINT m05_followup_items_item_kind_check,
    ADD CONSTRAINT m05_followup_items_item_kind_check CHECK (item_kind IN (@@KINDS@@));

-- t118:step:event_contract
-- The evidence event type, INACTIVE (the E3a test profile activates it).
INSERT INTO {schema}.event_types (event_type, action_family, is_active) VALUES
    ('m05_eligibility_case_event', 'm05_intake', false);
"""
    + _contract(EVENT_TYPE, _EVENT_SCHEMA)
    + r"""
-- t118:step:event_allowlist
INSERT INTO {schema}.m02_envelope_path_allowlist
    (consumer_module, event_type, contract_version, json_path) VALUES
"""
    + ",\n".join(f"    ('m05', 'm05_eligibility_case_event', 1, '{k}')" for k in METADATA_KEYS)
    + r""";

-- t118:step:tables
CREATE TABLE {schema}.m05_el_cases (
    case_id uuid PRIMARY KEY,
    appointment_ref uuid NOT NULL REFERENCES {schema}.m05_el_appointments (appointment_ref),
    state varchar(8) NOT NULL CHECK (state IN ('open', 'closed')),
    item_kind varchar(32) NOT NULL CHECK (item_kind IN (@@EL_KINDS@@)),
    source_event_id uuid NOT NULL UNIQUE,
    item_id uuid UNIQUE,
    current_class varchar(16) NOT NULL CHECK (current_class IN (@@UNRESOLVED@@)),
    current_reason varchar(32) NOT NULL,
    case_version integer NOT NULL CHECK (case_version >= 1),
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    CONSTRAINT m05_el_cases_closed CHECK ((state = 'closed') = (closed_at IS NOT NULL))
);
CREATE UNIQUE INDEX m05_el_cases_open ON {schema}.m05_el_cases (appointment_ref)
    WHERE state = 'open';
CREATE TABLE {schema}.m05_el_case_notes (
    note_id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES {schema}.m05_el_cases (case_id),
    case_event_id uuid NOT NULL REFERENCES {schema}.m05_el_case_events (event_id),
    seq integer NOT NULL CHECK (seq >= 1),
    kind varchar(16) NOT NULL CHECK (kind IN ('open_or_note', 'note_only')),
    class varchar(16),
    reason varchar(32) NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, case_event_id)
);
ALTER TABLE {schema}.m05_el_case_events
    ADD COLUMN evidence_event_id uuid UNIQUE,
    ADD COLUMN effect varchar(8) CHECK (effect IS NULL OR effect IN ('opened', 'noted', 'none')),
    ADD CONSTRAINT m05_el_case_events_applied CHECK (
        (applied_at IS NULL) = (evidence_event_id IS NULL)
        AND (applied_at IS NULL) = (effect IS NULL));
CREATE INDEX m05_el_case_events_unapplied ON {schema}.m05_el_case_events
    (created_at, appointment_ref, seq) WHERE applied_at IS NULL;

-- t118:step:runtime_privileges
REVOKE ALL ON {schema}.m05_el_cases, {schema}.m05_el_case_notes FROM haloflow_runtime;

-- t118:step:grants
GRANT SELECT, INSERT, UPDATE (state, item_id, current_class, current_reason, case_version,
    closed_at) ON {schema}.m05_el_cases TO haloflow_m05_owner;
GRANT SELECT, INSERT ON {schema}.m05_el_case_notes TO haloflow_m05_owner;
GRANT UPDATE (applied_at, evidence_event_id, effect)
    ON {schema}.m05_el_case_events TO haloflow_m05_owner;
"""
).replace("@@KINDS@@", _ALL_KINDS_SQL).replace("@@EL_KINDS@@", _in(EL_KINDS)).replace(
    "@@UNRESOLVED@@", _in(UNRESOLVED))

# --- t119 create v3 ----------------------------------------------------------------------

_EL_CASE_CHECK: Final = (
    "    -- E3a: an eligibility item comes only from the opening evidence of an OPEN case of\n"
    "    -- its kind (locked; lock order appointment key -> case -> item).\n"
    "    IF v_kind IN (" + _in(EL_KINDS) + ") THEN\n"
    "        PERFORM 1 FROM {schema}.m05_el_cases AS c\n"
    "         WHERE c.source_event_id = v_source AND c.state = 'open'\n"
    "           AND c.item_kind = v_kind\n"
    "           FOR UPDATE;\n"
    "        IF NOT FOUND THEN\n"
    "            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SOURCE_NOT_ELIGIBLE';\n"
    "        END IF;\n"
    "    END IF;\n"
    "\n"
    "    -- 8. Digest (compact UTF-8 JSON preimage), then insert or dedupe."
)
_CREATE_CREATED_RETURN: Final = (
    "    RETURN pg_catalog.jsonb_build_object('schema_version', 1,\n"
    "        'item_id', v_item::text, 'created_or_existing', 'created');"
)
_CREATE_V3_BODY: Final = _replace_once(_replace_once(_replace_once(
    _CREATE_V2_BODY, f"({_KINDS_SQL})", f"({_ALL_KINDS_SQL})"),
    "    -- 8. Digest (compact UTF-8 JSON preimage), then insert or dedupe.", _EL_CASE_CHECK),
    _CREATE_CREATED_RETURN,
    "    -- E3a: a new eligibility item is recorded on its case (same transaction).\n"
    "    IF v_kind IN (" + _in(EL_KINDS) + ") THEN\n"
    "        UPDATE {schema}.m05_el_cases AS c SET item_id = v_item\n"
    "         WHERE c.source_event_id = v_source AND c.item_id IS NULL;\n"
    "    END IF;\n" + _CREATE_CREATED_RETURN)

# --- t120 apply --------------------------------------------------------------------------

_KIND_CASE: Final = ("CASE v_e.class " + " ".join(f"WHEN '{c}' THEN '{k}'"
                                                   for c, k in KIND_BY_CLASS) + " END")
_UTC_ISO: Final = ("pg_catalog.to_char(pg_catalog.timezone('UTC', {x}),"
                   " 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")

_APPLY_BODY: Final = _head(r"""
    v_now timestamptz;
    v_ref uuid;
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
    v_tries integer := 0;""", ("attribution", "schema_version")) + r"""
    -- The appointment whose lowest unapplied event is oldest; its KEY row locked, skipping
    -- appointments another applier holds (M2 serialization point).
    LOOP
        v_tries := v_tries OPERATOR(pg_catalog.+) 1;
        SELECT a.appointment_ref INTO v_ref
          FROM {schema}.m05_el_case_events AS e
          JOIN {schema}.m05_el_appointments AS a ON a.appointment_ref = e.appointment_ref
         WHERE e.applied_at IS NULL
         ORDER BY e.created_at, e.appointment_ref, e.seq
         LIMIT 1
           FOR UPDATE OF a SKIP LOCKED;
        IF NOT FOUND THEN
            RETURN """ + _out(outcome="'idle'") + r""";
        END IF;
        SELECT * INTO v_k FROM {schema}.m05_el_appointments AS a
         WHERE a.appointment_ref = v_ref;
        -- Only the LOWEST unapplied seq of this appointment (re-read under the key lock).
        SELECT * INTO v_e FROM {schema}.m05_el_case_events AS e
         WHERE e.appointment_ref = v_ref AND e.applied_at IS NULL
         ORDER BY e.seq
         LIMIT 1
           FOR UPDATE;
        EXIT WHEN FOUND;
        IF v_tries >= 3 THEN
            RETURN """ + _out(outcome="'idle'") + r""";
        END IF;
    END LOOP;

    SELECT * INTO v_case FROM {schema}.m05_el_cases AS c
     WHERE c.appointment_ref = v_ref AND c.state = 'open'
       FOR UPDATE;
    v_has_case := FOUND;
    v_event := pg_catalog.gen_random_uuid();
    v_case_id := NULL;
    v_kind := NULL;
    IF v_e.coverage_version < v_k.coverage_version THEN
        v_effect := 'none';                          -- rule 2: an older coverage version
    ELSIF v_e.seq <= v_k.closure_watermark THEN
        v_effect := 'none';                          -- rule 3: staff already closed over it
    ELSIF v_has_case THEN
        v_effect := 'noted';                         -- rule 4: a note on the open case
        v_case_id := v_case.case_id;
        INSERT INTO {schema}.m05_el_case_notes (note_id, case_id, case_event_id, seq, kind,
            class, reason, created_at)
        VALUES (pg_catalog.gen_random_uuid(), v_case.case_id, v_e.event_id, v_e.seq, v_e.kind,
            v_e.class, v_e.reason, v_now);
        UPDATE {schema}.m05_el_cases AS c
           SET case_version = c.case_version OPERATOR(pg_catalog.+) 1,
               current_class = CASE WHEN v_e.class IN (""" + _in(UNRESOLVED) + r""")
                                    THEN v_e.class ELSE c.current_class END,
               current_reason = CASE WHEN v_e.class IN (""" + _in(UNRESOLVED) + r""")
                                     THEN v_e.reason ELSE c.current_reason END
         WHERE c.case_id = v_case.case_id;
    ELSIF v_e.class IN (""" + _in(UNRESOLVED) + r""") THEN
        v_effect := 'opened';                        -- rule 5: a new case
        v_case_id := pg_catalog.gen_random_uuid();
        v_kind := """ + _KIND_CASE + r""";
        INSERT INTO {schema}.m05_el_cases (case_id, appointment_ref, state, item_kind,
            source_event_id, item_id, current_class, current_reason, case_version, opened_at,
            closed_at)
        VALUES (v_case_id, v_ref, 'open', v_kind, v_event, NULL, v_e.class, v_e.reason, 1,
            v_now, NULL);
        INSERT INTO {schema}.m05_el_case_notes (note_id, case_id, case_event_id, seq, kind,
            class, reason, created_at)
        VALUES (pg_catalog.gen_random_uuid(), v_case_id, v_e.event_id, v_e.seq, v_e.kind,
            v_e.class, v_e.reason, v_now);
    ELSE
        v_effect := 'none';                          -- rule 5: active or note-only, no case
    END IF;

    -- The settlement discriminator (v1r1): only a verification settlement carries one.
    v_vid := NULL;
    IF v_e.kind = 'note_only' THEN
        v_record := 'note';
    ELSIF v_e.verification_id IS NOT NULL THEN
        v_record := 'settlement';
        v_vid := v_e.verification_id;
    ELSE
        IF v_e.class = 'manual_required' AND v_e.reason IN (""" + _in(MANUAL_REASONS) + r""") THEN
            -- An E1 manual settlement: its booking-owned verification of this version.
            SELECT v.verification_id INTO v_vid FROM {schema}.m05_el_verifications AS v
             WHERE v.appointment_ref = v_ref AND v.coverage_version = v_e.coverage_version
               AND v.owner_trigger = 'booking' AND v.state = 'settled'
               AND v.result_class = 'manual_required';
        END IF;
        v_record := CASE WHEN v_vid IS NULL THEN 'finding' ELSE 'settlement' END;
    END IF;

    UPDATE {schema}.m05_el_case_events AS e
       SET applied_at = v_now, effect = v_effect, evidence_event_id = v_event
     WHERE e.event_id = v_e.event_id;

    RETURN """ + _out(
    outcome="'applied'", effect="v_effect", case_event_id="v_e.event_id::text",
    appointment_ref="v_ref::text", seq="v_e.seq", case_id="v_case_id::text",
    item_kind="v_kind",
    evidence="pg_catalog.jsonb_build_object('event_id', v_event::text, 'patient_id',"
             " v_k.patient_id::text, 'occurred_at', " + _UTC_ISO.format(x="v_e.created_at")
             + ", 'metadata', pg_catalog.jsonb_build_object("
             "'appointment_ref', v_ref::text, 'case_event_id', v_e.event_id::text,"
             " 'verification_id', v_vid::text, 'seq', v_e.seq,"
             " 'coverage_version', v_e.coverage_version, 'class', v_e.class,"
             " 'reason', v_e.reason, 'effect', v_effect,"
             " 'open_kind', COALESCE(v_kind, 'none'), 'record', v_record))") + r""";
END;
"""

# --- t121 case view ----------------------------------------------------------------------

_NOT_FOUND: Final = ("RETURN pg_catalog.jsonb_build_object('schema_version', 1,"
                     " 'error', 'ITEM_NOT_FOUND');")

_VIEW_BODY: Final = r"""
DECLARE
    v_setting text;
    v_item record;
    v_case record;
    v_vid uuid;
    v_answer jsonb;
    v_summary jsonb;
    v_notes jsonb;
BEGIN""" + _gw._TENANT_CHECK + r"""
    SELECT i.item_id, i.item_kind INTO v_item FROM {schema}.m05_followup_items AS i
     WHERE i.item_id = p_item_id AND i.item_kind IN (""" + _in(EL_KINDS) + r""");
    IF NOT FOUND THEN
        """ + _NOT_FOUND + r"""
    END IF;
    SELECT * INTO v_case FROM {schema}.m05_el_cases AS c WHERE c.item_id = p_item_id;
    IF NOT FOUND THEN
        """ + _NOT_FOUND + r"""
    END IF;
    -- The latest applied payer finding of this case that carries a verification.
    SELECT e.verification_id INTO v_vid
      FROM {schema}.m05_el_case_notes AS n
      JOIN {schema}.m05_el_case_events AS e ON e.event_id = n.case_event_id
     WHERE n.case_id = v_case.case_id AND e.verification_id IS NOT NULL
     ORDER BY n.seq DESC
     LIMIT 1;
    v_summary := NULL;
    IF v_vid IS NOT NULL THEN
        SELECT a.answer INTO v_answer FROM {schema}.m05_el_attempts AS a
         WHERE a.verification_id = v_vid AND a.state = 'answered' AND a.answer IS NOT NULL
         ORDER BY a.n DESC
         LIMIT 1;
        IF v_answer IS NOT NULL AND (""" + " OR ".join(
    f"v_answer OPERATOR(pg_catalog.?) '{f}'" for f in SUMMARY_FIELDS) + r""") THEN
            -- OD-EL4 only: never coinsurance, deductible_total, txn_ref or identity.
            v_summary := pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
                """ + ", ".join(f"'{f}', v_answer OPERATOR(pg_catalog.->) '{f}'"
                                for f in SUMMARY_FIELDS) + r""",
                'checked_at', v_answer OPERATOR(pg_catalog.->) 'observed_at'));
        END IF;
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'seq', n.seq, 'kind', n.kind, 'class', n.class, 'reason', n.reason,
               'at', """ + _UTC_ISO.format(x="n.created_at") + r""") ORDER BY n.seq),
           '[]'::jsonb) INTO v_notes
      FROM {schema}.m05_el_case_notes AS n WHERE n.case_id = v_case.case_id;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1,
        'item_id', v_item.item_id::text, 'item_kind', v_item.item_kind,
        'case_id', v_case.case_id::text, 'case_version', v_case.case_version,
        'case_state', v_case.state, 'class', v_case.current_class,
        'reason', v_case.current_reason, 'summary', v_summary, 'notes', v_notes);
END;
"""

_VIEW_NAME: Final = "m05_el_case_view"
_VIEW_PARAMS: Final = (("p_item_id", "uuid"),)

# --- unit construction -------------------------------------------------------------------

_GATEWAYS: Final = (
    (CREATE_V3_ID, "m05_create_item_v3", _CREATE_V3_BODY),
    (APPLY_ID, "m05_el_apply", _APPLY_BODY),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {
    MIGRATION_ID: T118_SQL,
    **{mid: _definition(name, "volatile", body, RUNTIME_ROLE) for mid, name, body in _GATEWAYS},
    VIEW_ID: _gw._definition(_VIEW_NAME, _VIEW_PARAMS, "stable", _VIEW_BODY,
                             hashlib.sha256(_VIEW_BODY.encode("utf-8")).hexdigest()),
}

EL3A_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
EL3A_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _profile(name, "volatile", RUNTIME_ROLE) for mid, name, _body in _GATEWAYS},
    VIEW_ID: _gw._profile(_VIEW_NAME, _VIEW_PARAMS, "stable"),
})
