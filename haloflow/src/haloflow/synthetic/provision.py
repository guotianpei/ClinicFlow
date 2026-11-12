"""Phased, resumable synthetic install (Option B B2). SYNTHETIC ONLY.

`init` builds ONE synthetic clinic in a dedicated, disposable PostgreSQL cluster:
  intent -> stamped -> migrated -> logins -> tenant -> profile -> activated -> settings ->
  staff -> ready
Every committing transaction is guarded (guard.py, protocol F). The local state is written
BEFORE the first database change (intent, holding every identity and secret) and after each
phase's commit. A re-run inspects the next phase against the intent: nothing -> perform it;
exactly the intent's owned result -> acknowledge it; an owned partial result of a resumable
phase -> complete it; anything else -> STATE_DIVERGED (never repaired, never adopted).

Seeds only the synthetic clinic: registry-94 tenant, TEST profile, one activation grant,
default reminder settings and one staff member (the first-member bootstrap through a support
login, then follow-up read+act through the staff admin function). No scenario data (B5), no
app (B3), no reset.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from haloflow.m01.provisioning import (
    MIGRATOR_ROLE,
    PROVISIONER_ROLE,
    ProvisioningRequest,
    TenantMigrationRunner,
    TenantProvisioner,
)
from haloflow.m01.provisioning.roles import AUDIT_PROJECTOR_ROLE, RUNTIME_ROLE
from haloflow.m01.staff.l1w_units import BOOTSTRAP_CAPABILITY, SUPPORT_ROLE
from haloflow.m02.roles import M05_OWNER_ROLE
from haloflow.synthetic.attest import _DB_CHECKS, PUBLIC_ACL, admin_connect, check_a0
from haloflow.synthetic.composition import (
    CHECK_REASONS,
    SA_TEST_PROFILE_SQL,
    SI_TEST_PROFILE_SQL,
    build_synthetic_registry,
)
from haloflow.synthetic.dsn import AdminEndpoint, parse_admin_dsn
from haloflow.synthetic.errors import SyntheticRefused, refused_from
from haloflow.synthetic.guard import (
    INSTALL_LOCK_PID,
    PHASE_HOOK,
    InstallLock,
    begin_run,
    cluster_identity,
    guard_sync,
    guarded_factory,
    new_epoch,
    read_reservation,
    reservation_value,
)
from haloflow.synthetic.hooks import fire
from haloflow.synthetic.state import PHASES, LockedState, StateDir

STATE_SCHEMA: Final = 1
LOGIN_GROUPS: Final = {"runtime": RUNTIME_ROLE, "migrator": MIGRATOR_ROLE,
                       "provisioner": PROVISIONER_ROLE, "audit_projector": AUDIT_PROJECTOR_ROLE,
                       "support": SUPPORT_ROLE}
GROUP_ROLES: Final = frozenset({
    "haloflow_audit_projector", "haloflow_breakglass_ro", "haloflow_breakglass_rw",
    "haloflow_control_audit_writer", "haloflow_identity_owner", "haloflow_m02_append_owner",
    "haloflow_m02_lock_owner", "haloflow_m02_projection_owner", "haloflow_m02_read_owner",
    "haloflow_m05_owner", "haloflow_migrator", "haloflow_owner", "haloflow_provisioner",
    "haloflow_runtime", "haloflow_staff_owner", "haloflow_support", "haloflow_support_ro"})
ALEMBIC_HEAD: Final = "009"
SCRAM_ITERATIONS: Final = 4096
GRANT_WINDOW: Final = timedelta(hours=2)
SETTINGS: Final = {"lead_days": 2, "send_time": "10:00:00", "window_start": "09:00:00",
                   "window_end": "19:00:00", "clinic_tz": "America/New_York",
                   "clinic_display_name": "HaloVox Demo Clinic"}
STAFF_NAME: Final = "Demo Clinic Admin"
ACTIVATION_REASON: Final = "synthetic install"
STATE_KEYS: Final = frozenset({
    "schema_version", "phase", "cluster_sysid", "target", "target_oid", "nonce", "tenant_id",
    "schema_key", "staff_id", "activation_actor_id", "ticket_reference", "logins",
    "note_keys", "support"})


@dataclass(frozen=True)
class InitResult:
    phase: str


# ---------------------------------------------------------------------------- intent

def scram_verifier(password: str, salt_b64: str, iterations: int) -> str:
    salt = base64.b64decode(salt_b64)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client = hmac.new(salted, b"Client Key", "sha256").digest()
    stored = hashlib.sha256(client).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    enc = base64.b64encode
    return (f"SCRAM-SHA-256${iterations}:{salt_b64}$"
            f"{enc(stored).decode()}:{enc(server).decode()}")


def _new_intent(target: str, sysid: str, oid: int) -> dict[str, Any]:
    nonce = secrets.token_hex(16)
    n8, n16, base = nonce[:8], nonce[:16], uuid.UUID(nonce)
    starts = datetime.now(UTC).replace(microsecond=0)
    logins = {
        suffix: {"role": f"haloflow_syn_{n8}_{suffix}", "password": secrets.token_urlsafe(32),
                 "salt": base64.b64encode(secrets.token_bytes(16)).decode(),
                 "iterations": SCRAM_ITERATIONS}
        for suffix in LOGIN_GROUPS}
    return {
        "schema_version": STATE_SCHEMA, "phase": "intent", "cluster_sysid": sysid,
        "target": target, "target_oid": oid, "nonce": nonce,
        "tenant_id": f"clinic-syn-{n8}", "schema_key": f"tenant_{n16}",
        "staff_id": str(uuid.uuid5(base, "staff")),
        "activation_actor_id": str(uuid.uuid5(base, "activation")),
        "ticket_reference": f"SYN-{n8}", "logins": logins,
        "note_keys": {"hmac": secrets.token_hex(32), "enc": secrets.token_hex(32),
                      "version": 1},
        "support": {"actor_id": f"syn-support-{n8}",
                    "grant_id": str(uuid.uuid5(base, "support-grant")),
                    "starts_at": starts.isoformat(),
                    "expires_at": (starts + GRANT_WINDOW).isoformat(),
                    "decision_ref": hashlib.sha256(f"synthetic-install:{nonce}".encode())
                    .hexdigest()},
    }


# ---------------------------------------------------------------------------- run context

@dataclass
class _Ctx:
    endpoint: AdminEndpoint
    st: LockedState
    lock: InstallLock
    state: dict[str, Any]
    expected: str

    @property
    def schema(self) -> str:
        return str(self.state["schema_key"])

    @property
    def nonce(self) -> str:
        return str(self.state["nonce"])

    def login_conninfo(self, suffix: str) -> str:
        spec = self.state["logins"][suffix]
        return make_conninfo(self.endpoint.conninfo(self.endpoint.target), user=spec["role"],
                             password=spec["password"])

    def uuid(self, name: str) -> str:
        return str(uuid.uuid5(uuid.UUID(self.nonce), name))


def _connect(conninfo: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(conninfo, autocommit=True)
    conn.execute("SET default_transaction_read_only = on")
    return conn


@contextmanager
def _phase_txn(ctx: _Ctx, point: str, conn: psycopg.Connection[Any]) -> Iterator[Any]:
    """One guarded transaction whose COMMIT is phase `point`'s commit: before_commit inside the
    transaction, commit_ack inside the commit error scope, after_commit once acknowledged."""

    with conn.transaction():
        guard_sync(conn, ctx.expected)
        yield conn
        fire(f"phase.{point}.before_commit", {"backend_pid": conn.info.backend_pid})
    fire(f"phase.{point}.commit_ack", {"backend_pid": conn.info.backend_pid})


def _one(conn: psycopg.Connection[Any], query: Any, args: Any = None) -> Any:
    row = conn.execute(query, args).fetchone()
    return None if row is None else row[0]


def _exists(conn: psycopg.Connection[Any], name: str) -> bool:
    return _one(conn, "SELECT pg_catalog.to_regclass(%s) IS NOT NULL", (name,)) is True


def _diverged(detail: str) -> SyntheticRefused:
    return SyntheticRefused("STATE_DIVERGED", (detail,))


# ---------------------------------------------------------------------------- A1 namespace

def _namespace_check(ctx: _Ctx) -> None:
    """Global ownership check after the takeover: nothing outside the intent's namespace."""

    logins = {v["role"] for v in ctx.state["logins"].values()}
    with admin_connect(ctx.endpoint, "postgres") as a:
        roles = {r[0] for r in a.execute(
            "SELECT rolname FROM pg_catalog.pg_authid WHERE oid >= 16384").fetchall()}
        if roles - GROUP_ROLES - logins:
            raise _diverged("role")
        dbs = sorted(r[0] for r in a.execute("SELECT datname FROM pg_catalog.pg_database"))
        if dbs != sorted(["postgres", "template0", "template1", ctx.endpoint.target]):
            raise _diverged("database")
        for cls, query in _DB_CHECKS.items():
            if _one(a, query, {"public_acl": PUBLIC_ACL}) is not True:
                raise _diverged(cls)
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        schemas = {r[0] for r in t.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg\\_%'"
            " AND nspname <> 'information_schema'").fetchall()}
        if schemas - {"public", "shared", ctx.schema}:
            raise _diverged("schema")


