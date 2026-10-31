"""UC1 part A (M02 minimal observation append): the migrator-owned tenant units.

Test-interface package v1 with delta v1r1 Δ4, decisions TB-1..TB-3, CN-1..CN-6
and G1 (Claude/Codex aligned 2026-10-30). This module holds the ORDINARY,
migrator-owned units only:

- ``t004_m02_reference``           reference tables, the contract registry, the
                                   typed link-resolver registry, and their seed;
- ``t005_m02_events``              ``patient_events`` and ``patient_event_links``;
- ``t006_m02_outbox``              ``event_handoff_outbox``;
- ``t007_m02_envelope_allowlist``  ``m02_envelope_path_allowlist``.

The SECURITY DEFINER gateways are typed units with their own execution roles and
live elsewhere (one M01 typed unit carries exactly one execution role).

Every unit follows the ``t002`` pattern: one transaction; marked steps as SQL
comments; the inherited runtime default privileges are revoked IN FULL on every
table the unit creates (part A gives the runtime no direct table access at all,
PA-28); a read-only guard over the grantee roles runs before the first grant and
raises a fixed message; then exactly the reviewed column grants.

SEED SCOPE (stated limit). ``t004`` seeds the six levels and fifteen statuses of
OI-007 §§1-2 verbatim, one action family, the six synthetic resolvers of G1 (all
INACTIVE), and exactly ONE contract: ``m02_test_observation_received`` v1 from
synthetic descriptors v3, INACTIVE. The other twelve ``m02_test_`` contracts are
not seeded in part A: their routes (intent, submission, outcome, correction,
resolution) are excluded from part A and their value-constraint vocabularies do
not fit the closed part-A ``value_constraints`` shape. They arrive with their own
checkpoints. ``ref_event_levels.ordinal`` is NOT NULL by the frozen shape while
OI-007 §1 gives no ordinal for ``observation`` and ``correction``; both are
seeded ``0`` and mean "not folded into operation precedence".
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m02.roles import APPEND_OWNER_ROLE, PROJECTION_OWNER_ROLE, READ_OWNER_ROLE

T004_MIGRATION_ID: Final = "t004_m02_reference"
T005_MIGRATION_ID: Final = "t005_m02_events"
T006_MIGRATION_ID: Final = "t006_m02_outbox"
T007_MIGRATION_ID: Final = "t007_m02_envelope_allowlist"

_S: Final = "{schema}"

_MSG_ROLES: Final = "m02 part A role verification failed"


def _role_guard(unit: str, roles: tuple[str, ...]) -> str:
    """A read-only DO block: every grantee role exists and is an unsafe-free
    NOLOGIN role. Nothing is altered; a failure raises one fixed message."""

    names = ", ".join(f"'{role}'" for role in roles)
    return f"""
-- {unit}:step:guard
DO $guard$
BEGIN
    IF (SELECT count(*)
          FROM pg_catalog.pg_roles AS r
         WHERE r.rolname = ANY (ARRAY[{names}]::pg_catalog.name[])
           AND NOT r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls) <> {len(roles)} THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_ROLES}';
    END IF;
