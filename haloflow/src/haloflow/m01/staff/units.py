"""MA-10 staff directory units t021..t024 (UC1 part B).

- ``t021_m01_staff_directory`` migrator-owned tables (package v1 section 3.2,
  v1r1 delta 1-3, v1r2 corrections 3-4): the staff membership row, follow-up
  access, admin action keys and the immutable admin log. The runtime role's
  default table privileges (``t001``) are revoked, and PUBLIC holds nothing. The
  staff owner holds exactly the grants its three gateways need; no other role has
  any directory table privilege.
- ``t022``..``t024`` typed gateways owned by ``haloflow_staff_owner``. RED TRANCHE:
  every body is a fail-closed stub (tenant context check, then P0001
  STAFF_NOT_IMPLEMENTED), as M05's B1 stubs were. Implementation follows only after
  the Codex red gate.

Trust model (v1r1 delta 3): direct table privilege exists only for the staff owner
(exact grants), the migrator (table owner, SET edge to the staff owner) and the
provisioner (schema owner). The rejectors refuse UPDATE/DELETE/TRUNCATE through
DML for every role; they do not resist a privileged owner changing the
installation (L-2 carried).
"""

from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE

STAFF_OWNER_ROLE: Final = "haloflow_staff_owner"

T021_MIGRATION_ID: Final = "t021_m01_staff_directory"

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_MSG_ROLES: Final = "staff directory role verification failed"

# v1r2 correction 4: the Unicode White_Space set, enumerated. A display name may
# not start or end with any of these. Explicit class, not btrim.
_WS: Final = (
    r"\u0009-\u000d \u0085   -     　"
)
# Anywhere in the name: C0/C1 controls and the two line/paragraph separators.
_FORBIDDEN: Final = r"\u0001-\u001f\u007f-\u009f  "

DISPLAY_NAME_CHECK: Final = (
    "(display_name IS NFC NORMALIZED"
    " AND pg_catalog.char_length(display_name) BETWEEN 1 AND 80"
    f" AND display_name OPERATOR(pg_catalog.!~) '[{_FORBIDDEN}]'"
    f" AND display_name OPERATOR(pg_catalog.!~) '^[{_WS}]'"
    f" AND display_name OPERATOR(pg_catalog.!~) '[{_WS}]$')"
)

_SNAPSHOT_KEYS: Final = (
    "'schema_version', 'staff_id', 'status', 'display_name', 'access', 'status_changed_at'"
)
_RESULT_KEYS: Final = "'schema_version', 'client_action_id', 'operation', 'staff'"
_OPERATIONS: Final = "'add', 'rename', 'deactivate', 'reactivate', 'grant', 'revoke'"


def _closed(column: str, keys: str) -> str:
    return (
        f"(pg_catalog.jsonb_typeof({column}) = 'object'"
        f" AND {column} OPERATOR(pg_catalog.?&) ARRAY[{keys}]"
        f" AND ({column} OPERATOR(pg_catalog.-) ARRAY[{keys}]) = '{{}}'::jsonb)"
    )


