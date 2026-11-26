"""Incoming fax routing FX-IN, package FX4: sender display (OD-FX10) and the queue read.
Per-tenant migration units t145..t149. SYNTHETIC ONLY (`include_fx4`, on top of FX2, target 149).

Governing: package v1r1 (Codex ALIGN for red tests 8f518e20), red packet v1 (Codex accepted
1b0d3fd), pin correction 54edcaa (new v2 functions: the unit framework never replaces a function).

- t145 `m05_fx_staff_queues(jsonb)` (runtime, STABLE): the tenant's queues for an active staff
  member with read access — queue_id, name, active, position; ordered by position, lower(name);
  inactive queues included (historical faxes keep their queue's name).
- t146 (migrator SQL): `m05_fx_senders` (opaque sender_id, exact number, saved display name),
  `m05_fx_faxes.sender_name` (the normalised name as sent), the settings-audit op set widened.
- t147 `m05_fx_sender_put(jsonb)` (EXECUTE to haloflow_support only, OD-FX3): sender_put (upsert,
  sender_id kept from the first insert) and sender_remove; the t137 authority before any lock and
  re-checked after the sender row lock; one audit row per applied op (target = sender_id).
- t148 `m05_fx_receive_v2(jsonb)` (runtime): t139 plus an optional `sender_name`, normalised in
  SQL (the authority; `fax_rules.normalise_sender_name` mirrors it) and compared on replay.
- t149 `m05_fx_staff_read_v2(jsonb)` (runtime, STABLE): t143 plus `sender_display` on every fax
  summary — the saved name for the fax's exact sender number, else the name as sent, else none.

Sender-name normalisation (Codex 8f518e20): NFKC -> whitespace to ' ' -> drop Cc/Cf and '<' '>'
-> collapse spaces, trim -> every maximal run of Unicode digits (Nd) joined by separators (Zs,
Pd, U+2212, . ( ) + /) that starts and ends with a digit and holds >= 5 digits becomes '•••' +
its last four digits -> cut to 40 -> trim; empty is NULL. The character classes below are
generated from the same `unicodedata` tables the Python mirror uses.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from types import MappingProxyType
from typing import Final

from haloflow.m01.provisioning.roles import RUNTIME_ROLE
from haloflow.m01.provisioning.units import UnitDefinition, UnitDefinitions
from haloflow.m05.fax_rules import MASK, RUN_DIGITS, SAVED_NAME_MAX, SENDER_NAME_MAX
from haloflow.m05.fax_rules import SENDER_NAME_RAW_MAX as RAW_MAX
from haloflow.m05.fax_staff_units import _STAFF_HEAD, READ_BODY
from haloflow.m05.fax_units import (
    _AUTHORIZE,
    _E164,
    BINDING,
    RECEIVE_BODY,
    _raise,
)
from haloflow.m05.intake_units import _DECLARE_COMMON, _TENANT
from haloflow.m05.reminder_units import SUPPORT_ROLE, _definition, _profile

QUEUES_ID: Final = "t145_m05_fx_staff_queues"
SENDERS_ID: Final = "t146_m05_fx_senders"
SENDER_PUT_ID: Final = "t147_m05_fx_sender_put"
RECEIVE_V2_ID: Final = "t148_m05_fx_receive_v2"
READ_V2_ID: Final = "t149_m05_fx_staff_read_v2"
FX4_TARGET: Final = 149

QUEUES_NAME: Final = "m05_fx_staff_queues"
SENDER_PUT_NAME: Final = "m05_fx_sender_put"
RECEIVE_V2_NAME: Final = "m05_fx_receive_v2"
READ_V2_NAME: Final = "m05_fx_staff_read_v2"

# --- generated character classes ------------------------------------------------------------


def _codepoints(predicate: Callable[[str], bool]) -> list[int]:
    return [cp for cp in range(1, 0x110000) if predicate(chr(cp))]


def _ranges(points: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for cp in points:
        if out and cp == out[-1][1] + 1:
            out[-1] = (out[-1][0], cp)
        else:
            out.append((cp, cp))
    return out


def _esc(cp: int) -> str:
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}"


def _class(points: list[int]) -> str:
    parts = [(_esc(a) if a == b else f"{_esc(a)}-{_esc(b)}") for a, b in _ranges(points)]
    return "[" + "".join(parts) + "]"


_WHITESPACE: Final = _codepoints(lambda c: c.isspace())
_DROP: Final = sorted(set(_codepoints(lambda c: unicodedata.category(c) in ("Cc", "Cf")))
                      | {ord("<"), ord(">")})
_DIGIT_ZEROS: Final = sorted({cp - int(unicodedata.decimal(chr(cp)))
                              for cp in _codepoints(lambda c: unicodedata.category(c) == "Nd")})
_SEPARATORS: Final = sorted(set(_codepoints(lambda c: unicodedata.category(c) in ("Zs", "Pd")))
                            | {ord(c) for c in ".()+/−"})
WS_CLASS: Final = _class(_WHITESPACE)
DROP_CLASS: Final = _class(_DROP)
_ZEROS_SQL: Final = "ARRAY[" + ", ".join(str(z) for z in _DIGIT_ZEROS) + "]::integer[]"
_SEPS_SQL: Final = "ARRAY[" + ", ".join(str(s) for s in _SEPARATORS) + "]::integer[]"


def _clean_sql(source: str, target: str) -> str:
    """target := cleaned text of source (NFKC, whitespace, drop, collapse, trim)."""
    return (f"""
        {target} := pg_catalog.normalize({source}, 'NFKC');
        {target} := pg_catalog.regexp_replace({target}, '{WS_CLASS}', ' ', 'g');
        {target} := pg_catalog.regexp_replace({target}, '{DROP_CLASS}', '', 'g');
        {target} := pg_catalog.btrim(pg_catalog.regexp_replace({target}, ' +', ' ', 'g'));""")


# One block: n_text (cleaned) -> n_out (masked) and n_max_run (longest run, in digits).
_MASK_BLOCK: Final = r"""
        n_out := '';
        n_max_run := 0;
        n_len := pg_catalog.char_length(n_text);
        n_i := 1;
        WHILE n_i <= n_len LOOP
            n_cp := pg_catalog.ascii(pg_catalog.substr(n_text, n_i, 1));
            n_zero := (SELECT z FROM pg_catalog.unnest(@@ZEROS@@) AS z
                        WHERE n_cp >= z AND n_cp < z + 10 LIMIT 1);
            IF n_zero IS NULL THEN
                n_out := n_out OPERATOR(pg_catalog.||) pg_catalog.substr(n_text, n_i, 1);
                n_i := n_i + 1;
                CONTINUE;
            END IF;
            n_digits := '';
            n_last := n_i;
            n_j := n_i;
            WHILE n_j <= n_len LOOP
                n_cp := pg_catalog.ascii(pg_catalog.substr(n_text, n_j, 1));
                n_zero := (SELECT z FROM pg_catalog.unnest(@@ZEROS@@) AS z
                            WHERE n_cp >= z AND n_cp < z + 10 LIMIT 1);
                IF n_zero IS NOT NULL THEN
                    n_digits := n_digits OPERATOR(pg_catalog.||) (n_cp - n_zero)::text;
                    n_last := n_j;
                ELSIF NOT (n_cp = ANY (@@SEPS@@)) THEN
                    EXIT;
                END IF;
                n_j := n_j + 1;
            END LOOP;
            IF pg_catalog.char_length(n_digits) > n_max_run THEN
                n_max_run := pg_catalog.char_length(n_digits);
            END IF;
            IF pg_catalog.char_length(n_digits) >= @@RUN@@ THEN
                n_out := n_out OPERATOR(pg_catalog.||) '@@MASK@@'
                         OPERATOR(pg_catalog.||) pg_catalog.right(n_digits, 4);
            ELSE
                n_out := n_out OPERATOR(pg_catalog.||)
                         pg_catalog.substr(n_text, n_i, n_last - n_i + 1);
            END IF;
            n_i := n_last + 1;
        END LOOP;
