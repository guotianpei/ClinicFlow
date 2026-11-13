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
import json
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
from haloflow.synthetic import view
from haloflow.synthetic.attest import _DB_CHECKS, PUBLIC_ACL, admin_connect, check_a0
from haloflow.synthetic.composition import (
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
from haloflow.synthetic.installed_profile import COMPLETION, POINTS, PROFILE_ID, REPLACED
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
    "note_keys", "support", "seal"})


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
        "seal": None,
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

def _run_stamped(ctx: _Ctx) -> None:
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, \
            _phase_txn(ctx, "stamped", c):
        c.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(ctx.endpoint.target),
            sql.Literal(f"haloflow-synthetic:{ctx.nonce}")))


def _run_migrated(ctx: _Ctx) -> None:
    from haloflow.synthetic.migrate import upgrade_guarded

    before, after = upgrade_guarded(ctx.endpoint.conninfo(ctx.endpoint.target), ctx.expected,
                                    phase="migrated")
    if after != ALEMBIC_HEAD or before not in (None, ALEMBIC_HEAD):
        raise _diverged("migrated")


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


def _run_profile(ctx: _Ctx) -> None:
    with _connect(ctx.endpoint.conninfo(ctx.endpoint.target)) as c, _phase_txn(ctx, "profile", c):
        c.execute(SA_TEST_PROFILE_SQL.format(schema=ctx.schema).encode())
        c.execute(SI_TEST_PROFILE_SQL.format(schema=ctx.schema).encode())


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


# ---------------------------------------------------------------------------- reconciliation
#
# S1/S2 (Codex REVISE 20261112T190618Z; investigation v2 + v2r1). Before ANY write and after
# every phase, the owned state is captured coherently (view.capture under capture_lock) and
# reconciled:
#   profile  the normalised digest must be a point of the pinned approved installed-state
#            profile inside the segment the recorded state allows (installed_profile.POINTS);
#   seal     same-install exact values: the sealed nondeterministic values (unchanged except in
#            the approved update sets of the phases crossed), the sealed raw identities (same
#            OIDs), the sealed ledger (bounded attempt rule);
#   new rows nondeterministic values of rows not yet sealed: clocks within the install window,
#            ids are UUIDs, staff digests recomputed read-only.
# Anything else -> STATE_DIVERGED (never repaired, never adopted). The seal is persisted while
# the capture lock is held, so a takeover cannot interleave between capture and persistence.

SEGMENT_PHASES: Final = ("intent", "stamped", "migrated", "logins", "tenant", "profile",
                         "activated", "settings", "staff", "ready")
STAFF_STEPS: Final = ("staff.t1", "staff.t2", "staff.t3a", "staff.t3b")
# Phases whose transitions may legitimately change a SEALED row (row-exact rules in
# `_sealed_row_may_change`); every other sealed row keeps its nondeterministic values exactly.
APPROVED_UPDATES: Final = {
    "tenant": frozenset({"shared.tenants", "shared.schema_migrations"}),
    "settings": frozenset({"<SCHEMA>.m05_reminder_settings"}),
}
LEDGER_TABLE: Final = "shared.schema_migrations"
_CLOCKS: Final = frozenset({"started_at", "completed_at", "occurred_at", "created_at",
                            "updated_at", "status_changed_at", "granted_at", "at"})
_IDS: Final = frozenset({"record_id", "log_id"})
_CLOCK_SLACK: Final = timedelta(minutes=1)


def _utcnow() -> datetime:
    """The installer's clock (a test pin; DB clocks are never consulted for the grant window)."""
    return datetime.now(UTC)


def _grant_expired(state: Mapping[str, Any]) -> bool:
    """t034 authorizes against the recorded grant BEFORE any replay: once its window has
    passed, T2 fails closed (no silent renewal). After T2 no support authority is needed."""
    return _utcnow() >= datetime.fromisoformat(state["support"]["expires_at"])


def intent_verifier_digests(state: Mapping[str, Any]) -> dict[str, str]:
    return {suffix: hashlib.sha256(scram_verifier(
        spec["password"], spec["salt"], spec["iterations"]).encode()).hexdigest()
        for suffix, spec in state["logins"].items()}


def _completion(name: str) -> int:
    """The point index at which `name` is complete (staff completes at `ready`'s point)."""
    return int(COMPLETION["ready" if name == "staff" else name])


