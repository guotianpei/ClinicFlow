"""The M02 per-tenant migration unit set: `t002_m02_operation_registry` and,
from CP2-2b, the typed gateway unit `t003_m02_lock_operation` (at the end).

CP2-2a, architecture v4 §5 (C-3), read with its erratum 1 and the test-case
bindings in test cases v4 erratum 1 r3 (ET-2, I-2 to I-8).

One migrator-owned *ordinary* unit (no execution role), so the whole rendered
text runs in one transaction: a failure anywhere rolls back every object and
grant in the unit, and the runner records the ledger row as `failed` with
`MIGRATION_DDL_FAILED` (architecture erratum 1 E-A1).

The template runs in six marked steps (I-3). The markers are SQL comments, so
they do not change what PostgreSQL executes; tests split the template on them to
build one-defect mutants, and a missing or duplicated marker fails those tests
in setup rather than letting them pass.

1. ``table`` -- `operation_registry` (design v0.3 §4.2). `created_at` defaults to
   `statement_timestamp()`: rows from one multi-row INSERT share it (AQ-7). No
   claim is made about resend chain cycles (requirements erratum 1 E3 = C1; a
   release prerequisite carried to the `beginOperation` checkpoint).
2. ``runtime_privileges`` -- `t001`'s default privileges give the runtime SIUD on
   every table the migrator creates; this revokes INSERT, UPDATE and DELETE,
   leaving SELECT (R-A4, RQ-2).
3. ``rejector`` -- a `SECURITY INVOKER` trigger function with a pinned
   `search_path` and a fixed `0A000` body, and a `BEFORE UPDATE ... FOR EACH ROW`
   trigger (R-A5).
4. ``guard`` -- one `DO` block of read-only catalogue checks, in the order
   G-1, G-3, G-4, G-5, G-6 (§5.2). Every lookup is missing-safe and raises a
   fixed message; nothing is altered.
5. ``lock_owner_grants`` -- exactly the two column grants to the lock owner.
6. ``post_grant_check`` -- the lock owner holds exactly those grants (§5.3).

What the guard does NOT prove is stated in architecture v4 §5.4 and §8: G-6 is
an absence check at one point, not a record of order, so a grant followed by a
revoke before G-6 is not detected and CP2-D25-order stays pending; post-install
drift of these ACLs is not detected (L-2); and CP2-2a claims no runtime
compatibility for version-2 tenants (L-1).
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import MIGRATOR_ROLE, RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import LOCK_OWNER_ROLE

T002_MIGRATION_ID: Final = "t002_m02_operation_registry"
REJECTOR_FUNCTION_NAME: Final = "operation_registry_reject"
REJECTOR_TRIGGER_NAME: Final = "operation_registry_immutable"

# The literal placeholder the M01 unit renderer substitutes (units.py). It is
# spelled out here rather than imported: M01 keeps it private.
_S: Final = "{schema}"

# ET-2 I-6. sha256 of the UTF-8 bytes between the two `$body$` tags below. This
# is a reviewed literal, not computed at import: the guard compares the stored
# `prosrc` against it, and the tests hold their own copy, so a changed body fails
# two independent literals until a reviewer updates both (architecture §5.4).
_REJECTOR_BODY_SHA256: Final = "944e1d60c21f9efb574b73dce21073c8bb4cc2e8b08dabdd74be43c730c61200"

_MSG_LOCK_OWNER: Final = "operation_registry lock owner verification failed"
_MSG_REJECTOR: Final = "operation_registry rejector verification failed"
_MSG_PRIVILEGE: Final = "operation_registry privilege verification failed"
_MSG_GRANT_ORDER: Final = "operation_registry grant order verification failed"
_MSG_POST_GRANT: Final = "operation_registry lock owner grant verification failed"

# Step 5 is this one statement and nothing else (ET-2 I-5). Built here only to
# keep the source line within the line-length limit; it renders as one line.
_LOCK_OWNER_GRANTS: Final = (
    "GRANT SELECT (operation_id), UPDATE (correlation_id) "
    f"ON {_S}.operation_registry TO {LOCK_OWNER_ROLE};"
)

T002_SQL: Final = f"""
-- t002:step:table
CREATE TABLE {_S}.operation_registry (
    operation_id uuid PRIMARY KEY,
    owner_service varchar(64) NOT NULL,
    action_code varchar(96) NOT NULL,
    business_key_fingerprint bytea NOT NULL,
    business_key_version smallint NOT NULL,
    subject_type varchar(48) NOT NULL,
    subject_id uuid NOT NULL,
    resend_of_operation_id uuid,
    correlation_id uuid NOT NULL,
    producer_version varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT operation_registry_business_key_unique
        UNIQUE (owner_service, action_code, business_key_version, business_key_fingerprint),
    CONSTRAINT operation_registry_resend_fk
        FOREIGN KEY (resend_of_operation_id) REFERENCES {_S}.operation_registry (operation_id),
    CONSTRAINT operation_registry_resend_not_self
        CHECK (resend_of_operation_id <> operation_id)
);

