"""L-1S package S-D (service-chain integration, tests only): shared support.

Governing: S-D package v1 (claude_sd-service-chain-package-v1-2026-11-06.md) as corrected
by v1r1 (claude_sd-service-chain-package-v1r1-2026-11-07.md, sha256 de75251f...), Codex
ALIGN 20261107T121737Z (implementation pins). Synthetic data only.

S-D is SERVICE-CHAIN integration: directory -> dispatch -> reply or no-reply -> item ->
staff claim -> staff resolve, through the public services, on one S-C TEST runtime
(target 46) with ActivationPolicy.ENFORCE and the real resolver and gateway. Reply class
and dispatch are supplied facts (no sender, ingress or classifier); resolution is a staff
assertion (no EHR write). No source change; nothing here is a production composition.

Isolation (v1r1 C2): the runner walks every awaiting due window of the tenant and the
activation state is the latest t042 record, so every scenario starts with `begin()`:
the existing S-B fixture `sb.close_all_awaiting` plus an explicit grant/revoke, BEFORE
any row snapshot is taken.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final

import m05_support as m05
import sa_support as sa
import sb_runtime as sr
import sb_support as sb
import sc_support as sc
import service_support as sv
import staff_support as st

STAFF: Final = m05.S1  # the resolving staff member (active, read + act)
T_DISPATCHED: Final = sa.T_DISPATCHED
T_REPLY: Final = sa.T_REPLY
T_NO_REPLY: Final = sa.T_NO_REPLY
T_RESOLVED: Final = sv.RESOLUTION_TYPE
ITEM_LINK: Final = "m05_followup_item_v1"


# --- runtime (copied from the S-C local `_runtime`, so S-C tests stay byte-unchanged) ----


@asynccontextmanager
async def runtime(install: sc.SCInstall, *, policy: str = "ENFORCE",
                  pool_class: Any = None) -> AsyncIterator[Any]:
    from haloflow.m01.activation import ActivationPolicy
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    cls = pool_class or TenantPool
    pool = cls(install.ids.logins[sa.RUNTIME], min_size=1, max_size=3)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=sv._factory(install.ids.logins[sa.MIGRATOR]),
                provisioner_connect=sv._factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=sc.sc_registry(),
            catalog=sc.sc_catalog(),
            supported_schema_versions=frozenset({sc.SC_TARGET}),
            activation_policy=ActivationPolicy[policy],
        )
    finally:
        await pool.close()


# --- install preparation and scenario start ------------------------------------------------


def prepare(install: sc.SCInstall) -> None:
    """Install (S-C registry, S-A test profile), then once per module: the B4 resolution
    profile (the S-A profile does not apply it) and the resolving staff member."""

    install.require()
    if getattr(install, "_sd_ready", False):
        return
    sa.admin(install, m05.B4_RESOLUTION_PROFILE_SQL.format(schema=install.schema))
    st.seed_staff(install, STAFF, name="SD Staff")  # type: ignore[arg-type]
    install._sd_ready = True  # type: ignore[attr-defined]


def begin(install: sc.SCInstall, activation: str = "grant") -> None:
    """Scenario start (v1r1 C2): retire every awaiting window of the module tenant
    (existing S-B fixture), then set the activation state explicitly."""

    prepare(install)
    sb.close_all_awaiting(install)  # type: ignore[arg-type]
    sc.change(install, activation)


# --- exact row counts (deltas, never "one row each") --------------------------------------

_COUNTS: Final = (
    ("windows", "SELECT state, count(*) FROM {s}.m05_reminder_windows GROUP BY state"),
    ("replies", "SELECT 'all', count(*) FROM {s}.m05_inbound_replies"),
    ("opted_out", "SELECT 'all', count(*) FROM {s}.m05_contact_state WHERE opted_out"),
    ("events", "SELECT event_type, count(*) FROM {s}.patient_events GROUP BY event_type"),
    ("links", "SELECT link_type, count(*) FROM {s}.patient_event_links GROUP BY link_type"),
    ("outbox", "SELECT 'all', count(*) FROM {s}.event_handoff_outbox"),
    ("items", "SELECT item_kind || ':' || state, count(*) FROM {s}.m05_followup_items"
              " GROUP BY item_kind, state"),
    ("history", "SELECT 'all', count(*) FROM {s}.m05_followup_history"),
    ("actions", "SELECT 'all', count(*) FROM {s}.m05_followup_actions"),
    ("notes", "SELECT 'all', count(*) FROM {s}.m05_followup_notes"),
    ("activation", "SELECT 'all', count(*) FROM {s}.m05_activation_records"),
)


def counts(install: sc.SCInstall) -> Counter[str]:
    out: Counter[str] = Counter()
    for name, text in _COUNTS:
        for key, value in sa.admin(install, sa.q(install, text)):
            out[f"{name}" if key == "all" else f"{name}:{key}"] = int(value)
    return out


def delta(before: Counter[str], after: Counter[str]) -> dict[str, int]:
    """Exact non-zero differences per counted key."""

    keys = set(before) | set(after)
    return {k: after[k] - before[k] for k in sorted(keys) if after[k] != before[k]}


def assert_no_change(change: dict[str, int]) -> None:
    """The zero-delta oracle (SD-05/06/07/08)."""

    assert change == {}, change


# Expected per-call deltas (v1 section 3 with v1r1 C3 corrections).
DISPATCH: Final = {"windows:awaiting": 1, f"events:{T_DISPATCHED}": 1, "outbox": 1}
CONFIRM: Final = {"replies": 1, "windows:awaiting": -1, "windows:replied": 1,
                  f"events:{T_REPLY}": 1, "outbox": 1}


def reply_item(kind: str) -> dict[str, int]:
    return {"replies": 1, "windows:awaiting": -1, "windows:replied": 1,
            f"events:{T_REPLY}": 1, "outbox": 1, f"items:{kind}:open": 1, "history": 1}


NO_REPLY: Final = {"windows:awaiting": -1, "windows:no_reply": 1, f"events:{T_NO_REPLY}": 1,
                   "outbox": 1, "items:no_reply:open": 1, "history": 1}
CLAIM: Final = {"history": 1, "actions": 1}  # claim changes the assignee only


def resolve(kind: str) -> dict[str, int]:
    return {f"items:{kind}:open": -1, f"items:{kind}:resolved": 1, f"events:{T_RESOLVED}": 1,
            f"links:{ITEM_LINK}": 1, "outbox": 1, "history": 1, "actions": 1}


# --- the chain ----------------------------------------------------------------------------


class Chain:
    """One scenario's public services and contexts on one runtime."""

    def __init__(self, install: sc.SCInstall, rt: Any, *, gateway: Any = None) -> None:
        from haloflow.m02.observation import EventFoundation
        from haloflow.m05.intake import IntakeService
        from haloflow.m05.service import M05Service

        self.install = install
        self.rt = rt
        self.gateway = gateway or rt.gateway
        self.intake = IntakeService(EventFoundation(self.gateway, producer_service=sa.PRODUCER))
        self.keys = sv.TestNoteKeys()
        self.staff = M05Service(
            self.gateway,
            foundation=EventFoundation(self.gateway, producer_service=sv.PRODUCER),
            keys=self.keys)

    async def workload(self, call: str) -> Any:
        principal = {"dispatch": sa.W_DISPATCH, "inbound": sa.W_INGRESS,
                     "close": sa.W_TIMER}[call]
        return await sv.context(self.rt, self.install, principal, sa.CAPS[call],
                                kind="workload")

    async def actor(self, caps: frozenset[str] = sv.RESOLVE_CAPS) -> Any:
        return await sv.context(self.rt, self.install, STAFF, caps)

    async def dispatch(self, ref: str, reminder: uuid.UUID | None = None) -> Any:
        from haloflow.m05.intake import DispatchCommand

        return await self.intake.register_dispatch(
            await self.workload("dispatch"),
            DispatchCommand(reminder_id=reminder or uuid.uuid4(),
                            appointment_ref=uuid.UUID(ref)))

    async def inbound(self, phone: str, reply_class: str, message: str | None = None) -> Any:
        from haloflow.m05.intake import InboundCommand

        return await self.intake.register_inbound(
            await self.workload("inbound"),
            InboundCommand(channel="sms", provider_message_id=message or sa.new_id(),
                           phone=phone, reply_class=reply_class))

    async def close(self, reminder: uuid.UUID) -> Any:
        return await self.intake.close_window(await self.workload("close"), reminder)

    def runner(self, gate: Any = None) -> tuple[Any, Any]:
        return sr.runner(self.install, self.rt, page=10, cap=50,  # type: ignore[arg-type]
                         gate=gate)

    async def run_once(self, gate: Any = None) -> Any:
        r, _ = self.runner(gate)
        return await r.run_once(self.install.tenant_id, uuid.uuid4())

    async def claim(self, item: uuid.UUID, key: uuid.UUID | None = None,
                    version: int = 1) -> Any:
        from haloflow.m05.service import StaffAction

        return await self.staff.act(await self.actor(), StaffAction(
            item_id=item, client_action_id=key or uuid.uuid4(), expected_version=version,
            type="claim"))

    async def resolve(self, item: uuid.UUID, outcome: str, key: uuid.UUID | None = None,
                      version: int = 2) -> Any:
        from haloflow.m05.service import ResolveAction

        return await self.staff.act(await self.actor(), ResolveAction(
            item_id=item, client_action_id=key or uuid.uuid4(), expected_version=version,
            type="resolve", outcome_code=outcome, note=None))

    async def get(self, item: uuid.UUID) -> Any:
        return await self.staff.get(await self.actor(), item)

    async def history(self, item: uuid.UUID) -> Any:
        return await self.staff.history(await self.actor(), item)

    async def listed(self) -> set[str]:
        """Every item id visible to the staff member (all pages)."""

        from haloflow.m05.service import Cursor, ListQuery

        seen: set[str] = set()
        after: Any = None
        ctx = await self.actor()
        while True:
            page = await self.staff.list_items(ctx, ListQuery(limit=100, after=after))
            seen |= {view["item_id"] for view in page["items"]}
            if not page["items"] or page["next"] is None:
                return seen
            last = page["items"][-1]
            after = Cursor(created_at=datetime.fromisoformat(last["created_at"]),
                           item_id=uuid.UUID(last["item_id"]))


