"""Incoming fax routing FX-IN, package FX2 — PostgreSQL 17 rows (RED BY DESIGN).

Governing: see `fx2_support` (test cases v1r1 FX2/FX3 revision, Codex ALIGN 19629e35;
OD-FX6, OD-FX8). Every row provisions its tenant(s) at target 143 in its body (class A:
`include_fx2` -> TypeError, "FX2 test installation failed"); provisioner rows also need
`haloflow.synthetic.fax_lines`. ZSE as FX1 (`fx1_support.snapshot`). Contention waits are
bounded (sa.Waiter join 30 s; lock_timeout on blocking sessions).

Rows (test-case ids v1r1; trace in brackets):
- FX2-T00 install: target 143, FX2 ids in order; ACL (runtime EXECUTE on action/read/snapshot;
          staff check owner-only, EXECUTE to the M05 owner) [arch 5.6]
- FX2-A01 open: history `opened`, actor = the staff id, version unchanged; read-only staff may open
- FX2-A02 move: queue changed, version +1, history `moved` (actor, from, to)
- FX2-A03 move with a stale version (still routed) -> VERSION_CONFLICT, ZSE
- FX2-A04 move to unknown / inactive / tenant-B queue -> QUEUE_NOT_FOUND, A and B ZSE
- FX2-A05 file: filed, still listed in its queue, history `filed`
- FX2-A06/A07 move / file a filed fax with its CURRENT version -> FAX_FILED, ZSE (OD-FX8)
- FX2-A08 move / file a needs-sorting fax -> NOT_ROUTED, ZSE
- FX2-A09 sort: sorting item resolved (by, at, queue); fax routed, routed_by staff; `sorted`
- FX2-A10 sort again with the new current version -> ALREADY_SORTED; a rule-routed fax ->
          NOT_NEEDS_SORTING; ZSE
- FX2-A11 two sorts, same version, loser OBSERVED blocked -> loser ALREADY_SORTED; one `sorted`
- FX2-A12a file wins, move (same version) observed blocked -> FAX_FILED
- FX2-A12b move wins, file (same version) observed blocked -> VERSION_CONFLICT
- FX2-A13 sort observed blocked on the target queue held FOR UPDATE by a deactivation that then
          commits -> QUEUE_NOT_FOUND, fax still needs sorting; A13b the holder rolls back -> sorted
- FX2-A14 sort to inactive / tenant-B / unknown queue -> QUEUE_NOT_FOUND, A and B ZSE
- FX2-A15 open observed blocked on the fax row behind a move -> both history rows, commit order
- FX2-A16 identical redelivery after move / file / sort -> duplicate, ZSE
- FX2-A17 inactive staff (each action and read) / read-only staff on move/file/sort ->
          ACTOR_INACTIVE, ZSE
- FX2-A20 (Codex 4eabf042) act WITHOUT read cannot mutate: an act-only staff row is refused
          ACTOR_INACTIVE on move / file / sort (the platform grants act only with read,
          capabilities_for; the store enforces the same invariant); controls: read+act applies,
          read-only is refused [arch 5.6]
- FX2-A18 tenant-B fax id in tenant A (each action and read) -> FAX_NOT_FOUND, A and B ZSE
- FX2-A19 malformed: attribution principal differs from staff_id; workload principal; missing
          expected_version -> COMMAND_MALFORMED, ZSE
- FX2-V01 get: sender_last4 only; no full sender, line, doc_ref or provider id anywhere in the
          result; reason code only
- FX2-V02 list in A only A's faxes; needs-sorting list separate from queue lists
- FX2-V03 a queue list includes filed faxes, marked filed (OD-FX8)
- FX2-N01 lines snapshot returns this tenant's lines, sorted
- FX2-P01 provisioner adds line1 to A: written; snapshot returns it
- FX2-P02 one call adds line1 to A and B -> LINE_DUPLICATE; neither written
- FX2-P03 line1 in A; add line1 to B -> LINE_DUPLICATE; ZSE everywhere
- FX2-P04 another session holds the startup SHARED session lock -> LINE_PROVISION_BUSY; ZSE
- FX2-P05 two provisioner sessions; the second tries while the first holds the lock (barrier) ->
          BUSY; line in exactly one tenant
- FX2-P06 a provisioning transaction open while a startup session requests the shared lock: the
          startup session is OBSERVED blocked; after commit its snapshot contains the new line
- FX2-P07 an admitted receipt held open -> reassign line1 A->B is BUSY; after the receipt commits
          the reassignment succeeds; then a receipt for line1 in A -> LINE_NOT_REGISTERED, A and B
          ZSE
- FX2-P08 the provisioner's exclusive lock held -> a receipt in A is OBSERVED blocked; the
          reassignment commits; the receipt -> LINE_NOT_REGISTERED; no fax row in A or B
- FX2-P09 runtime / staff owner / support writing m05_fx_lines -> permission denied
- FX2-P10 (Codex FK point) remove or reassign line1 while a rule references it -> LINE_IN_USE,
          atomically (nothing of the call written); after the rule is removed by support, the
          reassignment succeeds
- FX2-P11 remove of an absent line -> LINE_NOT_FOUND; unknown tenant -> TENANT_UNKNOWN; ZSE

Mutant killers (wired at green): M-FX2-1 version check removed -> A03; -2 filed guard removed ->
A06/A07; -3 sort-once guard removed -> A10/A11; -4 queue tenant/active check removed -> A04/A14;
-5 provisioner try-lock made blocking -> P04/P05; -6 uniqueness per tenant only -> P02/P03;
-7 staff check removed -> A17; -8 receipt fence removed (FX1 M8) -> P07/P08; -9 version checked
before the state guard -> A11/A12a. (-10, the in-use check removed, is not separately killable:
the rule FK is a deliberate backstop that also yields LINE_IN_USE atomically; P10 pins the
behaviour.) Provisioner waits in P04/P05/P07 are bounded (`_bounded`).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fx1_support as fx
import fx2_support as f2
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
            m05_reset: Any) -> Iterator[f2.FX2Install]:
    yield from f2.module_install(request, m02_ids, m05_reset, f"fx2:{request.node.name}")


@pytest.fixture
def install_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Iterator[f2.FX2Install]:
    yield from f2.module_install(request, m02_ids, m05_reset, f"fx2b:{request.node.name}")


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


def _ready(install: f2.FX2Install) -> f2.FX2Install:
    install.require()
    fx.seed_line(install, fx.LINE1)
    fx.seed_line(install, fx.LINE2)
    return install


def _code(error: BaseException) -> str:
    return fx.code_of(error)


def _refused(install: sa.SAInstall, function: str, command: Any, code: str,
             *others: sa.SAInstall) -> None:
    before = [fx.snapshot(i) for i in others]
    fx.refused(install, function, command, code)
    assert [fx.snapshot(i) for i in others] == before


def _history(install: sa.SAInstall, fax_id: str) -> list[dict[str, Any]]:
    return sorted(fx.rows(install, "m05_fx_history", "fax_id = %s::uuid", (fax_id,)),
                  key=lambda r: int(r["seq"]))


# --- T --------------------------------------------------------------------------------------


def test_fx2_t00_install_and_acl(install: f2.FX2Install) -> None:
    """A."""
    import si_support as si

    from haloflow.synthetic.composition import build_synthetic_registry

    install.require()
    base = build_synthetic_registry(**fx.eligibility_flags(),
                                    include_fx1=True).migration_ids  # type: ignore[call-arg]
    registry = f2.fx2_registry()
    assert registry.target_version == f2.FX2_TARGET
    assert registry.migration_ids == (*base, *f2.FX2_IDS)
    for function in (f2.ACTION, f2.READ, f2.SNAPSHOT):
        assert si.execute_privilege(install, function, sa.RUNTIME), function
        assert not si.execute_privilege(install, function, fx.SUPPORT_ROLE), function
    assert not si.execute_privilege(install, f2.CHECK, sa.RUNTIME)
    assert si.execute_privilege(install, f2.CHECK, sa.M05_OWNER)


# --- A: staff actions -----------------------------------------------------------------------


def test_fx2_a01_open(install: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    reader = f2.seed_staff(install, access=("read",))
    fid = f2.routed_fax(install)
    result = f2.act(install, "open", fid, reader)
    assert (result["outcome"], result["version"]) == ("applied", 1)
    assert [(h["action"], h["actor"]) for h in _history(install, fid)] == [
        ("received_routed", None), ("opened", reader)]


def test_fx2_a02_move(install: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install, "Imaging")
    labs = fx.queue_id(install, "Labs/Results")
    result = f2.act(install, "move", fid, staff, expected_version=1, queue_id=labs)
    assert (result["outcome"], result["queue_id"], result["version"]) == ("applied", labs, 2)
    row = fx.fax(install, fid)
    assert (row["queue_id"], row["state"], row["version"]) == (labs, "routed", 2)
    moved = _history(install, fid)[-1]
    assert (moved["action"], moved["actor"], moved["from_queue_id"], moved["to_queue_id"]) == (
        "moved", staff, fx.queue_id(install, "Imaging"), labs)


def test_fx2_a03_stale_version(install: f2.FX2Install) -> None:
    """A (kills M-FX2-1)."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    f2.act(install, "move", fid, staff, expected_version=1,
           queue_id=fx.queue_id(install, "General"))
    fx.refused(install, f2.ACTION, f2.action_cmd("move", fid, staff, expected_version=1,
                                                 queue_id=fx.queue_id(install, "Imaging")),
               "VERSION_CONFLICT")