-- t002:step:runtime_privileges
REVOKE INSERT, UPDATE, DELETE ON {_S}.operation_registry FROM {RUNTIME_ROLE};

-- t002:step:rejector
CREATE FUNCTION {_S}.{REJECTOR_FUNCTION_NAME}() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY INVOKER
    SET search_path = pg_catalog, {_S}, pg_temp
    AS $body$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '0A000',
        MESSAGE = 'operation_registry rows are immutable';
END;
$body$;
REVOKE ALL ON FUNCTION {_S}.{REJECTOR_FUNCTION_NAME}() FROM PUBLIC;
CREATE TRIGGER {REJECTOR_TRIGGER_NAME}
    BEFORE UPDATE ON {_S}.operation_registry
    FOR EACH ROW
    EXECUTE FUNCTION {_S}.{REJECTOR_FUNCTION_NAME}();

-- t002:step:guard
DO $guard$
DECLARE
    v_lock_owner pg_catalog.oid;
    v_migrator pg_catalog.oid;
    v_runtime pg_catalog.oid;
    v_table pg_catalog.oid;
    v_function pg_catalog.oid;
    v_privilege pg_catalog.text;
    v_attnum pg_catalog.int2;
BEGIN
    -- t002:guard:G-1
    -- The lock owner exists and every unsafe attribute is false. Read only.
    v_lock_owner := pg_catalog.to_regrole('{LOCK_OWNER_ROLE}');
    IF v_lock_owner IS NULL OR NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_roles AS r
         WHERE r.oid = v_lock_owner
           AND NOT r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_LOCK_OWNER}';
    END IF;

    -- t002:guard:G-3
    -- The table, the rejector function by exact signature, its ACL, and the
    -- one non-internal trigger.
    v_migrator := pg_catalog.to_regrole('{MIGRATOR_ROLE}');
    v_table := pg_catalog.to_regclass('{_S}.operation_registry');
    v_function := pg_catalog.to_regprocedure('{_S}.{REJECTOR_FUNCTION_NAME}()');
    IF v_migrator IS NULL OR v_table IS NULL OR v_function IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_REJECTOR}';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class AS c
         WHERE c.oid = v_table
           AND c.relkind = 'r'
           AND c.relowner = v_migrator
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_REJECTOR}';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_proc AS p
          JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang
         WHERE p.oid = v_function
           AND p.prokind = 'f'
           AND p.prorettype = pg_catalog.to_regtype('pg_catalog.trigger')
           AND l.lanname = 'plpgsql'
           AND p.proowner = v_migrator
           AND NOT p.prosecdef
           AND p.proconfig = ARRAY['search_path=pg_catalog, {_S}, pg_temp']::pg_catalog.text[]
           AND pg_catalog.encode(
                   pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex'
               ) = '{_REJECTOR_BODY_SHA256}'
           AND p.proacl IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_REJECTOR}';
    END IF;
    IF (
        SELECT pg_catalog.count(*)
          FROM pg_catalog.pg_proc AS p,
               LATERAL pg_catalog.aclexplode(p.proacl) AS a
         WHERE p.oid = v_function
    ) <> 1 OR NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_proc AS p,
               LATERAL pg_catalog.aclexplode(p.proacl) AS a
         WHERE p.oid = v_function
           AND a.grantee = v_migrator
           AND a.grantor = v_migrator
           AND a.privilege_type = 'EXECUTE'
           AND NOT a.is_grantable
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_REJECTOR}';
    END IF;
    IF (
        SELECT pg_catalog.count(*)
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = v_table
           AND NOT t.tgisinternal
    ) <> 1 OR NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = v_table
           AND NOT t.tgisinternal
           AND t.tgfoid = v_function
           AND t.tgtype = 19
           AND t.tgqual IS NULL
           AND pg_catalog.cardinality(t.tgattr::pg_catalog.int2[]) = 0
           AND t.tgnargs = 0
           AND t.tgenabled = 'O'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_REJECTOR}';
    END IF;

    -- t002:guard:G-4
    -- The runtime holds table SELECT and nothing else: all eight PostgreSQL 17
    -- table privileges are covered, and no column INSERT, UPDATE or REFERENCES.
    v_runtime := pg_catalog.to_regrole('{RUNTIME_ROLE}');
    IF v_runtime IS NULL
       OR NOT pg_catalog.has_table_privilege(v_runtime, v_table, 'SELECT') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_PRIVILEGE}';
    END IF;
    FOREACH v_privilege IN ARRAY
        ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'MAINTAIN']
    LOOP
        IF pg_catalog.has_table_privilege(v_runtime, v_table, v_privilege) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_PRIVILEGE}';
        END IF;
    END LOOP;
    FOREACH v_privilege IN ARRAY ARRAY['INSERT', 'UPDATE', 'REFERENCES']
    LOOP
        IF pg_catalog.has_any_column_privilege(v_runtime, v_table, v_privilege) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_PRIVILEGE}';
        END IF;
    END LOOP;

    -- t002:guard:G-5
    -- No grant option for the runtime, on the table or on any column.
    IF pg_catalog.has_table_privilege(v_runtime, v_table, 'SELECT WITH GRANT OPTION') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_PRIVILEGE}';
    END IF;
    FOR v_attnum IN
        SELECT a.attnum
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = v_table
           AND a.attnum > 0
           AND NOT a.attisdropped
    LOOP
        IF pg_catalog.has_column_privilege(
               v_runtime, v_table, v_attnum, 'SELECT WITH GRANT OPTION'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_PRIVILEGE}';
        END IF;
    END LOOP;

    -- t002:guard:G-6
    -- Runs last, immediately before the grant: the lock owner holds nothing on
    -- the table yet. An absence check at this point only; see §5.4.
    FOREACH v_privilege IN ARRAY
        ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER',
              'MAINTAIN']
    LOOP
        IF pg_catalog.has_table_privilege(v_lock_owner, v_table, v_privilege) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_GRANT_ORDER}';
        END IF;
    END LOOP;
    FOREACH v_privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES']
    LOOP
        IF pg_catalog.has_any_column_privilege(v_lock_owner, v_table, v_privilege) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_GRANT_ORDER}';
        END IF;
    END LOOP;
