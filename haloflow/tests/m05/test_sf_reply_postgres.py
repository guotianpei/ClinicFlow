"""L-1S package S-F — reply adapter on PostgreSQL (S-C TEST runtime, ENFORCE).

Governing: S-F package v1, owner Q-SF1, Codex ALIGN for tests 20261107T160125Z. Every
row begins with `sd.begin` (isolated setup) before its first snapshot; row assertions
are exact S-D count deltas. Synthetic only; the S-A side-effect guard is active; no
provider, no HTTP, nothing sent, no EHR change.

- SF-P01 text -> class -> decision chain (confirm / reschedule / cancel / stop / unclear).
- SF-P02 replay is by class: same id + changed text of the same class -> existing;
         same id + different class -> INBOUND_CONFLICT (both zero deltas).
- SF-P03 the raw text never reaches storage, logs or exception args — on success,
         conflict and refusal (capability, activation).
- SF-P04 the tenant comes only from the context: tenant B's phone through tenant A's
         context -> quarantined in A, zero deltas in B.
- SF-P05 refusal paths with positive controls: wrong capabilities and a revoked clinic
         write nothing; the same message then succeeds once refusal is lifted.
- NC-F1 a 1601-character "STOP..." does not opt out (the opt-out oracle fails).
- NC-F2 "NO" does not record a confirmation (the confirm oracle fails).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterator
from typing import Any

import m02_support
import psycopg
import pytest
import sa_support as sa
import sc_support as sc
import sd_support as sd
import service_support as sv
from psycopg import sql

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("sa_guard")]

MARKER = "ZQX-sf-raw-marker-7731"
OPT_OUT = {"replies": 1, "opted_out": 1, "windows:awaiting": -1, "windows:opted_out": 1}


@pytest.fixture(scope="module")
def sf_install(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
               m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "sf")


@pytest.fixture(scope="module")
def sf_other(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
             m05_reset: Any) -> Iterator[sc.SCInstall]:
    yield from sc.module_install(request, m02_ids, m05_reset, "sf:other")


def _message(phone: str, text: str, message_id: str | None = None) -> Any:
    from haloflow.m05.reply_classifier import InboundMessage

    return InboundMessage(channel="sms", provider_message_id=message_id or sa.new_id(),
                          phone=phone, text=text)


def _adapter(chain: sd.Chain) -> Any:
    from haloflow.m05.reply_classifier import ReplyIntake

    return ReplyIntake(chain.intake)


async def _receive(chain: sd.Chain, message: Any, context: Any = None) -> Any:
    return await _adapter(chain).receive(context or await chain.workload("inbound"), message)


def _expect(result: Any, decision: str, change: dict[str, int],
            expected: dict[str, int]) -> None:
    """The shared decision oracle (NC-F1 / NC-F2 drive it to fail)."""

    assert (result.outcome, result.decision) == ("registered", decision), result
    assert change == expected, change


CHAIN: dict[str, tuple[str, dict[str, int]]] = {
    "yes": ("recorded", sd.CONFIRM),
    " 2 ": ("item", sd.reply_item("reschedule_request")),
    "Cancel": ("item", sd.reply_item("cancel_request")),
    "STOP": ("opt_out", OPT_OUT),
    "maybe tomorrow": ("item", sd.reply_item("unclear_reply")),
}


@pytest.mark.parametrize("text", sorted(CHAIN))
def test_sf_p01_text_to_decision(sf_install: sc.SCInstall, text: str) -> None:
    decision, expected = CHAIN[text]
    sd.begin(sf_install)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            chain = sd.Chain(sf_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref)
            before = sd.counts(sf_install)
            result = await _receive(chain, _message(phone, text))
            _expect(result, decision, sd.delta(before, sd.counts(sf_install)), expected)

    asyncio.run(body())


def test_sf_p02_replay_is_by_class(sf_install: sc.SCInstall) -> None:
    from haloflow.m05.intake import IntakeFailed

    sd.begin(sf_install)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            chain = sd.Chain(sf_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref)
            message_id = sa.new_id()
            first = await _receive(chain, _message(phone, "yes", message_id))
            mark = sd.counts(sf_install)
            same_class = await _receive(chain, _message(phone, " Y ", message_id))
            assert (same_class.outcome, same_class.inbound_id, same_class.decision) == (
                "existing", first.inbound_id, "recorded")
            sd.assert_no_change(sd.delta(mark, sd.counts(sf_install)))
            with pytest.raises(IntakeFailed) as caught:
                await _receive(chain, _message(phone, "2", message_id))
            assert caught.value.code == "INBOUND_CONFLICT"
            sd.assert_no_change(sd.delta(mark, sd.counts(sf_install)))

    asyncio.run(body())


_SCANNED = ("text", "character varying", "character", "json", "jsonb", "bytea")


def _marker_columns(conn: Any, schema: str) -> list[tuple[str, str]]:
    """Every text-like or bytea column of `schema` holding MARKER. Text and JSON are
    matched as text; bytea is matched on the raw bytes (bytea::text would be hex)."""

    rows = conn.execute("SELECT table_name, column_name, data_type FROM"
                        " information_schema.columns WHERE table_schema = %s"
                        " AND data_type = ANY(%s)", (schema, list(_SCANNED))).fetchall()
    found: list[tuple[str, str]] = []
    for table, column, data_type in rows:
        if data_type == "bytea":
            statement = sql.SQL("SELECT count(*) FROM {t} WHERE position(%s IN {c}) > 0")
            param: Any = MARKER.encode()
        else:
            statement = sql.SQL("SELECT count(*) FROM {t} WHERE {c}::text LIKE %s")
            param = f"%{MARKER}%"
        query = statement.format(t=sql.Identifier(schema, table), c=sql.Identifier(column))
        if conn.execute(query, (param,)).fetchone()[0]:
            found.append((table, column))
    return sorted(found)


def _scan(install: sc.SCInstall) -> list[tuple[str, str]]:
    with psycopg.connect(install.ids.admin, autocommit=True) as conn:
        return _marker_columns(conn, install.schema)


def test_sf_p03b_scanner_positive_control(sf_install: sc.SCInstall) -> None:
    """The marker scanner finds the marker in text, jsonb and bytea columns (probe
    table created and rolled back in one transaction; nothing persists)."""

    sd.prepare(sf_install)
    with psycopg.connect(sf_install.ids.admin) as conn:
        probe = sql.Identifier(sf_install.schema, "sf_scan_probe")
        conn.execute(sql.SQL("CREATE TABLE {p} (t text, j jsonb, b bytea, n text)").format(
            p=probe))
        conn.execute(sql.SQL("INSERT INTO {p} VALUES (%s, %s::jsonb, %s, %s)").format(
            p=probe), (f"x {MARKER} y", f'{{"k": "{MARKER}"}}', b"\x00" + MARKER.encode(),
                       "nothing here"))
        assert _marker_columns(conn, sf_install.schema) == [
            ("sf_scan_probe", "b"), ("sf_scan_probe", "j"), ("sf_scan_probe", "t")]
        conn.rollback()
    gone = sa.admin(sf_install, "SELECT count(*) FROM information_schema.tables"
                                " WHERE table_schema = %s AND table_name = 'sf_scan_probe'",
                    (sf_install.schema,))
    assert gone == [(0,)]  # rolled back: nothing persists


def _no_marker(error: BaseException) -> None:
    assert MARKER not in str(error) and MARKER not in repr(error.args), repr(error)


def test_sf_p03_raw_text_never_kept(sf_install: sc.SCInstall,
                                    caplog: pytest.LogCaptureFixture) -> None:
    from haloflow.m05.intake import IntakeFailed, IntakeRefused

    sd.begin(sf_install)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            chain = sd.Chain(sf_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref)
            ok = await _receive(chain, _message(phone, f"call me {MARKER}"))
            assert ok.decision == "item"
            # Conflict: a recognised "yes" first, then a marker-bearing unclear text under
            # the SAME provider id (different class) -> INBOUND_CONFLICT, zero deltas.
            _, phone2, (ref2,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref2)
            seeded_id = sa.new_id()
            seeded = await _receive(chain, _message(phone2, "yes", seeded_id))
            assert seeded.decision == "recorded"
            mark = sd.counts(sf_install)
            with pytest.raises(IntakeFailed) as conflict:
                await _receive(chain, _message(phone2, f"call me {MARKER}", seeded_id))
            assert conflict.value.code == "INBOUND_CONFLICT"
            sd.assert_no_change(sd.delta(mark, sd.counts(sf_install)))
            _no_marker(conflict.value)
            wrong = await sv.context(rt, sf_install, sa.W_INGRESS, sa.CAPS["dispatch"],
                                     kind="workload")
            with pytest.raises(IntakeRefused) as refused:
                await _receive(chain, _message(phone, f"yes {MARKER}"), wrong)
            _no_marker(refused.value)
            sc.change(sf_install, "revoke")
            try:
                with pytest.raises(Exception) as inactive:  # noqa: B017 - type asserted
                    await _receive(chain, _message(phone, f"yes {MARKER}"))
                from haloflow.m01.errors import ActivationRefused

                assert type(inactive.value) is ActivationRefused
                _no_marker(inactive.value)
            finally:
                sc.change(sf_install, "grant")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(body())
    assert all(MARKER not in r.getMessage() for r in caplog.records)
    assert _scan(sf_install) == []


def test_sf_p04_tenant_only_from_context(sf_install: sc.SCInstall,
                                         sf_other: sc.SCInstall) -> None:
    sd.begin(sf_other)
    sd.begin(sf_install)
    _, other_phone, (other_ref,) = sa.fresh_patient(sf_other)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            await sd.Chain(sf_other, rt).dispatch(other_ref)
            chain = sd.Chain(sf_install, rt)
            before_a, before_b = sd.counts(sf_install), sd.counts(sf_other)
            result = await _receive(chain, _message(other_phone, "yes"))
            assert (result.outcome, result.decision, result.event_id, result.item_id) == (
                "registered", "quarantined", None, None)
            assert sd.delta(before_a, sd.counts(sf_install)) == {"replies": 1}
            sd.assert_no_change(sd.delta(before_b, sd.counts(sf_other)))

    asyncio.run(body())


def test_sf_p05_refusals_then_positive_controls(sf_install: sc.SCInstall) -> None:
    from haloflow.m01.errors import ActivationRefused
    from haloflow.m05.intake import IntakeRefused

    sd.begin(sf_install)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            chain = sd.Chain(sf_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref)
            message_id = sa.new_id()
            before: Counter[str] = sd.counts(sf_install)
            wrong = await sv.context(rt, sf_install, sa.W_INGRESS, sa.CAPS["dispatch"],
                                     kind="workload")
            with pytest.raises(IntakeRefused) as refused:
                await _receive(chain, _message(phone, "2", message_id), wrong)
            assert refused.value.code == "CAPABILITIES_INVALID"
            sd.assert_no_change(sd.delta(before, sd.counts(sf_install)))
            sc.change(sf_install, "revoke")
            before = sd.counts(sf_install)
            try:
                with pytest.raises(Exception) as inactive:  # noqa: B017 - type asserted
                    await _receive(chain, _message(phone, "2", message_id))
                assert type(inactive.value) is ActivationRefused
                assert inactive.value.reason_code == "M05_NOT_ACTIVATED"
                sd.assert_no_change(sd.delta(before, sd.counts(sf_install)))
            finally:
                sc.change(sf_install, "grant")
            before = sd.counts(sf_install)
            result = await _receive(chain, _message(phone, "2", message_id))  # positive
            _expect(result, "item", sd.delta(before, sd.counts(sf_install)),
                    sd.reply_item("reschedule_request"))

    asyncio.run(body())


def test_sf_nc1_long_stop_does_not_opt_out(sf_install: sc.SCInstall) -> None:
    sd.begin(sf_install)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            chain = sd.Chain(sf_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref)
            before = sd.counts(sf_install)
            text = "STOP" + " " * 1597
            assert len(text) == 1601
            result = await _receive(chain, _message(phone, text))
            change = sd.delta(before, sd.counts(sf_install))
            with pytest.raises(AssertionError):
                _expect(result, "opt_out", change, OPT_OUT)
            assert (result.decision, change) == ("item", sd.reply_item("unclear_reply"))

    asyncio.run(body())


def test_sf_nc2_no_is_not_a_confirmation(sf_install: sc.SCInstall) -> None:
    sd.begin(sf_install)

    async def body() -> None:
        async with sd.runtime(sf_install) as rt:
            chain = sd.Chain(sf_install, rt)
            _, phone, (ref,) = sa.fresh_patient(sf_install)
            await chain.dispatch(ref)
            before = sd.counts(sf_install)
            result = await _receive(chain, _message(phone, "NO"))
            change = sd.delta(before, sd.counts(sf_install))
            with pytest.raises(AssertionError):
                _expect(result, "recorded", change, sd.CONFIRM)
            assert (result.decision, change) == ("item", sd.reply_item("unclear_reply"))

    asyncio.run(body())
