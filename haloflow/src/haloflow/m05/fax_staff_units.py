"""Incoming fax routing FX-IN, package FX2: per-tenant migration units t140..t143. SYNTHETIC ONLY.

Composed ONLY into the synthetic registry behind `include_fx2` (on FX1, target 139 -> 143).
Governing: FX-IN test cases v1r1 FX2/FX3 (Codex ALIGN 19629e35), the FX2 red packet v1
(`claude_fx2-red-packet-v1-2026-11-25.md`), OD-FX6 (open / move / mark filed; no assignment),
OD-FX8 (filed is final), OD-FX9.

- t140 `m05_fx_staff_check(uuid, text) -> boolean` (staff owner; EXECUTE to the M05 owner only;
  the t022 pattern): the m01_staff row FOR SHARE, status active, and read access; 'act'
  additionally needs act access (act never authorizes without read, as capabilities_for).
  False otherwise; never raises for those.
- t141 `m05_fx_staff_action(jsonb)` (runtime): open | move | file | sort. Precedence
  (test cases v1r1 §1): ACTOR_INACTIVE -> FAX_NOT_FOUND -> state guard -> VERSION_CONFLICT ->
  QUEUE_NOT_FOUND. Lock order fax -> sorting -> queue (FOR SHARE).
- t142 `m05_fx_staff_read(jsonb)` (runtime, STABLE): list | get | history. The sender appears
  only as its last four digits; never the line, doc_ref or provider id (FR-11).
- t143 `m05_fx_lines_snapshot(jsonb)` (runtime, STABLE): this tenant's receiving lines, sorted.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinition, UnitDefinitions
from haloflow.m01.staff import units as staff_units
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.m05.intake_units import _ATTRIBUTION, _DECLARE_COMMON, _TENANT
from haloflow.m05.reminder_units import _definition, _profile

CHECK_ID: Final = "t140_m05_fx_staff_check"
ACTION_ID: Final = "t141_m05_fx_staff_action"
READ_ID: Final = "t142_m05_fx_staff_read"
SNAPSHOT_ID: Final = "t143_m05_fx_lines_snapshot"
FX2_TARGET: Final = 143

CHECK_NAME: Final = "m05_fx_staff_check"
ACTION_NAME: Final = "m05_fx_staff_action"
READ_NAME: Final = "m05_fx_staff_read"
SNAPSHOT_NAME: Final = "m05_fx_lines_snapshot"


def _raise(code: str) -> str:
    return f"RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '{code}';"


# --- t140 staff check (staff owner; EXECUTE to the M05 owner) -------------------------------

CHECK_BODY: Final = r"""
DECLARE
    v_setting text;
    v_status text;
BEGIN
    v_setting := pg_catalog.current_setting('app.tenant_id', true);
    IF v_setting IS NULL OR v_setting = ''
       OR v_setting OPERATOR(pg_catalog.!~) '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'tenant context invalid';
    END IF;
    IF p_staff_id IS NULL OR p_access IS NULL OR p_access NOT IN ('read', 'act') THEN
        RETURN false;
    END IF;
    SELECT s.status INTO v_status
      FROM {schema}.m01_staff AS s
     WHERE s.staff_id = p_staff_id
       FOR SHARE;
    IF NOT FOUND OR v_status <> 'active' THEN
        RETURN false;
    END IF;
    -- 'read' needs read; 'act' needs read AND act (the platform grants act only with read:
    -- capabilities_for; Codex 4eabf042).
    RETURN EXISTS (SELECT 1 FROM {schema}.m01_staff_followup_access AS a
                    WHERE a.staff_id = p_staff_id AND a.access = 'read')
       AND (p_access = 'read'
            OR EXISTS (SELECT 1 FROM {schema}.m01_staff_followup_access AS a
                        WHERE a.staff_id = p_staff_id AND a.access = 'act'));
END;
"""

# --- shared command head: closed attribution, an ACTOR whose id is the staff id -------------

_STAFF_HEAD: Final = _ATTRIBUTION + r"""
    IF (v_attr OPERATOR(pg_catalog.->>) 'principal_kind') <> 'actor'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'staff_id')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) 'staff_id') OPERATOR(pg_catalog.!~) c_uuid
       OR (p_command OPERATOR(pg_catalog.->>) 'staff_id')
          IS DISTINCT FROM (v_attr OPERATOR(pg_catalog.->>) 'principal_id') THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_staff := (p_command OPERATOR(pg_catalog.->>) 'staff_id')::uuid;
