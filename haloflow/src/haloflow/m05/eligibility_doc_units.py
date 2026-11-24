"""Eligibility E5 (EL-12): per-tenant migration units t128..t134. SYNTHETIC ONLY.

Composed ONLY into the synthetic eligibility registry (`include_el5`); the production registry
{36} and catalogue are unchanged. Governing: E5 requirements v1r1 (Codex ALIGN fa5db003),
architecture v1r1 A2-A6 (Codex ALIGN 3671b547), test cases v1r1 (Codex ALIGN 9e6974f2),
owner decisions Q1-Q5 (2026-11-22) and OD-EL13 (2026-11-23).

- t128 (migrator SQL): `m05_el_doc_ops` (one row per documentable case event); the item kind
  `eligibility_doc_check` joins the item / intake-map kind CHECKs; the two closing outcome codes
  `entry_confirmed` / `manual_documentation` join `m05_outcome_codes` (the item FK). The doc
  kind is NOT added to `m05_outcome_kinds`, so no generic staff resolve can close a doc item:
  only t132 (confirmation) and t134 (manual) do.
- t129 `m05_el_doc_enqueue` (timer workload): one op per documentable event not yet enqueued
  (A2 selection, rendered from `eligibility_doc.selection_sql`), bounded.
- t130 `m05_el_doc_entry` (STABLE, timer workload): the per-event content reader: the event's
  class / reason / coverage version / time and, for a payer check, ONLY the permitted fields
  (`SUMMARY_FIELDS`, class, reason, observed_at) of THAT event's verification's answered
  attempt (highest n). Never identity, coinsurance, deductible total, txn ref or raw response.
- t131 `m05_el_doc_claim`: one fenced lease per call (epoch + 1), in
  priority order manual_pending -> needs_check confirmation -> reconcile / deliver. A delivery
  claim charges the attempt durably and returns the event row's appointment (the trusted
  binding); a new permit id only when there is none or the previous one is recorded closed.
- t132 `m05_el_doc_settle`: every outcome, fenced on (event_id, epoch), including the durable
  sent marker (`mark_sent`: a separate committed write at the CURRENT epoch before the external
  append, architecture A5.2 / t131 protocol); reads are classified
  in SQL by the A3 settlement rule (event appointment AND a permit this op issued).
- t133 `m05_el_doc_raise` (owner only): at most one `eligibility_doc_check` item per event.
- t134 `m05_el_doc_manual`: the staff "Recorded in chart manually" action, phase 1 on the OPEN
  item at its expected version with the op in needs_check; phase 2 fenced on that epoch.
- t135 `m05_el_doc_item_state` (STABLE, staff READ; REVISE 2026-11-24, Codex D2 ruling (b)):
  the display state of ONE doc item for the staff view — `actionable` (item open, op
  needs_check), `pending` (op manual_pending: "Being confirmed", A6.3 / OD-EL15), `closed`,
  `other`, or `none` (no doc item of that id). Reads only; no attribution, no capability of
  its own beyond the view capability of its statement.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinition, UnitDefinitions
from haloflow.m05.eligibility_case_units import EL_KINDS as EL3_KINDS
from haloflow.m05.eligibility_case_units import SUMMARY_FIELDS
from haloflow.m05.eligibility_doc_rules import (
    APPEND_ATTEMPTS,
    CONFIRM_READ_INTERVAL_S,
    CONFIRM_READS_MAX,
    DOC_KIND,
    LEASE_S,
    OUTCOME_CONFIRMED,
    OUTCOME_MANUAL,
    READ_DELAYS_S,
    RETRY_DELAYS_S,
    selection_sql,
)
from haloflow.m05.eligibility_units import _in
from haloflow.m05.eligibility_worker_units import _head, _out, _uuid_field
from haloflow.m05.reminder_units import _definition, _profile
from haloflow.m05.writeback_units import _KINDS_SQL

TABLES_ID: Final = "t128_m05_el_doc_tables"
ENQUEUE_ID: Final = "t129_m05_el_doc_enqueue"
ENTRY_ID: Final = "t130_m05_el_doc_entry"
CLAIM_ID: Final = "t131_m05_el_doc_claim"
SETTLE_ID: Final = "t132_m05_el_doc_settle"
RAISE_ID: Final = "t133_m05_el_doc_raise"
MANUAL_ID: Final = "t134_m05_el_doc_manual"
STATE_ID: Final = "t135_m05_el_doc_item_state"
EL5_TARGET: Final = 134

OP_STATES: Final = ("ready", "claimed", "retry_wait", "reconciling", "appended", "needs_check",
                    "manual_pending", "manual_done")
_ALL_KINDS_SQL: Final = _KINDS_SQL + ", " + _in(EL3_KINDS) + ", " + _in((DOC_KIND,))
_MALFORMED: Final = "RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';"


def _raise(code: str) -> str:
    return f"RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{code}';"


def _array(values: tuple[int, ...]) -> str:
    return "ARRAY[" + ", ".join(str(v) for v in values) + "]::integer[]"


# --- t128 migrator -------------------------------------------------------------------------

T128_SQL: Final = r"""
-- t128:step:item_kind
ALTER TABLE {schema}.m05_intake_map DROP CONSTRAINT m05_intake_map_item_kind_check,
    ADD CONSTRAINT m05_intake_map_item_kind_check CHECK (item_kind IN (@@KINDS@@));
