"""Eligibility checks E1: per-tenant migration units t098..t105. SYNTHETIC ONLY.

Composed ONLY into the synthetic registry behind `include_el1` (`synthetic.composition`);
production's registry {36} and catalogue are unchanged. Governing: eligibility requirements
v1r1; architecture v1, v1r1, v1r2 amendment, v1r3 correction; stage-3 test cases v1 + v1r1;
E1 red packet v1r2 (Codex red gate PASS 61b5336d); owner decisions OD-EL1, OD-EL2, OD-EL9,
OD-EL10; Codex D-1 ruling (no usable zone: record, defer, diagnose, recover once).

- t098 (migrator SQL) `m05_el_settings` (singleton: enabled DEFAULT false, lead_days 3..5
  DEFAULT 3, not_received_days 0..5 DEFAULT 1) and `m05_el_priority_payers`. Support path
  only: no runtime privilege.
- t099 (migrator SQL) `m05_el_appointments` (the per-appointment KEY row: the serialization
  point, M2), `m05_el_coverage` (the only table holding identifiers), `m05_el_triggers`,
  `m05_el_verifications`, `m05_el_counts`, `m05_el_case_events`. No runtime privilege.
- t100 `m05_el_local_instant(timestamp, text)` (STABLE, owner only): the R5 local-time rule
  (ambiguous -> earlier instant; non-existent -> first valid instant). PostgreSQL's own
  `timezone(zone, timestamp)` reads a gap time with the pre-transition offset and an
  ambiguous time as standard time, so it is NOT used directly for due instants.
- t101 `m05_el_deadline_passed(timestamptz, timestamptz)` (IMMUTABLE, owner only): the tick's
  only decision seam, `p_due <= p_now`.
- t102 `m05_el_intake(jsonb)` (VOLATILE, owner only): enrolment, coverage versions, status
  changes and planning ('put' / 'plan'), called by t103 and t104 inside their transaction.
- t103 `m05_directory_v4(jsonb)` (runtime): `m05_directory_v3` + an optional `coverage` block
  on `appointment_put`; result unchanged.
- t104 `m05_el_tick(jsonb)` (runtime, timer workload): re-plans no-zone deferrals once a zone
  is usable, raises the OD-EL9 not-received event through t101, reports the diagnostic.
- t105 remove-only closure: runtime EXECUTE leaves `m05_directory_v3`.

Deviation disclosed in the green packet: the red packet pinned t098..t101 / target 101 before
the seam (red v1r1) and the local-time helper existed; E1 is t098..t105, target 105.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import AclAmendment, InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import (
    ACL_CLOSURE_KIND,
    TYPED_FUNCTION_KIND,
    UnitDefinition,
    UnitDefinitions,
)
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.intake_units import _ATTRIBUTION, _DECLARE_COMMON, _TENANT, _workload
from haloflow.m05.reminder_units import _definition, _profile
from haloflow.m05.writeback_units import _DIRECTORY_V3_BODY, _replace_once

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_Params = tuple[tuple[str, str], ...]

SETTINGS_ID: Final = "t098_m05_el_settings"
TABLES_ID: Final = "t099_m05_el_tables"
LOCAL_INSTANT_ID: Final = "t100_m05_el_local_instant"
SEAM_ID: Final = "t101_m05_el_deadline_passed"
INTAKE_ID: Final = "t102_m05_el_intake"
DIRECTORY_V4_ID: Final = "t103_m05_directory_v4"
TICK_ID: Final = "t104_m05_el_tick"
DIRECTORY_V3_CLOSURE_ID: Final = "t105_m05_directory_v3_closure"
EL1_TARGET: Final = 105

MANUAL_REASONS: Final = ("nonpriority", "no_coverage", "incomplete")
EVENT_REASONS: Final = (*MANUAL_REASONS, "coverage_not_received", "appointment_cancelled",
                        "appointment_rescheduled", "coverage_changed")
_PRESENT_KEYS: Final = ("member_id", "patient_dob", "patient_name", "payer_id", "relationship",
                        "state", "subscriber_dob", "subscriber_member_id", "subscriber_name")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# --- t098 / t099 migrator units ------------------------------------------------------------

T098_SQL: Final = r"""
-- t098:step:tables
CREATE TABLE {schema}.m05_el_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled boolean NOT NULL DEFAULT false,
    lead_days smallint NOT NULL DEFAULT 3 CHECK (lead_days BETWEEN 3 AND 5),
    not_received_days smallint NOT NULL DEFAULT 1 CHECK (not_received_days BETWEEN 0 AND 5),
    settings_version bigint NOT NULL DEFAULT 1 CHECK (settings_version >= 1),
    updated_at timestamptz NOT NULL
);
INSERT INTO {schema}.m05_el_settings (singleton, updated_at) VALUES (true, clock_timestamp());
CREATE TABLE {schema}.m05_el_priority_payers (
    payer_id varchar(32) PRIMARY KEY CHECK (char_length(payer_id) BETWEEN 1 AND 32)
);

-- t098:step:runtime_privileges
REVOKE ALL ON {schema}.m05_el_settings, {schema}.m05_el_priority_payers FROM haloflow_runtime;

-- t098:step:grants
GRANT SELECT ON {schema}.m05_el_settings, {schema}.m05_el_priority_payers
    TO haloflow_m05_owner;
