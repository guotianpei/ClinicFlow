"""M05 typed gateway units t014..t020: one SECURITY DEFINER function each.

Owner and execution role `haloflow_m05_owner`; EXECUTE to `haloflow_runtime` only;
no PUBLIC; `search_path` pinned to pg_catalog, the tenant schema, pg_temp (package
v1 section 2.3, A-INV decisions v1).

Tranche B1 (gate PASS 20261031T194129Z, item 5): every body is a FAIL-CLOSED STUB.
It checks `app.tenant_id` well-formedness (22023, the part A pattern; not
authentication) and then refuses with P0001 / M05_NOT_IMPLEMENTED, touching no
table. B2..B4 replace bodies one function at a time, each with its behaviour tests;
a changed body changes the unit checksum, which is acceptable because no tenant
outside disposable test databases is at the M05 test target.

Each body's SHA-256 is a reviewed literal, so a changed body fails composition
until the literal is updated.
"""

from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition
from haloflow.m02.roles import M05_OWNER_ROLE

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_VOLATILITY_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE"}
_VOLATILITY_CODE: Final = {"stable": "s", "volatile": "v"}
_Params = tuple[tuple[str, str], ...]

_STUB_BODY: Final = r"""
DECLARE
    v_setting text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'M05_NOT_IMPLEMENTED';
END;
"""
_STUB_BODY_SHA256: Final = "11cf525d221081fdd4402cac2b8452ce4d371df2960973dd617936c0c58ea280"


def _template(name: str, params: _Params, volatility: str, body: str) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    return (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS jsonb\n"
        f"    LANGUAGE plpgsql {_VOLATILITY_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
        f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {RUNTIME_ROLE};\n"
    )


def _definition(
    name: str, params: _Params, volatility: str, body: str, body_sha256: str
) -> UnitDefinition:
    def acl() -> list[dict[str, Any]]:
        return [{"grantee": RUNTIME_ROLE, "privileges": ["EXECUTE"]}]

    return UnitDefinition(
        template=_template(name, params, volatility, body),
        execution_role=M05_OWNER_ROLE,
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
                    "return_type": "jsonb",
                    "language": "plpgsql",
                    "is_procedure": False,
                    "replace": False,
                    "security_definer": True,
                    "volatility": volatility,
                    "parallel": "unsafe",
                    "strict": False,
                    "config": [_CONFIG],
                    "body_sha256": body_sha256,
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
                    "owner": M05_OWNER_ROLE,
                    "security_definer": True,
                    "config": [_CONFIG],
                    "acl": acl(),
                    "body": body,
                }
            ],
        },
    )


def _profile(name: str, params: _Params, volatility: str) -> InstalledStateProfile:
    return InstalledStateProfile(
        function_name=name,
        argument_types=tuple(t for _, t in params),
        owner=M05_OWNER_ROLE,
        execution_role=M05_OWNER_ROLE,
        prokind="f",
        return_type="jsonb",
        returns_set=False,
        language="plpgsql",
        security_definer=True,
        volatility=_VOLATILITY_CODE[volatility],
        parallel="u",
        strict=False,
        installed_acl=(
            (M05_OWNER_ROLE, "EXECUTE", M05_OWNER_ROLE, False),
            (RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False),
        ),
    )


# (migration id, function, parameters, volatility). Writes VOLATILE, reads STABLE,
# all PARALLEL UNSAFE (gate ruling 2).
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str], ...]] = (
    ("t014_m05_create_item", "m05_create_item", (("p_command", "jsonb"),), "volatile"),
    ("t015_m05_prepare_action", "m05_prepare_action", (("p_command", "jsonb"),), "volatile"),
    ("t016_m05_apply_action", "m05_apply_action", (("p_command", "jsonb"),), "volatile"),
    ("t017_m05_get_item", "m05_get_item", (("p_item_id", "uuid"),), "stable"),
    ("t018_m05_list_items", "m05_list_items", (("p_query", "jsonb"),), "stable"),
    ("t019_m05_get_history", "m05_get_history", (("p_item_id", "uuid"),), "stable"),
    ("t020_m05_read_note", "m05_read_note", (("p_note_id", "uuid"),), "stable"),
)

M05_GATEWAY_UNITS: Final = MappingProxyType(
    {
        migration_id: _definition(name, params, volatility, _STUB_BODY, _STUB_BODY_SHA256)
        for migration_id, name, params, volatility in _FUNCTIONS
    }
)
M05_GATEWAY_PROFILES: Final = MappingProxyType(
    {
        migration_id: _profile(name, params, volatility)
        for migration_id, name, params, volatility in _FUNCTIONS
    }
)