ALTER TABLE {schema}.m05_followup_items DROP CONSTRAINT m05_followup_items_item_kind_check,
    ADD CONSTRAINT m05_followup_items_item_kind_check CHECK (item_kind IN (@@KINDS@@));

-- t128:step:outcome_codes
INSERT INTO {schema}.m05_outcome_codes (outcome_code, display_label, note_required) VALUES
    ('@@CONFIRMED@@', 'Chart entry found', false),
    ('@@MANUAL@@', 'Recorded in chart manually', false);

-- t128:step:ops
CREATE TABLE {schema}.m05_el_doc_ops (
    event_id uuid PRIMARY KEY REFERENCES {schema}.m05_el_case_events (event_id),
    appointment_ref uuid NOT NULL,
    state varchar(16) NOT NULL CHECK (state IN (@@STATES@@)),
    epoch bigint NOT NULL CHECK (epoch >= 0),
    permit_id uuid,
    permit_sent boolean NOT NULL,
    permit_closed boolean NOT NULL,
    issued_permits uuid[] NOT NULL,
    attempts smallint NOT NULL CHECK (attempts BETWEEN 0 AND @@ATTEMPTS@@),
    reads smallint NOT NULL CHECK (reads BETWEEN 0 AND @@READS@@),
    confirm_reads smallint NOT NULL CHECK (confirm_reads BETWEEN 0 AND @@CONFIRMS@@),
    last_failure varchar(32),
    lease_until timestamptz,
    next_at timestamptz NOT NULL,
    item_id uuid UNIQUE,
    manual_by varchar(64),
    manual_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT m05_el_doc_ops_manual CHECK ((manual_by IS NULL) = (manual_at IS NULL))
);
CREATE INDEX m05_el_doc_ops_due ON {schema}.m05_el_doc_ops (state, next_at);

-- t128:step:runtime_privileges
REVOKE ALL ON {schema}.m05_el_doc_ops FROM haloflow_runtime;

-- t128:step:grants
GRANT SELECT, INSERT, UPDATE ON {schema}.m05_el_doc_ops TO haloflow_m05_owner;
""".replace("@@KINDS@@", _ALL_KINDS_SQL).replace("@@STATES@@", _in(OP_STATES)).replace(
    "@@ATTEMPTS@@", str(APPEND_ATTEMPTS)).replace("@@READS@@", str(len(READ_DELAYS_S))).replace(
    "@@CONFIRMS@@", str(CONFIRM_READS_MAX)).replace("@@CONFIRMED@@", OUTCOME_CONFIRMED).replace(
    "@@MANUAL@@", OUTCOME_MANUAL)

# --- shared fragments ----------------------------------------------------------------------

_LEASE: Final = f"v_now + interval '{LEASE_S} seconds'"


def _secs(expr: str) -> str:
    return f"v_now + pg_catalog.make_interval(secs => {expr})"


# An op whose lease is free (never leased, or the lease expired).
_FREE: Final = "(o.lease_until IS NULL OR o.lease_until <= v_now)"

# Raise the doc item for v_op.event_id in this transaction (t133).
_RAISE_CALL: Final = ("    PERFORM {schema}.m05_el_doc_raise(pg_catalog.jsonb_build_object("
                      "'event_id', v_op.event_id::text));\n")

# --- t129 enqueue --------------------------------------------------------------------------

_ENQUEUE_BODY: Final = _head(r"""
    v_now timestamptz;
    v_limit integer;
    v_n integer;""", ("attribution", "limit", "schema_version")) + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'limit')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'limit') OPERATOR(pg_catalog.!~) '^[0-9]{1,4}$'
       OR (p_command OPERATOR(pg_catalog.->>) 'limit')::integer NOT BETWEEN 1 AND 1000 THEN
        """ + _MALFORMED + r"""
    END IF;
    v_limit := (p_command OPERATOR(pg_catalog.->>) 'limit')::integer;
    INSERT INTO {schema}.m05_el_doc_ops (event_id, appointment_ref, state, epoch, permit_id,
        permit_sent, permit_closed, issued_permits, attempts, reads, confirm_reads,
        last_failure, lease_until, next_at, item_id, manual_by, manual_at, created_at,
        updated_at)
    SELECT s.event_id, s.appointment_ref, 'ready', 0, NULL, false, false, '{}'::uuid[], 0, 0,
           0, NULL, NULL, v_now, NULL, NULL, NULL, v_now, v_now
      FROM (""" + selection_sql("{schema}") + r""") AS s
     WHERE NOT EXISTS (SELECT 1 FROM {schema}.m05_el_doc_ops AS o
                        WHERE o.event_id = s.event_id)
     ORDER BY s.created_at, s.event_id
     LIMIT v_limit
    ON CONFLICT (event_id) DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN """ + _out(outcome="'applied'", enqueued="v_n") + r""";
END;
"""