"""

T099_SQL: Final = r"""
-- t099:step:tables
CREATE TABLE {schema}.m05_el_appointments (
    appointment_ref uuid PRIMARY KEY REFERENCES {schema}.m05_appointments (appointment_ref),
    patient_id uuid NOT NULL,
    start_at timestamptz NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('booked', 'cancelled', 'replaced')),
    department_tz text CHECK (department_tz IS NULL OR char_length(department_tz) <= 64),
    zone text CHECK (zone IS NULL OR char_length(zone) <= 64),
    deferred_reason varchar(16) CHECK (deferred_reason IS NULL OR deferred_reason = 'no_zone'),
    coverage_state varchar(8) NOT NULL CHECK (coverage_state IN ('pending', 'none', 'present')),
    coverage_version integer NOT NULL CHECK (coverage_version >= 1),
    not_received_due_at timestamptz,
    not_received_for integer,
    next_seq integer NOT NULL CHECK (next_seq >= 1),
    closure_watermark integer NOT NULL CHECK (closure_watermark >= 0),
    enrolled_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT m05_el_appointments_zone CHECK ((zone IS NULL) = (deferred_reason IS NOT NULL))
);
CREATE TABLE {schema}.m05_el_coverage (
    appointment_ref uuid NOT NULL
        REFERENCES {schema}.m05_el_appointments (appointment_ref),
    coverage_version integer NOT NULL CHECK (coverage_version >= 1),
    state varchar(8) NOT NULL CHECK (state IN ('pending', 'none', 'present')),
    completeness varchar(12) CHECK (completeness IS NULL
        OR completeness IN ('complete', 'incomplete')),
    payer_id varchar(64),
    member_id varchar(64),
    relationship varchar(64),
    patient_name varchar(64),
    patient_dob varchar(10),
    subscriber_name varchar(64),
    subscriber_dob varchar(10),
    subscriber_member_id varchar(64),
    block jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (appointment_ref, coverage_version),
    CONSTRAINT m05_el_coverage_completeness CHECK ((state = 'present') = (completeness IS NOT NULL))
);
CREATE TABLE {schema}.m05_el_verifications (
    verification_id uuid PRIMARY KEY,
    appointment_ref uuid NOT NULL
        REFERENCES {schema}.m05_el_appointments (appointment_ref),
    coverage_version integer NOT NULL CHECK (coverage_version >= 1),
    owner_trigger varchar(8) NOT NULL CHECK (owner_trigger IN ('booking', 'previsit', 'dayof')),
    state varchar(12) NOT NULL CHECK (state IN ('due', 'claimed', 'settled', 'cancelled',
        'superseded')),
    result_class varchar(16) CHECK (result_class IS NULL OR result_class IN ('active',
        'inactive', 'inconclusive', 'manual_required')),
    reason varchar(32),
    created_at timestamptz NOT NULL,
    settled_at timestamptz,
    CONSTRAINT m05_el_verifications_settled CHECK ((state = 'settled')
        = (result_class IS NOT NULL AND settled_at IS NOT NULL))
);
CREATE UNIQUE INDEX m05_el_verifications_owner ON {schema}.m05_el_verifications
    (appointment_ref, coverage_version, owner_trigger);
CREATE TABLE {schema}.m05_el_triggers (
    appointment_ref uuid NOT NULL
        REFERENCES {schema}.m05_el_appointments (appointment_ref),
    trigger varchar(8) NOT NULL CHECK (trigger IN ('booking', 'previsit', 'dayof')),
    coverage_version integer NOT NULL CHECK (coverage_version >= 1),
    due_at timestamptz,
    status varchar(16) NOT NULL CHECK (status IN ('pending', 'assigned', 'satisfied',
        'skipped_fresh', 'superseded', 'cancelled', 'not_applicable', 'deferred')),
    verification_id uuid REFERENCES {schema}.m05_el_verifications (verification_id),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (appointment_ref, trigger),
    CONSTRAINT m05_el_triggers_due CHECK ((status = 'deferred') = (due_at IS NULL))
);
CREATE TABLE {schema}.m05_el_counts (
    period varchar(7) NOT NULL CHECK (period ~ '^[0-9]{4}-[0-9]{2}$'),
    bucket varchar(16) NOT NULL CHECK (bucket IN ('nonpriority', 'no_coverage', 'incomplete',
        'not_received')),
    payer_key varchar(64) NOT NULL CHECK (char_length(payer_key) >= 1),
    appointment_ref uuid NOT NULL
        REFERENCES {schema}.m05_el_appointments (appointment_ref),
    counted_at timestamptz NOT NULL,
    PRIMARY KEY (period, bucket, payer_key, appointment_ref)
);
CREATE TABLE {schema}.m05_el_case_events (
    event_id uuid PRIMARY KEY,
    appointment_ref uuid NOT NULL
        REFERENCES {schema}.m05_el_appointments (appointment_ref),
    seq integer NOT NULL CHECK (seq >= 1),
    coverage_version integer NOT NULL CHECK (coverage_version >= 1),
    kind varchar(16) NOT NULL CHECK (kind IN ('open_or_note', 'note_only')),
    class varchar(16) CHECK (class IS NULL OR class IN ('active', 'inactive', 'inconclusive',
        'manual_required')),
    reason varchar(32) NOT NULL CHECK (reason IN (""" + _in(EVENT_REASONS) + r""")),
    created_at timestamptz NOT NULL,
    applied_at timestamptz,
    UNIQUE (appointment_ref, seq),
    CONSTRAINT m05_el_case_events_kind CHECK ((kind = 'note_only') = (class IS NULL))
);

