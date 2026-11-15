"""Option B B3a F-R1 amendment: synthetic staff-resolution activation, test first.

Governing: Codex disposition `codex_b3a-fr1-review.md` (relay 20261115T023713Z, ALIGN for a
focused test-first amendment). Finding and baseline measured on UNMODIFIED e05bc51
(claude_b3a-measure/fr1-resolution-activation.jsonl fd52996e…; b2-runtime-measure.jsonl
96ff5382… for SC3-pre): on a fresh B2 install every staff resolve fails
AppendFailed(CONTRACT_UNSUPPORTED) because t013 installs the m05_followup family, the
m05_followup_resolution_asserted type and the m02 resolver m05_followup_item_v1 INACTIVE and
the synthetic TEST profile (B1: SA_/SI_TEST_PROFILE_SQL) never activated them. Durable
reachability with the activation EMULATED admin-side: fr1-reach.jsonl (all six W1 outcomes
across four item kinds applied, one note and six links persisted, exact replay after a runtime
restart returns `replayed` and changes nothing).

Amendment interface the rows pin (does not exist yet):
- `haloflow.synthetic.composition.RESOLUTION_PROFILE_SQL` == tests'
  `m05_support.B4_RESOLUTION_PROFILE_SQL` (whitespace-normalised), executed by
  `provision._run_profile` inside the `profile` phase transaction;
- `installed_profile.PROFILE_ID` / `POINTS` regenerated (B2-P1 re-measures), so a state file
  sealed under the e05bc51 identity is refused (STATE_CORRUPT), never adopted.

Fresh provisioning vs restart recovery (documented separately, as asked):
- fresh: a NEW install provisions the resolution activation in its `profile` phase; a ready
  install sealed under the e05bc51 profile is refused; the remedy is a new disposable install
  (`init` into a fresh cluster), never an in-place upgrade;
- restart: a runtime restart on an amended install re-reads durable state; an exact retry of a
  resolve (same client_action_id) returns `replayed` and changes nothing (FR-R1 gen 2).

Classes: FR-E1, FR-P1 A/B as stated; FR-E2, FR-R1, FR-L1 are B (behaviour-red on e05bc51 with
the named assertion; they pass on the amendment without any fixture-side activation).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import b2_support as s
import b3a_support as b3
import pytest

pytestmark = pytest.mark.postgres

E05_PROFILE_ID: Final = "a2e8f9cd622f3d01c1f499919a5b21067ecc59c4e5bb9cbfd0efa8f7e7858eaa"
OUTCOMES: Final = ("rescheduled", "cancelled_in_ehr", "reached_no_change",
                   "no_contact_left_message", "no_contact_no_answer", "other")


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[s.PgCluster]:
    with s.cluster(tmp_path) as c:
        yield c
    s.reap()


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_b3a_fr_e1_profile_carries_the_resolution_activation() -> None:
    """A: the synthetic composition carries RESOLUTION_PROFILE_SQL equal to the tests'
    B4_RESOLUTION_PROFILE_SQL (single statement set; no drift)."""
    import m05_support

    comp = s.mod("composition")
    assert _norm(comp.RESOLUTION_PROFILE_SQL) == _norm(m05_support.B4_RESOLUTION_PROFILE_SQL)


def test_b3a_fr_e2_fresh_install_has_resolution_active(pg: s.PgCluster, tmp_path: Path) -> None:
    """B: after `init` alone (no fixture activation) the family, the event type and the
    resolver are active. e05bc51 FAILS: all three inactive."""
    inst = b3.install(pg, tmp_path)
    sch = inst.schema
    got = (inst.admin(f'SELECT is_active FROM "{sch}".ref_action_families'
                      " WHERE action_family = 'm05_followup'")[0][0],
           inst.admin(f'SELECT is_active FROM "{sch}".event_types'
                      " WHERE event_type = 'm05_followup_resolution_asserted'")[0][0],
           inst.admin(f'SELECT is_active FROM "{sch}".m02_link_resolvers'
                      " WHERE resolver_id = 'm05_followup_item_v1'")[0][0])
    assert got == (True, True, True), f"resolution activation missing after init: {got}"


def test_b3a_fr_r1_every_outcome_resolves_persists_and_replays(pg: s.PgCluster,
                                                              tmp_path: Path) -> None:
    """B: on a fresh install with NO fixture-side activation, every W1 outcome resolves through
    the installed staff-service path (StaffServiceV3 with the install's own staff member)
    across four item kinds, the note-required Other included; links and the note persist; after
    a runtime restart an exact retry of each resolve returns `replayed` and changes nothing.
    e05bc51 FAILS: AppendFailed CONTRACT_UNSUPPORTED (the discriminator, asserted by name)."""
    import sa_support as sa
    import service_support as sv
    import sj_harness as sj
    import test_sj_postgres as tsj

    from haloflow.m02.observation import AppendFailed, EventFoundation
    from haloflow.m05.service import ResolveAction, StaffAction
    from haloflow.m05.writeback import StaffServiceV3

    inst = b3.install(pg, tmp_path)
    si_inst = b3.as_si_install(inst)
    st = inst.state
    done: dict[str, tuple[uuid.UUID, uuid.UUID, str | None]] = {}
    results: dict[str, Any] = {}

    def staff_for(rt: Any) -> Any:
        return StaffServiceV3(rt.gateway, foundation=EventFoundation(
            rt.gateway, producer_service=sv.PRODUCER), keys=sv.TestNoteKeys())

    async def gen1() -> None:
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        try:
            for _ in range(2):
                f = await tsj._sent_and_replied(stack)
                tsj._put(si_inst, ehr, f, provider_ref="prov-other")
                await stack.tick_writeback()
            for text in ("2", "3", "NO", "maybe later"):
                f = await tsj._sent_and_replied(stack, text)
                tsj._put(si_inst, ehr, f)
                await stack.tick_all()
            items = sa.admin(si_inst, sa.q(si_inst, "SELECT item_id::text, item_kind FROM"
                                                    " {s}.m05_followup_items ORDER BY created_at"))
            assert [k for _, k in items] == ["ehr_confirmation_check", "ehr_confirmation_check",
                                             "reschedule_request", "cancel_request",
                                             "unclear_reply", "unclear_reply"], items
            staff = staff_for(stack.rt)
            for (iid, kind), oc in zip(items, OUTCOMES, strict=True):
                u = uuid.UUID(iid)
                await staff.act(await b3._staff_ctx(stack.rt, si_inst.tenant_id, st["staff_id"]),
                                StaffAction(item_id=u, client_action_id=uuid.uuid4(),
                                            expected_version=1, type="claim"))
                cid, note = uuid.uuid4(), ("synthetic note" if oc == "other" else None)
                try:
                    r = await staff.act(
                        await b3._staff_ctx(stack.rt, si_inst.tenant_id, st["staff_id"]),
                        ResolveAction(item_id=u, client_action_id=cid, expected_version=2,
                                      type="resolve", outcome_code=oc, note=note))
                    results[f"{kind}:{oc}"] = r.outcome
                    done[oc] = (u, cid, note)
                except AppendFailed as e:
                    results[f"{kind}:{oc}"] = f"AppendFailed {getattr(e, 'reason_code', '')}"
        finally:
            assert await stack.shutdown() == {}

    asyncio.run(gen1())
    unsupported = [k for k, v in results.items() if v == "AppendFailed CONTRACT_UNSUPPORTED"]
    assert not unsupported, f"resolve unsupported on a fresh synthetic install " \
                            f"(CONTRACT_UNSUPPORTED): {unsupported}"
    assert set(results.values()) == {"applied"}, results
    sch = inst.schema
    assert inst.admin(f'SELECT count(*) FROM "{sch}".m05_followup_notes')[0][0] == 1
    assert inst.admin(f'SELECT count(*) FROM "{sch}".patient_event_links')[0][0] == len(OUTCOMES)
    mid = b3.table_digests(inst)

    async def gen2() -> dict[str, str]:  # restart: a NEW runtime on the same install
        ehr, _ = await tsj._flow_ehr(si_inst)
        stack = await sj.build(si_inst, ehr)
        out: dict[str, str] = {}
        try:
            staff = staff_for(stack.rt)
            for oc, (u, cid, note) in done.items():
                r = await staff.act(
                    await b3._staff_ctx(stack.rt, si_inst.tenant_id, st["staff_id"]),
                    ResolveAction(item_id=u, client_action_id=cid, expected_version=2,
                                  type="resolve", outcome_code=oc, note=note))
                out[oc] = r.outcome
        finally:
            assert await stack.shutdown() == {}
        return out

    assert asyncio.run(gen2()) == dict.fromkeys(OUTCOMES, "replayed")
    assert b3.changed(mid, b3.table_digests(inst)) == set()


def test_b3a_fr_p1_profile_identity_regenerated() -> None:
    """B: the installed-state profile identity is regenerated for the amended profile (B2-P1
    re-measures the new POINTS separately). e05bc51 FAILS: identity unchanged."""
    prof = s.mod("installed_profile")
    assert prof.PROFILE_ID != E05_PROFILE_ID, "profile identity not regenerated for F-R1"


def test_b3a_fr_l1_legacy_b2_install_refused_not_adopted(pg: s.PgCluster,
                                                        tmp_path: Path) -> None:
    """B: a ready state sealed under the e05bc51 profile identity is refused (STATE_CORRUPT)
    by `status`-guarded `init` and never adopted; the database is unchanged. e05bc51 FAILS:
    that identity is the current one, so the legacy install is accepted."""
    inst = b3.install(pg, tmp_path)
    path = inst.st_dir / "state.json"
    st = json.loads(path.read_text())
    st["seal"]["profile"] = E05_PROFILE_ID
    path.write_text(json.dumps(st))
    before = s.snapshot(inst.c)
    errors = s.mod("errors")
    try:
        s.mod("provision").init(inst.env, attest_flag=True)
        outcome = "accepted"
    except errors.SyntheticRefused as e:
        outcome = e.code
    assert outcome == "STATE_CORRUPT", f"legacy B2 install not refused: {outcome}"
    assert s.diff(before, s.snapshot(inst.c)) == {}