""".replace("@@ZEROS@@", _ZEROS_SQL).replace("@@SEPS@@", _SEPS_SQL).replace(
    "@@RUN@@", str(RUN_DIGITS)).replace("@@MASK@@", MASK)

_NORMALISE_DECLARE: Final = r"""
    n_text text;
    n_out text;
    n_len integer;
    n_i integer;
    n_j integer;
    n_last integer;
    n_cp integer;
    n_zero integer;
    n_digits text;
    n_max_run integer;"""


# --- t145 queue read (runtime) ---------------------------------------------------------------

QUEUES_BODY: Final = (
    "\nDECLARE" + _DECLARE_COMMON + r"""
    v_staff uuid;
    v_out jsonb;
BEGIN""" + _TENANT + BINDING + r"""
    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'schema_version', 'staff_id']::text[] THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;""" + _STAFF_HEAD + r"""
    IF NOT {schema}.m05_fx_staff_check(v_staff, 'read') THEN
        """ + _raise("ACTOR_INACTIVE") + r"""
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
               'queue_id', q.queue_id::text, 'name', q.name, 'active', q.active,
               'position', q.position) ORDER BY q.position, pg_catalog.lower(q.name)),
           '[]'::jsonb) INTO v_out
      FROM {schema}.m05_fx_queues AS q;
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'queues', v_out);
END;
""")

# --- t146 senders + sender_name (migrator) ----------------------------------------------------

T146_SQL: Final = r"""
-- t146:step:tables
CREATE TABLE {schema}.m05_fx_senders (
    sender_id uuid PRIMARY KEY,
    sender_e164 varchar(16) NOT NULL UNIQUE CHECK (sender_e164 OPERATOR(pg_catalog.~) @@E164@@),
    display_name varchar(64) NOT NULL
        CHECK (pg_catalog.char_length(display_name) BETWEEN 1 AND @@SAVED@@),
    updated_at timestamptz NOT NULL
);
ALTER TABLE {schema}.m05_fx_faxes ADD COLUMN sender_name varchar(40)
    CHECK (sender_name IS NULL OR pg_catalog.char_length(sender_name) BETWEEN 1 AND @@SENT@@);
