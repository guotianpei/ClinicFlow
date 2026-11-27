"""Incoming fax routing FX-IN, package FX5 — PostgreSQL rows (RED BY DESIGN). Governing: see
`fx5_support`. Classes: C control, A absence-red. Two-clinic harness (A = install, B = install_b).

- FX5-C00 CONTROL (pre-state): an FX4 tenant (149) — an ACTIVE READ-ONLY staff member lists, gets,
          reads history, reads queues and opens through the bound v2 / t142 functions (what OD-FX11
          removes); the runtime role can execute all four functions t153 will revoke
- FX5-P01 direct DB, independent of the catalogue: an ACTIVE READ-ONLY staff member on t150 list /
          get / history, t151 queues and t152 open -> ACTOR_INACTIVE, every FX table unchanged
          (no `opened` history row); an ABSENT fax id gives the identical refusal
- FX5-P02 fresh install (153): the runtime role has no EXECUTE on t142 / t143 / t145 / t149
          (effective
          privilege and an actual call -> InsufficientPrivilege); it has EXECUTE on t150 / t151 /
          t152; receive (t148) and the lines snapshot (t144) unchanged
- FX5-P03 positive: act staff list / get / history / queues; open records `opened` with that actor;
          move then file through t152 (FX2 core behaviour kept)
- FX5-P04 tenant: B's act staff through B's gateway with A's fax id -> FAX_NOT_FOUND, A unchanged;
          mismatched session context (B) on A's t151 queues and t150 list (no foreign id) -> 22023
- FX5-P05 premises of the one boolean check (t141): inactive with read+act, no access, act without
          read -> ACTOR_INACTIVE on t150 list and t152 open; ZSE
- FX5-P06 UPGRADE: an FX4 tenant (149) with a real fax and a read-only reader is migrated to 153;
          the FX5 units are recorded in shared.schema_migrations; the runtime role's EXECUTE on the
          four superseded functions is gone; the reader is refused on t150 / t151 / t152 open; an
          act staff member reads and opens
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fx1_support as fx
import fx2_support as f2
import fx4_support as f4
import fx5_support as f5
import m02_support
import psycopg
import pytest
import sa_support as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))

pytestmark = [pytest.mark.postgres]


@pytest.fixture
def install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
            m05_reset: Any) -> Iterator[f5.FX5Install]:
    yield from f5.module_install(request, m02_ids, m05_reset, f"fx5:{request.node.name}")


@pytest.fixture
def install_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Iterator[f5.FX5Install]:
    yield from f5.module_install(request, m02_ids, m05_reset, f"fx5b:{request.node.name}")


@pytest.fixture
def old(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
        m05_reset: Any) -> Iterator[f4.FX4Install]:
    yield from f4.module_install(request, m02_ids, m05_reset, f"fx5old:{request.node.name}")


def _ready(install: fx.FX1Install) -> fx.FX1Install:
    install.require()
    fx.seed_line(install, fx.LINE1)
    fx.seed_line(install, fx.LINE2)
    return install


def _refusals(install: sa.SAInstall, staff: str, fax_id: str) -> list[tuple[str, Any]]:
    """Every FX5 staff read / open entry point for one staff member."""
    return [
        (f5.READ_V3, f2.read_cmd("list", staff, queue_id=None)),
        (f5.READ_V3, f2.read_cmd("get", staff, fax_id=fax_id)),
        (f5.READ_V3, f2.read_cmd("history", staff, fax_id=fax_id)),
        (f5.QUEUES_V2, f4.queues_cmd(staff)),
        (f5.ACTION_V2, f2.action_cmd("open", fax_id, staff)),
    ]


# ------------------------------------------------------------------------------- control

def test_fx5_c00_read_only_today(old: f4.FX4Install) -> None:
    """C (the pre-state OD-FX11 removes)."""
    _ready(old)
    reader = f2.seed_staff(old, access=("read",))
    fid = f2.routed_fax(old)
    imaging = fx.queue_id(old, "Imaging")
    assert [f["fax_id"] for f in f4.read(old, "list", reader, queue_id=imaging)["faxes"]] == [fid]
    assert f4.read(old, "get", reader, fax_id=fid)["fax"]["fax_id"] == fid
    assert f4.read(old, "history", reader, fax_id=fid)["history"]
    assert f4.queues(old, reader)["queues"]
    f2.act(old, "open", fid, reader)
    assert f5.history_actions(old, fid)[-1] == "opened"
    for name in f5.SUPERSEDED:
        assert f5.runtime_can_execute(old, name), name


# ------------------------------------------------------------------------------- refusals

def test_fx5_p01_read_only_refused(install: f5.FX5Install) -> None:
    """A."""
    _ready(install)
    reader = f2.seed_staff(install, access=("read",))
    fid = f2.routed_fax(install)
    absent = str(uuid.uuid4())
    for staff_fax in (fid, absent):
        for function, command in _refusals(install, reader, staff_fax):
            fx.refused(install, function, command, "ACTOR_INACTIVE")
    assert "opened" not in f5.history_actions(install, fid)


def test_fx5_p02_superseded_revoked_fresh(install: f5.FX5Install) -> None:
    """A (Codex 273a6baf item 2: effective runtime denial)."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install)
    for name in f5.SUPERSEDED:
        assert not f5.runtime_can_execute(install, name), name
    for name in (f5.READ_V3, f5.QUEUES_V2, f5.ACTION_V2, f4.RECEIVE_V2, f2.SNAPSHOT):
        assert f5.runtime_can_execute(install, name), name
    before = fx.snapshot(install)
    for name, command in ((f2.READ, f2.read_cmd("get", staff, fax_id=fid)),
                          (f4.READ_V2, f2.read_cmd("get", staff, fax_id=fid)),
                          (f4.QUEUES, f4.queues_cmd(staff)),
                          (f2.ACTION, f2.action_cmd("open", fid, staff))):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            sa.call(install, name, command)
    assert fx.snapshot(install) == before


