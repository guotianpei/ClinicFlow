"""Eligibility checks E2: per-tenant migration units t106..t117. SYNTHETIC ONLY.

Composed ONLY into the synthetic registry behind `include_el2` (with `include_el1`); the
production registry {36} and catalogue are unchanged. Governing: eligibility requirements
v1r1; architecture v1, v1r1 (R1, R2, R4), v1r2 amendment (M1, M5), v1r3 correction (C2);
stage-3 test cases v1 + v1r1 T3; E2 red packet v1r1 (Codex red gate PASS b690404c).

- t106 (migrator SQL): verification lease/epoch, freshness key and observation columns;
  `returned` on triggers; `m05_el_attempts`; `m05_el_late_answers`; the settlement identity
  `m05_el_case_events.verification_id` (UNIQUE); the OD-EL8 payer reasons; the last-scrub
  stamp; (v1r1, Codex 7a2e2b91) row triggers that keep the retention cutoff on every later
  write (coverage, attempt answers; the digest in m05_el_due) and reset `returned` on a coverage
  version change only.
- t107..t110 owner-only IMMUTABLE seams: `m05_el_retry_ready`, `m05_el_takeover_ready`,
  `m05_el_fresh`, `m05_el_expired` (text ISO dates: the platform admits no `date`
  parameter, F-INT1; the E1 local-instant precedent).
- t111..t117 runtime gateways (timer workload): `m05_el_due`, `m05_el_claim`,
  `m05_el_dispatch`, `m05_el_record_answer`, `m05_el_timeout`, `m05_el_settle`,
  `m05_el_expire`.

Lock order (extends E1's): the appointment KEY row (`m05_el_appointments`) before a
verification (due, settle; E1 intake); claim, dispatch, record_answer and timeout lock only
the verification / attempt and never wait on a key row, so no cycle exists. Every write after
claim is fenced on (verification_id, epoch).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinition, UnitDefinitions
from haloflow.m05.eligibility_units import (
    _LI,
    _MALFORMED,
    EVENT_REASONS,
    _days_before,
    _in,
    _iso,
    _typed,
    _typed_profile,
)
from haloflow.m05.intake_units import _ATTRIBUTION, _DECLARE_COMMON, _TENANT, _workload
from haloflow.m05.reminder_units import _definition, _profile

MIGRATION_ID: Final = "t106_m05_el_worker_tables"
RETRY_SEAM_ID: Final = "t107_m05_el_retry_ready"
TAKEOVER_SEAM_ID: Final = "t108_m05_el_takeover_ready"
FRESH_SEAM_ID: Final = "t109_m05_el_fresh"
EXPIRED_SEAM_ID: Final = "t110_m05_el_expired"
DUE_ID: Final = "t111_m05_el_due"
CLAIM_ID: Final = "t112_m05_el_claim"
DISPATCH_ID: Final = "t113_m05_el_dispatch"
ANSWER_ID: Final = "t114_m05_el_record_answer"
TIMEOUT_ID: Final = "t115_m05_el_timeout"
SETTLE_ID: Final = "t116_m05_el_settle"
EXPIRE_ID: Final = "t117_m05_el_expire"
EL2_TARGET: Final = 117

RETRY_DELAY_S: Final = 120
LEASE_S: Final = 120
TAKEOVER_MARGIN_S: Final = 30
EXPIRY_DAYS: Final = 30

INCONCLUSIVE_REASONS: Final = ("subscriber_not_found", "name_dob_mismatch",
                               "member_id_not_recognized", "service_type_unsupported",
                               "payer_unavailable", "response_unreadable")
PAYER_REASONS: Final = ("coverage_active", "coverage_inactive", *INCONCLUSIVE_REASONS)
ALL_EVENT_REASONS: Final = (*EVENT_REASONS, *PAYER_REASONS)
RESULT_STRINGS: Final = ("source", "payer_name", "plan", "copay", "deductible_total",
                         "deductible_remaining", "coinsurance", "txn_ref")
RESULT_BOOLEANS: Final = ("referral_required", "auth_required")
RESULT_KEYS: Final = ("class", "reason", "observed_at", *RESULT_STRINGS, *RESULT_BOOLEANS)
ECHO_KEYS: Final = ("appointment_ref", "attempt_id", "coverage_version", "member_id",
                    "patient_id", "tenant_id")

# --- t106 migrator ---------------------------------------------------------------------

T106_SQL: Final = r"""
-- t106:step:tables
ALTER TABLE {schema}.m05_el_verifications
    ADD COLUMN epoch integer NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    ADD COLUMN lease_until timestamptz,
    ADD COLUMN patient_id uuid,
    ADD COLUMN payer_id varchar(64),
    ADD COLUMN member_digest char(64)
        CHECK (member_digest IS NULL OR member_digest ~ '^[0-9a-f]{64}$'),
    ADD COLUMN service_date date,
    ADD COLUMN observed_at timestamptz;
ALTER TABLE {schema}.m05_el_triggers
    ADD COLUMN returned boolean NOT NULL DEFAULT false;
ALTER TABLE {schema}.m05_el_appointments
    ADD COLUMN expired_at timestamptz;
CREATE TABLE {schema}.m05_el_attempts (
    attempt_id uuid PRIMARY KEY,
    verification_id uuid NOT NULL
        REFERENCES {schema}.m05_el_verifications (verification_id),
    n smallint NOT NULL CHECK (n BETWEEN 1 AND 2),
    epoch integer NOT NULL CHECK (epoch >= 1),
    state varchar(12) NOT NULL CHECK (state IN ('dispatched', 'answered', 'timed_out',
        'ambiguous')),
    dispatched_at timestamptz NOT NULL,
    finished_at timestamptz,
    answer jsonb,
    UNIQUE (verification_id, n),
    CONSTRAINT m05_el_attempts_answer CHECK ((state = 'answered') = (answer IS NOT NULL)),
    CONSTRAINT m05_el_attempts_finished CHECK ((state = 'dispatched') = (finished_at IS NULL))
);
CREATE TABLE {schema}.m05_el_late_answers (
    late_id uuid PRIMARY KEY,
    attempt_id uuid NOT NULL,
    verification_id uuid,
    cause varchar(16) NOT NULL CHECK (cause IN ('unknown_attempt', 'closed_attempt',
        'superseded', 'stale_epoch', 'mismatch')),
    class varchar(16) CHECK (class IS NULL OR class IN ('active', 'inactive',
        'inconclusive')),
    received_at timestamptz NOT NULL
);
ALTER TABLE {schema}.m05_el_case_events
    ADD COLUMN verification_id uuid
        REFERENCES {schema}.m05_el_verifications (verification_id);