# ---------------------------------------------------------------------------- phases

Status = str   # "none" | "partial" | "exact"


def _inspect_stamped(ctx: _Ctx) -> Status:
    with admin_connect(ctx.endpoint, "postgres") as a:
        comment = _one(a, "SELECT pg_catalog.shobj_description(oid, 'pg_database')"
                          " FROM pg_catalog.pg_database WHERE datname = %s", (ctx.endpoint.target,))
    if comment is None:
        return "none"
    if comment == f"haloflow-synthetic:{ctx.nonce}":
        return "exact"
    raise _diverged("stamped")


def _run_stamped(ctx: _Ctx) -> None:
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, \
            _phase_txn(ctx, "stamped", c):
        c.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(ctx.endpoint.target),
            sql.Literal(f"haloflow-synthetic:{ctx.nonce}")))


def _inspect_migrated(ctx: _Ctx) -> Status:
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        version = _one(t, "SELECT version_num FROM public.alembic_version") \
            if _exists(t, "public.alembic_version") else None
        roles = {r[0] for r in t.execute(
            "SELECT rolname FROM pg_catalog.pg_roles WHERE NOT rolcanlogin AND oid >= 16384")}
    present = roles & GROUP_ROLES
    if version is None and not present:
        return "none"
    if version == ALEMBIC_HEAD and present == GROUP_ROLES:
        return "exact"
    raise _diverged("migrated")