T021_SQL: Final = f"""
-- t021:step:guard
DO $guard$
BEGIN
    IF (SELECT count(*)
          FROM pg_catalog.pg_roles AS r
         WHERE r.rolname = '{STAFF_OWNER_ROLE}'
           AND NOT r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls) <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{_MSG_ROLES}';
    END IF;
END
$guard$;

-- t021:step:tables
CREATE TABLE {_S}.m01_staff (
    staff_id uuid PRIMARY KEY,
    status varchar(8) NOT NULL CHECK (status IN ('active', 'inactive')),
    status_changed_at timestamptz NOT NULL,
    display_name varchar(80) NOT NULL CHECK {DISPLAY_NAME_CHECK}
);
CREATE TABLE {_S}.m01_staff_followup_access (
    staff_id uuid NOT NULL REFERENCES {_S}.m01_staff (staff_id),
    access varchar(4) NOT NULL CHECK (access IN ('read', 'act')),
    granted_at timestamptz NOT NULL,
    PRIMARY KEY (staff_id, access)
);
CREATE TABLE {_S}.m01_staff_action_keys (
    client_action_id uuid PRIMARY KEY,
    digest bytea NOT NULL CHECK (pg_catalog.octet_length(digest) = 32),
    actor_id varchar(128) NOT NULL,
    staff_id uuid NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE {_S}.m01_staff_admin_log (
    log_id uuid PRIMARY KEY,
    client_action_id uuid NOT NULL UNIQUE
        REFERENCES {_S}.m01_staff_action_keys (client_action_id),
    staff_id uuid NOT NULL,
    operation varchar(16) NOT NULL CHECK (operation IN ({_OPERATIONS})),
    before jsonb,
    after jsonb NOT NULL,
    result jsonb NOT NULL,
    actor_id varchar(128) NOT NULL,
    execution_id uuid NOT NULL,
    request_id varchar(128),
    purpose_code varchar(64) NOT NULL,
    digest bytea NOT NULL CHECK (pg_catalog.octet_length(digest) = 32),
    created_at timestamptz NOT NULL,
    CONSTRAINT m01_staff_admin_log_before_iff_not_add
        CHECK ((before IS NULL) = (operation = 'add')),
    CONSTRAINT m01_staff_admin_log_before_closed
        CHECK (before IS NULL OR {_closed("before", _SNAPSHOT_KEYS)}),
    CONSTRAINT m01_staff_admin_log_after_closed CHECK {_closed("after", _SNAPSHOT_KEYS)},
    CONSTRAINT m01_staff_admin_log_result_closed CHECK {_closed("result", _RESULT_KEYS)}
);

-- t021:step:rejectors
CREATE FUNCTION {_S}.staff_record_reject() RETURNS trigger
    LANGUAGE plpgsql AS $reject$
BEGIN
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STAFF_RECORD_IMMUTABLE';
END
$reject$;
REVOKE ALL ON FUNCTION {_S}.staff_record_reject() FROM PUBLIC;
CREATE TRIGGER m01_staff_no_delete
    BEFORE DELETE ON {_S}.m01_staff
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_action_keys_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_action_keys
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_action_keys_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_action_keys
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_admin_log_immutable
    BEFORE UPDATE OR DELETE ON {_S}.m01_staff_admin_log
    FOR EACH ROW EXECUTE FUNCTION {_S}.staff_record_reject();
CREATE TRIGGER m01_staff_admin_log_no_truncate
    BEFORE TRUNCATE ON {_S}.m01_staff_admin_log
    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.staff_record_reject();

-- t021:step:runtime_privileges
REVOKE ALL ON {_S}.m01_staff, {_S}.m01_staff_followup_access,
    {_S}.m01_staff_action_keys, {_S}.m01_staff_admin_log
    FROM {RUNTIME_ROLE}, PUBLIC;

-- t021:step:grants
GRANT SELECT, INSERT ON {_S}.m01_staff TO {STAFF_OWNER_ROLE};
GRANT UPDATE (status, status_changed_at, display_name) ON {_S}.m01_staff
    TO {STAFF_OWNER_ROLE};
GRANT SELECT, INSERT, DELETE ON {_S}.m01_staff_followup_access TO {STAFF_OWNER_ROLE};
GRANT SELECT, INSERT ON {_S}.m01_staff_action_keys TO {STAFF_OWNER_ROLE};
GRANT INSERT ON {_S}.m01_staff_admin_log TO {STAFF_OWNER_ROLE};
GRANT SELECT (client_action_id, staff_id, operation, actor_id, digest, after, result)
    ON {_S}.m01_staff_admin_log TO {STAFF_OWNER_ROLE};
"""

# ---------------------------------------------------------------------------
# Typed gateways t022..t024 (RED TRANCHE: fail-closed stubs)
# ---------------------------------------------------------------------------

_STUB_BODY: Final = r"""
DECLARE
    v_setting text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'STAFF_NOT_IMPLEMENTED';
END;
"""
_STUB_BODY_SHA256: Final = "06b27a53e68ec36f7e7381e2f5da2773e4c5a7e80a499bf06a9359333fa63385"