# --- oracles --------------------------------------------------------------------------------


def resolution_events(install: sc.SCInstall, item: uuid.UUID) -> list[dict[str, Any]]:
    rows = sa.admin(install, sa.q(
        install, "SELECT to_jsonb(e) FROM {s}.patient_events e"
                 " JOIN {s}.patient_event_links l ON l.event_id = e.event_id"
                 " WHERE l.link_type = %s AND l.link_id = %s AND e.event_type = %s"),
        (ITEM_LINK, str(item), T_RESOLVED))
    return [r[0] for r in rows]


async def assert_resolved(chain: Chain, item: uuid.UUID, outcome: str, *, kind: str,
                          source_event: uuid.UUID) -> None:
    """The ONE positive resolution oracle (v1r1 C1; used unchanged by NC-3):
    item resolved with `outcome` at version 3 (created 1, claim 2, resolve 3); its
    history ends created -> claim -> resolve; exactly one resolution event linked to
    THIS item, caused by the item's source event, carrying outcome and kind."""

    view = await chain.get(item)
    assert (view["item_id"], view["item_kind"], view["source_event_id"]) == (
        str(item), kind, str(source_event)), view
    assert (view["state"], view["outcome_code"], view["assignee_id"], view["version"]) == (
        "resolved", outcome, STAFF, 3), view
    history = await chain.history(item)
    assert [e["action"] for e in history["entries"]] == ["created", "claim", "resolve"], history
    assert history["entries"][-1]["after"] == {"state": "resolved", "assignee_id": STAFF,
                                               "outcome_code": outcome}, history
    events = resolution_events(chain.install, item)
    assert len(events) == 1, events
    (event,) = events
    assert (event["causation_event_id"], event["patient_id"]) == (
        str(source_event), view["patient_id"]), event
    assert event["metadata"] == {"outcome_code": outcome, "assertion_kind": "staff",
                                 "item_kind": kind}, event


def item_row(install: sc.SCInstall, item: uuid.UUID) -> dict[str, Any]:
    rows = sa.admin(install, sa.q(install, "SELECT to_jsonb(i) FROM {s}.m05_followup_items i"
                                           " WHERE item_id = %s"), (str(item),))
    assert len(rows) == 1, rows
    value: dict[str, Any] = rows[0][0]
    return value