-- t099:step:runtime_privileges
REVOKE ALL ON {schema}.m05_el_appointments, {schema}.m05_el_coverage,
    {schema}.m05_el_verifications, {schema}.m05_el_triggers, {schema}.m05_el_counts,
    {schema}.m05_el_case_events FROM haloflow_runtime;

-- t099:step:grants
GRANT SELECT, INSERT, UPDATE ON {schema}.m05_el_appointments, {schema}.m05_el_triggers,
    {schema}.m05_el_verifications TO haloflow_m05_owner;
GRANT SELECT, INSERT ON {schema}.m05_el_coverage, {schema}.m05_el_counts
    TO haloflow_m05_owner;
GRANT SELECT, INSERT, UPDATE (applied_at) ON {schema}.m05_el_case_events
    TO haloflow_m05_owner;
"""

# --- t100 local instant / t101 seam (typed, owner only, non-jsonb parameters) --------------

_LOCAL_INSTANT_BODY: Final = r"""
DECLARE
    v_local timestamp;
    v_read timestamptz;
    v_off interval;
    v_cand timestamptz;
    v_best timestamptz;
    v_lo timestamptz;
    v_hi timestamptz;
    v_mid timestamptz;
    v_new interval;
BEGIN
    IF p_local IS NULL OR p_zone IS NULL THEN
        RETURN NULL;
    END IF;
    v_local := p_local::timestamp;
    -- PostgreSQL's own reading, used only to find the offsets in force nearby.
    v_read := pg_catalog.timezone(p_zone, v_local);
    FOR v_off IN
        SELECT DISTINCT pg_catalog.timezone(p_zone, v_read OPERATOR(pg_catalog.+) d.d)
                OPERATOR(pg_catalog.-)
                pg_catalog.timezone('UTC', v_read OPERATOR(pg_catalog.+) d.d)
          FROM (VALUES (interval '-6 hours'), (interval '0'), (interval '6 hours')) AS d (d)
    LOOP
        v_cand := pg_catalog.timezone('UTC', v_local OPERATOR(pg_catalog.-) v_off);
        IF pg_catalog.timezone(p_zone, v_cand) = v_local THEN
            -- Valid reading; ambiguous local times have two: take the EARLIER instant.
            IF v_best IS NULL OR v_cand < v_best THEN
                v_best := v_cand;
            END IF;
        END IF;
        IF v_lo IS NULL OR v_cand < v_lo THEN
            v_lo := v_cand;
        END IF;
        IF v_hi IS NULL OR v_cand > v_hi THEN
            v_hi := v_cand;
        END IF;
    END LOOP;
    IF v_best IS NOT NULL THEN
        RETURN v_best;
    END IF;
    -- Non-existent local time (a gap): the first valid instant is the transition. The two
    -- readings straddle it; bisect to the first whole second carrying the later offset.
    v_new := pg_catalog.timezone(p_zone, v_hi) OPERATOR(pg_catalog.-)
             pg_catalog.timezone('UTC', v_hi);
    WHILE v_hi OPERATOR(pg_catalog.-) v_lo > interval '1 second' LOOP
        -- Explicit grouping: OPERATOR() forms share one precedence and bind left to right.
        v_mid := pg_catalog.date_trunc('second', v_lo OPERATOR(pg_catalog.+)
                 ((v_hi OPERATOR(pg_catalog.-) v_lo) OPERATOR(pg_catalog./) 2));
        IF pg_catalog.timezone(p_zone, v_mid) OPERATOR(pg_catalog.-)
           pg_catalog.timezone('UTC', v_mid) = v_new THEN
            v_hi := v_mid;
        ELSE
            v_lo := v_mid;
        END IF;
    END LOOP;
    RETURN v_hi;
END;
"""

_SEAM_BODY: Final = r"""
BEGIN
    RETURN p_due <= p_now;