ALTER TABLE {schema}.m05_fx_settings_audit DROP CONSTRAINT m05_fx_settings_audit_op_check;
ALTER TABLE {schema}.m05_fx_settings_audit ADD CONSTRAINT m05_fx_settings_audit_op_check
    CHECK (op IN ('queue_put', 'queue_rename', 'queue_deactivate', 'rule_put', 'rule_remove',
                  'sender_put', 'sender_remove'));

-- t146:step:runtime_privileges
REVOKE ALL ON {schema}.m05_fx_senders FROM PUBLIC, haloflow_runtime, haloflow_support;

-- t146:step:grants
GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.m05_fx_senders TO haloflow_m05_owner;
""".replace("@@E164@@", _E164).replace("@@SAVED@@", str(SAVED_NAME_MAX)).replace(
    "@@SENT@@", str(SENDER_NAME_MAX))

# --- t147 sender_put (support only) -----------------------------------------------------------

SENDER_PUT_BODY: Final = (r"""
DECLARE
    c_phone constant text := @@E164@@;
    v_setting text;
    v_keys text[];
    v_op text;
    v_auth jsonb;
    v_sender text;
    v_name text;
    v_id uuid;
    v_found boolean;""" + _NORMALISE_DECLARE + r"""
BEGIN""" + _TENANT + r"""
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'schema_version')
          IS DISTINCT FROM 'number'
       OR (p_command OPERATOR(pg_catalog.->) 'schema_version')::text <> '1'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'op')
          IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'sender')
          IS DISTINCT FROM 'string' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_op := p_command OPERATOR(pg_catalog.->>) 'op';
    IF v_op = 'sender_put' THEN
        IF v_keys IS DISTINCT FROM ARRAY['name', 'op', 'schema_version', 'sender']::text[]
           OR pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'name')
              IS DISTINCT FROM 'string' THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSIF v_op = 'sender_remove' THEN
        IF v_keys IS DISTINCT FROM ARRAY['op', 'schema_version', 'sender']::text[] THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;
    ELSE
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_sender := p_command OPERATOR(pg_catalog.->>) 'sender';
    IF v_sender OPERATOR(pg_catalog.!~) c_phone THEN
        """ + _raise("SENDER_INVALID") + r"""
    END IF;
    IF v_op = 'sender_put' THEN
        v_name := p_command OPERATOR(pg_catalog.->>) 'name';
        IF pg_catalog.char_length(v_name) > @@RAW@@ THEN
            """ + _raise("SENDER_NAME_INVALID") + r"""
        END IF;
        -- the same detector as the sent-name masking (Codex v1 review 3): no number run at all
        v_name := pg_catalog.normalize(v_name, 'NFKC');
        v_name := pg_catalog.btrim(pg_catalog.regexp_replace(
            pg_catalog.regexp_replace(v_name, '@@WS@@', ' ', 'g'), ' +', ' ', 'g'));
        IF pg_catalog.char_length(v_name) NOT BETWEEN 1 AND @@SAVED@@
           OR v_name OPERATOR(pg_catalog.~) '@@DROP@@' THEN
            """ + _raise("SENDER_NAME_INVALID") + r"""
        END IF;
        n_text := v_name;""" + _MASK_BLOCK + r"""
        IF n_max_run >= @@RUN@@ THEN
            """ + _raise("SENDER_NAME_INVALID") + r"""
        END IF;
    END IF;
    -- OD-FX3: support authority before any lock or write ...""" + _AUTHORIZE + r"""
    SELECT s.sender_id INTO v_id FROM {schema}.m05_fx_senders AS s
     WHERE s.sender_e164 = v_sender FOR UPDATE;
    v_found := FOUND;
    -- ... and again after the sender row lock (a grant revoked while waiting refuses).""" + (
        _AUTHORIZE) + r"""
    IF v_op = 'sender_put' THEN
        INSERT INTO {schema}.m05_fx_senders (sender_id, sender_e164, display_name, updated_at)
        VALUES (pg_catalog.gen_random_uuid(), v_sender, v_name, pg_catalog.clock_timestamp())
        ON CONFLICT (sender_e164) DO UPDATE
           SET display_name = EXCLUDED.display_name, updated_at = EXCLUDED.updated_at
        RETURNING sender_id INTO v_id;
    ELSE
        IF NOT v_found THEN
            """ + _raise("SENDER_NOT_FOUND") + r"""
        END IF;
        DELETE FROM {schema}.m05_fx_senders WHERE sender_id = v_id;
    END IF;
    INSERT INTO {schema}.m05_fx_settings_audit (audit_id, actor_id, grant_id, op, target_id, at)
    VALUES (pg_catalog.gen_random_uuid(), v_auth OPERATOR(pg_catalog.->>) 'actor_id',
            (v_auth OPERATOR(pg_catalog.->>) 'grant_id')::uuid, v_op, v_id,
            pg_catalog.clock_timestamp());
    RETURN pg_catalog.jsonb_build_object('schema_version', 1, 'outcome', 'applied',
        'sender_id', v_id::text);