def _run_migrated(ctx: _Ctx) -> None:
    from haloflow.synthetic.migrate import upgrade_guarded

    before, after = upgrade_guarded(ctx.endpoint.conninfo(ctx.endpoint.target), ctx.expected,
                                    phase="migrated")
    if after != ALEMBIC_HEAD or before not in (None, ALEMBIC_HEAD):
        raise _diverged("migrated")


_LOGIN_ATTRS: Final = (False, True, False, False, True, False, False, -1, None)


def _inspect_logins(ctx: _Ctx) -> Status:
    specs = ctx.state["logins"]
    with admin_connect(ctx.endpoint, "postgres") as a:
        boot = _one(a, "SELECT rolname FROM pg_catalog.pg_authid WHERE oid = 10")
        found = 0
        exact = True
        for suffix, spec in specs.items():
            row = a.execute(
                "SELECT rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
                " rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil, rolpassword, oid"
                " FROM pg_catalog.pg_authid WHERE rolname = %s", (spec["role"],)).fetchone()
            if row is None:
                continue
            found += 1
            members = a.execute(
                "SELECT g.rolname, m.admin_option, m.inherit_option, m.set_option,"
                " pg_catalog.pg_get_userbyid(m.grantor) FROM pg_catalog.pg_auth_members m"
                " JOIN pg_catalog.pg_roles g ON g.oid = m.roleid WHERE m.member = %s",
                (row[10],)).fetchall()
            config = a.execute("SELECT setdatabase, setconfig FROM pg_catalog.pg_db_role_setting"
                               " WHERE setrole = %s", (row[10],)).fetchall()
            verifier = scram_verifier(spec["password"], spec["salt"], spec["iterations"])
            same = hmac.compare_digest(str(row[9] or ""), verifier)
            if (tuple(row[:9]) != _LOGIN_ATTRS or not same
                    or members != [(LOGIN_GROUPS[suffix], False, True, True, boot)]
                    or config != [(0, ['search_path=""'])]):
                exact = False
    if found == 0:
        return "none"
    if found == len(specs) and exact:
        return "exact"
    raise _diverged("logins")


