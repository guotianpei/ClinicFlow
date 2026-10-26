"""L-6 durable maintenance state (part 1 of revision 005).

Revision ID: 005
Revises: 004
Create Date: 2026-10-01

Implementation plan v4 (`574dec8a...b7f3`) CP-1 with amendment r3 (`b0b82277...523e`),
architecture v6 r3 §3 and §5, test cases v4 with IP-15 / IP-16; the frozen CP-1 test
file (`54aa6df1...73ee`) is the contract this revision is written against.

Three control-plane tables, owned by `haloflow_owner`:

* ``shared.tenant_maintenance_operations`` -- one row per L-6 maintenance operation.
  Trigger T2 makes the identity columns immutable, allows only the state moves
  ``establishing -> entered -> activated -> released`` and ``establishing -> abandoned``,
  lets the migrator move only ``activated -> released`` (OD-D1) and refuses DELETE. A
  partial unique index allows one open operation per tenant.
* ``shared.tenant_maintenance_withheld`` -- the withheld grant tuples per layer. The
  provisioner writes L1 and the migrator L2/L2b (checked on ``current_user``); a layer
  is closed once its ``*_withheld`` event exists; the table is append-only.
* ``shared.tenant_maintenance_attempts`` -- the append-only evidence log. A BEFORE
  INSERT trigger checks, in this pinned order: the closed ``detail`` schema of the
  event (plan v4 §9.3, r3 A2, Part B item 4, the domains pinned at freeze), the
  operation association (r3 A3), then the N-terminal rules (§3 [r2]). Unique indexes
  are checked after all BEFORE checks.

``detail`` holds typed control data only (r3 Part B item 1): the validator limits it
to integers, OIDs, timestamps, UUIDs, enumerated codes and opaque references. It
cannot prove where a value came from.

Grants (architecture §3, plan v4 §8.2, CP-1 part): P = ``haloflow_provisioner``,
M = ``haloflow_migrator``. P and M receive no UPDATE or DELETE on the withheld and
attempts tables and no DELETE on operations, so PostgreSQL refuses those by privilege
before any trigger (IP-15). OD-C3: P gets column SELECT on five
``tenant_state_history`` columns.

Part 2 (CP-4: the neutralizer role, its two functions, grants and the superuser
preflight of plan v4 §8.3) extends this same revision before merge.

`downgrade()` raises, consistent with `001`-`004`.
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


TABLES_SQL = """
CREATE TABLE shared.tenant_maintenance_operations (
    maintenance_operation_id uuid PRIMARY KEY,
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    from_version integer NOT NULL,
    to_version integer NOT NULL,
    neutralization_generation integer NOT NULL DEFAULT 1,
    neutralizer_lock_ack timestamptz,
    state varchar(16) NOT NULL,
    pre_entry_event_id bigint NOT NULL REFERENCES shared.tenant_state_history(event_id),
    entry_event_id bigint REFERENCES shared.tenant_state_history(event_id),
    resume_event_id bigint REFERENCES shared.tenant_state_history(event_id),
    t003_baseline varchar(32) NOT NULL,
    current_attempt uuid,
    migrator_ack_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT tenant_maintenance_operations_versions
        CHECK (from_version > 0 AND to_version > from_version),
    CONSTRAINT tenant_maintenance_operations_generation
        CHECK (neutralization_generation BETWEEN 1 AND 999999999),
    CONSTRAINT tenant_maintenance_operations_state
        CHECK (state IN ('establishing', 'entered', 'activated', 'released', 'abandoned')),
    CONSTRAINT tenant_maintenance_operations_t003_baseline
        CHECK (t003_baseline = 'absent' OR t003_baseline ~ '^failed:[1-9][0-9]{0,8}$')
);

CREATE UNIQUE INDEX tenant_maintenance_operations_one_open_per_tenant
    ON shared.tenant_maintenance_operations (tenant_id)
    WHERE state NOT IN ('released', 'abandoned');