"""

_UUID_FIELD: Final = r"""
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) '{f}')
          IS DISTINCT FROM 'string'
       OR (p_command OPERATOR(pg_catalog.->>) '{f}') OPERATOR(pg_catalog.!~) c_uuid"""


def _uuid(field: str) -> str:
    return _UUID_FIELD.replace("{f}", field)


_VERSION_FIELD: Final = r"""
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'expected_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->>) 'expected_version')
          OPERATOR(pg_catalog.!~) '^[1-9][0-9]{0,8}$'"""

# --- t141 staff action ----------------------------------------------------------------------

ACTION_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_staff uuid;
    v_action text;
    v_fax_id uuid;
    v_version integer;
    v_queue uuid;
    v_fax record;
    v_sort record;
    v_seq integer;
    v_now timestamptz;
    v_state text;
    v_new_queue uuid;
BEGIN""" + _TENANT + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'action')
          IS DISTINCT FROM 'string' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_action := p_command OPERATOR(pg_catalog.->>) 'action';
    IF v_action = 'open' THEN
        IF v_keys IS DISTINCT FROM ARRAY['action', 'attribution', 'fax_id', 'schema_version',
                                         'staff_id']::text[]""" + _uuid("fax_id") + r""" THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_action = 'file' THEN
        IF v_keys IS DISTINCT FROM ARRAY['action', 'attribution', 'expected_version', 'fax_id',
                                         'schema_version', 'staff_id']::text[]"""
    + _uuid("fax_id") + _VERSION_FIELD + r""" THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_action IN ('move', 'sort') THEN
        IF v_keys IS DISTINCT FROM ARRAY['action', 'attribution', 'expected_version', 'fax_id',
                                         'queue_id', 'schema_version', 'staff_id']::text[]"""
    + _uuid("fax_id") + _uuid("queue_id") + _VERSION_FIELD + r""" THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSE
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;""" + _STAFF_HEAD + r"""
    v_fax_id := (p_command OPERATOR(pg_catalog.->>) 'fax_id')::uuid;
    -- 1. the acting staff member (before any fax lock): read to open, act to change.
    IF NOT {schema}.m05_fx_staff_check(v_staff,
            CASE WHEN v_action = 'open' THEN 'read' ELSE 'act' END) THEN
        """ + _raise("ACTOR_INACTIVE") + r"""
    END IF;
    -- 2. the fax (lock order fax -> sorting -> queue).
    SELECT f.* INTO v_fax FROM {schema}.m05_fx_faxes AS f WHERE f.fax_id = v_fax_id FOR UPDATE;
    IF NOT FOUND THEN
        """ + _raise("FAX_NOT_FOUND") + r"""
    END IF;
    v_now := pg_catalog.clock_timestamp();
    SELECT COALESCE(pg_catalog.max(h.seq), 0) OPERATOR(pg_catalog.+) 1 INTO v_seq
      FROM {schema}.m05_fx_history AS h WHERE h.fax_id = v_fax_id;
    IF v_action = 'open' THEN
        INSERT INTO {schema}.m05_fx_history (fax_id, seq, action, actor, from_queue_id,
            to_queue_id, at) VALUES (v_fax_id, v_seq, 'opened', v_staff::text, NULL, NULL, v_now);
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',
            'fax_id', v_fax_id::text, 'state', v_fax.state, 'queue_id', v_fax.queue_id::text,
            'version', v_fax.version);
    END IF;
    -- 3. the state guard (before the version: test cases v1r1 §1).
    IF v_action = 'sort' THEN
        SELECT s.* INTO v_sort FROM {schema}.m05_fx_sorting AS s
         WHERE s.fax_id = v_fax_id FOR UPDATE;
        IF NOT FOUND THEN
            """ + _raise("NOT_NEEDS_SORTING") + r"""
        END IF;
        IF v_sort.state = 'resolved' THEN
            """ + _raise("ALREADY_SORTED") + r"""
        END IF;
    ELSIF v_fax.state = 'filed' THEN
        """ + _raise("FAX_FILED") + r"""
    ELSIF v_fax.state = 'needs_sorting' THEN
        """ + _raise("NOT_ROUTED") + r"""
    END IF;
    -- 4. the version.
    v_version := (p_command OPERATOR(pg_catalog.->>) 'expected_version')::integer;
    IF v_fax.version <> v_version THEN
        """ + _raise("VERSION_CONFLICT") + r"""
    END IF;
    -- 5. the target queue: this tenant's, active, held FOR SHARE to commit.
    IF v_action IN ('move', 'sort') THEN
        v_queue := (p_command OPERATOR(pg_catalog.->>) 'queue_id')::uuid;
        PERFORM 1 FROM {schema}.m05_fx_queues AS q
          WHERE q.queue_id = v_queue AND q.active FOR SHARE;
        IF NOT FOUND THEN
            """ + _raise("QUEUE_NOT_FOUND") + r"""
        END IF;
    END IF;
    IF v_action = 'file' THEN
        v_state := 'filed';
        v_new_queue := v_fax.queue_id;
        UPDATE {schema}.m05_fx_faxes SET state = 'filed',
               version = version OPERATOR(pg_catalog.+) 1 WHERE fax_id = v_fax_id;
    ELSIF v_action = 'move' THEN
        v_state := 'routed';
        v_new_queue := v_queue;
        UPDATE {schema}.m05_fx_faxes SET queue_id = v_queue,
               version = version OPERATOR(pg_catalog.+) 1 WHERE fax_id = v_fax_id;
    ELSE
        v_state := 'routed';
        v_new_queue := v_queue;
        UPDATE {schema}.m05_fx_sorting SET state = 'resolved', resolved_by = v_staff::text,
               resolved_at = v_now, resolved_queue_id = v_queue WHERE fax_id = v_fax_id;
        UPDATE {schema}.m05_fx_faxes SET state = 'routed', queue_id = v_queue,
               routed_by = 'staff', level = NULL,
               version = version OPERATOR(pg_catalog.+) 1 WHERE fax_id = v_fax_id;
    END IF;
    INSERT INTO {schema}.m05_fx_history (fax_id, seq, action, actor, from_queue_id, to_queue_id,
        at)
    VALUES (v_fax_id, v_seq,
            CASE v_action WHEN 'file' THEN 'filed' WHEN 'move' THEN 'moved' ELSE 'sorted' END,
            v_staff::text, v_fax.queue_id, v_new_queue, v_now);
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',
        'fax_id', v_fax_id::text, 'state', v_state, 'queue_id', v_new_queue::text,
        'version', v_fax.version OPERATOR(pg_catalog.+) 1);
