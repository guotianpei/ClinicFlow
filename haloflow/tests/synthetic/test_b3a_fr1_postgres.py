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


@pytest.fixture
def pg2(tmp_path: Path) -> Iterator[s.PgCluster]:
    """A second, independent per-case cluster (reproducibility across installs)."""
    with s.cluster(tmp_path / "second") as c:
        yield c


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


def test_b3a_fr_c8_frozen_e05bc51_installer_is_genuine_and_works(pg: s.PgCluster,
                                                                  tmp_path: Path) -> None:
    """C (fixture control): the frozen e05bc51 installer (zip sha256 pinned, every file's git
    blob id verified in the child) produces a ready install that the SAME frozen code attests."""
    inst = b3.legacy_install(pg, tmp_path)
    assert b3.legacy_run(inst.env, tmp_path, "status") == {
        "ok": True, "status": {"phase": "ready", "sealed": True, "attested": True}}


def test_b3a_fr_l1a_tampered_seal_identity_refused(pg: s.PgCluster, tmp_path: Path) -> None:
    """B (marker case, kept): on an install made by the CURRENT code, a state whose seal names
    the e05bc51 profile identity is refused STATE_CORRUPT, database unchanged. e05bc51 FAILS:
    that identity is the current one, so the edited state is accepted."""
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
    assert outcome == "STATE_CORRUPT", f"tampered seal identity not refused: {outcome}"
    assert s.diff(before, s.snapshot(inst.c)) == {}


def test_b3a_fr_l1b_genuine_e05bc51_install_refused_and_preserved(pg: s.PgCluster,
                                                                 tmp_path: Path) -> None:
    """B (Codex red gate 0719bcd item 1): a GENUINE accepted-B2 ready install (state and
    database written by the frozen e05bc51 installer) is refused by the current code's `init`
    and `status` (and by app start once the lifecycle exists), and neither the state file nor
    the database changes. Fresh provisioning is the remedy, never an in-place upgrade.
    e05bc51 FAILS: the current code is e05bc51, so the legacy install is accepted."""
    inst = b3.legacy_install(pg, tmp_path)
    raw = (inst.st_dir / "state.json").read_bytes()
    before = s.snapshot(inst.c)
    errors = s.mod("errors")
    outcomes: dict[str, str] = {}
    try:
        outcomes["init"] = s.mod("provision").init(inst.env, attest_flag=True).phase
    except errors.SyntheticRefused as e:
        outcomes["init"] = e.code
    try:
        st = s.mod("provision").status(inst.env)
        outcomes["status"] = "attested" if st["attested"] else "not_attested"
    except errors.SyntheticRefused as e:
        outcomes["status"] = e.code
    try:
        lifecycle = s.mod("lifecycle")
    except ModuleNotFoundError:
        lifecycle = None
    if lifecycle is not None:  # pragma: no cover - exercised after implementation
        try:
            asyncio.run(lifecycle.SyntheticLifecycle(inst.env).start())
            outcomes["start"] = "started"
        except errors.SyntheticRefused as e:
            outcomes["start"] = e.code
    accepted = {k: v for k, v in outcomes.items() if v in ("ready", "attested", "started")}
    assert not accepted, f"legacy B2 install accepted: {outcomes}"
    assert outcomes["init"] in ("STATE_CORRUPT", "STATE_DIVERGED"), outcomes
    assert (inst.st_dir / "state.json").read_bytes() == raw, "legacy state file changed"
    assert s.diff(before, s.snapshot(inst.c)) == {}, "legacy database changed"


def _generic(key: str, schema_token: str) -> str:
    """`rows:<normalised table>` -> `shared.<t>` / `{tenant}.<t>` (test-side, independent)."""
    table = key[len("rows:"):]
    nsp, _, rel = table.rpartition(".")
    return f"shared.{rel}" if nsp == "shared" else f"{{tenant}}.{rel}" \
        if nsp == schema_token else f"{nsp}.{rel}"


def _canonical_sealed_digest(cap: Any, mutable: frozenset[str]) -> tuple[str, int]:
    """Independent canonical projection (test-side): the capture's normalised view with the
    ROW CONTENT of runtime-mutable tables removed, every other key kept, sha256 of the
    sort_keys JSON (the same encoding as B2's profile digest)."""
    import hashlib

    tokens = {k[len("rows:"):].rpartition(".")[0] for k in cap.normalised
              if k.startswith("rows:")} - {"shared", "public"}
    (schema_token,) = tokens
    kept = {k: v for k, v in cap.normalised.items()
            if not (k.startswith("rows:") and _generic(k, schema_token) in mutable)}
    dropped = len(cap.normalised) - len(kept)
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest(), dropped


def _ready_capture(inst: b3.Install) -> Any:
    view = s.mod("view")
    attest = s.mod("attest")
    ep = s.mod("dsn").parse_admin_dsn(inst.env["HALOFLOW_SYNTHETIC_ADMIN_DSN"], {})
    with attest.admin_connect(ep, ep.target) as conn:
        return view.capture(conn, inst.state)


def test_b3a_u5b_sealed_ready_digest_reproducible_and_discriminating(
        pg: s.PgCluster, pg2: s.PgCluster, tmp_path: Path) -> None:
    """A (Codex red gate 0719bcd item 2; replaces the format-only U5b). On two fresh installs
    the pinned SEALED_READY_DIGEST equals (a) seal_scope.sealed_digest(capture) and (b) an
    INDEPENDENT test-side canonical projection of the trusted ready view. Discriminators: a
    tampered sealed row changes the recomputed digest (wrong digest refused); legitimate
    runtime rows do not change it."""
    sc = b3.mod("seal_scope")
    prof = s.mod("installed_profile")
    mutable = frozenset(sc.RUNTIME_MUTABLE_TABLES)
    digests = []
    for i, c in ((1, pg), (2, pg2)):
        (tmp_path / f"i{i}").mkdir()
        inst = b3.install(c, tmp_path / f"i{i}")
        cap = _ready_capture(inst)
        canonical, dropped = _canonical_sealed_digest(cap, mutable)
        assert dropped == len(mutable), (dropped, len(mutable))
        assert sc.sealed_digest(cap) == canonical == prof.SEALED_READY_DIGEST
        digests.append(canonical)
    assert digests[0] == digests[1]
    b3.sj_flow(inst, resolve=False)                                  # runtime rows only
    assert _canonical_sealed_digest(_ready_capture(inst), mutable)[0] == prof.SEALED_READY_DIGEST
    inst.admin(TAMPER_SETTINGS.format(s=inst.schema))               # one sealed row
    wrong = _canonical_sealed_digest(_ready_capture(inst), mutable)[0]
    assert wrong != prof.SEALED_READY_DIGEST
    assert sc.sealed_digest(_ready_capture(inst)) == wrong


TAMPER_SETTINGS: Final = 'UPDATE "{s}".m05_reminder_settings SET clinic_display_name = \'Other\''