CREATE TABLE shared.tenant_maintenance_withheld (
    maintenance_operation_id uuid NOT NULL
        REFERENCES shared.tenant_maintenance_operations(maintenance_operation_id),
    layer varchar(4) NOT NULL,
    object_kind varchar(32) NOT NULL,
    schema_name varchar(64) NOT NULL,
    object_name varchar(64) NOT NULL,
    column_name varchar(64),
    grantee varchar(64) NOT NULL,
    privilege varchar(32) NOT NULL,
    grantable boolean NOT NULL,
    grantor varchar(64) NOT NULL,
    CONSTRAINT tenant_maintenance_withheld_layer CHECK (layer IN ('L1', 'L2', 'L2b')),
    CONSTRAINT tenant_maintenance_withheld_tuple UNIQUE NULLS NOT DISTINCT (
        maintenance_operation_id, layer, object_kind, schema_name, object_name,
        column_name, grantee, privilege, grantable, grantor
    )
);

CREATE TABLE shared.tenant_maintenance_attempts (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id uuid NOT NULL,
    maintenance_operation_id uuid
        REFERENCES shared.tenant_maintenance_operations(maintenance_operation_id),
    tenant_id varchar(64) NOT NULL REFERENCES shared.tenants(tenant_id),
    event varchar(64) NOT NULL,
    detail jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE INDEX tenant_maintenance_attempts_operation_idx
    ON shared.tenant_maintenance_attempts (maintenance_operation_id, event);

-- Architecture §3: once-only events, per operation.
CREATE UNIQUE INDEX tenant_maintenance_attempts_once_only
    ON shared.tenant_maintenance_attempts (maintenance_operation_id, event)
    WHERE event IN (
        'op_created', 'l2_withheld', 'l1_withheld', 'drained', 'exclusion_established',
        'lo_granted', 'apply_applied', 'l2b_withheld', 'verified', 'activated',
        'l1_restored', 'l2_l2b_restored', 'released', 'completed', 'abandoned'
    );

-- N events keyed by generation. The validator has already refused any `gen` that is
-- not a JSON integer 1..999,999,999, so the cast cannot fail on a stored row.
CREATE UNIQUE INDEX tenant_maintenance_attempts_gen
    ON shared.tenant_maintenance_attempts (
        maintenance_operation_id, event, ((detail->>'gen')::integer)
    )
    WHERE event IN (
        'neutralization_bootstrap_started', 'neutralization_started',
        'neutralization_stopped', 'neutralized'
    );

CREATE UNIQUE INDEX tenant_maintenance_attempts_gen_round
    ON shared.tenant_maintenance_attempts (
        maintenance_operation_id, event,
        ((detail->>'gen')::integer), ((detail->>'round')::integer)
    )
    WHERE event IN ('neutralization_round_started', 'neutralization_round_outcome');

CREATE UNIQUE INDEX tenant_maintenance_attempts_neutralized_once
    ON shared.tenant_maintenance_attempts (maintenance_operation_id)
    WHERE event = 'neutralized';

CREATE UNIQUE INDEX tenant_maintenance_attempts_ledger_attempt
    ON shared.tenant_maintenance_attempts (
        maintenance_operation_id, event, ((detail->>'ledger_attempt')::integer)
    )
    WHERE event IN ('apply_running', 'apply_failed');

-- r3 A5: `started` and `attempt_refused` at most once per attempt.
CREATE UNIQUE INDEX tenant_maintenance_attempts_per_attempt
    ON shared.tenant_maintenance_attempts (attempt_id, event)
    WHERE event IN ('started', 'attempt_refused');

COMMENT ON TABLE shared.tenant_maintenance_operations IS
    'Control-plane L-6 maintenance record. PHI prohibited.';
COMMENT ON TABLE shared.tenant_maintenance_withheld IS
    'Control-plane L-6 maintenance record. PHI prohibited.';
COMMENT ON TABLE shared.tenant_maintenance_attempts IS
    'Control-plane L-6 maintenance record. PHI prohibited.';
"""


# Trigger T2 (architecture §3; OD-D1). The migrator rule is checked before the general
# transition rule, so any state change by M other than activated -> released gets the
# migrator message.
OPERATIONS_TRIGGER_SQL = """
CREATE FUNCTION shared.tenant_maintenance_operations_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tenant_maintenance_operations: rows cannot be deleted';
    END IF;
    IF NEW.maintenance_operation_id IS DISTINCT FROM OLD.maintenance_operation_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.from_version IS DISTINCT FROM OLD.from_version
       OR NEW.to_version IS DISTINCT FROM OLD.to_version
       OR NEW.pre_entry_event_id IS DISTINCT FROM OLD.pre_entry_event_id
       OR NEW.t003_baseline IS DISTINCT FROM OLD.t003_baseline
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'tenant_maintenance_operations: identity columns are immutable';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF current_user = 'haloflow_migrator'
           AND NOT (OLD.state = 'activated' AND NEW.state = 'released') THEN
            RAISE EXCEPTION
                'tenant_maintenance_operations: migrator may only move activated to released';
        END IF;
        IF NOT ((OLD.state = 'establishing' AND NEW.state = 'entered')
                OR (OLD.state = 'entered' AND NEW.state = 'activated')
                OR (OLD.state = 'activated' AND NEW.state = 'released')
                OR (OLD.state = 'establishing' AND NEW.state = 'abandoned')) THEN
            RAISE EXCEPTION 'tenant_maintenance_operations: illegal state transition';
        END IF;
    END IF;
    RETURN NEW;
END
$l6$;

CREATE TRIGGER tenant_maintenance_operations_guard
BEFORE UPDATE OR DELETE ON shared.tenant_maintenance_operations
FOR EACH ROW EXECUTE FUNCTION shared.tenant_maintenance_operations_guard();
"""


# Withheld layers (architecture §3). L1 is the provisioner's, L2 and L2b the
# migrator's; a layer is closed once the operation's `*_withheld` event exists.
WITHHELD_TRIGGER_SQL = """
CREATE FUNCTION shared.tenant_maintenance_append_only()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
BEGIN
    RAISE EXCEPTION 'tenant maintenance evidence is append-only';
END
$l6$;

CREATE FUNCTION shared.tenant_maintenance_withheld_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
BEGIN
    IF NOT ((NEW.layer = 'L1' AND current_user = 'haloflow_provisioner')
            OR (NEW.layer IN ('L2', 'L2b') AND current_user = 'haloflow_migrator')) THEN
        RAISE EXCEPTION 'tenant_maintenance_withheld: layer not writable by this role';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM shared.tenant_maintenance_attempts AS a
         WHERE a.maintenance_operation_id = NEW.maintenance_operation_id
           AND a.event = lower(NEW.layer) || '_withheld'
    ) THEN
        RAISE EXCEPTION 'tenant_maintenance_withheld: layer already withheld';
    END IF;
    RETURN NEW;