def test_fx2_a04_move_target(install: f2.FX2Install, install_b: f2.FX2Install) -> None:
    """A (kills M-FX2-4)."""
    _ready(install)
    _ready(install_b)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    fx.deactivate_raw(install, "General")
    for target in (sa.new_id(), fx.queue_id(install, "General"),
                   fx.queue_id(install_b, "Labs/Results")):
        _refused(install, f2.ACTION, f2.action_cmd("move", fid, staff, expected_version=1,
                                                   queue_id=target),
                 "QUEUE_NOT_FOUND", install_b)


def test_fx2_a05_file(install: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install, "Imaging")
    result = f2.act(install, "file", fid, staff, expected_version=1)
    assert (result["state"], result["version"]) == ("filed", 2)
    listed = f2.read(install, "list", staff, queue_id=fx.queue_id(install, "Imaging"))
    assert [(f["fax_id"], f["state"]) for f in listed["faxes"]] == [(fid, "filed")]
    assert _history(install, fid)[-1]["action"] == "filed"


@pytest.mark.parametrize("action", ["move", "file"])
def test_fx2_a06_a07_filed_is_final(install: f2.FX2Install, action: str) -> None:
    """A (OD-FX8; kills M-FX2-2). The CURRENT version is used."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    f2.act(install, "file", fid, staff, expected_version=1)
    extra = {"queue_id": fx.queue_id(install, "General")} if action == "move" else {}
    fx.refused(install, f2.ACTION, f2.action_cmd(action, fid, staff, expected_version=2,
                                                 **extra), "FAX_FILED")


@pytest.mark.parametrize("action", ["move", "file"])
def test_fx2_a08_needs_sorting_not_routed(install: f2.FX2Install, action: str) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.sorting_fax(install)
    extra = {"queue_id": fx.queue_id(install, "General")} if action == "move" else {}
    fx.refused(install, f2.ACTION, f2.action_cmd(action, fid, staff, expected_version=1,
                                                 **extra), "NOT_ROUTED")


def test_fx2_a09_sort(install: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.sorting_fax(install)
    labs = fx.queue_id(install, "Labs/Results")
    result = f2.act(install, "sort", fid, staff, expected_version=1, queue_id=labs)
    assert (result["state"], result["queue_id"], result["version"]) == ("routed", labs, 2)
    row = fx.fax(install, fid)
    assert (row["state"], row["queue_id"], row["routed_by"], row["level"]) == (
        "routed", labs, "staff", None)
    (sorting,) = fx.rows(install, "m05_fx_sorting", "fax_id = %s::uuid", (fid,))
    assert (sorting["state"], sorting["resolved_by"], sorting["resolved_queue_id"]) == (
        "resolved", staff, labs)
    assert sorting["resolved_at"] is not None
    last = _history(install, fid)[-1]
    assert (last["action"], last["actor"], last["to_queue_id"]) == ("sorted", staff, labs)


def test_fx2_a10_sort_once(install: f2.FX2Install) -> None:
    """A (kills M-FX2-3)."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.sorting_fax(install)
    labs = fx.queue_id(install, "Labs/Results")
    f2.act(install, "sort", fid, staff, expected_version=1, queue_id=labs)
    fx.refused(install, f2.ACTION, f2.action_cmd("sort", fid, staff, expected_version=2,
                                                 queue_id=labs), "ALREADY_SORTED")
    routed = f2.routed_fax(install)
    fx.refused(install, f2.ACTION, f2.action_cmd("sort", routed, staff, expected_version=1,
                                                 queue_id=labs), "NOT_NEEDS_SORTING")