def _run_logins(ctx: _Ctx) -> None:
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, _phase_txn(ctx, "logins", c):
        for suffix, spec in ctx.state["logins"].items():
            verifier = scram_verifier(spec["password"], spec["salt"], spec["iterations"])
            c.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB"
                              " NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 PASSWORD {}"
                              " IN ROLE {}").format(
                sql.Identifier(spec["role"]), sql.Literal(verifier),
                sql.Identifier(LOGIN_GROUPS[suffix])))
            c.execute(sql.SQL("ALTER ROLE {} SET search_path = ''").format(
                sql.Identifier(spec["role"])))


def _inspect_tenant(ctx: _Ctx) -> Status:
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        if not _exists(t, "shared.tenants"):
            raise _diverged("tenant")
        rows = t.execute("SELECT tenant_id, schema_key, lifecycle_state, schema_version"
                         " FROM shared.tenants").fetchall()
    if not rows:
        return "none"
    if len(rows) != 1 or rows[0][:2] != (ctx.state["tenant_id"], ctx.schema):
        raise _diverged("tenant")
    target = build_synthetic_registry().target_version
    return "exact" if rows[0][2:] == ("active", target) else "partial"


def _run_tenant(ctx: _Ctx) -> None:
    registry = build_synthetic_registry()
    runner = TenantMigrationRunner(
        guarded_factory(ctx.login_conninfo("migrator"), ctx.expected), registry)
    provisioner = TenantProvisioner(
        guarded_factory(ctx.login_conninfo("provisioner"), ctx.expected), runner,
        supported_schema_versions=range(registry.target_version, registry.target_version + 1))
    request = ProvisioningRequest(tenant_id=ctx.state["tenant_id"], schema_key=ctx.schema,
                                  actor_id="synthetic-init",
                                  execution_id=uuid.UUID(ctx.uuid("provision")))

    async def run() -> None:
        token = PHASE_HOOK.set("tenant")
        try:
            await provisioner.provision(request)
        finally:
            PHASE_HOOK.reset(token)

    asyncio.run(run())


def _inspect_profile(ctx: _Ctx) -> Status:
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        workloads = _one(t, sql.SQL("SELECT count(*) FROM {}.m05_intake_workloads").format(
            sql.Identifier(ctx.schema)))
        intake = _one(t, sql.SQL("SELECT count(*) FROM {}.m05_intake_map").format(
            sql.Identifier(ctx.schema)))
    if (workloads, intake) == (0, 0):
        return "none"
    if (workloads, intake) == (4, 4 + len(CHECK_REASONS)):
        return "exact"
    raise _diverged("profile")


def _run_profile(ctx: _Ctx) -> None:
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, _phase_txn(ctx, "profile", c):
        c.execute(SA_TEST_PROFILE_SQL.format(schema=ctx.schema).encode())
        c.execute(SI_TEST_PROFILE_SQL.format(schema=ctx.schema).encode())


def _inspect_activated(ctx: _Ctx) -> Status:
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        rows = t.execute(sql.SQL(
            "SELECT change, actor_id::text, ticket_reference, reason"
            " FROM {}.m05_activation_records").format(sql.Identifier(ctx.schema))).fetchall()
    if not rows:
        return "none"
    if rows == [("grant", ctx.state["activation_actor_id"], ctx.state["ticket_reference"],
                 ACTIVATION_REASON)]:
        return "exact"
    raise _diverged("activated")


def _run_activated(ctx: _Ctx) -> None:
    command = {"schema_version": 1, "change": "grant",
               "actor_id": ctx.state["activation_actor_id"],
               "ticket_reference": ctx.state["ticket_reference"], "reason": ACTIVATION_REASON}
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, \
            _phase_txn(ctx, "activated", c):
        c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(M05_OWNER_ROLE)))
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (ctx.state["tenant_id"],))
        c.execute(sql.SQL("SELECT {}.m05_activation_change(%s)").format(
            sql.Identifier(ctx.schema)), (Jsonb(command),))


def _settings_row(t: psycopg.Connection[Any], schema: str) -> tuple[Any, ...] | None:
    return t.execute(sql.SQL(
        "SELECT lead_days, send_time::text, window_start::text, window_end::text, clinic_tz,"
        " clinic_display_name, settings_version FROM {}.m05_reminder_settings").format(
        sql.Identifier(schema))).fetchone()


def _inspect_settings(ctx: _Ctx) -> Status:
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        row = _settings_row(t, ctx.schema)
    if row is None:
        raise _diverged("settings")
    if row[6] == 1:
        return "none"
    if row[6] == 2 and dict(zip(SETTINGS, row[:6], strict=True)) == SETTINGS:
        return "exact"
    raise _diverged("settings")


