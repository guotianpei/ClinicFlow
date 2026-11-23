"""Option B B3a/B3b mutation controls, group ``b3`` (B3-M1..B3-M60). SYNTHETIC ONLY.

Mutant list: v1r1 §5 (1-26) and v1r2 (27-34) as aligned for the B3a red packet, plus B3-M35
(the pinned SEALED_READY_DIGEST comparison skipped; Codex red gate bb62696, green condition 3)
and B3-M36 (the F-R1 resolution activation not provisioned), and B3-M37..M40 (lease ownership under
startup cancellation; Codex reviews 85d836a, 0e11467, 2c673fe), and B3-M41..M60 (Option B B3b:
the local exposure gate, identity, closed routes, simulator, labels and the HTTP drain; B3b red
packet v1 map, with the `http` step mutant split into the lifecycle and coordinator forms), and
B3-M61..M72 (Option B B4: the staff-screen form boundary, escaping, labels and replay; B4 red
packet v1 map, Codex red gate PASS at 54a71ef), and M-B5-1..16 (Option B B5: the scenario
load, the display, the simulator page and the Eastern placement; B5 red packet map, Codex red
gate PASS at f246ffa). Each mutant is ONE exact textual edit (the old text must occur exactly
once in the clean target, else the run fails as stale); a B5 mutant may carry further exact
edits (`also`, each checked the same way) when one behaviour spans two places.

B5 `repin` mutants edit a synthetic SQL function body. The sealed installed-state profile
(`installed_profile.py`) pins those bodies, so an unpinned edit would be refused at start
(STATE_DIVERGED) and "killed" by the seal, not by its rows. For these mutants the harness runs
`scripts/b2_measure_profile.py` after the edit (re-pinning the profile to the MUTATED
functions), runs the rows, then restores the target AND the profile; a failed re-pin is a
failed run, never a kill.

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
GATE, LABELS, STAFF, SIMU, APP = (SYN / "gate.py", SYN / "labels.py", SYN / "staff_routes.py",
                                  SYN / "simulator.py", SYN / "app.py")
UI: Final = SYN / "ui.py"
SIMA, SIMUI, DISP, SCEN = (SYN / "sim_actions.py", SYN / "sim_ui.py", SYN / "display_units.py",
                           SYN / "scenarios" / "__init__.py")
PROFILE: Final = SYN / "installed_profile.py"
# E4: a re-pinned mutant names the profile whose installed-state module it re-measures.
PROFILE_FILES: Final = {"default": PROFILE,
                        "eligibility": SYN / "installed_profile_eligibility.py"}
B5U: Final = "tests/synthetic/test_b5_units.py::"
B5P: Final = "tests/synthetic/test_b5_postgres.py::"
BU: Final = "tests/synthetic/test_b3b_units.py::"
BP: Final = "tests/synthetic/test_b3b_postgres.py::"
B4U: Final = "tests/synthetic/test_b4_units.py::"
B4P: Final = "tests/synthetic/test_b4_postgres.py::"
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
    also: tuple[tuple[Path, str, str], ...] = ()
    repin: bool = False
    repin_profile: str = "default"

    def edits(self) -> tuple[tuple[Path, str, str], ...]:
        return ((self.target, self.old, self.new), *self.also)


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
    '                Step("http", "http", cfg.t_http, http, "HTTP_NOT_DRAINED"),\n'
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
           '                Step("http", "http", cfg.t_http, http, "HTTP_NOT_DRAINED"),\n'
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
           # E4: the inventory is the recorded profile's (default: the same union).
           "    if live != inventory(_ACTIVE.get()):\n"
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
           "        return (self.t_watcher + 2 * sum(self.stop_timeout_s) + self.t_http"
           " + self.t_port\n",
           "        return (2 * sum(self.stop_timeout_s) + self.t_http + self.t_port\n",
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
           LIFECYCLE,
           '            release = self._lease_state == "abandoned" and not future.cancelled() \\\n',
           "            release = False and not future.cancelled() \\\n",
           (f"{P}test_b3a_s8_cancel_during_lease_acquisition_strands_no_lease"
            "[after_start_failed]",)),
    Mutant("B3-M39", "lifecycle: the abort drain is owned by the caller (wait not shielded)",
           LIFECYCLE, "        return await asyncio.shield(self._drain.task)\n",
           "        return await self._drain.task\n",
           (f"{P}test_b3a_s9_second_cancel_while_abort_drains_still_releases_the_lease",)),
    Mutant("B3-M40", "lifecycle: no single releaser election (abandon and callback both release)",
           LIFECYCLE, "        if self._lease_release_claimed:\n            return False\n", "",
           (f"{P}test_b3a_s8b_done_before_callback_interleaving_releases_exactly_once",)),
    # ------------------------------------------------------------------ Option B B3b
    Mutant("B3-M41", "gate: the Host check is removed", GATE,
           "        if len(hosts) != 1 or hosts[0] not in self.allowed:\n",
           "        if not hosts:\n",
           (f"{BU}test_b3b_h1u_spoofed_host_refused_before_routing[evil.example]",
            f"{BP}test_b3b_h2_spoofed_host_refused_on_every_route_while_running")),
    Mutant("B3-M42", "gate: the POST Origin check is removed", GATE,
           '            if len(origins) != 1 or origins[0] != f"http://{host}":\n',
           "            if False:\n",
           (f"{BP}test_b3b_h3_post_origin_required_and_exact[None]",
            f"{BP}test_b3b_h3b_cross_alias_origin_refused")),
    Mutant("B3-M43", "gate: the CSRF comparison always passes", GATE,
           "            if (len(tokens) != 1 or token is None\n",
           "            if False and (len(tokens) != 1 or token is None\n",
           (f"{BP}test_b3b_h5_csrf_token_required[wrong]",)),
    Mutant("B3-M44", "gate: the JSON content-type check is removed", GATE,
           "            if len(media) != 1 or media[0].strip().lower() not in _JSON:\n",
           "            if False:\n",
           (f"{BP}test_b3b_h4_post_content_type_must_be_json[text/plain]",)),
    Mutant("B3-M45", "app: a permissive CORS middleware is added (outermost)", APP,
           "    app.add_middleware(LocalGate, lifecycle=lc, allowed=allowed)\n",
           "    app.add_middleware(LocalGate, lifecycle=lc, allowed=allowed)\n"
           "    from starlette.middleware.cors import CORSMiddleware\n"
           '    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])\n',
           (f"{BU}test_b3b_h7u_options_405_no_cors_headers",)),
    Mutant("B3-M46", "gate: fetch metadata is ignored", GATE,
           "        if any(site.strip().lower() not in _FETCH_OK for site in sites):\n",
           "        if False:\n",
           (f"{BP}test_b3b_h6_cross_site_fetch_metadata_refused",)),
    Mutant("B3-M47", "staff_routes: the principal is not the fixed staff member", STAFF,
           "    principal = Principal(kind=PrincipalKind.ACTOR, id=services.staff_id,\n",
           "    principal = Principal(kind=PrincipalKind.ACTOR, id=str(uuid.uuid4()),\n",
           (f"{BP}test_b3b_i1_fixed_principal_on_every_staff_route",
            f"{BP}test_b3b_i3_request_chosen_actor_headers_ignored")),
    Mutant("B3-M48", "staff_routes: capabilities from a fixed full set, not the standing", STAFF,
           "        held = capabilities_for(await services.staff.self_standing(context))\n",
           "        await services.staff.self_standing(context)\n"
           "        from haloflow.platform.capabilities import CAPABILITY_MATRIX\n"
           "        held = frozenset().union(*CAPABILITY_MATRIX.values())\n",
           (f"{BP}test_b3b_i4_capabilities_come_from_the_standing",)),
    Mutant("B3-M49", "staff_routes: an admin route is mounted", STAFF,
           '    "contact": ("POST", PREFIX + "/{item_id}/contact"),\n}\n',
           '    "contact": ("POST", PREFIX + "/{item_id}/contact"),\n'
           '    "admin": ("POST", "/synthetic/v1/staff/admin"),\n}\n',
           (f"{BU}test_b3b_r7u_route_inventory_is_closed",
            f"{BP}test_b3b_r7_not_served_paths_are_404")),
    Mutant("B3-M50", "simulator: the reply bypasses the ingress handler (direct receive)", SIMU,
           "    status, out = await _deliver(_ingress_app(lifecycle), lifecycle.ingress_secret,"
           " payload)\n",
           "    from haloflow.m05.reply_classifier import InboundMessage\n"
           "    services = lifecycle.services\n"
           "    ctx = await services.resolver.resolve(principal=Principal("
           "kind=PrincipalKind.WORKLOAD, id=W_INGRESS, auth_method='synthetic_ingress',"
           " authorized_tenant_ids=frozenset({services.tenant_id}), capabilities=INGRESS_CAPS),"
           " tenant_hint=services.tenant_id, purpose='operations', capabilities=INGRESS_CAPS,"
           " source=TrustedSource.HTTP, execution_id=uuid.uuid4(), correlation_id=uuid.uuid4(),"
           " correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE)\n"
           "    result = await wiring.reply_intake(services.gateway).receive(ctx, InboundMessage("
           "channel='sms', provider_message_id=str(uuid.uuid4()), phone=phone, text=text))\n"
           "    status, out = 202, {'outcome': result.outcome, 'decision': result.decision,"
           " 'inbound_id': str(result.inbound_id)}\n",
           (f"{BP}test_b3b_s2_reply_goes_through_the_ingress_handler",)),
    Mutant("B3-M51", "app: the ingress path is mounted on the public app", APP,
           "    install_simulator(app, lc)\n",
           "    install_simulator(app, lc)\n"
           '    app.add_api_route("/v1/synthetic/inbound-sms", lambda: {"x": 1},'
           ' methods=["POST"])\n',
           (f"{BU}test_b3b_r7u_route_inventory_is_closed",
            f"{BP}test_b3b_s5_ingress_route_not_on_the_public_app")),
    Mutant("B3-M52", "simulator: the canonical phone check is removed", SIMU,
           # B5: re-anchored (deliver_reply split; same edit, 4-space indent).
           "    if type(phone) is not str or not PHONE.fullmatch(phone):\n",
           "    if type(phone) is not str:\n",
           (f"{BP}test_b3b_s3_non_canonical_phone_refused_before_ingress[+15555550200]",)),
    Mutant("B3-M53", "labels: refusals lose the simulated key", LABELS,
           '    return labelled({"code": code, **extra}, status_code)\n',
           '    return JSONResponse({"code": code, **extra}, status_code=status_code)\n',
           (f"{BU}test_b3b_l2u_unknown_path_and_method_are_labelled[GET-/nope]",
            f"{BP}test_b3b_l1_every_route_labelled_on_success_and_refusal")),
    Mutant("B3-M54", "gate: its own exception path is unlabelled", GATE,
           "                await internal()(scope, receive, labelled_send)\n",
           '                await JSONResponse({"code": "INTERNAL"}, status_code=500)('
           "scope, receive, send)\n",
           (f"{BP}test_b3b_l4_middleware_fault_is_labelled_500[gate]",)),
    Mutant("B3-M55", "lifecycle: the http drain step is skipped", LIFECYCLE,
           '                Step("http", "http", cfg.t_http, http, "HTTP_NOT_DRAINED"),\n', "",
           (f"{BP}test_b3b_d1_held_request_is_awaited_before_the_port",)),
    Mutant("B3-M56", "lifecycle: the coordinator step order omits http", LIFECYCLE,
           'STEP_ORDER: Final = ("watcher", "http", "port", "pool", "quiesce", "lease")\n',
           'STEP_ORDER: Final = ("watcher", "port", "pool", "quiesce", "lease")\n',
           (f"{BU}test_b3b_d1u_http_step_runs_after_producers_before_port",)),
    Mutant("B3-M57", "lifecycle: an unconfirmed http step continues to port, pool and lease",
           LIFECYCLE, _REST_CHECK,
           '            if outcome != "ok" and step.name != "http":\n'
           "                return self._terminal(drain, self._code(outcome, step.code),"
           " step.name)\n"
           '            if step.name == "lease":\n',
           (f"{BU}test_b3b_d2u_unconfirmed_http_is_terminal_no_close[hold]",
            f"{BP}test_b3b_d2_unconfirmed_http_drain_terminal_through_a_child[no_db]")),
    Mutant("B3-M58", "gate: the in-flight count is released when the response starts", GATE,
           "                started = True\n",
           "                started = True\n"
           "                if counted:\n"
           "                    self.lifecycle.http_leave()\n",
           (f"{BP}test_b3b_d1b_held_body_stream_is_counted",)),
    Mutant("B3-M59", "gate: admission is not checked", GATE,
           "                if not counted:\n",
           "                if False:\n",
           (f"{BP}test_b3b_d3_after_stop_request_new_requests_are_503",)),
    Mutant("B3-M60", "simulator: the EHR route also writes to the database (through intake)",
           SIMU, "            lifecycle.port.ehr.put(read)\n",
           "            lifecycle.port.ehr.put(read)\n"
           "            await _deliver(_ingress_app(lifecycle), lifecycle.ingress_secret,"
           " json.dumps({'channel': 'sms', 'provider_message_id': str(uuid.uuid4()),"
           " 'phone': '+15555550144', 'text': '1'}).encode())\n",
           (f"{BP}test_b3b_s9_reply_writes_only_the_ingress_set_and_ehr_writes_nothing",)),
    Mutant("B3-M61", "ui: the form CSRF comparison accepts anything", UI,
           '    return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))\n',
           "    return True\n",
           (f"{B4P}test_b4_p_frm4_csrf_403_precedes_missing_fields[wrong]",)),
    Mutant("B3-M62", "ui: the CSRF refusal comes after a service call", UI,
           '    if not _csrf_ok(fields["csrf"], lifecycle.csrf_token):       # step 5\n',
           '    if not _csrf_ok(fields["csrf"], lifecycle.csrf_token) and await execute(\n'
           '            lifecycle, "get", lambda m05, ctx: m05.get(ctx, item_id)) is not None:\n',
           (f"{B4P}test_b4_p_frm4_csrf_403_precedes_missing_fields[wrong]",)),
    Mutant("B3-M63", "gate: the form exception becomes a prefix match", GATE,
           "    return UI_FORM_ROUTE.fullmatch(path) is not None\n",
           '    return path.startswith("/synthetic/ui/")\n',
           (f"{B4P}test_b4_p_frm6_form_media_never_widens_beyond_the_registered_routes",)),
    Mutant("B3-M64", "gate: the Origin check is skipped for staff-screen POSTs", GATE,
           '            if len(origins) != 1 or origins[0] != f"http://{host}":\n',
           '            if not _ui_form(scope) and (len(origins) != 1'
           ' or origins[0] != f"http://{host}"):\n',
           (f"{B4P}test_b4_p_sec4_ui_post_keeps_the_b3b_gate[cross_origin]",
            f"{B4P}test_b4_p_sec4_ui_post_keeps_the_b3b_gate[no_origin]")),
    Mutant("B3-M65", "ui: one render site (closed-map labels) drops escaping", UI,
           "        return esc(mapping[code])\n",
           "        return mapping[code]\n",
           (f"{B4P}test_b4_p_esc1_injected_labels_render_inert",)),
    Mutant("B3-M66", "ui: Other is accepted without a note on the screen path", UI,
           "            value.update(outcome_code=fields[\"outcome_code\"],"
           " note=note if note else None)\n",
           "            value.update(outcome_code=fields[\"outcome_code\"],"
           " note=note if note else \"-\")\n",
           (f"{B4P}test_b4_p_res3_other_without_note_refused_before_service[None]",)),
    Mutant("B3-M67", "ui: error pages lose the banner and headers", UI,
           '    return _page(f"<p class=\\"message\\">{esc(MESSAGES[\'refused\'])}</p>"\n',
           '    return HTMLResponse(f"<p class=\\"message\\">{esc(MESSAGES[\'refused\'])}</p>"\n',
           (f"{B4P}test_b4_p_lbl1_every_ui_page_and_error_page_is_labelled",)),
    Mutant("B3-M68", "ui: duplicate form fields are not refused", UI,
           "    if len(set(names)) != len(names) or any(name not in allowed for name in names):\n",
           "    if any(name not in allowed for name in names):\n",
           (f"{B4P}test_b4_p_frm2_structure_refused_400_before_service[dup_type]",
            f"{B4U}test_b4_u_frm2b_duplicate_unknown_and_count[note=1&note=2]")),
    Mutant("B3-M69", "ui: the streamed byte caps (absolute and declared length) are removed", UI,
           "        if size > UI_FORM_MAX_BYTES or (length is not None and size > length):\n",
           "        if False:\n",
           (f"{B4P}test_b4_p_frm1_byte_cap_before_decoding",)),
    Mutant("B3-M70", "ui: the percent-triplet check is removed", UI,
           "    if not _RAW_BYTES.fullmatch(body) or _BAD_PERCENT.search(body):\n",
           "    if not _RAW_BYTES.fullmatch(body):\n",
           (f"{B4P}test_b4_p_frm3_encoding_refused_400_before_service[bad_hex]",)),
    Mutant("B3-M71", "ui: other missing fields are refused (400) before the absent CSRF (403)",
           UI, '    if "csrf" not in fields:                                     # step 4\n',
           '    if any(name not in fields for name in required - {"csrf"}):\n'
           "        return _error_page(400)\n"
           '    if "csrf" not in fields:                                     # step 4\n',
           (f"{B4P}test_b4_p_frm4_csrf_403_precedes_missing_fields[absent_and_type_absent]",)),
    Mutant("B3-M72", "ui: the action type is derived from the endpoint at submit, not the form",
           UI, '        "client_action_id": fields["client_action_id"], "type": fields["type"],\n',
           '        "client_action_id": fields["client_action_id"],'
           ' "type": "claim" if endpoint == "claim" else "resolve",\n',
           (f"{B4P}test_b4_p_ui7bc_close_replay_keeps_its_type[claim_and_resolve]",)),
    # ------------------------------------------------------------------ Option B B5
    Mutant("M-B5-1", "load: the present check is skipped (a loaded scenario is put again)",
           SIMA, '        if claim["present"] != 0:\n', "        if False:\n",
           (f"{B5P}test_b5_p_ld5_present_after_restart_never_redates",
            f"{B5P}test_b5_p_ld2_concurrent_same_scenario")),
    Mutant("M-B5-2", "display: the appointment is found by the item's patient, not its source row",
           DISP, "    IF v_n <> 1 OR v_ref IS NULL THEN\n", "    IF false THEN\n",
           (f"{B5P}test_b5_p_ds2_uncorrelated_reply_shows_no_appointment",),
           also=((DISP, "     WHERE a.appointment_ref = v_ref;\n",
                  "     WHERE a.patient_id = v_item.patient_id ORDER BY a.start_at LIMIT 1;\n"),),
           repin=True),
    Mutant("M-B5-3", "view: the display lines are not escaped", UI,
           '    parts.extend(f"<p>{esc(line)}</p>"\n', '    parts.extend(f"<p>{line}</p>"\n',
           (f"{B5P}test_b5_p_esc1_injection_in_every_display_slot_is_inert",)),
    Mutant("M-B5-4", "simulator forms: the CSRF field is not checked", SIMUI,
           '    if "csrf" not in fields or not _csrf_ok(fields["csrf"], lifecycle.csrf_token):\n',
           "    if False:\n",
           (f"{B5P}test_b5_p_csrf_form_token_checked_first[missing]",
            f"{B5P}test_b5_p_csrf_form_token_checked_first[wrong]")),
    Mutant("M-B5-5", "load: a non-allowlisted spelling is normalised and accepted", SIMA,
           "    if type(scenario) is not str or scenario not in fixtures \\\n",
           "    scenario = scenario.upper() if type(scenario) is str else scenario\n"
           "    if type(scenario) is not str or scenario not in fixtures \\\n",
           (f"{B5P}test_b5_p_ld6_malformed_load_writes_nothing[lower-body1-422-UNKNOWN_SCENARIO]",
            )),
    Mutant("M-B5-6", "tick: the tick path writes (it loads S1 before ticking)", SIMA,
           "        outcome = await lifecycle.schedulers[scheduler].tick()\n",
           "        _zones = scenarios.LOAD_ZONES\n"
           '        scenarios.LOAD_ZONES = ("Pacific/Honolulu", "America/Bogota", "Africa/Abidjan",'
           ' "Asia/Karachi", "Pacific/Port_Moresby")\n'
           "        try:\n"
           '            await load(lifecycle, "S1")\n'
           "        finally:\n"
           "            scenarios.LOAD_ZONES = _zones\n"
           "        outcome = await lifecycle.schedulers[scheduler].tick()\n",
           (f"{B5P}test_b5_p_tk4_tick_with_nothing_due_moves_no_deadline",)),
    Mutant("M-B5-7", "reply: the phone is logged", SIMU,
           '    LOGGER.info("SIMULATOR action=patient_reply status=%d code=%s", status,'
           ' shown or "-")\n',
           '    LOGGER.info("SIMULATOR action=patient_reply status=%d code=%s phone=%s", status,'
           ' shown or "-", phone)\n',
           (f"{B5P}test_b5_p_prv1_no_phone_text_or_name_in_logs",)),
    Mutant("M-B5-8", "claim: the advisory lock is shared (concurrent loads do not serialise)",
           DISP, "    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(\n",
           "    PERFORM pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended(\n",
           (f"{B5P}test_b5_p_ld2_concurrent_same_scenario",), repin=True),
    Mutant("M-B5-9", "display: the appointment's patient is not checked against the item's",
           DISP, "    IF NOT FOUND OR v_a.patient_id IS DISTINCT FROM v_item.patient_id\n",
           "    IF NOT FOUND\n",
           (f"{B5P}test_b5_p_ds5_broken_source_chains_fail_closed",), repin=True),
    Mutant("M-B5-10", "load: the EHR entries are published after commit", SIMA,
           "                ehr.put(EhrRead(", "                pending.append(EhrRead(",
           (f"{B5P}test_b5_p_rdy1_after_commit_writeback_reads_ehr_before_release",
            f"{B5P}test_b5_p_rdy2_before_commit_nothing_is_sent"),
           also=((SIMA, "    complete = False\n",
                  "    complete = False\n    pending: list[Any] = []\n"),
                 (SIMA, '        await afire("b5.load.after_commit", {})\n',
                  '        await afire("b5.load.after_commit", {})\n'
                  "        for _read in pending:\n            ehr.put(_read)\n"))),
    Mutant("M-B5-11", "load: a returned non-applied directory outcome does not roll back", SIMA,
           '        raise RuntimeError("directory result not applied")\n', "        pass\n",
           (f"{B5P}test_b5_p_ld4_returned_refusal_rolls_back[1]",
            f"{B5P}test_b5_p_ld4_returned_refusal_rolls_back[8]")),
    Mutant("M-B5-12", "present: t097 answers a count instead of the present subset", DISP,
           "        COALESCE((SELECT pg_catalog.jsonb_agg(a.appointment_ref::text"
           " ORDER BY a.appointment_ref)\n",
           "        COALESCE((SELECT pg_catalog.to_jsonb(pg_catalog.count(*))\n",
           (f"{B5P}test_b5_p_ss1_exact_present_subset_and_page_states",), repin=True),
    Mutant("M-B5-13", "page: EHR entries are not intersected with the present refs", SCEN,
           "    return frozenset(present & ehr)\n", "    return frozenset(ehr)\n",
           (f"{B5U}test_b5_u_ss6_orphans_are_never_visible",
            f"{B5P}test_b5_p_rdy3_failure_or_cancel_leaves_nothing[fault-b5.load.before_commit]")),
    Mutant("M-B5-14", "zones: a second product zone is added", SCEN,
           "LOAD_ZONES: tuple[str, ...] = (LOAD_ZONE,)",
           'LOAD_ZONES: tuple[str, ...] = (LOAD_ZONE, "America/Chicago")',
           (f"{B5U}test_b5_u_zn0_product_zone_is_eastern_only",
            f"{B5U}test_b5_u_zn0s_source_scan_no_zone_override")),
    Mutant("M-B5-15", "hours: the load window's end is inclusive", SCEN,
           "        if LOAD_START <= local.time() < LOAD_END:\n",
           "        if LOAD_START <= local.time() <= LOAD_END:\n",
           (f"{B5U}test_b5_u_zn1c_half_open_endpoints[2026-11-17]",)),
    Mutant("M-B5-16", "start: 18:00 is taken in UTC, not in the placed zone", SCEN,
           "                    tzinfo=ZoneInfo(zone)).astimezone(UTC)\n",
           "                    tzinfo=UTC).astimezone(UTC)\n",
           (f"{B5U}test_b5_u_zn1a_every_minute_matches_the_owner_rule",
            f"{B5P}test_b5_p_zn0_uninjected_route_follows_the_owner_rule")),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_edits() -> list[str]:
    problems = []
    for m in MUTANTS:
        for target, old, new in m.edits():
            count = target.read_text().count(old)
            if count != 1:
                problems.append(f"{m.id}: old text occurs {count} times in {target}")
            if old == new:
                problems.append(f"{m.id}: edit is a no-op")
        if len({t for t, _o, _n in m.edits()}) != 1 and m.repin:
            problems.append(f"{m.id}: a re-pinned mutant edits one file")
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
    targets = sorted({t for m in MUTANTS for t, _o, _n in m.edits()}
                     | {PROFILE_FILES[m.repin_profile] for m in MUTANTS if m.repin} | {PROFILE},
                     key=str)
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
        files = sorted({t for t, _o, _n in m.edits()}
                       | ({PROFILE_FILES[m.repin_profile]} if m.repin else set()), key=str)
        saved = {t: t.read_text() for t in files}
        repinned = True
        try:
            for target, old, new in m.edits():
                target.write_text(target.read_text().replace(old, new, 1))
            if m.repin:
                repinned = subprocess.run(
                    [sys.executable, "-I", "scripts/b2_measure_profile.py",
                     *(() if m.repin_profile == "default"
                       else ("--profile", m.repin_profile))],
                    capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
                ).returncode == 0
            if repinned:
                code, outcomes, elapsed = run_rows(
                    args.pytest, m.rows, evidence / f"{m.id}.xml",
                    HANG_TIMEOUT_S if m.hang else RUN_TIMEOUT_S)
            else:
                code, outcomes, elapsed = None, {}, 0.0
        finally:
            for target, text in saved.items():
                target.write_text(text)
        failed = sorted(n for n, k in outcomes.items() if k in ("failure", "error"))
        killed = repinned and (bool(failed) or (m.hang and code is None))
        ok = ok and killed
        rows_out.append({"id": m.id, "description": m.description, "target": str(m.target),
                         "rows": list(m.rows), "failed": failed, "exit": code,
                         "timed_out": code is None and repinned, "killed": killed,
                         "repin": m.repin, "repinned": repinned,
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