# --- t130 content reader -------------------------------------------------------------------

_PERMITTED_ANSWER: Final = (
    "pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object("
    + ", ".join(f"'{f}', v_answer OPERATOR(pg_catalog.->) '{f}'"
                for f in ("class", "reason", "observed_at", *SUMMARY_FIELDS))
    + "))")

_ENTRY_BODY: Final = _head(r"""
    v_now timestamptz;
    v_event uuid;
    v_e record;
    v_answer jsonb;""", ("attribution", "event_id", "schema_version")) + _uuid_field(
    "event_id", "v_event") + r"""
    SELECT e.event_id, e.kind, e.class, e.reason, e.coverage_version, e.verification_id,
           e.created_at INTO v_e
      FROM {schema}.m05_el_case_events AS e WHERE e.event_id = v_event;
    IF NOT FOUND THEN
        """ + _raise("EVENT_NOT_FOUND") + r"""
    END IF;
    v_answer := NULL;
    IF v_e.verification_id IS NOT NULL THEN
        SELECT a.answer INTO v_answer FROM {schema}.m05_el_attempts AS a
         WHERE a.verification_id = v_e.verification_id AND a.state = 'answered'
           AND a.answer IS NOT NULL
         ORDER BY a.n DESC
         LIMIT 1;
    END IF;
    RETURN """ + _out(
    outcome="'applied'",
    event="pg_catalog.jsonb_build_object('event_id', v_e.event_id::text, 'kind', v_e.kind,"
          " 'class', v_e.class, 'reason', v_e.reason, 'coverage_version',"
          " v_e.coverage_version, 'verification_id', v_e.verification_id::text,"
          " 'created_at', pg_catalog.to_char(pg_catalog.timezone('UTC', v_e.created_at),"
          " 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'))",
    answer="CASE WHEN v_answer IS NULL THEN NULL ELSE " + _PERMITTED_ANSWER + " END") + r""";
END;
"""

# --- t131 claim / mark-sent ----------------------------------------------------------------

_CLAIM_BODY: Final = _head(r"""
    v_now timestamptz;
    v_op record;
    v_ref uuid;
    v_permit uuid;""", ("attribution", "schema_version")) + r"""
    SELECT o.* INTO v_op FROM {schema}.m05_el_doc_ops AS o
     WHERE (o.state = 'manual_pending' AND """ + _FREE + r""")
        OR (o.state = 'needs_check' AND o.next_at <= v_now
            AND o.confirm_reads < """ + str(CONFIRM_READS_MAX) + " AND " + _FREE + r""")
        OR (o.state = 'reconciling' AND o.next_at <= v_now AND """ + _FREE + r""")
        OR (o.state = 'ready' AND """ + _FREE + r""")
        OR (o.state = 'retry_wait' AND o.next_at <= v_now)
        OR (o.state = 'claimed' AND o.lease_until <= v_now)
     ORDER BY CASE o.state WHEN 'manual_pending' THEN 0 WHEN 'needs_check' THEN 1 ELSE 2 END,
              o.next_at, o.event_id
     LIMIT 1
       FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN """ + _out(outcome="'idle'") + r""";
    END IF;
    -- The trusted binding: the event row's appointment (never the op's copy, never a request).
    SELECT e.appointment_ref INTO v_ref FROM {schema}.m05_el_case_events AS e
     WHERE e.event_id = v_op.event_id;

    IF v_op.state = 'manual_pending' THEN
        UPDATE {schema}.m05_el_doc_ops SET epoch = epoch + 1, lease_until = """ + _LEASE + r""",
               updated_at = v_now WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'manual_resume'", event_id="v_op.event_id::text",
                          epoch="v_op.epoch + 1", permit_id="v_op.permit_id::text") + r""";
    ELSIF v_op.state = 'needs_check' THEN
        UPDATE {schema}.m05_el_doc_ops SET epoch = epoch + 1, lease_until = """ + _LEASE + r""",
               confirm_reads = confirm_reads + 1,
               next_at = v_now + interval '""" + str(CONFIRM_READ_INTERVAL_S) + r""" seconds',
               updated_at = v_now WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'confirm'", event_id="v_op.event_id::text",
                          epoch="v_op.epoch + 1") + r""";
    ELSIF v_op.state = 'reconciling' THEN
        UPDATE {schema}.m05_el_doc_ops SET epoch = epoch + 1, lease_until = """ + _LEASE + r""",
               updated_at = v_now WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'reconcile'", event_id="v_op.event_id::text",
                          epoch="v_op.epoch + 1", permit_id="v_op.permit_id::text") + r""";
    ELSIF v_op.state = 'claimed' AND v_op.permit_sent THEN
        -- A worker died after the durable sent marker: the outcome is unknown (A5.5).
        UPDATE {schema}.m05_el_doc_ops SET state = 'reconciling', lease_until = NULL,
               last_failure = 'lease_expired_sent',
               next_at = """ + _secs(str(READ_DELAYS_S[0])) + r""", updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'recovered'", event_id="v_op.event_id::text") + r""";
    END IF;
    -- ready / due retry_wait / an expired unsent claim: a delivery attempt.
    IF v_op.attempts >= """ + str(APPEND_ATTEMPTS) + r""" THEN
        UPDATE {schema}.m05_el_doc_ops SET state = 'needs_check', lease_until = NULL,
               last_failure = 'attempts_exhausted',
               next_at = v_now + interval '""" + str(CONFIRM_READ_INTERVAL_S) + r""" seconds',
               updated_at = v_now WHERE event_id = v_op.event_id;
