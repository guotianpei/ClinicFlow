"""The owned-state view of a synthetic install (Option B B2 S1/S2). SYNTHETIC ONLY.

`capture()` reads, in ONE REPEATABLE READ READ ONLY transaction on the target database (the
shared catalogs are visible there), under a session-level `pg_advisory_lock_shared(K_X)` taken
BEFORE `BEGIN` (so the snapshot follows the lock and no guarded writer, which needs exclusive
K_X, can commit concurrently):
- the reservation slot (compared by the caller with its own epoch; never part of the view);
- the owned shared state: owned roles (attributes, verifier digest), their membership edges and
  settings, the target database row (owner, ACL, connection settings, comment);
- the target catalogs (schemas, relations, columns, constraints, indexes, triggers, policies,
  functions, types, enum labels, sequences, default ACLs, extensions, event triggers,
  publications, comments by identity);
- every user table's rows.

From it, three things are derived:
- `profile_digest`: sha256 of the view with ONLY intent-derived identities normalised (nonce,
  schema key, tenant id, n8 login names, uuid5 ids, decision ref, intent window, intent
  verifiers), the measured nondeterministic columns and the ledger `attempt` removed, and rows
  sorted AFTER normalisation. Comparable across installs (the pinned installed-state profile).
- `nondet`: the exact values of the removed nondeterministic columns, keyed by each row's
  normalised deterministic remainder (same-install seal).
- `identities`: the raw (class, oid, identity) of every owned object (same-install seal: a
  same-name recreation changes the OID).
- `ledger`: (migration_id, state, attempt, sanitized_error_code) of every ledger row.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from psycopg import sql

from haloflow.synthetic.attest import admin_connect
from haloflow.synthetic.dsn import AdminEndpoint
from haloflow.synthetic.guard import K_X, RES_SQL

# Measured nondeterministic columns (two independent installs + interrupted/retried paths;
# evidence claude_b2-impl/determinism-measurement-r1.json, retry-measurement.json). `{tenant}`
# is the install's tenant schema.
NONDETERMINISTIC: Final = frozenset({
    ("shared.schema_migrations", "started_at"),
    ("shared.schema_migrations", "completed_at"),
    ("shared.tenant_state_history", "occurred_at"),
    ("shared.tenants", "created_at"),
    ("shared.tenants", "updated_at"),
    ("{tenant}.m05_reminder_settings", "updated_at"),
    ("{tenant}.m05_activation_records", "created_at"),
    ("{tenant}.m05_activation_records", "record_id"),
    ("{tenant}.m01_staff", "status_changed_at"),
    ("{tenant}.m01_staff_roles", "granted_at"),
    ("{tenant}.m01_staff_followup_access", "granted_at"),
    ("{tenant}.m01_staff_bootstrap_keys", "created_at"),
    ("{tenant}.m01_staff_bootstrap_keys", "digest"),
    ("{tenant}.m01_staff_bootstrap_log", "at"),
    ("{tenant}.m01_staff_bootstrap_log", "log_id"),
    ("{tenant}.m01_staff_action_keys", "created_at"),
    ("{tenant}.m01_staff_action_keys", "digest"),
    ("{tenant}.m01_staff_admin_log", "created_at"),
    ("{tenant}.m01_staff_admin_log", "log_id"),
    ("{tenant}.m01_staff_admin_log", "digest"),
})
# A clock nested in JSON snapshot columns: only that key is removed (and sealed).
NESTED_CLOCK: Final = {"{tenant}.m01_staff_admin_log": (("before", "after", "result"),
                                                        "status_changed_at")}
LEDGER: Final = "shared.schema_migrations"
# Row columns holding an owned role's OID.
ROLE_OID_COLUMNS: Final = {"shared.support_operator_logins": "login_oid"}
_SYS_SCHEMAS: Final = ("pg_catalog", "information_schema", "pg_toast")
_NS: Final = ("n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
              " AND n.nspname NOT LIKE 'pg\\_temp\\_%%'"
              " AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%%'")

_SHARED: Final = {
    "roles": "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
             " rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil::text,"
             " pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
             "coalesce(rolpassword, ''), 'UTF8')), 'hex')"
             " FROM pg_catalog.pg_authid WHERE rolname = ANY(%(roles)s)",
    "memberships": "SELECT r.rolname, m.rolname, g.rolname, a.admin_option, a.inherit_option,"
                   " a.set_option FROM pg_catalog.pg_auth_members a"
                   " JOIN pg_catalog.pg_authid r ON r.oid = a.roleid"
                   " JOIN pg_catalog.pg_authid m ON m.oid = a.member"
                   " JOIN pg_catalog.pg_authid g ON g.oid = a.grantor"
                   " WHERE r.rolname = ANY(%(roles)s) OR m.rolname = ANY(%(roles)s)",
    "role_settings": "SELECT r.rolname, coalesce(d.datname, ''), s.setconfig::text"
                     " FROM pg_catalog.pg_db_role_setting s"
                     " JOIN pg_catalog.pg_authid r ON r.oid = s.setrole"
                     " LEFT JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase"
                     " WHERE r.rolname = ANY(%(roles)s)",
    "database": "SELECT datname, pg_catalog.pg_get_userbyid(datdba), coalesce(datacl::text, ''),"
                " datallowconn, datconnlimit, datistemplate,"
                " coalesce(pg_catalog.shobj_description(oid, 'pg_database'), '')"
                " FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()",
}
_CATALOG: Final = {
    "schemas": "SELECT n.nspname, pg_catalog.pg_get_userbyid(n.nspowner),"
               " coalesce(n.nspacl::text, '') FROM pg_catalog.pg_namespace n WHERE " + _NS,
    "relations": "SELECT n.nspname, c.relname, c.relkind, pg_catalog.pg_get_userbyid(c.relowner),"
                 " coalesce(c.relacl::text, ''), c.relrowsecurity, c.relforcerowsecurity,"
                 " coalesce(c.reloptions::text, ''), c.relpersistence"
                 " FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n"
                 " ON n.oid = c.relnamespace WHERE " + _NS,
    "columns": "SELECT n.nspname, c.relname, a.attnum, a.attname,"
               " pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,"
               " coalesce(pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''), a.attidentity,"
               " a.attgenerated, coalesce(a.attacl::text, ''), a.attisdropped"
               " FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid"
               " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
               " LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid"
               " AND d.adnum = a.attnum WHERE a.attnum > 0 AND " + _NS,
    "constraints": "SELECT n.nspname, coalesce(c.relname, ''), k.conname, k.contype,"
                   " pg_catalog.pg_get_constraintdef(k.oid, true)"
                   " FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_namespace n"
                   " ON n.oid = k.connamespace LEFT JOIN pg_catalog.pg_class c"
                   " ON c.oid = k.conrelid WHERE " + _NS,
    "indexes": "SELECT n.nspname, c.relname, pg_catalog.pg_get_indexdef(i.indexrelid)"
               " FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid"
               " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE " + _NS,
    "triggers": "SELECT n.nspname, c.relname, t.tgname, t.tgenabled,"
                " pg_catalog.pg_get_triggerdef(t.oid, true)"
                " FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid"
                " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                " WHERE NOT t.tgisinternal AND " + _NS,
    "policies": "SELECT n.nspname, c.relname, p.polname, p.polcmd, p.polpermissive,"
                " (SELECT pg_catalog.array_agg(r ORDER BY r) FROM pg_catalog.unnest("
                "ARRAY(SELECT CASE WHEN x = 0 THEN 'public' ELSE"
                " pg_catalog.pg_get_userbyid(x) END FROM pg_catalog.unnest(p.polroles) x)) r)"
                "::text,"
                " coalesce(pg_catalog.pg_get_expr(p.polqual, p.polrelid), ''),"
                " coalesce(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), '')"
                " FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid = p.polrelid"
                " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE " + _NS,
    "functions": "SELECT n.nspname, p.proname,"
                 " pg_catalog.pg_get_function_identity_arguments(p.oid),"
                 " pg_catalog.pg_get_function_result(p.oid), p.prokind, l.lanname,"
                 " pg_catalog.md5(pg_catalog.replace(coalesce(p.prosrc, ''), %(schema)s,"
                 " '<SCHEMA>')), p.prosecdef, p.provolatile,"
                 " p.proleakproof, p.proisstrict, coalesce(p.proconfig::text, ''),"
                 " coalesce(p.proacl::text, ''), pg_catalog.pg_get_userbyid(p.proowner)"
                 " FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n"
                 " ON n.oid = p.pronamespace JOIN pg_catalog.pg_language l"
                 " ON l.oid = p.prolang WHERE " + _NS,
    "types": "SELECT n.nspname, t.typname, t.typtype, pg_catalog.pg_get_userbyid(t.typowner),"
             " coalesce(t.typacl::text, ''), coalesce((SELECT pg_catalog.string_agg(e.enumlabel,"
             " ',' ORDER BY e.enumsortorder) FROM pg_catalog.pg_enum e"
             " WHERE e.enumtypid = t.oid), '')"
             " FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n"
             " ON n.oid = t.typnamespace WHERE t.typrelid = 0 AND " + _NS,
    "sequences": "SELECT schemaname, sequencename, coalesce(last_value::text, ''),"
                 " increment_by, cycle FROM pg_catalog.pg_sequences"
                 " WHERE schemaname NOT IN ('pg_catalog', 'information_schema')",
    "default_acls": "SELECT pg_catalog.pg_get_userbyid(d.defaclrole),"
                    " coalesce(n.nspname, ''), d.defaclobjtype, d.defaclacl::text"
                    " FROM pg_catalog.pg_default_acl d LEFT JOIN pg_catalog.pg_namespace n"
                    " ON n.oid = d.defaclnamespace",
    "extensions": "SELECT extname, extversion FROM pg_catalog.pg_extension",
    "event_triggers": "SELECT evtname, evtevent, evtenabled FROM pg_catalog.pg_event_trigger",
    "publications": "SELECT pubname FROM pg_catalog.pg_publication",
    "comments": "SELECT pg_catalog.pg_describe_object(classoid, objoid, objsubid), description"
                " FROM pg_catalog.pg_description WHERE objoid >= 16384",
}
# Raw identities (same-install only): class, oid, identity.
_IDENTITIES: Final = (
    "SELECT 'pg_authid'::text, oid::text, rolname::text FROM pg_catalog.pg_authid"
    " WHERE rolname = ANY(%(roles)s)"
    " UNION ALL SELECT 'pg_namespace', n.oid::text, n.nspname FROM pg_catalog.pg_namespace n"
    " WHERE " + _NS +
    " UNION ALL SELECT 'pg_class', c.oid::text, n.nspname || '.' || c.relname"
    " FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
    " WHERE " + _NS +
    " UNION ALL SELECT 'pg_proc', p.oid::text, p.oid::regprocedure::text"
    " FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace"
    " WHERE " + _NS +
    " UNION ALL SELECT 'pg_type', t.oid::text, n.nspname || '.' || t.typname"
    " FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace"
    " WHERE " + _NS +
    " UNION ALL SELECT 'pg_constraint', k.oid::text, n.nspname || '.' || k.conname || '@'"
    " || coalesce(k.conrelid::regclass::text, '')"
    " FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_namespace n ON n.oid = k.connamespace"
    " WHERE " + _NS +
    " UNION ALL SELECT 'pg_trigger', t.oid::text, t.tgname || '@' || t.tgrelid::regclass::text"
    " FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid"
    " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
    " WHERE NOT t.tgisinternal AND " + _NS +
    " UNION ALL SELECT 'pg_policy', p.oid::text, p.polname || '@' || p.polrelid::regclass::text"
    " FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid = p.polrelid"
    " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE " + _NS +
    " UNION ALL SELECT 'pg_description', d.classoid::regclass::text || ':' || d.objoid::text"
    " || ':' || d.objsubid::text, d.description FROM pg_catalog.pg_description d"
    " WHERE d.objoid >= 16384"
)


@dataclass(frozen=True)
class Capture:
    reservation: str | None
    profile_digest: str
    nondet: dict[str, dict[str, Any]]
    identities: list[list[str]]
    ledger: list[list[Any]]
    rows: dict[str, list[dict[str, Any]]]   # raw rows (for the new-row checks)
    normalised: dict[str, Any]              # the digested view (diagnostics; no secrets)


def owned_roles(state: Mapping[str, Any], group_roles: frozenset[str]) -> list[str]:
    return sorted(group_roles | {v["role"] for v in state["logins"].values()})


def _substitutions(state: Mapping[str, Any],
                   verifiers: Mapping[str, str]) -> list[tuple[str, str]]:
    nonce = str(state["nonce"])
    base = uuid.UUID(nonce)
    subs: list[tuple[str, str]] = [(nonce, "<NONCE>"), (str(state["schema_key"]), "<SCHEMA>"),
                                   (str(state["tenant_id"]), "<TENANT>")]
    for name in ("staff", "activation", "staff-first-admin", "staff-access-read",
                 "staff-access-act", "staff-execution", "provision", "support-grant"):
        subs.append((str(uuid.uuid5(base, name)), f"<U:{name}>"))
    sup = state["support"]
    subs += [(str(sup["decision_ref"]), "<DREF>"), (str(sup["starts_at"]), "<STARTS>"),
             (str(sup["expires_at"]), "<EXPIRES>"), (str(state["ticket_reference"]), "<TICKET>"),
             (str(sup["actor_id"]), "<SUPPORT_ACTOR>")]
    subs += [(digest, f"<VERIFIER:{suffix}>") for suffix, digest in verifiers.items()]
    subs.append((str(state["target"]), "<TARGETDB>"))
    subs.append((nonce[:8], "<N8>"))
    return subs


def _norm(value: str, subs: list[tuple[str, str]], boot: re.Pattern[str] | None = None) -> str:
    for old, new in subs:
        value = value.replace(old, new)
    if boot is not None:                       # the bootstrap superuser's name, as a whole word
        value = boot.sub("<BOOT>", value)
    return value


def _strip_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _strip_key(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_strip_key(v, key) for v in value]
    return value


def _nested(value: Any, key: str) -> Any:
    """The nested clock values (in document order) removed by `_strip_key`."""
    found: list[Any] = []
    if isinstance(value, dict):
        for k, v in sorted(value.items()):
            if k == key:
                found.append(v)
            else:
                found.extend(_nested(v, key))
    elif isinstance(value, list):
        for v in value:
            found.extend(_nested(v, key))
    return found


def identity_names(identities: list[list[str]], state: Mapping[str, Any]) -> dict[str, str]:
    """Normalised name of each raw identity, keyed "class|oid": the object's identity with the
    intent identities substituted; a comment is named by its owning object and sub-id."""
    subs = _substitutions(state, {})
    by_oid = {i[1]: _norm(i[2], subs) for i in identities if i[0] != "pg_description"}
    names: dict[str, str] = {}
    for cls, oid, name in identities:
        if cls == "pg_description":
            _class, objoid, subid = oid.split(":")
            names[f"{cls}|{oid}"] = f"comment:{by_oid.get(objoid, '?')}:{subid}"
        else:
            names[f"{cls}|{oid}"] = f"{cls}:{_norm(name, subs)}"
    return names


@contextmanager
def capture_lock(endpoint: AdminEndpoint) -> Iterator[psycopg.Connection[Any]]:
    """A target-database admin session holding `pg_advisory_lock_shared(K_X)` (session level)
    until the caller leaves the block: hold it through any local persistence that must not
    interleave with a takeover (which needs exclusive K_X)."""

    conn = admin_connect(endpoint, endpoint.target)
    try:
        conn.execute("SELECT pg_catalog.pg_advisory_lock_shared(%s)", (K_X,))
        try:
            yield conn
        finally:
            conn.execute("SELECT pg_catalog.pg_advisory_unlock_shared(%s)", (K_X,))
    finally:
        conn.close()


def capture(conn: psycopg.Connection[Any], state: Mapping[str, Any],
            group_roles: frozenset[str], verifiers: Mapping[str, str]) -> Capture:
    """One REPEATABLE READ READ ONLY snapshot (the caller holds `capture_lock`)."""

    roles = owned_roles(state, group_roles)
    schema = str(state["schema_key"])
    subs = _substitutions(state, verifiers)
    boot_name = conn.execute("SELECT rolname FROM pg_catalog.pg_authid WHERE oid = 10").fetchone()
    boot = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(str(boot_name[0])) + r"(?![A-Za-z0-9_$])") \
        if boot_name is not None else None
    view: dict[str, Any] = {}
    nondet: dict[str, dict[str, Any]] = {}
    raw_rows: dict[str, list[dict[str, Any]]] = {}
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL TimeZone = 'UTC'")
        row = conn.execute(RES_SQL).fetchone()
        reservation = None if row is None else row[0]
        params: dict[str, Any] = {"roles": roles, "schema": schema}
        for name, query in {**_SHARED, **_CATALOG}.items():
            view[name] = [[str(v) for v in r] for r in conn.execute(query, params).fetchall()]
        identities = sorted([str(v) for v in r] for r in conn.execute(_IDENTITIES, params))
        owned_oids = {str(r[0]): str(r[1]) for r in conn.execute(
            "SELECT oid, rolname FROM pg_catalog.pg_authid WHERE rolname = ANY(%(roles)s)",
            params).fetchall()}
        tables = conn.execute(
            "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace"
            " n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND " + _NS +
            " ORDER BY 1, 2", params).fetchall()
        for nsp, rel in tables:
            table = f"{nsp}.{rel}"
            generic = table.replace(schema, "{tenant}", 1) if nsp == schema else table
            cols = {c for (t, c) in NONDETERMINISTIC if t == generic}
            if table == LEDGER:
                cols = cols | {"attempt"}
            nested = NESTED_CLOCK.get(generic)
            q = sql.SQL("SELECT pg_catalog.to_jsonb(x)::text FROM {}.{} x").format(
                sql.Identifier(nsp), sql.Identifier(rel))
            det_rows: list[str] = []
            raws: list[dict[str, Any]] = []
            for (text,) in conn.execute(q).fetchall():
                record = json.loads(text)
                raws.append(dict(record))
                removed = {c: record.pop(c) for c in sorted(cols) if c in record}
                if table in ROLE_OID_COLUMNS:
                    # A row naming an owned role by OID: compared by the role it names
                    # (cross-install); the raw OID is sealed below (same-install).
                    column = ROLE_OID_COLUMNS[table]
                    raw_oid = str(record.get(column))
                    removed[column] = raw_oid
                    record[column] = f"<OID:{owned_oids[raw_oid]}>" if raw_oid in owned_oids \
                        else raw_oid
                if nested is not None:
                    columns, key = nested
                    removed["__nested__"] = [_nested(record.get(c), key) for c in columns]
                    for c in columns:
                        record[c] = _strip_key(record.get(c), key)
                det = _norm(json.dumps(record, sort_keys=True), subs, boot)
                det_rows.append(det)
                if removed:
                    norm_table = _norm(table, subs, boot)
                    if det in nondet.setdefault(norm_table, {}):
                        nondet[norm_table][det + "#dup"] = removed   # duplicates stay visible
                    else:
                        nondet[norm_table][det] = removed
            view[f"rows:{_norm(table, subs, boot)}"] = sorted(det_rows)
            raw_rows[table] = raws
        ledger = sorted([r[0], r[1], int(r[2]), r[3]] for r in conn.execute(
            "SELECT migration_id, state, attempt, sanitized_error_code"
            " FROM shared.schema_migrations").fetchall()) if any(
            f"{n}.{r}" == LEDGER for n, r in tables) else []
    normalised = {k: (v if k.startswith("rows:") else sorted(
        [_norm(x, subs, boot) for x in r] for r in v)) for k, v in view.items()}
    digest = hashlib.sha256(json.dumps(normalised, sort_keys=True).encode()).hexdigest()
    return Capture(reservation, digest, nondet, identities, ledger, raw_rows, normalised)