END
$l6$;

CREATE TRIGGER tenant_maintenance_withheld_guard
BEFORE INSERT ON shared.tenant_maintenance_withheld
FOR EACH ROW EXECUTE FUNCTION shared.tenant_maintenance_withheld_guard();

CREATE TRIGGER tenant_maintenance_withheld_append_only
BEFORE UPDATE OR DELETE ON shared.tenant_maintenance_withheld
FOR EACH ROW EXECUTE FUNCTION shared.tenant_maintenance_append_only();
"""


# The closed `detail` schema (plan v4 §9.3 with r3 A2; domains pinned at freeze by the
# frozen CP-1 test file). Kinds:
#   counter  JSON integer 1..999,999,999       from   JSON integer 1..999,999,998
#   next     `from` + 1                         int0   JSON integer 0..2,147,483,647
#   pid      JSON integer 1..2,147,483,647      oid    JSON integer 1..4,294,967,295
#   ts       ISO 8601 with zone, a valid timestamptz
#   uuid     canonical lowercase; uuid_null also allows JSON null
#   enum:<set>, ref, hex64, checks, s0, results, snapshot, oid_array, counts
DETAIL_VALIDATOR_SQL = r"""
CREATE FUNCTION shared.tenant_maintenance_detail_schema(event text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
SELECT CASE event
    WHEN 'started' THEN '{}'::jsonb
    WHEN 'op_created' THEN '{}'::jsonb
    WHEN 'drained' THEN '{}'::jsonb
    WHEN 'exclusion_established' THEN '{}'::jsonb
    WHEN 'lo_granted' THEN '{}'::jsonb
    WHEN 'apply_applied' THEN '{}'::jsonb
    WHEN 'l1_restored' THEN '{}'::jsonb
    WHEN 'l2_l2b_restored' THEN '{}'::jsonb
    WHEN 'released' THEN '{}'::jsonb
    WHEN 'completed' THEN '{}'::jsonb
    WHEN 'abandoned' THEN '{}'::jsonb
    WHEN 'claimed' THEN '{"claim": "enum:claim", "prior_attempt": "uuid_null"}'::jsonb
    WHEN 'l1_withheld' THEN '{"tuple_count": "int0"}'::jsonb
    WHEN 'l2_withheld' THEN '{"tuple_count": "int0"}'::jsonb
    WHEN 'l2b_withheld' THEN '{"tuple_count": "int0"}'::jsonb
    WHEN 'neutralization_bootstrap_started'
        THEN '{"gen": "counter", "t_bootstrap_deadline": "ts"}'::jsonb
    WHEN 'neutralization_started' THEN '{"gen": "counter", "t_deadline": "ts",
        "database_oid": "oid", "s0": "s0", "membership_snapshot": "snapshot",
        "classification_counts": "counts"}'::jsonb
    WHEN 'neutralization_round_started' THEN '{"gen": "counter", "round": "counter"}'::jsonb
    WHEN 'neutralization_round_outcome'
        THEN '{"gen": "counter", "round": "counter", "results": "results"}'::jsonb
    WHEN 'neutralized' THEN '{"gen": "counter"}'::jsonb
    WHEN 'neutralization_restarted' THEN '{"from": "from", "to": "next", "ref": "ref"}'::jsonb
    WHEN 'neutralization_stopped' THEN '{"gen": "counter", "phase": "enum:stop_phase",
        "reason": "enum:stop_reason"}'::jsonb
    WHEN 'apply_running' THEN '{"ledger_attempt": "counter"}'::jsonb
    WHEN 'apply_outcome_unrecorded' THEN '{"ledger_attempt": "counter"}'::jsonb
    WHEN 'apply_failed' THEN '{"ledger_attempt": "counter",
        "sanitized_error_code": "enum:sanitized_error_code"}'::jsonb
    WHEN 'verified' THEN '{"checks": "checks", "t003_checksum": "hex64"}'::jsonb
    WHEN 'activated' THEN '{"verified_event_id": "uuid"}'::jsonb
    WHEN 'interference_detected' THEN '{"point": "enum:point", "kind": "enum:kind"}'::jsonb
    WHEN 'reconciled_by_owner' THEN '{"ref": "ref"}'::jsonb
    WHEN 'attempt_refused'
        THEN '{"code": "enum:refusal_code", "phase": "enum:refusal_phase"}'::jsonb