# ------------------------------------------------------------------------------- positive

def test_fx5_p03_act_staff_unchanged(install: f5.FX5Install) -> None:
    """A."""
    _ready(install)
    staff = f2.seed_staff(install)
    fid = f2.routed_fax(install, "Imaging")
    listed = f5.read(install, "list", staff, queue_id=fx.queue_id(install, "Imaging"))["faxes"]
    assert [f["fax_id"] for f in listed] == [fid]
    got = f5.read(install, "get", staff, fax_id=fid)["fax"]
    assert got["fax_id"] == fid and "sender_display" in got
    assert [q["name"] for q in f5.queues(install, staff)["queues"]] == list(fx.DEFAULT_QUEUES)
    f5.act(install, "open", fid, staff)
    history = f5.read(install, "history", staff, fax_id=fid)["history"]
    assert (history[-1]["action"], history[-1]["actor"]) == ("opened", staff)
    moved = f5.act(install, "move", fid, staff, expected_version=f2.version(install, fid),
                   queue_id=fx.queue_id(install, "General"))
    assert moved["outcome"] == "applied"
    assert f5.history_actions(install, fid)[-1] == "moved"
    f5.act(install, "file", fid, staff, expected_version=f2.version(install, fid))
    assert fx.fax(install, fid)["state"] == "filed"


# ------------------------------------------------------------------------------- tenant

def test_fx5_p04_tenant_isolation(install: f5.FX5Install, install_b: f5.FX5Install) -> None:
    """A (Codex 273a6baf item 2: object isolation AND a collection under a mismatched context)."""
    _ready(install)
    _ready(install_b)
    staff_a, staff_b = f2.seed_staff(install), f2.seed_staff(install_b)
    fid = f2.routed_fax(install)
    assert f5.read(install, "get", staff_a, fax_id=fid)["fax"]["fax_id"] == fid  # positive
    before = fx.snapshot(install)
    fx.refused(install_b, f5.READ_V3, f2.read_cmd("get", staff_b, fax_id=fid), "FAX_NOT_FOUND")
    fx.refused(install_b, f5.ACTION_V2, f2.action_cmd("open", fid, staff_b), "FAX_NOT_FOUND")
    assert fx.snapshot(install) == before
    fx.context_refused(install, install_b, f5.QUEUES_V2, f4.queues_cmd(staff_a))
    fx.context_refused(install, install_b, f5.READ_V3, f2.read_cmd("list", staff_a,
                                                                   queue_id=None))


# ------------------------------------------------------------------------------- premises

def test_fx5_p05_check_premises(install: f5.FX5Install) -> None:
    """A (Codex 273a6baf item 1: one boolean, several premises)."""
    _ready(install)
    fid = f2.routed_fax(install)
    for staff in (f2.seed_staff(install, active=False), f2.seed_staff(install, access=()),
                  f2.seed_staff(install, access=("act",))):
        fx.refused(install, f5.READ_V3, f2.read_cmd("list", staff, queue_id=None),
                   "ACTOR_INACTIVE")
        fx.refused(install, f5.ACTION_V2, f2.action_cmd("open", fid, staff), "ACTOR_INACTIVE")


# ------------------------------------------------------------------------------- upgrade

def test_fx5_p06_upgrade_revokes_and_refuses(old: f4.FX4Install) -> None:
    """A (Codex 273a6baf item 2: a REAL 149 -> 153 upgrade)."""
    _ready(old)
    reader = f2.seed_staff(old, access=("read",))
    staff = f2.seed_staff(old)
    fid = f2.routed_fax(old)
    assert f4.read(old, "get", reader, fax_id=fid)["fax"]["fax_id"] == fid   # allowed at 149
    f5.upgrade_to_fx5(old)
    applied = sa.admin(old, "SELECT migration_id FROM shared.schema_migrations"
                            " WHERE tenant_id = %s AND state = 'applied'", (old.tenant_id,))
    assert set(f5.FX5_IDS) <= {row[0] for row in applied}
    for name in f5.SUPERSEDED:
        assert not f5.runtime_can_execute(old, name), name
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        sa.call(old, f4.READ_V2, f2.read_cmd("get", reader, fax_id=fid))
    for function, command in _refusals(old, reader, fid):
        fx.refused(old, function, command, "ACTOR_INACTIVE")
    assert f5.read(old, "get", staff, fax_id=fid)["fax"]["fax_id"] == fid
    f5.act(old, "open", fid, staff)
    assert f5.history_actions(old, fid)[-1] == "opened"