CREATE UNIQUE INDEX m05_el_case_events_verification
    ON {schema}.m05_el_case_events (verification_id);
ALTER TABLE {schema}.m05_el_case_events DROP CONSTRAINT m05_el_case_events_reason_check;
ALTER TABLE {schema}.m05_el_case_events ADD CONSTRAINT m05_el_case_events_reason_check
    CHECK (reason IN (""" + _in(ALL_EVENT_REASONS) + r"""));

-- t106:step:integrity
-- Codex 7a2e2b91 finding 1: the retention cutoff (service date + 30 days, clinic local)
-- holds for EVERY later write, not only for the sweep: a coverage row, an attempt answer
-- written for an appointment already past the cutoff keeps only the permitted history (a
-- verification's digest: at its single write site, m05_el_due). Not named m05_* (the m05_
-- namespace is reviewed gateways, ME-01).
CREATE FUNCTION {schema}.el_past_cutoff(p_ref uuid) RETURNS boolean
    LANGUAGE plpgsql STABLE
    SECURITY INVOKER
    SET search_path = pg_catalog, {schema}, pg_temp
    AS $body$
DECLARE
    v_k record;
BEGIN
    SELECT a.zone, a.start_at INTO v_k FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = p_ref;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_k.zone IS NULL THEN
        -- Codex 82910a80: no usable zone (the E1 D-1 deferral). A RETENTION-ONLY bound, never
        -- a scheduling zone: the latest possible local service date (UTC+14) against the
        -- earliest possible local today (UTC-12), so it is past only when the cutoff has
        -- passed in EVERY supported zone. Boundary: UTC date of (now - 12 h) > UTC date of
        -- (start + 14 h) + 30 days.
        RETURN {schema}.m05_el_expired(
            pg_catalog.to_char((pg_catalog.timezone('UTC', v_k.start_at)
                                OPERATOR(pg_catalog.+) interval '14 hours')::date, 'YYYY-MM-DD'),
            pg_catalog.to_char((pg_catalog.timezone('UTC', pg_catalog.clock_timestamp())
                                OPERATOR(pg_catalog.-) interval '12 hours')::date, 'YYYY-MM-DD'));
    END IF;
    RETURN {schema}.m05_el_expired(
        pg_catalog.to_char((pg_catalog.timezone(v_k.zone, v_k.start_at))::date, 'YYYY-MM-DD'),
        pg_catalog.to_char((pg_catalog.timezone(v_k.zone, pg_catalog.clock_timestamp()))::date,
                           'YYYY-MM-DD'));
END;
$body$;
REVOKE ALL ON FUNCTION {schema}.el_past_cutoff(uuid) FROM PUBLIC;
CREATE FUNCTION {schema}.el_cutoff_coverage() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {schema}, pg_temp
    AS $body$
BEGIN
    IF {schema}.el_past_cutoff(NEW.appointment_ref) THEN
        NEW.member_id := NULL;
        NEW.patient_name := NULL;
        NEW.patient_dob := NULL;
        NEW.subscriber_name := NULL;
        NEW.subscriber_dob := NULL;
        NEW.subscriber_member_id := NULL;
        NEW.block := pg_catalog.jsonb_build_object('state', NEW.state);
    END IF;
    RETURN NEW;
END;
$body$;
REVOKE ALL ON FUNCTION {schema}.el_cutoff_coverage() FROM PUBLIC;
CREATE FUNCTION {schema}.el_cutoff_attempt() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {schema}, pg_temp
    AS $body$
BEGIN
    IF NEW.answer IS NOT NULL AND {schema}.el_past_cutoff((
            SELECT v.appointment_ref FROM {schema}.m05_el_verifications AS v
             WHERE v.verification_id = NEW.verification_id)) THEN
        NEW.answer := (SELECT pg_catalog.jsonb_object_agg(e.key, e.value)
                         FROM pg_catalog.jsonb_each(NEW.answer) AS e
                        WHERE e.key IN ('class', 'reason', 'observed_at'));
    END IF;
    RETURN NEW;
END;
$body$;
REVOKE ALL ON FUNCTION {schema}.el_cutoff_attempt() FROM PUBLIC;
-- Codex 7a2e2b91 finding 2: the return budget (M1) is per appointment AND coverage
-- version. E1's trigger re-plan reuses the row; a version change resets `returned`, a
-- same-version upsert or replay keeps it.
CREATE FUNCTION {schema}.el_trigger_version_reset() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {schema}, pg_temp
    AS $body$
BEGIN
    IF NEW.coverage_version IS DISTINCT FROM OLD.coverage_version THEN
        NEW.returned := false;
    END IF;
    RETURN NEW;
END;
$body$;
REVOKE ALL ON FUNCTION {schema}.el_trigger_version_reset() FROM PUBLIC;
CREATE TRIGGER m05_el_coverage_cutoff
    BEFORE INSERT OR UPDATE ON {schema}.m05_el_coverage
    FOR EACH ROW EXECUTE FUNCTION {schema}.el_cutoff_coverage();
CREATE TRIGGER m05_el_attempts_cutoff
    BEFORE INSERT OR UPDATE ON {schema}.m05_el_attempts
    FOR EACH ROW EXECUTE FUNCTION {schema}.el_cutoff_attempt();
CREATE TRIGGER m05_el_triggers_version_reset
    BEFORE UPDATE ON {schema}.m05_el_triggers
    FOR EACH ROW EXECUTE FUNCTION {schema}.el_trigger_version_reset();

-- t106:step:runtime_privileges
REVOKE ALL ON {schema}.m05_el_attempts, {schema}.m05_el_late_answers FROM haloflow_runtime;