END
$guard$;

-- t002:step:lock_owner_grants
{_LOCK_OWNER_GRANTS}

-- t002:step:post_grant_check
DO $post_grant$
DECLARE
    v_lock_owner pg_catalog.oid;
    v_table pg_catalog.oid;
    v_privilege pg_catalog.text;
    v_attname pg_catalog.name;
    v_attnum pg_catalog.int2;
BEGIN
    v_lock_owner := pg_catalog.to_regrole('{LOCK_OWNER_ROLE}');
    v_table := pg_catalog.to_regclass('{_S}.operation_registry');
    IF v_lock_owner IS NULL OR v_table IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_POST_GRANT}';
    END IF;
    -- No table-level privilege, all eight.
    FOREACH v_privilege IN ARRAY
        ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER',
              'MAINTAIN']
    LOOP
        IF pg_catalog.has_table_privilege(v_lock_owner, v_table, v_privilege) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_POST_GRANT}';
        END IF;
    END LOOP;
    -- Per column: SELECT on operation_id only, UPDATE on correlation_id only, no
    -- INSERT or REFERENCES anywhere, and no grant option on anything.
    FOR v_attnum, v_attname IN
        SELECT a.attnum, a.attname
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = v_table
           AND a.attnum > 0
           AND NOT a.attisdropped
    LOOP
        IF pg_catalog.has_column_privilege(v_lock_owner, v_table, v_attnum, 'SELECT')
               IS DISTINCT FROM (v_attname = 'operation_id')
           OR pg_catalog.has_column_privilege(v_lock_owner, v_table, v_attnum, 'UPDATE')
               IS DISTINCT FROM (v_attname = 'correlation_id')
           OR pg_catalog.has_column_privilege(v_lock_owner, v_table, v_attnum, 'INSERT')
           OR pg_catalog.has_column_privilege(v_lock_owner, v_table, v_attnum, 'REFERENCES')
           OR pg_catalog.has_column_privilege(
                  v_lock_owner, v_table, v_attnum, 'SELECT WITH GRANT OPTION'
              )
           OR pg_catalog.has_column_privilege(
                  v_lock_owner, v_table, v_attnum, 'UPDATE WITH GRANT OPTION'
              ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_POST_GRANT}';
        END IF;
    END LOOP;