END
$guard$;
"""


# ---------------------------------------------------------------------------
# t004_m02_reference
# ---------------------------------------------------------------------------

_OBSERVATION_METADATA_SCHEMA: Final = (
    '{"type":"object","additionalProperties":false,'
    '"properties":{"label_code":{"enum":["reviewed","unreviewed"]}}}'
)

T004_SQL: Final = f"""
-- t004:step:tables
CREATE TABLE {_S}.ref_event_levels (
    code varchar(32) PRIMARY KEY,
    ordinal smallint NOT NULL CHECK (ordinal >= 0),
    is_active boolean NOT NULL
);
CREATE TABLE {_S}.ref_event_statuses (
    code varchar(64) PRIMARY KEY,
    status_scope varchar(16) NOT NULL
        CHECK (status_scope IN ('stored_event', 'projection_only', 'both')),
    is_active boolean NOT NULL
);
CREATE TABLE {_S}.ref_action_families (
    action_family varchar(64) PRIMARY KEY CHECK (action_family ~ '^[a-z][a-z0-9_]{{1,63}}$'),
    owner_module varchar(16) NOT NULL CHECK (owner_module ~ '^m[0-9]{{2}}$'),
    is_active boolean NOT NULL
);
CREATE TABLE {_S}.event_types (
    event_type varchar(128) PRIMARY KEY CHECK (event_type ~ '^[a-z][a-z0-9_]{{1,127}}$'),
    action_family varchar(64) NOT NULL REFERENCES {_S}.ref_action_families (action_family),
    is_active boolean NOT NULL
);
CREATE TABLE {_S}.event_contracts (
    event_type varchar(128) NOT NULL REFERENCES {_S}.event_types (event_type),
    version integer NOT NULL CHECK (version >= 1),
    action_family varchar(64) NOT NULL REFERENCES {_S}.ref_action_families (action_family),
    event_level varchar(32) NOT NULL REFERENCES {_S}.ref_event_levels (code),
    allowed_statuses varchar(64)[] NOT NULL CHECK (cardinality(allowed_statuses) >= 1),
    terminality boolean NOT NULL,
    compatible_outcome_class varchar(64),
    precedence_rank integer NOT NULL CHECK (precedence_rank >= 0),
    required_fields varchar(64)[] NOT NULL,
    required_links jsonb NOT NULL CHECK (jsonb_typeof(required_links) = 'array'),
    optional_links jsonb NOT NULL CHECK (jsonb_typeof(optional_links) = 'array'),
    metadata_json_schema jsonb NOT NULL CHECK (jsonb_typeof(metadata_json_schema) = 'object'),
    metadata_max_bytes integer NOT NULL CHECK (metadata_max_bytes BETWEEN 1 AND 8192),
    authorized_producers varchar(64)[] NOT NULL,
    required_provenance varchar(64)[] NOT NULL,
    correction_policy jsonb,
    projection_rule_id varchar(64),
    effective_from timestamptz NOT NULL,
    retired_at timestamptz,
    signal_rule jsonb CHECK (
        signal_rule IS NULL
        OR (jsonb_typeof(signal_rule) = 'object'
            AND signal_rule ?& ARRAY['handoff_kind', 'signal_type']
            AND (signal_rule - 'handoff_kind' - 'signal_type') = '{{}}'::jsonb)
    ),
    value_constraints jsonb NOT NULL CHECK (jsonb_typeof(value_constraints) = 'object'),
    PRIMARY KEY (event_type, version),
    CHECK (retired_at IS NULL OR retired_at > effective_from)
);
CREATE TABLE {_S}.m02_link_resolvers (
    resolver_id varchar(64) PRIMARY KEY,
    relationship varchar(32) NOT NULL,
    resolver_kind varchar(32) NOT NULL CHECK (resolver_kind IN ('synthetic_fixture')),
    is_active boolean NOT NULL
);

-- t004:step:seed
INSERT INTO {_S}.ref_event_levels (code, ordinal, is_active) VALUES
    ('intent', 100, true),
    ('submission', 200, true),
    ('delivery', 300, true),
    ('business_outcome', 300, true),
    ('observation', 0, true),
    ('correction', 0, true);
INSERT INTO {_S}.ref_event_statuses (code, status_scope, is_active) VALUES
    ('requested', 'stored_event', true),
    ('accepted', 'stored_event', true),
    ('rejected', 'stored_event', true),
    ('indeterminate', 'stored_event', true),
    ('delivered', 'stored_event', true),
    ('delivery_failed', 'stored_event', true),
    ('business_succeeded', 'stored_event', true),
    ('business_failed', 'stored_event', true),
    ('conflict', 'stored_event', true),
    ('completed', 'stored_event', true),
    ('correction_recorded', 'stored_event', true),
    ('resolution_recorded', 'stored_event', true),
    ('acceptance_conflict_requires_reconciliation', 'projection_only', true),
    ('delivery_conflict_requires_reconciliation', 'projection_only', true),
    ('projection_unsupported', 'projection_only', true);
INSERT INTO {_S}.ref_action_families (action_family, owner_module, is_active) VALUES
    ('m02_test', 'm02', false);
INSERT INTO {_S}.event_types (event_type, action_family, is_active) VALUES
    ('m02_test_observation_received', 'm02_test', false);