def _run_settings(ctx: _Ctx) -> None:
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, \
            _phase_txn(ctx, "settings", c):
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(k)) for k in SETTINGS)
        cur = c.execute(sql.SQL(
            "UPDATE {}.m05_reminder_settings SET {}, settings_version = settings_version + 1"
            " WHERE settings_version = 1").format(sql.Identifier(ctx.schema), assignments),
            tuple(SETTINGS.values()))
        if cur.rowcount != 1:
            raise _diverged("settings")


# ---------------------------------------------------------------------------- staff (T1-T3)

def _support_rows(ctx: _Ctx, t: psycopg.Connection[Any]) -> tuple[Status, Status]:
    """(registration+grant T1 status, whether the grant is still in its window)."""

    sup = ctx.state["support"]
    login = ctx.state["logins"]["support"]["role"]
    logins = t.execute(
        "SELECT login_oid::bigint, login_name::text, actor_id, added_at, decision_ref"
        " FROM shared.support_operator_logins").fetchall()
    grants = t.execute(
        "SELECT grant_id::text, actor_id, tenant_id, ticket_reference, purpose_code,"
        " capabilities, approver_id, starts_at, expires_at, state, break_glass, created_at"
        " FROM shared.support_access_grants").fetchall()
    if not logins and not grants:
        return "none", "in_window"
    oid = _one(t, "SELECT oid::bigint FROM pg_catalog.pg_roles WHERE rolname = %s", (login,))
    starts = datetime.fromisoformat(sup["starts_at"])
    expires = datetime.fromisoformat(sup["expires_at"])
    if logins != [(oid, login, sup["actor_id"], starts, sup["decision_ref"])] or grants != [(
            sup["grant_id"], sup["actor_id"], ctx.state["tenant_id"],
            ctx.state["ticket_reference"], "synthetic-install", [BOOTSTRAP_CAPABILITY],
            "synthetic-init", starts, expires, "active", False, starts)]:
        raise _diverged("staff")
    now = _one(t, "SELECT clock_timestamp()")
    return "exact", ("in_window" if starts <= now < expires else "expired")


def _staff_status(ctx: _Ctx) -> tuple[Status, Status, Status, Status]:
    s = sql.Identifier(ctx.schema)
    with admin_connect(ctx.endpoint, ctx.endpoint.target) as t:
        t1, window = _support_rows(ctx, t)
        staff = t.execute(sql.SQL("SELECT staff_id::text, status, display_name FROM {}.m01_staff"
                                  ).format(s)).fetchall()
        roles = t.execute(sql.SQL("SELECT staff_id::text, role FROM {}.m01_staff_roles"
                                  ).format(s)).fetchall()
        keys = t.execute(sql.SQL("SELECT client_action_id::text FROM {}"
                                 ".m01_staff_bootstrap_keys").format(s)).fetchall()
        logs = t.execute(sql.SQL("SELECT staff_id::text, ticket_reference FROM {}"
                                 ".m01_staff_bootstrap_log").format(s)).fetchall()
        access = sorted(r[0] for r in t.execute(sql.SQL(
            "SELECT access FROM {}.m01_staff_followup_access").format(s)))
    sid = ctx.state["staff_id"]
    if not staff and not roles and not keys and not logs:
        t2 = "none"
    elif (staff == [(sid, "active", STAFF_NAME)] and roles == [(sid, "clinic_admin")]
          and keys == [(ctx.uuid("staff-first-admin"),)]
          and logs == [(sid, ctx.state["ticket_reference"])]):
        t2 = "exact"
    else:
        raise _diverged("staff")
    # T3a grants read, T3b grants act (t029 admits ONE semantic request per transaction).
    t3 = {(): "none", ("read",): "partial", ("act", "read"): "exact"}.get(tuple(access))
    if t3 is None or (t2 == "none" and t3 != "none") or (t1 == "none" and t2 != "none"):
        raise _diverged("staff")
    return t1, t2, t3, window


def _inspect_staff(ctx: _Ctx) -> Status:
    t1, t2, t3, _ = _staff_status(ctx)
    if (t1, t2, t3) == ("none", "none", "none"):
        return "none"
    return "exact" if (t1, t2, t3) == ("exact", "exact", "exact") else "partial"