END;
"""

_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE", "immutable": "IMMUTABLE"}
_CODE: Final = {"stable": "s", "volatile": "v", "immutable": "i"}


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _typed(name: str, params: _Params, returns: str, volatility: str,
           body: str) -> UnitDefinition:
    """An owner-only typed SECURITY DEFINER function with non-jsonb parameters."""
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    template = (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS {returns}\n"
        f"    LANGUAGE plpgsql {_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
    )
    acl: list[dict[str, Any]] = []
    return UnitDefinition(
        template=template, execution_role=M05_OWNER_ROLE, kind=TYPED_FUNCTION_KIND,
        policy={
            "policy_format": 1, "semantic_version": 1, "parser_package": "pglast",
            "parser_version": "7.21", "grammar_major": 17,
            "functions": [{
                "schema": _S, "name": name,
                "inputs": [{"name": p, "type": t} for p, t in params],
                "outputs": [], "return_type": returns, "language": "plpgsql",
                "is_procedure": False, "replace": False, "security_definer": True,
                "volatility": volatility, "parallel": "unsafe", "strict": False,
                "config": [_CONFIG], "body_sha256": _sha(body), "acl": acl, "comment": None,
            }],
        },
        policy_verification={
            "kind": "function_metadata",
            "functions": [{
                "name": name, "argument_types": [t for _, t in params],
                "owner": M05_OWNER_ROLE, "security_definer": True, "config": [_CONFIG],
                "acl": acl, "body": body,
            }],
        },
    )


def _typed_profile(name: str, params: _Params, returns: str,
                   volatility: str) -> InstalledStateProfile:
    return InstalledStateProfile(
        function_name=name, argument_types=tuple(t for _, t in params),
        owner=M05_OWNER_ROLE, execution_role=M05_OWNER_ROLE, prokind="f",
        return_type=returns, returns_set=False, language="plpgsql", security_definer=True,
        volatility=_CODE[volatility], parallel="u", strict=False,
        installed_acl=((M05_OWNER_ROLE, "EXECUTE", M05_OWNER_ROLE, False),),
    )


# The platform admits only closed scalar parameter types (F-INT1: no `timestamp`), so the
# local date-time travels as ISO-8601 text 'YYYY-MM-DDTHH24:MI:SS.US' (DateStyle-independent).
_LOCAL_PARAMS: Final = (("p_local", "text"), ("p_zone", "text"))
_SEAM_PARAMS: Final = (("p_due", "timestamptz"), ("p_now", "timestamptz"))

# --- shared SQL fragments --------------------------------------------------------------

_LI: Final = f"{_S}.m05_el_local_instant"


def _iso(local_expr: str) -> str:
    """A local `timestamp` expression as the t100 ISO text (DateStyle-independent)."""
    return f"pg_catalog.to_char({local_expr}, 'YYYY-MM-DD\"T\"HH24:MI:SS.US')"


def _days_before(start: str, zone: str, days: str) -> str:
    """The R5 instant: local start date minus `days`, local time of day kept."""
    local = (f"((pg_catalog.timezone({zone}, {start}))::date OPERATOR(pg_catalog.-)"
             f" ({days})::integer) OPERATOR(pg_catalog.+)"
             f" (pg_catalog.timezone({zone}, {start}))::time")
    return f"{_LI}({_iso(local)}, {zone})"


def _dayof(start: str, zone: str) -> str:
    day = f"(pg_catalog.timezone({zone}, {start}))::date"
    seven = _iso(f"{day} OPERATOR(pg_catalog.+) time '07:00'")
    midnight = _iso(f"{day} OPERATOR(pg_catalog.+) time '00:00'")
    return (f"GREATEST(LEAST({_LI}({seven}, {zone}),"
            f" {start} OPERATOR(pg_catalog.-) interval '2 hours'),"
            f" {_LI}({midnight}, {zone}))")


def _zone_choice(dept: str, clinic: str) -> str:
    def ok(expr: str) -> str:
        return (f"(SELECT z.name FROM pg_catalog.pg_timezone_names AS z WHERE z.name = ({expr})"
                f" AND pg_catalog.char_length({expr}) BETWEEN 1 AND 64 LIMIT 1)")
    return f"COALESCE({ok(dept)}, {ok(clinic)})"


_MALFORMED: Final = "RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';"

# --- t102 intake (owner only) ---------------------------------------------------------

_INTAKE_BODY: Final = r"""
DECLARE
    v_op text;
    v_ref uuid;
    v_k record;
    v_c record;
    v_s record;
    v_clinic text;
    v_zone text;
    v_cov jsonb;
    v_has boolean;
    v_state text;
    v_status text;
    v_start timestamptz;
    v_patient uuid;
    v_dept text;
    v_old jsonb;
    v_version integer;
    v_reason text;
    v_vid uuid;
    v_now timestamptz;
    v_pre timestamptz;
    v_day timestamptz;
    v_nr timestamptz;