END;
""").replace("@@E164@@", _E164).replace("@@RAW@@", str(RAW_MAX)).replace(
    "@@SAVED@@", str(SAVED_NAME_MAX)).replace("@@WS@@", WS_CLASS).replace(
    "@@DROP@@", DROP_CLASS).replace("@@RUN@@", str(RUN_DIGITS))

# --- t148 receive v2 (runtime) ----------------------------------------------------------------


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"FX4 receive v2 anchor not unique: {old[:40]!r}")
    return text.replace(old, new)


_V1_KEYS_CHECK: Final = (
    r"""    IF v_keys IS DISTINCT FROM ARRAY['attribution', 'doc_ref', 'line', """
    r"""'pages', 'provider',""")
_V2_KEYS_CHECK: Final = (
    r"""    IF pg_catalog.array_remove(v_keys, 'sender_name')"""
    r""" IS DISTINCT FROM ARRAY['attribution', 'doc_ref', 'line', 'pages', 'provider',""")

_SENDER_NAME_BLOCK: Final = r"""
    -- FX4 (OD-FX10): the optional name as sent, normalised here (untrusted text, never logged).
    IF pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'sender_name') = 'string' THEN
        IF pg_catalog.char_length(p_command OPERATOR(pg_catalog.->>) 'sender_name') > @@RAW@@ THEN
            """ + _raise("COMMAND_MALFORMED") + r"""
        END IF;""" + _clean_sql("p_command OPERATOR(pg_catalog.->>) 'sender_name'", "n_text") + (
    _MASK_BLOCK) + r"""
        v_sname := NULLIF(pg_catalog.btrim(pg_catalog.left(n_out, @@SENT@@)), '');
    ELSIF (p_command OPERATOR(pg_catalog.?) 'sender_name')
          AND pg_catalog.jsonb_typeof(p_command OPERATOR(pg_catalog.->) 'sender_name')
              <> 'null' THEN
        """ + _raise("COMMAND_MALFORMED") + r"""
    END IF;
    v_digest := """

_RECEIVE_V2_BASE: Final = _replace_once(_replace_once(_replace_once(_replace_once(_replace_once(
    _replace_once(RECEIVE_BODY,
                  "    v_now timestamptz;\nBEGIN",
                  "    v_now timestamptz;\n    v_sname text;" + _NORMALISE_DECLARE + "\nBEGIN"),
    _V1_KEYS_CHECK, _V2_KEYS_CHECK),
    "\n    v_digest := ", _SENDER_NAME_BLOCK.replace("@@RAW@@", str(RAW_MAX)).replace(
        "@@SENT@@", str(SENDER_NAME_MAX))),
    "pages, received_at, doc_ref, meta_digest, state, queue_id, routed_by, level, version,\n"
    "        created_at)",
    "pages, received_at, doc_ref, meta_digest, state, queue_id, routed_by, level, version,\n"
    "        created_at, sender_name)"),
    "v_digest, 'needs_sorting', NULL, NULL, NULL, 1, v_now)",
    "v_digest, 'needs_sorting', NULL, NULL, NULL, 1, v_now, v_sname)"),
    "        IF v_existing.meta_digest = v_digest THEN",
    "        IF v_existing.meta_digest = v_digest\n"
    "           AND v_existing.sender_name IS NOT DISTINCT FROM v_sname THEN")
RECEIVE_V2_BODY: Final = _replace_once(
    _RECEIVE_V2_BASE,
    "        IF v_existing.doc_ref IS DISTINCT FROM v_doc THEN",
    "        IF v_existing.sender_name IS DISTINCT FROM v_sname THEN\n"
    "            v_fields := v_fields OPERATOR(pg_catalog.||) 'sender_name'::text;\n"
    "        END IF;\n"
    "        IF v_existing.doc_ref IS DISTINCT FROM v_doc THEN")

# --- t149 staff read v2 (runtime) -------------------------------------------------------------

_SORTING_JOIN: Final = "LEFT JOIN {schema}.m05_fx_sorting AS s ON s.fax_id = f.fax_id"
_SENDERS_JOIN: Final = (_SORTING_JOIN
                        + "\n              LEFT JOIN {schema}.m05_fx_senders AS sn"
                          " ON sn.sender_e164 = f.sender_e164")
_SUMMARY_TAIL: Final = "ELSE pg_catalog.right(f.sender_e164, 4) END)"
_SUMMARY_TAIL_V2: Final = (
    "ELSE pg_catalog.right(f.sender_e164, 4) END,\n"
    "            'sender_display', pg_catalog.jsonb_build_object(\n"
    "                'text', COALESCE(sn.display_name, f.sender_name),\n"
    "                'source', CASE WHEN sn.display_name IS NOT NULL THEN 'saved'\n"
    "                               WHEN f.sender_name IS NOT NULL THEN 'as_sent'\n"
    "                               ELSE 'none' END))")
if READ_BODY.count(_SORTING_JOIN) != 3 or READ_BODY.count(_SUMMARY_TAIL) != 3:
    raise RuntimeError("FX4 read v2 anchors changed in t143")
READ_V2_BODY: Final = READ_BODY.replace(_SORTING_JOIN, _SENDERS_JOIN).replace(
    _SUMMARY_TAIL, _SUMMARY_TAIL_V2)

# --- unit construction ------------------------------------------------------------------------

_FUNCTIONS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    (QUEUES_ID, QUEUES_NAME, "stable", QUEUES_BODY, RUNTIME_ROLE),
    (SENDER_PUT_ID, SENDER_PUT_NAME, "volatile", SENDER_PUT_BODY, SUPPORT_ROLE),
    (RECEIVE_V2_ID, RECEIVE_V2_NAME, "volatile", RECEIVE_V2_BODY, RUNTIME_ROLE),
    (READ_V2_ID, READ_V2_NAME, "stable", READ_V2_BODY, RUNTIME_ROLE),
)

_ALL: Final[dict[str, str | UnitDefinition]] = {
    SENDERS_ID: T146_SQL,
    **{mid: _definition(name, vol, body, grantee) for mid, name, vol, body, grantee in _FUNCTIONS},
}

FX4_TENANT_MIGRATIONS: Final[UnitDefinitions] = MappingProxyType(dict(sorted(_ALL.items())))
FX4_INSTALLED_STATE_PROFILES: Final = MappingProxyType({
    mid: _profile(name, vol, grantee) for mid, name, vol, _body, grantee in _FUNCTIONS})
