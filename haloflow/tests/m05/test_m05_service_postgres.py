"""B3b service rows on PostgreSQL 17, through the composed runtime and the real M01
gateway (package v2 + v2r1, Claude/Codex aligned 2026-11-01).

Rows (requirement map v2 section 7 + v2r1 A5):
- SV-03  the sent SQL command equals the context-only oracle (read x supervise).
- SV-04  non-canonical principal id / bad auth_method -> NOT_AUTHORIZED, no gateway call.
- SV-05  the capability matrix (pre-gateway RouteDenied; domain refusals in SQL).
- SV-06  read routes execute only READ statements.
- SV-07  actor directory standing for actions (eligible; unknown/inactive/foreign/
         no-read/no-act; read-only cannot act; replay after loss of standing).
- SV-08r/s/t/u reads: directory standing; RR snapshot control and post-revoke new
         transaction; act-only revocation keeps reads; admin authority independent.
- SV-09/09b display authority and valid subsets; SV-10 admin bootstrap.
- SV-11/11a/11d corrupted outputs -> *_RESULT_INVALID with rollback of real writes.
- SV-12  unknown commit, recovery (replay if committed, apply once if not), cancel.
- SV-13  REPEATABLE READ default -> ISOLATION_UNSUPPORTED, zero writes.
- SV-14a/b reader adapter fails closed; workload id collision gains no staff authority.
- SV-02b the SQL refuses the representable part of the SV-02 corpus.
- DT-R2  reader truth table (direct runtime call).

RED TRANCHE: the service methods raise NotImplementedError and t025 is a stub, so
every service row fails in its own body until the implementation gate.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import m05_support as m05
import psycopg
import pytest
import service_support as sv
import staff_support as st
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_m05_actions_postgres import _cmd
from test_m05_actions_postgres import _refused as _sql_refused
from test_m05_create_postgres import _command, _create, _ready, _source

pytestmark = pytest.mark.postgres

S1, S2, SUP = UUID(m05.S1), UUID(m05.S2), UUID(m05.SUP)


def _svc() -> Any:
    import haloflow.m05.service as service

    return service


def _staff_svc() -> Any:
    import haloflow.m01.staff.service as service

    return service


def _item(install: st.StaffInstall) -> UUID:
    _ready(install)
    return UUID(str(_create(install, _command(_source(install)))["item_id"]))


async def _aitem(install: st.StaffInstall) -> UUID:
    """The sync install/seed path uses asyncio.run, so async rows run it in a worker
    thread; installation still happens in the test body (class-B red rule)."""

    return await asyncio.to_thread(_item, install)


def _staff(install: st.StaffInstall, *, access: tuple[str, ...] = ("read", "act"),
           active: bool = True) -> UUID:
    staff = st.new_uuid()
    st.seed_staff(install, staff, name="Service Staff", access=access, active=active)
    return UUID(staff)


def _action(item: UUID, version: int, kind: str, assignee: UUID | None = None,
            key: UUID | None = None) -> Any:
    return _svc().StaffAction(item_id=item, client_action_id=key or uuid.uuid4(),
                              expected_version=version, type=kind, assignee_id=assignee)


def _untouched(install: st.StaffInstall, before: st.State) -> None:
    st.Delta(before, st.state(install)).untouched(st.ALL_TABLES)


@asynccontextmanager
async def _service(install: st.StaffInstall, *, substitute: Any = None, pool_class: Any = None,
                   login: str | None = None) -> AsyncIterator[tuple[Any, sv.Recorder]]:
    async with sv.runtime(install, pool_class=pool_class, login=login) as rt:
        recorder = sv.Recorder(rt.gateway, substitute)
        yield rt, recorder


# ---------------------------------------------------------------------------
# SV-03 / SV-04
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("read", [True, False])
@pytest.mark.parametrize("supervise", [True, False])
async def test_sv03_sent_command_is_the_context_oracle(staff_install, read, supervise):
    item = await _aitem(staff_install)
    caps = {sv.PREPARE, sv.APPLY} | ({sv.READ} if read else set()) | (
        {sv.SUPERVISE} if supervise else set())
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.SUP, frozenset(caps), request_id="req-1")
        action = _action(item, 1, "assign", S2)
        try:
            await _svc().M05Service(rec).act(ctx, action)
        except _svc().ActionRefused as refused:  # read, then supervise, decided in SQL
            assert not (read and supervise) and refused.code == "NOT_AUTHORIZED"
        expected = {
            "schema_version": 1, "item_id": str(item),
            "client_action_id": str(action.client_action_id), "expected_version": 1,
            "action": {"type": "assign", "assignee_id": str(S2)},
            "permissions": {"read": read, "act": True, "supervise": supervise},
            "attribution": {"principal_kind": "actor", "principal_id": m05.SUP,
                            "auth_method": "test", "execution_id": str(ctx.execution_id),
                            "request_id": "req-1", "purpose_code": "operations"},
        }
        assert rec.sent(sv.K_PREPARE) == [expected]
        assert rec.sent(sv.K_APPLY) == ([expected] if read and supervise else [])


@pytest.mark.parametrize(
    ("principal", "auth"),
    [(m05.S1.upper(), "test"), ("staff-one", "test"), (m05.S1, "Test"), (m05.S1, "x" * 33)],
    ids=["uppercase", "not-uuid", "auth-uppercase", "auth-too-long"],
)
async def test_sv04_noncanonical_principal_refused_before_gateway(staff_install, principal, auth):
    item = await _aitem(staff_install)
    before = st.state(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, principal, sv.STAFF_CAPS, auth_method=auth)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim"))
        assert caught.value.code == "NOT_AUTHORIZED"
        with pytest.raises(_svc().ReadRefused) as read:
            await _svc().M05Service(rec).get(ctx, item)
        assert read.value.code == "NOT_AUTHORIZED"
        assert rec.calls == 0
    _untouched(staff_install, before)


# ---------------------------------------------------------------------------
# SV-05 capability matrix / SV-06 read-only
# ---------------------------------------------------------------------------

ROUTE_DENIED_CASES = {
    "act-no-prepare": ("act", frozenset({sv.READ, sv.APPLY})),
    "act-no-apply": ("act", frozenset({sv.READ, sv.PREPARE})),
    "get-no-read": ("get", frozenset({sv.PREPARE, sv.APPLY})),
    "display-no-display": ("display", frozenset({sv.READ, sv.STAFF_ADMIN})),
    "display-no-admin-no-read": ("display", frozenset({sv.STAFF_DISPLAY})),
    "admin-no-admin": ("admin", frozenset({sv.STAFF_DISPLAY, sv.READ})),
}


async def _route(rec: Any, ctx: Any, route: str, item: UUID) -> Any:
    if route == "act":
        return await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim"))
    if route == "get":
        return await _svc().M05Service(rec).get(ctx, item)
    if route == "display":
        return await _staff_svc().StaffService(rec).display(ctx, (S1,))
    command = _staff_svc().StaffAdminCommand(client_action_id=uuid.uuid4(), operation="rename",
                                             staff_id=S2, display_name="Renamed")
    return await _staff_svc().StaffService(rec).admin(ctx, command)


@pytest.mark.parametrize("case", sorted(ROUTE_DENIED_CASES))
async def test_sv05_route_denied_before_any_gateway_call(staff_install, case):
    route, caps = ROUTE_DENIED_CASES[case]
    item = await _aitem(staff_install)
    before = st.state(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.SUP, caps)
        with pytest.raises(_svc().RouteDenied) as caught:
            await _route(rec, ctx, route, item)
        assert caught.value.reason_code == "CAPABILITY_DENIED"
        assert rec.calls == 0
    _untouched(staff_install, before)


@pytest.mark.parametrize(
    ("caps", "kind", "assignee"),
    [
        (frozenset({sv.PREPARE, sv.APPLY}), "claim", None),           # read missing
        (sv.STAFF_CAPS, "assign", S2),                                # supervise missing
    ],
    ids=["no-read", "no-supervise"],
)
async def test_sv05_domain_refusals_in_sql(staff_install, caps, kind, assignee):
    item = await _aitem(staff_install)
    before = st.state(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.SUP, caps)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, kind, assignee))
        assert caught.value.code == "NOT_AUTHORIZED"
        assert rec.statements == [sv.K_PREPARE]  # REFUSED short-circuits apply
    _untouched(staff_install, before)


@pytest.mark.parametrize(
    "caps", [sv.READ_ONLY_CAPS, sv.SUPERVISOR_CAPS], ids=["read-only", "mixed"]
)
async def test_sv06_read_routes_execute_only_read_statements(staff_install, caps):
    item = await _aitem(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.S1, caps)
        service = _svc().M05Service(rec)
        await service.get(ctx, item)
        await service.history(ctx, item)
        await service.list_items(ctx, _svc().ListQuery(limit=5))
        reads = {sv.K_READER, sv.K_GET, sv.K_HISTORY, sv.K_LIST}
        assert rec.statements and set(rec.statements) <= reads, rec.statements
        assert rec.statements.count(sv.K_READER) == 3  # one reader check per read route


# ---------------------------------------------------------------------------
# SV-07 actor directory standing for actions
# ---------------------------------------------------------------------------


async def test_sv07a_eligible_actor_acts(staff_install):
    item = await _aitem(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        result = await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim"))
        assert (result.outcome, result.snapshot["assignee_id"]) == ("applied", m05.S1)
        assert rec.statements == [sv.K_PREPARE, sv.K_APPLY]


def _actor(kind: str, install: st.StaffInstall, foreign: st.StaffInstall) -> UUID:
    if kind == "unknown":
        return uuid.uuid4()
    if kind == "inactive":
        return _staff(install, active=False)
    if kind == "foreign":
        foreign.require()
        return _staff(foreign)
    return _staff(install, access=("act",) if kind == "no_read" else ("read",))


@pytest.mark.parametrize("kind", ["unknown", "inactive", "foreign", "no_read", "no_act"])
@pytest.mark.parametrize("action", m05.NON_RESOLVE_ACTIONS)
async def test_sv07b_actor_without_standing_refused(staff_install, staff_foreign_install,
                                                    kind, action):
    item = await _aitem(staff_install)
    actor = await asyncio.to_thread(_actor, kind, staff_install, staff_foreign_install)
    if action == "start_progress":  # an item assigned to the actor, so only standing fails
        st.admin_rows(staff_install, sql.SQL(
            "UPDATE {}.m05_followup_items SET assignee_id = %s WHERE item_id = %s").format(
            sql.Identifier(staff_install.schema)), (str(actor), str(item)))
    assignee = S2 if action in ("assign", "reassign") else None
    if action == "reassign":
        st.admin_rows(staff_install, sql.SQL(
            "UPDATE {}.m05_followup_items SET assignee_id = %s WHERE item_id = %s").format(
            sql.Identifier(staff_install.schema)), (m05.S1, str(item)))
    before = st.state(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(actor), sv.SUPERVISOR_CAPS)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, action, assignee))
        assert caught.value.code == "NOT_AUTHORIZED"
    _untouched(staff_install, before)


async def test_sv07c_read_only_staff_cannot_act_but_can_read(staff_install):
    item = await _aitem(staff_install)
    reader = _staff(staff_install, access=("read",))
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(reader), sv.STAFF_CAPS)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim"))
        assert caught.value.code == "NOT_AUTHORIZED"
        view = await _svc().M05Service(rec).get(ctx, item)
        assert view["item_id"] == str(item)


@pytest.mark.parametrize("change", ["deactivate", "revoke_read", "revoke_act"])
async def test_sv07d_replay_requires_current_actor_standing(staff_install, change):
    item = await _aitem(staff_install)
    actor = _staff(staff_install)
    action = _action(item, 1, "claim")
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(actor), sv.STAFF_CAPS)
        first = await _svc().M05Service(rec).act(ctx, action)
        again = await _svc().M05Service(rec).act(ctx, action)
        assert (again.outcome, again.snapshot) == ("replayed", first.snapshot)
        command = (st.admin_command("deactivate", str(actor)) if change == "deactivate"
                   else st.admin_command("revoke", str(actor), access=change.split("_")[1]))
        st.admin_op(staff_install, command)
        before = st.state(staff_install)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _svc().M05Service(rec).act(ctx, action)
        assert caught.value.code == "NOT_AUTHORIZED"
    _untouched(staff_install, before)


# ---------------------------------------------------------------------------
# SV-08 reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["unknown", "inactive", "foreign", "no_read"])
async def test_sv08r_reader_without_standing_refused(staff_install, staff_foreign_install, kind):
    item = await _aitem(staff_install)
    actor = await asyncio.to_thread(_actor, kind, staff_install, staff_foreign_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(actor), sv.READ_ONLY_CAPS)
        service = _svc().M05Service(rec)
        for call in (lambda: service.get(ctx, item), lambda: service.history(ctx, item),
                     lambda: service.list_items(ctx, _svc().ListQuery(limit=5))):
            with pytest.raises(_svc().ReadRefused) as caught:
                await call()
            assert caught.value.code == "NOT_AUTHORIZED"
        assert set(rec.statements) == {sv.K_READER}  # no data statement ever ran


def _revoke(install: st.StaffInstall, staff: UUID, access: str) -> None:
    st.admin_op(install, st.admin_command("revoke", str(staff), access=access))


@pytest.fixture
def rr_login(staff_install) -> Iterator[str]:
    """A dedicated runtime login whose default isolation is REPEATABLE READ; created
    for one test and dropped after, so the shared role inventory is unchanged."""

    name = f"haloflow_test_rr_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(staff_install.ids.admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'rr' IN ROLE haloflow_runtime")
                     .format(sql.Identifier(name)))
        conn.execute(sql.SQL("ALTER ROLE {} SET search_path = ''").format(sql.Identifier(name)))
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_isolation = "
                             "'repeatable read'").format(sql.Identifier(name)))
    params = conninfo_to_dict(staff_install.ids.logins["haloflow_runtime"])
    params.update(user=name, password="rr")
    try:
        yield make_conninfo(**params)
    finally:
        with psycopg.connect(staff_install.ids.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


class _BeforeReader(sv.Recorder):
    """Runs `hook` (in a thread) just before the first reader statement."""

    def __init__(self, gateway: Any, hook: Any) -> None:
        super().__init__(gateway)
        self._hook = hook

    async def with_tenant_transaction(self, context: Any, callback: Any, **kwargs: Any) -> Any:
        recorder = self

        async def wrapped(handle: Any) -> Any:
            class _H(sv._Handle):
                async def fetch_all(self, key: str, params: Any = None) -> Any:
                    if key == sv.K_READER and recorder._hook is not None:
                        hook, recorder._hook = recorder._hook, None
                        await asyncio.to_thread(hook)
                    return await super().fetch_all(key, params)

                async def fetch_one(self, key: str, params: Any = None) -> Any:
                    if key == sv.K_READER and recorder._hook is not None:
                        hook, recorder._hook = recorder._hook, None
                        await asyncio.to_thread(hook)
                    return await super().fetch_one(key, params)

            return await callback(_H(handle, recorder))

        self.calls += 1
        return await self._gateway.with_tenant_transaction(context, wrapped, **kwargs)


async def test_sv08s_rr_snapshot_before_revoke_completes_new_txn_refused(staff_install, rr_login):
    item = await _aitem(staff_install)
    reader = _staff(staff_install)
    async with sv.runtime(staff_install, login=rr_login) as rt:
        ctx = await sv.context(rt, staff_install, str(reader), sv.READ_ONLY_CAPS)
        rec = _BeforeReader(rt.gateway, lambda: _revoke(staff_install, reader, "read"))
        view = await _svc().M05Service(rec).get(ctx, item)  # snapshot predates the revoke
        assert view["item_id"] == str(item)
        with pytest.raises(_svc().ReadRefused) as caught:  # a new transaction after it
            await _svc().M05Service(sv.Recorder(rt.gateway)).get(ctx, item)
        assert caught.value.code == "NOT_AUTHORIZED"


async def test_sv08s_rc_control_reader_statement_sees_committed_revoke(staff_install):
    item = await _aitem(staff_install)
    reader = _staff(staff_install)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, str(reader), sv.READ_ONLY_CAPS)
        rec = _BeforeReader(rt.gateway, lambda: _revoke(staff_install, reader, "read"))
        with pytest.raises(_svc().ReadRefused) as caught:
            await _svc().M05Service(rec).get(ctx, item)
        assert caught.value.code == "NOT_AUTHORIZED"
        assert sv.K_GET not in rec.statements


async def test_sv08t_act_only_revocation_keeps_reads(staff_install):
    item = await _aitem(staff_install)
    staff = _staff(staff_install)
    _revoke(staff_install, staff, "act")
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(staff), sv.STAFF_CAPS)
        assert (await _svc().M05Service(rec).get(ctx, item))["item_id"] == str(item)
        with pytest.raises(_svc().ActionRefused) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim"))
        assert caught.value.code == "NOT_AUTHORIZED"


async def test_sv08u_admin_authority_independent_of_followup_access(staff_install):
    await asyncio.to_thread(_ready, staff_install)
    admin = uuid.uuid4()  # no directory row at all
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(admin),
                               frozenset({sv.STAFF_ADMIN, sv.STAFF_DISPLAY}))
        target = uuid.uuid4()
        command = _staff_svc().StaffAdminCommand(client_action_id=uuid.uuid4(), operation="add",
                                                 staff_id=target, display_name="New Hire")
        result = await _staff_svc().StaffService(rec).admin(ctx, command)
        assert result.result["staff"]["staff_id"] == str(target)
        shown = await _staff_svc().StaffService(rec).display(ctx, (target,))
        assert [e["staff_id"] for e in shown["staff"]] == [str(target)]
        assert sv.K_READER not in rec.statements


# ---------------------------------------------------------------------------
# SV-09 / SV-09b display, SV-10 admin bootstrap
# ---------------------------------------------------------------------------


async def test_sv09_display_via_reader_and_refused_without_standing(staff_install):
    await asyncio.to_thread(_ready, staff_install)
    outsider = uuid.uuid4()
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.STAFF_DISPLAY, sv.READ}))
        shown = await _staff_svc().StaffService(rec).display(ctx, (S1, S2))
        assert [e["staff_id"] for e in shown["staff"]] == sorted([m05.S1, m05.S2])
        assert rec.statements == [sv.K_READER, sv.K_DISPLAY]
        ctx2 = await sv.context(rt, staff_install, str(outsider),
                                frozenset({sv.STAFF_DISPLAY, sv.READ}))
        with pytest.raises(_svc().ReadRefused) as caught:
            await _staff_svc().StaffService(rec).display(ctx2, (S1,))
        assert caught.value.code == "NOT_AUTHORIZED"


async def test_sv09b_display_subsets_are_valid(staff_install):
    await asyncio.to_thread(_ready, staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.STAFF_DISPLAY, sv.READ}))
        unknown = uuid.uuid4()
        mixed = await _staff_svc().StaffService(rec).display(ctx, (unknown, S1))
        assert [e["staff_id"] for e in mixed["staff"]] == [m05.S1]
        empty = await _staff_svc().StaffService(rec).display(ctx, (unknown,))
        assert empty == {"schema_version": 1, "staff": []}


async def test_sv10_admin_bootstrap_without_directory_row(staff_install):
    await asyncio.to_thread(_ready, staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, str(uuid.uuid4()), frozenset({sv.STAFF_ADMIN}))
        staff = uuid.uuid4()
        key = uuid.uuid4()
        command = _staff_svc().StaffAdminCommand(client_action_id=key, operation="add",
                                                 staff_id=staff, display_name="First Admin Hire")
        first = await _staff_svc().StaffService(rec).admin(ctx, command)
        again = await _staff_svc().StaffService(rec).admin(ctx, command)
        assert first.result == again.result
        assert rec.statements == [sv.K_ADMIN, sv.K_ADMIN]


# ---------------------------------------------------------------------------
# SV-11 corrupted outputs (substituted after the real statement ran)
# ---------------------------------------------------------------------------


def _good_apply(item: UUID, key: UUID) -> dict[str, Any]:
    return {"schema_version": 1, "outcome": "applied", "snapshot": {
        "schema_version": 1, "item_id": str(item), "client_action_id": str(key),
        "action": "claim", "version": 2, "state": "open", "assignee_id": m05.S1,
        "outcome_code": None}}


def _apply_corruptions(item: UUID, key: UUID) -> dict[str, list[tuple[Any, ...]]]:
    good = _good_apply(item, key)
    snap = good["snapshot"]
    return {
        "no-row": [],
        "null": [(None,)],
        "scalar": [(5,)],
        "extra-key": [({**good, "x": 1},)],
        "bool-schema": [({**good, "schema_version": True},)],
        "bool-version": [({**good, "snapshot": {**snap, "version": True}},)],
        "wrong-item": [({**good, "snapshot": {**snap, "item_id": str(uuid.uuid4())}},)],
        "wrong-key": [({**good, "snapshot": {**snap, "client_action_id": str(uuid.uuid4())}},)],
        "wrong-action": [({**good, "snapshot": {**snap, "action": "assign"}},)],
        "wrong-version": [({**good, "snapshot": {**snap, "version": 3}},)],
        "wrong-assignee": [({**good, "snapshot": {**snap, "assignee_id": m05.S2}},)],
        "bad-outcome": [({**good, "outcome": "done"},)],
        "two-rows": [(good,), (good,)],
    }


CORRUPTION_IDS = sorted(_apply_corruptions(uuid.uuid4(), uuid.uuid4()))


@pytest.mark.parametrize("case", CORRUPTION_IDS)
async def test_sv11_corrupt_apply_output_rolls_back(staff_install, case):
    item = await _aitem(staff_install)
    key = uuid.uuid4()
    before = st.state(staff_install)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway, {sv.K_APPLY: _apply_corruptions(item, key)[case]})
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        with pytest.raises(_svc().ActionFailed) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim", key=key))
        assert caught.value.reason_code == "ACTION_RESULT_INVALID"
        assert caught.value.__cause__ is None
    _untouched(staff_install, before)  # the real apply ran and was rolled back


@pytest.mark.parametrize("case", ["null", "scalar", "extra-key", "bad-decision", "bool-schema"])
async def test_sv11_corrupt_prepare_output(staff_install, case):
    item = await _aitem(staff_install)
    rows = {"null": [(None,)], "scalar": [("NEW",)],
            "extra-key": [({"schema_version": 1, "decision": "NEW", "x": 1},)],
            "bad-decision": [({"schema_version": 1, "decision": "MAYBE"},)],
            "bool-schema": [({"schema_version": True, "decision": "NEW"},)]}[case]
    before = st.state(staff_install)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway, {sv.K_PREPARE: rows})
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        with pytest.raises(_svc().ActionFailed) as caught:
            await _svc().M05Service(rec).act(ctx, _action(item, 1, "claim"))
        assert caught.value.reason_code == "ACTION_RESULT_INVALID"
        assert sv.K_APPLY not in rec.statements
    _untouched(staff_install, before)


def _good_admin(key: UUID, staff: UUID) -> dict[str, Any]:
    return {"schema_version": 1, "client_action_id": str(key), "operation": "add",
            "staff": {"schema_version": 1, "staff_id": str(staff), "display_name": "Corrupt",
                      "status": "active", "status_changed_at": "2026-11-01T16:00:00.000000Z",
                      "access": []}}


@pytest.mark.parametrize("case", ["null", "extra-key", "wrong-key", "wrong-staff", "bool-schema",
                                  "staff-five-keys", "bad-status", "bad-timestamp",
                                  "access-unsorted", "name-mismatch"])
async def test_sv11a_corrupt_admin_output_rolls_back(staff_install, case):
    await asyncio.to_thread(_ready, staff_install)
    key, staff = uuid.uuid4(), uuid.uuid4()
    good = _good_admin(key, staff)
    snap = good["staff"]
    bad = {
        "null": None,
        "extra-key": {**good, "x": 1},
        "wrong-key": {**good, "client_action_id": str(uuid.uuid4())},
        "wrong-staff": {**good, "staff": {**snap, "staff_id": str(uuid.uuid4())}},
        "bool-schema": {**good, "schema_version": True},
        "staff-five-keys": {**good, "staff": {k: v for k, v in snap.items() if k != "access"}},
        "bad-status": {**good, "staff": {**snap, "status": "gone"}},
        "bad-timestamp": {**good, "staff": {**snap,
                                            "status_changed_at": "2026-02-30T00:00:00.000000Z"}},
        "access-unsorted": {**good, "staff": {**snap, "access": ["read", "act"]}},
        "name-mismatch": {**good, "staff": {**snap, "display_name": "Other"}},
    }[case]
    before = st.state(staff_install)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway, {sv.K_ADMIN: [(bad,)]})
        ctx = await sv.context(rt, staff_install, st.ADMIN, frozenset({sv.STAFF_ADMIN}))
        command = _staff_svc().StaffAdminCommand(client_action_id=key, operation="add",
                                                 staff_id=staff, display_name="Corrupt")
        with pytest.raises(_staff_svc().AdminFailed) as caught:
            await _staff_svc().StaffService(rec).admin(ctx, command)
        assert caught.value.reason_code == "ADMIN_RESULT_INVALID"
    _untouched(staff_install, before)


@pytest.mark.parametrize("case", ["duplicate", "unrequested", "out-of-order", "bad-status",
                                  "extra-entry-key", "bool-schema"])
async def test_sv11d_corrupt_display_output(staff_install, case):
    await asyncio.to_thread(_ready, staff_install)
    a, b = sorted([m05.S1, m05.S2])
    entry = {"staff_id": a, "display_name": "Staff", "status": "active"}
    entry_b = {**entry, "staff_id": b}
    staff = {"duplicate": [entry, entry], "unrequested": [{**entry, "staff_id": m05.SUP}],
             "out-of-order": [entry_b, entry], "bad-status": [{**entry, "status": "gone"}],
             "extra-entry-key": [{**entry, "x": 1}], "bool-schema": [entry]}[case]
    body = {"schema_version": True if case == "bool-schema" else 1, "staff": staff}
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway, {sv.K_DISPLAY: [(body,)]})
        ctx = await sv.context(rt, staff_install, m05.S1, frozenset({sv.STAFF_DISPLAY, sv.READ}))
        with pytest.raises(_svc().ReadFailed) as caught:
            await _staff_svc().StaffService(rec).display(ctx, (S1, S2))
        assert caught.value.reason_code == "READ_RESULT_INVALID"


@pytest.mark.parametrize("case", ["get-wrong-item", "get-extra-key", "history-wrong-item",
                                  "list-over-limit", "get-bool-version"])
async def test_sv11_corrupt_read_output(staff_install, case):
    item = await _aitem(staff_install)
    view = st.call(staff_install, "m05_get_item", str(item))
    key = {"get-wrong-item": sv.K_GET, "get-extra-key": sv.K_GET, "get-bool-version": sv.K_GET,
           "history-wrong-item": sv.K_HISTORY, "list-over-limit": sv.K_LIST}[case]
    body = {
        "get-wrong-item": {**view, "item_id": str(uuid.uuid4())},
        "get-extra-key": {**view, "x": 1},
        "get-bool-version": {**view, "version": True},
        "history-wrong-item": {"schema_version": 1, "item_id": str(uuid.uuid4()), "entries": []},
        "list-over-limit": {"schema_version": 1, "items": [view, view], "next": None},
    }[case]
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway, {key: [(body,)]})
        ctx = await sv.context(rt, staff_install, m05.S1, sv.READ_ONLY_CAPS)
        service = _svc().M05Service(rec)
        call = {sv.K_GET: lambda: service.get(ctx, item),
                sv.K_HISTORY: lambda: service.history(ctx, item),
                sv.K_LIST: lambda: service.list_items(ctx, _svc().ListQuery(limit=1))}[key]
        with pytest.raises(_svc().ReadFailed) as caught:
            await call()
        assert caught.value.reason_code == "READ_RESULT_INVALID"


# ---------------------------------------------------------------------------
# SV-12 unknown commit and recovery
# ---------------------------------------------------------------------------


class _Rollback(Exception):
    pass


def _faulting_pool(mode: str, fired: list[str]) -> Any:
    """One-shot TenantPool double (the M02 PA-06 pattern), restored before return.
    Modes: commit_then_raise, raise_no_commit, commit_then_cancel."""

    from haloflow.m01.pool import TenantPool

    class _Pool(TenantPool):
        armed = True

        @asynccontextmanager
        async def _connection_for_gateway(self) -> AsyncIterator[Any]:
            async with super()._connection_for_gateway() as connection:
                if not type(self).armed:
                    yield connection
                    return
                type(self).armed = False
                real = connection.transaction

                @asynccontextmanager
                async def transaction(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                    fired.append(mode)
                    if mode in ("commit_then_raise", "commit_then_cancel"):
                        async with real(*args, **kwargs) as tx:
                            yield tx
                        if mode == "commit_then_cancel":
                            raise asyncio.CancelledError()
                        raise psycopg.OperationalError("simulated loss after COMMIT")
                    try:
                        async with real(*args, **kwargs) as tx:
                            yield tx
                            raise _Rollback()
                    except _Rollback:
                        pass
                    raise psycopg.OperationalError("simulated loss before COMMIT")

                connection.transaction = transaction  # type: ignore[method-assign]
                try:
                    yield connection
                finally:
                    del connection.transaction

    return _Pool


@pytest.mark.parametrize(
    ("mode", "retry_outcome"),
    [("commit_then_raise", "replayed"), ("raise_no_commit", "applied"),
     ("commit_then_cancel", "replayed")],
)
async def test_sv12_action_unknown_commit_then_recovery(staff_install, mode, retry_outcome):
    item = await _aitem(staff_install)
    action = _action(item, 1, "claim")
    fired: list[str] = []
    async with sv.runtime(staff_install, pool_class=_faulting_pool(mode, fired)) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        service = _svc().M05Service(rt.gateway)
        with pytest.raises(_svc().ActionCommitUnknown) as caught:
            await service.act(ctx, action)
        assert (caught.value.item_id, caught.value.client_action_id) == (
            item, action.client_action_id)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "simulated" not in str(caught.value)
        retry = await service.act(ctx, action)
        assert fired == [mode]
    assert retry.outcome == retry_outcome
    rows = st.admin_rows(staff_install, sql.SQL(
        "SELECT count(*) FROM {}.m05_followup_actions WHERE item_id = %s").format(
        sql.Identifier(staff_install.schema)), (str(item),))
    assert rows == [(1,)]  # applied exactly once


@pytest.mark.parametrize("mode", ["commit_then_raise", "raise_no_commit"])
async def test_sv12_admin_unknown_commit_then_recovery(staff_install, mode):
    await asyncio.to_thread(_ready, staff_install)
    staff = uuid.uuid4()
    command = _staff_svc().StaffAdminCommand(client_action_id=uuid.uuid4(), operation="add",
                                             staff_id=staff, display_name="Unknown Commit")
    fired: list[str] = []
    async with sv.runtime(staff_install, pool_class=_faulting_pool(mode, fired)) as rt:
        ctx = await sv.context(rt, staff_install, st.ADMIN, frozenset({sv.STAFF_ADMIN}))
        service = _staff_svc().StaffService(rt.gateway)
        with pytest.raises(_staff_svc().AdminCommitUnknown) as caught:
            await service.admin(ctx, command)
        assert (caught.value.staff_id, caught.value.client_action_id, caught.value.operation) == (
            staff, command.client_action_id, "add")
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        retry = await service.admin(ctx, command)
    assert retry.result["staff"]["staff_id"] == str(staff)
    rows = st.admin_rows(staff_install, sql.SQL(
        "SELECT count(*) FROM {}.m01_staff_admin_log WHERE staff_id = %s").format(
        sql.Identifier(staff_install.schema)), (str(staff),))
    assert rows == [(1,)]


async def test_sv12_post_body_path_fault_is_unknown(staff_install, monkeypatch):
    item = await _aitem(staff_install)
    async with sv.runtime(staff_install) as rt:
        gateway = rt.gateway
        original = gateway._verify_effective_path
        calls = {"n": 0}

        async def verify(connection: Any, schema_key: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated post-body path check failure")
            await original(connection, schema_key)

        monkeypatch.setattr(gateway, "_verify_effective_path", verify)
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        with pytest.raises(_svc().ActionCommitUnknown):
            await _svc().M05Service(gateway).act(ctx, _action(item, 1, "claim"))


async def test_sv12_cancellation_before_marker_is_cancellation(staff_install):
    item = await _aitem(staff_install)
    before = st.state(staff_install)

    class _CancelAtApply(sv.Recorder):
        async def with_tenant_transaction(self, context: Any, callback: Any, **kw: Any) -> Any:
            async def wrapped(handle: Any) -> Any:
                class _H(sv._Handle):
                    async def fetch_one(self, key: str, params: Any = None) -> Any:
                        if key == sv.K_APPLY:
                            raise asyncio.CancelledError()
                        return await super().fetch_one(key, params)

                    async def fetch_all(self, key: str, params: Any = None) -> Any:
                        if key == sv.K_APPLY:
                            raise asyncio.CancelledError()
                        return await super().fetch_all(key, params)

                return await callback(_H(handle, self))

            return await self._gateway.with_tenant_transaction(context, wrapped, **kw)

    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        with pytest.raises(asyncio.CancelledError):
            await _svc().M05Service(_CancelAtApply(rt.gateway)).act(ctx, _action(item, 1, "claim"))
    _untouched(staff_install, before)


async def test_sv12_sanitized_failures_carry_no_driver_text(staff_install, caplog):
    item = await _aitem(staff_install)
    async with sv.runtime(staff_install) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        service = _svc().M05Service(rt.gateway)
        await service.act(ctx, _action(item, 1, "claim"))
        with pytest.raises(_svc().ActionRefused) as caught:  # STALE_VERSION from SQL
            await service.act(ctx, _action(item, 1, "start_progress"))
    assert caught.value.code == "STALE_VERSION"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert str(caught.value) == "follow-up action refused"
    assert "P0001" not in caplog.text and "PL/pgSQL" not in caplog.text


# ---------------------------------------------------------------------------
# SV-13 isolation
# ---------------------------------------------------------------------------


async def test_sv13_repeatable_read_default_refused(staff_install, rr_login):
    item = await _aitem(staff_install)
    before = st.state(staff_install)
    async with sv.runtime(staff_install, login=rr_login) as rt:
        ctx = await sv.context(rt, staff_install, m05.S1, sv.STAFF_CAPS)
        with pytest.raises(_svc().ActionFailed) as caught:
            await _svc().M05Service(rt.gateway).act(ctx, _action(item, 1, "claim"))
        assert caught.value.reason_code == "ISOLATION_UNSUPPORTED"
        admin_ctx = await sv.context(rt, staff_install, st.ADMIN, frozenset({sv.STAFF_ADMIN}))
        command = _staff_svc().StaffAdminCommand(client_action_id=uuid.uuid4(),
                                                 operation="rename", staff_id=S2,
                                                 display_name="RR Rename")
        with pytest.raises(_staff_svc().AdminFailed) as admin:
            await _staff_svc().StaffService(rt.gateway).admin(admin_ctx, command)
        assert admin.value.reason_code == "ISOLATION_UNSUPPORTED"
    _untouched(staff_install, before)


# ---------------------------------------------------------------------------
# SV-14 reader adapter, workload collision
# ---------------------------------------------------------------------------

READER_CORRUPTIONS = {
    "no-row": [], "two-rows": [(True,), (True,)], "two-columns": [(True, True)],
    "null": [(None,)], "str-t": [("t",)], "str-true": [("true",)], "int-1": [(1,)],
    "int-0": [(0,)],
}


@pytest.mark.parametrize("case", sorted(READER_CORRUPTIONS))
async def test_sv14a_reader_adapter_fails_closed(staff_install, case):
    item = await _aitem(staff_install)
    async with sv.runtime(staff_install) as rt:
        rec = sv.Recorder(rt.gateway, {sv.K_READER: READER_CORRUPTIONS[case]})
        ctx = await sv.context(rt, staff_install, m05.S1, sv.READ_ONLY_CAPS)
        with pytest.raises(_svc().ReadFailed) as caught:
            await _svc().M05Service(rec).get(ctx, item)
        assert caught.value.reason_code == "READ_RESULT_INVALID"
        assert rec.statements == [sv.K_READER]


async def test_sv14b_workload_with_staff_uuid_gains_no_staff_authority(staff_install):
    item = await _aitem(staff_install)
    async with _service(staff_install) as (rt, rec):
        ctx = await sv.context(rt, staff_install, m05.S1, sv.SUPERVISOR_CAPS | {sv.STAFF_DISPLAY},
                               kind="workload")
        service = _svc().M05Service(rec)
        with pytest.raises(_svc().ReadRefused):
            await service.get(ctx, item)
        with pytest.raises(_svc().ReadRefused):
            await _staff_svc().StaffService(rec).display(ctx, (S1,))
        with pytest.raises(_svc().ActionRefused) as caught:
            await service.act(ctx, _action(item, 1, "claim"))
        assert caught.value.code == "NOT_AUTHORIZED"
        assert sv.K_READER not in rec.statements and sv.K_GET not in rec.statements


# ---------------------------------------------------------------------------
# SV-02b SQL parity; DT-R2 reader truth table
# ---------------------------------------------------------------------------

SQL_PARITY = {
    "version-zero": {"expected_version": 0},
    "version-too-large": {"expected_version": 2147483648},
    "version-bool": {"expected_version": True},
    "version-float": {"expected_version": 1.0},
    "version-str": {"expected_version": "1"},
    "type-unknown": {"action": {"type": "close"}},
    "type-not-str": {"action": {"type": 1}},
    "claim-with-assignee": {"action": {"type": "claim", "assignee_id": m05.S2}},
    "start-with-assignee": {"action": {"type": "start_progress", "assignee_id": m05.S2}},
    "assign-without-assignee": {"action": {"type": "assign"}},
    "assignee-uppercase": {"action": {"type": "assign", "assignee_id": m05.S2.upper()}},
    "item-uppercase": {"item_id": "UPPER"},
}


@pytest.mark.parametrize("case", sorted(SQL_PARITY))
def test_sv02b_sql_refuses_the_representable_corpus(staff_install, case):
    item = str(_item(staff_install))
    command = {**_cmd(item, 1, "claim"), **SQL_PARITY[case]}
    if case == "item-uppercase":
        command["item_id"] = item.upper()
    _sql_refused(staff_install, command, "COMMAND_MALFORMED")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("active_read_act", True), ("active_read", True), ("active_act", False),
     ("inactive", False), ("unknown", False), ("foreign", False)],
)
def test_dtr2_reader_truth_table(staff_install, staff_foreign_install, kind, expected):
    _ready(staff_install)
    if kind == "unknown":
        staff = st.new_uuid()
    elif kind == "foreign":
        staff_foreign_install.require()
        staff = str(_staff(staff_foreign_install))
    else:
        access = {"active_read_act": ("read", "act"), "active_read": ("read",),
                  "active_act": ("act",), "inactive": ("read", "act")}[kind]
        staff = str(_staff(staff_install, access=access, active=kind != "inactive"))
    assert st.call(staff_install, "m01_staff_followup_reader", staff) is expected


def test_dtr1_reader_takes_no_lock(staff_install):
    _ready(staff_install)
    with st.holder(staff_install, [("m01_staff", "staff_id", m05.S1)]), \
            st.runtime_connection(staff_install) as conn:
        st.set_tenant(conn, staff_install)
        conn.execute(f"SET LOCAL lock_timeout = '{st.SAFETY_LOCK_TIMEOUT}'")
        assert st.call_on(conn, staff_install, "m01_staff_followup_reader", m05.S1) is True
        conn.rollback()