BEGIN
    v_op := p_command OPERATOR(pg_catalog.->>) 'op';
    v_ref := (p_command OPERATOR(pg_catalog.->>) 'appointment_ref')::uuid;
    v_now := pg_catalog.clock_timestamp();
    SELECT s.enabled, s.lead_days, s.not_received_days INTO v_s
      FROM {schema}.m05_el_settings AS s;
    SELECT r.clinic_tz INTO v_clinic FROM {schema}.m05_reminder_settings AS r;

    IF v_op = 'plan' THEN
        -- Plan the CURRENT coverage version of a locked, enrolled, booked appointment.
        SELECT * INTO v_k FROM {schema}.m05_el_appointments AS a
         WHERE a.appointment_ref = v_ref FOR UPDATE;
        SELECT * INTO v_c FROM {schema}.m05_el_coverage AS c
         WHERE c.appointment_ref = v_ref AND c.coverage_version = v_k.coverage_version;
        IF v_k.zone IS NULL THEN
            -- Codex D-1: no usable zone -> every date-dependent action waits.
            INSERT INTO {schema}.m05_el_triggers AS t (appointment_ref, trigger,
                coverage_version, due_at, status, verification_id, updated_at)
            SELECT v_ref, x.t, v_k.coverage_version, NULL, 'deferred', NULL, v_now
              FROM (VALUES ('booking'), ('previsit'), ('dayof')) AS x (t)
            ON CONFLICT (appointment_ref, trigger) DO UPDATE
               SET coverage_version = EXCLUDED.coverage_version, due_at = NULL,
                   status = 'deferred', verification_id = NULL, updated_at = v_now;
            UPDATE {schema}.m05_el_appointments
               SET deferred_reason = 'no_zone', not_received_due_at = NULL, updated_at = v_now
             WHERE appointment_ref = v_ref;
            RETURN pg_catalog.jsonb_build_object('outcome', 'deferred');
        END IF;
        v_pre := """ + _days_before("v_k.start_at", "v_k.zone", "v_s.lead_days") + r""";
        v_day := """ + _dayof("v_k.start_at", "v_k.zone") + r""";
        v_reason := NULL;
        IF v_c.state = 'none' THEN
            v_reason := 'no_coverage';
        ELSIF v_c.state = 'present' AND v_c.completeness = 'incomplete' THEN
            v_reason := 'incomplete';
        ELSIF v_c.state = 'present' AND NOT EXISTS (
                SELECT 1 FROM {schema}.m05_el_priority_payers AS p
                 WHERE p.payer_id = v_c.payer_id) THEN
            v_reason := 'nonpriority';
        END IF;
        IF v_c.state = 'pending' THEN
            v_nr := CASE WHEN v_s.not_received_days = 0 THEN v_day
                         ELSE """ + _days_before("v_k.start_at", "v_k.zone",
                                                  "v_s.not_received_days") + r""" END;
        ELSE
            v_nr := NULL;
        END IF;
        UPDATE {schema}.m05_el_appointments
           SET deferred_reason = NULL, not_received_due_at = v_nr, updated_at = v_now
         WHERE appointment_ref = v_ref;
        IF v_reason IS NULL THEN
            -- Checkable (or pending): three pending triggers with their due instants.
            INSERT INTO {schema}.m05_el_triggers AS t (appointment_ref, trigger,
                coverage_version, due_at, status, verification_id, updated_at)
            VALUES (v_ref, 'booking', v_k.coverage_version, v_now, 'pending', NULL, v_now),
                   (v_ref, 'previsit', v_k.coverage_version, v_pre, 'pending', NULL, v_now),
                   (v_ref, 'dayof', v_k.coverage_version, v_day, 'pending', NULL, v_now)
            ON CONFLICT (appointment_ref, trigger) DO UPDATE
               SET coverage_version = EXCLUDED.coverage_version, due_at = EXCLUDED.due_at,
                   status = 'pending', verification_id = NULL, updated_at = v_now;
            RETURN pg_catalog.jsonb_build_object('outcome', 'planned');
        END IF;
        -- Manual (OD-EL2): one settled verification per coverage version (replay-safe).
        SELECT v.verification_id INTO v_vid FROM {schema}.m05_el_verifications AS v
         WHERE v.appointment_ref = v_ref AND v.coverage_version = v_k.coverage_version
           AND v.owner_trigger = 'booking';
        IF FOUND THEN
            RETURN pg_catalog.jsonb_build_object('outcome', 'unchanged');
        END IF;
        v_vid := pg_catalog.gen_random_uuid();
        INSERT INTO {schema}.m05_el_verifications (verification_id, appointment_ref,
            coverage_version, owner_trigger, state, result_class, reason, created_at,
            settled_at)
        VALUES (v_vid, v_ref, v_k.coverage_version, 'booking', 'settled', 'manual_required',
            v_reason, v_now, v_now);
        INSERT INTO {schema}.m05_el_triggers AS t (appointment_ref, trigger, coverage_version,
            due_at, status, verification_id, updated_at)
        VALUES (v_ref, 'booking', v_k.coverage_version, v_now, 'satisfied', v_vid, v_now),
               (v_ref, 'previsit', v_k.coverage_version, v_pre, 'not_applicable', NULL, v_now),
               (v_ref, 'dayof', v_k.coverage_version, v_day, 'not_applicable', NULL, v_now)
        ON CONFLICT (appointment_ref, trigger) DO UPDATE
           SET coverage_version = EXCLUDED.coverage_version, due_at = EXCLUDED.due_at,
               status = EXCLUDED.status, verification_id = EXCLUDED.verification_id,
               updated_at = v_now;
        INSERT INTO {schema}.m05_el_counts (period, bucket, payer_key, appointment_ref,
            counted_at)
        VALUES (pg_catalog.to_char(pg_catalog.timezone(v_k.zone, v_k.start_at), 'YYYY-MM'),
            v_reason, CASE WHEN v_reason = 'nonpriority' THEN v_c.payer_id ELSE '-' END,
            v_ref, v_now)
        ON CONFLICT DO NOTHING;
        INSERT INTO {schema}.m05_el_case_events (event_id, appointment_ref, seq,
            coverage_version, kind, class, reason, created_at)
        VALUES (pg_catalog.gen_random_uuid(), v_ref, v_k.next_seq, v_k.coverage_version,
            'open_or_note', 'manual_required', v_reason, v_now);
        UPDATE {schema}.m05_el_appointments
           SET next_seq = next_seq OPERATOR(pg_catalog.+) 1, updated_at = v_now
         WHERE appointment_ref = v_ref;
        RETURN pg_catalog.jsonb_build_object('outcome', 'manual');
    END IF;

    -- op = 'put' (from m05_directory_v4, after its own writes, in its transaction).
    v_patient := (p_command OPERATOR(pg_catalog.->>) 'patient_id')::uuid;
    v_start := (p_command OPERATOR(pg_catalog.->>) 'start_at')::timestamptz;
    v_status := p_command OPERATOR(pg_catalog.->>) 'status';
    v_dept := p_command OPERATOR(pg_catalog.->>) 'department_tz';
    v_has := (p_command OPERATOR(pg_catalog.->) 'has_coverage')::text = 'true';
    v_cov := CASE WHEN v_has THEN p_command OPERATOR(pg_catalog.->) 'coverage' END;
    v_state := v_cov OPERATOR(pg_catalog.->>) 'state';
    -- The appointment KEY row is the serialization point (M2): lock it before reading.
    SELECT * INTO v_k FROM {schema}.m05_el_appointments AS a
     WHERE a.appointment_ref = v_ref FOR UPDATE;
    IF NOT FOUND THEN
        -- M3: only an explicit block, while enabled, enrols a booked appointment.
        IF NOT v_has OR NOT v_s.enabled OR v_status <> 'booked' THEN
            RETURN pg_catalog.jsonb_build_object('outcome', 'not_enrolled');
        END IF;
        v_zone := """ + _zone_choice("v_dept", "v_clinic") + r""";
        INSERT INTO {schema}.m05_el_appointments (appointment_ref, patient_id, start_at, status,
            department_tz, zone, deferred_reason, coverage_state, coverage_version,
            not_received_due_at, not_received_for, next_seq, closure_watermark, enrolled_at,
            updated_at)
        VALUES (v_ref, v_patient, v_start, 'booked', v_dept, v_zone,
            CASE WHEN v_zone IS NULL THEN 'no_zone' END, v_state, 1, NULL, NULL, 1, 0, v_now,
            v_now);
        v_version := 1;
    ELSE
        IF v_status <> 'booked' THEN
            IF v_k.status = v_status THEN
                RETURN pg_catalog.jsonb_build_object('outcome', 'unchanged');
            END IF;
            UPDATE {schema}.m05_el_appointments
               SET status = v_status, updated_at = v_now WHERE appointment_ref = v_ref;
            UPDATE {schema}.m05_el_triggers
               SET status = 'cancelled', updated_at = v_now
             WHERE appointment_ref = v_ref AND status IN ('pending', 'assigned', 'deferred');
            UPDATE {schema}.m05_el_verifications
               SET state = 'cancelled'
             WHERE appointment_ref = v_ref AND state IN ('due', 'claimed');
            INSERT INTO {schema}.m05_el_case_events (event_id, appointment_ref, seq,
                coverage_version, kind, class, reason, created_at)
            VALUES (pg_catalog.gen_random_uuid(), v_ref, v_k.next_seq, v_k.coverage_version,
                'note_only', NULL, CASE WHEN v_status = 'cancelled'
                    THEN 'appointment_cancelled' ELSE 'appointment_rescheduled' END, v_now);
            UPDATE {schema}.m05_el_appointments
               SET next_seq = next_seq OPERATOR(pg_catalog.+) 1 WHERE appointment_ref = v_ref;
            RETURN pg_catalog.jsonb_build_object('outcome', 'status');
        END IF;
        IF v_k.status <> 'booked' THEN
            -- A cancelled or replaced appointment is not re-enrolled by a later put.
            RETURN pg_catalog.jsonb_build_object('outcome', 'unchanged');
        END IF;
        IF NOT v_has THEN
            -- C2: omission keeps enrolment and the current coverage version.
            IF v_start IS DISTINCT FROM v_k.start_at THEN
                UPDATE {schema}.m05_el_appointments SET start_at = v_start, updated_at = v_now
                 WHERE appointment_ref = v_ref;
                RETURN {schema}.m05_el_intake(pg_catalog.jsonb_build_object('op', 'plan',
                    'appointment_ref', v_ref::text));
            END IF;
            RETURN pg_catalog.jsonb_build_object('outcome', 'unchanged');
        END IF;
        SELECT c.block INTO v_old FROM {schema}.m05_el_coverage AS c
         WHERE c.appointment_ref = v_ref AND c.coverage_version = v_k.coverage_version;
        IF v_old = v_cov THEN
            IF v_start IS DISTINCT FROM v_k.start_at THEN
                UPDATE {schema}.m05_el_appointments SET start_at = v_start, updated_at = v_now
                 WHERE appointment_ref = v_ref;
                RETURN {schema}.m05_el_intake(pg_catalog.jsonb_build_object('op', 'plan',
                    'appointment_ref', v_ref::text));
            END IF;
            RETURN pg_catalog.jsonb_build_object('outcome', 'unchanged');
        END IF;
        -- A new coverage version (R2): supersede the old version's unsettled work.
        v_version := v_k.coverage_version OPERATOR(pg_catalog.+) 1;
        UPDATE {schema}.m05_el_verifications
           SET state = 'superseded'
         WHERE appointment_ref = v_ref AND state IN ('due', 'claimed');
        UPDATE {schema}.m05_el_appointments
           SET coverage_state = v_state, coverage_version = v_version, start_at = v_start,
               updated_at = v_now
         WHERE appointment_ref = v_ref;
        IF EXISTS (SELECT 1 FROM {schema}.m05_el_case_events AS e
                    WHERE e.appointment_ref = v_ref) THEN
            INSERT INTO {schema}.m05_el_case_events (event_id, appointment_ref, seq,
                coverage_version, kind, class, reason, created_at)
            VALUES (pg_catalog.gen_random_uuid(), v_ref, v_k.next_seq, v_version,
                'note_only', NULL, 'coverage_changed', v_now);
            UPDATE {schema}.m05_el_appointments
               SET next_seq = next_seq OPERATOR(pg_catalog.+) 1 WHERE appointment_ref = v_ref;
        END IF;
    END IF;
    INSERT INTO {schema}.m05_el_coverage (appointment_ref, coverage_version, state,
        completeness, payer_id, member_id, relationship, patient_name, patient_dob,
        subscriber_name, subscriber_dob, subscriber_member_id, block, received_at)
    VALUES (v_ref, v_version, v_state,
        CASE WHEN v_state <> 'present' THEN NULL
             WHEN (v_cov OPERATOR(pg_catalog.->>) 'relationship') NOT IN ('self', 'dependent')
               OR COALESCE(v_cov OPERATOR(pg_catalog.->>) 'payer_id', '') = ''
               OR COALESCE(v_cov OPERATOR(pg_catalog.->>) 'member_id', '') = ''
               OR COALESCE(v_cov OPERATOR(pg_catalog.->>) 'patient_name', '') = ''
               OR COALESCE(v_cov OPERATOR(pg_catalog.->>) 'patient_dob', '') = ''
               OR ((v_cov OPERATOR(pg_catalog.->>) 'relationship') = 'dependent' AND (
                      COALESCE(v_cov OPERATOR(pg_catalog.->>) 'subscriber_name', '') = ''
                   OR COALESCE(v_cov OPERATOR(pg_catalog.->>) 'subscriber_dob', '') = ''
                   OR COALESCE(v_cov OPERATOR(pg_catalog.->>) 'subscriber_member_id', '')
                      = ''))
             THEN 'incomplete' ELSE 'complete' END,
        v_cov OPERATOR(pg_catalog.->>) 'payer_id', v_cov OPERATOR(pg_catalog.->>) 'member_id',
        v_cov OPERATOR(pg_catalog.->>) 'relationship',
        v_cov OPERATOR(pg_catalog.->>) 'patient_name',
        v_cov OPERATOR(pg_catalog.->>) 'patient_dob',
        v_cov OPERATOR(pg_catalog.->>) 'subscriber_name',
        v_cov OPERATOR(pg_catalog.->>) 'subscriber_dob',
        v_cov OPERATOR(pg_catalog.->>) 'subscriber_member_id', v_cov, v_now);
    RETURN {schema}.m05_el_intake(pg_catalog.jsonb_build_object('op', 'plan',
        'appointment_ref', v_ref::text));
