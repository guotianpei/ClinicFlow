"""Eligibility E3b - implementation evidence (green; Codex D-1 option A + B1-b / B2 rulings).

Governing: `codex_e3b-projection-design.md` (D-1: owner-only projection helper, open-case
invariant, replay first, full evidence incl. none, same-transaction append, rollback/retry,
timer vs staff parity, closed-target replay with later pending work, actual grants) and
`codex_e3b-evidence-authority.md` (B1-b: contract v2 with producer m05_followup, v1 unchanged;
B2: the contact action needs the append capability, refused before I/O without it). Synthetic
only.

Rows:
- E3b-G01 grants: `m05_el_project` is SECURITY DEFINER, owned by the M05 owner, its ACL is the
  owner alone (no PUBLIC, no runtime EXECUTE; a runtime call is refused); the case-evidence
  contract v1 producers are unchanged (m05_intake) and v2 admits m05_followup only; v4 is
  runtime-executable.
- E3b-G02 timer vs staff parity on equivalent fixtures, for a `noted` and a `none` effect:
  notes, case state / version / current class and reason, case-event effects, settlement
  identity (record, verification presence) and evidence multiplicity (exactly one observation
  per case event); the staff observation is contract v2, producer m05_followup, the staff
  actor; the timer's is contract v1, producer m05_intake, a workload.
- E3b-G03 rollback / retry: (a) the case-evidence append fails inside a stale-token action ->
  ActionFailed, the event stays unapplied, case_version unchanged, no evidence, no contact
  entry; the retry is case_changed with exactly one observation for the event; (b) the
  resolution append fails inside a close -> no closure effect; the identical retry applies
  once.
- E3b-G04 closed-target replay with later PENDING work: after a close a later finding is
  committed but unapplied; the replay of the close is `replayed` and projects nothing; a new
  close and a contact on the resolved item are ITEM_RESOLVED and project nothing; no case is
  opened by the staff path; the applier then opens exactly one new case.
- E3b-G05 the open-case invariant inside the helper: called (as its owner) with a target that
  is not the appointment's open case while work is pending -> CASE_INVARIANT_FAILED, nothing
  consumed.
- E3b-G06 contact without the append capability: refused (RouteDenied) before any statement.
- E3b-G07 a token-less `ResolveAction` on an eligibility item is COMMAND_MALFORMED at the v4
  kind check (disclosed correction G-5 to the pins' "E3a P19 refusal" wording).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import el3a_support as e3
import el3b_support as e4
import m02_support
import pytest
import sa_support as sa
import sd_support as sd
from test_el3b_postgres import _begin, _el_item

CASE_EVENT: str = "m05_eligibility_case_event"


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[e4.EL3BInstall]:
    yield from e4.module_install(request, m02_ids, m05_reset, f"el3b-ev:{request.node.name}")


def _token_of(inst: Any, item: uuid.UUID) -> Any:
    async def body() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).token(item)

    return asyncio.run(body())


def _contact(inst: Any, item: uuid.UUID, token: Any, version: int = 1,
             key: uuid.UUID | None = None) -> Any:
    async def body() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).contact(item, version, "el_left_message", token,
                                                    key=key)

    return asyncio.run(body())


def _events(inst: Any, ref: str) -> list[dict[str, Any]]:
    return [r for r in e3.rows(inst, "patient_events", "event_type = %s", (CASE_EVENT,))
            if (r.get("metadata") or {}).get("appointment_ref") == ref]


# --- E3b-G01 grants -------------------------------------------------------------------------

def test_el3b_g01_helper_is_owner_only_and_contracts_keep_their_producers(
        install: e4.EL3BInstall) -> None:
    from psycopg import errors

    from haloflow.m02.roles import M05_OWNER_ROLE

    inst = _begin(install)
    ((secdef, owner, acl),) = e3.q(inst, (
        "SELECT p.prosecdef, pg_catalog.pg_get_userbyid(p.proowner),"
        " ARRAY(SELECT pg_catalog.pg_get_userbyid(a.grantee) || ':' || a.privilege_type"
        "       FROM pg_catalog.aclexplode(p.proacl) AS a ORDER BY 1)"
        " FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %s AND p.proname = 'm05_el_project'"), (inst.schema,))
    assert (secdef, owner, list(acl)) == (True, M05_OWNER_ROLE, [f"{M05_OWNER_ROLE}:EXECUTE"])
    got = dict(e3.q(inst, (
        "SELECT r.rolname, pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE')"
        " FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,"
        " pg_catalog.pg_roles r WHERE n.nspname = %s AND p.proname = 'm05_el_project'"
        " AND r.rolname = %s"), (inst.schema, sa.RUNTIME)))
    assert got == {sa.RUNTIME: False}
    with sa.runtime_conn(inst) as conn, pytest.raises(errors.InsufficientPrivilege):
        sa.call_in(conn, inst, "m05_el_project",
                   {"appointment_ref": str(uuid.uuid4()), "case_id": str(uuid.uuid4())})
    producers = {(int(v), tuple(p)) for v, p in e3.q(inst, (
        "SELECT version, authorized_producers FROM {s}.event_contracts"
        " WHERE event_type = %s"), (CASE_EVENT,))}
    assert producers == {(1, ("m05_intake",)), (2, ("m05_followup",))}
    ((v3,),) = e3.q(inst, ("SELECT authorized_producers FROM {s}.event_contracts"
                           " WHERE event_type = 'm05_followup_resolution_asserted'"
                           " AND version = 3"))
    assert list(v3) == ["m05_followup"]


# --- E3b-G02 timer vs staff parity ----------------------------------------------------------

def _normal(inst: Any, ref: str) -> tuple[Any, ...]:
    (case,) = e3.cases(inst, ref)
    notes = [(n["seq"], n["kind"], n["class"], n["reason"])
             for n in e3.notes(inst, case["case_id"])]
    effects = [(r["seq"], r["effect"], r["applied_at"] is not None,
                r["evidence_event_id"] is not None) for r in e3.case_events(inst, ref)]
    events = _events(inst, ref)
    per_event: dict[str, int] = {}
    for ev in events:
        key = str(ev["metadata"]["case_event_id"])
        per_event[key] = per_event.get(key, 0) + 1
    identity = sorted((ev["metadata"]["seq"], ev["metadata"]["record"],
                       ev["metadata"]["verification_id"] is not None, ev["metadata"]["class"],
                       ev["metadata"]["reason"], ev["metadata"]["effect"],
                       ev["metadata"]["open_kind"]) for ev in events)
    evidence_ids = {str(r["evidence_event_id"]) for r in e3.case_events(inst, ref)}
    assert evidence_ids == {str(ev["event_id"]) for ev in events}
    return ((case["state"], case["case_version"], case["current_class"],
             case["current_reason"], case["item_kind"]), notes, effects,
            sorted(per_event.values()), identity)


@pytest.mark.parametrize("effect", ["noted", "none"])
def test_el3b_g02_timer_and_staff_projection_are_equivalent(install: e4.EL3BInstall,
                                                            effect: str) -> None:
    inst = _begin(install)
    ref_t, _item_t = _el_item(inst)
    ref_s, item_s = _el_item(inst)
    token = _token_of(inst, item_s)
    for ref in (ref_t, ref_s):
        e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
        if effect == "none":  # FIXTURE: the same rule-3 input on both appointments
            e3.set_watermark(inst, ref, int(e4.key_row(inst, ref)["next_seq"]) - 1)
    staff = _contact(inst, item_s, token)                    # the staff path projects seq2
    assert staff.outcome == ("case_changed" if effect == "noted" else "applied")
    assert e3.drain(inst) == {effect: 1}                      # the timer projects ref_t's
    assert _normal(inst, ref_t) == _normal(inst, ref_s)
    seq2 = {ref: [ev for ev in _events(inst, ref) if ev["metadata"]["seq"] == 2]
            for ref in (ref_t, ref_s)}
    ((t2,), (s2,)) = (seq2[ref_t], seq2[ref_s])
    assert (t2["contract_version"], t2["producer_service"], t2["actor_id"]) == (
        1, "m05_intake", None)
    assert t2["workload_id"] is not None
    assert (s2["contract_version"], s2["producer_service"], str(s2["actor_id"]),
            s2["workload_id"]) == (2, "m05_followup", sd.STAFF, None)


# --- E3b-G03 rollback / retry ---------------------------------------------------------------

def test_el3b_g03a_evidence_append_failure_rolls_back_projection_then_retry_once(
        install: e4.EL3BInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    from haloflow.m02.observation import AppendFailed, EventFoundation
    from haloflow.m05.service import ActionFailed

    inst = _begin(install)
    ref, item = _el_item(inst)
    old = _token_of(inst, item)
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    before = (e4.side_effects(inst, ref, str(item)), e3.cases(inst, ref),
              [(r["seq"], r["applied_at"]) for r in e3.case_events(inst, ref)],
              len(_events(inst, ref)))
    original = EventFoundation.append_observation_in
    calls = {"n": 0}

    async def failing(self: Any, binding: Any, observation: Any) -> Any:
        if observation.event_type == CASE_EVENT and observation.contract_version == 2:
            calls["n"] += 1
            raise AppendFailed("INJECTED")
        return await original(self, binding, observation)

    monkeypatch.setattr(EventFoundation, "append_observation_in", failing)
    with pytest.raises(ActionFailed) as failed:
        _contact(inst, item, old)
    assert failed.value.reason_code == "INJECTED" and calls["n"] == 1
    assert (e4.side_effects(inst, ref, str(item)), e3.cases(inst, ref),
            [(r["seq"], r["applied_at"]) for r in e3.case_events(inst, ref)],
            len(_events(inst, ref))) == before  # projection rolled back with the action
    monkeypatch.undo()
    retry = _contact(inst, item, old)
    assert retry.outcome == "case_changed"
    per_seq = sorted(ev["metadata"]["seq"] for ev in _events(inst, ref))
    assert per_seq == [1, 2]  # one observation per case event
    assert e3.count(inst, "m05_contact_entries") == 0


def test_el3b_g03b_resolution_append_failure_rolls_back_the_close_then_retry_once(
        install: e4.EL3BInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    from haloflow.m02.observation import AppendFailed, EventFoundation
    from haloflow.m05.service import ActionFailed

    inst = _begin(install)
    ref, item = _el_item(inst)
    token = _token_of(inst, item)
    key = uuid.uuid4()
    before = e4.side_effects(inst, ref, str(item))
    original = EventFoundation.append_observation_in

    async def failing(self: Any, binding: Any, observation: Any) -> Any:
        if observation.event_type == "m05_followup_resolution_asserted":
            raise AppendFailed("INJECTED")
        return await original(self, binding, observation)

    async def close() -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).resolve(item, 1, "el_self_pay", token,
                                                    kind="claim_and_resolve", key=key)

    monkeypatch.setattr(EventFoundation, "append_observation_in", failing)
    with pytest.raises(ActionFailed):
        asyncio.run(close())
    assert e4.side_effects(inst, ref, str(item)) == before
    monkeypatch.undo()
    assert asyncio.run(close()).outcome == "applied"
    assert asyncio.run(close()).outcome == "replayed"
    assert e3.count(inst, "patient_events",
                    "event_type = 'm05_followup_resolution_asserted'") == 1
    assert [c["state"] for c in e3.cases(inst, ref)] == ["closed"]


# --- E3b-G04 closed-target replay with later pending work -----------------------------------

def test_el3b_g04_closed_target_replay_and_refusals_never_project_or_open(
        install: e4.EL3BInstall) -> None:
    from haloflow.m05.service import ActionRefused

    inst = _begin(install)
    ref, item = _el_item(inst)
    token = _token_of(inst, item)
    key = uuid.uuid4()

    async def close(tok: Any, k: uuid.UUID) -> Any:
        async with e4.staff_runtime(inst) as rt:
            return await e4.Staff(inst, rt).resolve(item, 1, "el_self_pay", tok,
                                                    kind="claim_and_resolve", key=k)

    original = asyncio.run(close(token, key))
    assert original.outcome == "applied"
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")  # pending, unapplied
    pending = [r for r in e3.case_events(inst, ref) if r["applied_at"] is None]
    assert len(pending) == 1
    snapshot = (e3.cases(inst, ref), len(_events(inst, ref)), len(e3.el_items(inst)))

    replay = asyncio.run(close(token, key))
    assert replay.outcome == "replayed" and dict(replay.snapshot) == dict(original.snapshot)

    async def refused(call: Any) -> str:
        try:
            await call
        except ActionRefused as error:
            return str(error.code)
        return "applied"

    async def attempts() -> list[str]:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            fresh = await staff.token(item)
            return [await refused(staff.resolve(item, 2, "el_confirmed_inactive", fresh)),
                    await refused(staff.contact(item, 2, "el_left_message", fresh))]

    assert asyncio.run(attempts()) == ["ITEM_RESOLVED", "ITEM_RESOLVED"]
    assert [r["applied_at"] for r in e3.case_events(inst, ref)
            if r["event_id"] == pending[0]["event_id"]] == [None]  # nothing projected
    assert (e3.cases(inst, ref), len(_events(inst, ref)), len(e3.el_items(inst))) == snapshot
    assert e3.drain(inst) == {"opened": 1}  # the applier opens the later case, once
    assert sorted(c["state"] for c in e3.cases(inst, ref)) == ["closed", "open"]


# --- E3b-G05 the open-case invariant inside the helper --------------------------------------

def test_el3b_g05_helper_refuses_a_target_that_is_not_the_open_case(
        install: e4.EL3BInstall) -> None:
    import psycopg
    from psycopg import errors

    from haloflow.m02.roles import M05_OWNER_ROLE

    inst = _begin(install)
    ref, _item = _el_item(inst)
    e3.later(inst, ref, "previsit", "inactive", "coverage_inactive")
    before = (e3.cases(inst, ref), [(r["seq"], r["applied_at"])
                                    for r in e3.case_events(inst, ref)])
    with psycopg.connect(inst.ids.admin) as conn:
        conn.execute(f"SET ROLE {M05_OWNER_ROLE}")
        with pytest.raises(errors.RaiseException) as raised:
            sa.call_in(conn, inst, "m05_el_project",
                       {"appointment_ref": ref, "case_id": str(uuid.uuid4())})
        conn.rollback()
    assert "CASE_INVARIANT_FAILED" in str(raised.value)
    assert (e3.cases(inst, ref), [(r["seq"], r["applied_at"])
                                  for r in e3.case_events(inst, ref)]) == before


# --- E3b-G06 contact without the append capability ------------------------------------------

def test_el3b_g06_contact_without_append_is_refused_before_any_statement(
        install: e4.EL3BInstall, monkeypatch: pytest.MonkeyPatch) -> None:
    import service_support as sv

    from haloflow.m01.gateway import TenantRepositoryHandle
    from haloflow.m01.staff.service import RouteDenied

    inst = _begin(install)
    ref, item = _el_item(inst)
    token = _token_of(inst, item)
    seen: list[str] = []
    for name in ("execute", "fetch_one", "fetch_all"):
        original = getattr(TenantRepositoryHandle, name)

        def make(method: Any) -> Any:
            async def spy(self: Any, key: str, params: Any = None) -> Any:
                seen.append(key)
                return await method(self, key, params)

            return spy

        monkeypatch.setattr(TenantRepositoryHandle, name, make(original))

    async def body() -> None:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            with pytest.raises(RouteDenied):
                await staff.contact(item, 1, "el_left_message", token,
                                    caps=frozenset(sv.STAFF_CAPS))

    asyncio.run(body())
    assert not [k for k in seen if k.startswith("m05.followup.")]
    assert e3.count(inst, "m05_contact_entries") == 0


# --- E3b-G07 token-less ResolveAction on an eligibility item --------------------------------

def test_el3b_g07_tokenless_resolve_on_an_eligibility_item_is_malformed(
        install: e4.EL3BInstall) -> None:
    from haloflow.m05.service import ActionRefused

    inst = _begin(install)
    ref, item = _el_item(inst)
    before = e4.side_effects(inst, ref, str(item))

    async def body() -> str:
        async with e4.staff_runtime(inst) as rt:
            staff = e4.Staff(inst, rt)
            try:
                await staff.legacy_resolve(item, 1, "reached_no_change",
                                           kind="claim_and_resolve")
            except ActionRefused as refused:
                return str(refused.code)
            return "applied"

    assert asyncio.run(body()) == "COMMAND_MALFORMED"
    assert e4.side_effects(inst, ref, str(item)) == before
