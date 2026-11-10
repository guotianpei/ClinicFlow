"""L-1S package S-H, checkpoint H3: one additive typed unit, t075 (TEST registry only).

- ``t075``  ``m05_outbound_accepted_pending``: lists ops in state ``accepted`` only (a
  send the provider accepted whose registration did not complete), ordered ascending by
  ``(updated_at, op_id)``, strictly after an optional keyset cursor, bounded by
  ``limit`` 1..1000. Read-only: it never writes. The t056 pattern exactly: the same
  tenant, attribution and dispatch-workload fences, the same profile and ACL (runtime
  EXECUTE). ``held`` (t056) is NOT widened and no H1/H2 unit is edited.

Command keys are exactly ``{attribution, after, limit, schema_version}``. ``after`` is
JSON null or ``{updated_at, op_id}`` with both fields present: ``updated_at`` in the
canonical UTC text the units emit (``YYYY-MM-DDTHH:MM:SS.ffffffZ``, which must also
round-trip, so an impossible date such as Feb 30 is malformed, never a raw datetime
error) and ``op_id`` a lower-case UUID. Anything else is ``COMMAND_MALFORMED``.
Governing: H3 package v1 + v1r1 + v1r2 deltas, red interface pins v1, Codex source gate
PASS (20261109T201944Z). NOT composed; no production registration.
"""

from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.units import UnitDefinitions
from haloflow.m05.intake_units import (
    _ATTRIBUTION,
    _COMMAND,
    _DECLARE_COMMON,
    _TENANT,
    _definition,
    _profile,
    _ts,
    _workload,
)

T075_MIGRATION_ID: Final = "t075_m05_outbound_accepted_pending"
T075_FUNCTION: Final = "m05_outbound_accepted_pending"

_ACCEPTED_PENDING_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    c_ts constant text := '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$';
    v_limit integer;
    v_after jsonb;
    v_after_keys text[];
    v_ts_text text;
    v_ts timestamptz;
    v_op uuid;
    v_ops jsonb;
BEGIN""" + _TENANT + r"""
    v_after := p_command OPERATOR(pg_catalog.->) 'after';
    IF v_keys IS DISTINCT FROM ARRAY['after', 'attribution', 'limit', 'schema_version']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'limit')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'limit') OPERATOR(pg_catalog.!~) '^[0-9]{1,4}$'
       OR pg_catalog.jsonb_typeof(v_after) NOT IN ('null', 'object') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF pg_catalog.jsonb_typeof(v_after) = 'object' THEN
        SELECT pg_catalog.array_agg(k ORDER BY k) INTO v_after_keys
          FROM pg_catalog.jsonb_object_keys(v_after) AS k;
        IF v_after_keys IS DISTINCT FROM ARRAY['op_id', 'updated_at']::text[]
           OR pg_catalog.jsonb_typeof(v_after OPERATOR(pg_catalog.->) 'updated_at')
              IS DISTINCT FROM 'string'
           OR (v_after OPERATOR(pg_catalog.->>) 'updated_at') OPERATOR(pg_catalog.!~) c_ts
           OR pg_catalog.jsonb_typeof(v_after OPERATOR(pg_catalog.->) 'op_id')
              IS DISTINCT FROM 'string'
           OR (v_after OPERATOR(pg_catalog.->>) 'op_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
    END IF;
""" + _ATTRIBUTION + _workload("dispatch") + r"""
    v_limit := (p_command OPERATOR(pg_catalog.->>) 'limit')::integer;
    IF v_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    IF pg_catalog.jsonb_typeof(v_after) = 'object' THEN
        v_ts_text := v_after OPERATOR(pg_catalog.->>) 'updated_at';
        BEGIN
            -- the canonical text must parse AND round-trip (Feb 30, hour 25, month 13 do not)
            v_ts := pg_catalog.timezone('UTC', (pg_catalog.rtrim(v_ts_text, 'Z'))::timestamp);
        EXCEPTION WHEN data_exception THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END;
        IF """ + _ts("v_ts") + r""" IS DISTINCT FROM v_ts_text THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_op := (v_after OPERATOR(pg_catalog.->>) 'op_id')::uuid;
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'op_id', p.op_id::text,
               'updated_at', """ + _ts("p.updated_at") + r""")
               ORDER BY p.updated_at, p.op_id), '[]'::jsonb)
      INTO v_ops
      FROM (SELECT o.op_id, o.updated_at
              FROM {schema}.m05_outbound_ops AS o
             WHERE o.state = 'accepted'
               AND (v_ts IS NULL
                    OR o.updated_at OPERATOR(pg_catalog.>) v_ts
                    OR (o.updated_at OPERATOR(pg_catalog.=) v_ts
                        AND o.op_id OPERATOR(pg_catalog.>) v_op))
             ORDER BY o.updated_at, o.op_id LIMIT v_limit) AS p;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'ops', v_ops);
END;
"""
)

SH3_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType({
    T075_MIGRATION_ID: _definition(T075_FUNCTION, _COMMAND, "volatile",
                                   _ACCEPTED_PENDING_BODY, True),
})
SH3_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    T075_MIGRATION_ID: _profile(T075_FUNCTION, _COMMAND, "volatile", True),
})
