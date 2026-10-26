"""L-6 CP-3: TenantSchemaUpgrade, capability, F, CL/CLc, classifier, C1-C3, AB (tiers D and C).

PostgreSQL 17, pre-merge CI or an owner-authorized local run only. D/C pre-merge
evidence is useful, not final (test cases v4 R1); nothing here is R-L6.X2 final
evidence. Data are synthetic (R2). Existing tests stay byte-unchanged (R3). A skip
fails the census (R4). "Nothing changed" is the R5 snapshot (`_r5_snapshot`).

Traceability (rows, variants and decisions):
- test cases v4 `a3d44758…bf3e`: TC-F01, F02, F03b, F04, F05, F06a, F06b, F06c, F07,
  F11, K03, K05, K07, V09, N09;
- IP-15/16 v2 `2068f441…de48`: TC-A03, A04, A05;
- owner records d6c0d06b…0580 (Q10/Q11), 0273e77d…f32b (Q6), f3c8b5e9…03ed
  (N09, A04 seam, A05, G01a-G03, A03 split, Q1/Q2/V09/Q8), de5024f1…508c (Q-B1..Q-B5);
- interface B5.1 v3 `9494f6e0…272e`;
- owner records 250102df…073c (Q-P2/Q-P5 capability_fault seam, Q-P3 F11-outer-G3) and
  03ae4897…1393 (Q-P4a, Q-P6, Q-P9, Q-P11 with conditions); Codex API clarifications
  Q-P4b/c/d and Q-P10 (`codex_cp3-b52-v3-review.md`).
  Nodes that depend on an open item say so in their docstring and in the manifest.

Interface symbols are imported inside each test or helper (plan v4 §6.1: D-sym rows
fail at the first CP-3 import before the implementation exists). The exact expected
pre-change site of every node is in the manifest (`pre_site`); the `k0_tenant`
fixture imports no CP-3 symbol, and any failure inside it is a SetupError.

Every harness database mutation is listed in README §5 with its approval status.
Preconditions for any run (B5.1 v3 §5.1): a fresh cluster, or a recorded role-state
preflight that matches, stopping on any difference. Run only when Rachel authorizes it.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg import AsyncConnection, sql

from haloflow.m01.provisioning import MIGRATOR_ROLE, PROVISIONER_ROLE

pytestmark = pytest.mark.postgres

OPS = "shared.tenant_maintenance_operations"
WITHHELD = "shared.tenant_maintenance_withheld"
ATTEMPTS = "shared.tenant_maintenance_attempts"
NEW_TABLES = (ATTEMPTS, WITHHELD, OPS)
T_LOCK_SECONDS = 30.0
BARRIER_WAIT_SECONDS = T_LOCK_SECONDS  # every barrier wait is bounded (R6)
POLL_SECONDS = 0.05
LOCK_OWNER_ROLE = "haloflow_m02_lock_owner"
RUNTIME_ROLE = "haloflow_runtime"
PROJECTOR_ROLE = "haloflow_audit_projector"
# The roles migrations 001 and 004 create (alembic/versions/001_m01_foundation.py:23-47,
# 004_m02_lock_owner_role.py:42). Role-state checks compare edges among these only.
CONTROLLED_ROLES = (
    "haloflow_owner",
    RUNTIME_ROLE,
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    PROJECTOR_ROLE,
    "haloflow_control_audit_writer",
    "haloflow_support_ro",
    "haloflow_breakglass_ro",
    "haloflow_breakglass_rw",
    LOCK_OWNER_ROLE,
)


class SetupError(AssertionError):
    """A seeded or constructed pre-state did not match; never a refusal under test."""


class UnmetInjection(AssertionError):
    """The injected fault did not produce the required condition (TC-A04-b3)."""


# --- administrator access (harness only) ------------------------------------


@contextmanager
def _admin(admin: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(admin, autocommit=True) as conn:
        yield conn


def _admin_rows(admin: str, query: Any, params: Any = None) -> list[tuple[Any, ...]]:
    with _admin(admin) as conn:
        return list(conn.execute(query, params).fetchall())


def _admin_one(admin: str, query: Any, params: Any = None) -> tuple[Any, ...]:
    rows = _admin_rows(admin, query, params)
    if len(rows) != 1:
        raise SetupError(f"expected one row, got {len(rows)}")
    return rows[0]


@pytest.fixture(scope="module", autouse=True)
def _record_server_version(migrated_database: str) -> None:
    """Evidence instrumentation (plan v4 §5.3): the census reads this line from -rA."""

    print(f"L6_SERVER_VERSION={_admin_one(migrated_database, 'SELECT version()')[0]}")


def _purge(admin: str, tenant_id: str) -> None:
    """Remove this tenant's L-6 rows. Superuser escape hatch, one transaction, as the
    CP-1 suite's helper (README §5, H1; owner record 03ae4897…1393). The user triggers on
    the three tables must all be in the ordinary enabled state ('O') first; any other
    state is a setup failure, never repaired. Isolated test database only."""

    with psycopg.connect(admin, autocommit=False) as conn, conn.transaction():
        states = conn.execute(
            "SELECT tgrelid::regclass::text, tgname, tgenabled::text FROM pg_trigger "
            "WHERE tgrelid = ANY(%s::regclass[]) AND NOT tgisinternal",
            (list(NEW_TABLES),),
        ).fetchall()
        if not states or any(state != "O" for _rel, _name, state in states):
            raise SetupError("H1: L-6 user triggers are not all in the expected enabled state")
        for table in NEW_TABLES:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        conn.execute(f"DELETE FROM {ATTEMPTS} WHERE tenant_id = %s", (tenant_id,))
        conn.execute(
            f"DELETE FROM {WITHHELD} WHERE maintenance_operation_id IN "
            f"(SELECT maintenance_operation_id FROM {OPS} WHERE tenant_id = %s)",
            (tenant_id,),
        )
        conn.execute(f"DELETE FROM {OPS} WHERE tenant_id = %s", (tenant_id,))
        for table in NEW_TABLES:
            conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


# --- task tracking: every background task ends, whatever the test outcome ----

_LIVE_TASKS: list[asyncio.Task[Any]] = []


def _spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Create a tracked task. `k0_tenant` cancels and awaits every tracked task before its
    cleanup, on success, failure or setup error (Codex B5.2 v1 finding 6)."""

    task = asyncio.create_task(coro)
    _LIVE_TASKS.append(task)
    return task


async def _end_tracked_tasks() -> list[str]:
    """Cancel (never release) every tracked task and await it. A paused attempt is not
    resumed during cleanup, so cleanup runs no step. Returns what each task ended with."""

    tasks, ended = list(_LIVE_TASKS), []
    _LIVE_TASKS.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    results = (
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), BARRIER_WAIT_SECONDS)
        if tasks
        else []
    )
    for result in results:
        ended.append(type(result).__name__ if isinstance(result, BaseException) else "returned")
    return ended


# --- exact Φ and role state, derived from sources, not from the classifier --


def _owner_default(admin: str, objtype: str, owner: str) -> set[str]:
    """`acldefault(objtype, owner)` restricted to the owner's own tuple (arch v6 r3 §4).
    Computed by PostgreSQL, independent of the classifier under test (PG17: MAINTAIN)."""

    return {
        str(p)
        for (p,) in _admin_rows(
            admin,
            'SELECT a.privilege_type FROM aclexplode(acldefault(%s::"char", %s::regrole)) a '
            "WHERE a.grantee = %s::regrole",
            (objtype, owner, owner),
        )
    }


def _schema_acl(admin: str, schema_key: str) -> set[tuple[str, str, str, bool]]:
    return {
        (str(g), str(p), str(gr), bool(o))
        for g, p, gr, o in _admin_rows(
            admin,
            """
            SELECT COALESCE(g.rolname, 'PUBLIC'), a.privilege_type, gr.rolname, a.is_grantable
              FROM pg_namespace n
              CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a
              LEFT JOIN pg_roles g ON g.oid = a.grantee
              LEFT JOIN pg_roles gr ON gr.oid = a.grantor
             WHERE n.nspname = %s
            """,
            (schema_key,),
        )
    }


def _phi_actual(admin: str, schema_key: str) -> dict[str, Any]:
    """Every ACL-bearing tuple of the tenant schema, with grantor and grant option."""

    s = schema_key
    return {
        "schema_owner": _admin_one(
            admin, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (s,)
        )[0],
        "schema": _schema_acl(admin, s),
        "relations": {
            (str(n), str(k), str(o))
            for n, k, o in _admin_rows(
                admin,
                "SELECT c.relname, c.relkind::text, c.relowner::regrole::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','S','f')",
                (s,),
            )
        },
        "tables": {
            (str(t), str(g), str(p), str(gr), bool(o))
            for t, g, p, gr, o in _admin_rows(
                admin,
                "SELECT c.relname, COALESCE(g.rolname, 'PUBLIC'), a.privilege_type, gr.rolname, "
                "a.is_grantable "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee LEFT JOIN pg_roles gr ON gr.oid = "
                "a.grantor "
                "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','S','f')",
                (s,),
            )
        },
        "columns": {
            (str(t), str(col), str(g), str(p), str(gr), bool(o))
            for t, col, g, p, gr, o in _admin_rows(
                admin,
                "SELECT c.relname, a.attname, COALESCE(g.rolname, 'PUBLIC'), x.privilege_type, "
                "gr.rolname, "
                "x.is_grantable FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace CROSS JOIN LATERAL "
                "aclexplode(a.attacl) x "
                "LEFT JOIN pg_roles g ON g.oid = x.grantee LEFT JOIN pg_roles gr ON gr.oid = "
                "x.grantor "
                "WHERE n.nspname = %s AND a.attacl IS NOT NULL",
                (s,),
            )
        },
        "functions": {
            (str(f), str(ow), str(g), str(p), str(gr), bool(o))
            for f, ow, g, p, gr, o in _admin_rows(
                admin,
                "SELECT p.proname, p.proowner::regrole::text, COALESCE(g.rolname, 'PUBLIC'), "
                "a.privilege_type, "
                "gr.rolname, a.is_grantable FROM pg_proc p JOIN pg_namespace n ON n.oid = "
                "p.pronamespace "
                "CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee LEFT JOIN pg_roles gr ON gr.oid = "
                "a.grantor "
                "WHERE n.nspname = %s",
                (s,),
            )
        },
        "default_acl": {
            (str(r), str(t), str(g), str(p), str(gr), bool(o))
            for r, t, g, p, gr, o in _admin_rows(
                admin,
                "SELECT d.defaclrole::regrole::text, d.defaclobjtype::text, COALESCE(g.rolname, "
                "'PUBLIC'), "
                "a.privilege_type, gr.rolname, a.is_grantable FROM pg_default_acl d "
                "JOIN pg_namespace n ON n.oid = d.defaclnamespace CROSS JOIN LATERAL "
                "aclexplode(d.defaclacl) a "
                "LEFT JOIN pg_roles g ON g.oid = a.grantee LEFT JOIN pg_roles gr ON gr.oid = "
                "a.grantor "
                "WHERE n.nspname = %s",
                (s,),
            )
        },
    }


