"""Incoming fax routing FX-IN, FX2: the OFFLINE receiving-line provisioner. SYNTHETIC ONLY.

Architecture v1r2 §3.2 and test cases v1r1 §0 (Codex ALIGN 19629e35): receiving lines are written
only by this step, never by a runtime or support operation. One transaction on the installer's
admin connection:

1. every op is validated before any database contact (`OP_INVALID`, `LINE_INVALID`);
2. `pg_try_advisory_xact_lock(K_FX_LINES)` — exclusive with every running app (which holds the
   shared session lock) and every in-flight receipt (which holds the shared transaction lock);
   not acquired -> `LINE_PROVISION_BUSY`, nothing written;
3. after the lock, the ops are checked against EVERY tenant's lines (`TENANT_UNKNOWN`,
   `LINE_DUPLICATE`, `LINE_NOT_FOUND`, `LINE_IN_USE` while a rule references the line);
4. then applied in order. Any refusal rolls the whole call back.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

import psycopg
from psycopg import sql

from haloflow.m05.fax_rules import E164, K_FX_LINES

OPS: Final = ("add", "remove")


class LineProvisionRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LineOp:
    op: str
    tenant_id: str
    line: str


def _validate(ops: list[LineOp]) -> None:
    for item in ops:
        if item.op not in OPS:
            raise LineProvisionRefused("OP_INVALID")
        if not isinstance(item.line, str) or not E164.fullmatch(item.line):
            raise LineProvisionRefused("LINE_INVALID")


def _tenant_schemas(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """tenant_id -> schema for every tenant whose schema has the FX tables."""

    rows = conn.execute(
        "SELECT t.tenant_id, t.schema_key FROM shared.tenants AS t"
        " WHERE to_regclass(quote_ident(t.schema_key) || '.m05_fx_lines') IS NOT NULL"
    ).fetchall()
    return {str(tenant): str(schema) for tenant, schema in rows}


def _current(conn: psycopg.Connection[Any], schemas: dict[str, str]) -> dict[str, str]:
    """line -> tenant_id across the install."""

    owners: dict[str, str] = {}
    for tenant, schema in schemas.items():
        for (line,) in conn.execute(sql.SQL("SELECT line_e164 FROM {}.m05_fx_lines").format(
                sql.Identifier(schema))).fetchall():
            if line in owners:  # an install already broken: refuse to build on it
                raise LineProvisionRefused("LINE_DUPLICATE")
            owners[str(line)] = tenant
    return owners


def _in_use(conn: psycopg.Connection[Any], schema: str, line: str) -> bool:
    row = conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.m05_fx_rules"
                               " WHERE line_e164 = %s)").format(sql.Identifier(schema)),
                       (line,)).fetchone()
    return bool(row and row[0])


def provision_lines(conninfo: str, ops: Iterable[LineOp]) -> None:
    items = list(ops)
    _validate(items)
    with psycopg.connect(conninfo) as conn:
        try:
            with conn.transaction():
                locked = conn.execute("SELECT pg_try_advisory_xact_lock(%s)",
                                      (K_FX_LINES,)).fetchone()
                if not (locked and locked[0]):
                    raise LineProvisionRefused("LINE_PROVISION_BUSY")
                schemas = _tenant_schemas(conn)
                owners = _current(conn, schemas)
                for item in items:  # validate the whole call against the evolving state
                    if item.tenant_id not in schemas:
                        raise LineProvisionRefused("TENANT_UNKNOWN")
                    if item.op == "add":
                        if item.line in owners:
                            raise LineProvisionRefused("LINE_DUPLICATE")
                        owners[item.line] = item.tenant_id
                    else:
                        if owners.get(item.line) != item.tenant_id:
                            raise LineProvisionRefused("LINE_NOT_FOUND")
                        if _in_use(conn, schemas[item.tenant_id], item.line):
                            raise LineProvisionRefused("LINE_IN_USE")
                        del owners[item.line]
                for item in items:
                    target = sql.Identifier(schemas[item.tenant_id])
                    if item.op == "add":
                        conn.execute(sql.SQL("INSERT INTO {}.m05_fx_lines (line_e164)"
                                             " VALUES (%s)").format(target), (item.line,))
                    else:
                        conn.execute(sql.SQL("DELETE FROM {}.m05_fx_lines WHERE line_e164 = %s"
                                             ).format(target), (item.line,))
        except psycopg.errors.ForeignKeyViolation as error:
            # a rule written after the in-use check: the delete itself is refused (atomic)
            raise LineProvisionRefused("LINE_IN_USE") from error
