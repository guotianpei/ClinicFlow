"""L-1S S-H H1 — t053 replacement regression: with NO open outbound op, the v2 inbound
handler behaves exactly like the original (Codex review 9e14672 item 1). RED first.

Each scenario runs twice with fresh synthetic data: once on an S-C tenant (target 46,
original m05_register_inbound) and once on an S-H tenant (target 54, t053 body), and the
normalised results must be identical. Normalisation drops generated ids, keeping
decision, triage kind, outcome, which known window a reply attached to, every window's
state and the opt-out flag, so a behaviour change cannot hide behind fresh ids.

Scenarios: confirm / reschedule / cancel / unclear on one window; STOP with two windows;
shared phone (two patients) -> quarantined; two current windows -> unclear; late reply
after the deadline; redelivery -> existing; conflicting redelivery -> INBOUND_CONFLICT;
no reply -> close_window; reply before any window.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import m02_support
import psycopg
import pytest
import sa_support as sa
import sc_support as sc
import sh_support as sh

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

SCENARIOS = ("confirm", "reschedule", "cancel", "unclear", "stop_two_windows",
             "shared_phone", "two_windows", "late_reply", "redelivery", "conflict",
             "no_reply_close", "reply_no_window")


@pytest.fixture(scope="module")
def old_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
                m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "regression:sc")


@pytest.fixture(scope="module")
def new_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
                m05_reset: Any) -> Iterator[sh.SHInstall]:
    yield from sh.module_install(request, m02_ids, m05_reset, "regression:sh")


def _norm(result: dict[str, Any], windows: list[str]) -> tuple[Any, ...]:
    reminder = result.get("reminder_id")
    return (result.get("outcome"), result.get("decision"), result.get("triage_kind"),
            windows.index(reminder) if reminder in windows else reminder)


def _states(install: sa.SAInstall, windows: list[str]) -> list[str]:
    return [sa.window(install, w)["state"] for w in windows]


def _run(install: sa.SAInstall, name: str) -> list[Any]:
    out: list[Any] = []
    if name in ("confirm", "reschedule", "cancel", "unclear"):
        _p, phone, (ref,) = sa.fresh_patient(install)
        windows = [sa.new_id()]
        sa.dispatch(install, windows[0], ref)
        out.append(_norm(sa.inbound(install, sa.new_id(), phone, name), windows))
    elif name in ("stop_two_windows", "two_windows"):
        _p, phone, refs = sa.fresh_patient(install, appointments=2)
        windows = [sa.new_id(), sa.new_id()]
        for w, ref in zip(windows, refs, strict=True):
            sa.dispatch(install, w, ref)
        reply = "stop" if name == "stop_two_windows" else "reschedule"
        out.append(_norm(sa.inbound(install, sa.new_id(), phone, reply), windows))
        contact = sa.contact(install, phone)
        out.append(contact["opted_out"] if contact else None)
    elif name == "shared_phone":
        p1, phone, (ref,) = sa.fresh_patient(install)
        p2 = sa.new_id()
        sa.directory(install, "contact_put", phone=phone, patient_id=p2)
        windows = [sa.new_id()]
        sa.dispatch(install, windows[0], ref)
        out.append(_norm(sa.inbound(install, sa.new_id(), phone, "cancel"), windows))
        assert p1 != p2
    elif name == "late_reply":
        _p, phone, (ref,) = sa.fresh_patient(install)
        windows = [sa.new_id()]
        sa.dispatch(install, windows[0], ref)
        sa.shift_window(install, windows[0], -25)
        out.append(_norm(sa.inbound(install, sa.new_id(), phone, "cancel"), windows))
    elif name in ("redelivery", "conflict"):
        _p, phone, (ref,) = sa.fresh_patient(install)
        windows = [sa.new_id()]
        sa.dispatch(install, windows[0], ref)
        message = sa.new_id()
        out.append(_norm(sa.inbound(install, message, phone, "reschedule"), windows))
        if name == "redelivery":
            out.append(_norm(sa.inbound(install, message, phone, "reschedule"), windows))
        else:
            with pytest.raises(psycopg.Error) as conflict:
                sa.inbound(install, message, phone, "cancel")
            assert conflict.value.diag.message_primary == "INBOUND_CONFLICT"
            out.append("INBOUND_CONFLICT")
    elif name == "no_reply_close":
        _p, _phone, (ref,) = sa.fresh_patient(install)
        windows = [sa.new_id()]
        sa.dispatch(install, windows[0], ref)
        sa.shift_window(install, windows[0], -25)
        closed = sa.close(install, windows[0])
        out.append((closed.get("outcome"), closed.get("decision")))
    elif name == "reply_no_window":
        _p, phone, (_ref,) = sa.fresh_patient(install)
        windows = []
        out.append(_norm(sa.inbound(install, sa.new_id(), phone, "confirm"), windows))
    else:
        raise AssertionError(name)
    out.append(_states(install, windows))
    return out


@pytest.mark.parametrize("name", SCENARIOS)
def test_h1_r01_t053_matches_original_without_open_ops(old_install: sc.SCInstall,
                                                      new_install: sh.SHInstall,
                                                      name: str) -> None:
    old_install.require()
    new_install.require()
    assert _run(new_install, name) == _run(old_install, name)