def _phi_expected(admin: str, phase: str) -> dict[str, Any]:
    """Exact expected state for Φ0/Φ1/Φ2, written from arch v6 r3 §4 (table rows), the
    t001/t002 sources (objects, default privileges; m01/provisioning/units.py:578-602,
    m02/units.py:69-112) and D13 (schema owner P). Owner entries are owner-default via
    `acldefault`. Grant options: none of the listed GRANTs carries WITH GRANT OPTION."""

    m, p, rt, pj, lo = (
        MIGRATOR_ROLE,
        PROVISIONER_ROLE,
        RUNTIME_ROLE,
        PROJECTOR_ROLE,
        LOCK_OWNER_ROLE,
    )
    outbox, registry, rejector = (
        "access_audit_outbox",
        "operation_registry",
        "operation_registry_reject",
    )
    owner_table = _owner_default(admin, "r", m)
    schema = {(p, priv, p, False) for priv in _owner_default(admin, "n", p)} | {
        (m, "USAGE", p, False),
        (m, "CREATE", p, False),
    }
    if phase in ("Φ0", "Φ1"):
        schema |= {(rt, "USAGE", p, False), (pj, "USAGE", p, False)}
    tables = {(t, m, priv, m, False) for t in (outbox, registry) for priv in owner_table}
    if phase == "Φ0":
        tables |= {
            (outbox, rt, priv, m, False) for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        tables |= {(outbox, pj, "SELECT", m, False), (registry, rt, "SELECT", m, False)}
    return {
        "schema_owner": p,
        "schema": schema,
        "relations": {(outbox, "r", m), (registry, "r", m)},  # no sequences [C], no t003
        "tables": tables,
        "columns": {
            (registry, "operation_id", lo, "SELECT", m, False),
            (registry, "correlation_id", lo, "UPDATE", m, False),
        },
        "functions": {
            (rejector, m, m, "EXECUTE", m, False)
        },  # t002 revokes PUBLIC; no t003 gateway
        "default_acl": {
            (m, "r", rt, priv, m, False) for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
        | {(m, "S", rt, priv, m, False) for priv in ("USAGE", "SELECT")},
    }


def _assert_phi(admin: str, schema_key: str, phase: str, case: str, *, setup: bool = False) -> None:
    actual, expected = _phi_actual(admin, schema_key), _phi_expected(admin, phase)
    for key in expected:
        if actual[key] != expected[key]:
            message = f"{case}: {phase} {key} exact"
            if setup:
                raise SetupError(message)
            raise AssertionError(message)


def _role_state(admin: str) -> dict[str, Any]:
    edges = {
        (str(r), str(m), bool(a), bool(i), bool(s))
        for r, m, a, i, s in _admin_rows(
            admin,
            "SELECT r.rolname, m.rolname, am.admin_option, am.inherit_option, am.set_option "
            "FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.roleid JOIN pg_roles m ON m.oid "
            "= am.member "
            "WHERE r.rolname = ANY(%s) AND m.rolname = ANY(%s)",
            (list(CONTROLLED_ROLES), list(CONTROLLED_ROLES)),
        )
    }
    attributes = {
        str(r[0]): tuple(r[1:])
        for r in _admin_rows(
            admin,
            "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, "
            "rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(CONTROLLED_ROLES),),
        )
    }
    return {"edges": edges, "attributes": attributes}


# The only edge among the controlled roles: 004's (role, member, admin, inherit, set).
EXPECTED_EDGES = {(LOCK_OWNER_ROLE, MIGRATOR_ROLE, False, False, True)}


def _assert_role_state(admin: str, case: str) -> None:
    state = _role_state(admin)
    if set(state["attributes"]) != set(CONTROLLED_ROLES):
        raise SetupError(f"{case}: controlled roles present")
    if state["edges"] != EXPECTED_EDGES:
        raise SetupError(f"{case}: membership edges among controlled roles are exactly 004's")
    for role, (superuser, _inherit, createrole, createdb, login, replication, bypassrls) in state[
        "attributes"
    ].items():
        if superuser or createrole or createdb or login or replication or bypassrls:
            raise SetupError(f"{case}: {role} attributes are NOLOGIN and unprivileged")


# --- the historical K0 tenant (owner record de5024f1…508c, Q-B3 (a)) ---------


def _historical_manifest() -> Any:
    """The shipped provisioning manifest minus the lock-owner execution profile and its
    membership, exactly as B5.1 v3 §5.4. Test-only; never passed to production code."""

    from importlib import resources

    from haloflow.m01.provisioning.manifest import (
        MANIFEST_PACKAGE,
        PROVISIONING_MANIFEST,
        load_provisioning_manifest,
    )

    shipped = json.loads(
        resources.files(MANIFEST_PACKAGE).joinpath(PROVISIONING_MANIFEST).read_text()
    )
    document = dict(shipped)
    document["execution_role_profiles"] = {}
    document["role_memberships"] = []
    return load_provisioning_manifest(document)


def _target_two_registry() -> Any:
    from haloflow.m01.provisioning.units import TENANT_MIGRATIONS, build_tenant_migration_registry
    from haloflow.m02.units import T002_MIGRATION_ID, T002_SQL

    return build_tenant_migration_registry(TENANT_MIGRATIONS, {T002_MIGRATION_ID: T002_SQL})


def _assert_k0(admin: str, tenant_id: str, schema_key: str) -> None:
    """Independent checks before a tenant is called K0 (Q-B3: Φ0, ledger incl. production
    checksums, schema, role state). Every mismatch is a SetupError."""

    from haloflow.composition import build_production_tenant_migrations

    state = _admin_one(
        admin,
        "SELECT lifecycle_state, schema_version FROM shared.tenants WHERE tenant_id = %s",
        (tenant_id,),
    )
    if state != ("active", 2):
        raise SetupError(f"K0 registry {state!r}")
    _assert_phi(admin, schema_key, "Φ0", "K0", setup=True)
    _assert_role_state(admin, "K0")
    production = {u.migration_id: u.checksum for u in build_production_tenant_migrations()}
    ledger = {
        str(m): (str(c), str(s))
        for m, c, s in _admin_rows(
            admin,
            "SELECT migration_id, checksum, state FROM shared.schema_migrations WHERE tenant_id = "
            "%s",
            (tenant_id,),
        )
    }
    expected = {m: (production[m], "applied") for m in production if not m.startswith("t003")}
    if ledger != expected:
        raise SetupError("K0 ledger is not t001/t002 applied at production checksums, t003 absent")
    if _admin_rows(admin, f"SELECT 1 FROM {OPS} WHERE tenant_id = %s", (tenant_id,)):
        raise SetupError("K0 must have no operation row")
    if _admin_rows(admin, f"SELECT 1 FROM {ATTEMPTS} WHERE tenant_id = %s", (tenant_id,)):
        raise SetupError("K0 must have no attempt evidence")


@dataclass
class Tenant:
    tenant_id: str
    schema_key: str


def _tenant_identity(label: str, sign: str | None = None) -> tuple[str, str]:
    """Deterministic synthetic identity; with `sign`, the first salt whose
    `tenant_lock_key` has that sign (F07, F06a-x1/x2)."""

    from haloflow.m01.provisioning.runner import tenant_lock_key

    for salt in range(1000):
        digest = uuid5(NAMESPACE_URL, f"haloflow-test:l6cp3:{label}:{salt}").hex[:8]
        tenant_id = f"clinic-l{digest}"
        key = tenant_lock_key(tenant_id)
        if sign is None or (sign == "positive" and key > 0) or (sign == "negative" and key < 0):
            return tenant_id, f"tenant_l{digest}"
    raise SetupError("no synthetic tenant with the requested key sign")


async def _provision_k0(
    m02: ModuleType,
    m02_ids: Any,
    m02_reset: Callable[[tuple[str, str]], None],
    admin: str,
    tenant: tuple[str, str],
) -> None:
    try:
        _purge(admin, tenant[0])
        m02_reset(tenant)
        await m02.provision(
            m02_ids,
            _target_two_registry(),
            tenant,
            supported=range(1, 3),
            manifest=_historical_manifest(),
        )
    except SetupError:
        raise
    except Exception as error:  # a fixture failure is never read as a pre-change D-sym result
        raise SetupError(f"K0 provisioning failed: {type(error).__name__}") from error
    _assert_k0(admin, *tenant)


@pytest.fixture
async def k0_tenant(
    request: pytest.FixtureRequest,
    m02: ModuleType,
    m02_ids: Any,
    m02_reset: Callable[[tuple[str, str]], None],
    migrated_database: str,
) -> AsyncIterator[Tenant]:
    """Imports no CP-3 symbol. Teardown order: end tracked tasks, then purge (H1)."""

    tenant = _tenant_identity(request.node.nodeid, getattr(request, "param", None))
    _LIVE_TASKS.clear()
    await _provision_k0(m02, m02_ids, m02_reset, migrated_database, tenant)
    try:
        yield Tenant(*tenant)
    finally:
        ended = await _end_tracked_tasks()
        if ended:
            print(f"L6_CLEANUP_TASKS={ended}")
        _purge(migrated_database, tenant[0])
        m02_reset(tenant)


# --- composition and direct sessions ----------------------------------------


def _factory(
    conninfo: str, log: list[tuple[str, ...]] | None = None, label: str = ""
) -> Callable[[], Awaitable[AsyncConnection[Any]]]:
    """Connection factory handed to the composition. With `log`, it passively records each
    request for a connection (before connecting) and changes nothing else."""

    async def _connect() -> AsyncConnection[Any]:
        if log is not None:
            log.append(("connect", label))
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


def _compose(
    role_logins: dict[str, str],
    hooks: Any = None,
    *,
    lock_timeout: float = T_LOCK_SECONDS,
    connect_log: list[tuple[str, ...]] | None = None,
) -> Any:
    """The CP-3 upgrade through `compose_tenant_upgrade` with the target-3 production registry.
    Hooks are the reviewed test seams of B5.1 v3 §4.4 only. `connect_log` (A03-c) passively
    records connection requests through the existing dependency factories."""

    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m01.runtime import TenantUpgradeDependencies, compose_tenant_upgrade

    dependencies = TenantUpgradeDependencies(
        provisioner_connect=_factory(role_logins[PROVISIONER_ROLE], connect_log, "P"),
        migrator_connect=_factory(role_logins[MIGRATOR_ROLE], connect_log, "M"),
        lock_timeout_seconds=lock_timeout,
    )
    return compose_tenant_upgrade(
        dependencies, registry=build_production_tenant_migrations(), hooks=hooks
    )


@asynccontextmanager
async def _p_session(role_logins: dict[str, str]) -> AsyncIterator[AsyncConnection[Any]]:
    """A provisioner session as the application uses it (login shim, SET ROLE)."""

    conn = await AsyncConnection.connect(role_logins[PROVISIONER_ROLE], autocommit=True)
    try:
        await conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(PROVISIONER_ROLE)))
        yield conn
    finally:
        await conn.close()


async def _backend_pid(conn: AsyncConnection[Any]) -> int:
    row = await (await conn.execute("SELECT pg_backend_pid()")).fetchone()
    assert row is not None
    return int(row[0])


def _login(conninfo: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(conninfo)["user"])


# --- refusal expectations (Codex B5.2 v1 finding 4: pinned sites) -----------


async def _refused(awaitable: Awaitable[Any], case: str) -> Any:
    """Await; return the MaintenanceRefused. A normal return, or the pinned CP-3 boundary
    stop, fails here with the mapped message `<case>: did not refuse[ (reached the CP-3
    boundary)]`. Any other exception propagates unchanged, so it is never accepted as the
    refusal (and is never a mapped kill)."""

    from haloflow.m01.provisioning.upgrade import CheckpointBoundaryReached, MaintenanceRefused

    try:
        await awaitable
    except MaintenanceRefused as refused:
        return refused
    except (
        CheckpointBoundaryReached
    ) as boundary:  # the pinned "proceeded" outcome, not an arbitrary error
        raise AssertionError(f"{case}: did not refuse (reached the CP-3 boundary)") from boundary
    raise AssertionError(f"{case}: did not refuse")


def _refused_sync(call: Callable[[], Any], case: str) -> Any:
    from haloflow.m01.provisioning.upgrade import MaintenanceRefused

    try:
        call()
    except MaintenanceRefused as refused:
        return refused
    raise AssertionError(f"{case}: did not refuse")


def _assert_code(error: Any, code: str, phase: str, case: str) -> None:
    assert (error.reason_code, error.phase) == (code, phase), f"{case}: code/phase"


async def _fence_tx(conn: AsyncConnection[Any], cap: Any, k: Any) -> Any:
    from haloflow.m01.provisioning.upgrade import fence

    async with conn.transaction():
        return await fence(conn, cap, k)


async def _claim_tx(conn: AsyncConnection[Any], **kwargs: Any) -> None:
    """v7 (owner record 5c630668…8ea2): every direct claim passes the target-3
    production registry, because `claim` applies the full §8 classified-state predicate
    on every invocation and refuses (RC-08) without a registry (Codex v2 review)."""

    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m01.provisioning.upgrade import claim

    kwargs.setdefault("registry", build_production_tenant_migrations())
    async with conn.transaction():
        await claim(conn, **kwargs)


# --- evidence readers -------------------------------------------------------


def _events(admin: str, tenant_id: str, attempt_id: UUID | None = None) -> list[dict[str, Any]]:
    query = (
        f"SELECT event_id, attempt_id, maintenance_operation_id, event, detail FROM {ATTEMPTS} "
        "WHERE tenant_id = %s"
        + (" AND attempt_id = %s" if attempt_id else "")
        + " ORDER BY occurred_at, event_id"
    )
    params: tuple[Any, ...] = (tenant_id, attempt_id) if attempt_id else (tenant_id,)
    return [
        {"event_id": i, "attempt": a, "operation": o, "event": e, "detail": d}
        for i, a, o, e, d in _admin_rows(admin, query, params)
    ]


def _refusals(admin: str, tenant_id: str, attempt_id: UUID | None = None) -> list[dict[str, Any]]:
    return [e for e in _events(admin, tenant_id, attempt_id) if e["event"] == "attempt_refused"]