def _segment(state: Mapping[str, Any]) -> tuple[int, int]:
    """The point indices a capture may legitimately match for this recorded state."""
    phase = str(state["phase"])
    seal = state.get("seal")
    low = _completion(phase) if seal is None else int(seal["point"])
    if phase == "ready":
        return low, _completion("ready")
    return low, _completion(SEGMENT_PHASES[SEGMENT_PHASES.index(phase) + 1])


def _phases_crossed(low: int, high: int) -> set[str]:
    """Phases with an approved update set whose segment (prev completion, completion]
    intersects (low, high]: their sealed rows may legitimately change."""
    prev = {"tenant": "logins", "settings": "activated"}
    return {p for p in APPROVED_UPDATES
            if low < _completion(p) and high > _completion(prev[p])}


def _check_seal(state: Mapping[str, Any], cap: view.Capture, j: int) -> None:
    seal = state.get("seal")
    if seal is None:
        return                       # `intent` only: the profile alone binds a fresh baseline
    low = int(seal["point"])
    crossed = _phases_crossed(low, j)
    approved = set().union(*(APPROVED_UPDATES[p] for p in crossed)) if crossed else set()
    # Sealed nondeterministic values: byte-equal, except the exact row transitions of a crossed
    # phase (`_sealed_row_may_change`).
    for table, rows in seal["nondet"].items():
        current = cap.nondet.get(table, {})
        for key, values in rows.items():
            if current.get(key) == values:
                continue
            if table in approved and _sealed_row_may_change(table, key, values, seal, cap, state):
                continue
            raise _diverged("sealed_values")
    # Raw identities: every sealed (class, oid, identity) still present with its OID, exact if
    # there was no progress. An identity may leave only by a MEASURED transition crossed since
    # the seal (installed_profile.REPLACED: a later unit replacing an earlier unit's object).
    sealed_ids = {tuple(x) for x in seal["identities"]}
    now_ids = {tuple(x) for x in cap.identities}
    if j == low and sealed_ids != now_ids:
        raise _diverged("identities")
    missing = sealed_ids - now_ids
    if missing:
        allowed = {n for i in range(low + 1, j + 1) for n in REPLACED.get(i, ())}
        names = view.identity_names([list(x) for x in sealed_ids], state)
        if any(names[f"{x[0]}|{x[1]}"] not in allowed for x in missing):
            raise _diverged("identities")
    _check_ledger(seal["ledger"], cap.ledger)


def _sealed_row_may_change(table: str, key: str, values: Mapping[str, Any],
                           seal: Mapping[str, Any], cap: view.Capture,
                           state: Mapping[str, Any]) -> bool:
    """The ONLY sealed-row changes a crossed phase may make (row and column exact):
    - ledger: a unit sealed `running` may be retried (attempt +1: new started_at) or completed
      (same attempt: started_at unchanged, completed_at set); an `applied` unit never changes;
    - tenants: the tenant sealed `provisioning` may become `active` (created_at unchanged);
    - settings: the version-1 row may become version 2 (updated_at only).
    Newly written values are range-checked by `_check_new_rows`."""
    try:
        det = json.loads(key.removesuffix("#dup"))
    except ValueError:
        return False
    if table == "shared.schema_migrations":
        if det.get("state") != "running":
            return False
        unit = det.get("migration_id")
        prior = next((r for r in seal["ledger"] if r[0] == unit), None)
        rows = [r for r in cap.rows.get(LEDGER_TABLE, []) if r.get("migration_id") == unit]
        if prior is None or len(rows) != 1:
            return False
        row = rows[0]
        if int(row["attempt"]) == int(prior[2]):
            return bool(row.get("started_at") == values.get("started_at"))
        return int(row["attempt"]) == int(prior[2]) + 1
    if table == "shared.tenants":
        rows = [r for r in cap.rows.get("shared.tenants", [])
                if r.get("tenant_id") == state["tenant_id"]]
        return det.get("lifecycle_state") == "provisioning" and len(rows) == 1 \
            and rows[0].get("created_at") == values.get("created_at") \
            and rows[0].get("lifecycle_state") == "active"
    if table == "<SCHEMA>.m05_reminder_settings":
        return bool(det.get("settings_version") == 1)
    return False


