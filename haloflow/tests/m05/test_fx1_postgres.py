"""Incoming fax routing FX-IN, package FX1 — PostgreSQL 17 rows (RED BY DESIGN).

Governing: see `fx1_support` (requirements v1r1, architecture v1r2, test cases v1 + Codex
65d1f9db clarifications, OD-FX1..FX9). Every row provisions its tenant(s) at target 139 in its
body, so the intended red cause is the missing FX1 source (`include_fx1` -> TypeError, reported as
"FX1 test installation failed"). Rows marked CONTROL use only existing code and pass now.
ZSE = every FX table of the named tenant(s) byte-identical before/after (`fx1_support.snapshot`).

Rows (test-case ids v1; trace in brackets):
- FX1-T01 seven OD-FX1 queues, exact names, active, positions 1..7 [FR-2]
- FX1-T02 rule CHECK / UNIQUE NULLS NOT DISTINCT: (null,null) and a duplicate refused; same key to
          another queue accepted [FR-3, arch 4]
- FX1-T03 fax CHECKs: state/queue coupling, digest length 31/33 [arch 4]
- FX1-T04 ACL: runtime EXECUTE on receive only; support EXECUTE on settings_put only; authority
          owner-only; no runtime/support table privilege; PUBLIC none [arch 5.6, 8.2]
- FX1-R01..R04 levels L1 / L2 / L3 / sender hidden -> L3 [FR-4, OD-FX5]
- FX1-R05 OFF-SCHEMA robustness probe (Codex): UNIQUE dropped in the fixture, two identical L2 rules
          to one queue -> routed ONCE (kills M-FX1-2, count rows vs destinations) [FR-5]
- FX1-R06 conflict at the deciding level -> one sorting item rules_conflict; lower levels ignored
- FX1-R07 conflict only at a lower level -> routed by the deciding level [FR-5]
- FX1-R08 / R09 no match: no_matching_rule / sender_not_shown [FR-6, OD-FX7]
- FX1-R10 sender hidden with an L3 rule -> routed (not sorting) [FR-6]
- FX1-R11 the only matching rule points to an inactive queue -> no_matching_rule [FR-3]
- FX1-R12 no silent General: no rule, General active -> sorting, never General [FR-6, OD-FX4]
- FX1-D01 identical redelivery -> duplicate, ZSE [FR-7]
- FX1-D02 concurrent identical deliveries, KNOWN NO-MATCH: the loser is observed blocked; exactly
          one fax, exactly one OPEN sorting item, one history row; loser duplicate (Codex) [FR-7]
- FX1-D02b the same with a matching rule: one routed fax, no sorting item, one history row
- FX1-D03 concurrent same id, different pages -> one row; loser duplicate_mismatch; one
          discrepancy row [FR-7]
- FX1-D04 each immutable field changed -> duplicate_mismatch; original unchanged; `fields` = that
          name only; no values stored [FR-7, FR-11]
- FX1-D05 two mismatches on one fax, the second observed blocked on the fax row -> seq 1, 2
- FX1-D06 same provider id in tenants A and B -> both accepted [FR-7, FR-10]
- FX1-D07 another provider, same provider_fax_id -> two rows [FR-7]
- FX1-P01 SQL meta_digest equals `fax_rules.meta_digest` (parity) [arch 4]
- FX1-X01 fault after the fax insert (fixture trigger on history) -> rolled back, ZSE; retry
          routes normally [arch 5.4]
- FX1-X02 a rule added after routing never re-routes the fax [arch 5.4]
- FX1-X03 delivery while an uncommitted rule insert is open: not blocked, uses committed rules
          only [arch 5.5]
- FX1-F01 (Codex 19629e35, green-time contention evidence) an EXCLUSIVE K_FX_LINES xact lock is
          held: the receipt is OBSERVED blocked on the advisory lock, then completes after release
          [test cases v1r1 §0]
- FX1-F02 a receipt transaction is held open: a try of the EXCLUSIVE K_FX_LINES lock fails; it
          succeeds after the receipt commits [test cases v1r1 §0]
- FX1-L01 line not registered -> LINE_NOT_REGISTERED, ZSE [FR-1]
- FX1-W01 workload not allow-listed / actor principal -> WORKLOAD_NOT_AUTHORIZED, ZSE [arch 5.6]
- FX1-M01 malformed commands (each field rule) -> COMMAND_MALFORMED, ZSE
- FX1-S01 POSITIVE CONTROL of the support path: queue_put, queue_deactivate, rule_put, rule_remove
          applied with one audit row each [OD-FX3]
- FX1-S02 support refusals: runtime 42501; not allow-listed, no grant, other tenant, expired,
          revoked, wrong capability -> SUPPORT_NOT_AUTHORIZED; ZSE [arch 5.3]
- FX1-S04 grant REVOKED while the support op is OBSERVED blocked on the queue lock ->
          SUPPORT_NOT_AUTHORIZED after the lock, ZSE (queue_deactivate and rule_put) [arch 5.3]
- FX1-S05 positive control of S04 (no revoke): applied after the lock [arch 5.3]
- FX1-S06 support deactivation keeps routed faxes; new routing ignores the queue [FR-2]
- FX1-S07 queue_put name equal ignoring case -> QUEUE_NAME_TAKEN [FR-2]
- FX1-S08 settings_put refusals: unknown/foreign queue, unknown rule, unregistered line,
          duplicate rule, no sender and no line [FR-3, FR-10]
- FX1-S09 (Codex REVISE 7859c910; OD-FX1 rename) support queue_rename keeps the queue's ID: a
          default queue with an existing rule and routed fax is renamed; the rule and fax still
          reference it; a new receipt routes there; one attributed audit row
- FX1-S10 queue_rename refusals: another queue's name ignoring case -> QUEUE_NAME_TAKEN; unknown,
          inactive or tenant-B queue -> QUEUE_NOT_FOUND (A and B ZSE); no grant ->
          SUPPORT_NOT_AUTHORIZED; runtime -> 42501; malformed -> COMMAND_MALFORMED
- FX1-C00 CONTROL: an existing eligibility tenant (E3b, target 126) has no FX table.

Mutant killers (wired at green): M-FX1-1 level order -> R01/R02; -2 count rows -> R05; -3 dedup key
drops provider -> D07; -4 mismatch ignores sender -> D04[sender]; -5 authority accepts any
capability -> S02[cap]; -6 post-lock re-check removed -> S04; -7 inactive filter removed -> R11;
-8 receipt fence removed -> F01/F02; -9 rename re-check after lock removed -> S04[queue_rename];
-10 rename collision check case-sensitive -> S10.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import fx1_support as fx
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
            m05_reset: Any) -> Iterator[fx.FX1Install]:
    yield from fx.module_install(request, m02_ids, m05_reset, f"fx1:{request.node.name}")


@pytest.fixture
def install_b(request: pytest.FixtureRequest, m02_ids: m02_support.Identities,
              m05_reset: Any) -> Iterator[fx.FX1Install]:
    yield from fx.module_install(request, m02_ids, m05_reset, f"fx1b:{request.node.name}")


@pytest.fixture(scope="module")
def env(migrated_database: str) -> Iterator[db.L1WEnv]:
    with db.l1w_environment(migrated_database) as handle:
        yield handle


def _ready(install: fx.FX1Install) -> fx.FX1Install:
    install.require()
    fx.seed_line(install, fx.LINE1)
    fx.seed_line(install, fx.LINE2)
    return install


def _history(install: sa.SAInstall, fax_id: str) -> list[dict[str, Any]]:
    return sorted(fx.rows(install, "m05_fx_history", "fax_id = %s::uuid", (fax_id,)),
                  key=lambda r: int(r["seq"]))


def _sorting(install: sa.SAInstall, fax_id: str) -> list[dict[str, Any]]:
    return fx.rows(install, "m05_fx_sorting", "fax_id = %s::uuid", (fax_id,))


def _assert_routed(install: sa.SAInstall, result: dict[str, Any], queue: str,
                   level: int) -> None:
    assert result["schema_version"] == 1
    assert (result["outcome"], result["queue_id"], result["level"], result["reason"]) == (
        "routed", fx.queue_id(install, queue), level, None), result
    row = fx.fax(install, str(result["fax_id"]))
    assert (row["state"], row["queue_id"], row["routed_by"], row["level"]) == (
        "routed", fx.queue_id(install, queue), "rule", level)
    assert _sorting(install, str(result["fax_id"])) == []
    history = _history(install, str(result["fax_id"]))
    assert [(h["seq"], h["action"], h["actor"], h["to_queue_id"]) for h in history] == [
        (1, "received_routed", None, fx.queue_id(install, queue))]


def _assert_sorting(install: sa.SAInstall, result: dict[str, Any], reason: str) -> None:
    assert (result["outcome"], result["queue_id"], result["level"], result["reason"]) == (
        "needs_sorting", None, None, reason), result
    fid = str(result["fax_id"])
    row = fx.fax(install, fid)
    assert (row["state"], row["queue_id"], row["routed_by"]) == ("needs_sorting", None, None)
    sorting = _sorting(install, fid)
    assert [(s["reason"], s["state"]) for s in sorting] == [(reason, "open")]
    assert [h["action"] for h in _history(install, fid)] == ["received_needs_sorting"]


# --- T: install ----------------------------------------------------------------------------


def test_fx1_c00_eligibility_tenant_has_no_fx_tables(request: pytest.FixtureRequest,
                                                     m02_ids: m02_support.Identities,
                                                     m05_reset: Any) -> None:
    """C: an existing eligibility tenant (E3b, 126) has no FX object."""
    import el3b_support as e3b

    for install in e3b.module_install(request, m02_ids, m05_reset, "fx1:c00"):
        install.require()
        found = sa.admin(install, "SELECT count(*) FROM pg_class c JOIN pg_namespace n"
                                  " ON n.oid = c.relnamespace WHERE n.nspname = %s"
                                  " AND c.relname LIKE 'm05\\_fx\\_%%'", (install.schema,))
        assert found[0][0] == 0


def test_fx1_t01_default_queues(install: fx.FX1Install) -> None:
    """A."""
    install.require()
    queues = sorted(fx.rows(install, "m05_fx_queues"), key=lambda q: int(q["position"]))
    assert [(q["name"], q["active"], q["position"]) for q in queues] == [
        (name, True, i) for i, name in enumerate(fx.DEFAULT_QUEUES, start=1)]


def test_fx1_t02_rule_constraints(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    with pytest.raises(psycopg.errors.CheckViolation):
        fx.seed_rule(install, sender=None, line=None, queue="Imaging")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    with pytest.raises(psycopg.errors.UniqueViolation):  # NULLS NOT DISTINCT
        fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="General")  # another queue: allowed
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # line must be registered
        fx.seed_rule(install, sender=None, line="+15405559999", queue="General")


@pytest.mark.parametrize("violation", ["sorting-with-queue", "routed-without-queue",
                                       "digest-31", "digest-33"])
def test_fx1_t03_fax_checks(install: fx.FX1Install, violation: str) -> None:
    """A."""
    _ready(install)
    queue = fx.queue_id(install, "General")
    state, qid, digest_len = {
        "sorting-with-queue": ("needs_sorting", queue, 32),
        "routed-without-queue": ("routed", None, 32),
        "digest-31": ("routed", queue, 31),
        "digest-33": ("routed", queue, 33),
    }[violation]
    with pytest.raises(psycopg.errors.CheckViolation):
        sa.admin(install, sa.q(install, (
            "INSERT INTO {s}.m05_fx_faxes (fax_id, provider, provider_fax_id, line_e164,"
            " sender_e164, pages, received_at, doc_ref, meta_digest, state, queue_id,"
            " routed_by, level, version) VALUES (gen_random_uuid(), 'simfax', 'X', %s, NULL, 1,"
            " clock_timestamp(), 'syn-doc-x', %s, %s, %s::uuid, %s, %s, 1)")),
            (fx.LINE1, b"\x00" * digest_len, state, qid,
             None if state == "needs_sorting" else "rule", None if state == "needs_sorting" else 3))


def test_fx1_t04_acl(install: fx.FX1Install) -> None:
    """A."""
    import si_support as si

    install.require()
    assert si.execute_privilege(install, fx.RECEIVE, sa.RUNTIME)
    assert not si.execute_privilege(install, fx.SETTINGS_PUT, sa.RUNTIME)
    assert not si.execute_privilege(install, fx.AUTHORITY, sa.RUNTIME)
    assert si.execute_privilege(install, fx.SETTINGS_PUT, fx.SUPPORT_ROLE)
    assert not si.execute_privilege(install, fx.RECEIVE, fx.SUPPORT_ROLE)
    assert not si.execute_privilege(install, fx.AUTHORITY, fx.SUPPORT_ROLE)
    assert si.execute_privilege(install, fx.AUTHORITY, sa.M05_OWNER)
    for function in (fx.RECEIVE, fx.SETTINGS_PUT, fx.AUTHORITY):
        assert not si.execute_privilege(install, function, "public"), function
    for table in fx.FX_TABLES:
        for role in (sa.RUNTIME, fx.SUPPORT_ROLE):
            granted = sa.admin(install, "SELECT has_table_privilege(%s, %s,"
                                        " 'SELECT,INSERT,UPDATE,DELETE')",
                               (role, f"{install.schema}.{table}"))[0][0]
            assert granted is False, (role, table)


# --- R: routing ----------------------------------------------------------------------------


def test_fx1_r01_level1(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=fx.LINE1, queue="Labs/Results")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    _assert_routed(install, fx.receive(install, fx.fax_cmd()), "Labs/Results", 1)


def test_fx1_r02_level2(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=fx.LINE2, queue="Labs/Results")  # other line
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    _assert_routed(install, fx.receive(install, fx.fax_cmd()), "Imaging", 2)


def test_fx1_r03_level3(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER2, line=None, queue="Imaging")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    _assert_routed(install, fx.receive(install, fx.fax_cmd()), "General", 3)


def test_fx1_r04_sender_hidden_uses_level3(install: fx.FX1Install) -> None:
    """A (OD-FX5)."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=fx.LINE1, queue="Labs/Results")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    _assert_routed(install, fx.receive(install, fx.fax_cmd(sender=None)), "General", 3)