def _assert_one_refusal(
    admin: str,
    tenant_id: str,
    attempt_id: UUID,
    code: str,
    phase: str,
    operation: UUID | None,
    case: str,
) -> None:
    found = _refusals(admin, tenant_id, attempt_id)
    assert len(found) == 1, f"{case}: exactly one attempt_refused"
    assert found[0]["detail"] == {"code": code, "phase": phase}, f"{case}: refusal detail"
    assert found[0]["operation"] == operation, f"{case}: association"


def _operation(admin: str, tenant_id: str) -> tuple[Any, ...] | None:
    rows = _admin_rows(
        admin,
        f"SELECT maintenance_operation_id, state, current_attempt, neutralization_generation "
        f"FROM {OPS} WHERE tenant_id = %s AND state NOT IN ('released','abandoned')",
        (tenant_id,),
    )
    return rows[0] if rows else None


def _operation_id(admin: str, tenant_id: str) -> UUID:
    row = _operation(admin, tenant_id)
    if row is None:
        raise SetupError("no open operation")
    return UUID(str(row[0]))


def _current_attempt(admin: str, operation_id: UUID) -> UUID:
    return UUID(
        str(
            _admin_one(
                admin,
                f"SELECT current_attempt FROM {OPS} WHERE maintenance_operation_id = %s",
                (operation_id,),
            )[0]
        )
    )


def _attempts_in_order(admin: str, tenant_id: str) -> list[UUID]:
    return [
        UUID(str(r[0]))
        for r in _admin_rows(
            admin,
            f"SELECT attempt_id FROM {ATTEMPTS} WHERE tenant_id = %s AND event = 'started' ORDER "
            "BY occurred_at, event_id",
            (tenant_id,),
        )
    ]


# --- R5 "nothing changed" snapshot (test cases v4 §0 R5; Codex B5.2 v1 finding 2) --


def _business_contents(admin: str, schema_key: str) -> list[tuple[str, int, str]]:
    """Per tenant table: row count and an md5 over every row's text, ordered (R-L6.9)."""

    tables = [
        str(r[0])
        for r in _admin_rows(
            admin,
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p') ORDER BY 1",
            (schema_key,),
        )
    ]
    out = []
    for table in tables:
        count, digest = _admin_one(
            admin,
            sql.SQL(
                "SELECT count(*), md5(COALESCE(string_agg(t::text, E'\\n' ORDER BY t::text), '')) "
                "FROM {}.{} t"
            ).format(sql.Identifier(schema_key), sql.Identifier(table)),
        )
        out.append((table, int(count), str(digest)))
    return out


def _r5_snapshot(admin: str, tenant: Tenant) -> dict[str, Any]:
    t, s = tenant.tenant_id, tenant.schema_key
    return {
        "registry": _admin_rows(admin, "SELECT * FROM shared.tenants WHERE tenant_id = %s", (t,)),
        "history": _admin_rows(
            admin,
            "SELECT * FROM shared.tenant_state_history WHERE tenant_id = %s ORDER BY event_id",
            (t,),
        ),
        "phi": _phi_actual(admin, s),
        "function_definitions": _admin_rows(
            admin,
            "SELECT p.proname, p.prosecdef, p.proconfig, pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s ORDER BY 1",
            (s,),
        ),
        "triggers": _admin_rows(
            admin,
            "SELECT c.relname, tg.tgname, tg.tgenabled::text, tg.tgfoid::regproc::text, "
            "pg_get_triggerdef(tg.oid, true) FROM pg_trigger tg "
            "JOIN pg_class c ON c.oid = tg.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND NOT tg.tgisinternal ORDER BY 1, 2",
            (s,),
        ),
        "roles": _role_state(admin),
        "ledger": _admin_rows(
            admin,
            "SELECT * FROM shared.schema_migrations WHERE tenant_id = %s ORDER BY migration_id",
            (t,),
        ),
        "operation": _admin_rows(
            admin, f"SELECT * FROM {OPS} WHERE tenant_id = %s ORDER BY 1", (t,)
        ),
        "withheld": _admin_rows(
            admin,
            f"SELECT * FROM {WITHHELD} WHERE maintenance_operation_id IN "
            f"(SELECT maintenance_operation_id FROM {OPS} WHERE tenant_id = %s) ORDER BY 1",
            (t,),
        ),
        "attempts": {
            r[0]: r
            for r in _admin_rows(
                admin,
                "SELECT event_id, attempt_id, maintenance_operation_id, tenant_id, event, detail, "
                "occurred_at "
                f"FROM {ATTEMPTS} WHERE tenant_id = %s",
                (t,),
            )
        },
        "business": _business_contents(admin, s),
    }


def _assert_r5(
    before: dict[str, Any], after: dict[str, Any], case: str, *, new_events: list[str]
) -> list[Any]:
    """Everything is identical except the attempts table, whose old rows must all still be
    present and unchanged, and whose new rows must be exactly `new_events` (a multiset).
    Returns the new attempt rows."""

    for key in before:
        if key != "attempts":
            assert after[key] == before[key], f"{case}: R5 {key} unchanged"
    old, now = before["attempts"], after["attempts"]
    assert all(now.get(i) == row for i, row in old.items()), (
        f"{case}: R5 old attempt rows preserved"
    )
    added = [row for i, row in now.items() if i not in old]
    names = Counter(_event_name(row) for row in added)
    assert names == Counter(new_events), f"{case}: R5 new evidence exactly {sorted(new_events)}"
    return added


def _event_name(row: tuple[Any, ...]) -> str:
    """Rows of the R5 attempts read: (event_id, attempt_id, maintenance_operation_id,
    tenant_id, event, detail, occurred_at), named explicitly in the query."""

    return str(row[4])


def _new_rows_for(added: list[Any], attempt_id: UUID) -> list[str]:
    return [_event_name(r) for r in added if r[1] == attempt_id]


# --- real-session barriers (R6: no sleeps as ordering) ----------------------