-- t106:step:grants
GRANT SELECT, INSERT, UPDATE ON {schema}.m05_el_attempts TO haloflow_m05_owner;
GRANT SELECT, INSERT ON {schema}.m05_el_late_answers TO haloflow_m05_owner;
GRANT EXECUTE ON FUNCTION {schema}.el_past_cutoff(uuid) TO haloflow_m05_owner;
GRANT UPDATE (member_id, patient_name, patient_dob, subscriber_name, subscriber_dob,
    subscriber_member_id, block) ON {schema}.m05_el_coverage TO haloflow_m05_owner;
"""

# --- t107..t110 seams (owner only, IMMUTABLE) -----------------------------------------

_RETRY_PARAMS: Final = (("p_failed_at", "timestamptz"), ("p_now", "timestamptz"))
_TAKEOVER_PARAMS: Final = (("p_lease_until", "timestamptz"), ("p_now", "timestamptz"))
_FRESH_PARAMS: Final = (("p_observed", "timestamptz"), ("p_window_start", "timestamptz"))
_EXPIRED_PARAMS: Final = (("p_service_date", "text"), ("p_today", "text"))

_RETRY_BODY: Final = f"""
BEGIN
    RETURN p_now >= p_failed_at + interval '{RETRY_DELAY_S} seconds';
END;
"""
_TAKEOVER_BODY: Final = f"""
BEGIN
    RETURN p_now >= p_lease_until + interval '{TAKEOVER_MARGIN_S} seconds';
END;
"""
_FRESH_BODY: Final = """
BEGIN
    RETURN p_observed >= p_window_start;
END;
"""
_EXPIRED_BODY: Final = f"""
BEGIN
    RETURN p_today::date > p_service_date::date + {EXPIRY_DAYS};
END;
"""

# --- shared fragments -------------------------------------------------------------------

_S: Final = "{schema}"
_LOCAL_DATE: Final = "(pg_catalog.timezone({z}, {t}))::date"


def _local_date(zone: str, instant: str) -> str:
    return _LOCAL_DATE.format(z=zone, t=instant)


def _date_text(expr: str) -> str:
    return f"pg_catalog.to_char({expr}, 'YYYY-MM-DD')"


def _midnight(zone: str, instant: str) -> str:
    local = f"({_local_date(zone, instant)}) OPERATOR(pg_catalog.+) time '00:00'"
    return f"{_LI}({_iso(local)}, {zone})"


def _digest(ref: str, member: str) -> str:
    return (f"pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
            f"({ref})::text || '|' || COALESCE({member}, ''), 'UTF8')), 'hex')")


def _keys(*names: str) -> str:
    sorted_names = ", ".join(f"'{n}'" for n in sorted(names))
    return f"""
    IF v_keys IS DISTINCT FROM ARRAY[{sorted_names}]::text[] THEN
        {_MALFORMED}
    END IF;
"""


def _uuid_field(name: str, var: str) -> str:
    return f"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{name}')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) '{name}') OPERATOR(pg_catalog.!~) c_uuid THEN
        {_MALFORMED}
    END IF;
    {var} := (p_command OPERATOR(pg_catalog.->>) '{name}')::uuid;
"""


def _int_field(name: str, var: str, low: int, high: int) -> str:
    return f"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{name}')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) '{name}') OPERATOR(pg_catalog.!~) '^[0-9]{{1,9}}$'
       OR (p_command OPERATOR(pg_catalog.->>) '{name}')::integer NOT BETWEEN {low} AND {high} THEN
        {_MALFORMED}
    END IF;
    {var} := (p_command OPERATOR(pg_catalog.->>) '{name}')::integer;
"""


def _head(declare: str, keys: tuple[str, ...]) -> str:
    return ("\nDECLARE" + _DECLARE_COMMON + declare + "\nBEGIN" + _TENANT + _keys(*keys)
            + _ATTRIBUTION + _workload("timer") + "\n    v_now := pg_catalog.clock_timestamp();\n")


def _out(**pairs: str) -> str:
    inner = ", ".join(f"'{k}', {v}" for k, v in pairs.items())
    return f"pg_catalog.jsonb_build_object('schema_version', 1, {inner})"


def _ret(outcome: str) -> str:
    return f"RETURN {_out(outcome=repr(outcome))};"


# Revalidation (R2) of a locked verification v_v against its key row: sets v_reval to NULL
# (still valid) or 'superseded' | 'cancelled' | 'activation_off' and applies the state.
_REVALIDATE: Final = r"""
    SELECT a.status, a.coverage_version INTO v_k FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = v_v.appointment_ref;
    SELECT s.enabled INTO v_enabled FROM {schema}.m05_el_settings AS s;
    v_reval := NULL;
    IF v_v.coverage_version <> v_k.coverage_version THEN
        UPDATE {schema}.m05_el_verifications SET state = 'superseded', lease_until = NULL
         WHERE verification_id = v_v.verification_id;
        v_reval := 'superseded';
    ELSIF v_k.status <> 'booked' THEN
        UPDATE {schema}.m05_el_verifications SET state = 'cancelled', lease_until = NULL
         WHERE verification_id = v_v.verification_id;
        v_reval := 'cancelled';
    ELSIF NOT v_enabled THEN
        UPDATE {schema}.m05_el_verifications SET state = 'due', lease_until = NULL
         WHERE verification_id = v_v.verification_id;
        v_reval := 'activation_off';
    END IF;
"""

# --- t111 due ---------------------------------------------------------------------------

_DUE_BODY: Final = _head(r"""
    v_now timestamptz;
    v_limit integer;
    v_s record;
    v_t record;
    v_k record;
    v_c record;
    v_vid uuid;
    v_ws timestamptz;
    v_digest text;
    v_sdate date;
    v_created integer := 0;
    v_attached integer := 0;
    v_fresh integer := 0;
    v_ids jsonb;