END;
"""

# --- t103 directory v4 (runtime) --------------------------------------------------------

_V3_KEY_CHECK: Final = (
    "        IF v_keys IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'confirmation',")
_V4_KEY_CHECK: Final = (
    "        IF pg_catalog.array_remove(v_keys, 'coverage')\n"
    "              IS DISTINCT FROM ARRAY['appointment_ref', 'attribution', 'confirmation',")
_V3_STATUS: Final = "        v_status := p_command OPERATOR(pg_catalog.->>) 'status';\n"
_V4_COVERAGE_CHECK: Final = _V3_STATUS + r"""
        -- E1 (M3): an optional, strictly shaped coverage block. Omission is the legacy shape;
        -- an explicit JSON null, or anything else malformed, is refused before any write.
        IF p_command OPERATOR(pg_catalog.?) 'coverage' THEN
            v_cov := p_command OPERATOR(pg_catalog.->) 'coverage';
            IF pg_catalog.jsonb_typeof(v_cov) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_cov OPERATOR(pg_catalog.->) 'state')
                  IS DISTINCT FROM 'string' THEN
                """ + _MALFORMED + r"""
            END IF;
            SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_ck
              FROM pg_catalog.jsonb_object_keys(v_cov) AS k;
            IF (v_cov OPERATOR(pg_catalog.->>) 'state') IN ('pending', 'none') THEN
                IF v_ck IS DISTINCT FROM ARRAY['state']::text[] THEN
                    """ + _MALFORMED + r"""
                END IF;
            ELSIF (v_cov OPERATOR(pg_catalog.->>) 'state') = 'present' THEN
                IF v_ck IS DISTINCT FROM ARRAY[""" + _in(_PRESENT_KEYS) + r"""]::text[]
                   OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_each(v_cov) AS e
                               WHERE e.key <> 'state'
                                 AND (pg_catalog.jsonb_typeof(e.value) NOT IN ('string', 'null')
                                  OR pg_catalog.char_length(e.value OPERATOR(pg_catalog.#>>)
                                     '{}') > 64))
                   OR (v_cov OPERATOR(pg_catalog.->>) 'patient_dob')
                      OPERATOR(pg_catalog.!~) '^\d{4}-\d{2}-\d{2}$'
                   OR (v_cov OPERATOR(pg_catalog.->>) 'subscriber_dob')
                      OPERATOR(pg_catalog.!~) '^\d{4}-\d{2}-\d{2}$' THEN
                    """ + _MALFORMED + r"""
                END IF;
            ELSE
                """ + _MALFORMED + r"""
            END IF;
        END IF;