END
$l6$;

CREATE FUNCTION shared.tenant_maintenance_detail_enum(name text)
RETURNS text[]
LANGUAGE sql
IMMUTABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
SELECT CASE name
    WHEN 'claim' THEN ARRAY['CL', 'CLc']
    WHEN 'stop_phase' THEN ARRAY['bootstrap', 'rounds']
    WHEN 'stop_reason' THEN ARRAY[
        'bootstrap_deadline_passed', 'contradictory_classification',
        'deployment_precondition_failed', 'membership_snapshot_too_large',
        'deadline_passed', 'budget_exhausted']
    WHEN 'result' THEN ARRAY[
        'terminated', 'timeout', 'gone', 'identity_changed', 'budget_exhausted', 'lock_lost']
    WHEN 'sanitized_error_code' THEN ARRAY[
        'SCHEMA_CREATE_FAILED', 'MIGRATION_DDL_FAILED', 'MIGRATION_COMMIT_FAILED',
        'MIGRATION_CHECKSUM_DRIFT', 'LEDGER_WRITE_FAILED', 'LOCK_UNAVAILABLE',
        'GRANT_APPLY_FAILED', 'SCHEMA_ACL_MISMATCH', 'VERIFICATION_FAILED',
        'REGISTRY_WRITE_FAILED']
    WHEN 'point' THEN ARRAY['a', 'b', 'c', 'd']
    WHEN 'kind' THEN ARRAY['registry', 'acl', 'ledger', 'other']
    WHEN 'refusal_code' THEN ARRAY[
        'LOCK_UNAVAILABLE', 'MAINTENANCE_FENCE_LOST', 'MAINTENANCE_LOCK_LOST',
        'MAINTENANCE_TOKEN_INVALID', 'MAINTENANCE_CASE_REFUSED', 'MAINTENANCE_STATE_UNKNOWN',
        'MAINTENANCE_CLAIM_REFUSED', 'MAINTENANCE_N_REFUSED',
        'MAINTENANCE_ESTABLISHMENT_INCOMPLETE', 'MAINTENANCE_DRAIN_TIMEOUT',
        'MAINTENANCE_VERIFICATION_FAILED']
    WHEN 'refusal_phase' THEN ARRAY[
        'lock_acquire', 'capability', 'fence', 'classify', 'claim', 'establish',
        'neutralize', 'drain', 'exclusion', 'apply', 'verify', 'activate', 'restore',
        'finalize']