""" + _RAISE_CALL + r"""
        RETURN """ + _out(outcome="'exhausted'", event_id="v_op.event_id::text",
                          permit_id="CASE WHEN v_op.permit_closed THEN NULL"
                                    " ELSE v_op.permit_id::text END") + r""";
    END IF;
    v_permit := v_op.permit_id;
    IF v_permit IS NULL OR v_op.permit_closed THEN
        v_permit := pg_catalog.gen_random_uuid();
        UPDATE {schema}.m05_el_doc_ops SET permit_id = v_permit, permit_sent = false,
               permit_closed = false,
               issued_permits = pg_catalog.array_append(issued_permits, v_permit)
         WHERE event_id = v_op.event_id;
    END IF;
    UPDATE {schema}.m05_el_doc_ops SET state = 'claimed', epoch = epoch + 1,
           attempts = attempts + 1, lease_until = """ + _LEASE + r""", updated_at = v_now
     WHERE event_id = v_op.event_id;
    RETURN """ + _out(outcome="'deliver'", event_id="v_op.event_id::text",
                      epoch="v_op.epoch + 1", permit_id="v_permit::text",
                      appointment_ref="v_ref::text", attempt="v_op.attempts + 1") + r""";
END;
"""

# The next reconciliation read after read number v_op.reads (1-based delays 60 / 300 / 900 s).
_NEXT_READ: Final = _secs("(" + _array(READ_DELAYS_S) + ")[LEAST(v_op.reads + 1, "
                          + str(len(READ_DELAYS_S)) + ")]")

# --- t132 settle ---------------------------------------------------------------------------

_RESULTS: Final = ("mark_sent", "appended", "refused", "unknown", "mismatch", "read",
                   "charge_read", "read_unavailable", "definitive", "close_unknown", "confirm",
                   "confirm_unavailable")

# Classify v_entry (NULL or {appointment_ref, permit_id}) against v_op / v_ref into v_class:
# 'absent' | 'refused_mismatch' | 'foreign_permit' | 'match' (A3 settlement rule).
_CLASSIFY: Final = r"""
    IF v_entry IS NULL OR pg_catalog.jsonb_typeof(v_entry) = 'null' THEN
        v_class := 'absent';
    ELSIF (v_entry OPERATOR(pg_catalog.->>) 'appointment_ref') IS DISTINCT FROM v_ref::text THEN
        v_class := 'refused_mismatch';
    ELSIF (v_entry OPERATOR(pg_catalog.->>) 'permit_id') IS NULL
          OR (v_entry OPERATOR(pg_catalog.->>) 'permit_id') OPERATOR(pg_catalog.!~) c_uuid
          OR NOT ((v_entry OPERATOR(pg_catalog.->>) 'permit_id')::uuid
                  = ANY (v_op.issued_permits)) THEN
        v_class := 'foreign_permit';
    ELSE
        v_class := 'match';
    END IF;
