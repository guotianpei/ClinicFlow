"""Option B B5: the synthetic display and scenario units (t095..t097). SYNTHETIC ONLY.

Composed ONLY into the synthetic registry (`synthetic.composition`, target 97); production's
registry and catalogue are unchanged. All three are typed SECURITY DEFINER functions owned by
the M05 owner role, EXECUTE for the runtime role only, built on the t092 pattern
(`m05.gateways`): the tenant context check, schema-local lookups, fixed refusal codes.

- t095 `m05_synthetic_display(p_item_id uuid) RETURNS jsonb` (STABLE, staff READ): the
  synthetic patient display of one follow-up item (B5 v1r1 C1/C2). The appointment is reached
  ONLY through the item's own source row (inbound reply / reminder window / write-back op,
  one table per kind, each source column UNIQUE) and must belong to the item's patient;
  anything else is `{"linked": false}` (fail closed, never a guess, never by patient_id).
- t096 `m05_synthetic_scenario_claim(p_scenario text, p_refs jsonb) RETURNS jsonb`
  (VOLATILE, the load's first statement, v1r2 §3): a transaction-scoped advisory lock per
  (tenant, scenario), then {"schema_version": 1, "present": <count of the given refs already
  present>, "now": <clock_timestamp() after the lock, ISO-8601 UTC>}: the load's clock.
- t097 `m05_synthetic_scenario_present(p_refs jsonb) RETURNS jsonb` (STABLE, staff READ,
  v1r3 §2): {"schema_version": 1, "present": [...]}, the exact sorted subset of the given
  refs present, no lock.
`p_refs` is a JSON array of canonical uuid strings (0..64; t096 needs at least one): the
platform's installed-state rule F-INT1 admits no array type in a function signature.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Any, Final

from haloflow.m01.provisioning.installed_state import InstalledStateProfile
from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND, UnitDefinition, UnitDefinitions
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05 import gateways as _gw

_S: Final = "{schema}"
_CONFIG: Final = "search_path=pg_catalog, {schema}, pg_temp"
_Params = tuple[tuple[str, str], ...]

DISPLAY_ID: Final = "t095_m05_synthetic_display"
CLAIM_ID: Final = "t096_m05_synthetic_scenario_claim"
PRESENT_ID: Final = "t097_m05_synthetic_scenario_present"
MAX_REFS: Final = 64

_UNLINKED: Final = "pg_catalog.jsonb_build_object('schema_version', 1, 'linked', false)"
_REPLY_KINDS: Final = "('reschedule_request', 'cancel_request', 'unclear_reply')"

_DISPLAY_BODY: Final = r"""
DECLARE
    v_setting text;
    v_item record;
    v_n integer := 0;
    v_ref uuid;
    v_a record;