INSERT INTO {_S}.event_contracts (
    event_type, version, action_family, event_level, allowed_statuses, terminality,
    compatible_outcome_class, precedence_rank, required_fields, required_links,
    optional_links, metadata_json_schema, metadata_max_bytes, authorized_producers,
    required_provenance, correction_policy, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints
) VALUES (
    'm02_test_observation_received', 1, 'm02_test', 'observation',
    ARRAY['completed']::varchar(64)[], true, 'observation', 100,
    ARRAY['correlation_id']::varchar(64)[],
    '[["m02_test_source_v1","m02_test_subject_v1"]]'::jsonb,
    '[]'::jsonb,
    '{_OBSERVATION_METADATA_SCHEMA}'::jsonb,
    8192,
    ARRAY[]::varchar(64)[],
    ARRAY[]::varchar(64)[],
    NULL, NULL, '2026-10-30T00:00:00Z', NULL,
    '{{"handoff_kind":"event_signal","signal_type":"m02.event.appended"}}'::jsonb,
    '{{"operation_id":{{"must_be_null":true}},"synthetic_intent":{{"forbidden":true}}}}'::jsonb
);
INSERT INTO {_S}.m02_link_resolvers (resolver_id, relationship, resolver_kind, is_active) VALUES
    ('m02_test_subject_v1', 'subject', 'synthetic_fixture', false),
    ('m02_test_source_v1', 'source', 'synthetic_fixture', false),
    ('m02_test_provider_v1', 'provider', 'synthetic_fixture', false),
    ('m02_test_business_v1', 'business_resource', 'synthetic_fixture', false),
    ('m02_test_evidence_v1', 'evidence', 'synthetic_fixture', false),
    ('m02_test_case_v1', 'case', 'synthetic_fixture', false);

-- t004:step:runtime_privileges
REVOKE ALL ON {_S}.ref_event_levels, {_S}.ref_event_statuses, {_S}.ref_action_families,
    {_S}.event_types, {_S}.event_contracts, {_S}.m02_link_resolvers FROM {RUNTIME_ROLE};
{_role_guard("t004", (APPEND_OWNER_ROLE, READ_OWNER_ROLE))}
-- t004:step:grants
GRANT SELECT (event_type, version, action_family, event_level, allowed_statuses,
    required_fields, required_links, optional_links, metadata_json_schema,
    metadata_max_bytes, authorized_producers, projection_rule_id, effective_from,
    retired_at, signal_rule, value_constraints)
    ON {_S}.event_contracts TO {APPEND_OWNER_ROLE};
GRANT SELECT (event_type, is_active) ON {_S}.event_types TO {APPEND_OWNER_ROLE};
GRANT SELECT (code, ordinal, is_active) ON {_S}.ref_event_levels TO {APPEND_OWNER_ROLE};
GRANT SELECT (code, status_scope, is_active) ON {_S}.ref_event_statuses TO {APPEND_OWNER_ROLE};
GRANT SELECT (resolver_id, relationship, resolver_kind, is_active)
    ON {_S}.m02_link_resolvers TO {APPEND_OWNER_ROLE};