"""
_V3_RETURN: Final = (
    "    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied');\n")
_V4_TAIL: Final = r"""
    -- E1: eligibility intake, in this transaction, after the directory writes.
    IF v_op = 'appointment_put' THEN
        PERFORM {schema}.m05_el_intake(pg_catalog.jsonb_build_object('op', 'put',
            'appointment_ref', v_ref::text, 'patient_id', v_patient::text,
            'start_at', pg_catalog.to_jsonb(v_start), 'status', v_status,
            'department_tz', p_command OPERATOR(pg_catalog.->) 'department_tz',
            'has_coverage', p_command OPERATOR(pg_catalog.?) 'coverage',
            'coverage', p_command OPERATOR(pg_catalog.->) 'coverage'));
    END IF;
""" + _V3_RETURN

_DIRECTORY_V4_BODY: Final = _replace_once(_replace_once(_replace_once(_replace_once(
    _DIRECTORY_V3_BODY,
    "    v_op text;\n", "    v_op text;\n    v_cov jsonb;\n    v_ck text[];\n"),
    _V3_KEY_CHECK, _V4_KEY_CHECK),
    _V3_STATUS, _V4_COVERAGE_CHECK),
    _V3_RETURN, _V4_TAIL)

# --- t104 tick (runtime, timer workload) ------------------------------------------------

_TICK_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_now timestamptz;
    v_k record;
    v_zone text;
    v_clinic text;
    v_raised integer := 0;
    v_replanned integer := 0;
    v_deferred integer;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'schema_version']::text[] THEN
        """ + _MALFORMED + r"""
    END IF;
""" + _ATTRIBUTION + _workload("timer") + r"""
    -- One clock read per tick; every deadline decision goes through the t101 seam.
    v_now := pg_catalog.clock_timestamp();
    SELECT r.clinic_tz INTO v_clinic FROM {schema}.m05_reminder_settings AS r;
    -- Codex D-1 recovery: re-plan no-zone deferrals once a zone is usable (exactly once:
    -- planning clears deferred_reason).
    FOR v_k IN
        SELECT a.appointment_ref, a.department_tz FROM {schema}.m05_el_appointments AS a
         WHERE a.deferred_reason = 'no_zone' AND a.status = 'booked'
         ORDER BY a.appointment_ref FOR UPDATE
    LOOP
        v_zone := """ + _zone_choice("v_k.department_tz", "v_clinic") + r""";
        IF v_zone IS NOT NULL THEN
            UPDATE {schema}.m05_el_appointments
               SET zone = v_zone, deferred_reason = NULL, updated_at = v_now
             WHERE appointment_ref = v_k.appointment_ref;
            PERFORM {schema}.m05_el_intake(pg_catalog.jsonb_build_object('op', 'plan',
                'appointment_ref', v_k.appointment_ref::text));
            v_replanned := v_replanned OPERATOR(pg_catalog.+) 1;
        END IF;
    END LOOP;
    -- OD-EL9: pending coverage past its deadline -> one manual case event per version.
    FOR v_k IN
        SELECT a.appointment_ref, a.coverage_version, a.next_seq, a.zone, a.start_at
          FROM {schema}.m05_el_appointments AS a
         WHERE a.coverage_state = 'pending' AND a.status = 'booked'
           AND a.not_received_due_at IS NOT NULL
           AND a.not_received_for IS DISTINCT FROM a.coverage_version
           AND {schema}.m05_el_deadline_passed(a.not_received_due_at, v_now)
         ORDER BY a.appointment_ref FOR UPDATE
    LOOP
        INSERT INTO {schema}.m05_el_counts (period, bucket, payer_key, appointment_ref,
            counted_at)
        VALUES (pg_catalog.to_char(pg_catalog.timezone(v_k.zone, v_k.start_at), 'YYYY-MM'),
            'not_received', '-', v_k.appointment_ref, v_now)
        ON CONFLICT DO NOTHING;
        INSERT INTO {schema}.m05_el_case_events (event_id, appointment_ref, seq,
            coverage_version, kind, class, reason, created_at)
        VALUES (pg_catalog.gen_random_uuid(), v_k.appointment_ref, v_k.next_seq,
            v_k.coverage_version, 'open_or_note', 'manual_required', 'coverage_not_received',
            v_now);
        UPDATE {schema}.m05_el_appointments
           SET next_seq = next_seq OPERATOR(pg_catalog.+) 1,
               not_received_for = v_k.coverage_version, updated_at = v_now
         WHERE appointment_ref = v_k.appointment_ref;
        v_raised := v_raised OPERATOR(pg_catalog.+) 1;
    END LOOP;
    SELECT pg_catalog.count(*)::integer INTO v_deferred
      FROM {schema}.m05_el_appointments AS a
     WHERE a.deferred_reason = 'no_zone' AND a.status = 'booked';
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',
        'raised', v_raised, 'replanned', v_replanned, 'deferred_no_zone', v_deferred);
END;
"""
)


