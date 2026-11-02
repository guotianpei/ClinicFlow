"""UC1 closeout trace (B5 package v2 section 7, v2r1; Codex 6abf4d64 point 4).

Maps every id of the independent required-id inventory
(`tests/m05/uc1_trace_inventory.py`: UC1 requirements, every agreed test id and the
release holds) to the collected pytest node ids that evidence it TODAY, or declares
it a pending release hold.

`TRACE` is a hand-reviewed LITERAL. It is never computed from the test files or the
collection; the collection is only used to check it. A row whose agreed id was
renamed, split or superseded during Part A / Part B carries a comment citing the
document that made the change.

Statuses: covered | pending_hold | out_of_scope. Evidence levels: unit (no
database), integration_pg17 (postgres-marked rows), process_restart (real SIGKILL
restart rows in tests/m05/test_b5_restart_postgres.py), simulated, none. A row's
level is the strongest kind among its nodes (process_restart > integration_pg17 >
unit).

`build_report(trace, collected)` refuses (TraceRefused) when:
- an inventory id is missing from the trace, or the trace has an id the inventory
  does not list;
- a release hold is not exactly (pending_hold, none, []), or a non-hold row is
  pending_hold;
- a status or evidence level is outside the closed sets;
- a covered row has no nodes or evidence level none; an out_of_scope row has nodes
  or evidence;
- a mapped node is not in `collected`.

CLI: `python scripts/uc1_trace.py [--collected FILE]` (run from haloflow/). Without
`--collected` it runs `pytest --collect-only -q -p no:cacheprovider tests/m01
tests/m02 tests/m05` itself. It prints the JSON report and exits 0, or prints the
refusal reason and exits 1 (2 when the collection itself fails).

B5 green does not complete the UC1 milestone: the pending_hold rows remain open.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = ROOT / "tests" / "m05" / "uc1_trace_inventory.py"
COLLECT_TARGETS = ("tests/m01", "tests/m02", "tests/m05")


class TraceRow(TypedDict):
    status: str
    evidence_level: str
    nodes: list[str]


class TraceRefused(Exception):
    """The trace cannot be reported truthfully; `reason` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _inventory() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_uc1_trace_inventory", INVENTORY_PATH)
    if spec is None or spec.loader is None:
        raise TraceRefused(f"inventory not loadable: {INVENTORY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_report(trace: Mapping[str, Any], collected: Iterable[str]) -> dict[str, Any]:
    inv = _inventory()
    holds = set(inv.HOLDS)
    required = set(inv.REQUIREMENTS) | set(inv.AGREED_TESTS) | set(inv.B5_TESTS) | holds
    present = set(collected)

    missing = sorted(required - set(trace))
    if missing:
        raise TraceRefused(f"inventory ids not traced: {missing}")
    extra = sorted(set(trace) - required)
    if extra:
        raise TraceRefused(f"trace ids not in the inventory: {extra}")

    rows: dict[str, dict[str, Any]] = {}
    for key in sorted(required):
        row = trace[key]
        if not isinstance(row, Mapping) or set(row) != {"status", "evidence_level", "nodes"}:
            raise TraceRefused(f"{key}: row must have exactly status, evidence_level, nodes")
        status, level, nodes = row["status"], row["evidence_level"], row["nodes"]
        if status not in inv.STATUSES:
            raise TraceRefused(f"{key}: status {status!r} is not in the closed set")
        if level not in inv.EVIDENCE_LEVELS:
            raise TraceRefused(f"{key}: evidence level {level!r} is not in the closed set")
        if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
            raise TraceRefused(f"{key}: nodes must be a list of node-id strings")
        if key in holds:
            if (status, level, nodes) != ("pending_hold", "none", []):
                raise TraceRefused(f"{key}: a release hold must be pending_hold, none, []")
        elif status == "pending_hold":
            raise TraceRefused(f"{key}: only a release hold may be pending_hold")
        if status == "covered" and (not nodes or level == "none"):
            raise TraceRefused(f"{key}: covered needs at least one node and real evidence")
        if status == "out_of_scope" and (nodes or level != "none"):
            raise TraceRefused(f"{key}: out_of_scope carries no nodes and evidence none")
        dangling = sorted(set(nodes) - present)
        if dangling:
            raise TraceRefused(f"{key}: nodes not collected in this tree: {dangling[:5]}")
        rows[key] = {"status": status, "evidence_level": level, "nodes": list(nodes)}
    return {"header": inv.REQUIRED_HEADER, "rows": rows}


def collect() -> set[str]:
    """Node ids from `pytest --collect-only -q` over the UC1 suites, run from haloflow/."""

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *COLLECT_TARGETS],
        capture_output=True, text=True, check=False, cwd=ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"collection failed with exit {result.returncode}")
    return _parse_collection(result.stdout)