END
$l6$;

-- A closed object: exactly the schema's keys, each present and valid.
CREATE FUNCTION shared.tenant_maintenance_detail_object_ok(
    schema jsonb, obj jsonb, from_value jsonb
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
DECLARE
    key text;
    kind text;
BEGIN
    IF obj IS NULL OR jsonb_typeof(obj) <> 'object' THEN
        RETURN false;
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_object_keys(obj) AS k WHERE NOT schema ? k) THEN
        RETURN false;
    END IF;
    FOR key, kind IN SELECT s.key, s.value FROM jsonb_each_text(schema) AS s LOOP
        IF NOT obj ? key
           OR NOT shared.tenant_maintenance_detail_kind_ok(kind, obj -> key, from_value) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END
$l6$;

CREATE FUNCTION shared.tenant_maintenance_detail_kind_ok(
    kind text, v jsonb, from_value jsonb
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
DECLARE
    t text := jsonb_typeof(v);
    s text;
    n numeric;
    element jsonb;
    previous numeric;
    total integer := 0;
BEGIN
    IF v IS NULL THEN
        RETURN false;
    END IF;
    IF t = 'null' THEN
        RETURN kind = 'uuid_null';
    END IF;

    IF kind IN ('counter', 'from', 'next', 'int0', 'pid', 'oid') THEN
        IF t <> 'number' OR v::text !~ '^-?[0-9]+$' THEN
            RETURN false;
        END IF;
        n := v::text::numeric;
        IF kind = 'counter' THEN
            RETURN n BETWEEN 1 AND 999999999;
        ELSIF kind = 'from' THEN
            RETURN n BETWEEN 1 AND 999999998;
        ELSIF kind = 'next' THEN
            RETURN from_value IS NOT NULL
               AND jsonb_typeof(from_value) = 'number'
               AND from_value::text ~ '^-?[0-9]+$'
               AND n = from_value::text::numeric + 1;
        ELSIF kind = 'int0' THEN
            RETURN n BETWEEN 0 AND 2147483647;
        ELSIF kind = 'pid' THEN
            RETURN n BETWEEN 1 AND 2147483647;
        ELSE
            RETURN n BETWEEN 1 AND 4294967295;
        END IF;
    END IF;

    IF kind IN ('ts', 'uuid', 'uuid_null', 'ref', 'hex64') OR kind LIKE 'enum:%' THEN
        IF t <> 'string' THEN
            RETURN false;
        END IF;
        s := v #>> '{}';
        IF kind = 'ts' THEN
            IF s !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$' THEN
                RETURN false;
            END IF;
            BEGIN
                PERFORM s::timestamptz;
            EXCEPTION WHEN others THEN
                RETURN false;
            END;
            RETURN true;
        ELSIF kind IN ('uuid', 'uuid_null') THEN
            RETURN s ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
        ELSIF kind = 'ref' THEN
            RETURN s ~ '^[A-Za-z0-9._:-]{1,64}$';
        ELSIF kind = 'hex64' THEN
            RETURN s ~ '^[0-9a-f]{64}$';
        ELSE
            RETURN s = ANY (shared.tenant_maintenance_detail_enum(substr(kind, 6)));
        END IF;
    END IF;

    IF kind = 'checks' THEN
        -- Exactly the four V check codes, each once, in any order.
        IF t <> 'array' OR jsonb_array_length(v) <> 4 THEN
            RETURN false;
        END IF;
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(v) AS e WHERE jsonb_typeof(e) <> 'string')
        THEN
            RETURN false;
        END IF;
        RETURN (SELECT array_agg(e ORDER BY e COLLATE "C") FROM jsonb_array_elements_text(v) AS e)
             = ARRAY['acl_phase5', 'ledger_checksums', 'step5_subset', 't003_profile'];
    END IF;

    IF kind IN ('s0', 'results') THEN
        IF t <> 'array' THEN
            RETURN false;
        END IF;
        -- PL/pgSQL ends an IF condition at the first THEN, so the bound is computed first.
        IF kind = 's0' THEN
            total := current_setting('max_connections')::integer;
        ELSE
            total := 8;
        END IF;
        IF jsonb_array_length(v) > total THEN
            RETURN false;
        END IF;
        FOR element IN SELECT e FROM jsonb_array_elements(v) AS e LOOP
            IF NOT shared.tenant_maintenance_detail_object_ok(
                CASE kind
                    WHEN 's0' THEN
                        '{"pid": "pid", "datid": "oid", "usesysid": "oid",
                          "backend_start": "ts"}'::jsonb
                    ELSE
                        '{"pid": "pid", "backend_start": "ts",
                          "result": "enum:result"}'::jsonb
                END,
                element, NULL) THEN
                RETURN false;
            END IF;
        END LOOP;
        RETURN true;
    END IF;

    IF kind = 'snapshot' THEN
        -- Closed {role_oid, member_oids}; role_oid strictly ascending; at most 4,096
        -- member OIDs in total (L_snapshot, r3 Part B item 3).
        IF t <> 'array' THEN
            RETURN false;
        END IF;
        FOR element IN SELECT e FROM jsonb_array_elements(v) AS e LOOP
            IF NOT shared.tenant_maintenance_detail_object_ok(
                '{"role_oid": "oid", "member_oids": "oid_array"}'::jsonb, element, NULL) THEN
                RETURN false;
            END IF;
            n := (element ->> 'role_oid')::numeric;
            IF previous IS NOT NULL AND n <= previous THEN
                RETURN false;
            END IF;
            previous := n;
            total := total + jsonb_array_length(element -> 'member_oids');
        END LOOP;
        RETURN total <= 4096;
    END IF;

    IF kind = 'oid_array' THEN
        -- OIDs, strictly ascending.
        IF t <> 'array' THEN
            RETURN false;
        END IF;
        FOR element IN SELECT e FROM jsonb_array_elements(v) AS e LOOP
            IF NOT shared.tenant_maintenance_detail_kind_ok('oid', element, NULL) THEN
                RETURN false;
            END IF;
            n := element::text::numeric;
            IF previous IS NOT NULL AND n <= previous THEN
                RETURN false;
            END IF;
            previous := n;
        END LOOP;
        RETURN true;
    END IF;

    IF kind = 'counts' THEN
        RETURN shared.tenant_maintenance_detail_object_ok(
            '{"target": "int0", "protected": "int0", "other": "int0",
              "out_of_scope": "int0", "unknown": "int0"}'::jsonb, v, NULL);
    END IF;

    RETURN false;