"""

_RESOLVE_ITEM: Final = (
    "        UPDATE {schema}.m05_followup_items SET state = 'resolved', outcome_code = '{code}',\n"
    "               version = version + 1\n"
    "         WHERE item_id = v_op.item_id AND state <> 'resolved';\n")

_TO_NEEDS_CHECK: Final = (
    "        UPDATE {schema}.m05_el_doc_ops SET state = 'needs_check', lease_until = NULL,\n"
    "               last_failure = v_failure,\n"
    "               next_at = v_now + interval '" + str(CONFIRM_READ_INTERVAL_S) + " seconds',\n"
    "               updated_at = v_now WHERE event_id = v_op.event_id;\n" + _RAISE_CALL)

_SETTLE_BODY: Final = _head(r"""
    v_now timestamptz;
    v_event uuid;
    v_epoch bigint;
    v_result text;
    v_entry jsonb;
    v_closed boolean;
    v_op record;
    v_ref uuid;
    v_class text;
    v_failure text;
    v_delays integer[];""", ("attribution", "entry", "epoch", "event_id", "permit_closed",
                             "result", "schema_version")) + _uuid_field(
    "event_id", "v_event") + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'epoch')
          IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'result')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'result') NOT IN (""" + _in(_RESULTS) + r""")
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'permit_closed')
          IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'entry')
          NOT IN ('null', 'object') THEN
        """ + _MALFORMED + r"""
    END IF;
    v_epoch := (p_command OPERATOR(pg_catalog.->>) 'epoch')::bigint;
    v_result := p_command OPERATOR(pg_catalog.->>) 'result';
    v_entry := p_command OPERATOR(pg_catalog.->) 'entry';
    v_closed := (p_command OPERATOR(pg_catalog.->>) 'permit_closed')::boolean;
    SELECT o.* INTO v_op FROM {schema}.m05_el_doc_ops AS o
     WHERE o.event_id = v_event AND o.epoch = v_epoch
       FOR UPDATE;
    IF NOT FOUND
       OR (v_result IN ('mark_sent', 'appended', 'refused', 'unknown')
           AND v_op.state <> 'claimed')
       OR (v_result IN ('read', 'charge_read', 'read_unavailable', 'definitive',
                        'close_unknown') AND v_op.state <> 'reconciling')
       OR (v_result IN ('confirm', 'confirm_unavailable') AND v_op.state <> 'needs_check')
       OR (v_result = 'mismatch' AND v_op.state NOT IN ('claimed', 'reconciling')) THEN
        """ + _raise("EPOCH_STALE") + r"""
    END IF;
    SELECT e.appointment_ref INTO v_ref FROM {schema}.m05_el_case_events AS e
     WHERE e.event_id = v_op.event_id;
    v_delays := """ + _array(RETRY_DELAYS_S) + r""";

    IF v_result = 'mark_sent' THEN
        -- The durable sent marker (t131 protocol), committed at the CURRENT epoch before the
        -- external append; a stale epoch, another permit or a closed permit is refused.
        IF pg_catalog.jsonb_typeof(v_entry) IS DISTINCT FROM 'object'
           OR (v_entry OPERATOR(pg_catalog.->>) 'permit_id') IS DISTINCT FROM v_op.permit_id::text
           OR v_op.permit_closed THEN
            """ + _raise("EPOCH_STALE") + r"""
        END IF;
        UPDATE {schema}.m05_el_doc_ops SET permit_sent = true, updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'sent'") + r""";
    ELSIF v_result = 'appended' THEN
        UPDATE {schema}.m05_el_doc_ops SET state = 'appended', lease_until = NULL,
               last_failure = NULL, updated_at = v_now WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'appended'") + r""";
    ELSIF v_result = 'refused' THEN
        -- A definitive non-write: nothing was written under this permit.
        IF v_op.attempts >= """ + str(APPEND_ATTEMPTS) + r""" THEN
            v_failure := 'attempts_exhausted';
            UPDATE {schema}.m05_el_doc_ops SET permit_sent = false, permit_closed = v_closed
             WHERE event_id = v_op.event_id;
""" + _TO_NEEDS_CHECK + r"""
            RETURN """ + _out(outcome="'needs_check'") + r""";
        END IF;
        UPDATE {schema}.m05_el_doc_ops SET state = 'retry_wait', lease_until = NULL,
               permit_sent = false, permit_closed = v_closed, last_failure = 'refused',
               next_at = """ + _secs("v_delays[v_op.attempts]") + r""", updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'retry_wait'") + r""";
    ELSIF v_result = 'unknown' THEN
        UPDATE {schema}.m05_el_doc_ops SET state = 'reconciling', lease_until = NULL,
               last_failure = 'unknown',
               next_at = """ + _secs(str(READ_DELAYS_S[0])) + r""", updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'reconciling'") + r""";
    ELSIF v_result = 'mismatch' THEN
        v_failure := 'refused_mismatch';
""" + _TO_NEEDS_CHECK + r"""
        RETURN """ + _out(outcome="'refused_mismatch'") + r""";
    ELSIF v_result = 'charge_read' THEN
        IF v_op.reads >= """ + str(len(READ_DELAYS_S)) + r""" THEN
            v_failure := 'reads_exhausted';