GRANT SELECT (event_type, is_active) ON {_S}.event_types TO {READ_OWNER_ROLE};
"""

# ---------------------------------------------------------------------------
# t005_m02_events
#
# patient_events: design v0.3 §4.3 columns and the §4.6 indexes/constraints; the
# evidence INSERT set E (physical batch v2 l.150) is the append owner's exact
# INSERT grant. Immutable: no UPDATE/DELETE grant to any business role, and a
# rejector trigger refuses both for every role including the migrator.
#
# The subject invariant (patient_id, or at least one link row) is checked at
# COMMIT by a deferred constraint trigger. A deferred trigger fires as the
# session's role at COMMIT (the runtime), which has no table access, so the
# check function is SECURITY DEFINER, owned by the migrator that owns both
# tables, read-only, pinned search_path, no PUBLIC EXECUTE (trigger firing does
# not consult EXECUTE). The status-scope check is the same shape: it reads
# ref_event_statuses, so it is a migrator-owned read-only definer as well.
# ---------------------------------------------------------------------------

_EVENT_INSERT_SET_E: Final = (
    "event_id, operation_id, patient_id, event_type, contract_version, event_level, "
    "status, channel, direction, correlation_id, causation_event_id, provider, "
    "provider_account, external_id, provider_event_id, source_namespace, "
    "source_event_id, producer_dedupe_key, content_fingerprint, producer_service, "
    "producer_version, actor_id, workload_id, auth_method, occurred_at, "
    "source_time_quality, received_at, corrected_event_id, correction_reason, "
    "correction_scope, failure_reason_code, resolution_path_code, metadata"
)

T005_SQL: Final = f"""
-- t005:step:tables
CREATE TABLE {_S}.patient_events (
    event_id uuid PRIMARY KEY,
    append_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    operation_id uuid REFERENCES {_S}.operation_registry (operation_id),
    patient_id uuid,
    event_type varchar(128) NOT NULL,
    contract_version integer NOT NULL,
    event_level varchar(32) NOT NULL REFERENCES {_S}.ref_event_levels (code),
    status varchar(64) NOT NULL REFERENCES {_S}.ref_event_statuses (code),
    channel varchar(32),
    direction varchar(32),
    correlation_id uuid NOT NULL,
    causation_event_id uuid REFERENCES {_S}.patient_events (event_id),
    provider varchar(64),
    provider_account varchar(64),
    external_id varchar(256),
    provider_event_id varchar(256),
    source_namespace varchar(64),
    source_event_id varchar(256),
    producer_dedupe_key varchar(256) CHECK (producer_dedupe_key ~ '^[a-z0-9:]+$'),
    content_fingerprint bytea NOT NULL CHECK (octet_length(content_fingerprint) = 32),
    producer_service varchar(64) NOT NULL,
    producer_version varchar(64),
    actor_id uuid,
    workload_id uuid,
    auth_method varchar(32) NOT NULL,
    occurred_at timestamptz,
    source_time_quality varchar(24) NOT NULL
        CHECK (source_time_quality IN ('trusted', 'estimated', 'absent', 'invalid')),
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    corrected_event_id uuid REFERENCES {_S}.patient_events (event_id),
    correction_reason varchar(64),
    correction_scope varchar(32) CHECK (correction_scope IN ('metadata_only', 'asserted_fact')),
    failure_reason_code varchar(64),
    resolution_path_code varchar(64),
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 8192),
    CONSTRAINT patient_events_contract_fk FOREIGN KEY (event_type, contract_version)
        REFERENCES {_S}.event_contracts (event_type, version),
    CONSTRAINT patient_events_one_principal CHECK (num_nonnulls(actor_id, workload_id) = 1),
    CONSTRAINT patient_events_not_self_caused CHECK (causation_event_id <> event_id),
    CONSTRAINT patient_events_not_self_corrected CHECK (corrected_event_id <> event_id),
    CONSTRAINT patient_events_source_identity CHECK (
        (source_event_id IS NULL) = (source_namespace IS NULL)
    )
);
CREATE INDEX patient_events_operation_idx ON {_S}.patient_events (operation_id, append_sequence);
CREATE INDEX patient_events_patient_idx ON {_S}.patient_events (patient_id, created_at, event_id);
CREATE INDEX patient_events_correlation_idx
    ON {_S}.patient_events (correlation_id, append_sequence);
CREATE INDEX idx_patient_events_stale_intent_scan
    ON {_S}.patient_events (created_at, operation_id, event_id)
    WHERE event_level = 'intent' AND operation_id IS NOT NULL;
CREATE UNIQUE INDEX patient_events_source_dedupe
    ON {_S}.patient_events (producer_service, source_namespace, source_event_id)
    WHERE source_event_id IS NOT NULL;
CREATE UNIQUE INDEX patient_events_producer_dedupe
    ON {_S}.patient_events (producer_service, producer_dedupe_key)
    WHERE producer_dedupe_key IS NOT NULL;

CREATE TABLE {_S}.patient_event_links (
    event_id uuid NOT NULL REFERENCES {_S}.patient_events (event_id),
    link_type varchar(64) NOT NULL REFERENCES {_S}.m02_link_resolvers (resolver_id),
    link_id uuid NOT NULL,
    relationship varchar(32) NOT NULL,
    PRIMARY KEY (event_id, link_type, link_id, relationship)
);
CREATE INDEX patient_event_links_target_idx
    ON {_S}.patient_event_links (link_type, link_id, event_id);