END;
""")

# --- t142 staff read (STABLE) ---------------------------------------------------------------

_SUMMARY: Final = r"""pg_catalog.jsonb_build_object(
            'fax_id', f.fax_id::text, 'state', f.state, 'queue_id', f.queue_id::text,
            'reason', s.reason, 'pages', f.pages,
            'received_at', pg_catalog.to_char(pg_catalog.timezone('UTC', f.received_at),
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'version', f.version,
            'sender_last4', CASE WHEN f.sender_e164 IS NULL THEN NULL
                                 ELSE pg_catalog.right(f.sender_e164, 4) END)"""

READ_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_staff uuid;
    v_view text;
    v_fax_id uuid;
    v_queue uuid;
    v_out jsonb;
BEGIN""" + _TENANT + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'view')
          IS DISTINCT FROM 'string' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_view := p_command OPERATOR(pg_catalog.->>) 'view';
    IF v_view = 'list' THEN
        IF v_keys IS DISTINCT FROM ARRAY['attribution', 'queue_id', 'schema_version',
                                         'staff_id', 'view']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'queue_id')
              NOT IN ('string', 'null')
           OR (p_command OPERATOR(pg_catalog.->>) 'queue_id') OPERATOR(pg_catalog.!~) c_uuid THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_view IN ('get', 'history') THEN
        IF v_keys IS DISTINCT FROM ARRAY['attribution', 'fax_id', 'schema_version', 'staff_id',
                                         'view']::text[]""" + _uuid("fax_id") + r""" THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSE
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;""" + _STAFF_HEAD + r"""
    IF NOT {schema}.m05_fx_staff_check(v_staff, 'read') THEN
        """ + _raise("ACTOR_INACTIVE") + r"""
    END IF;
    IF v_view = 'list' THEN
        IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'queue_id') = 'null' THEN
            SELECT COALESCE(pg_catalog.jsonb_agg(""" + _SUMMARY + r"""
                   ORDER BY f.received_at, f.fax_id), '[]'::jsonb) INTO v_out
              FROM {schema}.m05_fx_faxes AS f
              LEFT JOIN {schema}.m05_fx_sorting AS s ON s.fax_id = f.fax_id
             WHERE f.state = 'needs_sorting';
        ELSE
            v_queue := (p_command OPERATOR(pg_catalog.->>) 'queue_id')::uuid;
            IF NOT EXISTS (SELECT 1 FROM {schema}.m05_fx_queues AS q
                            WHERE q.queue_id = v_queue) THEN
                """ + _raise("QUEUE_NOT_FOUND") + r"""
            END IF;
            SELECT COALESCE(pg_catalog.jsonb_agg(""" + _SUMMARY + r"""
                   ORDER BY f.received_at, f.fax_id), '[]'::jsonb) INTO v_out
              FROM {schema}.m05_fx_faxes AS f
              LEFT JOIN {schema}.m05_fx_sorting AS s ON s.fax_id = f.fax_id
             WHERE f.queue_id = v_queue;
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'faxes', v_out);
    END IF;
    v_fax_id := (p_command OPERATOR(pg_catalog.->>) 'fax_id')::uuid;
    IF NOT EXISTS (SELECT 1 FROM {schema}.m05_fx_faxes AS f WHERE f.fax_id = v_fax_id) THEN
        """ + _raise("FAX_NOT_FOUND") + r"""
    END IF;
    IF v_view = 'get' THEN
        SELECT """ + _SUMMARY + r""" INTO v_out
          FROM {schema}.m05_fx_faxes AS f
          LEFT JOIN {schema}.m05_fx_sorting AS s ON s.fax_id = f.fax_id
         WHERE f.fax_id = v_fax_id;
        RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'fax', v_out);
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'seq', h.seq, 'action', h.action, 'actor', h.actor,
               'from_queue_id', h.from_queue_id::text, 'to_queue_id', h.to_queue_id::text,
               'at', pg_catalog.to_char(pg_catalog.timezone('UTC', h.at),
                   'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ORDER BY h.seq), '[]'::jsonb) INTO v_out
      FROM {schema}.m05_fx_history AS h WHERE h.fax_id = v_fax_id;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'history', v_out);