def _check_ledger(sealed: list[list[Any]], current: list[list[Any]]) -> None:
    before = {r[0]: r for r in sealed}
    for unit, state, attempt, error in current:
        if error is not None or state not in ("running", "applied"):
            raise _diverged("ledger")
        prior = before.pop(unit, None)
        if prior is None:
            if attempt != 1:
                raise _diverged("ledger")
        elif prior[1] == "applied":
            if [unit, state, attempt, error] != list(prior):
                raise _diverged("ledger")
        elif attempt not in (prior[2], prior[2] + 1):
            raise _diverged("ledger")
    if before:
        raise _diverged("ledger")


def _check_new_rows(ctx: _Ctx, cap: view.Capture, conn: psycopg.Connection[Any]) -> None:
    """Nondeterministic values of rows not covered by the seal."""
    seal = ctx.state.get("seal") or {"nondet": {}}
    floor = datetime.fromisoformat(ctx.state["support"]["starts_at"]) - _CLOCK_SLACK
    end = _utcnow() + _CLOCK_SLACK
    seen_ids: set[str] = set()
    for table, rows in cap.nondet.items():
        sealed = seal["nondet"].get(table, {})
        for key, values in rows.items():
            if key.endswith("#dup"):
                raise _diverged("duplicate")
            if sealed.get(key) == values:
                continue
            for column, value in values.items():
                if column in _CLOCKS or column == "__nested__":
                    stamps = [value] if column != "__nested__" else [
                        v for group in value for v in group]
                    for stamp in stamps:
                        if stamp is None:
                            continue
                        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                        if not floor <= moment <= end:
                            raise _diverged("clock")
                elif column in _IDS:
                    if str(uuid.UUID(str(value))) != value or value in seen_ids:
                        raise _diverged("identifier")
                    seen_ids.add(value)
    _check_staff_digests(ctx, cap, conn)


_ADMIN_DIGEST: Final = (
    "SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
    "'[\"m01.staff_admin/v2\",' || pg_catalog.to_json(%(tenant)s::text)::text"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'client_action_id')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb #>> '{attribution,principal_id}')::text,"
    " 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'operation')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'staff_id')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'display_name')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'access')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'role')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'issuer')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'subject')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'setting')::text, 'null')"
    " || ',' || COALESCE(pg_catalog.to_json(%(c)s::jsonb ->> 'value')::text, 'null')"
    " || ']', 'UTF8')), 'hex')")
# t034 (l1w_w3b_units.py, `m01_staff_bootstrap_first_admin`): the same canonical expression.
_BOOTSTRAP_DIGEST: Final = (
    "SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
    "'[\"m01.staff_bootstrap_first_admin/v1\",' || pg_catalog.to_json(%(tenant)s::text)::text"
    " || ',' || pg_catalog.to_json(%(key)s::text)::text"
    " || ',' || pg_catalog.to_json(%(actor)s::text)::text"
    " || ',\"create_first_admin\"'"
    " || ',' || pg_catalog.to_json(%(staff)s::text)::text"
    " || ',' || pg_catalog.to_json(%(name)s::text)::text || ']', 'UTF8')), 'hex')")


def _staff_access_command(ctx: _Ctx, access: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "client_action_id": ctx.uuid(f"staff-access-{access}"),
        "staff_id": ctx.state["staff_id"], "operation": "grant", "access": access,
        "permissions": {"staff_admin": True},
        "attribution": {"principal_kind": "actor", "principal_id": ctx.state["staff_id"],
                        "auth_method": "synthetic_init",
                        "execution_id": ctx.uuid("staff-execution"),
                        "request_id": None, "purpose_code": "operations"}}


def _check_staff_digests(ctx: _Ctx, cap: view.Capture, conn: psycopg.Connection[Any]) -> None:
    """Recompute (read-only) the canonical digests the existing staff functions store."""
    schema, tenant = ctx.schema, ctx.state["tenant_id"]
    expected: dict[str, str] = {}
    key = ctx.uuid("staff-first-admin")
    expected[key] = str(_one(conn, _BOOTSTRAP_DIGEST, {
        "tenant": tenant, "key": key, "actor": ctx.state["support"]["actor_id"],
        "staff": ctx.state["staff_id"], "name": STAFF_NAME}))
    for access in ("read", "act"):
        command = _staff_access_command(ctx, access)
        expected[command["client_action_id"]] = str(_one(conn, _ADMIN_DIGEST, {
            "tenant": tenant, "c": json.dumps(command)}))
    for table in ("m01_staff_bootstrap_keys", "m01_staff_action_keys", "m01_staff_admin_log"):
        for row in cap.rows.get(f"{schema}.{table}", []):
            want = expected.get(str(row.get("client_action_id")))
            got = str(row.get("digest", ""))
            if want is None or got != "\\x" + want:
                raise _diverged("staff_digest")