async def _wait_until(predicate: Callable[[], bool], what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BARRIER_WAIT_SECONDS
    while not predicate():
        if loop.time() > deadline:
            raise SetupError(f"barrier timed out: {what}")
        await asyncio.sleep(POLL_SECONDS)  # polling interval only; ordering is the predicate


async def _eventually(predicate: Callable[[], bool]) -> bool:
    """Bounded poll that returns the final truth value, for assertions (not a setup barrier)."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + BARRIER_WAIT_SECONDS
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(POLL_SECONDS)
    return True


def _namespace_holders(admin: str) -> dict[int, tuple[str, str]]:
    """Granted advisory locks in the migration namespace, in this database:
    pid -> (login, database). Matches classid and objsubid only, never the objid encoding
    of a negative key (that encoding is what TC-F07 tests)."""

    from haloflow.m01.provisioning.runner import MIGRATION_LOCK_NAMESPACE

    return {
        int(pid): (str(user), str(db))
        for pid, user, db in _admin_rows(
            admin,
            "SELECT l.pid, a.usename, a.datname FROM pg_locks l JOIN pg_stat_activity a ON a.pid = "
            "l.pid "
            "WHERE l.locktype = 'advisory' AND l.classid = %s::oid AND l.objsubid = 2 AND "
            "l.granted "
            "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            (MIGRATION_LOCK_NAMESPACE,),
        )
    }


def _advisory_held_by(admin: str, pid: int) -> bool:
    return pid in _namespace_holders(admin)


def _blocked_by(admin: str, waiter_pid: int, blocker_pid: int) -> bool:
    return bool(
        _admin_one(admin, "SELECT %s = ANY(pg_blocking_pids(%s))", (blocker_pid, waiter_pid))[0]
    )


def _backend_gone(admin: str, pid: int) -> bool:
    return not _admin_rows(admin, "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,))


# --- driving the outer attempt ----------------------------------------------


@dataclass
class Halted:
    """An outer attempt paused by `pause_after[step]`, as a tracked task. `k_pid` is the one
    migration-namespace holder that appeared, as the migrator login, between the start of
    this attempt and its pause (identity, not test order; Codex B5.2 v1 finding 6)."""

    task: asyncio.Task[Any]
    reached: asyncio.Event
    release: asyncio.Event
    k_pid: int | None = None
    holders_before: set[int] = field(default_factory=set)

    async def cancel(self) -> None:
        self.task.cancel()
        try:  # noqa: SIM105
            await self.task
        except asyncio.CancelledError:
            pass

    async def resume(self) -> Any:
        self.release.set()
        return await self.task


async def _start_paused(
    role_logins: dict[str, str],
    admin: str,
    step: str,
    request: Any,
    *,
    wait: bool,
    connect_log: list[tuple[str, ...]] | None = None,
    **extra_hooks: Any,
) -> Halted:
    from haloflow.m01.provisioning.upgrade import UpgradeTestHooks

    reached, release = asyncio.Event(), asyncio.Event()

    async def _pause() -> None:
        reached.set()
        await release.wait()

    pauses = dict(extra_hooks.pop("pause_after", {}))
    pauses[step] = _pause
    holders_before = set(_namespace_holders(admin))
    upgrade = _compose(
        role_logins, UpgradeTestHooks(pause_after=pauses, **extra_hooks), connect_log=connect_log
    )
    halted = Halted(_spawn(upgrade.run(request)), reached, release, holders_before=holders_before)
    if wait:
        await _wait_paused(halted, step)
        _record_k_identity(role_logins, admin, halted)
    return halted


async def _wait_paused(halted: Halted, step: str) -> None:
    waiter = asyncio.create_task(halted.reached.wait())
    done, _ = await asyncio.wait(
        {halted.task, waiter}, timeout=BARRIER_WAIT_SECONDS, return_when=asyncio.FIRST_COMPLETED
    )
    if waiter not in done:
        waiter.cancel()
        if halted.task in done:
            halted.task.result()  # surfaces the real error
        raise SetupError(f"attempt did not reach pause after {step}")


def _record_k_identity(role_logins: dict[str, str], admin: str, halted: Halted) -> None:
    migrator = _login(role_logins[MIGRATOR_ROLE])
    new = {
        pid
        for pid, (user, _db) in _namespace_holders(admin).items()
        if pid not in halted.holders_before and user == migrator
    }
    halted.k_pid = new.pop() if len(new) == 1 else None


async def _halt_after(
    role_logins: dict[str, str], admin: str, step: str, request: Any, **extra_hooks: Any
) -> Halted:
    """Start `run(request)` and pause after `step` commits; returns once paused."""

    return await _start_paused(role_logins, admin, step, request, wait=True, **extra_hooks)


async def _close_halted_k(admin: str, halted: Halted, role_logins: dict[str, str]) -> int:
    """K-loss harness for a paused outer attempt (README §5 H5; conditional owner approval,
    record 03ae4897…1393). The identity is inferred, not bound to the K handle, so the test
    establishes isolation itself: there was no namespace holder in this database when the
    attempt started, exactly one new candidate appeared, and immediately before
    termination that pid is still the only holder, as the migrator login. Anything else
    is a setup failure and nothing is terminated."""

    pid = halted.k_pid
    if halted.holders_before:
        raise SetupError("H5: a competing namespace holder existed when the attempt started")
    if pid is None:
        raise SetupError(
            "H5: the paused attempt's K session was not identified (zero or several candidates)"
        )
    holders = _namespace_holders(admin)
    if set(holders) != {pid} or holders[pid][0] != _login(role_logins[MIGRATOR_ROLE]):
        raise SetupError(
            "H5: the identified K session is not the only, migrator-owned namespace holder"
        )
    if not _admin_one(admin, "SELECT pg_terminate_backend(%s)", (pid,))[0]:
        raise SetupError("termination of the K session was not delivered")
    await _wait_until(lambda: _backend_gone(admin, pid), "halted K backend gone")
    return pid


async def _to_state(role_logins: dict[str, str], admin: str, tenant: Tenant, state: str) -> UUID:
    """Bring a K0 tenant to K1, K2 or K3 with the real controller, then stop that attempt.
    Returns the operation id. The stopping cancellation is a harness action."""

    from haloflow.m01.provisioning.upgrade import UpgradeRequest

    step = {"K1": "C1", "K2": "C2", "K3": "C3"}[state]
    halted = await _halt_after(
        role_logins, admin, step, UpgradeRequest(tenant_id=tenant.tenant_id, operation_id=None)
    )
    await halted.cancel()
    op = _operation(admin, tenant.tenant_id)
    if op is None:
        raise SetupError(f"no operation after {step}")
    expected = {"K1": "establishing", "K2": "establishing", "K3": "entered"}[state]
    if op[1] != expected:
        raise SetupError(f"{state}: operation state {op[1]!r}")
    return UUID(str(op[0]))


async def _capability_for(
    conn: AsyncConnection[Any], tenant_id: str, operation_id: UUID, attempt_id: UUID, k: Any
) -> Any:
    from haloflow.m01.provisioning.runner import tenant_lock_key
    from haloflow.m01.provisioning.upgrade import MaintenanceCapability

    row = await (await conn.execute("SELECT current_database()")).fetchone()
    assert row is not None
    return MaintenanceCapability(
        tenant_id=tenant_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        key=tenant_lock_key(tenant_id),
        k_pid=k.pid,
        database=str(row[0]),
    )


@asynccontextmanager
async def _k(role_logins: dict[str, str], tenant_id: str, **kwargs: Any) -> AsyncIterator[Any]:
    from haloflow.m01.provisioning.upgrade import hold_k_session

    async with hold_k_session(
        _factory(role_logins[MIGRATOR_ROLE]),
        tenant_id,
        lock_timeout_seconds=kwargs.pop("timeout", T_LOCK_SECONDS),
    ) as k:
        yield k


def _request(tenant: Tenant, operation_id: UUID | None) -> Any:
    from haloflow.m01.provisioning.upgrade import UpgradeRequest

    return UpgradeRequest(tenant_id=tenant.tenant_id, operation_id=operation_id)


# --- TC-F01 / TC-F06c: RC-01 ------------------------------------------------


async def test_tc_f01_component_wrong_attempt(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        cap = await _capability_for(conn, t.tenant_id, op, uuid4(), k)
        validate_capability(cap)
        error = await _refused(_fence_tx(conn, cap, k), "TC-F01 component")
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", "TC-F01 component")
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F01 component", new_events=[])


async def _stale_a_after_b_claims(
    role_logins: dict[str, str], admin: str, t: Tenant
) -> tuple[Halted, UUID, UUID, UUID]:
    """A pauses after C1 (live, holds K_A); K_A is terminated (identity-checked); B claims
    (CL) with its own K and commits. Returns (A halted, op, att_A, att_B)."""

    a = await _halt_after(role_logins, admin, "C1", _request(t, None))
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    await _close_halted_k(admin, a, role_logins)
    att_b = uuid4()
    async with _k(role_logins, t.tenant_id) as k_b, _p_session(role_logins) as conn:
        await _claim_tx(
            conn, form="CL", tenant_id=t.tenant_id, operation_id=op, new_attempt_id=att_b, k=k_b
        )
    if _current_attempt(admin, op) != att_b:
        raise SetupError("B's claim did not commit")
    return a, op, att_a, att_b


async def test_tc_f01_outer_wrong_attempt(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """IP-14 A3: RC-01, operation NULL (statement 2 returned no row; C1 created the row
    but is neither a statement-2 nor a claim match). Codex: consistent with A3 (Q-P1)."""

    admin, t = migrated_database, k0_tenant
    a, _op, att_a, _ = await _stale_a_after_b_claims(role_logins, admin, t)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-F01 outer")
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", "TC-F01 outer")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_FENCE_LOST", "fence", None, "TC-F01 outer"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F01 outer", new_events=["attempt_refused"])


async def test_tc_f06c_component_stale_a_refused(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    admin, t = migrated_database, k0_tenant
    a, op, att_a, _ = await _stale_a_after_b_claims(role_logins, admin, t)
    await a.cancel()
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        cap = await _capability_for(conn, t.tenant_id, op, att_a, k)
        validate_capability(cap)
        error = await _refused(_fence_tx(conn, cap, k), "TC-F06c component")
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", "TC-F06c component")
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F06c component", new_events=[])


async def test_tc_f06c_outer_stale_a_refused(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    a, op, att_a, att_b = await _stale_a_after_b_claims(role_logins, admin, t)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-F06c outer")
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", "TC-F06c outer")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_FENCE_LOST", "fence", None, "TC-F06c outer"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F06c outer", new_events=["attempt_refused"])
    assert _current_attempt(admin, op) == att_b, "TC-F06c outer: B remains current"


# --- TC-F02 / TC-F05: RC-02 -------------------------------------------------


async def test_tc_f02_component_k_closed(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    att = _current_attempt(admin, op)
    async with _k(role_logins, t.tenant_id) as k:
        stale_k = k
    await _wait_until(
        lambda: not _advisory_held_by(admin, stale_k.pid), "K's row gone from pg_locks"
    )
    before = _r5_snapshot(admin, t)
    async with _p_session(role_logins) as conn:
        cap = await _capability_for(conn, t.tenant_id, op, att, stale_k)
        validate_capability(cap)
        error = await _refused(_fence_tx(conn, cap, stale_k), "TC-F02 component")
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-F02 component")
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F02 component", new_events=[])


async def test_tc_f02_lock_held_by_other_pid(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Q-B5 / mutation M-F3-pid: the key is held, but by another backend."""

    from haloflow.m01.provisioning.upgrade import validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    att = _current_attempt(admin, op)
    async with _k(role_logins, t.tenant_id) as k:
        stale_k = k
    async with _k(role_logins, t.tenant_id) as other:
        if other.pid == stale_k.pid:
            raise SetupError("TC-F02 other pid: the second K session reused the pid")
        before = _r5_snapshot(admin, t)
        async with _p_session(role_logins) as conn:
            cap = await _capability_for(conn, t.tenant_id, op, att, stale_k)
            validate_capability(cap)
            error = await _refused(_fence_tx(conn, cap, stale_k), "TC-F02 other pid")
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-F02 other pid")
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F02 other pid", new_events=[])


async def test_tc_f02_outer_k_lost(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Statement 2 returned the row with matching binding before statement 3 failed, so the
    match is established in this fence: operation set (IP-14 A3; Q-P1, Codex: consistent)."""

    admin, t = migrated_database, k0_tenant
    a = await _halt_after(role_logins, admin, "C1", _request(t, None))
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    await _close_halted_k(admin, a, role_logins)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-F02 outer")
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-F02 outer")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_LOCK_LOST", "fence", op, "TC-F02 outer"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F02 outer", new_events=["attempt_refused"])


async def test_tc_f05_component_loss_after_statement_3(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """A-LK residual: K lost after statement 3 and before COMMIT; that COMMIT may succeed.
    Both COMMIT outcomes are recorded; the next F gets RC-02 either way."""

    from haloflow.m01.provisioning.upgrade import fence, validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    att = _current_attempt(admin, op)
    async with _p_session(role_logins) as conn:
        async with _k(role_logins, t.tenant_id) as k:
            cap = await _capability_for(conn, t.tenant_id, op, att, k)
            validate_capability(cap)
            await conn.execute("BEGIN")
            await fence(conn, cap, k)
            stale_k = k
        await _wait_until(
            lambda: not _advisory_held_by(admin, stale_k.pid), "K closed after statement 3"
        )
        try:
            await conn.execute("COMMIT")
            print("L6_F05_RESIDUAL_COMMIT=succeeded")
        except psycopg.Error:
            print("L6_F05_RESIDUAL_COMMIT=failed")
        before = _r5_snapshot(admin, t)
        validate_capability(cap)
        error = await _refused(_fence_tx(conn, cap, stale_k), "TC-F05 component")
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-F05 component")
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F05 component", new_events=[])


async def test_tc_f05_outer_attempt_stops(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Q-B5 (i): the approved "the attempt stops" obligation on the real controller path.
    Not loss between statement 3 and COMMIT (the component variant covers that)."""

    admin, t = migrated_database, k0_tenant
    a = await _halt_after(role_logins, admin, "C1", _request(t, None))
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    await _close_halted_k(admin, a, role_logins)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-F05 outer")
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-F05 outer")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_LOCK_LOST", "fence", op, "TC-F05 outer"
    )
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-F05 outer", new_events=["attempt_refused"]
    )
    assert _new_rows_for(added, att_a) == ["attempt_refused"], "TC-F05 outer: no further step ran"


# --- TC-F03b: RC-05 after statement 2, before any protected write ------------