def _waiter(install: sa.SAInstall, command: dict[str, Any]) -> sa.Waiter:
    return sa.Waiter(install, f2.ACTION, command).start_and_block()


def test_fx2_a11_concurrent_sorts(install: f2.FX2Install) -> None:
    """A (kills M-FX2-3, M-FX2-9): the loser is OBSERVED blocked on the fax row; state guard
    precedes the version check, so the loser is ALREADY_SORTED, not VERSION_CONFLICT."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.sorting_fax(install)
    labs, imaging = fx.queue_id(install, "Labs/Results"), fx.queue_id(install, "Imaging")
    with sa.held(install, f2.ACTION, f2.action_cmd("sort", fid, staff, expected_version=1,
                                                   queue_id=labs)):
        loser = _waiter(install, f2.action_cmd("sort", fid, staff, expected_version=1,
                                               queue_id=imaging))
    with pytest.raises(psycopg.Error) as lost:
        loser.join()
    assert _code(lost.value) == "ALREADY_SORTED"
    assert [h["action"] for h in _history(install, fid)].count("sorted") == 1
    assert fx.fax(install, fid)["queue_id"] == labs


@pytest.mark.parametrize(("winner", "loser", "code"), [
    ("file", "move", "FAX_FILED"),
    ("move", "file", "VERSION_CONFLICT"),
], ids=["a12a-file-wins", "a12b-move-wins"])
def test_fx2_a12_move_vs_file(install: f2.FX2Install, winner: str, loser: str,
                              code: str) -> None:
    """A (a12a kills M-FX2-9)."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    general = fx.queue_id(install, "General")

    def command(action: str) -> dict[str, Any]:
        extra = {"queue_id": general} if action == "move" else {}
        return f2.action_cmd(action, fid, staff, expected_version=1, **extra)

    with sa.held(install, f2.ACTION, command(winner)):
        waiter = _waiter(install, command(loser))
    with pytest.raises(psycopg.Error) as lost:
        waiter.join()
    assert _code(lost.value) == code


