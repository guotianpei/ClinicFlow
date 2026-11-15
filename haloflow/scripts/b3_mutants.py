"""Option B B3a mutation controls, group ``b3`` (B3-M1..B3-M39). SYNTHETIC ONLY.

Mutant list: v1r1 §5 (1-26) and v1r2 (27-34) as aligned for the B3a red packet, plus B3-M35
(the pinned SEALED_READY_DIGEST comparison skipped; Codex red gate bb62696, green condition 3)
and B3-M36 (the F-R1 resolution activation not provisioned), and B3-M37..M39 (lease ownership under
startup cancellation; Codex reviews 85d836a, 0e11467). Each mutant is ONE exact textual
edit (the old text must occur exactly once in the clean target, else the run fails as stale).

Judgement (B3 rule; the part-A whole-module rule does not fit the 6-hour job with PostgreSQL
lifecycle rows, so it is replaced, stated openly, by a declared-rows rule):
- control: the UNION of every mutant's declared killing rows runs once on the clean source and
  must pass completely (no failure, error or skip; every declared node collected);
- mutant: its declared killing rows run on the mutated source; it is KILLED when at least one
  of them fails or errors. A mutant marked `hang` (an edit that makes the coordinator wait
  for a step that ignores cancellation) is also killed when the run exceeds its timeout;
- restoration: after each mutant the target is restored and its SHA-256 re-checked; the run
  fails unless every target is byte-identical at the end.
Exit 0 only if the control is accepted, every mutant is killed and every target is restored.

Usage (from haloflow/):
    python3 -I scripts/b3_mutants.py --pytest "$VENV/bin/pytest" \\
        --evidence "$RUNNER_TEMP/verify-evidence/b3-mutation" [--mutant B3-M7 ...] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SYN: Final = Path("src/haloflow/synthetic")
LIFECYCLE, GUARD, RUNTIME, PROVISION, SEAL, MAIN = (
    SYN / "lifecycle.py", SYN / "guard.py", SYN / "runtime.py", SYN / "provision.py",
    SYN / "seal_scope.py", SYN / "__main__.py")
POOL: Final = Path("src/haloflow/m01/pool.py")
U: Final = "tests/synthetic/test_b3a_units.py::"
P: Final = "tests/synthetic/test_b3a_postgres.py::"
C: Final = "tests/synthetic/test_b3a_controls_postgres.py::"
F: Final = "tests/synthetic/test_b3a_fr1_postgres.py::"
RUN_TIMEOUT_S: Final = 900
HANG_TIMEOUT_S: Final = 240


@dataclass(frozen=True)
class Mutant:
    id: str
    description: str
    target: Path
    old: str
    new: str
    rows: tuple[str, ...]
    hang: bool = False


def g2(case: str) -> str:
    return f"{P}test_b3a_g2_unguarded_statements_refused_client_side[{case}]"


_START_BLOCK = (
    "        await asyncio.to_thread(runtime.read_reservation)\n"
    "        self._lease_future = _in_thread(runtime.acquire_lease)\n"
    "        self._lease_future.add_done_callback(self._lease_done)\n"
    "        await asyncio.shield(asyncio.wrap_future(self._lease_future))\n"
    "        self._claim_lease()              # synchronous, right after the await: no gap\n"
    '        fire("b3.start.lease", {})\n'
    "        await asyncio.to_thread(runtime.attest)\n"
    '        fire("b3.start.a1", {})\n'
    "        if runtime.needs_marker():\n"
    '            fire("b3.start.marker.before", {})\n'
    "            await asyncio.to_thread(runtime.write_marker)\n"
    '            fire("b3.start.marker.after", {})\n'
    "        await runtime.open_pool()\n"
    '        fire("b3.start.pool", {})\n')
_MARKER = (
    "        if runtime.needs_marker():\n"
    '            fire("b3.start.marker.before", {})\n'
    "            await asyncio.to_thread(runtime.write_marker)\n"
    '            fire("b3.start.marker.after", {})\n')
_POOL = "        await runtime.open_pool()\n" + '        fire("b3.start.pool", {})\n'
_SCHEDULERS = (
    '        for name in ("due", "reminder"):\n'
    "            self.schedulers[name].start()\n"
    "            self._started.append(name)\n"
    '        fire("b3.start.schedulers", {})\n'
    '        self.schedulers["writeback"].start()\n'
    '        self._started.append("writeback")\n')
_WATCHER_END = (
    "        self._watcher_task = asyncio.get_running_loop().create_task(self._watch())\n"
    '        fire("b3.start.watcher", {})\n')
_STEPS = (
    '        return [Step("watcher", "watcher", cfg.t_watcher, watcher, "WATCHER_STOP_FAILED"),\n'
    '                Step("port", "port", cfg.t_port, port, "CLOSE_TIMEOUT"),\n'
    '                Step("pool", "pool", cfg.t_pool, pool, "POOL_NOT_QUIESCENT"),\n'
    '                Step("quiesce", "quiesce", cfg.t_quiesce, quiesce, "POOL_NOT_QUIESCENT"),\n'
    '                Step("lease", "lease", cfg.t_lease, lease, "CLOSE_TIMEOUT")]\n')
_REST_CHECK = (
    '            if outcome != "ok":\n'
    "                return self._terminal(drain, self._code(outcome, step.code), step.name)\n"
    '            if step.name == "lease":\n')

MUTANTS: Final[tuple[Mutant, ...]] = (
    Mutant("B3-M1", "lifecycle: the guarded pool opens before the lease (and before A1)",
           LIFECYCLE, _START_BLOCK,
           "        await asyncio.to_thread(runtime.read_reservation)\n" + _POOL
           + "        self._lease_future = _in_thread(runtime.acquire_lease)\n"
           "        self._lease_future.add_done_callback(self._lease_done)\n"
           "        await asyncio.shield(asyncio.wrap_future(self._lease_future))\n"
           "        self._claim_lease()              # synchronous, right after the await: no gap\n"
           '        fire("b3.start.lease", {})\n'
           "        await asyncio.to_thread(runtime.attest)\n"
           '        fire("b3.start.a1", {})\n' + _MARKER,
           (f"{P}test_b3a_s2_installer_holding_ks_refuses_start",
            f"{P}test_b3a_sc3b_first_start_of_the_new_profile_writes_marker_before_pool")),
    Mutant("B3-M2", "lifecycle: A1 skipped at start", LIFECYCLE,
           "        await asyncio.to_thread(runtime.attest)\n", "",
           (f"{P}test_b3a_s3_epoch_superseded_refuses_before_pool",
            f"{P}test_b3a_sc4abc_pre_first_start_sealed_tamper_refused[staff]")),
    Mutant("B3-M3", "guard: runtime transactions take the EXCLUSIVE K_X", GUARD,
           "                await guard_async(self, self.expected, shared=True)\n",
           "                await guard_async(self, self.expected, shared=False)\n",
           (f"{P}test_b3a_g1_every_runtime_transaction_is_guarded",)),
    Mutant("B3-M4", "guard: the guard is dropped from the runtime outer transaction", GUARD,
           "                await guard_async(self, self.expected, shared=True)\n", "",
           (f"{P}test_b3a_g1_every_runtime_transaction_is_guarded",)),
    Mutant("B3-M5", "guard: the cursor fence is removed", GUARD,
           "        return not self.in_guard and not (isinstance(query, str) and query in"
           " ALLOWLIST)\n", "        return False\n",
           tuple(g2(c) for c in ("execute_insert", "begin", "cursor", "set_off"))),
    Mutant("B3-M6", "guard: the allowlist is widened by `SET default_transaction_read_only = off`",
           GUARD, "    DISCARD_SQL,\n    READ_ONLY_SQL,\n",
           '    DISCARD_SQL,\n    READ_ONLY_SQL,\n    "SET default_transaction_read_only = off",\n',
           (f"{U}test_b3a_g2u_allowlist_is_exact_and_read_only",)),
    Mutant("B3-M7", "guard: the read-only default is not re-applied after DISCARD ALL", GUARD,
           "            await super().execute(READ_ONLY_SQL)                    # P-c\n",
           "            pass\n",
           (f"{P}test_b3a_g5_read_only_reapplied_after_reset",
            f"{C}test_b3a_g5pre_write_after_pool_reset_is_refused")),
    Mutant("B3-M8", "pool: TenantPool ignores connection_class", POOL,
           "            connection_class=connection_class,\n", "",
           (*(g2(c) for c in ("execute_insert", "cursor")),
            f"{P}test_b3a_g5_read_only_reapplied_after_reset")),
    Mutant("B3-M9", "runtime: the migrator/provisioner factories do not refuse", RUNTIME,
           '        raise SyntheticRefused("RUNTIME_NO_DDL")\n',
           "        return None  # type: ignore[return-value]\n",
           (f"{P}test_b3a_g6_runtime_never_migrates_or_provisions",)),
    Mutant("B3-M10", "provision: the full seal is skipped before the first start", PROVISION,
           '    return state.get("phase") == "ready" and RUNTIME_KEY in state\n',
           '    return state.get("phase") == "ready"\n',
           (f"{P}test_b3a_sc4d_pre_first_start_runtime_row_tamper_refused",
            f"{P}test_b3a_sc9e_hand_removed_marker_fails_closed")),
    Mutant("B3-M11", "seal_scope: the reduced baseline ignores the stored seal's values", SEAL,
           '    return {"nondet": {table: rows for table, rows in seal["nondet"].items()\n'
           "                       if not is_runtime_mutable(table)},\n",
           '    return {"nondet": {},\n',
           (f"{U}test_b3a_u5_reduced_baseline_is_a_pure_projection",)),
    Mutant("B3-M12", "lifecycle: the first-start marker is written after the pool opens",
           LIFECYCLE, _MARKER + _POOL, _POOL + _MARKER,
           (f"{P}test_b3a_sc3b_first_start_of_the_new_profile_writes_marker_before_pool",)),
    Mutant("B3-M13", "seal_scope: RUNTIME_MUTABLE_TABLES widened by m05_reminder_settings",
           SEAL, '    "{tenant}.patient_event_links", "{tenant}.patient_events",\n})\n',
           '    "{tenant}.patient_event_links", "{tenant}.patient_events",\n'
           '    "{tenant}.m05_reminder_settings",\n})\n',
           (f"{U}test_b3a_u4_inventory_pins",
            f"{P}test_b3a_sc4efg_post_first_start_sealed_tamper_refused[settings]")),
    Mutant("B3-M14", "seal_scope: RUNTIME_MUTABLE_TABLES missing m05_followup_notes", SEAL,
           '    "{tenant}.m05_followup_items", "{tenant}.m05_followup_notes",\n',
           '    "{tenant}.m05_followup_items",\n',
           (f"{U}test_b3a_u4_inventory_pins",
            f"{P}test_b3a_sc1_composed_paths_change_exactly_the_runtime_mutable_tables")),
    Mutant("B3-M15", "seal_scope: column definitions dropped from the sealed digest", SEAL,
           '            if not (k.startswith("rows:") and is_runtime_mutable(k[len("rows:"):]))}\n',
           '            if k != "columns" and not (k.startswith("rows:")'
           ' and is_runtime_mutable(k[len("rows:"):]))}\n',
           # Killed by the independent canonical projection (U5b) and by a reduced start that
           # must succeed (SC3). SC5 alone cannot kill it: with definitions dropped, EVERY
           # reduced start diverges from the pin, so SC5's expected refusal still happens.
           (f"{F}test_b3a_u5b_sealed_ready_digest_reproducible_and_discriminating",
            f"{P}test_b3a_sc3_installer_accepts_runtime_rows_after_first_start")),
    Mutant("B3-M16", "lifecycle: request_stop is not idempotent (a second drain starts)",
           LIFECYCLE, "        if self._drain is not None:\n            return None\n"
           "        deadline = time.monotonic() + self.config.t_total\n",
           "        deadline = time.monotonic() + self.config.t_total\n",
           (f"{U}test_b3a_d4u_concurrent_requests_one_drain_one_deadline",
            f"{P}test_b3a_d4_simultaneous_requests_one_sequence")),
    Mutant("B3-M17", "lifecycle: admission is not closed at the stop request", LIFECYCLE,
           '        self.admission_open = False\n        if self.state == "RUNNING":\n',
           '        if self.state == "RUNNING":\n',
           (f"{P}test_b3a_d1_normal_stop_order_quiescence_and_admission",)),
    Mutant("B3-M18", "lifecycle: no second stop attempt for a failed producer", LIFECYCLE,
           "            failed = [n for n in failed\n"
           '                      if await self._step(drain, retry[n][0], retry[n][1]) != "ok"]\n',
           "            failed = list(failed)\n",
           (f"{U}test_b3a_d2u_second_attempt_recovers_then_closes_in_order",)),
    Mutant("B3-M19", "lifecycle: the terminal path does not call fatal_exit", LIFECYCLE,
           "        self._fatal_exit(FATAL_EXIT_CODE, code, step)\n", "",
           (f"{U}test_b3a_d3u_watchdog_and_coordinator_one_terminal_disposition",
            f"{P}test_b3a_d3_terminal_stop_failure_in_process")),
    Mutant("B3-M20", "lifecycle: a lease backend left alive is not terminated", LIFECYCLE,
           "                if present:\n"
           '                    conn.execute("SELECT pg_catalog.pg_terminate_backend(%s)",'
           " (pid,))\n",
           "                if False:\n"
           '                    conn.execute("SELECT pg_catalog.pg_terminate_backend(%s)",'
           " (pid,))\n",
           (f"{P}test_b3a_d6_lease_backend_left_alive_is_terminated",)),
    Mutant("B3-M21", "lifecycle: an unconfirmed PORT close continues the sequence", LIFECYCLE,
           _REST_CHECK, _REST_CHECK.replace('if outcome != "ok":',
                                            'if outcome != "ok" and step.name != "port":'),
           # A hang kill: the abandoned cancellation-resistant step is disposed only on the
           # terminal path, so with the sequence continuing the row's event loop cannot close.
           (f"{U}test_b3a_d9u_cancellation_resistant_step_never_awaited[port]",), hang=True),
    Mutant("B3-M22", "lifecycle: the port is closed before the producers stop", LIFECYCLE,
           '        watcher = [s for s in steps if s.name == "watcher"]\n'
           '        rest = [s for s in steps if s.name != "watcher"]\n',
           '        watcher = [s for s in steps if s.name in ("watcher", "port")]\n'
           '        rest = [s for s in steps if s.name not in ("watcher", "port")]\n',
           (f"{P}test_b3a_d1_normal_stop_order_quiescence_and_admission",)),
    Mutant("B3-M23", "lifecycle: the lease is released before the pool closes", LIFECYCLE,
           _STEPS,
           '        return [Step("watcher", "watcher", cfg.t_watcher, watcher,'
           ' "WATCHER_STOP_FAILED"),\n'
           '                Step("port", "port", cfg.t_port, port, "CLOSE_TIMEOUT"),\n'
           '                Step("lease", "lease", cfg.t_lease, lease, "CLOSE_TIMEOUT"),\n'
           '                Step("pool", "pool", cfg.t_pool, pool, "POOL_NOT_QUIESCENT"),\n'
           '                Step("quiesce", "quiesce", cfg.t_quiesce, quiesce,'
           ' "POOL_NOT_QUIESCENT")]\n',
           (f"{P}test_b3a_d1_normal_stop_order_quiescence_and_admission",)),
    Mutant("B3-M24", "lifecycle: the scheduler start order is reversed", LIFECYCLE,
           _SCHEDULERS,
           '        for name in ("writeback", "reminder"):\n'
           "            self.schedulers[name].start()\n"
           "            self._started.append(name)\n"
           '        fire("b3.start.schedulers", {})\n'
           '        self.schedulers["due"].start()\n'
           '        self._started.append("due")\n',
           (f"{P}test_b3a_d5_partial_startup_stops_only_started",)),
    Mutant("B3-M25", "lifecycle: no reverse close on a startup failure", LIFECYCLE,
           '        self._coordinator.request_stop("STARTUP_FAILED")\n'
           "        await self._coordinator.wait()\n",
           "        return\n",
           (f"{P}test_b3a_s6_startup_failure_reverse_close[pool]",
            f"{P}test_b3a_s6_startup_failure_reverse_close[lease]")),
    Mutant("B3-M26", "serve: a non-loopback bind is allowed", MAIN,
           "        return ipaddress.ip_address(host).is_loopback\n", "        return True\n",
           (f"{U}test_b3a_f4u_serve_refuses_non_loopback_before_binding[0.0.0.0]",)),
    Mutant("B3-M27", "provision: the inventory equality is dropped in reduced mode", PROVISION,
           "    if live != INSTALL_SEALED_TABLES | RUNTIME_MUTABLE_TABLES:\n"
           '        raise _diverged("inventory")\n', "",
           (f"{P}test_b3a_sc2b_post_extra_table_refused_by_the_inventory_check[{{schema}}]",
            f"{P}test_b3a_sc2b_post_extra_table_refused_by_the_inventory_check[shared]")),
    Mutant("B3-M28", "lifecycle: the marker is removed on a startup failure", LIFECYCLE,
           '        self._coordinator.request_stop("STARTUP_FAILED")\n',
           "        with contextlib.suppress(Exception), StateDir(\n"
           '                self._environ["HALOFLOW_SYNTHETIC_STATE"]).locked() as st:\n'
           "            current = st.read() or {}\n"
           '            current.pop("runtime", None)\n'
           "            st.write(current)\n"
           '        self._coordinator.request_stop("STARTUP_FAILED")\n',
           (f"{P}test_b3a_s6_startup_failure_reverse_close[pool]",)),
    Mutant("B3-M29", "lifecycle: the marker is written after admission-ready (end of start)",
           LIFECYCLE, _MARKER + _POOL + "        rt = runtime.compose()\n",
           _POOL + "        rt = runtime.compose()\n" + _MARKER,
           (f"{P}test_b3a_sc3b_first_start_of_the_new_profile_writes_marker_before_pool",)),
    Mutant("B3-M30", "lifecycle: a step is waited with asyncio.wait_for (awaits cancellation)",
           LIFECYCLE,
           "        done, _ = await asyncio.wait({task}, timeout=bound)\n",
           "        with contextlib.suppress(TimeoutError):\n"
           "            await asyncio.wait_for(task, timeout=bound)\n"
           "        done = {task} if task.done() else set()\n",
           (f"{U}test_b3a_d9u_cancellation_resistant_step_never_awaited[pool]",), hang=True),
    Mutant("B3-M31", "lifecycle: no watchdog thread", LIFECYCLE,
           "        watchdog.start()\n", "",
           (f"{P}test_b3a_d10_blocked_event_loop_watchdog_exit",)),
    Mutant("B3-M32", "lifecycle: the watcher stop is outside the drain budget", LIFECYCLE,
           "        return (self.t_watcher + 2 * sum(self.stop_timeout_s) + self.t_port"
           " + self.t_pool\n",
           "        return (2 * sum(self.stop_timeout_s) + self.t_port + self.t_pool\n",
           (f"{U}test_b3a_u1_defaults_and_total_budget",)),
    Mutant("B3-M33", "lifecycle: the quiescence check is skipped", LIFECYCLE,
           "            if self.runtime is None or self.runtime.pool is None:\n"
           "                return\n"
           "            while await asyncio.to_thread(self._runtime_backends) != 0:\n",
           "            return\n"
           "            while await asyncio.to_thread(self._runtime_backends) != 0:\n",
           (f"{P}test_b3a_d12_non_quiescent_backend_terminal",)),
    Mutant("B3-M34", "lifecycle: the lease is released after an unconfirmed pool close",
           LIFECYCLE, _REST_CHECK,
           _REST_CHECK.replace('if outcome != "ok":',
                               'if outcome != "ok" and step.name not in ("pool", "quiesce"):'),
           (f"{P}test_b3a_d7_pool_close_unconfirmed_goes_terminal_lease_kept[hang]",)),
    Mutant("B3-M35", "provision: the pinned SEALED_READY_DIGEST comparison is skipped",
           PROVISION,
           "    if sealed_digest(cap) != installed_profile.SEALED_READY_DIGEST:"
           "   # the pin, read now\n"
           '        raise _diverged("profile")\n', "",
           (f"{P}test_b3a_sc10_wrong_pinned_digest_enforced_after_first_start",)),
    Mutant("B3-M36", "provision: the F-R1 resolution activation is not provisioned", PROVISION,
           "        c.execute(RESOLUTION_PROFILE_SQL.format(schema=ctx.schema).encode())"
           "   # F-R1\n", "",
           (f"{F}test_b3a_fr_e2_fresh_install_has_resolution_active",)),
    Mutant("B3-M37", "lifecycle: a failed start does not abandon an unclaimed lease",
           LIFECYCLE, "            self._abandon_lease()            # synchronous: no await can"
           " interrupt it\n", "",
           (f"{P}test_b3a_s8_cancel_during_lease_acquisition_strands_no_lease"
            "[after_start_failed]",
            f"{P}test_b3a_s8_cancel_during_lease_acquisition_strands_no_lease"
            "[repeated_cancel]")),
    Mutant("B3-M38", "lifecycle: the acquiring thread does not release an abandoned success",
           LIFECYCLE, '            release = self._lease_state == "abandoned"\n',
           "            release = False\n",
           (f"{P}test_b3a_s8_cancel_during_lease_acquisition_strands_no_lease"
            "[after_start_failed]",)),
    Mutant("B3-M39", "lifecycle: the abort drain is owned by the caller (wait not shielded)",
           LIFECYCLE, "        return await asyncio.shield(self._drain.task)\n",
           "        return await self._drain.task\n",
           (f"{P}test_b3a_s9_second_cancel_while_abort_drains_still_releases_the_lease",)),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_edits() -> list[str]:
    problems = []
    for m in MUTANTS:
        count = m.target.read_text().count(m.old)
        if count != 1:
            problems.append(f"{m.id}: old text occurs {count} times in {m.target}")
        if m.old == m.new:
            problems.append(f"{m.id}: edit is a no-op")
    ids = [m.id for m in MUTANTS]
    if len(set(ids)) != len(ids):
        problems.append("duplicate mutant ids")
    return problems


def run_rows(pytest: str, rows: tuple[str, ...], junit: Path, timeout: int
             ) -> tuple[int | None, dict[str, str], float]:
    junit.unlink(missing_ok=True)
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    start = time.monotonic()
    try:
        done = subprocess.run([pytest, *rows, "-q", "-p", "no:cacheprovider", "-p",
                               "no:randomly", "-o", "junit_family=xunit1",
                               f"--junitxml={junit}"], capture_output=True, text=True,
                              timeout=timeout, check=False, env=env)
        code: int | None = done.returncode
        (junit.parent / (junit.stem + ".log")).write_text(done.stdout[-20000:]
                                                          + done.stderr[-5000:])
    except subprocess.TimeoutExpired:
        code = None
    elapsed = time.monotonic() - start
    outcomes: dict[str, str] = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            node = case.get("classname", "").replace(".", "/") + ".py::" + case.get("name", "")
            kinds = [c.tag for c in case if c.tag in ("failure", "error", "skipped")]
            outcomes[node] = kinds[0] if kinds else "passed"
    return code, outcomes, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=False, default="pytest")
    parser.add_argument("--evidence", required=False, default="")
    parser.add_argument("--mutant", action="append", default=None)
    parser.add_argument("--skip-control", action="store_true")
    parser.add_argument("--check", action="store_true", help="edit applicability only")
    args = parser.parse_args(argv)
    problems = check_edits()
    if args.check or problems:
        for p in problems:
            print("STALE " + p)
        print(f"b3 edits: {len(MUTANTS)} mutants, {len(problems)} problems")
        return 1 if problems else 0
    os.environ["PYTHONPYCACHEPREFIX"] = tempfile.mkdtemp(prefix="b3-pycache-")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    evidence = Path(args.evidence or tempfile.mkdtemp(prefix="b3-mutation-"))
    evidence.mkdir(parents=True, exist_ok=True)
    chosen = [m for m in MUTANTS if args.mutant is None or m.id in args.mutant]
    targets = sorted({m.target for m in MUTANTS}, key=str)
    original = {t: sha(t) for t in targets}
    results: dict[str, object] = {"targets": {str(t): h for t, h in original.items()}}
    ok = True
    if not args.skip_control:
        union = tuple(sorted({r for m in chosen for r in m.rows}))
        code, outcomes, elapsed = run_rows(args.pytest, union, evidence / "control.xml",
                                           RUN_TIMEOUT_S * 2)
        bad = sorted(n for n in union if outcomes.get(n) != "passed")
        results["control"] = {"exit": code, "rows": len(union), "not_passed": bad,
                              "seconds": round(elapsed, 1)}
        print(f"b3 control: rows={len(union)} exit={code} not_passed={len(bad)}"
              f" seconds={elapsed:.0f}")
        for n in bad:
            print(f"  control not passed: {n} -> {outcomes.get(n, 'not collected')}")
        if code != 0 or bad:
            ok = False
    rows_out = []
    for m in chosen:
        text = m.target.read_text()
        try:
            m.target.write_text(text.replace(m.old, m.new, 1))
            code, outcomes, elapsed = run_rows(
                args.pytest, m.rows, evidence / f"{m.id}.xml",
                HANG_TIMEOUT_S if m.hang else RUN_TIMEOUT_S)
        finally:
            m.target.write_text(text)
        failed = sorted(n for n, k in outcomes.items() if k in ("failure", "error"))
        killed = bool(failed) or (m.hang and code is None)
        ok = ok and killed
        rows_out.append({"id": m.id, "description": m.description, "target": str(m.target),
                         "rows": list(m.rows), "failed": failed, "exit": code,
                         "timed_out": code is None, "killed": killed,
                         "seconds": round(elapsed, 1)})
        print(f"{m.id} killed={killed} failed={len(failed)}/{len(m.rows)}"
              f"{' timeout' if code is None else ''} seconds={elapsed:.0f} {m.description}")
    restored = all(sha(t) == h for t, h in original.items())
    ok = ok and restored
    results["mutants"] = rows_out
    results["restored"] = restored
    results["ok"] = ok
    (evidence / "b3-mutation.json").write_text(json.dumps(results, indent=2, sort_keys=True))
    killed_n = sum(1 for r in rows_out if r["killed"])
    print(f"b3 mutation: ok={ok} killed={killed_n}/{len(rows_out)} restored={restored}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