""" + _TO_NEEDS_CHECK + r"""
            RETURN """ + _out(outcome="'exhausted'") + r""";
        END IF;
        UPDATE {schema}.m05_el_doc_ops SET reads = reads + 1, updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'charged'") + r""";
    ELSIF v_result IN ('read', 'confirm') THEN
""" + _CLASSIFY + r"""
        IF v_class = 'absent' THEN
            IF v_result = 'confirm' THEN
                UPDATE {schema}.m05_el_doc_ops SET lease_until = NULL, updated_at = v_now
                 WHERE event_id = v_op.event_id;
            END IF;
            RETURN """ + _out(outcome="'absent'") + r""";
        ELSIF v_class = 'match' THEN
            UPDATE {schema}.m05_el_doc_ops SET state = 'appended', lease_until = NULL,
                   last_failure = NULL, updated_at = v_now WHERE event_id = v_op.event_id;
            IF v_result = 'confirm' THEN
""" + _RESOLVE_ITEM.replace("{code}", OUTCOME_CONFIRMED) + r"""
                RETURN """ + _out(outcome="'entry_confirmed'") + r""";
            END IF;
            RETURN """ + _out(outcome="'appended'") + r""";
        END IF;
        -- Never settled: another appointment or a permit this op did not issue (counted).
        IF v_result = 'confirm' THEN
            UPDATE {schema}.m05_el_doc_ops SET lease_until = NULL, last_failure = v_class,
                   updated_at = v_now WHERE event_id = v_op.event_id;
            RETURN """ + _out(outcome="v_class") + r""";
        END IF;
        v_failure := v_class;
""" + _TO_NEEDS_CHECK + r"""
        RETURN """ + _out(outcome="v_class") + r""";
    ELSIF v_result = 'read_unavailable' THEN
        IF v_op.reads >= """ + str(len(READ_DELAYS_S)) + r""" THEN
            v_failure := 'reads_exhausted';
""" + _TO_NEEDS_CHECK + r"""
            RETURN """ + _out(outcome="'needs_check'") + r""";
        END IF;
        UPDATE {schema}.m05_el_doc_ops SET lease_until = NULL, last_failure = 'read_unavailable',
               next_at = """ + _NEXT_READ + r""",
               updated_at = v_now WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'reconciling'") + r""";
    ELSIF v_result = 'close_unknown' THEN
        UPDATE {schema}.m05_el_doc_ops SET lease_until = NULL, last_failure = 'close_unknown',
               next_at = """ + _NEXT_READ + r""",
               updated_at = v_now WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'reconciling'") + r""";
    ELSIF v_result = 'definitive' THEN
        -- Absent after the close: nothing can commit under the closed permit (A5.4). At the
        -- last read, or with the attempts spent, exhaustion wins over a new permit.
        UPDATE {schema}.m05_el_doc_ops SET permit_sent = false, permit_closed = true
         WHERE event_id = v_op.event_id;
        IF v_op.reads >= """ + str(len(READ_DELAYS_S)) + " OR v_op.attempts >= " + str(
    APPEND_ATTEMPTS) + r""" THEN
            v_failure := CASE WHEN v_op.attempts >= """ + str(APPEND_ATTEMPTS) + r"""
                              THEN 'attempts_exhausted' ELSE 'reads_exhausted' END;
""" + _TO_NEEDS_CHECK + r"""
            RETURN """ + _out(outcome="'needs_check'") + r""";
        END IF;
        UPDATE {schema}.m05_el_doc_ops SET state = 'retry_wait', lease_until = NULL,
               last_failure = 'absent_after_close',
               next_at = """ + _secs("v_delays[v_op.attempts]") + r""", updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN """ + _out(outcome="'retry_wait'") + r""";
    END IF;
    -- confirm_unavailable: the confirmation read failed; the next one is hourly.
    UPDATE {schema}.m05_el_doc_ops SET lease_until = NULL, updated_at = v_now
     WHERE event_id = v_op.event_id;
    RETURN """ + _out(outcome="'absent'") + r""";