END
$l6$;

CREATE FUNCTION shared.tenant_maintenance_detail_ok(event text, detail jsonb)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
DECLARE
    schema jsonb := shared.tenant_maintenance_detail_schema(event);
BEGIN
    IF schema IS NULL OR detail IS NULL OR jsonb_typeof(detail) <> 'object' THEN
        RETURN false;
    END IF;
    RETURN shared.tenant_maintenance_detail_object_ok(schema, detail, detail -> 'from');
END
$l6$;
"""


# BEFORE INSERT on the attempts table. Pinned order (frozen CP-1 test file, IP-16):
# detail validation, then operation association (r3 A3), then the N-terminal rules
# (architecture §3 [r2]). The N checks serialize on the operation row (FOR UPDATE).
ATTEMPTS_TRIGGER_SQL = """
CREATE FUNCTION shared.tenant_maintenance_attempts_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $l6$
DECLARE
    operation_tenant varchar(64);
    generation text;
BEGIN
    IF NOT shared.tenant_maintenance_detail_ok(NEW.event, NEW.detail) THEN
        RAISE EXCEPTION 'tenant_maintenance_attempts: invalid detail';
    END IF;

    IF NEW.maintenance_operation_id IS NULL THEN
        IF NEW.event NOT IN ('started', 'interference_detected', 'attempt_refused') THEN
            RAISE EXCEPTION 'tenant_maintenance_attempts: invalid operation association';
        END IF;
    ELSE
        SELECT o.tenant_id INTO operation_tenant
          FROM shared.tenant_maintenance_operations AS o
         WHERE o.maintenance_operation_id = NEW.maintenance_operation_id;
        IF NOT FOUND OR operation_tenant IS DISTINCT FROM NEW.tenant_id THEN
            RAISE EXCEPTION 'tenant_maintenance_attempts: invalid operation association';
        END IF;
    END IF;

    IF NEW.event IN (
        'neutralization_bootstrap_started', 'neutralization_started',
        'neutralization_round_started', 'neutralization_round_outcome', 'neutralized',
        'neutralization_stopped'
    ) THEN
        PERFORM 1
           FROM shared.tenant_maintenance_operations AS o
          WHERE o.maintenance_operation_id = NEW.maintenance_operation_id
            FOR UPDATE;
        generation := NEW.detail ->> 'gen';
        IF NEW.event <> 'neutralization_stopped' AND EXISTS (
            SELECT 1
              FROM shared.tenant_maintenance_attempts AS a
             WHERE a.maintenance_operation_id = NEW.maintenance_operation_id
               AND a.event = 'neutralization_stopped'
               AND a.detail ->> 'gen' = generation
        ) THEN
            RAISE EXCEPTION 'tenant_maintenance_attempts: neutralization generation is stopped';
        END IF;
        IF NEW.event = 'neutralization_stopped' THEN
            IF EXISTS (
                SELECT 1
                  FROM shared.tenant_maintenance_attempts AS a
                 WHERE a.maintenance_operation_id = NEW.maintenance_operation_id
                   AND a.event = 'neutralized'
            ) THEN
                RAISE EXCEPTION 'tenant_maintenance_attempts: stop not allowed after neutralized';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM shared.tenant_maintenance_attempts AS a
                 WHERE a.maintenance_operation_id = NEW.maintenance_operation_id
                   AND a.event = 'neutralization_bootstrap_started'
                   AND a.detail ->> 'gen' = generation
            ) THEN
                RAISE EXCEPTION 'tenant_maintenance_attempts: stop requires bootstrap_started';
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END
$l6$;

