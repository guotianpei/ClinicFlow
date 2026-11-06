"""L-1S package S-B: per-tenant units t044/t045 (due-window selection).

Governing: S-B requirements v1/v1r1, architecture v1, interface pins v1 and the red
package v1 interface deltas D1..D7 (red gate PASS at 22be664, Codex 20261106T144748Z).

NOT composed into production: `haloflow.composition` names none of this. These units
install only through the S-B TEST registry (tests/m05/sb_support.py, target 45:
the S-A units t037..t043, then these); the production target stays 36 (SB-S01).

- ``t044_m05_reminder_windows_due``  migrator-owned partial index on
  ``m05_reminder_windows (deadline, reminder_id) WHERE state = 'awaiting'``.
- ``t045_m05_due_windows``  runtime selector (SECURITY DEFINER, EXECUTE to the
  runtime, run as the M05 owner): the timer workload only (S-A ``_workload("timer")``),
  one ``clock_timestamp()`` cutoff per call, read-only (no row lock, no write), returns
  a jsonb scalar ``{"schema_version": 1, "keys": [{"deadline", "reminder_id"}, ...]}``
  (D1: M01 installed-state profiles admit scalar return types only).

Selection is a hint, never a decision: every returned window is closed only through
S-A's t041, which re-checks state and due-ness under the contact lock with
``intake_reply_is_timely``. The selector's inline comparison ``DUE_PREDICATE`` is the
disclosed, index-usable exception to S-A's single-predicate rule (Codex ALIGN 07:58 ET),
guarded by SB-T01 (fixed-cutoff equivalence) and SB-S04 (source tie).
"""

from types import MappingProxyType
from typing import Final

from haloflow.m05.intake_units import (
    _ATTRIBUTION,
    _DECLARE_COMMON,
    _TENANT,
    _definition,
    _profile,
    _ts,
    _workload,
)

T044_MIGRATION_ID: Final = "t044_m05_reminder_windows_due"
T045_MIGRATION_ID: Final = "t045_m05_due_windows"
SELECTOR_NAME: Final = "m05_due_windows"

T044_SQL: Final = """
CREATE INDEX m05_reminder_windows_due ON {schema}.m05_reminder_windows
    (deadline, reminder_id) WHERE state = 'awaiting';
"""

# D2: the ONE deadline/cutoff comparison in the selector (deadline instant is due).
DUE_PREDICATE: Final = "w.deadline OPERATOR(pg_catalog.<=) v_cutoff"

_SELECT: Final = (
    "SELECT w.deadline, w.reminder_id FROM {schema}.m05_reminder_windows AS w"
    " WHERE w.state OPERATOR(pg_catalog.=) 'awaiting' AND " + DUE_PREDICATE
)
_AFTER: Final = (" AND (w.deadline, w.reminder_id) OPERATOR(pg_catalog.>)"
                 " (v_after_deadline, v_after_id)")
_UNTIL: Final = (" AND (w.deadline, w.reminder_id) OPERATOR(pg_catalog.<=)"
                 " (v_until_deadline, v_until_id)")
_ORDER: Final = " ORDER BY w.deadline, w.reminder_id LIMIT v_limit"

# D3: one statically shaped query per cursor case (plans need no NULL-guarded branches).
DUE_QUERIES: Final = MappingProxyType({
    "first": _SELECT + _ORDER,
    "after": _SELECT + _AFTER + _ORDER,
    "until": _SELECT + _UNTIL + _ORDER,
    "after_until": _SELECT + _AFTER + _UNTIL + _ORDER,
})

_TS_PATTERN: Final = r"'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$'"
_UTC_TEXT: Final = "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'"