def test_fx1_r05_offschema_same_queue_routes_once(install: fx.FX1Install) -> None:
    """A. OFF-SCHEMA probe: the installed UNIQUE forbids identical rules (T02); the fixture drops
    it so two identical L2 rules point to ONE queue. Distinct destinations = 1 -> routed once."""
    _ready(install)
    constraint = sa.admin(install, (
        "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = %s"
        " AND t.relname = 'm05_fx_rules' AND c.contype = 'u'"), (install.schema,))
    assert len(constraint) == 1, constraint
    sa.admin(install, sa.q(install, "ALTER TABLE {s}.m05_fx_rules DROP CONSTRAINT "
                                    + str(constraint[0][0])))
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    _assert_routed(install, fx.receive(install, fx.fax_cmd()), "Imaging", 2)


def test_fx1_r06_conflict_at_deciding_level(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Labs/Results")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    _assert_sorting(install, fx.receive(install, fx.fax_cmd()), "rules_conflict")


def test_fx1_r07_lower_conflict_irrelevant(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Labs/Results")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="General")
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="Imaging")
    _assert_routed(install, fx.receive(install, fx.fax_cmd()), "Labs/Results", 2)


def test_fx1_r08_no_match(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER2, line=None, queue="Imaging")
    _assert_sorting(install, fx.receive(install, fx.fax_cmd()), "no_matching_rule")


def test_fx1_r09_no_match_sender_hidden(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    _assert_sorting(install, fx.receive(install, fx.fax_cmd(sender=None)), "sender_not_shown")


def test_fx1_r10_sender_hidden_line_rule(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=None, line=fx.LINE1, queue="Pharmacy/Refills")
    _assert_routed(install, fx.receive(install, fx.fax_cmd(sender=None)), "Pharmacy/Refills", 3)


def test_fx1_r11_inactive_queue_ignored(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    fx.deactivate_raw(install, "Imaging")
    _assert_sorting(install, fx.receive(install, fx.fax_cmd()), "no_matching_rule")


def test_fx1_r12_no_silent_general(install: fx.FX1Install) -> None:
    """A (OD-FX4)."""
    _ready(install)
    result = fx.receive(install, fx.fax_cmd())
    _assert_sorting(install, result, "no_matching_rule")
    assert fx.rows(install, "m05_fx_faxes", "queue_id IS NOT NULL") == []


# --- D: deduplication -----------------------------------------------------------------------


def test_fx1_d01_identical_redelivery(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    command = fx.fax_cmd()
    first = fx.receive(install, command)
    before = fx.snapshot(install)
    again = fx.receive(install, command)
    assert (again["outcome"], again["fax_id"]) == ("duplicate", first["fax_id"])
    assert fx.snapshot(install) == before


@pytest.mark.parametrize("matching", [False, True], ids=["no-match", "routed"])
def test_fx1_d02_concurrent_identical(install: fx.FX1Install, matching: bool) -> None:
    """A. The loser is OBSERVED blocked on the unique index before the winner commits."""
    _ready(install)
    if matching:
        fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    command = fx.fax_cmd()
    with sa.held(install, fx.RECEIVE, command) as won:
        loser = sa.Waiter(install, fx.RECEIVE, command).start_and_block()
    lost = loser.join()
    assert lost["outcome"] == "duplicate" and lost["fax_id"] == won["fax_id"]
    assert len(fx.rows(install, "m05_fx_faxes")) == 1
    assert len(fx.rows(install, "m05_fx_history")) == 1
    if matching:
        assert won["outcome"] == "routed"
        assert fx.rows(install, "m05_fx_sorting") == []
    else:
        assert won["outcome"] == "needs_sorting"
        assert [s["state"] for s in fx.rows(install, "m05_fx_sorting")] == ["open"]


def test_fx1_d03_concurrent_mismatch(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    command = fx.fax_cmd(pages=2)
    with sa.held(install, fx.RECEIVE, command) as won:
        loser = sa.Waiter(install, fx.RECEIVE, {**command, "pages": 3}).start_and_block()
    lost = loser.join()
    assert (lost["outcome"], lost["fax_id"]) == ("duplicate_mismatch", won["fax_id"])
    assert fx.fax(install, str(won["fax_id"]))["pages"] == 2
    discrepancies = fx.rows(install, "m05_fx_discrepancies")
    assert [(d["seq"], d["fields"]) for d in discrepancies] == [(1, ["pages"])]


@pytest.mark.parametrize(("field", "value"), [
    ("line", fx.LINE2), ("sender", fx.SENDER2), ("sender", None), ("pages", 9),
    ("received_at", "2026-11-25T14:00:00.000000Z"), ("doc_ref", "syn-doc-other"),
], ids=["line", "sender", "sender-null", "pages", "received_at", "doc_ref"])
def test_fx1_d04_each_field_mismatch(install: fx.FX1Install, field: str, value: Any) -> None:
    """A."""
    _ready(install)
    command = fx.fax_cmd()
    first = fx.receive(install, command)
    original = fx.fax(install, str(first["fax_id"]))
    result = fx.receive(install, {**command, field: value})
    assert (result["outcome"], result["fax_id"]) == ("duplicate_mismatch", first["fax_id"])
    assert fx.fax(install, str(first["fax_id"])) == original
    (row,) = fx.rows(install, "m05_fx_discrepancies")
    expected = "sender" if field == "sender" else field
    assert row["fields"] == [expected]
    stored = str(row)
    if isinstance(value, str):
        assert value not in stored, "a discrepancy row stored a value"


def test_fx1_d05_concurrent_mismatches_serialised(install: fx.FX1Install) -> None:
    """A. The second mismatch is OBSERVED blocked on the existing fax row."""
    _ready(install)
    command = fx.fax_cmd()
    first = fx.receive(install, command)
    with sa.held(install, fx.RECEIVE, {**command, "pages": 7}):
        loser = sa.Waiter(install, fx.RECEIVE, {**command, "pages": 8}).start_and_block()
    assert loser.join()["outcome"] == "duplicate_mismatch"
    seqs = sorted(int(d["seq"]) for d in fx.rows(install, "m05_fx_discrepancies",
                                                 "fax_id = %s::uuid", (first["fax_id"],)))
    assert seqs == [1, 2]


def test_fx1_d06_same_id_two_tenants(install: fx.FX1Install, install_b: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    _ready(install_b)
    command = fx.fax_cmd()
    a = fx.receive(install, command)
    b = fx.receive(install_b, command)
    assert a["outcome"] == b["outcome"] == "needs_sorting"
    assert len(fx.rows(install, "m05_fx_faxes")) == len(fx.rows(install_b, "m05_fx_faxes")) == 1


def test_fx1_d07_other_provider_same_id(install: fx.FX1Install) -> None:
    """A (kills M-FX1-3)."""
    _ready(install)
    command = fx.fax_cmd()
    first = fx.receive(install, command)
    other = fx.receive(install, {**command, "provider": "simfax_b"})
    assert other["outcome"] == "needs_sorting" and other["fax_id"] != first["fax_id"]
    assert len(fx.rows(install, "m05_fx_faxes")) == 2


def test_fx1_p01_digest_parity(install: fx.FX1Install) -> None:
    """A."""
    from haloflow.m05 import fax_rules

    _ready(install)
    for sender in (fx.SENDER, None):
        command = fx.fax_cmd(sender=sender)
        result = fx.receive(install, command)
        stored = sa.admin(install, sa.q(install, "SELECT meta_digest FROM {s}.m05_fx_faxes"
                                                 " WHERE fax_id = %s::uuid"), (result["fax_id"],))
        assert bytes(stored[0][0]) == fax_rules.meta_digest(
            line=command["line"], sender=sender, pages=command["pages"],
            received_at=command["received_at"], doc_ref=command["doc_ref"])


# --- X: transaction boundary and races ------------------------------------------------------


def test_fx1_x01_atomic_receipt(install: fx.FX1Install) -> None:
    """A. A fixture trigger fails the FIRST history insert: nothing of the fax survives."""
    _ready(install)
    sa.admin(install, sa.q(install, (
        "CREATE FUNCTION {s}.fx1_fail() RETURNS trigger LANGUAGE plpgsql AS"
        " $$BEGIN RAISE EXCEPTION 'fx1 injected fault'; END$$;"
        " CREATE TRIGGER fx1_fail BEFORE INSERT ON {s}.m05_fx_history"
        " FOR EACH ROW EXECUTE FUNCTION {s}.fx1_fail();")))
    command = fx.fax_cmd()
    before = fx.snapshot(install)
    with pytest.raises(psycopg.Error, match="injected fault"):
        fx.receive(install, command)
    assert fx.snapshot(install) == before
    sa.admin(install, sa.q(install, "DROP TRIGGER fx1_fail ON {s}.m05_fx_history"))
    assert fx.receive(install, command)["outcome"] == "needs_sorting"  # the retry is fresh


def test_fx1_x02_rule_change_never_reroutes(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    result = fx.receive(install, fx.fax_cmd())
    before = fx.fax(install, str(result["fax_id"]))
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    assert fx.fax(install, str(result["fax_id"])) == before


def test_fx1_x03_uncommitted_rule_not_seen(install: fx.FX1Install) -> None:
    """A. An open (uncommitted) rule insert neither blocks the delivery nor routes it."""
    _ready(install)
    with psycopg.connect(install.ids.admin) as admin, admin.transaction():
        admin.execute(sa.q(install, "INSERT INTO {s}.m05_fx_rules (rule_id, sender_e164,"
                                    " line_e164, queue_id) SELECT gen_random_uuid(), %s, NULL,"
                                    " queue_id FROM {s}.m05_fx_queues WHERE name = 'Imaging'"),
                      (fx.SENDER,))
        with sa.runtime_conn(install) as conn:
            conn.execute("SET lock_timeout = '5s'")  # a blocking delivery fails, never hangs
            with conn.transaction():
                result = sa.call_in(conn, install, fx.RECEIVE, fx.fax_cmd())
    assert result["outcome"] == "needs_sorting"
    _assert_routed(install, fx.receive(install, fx.fax_cmd()), "Imaging", 2)


# --- F: the receipt's line fence (test cases v1r1 §0) ---------------------------------------


def _try_exclusive(install: sa.SAInstall) -> bool:
    from haloflow.m05.fax_rules import K_FX_LINES

    return bool(sa.admin(install, "SELECT pg_try_advisory_xact_lock(%s)", (K_FX_LINES,))[0][0])


def test_fx1_f01_receipt_waits_for_exclusive_fence(install: fx.FX1Install) -> None:
    """Green-time contention evidence: the receipt takes the shared fence before its line check."""
    from haloflow.m05.fax_rules import K_FX_LINES

    _ready(install)
    with psycopg.connect(install.ids.admin) as admin, admin.transaction():
        admin.execute("SELECT pg_advisory_xact_lock(%s)", (K_FX_LINES,))
        waiter = sa.Waiter(install, fx.RECEIVE, fx.fax_cmd()).start_and_block()
        assert fx.rows(install, "m05_fx_faxes") == []
    assert waiter.join()["outcome"] == "needs_sorting"


def test_fx1_f02_open_receipt_holds_the_fence(install: fx.FX1Install) -> None:
    """An admitted receipt holds the shared fence to commit."""
    _ready(install)
    with sa.held(install, fx.RECEIVE, fx.fax_cmd()) as result:
        assert result["outcome"] == "needs_sorting"
        assert _try_exclusive(install) is False
    assert _try_exclusive(install) is True


# --- L / W / M: refusals --------------------------------------------------------------------


def test_fx1_l01_line_not_registered(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.refused(install, fx.RECEIVE, fx.fax_cmd(line="+15405559999"), "LINE_NOT_REGISTERED")


def test_fx1_w01_workload(install: fx.FX1Install) -> None:
    """A."""
    _ready(install)
    fx.refused(install, fx.RECEIVE, fx.fax_cmd(principal=sa.W_INGRESS),
               "WORKLOAD_NOT_AUTHORIZED")
    actor = fx.fax_cmd()  # a well-formed attribution whose principal is an actor, not a workload
    actor["attribution"] = sa.attribution(fx.W_FAX, kind="actor")
    fx.refused(install, fx.RECEIVE, actor, "WORKLOAD_NOT_AUTHORIZED")


MALFORMED: list[tuple[str, Any]] = [
    ("schema_version", 2), ("provider", "SimFax"), ("provider", ""), ("provider_fax_id", ""),
    ("provider_fax_id", "x" * 129), ("provider_fax_id", "has space"), ("line", "5405550101"),
    ("line", "+1540555010a"), ("sender", "8045550199"), ("sender", ""), ("pages", 0),
    ("pages", 1000), ("pages", "2"), ("received_at", "2026-11-25 13:00:00"),
    ("doc_ref", "doc-1"), ("doc_ref", "syn-doc-" + "a" * 65), ("unknown_field", "x"),
]


@pytest.mark.parametrize(("field", "value"), MALFORMED,
                         ids=[f"{f}-{i}" for i, (f, _v) in enumerate(MALFORMED)])
def test_fx1_m01_malformed(install: fx.FX1Install, field: str, value: Any) -> None:
    """A."""
    _ready(install)
    fx.refused(install, fx.RECEIVE, {**fx.fax_cmd(), field: value}, "COMMAND_MALFORMED")


@pytest.mark.parametrize("missing", ["provider", "provider_fax_id", "line", "sender", "pages",
                                     "received_at", "doc_ref", "attribution"])
def test_fx1_m02_missing_field(install: fx.FX1Install, missing: str) -> None:
    """A (sender must be PRESENT, null allowed)."""
    _ready(install)
    command = fx.fax_cmd()
    del command[missing]
    fx.refused(install, fx.RECEIVE, command, "COMMAND_MALFORMED")


# --- S: support path ------------------------------------------------------------------------


def _support(env: db.L1WEnv, install: sa.SAInstall, key: str, **grant: Any) -> Any:
    caps = grant.pop("caps", (fx.SETTINGS_CAP,))
    return sh2.support_login(env, install, key, caps=caps, **grant)


def _audit(install: sa.SAInstall) -> list[dict[str, Any]]:
    return fx.rows(install, "m05_fx_settings_audit")


def test_fx1_s01_support_positive_control(install: fx.FX1Install, env: db.L1WEnv) -> None:
    """A."""
    _ready(install)
    with _support(env, install, "fx1-s01") as support:
        made = fx.call_as(support, install, fx.SETTINGS_PUT,
                          fx.settings_cmd("queue_put", name="Hospital discharges", position=8))
        assert made["outcome"] == "applied"
        rule = fx.call_as(support, install, fx.SETTINGS_PUT, fx.settings_cmd(
            "rule_put", sender=fx.SENDER, line=None, queue_id=made["queue_id"]))
        assert rule["outcome"] == "applied"
        _assert_routed(install, fx.receive(install, fx.fax_cmd()), "Hospital discharges", 2)
        assert fx.call_as(support, install, fx.SETTINGS_PUT, fx.settings_cmd(
            "rule_remove", rule_id=rule["rule_id"]))["outcome"] == "applied"
        assert fx.call_as(support, install, fx.SETTINGS_PUT, fx.settings_cmd(
            "queue_deactivate", queue_id=made["queue_id"]))["outcome"] == "applied"
    audit = sorted(_audit(install), key=lambda r: str(r["at"]))
    assert [a["op"] for a in audit] == ["queue_put", "rule_put", "rule_remove",
                                        "queue_deactivate"]
    assert all(a["actor_id"] and a["grant_id"] for a in audit)


def test_fx1_s02_support_refusals(install: fx.FX1Install, install_b: fx.FX1Install,
                                  env: db.L1WEnv) -> None:
    """A (cap row kills M-FX1-5)."""
    _ready(install)
    install_b.require()
    command = fx.settings_cmd("queue_put", name="Never", position=9)
    before = fx.snapshot(install)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        sa.call(install, fx.SETTINGS_PUT, command)  # the runtime role
    assert fx.snapshot(install) == before
    env.login("fx1-s02-unlisted", db.SUPPORT)
    fx.refused(install, fx.SETTINGS_PUT, command, "SUPPORT_NOT_AUTHORIZED",
               conninfo=env.conninfo("fx1-s02-unlisted"))
    cases: list[tuple[str, sa.SAInstall, dict[str, Any]]] = [
        ("nogrant", install, {"caps": None}),
        ("other", install_b, {}),
        ("expired", install, {"expires": timedelta(seconds=-1)}),
        ("revoked", install, {"state": "revoked"}),
        ("cap", install, {"caps": ("m05.reminder.settings",)}),
    ]
    for key, granted_on, grant in cases:
        with _support(env, granted_on, f"fx1-s02-{key}", **grant) as conninfo:
            fx.refused(install, fx.SETTINGS_PUT, command, "SUPPORT_NOT_AUTHORIZED",
                       conninfo=conninfo)


def _revoke(install: sa.SAInstall) -> None:
    sa.admin(install, "UPDATE shared.support_access_grants SET state = 'revoked'"
                      " WHERE tenant_id = %s AND %s = ANY (capabilities)",
             (install.tenant_id, fx.SETTINGS_CAP))


@pytest.mark.parametrize("op", ["queue_deactivate", "rule_put", "queue_rename"])
@pytest.mark.parametrize("revoke", [True, False], ids=["s04-revoked", "s05-control"])
def test_fx1_s04_s05_authority_after_lock(install: fx.FX1Install, env: db.L1WEnv, op: str,
                                          revoke: bool) -> None:
    """A (S04 kills M-FX1-6). The support call is OBSERVED blocked on the queue row lock before
    the grant is revoked (S04) or not (S05 positive control); the lock is then released."""
    _ready(install)
    imaging = fx.queue_id(install, "Imaging")
    command = {
        "queue_deactivate": fx.settings_cmd("queue_deactivate", queue_id=imaging),
        "rule_put": fx.settings_cmd("rule_put", sender=fx.SENDER, line=None, queue_id=imaging),
        "queue_rename": fx.settings_cmd("queue_rename", queue_id=imaging, name="Imaging & X-ray"),
    }[op]
    with _support(env, install, f"fx1-s04-{op}-{revoke}") as support:
        before = fx.snapshot(install)
        with fx.row_locked(install, "m05_fx_queues", "queue_id = %s::uuid", (imaging,)):
            waiter = fx.LoginWaiter(install, support, fx.SETTINGS_PUT, command).start_and_block()
            if revoke:
                _revoke(install)
        if revoke:
            with pytest.raises(psycopg.Error) as late:
                waiter.join()
            assert fx.code_of(late.value) == "SUPPORT_NOT_AUTHORIZED"
            assert fx.snapshot(install) == before
        else:
            assert waiter.join()["outcome"] == "applied"
            assert [a["op"] for a in _audit(install)] == [op]


def test_fx1_s06_deactivation_keeps_faxes(install: fx.FX1Install, env: db.L1WEnv) -> None:
    """A."""
    _ready(install)
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    routed = fx.receive(install, fx.fax_cmd())
    with _support(env, install, "fx1-s06") as support:
        fx.call_as(support, install, fx.SETTINGS_PUT, fx.settings_cmd(
            "queue_deactivate", queue_id=fx.queue_id(install, "Imaging")))
    assert fx.fax(install, str(routed["fax_id"]))["queue_id"] == fx.queue_id(install, "Imaging")
    _assert_sorting(install, fx.receive(install, fx.fax_cmd()), "no_matching_rule")


def test_fx1_s07_queue_name_case(install: fx.FX1Install, env: db.L1WEnv) -> None:
    """A."""
    _ready(install)
    with _support(env, install, "fx1-s07") as support:
        fx.refused(install, fx.SETTINGS_PUT,
                   fx.settings_cmd("queue_put", name="imaging", position=8),
                   "QUEUE_NAME_TAKEN", conninfo=support)


def test_fx1_s08_settings_refusals(install: fx.FX1Install, install_b: fx.FX1Install,
                                   env: db.L1WEnv) -> None:
    """A (foreign queue: FR-10)."""
    _ready(install)
    _ready(install_b)
    foreign = fx.queue_id(install_b, "Imaging")
    imaging = fx.queue_id(install, "Imaging")
    fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    with _support(env, install, "fx1-s08") as support:
        b_before = fx.snapshot(install_b)
        for command, code in [
            (fx.settings_cmd("queue_deactivate", queue_id=foreign), "QUEUE_NOT_FOUND"),
            (fx.settings_cmd("rule_put", sender=fx.SENDER2, line=None, queue_id=foreign),
             "QUEUE_NOT_FOUND"),
            (fx.settings_cmd("rule_remove", rule_id=sa.new_id()), "RULE_NOT_FOUND"),
            (fx.settings_cmd("rule_put", sender=None, line="+15405559999", queue_id=imaging),
             "LINE_NOT_REGISTERED"),
            (fx.settings_cmd("rule_put", sender=fx.SENDER, line=None, queue_id=imaging),
             "RULE_DUPLICATE"),
            (fx.settings_cmd("rule_put", sender=None, line=None, queue_id=imaging),
             "COMMAND_MALFORMED"),
            (fx.settings_cmd("queue_put", name="", position=8), "COMMAND_MALFORMED"),
            (fx.settings_cmd("unknown_op"), "COMMAND_MALFORMED"),
        ]:
            fx.refused(install, fx.SETTINGS_PUT, command, code, conninfo=support)
        assert fx.snapshot(install_b) == b_before


def test_fx1_s09_queue_rename_keeps_identity(install: fx.FX1Install, env: db.L1WEnv) -> None:
    """A (OD-FX1 rename; Codex REVISE 7859c910)."""
    _ready(install)
    imaging = fx.queue_id(install, "Imaging")
    rule = fx.seed_rule(install, sender=fx.SENDER, line=None, queue="Imaging")
    routed = fx.receive(install, fx.fax_cmd())
    with _support(env, install, "fx1-s09") as support:
        result = fx.call_as(support, install, fx.SETTINGS_PUT, fx.settings_cmd(
            "queue_rename", queue_id=imaging, name="Imaging & X-ray"))
    assert (result["outcome"], result["queue_id"]) == ("applied", imaging)
    (queue,) = fx.rows(install, "m05_fx_queues", "queue_id = %s::uuid", (imaging,))
    assert (queue["name"], queue["active"], queue["position"]) == ("Imaging & X-ray", True, 2)
    assert fx.rows(install, "m05_fx_queues", "name = 'Imaging'") == []
    (rule_row,) = fx.rows(install, "m05_fx_rules", "rule_id = %s::uuid", (rule,))
    assert rule_row["queue_id"] == imaging
    assert fx.fax(install, str(routed["fax_id"]))["queue_id"] == imaging
    again = fx.receive(install, fx.fax_cmd())
    assert (again["outcome"], again["queue_id"]) == ("routed", imaging)
    (audit,) = _audit(install)
    assert (audit["op"], audit["target_id"]) == ("queue_rename", imaging)
    assert audit["actor_id"] and audit["grant_id"]


def test_fx1_s10_queue_rename_refusals(install: fx.FX1Install, install_b: fx.FX1Install,
                                       env: db.L1WEnv) -> None:
    """A (kills M-FX1-10 through the case-insensitive collision)."""
    _ready(install)
    _ready(install_b)
    imaging = fx.queue_id(install, "Imaging")
    fx.deactivate_raw(install, "General")
    b_before = fx.snapshot(install_b)
    with _support(env, install, "fx1-s10") as support:
        for command, code in [
            (fx.settings_cmd("queue_rename", queue_id=imaging, name="labs/results"),
             "QUEUE_NAME_TAKEN"),
            (fx.settings_cmd("queue_rename", queue_id=sa.new_id(), name="Elsewhere"),
             "QUEUE_NOT_FOUND"),
            (fx.settings_cmd("queue_rename", queue_id=fx.queue_id(install, "General"),
                             name="Elsewhere"), "QUEUE_NOT_FOUND"),
            (fx.settings_cmd("queue_rename", queue_id=fx.queue_id(install_b, "Imaging"),
                             name="Elsewhere"), "QUEUE_NOT_FOUND"),
            (fx.settings_cmd("queue_rename", queue_id=imaging, name=""), "COMMAND_MALFORMED"),
            (fx.settings_cmd("queue_rename", queue_id=imaging), "COMMAND_MALFORMED"),
        ]:
            fx.refused(install, fx.SETTINGS_PUT, command, code, conninfo=support)
    with _support(env, install, "fx1-s10-nogrant", caps=None) as nogrant:
        fx.refused(install, fx.SETTINGS_PUT,
                   fx.settings_cmd("queue_rename", queue_id=imaging, name="Elsewhere"),
                   "SUPPORT_NOT_AUTHORIZED", conninfo=nogrant)
    before = fx.snapshot(install)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        sa.call(install, fx.SETTINGS_PUT,
                fx.settings_cmd("queue_rename", queue_id=imaging, name="Elsewhere"))
    assert fx.snapshot(install) == before
    assert fx.snapshot(install_b) == b_before
