"""CP2-2a test support: template handles, mutants, identities and oracles.

Imported only by `tests/m02/conftest.py`, which exposes it to test modules as the
`m02` fixture. Test modules import nothing across modules.

Everything here derives from the approved test cases v4 and erratum 1:

- ET-2 I-3/I-4: `T002_SQL` carries step markers and guard-group markers. The
  builders below split on them and raise `TemplateHandleError` when a marker is
  missing, duplicated or out of order. A test that hits that error fails in
  setup. It never passes.
- ET-2 I-5: each mutant edits the production template at one exact anchor that
  occurs exactly once in its step. A missing or duplicated anchor raises in the
  same way.
- ET-1: a `t002` guard failure reaches the provisioning caller as
  `TenantMigrationFailed(MIGRATION_DDL_FAILED)`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import AsyncConnection, sql

# ---------------------------------------------------------------------------
# Fixed vocabulary (architecture v4; test cases v4; erratum 1 ET-2)
# ---------------------------------------------------------------------------

LOCK_OWNER = "haloflow_m02_lock_owner"
MIGRATOR = "haloflow_migrator"
RUNTIME = "haloflow_runtime"
TABLE = "operation_registry"
T002_ID = "t002_m02_operation_registry"
REJECTOR_FUNCTION = "operation_registry_reject"  # architecture v1 §5 step 3
REJECTOR_TRIGGER = "operation_registry_immutable"

# PostgreSQL 17's eight table privileges (test cases v4 §1, "P8").
P8 = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")
# Column privilege universe (test cases v4 §1).
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
ROLE_ATTRIBUTES = (
    "rolcanlogin",
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolreplication",
    "rolbypassrls",
)

# Design v0.3 §4.2 as carried by architecture v1 §5 step 1; types and order as
# test case 2A-T01 lists them.
EXPECTED_COLUMNS = (
    ("operation_id", "uuid", True),
    ("owner_service", "character varying(64)", True),
    ("action_code", "character varying(96)", True),
    ("business_key_fingerprint", "bytea", True),
    ("business_key_version", "smallint", True),
    ("subject_type", "character varying(48)", True),
    ("subject_id", "uuid", True),
    ("resend_of_operation_id", "uuid", False),
    ("correlation_id", "uuid", True),
    ("producer_version", "character varying(64)", True),
    ("created_at", "timestamp with time zone", True),
)
UNIQUE_COLUMNS = (
    "owner_service",
    "action_code",
    "business_key_version",
    "business_key_fingerprint",
)

# ET-2 I-6. The reviewed rejector body and its digest, written here and NOT
# imported from the module under test (architecture v4 §5.4, independence).
REJECTOR_BODY = (
    "\nBEGIN\n"
    "    RAISE EXCEPTION USING\n"
    "        ERRCODE = '0A000',\n"
    "        MESSAGE = 'operation_registry rows are immutable';\n"
    "END;\n"
)
REJECTOR_BODY_SHA256 = "944e1d60c21f9efb574b73dce21073c8bb4cc2e8b08dabdd74be43c730c61200"
REJECTOR_DOLLAR_TAG = "$body$"  # architecture v1 §5 step 3
IMMUTABLE_MESSAGE = "operation_registry rows are immutable"

# Architecture v4 §5.2 / §5.3 fixed guard messages (ET-2 I-8).
MSG_REJECTOR = "operation_registry rejector verification failed"
MSG_PRIVILEGE = "operation_registry privilege verification failed"
MSG_LOCK_OWNER = "operation_registry lock owner verification failed"
MSG_GRANT_ORDER = "operation_registry grant order verification failed"
MSG_POST_GRANT = "operation_registry lock owner grant verification failed"

MIGRATION_DDL_FAILED = "MIGRATION_DDL_FAILED"
EXECUTION_ROLE_UNAVAILABLE = "EXECUTION_ROLE_UNAVAILABLE"

STEP_NAMES = (
    "table",
    "runtime_privileges",
    "rejector",
    "guard",
    "lock_owner_grants",
    "post_grant_check",
)
GROUP_NAMES = ("G-1", "G-3", "G-4", "G-5", "G-6")

LOCK_OWNER_GRANT = (
    "GRANT SELECT (operation_id), UPDATE (correlation_id) "
    "ON {schema}.operation_registry TO haloflow_m02_lock_owner;"
)


class TemplateHandleError(AssertionError):
    """A marker or anchor the tests bind (ET-2) is missing, duplicated or misplaced."""


# ---------------------------------------------------------------------------
# Template handles
# ---------------------------------------------------------------------------


def _marker_positions(text: str, markers: Sequence[str]) -> list[int]:
    positions: list[int] = []
    for marker in markers:
        pattern = re.compile(rf"^[ \t]*{re.escape(marker)}[ \t]*$", re.MULTILINE)
        found = [match.start() for match in pattern.finditer(text)]
        if len(found) != 1:
            raise TemplateHandleError(f"marker {marker!r} found {len(found)} times")
        positions.append(found[0])
    if positions != sorted(positions):
        raise TemplateHandleError(f"markers out of order: {list(markers)}")
    return positions


@dataclass(frozen=True)
class Template:
    """`T002_SQL` split at its six step markers (ET-2 I-3). Joins back exactly."""

    prefix: str
    steps: dict[str, str]

    @classmethod
    def parse(cls, template: str) -> Template:
        markers = [f"-- t002:step:{name}" for name in STEP_NAMES]
        positions = _marker_positions(template, markers)
        bounds = [*positions, len(template)]
        steps = {
            name: template[bounds[index] : bounds[index + 1]]
            for index, name in enumerate(STEP_NAMES)
        }
        parsed = cls(prefix=template[: positions[0]], steps=steps)
        if parsed.join() != template:
            raise TemplateHandleError("step split does not reproduce the template")
        return parsed

    def join(self, steps: dict[str, str] | None = None) -> str:
        chosen = self.steps if steps is None else steps
        return self.prefix + "".join(chosen[name] for name in STEP_NAMES if name in chosen)

    def with_step(self, name: str, text: str) -> Template:
        return Template(self.prefix, {**self.steps, name: text})

    def without(self, *names: str) -> Template:
        return Template(self.prefix, {k: v for k, v in self.steps.items() if k not in names})

    def only(self, *names: str) -> Template:
        return Template(self.prefix, {k: v for k, v in self.steps.items() if k in names})

    def render(self) -> str:
        return self.join()


def replace_once(text: str, anchor: str, replacement: str) -> str:
    count = text.count(anchor)
    if count != 1:
        raise TemplateHandleError(f"anchor {anchor!r} found {count} times")
    return text.replace(anchor, replacement)


def append_to(step_text: str, addition: str) -> str:
    return step_text.rstrip("\n") + "\n" + addition + "\n\n"


def guard_group_bounds(guard: str) -> dict[str, tuple[int, int]]:
    """Start/end offsets of each guard group (ET-2 I-4).

    A group runs to the next group marker; G-6 runs to the last `END` line of the
    step, which closes the `DO` body.
    """

    markers = [f"-- t002:guard:{group}" for group in GROUP_NAMES]
    positions = _marker_positions(guard, markers)
    ends = [match.start() for match in re.finditer(r"^[ \t]*END[ \t]*;?[ \t]*$", guard, re.M)]
    closing = [end for end in ends if end > positions[-1]]
    if not closing:
        raise TemplateHandleError("no END closes the guard DO body after G-6")
    bounds = [*positions, closing[-1]]
    return {group: (bounds[i], bounds[i + 1]) for i, group in enumerate(GROUP_NAMES)}


def insert_before_group(guard: str, group: str, statement: str) -> str:
    start, _ = guard_group_bounds(guard)[group]
    return guard[:start] + "    " + statement + "\n" + guard[start:]


def delete_group(guard: str, group: str) -> str:
    start, end = guard_group_bounds(guard)[group]
    return guard[:start] + guard[end:]


def rejector_body(template: str) -> str:
    """The bytes between the two `$body$` tags (ET-2 I-6)."""

    if template.count(REJECTOR_DOLLAR_TAG) != 2:
        raise TemplateHandleError("expected exactly two $body$ tags")
    first = template.index(REJECTOR_DOLLAR_TAG) + len(REJECTOR_DOLLAR_TAG)
    second = template.index(REJECTOR_DOLLAR_TAG, first)
    return template[first:second]


# ---------------------------------------------------------------------------
# Mutants (test cases v4 §2.3, §2.6, §2.7, §2.8)
# ---------------------------------------------------------------------------

_S = "{schema}"
_TABLE_REF = f"{_S}.operation_registry"
_FUNCTION_REF = f"{_S}.{REJECTOR_FUNCTION}()"

_TRIGGER_STATEMENT = re.compile(rf"CREATE TRIGGER {REJECTOR_TRIGGER}\b[^;]*;", re.MULTILINE)


def _edit(step: str, fn: Callable[[str], str]) -> Callable[[Template], Template]:
    def apply(template: Template) -> Template:
        return template.with_step(step, fn(template.steps[step]))

    return apply


def _remove_trigger(text: str) -> str:
    found = _TRIGGER_STATEMENT.findall(text)
    if len(found) != 1:
        raise TemplateHandleError(f"trigger statement found {len(found)} times")
    return text.replace(found[0], "")


_NOOP_TRIGGER = (
    f"CREATE FUNCTION {_S}.operation_registry_test_noop() RETURNS trigger\n"
    "    LANGUAGE plpgsql AS $noop$ BEGIN RETURN NULL; END; $noop$;\n"
    f"CREATE TRIGGER operation_registry_test_noop AFTER INSERT ON {_TABLE_REF}\n"
    f"    FOR EACH STATEMENT EXECUTE FUNCTION {_S}.operation_registry_test_noop();"
)


def _lock_grants(*statements: str) -> Callable[[Template], Template]:
    def apply(template: Template) -> Template:
        step = template.steps["lock_owner_grants"]
        replaced = replace_once(step, LOCK_OWNER_GRANT, "\n".join(statements))
        return template.with_step("lock_owner_grants", replaced)

    return apply


def _lock_grants_plus(*statements: str) -> Callable[[Template], Template]:
    def apply(template: Template) -> Template:
        step = template.steps["lock_owner_grants"]
        replace_once(step, LOCK_OWNER_GRANT, LOCK_OWNER_GRANT)  # anchor check only
        return template.with_step("lock_owner_grants", append_to(step, "\n".join(statements)))

    return apply


def _grant_moved_before_group(group: str) -> Callable[[Template], Template]:
    def apply(template: Template) -> Template:
        replace_once(template.steps["lock_owner_grants"], LOCK_OWNER_GRANT, LOCK_OWNER_GRANT)
        guard = insert_before_group(template.steps["guard"], group, LOCK_OWNER_GRANT)
        return template.with_step("guard", guard).without("lock_owner_grants")

    return apply


def _grant_moved_before_guard(template: Template) -> Template:
    replace_once(template.steps["lock_owner_grants"], LOCK_OWNER_GRANT, LOCK_OWNER_GRANT)
    rejector = append_to(template.steps["rejector"], LOCK_OWNER_GRANT)
    return template.with_step("rejector", rejector).without("lock_owner_grants")


def _chain(*fns: Callable[[Template], Template]) -> Callable[[Template], Template]:
    def apply(template: Template) -> Template:
        for fn in fns:
            template = fn(template)
        return template

    return apply


def _lo(statement: str) -> str:
    return statement.format(t=_TABLE_REF, lo=LOCK_OWNER)


_X03 = _edit("rejector", lambda t: replace_once(t, "FOR EACH ROW", "FOR EACH ROW WHEN (false)"))
_Q04 = _lock_grants_plus(_lo("GRANT MAINTAIN ON {t} TO {lo};"))

MUTANTS: dict[str, Callable[[Template], Template]] = {
    # 2A-T04c
    "t04c": _edit(
        "table",
        lambda t: replace_once(
            t, "DEFAULT statement_timestamp()", "DEFAULT transaction_timestamp()"
        ),
    ),
    # 2A-X01 .. X13 (G-3)
    "x01": _edit("rejector", _remove_trigger),
    "x02": _edit(
        "rejector",
        lambda t: append_to(t, f"ALTER TABLE {_TABLE_REF} DISABLE TRIGGER {REJECTOR_TRIGGER};"),
    ),
    "x03": _X03,
    "x04": _edit(
        "rejector",
        lambda t: replace_once(
            t, f"BEFORE UPDATE ON {_TABLE_REF}", f"BEFORE UPDATE OF correlation_id ON {_TABLE_REF}"
        ),
    ),
    "x05": _edit(
        "rejector",
        lambda t: replace_once(
            t, f"BEFORE UPDATE ON {_TABLE_REF}", f"AFTER UPDATE ON {_TABLE_REF}"
        ),
    ),
    "x06": _edit("rejector", lambda t: replace_once(t, "FOR EACH ROW", "FOR EACH STATEMENT")),
    "x07": _edit(
        "rejector",
        lambda t: replace_once(
            t,
            f"EXECUTE FUNCTION {_FUNCTION_REF}",
            f"EXECUTE FUNCTION {_S}.{REJECTOR_FUNCTION}('x')",
        ),
    ),
    "x08": _edit(
        "rejector",
        lambda t: replace_once(t, IMMUTABLE_MESSAGE, "operation_registry rows are frozen"),
    ),
    "x09": _edit("rejector", lambda t: replace_once(t, "SECURITY INVOKER", "SECURITY DEFINER")),
    "x10": _edit(
        "rejector",
        lambda t: replace_once(
            t,
            f"SET search_path = pg_catalog, {_S}, pg_temp",
            f"SET search_path = pg_temp, pg_catalog, {_S}",
        ),
    ),
    "x11": _edit(
        "rejector",
        lambda t: replace_once(t, f"REVOKE ALL ON FUNCTION {_FUNCTION_REF} FROM PUBLIC;", ""),
    ),
    "x12": _edit(
        "rejector",
        lambda t: append_to(t, f"GRANT EXECUTE ON FUNCTION {_FUNCTION_REF} TO {RUNTIME};"),
    ),
    "x13": _edit("rejector", lambda t: append_to(t, _NOOP_TRIGGER)),
    # 2A-X14 .. X18 (G-4, G-5)
    "x14": _edit(
        "runtime_privileges",
        lambda t: replace_once(
            t,
            f"REVOKE INSERT, UPDATE, DELETE ON {_TABLE_REF} FROM {RUNTIME};",
            f"REVOKE UPDATE, DELETE ON {_TABLE_REF} FROM {RUNTIME};",
        ),
    ),
    "x15": _edit(
        "runtime_privileges",
        lambda t: append_to(t, f"GRANT UPDATE (correlation_id) ON {_TABLE_REF} TO {RUNTIME};"),
    ),
    "x16": _edit(
        "runtime_privileges",
        lambda t: append_to(t, f"GRANT MAINTAIN ON {_TABLE_REF} TO {RUNTIME};"),
    ),
    "x17": _edit(
        "runtime_privileges",
        lambda t: append_to(t, f"GRANT SELECT ON {_TABLE_REF} TO {RUNTIME} WITH GRANT OPTION;"),
    ),
    "x18": _edit(
        "runtime_privileges",
        lambda t: append_to(
            t, f"GRANT SELECT (operation_id) ON {_TABLE_REF} TO {RUNTIME} WITH GRANT OPTION;"
        ),
    ),
    # 2A-X30 kill control: X03's defect plus the guard deleted.
    "x30": _chain(_X03, lambda t: t.without("guard")),
    # 2A-O01 .. O07 (D25-order placement mutants; PENDING)
    "o01": _grant_moved_before_guard,
    "o02": _grant_moved_before_group("G-3"),
    "o03": _grant_moved_before_group("G-4"),
    "o04": _grant_moved_before_group("G-5"),
    "o05": _grant_moved_before_group("G-6"),
    "o06": _chain(
        _grant_moved_before_group("G-3"),
        lambda t: t.with_step("guard", delete_group(t.steps["guard"], "G-6")),
    ),
    "o07": lambda t: t.with_step(
        "guard",
        insert_before_group(
            t.steps["guard"],
            "G-6",
            LOCK_OWNER_GRANT
            + "\n    "
            + _lo("REVOKE SELECT (operation_id), UPDATE (correlation_id) ON {t} FROM {lo};"),
        ),
    ),
    # 2A-Q01 .. Q10 (post-grant check)
    "q01": _lock_grants(_lo("GRANT UPDATE (correlation_id) ON {t} TO {lo};")),
    "q02": _lock_grants(_lo("GRANT SELECT (operation_id) ON {t} TO {lo};")),
    "q03": _lock_grants_plus(_lo("GRANT SELECT ON {t} TO {lo};")),
    "q04": _Q04,
    "q05": _lock_grants(_lo("GRANT SELECT (operation_id), UPDATE (owner_service) ON {t} TO {lo};")),
    "q06": _lock_grants_plus(_lo("GRANT SELECT (correlation_id) ON {t} TO {lo};")),
    "q07": _lock_grants(
        _lo("GRANT SELECT (operation_id) ON {t} TO {lo} WITH GRANT OPTION;"),
        _lo("GRANT UPDATE (correlation_id) ON {t} TO {lo};"),
    ),
    "q08": _lock_grants(
        _lo("GRANT SELECT (operation_id) ON {t} TO {lo};"),
        _lo("GRANT UPDATE (correlation_id) ON {t} TO {lo} WITH GRANT OPTION;"),
    ),
    "q09": _lock_grants_plus(_lo("GRANT REFERENCES (operation_id) ON {t} TO {lo};")),
    "q10": _chain(_Q04, lambda t: t.without("post_grant_check")),
}

G3_MUTANTS = tuple(f"x{n:02d}" for n in range(1, 14))
G45_MUTANTS = ("x14", "x15", "x16", "x17", "x18")
ORDER_REFUSED = ("o01", "o02", "o03", "o04", "o05")
POST_GRANT_REFUSED = tuple(f"q{n:02d}" for n in range(1, 10))


def mutant_template(production_sql: str, name: str) -> str:
    return MUTANTS[name](Template.parse(production_sql)).render()


def mutant_id(name: str) -> str:
    return f"t002_test_{name}"


# ---------------------------------------------------------------------------
# Identities (test cases v4 §1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identities:
    admin: str
    logins: dict[str, str]


def _set_role(conn: psycopg.Connection[Any], *roles: str) -> None:
    for role in roles:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))


@contextmanager
def connect_admin(ids: Identities) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(ids.admin, autocommit=True) as conn:
        yield conn


@contextmanager
def connect_as(ids: Identities, identity: str) -> Iterator[psycopg.Connection[Any]]:
    """`MIG`, `LOCK` or `RT`, through the real login-shim chain; never a superuser.

    `SET ROLE` is its own committed statement, outside any transaction a test
    expects to fail (§1 "Errored sessions and roles").
    """

    chains = {
        "MIG": (MIGRATOR, (MIGRATOR,)),
        "LOCK": (MIGRATOR, (MIGRATOR, LOCK_OWNER)),
        "RT": (RUNTIME, (RUNTIME,)),
    }
    login, roles = chains[identity]
    with psycopg.connect(ids.logins[login], autocommit=True) as conn:
        _set_role(conn, *roles)
        assert_current_user(conn, roles[-1])
        yield conn


def current_user(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute("SELECT current_user").fetchone()
    assert row is not None
    return str(row[0])


def assert_current_user(conn: psycopg.Connection[Any], expected: str) -> None:
    assert current_user(conn) == expected


def reassert_role(conn: psycopg.Connection[Any], identity: str) -> None:
    """After an expected error: the transaction is gone; re-establish the role."""

    expected = {"MIG": MIGRATOR, "LOCK": LOCK_OWNER, "RT": RUNTIME}[identity]
    if current_user(conn) != expected:
        chain = {"MIG": (MIGRATOR,), "LOCK": (MIGRATOR, LOCK_OWNER), "RT": (RUNTIME,)}
        _set_role(conn, *chain[identity])
    assert_current_user(conn, expected)


def expect_sqlstate(
    conn: psycopg.Connection[Any], statement: str | sql.Composable, params: Any, sqlstate: str
) -> psycopg.Error:
    """Run `statement` in its own transaction and require exactly `sqlstate`."""

    try:
        with conn.transaction():
            conn.execute(statement, params)  # type: ignore[arg-type]
    except psycopg.Error as error:
        assert error.sqlstate == sqlstate, f"expected {sqlstate}, got {error.sqlstate}"
        return error
    raise AssertionError(f"expected SQLSTATE {sqlstate}; the statement succeeded")


def admin_one(ids: Identities, query: str, params: Any = None) -> tuple[Any, ...]:
    with connect_admin(ids) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return tuple(row)


def admin_all(ids: Identities, query: str, params: Any = None) -> list[tuple[Any, ...]]:
    with connect_admin(ids) as conn:
        return [tuple(row) for row in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Catalogue reads
# ---------------------------------------------------------------------------


def qualified(schema: str) -> str:
    return f"{schema}.{TABLE}"


def role_attributes(ids: Identities, role: str) -> dict[str, bool] | None:
    rows = admin_all(
        ids,
        f"SELECT {', '.join(ROLE_ATTRIBUTES)} FROM pg_catalog.pg_roles WHERE rolname = %s",
        (role,),
    )
    if not rows:
        return None
    return dict(zip(ROLE_ATTRIBUTES, rows[0], strict=True))


def edges_into(ids: Identities, role: str) -> list[tuple[str, bool, bool, bool, str]]:
    """(member, set, inherit, admin, grantor) for every edge into `role`."""

    return admin_all(
        ids,
        """
        SELECT m.rolname, a.set_option, a.inherit_option, a.admin_option, g.rolname
          FROM pg_catalog.pg_auth_members AS a
          JOIN pg_catalog.pg_roles AS r ON r.oid = a.roleid
          JOIN pg_catalog.pg_roles AS m ON m.oid = a.member
          JOIN pg_catalog.pg_roles AS g ON g.oid = a.grantor
         WHERE r.rolname = %s
         ORDER BY m.rolname
        """,
        (role,),
    )


def assert_baseline_role(ids: Identities) -> None:
    """R01 + R02 on the session baseline; used after every shared-cluster edit."""

    assert role_attributes(ids, LOCK_OWNER) == dict.fromkeys(ROLE_ATTRIBUTES, False)
    edges = edges_into(ids, LOCK_OWNER)
    assert [(m, s, i, a) for m, s, i, a, _ in edges] == [(MIGRATOR, True, False, False)]


def set_lock_owner_edge(ids: Identities, options: str | None) -> None:
    """Revoke every migrator edge into the lock owner, assert none, then regrant.

    ET-3: `GRANTED BY` each recorded grantor so the result is deterministic.
    `options=None` leaves the edge revoked.
    """

    with connect_admin(ids) as conn:
        grantors = conn.execute(
            """
            SELECT g.rolname
              FROM pg_catalog.pg_auth_members AS a
              JOIN pg_catalog.pg_roles AS r ON r.oid = a.roleid
              JOIN pg_catalog.pg_roles AS m ON m.oid = a.member
              JOIN pg_catalog.pg_roles AS g ON g.oid = a.grantor
             WHERE r.rolname = %s AND m.rolname = %s
            """,
            (LOCK_OWNER, MIGRATOR),
        ).fetchall()
        for (grantor,) in grantors:
            conn.execute(
                sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
                    sql.Identifier(LOCK_OWNER), sql.Identifier(MIGRATOR), sql.Identifier(grantor)
                )
            )
        remaining = conn.execute(
            """
            SELECT count(*) FROM pg_catalog.pg_auth_members AS a
              JOIN pg_catalog.pg_roles AS r ON r.oid = a.roleid
              JOIN pg_catalog.pg_roles AS m ON m.oid = a.member
             WHERE r.rolname = %s AND m.rolname = %s
            """,
            (LOCK_OWNER, MIGRATOR),
        ).fetchone()
        assert remaining == (0,)
        if options is not None:
            conn.execute(
                sql.SQL("GRANT {} TO {} WITH " + options).format(
                    sql.Identifier(LOCK_OWNER), sql.Identifier(MIGRATOR)
                )
            )


N1_OPTIONS = "INHERIT FALSE, SET TRUE, ADMIN FALSE"


def restore_lock_owner(ids: Identities) -> None:
    """Restore attributes and the N1 edge, then re-assert R01/R02."""

    with connect_admin(ids) as conn:
        conn.execute(
            sql.SQL(
                "ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(LOCK_OWNER))
        )
    set_lock_owner_edge(ids, N1_OPTIONS)
    assert_baseline_role(ids)


def table_privilege(ids: Identities, role: str, schema: str, privilege: str) -> bool:
    return bool(
        admin_one(
            ids,
            "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
            (role, qualified(schema), privilege),
        )[0]
    )


def column_privilege(ids: Identities, role: str, schema: str, column: str, privilege: str) -> bool:
    return bool(
        admin_one(
            ids,
            "SELECT pg_catalog.has_column_privilege(%s, %s, %s, %s)",
            (role, qualified(schema), column, privilege),
        )[0]
    )


def any_column_privilege(ids: Identities, role: str, schema: str, privilege: str) -> bool:
    return bool(
        admin_one(
            ids,
            "SELECT pg_catalog.has_any_column_privilege(%s, %s, %s)",
            (role, qualified(schema), privilege),
        )[0]
    )


def assert_installed(ids: Identities, schema: str) -> None:
    """First assertion of every row that reads the installed table (test cases v4 §5).

    At the pre-change baseline the production composition installs no `t002`, so
    this is where those rows go behaviour-red: inside the test body, never in
    fixture setup (Codex packet-v1 review, P1).
    """

    assert admin_one(
        ids, "SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (qualified(schema),)
    ) == (True,), f"{qualified(schema)} is not installed"


def assert_lock_owner_grants_exact(ids: Identities, schema: str) -> None:
    """§2.8 / 2A-Q00: exactly SELECT(operation_id) and UPDATE(correlation_id)."""

    columns = [name for name, _, _ in EXPECTED_COLUMNS]
    for column in columns:
        assert column_privilege(ids, LOCK_OWNER, schema, column, "SELECT") is (
            column == "operation_id"
        ), column
        assert column_privilege(ids, LOCK_OWNER, schema, column, "UPDATE") is (
            column == "correlation_id"
        ), column
    for privilege in P8:
        assert table_privilege(ids, LOCK_OWNER, schema, privilege) is False, privilege
    for privilege in ("INSERT", "REFERENCES"):
        assert any_column_privilege(ids, LOCK_OWNER, schema, privilege) is False, privilege
    assert (
        column_privilege(ids, LOCK_OWNER, schema, "operation_id", "SELECT WITH GRANT OPTION")
        is False
    )
    assert (
        column_privilege(ids, LOCK_OWNER, schema, "correlation_id", "UPDATE WITH GRANT OPTION")
        is False
    )


# ---------------------------------------------------------------------------
# Provisioning (production composition, mutant registries, M01-only)
# ---------------------------------------------------------------------------


def tenant_for(label: str) -> tuple[str, str]:
    """A deterministic synthetic tenant identity per test label (R-X4)."""

    digest = uuid5(NAMESPACE_URL, f"haloflow-test:m02:{label}").hex[:8]
    return f"clinic-m{digest}", f"tenant_m{digest}"


def _connection_factory(conninfo: str) -> Callable[[], Any]:
    async def _connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


async def provision(
    ids: Identities,
    registry: Any,
    tenant: tuple[str, str],
    *,
    supported: range = range(1, 3),
    manifest: Any = None,
) -> Any:
    from haloflow.m01.provisioning import (
        MIGRATOR_ROLE,
        PROVISIONER_ROLE,
        ProvisioningRequest,
        TenantMigrationRunner,
        TenantProvisioner,
    )

    kwargs: dict[str, Any] = {} if manifest is None else {"manifest": manifest}
    runner = TenantMigrationRunner(
        _connection_factory(ids.logins[MIGRATOR_ROLE]), registry, **kwargs
    )
    provisioner = TenantProvisioner(
        _connection_factory(ids.logins[PROVISIONER_ROLE]),
        runner,
        supported_schema_versions=supported,
    )
    tenant_id, schema_key = tenant
    return await provisioner.provision(
        ProvisioningRequest(
            tenant_id=tenant_id,
            schema_key=schema_key,
            actor_id="m02-test",
            execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:m02:provision:{tenant_id}"),
        )
    )


def provision_sync(*args: Any, **kwargs: Any) -> Any:
    return asyncio.run(provision(*args, **kwargs))


def production_registry() -> Any:
    from haloflow.composition import build_production_tenant_migrations

    return build_production_tenant_migrations()


def m01_only_registry() -> Any:
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry

    return build_tenant_migration_registry(TENANT_MIGRATIONS)


def mutant_registry(name: str) -> Any:
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.units import T002_SQL

    return build_tenant_migration_registry(
        TENANT_MIGRATIONS,
        {mutant_id(name): mutant_template(T002_SQL, name)},
        allow_test_units=True,
    )


def render(template: str, schema_key: str) -> str:
    """Render through the production unit path (validation + `{schema}` replace)."""

    from haloflow.m01.provisioning.units import build_tenant_migration_registry

    registry = build_tenant_migration_registry(
        {"t002_test_direct_sql": template}, allow_test_units=True
    )
    rendered: str = registry.units[0].render(schema_key)
    return rendered


# ---------------------------------------------------------------------------
# Oracles
# ---------------------------------------------------------------------------


def ledger(ids: Identities, tenant_id: str) -> dict[str, tuple[Any, ...]]:
    rows = admin_all(
        ids,
        """
        SELECT migration_id, state, sanitized_error_code, completed_at IS NOT NULL
          FROM shared.schema_migrations WHERE tenant_id = %s
        """,
        (tenant_id,),
    )
    return {row[0]: tuple(row[1:]) for row in rows}


def assert_refusal_oracle(
    ids: Identities, error: BaseException, tenant: tuple[str, str], migration_id: str
) -> None:
    """Test cases v4 §1 as corrected by erratum 1 ET-1: all four parts."""

    from haloflow.m01.errors import TenantMigrationFailed

    tenant_id, schema_key = tenant
    # 1. sanitized refusal from inside t002, no raw guard text
    assert isinstance(error, TenantMigrationFailed), type(error)
    assert error.reason_code == MIGRATION_DDL_FAILED
    surfaced = f"{error} {error.reason_code}"
    for leaked in ("verification failed", "operation_registry", schema_key):
        assert leaked not in surfaced
    # 2. ledger witness
    rows = ledger(ids, tenant_id)
    assert rows["t001_m01_baseline"][0] == "applied"
    assert rows[migration_id] == ("failed", MIGRATION_DDL_FAILED, True)
    # 3. absence, missing-safe, fresh ADMIN transaction
    assert admin_one(
        ids, "SELECT pg_catalog.to_regclass(%s) IS NULL", (qualified(schema_key),)
    ) == (True,)
    # 4. catalogue witness: no relation or column ACL in the schema names the lock owner
    assert admin_one(
        ids,
        """
        SELECT count(*) FROM (
            SELECT e.grantee
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace,
                   LATERAL pg_catalog.aclexplode(c.relacl) AS e
             WHERE n.nspname = %s
            UNION ALL
            SELECT e.grantee
              FROM pg_catalog.pg_attribute AS a
              JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace,
                   LATERAL pg_catalog.aclexplode(a.attacl) AS e
             WHERE n.nspname = %s
        ) AS entries
        WHERE entries.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s)
        """,
        (schema_key, schema_key, LOCK_OWNER),
    ) == (0,)


def assert_stage_one_refusal(
    ids: Identities, error: BaseException, tenant: tuple[str, str]
) -> None:
    """2A-M oracle: stage-1 refusal, no schema created or granted, no ledger row."""

    from haloflow.m01.errors import ProvisioningFailed

    tenant_id, schema_key = tenant
    assert isinstance(error, ProvisioningFailed), type(error)
    assert error.reason_code == EXECUTION_ROLE_UNAVAILABLE
    assert ledger(ids, tenant_id) == {}
    assert admin_one(ids, "SELECT pg_catalog.to_regnamespace(%s) IS NULL", (schema_key,)) == (True,)


def expect_install_failure(
    ids: Identities, registry: Any, tenant: tuple[str, str], migration_id: str
) -> None:
    from haloflow.m01.errors import TenantMigrationFailed

    try:
        provision_sync(ids, registry, tenant)
    except TenantMigrationFailed as error:
        assert_refusal_oracle(ids, error, tenant, migration_id)
        return
    raise AssertionError("the mutant installed; the guard or check did not refuse it")


def seeded_row(label: str) -> dict[str, Any]:
    """One synthetic registry row with known literals (R-X4: no PHI)."""

    def ident(part: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"haloflow-test:m02:row:{label}:{part}")

    return {
        "operation_id": ident("operation"),
        "owner_service": "m02-test",
        "action_code": f"test.{label}",
        "business_key_fingerprint": ident("fingerprint").bytes,
        "business_key_version": 1,
        "subject_type": "test_subject",
        "subject_id": ident("subject"),
        "resend_of_operation_id": None,
        "correlation_id": ident("correlation"),
        "producer_version": "m02-test-1",
    }


def insert_statement(schema: str) -> sql.Composed:
    columns = [name for name, _, _ in EXPECTED_COLUMNS if name != "created_at"]
    return sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(schema, TABLE),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
    )


def full_row(ids: Identities, schema: str, operation_id: UUID) -> tuple[Any, ...] | None:
    """Full row read by MIG (the table owner) in a fresh transaction (§2.5)."""

    with connect_as(ids, "MIG") as conn:
        row = conn.execute(
            sql.SQL("SELECT * FROM {} WHERE operation_id = %s").format(
                sql.Identifier(schema, TABLE)
            ),
            (operation_id,),
        ).fetchone()
    return None if row is None else tuple(row)