-- t005:step:runtime_privileges
REVOKE ALL ON {_S}.patient_events, {_S}.patient_event_links FROM {RUNTIME_ROLE};
REVOKE ALL ON SEQUENCE {_S}.patient_events_append_sequence_seq FROM {RUNTIME_ROLE};

-- t005:step:integrity
CREATE FUNCTION {_S}.m02_evidence_reject() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {_S}, pg_temp
    AS $body$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '0A000', MESSAGE = 'event evidence is immutable';
END;
$body$;
REVOKE ALL ON FUNCTION {_S}.m02_evidence_reject() FROM PUBLIC;
CREATE TRIGGER patient_events_immutable
    BEFORE UPDATE OR DELETE ON {_S}.patient_events
    FOR EACH ROW EXECUTE FUNCTION {_S}.m02_evidence_reject();
CREATE TRIGGER patient_event_links_immutable
    BEFORE UPDATE OR DELETE ON {_S}.patient_event_links
    FOR EACH ROW EXECUTE FUNCTION {_S}.m02_evidence_reject();
CREATE TRIGGER patient_events_no_truncate
    BEFORE TRUNCATE ON {_S}.patient_events
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.m02_evidence_reject();
CREATE TRIGGER patient_event_links_no_truncate
    BEFORE TRUNCATE ON {_S}.patient_event_links
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.m02_evidence_reject();

CREATE FUNCTION {_S}.m02_event_status_scope_check() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, {_S}, pg_temp
    AS $body$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM {_S}.ref_event_statuses AS s
         WHERE s.code = NEW.status
           AND s.status_scope IN ('stored_event', 'both')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'event status scope not storable';
    END IF;
    RETURN NEW;
END;
$body$;
REVOKE ALL ON FUNCTION {_S}.m02_event_status_scope_check() FROM PUBLIC;
CREATE TRIGGER patient_events_status_scope
    BEFORE INSERT ON {_S}.patient_events
    FOR EACH ROW EXECUTE FUNCTION {_S}.m02_event_status_scope_check();

CREATE FUNCTION {_S}.m02_event_subject_required() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, {_S}, pg_temp
    AS $body$