def _run_staff(ctx: _Ctx) -> None:
    t1, t2, t3, window = _staff_status(ctx)
    sup = ctx.state["support"]
    # t034 authorizes against the grant BEFORE any replay: an expired window fails closed.
    # No silent renewal or replacement of the recorded grant. Once T2 is committed, completing
    # T3 or verifying owned state needs no support authority.
    expired = window == "expired" or datetime.now(UTC) >= datetime.fromisoformat(
        sup["expires_at"])
    if t2 == "none" and expired:
        raise SyntheticRefused("STATE_DIVERGED", ("support_grant_expired",))
    if t1 == "none":
        with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, \
                _phase_txn(ctx, "staff.t1", c):
            c.execute(
                "INSERT INTO shared.support_operator_logins (login_oid, login_name, actor_id,"
                " added_at, decision_ref) SELECT oid, rolname, %s, %s, %s FROM pg_catalog.pg_roles"
                " WHERE rolname = %s",
                (sup["actor_id"], sup["starts_at"], sup["decision_ref"],
                 ctx.state["logins"]["support"]["role"]))
            c.execute(
                "INSERT INTO shared.support_access_grants (grant_id, actor_id, tenant_id,"
                " ticket_reference, purpose_code, capabilities, approver_id, starts_at,"
                " expires_at, state, break_glass, created_at) VALUES (%s, %s, %s, %s,"
                " 'synthetic-install', %s, 'synthetic-init', %s, %s, 'active', false, %s)",
                (sup["grant_id"], sup["actor_id"], ctx.state["tenant_id"],
                 ctx.state["ticket_reference"], [BOOTSTRAP_CAPABILITY], sup["starts_at"],
                 sup["expires_at"], sup["starts_at"]))
        fire("phase.staff.t1.after_commit", {})
    if t2 == "none":
        command = {"schema_version": 1, "client_action_id": ctx.uuid("staff-first-admin"),
                   "staff_id": ctx.state["staff_id"], "display_name": STAFF_NAME}
        with _connect(ctx.login_conninfo("support")) as c, _phase_txn(ctx, "staff.t2", c):
            c.execute("SELECT set_config('app.tenant_id', %s, true)", (ctx.state["tenant_id"],))
            outcome = _one(c, sql.SQL("SELECT {}.m01_staff_bootstrap_first_admin(%s)").format(
                sql.Identifier(ctx.schema)), (Jsonb(command),))
            if not isinstance(outcome, dict) or outcome.get("outcome") != "BOOTSTRAPPED":
                raise _diverged("staff")
        fire("phase.staff.t2.after_commit", {})
    for access, point, needed in (("read", "staff.t3a", t3 == "none"),
                                  ("act", "staff.t3b", t3 != "exact")):
        if not needed:
            continue
        with _connect(ctx.login_conninfo("runtime")) as c, _phase_txn(ctx, point, c):
            c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(RUNTIME_ROLE)))
            c.execute("SELECT set_config('app.tenant_id', %s, true)", (ctx.state["tenant_id"],))
            command = {
                "schema_version": 1, "client_action_id": ctx.uuid(f"staff-access-{access}"),
                "staff_id": ctx.state["staff_id"], "operation": "grant", "access": access,
                "permissions": {"staff_admin": True},
                "attribution": {"principal_kind": "actor",
                                "principal_id": ctx.state["staff_id"],
                                "auth_method": "synthetic_init",
                                "execution_id": ctx.uuid("staff-execution"),
                                "request_id": None, "purpose_code": "operations"}}
            result = _one(c, sql.SQL("SELECT {}.m01_staff_admin_v2(%s)").format(
                sql.Identifier(ctx.schema)), (Jsonb(command),))
            if not isinstance(result, dict) or "staff" not in result:
                raise _diverged("staff")
        fire(f"phase.{point}.after_commit", {})


_PHASES: Final[dict[str, tuple[Callable[[_Ctx], Status], Callable[[_Ctx], None]]]] = {
    "stamped": (_inspect_stamped, _run_stamped),
    "migrated": (_inspect_migrated, _run_migrated),
    "logins": (_inspect_logins, _run_logins),
    "tenant": (_inspect_tenant, _run_tenant),
    "profile": (_inspect_profile, _run_profile),
    "activated": (_inspect_activated, _run_activated),
    "settings": (_inspect_settings, _run_settings),
    "staff": (_inspect_staff, _run_staff),
}