""", ("attribution", "limit", "schema_version")) + _int_field("limit", "v_limit", 1, 1000) + r"""
    SELECT s.enabled, s.lead_days INTO v_s FROM {schema}.m05_el_settings AS s;
    IF v_s.enabled THEN
        FOR v_t IN
            SELECT t.appointment_ref, t.trigger, t.returned
              FROM {schema}.m05_el_triggers AS t
              JOIN {schema}.m05_el_appointments AS a USING (appointment_ref)
             WHERE t.status = 'pending' AND t.due_at IS NOT NULL
               AND {schema}.m05_el_deadline_passed(t.due_at, v_now)
               AND a.status = 'booked' AND a.coverage_state = 'present'
               AND a.zone IS NOT NULL AND t.coverage_version = a.coverage_version
             ORDER BY t.appointment_ref,
                      CASE t.trigger WHEN 'booking' THEN 1 WHEN 'previsit' THEN 2 ELSE 3 END
        LOOP
            -- The key row first (E1 lock order); a busy appointment waits for the next pass.
            SELECT a.* INTO v_k FROM {schema}.m05_el_appointments AS a
             WHERE a.appointment_ref = v_t.appointment_ref FOR UPDATE SKIP LOCKED;
            IF NOT FOUND THEN
                CONTINUE;
            END IF;
            -- Re-read the trigger under the lock: still pending and current?
            PERFORM 1 FROM {schema}.m05_el_triggers AS t
             WHERE t.appointment_ref = v_t.appointment_ref AND t.trigger = v_t.trigger
               AND t.status = 'pending' AND t.coverage_version = v_k.coverage_version;
            IF NOT FOUND THEN
                CONTINUE;
            END IF;
            SELECT c.* INTO v_c FROM {schema}.m05_el_coverage AS c
             WHERE c.appointment_ref = v_k.appointment_ref
               AND c.coverage_version = v_k.coverage_version;
            v_digest := """ + _digest("v_k.appointment_ref", "v_c.member_id") + r""";
            v_sdate := """ + _local_date("v_k.zone", "v_k.start_at") + r""";
            IF v_t.trigger = 'previsit' THEN
                v_ws := """ + _days_before("v_k.start_at", "v_k.zone", "v_s.lead_days") + r""";
            ELSIF v_t.trigger = 'dayof' THEN
                v_ws := """ + _midnight("v_k.zone", "v_k.start_at") + r""";
            ELSE
                v_ws := NULL;
            END IF;
            -- (a) a fresh active result with the identical stored key (EL-5; M1 observed_at).
            v_vid := NULL;
            IF v_ws IS NOT NULL THEN
                SELECT v.verification_id INTO v_vid FROM {schema}.m05_el_verifications AS v
                 WHERE v.appointment_ref = v_k.appointment_ref AND v.state = 'settled'
                   AND v.result_class = 'active'
                   AND v.patient_id = v_k.patient_id AND v.payer_id = v_c.payer_id
                   AND v.member_digest = v_digest
                   AND v.coverage_version = v_k.coverage_version
                   AND v.service_date = v_sdate AND v.observed_at IS NOT NULL
                   AND {schema}.m05_el_fresh(v.observed_at, v_ws)
                 ORDER BY v.observed_at DESC LIMIT 1;
            END IF;
            IF v_vid IS NOT NULL THEN
                UPDATE {schema}.m05_el_triggers
                   SET status = 'skipped_fresh', verification_id = v_vid, updated_at = v_now
                 WHERE appointment_ref = v_k.appointment_ref AND trigger = v_t.trigger;
                v_fresh := v_fresh OPERATOR(pg_catalog.+) 1;
                CONTINUE;
            END IF;
            -- (b) attach to an unsettled verification of this coverage version; a trigger
            -- that was returned once never attaches again: it owns its next one (M1).
            SELECT v.verification_id INTO v_vid FROM {schema}.m05_el_verifications AS v
             WHERE v.appointment_ref = v_k.appointment_ref
               AND v.coverage_version = v_k.coverage_version
               AND v.state IN ('due', 'claimed')
               AND NOT v_t.returned
             ORDER BY v.created_at LIMIT 1;
            IF v_vid IS NOT NULL THEN
                UPDATE {schema}.m05_el_triggers
                   SET status = 'assigned', verification_id = v_vid, updated_at = v_now
                 WHERE appointment_ref = v_k.appointment_ref AND trigger = v_t.trigger;
                v_attached := v_attached OPERATOR(pg_catalog.+) 1;
                CONTINUE;
            END IF;
            -- (c) a new verification owned by this trigger.
            v_vid := pg_catalog.gen_random_uuid();
            INSERT INTO {schema}.m05_el_verifications (verification_id, appointment_ref,
                coverage_version, owner_trigger, state, created_at, patient_id, payer_id,
                member_digest, service_date)
            VALUES (v_vid, v_k.appointment_ref, v_k.coverage_version, v_t.trigger, 'due',
                v_now, v_k.patient_id, v_c.payer_id,
                -- retention cutoff at write time (Codex 7a2e2b91 finding 1)
                CASE WHEN {schema}.el_past_cutoff(v_k.appointment_ref) THEN NULL
                     ELSE v_digest END, v_sdate);
            UPDATE {schema}.m05_el_triggers
               SET status = 'assigned', verification_id = v_vid, updated_at = v_now
             WHERE appointment_ref = v_k.appointment_ref AND trigger = v_t.trigger;
            v_created := v_created OPERATOR(pg_catalog.+) 1;
        END LOOP;
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(x.id ORDER BY x.created_at, x.id), '[]'::jsonb)
      INTO v_ids
      FROM (SELECT v.verification_id::text AS id, v.created_at
              FROM {schema}.m05_el_verifications AS v
              JOIN {schema}.m05_el_appointments AS a USING (appointment_ref)
             WHERE v_s.enabled AND a.status = 'booked'
               AND ((v.state = 'due' AND NOT EXISTS (
                        SELECT 1 FROM {schema}.m05_el_attempts AS f
                         WHERE f.verification_id = v.verification_id
                           AND f.state IN ('timed_out', 'ambiguous')
                           AND (SELECT pg_catalog.count(*) FROM {schema}.m05_el_attempts AS g
                                 WHERE g.verification_id = v.verification_id) = 1
                           AND NOT {schema}.m05_el_retry_ready(f.finished_at, v_now)))
                 OR (v.state = 'claimed'
                     AND {schema}.m05_el_takeover_ready(v.lease_until, v_now)))
             ORDER BY v.created_at, v.verification_id
             LIMIT v_limit) AS x;
    RETURN """ + _out(outcome="'applied'", created="v_created", attached="v_attached",
                      skipped_fresh="v_fresh", verifications="v_ids") + r""";
