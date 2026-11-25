"""Incoming fax routing FX-IN, package FX1 — test support (TEST ONLY). RED BY DESIGN.

Governing records (Shared Workspace, `Work Session 2026-11-24/` and `Work Session 2026-11-25/`):
- requirements `claude_fax-inbound-requirements-v1r1-2026-11-24.md` (Codex ALIGN 8e064b80)
- architecture `claude_fax-inbound-architecture-v1r2-2026-11-25.md` (Codex ALIGN 469d7802)
- test cases `claude_fax-inbound-test-cases-v1-2026-11-25.md` (Codex 65d1f9db: FX1 ALIGN for red
  tests, with the clarifications in `codex_fax-test-cases-v1-review.md`)
- owner decisions OD-NX3, OD-FX1..FX9 (FX8 filed is final; FX9 sorting section on the fax screen).
Baseline `main` 4529d15. Synthetic only; simulated provider only; no OCR; production registry {36}
unchanged.

Red classes (as E4/E5): C control (passes on the baseline), A absence-red (fails in its own body on
the missing FX1 interface: `build_synthetic_registry(include_fx1=...)` raises TypeError, reported as
"FX1 test installation failed"; the pure rows fail on ModuleNotFoundError for
`haloflow.m05.fax_rules`).

FX1 interface pinned here (red packet v1, section 2):

Registry: `build_synthetic_registry(include_el1..include_el5=True, include_fx1=True)` = the
eligibility registry (target 135) + t136..t139, target **139**; `include_fx1` without
`include_el5` -> ValueError. `build_synthetic_catalog(..., include_fx1=True)` likewise. The
default (97) and eligibility (135) registries are unchanged (no `fax` profile until FX3).

Unit ids (one function per unit, house rule):
- `t136_m05_fx_tables`            tables (architecture v1r2 section 4), the seven OD-FX1 queues,
                                  `m05_fx_workloads (workload_id uuid PK)` (the fax ingress
                                  workload allow-list, test profile `FX_TEST_PROFILE_SQL`),
                                  `m05_fx_settings_audit`; no runtime table privilege.
- `t137_m05_fx_settings_authority` `m05_fx_settings_authority() RETURNS jsonb` — the t074 pattern:
                                  staff owner; EXECUTE to the M05 owner only; authorized iff the
                                  session login is a support operator with an ACTIVE, in-window
                                  `shared.support_access_grants` row for THIS tenant holding
                                  capability `m05.fax.settings`.
- `t138_m05_fx_settings_put`       `m05_fx_settings_put(jsonb) RETURNS jsonb`, EXECUTE to
                                  `haloflow_support` only. Ops (command `op`):
                                  queue_put {name, position} -> {outcome: applied, queue_id};
                                  queue_deactivate {queue_id}; queue_rename {queue_id, name}
                                  -> {outcome: applied, queue_id} (same ID; OD-FX1 rename, Codex
                                  REVISE 7859c910); rule_put {sender|null, line|null,
                                  queue_id} -> {outcome: applied, rule_id}; rule_remove {rule_id}.
                                  Authority before any lock or write AND re-checked after the
                                  target row lock (queue row FOR UPDATE for queue ops incl. rename;
                                  target queue FOR SHARE for rule_put). One audit row per op
                                  (actor_id, grant_id, op, at).
- `t139_m05_fx_receive`            `m05_fx_receive(jsonb) RETURNS jsonb`, EXECUTE to the runtime.
                                  Command {schema_version 1, provider, provider_fax_id, line,
                                  sender|null, pages, received_at, doc_ref, attribution (workload,
                                  principal in m05_fx_workloads)}. Result {schema_version 1,
                                  outcome in routed | needs_sorting | duplicate |
                                  duplicate_mismatch, fax_id, queue_id|null, level|null,
                                  reason|null}. One transaction: fax row, sorting row, first
                                  history row (architecture 5.1, 5.4).
Refusals: P0001 with message = code: COMMAND_MALFORMED, WORKLOAD_NOT_AUTHORIZED,
LINE_NOT_REGISTERED, SUPPORT_NOT_AUTHORIZED, QUEUE_NAME_TAKEN, QUEUE_NOT_FOUND, RULE_NOT_FOUND,
RULE_DUPLICATE. A non-support role calling t138 -> 42501.

Command field rules: provider ^[a-z][a-z0-9_]{0,31}$; provider_fax_id 1..128 chars [\\x21-\\x7e];
line and sender canonical E.164 ^\\+[1-9][0-9]{7,14}$ (sender may be null); pages integer 1..999;
received_at `YYYY-MM-DDTHH:MM:SS.ffffffZ`; doc_ref ^syn-doc-[A-Za-z0-9-]{1,64}$.

`haloflow.m05.fax_rules` (pure, new module):
  INVALID_NUMBER = "INVALID_NUMBER"
  canonical_e164(raw: str) -> str      NANP digits/punctuation -> "+1XXXXXXXXXX"; a leading "+"
                                       keeps its country code; else ValueError(INVALID_NUMBER)
  Rule(sender: str | None, line: str | None, queue_id: str)   frozen dataclass
  level_of(rule) -> int                (1 both, 2 sender only, 3 line only)
  decide(rules, *, sender, line) -> Decision(level: int | None, queue_ids: frozenset[str])
                                       the deciding level and its DISTINCT destinations; sender None
                                       skips levels 1-2; no match -> Decision(None, frozenset())
  META_TAG = "m05.fx.meta/v1"
  meta_digest(*, line, sender, pages, received_at, doc_ref) -> bytes   sha256 over the UTF-8 of
                                       META_TAG, line, sender or "", str(pages), received_at,
                                       doc_ref joined by U+001F (SQL computes the same bytes).
  K_FX_LINES: int                      the install-wide advisory lock key (used from FX2).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final

import m02_support
import psycopg
import sa_support as sa
from psycopg import sql
from psycopg.types.json import Jsonb

FX1_TARGET: Final = 139
FX1_SUPPORTED: Final = range(FX1_TARGET, FX1_TARGET + 1)
ELIGIBILITY_TARGET: Final = 135
FX1_IDS: Final = ("t136_m05_fx_tables", "t137_m05_fx_settings_authority",
                  "t138_m05_fx_settings_put", "t139_m05_fx_receive")
RECEIVE: Final = "m05_fx_receive"
SETTINGS_PUT: Final = "m05_fx_settings_put"
AUTHORITY: Final = "m05_fx_settings_authority"
SETTINGS_CAP: Final = "m05.fax.settings"
SUPPORT_ROLE: Final = "haloflow_support"

# The fax ingress workload (fixture identity; FX3 composes the synthetic one).
W_FAX: Final = "00000000-0000-4000-8000-0000000fa001"
FX_TEST_PROFILE_SQL: Final = (
    "INSERT INTO {schema}.m05_fx_workloads (workload_id) VALUES ('" + W_FAX + "');")

DEFAULT_QUEUES: Final = ("Labs/Results", "Imaging", "Referrals & consults", "Records requests",
                         "Insurance/Prior auth", "Pharmacy/Refills", "General")  # OD-FX1
REASONS: Final = ("no_matching_rule", "sender_not_shown", "rules_conflict")  # OD-FX7 codes
FX_TABLES: Final = ("m05_fx_queues", "m05_fx_lines", "m05_fx_rules", "m05_fx_faxes",
                    "m05_fx_sorting", "m05_fx_history", "m05_fx_discrepancies",
                    "m05_fx_settings_audit", "m05_fx_workloads")

LINE1: Final = "+15405550101"
LINE2: Final = "+15405550102"
SENDER: Final = "+18045550199"
SENDER2: Final = "+18045550198"
PROVIDER: Final = "simfax"


def eligibility_flags() -> dict[str, bool]:
    return {"include_el1": True, "include_el2": True, "include_el3a": True,
            "include_el3b": True, "include_el4": True, "include_el5": True}


def fx1_registry() -> Any:
    from haloflow.synthetic.composition import build_synthetic_registry

    return build_synthetic_registry(**eligibility_flags(), include_fx1=True)  # type: ignore[call-arg]


def fx1_catalog() -> Any:
    from haloflow.synthetic.composition import build_synthetic_catalog

    return build_synthetic_catalog(**eligibility_flags(), include_fx1=True)  # type: ignore[call-arg]


class FX1Install(sa.SAInstall):
    """A synthetic tenant at target 139 (eligibility 135 + FX1), provisioned in the row body
    (class-A red rule). Lines are seeded by the privileged fixture `seed_line` (Codex: FX1 must
    not depend on the FX2 provisioner helper)."""

    def require(self) -> None:
        if not self._attempted:
            self._attempted = True
            try:
                from staff_support import staff_manifest

                m02_support.provision_sync(self.ids, fx1_registry(), self.tenant,
                                           supported=FX1_SUPPORTED, manifest=staff_manifest())
                sa.admin(self, FX_TEST_PROFILE_SQL.format(schema=self.schema))
            except Exception as error:  # noqa: BLE001 - reported in each row's body
                self._error = error
        if self._error is not None:
            code = getattr(self._error, "reason_code", None)
            raise AssertionError(
                f"FX1 test installation failed: {type(self._error).__name__}"
                + (f" ({code})" if code else "")
            ) from self._error


def module_install(request: Any, ids: m02_support.Identities,
                   reset: Callable[[tuple[str, str]], None], label: str) -> Iterator[FX1Install]:
    tenant = m02_support.tenant_for(f"{request.module.__name__}:{label}")
    reset(tenant)
    try:
        yield FX1Install(ids, tenant)
    finally:
        reset(tenant)


# --- fixtures (privileged) ------------------------------------------------------------------


def seed_line(install: sa.SAInstall, line: str) -> None:
    """FIXTURE: the privileged line write (FX1 only; FX2 exercises the real provisioner)."""
    sa.admin(install, sa.q(install, "INSERT INTO {s}.m05_fx_lines (line_e164) VALUES (%s)"),
             (line,))


def queue_id(install: sa.SAInstall, name: str) -> str:
    rows = sa.admin(install, sa.q(install, "SELECT queue_id::text FROM {s}.m05_fx_queues"
                                           " WHERE name = %s"), (name,))
    assert len(rows) == 1, (name, rows)
    return str(rows[0][0])


def seed_rule(install: sa.SAInstall, *, sender: str | None, line: str | None, queue: str
              ) -> str:
    """FIXTURE: a privileged rule write (routing rows; the support path has its own rows)."""
    rows = sa.admin(install, sa.q(install, "INSERT INTO {s}.m05_fx_rules (rule_id, sender_e164,"
                                           " line_e164, queue_id) VALUES (gen_random_uuid(), %s,"
                                           " %s, %s::uuid) RETURNING rule_id::text"),
                    (sender, line, queue_id(install, queue)))
    return str(rows[0][0])


def deactivate_raw(install: sa.SAInstall, queue: str) -> None:
    sa.admin(install, sa.q(install, "UPDATE {s}.m05_fx_queues SET active = false WHERE name = %s"),
             (queue,))


# --- commands -------------------------------------------------------------------------------


def received_at(offset_s: int = 0) -> str:
    return f"2026-11-25T13:{offset_s // 60 % 60:02d}:{offset_s % 60:02d}.000000Z"


def fax_cmd(provider_fax_id: str | None = None, *, line: str = LINE1,
            sender: str | None = SENDER, pages: int = 2, received: str | None = None,
            doc_ref: str | None = None, provider: str = PROVIDER,
            principal: str = W_FAX) -> dict[str, Any]:
    fid = provider_fax_id or f"FX-{uuid.uuid4().hex[:16]}"
    return {"schema_version": 1, "provider": provider, "provider_fax_id": fid, "line": line,
            "sender": sender, "pages": pages, "received_at": received or received_at(),
            "doc_ref": doc_ref or f"syn-doc-{fid[-8:]}",
            "attribution": sa.attribution(principal)}


def receive(install: sa.SAInstall, command: dict[str, Any]) -> dict[str, Any]:
    return sa.call(install, RECEIVE, command)


def settings_cmd(op: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": 1, "op": op, **fields}


def call_as(conninfo: str, install: sa.SAInstall, function: str, command: Any
            ) -> dict[str, Any]:
    with psycopg.connect(conninfo) as conn:
        result = sa.call_in(conn, install, function, command)
        conn.commit()
    return result


# --- observation ----------------------------------------------------------------------------


def snapshot(install: sa.SAInstall) -> dict[str, list[str]]:
    """Zero side effects (ZSE): the full contents of every FX table."""
    out: dict[str, list[str]] = {}
    for table in FX_TABLES:
        out[table] = sorted(row[0] for row in sa.admin(
            install, sa.q(install, "SELECT to_jsonb(t)::text FROM {s}." + table + " AS t")))
    return out


def rows(install: sa.SAInstall, table: str, where: str = "true", params: Any = None
         ) -> list[dict[str, Any]]:
    return [r[0] for r in sa.admin(install, sa.q(
        install, "SELECT to_jsonb(t) FROM {s}." + table + " AS t WHERE " + where), params)]


def fax(install: sa.SAInstall, fax_id: str) -> dict[str, Any]:
    found = rows(install, "m05_fx_faxes", "fax_id = %s::uuid", (fax_id,))
    assert len(found) == 1, found
    return found[0]


def code_of(error: BaseException) -> str:
    assert isinstance(error, psycopg.Error), repr(error)
    return str(error.diag.message_primary)


def refused(install: sa.SAInstall, function: str, command: Any, code: str, *,
            conninfo: str | None = None) -> None:
    """The call raises P0001 `code` and every FX table is unchanged (ZSE)."""
    before = snapshot(install)
    try:
        if conninfo is None:
            sa.call(install, function, command)
        else:
            call_as(conninfo, install, function, command)
    except psycopg.Error as error:
        assert (error.sqlstate, error.diag.message_primary) == ("P0001", code), (
            error.sqlstate, error.diag.message_primary)
    else:
        raise AssertionError(f"expected P0001 {code}")
    assert snapshot(install) == before, "a refused call wrote something"


@contextmanager
def row_locked(install: sa.SAInstall, table: str, where: str, params: Any,
               mode: str = "FOR UPDATE") -> Iterator[None]:
    """FIXTURE held lock on an admin transaction (bounded by the waiter's join timeout)."""
    with psycopg.connect(install.ids.admin) as conn, conn.transaction():
        conn.execute(sa.q(install, "SELECT 1 FROM {s}." + table + " WHERE " + where + " "
                                   + mode), params)
        yield


class LoginWaiter:
    """A call on an arbitrary login in a thread that must be OBSERVED blocked on a lock
    (pg_stat_activity wait_event_type = 'Lock') before the row proceeds (Codex S04/S05)."""

    def __init__(self, install: sa.SAInstall, conninfo: str, function: str, command: Any,
                 *, set_role: str | None = None) -> None:
        self.install = install
        self.name = f"fx1-waiter-{uuid.uuid4().hex[:12]}"
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                with psycopg.connect(conninfo, autocommit=True,
                                     application_name=self.name) as conn:
                    if set_role:
                        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(set_role)))
                    conn.execute("SET lock_timeout = '20s'")  # bounded: a blocking mutant fails
                    with conn.transaction():
                        self.result = sa.call_in(conn, install, function, command)
            except BaseException as error:  # noqa: BLE001 - re-raised in join()
                self.error = error

        self.thread = threading.Thread(target=run, daemon=True)

    def start_and_block(self) -> LoginWaiter:
        self.thread.start()
        sa.wait_until_blocked(self.install, self.name)
        return self

    def start(self) -> LoginWaiter:
        self.thread.start()
        return self

    def join(self) -> dict[str, Any]:
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "waiter did not finish"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def runtime_conninfo(install: sa.SAInstall) -> str:
    return str(install.ids.logins[sa.RUNTIME])


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def wait(predicate: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)