BEGIN""" + _gw._TENANT_CHECK + r"""
    SELECT i.item_id, i.item_kind, i.source_event_id, i.patient_id INTO v_item
      FROM {schema}.m05_followup_items AS i WHERE i.item_id = p_item_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'ITEM_NOT_FOUND';
    END IF;
    -- The item's OWN source row, by kind; it must name the item's patient (or, for a check,
    -- the item itself). Each source column is UNIQUE, so v_n is 0 or 1.
    IF v_item.item_kind IN """ + _REPLY_KINDS + r""" THEN
        SELECT pg_catalog.count(*), pg_catalog.min(r.appointment_ref::text)::uuid
          INTO v_n, v_ref
          FROM {schema}.m05_inbound_replies AS r
         WHERE r.event_id = v_item.source_event_id AND r.patient_id = v_item.patient_id;
    ELSIF v_item.item_kind = 'no_reply' THEN
        SELECT pg_catalog.count(*), pg_catalog.min(w.appointment_ref::text)::uuid
          INTO v_n, v_ref
          FROM {schema}.m05_reminder_windows AS w
         WHERE w.closed_event_id = v_item.source_event_id AND w.patient_id = v_item.patient_id;
    ELSIF v_item.item_kind = 'ehr_confirmation_check' THEN
        SELECT pg_catalog.count(*), pg_catalog.min(o.appointment_ref::text)::uuid
          INTO v_n, v_ref
          FROM {schema}.m05_writeback_ops AS o
         WHERE o.event_id = v_item.source_event_id AND o.item_id = v_item.item_id
           AND o.patient_id = v_item.patient_id;
    END IF;
    IF v_n <> 1 OR v_ref IS NULL THEN
        RETURN """ + _UNLINKED + r""";
    END IF;
    SELECT a.start_at, a.patient_id, p.first_name, p.department_tz, p.provider_ref INTO v_a
      FROM {schema}.m05_appointments AS a
      JOIN {schema}.m05_appointment_profile AS p ON p.appointment_ref = a.appointment_ref
     WHERE a.appointment_ref = v_ref;
    IF NOT FOUND OR v_a.patient_id IS DISTINCT FROM v_item.patient_id
       OR v_a.first_name IS NULL THEN
        RETURN """ + _UNLINKED + r""";
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'linked', true,
        'first_name', v_a.first_name,
        'start_at', pg_catalog.to_char(pg_catalog.timezone('UTC', v_a.start_at),
                                       'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
        'department_tz', v_a.department_tz, 'provider_ref', v_a.provider_ref);
END;
"""

# p_refs: a JSON array of 0..64 canonical lowercase uuid strings (F-INT1 admits no array
# type in a function signature; the array exists only as a local variable).
_REFS_CHECK: Final = r"""
    IF p_refs IS NULL OR pg_catalog.jsonb_typeof(p_refs) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_refs) > """ + str(MAX_REFS) + r"""
       OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(p_refs) AS e(v)
                   WHERE pg_catalog.jsonb_typeof(e.v) IS DISTINCT FROM 'string'
                      OR (e.v OPERATOR(pg_catalog.#>>) '{}') OPERATOR(pg_catalog.!~)
                         '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_refs := ARRAY(SELECT (e.v OPERATOR(pg_catalog.#>>) '{}')::uuid
                      FROM pg_catalog.jsonb_array_elements(p_refs) AS e(v));
"""

_CLAIM_BODY: Final = r"""
DECLARE
    v_setting text;
    v_refs uuid[];
    v_n integer;
BEGIN""" + _gw._TENANT_CHECK + _REFS_CHECK + r"""
    IF p_scenario IS NULL OR p_scenario NOT IN ('S1', 'S2', 'S3', 'S4')
       OR pg_catalog.cardinality(v_refs) = 0 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    -- One lock per (tenant, scenario), held to the end of the load's transaction: a
    -- concurrent load of the same scenario waits here and then sees this load's rows.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        'haloflow.synthetic.scenario:' OPERATOR(pg_catalog.||) v_setting
        OPERATOR(pg_catalog.||) ':' OPERATOR(pg_catalog.||) p_scenario, 0));
    SELECT pg_catalog.count(*) INTO v_n FROM {schema}.m05_appointments AS a
     WHERE a.appointment_ref OPERATOR(pg_catalog.=) ANY (v_refs);
    -- The load's clock (B5 b5.load.clock): read after the lock, in this transaction.
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'present', v_n, 'now',
        pg_catalog.to_char(pg_catalog.clock_timestamp() AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
END;
"""

_PRESENT_BODY: Final = r"""
DECLARE
    v_setting text;
    v_refs uuid[];
BEGIN""" + _gw._TENANT_CHECK + _REFS_CHECK + r"""
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'present',
        COALESCE((SELECT pg_catalog.jsonb_agg(a.appointment_ref::text ORDER BY a.appointment_ref)
                    FROM {schema}.m05_appointments AS a
                   WHERE a.appointment_ref OPERATOR(pg_catalog.=) ANY (v_refs)),
                 '[]'::jsonb));