def _closure() -> UnitDefinition:
    return UnitDefinition(template="", kind=ACL_CLOSURE_KIND, execution_role=M05_OWNER_ROLE)


# (migration id, function, volatility, body, EXECUTE grantee or None for owner only)
_JSONB_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    (INTAKE_ID, "m05_el_intake", "volatile", _INTAKE_BODY, None),
    (DIRECTORY_V4_ID, "m05_directory_v4", "volatile", _DIRECTORY_V4_BODY, RUNTIME_ROLE),
    (TICK_ID, "m05_el_tick", "volatile", _TICK_BODY, RUNTIME_ROLE),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {
    SETTINGS_ID: T098_SQL,
    TABLES_ID: T099_SQL,
    LOCAL_INSTANT_ID: _typed("m05_el_local_instant", _LOCAL_PARAMS, "timestamptz", "stable",
                             _LOCAL_INSTANT_BODY),
    SEAM_ID: _typed("m05_el_deadline_passed", _SEAM_PARAMS, "bool", "immutable", _SEAM_BODY),
    **{mid: _definition(name, vol, body, grantee)
       for mid, name, vol, body, grantee in _JSONB_FUNCTIONS},
    DIRECTORY_V3_CLOSURE_ID: _closure(),
}

EL1_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
EL1_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    LOCAL_INSTANT_ID: _typed_profile("m05_el_local_instant", _LOCAL_PARAMS, "timestamptz",
                                     "stable"),
    SEAM_ID: _typed_profile("m05_el_deadline_passed", _SEAM_PARAMS, "bool", "immutable"),
    **{mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _JSONB_FUNCTIONS},
})
EL1_ACL_AMENDMENTS: Final[tuple[AclAmendment, ...]] = (
    AclAmendment(target_unit="t077_m05_directory_v3", activated_by=DIRECTORY_V3_CLOSURE_ID,
                 removes=((RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),)),
)