END;
"""

# --- t112 claim (also the takeover) ----------------------------------------------------

_CLAIM_BODY: Final = _head(r"""
    v_now timestamptz;
    v_vid uuid;
    v_v record;
    v_takeover boolean;
    v_epoch integer;
""", ("attribution", "schema_version", "verification_id")) + _uuid_field(
    "verification_id", "v_vid") + r"""
    SELECT v.* INTO v_v FROM {schema}.m05_el_verifications AS v
     WHERE v.verification_id = v_vid FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        """ + _ret("not_claimable") + r"""
    END IF;
    IF v_v.state = 'due' THEN
        v_takeover := false;
    ELSIF v_v.state = 'claimed' AND v_v.lease_until IS NOT NULL
          AND {schema}.m05_el_takeover_ready(v_v.lease_until, v_now) THEN
        v_takeover := true;
        -- R1: an attempt left dispatched consumed its slot; it is never sent again.
        UPDATE {schema}.m05_el_attempts SET state = 'ambiguous', finished_at = v_now
         WHERE verification_id = v_vid AND state = 'dispatched';
    ELSE
        """ + _ret("not_claimable") + r"""
    END IF;
    v_epoch := v_v.epoch OPERATOR(pg_catalog.+) 1;
    UPDATE {schema}.m05_el_verifications
       SET state = 'claimed', epoch = v_epoch,
           lease_until = v_now OPERATOR(pg_catalog.+) interval '""" + str(LEASE_S) + r""" seconds'
     WHERE verification_id = v_vid;
    RETURN """ + _out(outcome="'claimed'", epoch="v_epoch", takeover="v_takeover") + r""";
END;
"""

# --- t113 dispatch ----------------------------------------------------------------------

_DISPATCH_BODY: Final = _head(r"""
    v_now timestamptz;
    v_vid uuid;
    v_epoch integer;
    v_v record;
    v_k record;
    v_a record;
    v_c record;
    v_enabled boolean;
    v_reval text;
    v_count integer;
    v_failed record;
    v_n integer;
    v_attempt uuid;
""", ("attribution", "epoch", "schema_version", "verification_id")) + _uuid_field(
    "verification_id", "v_vid") + _int_field("epoch", "v_epoch", 0, 1000000000) + r"""
    SELECT v.* INTO v_v FROM {schema}.m05_el_verifications AS v
     WHERE v.verification_id = v_vid FOR UPDATE;
    IF NOT FOUND OR v_v.epoch <> v_epoch THEN
        """ + _ret("fenced") + r"""
    END IF;
    IF v_v.state IN ('superseded', 'cancelled') THEN
        RETURN """ + _out(outcome="v_v.state") + r""";
    END IF;
    IF v_v.state <> 'claimed' THEN
        """ + _ret("fenced") + r"""
    END IF;
""" + _REVALIDATE + r"""
    IF v_reval IS NOT NULL THEN
        RETURN """ + _out(outcome="v_reval") + r""";
    END IF;
    IF EXISTS (SELECT 1 FROM {schema}.m05_el_attempts AS a
                WHERE a.verification_id = v_vid AND a.state = 'answered') THEN
        """ + _ret("answered") + r"""
    END IF;
    IF EXISTS (SELECT 1 FROM {schema}.m05_el_attempts AS a
                WHERE a.verification_id = v_vid AND a.state = 'dispatched') THEN
        """ + _ret("in_flight") + r"""
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_count FROM {schema}.m05_el_attempts AS a
     WHERE a.verification_id = v_vid;
    IF v_count >= 2 THEN
        """ + _ret("exhausted") + r"""
    END IF;
    IF v_count = 1 THEN
        SELECT a.finished_at INTO v_failed FROM {schema}.m05_el_attempts AS a
         WHERE a.verification_id = v_vid;
        IF NOT {schema}.m05_el_retry_ready(v_failed.finished_at, v_now) THEN
            -- D-5: release; m05_el_due lists it again once the delay is over.
            UPDATE {schema}.m05_el_verifications SET state = 'due', lease_until = NULL
             WHERE verification_id = v_vid;
            """ + _ret("retry_not_ready") + r"""
        END IF;
    END IF;
    v_n := v_count OPERATOR(pg_catalog.+) 1;
    v_attempt := pg_catalog.gen_random_uuid();
    -- R1: the attempt is durable (dispatched) before any port call; the caller commits first.
    INSERT INTO {schema}.m05_el_attempts (attempt_id, verification_id, n, epoch, state,
        dispatched_at)
    VALUES (v_attempt, v_vid, v_n, v_epoch, 'dispatched', v_now);
    UPDATE {schema}.m05_el_verifications
       SET lease_until = v_now OPERATOR(pg_catalog.+) interval '""" + str(LEASE_S) + r""" seconds'
     WHERE verification_id = v_vid;
    SELECT a.patient_id, a.zone, a.start_at INTO v_a FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = v_v.appointment_ref;
    SELECT c.* INTO v_c FROM {schema}.m05_el_coverage AS c
     WHERE c.appointment_ref = v_v.appointment_ref
       AND c.coverage_version = v_v.coverage_version;
    RETURN """ + _out(
        outcome="'dispatched'", attempt_id="v_attempt::text", n="v_n",
        request="pg_catalog.jsonb_build_object("
                "'attempt_id', v_attempt::text, 'tenant_id', v_setting, "
                "'appointment_ref', v_v.appointment_ref::text, "
                "'patient_id', v_a.patient_id::text, "
                "'coverage_version', v_v.coverage_version, 'payer_id', v_c.payer_id, "
                "'member_id', v_c.member_id, 'relationship', v_c.relationship, "
                "'patient_name', v_c.patient_name, 'patient_dob', v_c.patient_dob, "
                "'subscriber_name', v_c.subscriber_name, "
                "'subscriber_dob', v_c.subscriber_dob, "
                "'subscriber_member_id', v_c.subscriber_member_id, "
                "'service_date', " + _date_text(_local_date("v_a.zone", "v_a.start_at")) + ")"
    ) + r""";
