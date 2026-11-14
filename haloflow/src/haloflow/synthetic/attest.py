"""Attestation for the synthetic installer (Option B B2). SYNTHETIC ONLY.

A0 (no I/O beyond the environment): deployment, operator attestation, state path, admin DSN.
Fresh-cluster baseline: the classes in `FRESH_CLASSES`, checked read-only in `postgres` AND
the target. A cluster that fails any class is refused CLUSTER_NOT_FRESH naming the classes;
nothing is changed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final

import psycopg

from haloflow.synthetic.dsn import AdminEndpoint, parse_admin_dsn
from haloflow.synthetic.errors import SyntheticRefused

FRESH_CLASSES: Final = (
    "role", "role_attribute", "membership", "role_setting", "database", "database_acl",
    "tablespace", "target_comment", "reservation_slot", "schema", "extension", "relation",
    "function", "type", "event_trigger", "public_owner", "public_acl", "default_acl", "fdw",
    "publication")

PUBLIC_ACL: Final = "{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}"
_SYSTEM_NS: Final = ("pg_catalog", "information_schema", "pg_toast")
_NS_FILTER: Final = (
    "n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')"
    " AND n.nspname NOT LIKE 'pg\\_temp\\_%%' AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%%'")

_CLUSTER_CHECKS: Final = {
    "role": "SELECT count(*) = 0 FROM pg_catalog.pg_authid WHERE oid >= 16384",
    "role_attribute": (
        "SELECT rolsuper AND rolinherit AND rolcreaterole AND rolcreatedb AND rolcanlogin"
        " AND rolreplication AND rolbypassrls AND rolconnlimit = -1 AND rolvaliduntil IS NULL"
        " FROM pg_catalog.pg_authid WHERE oid = 10"),
    "membership": ("SELECT count(*) = 0 FROM pg_catalog.pg_auth_members"
                   " WHERE member = 10 OR member >= 16384 OR roleid >= 16384"),
    "role_setting": "SELECT count(*) = 0 FROM pg_catalog.pg_db_role_setting",
    "database": ("SELECT array_agg(datname::text ORDER BY datname) = %(dbs)s"
                 " FROM pg_catalog.pg_database"),
    "database_acl": ("SELECT bool_and(datacl IS NULL) FROM pg_catalog.pg_database"
                     " WHERE datname IN ('postgres', %(target)s)"),
    "tablespace": ("SELECT array_agg(spcname::text ORDER BY spcname)"
                   " = ARRAY['pg_default','pg_global'] FROM pg_catalog.pg_tablespace"),
    "target_comment": ("SELECT pg_catalog.shobj_description(oid, 'pg_database') IS NULL"
                       " FROM pg_catalog.pg_database WHERE datname = %(target)s"),
    "reservation_slot": "SELECT pg_catalog.shobj_description(10, 'pg_authid') IS NULL",
}
_DB_CHECKS: Final = {
    "schema": ("SELECT count(*) = 0 FROM pg_catalog.pg_namespace n WHERE " + _NS_FILTER
               + " AND n.nspname <> 'public'"),
    "extension": "SELECT array_agg(extname::text) = ARRAY['plpgsql'] FROM pg_catalog.pg_extension",
    "relation": ("SELECT count(*) = 0 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n"
                 " ON n.oid = c.relnamespace WHERE " + _NS_FILTER),
    "function": ("SELECT count(*) = 0 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n"
                 " ON n.oid = p.pronamespace WHERE " + _NS_FILTER),
    "type": ("SELECT count(*) = 0 FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n"
             " ON n.oid = t.typnamespace WHERE " + _NS_FILTER),
    "event_trigger": "SELECT count(*) = 0 FROM pg_catalog.pg_event_trigger",
    "public_owner": ("SELECT pg_catalog.pg_get_userbyid(nspowner) = 'pg_database_owner'"
                     " FROM pg_catalog.pg_namespace WHERE nspname = 'public'"),
    "public_acl": ("SELECT coalesce(nspacl::text, '') = %(public_acl)s"
                   " FROM pg_catalog.pg_namespace WHERE nspname = 'public'"),
    "default_acl": "SELECT count(*) = 0 FROM pg_catalog.pg_default_acl",
    "fdw": ("SELECT (SELECT count(*) FROM pg_catalog.pg_foreign_data_wrapper)"
            " + (SELECT count(*) FROM pg_catalog.pg_foreign_server) = 0"),
    "publication": "SELECT count(*) = 0 FROM pg_catalog.pg_publication",
}


def check_a0(environ: Mapping[str, str], *, attest_flag: bool) -> None:
    if environ.get("HALOFLOW_PLATFORM_DEPLOYMENT", "").strip().lower() == "production":
        raise SyntheticRefused("DEPLOYMENT_PRODUCTION")
    if not attest_flag or environ.get("HALOFLOW_SYNTHETIC_DISPOSABLE") != "1":
        raise SyntheticRefused("ATTESTATION_MISSING")
    state = environ.get("HALOFLOW_SYNTHETIC_STATE", "")
    if not state or not os.path.isabs(state):
        raise SyntheticRefused("STATE_PATH_INVALID")
    dsn = environ.get("HALOFLOW_SYNTHETIC_ADMIN_DSN")
    if not dsn:
        raise SyntheticRefused("DSN_INVALID")
    parse_admin_dsn(dsn, environ)


def admin_connect(endpoint: AdminEndpoint, db: str) -> psycopg.Connection[Any]:
    """A read-only-by-default admin session, verified to be the pinned endpoint/database."""

    conn = psycopg.connect(endpoint.conninfo(db), autocommit=True)
    try:
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET statement_timeout = '120s'")
        conn.execute("SET lock_timeout = '120s'")
        row = conn.execute(
            "SELECT pg_catalog.host(pg_catalog.inet_server_addr()),"
            " pg_catalog.current_setting('port'),"
            " pg_catalog.current_database()").fetchone()
        addr, port, current = row if row is not None else (None, None, None)
        if (addr is not None and addr not in ("127.0.0.1", "::1")) \
                or str(port) != str(endpoint.port) or current != db:
            raise SyntheticRefused("TARGET_MISMATCH")
        version = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()
        if version is None or int(version[0]) < 170000:
            raise SyntheticRefused("SERVER_VERSION_UNSUPPORTED")
    except BaseException:
        conn.close()
        raise
    return conn


def fresh_failures(endpoint: AdminEndpoint, target: str) -> tuple[str, ...]:
    """Every failing fresh-baseline class, in FRESH_CLASSES order (read-only)."""

    failed: set[str] = set()
    params = {"dbs": sorted(["postgres", "template0", "template1", target]), "target": target,
              "public_acl": PUBLIC_ACL}
    with admin_connect(endpoint, "postgres") as conn:
        for cls, query in _CLUSTER_CHECKS.items():
            row = conn.execute(query, params).fetchone()
            if row is None or row[0] is not True:
                failed.add(cls)
        databases = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_catalog.pg_database WHERE datname IN ('postgres', %s)",
            (target,)).fetchall()]
    for db in databases:
        with admin_connect(endpoint, db) as conn:
            for cls, query in _DB_CHECKS.items():
                row = conn.execute(query, params).fetchone()
                if row is None or row[0] is not True:
                    failed.add(cls)
    return tuple(c for c in FRESH_CLASSES if c in failed)
