"""Incoming fax routing FX-IN, package FX4 — PostgreSQL rows (RED BY DESIGN). Governing: see
`fx4_support`. Classes: C control, A absence-red. Two-clinic harness (A = install, B = install_b).

- FX4-C00 CONTROL: an FX2 tenant (144) provisions and receives a v1 command (P06b's base)
- FX4-P01 tenant-specific saved names: a name saved in A shows for A's fax from S; B's fax from S
          shows `as_sent` / `none`
- FX4-P02 precedence: saved > as_sent > none; withheld number with a sent name -> `as_sent`,
          sender_last4 null
- FX4-P03 routing unchanged: a sent name equal to another number's saved name routes by number only;
          the hidden-sender reason is unchanged when a sent name is present
- FX4-P04 SQL normalisation == Python mirror on the corpus (stored `sender_name`)
- FX4-P05 replay: same identity + same normalised name -> duplicate (ZSE); two raw names that mask
          the same -> duplicate; name added / changed / removed -> duplicate_mismatch, original
          byte-identical, discrepancy fields include sender_name and no value; a saved name added /
          edited / removed between deliveries does not change the outcome
- FX4-P06 a v1 command (no sender_name) through v2 AND through t139 -> the FX1 outcome and rows,
          stored name NULL
- FX4-P06b UPGRADE: an FX2 tenant (144) receives a real fax through t139, is migrated to 149;
          replay without a name -> duplicate (no write); with a name -> duplicate_mismatch,
          original preserved
- FX4-P07 t147: runtime role refused; support without grant / wrong cap / other clinic refused;
          put, upsert (same sender_id), remove applied; audit target_id = sender_id, no number,
          digest or name in the audit; SENDER_NAME_INVALID over the invalid corpus;
          SENDER_INVALID; SENDER_NOT_FOUND; stored display_name normalised
- FX4-P07b concurrency: a second first-put of the same number blocks on the first (observed),
          then applies: one row, one sender_id, two audit rows with that id, later name wins
- FX4-P07c post-lock revocation: sender_put (upsert) and sender_remove each OBSERVED blocked on the
          sender row lock; the grant is revoked -> SUPPORT_NOT_AUTHORIZED, nothing committed;
          positive control without revocation applies
- FX4-P08 t145: read-only staff reads exactly four keys per queue, position then lower(name),
          inactive included; inactive staff -> ACTOR_INACTIVE; rename continuity
- FX4-P08b route GET /synthetic/v1/fax/queues (actual fax_staff_routes): A's route lists A's queues
          only (B has a queue A lacks); inactive queue `active:false`; no write method on /queues
- FX4-P09 logs: FaxService.receive with a sent name holding a number -> no name, number or digit run
          in any log record
- FX4-P10 two clinics, same number, own saved names
- FX4-B01 direct gateway (v2): a 257-character sender_name -> COMMAND_MALFORMED, ZSE; 256 accepted
- FX4-B02 FOREIGN session context (B's tenant) on A's t149 read (v2) and t145 queues with VALID A
          inputs
          (A's staff, A's fax) -> 22023 'tenant context invalid', both clinics unchanged
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fx1_support as fx
import fx2_support as f2
import fx3a_support as f3
import fx4_support as f4
import m02_support
import psycopg
import pytest
import sa_support as sa
import sh2_support as sh2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
import l1w_db_support as db  # noqa: E402

pytestmark = [pytest.mark.postgres]


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[f4.FX4Install]:
    yield from f4.module_install(request, m02_ids, m05_reset, f"fx4:{request.node.name}")


@pytest.fixture
def install_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Iterator[f4.FX4Install]:
    yield from f4.module_install(request, m02_ids, m05_reset, f"fx4b:{request.node.name}")


@pytest.fixture
def old(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
        m05_reset: Any) -> Iterator[f2.FX2Install]:
    yield from f2.module_install(request, m02_ids, m05_reset, f"fx4old:{request.node.name}")


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


def _support(env: db.L1WEnv, install: sa.SAInstall, key: str, **grant: Any) -> Any:
    caps = grant.pop("caps", (fx.SETTINGS_CAP,))
    return sh2.support_login(env, install, key, caps=caps, **grant)


def _put(conninfo: str, install: sa.SAInstall, sender: str, name: str) -> dict[str, Any]:
    return fx.call_as(conninfo, install, f4.SENDER_PUT, f4.sender_cmd("sender_put", sender, name))


def _audit(install: sa.SAInstall) -> list[dict[str, Any]]:
    return sorted(fx.rows(install, "m05_fx_settings_audit"), key=lambda r: str(r["at"]))


def _senders(install: sa.SAInstall) -> list[dict[str, Any]]:
    return fx.rows(install, "m05_fx_senders")


def _display(install: sa.SAInstall, staff: str, fax_id: str) -> tuple[Any, Any, Any]:
    got = f4.summary(install, staff, fax_id)
    return got["sender_display"]["text"], got["sender_display"]["source"], got["sender_last4"]


def _revoke(install: sa.SAInstall) -> None:
    sa.admin(install, "UPDATE shared.support_access_grants SET state = 'revoked'"
                      " WHERE tenant_id = %s AND %s = ANY (capabilities)",
             (install.tenant_id, fx.SETTINGS_CAP))


# ------------------------------------------------------------------------------- control

def test_fx4_c00_fx2_base(old: f2.FX2Install) -> None:
    """C."""
    old.require()
    fx.seed_line(old, fx.LINE1)
    assert fx.receive(old, fx.fax_cmd(sender=None))["outcome"] == "needs_sorting"


# ------------------------------------------------------------------------------- display

def test_fx4_p01_p10_saved_names_are_per_clinic(install: f4.FX4Install, install_b: f4.FX4Install,
                                                env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install, fx.LINE1)
    f4.ready(install_b, fx.LINE2)
    staff_a, staff_b = f2.seed_staff(install), f2.seed_staff(install_b)
    sender = f4.fresh_sender()
    with _support(env, install, "fx4-p01-a") as support_a:
        _put(support_a, install, sender, "Valley Radiology")
    fid_a = str(f4.receive_named(install, "Valley Rad (fax)", sender=sender)["fax_id"])
    fid_b = str(f4.receive_named(install_b, "Valley Rad (fax)", sender=sender,
                                 line=fx.LINE2)["fax_id"])
    assert _display(install, staff_a, fid_a) == ("Valley Radiology", "saved", sender[-4:])
    assert _display(install_b, staff_b, fid_b) == ("Valley Rad (fax)", "as_sent", sender[-4:])
    # P10: B saves its own name for the same number
    with _support(env, install_b, "fx4-p10-b") as support_b:
        _put(support_b, install_b, sender, "Valley Imaging")
    assert _display(install, staff_a, fid_a)[0] == "Valley Radiology"
    assert _display(install_b, staff_b, fid_b) == ("Valley Imaging", "saved", sender[-4:])


def test_fx4_p02_precedence(install: f4.FX4Install, env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    staff = f2.seed_staff(install, access=("read",))
    saved, plain = f4.fresh_sender(), f4.fresh_sender()
    with _support(env, install, "fx4-p02") as support:
        _put(support, install, saved, "Quest Diagnostics")
    both = str(f4.receive_named(install, "QUEST LAB", sender=saved)["fax_id"])
    sent = str(f4.receive_named(install, "Lakeside Imaging", sender=plain)["fax_id"])
    none = str(f4.receive_named(install, None, sender=f4.fresh_sender())["fax_id"])
    withheld = str(f4.receive_named(install, "Night Clinic", sender=None)["fax_id"])
    assert _display(install, staff, both) == ("Quest Diagnostics", "saved", saved[-4:])
    assert _display(install, staff, sent) == ("Lakeside Imaging", "as_sent", plain[-4:])
    assert _display(install, staff, none)[:2] == (None, "none")
    assert _display(install, staff, withheld) == ("Night Clinic", "as_sent", None)
    listed = f4.read(install, "list", staff, queue_id=None)["faxes"]
    assert all("sender_display" in f for f in listed)


def test_fx4_p03_routing_ignores_names(install: f4.FX4Install, env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    routed_sender, other = f4.fresh_sender(), f4.fresh_sender()
    fx.seed_rule(install, sender=routed_sender, line=None, queue="Imaging")
    with _support(env, install, "fx4-p03") as support:
        _put(support, install, routed_sender, "Valley Radiology")
    result = f4.receive_named(install, "Valley Radiology", sender=other)
    assert (result["outcome"], result["reason"]) == ("needs_sorting", "no_matching_rule")
    hidden = f4.receive_named(install, "Valley Radiology", sender=None)
    assert (hidden["outcome"], hidden["reason"]) == ("needs_sorting", "sender_not_shown")


def test_fx4_p04_sql_matches_mirror(install: f4.FX4Install) -> None:
    """A."""
    from haloflow.m05 import fax_rules

    f4.ready(install)
    for raw, expected in f4.CORPUS:
        fid = str(f4.receive_named(install, raw, sender=None)["fax_id"])
        stored = fx.fax(install, fid)["sender_name"]
        assert stored == expected == fax_rules.normalise_sender_name(raw), (raw, stored)


# ------------------------------------------------------------------------------- replay

def test_fx4_p05_replay(install: f4.FX4Install, env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    sender = f4.fresh_sender()
    base = fx.fax_cmd(sender=sender)
    first = f4.receive(install, {**base, "sender_name": "Lab 540 555 0100"})
    original = fx.fax(install, str(first["fax_id"]))
    before = f4.snapshot(install)
    same = f4.receive(install, {**base, "sender_name": "Lab 540-555-0100"})  # masks the same
    assert (same["outcome"], same["fax_id"]) == ("duplicate", first["fax_id"])
    assert f4.snapshot(install) == before
    with _support(env, install, "fx4-p05") as support:
        _put(support, install, sender, "Saved Name")
        assert f4.receive(install, {**base, "sender_name": "Lab 540-555-0100"})["outcome"] == (
            "duplicate")
        _put(support, install, sender, "Edited Name")
        assert f4.receive(install, {**base, "sender_name": "Lab 540-555-0100"})["outcome"] == (
            "duplicate")
    for variant in ("Other Lab", None):
        changed = f4.receive(install, {**base, "sender_name": variant})
        assert (changed["outcome"], changed["fax_id"]) == ("duplicate_mismatch", first["fax_id"])
    removed = dict(base)
    assert f4.receive(install, removed)["outcome"] == "duplicate_mismatch"
    assert fx.fax(install, str(first["fax_id"])) == original
    discrepancies = fx.rows(install, "m05_fx_discrepancies")
    assert len(discrepancies) == 3
    assert all("sender_name" in d["fields"] for d in discrepancies)
    assert "Other Lab" not in str(discrepancies)


def test_fx4_p06_v1_command_unchanged(install: f4.FX4Install) -> None:
    """A."""
    f4.ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    for call in (f4.receive, fx.receive):            # v2 without the key, and t139 itself
        result = call(install, fx.fax_cmd())
        assert (result["outcome"], result["queue_id"]) == ("routed",
                                                           fx.queue_id(install, "Imaging"))
        assert fx.fax(install, str(result["fax_id"]))["sender_name"] is None


def test_fx4_p06b_upgrade_then_replay(old: f2.FX2Install) -> None:
    """A (Codex v1r1: a REAL pre-t146 receipt across the upgrade)."""
    old.require()
    fx.seed_line(old, fx.LINE1)
    command = fx.fax_cmd(sender=f4.fresh_sender())
    first = fx.receive(old, command)
    f4.upgrade_to_fx4(old)
    assert sa.admin(old, "SELECT schema_version FROM shared.tenants WHERE tenant_id = %s",
                    (old.tenant_id,))[0][0] == f4.FX4_TARGET
    original = fx.fax(old, str(first["fax_id"]))
    assert original["sender_name"] is None
    before = f4.snapshot(old)
    again = f4.receive(old, command)
    assert (again["outcome"], again["fax_id"]) == ("duplicate", first["fax_id"])
    assert f4.snapshot(old) == before
    named = f4.receive(old, {**command, "sender_name": "Late Name"})
    assert (named["outcome"], named["fax_id"]) == ("duplicate_mismatch", first["fax_id"])
    assert fx.fax(old, str(first["fax_id"])) == original


# ------------------------------------------------------------------------------- t147

def test_fx4_p07_sender_settings(install: f4.FX4Install, install_b: f4.FX4Install,
                                 env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    install_b.require()
    sender = f4.fresh_sender()
    command = f4.sender_cmd("sender_put", sender, "Quest Diagnostics")
    before = f4.snapshot(install)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        sa.call(install, f4.SENDER_PUT, command)
    assert f4.snapshot(install) == before
    for key, granted_on, grant in (("nogrant", install, {"caps": None}),
                                   ("other", install_b, {}),
                                   ("cap", install, {"caps": ("m05.reminder.settings",)})):
        with _support(env, granted_on, f"fx4-p07-{key}", **grant) as conninfo:
            fx.refused(install, f4.SENDER_PUT, command, "SUPPORT_NOT_AUTHORIZED",
                       conninfo=conninfo)
    with _support(env, install, "fx4-p07") as support:
        made = _put(support, install, sender, "  Quest  Diagnostics ")
        again = _put(support, install, sender, "Quest Labs")
        assert made["outcome"] == again["outcome"] == "applied"
        assert made["sender_id"] == again["sender_id"]
        assert [(r["sender_e164"], r["display_name"]) for r in _senders(install)] == [
            (sender, "Quest Labs")]
        for bad in f4.SAVED_INVALID:
            fx.refused(install, f4.SENDER_PUT, f4.sender_cmd("sender_put", sender, bad),
                       "SENDER_NAME_INVALID", conninfo=support)
        fx.refused(install, f4.SENDER_PUT, f4.sender_cmd("sender_put", "5405550100", "Quest"),
                   "SENDER_INVALID", conninfo=support)
        removed = fx.call_as(support, install, f4.SENDER_PUT, f4.sender_cmd("sender_remove",
                                                                            sender))
        assert removed["sender_id"] == made["sender_id"]
        fx.refused(install, f4.SENDER_PUT, f4.sender_cmd("sender_remove", sender),
                   "SENDER_NOT_FOUND", conninfo=support)
    audit = _audit(install)
    assert [a["op"] for a in audit] == ["sender_put", "sender_put", "sender_remove"]
    assert {str(a["target_id"]) for a in audit} == {made["sender_id"]}
    text = str(audit)
    assert sender not in text and sender[1:] not in text and "Quest" not in text
    assert _senders(install) == []


def test_fx4_p07_normalised_storage(install: f4.FX4Install, env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    sender = f4.fresh_sender()
    with _support(env, install, "fx4-p07n") as support:
        _put(support, install, sender, "Ｑuest　Diagnostics")  # fullwidth Q + ideographic sp
    assert _senders(install)[0]["display_name"] == "Quest Diagnostics"


def test_fx4_p07b_concurrent_first_put(install: f4.FX4Install, env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    sender = f4.fresh_sender()
    with _support(env, install, "fx4-p07b") as support:
        with psycopg.connect(support) as first:
            one = sa.call_in(first, install, f4.SENDER_PUT,
                             f4.sender_cmd("sender_put", sender, "First Name"))
            waiter = fx.LoginWaiter(install, support, f4.SENDER_PUT,
                                    f4.sender_cmd("sender_put", sender, "Second Name")
                                    ).start_and_block()
            first.commit()
        two = waiter.join()
    assert one["sender_id"] == two["sender_id"]
    assert [(r["display_name"], str(r["sender_id"])) for r in _senders(install)] == [
        ("Second Name", one["sender_id"])]
    assert [str(a["target_id"]) for a in _audit(install)] == [one["sender_id"]] * 2


@pytest.mark.parametrize("op", ["sender_put", "sender_remove"])
@pytest.mark.parametrize("revoke", [True, False], ids=["revoked", "control"])
def test_fx4_p07c_authority_after_lock(install: f4.FX4Install, env: db.L1WEnv, op: str,
                                       revoke: bool) -> None:
    """A (Codex 8f518e20: put/upsert as well as remove)."""
    f4.ready(install)
    sender = f4.fresh_sender()
    command = (f4.sender_cmd("sender_put", sender, "New Name") if op == "sender_put"
               else f4.sender_cmd("sender_remove", sender))
    with _support(env, install, f"fx4-p07c-{op}-{revoke}") as support:
        _put(support, install, sender, "Old Name")
        before = f4.snapshot(install)
        with fx.row_locked(install, "m05_fx_senders", "sender_e164 = %s", (sender,)):
            waiter = fx.LoginWaiter(install, support, f4.SENDER_PUT, command).start_and_block()
            if revoke:
                _revoke(install)
        if revoke:
            with pytest.raises(psycopg.Error) as late:
                waiter.join()
            assert fx.code_of(late.value) == "SUPPORT_NOT_AUTHORIZED"
            assert f4.snapshot(install) == before
        else:
            assert waiter.join()["outcome"] == "applied"
            assert [a["op"] for a in _audit(install)] == ["sender_put", op]


# ------------------------------------------------------------------------------- t145

def test_fx4_p08_queue_read(install: f4.FX4Install, env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install)
    reader = f2.seed_staff(install, access=("read",))
    inactive = f2.seed_staff(install, active=False)
    fx.deactivate_raw(install, "Pharmacy/Refills")
    got = f4.queues(install, reader)
    assert got["schema_version"] == 1
    listed = got["queues"]
    assert all(set(q) == {"queue_id", "name", "active", "position"} for q in listed)
    assert [q["name"] for q in listed] == list(fx.DEFAULT_QUEUES)
    assert [q["active"] for q in listed if q["name"] == "Pharmacy/Refills"] == [False]
    fx.refused(install, f4.QUEUES, f4.queues_cmd(inactive), "ACTOR_INACTIVE")
    imaging = fx.queue_id(install, "Imaging")
    with _support(env, install, "fx4-p08") as support:
        fx.call_as(support, install, fx.SETTINGS_PUT, fx.settings_cmd(
            "queue_rename", queue_id=imaging, name="Radiology & Imaging"))
    renamed = [q for q in f4.queues(install, reader)["queues"] if q["queue_id"] == imaging]
    assert [q["name"] for q in renamed] == ["Radiology & Imaging"]


def test_fx4_p08b_queues_route(install: f4.FX4Install, install_b: f4.FX4Install,
                               env: db.L1WEnv) -> None:
    """A."""
    f4.ready(install, fx.LINE1)
    f4.ready(install_b, fx.LINE2)
    reader = f2.seed_staff(install, access=("read",))
    fx.deactivate_raw(install, "General")
    with _support(env, install_b, "fx4-p08b") as support_b:
        fx.call_as(support_b, install_b, fx.SETTINGS_PUT,
                   fx.settings_cmd("queue_put", name="B Only Queue", position=8))
    prefix = f3.m("fax_staff_routes", package="haloflow.synthetic").PREFIX

    async def body() -> None:
        async with f4.runtime(install) as rt:
            app = f4.staff_app(rt, install, reader)
            status, out = await f3.staff_request(app, "GET", prefix + "/queues")
            assert status == 200
            names = [q["name"] for q in out["queues"]]
            assert "B Only Queue" not in names and names == list(fx.DEFAULT_QUEUES)
            assert [q["active"] for q in out["queues"] if q["name"] == "General"] == [False]
            write, _ = await f3.staff_request(app, "POST", prefix + "/queues", {})
            assert write in (404, 405)

    asyncio.run(body())


# ------------------------------------------------------------------------------- logs, bounds

def test_fx4_p09_no_names_in_logs(install: f4.FX4Install, caplog: pytest.LogCaptureFixture) -> None:
    """A."""
    from haloflow.m02.observation import EventFoundation

    f4.ready(install, fx.LINE1)
    fax_mod = f3.m("fax")

    async def body() -> None:
        async with f4.runtime(install) as rt:
            svc = fax_mod.FaxService(EventFoundation(rt.gateway, producer_service="m05_fax"))
            ctx = await f3.ctx(rt, install, fx.W_FAX, fax_mod.INGRESS_CAPABILITIES,
                               kind="workload")
            with caplog.at_level(logging.DEBUG):
                await svc.receive(ctx, fax_mod.InboundFax(
                    **{**f3.body(), "line": fx.LINE1}, sender_name="Secret Lab 540-555-0177"))

    asyncio.run(body())
    text = "\n".join(r.getMessage() for r in caplog.records)
    for leak in ("Secret Lab", "5405550177", "540-555-0177", "0177"):
        assert leak not in text, leak


def test_fx4_b01_raw_limit_at_gateway(install: f4.FX4Install) -> None:
    """A (Codex 8f518e20: direct gateway 257)."""
    f4.ready(install)
    fx.refused(install, f4.RECEIVE_V2, {**fx.fax_cmd(), "sender_name": "q" * 257},
               "COMMAND_MALFORMED")
    for bad in (42, ["Quest"], {"n": "Quest"}):
        fx.refused(install, f4.RECEIVE_V2, {**fx.fax_cmd(), "sender_name": bad},
                   "COMMAND_MALFORMED")
    ok = f4.receive_named(install, "q" * 256)
    assert fx.fax(install, str(ok["fax_id"]))["sender_name"] == "q" * 40


def test_fx4_b02_foreign_context(install: f4.FX4Install, install_b: f4.FX4Install) -> None:
    """A (Codex 8f518e20: a FOREIGN session context with VALID A inputs kills a missing binding)."""
    f4.ready(install, fx.LINE1)
    install_b.require()
    staff_a = f2.seed_staff(install)
    fid = str(f4.receive_named(install, "Lab", sender=None)["fax_id"])
    assert f4.read(install, "get", staff_a, fax_id=fid)["fax"]["fax_id"] == fid  # positive
    assert f4.queues(install, staff_a)["queues"]                                  # positive
    fx.context_refused(install, install_b, f4.READ_V2, f2.read_cmd("get", staff_a, fax_id=fid))
    fx.context_refused(install, install_b, f4.QUEUES, f4.queues_cmd(staff_a))
    assert str(uuid.uuid4())