@pytest.mark.parametrize("mismatch", ["tenant", "k_pid", "database"])
async def test_tc_f03b_component_binding_mismatch(
    mismatch: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Q-B1 order: validation passes (the capability is internally consistent), statement 2
    returns the row, then the row/binding check refuses with RC-05 before statement 3.
    The outer variants are `test_tc_f03b_outer_binding_mismatch` (Q-P2 (a))."""

    import dataclasses

    from haloflow.m01.provisioning.runner import tenant_lock_key
    from haloflow.m01.provisioning.upgrade import validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    att = _current_attempt(admin, op)
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        cap = await _capability_for(conn, t.tenant_id, op, att, k)
        if mismatch == "tenant":
            other = _tenant_identity("f03b-other")[0]
            cap = dataclasses.replace(cap, tenant_id=other, key=tenant_lock_key(other))
        elif mismatch == "k_pid":
            cap = dataclasses.replace(cap, k_pid=k.pid + 1)
        else:
            cap = dataclasses.replace(cap, database="haloflow_test_not_this_one")
        validate_capability(cap)
        error = await _refused(_fence_tx(conn, cap, k), f"TC-F03b {mismatch}")
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "fence", f"TC-F03b {mismatch}")
    _assert_r5(before, _r5_snapshot(admin, t), f"TC-F03b {mismatch}", new_events=[])


# --- capability_fault seam (owner record 250102df…073c, Q-P2/Q-P5 (a)) ----------
# Test composition only. The seam replaces the controller-minted capability for the
# next fenced step (C2 here, after a pause at C1): `missing_attempt` before
# validate_capability; the others as an internally consistent capability bound to the
# wrong value, reaching the real post-statement-2 binding check. It never changes the
# request's evidence identity, authorization or K handle, and skips no check.


async def _faulted_after_c1(
    role_logins: dict[str, str], admin: str, t: Tenant, fault: str, **observe: Any
) -> tuple[Halted, UUID, UUID]:
    a = await _halt_after(
        role_logins, admin, "C1", _request(t, None), capability_fault=fault, **observe
    )
    op = _operation_id(admin, t.tenant_id)
    return a, op, _current_attempt(admin, op)


@pytest.mark.parametrize("mismatch", ["tenant", "k_pid", "database"])
async def test_tc_f03b_outer_binding_mismatch(
    mismatch: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """TC-F03b outer (Q1 bundle): RC-05 / fence on the real controller path, one refusal
    for this attempt, nothing else changed. Association is credited to TC-A03-e only."""

    admin, t = migrated_database, k0_tenant
    case = f"TC-F03b outer {mismatch}"
    a, _op, att_a = await _faulted_after_c1(role_logins, admin, t, mismatch)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), case)
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "fence", case)
    added = _assert_r5(before, _r5_snapshot(admin, t), case, new_events=["attempt_refused"])
    assert [r[1] for r in added] == [att_a], f"{case}: the refusal is this attempt's"


# --- TC-F04: a token value alone ---------------------------------------------


@pytest.mark.parametrize("token", ["opaque-token", b"opaque-token", 42])
async def test_tc_f04_token_value_alone_refused(token: object) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    error = _refused_sync(lambda: validate_capability(token), f"TC-F04 {token!r}")
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "capability", f"TC-F04 {token!r}")


# --- TC-F06a and the K-helper contention variants (Q11) ---------------------


async def test_tc_f06a_successor_cannot_take_the_lock(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """A's next F passing is observed as A committing C2 and stopping at the CP-3
    boundary (Q-P8 note)."""

    from haloflow.m01.provisioning.upgrade import CheckpointBoundaryReached

    admin, t = migrated_database, k0_tenant
    a = await _halt_after(role_logins, admin, "C1", _request(t, None))
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    before = _r5_snapshot(admin, t)
    error = await _refused(_compose(role_logins, lock_timeout=2.0).run(_request(t, op)), "TC-F06a")
    _assert_code(error, "LOCK_UNAVAILABLE", "lock_acquire", "TC-F06a")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-F06a", new_events=["started", "attempt_refused"]
    )
    (att_b,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, att_b, "LOCK_UNAVAILABLE", "lock_acquire", None, "TC-F06a"
    )
    assert _current_attempt(admin, op) == att_a, "TC-F06a: no claim"
    try:
        await a.resume()  # A continues; it stops only at the CP-3 boundary after C3 (Q10)
    except CheckpointBoundaryReached:
        pass
    else:
        raise AssertionError("TC-F06a: A stopped at the CP-3 boundary")
    assert "l2_withheld" in [e["event"] for e in _events(admin, t.tenant_id, att_a)], (
        "TC-F06a: A's next F passed"
    )


@pytest.mark.parametrize("k0_tenant", ["positive", "negative"], indirect=True)
async def test_tc_f06a_x1_runner_lock_blocks_helper(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m01.provisioning.runner import TenantMigrationRunner

    admin, t = migrated_database, k0_tenant
    runner = TenantMigrationRunner(
        _factory(role_logins[MIGRATOR_ROLE]), build_production_tenant_migrations()
    )
    before = _r5_snapshot(admin, t)
    async with runner.tenant_lock(t.tenant_id):
        error = await _refused(
            _compose(role_logins, lock_timeout=2.0).run(_request(t, None)), "TC-F06a-x1"
        )
    _assert_code(error, "LOCK_UNAVAILABLE", "lock_acquire", "TC-F06a-x1")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-F06a-x1", new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, attempt, "LOCK_UNAVAILABLE", "lock_acquire", None, "TC-F06a-x1"
    )


@pytest.mark.parametrize("k0_tenant", ["positive", "negative"], indirect=True)
async def test_tc_f06a_x2_helper_blocks_runner_lock(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m01.errors import TenantMigrationFailed
    from haloflow.m01.provisioning.runner import TenantMigrationRunner

    admin, t = migrated_database, k0_tenant
    runner = TenantMigrationRunner(
        _factory(role_logins[MIGRATOR_ROLE]),
        build_production_tenant_migrations(),
        lock_timeout_seconds=2.0,
    )
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id):
        with pytest.raises(TenantMigrationFailed) as error:
            async with runner.tenant_lock(t.tenant_id):
                pass
    assert error.value.reason_code == "LOCK_UNAVAILABLE", "TC-F06a-x2: runner refused"
    _assert_r5(before, _r5_snapshot(admin, t), "TC-F06a-x2", new_events=[])


@pytest.mark.parametrize("fault", ["raise_after_lock", "cancel_after_lock"])
async def test_tc_f06a_x3_no_leaked_lock(
    fault: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Q-P4a (a): `raise_after_lock` surfaces InjectedFault unchanged."""

    from haloflow.m01.provisioning.upgrade import InjectedFault, UpgradeTestHooks

    admin, t = migrated_database, k0_tenant
    migrator = _login(role_logins[MIGRATOR_ROLE])

    def _migrator_backends() -> int:
        return len(
            _admin_rows(admin, "SELECT 1 FROM pg_stat_activity WHERE usename = %s", (migrator,))
        )

    baseline = _migrator_backends()
    upgrade = _compose(role_logins, UpgradeTestHooks(k_session_fault=fault))
    expected: type[BaseException] = (
        InjectedFault if fault == "raise_after_lock" else asyncio.CancelledError
    )
    task = _spawn(upgrade.run(_request(t, None)))
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    assert type(outcome) is expected, f"TC-F06a-x3 {fault}: the injected fault surfaced unchanged"
    assert await _eventually(lambda: _namespace_holders(admin) == {}), (
        f"TC-F06a-x3 {fault}: no leaked lock"
    )
    assert await _eventually(lambda: _migrator_backends() == baseline), (
        f"TC-F06a-x3 {fault}: K connection closed"
    )


# --- TC-F06b: successor claim waits on A's row lock --------------------------


@pytest.mark.parametrize(
    "schedule", ["schedule1_commit_within_T_lock", "schedule2_open_past_T_lock"]
)
async def test_tc_f06b_claim_waits_on_row_lock(
    schedule: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Attempts: A = the attempt that created the operation (continued by direct calls);
    B1, B2 = new outer attempts (owner record de5024f1…508c, Q-B5 (ii)). B attempts are
    halted after C2, which commits only after their claim did."""

    from haloflow.m01.provisioning.upgrade import fence, validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    att_a = _current_attempt(admin, op)
    async with _p_session(role_logins) as a_conn:
        a_pid = await _backend_pid(a_conn)
        async with _k(role_logins, t.tenant_id) as k_a:
            cap_a = await _capability_for(a_conn, t.tenant_id, op, att_a, k_a)
            validate_capability(cap_a)
            await a_conn.execute("BEGIN")
            await fence(a_conn, cap_a, k_a)  # A holds the row lock; transaction open
            stale_k_a = k_a
        await _wait_until(lambda: not _advisory_held_by(admin, stale_k_a.pid), "K_A lost")

        def _someone_blocked_by_a() -> bool:
            return bool(
                _admin_rows(
                    admin,
                    "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                    (a_pid,),
                )
            )

        try:
            if schedule.startswith("schedule1"):
                b1 = await _start_paused(role_logins, admin, "C2", _request(t, op), wait=False)
                await _wait_until(_someone_blocked_by_a, "B1's claim waits on A's row lock")
                await a_conn.execute("COMMIT")
                assert a_conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, (
                    "TC-F06b s1: A committed"
                )
                await _wait_paused(b1, "C2")
                (att_b1,) = [x for x in _attempts_in_order(admin, t.tenant_id) if x != att_a]
                assert _current_attempt(admin, op) == att_b1, "TC-F06b s1: B1's claim committed"
                b1_events = [e["event"] for e in _events(admin, t.tenant_id, att_b1)]
                assert b1_events == ["started", "claimed", "l2_withheld"], (
                    "TC-F06b s1: B1 events (to C2)"
                )
                await b1.cancel()
            else:
                before = _r5_snapshot(admin, t)
                b1_task = _spawn(_compose(role_logins, lock_timeout=2.0).run(_request(t, op)))
                await _wait_until(_someone_blocked_by_a, "B1's claim waits on A's row lock")
                error = await _refused(b1_task, "TC-F06b s2 B1")
                _assert_code(error, "LOCK_UNAVAILABLE", "claim", "TC-F06b s2 B1")
                added = _assert_r5(
                    before,
                    _r5_snapshot(admin, t),
                    "TC-F06b s2 B1",
                    new_events=["started", "attempt_refused"],
                )
                (att_b1,) = {r[1] for r in added}
                _assert_one_refusal(
                    admin, t.tenant_id, att_b1, "LOCK_UNAVAILABLE", "claim", None, "TC-F06b s2 B1"
                )
                await a_conn.execute("ROLLBACK")
                assert a_conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, (
                    "TC-F06b s2: A rolled back"
                )
                b2 = await _halt_after(role_logins, admin, "C2", _request(t, op))
                (att_b2,) = [
                    x for x in _attempts_in_order(admin, t.tenant_id) if x not in (att_a, att_b1)
                ]
                assert _current_attempt(admin, op) == att_b2, "TC-F06b s2: B2's claim committed"
                assert [e["event"] for e in _events(admin, t.tenant_id, att_b2)] == [
                    "started",
                    "claimed",
                    "l2_withheld",
                ], "TC-F06b s2: B2 events (to C2)"
                await b2.cancel()
        finally:
            if a_conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                await a_conn.execute("ROLLBACK")
        validate_capability(cap_a)
        stale = await _refused(_fence_tx(a_conn, cap_a, stale_k_a), f"TC-F06b {schedule} A")
    _assert_code(stale, "MAINTENANCE_FENCE_LOST", "fence", f"TC-F06b {schedule} A")


# --- TC-F07: both key signs --------------------------------------------------


@pytest.mark.parametrize("k0_tenant", ["positive", "negative"], indirect=True)
async def test_tc_f07_statement_3_finds_the_lock(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import MaintenanceRefused, validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    att = _current_attempt(admin, op)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        cap = await _capability_for(conn, t.tenant_id, op, att, k)
        validate_capability(cap)
        try:
            row = await _fence_tx(conn, cap, k)
        except MaintenanceRefused as refused:  # only a refusal is mapped; anything else propagates
            raise AssertionError(
                f"TC-F07: statement 3 found the lock (refused: {refused.reason_code})"
            ) from refused
    assert row is not None, "TC-F07: statement 3 found the lock"


# --- TC-F11: CL naming the wrong tenant (component; outer reachable node below, Q-P3 (a)) --


@pytest.mark.parametrize("state", ["K1", "K2", "K3"])
async def test_tc_f11_claim_naming_wrong_tenant(
    state: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, state)
    before = _r5_snapshot(admin, t)
    other = _tenant_identity("f11-other")[0]
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        error = await _refused(
            _claim_tx(
                conn, form="CL", tenant_id=other, operation_id=op, new_attempt_id=uuid4(), k=k
            ),
            f"TC-F11 {state}",
        )
    _assert_code(error, "MAINTENANCE_CLAIM_REFUSED", "claim", f"TC-F11 {state}")
    _assert_r5(before, _r5_snapshot(admin, t), f"TC-F11 {state}", new_events=[])


async def test_tc_f11_outer_g3_wrong_tenant_operation(
    m02: ModuleType,
    m02_ids: Any,
    m02_reset: Callable[[tuple[str, str]], None],
    k0_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """Q-P3 (a): an outer request naming another tenant's operation is refused at S0 by G3
    (RC-06 / classify), one attempt_refused, operation NULL, R5 on both tenants. A
    CLAIM_REFUSED/claim outer refusal for a wrong-tenant claim is unreachable by design."""

    admin, t = migrated_database, k0_tenant
    other = _tenant_identity("f11-outer-other")
    try:
        await _provision_k0(m02, m02_ids, m02_reset, admin, other)
        other_t = Tenant(*other)
        op_other = await _to_state(role_logins, admin, other_t, "K1")
        before, before_other = _r5_snapshot(admin, t), _r5_snapshot(admin, other_t)
        error = await _refused(_compose(role_logins).run(_request(t, op_other)), "TC-F11 outer G3")
        _assert_code(error, "MAINTENANCE_CASE_REFUSED", "classify", "TC-F11 outer G3")
        added = _assert_r5(
            before,
            _r5_snapshot(admin, t),
            "TC-F11 outer G3",
            new_events=["started", "attempt_refused"],
        )
        (attempt,) = {r[1] for r in added}
        _assert_one_refusal(
            admin,
            t.tenant_id,
            attempt,
            "MAINTENANCE_CASE_REFUSED",
            "classify",
            None,
            "TC-F11 outer G3",
        )
        _assert_r5(
            before_other,
            _r5_snapshot(admin, other_t),
            "TC-F11 outer G3 other tenant",
            new_events=[],
        )
    finally:
        await _end_tracked_tasks()
        _purge(admin, other[0])
        m02_reset(other)


async def _classify(role_logins: dict[str, str], tenant_id: str, operation_id: UUID | None) -> Any:
    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m01.provisioning.upgrade import classify

    async with _p_session(role_logins) as conn:
        return await classify(
            conn, tenant_id, operation_id, registry=build_production_tenant_migrations()
        )


# --- TC-K03: classifier read permission removed (whitelisted, plan v4 §4.1; H3) --


@contextmanager
def _attempts_select_revoked(admin: str) -> Iterator[None]:
    """H3/H3′: REVOKE SELECT on the attempts table from P, always restored and verified.
    The expected initial privilege is checked first; any other baseline is a setup failure."""

    if not _admin_one(
        admin, "SELECT has_table_privilege(%s, %s, 'SELECT')", (PROVISIONER_ROLE, ATTEMPTS)
    )[0]:
        raise SetupError("H3: P does not hold SELECT on the attempts table before the revoke")
    with _admin(admin) as conn:
        conn.execute(f"REVOKE SELECT ON {ATTEMPTS} FROM {PROVISIONER_ROLE}")
    try:
        yield
    finally:
        with _admin(admin) as conn:
            conn.execute(f"GRANT SELECT ON {ATTEMPTS} TO {PROVISIONER_ROLE}")
        if not _admin_one(
            admin, "SELECT has_table_privilege(%s, %s, 'SELECT')", (PROVISIONER_ROLE, ATTEMPTS)
        )[0]:
            raise SetupError("H3: privilege not restored")


async def test_tc_k03_unreadable_state_is_unknown(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Expected new evidence [started, attempt_refused]: Q-P9 (S0 result refused after S1; owner record 03ae4897…1393)."""  # noqa: E501

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    before = _r5_snapshot(admin, t)
    with _attempts_select_revoked(admin):
        error = await _refused(_compose(role_logins).run(_request(t, op)), "TC-K03")
    _assert_code(error, "MAINTENANCE_STATE_UNKNOWN", "classify", "TC-K03")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-K03", new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, attempt, "MAINTENANCE_STATE_UNKNOWN", "classify", None, "TC-K03"
    )


# --- TC-K05: PG17 owner-default includes MAINTAIN ---------------------------


async def test_tc_k05_owner_default_with_maintain_is_owner_default(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, k0_tenant
    if "MAINTAIN" not in _owner_default(admin, "r", MIGRATOR_ROLE):
        raise SetupError("TC-K05 needs a PG17 owner-default with MAINTAIN")
    owner_maintain = {
        (tb, g, p)
        for tb, g, p, _gr, _o in _phi_actual(admin, t.schema_key)["tables"]
        if g == MIGRATOR_ROLE and p == "MAINTAIN"
    }
    if owner_maintain != {
        ("access_audit_outbox", MIGRATOR_ROLE, "MAINTAIN"),
        ("operation_registry", MIGRATOR_ROLE, "MAINTAIN"),
    }:
        raise SetupError("TC-K05: owner MAINTAIN entries on both tables")
    c = await _classify(role_logins, t.tenant_id, None)
    assert c.state == KState.K0, "TC-K05: owner-default incl. MAINTAIN is not an extra tuple"


# --- TC-K07: G7 baselines (class T seed, whitelisted, plan v4 §4.1; H2) ------


def _seed_t003(admin: str, tenant_id: str, state: str, *, pre_c1: bool = True) -> None:
    """H2, class T ledger seed. `pre_c1` (TC-K07, A03-g1, A04): no L-6 marker and no
    operation may exist yet. TC-A05 seeds mid-attempt (after C2), so only the ledger row
    is checked. Uses outside TC-K07/TC-A05 are listed in README §5 for approval."""

    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m02.units import T003_MIGRATION_ID

    checksum = {u.migration_id: u.checksum for u in build_production_tenant_migrations()}[
        T003_MIGRATION_ID
    ]
    if pre_c1 and (
        _admin_rows(admin, f"SELECT 1 FROM {ATTEMPTS} WHERE tenant_id = %s", (tenant_id,))
        or _operation(admin, tenant_id)
    ):
        raise SetupError("seed: no L-6 marker and no operation before C1")
    with _admin(admin) as conn:
        conn.execute(
            "INSERT INTO shared.schema_migrations (tenant_id, migration_id, checksum, state, "
            "attempt) "
            "VALUES (%s, %s, %s, %s, 1)",
            (tenant_id, T003_MIGRATION_ID, checksum, state),
        )
    rows = _admin_rows(
        admin,
        "SELECT state, attempt, checksum FROM shared.schema_migrations WHERE tenant_id = %s AND "
        "migration_id = %s",
        (tenant_id, T003_MIGRATION_ID),
    )
    if rows != [(state, 1, checksum)]:
        raise SetupError("seed: exact ledger row")


async def test_tc_k07_failed_baseline_accepted(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, k0_tenant
    _seed_t003(admin, t.tenant_id, "failed")
    op = await _to_state(role_logins, admin, t, "K1")
    baseline = _admin_one(
        admin, f"SELECT t003_baseline FROM {OPS} WHERE maintenance_operation_id = %s", (op,)
    )[0]
    assert baseline == "failed:1", "TC-K07 baseline: C1 records failed:1"
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.K1, (
        "TC-K07 baseline: accepted at K1"
    )
    for step, state in (("C2", KState.K2), ("C3", KState.K3)):
        halted = await _halt_after(role_logins, admin, step, _request(t, op))
        await halted.cancel()
        assert (await _classify(role_logins, t.tenant_id, op)).state == state, (
            f"TC-K07 baseline: accepted at {state}"
        )
    assert not [e for e in _events(admin, t.tenant_id) if e["event"] == "apply_failed"], (
        "TC-K07: no apply_failed invented"
    )


async def test_tc_k07_unmarked_running_refused(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Expected new evidence [started, attempt_refused]: Q-P9."""

    admin, t = migrated_database, k0_tenant
    _seed_t003(admin, t.tenant_id, "running")
    before = _r5_snapshot(admin, t)
    error = await _refused(_compose(role_logins).run(_request(t, None)), "TC-K07 running")
    _assert_code(error, "MAINTENANCE_CASE_REFUSED", "classify", "TC-K07 running")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-K07 running", new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, attempt, "MAINTENANCE_CASE_REFUSED", "classify", None, "TC-K07 running"
    )


# --- TC-V09: AB (owner records 0273e77d…f32b, de5024f1…508c) ------------------


DECISION_SHA256 = "a" * 64  # synthetic attestation for tests (procedural trust gate, Q6)


def _authorization(tenant_id: str, op: UUID, **overrides: Any) -> Any:
    from haloflow.m01.provisioning.upgrade import OwnerAuthorization

    fields = {
        "decision_sha256": DECISION_SHA256,
        "tenant_id": tenant_id,
        "operation_id": op,
        "action": "abandon",
    }
    fields.update(overrides)
    return OwnerAuthorization(**fields)


async def test_tc_v09_k1_abandon(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import KState

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    outcome = await _compose(role_logins).abandon(_request(t, op), _authorization(t.tenant_id, op))
    assert outcome.final_state == KState.K0, "TC-V09 K1: back to K0"
    state = _admin_one(
        admin, f"SELECT state FROM {OPS} WHERE maintenance_operation_id = %s", (op,)
    )[0]
    assert state == "abandoned", "TC-V09 K1: operation closed"
    assert [e["event"] for e in _events(admin, t.tenant_id)].count("abandoned") == 1, (
        "TC-V09 K1: one abandoned event"
    )
    _assert_phi(admin, t.schema_key, "Φ0", "TC-V09 K1")


@pytest.mark.parametrize("state", ["K2", "K3"])
async def test_tc_v09_abandon_after_k1_refused(
    state: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Expected new evidence [started, attempt_refused]: Q-P9."""

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, state)
    before = _r5_snapshot(admin, t)
    error = await _refused(
        _compose(role_logins).abandon(_request(t, op), _authorization(t.tenant_id, op)),
        f"TC-V09 {state}",
    )
    _assert_code(error, "MAINTENANCE_CLAIM_REFUSED", "claim", f"TC-V09 {state}")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), f"TC-V09 {state}", new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, attempt, "MAINTENANCE_CLAIM_REFUSED", "claim", None, f"TC-V09 {state}"
    )


@pytest.mark.parametrize(
    "variant", ["missing", "malformed", "tenant_mismatch", "operation_mismatch", "action_mismatch"]
)
async def test_tc_v09_authorization_refused(
    variant: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Q6 two phases. Expected new evidence [started, attempt_refused]: Q-P9 (the
    authorization is checked after S0/S1 and before any step transaction)."""

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    auth = {
        "missing": None,
        "malformed": _authorization(t.tenant_id, op, decision_sha256="A" * 64),
        "tenant_mismatch": _authorization(_tenant_identity("v09-other")[0], op),
        "operation_mismatch": _authorization(t.tenant_id, uuid4()),
        "action_mismatch": _authorization(t.tenant_id, op, action="release"),
    }[variant]
    before = _r5_snapshot(admin, t)
    case = f"TC-V09 auth {variant}"
    error = await _refused(_compose(role_logins).abandon(_request(t, op), auth), case)
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "capability", case)
    added = _assert_r5(
        before, _r5_snapshot(admin, t), case, new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, attempt, "MAINTENANCE_TOKEN_INVALID", "capability", None, case
    )


# --- TC-N09 (clarified by owner record f3c8b5e9…03ed, b8 (a)) -----------------


async def test_tc_n09_runtime_pooled_session_refused_before_callback(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.composition import build_production_catalog
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.errors import RegistryInconsistent
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    admin, t = migrated_database, k0_tenant
    pool = TenantPool(role_logins[RUNTIME_ROLE], min_size=1, max_size=1)
    await pool.open()
    try:
        runtime = compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=_factory(role_logins[MIGRATOR_ROLE]),
                provisioner_connect=_factory(role_logins[PROVISIONER_ROLE]),
            ),
            registry=_target_two_registry(),
            catalog=build_production_catalog(),
            supported_schema_versions=frozenset({2}),
        )
        principal = Principal(
            kind=PrincipalKind.WORKLOAD,
            id="l6-n09",
            auth_method="test",
            authorized_tenant_ids=frozenset({t.tenant_id}),
            capabilities=frozenset({"probe:read"}),
        )
        context = await runtime.resolver.resolve(
            principal=principal,
            tenant_hint=t.tenant_id,
            purpose="operations",
            capabilities=frozenset({"probe:read"}),
            source=TrustedSource.WORKER,
            execution_id=uuid5(NAMESPACE_URL, f"l6-n09:{t.tenant_id}"),
            correlation_id=uuid5(NAMESPACE_URL, f"l6-n09-corr:{t.tenant_id}"),
            correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
        )
        await _to_state(role_logins, admin, t, "K3")  # C3 committed
        calls: list[int] = []

        async def _callback(handle: Any) -> None:
            calls.append(1)

        try:
            await runtime.gateway.with_tenant_transaction(context, _callback)
        except RegistryInconsistent as error:
            refused = error
        else:
            raise AssertionError("TC-N09: registry mismatch (the gateway admitted the session)")
    finally:
        await pool.close()
    assert refused.reason_code == "REGISTRY_REVALIDATION_MISMATCH", "TC-N09: registry mismatch"
    assert calls == [], "TC-N09: callback not run"


# --- TC-G01a: CLc at K1-K3, component (owner record de5024f1…508c, Q-B4) ------


@pytest.mark.parametrize("state", ["K1", "K2", "K3"])
async def test_tc_g01a_clc_refused_before_k11(
    state: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, state)
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        error = await _refused(
            _claim_tx(
                conn,
                form="CLc",
                tenant_id=t.tenant_id,
                operation_id=op,
                new_attempt_id=uuid4(),
                k=k,
            ),
            f"TC-G01a {state}",
        )
    _assert_code(error, "MAINTENANCE_CLAIM_REFUSED", "claim", f"TC-G01a {state}")
    _assert_r5(before, _r5_snapshot(admin, t), f"TC-G01a {state}", new_events=[])


# --- TC-G02: Φ1 after C2, Φ2 after C3, exact (arch v6 r3 §4) ------------------


@pytest.mark.parametrize("state", ["K2", "K3"])
async def test_tc_g02_phi_exact_after_c2_and_c3(
    state: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Expected tuples are written from arch §4 and the unit sources (`_phi_expected`), with
    owner entries from `acldefault`; never from the classifier under test."""

    admin, t = migrated_database, k0_tenant
    await _to_state(role_logins, admin, t, state)
    _assert_phi(admin, t.schema_key, "Φ1" if state == "K2" else "Φ2", f"TC-G02 {state}")


# --- TC-G03: C1-C3 crash and lost ack, component (v4 §1a expectations) --------


@pytest.mark.parametrize(
    ("step", "fault", "expected"),
    [
        ("C1", "crash", "K0"),
        ("C1", "lost_ack", "K1"),
        ("C2", "crash", "K1"),
        ("C2", "lost_ack", "K2"),
        ("C3", "crash", "K2"),
        ("C3", "lost_ack", "K3"),
    ],
)
async def test_tc_g03_step_fault_classifies_as_section_1a(
    step: str,
    fault: str,
    expected: str,
    k0_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """Q-P4a (a): crash surfaces InjectedFault; lost_ack surfaces the connection-class
    error the hook raises after COMMIT (B5.1 v3 §4.4), as psycopg.OperationalError,
    unwrapped; a fault is not a refusal (no attempt_refused). Owner record 03ae4897…1393
    also requires the resulting state and evidence: the faulted attempt's events and the
    exact Φ phase of the committed state (v4 §1a; arch v6 r3 §4)."""

    from haloflow.m01.provisioning.upgrade import InjectedFault, KState, UpgradeTestHooks

    admin, t = migrated_database, k0_tenant
    op: UUID | None = None
    if step != "C1":
        op = await _to_state(role_logins, admin, t, {"C2": "K1", "C3": "K2"}[step])
    known = set(_attempts_in_order(admin, t.tenant_id))
    task = _spawn(
        _compose(role_logins, UpgradeTestHooks(step_fault={step: fault})).run(_request(t, op))
    )
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    surfaced = InjectedFault if fault == "crash" else psycopg.OperationalError
    assert type(outcome) is surfaced, f"TC-G03 {step} {fault}: fault surfaced unchanged"
    row = _operation(admin, t.tenant_id)
    c = await _classify(role_logins, t.tenant_id, None if row is None else UUID(str(row[0])))
    assert c.state == KState(expected), f"TC-G03 {step} {fault}: classified {expected}"
    registry = _admin_one(
        admin,
        "SELECT lifecycle_state, schema_version FROM shared.tenants WHERE tenant_id = %s",
        (t.tenant_id,),
    )
    assert registry == (("suspended", 2) if expected == "K3" else ("active", 2)), (
        f"TC-G03 {step} {fault}: registry"
    )
    assert _refusals(admin, t.tenant_id) == [], f"TC-G03 {step} {fault}: a fault is not a refusal"
    (faulted,) = [x for x in _attempts_in_order(admin, t.tenant_id) if x not in known]
    expected_events = {
        ("C1", "crash"): ["started"],
        ("C1", "lost_ack"): ["started", "op_created"],
        ("C2", "crash"): ["started", "claimed"],
        ("C2", "lost_ack"): ["started", "claimed", "l2_withheld"],
        ("C3", "crash"): ["started", "claimed"],
        ("C3", "lost_ack"): ["started", "claimed", "l1_withheld"],
    }[(step, fault)]
    assert [e["event"] for e in _events(admin, t.tenant_id, faulted)] == expected_events, (
        f"TC-G03 {step} {fault}: faulted attempt's evidence"
    )
    phase = {"K0": "Φ0", "K1": "Φ0", "K2": "Φ1", "K3": "Φ2"}[expected]
    _assert_phi(admin, t.schema_key, phase, f"TC-G03 {step} {fault}")


# --- TC-G04: the CP-3 boundary stop (owner record de5024f1…508c, Q-B2) --------


async def test_tc_g04_boundary_stop_at_k3(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Retirement or replacement at CP-4 needs its own recorded owner decision."""

    from haloflow.m01.provisioning.upgrade import CheckpointBoundaryReached, KState

    admin, t = migrated_database, k0_tenant
    try:
        await _compose(role_logins).run(_request(t, None))
    except CheckpointBoundaryReached:
        pass
    else:
        raise AssertionError("TC-G04: stopped at the CP-3 boundary")
    (attempt,) = _attempts_in_order(admin, t.tenant_id)
    assert [e["event"] for e in _events(admin, t.tenant_id, attempt)] == [
        "started",
        "op_created",
        "l2_withheld",
        "l1_withheld",
    ], "TC-G04: events up to C3, no refusal, no completion"
    op = _operation_id(admin, t.tenant_id)
    assert (await _classify(role_logins, t.tenant_id, op)).state == KState.K3, (
        "TC-G04: committed state K3"
    )
    _assert_phi(admin, t.schema_key, "Φ2", "TC-G04")


# --- TC-A03 (IP-15/16 v2; split per owner record f3c8b5e9…03ed, b7) ----------
# A03-c and A03-e: outer nodes through the capability_fault seam (Q-P5 (a)), below.


async def test_tc_a03_a_lock_acquire(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    a = await _halt_after(role_logins, admin, "C1", _request(t, None))
    op = _operation_id(admin, t.tenant_id)
    before = _r5_snapshot(admin, t)
    error = await _refused(_compose(role_logins, lock_timeout=2.0).run(_request(t, op)), "TC-A03-a")
    _assert_code(error, "LOCK_UNAVAILABLE", "lock_acquire", "TC-A03-a")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-A03-a", new_events=["started", "attempt_refused"]
    )
    (att_b,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, att_b, "LOCK_UNAVAILABLE", "lock_acquire", None, "TC-A03-a"
    )
    await a.cancel()


async def test_tc_a03_c_invalid_capability_outer(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """A03-c: an invalid capability on an outer attempt -> RC-04 / capability before any step
    connection; a separate later trusted evidence phase appends one refusal with operation
    NULL, never built from the invalid capability (it names this attempt and tenant).

    Ordering (Codex v4 review): the composition's own connection factories and the
    approved appender-entry seam passively log, in one list, every connection request and
    the entry to the trusted evidence phase. From the C1 pause on, the first logged event
    must be the evidence-phase entry: no step connection (P or M) is requested before the
    capability is refused. Connection requests after the entry (the evidence connection)
    are permitted. Nothing is fabricated and no check is bypassed."""

    admin, t = migrated_database, k0_tenant
    log: list[tuple[str, ...]] = []
    probe = _Probe("order", admin, t.tenant_id, log)
    a, _op, att_a = await _faulted_after_c1(
        role_logins, admin, t, "missing_attempt", connect_log=log, appender_probe=probe
    )
    baseline = len(log)  # everything before the C1 pause (S0-S3, C1) is outside this check
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-A03-c")
    after_pause = log[baseline:]
    if probe.invocations != 1:
        raise AssertionError("TC-A03-c: the trusted evidence phase was entered once")
    assert after_pause and after_pause[0] == ("appender_entry",), (
        "TC-A03-c: no step connection before the trusted evidence phase"
    )
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "capability", "TC-A03-c")
    added = _assert_r5(before, _r5_snapshot(admin, t), "TC-A03-c", new_events=["attempt_refused"])
    (row,) = added
    assert (row[1], row[3]) == (att_a, t.tenant_id), (
        "TC-A03-c: evidence uses the trusted attempt and tenant"
    )
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_TOKEN_INVALID", "capability", None, "TC-A03-c"
    )


async def test_tc_a03_e_binding_mismatch_outer_association(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """A03-e: outer RC-05 (tenant variant) -> operation NULL, never the mismatched row; the
    evidence names the trusted tenant, and nothing is appended under the capability's tenant."""

    admin, t = migrated_database, k0_tenant

    def _foreign_rows() -> list[tuple[Any, ...]]:  # any tenant other than this one
        return _admin_rows(
            admin,
            f"SELECT event_id FROM {ATTEMPTS} WHERE tenant_id <> %s ORDER BY 1",
            (t.tenant_id,),
        )

    a, _op, att_a = await _faulted_after_c1(role_logins, admin, t, "tenant")
    foreign_before = _foreign_rows()
    error = await _refused(a.resume(), "TC-A03-e")
    _assert_code(error, "MAINTENANCE_TOKEN_INVALID", "fence", "TC-A03-e")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_TOKEN_INVALID", "fence", None, "TC-A03-e"
    )
    assert _foreign_rows() == foreign_before, (
        "TC-A03-e: nothing appended under the capability's tenant"
    )


async def test_tc_a03_d1_fence_lost(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    a, _, att_a, _ = await _stale_a_after_b_claims(role_logins, admin, t)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-A03-d1")
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", "TC-A03-d1")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_FENCE_LOST", "fence", None, "TC-A03-d1"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A03-d1", new_events=["attempt_refused"])


class _Probe:
    """The reviewed A04 seam (B5.1 v3 §4.4; Q-P7: the `attempt_refused` appender only).
    `on_invocation` runs at the appender's entry, before it obtains its connection; it is
    the synchronization point: by then S1's `started` has committed, so the harness
    barrier taken here can block only the target append (Codex B5.2 v1 finding 1).

    Modes: "a" (read-only session -> 25006), "b1" (known commit, then an injected
    connection-class error), "b2"/"b3" (barrier, then cancel / terminate), "h" (A03-h
    observations only). The wrapper never fabricates a result and never skips SQL."""

    def __init__(
        self, mode: str, admin: str, tenant_id: str, log: list[tuple[str, ...]] | None = None
    ) -> None:
        self.mode, self.admin, self.tenant_id, self.log = mode, admin, tenant_id, log
        self.invocations, self.sent = 0, 0
        self.pid: int | None = None
        self.sent_event = asyncio.Event()
        self.started_committed_at_entry: bool | None = None
        self.ops_locks_at_entry: list[Any] | None = None
        self.insert_conn_state: tuple[bool, Any] | None = None
        self.visible_after_insert: int | None = None
        self.barrier: psycopg.Connection[Any] | None = None
        self.barrier_pid: int | None = None
        self.barrier_error: str | None = None

    # -- seam: appender entry
    def on_invocation(self) -> None:
        self.invocations += 1
        if self.log is not None:
            self.log.append(("appender_entry",))
        self.started_committed_at_entry = bool(
            _admin_rows(
                self.admin,
                f"SELECT 1 FROM {ATTEMPTS} WHERE tenant_id = %s AND event = 'started'",
                (self.tenant_id,),
            )
        )
        self.ops_locks_at_entry = _admin_rows(
            self.admin,
            "SELECT pid, mode FROM pg_locks WHERE locktype = 'relation' AND relation = "
            "%s::regclass",
            (OPS,),
        )
        if self.mode in ("b2", "b3"):
            self._take_barrier()

    def _take_barrier(self) -> None:
        """H4: SHARE lock on the attempts table from the harness admin session; writes
        nothing; bounded by lock_timeout; released in the test's `finally`."""

        try:
            conn = psycopg.connect(self.admin, autocommit=False)
            self.barrier = conn
            self.barrier_pid = int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])  # type: ignore[index]
            conn.execute(f"SET LOCAL lock_timeout = '{int(T_LOCK_SECONDS)}s'")
            conn.execute(f"LOCK TABLE {ATTEMPTS} IN SHARE MODE")
        except psycopg.Error as error:  # recorded, never raised into the production code
            self.barrier_error = type(error).__name__

    def release_barrier(self) -> None:
        if self.barrier is not None:
            try:
                self.barrier.rollback()
            finally:
                self.barrier.close()
                self.barrier = None

    # -- seam: appender connection
    def wrap(self, conn: AsyncConnection[Any]) -> Any:
        return _CountingConnection(conn, self)


class _CountingConnection:
    def __init__(self, inner: AsyncConnection[Any], probe: _Probe) -> None:
        self._inner, self._probe = inner, probe

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        probe = self._probe
        text = query.as_string(self._inner) if hasattr(query, "as_string") else str(query)
        is_insert = text.lstrip().upper().startswith("INSERT")
        if is_insert:
            probe.pid = await _backend_pid(self._inner)
            probe.insert_conn_state = (self._inner.autocommit, self._inner.info.transaction_status)
            if probe.mode == "a":
                await self._inner.execute("SET default_transaction_read_only = on")
            probe.sent += 1
            probe.sent_event.set()
        result = await self._inner.execute(query, params, **kwargs)
        if is_insert:
            probe.visible_after_insert = len(_refusals(probe.admin, probe.tenant_id))
            if probe.mode == "b1":
                if probe.visible_after_insert != 1:  # the approved independent observation
                    raise SetupError(
                        "TC-A04-b1: the commit was not independently observed before the drop"
                    )
                raise psycopg.OperationalError("injected: reply dropped after server commit")
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_tc_a03_d2_lock_lost_and_h_rollback_then_append(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """d2: RC-02 with the established association. h: the refused step's transaction ended
    before the appender was entered (no backend holds any lock on the operations table),
    and the append is one INSERT on an autocommit connection with no open transaction,
    visible to an independent reader as soon as it returns (Codex B5.2 v1 finding 5)."""

    admin, t = migrated_database, k0_tenant
    probe = _Probe("h", admin, t.tenant_id)
    a = await _halt_after(role_logins, admin, "C1", _request(t, None), appender_probe=probe)
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    await _close_halted_k(admin, a, role_logins)
    before = _r5_snapshot(admin, t)
    error = await _refused(a.resume(), "TC-A03-d2")
    _assert_code(error, "MAINTENANCE_LOCK_LOST", "fence", "TC-A03-d2")
    _assert_one_refusal(
        admin, t.tenant_id, att_a, "MAINTENANCE_LOCK_LOST", "fence", op, "TC-A03-d2"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A03-h", new_events=["attempt_refused"])
    assert probe.invocations == 1, "TC-A03-h: the appender was entered once"
    assert probe.ops_locks_at_entry == [], (
        "TC-A03-h: refused transaction ended before the appender was entered"
    )
    assert probe.insert_conn_state == (True, psycopg.pq.TransactionStatus.IDLE), (
        "TC-A03-h: the append is a separate autocommit INSERT"
    )
    assert probe.visible_after_insert == 1, (
        "TC-A03-h: committed on its own when the INSERT returned"
    )


async def test_tc_a03_f_claim_refused(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K2")
    before = _r5_snapshot(admin, t)
    error = await _refused(
        _compose(role_logins).abandon(_request(t, op), _authorization(t.tenant_id, op)), "TC-A03-f"
    )
    _assert_code(error, "MAINTENANCE_CLAIM_REFUSED", "claim", "TC-A03-f")
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-A03-f", new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(
        admin, t.tenant_id, attempt, "MAINTENANCE_CLAIM_REFUSED", "claim", None, "TC-A03-f"
    )


@pytest.mark.parametrize("variant", ["g1_case_refused", "g2_state_unknown"])
async def test_tc_a03_g_classification(
    variant: str, k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """g1 uses the H2 seed (README §5: use beyond TC-K07 listed for approval); g2 uses H3."""

    admin, t = migrated_database, k0_tenant
    case = f"TC-A03-{variant}"
    if variant == "g1_case_refused":
        _seed_t003(admin, t.tenant_id, "running")
        before = _r5_snapshot(admin, t)
        error = await _refused(_compose(role_logins).run(_request(t, None)), case)
        code = "MAINTENANCE_CASE_REFUSED"
    else:
        before = _r5_snapshot(admin, t)
        with _attempts_select_revoked(admin):
            error = await _refused(_compose(role_logins).run(_request(t, None)), case)
        code = "MAINTENANCE_STATE_UNKNOWN"
    _assert_code(error, code, "classify", case)
    added = _assert_r5(
        before, _r5_snapshot(admin, t), case, new_events=["started", "attempt_refused"]
    )
    (attempt,) = {r[1] for r in added}
    _assert_one_refusal(admin, t.tenant_id, attempt, code, "classify", None, case)


async def test_tc_a03_i_provision_on_owned_tenant_appends_nothing(
    m02: ModuleType,
    m02_ids: Any,
    k0_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    from haloflow.m01.errors import ProvisioningFailed

    admin, t = migrated_database, k0_tenant
    await _to_state(role_logins, admin, t, "K1")
    before = _r5_snapshot(admin, t)
    with pytest.raises(ProvisioningFailed) as error:
        await m02.provision(
            m02_ids,
            _target_two_registry(),
            (t.tenant_id, t.schema_key),
            supported=range(1, 3),
            manifest=_historical_manifest(),
        )
    assert error.value.reason_code == "TENANT_NOT_RESUMABLE", "TC-A03-i provision: RC-24"
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A03-i provision", new_events=[])


async def test_tc_a03_j1_direct_fence_appends_nothing(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    from haloflow.m01.provisioning.upgrade import validate_capability

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    before = _r5_snapshot(admin, t)
    async with _k(role_logins, t.tenant_id) as k, _p_session(role_logins) as conn:
        cap = await _capability_for(conn, t.tenant_id, op, uuid4(), k)
        validate_capability(cap)
        error = await _refused(_fence_tx(conn, cap, k), "TC-A03-j1")
    _assert_code(error, "MAINTENANCE_FENCE_LOST", "fence", "TC-A03-j1")
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A03-j1", new_events=[])


async def test_tc_a03_k_foreign_association_refused(
    m02: ModuleType,
    m02_ids: Any,
    m02_reset: Callable[[tuple[str, str]], None],
    k0_tenant: Tenant,
    role_logins: dict[str, str],
    migrated_database: str,
) -> None:
    """Component: the database's association check (TC-S15 shape), written as P."""

    admin, t = migrated_database, k0_tenant
    op = await _to_state(role_logins, admin, t, "K1")
    other = _tenant_identity("a03k-other")
    try:
        await _provision_k0(m02, m02_ids, m02_reset, admin, other)
        before = _r5_snapshot(admin, Tenant(*other))
        async with _p_session(role_logins) as conn:
            with pytest.raises(psycopg.errors.RaiseException) as error:
                await conn.execute(
                    f"INSERT INTO {ATTEMPTS} (attempt_id, maintenance_operation_id, tenant_id, "
                    "event, detail) "
                    "VALUES (%s, %s, %s, 'attempt_refused', %s)",
                    (
                        uuid4(),
                        op,
                        other[0],
                        json.dumps({"code": "MAINTENANCE_FENCE_LOST", "phase": "fence"}),
                    ),
                )
        assert "invalid operation association" in str(error.value), "TC-A03-k: RC-09 association"
        _assert_r5(before, _r5_snapshot(admin, Tenant(*other)), "TC-A03-k", new_events=[])
    finally:
        _purge(admin, other[0])
        m02_reset(other)


# --- TC-A04: append outcome (IP-14 A6; seam per owner record f3c8b5e9…03ed) ---


async def _refusing_attempt(
    role_logins: dict[str, str], admin: str, t: Tenant, probe: _Probe
) -> asyncio.Task[Any]:
    """An outer attempt that refuses with RC-06 (H2 unmarked-running seed), with the probe."""

    from haloflow.m01.provisioning.upgrade import UpgradeTestHooks

    upgrade = _compose(role_logins, UpgradeTestHooks(appender_probe=probe))
    return _spawn(upgrade.run(_request(t, None)))


def _assert_seam_order(probe: _Probe, case: str) -> None:
    if probe.started_committed_at_entry is not True:
        raise SetupError(f"{case}: S1 `started` was not committed when the appender was entered")


async def test_tc_a04_a_insert_error(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    probe = _Probe("a", admin, t.tenant_id)
    _seed_t003(admin, t.tenant_id, "running")
    before = _r5_snapshot(admin, t)
    error = await _refused(await _refusing_attempt(role_logins, admin, t, probe), "TC-A04-a")
    _assert_seam_order(probe, "TC-A04-a")
    _assert_code(error, "MAINTENANCE_EVIDENCE_WRITE_FAILED", "classify", "TC-A04-a")
    assert isinstance(error.__cause__, psycopg.errors.ReadOnlySqlTransaction), (
        "TC-A04-a: the real 25006 cause"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A04-a", new_events=["started"])
    assert (probe.invocations, probe.sent) == (1, 1), "TC-A04-a: no retry"


async def test_tc_a04_b1_known_commit_reply_dropped(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    probe = _Probe("b1", admin, t.tenant_id)
    _seed_t003(admin, t.tenant_id, "running")
    before = _r5_snapshot(admin, t)
    error = await _refused(await _refusing_attempt(role_logins, admin, t, probe), "TC-A04-b1")
    _assert_seam_order(probe, "TC-A04-b1")
    _assert_code(error, "MAINTENANCE_EVIDENCE_WRITE_FAILED", "classify", "TC-A04-b1")
    assert probe.visible_after_insert == 1, (
        "TC-A04-b1: commit observed independently before the drop"
    )
    _assert_r5(
        before, _r5_snapshot(admin, t), "TC-A04-b1", new_events=["started", "attempt_refused"]
    )
    assert (probe.invocations, probe.sent) == (1, 1), "TC-A04-b1: no retry"


async def _blocked_by_barrier(admin: str, probe: _Probe, case: str) -> None:
    try:
        await asyncio.wait_for(probe.sent_event.wait(), BARRIER_WAIT_SECONDS)
    except TimeoutError as timeout:
        raise SetupError(f"{case}: the appender INSERT was never sent") from timeout
    if probe.barrier_pid is None or probe.pid is None:
        raise SetupError(f"{case}: barrier not taken ({probe.barrier_error})")
    barrier_pid, appender_pid = probe.barrier_pid, probe.pid

    def _ok() -> bool:
        return _blocked_by(admin, appender_pid, barrier_pid) and bool(
            _admin_rows(
                admin,
                "SELECT 1 FROM pg_locks WHERE pid = %s AND relation = %s::regclass AND mode = "
                "'ShareLock' AND granted",
                (barrier_pid, ATTEMPTS),
            )
        )

    await _wait_until(_ok, f"{case}: the appender's INSERT waits on the barrier")


async def test_tc_a04_b2_proven_rollback(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    admin, t = migrated_database, k0_tenant
    probe = _Probe("b2", admin, t.tenant_id)
    _seed_t003(admin, t.tenant_id, "running")
    before = _r5_snapshot(admin, t)
    try:
        task = await _refusing_attempt(role_logins, admin, t, probe)
        await _blocked_by_barrier(admin, probe, "TC-A04-b2")
        _assert_seam_order(probe, "TC-A04-b2")
        if not _admin_one(admin, "SELECT pg_cancel_backend(%s)", (probe.pid,))[0]:
            raise SetupError("TC-A04-b2: cancel not delivered")
        error = await _refused(task, "TC-A04-b2")
    finally:
        probe.release_barrier()
    _assert_code(error, "MAINTENANCE_EVIDENCE_WRITE_FAILED", "classify", "TC-A04-b2")
    assert isinstance(error.__cause__, psycopg.errors.QueryCanceled), (
        "TC-A04-b2: the real 57014 abort"
    )
    _assert_r5(before, _r5_snapshot(admin, t), "TC-A04-b2", new_events=["started"])
    assert (probe.invocations, probe.sent) == (1, 1), "TC-A04-b2: no retry"


async def test_tc_a04_b3_ambiguous_outcome(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Application-side ambiguity only (owner record f3c8b5e9…03ed). v7 (owner record
    5c630668…8ea2): the backend is terminated while its INSERT is provably blocked
    on the barrier, then the barrier is released. The application gets a connection error
    and cannot tell whether the row committed; the independent reader proves the server
    outcome is a rollback (0 rows). A server-side unknown outcome is not constructed
    deterministically and is not claimed. An INSERT that returned success, or any
    non-connection error, is an UNMET injection: never a pass and never retried."""

    admin, t = migrated_database, k0_tenant
    probe = _Probe("b3", admin, t.tenant_id)
    _seed_t003(admin, t.tenant_id, "running")
    before = _r5_snapshot(admin, t)
    try:
        task = await _refusing_attempt(role_logins, admin, t, probe)
        await _blocked_by_barrier(admin, probe, "TC-A04-b3")
        _assert_seam_order(probe, "TC-A04-b3")
        appender_pid = probe.pid
        assert appender_pid is not None
        if not _admin_one(admin, "SELECT pg_terminate_backend(%s)", (appender_pid,))[0]:
            raise SetupError("TC-A04-b3: termination not delivered")
        await _wait_until(lambda: _backend_gone(admin, appender_pid), "appender backend gone")
        probe.release_barrier()  # only after the blocked backend is gone
        error = await _refused(task, "TC-A04-b3")
    finally:
        probe.release_barrier()
    if error.reason_code == "MAINTENANCE_CASE_REFUSED":
        raise UnmetInjection("TC-A04-b3: the INSERT returned success before termination")
    if not isinstance(error.__cause__, (psycopg.OperationalError, psycopg.errors.AdminShutdown)):
        raise UnmetInjection("TC-A04-b3: the driver did not report a connection error")
    _assert_code(error, "MAINTENANCE_EVIDENCE_WRITE_FAILED", "classify", "TC-A04-b3")
    count = len(_refusals(admin, t.tenant_id))
    print(f"L6_A04_B3_OBSERVED_COUNT={count}")
    assert count == 0, "TC-A04-b3: terminated while blocked, so 0 by independent read"
    _assert_r5(
        before,
        _r5_snapshot(admin, t),
        "TC-A04-b3",
        new_events=["started"] + ["attempt_refused"] * count,
    )
    assert (probe.invocations, probe.sent) == (1, 1), "TC-A04-b3: no retry"


# --- TC-A05: interference at C3's T-2 point (owner record f3c8b5e9…03ed, b5) ---


async def test_tc_a05_interference_then_no_refusal(
    k0_tenant: Tenant, role_logins: dict[str, str], migrated_database: str
) -> None:
    """Point `b` is sourced (addendum 4 r3 T-2). The stop outcome is Q-P6 (a) (owner record 03ae4897…1393);
    `MaintenanceInterferenceStopped(point, kind, phase, observed)`, not a
    MaintenanceRefused. Addendum 4 r3 D4 (before activation): the
    operation reports the actual committed state it observed, without repairing it, and
    the observed denial state, with no safe-denial claim. `observed` is compared with
    the committed state read independently here."""  # noqa: E501

    from haloflow.m01.provisioning.upgrade import MaintenanceInterferenceStopped, MaintenanceRefused

    admin, t = migrated_database, k0_tenant
    a = await _halt_after(role_logins, admin, "C2", _request(t, None))
    op = _operation_id(admin, t.tenant_id)
    att_a = _current_attempt(admin, op)
    _seed_t003(admin, t.tenant_id, "failed", pre_c1=False)  # H2, class T write, validated
    before = _r5_snapshot(admin, t)
    try:
        await a.resume()
    except MaintenanceInterferenceStopped as caught:
        stopped = caught
    except MaintenanceRefused as refused:
        raise AssertionError(
            f"TC-A05: interference stop is not a refusal ({refused.reason_code})"
        ) from refused
    else:
        raise AssertionError("TC-A05: the attempt stopped on interference")
    assert (stopped.point, stopped.kind, stopped.phase) == ("b", "ledger", "C3"), (
        "TC-A05: point b, kind ledger, phase C3"
    )
    registry = _admin_one(
        admin,
        "SELECT lifecycle_state, schema_version FROM shared.tenants WHERE tenant_id = %s",
        (t.tenant_id,),
    )
    op_state = _admin_one(
        admin, f"SELECT state FROM {OPS} WHERE maintenance_operation_id = %s", (op,)
    )[0]
    ledger = _admin_one(
        admin,
        "SELECT state, attempt FROM shared.schema_migrations WHERE tenant_id = %s AND migration_id "
        "LIKE 't003%%'",
        (t.tenant_id,),
    )
    independent = {
        "registry": (str(registry[0]), int(registry[1])),
        "operation_state": str(op_state),
        "t003_ledger": (str(ledger[0]), int(ledger[1])),
        "denial": "establishment_incomplete",  # arch v6 r3 §7, K2: active, L2 only
    }
    observed = stopped.observed
    assert {
        "registry": (observed.lifecycle_state, observed.schema_version),
        "operation_state": observed.operation_state,
        "t003_ledger": (observed.t003_state, observed.t003_attempt),
        "denial": observed.denial_status,
    } == independent, "TC-A05: reports the committed state it observed"
    assert observed.safe_denial_claimed is False, "TC-A05: no safe-denial claim"
    added = _assert_r5(
        before, _r5_snapshot(admin, t), "TC-A05", new_events=["interference_detected"]
    )
    (row,) = added
    assert (row[1], row[5]) == (att_a, {"point": "b", "kind": "ledger"}), (
        "TC-A05: interference_detected{b, ledger}"
    )