END;
"""

_WORD: Final = {"stable": "STABLE", "volatile": "VOLATILE"}
_CODE: Final = {"stable": "s", "volatile": "v"}

# (migration id, function, parameters, return type, volatility, body)
_FUNCTIONS: Final[tuple[tuple[str, str, _Params, str, str, str], ...]] = (
    (DISPLAY_ID, "m05_synthetic_display", (("p_item_id", "uuid"),), "jsonb", "stable",
     _DISPLAY_BODY),
    (CLAIM_ID, "m05_synthetic_scenario_claim", (("p_scenario", "text"), ("p_refs", "jsonb")),
     "jsonb", "volatile", _CLAIM_BODY),
    (PRESENT_ID, "m05_synthetic_scenario_present", (("p_refs", "jsonb"),), "jsonb", "stable",
     _PRESENT_BODY),
)


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _template(name: str, params: _Params, returns: str, volatility: str, body: str) -> str:
    signature = ", ".join(f"{p} {t}" for p, t in params)
    argtypes = ", ".join(t for _, t in params)
    return (
        f"CREATE FUNCTION {_S}.{name}({signature}) RETURNS {returns}\n"
        f"    LANGUAGE plpgsql {_WORD[volatility]} PARALLEL UNSAFE"
        " CALLED ON NULL INPUT SECURITY DEFINER\n"
        f"    SET search_path = pg_catalog, {_S}, pg_temp\n"
        f"    AS $body${body}$body$;\n"
        f"REVOKE ALL ON FUNCTION {_S}.{name}({argtypes}) FROM PUBLIC;\n"
        f"GRANT EXECUTE ON FUNCTION {_S}.{name}({argtypes}) TO {RUNTIME_ROLE};\n"
    )


def _definition(name: str, params: _Params, returns: str, volatility: str,
                body: str) -> UnitDefinition:
    def acl() -> list[dict[str, Any]]:
        return [{"grantee": RUNTIME_ROLE, "privileges": ["EXECUTE"]}]

    return UnitDefinition(
        template=_template(name, params, returns, volatility, body),
        execution_role=M05_OWNER_ROLE,
        kind=TYPED_FUNCTION_KIND,
        policy={
            "policy_format": 1, "semantic_version": 1, "parser_package": "pglast",
            "parser_version": "7.21", "grammar_major": 17,
            "functions": [{
                "schema": _S, "name": name,
                "inputs": [{"name": p, "type": t} for p, t in params],
                "outputs": [], "return_type": returns, "language": "plpgsql",
                "is_procedure": False, "replace": False, "security_definer": True,
                "volatility": volatility, "parallel": "unsafe", "strict": False,
                "config": [_CONFIG], "body_sha256": _sha(body), "acl": acl(),
                "comment": None,
            }],
        },
        policy_verification={
            "kind": "function_metadata",
            "functions": [{
                "name": name, "argument_types": [t for _, t in params],
                "owner": M05_OWNER_ROLE, "security_definer": True, "config": [_CONFIG],
                "acl": acl(), "body": body,
            }],
        },
    )


def _profile(name: str, params: _Params, returns: str, volatility: str
             ) -> InstalledStateProfile:
    return InstalledStateProfile(
        function_name=name, argument_types=tuple(t for _, t in params),
        owner=M05_OWNER_ROLE, execution_role=M05_OWNER_ROLE, prokind="f",
        return_type=returns, returns_set=False, language="plpgsql", security_definer=True,
        volatility=_CODE[volatility], parallel="u", strict=False,
        installed_acl=((M05_OWNER_ROLE, "EXECUTE", M05_OWNER_ROLE, False),
                       (RUNTIME_ROLE, "EXECUTE", M05_OWNER_ROLE, False)),
    )


B5_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType({
    mid: _definition(name, params, returns, vol, body)
    for mid, name, params, returns, vol, body in _FUNCTIONS})
B5_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    mid: _profile(name, params, returns, vol)
    for mid, name, params, returns, vol, _body in _FUNCTIONS})