END;
"""

# --- t133 raise (owner only) ---------------------------------------------------------------

_RAISE_BODY: Final = r"""
DECLARE
    v_setting text;
    v_event uuid;
    v_patient uuid;
    v_item uuid;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = '' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    v_event := (p_command OPERATOR(pg_catalog.->>) 'event_id')::uuid;
    SELECT a.patient_id INTO v_patient
      FROM {schema}.m05_el_case_events AS e
      JOIN {schema}.m05_el_appointments AS a ON a.appointment_ref = e.appointment_ref
     WHERE e.event_id = v_event;
    IF NOT FOUND THEN
        """ + _raise("EVENT_NOT_FOUND") + r"""
    END IF;
    INSERT INTO {schema}.m05_followup_items (item_id, source_event_id, item_kind, patient_id,
        state, assignee_id, outcome_code, version, create_digest, created_at)
    VALUES (pg_catalog.gen_random_uuid(), v_event, '""" + DOC_KIND + r"""', v_patient, 'open',
        NULL, NULL, 1, pg_catalog.sha256(pg_catalog.convert_to('""" + DOC_KIND + r"""|'
            OPERATOR(pg_catalog.||) v_event::text, 'UTF8')), pg_catalog.clock_timestamp())
    ON CONFLICT (source_event_id, item_kind) DO NOTHING;
    SELECT i.item_id INTO v_item FROM {schema}.m05_followup_items AS i
     WHERE i.source_event_id = v_event AND i.item_kind = '""" + DOC_KIND + r"""';
    UPDATE {schema}.m05_el_doc_ops SET item_id = v_item
     WHERE event_id = v_event AND item_id IS NULL;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'item_id', v_item::text);
END;
"""

# --- t134 manual closure -------------------------------------------------------------------

_MANUAL_BODY: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_keys text[];
    v_attr jsonb;
    v_now timestamptz;
    v_phase integer;
    v_item_id uuid;
    v_version integer;
    v_staff text;
    v_event uuid;
    v_epoch bigint;
    v_entry jsonb;
    v_item record;
    v_op record;
    v_ref uuid;
    v_class text;
    v_outcome text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object'
       OR (p_command OPERATOR(pg_catalog.->>) 'schema_version') IS DISTINCT FROM '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'phase')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'phase') NOT IN ('1', '2') THEN
        """ + _MALFORMED + r"""
    END IF;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
    v_attr := p_command OPERATOR(pg_catalog.->) 'attribution';
    IF pg_catalog.jsonb_typeof(v_attr) IS DISTINCT FROM 'object'
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') NOT IN ('actor', 'workload')
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_id') IS NULL
       OR (v_attr OPERATOR(pg_catalog.->>) 'principal_id')
           OPERATOR(pg_catalog.!~) '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}$' THEN
        """ + _MALFORMED + r"""
    END IF;
    v_now := pg_catalog.clock_timestamp();
    v_phase := (p_command OPERATOR(pg_catalog.->>) 'phase')::integer;

    IF v_phase = 1 THEN
        -- Phase 1: only an authorized STAFF actor, on the OPEN doc item at its version.
        IF v_keys IS DISTINCT FROM ARRAY['attribution', 'expected_version', 'item_id', 'phase',
                                         'schema_version', 'staff_id']::text[]
           OR (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'actor'
           OR (p_command OPERATOR(pg_catalog.->>) 'staff_id')
              IS DISTINCT FROM (v_attr OPERATOR(pg_catalog.->>) 'principal_id')
           OR (p_command OPERATOR(pg_catalog.->>) 'item_id') OPERATOR(pg_catalog.!~) c_uuid
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'expected_version')
              IS DISTINCT FROM 'number' THEN
            """ + _MALFORMED + r"""
        END IF;
        v_item_id := (p_command OPERATOR(pg_catalog.->>) 'item_id')::uuid;
        v_version := (p_command OPERATOR(pg_catalog.->>) 'expected_version')::integer;
        v_staff := p_command OPERATOR(pg_catalog.->>) 'staff_id';
        SELECT i.item_id, i.state, i.version INTO v_item FROM {schema}.m05_followup_items AS i
         WHERE i.item_id = v_item_id AND i.item_kind = '""" + DOC_KIND + r"""'
           FOR UPDATE;
        IF NOT FOUND THEN
            """ + _raise("ITEM_NOT_FOUND") + r"""
        END IF;
        IF v_item.state = 'resolved' THEN
            """ + _raise("ITEM_CLOSED") + r"""
        END IF;
        IF v_item.version <> v_version THEN
            """ + _raise("VERSION_STALE") + r"""
        END IF;
        SELECT o.* INTO v_op FROM {schema}.m05_el_doc_ops AS o
         WHERE o.item_id = v_item_id FOR UPDATE;
        IF NOT FOUND OR v_op.state <> 'needs_check' THEN
            """ + _raise("OP_NOT_NEEDS_CHECK") + r"""
        END IF;
        UPDATE {schema}.m05_el_doc_ops SET state = 'manual_pending', epoch = epoch + 1,
               manual_by = v_staff, manual_at = v_now, lease_until = NULL, updated_at = v_now
         WHERE event_id = v_op.event_id;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'manual_pending',
            'event_id', v_op.event_id::text, 'epoch', v_op.epoch + 1,
            'permit_id', v_op.permit_id::text);
    END IF;

    -- Phase 2 (fenced on the phase-1 or resumption epoch): settle by the close-then-read.
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'entry', 'epoch', 'event_id', 'phase',
                                     'schema_version']::text[]
       OR (p_command OPERATOR(pg_catalog.->>) 'event_id') OPERATOR(pg_catalog.!~) c_uuid
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'epoch')
          IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'entry')
          NOT IN ('null', 'object') THEN
        """ + _MALFORMED + r"""
    END IF;
    v_event := (p_command OPERATOR(pg_catalog.->>) 'event_id')::uuid;
    v_epoch := (p_command OPERATOR(pg_catalog.->>) 'epoch')::bigint;
    v_entry := p_command OPERATOR(pg_catalog.->) 'entry';
    SELECT o.* INTO v_op FROM {schema}.m05_el_doc_ops AS o
     WHERE o.event_id = v_event AND o.epoch = v_epoch AND o.state = 'manual_pending'
       FOR UPDATE;
    IF NOT FOUND THEN
        """ + _raise("EPOCH_STALE") + r"""
    END IF;
    SELECT e.appointment_ref INTO v_ref FROM {schema}.m05_el_case_events AS e
     WHERE e.event_id = v_op.event_id;
""" + _CLASSIFY + r"""
    IF v_class = 'match' THEN
        UPDATE {schema}.m05_el_doc_ops SET state = 'appended', lease_until = NULL,
               updated_at = v_now WHERE event_id = v_op.event_id;
""" + _RESOLVE_ITEM.replace("{code}", OUTCOME_CONFIRMED) + r"""
        v_outcome := '""" + OUTCOME_CONFIRMED + r"""';
    ELSE
        UPDATE {schema}.m05_el_doc_ops SET state = 'manual_done', lease_until = NULL,
               permit_sent = false, permit_closed = true,
               last_failure = CASE WHEN v_class = 'absent' THEN last_failure ELSE v_class END,
               updated_at = v_now WHERE event_id = v_op.event_id;
""" + _RESOLVE_ITEM.replace("{code}", OUTCOME_MANUAL) + r"""
        v_outcome := '""" + OUTCOME_MANUAL + r"""';
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', v_outcome,
        'class', v_class);
END;
"""