@pytest.mark.parametrize("commit", [True, False], ids=["a13-deactivated", "a13b-rolled-back"])
def test_fx2_a13_sort_vs_deactivation(install: f2.FX2Install, commit: bool) -> None:
    """A: the sort is OBSERVED blocked on the target queue (its FOR SHARE vs the holder's
    FOR UPDATE)."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.sorting_fax(install)
    labs = fx.queue_id(install, "Labs/Results")
    with psycopg.connect(install.ids.admin) as holder:
        holder.execute(sa.q(install, "SELECT 1 FROM {s}.m05_fx_queues WHERE queue_id = %s::uuid"
                                     " FOR UPDATE"), (labs,))
        holder.execute(sa.q(install, "UPDATE {s}.m05_fx_queues SET active = false"
                                     " WHERE queue_id = %s::uuid"), (labs,))
        waiter = _waiter(install, f2.action_cmd("sort", fid, staff, expected_version=1,
                                                queue_id=labs))
        if commit:
            holder.commit()
        else:
            holder.rollback()
    if commit:
        with pytest.raises(psycopg.Error) as refused:
            waiter.join()
        assert _code(refused.value) == "QUEUE_NOT_FOUND"
        assert fx.fax(install, fid)["state"] == "needs_sorting"
    else:
        assert waiter.join()["queue_id"] == labs


def test_fx2_a14_sort_target(install: f2.FX2Install, install_b: f2.FX2Install) -> None:
    """A (kills M-FX2-4)."""
    _ready(install)
    _ready(install_b)
    staff = f2.seed_staff(install)
    fid = f2.sorting_fax(install)
    fx.deactivate_raw(install, "General")
    for target in (fx.queue_id(install, "General"), fx.queue_id(install_b, "Labs/Results"),
                   sa.new_id()):
        _refused(install, f2.ACTION, f2.action_cmd("sort", fid, staff, expected_version=1,
                                                   queue_id=target),
                 "QUEUE_NOT_FOUND", install_b)


def test_fx2_a15_open_behind_move(install: f2.FX2Install) -> None:
    """A: the open is OBSERVED blocked on the fax row; both history rows, in commit order."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    with sa.held(install, f2.ACTION, f2.action_cmd("move", fid, staff, expected_version=1,
                                                   queue_id=fx.queue_id(install, "General"))):
        opener = _waiter(install, f2.action_cmd("open", fid, staff))
    opener.join()
    assert [(h["seq"], h["action"]) for h in _history(install, fid)] == [
        (1, "received_routed"), (2, "moved"), (3, "opened")]