END
$post_grant$;
"""


# ---------------------------------------------------------------------------
# CP2-2b: `t003_m02_lock_operation`, the typed lock gateway (architecture v3 §2).
#
# A `SECURITY DEFINER` function owned by the lock owner, installed through the
# typed path (the frozen CP1 checker binds these exact bytes) and verified after
# install against `gateway_profile.LOCK_OPERATION_PROFILE` (§5).
#
# Refusal precedence, fixed (R-B2): the NULL argument first (`22004`), then the
# tenant context (`22023`: unset, empty or malformed), and both BEFORE the only
# table access; then `P0002` when no row exists. The context pattern is the same
# literal as `resolver.TENANT_ID_PATTERN` and the `shared.tenants` CHECK (U41). A
# well-formed value is NOT compared with the schema: this is not authentication.
#
# What the syntax does, as distinct from the intended hardening: the function
# call and the regex operator are schema-qualified; the equality operators are
# not, and resolve through the pinned `search_path` (pg_catalog first, pg_temp
# last). The pinned `proconfig` IS the hardening; no stronger claim is made.
# ---------------------------------------------------------------------------

T003_MIGRATION_ID: Final = "t003_m02_lock_operation"
_GATEWAY: Final = "m02_lock_operation"
_GATEWAY_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"

# The exact text between the two `$body$` tags. The verification block declares
# it with the `{schema}` placeholder; the checker renders both sides.
_T003_BODY: Final = """
DECLARE
    v_tenant text;
    v_id uuid;
BEGIN
    IF p_operation_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22004', MESSAGE = 'operation id required';
    END IF;
    v_tenant := pg_catalog.current_setting('app.tenant_id', true);
    IF v_tenant IS NULL OR v_tenant = ''
       OR v_tenant OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    SELECT r.operation_id INTO v_id
      FROM {schema}.operation_registry AS r
     WHERE r.operation_id = p_operation_id
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'operation not found';
    END IF;
    RETURN v_id;
END;
"""

# sha256 of the NFC-normalized body above. A reviewed literal, like 2a's
# `_REJECTOR_BODY_SHA256`: the frozen checker recomputes it at pending install
# and composition's agreement check compares it with the verification body, so a
# changed body fails until a reviewer updates this value.
_T003_BODY_SHA256: Final = "7ce15aea8124bbecd62a91a6310e6c40c4108876958178ee925dea7d06795168"

T003_SQL: Final = (
    f"CREATE FUNCTION {_S}.{_GATEWAY}(p_operation_id uuid) RETURNS uuid\n"
    "    LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE CALLED ON NULL INPUT SECURITY DEFINER\n"
    f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
    f"    AS $body${_T003_BODY}$body$;\n"
    f"REVOKE ALL ON FUNCTION {_S}.{_GATEWAY}(uuid) FROM PUBLIC;\n"
    f"GRANT EXECUTE ON FUNCTION {_S}.{_GATEWAY}(uuid) TO {RUNTIME_ROLE};\n"
)

# Declared Form A: the owner's own entry is not declared (E6); the installed-state
# profile carries it. Plain dicts and lists, because the frozen shape check
# requires exactly those types; composition deep-copies and freezes them.
T003_DEFINITION: Final = UnitDefinition(
    template=T003_SQL,
    execution_role=LOCK_OWNER_ROLE,
    kind=TYPED_FUNCTION_KIND,
    policy={
        "policy_format": 1,
        "semantic_version": 1,
        "parser_package": "pglast",
        "parser_version": "7.17",
        "grammar_major": 17,
        "functions": [
            {
                "schema": _S,
                "name": _GATEWAY,
                "inputs": [{"name": "p_operation_id", "type": "uuid"}],
                "outputs": [],
                "return_type": "uuid",
                "language": "plpgsql",
                "is_procedure": False,
                "replace": False,
                "security_definer": True,
                "volatility": "volatile",
                "parallel": "unsafe",
                "strict": False,
                "config": [_GATEWAY_CONFIG],
                "body_sha256": _T003_BODY_SHA256,
                "acl": [{"grantee": RUNTIME_ROLE, "privileges": ["EXECUTE"]}],
                "comment": None,
            }
        ],
    },
    policy_verification={
        "kind": "function_metadata",
        "functions": [
            {
                "name": _GATEWAY,
                "argument_types": ["uuid"],
                "owner": LOCK_OWNER_ROLE,
                "security_definer": True,
                "config": [_GATEWAY_CONFIG],
                "acl": [{"grantee": RUNTIME_ROLE, "privileges": ["EXECUTE"]}],
                "body": _T003_BODY,
            }
        ],
    },
)


M02_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {T002_MIGRATION_ID: T002_SQL, T003_MIGRATION_ID: T003_DEFINITION}
)