_VOLATILITY_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE"}
_VOLATILITY_CODE: Final = {"stable": "s", "volatile": "v"}
_RETURN_WORD: Final = {"bool": "bool", "jsonb": "jsonb"}
_Params = tuple[tuple[str, str], ...]


def _template(name: str, params: _Params, returns: str, volatility: str, grantee: str) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    return (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS {_RETURN_WORD[returns]}\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${_STUB_BODY}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
        f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {grantee};\n"
    )


def _definition(
    name: str, params: _Params, returns: str, volatility: str, grantee: str
) -> UnitDefinition:
    def acl() -> list[dict[str, Any]]:
        return [{"grantee": grantee, "privileges": ["EXECUTE"]}]

    return UnitDefinition(
        template=_template(name, params, returns, volatility, grantee),
        execution_role=STAFF_OWNER_ROLE,
        kind=TYPED_FUNCTION_KIND,
        policy={
            "policy_format": 1,
            "semantic_version": 1,
            "parser_package": "pglast",
            "parser_version": "7.21",
            "grammar_major": 17,
            "functions": [
                {
                    "schema": _S,
                    "name": name,
                    "inputs": [{"name": p, "type": t} for p, t in params],
                    "outputs": [],
                    "return_type": returns,
                    "language": "plpgsql",
                    "is_procedure": False,
                    "replace": False,
                    "security_definer": True,
                    "volatility": volatility,
                    "parallel": "unsafe",
                    "strict": False,
                    "config": [_CONFIG],
                    "body_sha256": _STUB_BODY_SHA256,
                    "acl": acl(),
                    "comment": None,
                }
            ],
        },
        policy_verification={
            "kind": "function_metadata",
            "functions": [
                {
                    "name": name,
                    "argument_types": [t for _, t in params],
                    "owner": STAFF_OWNER_ROLE,
                    "security_definer": True,
                    "config": [_CONFIG],
                    "acl": acl(),
                    "body": _STUB_BODY,
                }
            ],
        },
    )


def _profile(
    name: str, params: _Params, returns: str, volatility: str, grantee: str
) -> InstalledStateProfile:
    return InstalledStateProfile(
        function_name=name,
        argument_types=tuple(t for _, t in params),
        owner=STAFF_OWNER_ROLE,
        execution_role=STAFF_OWNER_ROLE,
        prokind="f",
        return_type=returns,
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility=_VOLATILITY_CODE[volatility],
        parallel="u",
        strict=False,
        installed_acl=(
            (STAFF_OWNER_ROLE, "EXECUTE", STAFF_OWNER_ROLE, False),
            (grantee, "EXECUTE", STAFF_OWNER_ROLE, False),
        ),
    )


# (migration id, function, parameters, return type, volatility, EXECUTE grantee).
# Assignable is explicitly VOLATILE (v2 review): its lock statement and its access
# read each take a fresh READ COMMITTED snapshot. EXECUTE on it goes to the M05
# owner only; admin and display go to the runtime.
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str, str, str], ...]] = (
    ("t022_m01_staff_assignable", "m01_staff_assignable", (("p_staff_id", "uuid"),),
     "bool", "volatile", M05_OWNER_ROLE),
    ("t023_m01_staff_admin", "m01_staff_admin", (("p_command", "jsonb"),),
     "jsonb", "volatile", RUNTIME_ROLE),
    ("t024_m01_staff_display", "m01_staff_display", (("p_query", "jsonb"),),
     "jsonb", "stable", RUNTIME_ROLE),
)

STAFF_GATEWAY_UNITS: Final = MappingProxyType(
    {mid: _definition(name, params, ret, vol, grantee)
     for mid, name, params, ret, vol, grantee in _FUNCTIONS}
)
STAFF_GATEWAY_PROFILES: Final = MappingProxyType(
    {mid: _profile(name, params, ret, vol, grantee)
     for mid, name, params, ret, vol, grantee in _FUNCTIONS}
)

STAFF_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(
    {T021_MIGRATION_ID: T021_SQL, **STAFF_GATEWAY_UNITS}
)