@pytest.mark.parametrize("step", ["move", "file", "sort"])
def test_fx2_a16_redelivery_after_action(install: f2.FX2Install, step: str) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    if step == "sort":
        command = fx.fax_cmd(sender=None)
        fid = str(fx.receive(install, command)["fax_id"])
        f2.act(install, "sort", fid, staff, expected_version=1,
               queue_id=fx.queue_id(install, "General"))
    else:
        sender = "+18045550111"
        fx.seed_rule(install, sender=sender, line=None, queue="Imaging")
        command = fx.fax_cmd(sender=sender)
        fid = str(fx.receive(install, command)["fax_id"])
        extra = {"queue_id": fx.queue_id(install, "General")} if step == "move" else {}
        f2.act(install, step, fid, staff, expected_version=1, **extra)
    before = fx.snapshot(install)
    assert fx.receive(install, command)["outcome"] == "duplicate"
    assert fx.snapshot(install) == before


def test_fx2_a17_staff_check(install: f2.FX2Install) -> None:
    """A (kills M-FX2-7)."""
    _ready(install)
    staff = f2.seed_staff(install)
    reader = f2.seed_staff(install, access=("read",))
    gone = f2.seed_staff(install)
    f2.deactivate_staff(install, gone)
    fid = f2.routed_fax(install)
    sid = f2.sorting_fax(install)
    general = fx.queue_id(install, "General")
    commands = [
        f2.action_cmd("open", fid, gone),
        f2.action_cmd("move", fid, gone, expected_version=1, queue_id=general),
        f2.action_cmd("file", fid, gone, expected_version=1),
        f2.action_cmd("sort", sid, gone, expected_version=1, queue_id=general),
        f2.action_cmd("move", fid, reader, expected_version=1, queue_id=general),
        f2.action_cmd("file", fid, reader, expected_version=1),
        f2.action_cmd("sort", sid, reader, expected_version=1, queue_id=general),
        f2.action_cmd("open", fid, sa.new_id()),
    ]
    for command in commands:
        fx.refused(install, f2.ACTION, command, "ACTOR_INACTIVE")
    fx.refused(install, f2.READ, f2.read_cmd("get", gone, fax_id=fid), "ACTOR_INACTIVE")
    assert f2.act(install, "open", fid, staff)["outcome"] == "applied"  # positive control


def test_fx2_a18_foreign_fax(install: f2.FX2Install, install_b: f2.FX2Install) -> None:
    """A (FR-10)."""
    _ready(install)
    _ready(install_b)
    staff = f2.seed_staff(install)
    foreign = f2.routed_fax(install_b)
    general = fx.queue_id(install, "General")
    for command in (f2.action_cmd("open", foreign, staff),
                    f2.action_cmd("move", foreign, staff, expected_version=1, queue_id=general),
                    f2.action_cmd("file", foreign, staff, expected_version=1),
                    f2.action_cmd("sort", foreign, staff, expected_version=1, queue_id=general)):
        _refused(install, f2.ACTION, command, "FAX_NOT_FOUND", install_b)
    for view in ("get", "history"):
        _refused(install, f2.READ, f2.read_cmd(view, staff, fax_id=foreign), "FAX_NOT_FOUND",
                 install_b)