# ---------------------------------------------------------------------------- entry points

def init(environ: Mapping[str, str], *, attest_flag: bool) -> InitResult:
    check_a0(environ, attest_flag=attest_flag)
    endpoint = parse_admin_dsn(environ["HALOFLOW_SYNTHETIC_ADMIN_DSN"], environ)
    with StateDir(environ["HALOFLOW_SYNTHETIC_STATE"]).locked() as st:
        lock = InstallLock(endpoint)
        lock.acquire()
        token = INSTALL_LOCK_PID.set(lock.pid)
        try:
            return _run(endpoint, st, lock)
        except Exception as error:
            refused = refused_from(error)
            if refused is not None and refused is not error:
                raise refused from None
            raise
        finally:
            INSTALL_LOCK_PID.reset(token)
            lock.release()


def _run(endpoint: AdminEndpoint, st: LockedState, lock: InstallLock) -> InitResult:
    state = st.read()
    if state is None:
        if read_reservation(endpoint) is not None:
            raise SyntheticRefused("CLUSTER_NOT_FRESH", ("reservation_slot",))
        from haloflow.synthetic.attest import fresh_failures

        bad = fresh_failures(endpoint, endpoint.target)
        if bad:
            raise SyntheticRefused("CLUSTER_NOT_FRESH", bad)
        sysid, oid = cluster_identity(endpoint)
        state = _new_intent(endpoint.target, sysid, oid)
        st.write(state)
    else:
        if set(state) != STATE_KEYS or state.get("schema_version") != STATE_SCHEMA \
                or state.get("phase") not in PHASES:
            raise SyntheticRefused("STATE_CORRUPT")
        sysid, oid = cluster_identity(endpoint)
        if state["cluster_sysid"] != sysid:
            raise SyntheticRefused("CLUSTER_MISMATCH")
        if state["target"] != endpoint.target or state["target_oid"] != oid:
            raise SyntheticRefused("TARGET_MISMATCH")
    epoch = new_epoch()
    begin_run(endpoint, state["phase"], state["nonce"], epoch)
    ctx = _Ctx(endpoint, st, lock,
               state, reservation_value(state["nonce"], sysid, oid, epoch))
    _namespace_check(ctx)
    start = PHASES.index(state["phase"]) + 1
    for phase in PHASES[start:]:
        lock.check()
        if phase != "ready":
            inspect, run = _PHASES[phase]
            if inspect(ctx) != "exact":
                run(ctx)
                if phase != "staff":              # staff fires per transaction (t1..t3)
                    fire(f"phase.{phase}.after_commit", {})
        state["phase"] = phase
        st.write(state)
    return InitResult(state["phase"])


def status(environ: Mapping[str, str]) -> dict[str, Any]:
    """Read-only: phase, counts and whether the namespace still attests. No secrets."""

    check_a0(environ, attest_flag=True)
    endpoint = parse_admin_dsn(environ["HALOFLOW_SYNTHETIC_ADMIN_DSN"], environ)
    with StateDir(environ["HALOFLOW_SYNTHETIC_STATE"]).locked() as st:
        state = st.read()
        if state is None:
            raise SyntheticRefused("STATE_MISSING")
        sysid, oid = cluster_identity(endpoint)
        expected_prefix = reservation_value(state["nonce"], sysid, oid, "")
        current = read_reservation(endpoint) or ""
        ctx = _Ctx(endpoint, st, InstallLock(endpoint), state, current)
        try:
            _namespace_check(ctx)
            attested = current.startswith(expected_prefix)
        except SyntheticRefused:
            attested = False
        s = sql.Identifier(state["schema_key"])
        with admin_connect(endpoint, endpoint.target) as t:
            tenants = _one(t, "SELECT count(*) FROM shared.tenants") \
                if _exists(t, "shared.tenants") else 0
            staff = _one(t, sql.SQL("SELECT count(*) FROM {}.m01_staff").format(s)) \
                if _exists(t, f"{state['schema_key']}.m01_staff") else 0
            items = _one(t, sql.SQL("SELECT count(*) FROM {}.m05_followup_items").format(s)) \
                if _exists(t, f"{state['schema_key']}.m05_followup_items") else 0
    return {"phase": state["phase"], "tenant_id": state["tenant_id"],
            "schema_key": state["schema_key"], "tenant_count": int(tenants),
            "staff_count": int(staff), "open_items": int(items), "attested": attested}