CREATE TRIGGER tenant_maintenance_attempts_guard
BEFORE INSERT ON shared.tenant_maintenance_attempts
FOR EACH ROW EXECUTE FUNCTION shared.tenant_maintenance_attempts_guard();

CREATE TRIGGER tenant_maintenance_attempts_append_only
BEFORE UPDATE OR DELETE ON shared.tenant_maintenance_attempts
FOR EACH ROW EXECUTE FUNCTION shared.tenant_maintenance_append_only();
"""


OWNERSHIP_AND_GRANTS_SQL = """
ALTER FUNCTION shared.tenant_maintenance_operations_guard() OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_append_only() OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_withheld_guard() OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_detail_schema(text) OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_detail_enum(text) OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_detail_object_ok(jsonb, jsonb, jsonb)
    OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_detail_kind_ok(text, jsonb, jsonb)
    OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_detail_ok(text, jsonb) OWNER TO haloflow_owner;
ALTER FUNCTION shared.tenant_maintenance_attempts_guard() OWNER TO haloflow_owner;
ALTER TABLE shared.tenant_maintenance_operations OWNER TO haloflow_owner;
ALTER TABLE shared.tenant_maintenance_withheld OWNER TO haloflow_owner;
ALTER TABLE shared.tenant_maintenance_attempts OWNER TO haloflow_owner;

GRANT SELECT, INSERT ON shared.tenant_maintenance_operations TO haloflow_provisioner;
GRANT UPDATE (state, current_attempt, entry_event_id, resume_event_id, t003_baseline,
              updated_at, neutralization_generation)
    ON shared.tenant_maintenance_operations TO haloflow_provisioner;
GRANT SELECT, INSERT ON shared.tenant_maintenance_withheld TO haloflow_provisioner;
GRANT SELECT, INSERT ON shared.tenant_maintenance_attempts TO haloflow_provisioner;
GRANT SELECT (tenant_id, event_id, new_state, reason_code, execution_id)
    ON shared.tenant_state_history TO haloflow_provisioner;

GRANT SELECT ON shared.tenant_maintenance_operations TO haloflow_migrator;
GRANT UPDATE (state) ON shared.tenant_maintenance_operations TO haloflow_migrator;
GRANT SELECT, INSERT ON shared.tenant_maintenance_withheld TO haloflow_migrator;
GRANT SELECT, INSERT ON shared.tenant_maintenance_attempts TO haloflow_migrator;
"""


def upgrade() -> None:
    op.execute(TABLES_SQL)
    op.execute(OPERATIONS_TRIGGER_SQL)
    op.execute(WITHHELD_TRIGGER_SQL)
    op.execute(DETAIL_VALIDATOR_SQL)
    op.execute(ATTEMPTS_TRIGGER_SQL)
    op.execute(OWNERSHIP_AND_GRANTS_SQL)


def downgrade() -> None:
    raise RuntimeError("Revision 005 is not reversible; restore from backup instead.")