def test_fx2_a19_malformed(install: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    other = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    mismatched = f2.action_cmd("open", fid, staff)
    mismatched["attribution"] = sa.attribution(other, kind="actor")
    workload = f2.action_cmd("open", fid, staff)
    workload["attribution"] = sa.attribution(staff, kind="workload")
    missing = f2.action_cmd("file", fid, staff)
    for command in (mismatched, workload, missing,
                    {**f2.action_cmd("open", fid, staff), "action": "delete"}):
        fx.refused(install, f2.ACTION, command, "COMMAND_MALFORMED")


# --- V: reads -------------------------------------------------------------------------------


def test_fx2_v01_get_masks(install: f2.FX2Install) -> None:
    """A (FR-11)."""
    _ready(install)
    staff = f2.seed_staff(install)
    command = fx.fax_cmd(sender=None)
    fid = str(fx.receive(install, command)["fax_id"])
    got = f2.read(install, "get", staff, fax_id=fid)["fax"]
    assert (got["fax_id"], got["state"], got["reason"], got["sender_last4"]) == (
        fid, "needs_sorting", "sender_not_shown", None)
    routed_cmd = fx.fax_cmd(sender="+18045551234")
    fx.seed_rule(install, sender="+18045551234", line=None, queue="Imaging")
    rid = str(fx.receive(install, routed_cmd)["fax_id"])
    text = str(f2.read(install, "get", staff, fax_id=rid))
    assert f2.read(install, "get", staff, fax_id=rid)["fax"]["sender_last4"] == "1234"
    for secret in ("+18045551234", fx.LINE1, routed_cmd["doc_ref"],
                   routed_cmd["provider_fax_id"]):
        assert secret not in text, secret


def test_fx2_v02_lists(install: f2.FX2Install, install_b: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    _ready(install_b)
    staff = f2.seed_staff(install)
    routed = f2.routed_fax(install, "Imaging")
    sorting = f2.sorting_fax(install)
    f2.routed_fax(install_b, "Imaging")
    f2.sorting_fax(install_b)
    imaging = f2.read(install, "list", staff, queue_id=fx.queue_id(install, "Imaging"))
    assert [f["fax_id"] for f in imaging["faxes"]] == [routed]
    pending = f2.read(install, "list", staff, queue_id=None)
    assert [(f["fax_id"], f["reason"]) for f in pending["faxes"]] == [
        (sorting, "sender_not_shown")]
    fx.refused(install, f2.READ, f2.read_cmd("list", staff,
                                             queue_id=fx.queue_id(install_b, "Imaging")),
               "QUEUE_NOT_FOUND")


def test_fx2_v03_filed_listed(install: f2.FX2Install) -> None:
    """A (OD-FX8)."""
    _ready(install)
    staff = f2.seed_staff(install, access=("read", "act"))
    fid = f2.routed_fax(install, "Imaging")
    f2.act(install, "file", fid, staff, expected_version=1)
    listed = f2.read(install, "list", staff, queue_id=fx.queue_id(install, "Imaging"))
    assert [(f["fax_id"], f["state"]) for f in listed["faxes"]] == [(fid, "filed")]
    history = f2.read(install, "history", staff, fax_id=fid)["history"]
    assert [(h["action"], h["actor"]) for h in history] == [
        ("received_routed", None), ("filed", staff)]


def test_fx2_n01_lines_snapshot(install: f2.FX2Install) -> None:
    """A."""
    _ready(install)
    result = sa.call(install, f2.SNAPSHOT, {"schema_version": 1})
    assert result == {"schema_version": 1, "lines": sorted([fx.LINE1, fx.LINE2])}


# --- P: provisioner line step ---------------------------------------------------------------


def _lines() -> Any:
    from haloflow.synthetic import fax_lines

    return fax_lines


def _provision(install: sa.SAInstall, *ops: tuple[str, sa.SAInstall, str]) -> None:
    lines = _lines()
    lines.provision_lines(install.ids.admin,
                          [lines.LineOp(op=op, tenant_id=t.tenant_id, line=line)
                           for op, t, line in ops])


def _provision_refused(install: sa.SAInstall, code: str, *ops: tuple[str, sa.SAInstall, str],
                       tenants: tuple[sa.SAInstall, ...] = ()) -> None:
    lines = _lines()
    before = [fx.snapshot(t) for t in (install, *tenants)]
    with pytest.raises(lines.LineProvisionRefused) as refused:
        _provision(install, *ops)
    assert refused.value.code == code
    assert [fx.snapshot(t) for t in (install, *tenants)] == before


def _snapshot_lines(install: sa.SAInstall) -> list[str]:
    return list(sa.call(install, f2.SNAPSHOT, {"schema_version": 1})["lines"])


def test_fx2_p01_add(install: f2.FX2Install) -> None:
    """A."""
    install.require()
    _provision(install, ("add", install, fx.LINE1))
    assert _snapshot_lines(install) == [fx.LINE1]


def test_fx2_p02_p03_duplicates(install: f2.FX2Install, install_b: f2.FX2Install) -> None:
    """A (kills M-FX2-6)."""
    install.require()
    install_b.require()
    _provision_refused(install, "LINE_DUPLICATE", ("add", install, fx.LINE1),
                       ("add", install_b, fx.LINE1), tenants=(install_b,))
    _provision(install, ("add", install, fx.LINE1))
    _provision_refused(install, "LINE_DUPLICATE", ("add", install_b, fx.LINE1),
                       tenants=(install_b,))


def _shared_session(install: sa.SAInstall) -> psycopg.Connection[Any]:
    from haloflow.m05.fax_rules import K_FX_LINES

    conn = psycopg.connect(install.ids.admin, autocommit=True)
    conn.execute("SELECT pg_advisory_lock_shared(%s)", (K_FX_LINES,))
    return conn


def _bounded(body: Any, timeout_s: float = 20.0) -> None:
    """Run `body` in a thread; a blocking provisioner (M-FX2-5) fails the row, never hangs it."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            body()
            outcome["ok"] = True
        except BaseException as error:  # noqa: BLE001 - re-raised below
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)
    assert not thread.is_alive(), "the provisioner blocked instead of refusing BUSY"
    if "error" in outcome:
        raise outcome["error"]
    assert outcome == {"ok": True}


def test_fx2_p04_busy_while_running(install: f2.FX2Install) -> None:
    """A (kills M-FX2-5: a blocking lock fails the bounded 20 s thread join)."""
    install.require()
    with _shared_session(install):
        _bounded(lambda: _provision_refused(install, "LINE_PROVISION_BUSY",
                                            ("add", install, fx.LINE1)))


def test_fx2_p05_two_provisioners(install: f2.FX2Install, install_b: f2.FX2Install) -> None:
    """A: the first provisioner's transaction is held (exclusive lock taken) while the second
    tries; the second is BUSY; the line ends in exactly one tenant."""
    from haloflow.m05.fax_rules import K_FX_LINES

    install.require()
    install_b.require()
    with psycopg.connect(install.ids.admin) as first:
        assert first.execute("SELECT pg_try_advisory_xact_lock(%s)", (K_FX_LINES,)
                             ).fetchone() == (True,)
        first.execute(sa.q(install, "INSERT INTO {s}.m05_fx_lines (line_e164) VALUES (%s)"),
                      (fx.LINE1,))
        _bounded(lambda: _provision_refused(install, "LINE_PROVISION_BUSY",
                                            ("add", install_b, fx.LINE1), tenants=(install_b,)))
        first.commit()
    assert (_snapshot_lines(install), _snapshot_lines(install_b)) == ([fx.LINE1], [])


def test_fx2_p06_startup_waits_for_provisioning(install: f2.FX2Install) -> None:
    """A: a startup session asking for the shared lock is OBSERVED blocked by an open
    provisioning transaction and then reads the committed line."""
    from haloflow.m05.fax_rules import K_FX_LINES

    install.require()
    name = f"fx2-startup-{sa.new_id()[:8]}"
    seen: dict[str, Any] = {}

    def startup() -> None:
        with psycopg.connect(install.ids.admin, autocommit=True,
                             application_name=name) as conn:
            conn.execute("SET lock_timeout = '20s'")
            conn.execute("SELECT pg_advisory_lock_shared(%s)", (K_FX_LINES,))
            seen["lines"] = _snapshot_lines(install)

    with psycopg.connect(install.ids.admin) as provisioner:
        provisioner.execute("SELECT pg_advisory_xact_lock(%s)", (K_FX_LINES,))
        provisioner.execute(sa.q(install, "INSERT INTO {s}.m05_fx_lines (line_e164)"
                                          " VALUES (%s)"), (fx.LINE1,))
        thread = threading.Thread(target=startup, daemon=True)
        thread.start()
        sa.wait_until_blocked(install, name)
        provisioner.commit()
    thread.join(timeout=30)
    assert seen == {"lines": [fx.LINE1]}


def test_fx2_p07_receipt_blocks_reassignment(install: f2.FX2Install,
                                             install_b: f2.FX2Install) -> None:
    """A (kills M-FX2-8 / FX1 M8)."""
    install.require()
    install_b.require()
    _provision(install, ("add", install, fx.LINE1))
    with sa.held(install, fx.RECEIVE, fx.fax_cmd()):
        _bounded(lambda: _provision_refused(install, "LINE_PROVISION_BUSY",
                                            ("remove", install, fx.LINE1),
                                            ("add", install_b, fx.LINE1), tenants=(install_b,)))
    _provision(install, ("remove", install, fx.LINE1), ("add", install_b, fx.LINE1))
    assert (_snapshot_lines(install), _snapshot_lines(install_b)) == ([], [fx.LINE1])
    before_b = fx.snapshot(install_b)
    fx.refused(install, fx.RECEIVE, fx.fax_cmd(), "LINE_NOT_REGISTERED")
    assert fx.snapshot(install_b) == before_b


def test_fx2_p08_provisioner_blocks_receipt(install: f2.FX2Install,
                                            install_b: f2.FX2Install) -> None:
    """A (kills M-FX2-8): a receipt in A is OBSERVED blocked while a reassignment holds the
    exclusive lock, then refused; no fax row anywhere."""
    from haloflow.m05.fax_rules import K_FX_LINES

    install.require()
    install_b.require()
    _provision(install, ("add", install, fx.LINE1))
    with psycopg.connect(install.ids.admin) as provisioner:
        provisioner.execute("SELECT pg_advisory_xact_lock(%s)", (K_FX_LINES,))
        provisioner.execute(sa.q(install, "DELETE FROM {s}.m05_fx_lines WHERE line_e164 = %s"),
                            (fx.LINE1,))
        provisioner.execute(sa.q(install_b, "INSERT INTO {s}.m05_fx_lines (line_e164)"
                                            " VALUES (%s)"), (fx.LINE1,))
        receipt = sa.Waiter(install, fx.RECEIVE, fx.fax_cmd()).start_and_block()
        provisioner.commit()
    with pytest.raises(psycopg.Error) as refused:
        receipt.join()
    assert _code(refused.value) == "LINE_NOT_REGISTERED"
    assert fx.rows(install, "m05_fx_faxes") == fx.rows(install_b, "m05_fx_faxes") == []


def test_fx2_p09_no_direct_line_writes(install: f2.FX2Install, env: db.L1WEnv) -> None:
    """A."""
    install.require()
    insert = sa.q(install, "INSERT INTO {s}.m05_fx_lines (line_e164) VALUES ('+15405550999')")
    with sa.runtime_conn(install) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(insert)
    with psycopg.connect(install.ids.admin, autocommit=True) as conn:
        conn.execute(f"SET ROLE {f2.STAFF_OWNER}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(insert)
    with sh2.support_login(env, install, "fx2-p09", caps=(fx.SETTINGS_CAP,)) as support, \
            psycopg.connect(support, autocommit=True) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(insert)


def test_fx2_p10_line_in_use(install: f2.FX2Install, install_b: f2.FX2Install,
                             env: db.L1WEnv) -> None:
    """A (Codex FK point; kills M-FX2-10): removal and reassignment are refused atomically while
    a rule references the line; after support removes the rule the reassignment succeeds."""
    install.require()
    install_b.require()
    _provision(install, ("add", install, fx.LINE1), ("add", install, fx.LINE2))
    rule = fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    _provision_refused(install, "LINE_IN_USE", ("remove", install, fx.LINE1),
                       tenants=(install_b,))
    _provision_refused(install, "LINE_IN_USE", ("remove", install, fx.LINE2),
                       ("remove", install, fx.LINE1), ("add", install_b, fx.LINE1),
                       tenants=(install_b,))  # nothing of the call is written, not even LINE2
    with sh2.support_login(env, install, "fx2-p10", caps=(fx.SETTINGS_CAP,)) as support:
        fx.call_as(support, install, fx.SETTINGS_PUT,
                   fx.settings_cmd("rule_remove", rule_id=rule))
    _provision(install, ("remove", install, fx.LINE1), ("add", install_b, fx.LINE1))
    assert (_snapshot_lines(install), _snapshot_lines(install_b)) == ([fx.LINE2], [fx.LINE1])


def test_fx2_p11_absent_and_unknown(install: f2.FX2Install) -> None:
    """A."""
    install.require()
    _provision_refused(install, "LINE_NOT_FOUND", ("remove", install, fx.LINE1))
    lines = _lines()
    with pytest.raises(lines.LineProvisionRefused) as refused:
        lines.provision_lines(install.ids.admin,
                              [lines.LineOp(op="add", tenant_id="no-such-tenant",
                                            line=fx.LINE1)])
    assert refused.value.code == "TENANT_UNKNOWN"


def test_fx2_a20_act_without_read_cannot_mutate(install: f2.FX2Install) -> None:
    """A (Codex 4eabf042). Platform policy and the stored invariant agree."""
    from haloflow.m01.staff.service import Standing
    from haloflow.platform.capabilities import capabilities_for, route_capabilities

    act_only = Standing(status="active", access=("act",), roles=())
    assert route_capabilities("act", capabilities_for(act_only)) is None  # platform: 403
    _ready(install)
    act_staff = f2.seed_staff(install, access=("act",))
    reader = f2.seed_staff(install, access=("read",))
    both = f2.seed_staff(install, access=("read", "act"))
    fid = f2.routed_fax(install)
    sid = f2.sorting_fax(install)
    general = fx.queue_id(install, "General")
    for staff in (act_staff, reader):
        for command in (f2.action_cmd("move", fid, staff, expected_version=1, queue_id=general),
                        f2.action_cmd("file", fid, staff, expected_version=1),
                        f2.action_cmd("sort", sid, staff, expected_version=1, queue_id=general)):
            fx.refused(install, f2.ACTION, command, "ACTOR_INACTIVE")
    assert f2.act(install, "move", fid, both, expected_version=1,
                  queue_id=general)["outcome"] == "applied"  # authorized control