# --- t135 display state (staff READ) -------------------------------------------------------

_STATE_BODY: Final = r"""
DECLARE
    c_uuid constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_setting text;
    v_keys text[];
    v_item_id uuid;
    v_item_state text;
    v_op_state text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    IF pg_catalog.jsonb_typeof(p_command) IS DISTINCT FROM 'object'
       OR (p_command OPERATOR(pg_catalog.->>) 'schema_version') IS DISTINCT FROM '1' THEN
        """ + _MALFORMED + r"""
    END IF;
    SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_keys
      FROM pg_catalog.jsonb_object_keys(p_command) AS k;
    IF v_keys IS DISTINCT FROM ARRAY['item_id', 'schema_version']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'item_id')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'item_id') OPERATOR(pg_catalog.!~) c_uuid THEN
        """ + _MALFORMED + r"""
    END IF;
    v_item_id := (p_command OPERATOR(pg_catalog.->>) 'item_id')::uuid;
    SELECT i.state INTO v_item_state FROM {schema}.m05_followup_items AS i
     WHERE i.item_id = v_item_id AND i.item_kind = '""" + DOC_KIND + r"""';
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'display', 'none');
    END IF;
    SELECT o.state INTO v_op_state FROM {schema}.m05_el_doc_ops AS o
     WHERE o.item_id = v_item_id;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'display',
        CASE WHEN v_item_state = 'resolved' THEN 'closed'
             WHEN v_op_state = 'manual_pending' THEN 'pending'
             WHEN v_op_state = 'needs_check' THEN 'actionable'
             ELSE 'other' END);
END;
"""

# --- unit construction ---------------------------------------------------------------------

# (migration id, function, volatility, body, EXECUTE grantee or None for owner only)
_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    (ENQUEUE_ID, "m05_el_doc_enqueue", "volatile", _ENQUEUE_BODY, RUNTIME_ROLE),
    (ENTRY_ID, "m05_el_doc_entry", "stable", _ENTRY_BODY, RUNTIME_ROLE),
    (CLAIM_ID, "m05_el_doc_claim", "volatile", _CLAIM_BODY, RUNTIME_ROLE),
    (SETTLE_ID, "m05_el_doc_settle", "volatile", _SETTLE_BODY, RUNTIME_ROLE),
    (RAISE_ID, "m05_el_doc_raise", "volatile", _RAISE_BODY, None),
    (MANUAL_ID, "m05_el_doc_manual", "volatile", _MANUAL_BODY, RUNTIME_ROLE),
    (STATE_ID, "m05_el_doc_item_state", "stable", _STATE_BODY, RUNTIME_ROLE),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {TABLES_ID: T128_SQL,
               **{mid: _definition(name, vol, body, grantee)
                  for mid, name, vol, body, grantee in _FUNCTIONS}}

EL5_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
EL5_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS})