def _reconcile(ctx: _Ctx, conn: psycopg.Connection[Any]) -> tuple[int, view.Capture]:
    """Capture (caller holds capture_lock), check epoch, profile segment and seal."""
    cap = view.capture(conn, ctx.state, GROUP_ROLES, intent_verifier_digests(ctx.state))
    if cap.reservation != ctx.expected:
        raise SyntheticRefused("EPOCH_SUPERSEDED")
    low, high = _segment(ctx.state)
    matches = [i for i in range(low, high + 1) if POINTS[i] == cap.profile_digest]
    if not matches:
        raise _diverged("profile")
    j = max(matches)
    _check_seal(ctx.state, cap, j)
    _check_new_rows(ctx, cap, conn)
    return j, cap


def _new_seal(j: int, cap: view.Capture) -> dict[str, Any]:
    return {"profile": PROFILE_ID, "point": j, "nondet": cap.nondet,
            "identities": cap.identities, "ledger": cap.ledger}


def _persist(ctx: _Ctx, phase: str, j: int, cap: view.Capture) -> None:
    ctx.state["phase"] = phase
    ctx.state["seal"] = _new_seal(j, cap)
    ctx.st.write(ctx.state)


def _check_seal_shape(state: Mapping[str, Any]) -> None:
    seal = state.get("seal")
    if seal is None:
        if state["phase"] != "intent":
            raise SyntheticRefused("STATE_CORRUPT")
        return
    if not isinstance(seal, dict) or set(seal) != {"profile", "point", "nondet", "identities",
                                                    "ledger"}:
        raise SyntheticRefused("STATE_CORRUPT")
    if seal["profile"] != PROFILE_ID or not isinstance(seal["point"], int) \
            or not 0 <= seal["point"] < len(POINTS):
        raise SyntheticRefused("STATE_CORRUPT")
    phase = str(state["phase"])
    low = _completion(phase)
    high = _completion("ready") if phase == "ready" else _completion(
        SEGMENT_PHASES[SEGMENT_PHASES.index(phase) + 1])
    if not low <= seal["point"] <= high:
        raise SyntheticRefused("STATE_CORRUPT")


# ---------------------------------------------------------------------------- staff (T1-T3)

def _run_staff_t1(ctx: _Ctx) -> None:
    sup = ctx.state["support"]
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


def _run_staff_t2(ctx: _Ctx) -> None:
    command = {"schema_version": 1, "client_action_id": ctx.uuid("staff-first-admin"),
               "staff_id": ctx.state["staff_id"], "display_name": STAFF_NAME}
    with _connect(ctx.login_conninfo("support")) as c, _phase_txn(ctx, "staff.t2", c):
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (ctx.state["tenant_id"],))
        outcome = _one(c, sql.SQL("SELECT {}.m01_staff_bootstrap_first_admin(%s)").format(
            sql.Identifier(ctx.schema)), (Jsonb(command),))
        if not isinstance(outcome, dict) or outcome.get("outcome") != "BOOTSTRAPPED":
            raise _diverged("staff")


def _run_staff_t3(ctx: _Ctx, access: str) -> None:
    point = "staff.t3a" if access == "read" else "staff.t3b"
    with _connect(ctx.login_conninfo("runtime")) as c, _phase_txn(ctx, point, c):
        c.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(RUNTIME_ROLE)))
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (ctx.state["tenant_id"],))
        result = _one(c, sql.SQL("SELECT {}.m01_staff_admin_v2(%s)").format(
            sql.Identifier(ctx.schema)), (Jsonb(_staff_access_command(ctx, access)),))
        if not isinstance(result, dict) or "staff" not in result:
            raise _diverged("staff")