def _parse_collection(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if "::" in line}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--collected", type=Path,
                        help="a saved `pytest --collect-only -q` output to check against")
    args = parser.parse_args(argv)
    try:
        if args.collected is not None:
            collected = _parse_collection(args.collected.read_text(encoding="utf-8"))
        else:
            collected = collect()
    except (OSError, RuntimeError) as error:
        print(f"uc1_trace: cannot collect: {error}", file=sys.stderr)
        return 2
    try:
        report = build_report(TRACE, collected)
    except TraceRefused as refused:
        print(f"uc1_trace: REFUSED: {refused.reason}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


# The literal trace. Holds are pending_hold with no evidence. Rows whose agreed id
# was renamed, split, merged or superseded cite the source document (Shared
# Workspace "Work Session" folders, or the project docs) in a comment.
TRACE: dict[str, TraceRow] = {
    # identities: item key (create digest), action key (action digest), command id never a dedupe
    # key (PA-22)
    "UC1-R1": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa22_retry_with_fresh_event_id_and_transport_fields_is_existing",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_mb3_v_action_digest_vectors",
            "tests/m05/test_m05_actions_postgres.py::test_mb3_v_stored_digest_equals_the_oracle",
            "tests/m05/test_m05_actions_postgres.py::test_mr02_same_key_different_payload_conflicts",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mb2_v_create_digest_vectors",
            "tests/m05/test_m05_create_postgres.py::test_mc02_exact_redelivery_is_existing_with_no_new_rows",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc03_same_source_and_kind_other_purpose_is_dedupe_conflict",  # noqa: E501
        ],
    },
    # idempotent creation (MC-01..03, MC-07, MB2-C)
    "UC1-R2": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mb2_c_concurrent_duplicate_converges",
            "tests/m05/test_m05_create_postgres.py::test_mc01_mapped_source_creates_item_and_history",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc02_exact_redelivery_is_existing_with_no_new_rows",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc03_same_source_and_kind_other_purpose_is_dedupe_conflict",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc07_second_distinct_source_creates_new_item_first_untouched",  # noqa: E501
        ],
    },
    # idempotent actions incl. authorization recheck on retry (MR-01..04, MA-08, SV-07d, RP-1..5)
    "UC1-R3": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma08_stale_version",
            "tests/m05/test_m05_actions_postgres.py::test_mr01_replay_after_intervening_actions_and_reassignment",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_mr01r_replay_refused_when_read_or_act_revoked",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_mr02_same_key_different_payload_conflicts",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_mr03_same_key_different_actor",
            "tests/m05/test_m05_resolve_postgres.py::test_rp1_rp2_exact_retry_replays[note-less]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp1_rp2_exact_retry_replays[note]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp3_rp5_changed_retry_conflicts[added]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp3_rp5_changed_retry_conflicts[outcome]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp3_rp5_changed_retry_conflicts[removed]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp3_rp5_changed_retry_conflicts[text]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp3_rp5_changed_retry_conflicts[type]",
            "tests/m05/test_m05_service_postgres.py::test_sv07d_replay_requires_current_actor_standing[deactivate]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07d_replay_requires_current_actor_standing[revoke_act]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07d_replay_requires_current_actor_standing[revoke_read]",  # noqa: E501
        ],
    },
    # atomicity incl. the M02 append (UC1-T2 settled as (b) by A-INV D1/D2)
    "UC1-R4": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_service_postgres.py::test_atom_domain_write_then_exception_neither_commits",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_s01_event_and_domain_state_commit_together",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_mb3_a_fault_at_record_insert_rolls_back_the_item[m05_followup_actions]",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_mb3_a_fault_at_record_insert_rolls_back_the_item[m05_followup_history]",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mb2_a_history_fault_rolls_back_the_item",
            "tests/m05/test_m05_resolve_postgres.py::test_rb1_rb9_fault_before_marker_rolls_back_everything[after-append]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb1_rb9_fault_before_marker_rolls_back_everything[after-apply]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[event_handoff_outbox]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_actions]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_history]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_items]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_notes]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb8_invalid_apply_output_rolls_back",
            "tests/m05/test_m05_resolve_postgres.py::test_rb_control_unfaulted_path_reaches_every_write",  # noqa: E501
        ],
    },
    # lifecycle, controlled outcome list, no reopen
    "UC1-R5": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma02a_claim_then_start_progress",
            "tests/m05/test_m05_actions_postgres.py::test_ma07_every_action_on_a_resolved_item",
            "tests/m05/test_m05_actions_postgres.py::test_mb3_pc_preconditions",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[item-str]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[note-control]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[note-not-str]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[note-too-long]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[other-empty-note]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[other-without-note]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[outcome-list]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[outcome-unknown]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[type-claim]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[version-bool]",
            "tests/m05/test_m05_resolve_postgres.py::test_ma03_claim_and_resolve_unassigned_one_history_row",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_ma07r_resolved_item_refused_no_reopen",
            "tests/m05/test_m05_resolve_postgres.py::test_ma_or_other_requires_note_in_sql_too",
        ],
    },
    # append-only history with actor/time/before/after
    "UC1-R6": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_privileges_and_rejector_trigger",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[DELETE-haloflow_m05_owner]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[DELETE-haloflow_runtime]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[UPDATE-haloflow_m05_owner]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[UPDATE-haloflow_runtime]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_trigger_predicate_negative_control",
            "tests/m05/test_m05_create_postgres.py::test_mb2_i_created_history_row_is_immutable",
            "tests/m05/test_m05_reads_postgres.py::test_mb3_r_get_history_exact_shape",
            "tests/m05/test_m05_resolve_postgres.py::test_hn1_history_note_id_on_every_entry",
        ],
    },
    # notes: bounded, encrypted, never in logs/M02 metadata -- DEVELOPER-TESTED with the test-only
    # key provider; production key custody remains the OD-D2 pending_hold row (KP-0 pins fail-
    # closed)
    "UC1-R7": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[500-code-points]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[bare-cr]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[crlf]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[empty-after-trim]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[escaped-json-pair]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[nfc]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[none]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[supplementary]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[trim]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[501-code-points]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[c0-bell]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[c0-tab-inside]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[c1-nel]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[escaped-json-lone-surrogate]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[lone-high-surrogate]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[lone-low-surrogate]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[not-str]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[u2028]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[u2029]",
            "tests/m05/test_m05_notes_units.py::test_d11_surrogate_note_refused_without_text_leak[escaped-json]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_d11_surrogate_note_refused_without_text_leak[high]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_d11_surrogate_note_refused_without_text_leak[low]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_kp0_production_provider_fails_closed",
            "tests/m05/test_m05_notes_units.py::test_ne1_round_trip_and_fresh_nonce",
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[action-key]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[ciphertext]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[item]",
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[nonce]",
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[note-id]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[tenant]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[version]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne3_wrong_key_fails_integrity",
            "tests/m05/test_m05_resolve_postgres.py::test_nr1_ordinary_reader_reads_note",
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[foreign-clinic]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[inactive]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[no-view]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[workload]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr3_missing_capability_route_denied",
            "tests/m05/test_m05_resolve_postgres.py::test_san1_canary_absent_everywhere",
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[driver-fault]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[key-unavailable]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[read-integrity]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[refused]",  # noqa: E501
        ],
    },
    # tenant scope; cross-tenant / inactive assignee refused; non-revealing not-found
    "UC1-R8": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa12_same_event_id_in_two_tenants_is_independent",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_a01_independent_context_for_unserved_tenant_refused_on_every_service",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_ma11_unknown_and_foreign_items_share_item_not_found",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-foreign]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-inactive]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-no_act]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-no_read]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-unknown]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-foreign]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-inactive]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-no_act]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-no_read]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-unknown]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-foreign]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-inactive]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-no_act]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-no_read]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-unknown]",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc04_ineligible_callers_and_sources_share_one_shape",  # noqa: E501
            "tests/m05/test_m05_reads_postgres.py::test_mb3_r_unknown_and_foreign_ids_share_item_not_found",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_other_tenant_event_identical_to_unknown",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr6_foreign_note_id_with_eligible_local_reader",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_xr3_resolver_a_rejects_other_tenant_item",
        ],
    },
    # authority -- covered at the DEVELOPER-TESTED level only (synthetic UUID principals and a
    # test staff directory). "Production staff identity requires L-1W" is NOT claimed: it stays
    # open as the L-1W pending_hold row below (kickoff v1 section 4: L-1W is a non-claim)
    "UC1-R9": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma04_start_progress_on_anothers_item",
            "tests/m05/test_m05_actions_postgres.py::test_ma06_assign_and_reassign_require_supervise",  # noqa: E501
            "tests/m05/test_m05_actions_postgres.py::test_ma13_permission_shapes_not_authorized",
            "tests/m05/test_m05_resolve_postgres.py::test_mr14_resolve_without_append_capability_route_denied",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr3_missing_capability_route_denied",
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[False-False]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[False-True]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[True-False]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[True-True]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv04_noncanonical_principal_refused_before_gateway[auth-too-long]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv04_noncanonical_principal_refused_before_gateway[auth-uppercase]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv04_noncanonical_principal_refused_before_gateway[not-uuid]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv04_noncanonical_principal_refused_before_gateway[uppercase]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_domain_refusals_in_sql[no-read]",
            "tests/m05/test_m05_service_postgres.py::test_sv05_domain_refusals_in_sql[no-supervise]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[act-no-apply]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[act-no-prepare]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[admin-no-admin]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[display-no-admin-no-read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[display-no-display]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[get-no-read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[assign-foreign]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[assign-inactive]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[assign-no_act]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[assign-no_read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[assign-unknown]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[claim-foreign]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[claim-inactive]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[claim-no_act]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[claim-no_read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[claim-unknown]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[reassign-foreign]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[reassign-inactive]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[reassign-no_act]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[reassign-no_read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[reassign-unknown]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[start_progress-foreign]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[start_progress-inactive]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[start_progress-no_act]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[start_progress-no_read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07b_actor_without_standing_refused[start_progress-unknown]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv08r_reader_without_standing_refused[foreign]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv08r_reader_without_standing_refused[inactive]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv08r_reader_without_standing_refused[no_read]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv08r_reader_without_standing_refused[unknown]",  # noqa: E501
            "tests/m05/test_m05_service_units.py::test_sv01_public_commands_have_no_trusted_fields",
        ],
    },
    # no SMS/EHR side effects; staff assertion only (live SMS / real EHR stay pending_hold)
    "UC1-R10": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_mx02_no_ehr_state_column_and_staff_assertion_check",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mx01_resolve_and_note_read_make_no_vendor_call",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mx01_spy_control",
        ],
    },
    # restart safety: real SIGKILL restart rows plus in-process unknown-commit recovery
    "UC1-R11": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr05a_claim_killed_after_commit_replays",
            "tests/m05/test_b5_restart_postgres.py::test_mr05b_noted_resolve_killed_after_commit_replays",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr05c_plain_resolve_killed_after_commit_replays",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr06a_claim_killed_in_tx_leaves_nothing_then_new",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr06b_noted_resolve_killed_in_tx_then_new",
            "tests/m05/test_b5_restart_postgres.py::test_mr07a_waiter_blocked_by_child1_then_replays",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr07b_child1_killed_while_waiter_blocked_then_new",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[commit_then_cancel]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[commit_then_raise]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[raise_no_commit]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv12_action_unknown_commit_then_recovery[commit_then_cancel]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv12_action_unknown_commit_then_recovery[commit_then_raise]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv12_action_unknown_commit_then_recovery[raise_no_commit]",  # noqa: E501
        ],
    },
    # versioned M05 consumer command with durable dedupe; producer transport deferred (not
    # claimed)
    "UC1-T1": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mb2_v_create_digest_vectors",
            "tests/m05/test_m05_create_postgres.py::test_mc01_mapped_source_creates_item_and_history",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc02_exact_redelivery_is_existing_with_no_new_rows",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc03_same_source_and_kind_other_purpose_is_dedupe_conflict",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc06_closed_schema_refused_as_malformed",
        ],
    },
    # items are test-created only: test intake refused in production; not end-to-end evidence
    "UC1-T3": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[test-intake-present-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[test-intake-present-last]",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_non_production_with_test_intake_row_is_accepted",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_production_with_test_intake_row_is_refused",  # noqa: E501
        ],
    },
    # EC-1..EC-3 evidence for M05 tables, functions and roles
    "UC1-T4": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_apply_action]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_create_item]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_get_history]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_get_item]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_list_items]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_prepare_action]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_read_note]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_actions]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_history]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_items]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_notes]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_intake_map]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_outcome_codes]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me03_m05_owner_executes_exactly_the_envelope",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me04_provisioning_before_role_revision_refuses",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mb2_g_m05_owner_effective_privileges",
            "tests/m05/test_m05_create_postgres.py::test_mb2_g_m05_owner_explicit_acl_entries",
            "tests/m05/test_m05_resolve_binding_postgres.py::test_gb4_exact_grant_set",
        ],
    },
    "PA-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa01_append_writes_event_links_and_one_signal_row",  # noqa: E501
        ],
    },
    # covered under another name per Shared Workspace 2026-10-31/claude_uc1-part-a-
    # closeout-v3-2026-10-31.md s3 ("PA-02 and PA-06d")
    "PA-02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_service_postgres.py::test_atom_domain_write_then_exception_neither_commits",  # noqa: E501
        ],
    },
    "PA-03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa03_nested_gateway_in_callback_unchanged_type",  # noqa: E501
        ],
    },
    "PA-04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_faults_postgres.py::test_pa04_stage_fault_rolls_back_and_prior_evidence_intact[after_event_insert]",  # noqa: E501
            "tests/m02/test_m02_part_a_faults_postgres.py::test_pa04_stage_fault_rolls_back_and_prior_evidence_intact[after_links]",  # noqa: E501
            "tests/m02/test_m02_part_a_faults_postgres.py::test_pa04_stage_fault_rolls_back_and_prior_evidence_intact[at_commit]",  # noqa: E501
            "tests/m02/test_m02_part_a_faults_postgres.py::test_pa04_stage_fault_rolls_back_and_prior_evidence_intact[inside_enqueue]",  # noqa: E501
        ],
    },
    # PA-05 rewritten as (a)+(b) by the v1r1 delta Delta3; rows PA-05a/PA-05b
    "PA-05": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_faults_postgres.py::test_pa05a_enqueue_omission_detected_by_outbox_oracle[mutant_no_enqueue]",  # noqa: E501
            "tests/m02/test_m02_part_a_faults_postgres.py::test_pa05a_enqueue_omission_detected_by_outbox_oracle[reviewed]",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa05b_callback_omission_detected_by_domain_oracle[mutant_skip_callback]",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa05b_callback_omission_detected_by_domain_oracle[reviewed]",  # noqa: E501
        ],
    },
    # split into PA-06a..e by the G1/G2 decision v1; PA-06d and cancellation under other names per
    # Shared Workspace 2026-10-31/claude_uc1-part-a-closeout-v3-2026-10-31.md s3
    "PA-06": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_service_postgres.py::test_atom_domain_write_then_exception_neither_commits",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_fobs1_post_marker_cancellation_is_unknown_then_retry_converges[cancel_no_commit-appended-0]",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_fobs1_post_marker_cancellation_is_unknown_then_retry_converges[commit_then_cancel-existing-1]",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_fobs1_pre_marker_cancellation_propagates_and_rolls_back",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa06ab_one_shot_commit_phase_fault_then_real_retry[commit_then_raise-existing-1]",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa06ab_one_shot_commit_phase_fault_then_real_retry[raise_no_commit-appended-0]",  # noqa: E501
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa06c_append_failure_policy",
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa06e_callback_database_exception_object_propagates",  # noqa: E501
        ],
    },
    "PA-07": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa07_equivalent_duplicate_returns_original",  # noqa: E501
        ],
    },
    "PA-08": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[causation]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[metadata]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[patient]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[principal]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[subject_link]",  # noqa: E501
        ],
    },
    "PA-09": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_inactive_type_refused",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[actor_field-patch8-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[extra_key-patch3-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[metadata_enum-patch6-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[metadata_extra-patch5-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[missing_correlation-patch7-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[operation_id-patch0-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[unknown_type-patch1-CONTRACT_UNSUPPORTED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[uppercase_uuid-patch4-COMMAND_MALFORMED]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa09_input_refusals[wrong_level_type-patch2-CONTRACT_UNSUPPORTED]",  # noqa: E501
        ],
    },
    "PA-10": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[group_unsatisfied]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[inactive]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[missing]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[principal]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[relationship]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[source_class]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_link_refusals[subject_kind]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10_principal_bound_target_accepts_matching_principal",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa10b_other_tenants_target_is_absent",  # noqa: E501
        ],
    },
    "PA-11": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa11_attribution_refusals[bad_kind-patch2]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa11_attribution_refusals[bad_purpose-patch1]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa11_attribution_refusals[extra_key-patch4]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa11_attribution_refusals[long_request_id-patch3]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa11_attribution_refusals[non_uuid_principal-patch0]",  # noqa: E501
        ],
    },
    "PA-12": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa12_same_event_id_in_two_tenants_is_independent",  # noqa: E501
        ],
    },
    "PA-13": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[integrity_alert-row_args3-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[m05_handoff-row_args1-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[m06_handoff-row_args2-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[no_signal_rule-row_args7-SIGNAL_RULE_REQUIRED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[non_observation_level-row_args6-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[projection_rule-row_args0-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[requires_operation-row_args5-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_active_contract_needing_uninstalled_route_refused[unknown_signal-row_args4-ROUTE_NOT_INSTALLED]",  # noqa: E501
            "tests/m02/test_m02_part_a_registry_units.py::test_pa13_inactive_contract_needing_uninstalled_route_is_ignored",  # noqa: E501
        ],
    },
    "PA-14": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_canon_units.py::test_pa14_acl_oracle_negative_controls[extra_role]",  # noqa: E501
            "tests/m02/test_m02_part_a_canon_units.py::test_pa14_acl_oracle_negative_controls[grant_option]",  # noqa: E501
            "tests/m02/test_m02_part_a_canon_units.py::test_pa14_acl_oracle_negative_controls[missing_executor]",  # noqa: E501
            "tests/m02/test_m02_part_a_canon_units.py::test_pa14_acl_oracle_negative_controls[null_acl]",  # noqa: E501
            "tests/m02/test_m02_part_a_canon_units.py::test_pa14_acl_oracle_negative_controls[public]",  # noqa: E501
            "tests/m02/test_m02_part_a_canon_units.py::test_pa14_acl_oracle_positive_control",
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa14_canon_serializer_not_executable_by_others[haloflow_m05_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa14_canon_serializer_not_executable_by_others[haloflow_runtime]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa14_function_owner_definer_path_acl[m02_append_observation]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa14_function_owner_definer_path_acl[m02_canon_v1]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa14_function_owner_definer_path_acl[m02_enqueue_event_handoffs]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa14_function_owner_definer_path_acl[m02_get_observation_envelope]",  # noqa: E501
        ],
    },
    "PA-15": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa15_append_owner_insert_sets_and_no_mutation",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa15_pa28_pa47_exact_select_grants[APPEND_OWNER-APPEND_OWNER_SELECT]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa15_pa28_pa47_exact_select_grants[READ_OWNER-READ_OWNER_SELECT]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa15_projection_owner_outbox_grants",  # noqa: E501
        ],
    },
    "PA-16": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa16_allowlisted_path_is_returned",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa16_non_allowlisted_stored_field_is_absent",  # noqa: E501
        ],
    },
    "PA-17": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa17_pa45_alh_inactive_type_unsupported_history_untouched",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa17_unknown_and_foreign_tenant_ids_are_null",  # noqa: E501
        ],
    },
    "PA-18": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa18_no_metadata_or_driver_text_in_logs",  # noqa: E501
        ],
    },
    "PA-19": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m02/test_m02_part_a_canon_units.py::test_pa19_pa26_oracle_vectors",
            "tests/m02/test_m02_part_a_canon_units.py::test_pa19_service_m11_key",
        ],
    },
    "PA-20": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa20_equivalent_collision_returns_existing_row_unchanged",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa20_incompatible_logical_collision_refused_seed_unchanged",  # noqa: E501
        ],
    },
    "PA-21": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m02/test_m02_part_a_canon_units.py::test_pa21_service_m11_key_rejects_bad_tenant",  # noqa: E501
        ],
    },
    "PA-22": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa22_retry_with_fresh_event_id_and_transport_fields_is_existing",  # noqa: E501
        ],
    },
    "PA-23": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa23_second_attempt_blocks_on_first_then_converges_by_dedupe_key",  # noqa: E501
        ],
    },
    # merged with PA-08 into one node (closeout s3)
    "PA-24": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[causation]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[metadata]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[patient]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[principal]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa08_pa24_same_key_changed_content_conflicts[subject_link]",  # noqa: E501
        ],
    },
    "PA-25": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa25_second_authorized_producer_same_key_is_independent",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa25_unauthorized_producer_refused_before_dedupe",  # noqa: E501
        ],
    },
    "PA-26": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa26_sql_fingerprint_matches_oracle",  # noqa: E501
            "tests/m02/test_m02_part_a_canon_units.py::test_pa19_pa26_oracle_vectors",
        ],
    },
    "PA-27": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa27_m05_owner_owns_nothing_in_part_a",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa27_role_is_nologin_with_exact_migrator_edge[haloflow_m02_append_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa27_role_is_nologin_with_exact_migrator_edge[haloflow_m02_projection_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa27_role_is_nologin_with_exact_migrator_edge[haloflow_m02_read_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa27_role_is_nologin_with_exact_migrator_edge[haloflow_m05_owner]",  # noqa: E501
        ],
    },
    "PA-28": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa15_pa28_pa47_exact_select_grants[APPEND_OWNER-APPEND_OWNER_SELECT]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa15_pa28_pa47_exact_select_grants[READ_OWNER-READ_OWNER_SELECT]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa28_runtime_has_no_table_privileges[event_contracts]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa28_runtime_has_no_table_privileges[event_handoff_outbox]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa28_runtime_has_no_table_privileges[event_types]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa28_runtime_has_no_table_privileges[m02_envelope_path_allowlist]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa28_runtime_has_no_table_privileges[patient_event_links]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa28_runtime_has_no_table_privileges[patient_events]",  # noqa: E501
        ],
    },
    "PA-29": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa29_malformed_tenant_setting[Bad Tenant]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa29_malformed_tenant_setting[]",
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa29_malformed_tenant_setting[x]",
        ],
    },
    "PA-30": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_service_postgres.py::test_pa30_suspended_tenant_refused_with_zero_append_dispatch",  # noqa: E501
        ],
    },
    # REPLACED: superseded by TB-4 rows PA-33..41 (claude_uc1-tenant-binding-canonicalization-
    # decision-v1-2026-10-30.md TB-4 "they replace ... PA-31"); supersession accepted by Codex
    # (codex_closeout-v1-gap-disposition.md); Shared Workspace 2026-10-31/claude_uc1-part-a-
    # closeout-v3-2026-10-31.md s3.1
    "PA-31": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa34_valid_looking_foreign_tenant_setting_is_mismatch",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa35_absent_registry_row_for_rendered_schema",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa36_inactive_registry_row[archival_pending]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa36_inactive_registry_row[archived]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa36_inactive_registry_row[decommissioned]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa36_inactive_registry_row[provisioning]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa36_inactive_registry_row[suspended]",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa37_temp_shadow_does_not_affect_binding",  # noqa: E501
            "tests/m02/test_m02_part_a_append_postgres.py::test_pa38_direct_enqueue_and_canon_denied_to_runtime",  # noqa: E501
            "tests/m02/test_m02_part_a_binding_postgres.py::test_pa40_control_both_tenants_are_distinct_and_active",  # noqa: E501
            "tests/m02/test_m02_part_a_binding_postgres.py::test_pa40_each_enqueue_body_is_its_own_schemas_rendering",  # noqa: E501
            "tests/m02/test_m02_part_a_binding_postgres.py::test_pa40_pa33_cross_binding_refused_after_a_registry_derived_valid_append[a_with_b_setting]",  # noqa: E501
            "tests/m02/test_m02_part_a_binding_postgres.py::test_pa40_pa33_cross_binding_refused_after_a_registry_derived_valid_append[b_with_a_setting]",  # noqa: E501
            "tests/m02/test_m02_part_a_binding_postgres.py::test_pa40_the_rendered_literal_is_the_only_tenant_reference",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa39_other_part_a_roles_have_no_shared_access[haloflow_m02_append_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa39_other_part_a_roles_have_no_shared_access[haloflow_m02_read_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa39_other_part_a_roles_have_no_shared_access[haloflow_m05_owner]",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa39_projection_owner_shared_tenants_exact_columns",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa41_shared_grants_grantor_is_registry_owner",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa41_staged_fresh_database_grants_before_any_tenant",  # noqa: E501
            "tests/m02/test_m02_part_a_catalog_postgres.py::test_pa41_supplementary_revision_text",
        ],
    },
    "PA-32": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_prerequisites_postgres.py::test_pa32_a_missing_m05_migrator_edge_is_refused_and_restored_exactly",  # noqa: E501
            "tests/m02/test_m02_part_a_prerequisites_postgres.py::test_pa32_a_missing_required_column_grant_is_refused",  # noqa: E501
            "tests/m02/test_m02_part_a_prerequisites_postgres.py::test_pa32_a_table_maintain_grant_is_refused_as_not_exact",  # noqa: E501
            "tests/m02/test_m02_part_a_prerequisites_postgres.py::test_pa32_a_table_wide_grant_is_refused_as_not_exact",  # noqa: E501
            "tests/m02/test_m02_part_a_prerequisites_postgres.py::test_pa32_staged_004_refused_then_005_provisions",  # noqa: E501
            "tests/m02/test_m02_part_a_prerequisites_postgres.py::test_pa32_the_guard_verifies_installed_state_not_the_revision_marker",  # noqa: E501
        ],
    },
    "MC-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc01_mapped_source_creates_item_and_history",  # noqa: E501
        ],
    },
    "MC-02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc02_exact_redelivery_is_existing_with_no_new_rows",  # noqa: E501
        ],
    },
    "MC-03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc03_same_source_and_kind_other_purpose_is_dedupe_conflict",  # noqa: E501
        ],
    },
    "MC-04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc04_ineligible_callers_and_sources_share_one_shape",  # noqa: E501
        ],
    },
    "MC-05": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc05_kind_mismatch_and_single_match_controls",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc05_unmapped_ambiguous_absent_and_null_patient_refused",  # noqa: E501
            "tests/m05/test_m05_create_postgres.py::test_mc05b_type_without_m05_allowlist_is_not_eligible",  # noqa: E501
        ],
    },
    "MC-06": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc06_closed_schema_refused_as_malformed",
        ],
    },
    "MC-07": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_create_postgres.py::test_mc07_second_distinct_source_creates_new_item_first_untouched",  # noqa: E501
        ],
    },
    # refusal lives in the D3 startup validator (B2 design delta v1 Delta4, test module docstring)
    "MC-08": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_intake_startup.py::test_mc08_existing_constructor_keeps_its_meaning",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_loader_production_valid_registry_is_accepted",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_loader_real_test_intake_row_reaches_test_intake_present",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_non_production_with_test_intake_row_is_accepted",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_one_test_row_among_production_rows_still_refuses",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_production_with_empty_or_production_intake_is_accepted",  # noqa: E501
            "tests/m05/test_m05_intake_startup.py::test_mc08_production_with_test_intake_row_is_refused",  # noqa: E501
        ],
    },
    # origin MA-01 is claim_and_resolve on an unassigned item -> ONE history entry; B4 relabelled
    # that case "MA-03" and reused "MA-01" for assignee-resolves-own (Shared Workspace
    # 2026-11-01/claude_uc1-b4-resolve-package-v1-2026-11-01.md s4). Both mapped here
    "MA-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_ma01_assignee_resolves_own_item[in_progress]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_ma01_assignee_resolves_own_item[open_assigned]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_ma03_claim_and_resolve_unassigned_one_history_row",  # noqa: E501
        ],
    },
    # split: claim->start_progress half = MA-02a (Shared Workspace 2026-10-31/claude_uc1-part-
    # b-b3-design-delta-v1-2026-10-31.md Delta1/Delta8); resolve half = B4 MA-01[in_progress]
    "MA-02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma02a_claim_then_start_progress",
            "tests/m05/test_m05_resolve_postgres.py::test_ma01_assignee_resolves_own_item[in_progress]",  # noqa: E501
        ],
    },
    # origin MA-03 = resolve with unknown outcome / `other` without a note -> refused; implemented
    # as RV-1 (service) and MA-OR (SQL NOTE_REQUIRED) per Shared Workspace
    # 2026-11-01/claude_uc1-b4-resolve-package-v1-2026-11-01.md s4 / Shared Workspace
    # 2026-11-01/claude_uc1-b4-resolve-package-v2-2026-11-01.md s1.1. A missing outcome is a
    # constructor TypeError (required field). Nodes named test_ma03_* are the B4 relabel (see
    # MA-01)
    "MA-03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[other-empty-note]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[other-without-note]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[outcome-list]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[outcome-unknown]",
            "tests/m05/test_m05_resolve_postgres.py::test_ma_or_other_requires_note_in_sql_too",
        ],
    },
    # start_progress half MA-04 (Shared Workspace 2026-10-31/claude_uc1-part-b-b3-design-
    # delta-v1-2026-10-31.md Delta8); resolve half MA-03s (Shared Workspace
    # 2026-11-01/claude_uc1-b4-resolve-package-v1-2026-11-01.md s4)
    "MA-04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma04_start_progress_on_anothers_item",
            "tests/m05/test_m05_resolve_postgres.py::test_ma03s_others_item_needs_supervise",
        ],
    },
    # supervisor resolve allowed = MA-03s (Shared Workspace 2026-11-01/claude_uc1-b4-resolve-
    # package-v1-2026-11-01.md s4). NOTE: the "history actor = supervisor" assertion is pinned by
    # MA-04b (start_progress) only; MA-03s asserts the supervisor resolve succeeds
    "MA-05": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma04_start_progress_on_anothers_item",
            "tests/m05/test_m05_resolve_postgres.py::test_ma03s_others_item_needs_supervise",
        ],
    },
    "MA-06": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma06_assign_and_reassign_require_supervise",  # noqa: E501
        ],
    },
    "MA-07": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma07_every_action_on_a_resolved_item",
            "tests/m05/test_m05_resolve_postgres.py::test_ma07r_resolved_item_refused_no_reopen",
        ],
    },
    "MA-08": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma08_stale_version",
        ],
    },
    "MA-09": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma09_concurrent_claim",
        ],
    },
    # retained by Rachel (directory first); MA-10 directory package v1+v1r1+v1r2
    "MA-10": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10a_eligible_targets_accepted",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-foreign]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-inactive]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-no_act]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-no_read]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[assign-unknown]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-foreign]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-inactive]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-no_act]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-no_read]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[claim-unknown]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-foreign]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-inactive]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-no_act]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-no_read]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10bcde_ineligible_target_refused[reassign-unknown]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10f_caller_assignee_fields_rejected[eligible_flag]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10f_caller_assignee_fields_rejected[permissions_object]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10g_assignable_not_executable_fails_closed",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10i_deactivated_assignee_item_visible_supervisor_reassigns",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10j_replay_after_target_deactivated",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[assign-m05_apply_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[assign-m05_apply_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[assign-m05_prepare_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[assign-m05_prepare_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[claim-m05_apply_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[claim-m05_apply_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[claim-m05_prepare_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[claim-m05_prepare_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[reassign-m05_apply_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[reassign-m05_apply_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[reassign-m05_prepare_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[reassign-m05_prepare_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[start_progress-m05_apply_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[start_progress-m05_apply_action-SERIALIZABLE]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[start_progress-m05_prepare_action-REPEATABLE READ]",  # noqa: E501
            "tests/m05/test_m05_assignee_eligibility_postgres.py::test_ma10k_isolation_guard[start_progress-m05_prepare_action-SERIALIZABLE]",  # noqa: E501
        ],
    },
    "MA-11": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma11_unknown_and_foreign_items_share_item_not_found",  # noqa: E501
            "tests/m05/test_m05_reads_postgres.py::test_mb3_r_unknown_and_foreign_ids_share_item_not_found",  # noqa: E501
        ],
    },
    # B3b row SV-05 (Shared Workspace 2026-11-01/claude_uc1-b3b-service-package-v2-2026-11-01.md
    # s3 matrix row 1: action route without m05.followup.prepare/apply -> RouteDenied
    # CAPABILITY_DENIED, zero gateway calls); "act" = m05.followup.apply in B3b
    "MA-12": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[act-no-apply]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[act-no-prepare]",  # noqa: E501
        ],
    },
    # MA-13" in Shared Workspace 2026-10-31/claude_uc1-part-b-b3-design-delta-v1-2026-10-31.md
    # Delta8 (SQL re-derivation) + SV-03 (booleans derived from context only)
    "MA-13": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma13_permission_shapes_not_authorized",
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[False-False]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[False-True]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[True-False]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[True-True]",  # noqa: E501
        ],
    },
    # REPLACED by B3b SV-01 "closed public commands" (Shared Workspace
    # 2026-11-01/claude_uc1-b3b-service-package-v2-2026-11-01.md preamble + s7): the public
    # command types have no permission-shaped field, so such a command cannot be constructed
    # (stronger than a runtime COMMAND_MALFORMED); SV-03 shows the SQL booleans come from the
    # context only
    "MA-14": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[False-False]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[False-True]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[True-False]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv03_sent_command_is_the_context_oracle[True-True]",  # noqa: E501
            "tests/m05/test_m05_service_units.py::test_sv01_public_commands_have_no_trusted_fields",
        ],
    },
    # expectation CHANGED by Shared Workspace 2026-11-01/claude_uc1-b3b-service-
    # package-v2-2026-11-01.md s3 matrix row 2: an action context with act but without read is
    # refused NOT_AUTHORIZED by the SQL at step 4 (prepare only, apply never runs), not
    # CAPABILITY_DENIED pre-SQL; the read route without read is RouteDenied pre-gateway; the
    # replay half is MR-01r
    "MA-15": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr01r_replay_refused_when_read_or_act_revoked",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv05_domain_refusals_in_sql[no-read]",
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[get-no-read]",  # noqa: E501
        ],
    },
    # supervise does not imply act: MA-13 case {read, supervise, not act} -> NOT_AUTHORIZED (SQL)
    # and the route check refuses any action context lacking prepare/apply. NOTE: no row builds a
    # context holding ONLY m05.followup.supervise; the invariant is evidenced by these two nodes
    "MA-16": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_ma13_permission_shapes_not_authorized",
            "tests/m05/test_m05_service_postgres.py::test_sv05_route_denied_before_any_gateway_call[act-no-apply]",  # noqa: E501
        ],
    },
    "MR-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr01_replay_after_intervening_actions_and_reassignment",  # noqa: E501
        ],
    },
    "MR-02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr02_same_key_different_payload_conflicts",  # noqa: E501
        ],
    },
    "MR-03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr03_same_key_different_actor",
        ],
    },
    # MR-04 + v1r1 Delta6 extension (read revoked / act revoked) = MR-01r (SQL) and SV-07d
    # (service, Shared Workspace 2026-11-01/claude_uc1-b3b-service-package-v2-2026-11-01.md s7)
    "MR-04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr01r_replay_refused_when_read_or_act_revoked",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07d_replay_requires_current_actor_standing[deactivate]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07d_replay_requires_current_actor_standing[revoke_act]",  # noqa: E501
            "tests/m05/test_m05_service_postgres.py::test_sv07d_replay_requires_current_actor_standing[revoke_read]",  # noqa: E501
        ],
    },
    # split MR-05a..c (Shared Workspace 2026-11-01/claude_uc1-b5-composition-startup-
    # package-v2-2026-11-01.md s6)
    "MR-05": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr05a_claim_killed_after_commit_replays",
            "tests/m05/test_b5_restart_postgres.py::test_mr05b_noted_resolve_killed_after_commit_replays",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr05c_plain_resolve_killed_after_commit_replays",  # noqa: E501
        ],
    },
    # split MR-06a/b (Shared Workspace 2026-11-01/claude_uc1-b5-composition-startup-
    # package-v2-2026-11-01.md s6)
    "MR-06": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr06a_claim_killed_in_tx_leaves_nothing_then_new",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr06b_noted_resolve_killed_in_tx_then_new",
        ],
    },
    # split MR-07a/b (Shared Workspace 2026-11-01/claude_uc1-b5-composition-startup-
    # package-v2-2026-11-01.md s6)
    "MR-07": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr07a_waiter_blocked_by_child1_then_replays",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr07b_child1_killed_while_waiter_blocked_then_new",  # noqa: E501
        ],
    },
    "MR-08": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_mr08_orphan_resolution_event_fails_closed",  # noqa: E501
        ],
    },
    "MR-09": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr09_direct_apply_equals_prepare_then_apply",  # noqa: E501
        ],
    },
    # MR-10a..n matrix (Shared Workspace 2026-11-01/claude_uc1-b4-resolve-package-v2-2026-11-01.md
    # s3/s7)
    "MR-10": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_control_valid_synthetic_event_applies",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_genuine_reuse_is_reused",
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[actor]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[causation]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[dedupe]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[level]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[links-extra]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[links-none]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[links-other-item]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[links-relationship]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[metadata-assertion]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[metadata-item-kind]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[metadata-missing-key]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[metadata-outcome]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[occurred-at-only]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[operation-id]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[patient]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[status]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[time-quality-only]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[type]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[version]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_other_tenant_event_identical_to_unknown",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_precedence_binding_before_reuse",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_real_other_type_event_is_invalid",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_unknown_event_is_invalid",
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_workload_principal_is_invalid",  # noqa: E501
        ],
    },
    "MR-11": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_actions_postgres.py::test_mr11_closed_schema_refused_as_malformed",
        ],
    },
    # origin MR-12 (v1r1 Delta5: faults after append / history / action / note -> full rollback) =
    # RB-1, RB-3..7, RB-9 (Shared Workspace 2026-11-01/claude_uc1-b4-resolve-
    # package-v2-2026-11-01.md s6). B4 v1 s4 reused the label for unknown-commit replay = RB-10
    "MR-12": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[commit_then_cancel]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[commit_then_raise]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[raise_no_commit]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb1_rb9_fault_before_marker_rolls_back_everything[after-append]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb1_rb9_fault_before_marker_rolls_back_everything[after-apply]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[event_handoff_outbox]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_actions]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_history]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_items]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_notes]",  # noqa: E501
        ],
    },
    # origin MR-13 (v1r1 Delta5: one differing field among patient / item_kind / status / level /
    # operation_id -> apply refuses) = the matching MR-10 matrix rows (Shared Workspace
    # 2026-11-01/claude_uc1-b4-resolve-package-v2-2026-11-01.md s3). B4 v1 s4 reused the label for
    # "not committed -> retry applies once" = RB-10[raise_no_commit]
    "MR-13": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[level]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[metadata-item-kind]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[operation-id]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[patient]",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mr10_one_predicate_false_is_invalid[status]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[raise_no_commit]",  # noqa: E501
        ],
    },
    # origin MR-14 (v1r1 Delta5: a resolve retry on the REPLAY and restart paths leaves exactly
    # one outbox row) = RP-1/2 (full-state unchanged on replay, outbox included), RB-10 and
    # MR-05b/c (exactly one outbox row). The node named test_mr14_* is the B4 v1 relabel (resolve
    # without append capability) and is mapped under UC1-R9
    "MR-14": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr05b_noted_resolve_killed_after_commit_replays",  # noqa: E501
            "tests/m05/test_b5_restart_postgres.py::test_mr05c_plain_resolve_killed_after_commit_replays",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[commit_then_cancel]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[commit_then_raise]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb10_unknown_commit_recovery_exactly_once[raise_no_commit]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rp1_rp2_exact_retry_replays[note-less]",
            "tests/m05/test_m05_resolve_postgres.py::test_rp1_rp2_exact_retry_replays[note]",
        ],
    },
    # = SAN-1/SAN-2 + NE-1 (Shared Workspace 2026-11-01/claude_uc1-b4-resolve-
    # package-v2-2026-11-01.md s7)
    "MN-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_notes_units.py::test_ne1_round_trip_and_fresh_nonce",
            "tests/m05/test_m05_resolve_postgres.py::test_san1_canary_absent_everywhere",
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[driver-fault]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[key-unavailable]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[read-integrity]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_san2_error_paths_carry_no_canary[refused]",  # noqa: E501
        ],
    },
    # = NH variants + D11-1..7 (Shared Workspace 2026-11-01/claude_uc1-b4-resolve-
    # package-v2-2026-11-01.md s1.1/s7): NFC/NFD, CRLF/LF and trailing-space variants give one
    # digest; REPLAY by equal digest is the RP-1/2 path (not mapped here, so the evidence level
    # stays unit)
    "MN-02": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[500-code-points]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[bare-cr]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[crlf]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[empty-after-trim]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[escaped-json-pair]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[nfc]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[none]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[supplementary]",
            "tests/m05/test_m05_notes_units.py::test_d11_normalization[trim]",
            "tests/m05/test_m05_notes_units.py::test_nh_variants_share_one_digest_after_normalization",  # noqa: E501
        ],
    },
    # = D11 refusals (501 cp, C0, U+2028) + RV-1
    "MN-03": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[501-code-points]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[c0-bell]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[c0-tab-inside]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[c1-nel]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[escaped-json-lone-surrogate]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[lone-high-surrogate]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[lone-low-surrogate]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[not-str]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[u2028]",
            "tests/m05/test_m05_notes_units.py::test_d11_refusals[u2029]",
            "tests/m05/test_m05_notes_units.py::test_d11_surrogate_note_refused_without_text_leak[escaped-json]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_d11_surrogate_note_refused_without_text_leak[high]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_d11_surrogate_note_refused_without_text_leak[low]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[note-control]",
            "tests/m05/test_m05_notes_units.py::test_rv1_resolve_action_malformed[note-too-long]",
        ],
    },
    # = NR-5 swapped ciphertext + NE-2/NE-3 -> NOTE_INTEGRITY_FAILED
    "MN-04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[action-key]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[ciphertext]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[item]",
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[nonce]",
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[note-id]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[tenant]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne2_any_binding_change_fails_integrity[version]",  # noqa: E501
            "tests/m05/test_m05_notes_units.py::test_ne3_wrong_key_fails_integrity",
            "tests/m05/test_m05_resolve_postgres.py::test_nr5_envelope_binding_and_size_corpus[item-id-mismatch]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr5_envelope_binding_and_size_corpus[swapped-ciphertext]",  # noqa: E501
        ],
    },
    # = NR-3 / NR-3b
    "MN-05": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_nr3_missing_capability_route_denied",
            "tests/m05/test_m05_resolve_postgres.py::test_nr3b_note_capability_without_read_route_denied",  # noqa: E501
        ],
    },
    # = RP-6
    "MN-06": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_rp6_rotated_key_retained_replays_with_stored_version",  # noqa: E501
        ],
    },
    # = RP-7
    "MN-07": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_rp7_retired_version_is_unverifiable",
        ],
    },
    # = RB rows: the full-state oracle includes m05_followup_notes, so a rolled-back resolve
    # leaves no note row
    "MN-08": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_rb1_rb9_fault_before_marker_rolls_back_everything[after-append]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb1_rb9_fault_before_marker_rolls_back_everything[after-apply]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[event_handoff_outbox]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_actions]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_history]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_items]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_rb3_rb7_write_fault_rolls_back_everything[m05_followup_notes]",  # noqa: E501
        ],
    },
    # origin MN-09 (v1r1 Delta5: foreign-tenant note_id -> ITEM_NOT_FOUND shape) = NR-6/NR-4; B4
    # v1 s4 relabel "reader standing per O-1" = NR-1/NR-2
    "MN-09": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_postgres.py::test_nr1_ordinary_reader_reads_note",
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[foreign-clinic]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[inactive]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[no-view]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr2_reader_without_standing_refused[workload]",  # noqa: E501
            "tests/m05/test_m05_resolve_postgres.py::test_nr4_unknown_note_is_item_not_found",
            "tests/m05/test_m05_resolve_postgres.py::test_nr6_foreign_note_id_with_eligible_local_reader",  # noqa: E501
        ],
    },
    "ME-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_apply_action]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_create_item]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_get_history]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_get_item]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_list_items]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_prepare_action]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_function_owner_definer_search_path_acl[m05_read_note]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me01_no_other_m05_function",
        ],
    },
    "ME-02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_me02_columns_exact[m05_followup_actions]",
            "tests/m05/test_m05_catalog_postgres.py::test_me02_columns_exact[m05_followup_history]",
            "tests/m05/test_m05_catalog_postgres.py::test_me02_columns_exact[m05_followup_items]",
            "tests/m05/test_m05_catalog_postgres.py::test_me02_columns_exact[m05_followup_notes]",
            "tests/m05/test_m05_catalog_postgres.py::test_me02_no_other_m05_table",
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_actions]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_history]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_items]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_followup_notes]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_intake_map]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me02_table_owner_and_no_runtime_privilege[m05_outcome_codes]",  # noqa: E501
        ],
    },
    "ME-03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m02/test_m02_part_a_m05_access_postgres.py::test_a4_m05_owner_executes_only_the_envelope",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me03_m05_owner_executes_exactly_the_envelope",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_gb4_me03_and_resolver_registration",  # noqa: E501
        ],
    },
    "ME-04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_me04_missing_m05_migrator_edge_refuses_before_any_unit",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me04_provisioning_before_role_revision_refuses",  # noqa: E501
        ],
    },
    "ME-05": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_privileges_and_rejector_trigger",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[DELETE-haloflow_m05_owner]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[DELETE-haloflow_runtime]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[UPDATE-haloflow_m05_owner]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_history_update_delete_refused_without_grant[UPDATE-haloflow_runtime]",  # noqa: E501
            "tests/m05/test_m05_catalog_postgres.py::test_me05_trigger_predicate_negative_control",
        ],
    },
    "MX-01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mx01_resolve_and_note_read_make_no_vendor_call",  # noqa: E501
            "tests/m05/test_m05_resolve_binding_postgres.py::test_mx01_spy_control",
        ],
    },
    "MX-02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_m05_catalog_postgres.py::test_mx02_no_ehr_state_column_and_staff_assertion_check",  # noqa: E501
        ],
    },
    "B5-C01": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c01_production_statement_set_is_exact",
        ],
    },
    "B5-C02": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c02_staff_keys_live_in_m01_statements_only",  # noqa: E501
        ],
    },
    "B5-C03": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c03_production_registry_units_and_target",  # noqa: E501
        ],
    },
    "B5-C04": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c04_execution_roles_are_exact",
        ],
    },
    "B5-C05": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c05_every_typed_unit_has_a_profile_and_nothing_else_does",  # noqa: E501
        ],
    },
    "B5-C06": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c06_supported_set_is_the_literal_25",
        ],
    },
    "B5-C07": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c07_control_the_production_registry_satisfies_the_rule",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_c07_generalized_profile_rule_refuses_a_profile_less_typed_unit[t009_m02_append]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_c07_generalized_profile_rule_refuses_a_profile_less_typed_unit[t015_m05_prepare_action]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_c07_generalized_profile_rule_refuses_a_profile_less_typed_unit[t022_m01_staff_assignable]",  # noqa: E501
        ],
    },
    "B5-C08": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c08_provisioning_manifest_changes_exactly_two_values",  # noqa: E501
        ],
    },
    "B5-C09": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c09_statement_catalogue_manifest_pins_the_exact_set",  # noqa: E501
        ],
    },
    "B5-C10": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_c10_ci_and_makefile_cover_startup",
        ],
    },
    "B5-I01": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[bool]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[int]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[lookalike-enum]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[none]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[str-nonprod]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[str-subclass]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i01_deployment_must_be_the_exact_enum[str]",  # noqa: E501
        ],
    },
    "B5-I02": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i02_deployment_has_no_default",
        ],
    },
    "B5-I03": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i03_signature_takes_no_override_parameters",  # noqa: E501
        ],
    },
    "B5-I04": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i04_empty_tenant_set_refused",
        ],
    },
    "B5-I05": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i05_duplicate_tenant_refused_not_deduplicated",  # noqa: E501
        ],
    },
    "B5-I06": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i06_tenant_collection_and_elements_must_be_exact[bare-str]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i06_tenant_collection_and_elements_must_be_exact[int-element]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i06_tenant_collection_and_elements_must_be_exact[none-element]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i06_tenant_collection_and_elements_must_be_exact[set]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i06_tenant_collection_and_elements_must_be_exact[str-subclass-element]",  # noqa: E501
        ],
    },
    "B5-I07": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i07_tenant_id_grammar_enforced[-tenant]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i07_tenant_id_grammar_enforced[Tenant-A1]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i07_tenant_id_grammar_enforced[a]",
            "tests/m05/test_b5_composition_units.py::test_b5_i07_tenant_id_grammar_enforced[tenant_a1]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_i07_tenant_id_grammar_enforced[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx]",  # noqa: E501
        ],
    },
    "B5-I08": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_i08_dependencies_must_be_the_exact_dataclass",  # noqa: E501
        ],
    },
    "B5-K01": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_k01_services_are_constructed_only_in_startup",  # noqa: E501
        ],
    },
    "B5-K02": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_k02_startup_fixes_the_producer_literal",  # noqa: E501
        ],
    },
    "B5-K03": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_k03_startup_wires_only_the_fail_closed_key_provider",  # noqa: E501
        ],
    },
    "B5-K04": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_k04_startup_and_composition_import_nothing_from_tests",  # noqa: E501
        ],
    },
    "B5-V01": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v01_full_activation_in_production_refused",  # noqa: E501
        ],
    },
    "B5-V02": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v02_full_activation_in_non_production_accepted",  # noqa: E501
        ],
    },
    "B5-V03": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[non_production-001]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[non_production-010]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[non_production-011]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[non_production-100]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[non_production-101]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[non_production-110]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[production-001]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[production-010]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[production-011]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[production-100]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[production-101]",  # noqa: E501
            "tests/m05/test_b5_composition_units.py::test_b5_v03_partial_activation_is_route_not_installed[production-110]",  # noqa: E501
        ],
    },
    "B5-V04": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v04_partial_with_test_family_in_production_is_linkage_first",  # noqa: E501
        ],
    },
    "B5-V05": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v05_full_with_test_family_in_production_is_m05_a1_first",  # noqa: E501
        ],
    },
    "B5-V06": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v06_full_with_synthetic_resolver_in_production_is_m05_a1_first",  # noqa: E501
        ],
    },
    "B5-V07": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v07_all_inactive_in_production_accepted",  # noqa: E501
        ],
    },
    "B5-V08": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v08_part_a_fixture_without_m05_rows_keeps_its_outcome",  # noqa: E501
        ],
    },
    "B5-V09": {
        "status": "covered", "evidence_level": "unit",
        "nodes": [
            "tests/m05/test_b5_composition_units.py::test_b5_v09_inactive_m05_with_test_family_keeps_the_existing_refusal",  # noqa: E501
        ],
    },
    "B5-S01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s01_two_valid_tenants_start_and_serve",
        ],
    },
    "B5-S02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[function-config-drift-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[function-config-drift-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-family-row-missing-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-family-row-missing-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-full-activation-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-full-activation-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-partial-activation-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-partial-activation-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-resolver-row-missing-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-resolver-row-missing-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-type-row-missing-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[m05-type-row-missing-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[suspended-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[suspended-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[synthetic-resolver-active-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[synthetic-resolver-active-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[test-family-active-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[test-family-active-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[test-intake-present-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[test-intake-present-last]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[version-11-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[version-11-last]",  # noqa: E501
        ],
    },
    "B5-S03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s03_non_production_accepts_full_m05_activation",  # noqa: E501
        ],
    },
    "B5-S04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s04_served_set_is_captured_once",
        ],
    },
    # SU-SNAPSHOT barrier, rows a (generation-row) and b (comparator-config)
    "B5-S15": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s15_commit_after_tenant_row_read_is_invisible[comparator-config]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s15_commit_after_tenant_row_read_is_invisible[generation-row]",  # noqa: E501
        ],
    },
    "B5-A01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_a01_independent_context_for_unserved_tenant_refused_on_every_service",  # noqa: E501
        ],
    },
    "B5-A02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_a02_facade_refuses_unserved_and_unknown_before_resolver_io",  # noqa: E501
        ],
    },
    "B5-A03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_a03_held_context_after_version_bump_observed_gateway_refusal",  # noqa: E501
        ],
    },
    "B5-A04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_a04_bundle_exposes_only_the_facade_and_admitted_services",  # noqa: E501
        ],
    },
    "B5-E01": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_e01_cleanup_failure_without_prior_failure_refuses",  # noqa: E501
        ],
    },
    "B5-E02": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_e02_non_idle_connection_refused_not_adopted",  # noqa: E501
        ],
    },
    "B5-E03": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_e03_factory_failure_is_sanitized",
        ],
    },
    "B5-E04": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_e04_cancellation_propagates_and_cleans_up",  # noqa: E501
        ],
    },
    # observation only: nothing it observes is approved behaviour (test module docstring)
    "COMPAT-B5a": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_compat_b5a_runner_over_an_11_unit_ledger_is_observed_only",  # noqa: E501
        ],
    },
    # "COMPAT-B5b is version 11 refused (the S-version row)" (test module docstring; B5 red gate
    # request v1)
    "COMPAT-B5b": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[version-11-first]",  # noqa: E501
            "tests/m05/test_b5_startup_postgres.py::test_b5_s02_one_bad_tenant_refuses_startup[version-11-last]",  # noqa: E501
        ],
    },
    # P-4 rows numbered in the Shared Workspace 2026-11-01/claude_uc1-b5-composition-startup-
    # package-v2r1-adoption-2026-11-01.md P-4 bullet order (same-target resumes / mismatched
    # refuses / active retained refuses)
    "P4-1": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_p4_control_same_target_interrupted_provisioning_resumes",  # noqa: E501
        ],
    },
    "P4-2": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_p4_mismatched_target_resume_refused_before_writes",  # noqa: E501
        ],
    },
    "P4-3": {
        "status": "covered", "evidence_level": "integration_pg17",
        "nodes": [
            "tests/m05/test_b5_startup_postgres.py::test_p4_active_retained_tenant_refused_before_writes",  # noqa: E501
        ],
    },
    "MR-05a": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr05a_claim_killed_after_commit_replays",
        ],
    },
    "MR-05b": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr05b_noted_resolve_killed_after_commit_replays",  # noqa: E501
        ],
    },
    "MR-05c": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr05c_plain_resolve_killed_after_commit_replays",  # noqa: E501
        ],
    },
    "MR-06a": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr06a_claim_killed_in_tx_leaves_nothing_then_new",  # noqa: E501
        ],
    },
    "MR-06b": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr06b_noted_resolve_killed_in_tx_then_new",
        ],
    },
    "MR-07a": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr07a_waiter_blocked_by_child1_then_replays",  # noqa: E501
        ],
    },
    "MR-07b": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr07b_child1_killed_while_waiter_blocked_then_new",  # noqa: E501
        ],
    },
    "MR-RC": {
        "status": "covered", "evidence_level": "process_restart",
        "nodes": [
            "tests/m05/test_b5_restart_postgres.py::test_mr_rc_unpaused_child_applies_like_in_process",  # noqa: E501
        ],
    },
    # release hold: production staff identity mapping (UC1-R9: requires L-1W)
    "L-1W": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
    # release hold: release hold L-1S
    "L-1S": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
    # release hold: production note-key custody and rotation (provider fails closed)
    "OD-D2": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
    # release hold: parked
    "L-6": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
    # release hold: human demo not yet run
    "A-1-HUMAN-DEMO": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
    # release hold: rehearsal / live SMS on hold
    "LIVE-SMS": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
    # release hold: real EHR writes NO-GO
    "REAL-EHR-WRITE": {"status": "pending_hold", "evidence_level": "none", "nodes": []},
}


if __name__ == "__main__":
    sys.exit(main())