END;
"""

# --- t114 record_answer ---------------------------------------------------------------

_ISO: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?([+-]\d{2}:\d{2}|Z)$"

_RESULT_CHECK: Final = r"""
    v_result := p_command OPERATOR(pg_catalog.->) 'result';
    v_echo := p_command OPERATOR(pg_catalog.->) 'echo';
    IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_echo) IS DISTINCT FROM 'object' THEN
        """ + _MALFORMED + r"""
    END IF;
    IF (SELECT pg_catalog.array_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_echo) AS k)
          IS DISTINCT FROM ARRAY[""" + _in(ECHO_KEYS) + r"""]::text[]
       OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_result) AS k
                   WHERE k NOT IN (""" + _in(RESULT_KEYS) + r"""))
       OR pg_catalog.jsonb_typeof(v_result OPERATOR(pg_catalog.->) 'class')
          IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_result OPERATOR(pg_catalog.->) 'reason')
          IS DISTINCT FROM 'string' THEN
        """ + _MALFORMED + r"""
    END IF;
    v_class := v_result OPERATOR(pg_catalog.->>) 'class';
    v_reason := v_result OPERATOR(pg_catalog.->>) 'reason';
    IF NOT ((v_class = 'active' AND v_reason = 'coverage_active')
         OR (v_class = 'inactive' AND v_reason = 'coverage_inactive')
         OR (v_class = 'inconclusive'
             AND v_reason IN (""" + _in(INCONCLUSIVE_REASONS) + r"""))) THEN
        """ + _MALFORMED + r"""
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_each(v_result) AS e
                WHERE (e.key IN (""" + _in(RESULT_STRINGS) + r""")
                       AND (pg_catalog.jsonb_typeof(e.value) IS DISTINCT FROM 'string'
                            OR pg_catalog.char_length(e.value OPERATOR(pg_catalog.#>>) '{}') > 64))
                   OR (e.key IN (""" + _in(RESULT_BOOLEANS) + r""")
                       AND pg_catalog.jsonb_typeof(e.value) IS DISTINCT FROM 'boolean')) THEN
        """ + _MALFORMED + r"""
    END IF;
    IF v_result OPERATOR(pg_catalog.?) 'observed_at' THEN
        IF pg_catalog.jsonb_typeof(v_result OPERATOR(pg_catalog.->) 'observed_at')
              IS DISTINCT FROM 'string'
           OR (v_result OPERATOR(pg_catalog.->>) 'observed_at')
              OPERATOR(pg_catalog.!~) '""" + _ISO + r"""' THEN
            """ + _MALFORMED + r"""
        END IF;
        BEGIN
            v_observed := (v_result OPERATOR(pg_catalog.->>) 'observed_at')::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            """ + _MALFORMED + r"""
        END;
    ELSIF v_reason <> 'response_unreadable' THEN
        """ + _MALFORMED + r"""
    END IF;
"""


def _late(cause: str, verification: str) -> str:
    return f"""
        INSERT INTO {_S}.m05_el_late_answers (late_id, attempt_id, verification_id, cause,
            class, received_at)
        VALUES (pg_catalog.gen_random_uuid(), v_aid, {verification}, '{cause}', v_class, v_now);
        RETURN {_out(outcome="'late'", cause=repr(cause))};
"""


_ANSWER_BODY: Final = _head(r"""
    v_now timestamptz;
    v_aid uuid;
    v_epoch integer;
    v_result jsonb;
    v_echo jsonb;
    v_class text;
    v_reason text;
    v_observed timestamptz;
    v_at record;
    v_v record;
    v_k record;
    v_member text;
""", ("attempt_id", "attribution", "echo", "epoch", "result", "schema_version")) + _uuid_field(
    "attempt_id", "v_aid") + _int_field("epoch", "v_epoch", 0, 1000000000) + _RESULT_CHECK + r"""
    SELECT a.* INTO v_at FROM {schema}.m05_el_attempts AS a
     WHERE a.attempt_id = v_aid FOR UPDATE;
    IF NOT FOUND THEN
""" + _late("unknown_attempt", "NULL") + r"""
    END IF;
    SELECT v.* INTO v_v FROM {schema}.m05_el_verifications AS v
     WHERE v.verification_id = v_at.verification_id;
    IF v_at.state <> 'dispatched' THEN
""" + _late("closed_attempt", "v_at.verification_id") + r"""
    END IF;
    IF v_v.state IN ('superseded', 'cancelled') THEN
""" + _late("superseded", "v_at.verification_id") + r"""
    END IF;
    IF v_v.epoch <> v_epoch OR v_at.epoch <> v_epoch THEN
""" + _late("stale_epoch", "v_at.verification_id") + r"""
    END IF;
    SELECT a.patient_id INTO v_k FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = v_v.appointment_ref;
    SELECT c.member_id INTO v_member FROM {schema}.m05_el_coverage AS c
     WHERE c.appointment_ref = v_v.appointment_ref
       AND c.coverage_version = v_v.coverage_version;
    IF (v_echo OPERATOR(pg_catalog.->>) 'attempt_id') IS DISTINCT FROM v_aid::text
       OR (v_echo OPERATOR(pg_catalog.->>) 'tenant_id') IS DISTINCT FROM v_setting
       OR (v_echo OPERATOR(pg_catalog.->>) 'appointment_ref')
          IS DISTINCT FROM v_v.appointment_ref::text
       OR (v_echo OPERATOR(pg_catalog.->>) 'patient_id') IS DISTINCT FROM v_k.patient_id::text
       OR (v_echo OPERATOR(pg_catalog.->>) 'member_id') IS DISTINCT FROM v_member
       OR pg_catalog.jsonb_typeof(v_echo OPERATOR(pg_catalog.->) 'coverage_version')
          IS DISTINCT FROM 'number'
       OR (v_echo OPERATOR(pg_catalog.->>) 'coverage_version')
          IS DISTINCT FROM v_v.coverage_version::text THEN
""" + _late("mismatch", "v_at.verification_id") + r"""
    END IF;
    UPDATE {schema}.m05_el_attempts
       SET state = 'answered', finished_at = v_now, answer = v_result
     WHERE attempt_id = v_aid;
    """ + _ret("recorded") + r"""
END;
"""

# --- t115 timeout -------------------------------------------------------------------------

_TIMEOUT_BODY: Final = _head(r"""
    v_now timestamptz;
    v_aid uuid;
    v_epoch integer;
    v_at record;
    v_v record;
""", ("attempt_id", "attribution", "epoch", "schema_version")) + _uuid_field(
    "attempt_id", "v_aid") + _int_field("epoch", "v_epoch", 0, 1000000000) + r"""
    SELECT a.* INTO v_at FROM {schema}.m05_el_attempts AS a
     WHERE a.attempt_id = v_aid FOR UPDATE;
    IF NOT FOUND THEN
        """ + _ret("not_dispatched") + r"""
    END IF;
    SELECT v.epoch INTO v_v FROM {schema}.m05_el_verifications AS v
     WHERE v.verification_id = v_at.verification_id;
    IF v_v.epoch <> v_epoch OR v_at.epoch <> v_epoch THEN
        """ + _ret("fenced") + r"""
    END IF;
    IF v_at.state <> 'dispatched' THEN
        """ + _ret("not_dispatched") + r"""
    END IF;
    UPDATE {schema}.m05_el_attempts SET state = 'timed_out', finished_at = v_now
     WHERE attempt_id = v_aid;
    """ + _ret("timed_out") + r"""
END;
"""

# --- t116 settle ------------------------------------------------------------------------

_SETTLE_BODY: Final = _head(r"""
    v_now timestamptz;
    v_vid uuid;
    v_epoch integer;
    v_ref uuid;
    v_key record;
    v_v record;
    v_k record;
    v_s record;
    v_enabled boolean;
    v_reval text;
    v_answer jsonb;
    v_count integer;
    v_open integer;
    v_class text;
    v_reason text;
    v_observed timestamptz;
    v_t record;
    v_ws timestamptz;
""", ("attribution", "epoch", "schema_version", "verification_id")) + _uuid_field(
    "verification_id", "v_vid") + _int_field("epoch", "v_epoch", 0, 1000000000) + r"""
    SELECT v.appointment_ref INTO v_ref FROM {schema}.m05_el_verifications AS v
     WHERE v.verification_id = v_vid;
    IF NOT FOUND THEN
        """ + _ret("fenced") + r"""
    END IF;
    -- E1 lock order: the KEY row (the case-event serialization point), then the verification.
    SELECT a.* INTO v_key FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = v_ref FOR UPDATE;
    SELECT v.* INTO v_v FROM {schema}.m05_el_verifications AS v
     WHERE v.verification_id = v_vid FOR UPDATE;
    IF v_v.epoch <> v_epoch THEN
        """ + _ret("fenced") + r"""
    END IF;
    IF v_v.state = 'settled' THEN
        -- Replay: the stored result; nothing is written again.
        RETURN """ + _out(outcome="'settled'", **{"class": "v_v.result_class"},
                          reason="v_v.reason") + r""";
    END IF;
    IF v_v.state IN ('superseded', 'cancelled') THEN
        RETURN """ + _out(outcome="v_v.state") + r""";
    END IF;
    IF v_v.state <> 'claimed' THEN
        """ + _ret("fenced") + r"""
    END IF;
""" + _REVALIDATE + r"""
    IF v_reval IS NOT NULL THEN
        RETURN """ + _out(outcome="v_reval") + r""";
    END IF;
    SELECT a.answer INTO v_answer FROM {schema}.m05_el_attempts AS a
     WHERE a.verification_id = v_vid AND a.state = 'answered' ORDER BY a.n DESC LIMIT 1;
    SELECT pg_catalog.count(*)::integer,
           pg_catalog.count(*) FILTER (WHERE a.state = 'dispatched')::integer
      INTO v_count, v_open
      FROM {schema}.m05_el_attempts AS a WHERE a.verification_id = v_vid;
    IF v_answer IS NOT NULL THEN
        v_class := v_answer OPERATOR(pg_catalog.->>) 'class';
        v_reason := v_answer OPERATOR(pg_catalog.->>) 'reason';
        v_observed := (v_answer OPERATOR(pg_catalog.->>) 'observed_at')::timestamptz;
    ELSIF v_count >= 2 AND v_open = 0 THEN
        v_class := 'inconclusive';
        v_reason := 'payer_unavailable';
        v_observed := NULL;
    ELSE
        """ + _ret("not_settleable") + r"""
    END IF;
    UPDATE {schema}.m05_el_verifications
       SET state = 'settled', result_class = v_class, reason = v_reason,
           observed_at = v_observed, settled_at = v_now, lease_until = NULL
     WHERE verification_id = v_vid;
    -- M1 trigger termination.
    SELECT s.lead_days INTO v_s FROM {schema}.m05_el_settings AS s;
    FOR v_t IN
        SELECT t.trigger, t.returned FROM {schema}.m05_el_triggers AS t
         WHERE t.appointment_ref = v_ref AND t.status = 'assigned' AND t.verification_id = v_vid
         ORDER BY t.trigger FOR UPDATE
    LOOP
        IF v_t.trigger = v_v.owner_trigger OR v_observed IS NULL OR v_t.returned
           OR v_t.trigger = 'booking' THEN
            v_ws := NULL;
        ELSIF v_t.trigger = 'previsit' THEN
            v_ws := """ + _days_before("v_key.start_at", "v_key.zone", "v_s.lead_days") + r""";
        ELSE
            v_ws := """ + _midnight("v_key.zone", "v_key.start_at") + r""";
        END IF;
        IF v_ws IS NULL OR {schema}.m05_el_fresh(v_observed, v_ws) THEN
            UPDATE {schema}.m05_el_triggers SET status = 'satisfied', updated_at = v_now
             WHERE appointment_ref = v_ref AND trigger = v_t.trigger;
        ELSE
            -- Observed before this attached trigger's window: back to pending ONCE; it then
            -- owns its next verification (M1).
            UPDATE {schema}.m05_el_triggers
               SET status = 'pending', verification_id = NULL, returned = true,
                   updated_at = v_now
             WHERE appointment_ref = v_ref AND trigger = v_t.trigger;
        END IF;
    END LOOP;
    -- One case event per settlement, carrying its verification id (Codex D-3).
    INSERT INTO {schema}.m05_el_case_events (event_id, appointment_ref, seq, coverage_version,
        kind, class, reason, created_at, verification_id)
    VALUES (pg_catalog.gen_random_uuid(), v_ref, v_key.next_seq, v_v.coverage_version,
        'open_or_note', v_class, v_reason, v_now, v_vid);
    UPDATE {schema}.m05_el_appointments
       SET next_seq = next_seq OPERATOR(pg_catalog.+) 1, updated_at = v_now
     WHERE appointment_ref = v_ref;
    RETURN """ + _out(outcome="'settled'", **{"class": "v_class"}, reason="v_reason") + r""";
END;
"""

# --- t117 expire ------------------------------------------------------------------------

_EXPIRE_BODY: Final = _head(r"""
    v_now timestamptz;
    v_limit integer;
    v_k record;
    v_expired integer := 0;
""", ("attribution", "limit", "schema_version")) + _int_field("limit", "v_limit", 1, 1000) + r"""
    -- Codex 7a2e2b91 finding 1: selection is by REMAINING data, never by a marker, so a
    -- later write for an already-expired appointment is found again; bounded and
    -- idempotent (a scrubbed appointment no longer matches).
    FOR v_k IN
        SELECT a.appointment_ref FROM {schema}.m05_el_appointments AS a
         WHERE (EXISTS (SELECT 1 FROM {schema}.m05_el_coverage AS c
                         WHERE c.appointment_ref = a.appointment_ref
                           AND (c.member_id IS NOT NULL OR c.patient_name IS NOT NULL
                                OR c.patient_dob IS NOT NULL OR c.subscriber_name IS NOT NULL
                                OR c.subscriber_dob IS NOT NULL
                                OR c.subscriber_member_id IS NOT NULL
                                OR (c.block OPERATOR(pg_catalog.-) 'state')
                                   <> '{}'::jsonb))
                OR EXISTS (SELECT 1 FROM {schema}.m05_el_verifications AS v
                            WHERE v.appointment_ref = a.appointment_ref
                              AND v.member_digest IS NOT NULL)
                OR EXISTS (SELECT 1 FROM {schema}.m05_el_attempts AS t
                             JOIN {schema}.m05_el_verifications AS v USING (verification_id)
                            WHERE v.appointment_ref = a.appointment_ref
                              AND t.answer IS NOT NULL
                              AND (t.answer OPERATOR(pg_catalog.-) 'class'
                                   OPERATOR(pg_catalog.-) 'reason'
                                   OPERATOR(pg_catalog.-) 'observed_at') <> '{}'::jsonb))
           -- the same cutoff as the write-time triggers, including the no-zone bound
           AND {schema}.el_past_cutoff(a.appointment_ref)
         ORDER BY a.appointment_ref
         LIMIT v_limit
         FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE {schema}.m05_el_coverage
           SET member_id = NULL, patient_name = NULL, patient_dob = NULL,
               subscriber_name = NULL, subscriber_dob = NULL, subscriber_member_id = NULL,
               block = pg_catalog.jsonb_build_object('state', state)
         WHERE appointment_ref = v_k.appointment_ref;
        UPDATE {schema}.m05_el_attempts AS a
           SET answer = (SELECT pg_catalog.jsonb_object_agg(e.key, e.value)
                           FROM pg_catalog.jsonb_each(a.answer) AS e
                          WHERE e.key IN ('class', 'reason', 'observed_at'))
          FROM {schema}.m05_el_verifications AS v
         WHERE a.verification_id = v.verification_id
           AND v.appointment_ref = v_k.appointment_ref AND a.answer IS NOT NULL;
        UPDATE {schema}.m05_el_verifications SET member_digest = NULL
         WHERE appointment_ref = v_k.appointment_ref;
        UPDATE {schema}.m05_el_appointments SET expired_at = v_now, updated_at = v_now
         WHERE appointment_ref = v_k.appointment_ref;
        v_expired := v_expired OPERATOR(pg_catalog.+) 1;
    END LOOP;
    RETURN """ + _out(outcome="'applied'", expired="v_expired") + r""";
END;
"""

# --- registration -----------------------------------------------------------------------

_SEAMS: Final = (
    (RETRY_SEAM_ID, "m05_el_retry_ready", _RETRY_PARAMS, _RETRY_BODY),
    (TAKEOVER_SEAM_ID, "m05_el_takeover_ready", _TAKEOVER_PARAMS, _TAKEOVER_BODY),
    (FRESH_SEAM_ID, "m05_el_fresh", _FRESH_PARAMS, _FRESH_BODY),
    (EXPIRED_SEAM_ID, "m05_el_expired", _EXPIRED_PARAMS, _EXPIRED_BODY),
)
_GATEWAYS: Final = (
    (DUE_ID, "m05_el_due", _DUE_BODY),
    (CLAIM_ID, "m05_el_claim", _CLAIM_BODY),
    (DISPATCH_ID, "m05_el_dispatch", _DISPATCH_BODY),
    (ANSWER_ID, "m05_el_record_answer", _ANSWER_BODY),
    (TIMEOUT_ID, "m05_el_timeout", _TIMEOUT_BODY),
    (SETTLE_ID, "m05_el_settle", _SETTLE_BODY),
    (EXPIRE_ID, "m05_el_expire", _EXPIRE_BODY),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {
    MIGRATION_ID: T106_SQL,
    **{mid: _typed(name, params, "bool", "immutable", body)
       for mid, name, params, body in _SEAMS},
    **{mid: _definition(name, "volatile", body, RUNTIME_ROLE) for mid, name, body in _GATEWAYS},
}

EL2_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
EL2_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _typed_profile(name, params, "bool", "immutable")
       for mid, name, params, _body in _SEAMS},
    **{mid: _profile(name, "volatile", RUNTIME_ROLE) for mid, name, _body in _GATEWAYS},
})