BEGIN
    IF NEW.patient_id IS NULL AND NOT EXISTS (
        SELECT 1 FROM {_S}.patient_event_links AS l WHERE l.event_id = NEW.event_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'event subject required';
    END IF;
    RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION {_S}.m02_event_subject_required() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER trg_patient_event_subject_required
    AFTER INSERT ON {_S}.patient_events
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION {_S}.m02_event_subject_required();
{_role_guard("t005", (APPEND_OWNER_ROLE, READ_OWNER_ROLE))}
-- t005:step:grants
GRANT INSERT ({_EVENT_INSERT_SET_E}) ON {_S}.patient_events TO {APPEND_OWNER_ROLE};
GRANT SELECT (event_id, producer_service, producer_dedupe_key, content_fingerprint)
    ON {_S}.patient_events TO {APPEND_OWNER_ROLE};
GRANT INSERT (event_id, link_type, link_id, relationship)
    ON {_S}.patient_event_links TO {APPEND_OWNER_ROLE};
GRANT SELECT (event_id, event_type, contract_version, event_level, status, operation_id,
    patient_id, actor_id, workload_id, causation_event_id, producer_dedupe_key, metadata)
    ON {_S}.patient_events TO {READ_OWNER_ROLE};
GRANT SELECT (event_id, link_type, link_id, relationship)
    ON {_S}.patient_event_links TO {READ_OWNER_ROLE};
"""

# ---------------------------------------------------------------------------
# t006_m02_outbox
#
# physical batch v2 §4 (projection-owner INSERT set, SELECT for collision
# comparison) and §8 (physical details, lease/delivery state CHECKs). No
# dispatcher role exists in part A, so no UPDATE grant is issued to anyone;
# pending rows are a disclosed production-enablement prerequisite. The append
# owner reaches the outbox only through the enqueue gateway (no privilege).
# ---------------------------------------------------------------------------

T006_SQL: Final = f"""
-- t006:step:table
CREATE TABLE {_S}.event_handoff_outbox (
    handoff_id uuid PRIMARY KEY,
    tenant_id text NOT NULL CHECK (tenant_id ~ '^[a-z0-9][a-z0-9-]{{1,62}}[a-z0-9]$'),
    operation_id uuid REFERENCES {_S}.operation_registry (operation_id),
    source_event_id uuid NOT NULL REFERENCES {_S}.patient_events (event_id),
    handoff_kind varchar(32) NOT NULL CHECK (handoff_kind IN (
        'ordinary_failure', 'acceptance_conflict', 'delivery_conflict',
        'reconciliation', 'event_signal', 'integrity_alert')),
    dedupe_key varchar(70) NOT NULL UNIQUE CHECK (dedupe_key ~ '^m02h1:[0-9a-f]{{64}}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    state varchar(16) NOT NULL CHECK (state IN ('pending', 'leased', 'delivered', 'dead_letter')),
    attempts integer NOT NULL CHECK (attempts >= 0),
    available_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    lease_owner varchar(128),
    lease_token uuid,
    lease_expires_at timestamptz,
    delivered_at timestamptz,
    CONSTRAINT event_handoff_outbox_lease CHECK (
        (state = 'leased') = (num_nonnulls(lease_owner, lease_token, lease_expires_at) = 3)
        AND (state = 'leased' OR num_nonnulls(lease_owner, lease_token, lease_expires_at) = 0)
    ),
    CONSTRAINT event_handoff_outbox_delivered CHECK (
        (state = 'delivered') = (delivered_at IS NOT NULL)
    )
);
CREATE INDEX event_handoff_outbox_ready_idx
    ON {_S}.event_handoff_outbox (available_at, handoff_id) WHERE state = 'pending';

-- t006:step:runtime_privileges
REVOKE ALL ON {_S}.event_handoff_outbox FROM {RUNTIME_ROLE};
{_role_guard("t006", (PROJECTION_OWNER_ROLE,))}
-- t006:step:grants
GRANT INSERT (handoff_id, tenant_id, operation_id, source_event_id, handoff_kind,
    dedupe_key, payload, state, attempts, available_at)
    ON {_S}.event_handoff_outbox TO {PROJECTION_OWNER_ROLE};
GRANT SELECT (dedupe_key, tenant_id, operation_id, source_event_id, handoff_kind, payload)
    ON {_S}.event_handoff_outbox TO {PROJECTION_OWNER_ROLE};
"""

# ---------------------------------------------------------------------------
# t007_m02_envelope_allowlist
#
# A-D1 (package v1 §1.3, delta Δ4). The finite path allowlist the envelope read
# consults. Migrator-owned; read owner SELECT on the four columns only; the
# runtime has nothing. Seeded with NO rows in part A: the UC1 M05 rows name the
# M05 resolution contract, which part B installs (the composite FK below would
# refuse them earlier), and intake fixture rows are test-owned.
# ---------------------------------------------------------------------------

T007_SQL: Final = f"""
-- t007:step:table
CREATE TABLE {_S}.m02_envelope_path_allowlist (
    consumer_module varchar(16) NOT NULL CHECK (consumer_module ~ '^m[0-9]{{2}}$'),
    event_type varchar(128) NOT NULL,
    contract_version integer NOT NULL,
    json_path varchar(128) NOT NULL CHECK (json_path ~ '^[a-z][a-z0-9_]{{0,127}}$'),
    PRIMARY KEY (consumer_module, event_type, contract_version, json_path),
    CONSTRAINT m02_envelope_path_allowlist_contract_fk FOREIGN KEY (event_type, contract_version)
        REFERENCES {_S}.event_contracts (event_type, version)
);

-- t007:step:runtime_privileges
REVOKE ALL ON {_S}.m02_envelope_path_allowlist FROM {RUNTIME_ROLE};
{_role_guard("t007", (READ_OWNER_ROLE,))}
-- t007:step:grants
GRANT SELECT (consumer_module, event_type, contract_version, json_path)
    ON {_S}.m02_envelope_path_allowlist TO {READ_OWNER_ROLE};
"""

# NOT yet composed into M02_TENANT_MIGRATIONS: see the composition note to Codex
# (target schema version vs. the frozen harness supported range).
PART_A_ORDINARY_UNITS: Final = MappingProxyType(
    {
        T004_MIGRATION_ID: T004_SQL,
        T005_MIGRATION_ID: T005_SQL,
        T006_MIGRATION_ID: T006_SQL,
        T007_MIGRATION_ID: T007_SQL,
    }
)