def _cursor(name: str) -> str:
    """Validate p_command->'<name>' (null, or exactly {deadline, reminder_id} with a
    canonical UTC timestamp and a lower-case UUID) into v_<name>_deadline/_id."""

    node = f"p_command OPERATOR(pg_catalog.->) '{name}'"
    return f"""
    v_cur := {node};
    IF pg_catalog.jsonb_typeof(v_cur) = 'object' THEN
        IF (SELECT pg_catalog.array_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_cur) AS k)
              IS DISTINCT FROM ARRAY['deadline', 'reminder_id']::text[]
           OR pg_catalog.jsonb_typeof(v_cur OPERATOR(pg_catalog.->) 'deadline')
              IS DISTINCT FROM 'string'
           OR (v_cur OPERATOR(pg_catalog.->>) 'deadline') OPERATOR(pg_catalog.!~) {_TS_PATTERN}
           OR pg_catalog.jsonb_typeof(v_cur OPERATOR(pg_catalog.->) 'reminder_id')
              IS DISTINCT FROM 'string'
           OR (v_cur OPERATOR(pg_catalog.->>) 'reminder_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        BEGIN
            v_ts := (v_cur OPERATOR(pg_catalog.->>) 'deadline')::timestamptz;
        EXCEPTION WHEN data_exception THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END;
        IF pg_catalog.to_char(pg_catalog.timezone('UTC', v_ts), {_UTC_TEXT})
           IS DISTINCT FROM (v_cur OPERATOR(pg_catalog.->>) 'deadline') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
        END IF;
        v_{name}_deadline := v_ts;
        v_{name}_id := (v_cur OPERATOR(pg_catalog.->>) 'reminder_id')::uuid;
    ELSIF pg_catalog.jsonb_typeof(v_cur) IS DISTINCT FROM 'null' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
"""


def _collect(query: str) -> str:
    return (
        "SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object("
        "'deadline', " + _ts("q.deadline") + ", 'reminder_id', q.reminder_id::text)"
        " ORDER BY q.deadline, q.reminder_id), '[]'::jsonb) INTO v_out FROM ("
        + query + ") AS q;"
    )


_SELECTOR_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_cur jsonb;
    v_ts timestamptz;
    v_after_deadline timestamptz;
    v_after_id uuid;
    v_until_deadline timestamptz;
    v_until_id uuid;
    v_limit integer;
    v_cutoff timestamptz;
    v_out jsonb;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['after', 'attribution', 'limit', 'schema_version',
                                     'until']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'limit')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'limit')::text
          OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,2}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
    v_limit := (p_command OPERATOR(pg_catalog.->>) 'limit')::integer;
    IF v_limit OPERATOR(pg_catalog.>) 500 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'COMMAND_MALFORMED';
    END IF;
""" + _ATTRIBUTION + _cursor("after") + _cursor("until") + r"""
    IF v_after_deadline IS NOT NULL AND v_until_deadline IS NOT NULL
       AND (v_after_deadline, v_after_id) OPERATOR(pg_catalog.>=)
           (v_until_deadline, v_until_id) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'BOUNDS_INVALID';
    END IF;
""" + _workload("timer") + r"""
    v_cutoff := pg_catalog.clock_timestamp();
    IF v_after_deadline IS NULL AND v_until_deadline IS NULL THEN
        """ + _collect(DUE_QUERIES["first"]) + r"""
    ELSIF v_until_deadline IS NULL THEN
        """ + _collect(DUE_QUERIES["after"]) + r"""
    ELSIF v_after_deadline IS NULL THEN
        """ + _collect(DUE_QUERIES["until"]) + r"""
    ELSE
        """ + _collect(DUE_QUERIES["after_until"]) + r"""
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'keys', v_out);
END;
"""
)

_COMMAND: Final = (("p_command", "jsonb"),)

SB_TENANT_MIGRATIONS: Final = MappingProxyType({
    T044_MIGRATION_ID: T044_SQL,
    T045_MIGRATION_ID: _definition(SELECTOR_NAME, _COMMAND, "volatile", _SELECTOR_BODY, True),
})
SB_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    T045_MIGRATION_ID: _profile(SELECTOR_NAME, _COMMAND, "volatile", True),
})
