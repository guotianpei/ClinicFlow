"""Runtime seal scope of a synthetic install (Option B B3a, v1r1 D1.1-D1.4). SYNTHETIC ONLY.

Every table of the owned view belongs to exactly one of two explicit, disjoint inventories
(names `shared.<t>` / `{tenant}.<t>`):

- `RUNTIME_MUTABLE_TABLES`: row contents may change after the first runtime start, because a
  path the synthetic runtime COMPOSES writes them (Codex Q4 / Q-T2: classified by reachable
  composed write paths, then validated on a real B2 install: SC1). Their catalog definitions
  (columns, constraints, indexes, policies, grants) stay sealed.
- `INSTALL_SEALED_TABLES`: everything else, including every `shared.*` table (Q-T1:
  `shared.isolation_alerts` is sealed, so an alert makes the next start refuse), the
  `m01_staff*` tables, `m05_activation_records` and `m05_reminder_settings` (no staff-admin,
  activation or settings path is composed).

`sealed_digest(capture)` and `reduced_baseline(state)` are PURE (no database): the reduced
baseline is a projection of the state file's stored B2 seal and the pinned
`installed_profile.SEALED_READY_DIGEST`, never of the live database (mutant 11, U5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

RUNTIME_MUTABLE_TABLES: Final = frozenset({
    "{tenant}.event_handoff_outbox", "{tenant}.m05_appointment_profile",
    "{tenant}.m05_appointments", "{tenant}.m05_contact_state",
    "{tenant}.m05_followup_actions", "{tenant}.m05_followup_history",
    "{tenant}.m05_followup_items", "{tenant}.m05_followup_notes",
    "{tenant}.m05_inbound_replies", "{tenant}.m05_outbound_ops",
    "{tenant}.m05_outbound_render_inputs", "{tenant}.m05_outbound_schedule",
    "{tenant}.m05_outbound_send_tickets", "{tenant}.m05_patient_contacts",
    "{tenant}.m05_reminder_windows", "{tenant}.m05_writeback_baselines",
    "{tenant}.m05_writeback_ops", "{tenant}.m05_writeback_permits",
    "{tenant}.patient_event_links", "{tenant}.patient_events",
})

INSTALL_SEALED_TABLES: Final = frozenset({
    "shared.access_audit_log", "shared.isolation_alerts", "shared.schema_migrations",
    "shared.staff_identity_anchor", "shared.staff_identity_bindings",
    "shared.staff_identity_log", "shared.staff_sessions", "shared.support_access_grants",
    "shared.support_operator_logins", "shared.tenant_auth_settings",
    "shared.tenant_auth_settings_log", "shared.tenant_state_history", "shared.tenants",
    "{tenant}.access_audit_outbox", "{tenant}.event_contracts", "{tenant}.event_types",
    "{tenant}.m01_staff", "{tenant}.m01_staff_action_keys", "{tenant}.m01_staff_admin_log",
    "{tenant}.m01_staff_bootstrap_keys", "{tenant}.m01_staff_bootstrap_log",
    "{tenant}.m01_staff_followup_access", "{tenant}.m01_staff_role_guard",
    "{tenant}.m01_staff_roles", "{tenant}.m02_envelope_path_allowlist",
    "{tenant}.m02_link_resolvers", "{tenant}.m05_activation_records",
    "{tenant}.m05_intake_map", "{tenant}.m05_intake_workloads",
    "{tenant}.m05_outcome_codes", "{tenant}.m05_reminder_settings",
    "{tenant}.operation_registry", "{tenant}.ref_action_families",
    "{tenant}.ref_event_levels", "{tenant}.ref_event_statuses",
})

# Eligibility E4 (eligibility install profile only; a default install has none of these
# tables). Runtime-mutable: the tables the composed eligibility scheduler and the E3b staff
# actions write. Sealed: the eligibility settings, the priority payers and the E3b reference
# tables (no settings or reference write path is composed).
EL_RUNTIME_MUTABLE_TABLES: Final = frozenset({
    "{tenant}.m05_el_appointments", "{tenant}.m05_el_attempts", "{tenant}.m05_el_case_events",
    "{tenant}.m05_el_case_notes", "{tenant}.m05_el_cases", "{tenant}.m05_el_counts",
    "{tenant}.m05_el_coverage", "{tenant}.m05_el_late_answers", "{tenant}.m05_el_triggers",
    "{tenant}.m05_el_verifications", "{tenant}.m05_contact_entries",
    "{tenant}.m05_el_doc_ops",                     # E5: the documentation ops
})
# Eligibility E5: the SIMULATED chart store (schema `sim_chart`, eligibility profile only;
# outside the shared / tenant inventories). Written by the composed doc service at runtime.
SIM_CHART_RUNTIME_MUTABLE_TABLES: Final = frozenset({"sim_chart.permits", "sim_chart.entries"})
EL_INSTALL_SEALED_TABLES: Final = frozenset({
    "{tenant}.m05_el_settings", "{tenant}.m05_el_priority_payers",
    "{tenant}.m05_contact_codes", "{tenant}.m05_outcome_kinds",
})

# The token `view.capture` substitutes for the tenant schema in normalised names.
SCHEMA_TOKEN: Final = "<SCHEMA>"


def generic_table(name: str, schema: str | None = None) -> str:
    """`<schema>.<t>` / `<SCHEMA>.<t>` / `{tenant}.<t>` / `shared.<t>` -> inventory form."""

    nsp, _, rel = name.partition(".")
    if nsp in (SCHEMA_TOKEN, "{tenant}") or (schema is not None and nsp == schema):
        return "{tenant}." + rel
    return name


def is_runtime_mutable(name: str, schema: str | None = None) -> bool:
    table = generic_table(name, schema)
    return (table in RUNTIME_MUTABLE_TABLES or table in EL_RUNTIME_MUTABLE_TABLES
            or table in SIM_CHART_RUNTIME_MUTABLE_TABLES)


def sealed_view(normalised: Mapping[str, Any]) -> dict[str, Any]:
    """The normalised owned view minus the ROW CONTENT keys (`rows:<table>`) of the
    runtime-mutable tables. Their catalog entries (relations, columns, constraints, indexes,
    triggers, policies, sequence definitions and ownership) are kept."""

    return {k: v for k, v in normalised.items()
            if not (k.startswith("rows:") and is_runtime_mutable(k[len("rows:"):]))}


def sealed_digest(capture: Any) -> str:
    """sha256 of the sort_keys JSON of `sealed_view(capture.normalised)` (the encoding of the
    B2 profile digest). `SEALED_READY_DIGEST` is this value at the ready point."""

    return hashlib.sha256(json.dumps(sealed_view(capture.normalised),
                                     sort_keys=True).encode()).hexdigest()


def reduced_baseline(state: Mapping[str, Any]) -> dict[str, Any]:
    """Pure projection of the stored B2 seal (`state["seal"]`): the same-install values of
    the sealed tables only. Drops ONLY the `nondet` entries of runtime-mutable tables;
    identities and ledger are kept unchanged."""

    seal = state["seal"]
    return {"nondet": {table: rows for table, rows in seal["nondet"].items()
                       if not is_runtime_mutable(table)},
            "identities": seal["identities"], "ledger": seal["ledger"]}