_STEP_RUN: Final[dict[str, Callable[[_Ctx], None]]] = {
    "stamped": _run_stamped, "migrated": _run_migrated, "logins": _run_logins,
    "tenant": _run_tenant, "profile": _run_profile, "activated": _run_activated,
    "settings": _run_settings, "staff.t1": _run_staff_t1, "staff.t2": _run_staff_t2,
    "staff.t3a": lambda ctx: _run_staff_t3(ctx, "read"),
    "staff.t3b": lambda ctx: _run_staff_t3(ctx, "act"),
}


def _steps(phase: str) -> tuple[str, ...]:
    return STAFF_STEPS if phase == "staff" else (phase,)


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


def _check_state(state: Mapping[str, Any]) -> None:
    if set(state) != STATE_KEYS or state.get("schema_version") != STATE_SCHEMA \
            or state.get("phase") not in PHASES:
        raise SyntheticRefused("STATE_CORRUPT")
    _check_seal_shape(state)


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
        _check_state(state)
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
    # Reconcile the recorded state before any write. A validated change since the seal (an
    # interrupted run's committed work) is persisted as an observation under the lock.
    with view.capture_lock(endpoint) as conn:
        j, cap = _reconcile(ctx, conn)
        if j < int(COMPLETION["staff.t2"]) and state["phase"] in ("settings", "staff") \
                and _grant_expired(state):
            raise SyntheticRefused("STATE_DIVERGED", ("support_grant_expired",))
        if state.get("seal") is not None and _new_seal(j, cap) != state["seal"]:
            lock.check()                        # an interrupted run's validated work
            _persist(ctx, str(state["phase"]), j, cap)
    start = PHASES.index(state["phase"]) + 1
    for phase in PHASES[start:]:
        lock.check()
        if phase != "ready":
            for step in _steps(phase):
                if j >= int(COMPLETION[step]):
                    continue                              # committed already (acknowledged)
                if step == "staff.t2" and _grant_expired(state):
                    raise SyntheticRefused("STATE_DIVERGED", ("support_grant_expired",))
                _STEP_RUN[step](ctx)
                fire(f"phase.{step}.after_commit", {})
                j = int(COMPLETION[step])
        with view.capture_lock(endpoint) as conn:
            j, cap = _reconcile(ctx, conn)
            if j != _completion(phase):
                raise _diverged("profile")
            lock.check()
            _persist(ctx, phase, j, cap)
    return InitResult(state["phase"])


def status(environ: Mapping[str, str]) -> dict[str, Any]:
    """Read-only (advisory session lock and READ ONLY snapshots only). No secrets."""

    check_a0(environ, attest_flag=True)
    endpoint = parse_admin_dsn(environ["HALOFLOW_SYNTHETIC_ADMIN_DSN"], environ)
    with StateDir(environ["HALOFLOW_SYNTHETIC_STATE"]).locked() as st:
        state = st.read()
        if state is None:
            raise SyntheticRefused("STATE_MISSING")
        _check_state(state)
        sysid, oid = cluster_identity(endpoint)
        if state["cluster_sysid"] != sysid:
            raise SyntheticRefused("CLUSTER_MISMATCH")
        if state["target"] != endpoint.target or state["target_oid"] != oid:
            raise SyntheticRefused("TARGET_MISMATCH")
        expected_prefix = reservation_value(state["nonce"], sysid, oid, "")
        current = read_reservation(endpoint) or ""
        ctx = _Ctx(endpoint, st, InstallLock(endpoint), state, current)
        sealed, pending = False, None
        try:
            _namespace_check(ctx)
            namespace_ok = True
        except SyntheticRefused:
            namespace_ok = False
        with view.capture_lock(endpoint) as conn:
            try:
                j, _cap = _reconcile(ctx, conn)
                seal_point = _completion(str(state["phase"])) if state.get("seal") is None \
                    else int(state["seal"]["point"])
                sealed = j == seal_point == _completion(str(state["phase"]))
                if j > _completion(str(state["phase"])):
                    nxt = SEGMENT_PHASES[SEGMENT_PHASES.index(str(state["phase"])) + 1]
                    pending = nxt
            except SyntheticRefused:
                sealed = False
        attested = bool(current.startswith(expected_prefix) and sealed and namespace_ok)
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
            "staff_count": int(staff), "open_items": int(items), "attested": attested,
            "sealed": sealed, "pending_effect": pending}