END;
""")

# --- t143 lines snapshot (STABLE) -----------------------------------------------------------

SNAPSHOT_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_lines jsonb;
BEGIN""" + _TENANT + r"""
    IF v_keys IS DISTINCT FROM ARRAY['schema_version']::text[]
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(l.line_e164 ORDER BY l.line_e164), '[]'::jsonb)
      INTO v_lines FROM {schema}.m05_fx_lines AS l;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'lines', v_lines);
END;
""")

# --- unit construction ---------------------------------------------------------------------

_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    (ACTION_ID, ACTION_NAME, "volatile", ACTION_BODY, RUNTIME_ROLE),
    (READ_ID, READ_NAME, "stable", READ_BODY, RUNTIME_ROLE),
    (SNAPSHOT_ID, SNAPSHOT_NAME, "stable", SNAPSHOT_BODY, RUNTIME_ROLE),
)
_CHECK_PARAMS: Final = (("p_staff_id", "uuid"), ("p_access", "text"))

_ALL: Final[dict[str, str | UnitDefinition]] = {
    CHECK_ID: staff_units._definition(
        CHECK_NAME, _CHECK_PARAMS, "bool", "volatile", M05_OWNER_ROLE, CHECK_BODY,
        hashlib.sha256(CHECK_BODY.encode("utf-8")).hexdigest()),
    **{mid: _definition(name, vol, body, grantee) for mid, name, vol, body, grantee in _FUNCTIONS},
}

FX2_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
FX2_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    **{mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS},
    CHECK_ID: staff_units._profile(CHECK_NAME, _CHECK_PARAMS, "bool", "volatile",
                                   M05_OWNER_ROLE),
})
