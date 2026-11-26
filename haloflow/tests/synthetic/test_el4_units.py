"""Eligibility E4 unit rows U01-U09 (no database). RED BY DESIGN on merged E3b (3a8b884).

Governing: `claude_e4-test-packet-draft-v1.md` section 3 (Units) + v1r1 R3 (U01 extended to
the pre-profile state file). Classes C / B / A as `el4_support` states; each row names its
class. Interface pins: `el4_support` module docstring.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import el4_support as e4
import pytest

# ---------------------------------------------------------------------------- U01 profile parse

def _raw(**changes: Any) -> bytes:
    value = json.loads(e4.PRE_E4_STATE.read_bytes())
    value.update(changes)
    return json.dumps(value, sort_keys=True).encode("utf-8")


def test_el4_u01_fixture_is_pre_e4_shape() -> None:
    """C. The committed pre-E4 state file has exactly the baseline STATE_KEYS and no profile
    field (bound to a real baseline `init` by PostgreSQL row P01d-0)."""
    value = json.loads(e4.PRE_E4_STATE.read_bytes())
    assert set(value) == set(e4.mod("provision").STATE_KEYS), sorted(value)
    assert e4.STATE_FIELD not in value


def test_el4_u01_fieldless_file_reads_default() -> None:
    """A. A field-less (pre-E4) state file reads as profile `default`."""
    profiles = e4.mod("profiles")
    assert profiles.read_profile(e4.PRE_E4_STATE.read_bytes()) == e4.DEFAULT


def test_el4_u01_explicit_values() -> None:
    """A. An explicit `default` equals the field-less reading; `eligibility` is read only
    from an explicit field."""
    profiles = e4.mod("profiles")
    # FX3a (red packet v1 section 4): the closed set gains the fax profile (target 144)
    assert profiles.PROFILES == (e4.DEFAULT, e4.ELIGIBILITY, "fax")
    assert profiles.STATE_FIELD == e4.STATE_FIELD
    assert profiles.TARGETS == {e4.DEFAULT: e4.DEFAULT_TARGET,
                                e4.ELIGIBILITY: e4.ELIGIBILITY_TARGET, "fax": 149}  # FX4-G04
    assert profiles.read_profile(_raw(install_profile="default")) == e4.DEFAULT
    assert profiles.read_profile(_raw(install_profile="eligibility")) == e4.ELIGIBILITY


@pytest.mark.parametrize("bad", ["Eligibility", "eligibility ", "", "full", 1, True, None,
                                 ["eligibility"], {"p": "eligibility"}])
def test_el4_u01_invalid_value_refused(bad: Any) -> None:
    """A. Unknown value or wrong type -> PROFILE_INVALID (closed code, never the value)."""
    profiles = e4.mod("profiles")
    with pytest.raises(e4.mod("errors").SyntheticRefused) as err:
        profiles.read_profile(_raw(install_profile=bad))
    assert err.value.code == "PROFILE_INVALID"
    if type(bad) is str and bad.strip():
        assert bad.strip() not in str(err.value), str(err.value)


def test_el4_u01_duplicate_field_refused() -> None:
    """A. The field given twice in the JSON object -> PROFILE_INVALID (json.loads would
    silently keep the last; the parser must not)."""
    profiles = e4.mod("profiles")
    body = e4.PRE_E4_STATE.read_bytes().rstrip(b"}")
    raw = body + b',"install_profile":"default","install_profile":"eligibility"}'
    with pytest.raises(e4.mod("errors").SyntheticRefused) as err:
        profiles.read_profile(raw)
    assert err.value.code == "PROFILE_INVALID"


@pytest.mark.parametrize(("installed", "requested", "ok"), [
    ("default", "default", True), ("eligibility", "eligibility", True),
    ("default", "eligibility", False), ("eligibility", "default", False)])
def test_el4_u01_request_check(installed: str, requested: str, ok: bool) -> None:
    """A. check_request: equal -> None; different -> PROFILE_MISMATCH."""
    profiles = e4.mod("profiles")
    if ok:
        assert profiles.check_request(installed, requested) is None
    else:
        with pytest.raises(e4.mod("errors").SyntheticRefused) as err:
            profiles.check_request(installed, requested)
        assert err.value.code == "PROFILE_MISMATCH"


def test_el4_u01_registry_targets() -> None:
    """A. registry(profile).target_version: default 97 (unchanged), eligibility 126; the
    default registry is the one `build_synthetic_registry()` builds."""
    profiles = e4.mod("profiles")
    comp = e4.mod("composition")
    assert profiles.registry(e4.DEFAULT).target_version == e4.DEFAULT_TARGET
    assert profiles.registry(e4.ELIGIBILITY).target_version == e4.ELIGIBILITY_TARGET
    assert comp.build_synthetic_registry().target_version == e4.DEFAULT_TARGET
    # OD-EL12 (Codex t127 ALIGN): the eligibility registry is E3b's (126) plus t127.
    full = comp.build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True,
                                         include_el3b=True, include_el4=True)
    assert full.target_version == e4.E4_TARGET == 127
    # Eligibility E5 (declared pin move): the eligibility profile composes E5 on top (135:
    # t128..t134 + t135, the doc item display-state read of the 2026-11-24 REVISE).
    el5 = comp.build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True,
                                        include_el3b=True, include_el4=True, include_el5=True)
    assert profiles.registry(e4.ELIGIBILITY).target_version == el5.target_version == 135


def test_el4_u01_baseline_registries_control() -> None:
    """C. The baseline builders already give 97 (default) and 126 (el1..el3b)."""
    comp = e4.mod("composition")
    assert comp.build_synthetic_registry().target_version == e4.DEFAULT_TARGET
    assert comp.build_synthetic_registry(include_el1=True, include_el2=True, include_el3a=True,
                                         include_el3b=True).target_version \
        == e4.E3B_TARGET


# ---------------------------------------------------------------------------- U02 schedulers

def test_el4_u02_scheduler_sets() -> None:
    """A. schedulers(default) is the B5 set; schedulers(eligibility) adds `eligibility`."""
    profiles = e4.mod("profiles")
    assert profiles.schedulers(e4.DEFAULT) == e4.B5_SCHEDULERS
    assert profiles.schedulers(e4.ELIGIBILITY) == e4.EL_SCHEDULERS


def test_el4_u02_default_set_control() -> None:
    """C. The baseline managed set is the B5 set (default profile keeps it)."""
    assert e4.mod("sim_actions").SCHEDULERS == e4.B5_SCHEDULERS


# ---------------------------------------------------------------------------- U03 controls

GOOD_CONTROLS: list[tuple[Any, tuple[str, Any]]] = [
    ({"member": {"member_id": "SYN-E1-001", "status": "active"}},
     ("member", ("SYN-E1-001", "active", None))),
    ({"member": {"member_id": "SYN-E1-002", "status": "inactive"}},
     ("member", ("SYN-E1-002", "inactive", None))),
    ({"member": {"member_id": "SYN-E1-003", "status": "error",
                 "error": "subscriber_not_found"}},
     ("member", ("SYN-E1-003", "error", "subscriber_not_found"))),
    ({"fail_next": 0}, ("fail_next", 0)),
    ({"fail_next": 2}, ("fail_next", 2)),
    ({"malformed_next": 10}, ("malformed_next", 10)),
]
BAD_CONTROLS: list[Any] = [
    None, [], "fail_next", {}, {"fail_next": 1, "malformed_next": 1},
    {"fail_next": -1}, {"fail_next": 11}, {"fail_next": True}, {"fail_next": 1.0},
    {"fail_next": "2"}, {"malformed_next": None}, {"unknown": 1},
    {"member": {"member_id": "M000001", "status": "active"}},          # not synthetic
    {"member": {"member_id": "syn-e1-001", "status": "active"}},
    {"member": {"member_id": "SYN-E1-001", "status": "lapsed"}},       # unknown status
    {"member": {"member_id": "SYN-E1-001", "status": "error"}},        # error code missing
    {"member": {"member_id": "SYN-E1-001", "status": "active", "error": "subscriber_not_found"}},
    {"member": {"member_id": "SYN-E1-001", "status": "error", "error": "payer_down"}},
    {"member": {"member_id": "SYN-E1-001", "status": "active", "extra": 1}},
    {"member": {"member_id": "SYN-E1-001"}},
    {"member": "SYN-E1-001"},
    # Codex 2026-11-23 REVISE: an explicit null error code is malformed at parse (appended last:
    # the M-E4-7 declared rows value12/value13 keep their ids).
    {"member": {"member_id": "SYN-E1-001", "status": "error", "error": None}},
    {"member": {"member_id": "SYN-E1-001", "status": "active", "error": None}},
]


@pytest.mark.parametrize(("value", "want"), GOOD_CONTROLS)
def test_el4_u03_control_accepts(value: Any, want: tuple[str, Any]) -> None:
    """A. The clearinghouse control validator accepts exactly the pinned shapes."""
    assert e4.mod("sim_clearinghouse").parse(value) == want


@pytest.mark.parametrize("value", BAD_CONTROLS)
def test_el4_u03_control_refuses(value: Any) -> None:
    """A. Everything else -> ControlMalformed (a ValueError), never a partial parse."""
    sc = e4.mod("sim_clearinghouse")
    assert issubclass(sc.ControlMalformed, ValueError)
    with pytest.raises(sc.ControlMalformed):
        sc.parse(value)


# ---------------------------------------------------------------------------- U04 fixtures

def test_el4_u04_scenario_names() -> None:
    """A. NAMES = S1..S4 + E1..E5, in that order (the S scenarios unchanged)."""
    assert ("S1", "S2", "S3", "S4", *e4.EL_SCENARIOS) == e4.mod("scenarios").NAMES


def test_el4_u04_s_scenarios_control() -> None:
    """C. The four B5 scenarios load from their fixtures today."""
    fx = e4.mod("scenarios").fixtures()
    assert {"S1", "S2", "S3", "S4"} <= set(fx)


def test_el4_u04_e_fixture_shapes() -> None:
    """A. Each E fixture loads; each row carries a coverage block of the E1 shape with the
    pinned state; present blocks carry a synthetic member id and a synthetic payer; S rows
    carry coverage None."""
    sc = e4.mod("scenarios")
    fx = sc.fixtures()
    for name in ("S1", "S2", "S3", "S4"):
        assert all(r.coverage is None for r in fx[name].rows), name
    for name, (state, payer_class, _completeness) in e4.EL_SCENARIO_SHAPE.items():
        rows = fx[name].rows
        assert rows, name
        for r in rows:
            cov = dict(r.coverage)
            assert cov["state"] == state, (name, cov)
            if state != "present":
                assert set(cov) == {"state"}, (name, cov)
                continue
            assert set(cov) == e4.COVERAGE_PRESENT_KEYS, (name, sorted(cov))
            if cov["member_id"] is not None:
                assert e4.MEMBER_ID.fullmatch(cov["member_id"]), (name, cov["member_id"])
            want = e4.PRIORITY_PAYERS[0] if payer_class == "priority" else e4.NONPRIORITY_PAYER
            assert cov["payer_id"] == want, (name, cov["payer_id"])


def _e_row(**cov: Any) -> dict[str, Any]:
    raw = json.loads(e4.SCENARIO_DIR.joinpath("E1.json").read_text(encoding="utf-8"))
    raw["rows"][0]["coverage"].update(cov)
    return raw


@pytest.mark.parametrize("cov", [
    {"member_id": "M000001"}, {"member_id": "SYN"}, {"payer_id": "REAL-PAYER"},
    {"patient_dob": "07/04/1931"}, {"state": "present", "extra": 1}, {"state": "unknown"}])
def test_el4_u04_coverage_refused(cov: dict[str, Any]) -> None:
    """A. A non-synthetic member id or payer, a malformed dob, an extra key or an unknown
    state -> ScenarioInvalid("coverage")."""
    sc = e4.mod("scenarios")
    with pytest.raises(sc.ScenarioInvalid) as err:
        sc.validate(_e_row(**cov))
    assert err.value.field == "coverage"


# ---------------------------------------------------------------------------- U05 labels

def test_el4_u05_kind_titles() -> None:
    """A. ui.KIND_LABELS carries the OD-EL11 titles byte-exact; the five B4 kinds unchanged."""
    ui = e4.mod("ui")
    for kind, title in e4.EL_TITLES.items():
        assert ui.KIND_LABELS.get(kind) == title, (kind, ui.KIND_LABELS.get(kind))
    # Eligibility E5 (declared pin move, 2026-11-24): + owner decision 4's doc-check title.
    assert len(ui.KIND_LABELS) == 9
    assert ui.KIND_LABELS["eligibility_doc_check"] == "Chart entry needs a check"


def test_el4_u05_eligibility_label_tables_control() -> None:
    """C. The installed E3a/E3b label tables equal the approved OD-EL8/6/7 copies."""
    from haloflow.m05 import eligibility_labels as lab

    assert dict(lab.REASON_LABELS) == e4.EL_REASONS
    assert dict(lab.OUTCOME_LABELS) == e4.EL_OUTCOMES
    assert dict(lab.CONTACT_LABELS) == e4.EL_CONTACTS


def test_el4_u05_unknown_kind_unrecognized_control() -> None:
    """C (kills M08 at green). An unknown code renders "Unrecognized", never the raw code."""
    ui = e4.mod("ui")
    assert ui.label(ui.KIND_LABELS, "eligibility_unknown_kind") == ui.UNRECOGNIZED


# ---------------------------------------------------------------------------- U06 view token

def test_el4_u06_token_round_trip() -> None:
    """A. The E3b view token survives hidden-field render + parse exactly."""
    ui = e4.mod("ui")
    case_id = "0b8e7f3c-1d2a-4c5b-8e9f-0a1b2c3d4e5f"
    item_id = "1c9f8a4d-2e3b-4d6c-9f0a-1b2c3d4e5f60"
    fields = ui.case_token_fields(case_id, item_id, 7)
    assert [n for n, _ in fields] == ["case_id", "item_id", "case_version"]
    assert ui.parse_case_token(dict(fields)) == (case_id, item_id, 7)


@pytest.mark.parametrize("bad", [
    {}, {"case_id": "x", "item_id": "y", "case_version": "1"},
    {"case_id": "0b8e7f3c-1d2a-4c5b-8e9f-0a1b2c3d4e5f",
     "item_id": "1c9f8a4d-2e3b-4d6c-9f0a-1b2c3d4e5f60", "case_version": "0"},
    {"case_id": "0b8e7f3c-1d2a-4c5b-8e9f-0a1b2c3d4e5f",
     "item_id": "1c9f8a4d-2e3b-4d6c-9f0a-1b2c3d4e5f60", "case_version": "01"},
    {"case_id": "0b8e7f3c-1d2a-4c5b-8e9f-0a1b2c3d4e5f",
     "item_id": "1c9f8a4d-2e3b-4d6c-9f0a-1b2c3d4e5f60"}])
def test_el4_u06_token_malformed(bad: dict[str, str]) -> None:
    """A. Any missing or malformed token part -> ValueError (refused before the service)."""
    with pytest.raises(ValueError):
        e4.mod("ui").parse_case_token(bad)


# ---------------------------------------------------------------------------- U07 reload message

def test_el4_u07_reload_message_control() -> None:
    """C. The OD-B4-2 conflict wording is the reload message, byte-exact."""
    assert e4.mod("ui").MESSAGES["conflict"] == e4.CASE_CHANGED_MESSAGE


def test_el4_u07_case_changed_maps_to_conflict() -> None:
    """A. CASE_CHANGED maps to the conflict message (never to success)."""
    assert e4.mod("ui")._SERVICE_MESSAGES.get("CASE_CHANGED") == "conflict"


def test_el4_u11_summary_renderer_shows_only_od_el4_fields() -> None:
    """A (Codex codex_e4-m06-review.md; kills M-E4-6). The screen's summary renderer selects the
    OD-EL4 fields itself: an approved field renders; a coinsurance sentinel handed to it (label
    and value) never does, whatever the upstream projection returns."""
    el_ui = e4.mod("el_ui")
    sentinel = "SYN-COINS-SENTINEL-20"
    text = "".join(el_ui._summary_lines({"payer_name": "Synthetic Payer A",
                                         "coinsurance": sentinel}))
    assert "Payer name: Synthetic Payer A" in text
    assert sentinel not in text
    assert "coinsurance" not in text.lower()


# ---------------------------------------------------------------------------- U08 routes

def _routes(profile: str | None) -> set[tuple[str, str]]:
    lc_cls = e4.mod("lifecycle").SyntheticLifecycle
    env = {"HALOFLOW_SYNTHETIC_STATE": "/nonexistent"}
    lc = lc_cls(env) if profile is None else lc_cls(env, profile=profile)
    app = e4.mod("app").create_synthetic_app(lifecycle=lc)
    out: set[tuple[str, str]] = set()
    for r in app.routes:
        for m in getattr(r, "methods", None) or ():
            out.add((m, r.path))
    return out


EL4_ROUTES = {("POST", e4.CLEARINGHOUSE_ROUTE), ("POST", e4.CLEARINGHOUSE_FORM),
              ("POST", e4.UI_PREFIX + "/{item_id}/contact"),
              ("POST", e4.UI_PREFIX + "/{item_id}/close")}
# Eligibility E5 (EL-12; declared pin moves): the read-only simulated chart page (R9.1) and the
# staff action route are the routes E5 adds, eligibility profile only.
EL5_ROUTES = {("GET", "/synthetic/ui/chart/{appointment_ref}"),
              # E5 REVISE 2026-11-24 (declared pin move, Codex-aligned): the staff action
              # "Recorded in chart manually" on the open doc item.
              ("POST", e4.UI_PREFIX + "/{item_id}/chart-recorded")}


def test_el4_u08_default_inventory_control() -> None:
    """C. The baseline app serves none of the E4 routes."""
    assert not (_routes(None) & EL4_ROUTES)


def test_el4_u08_inventory_per_profile() -> None:
    """A. Default profile: the baseline inventory exactly. Eligibility profile: that plus
    exactly the four E4 routes."""
    base = _routes(None)
    assert _routes(e4.DEFAULT) == base
    assert _routes(e4.ELIGIBILITY) == base | EL4_ROUTES | EL5_ROUTES


# ---------------------------------------------------------------------------- U09 logs

def test_el4_u09_control_logs_codes_only(caplog: pytest.LogCaptureFixture) -> None:
    """A. Parsing controls (accepted and refused) logs no member id, status value or count
    supplied by the caller; only fixed text."""
    sc = e4.mod("sim_clearinghouse")
    caplog.set_level(logging.DEBUG, logger="haloflow")
    for value in ({"member": {"member_id": e4.CANARY["member_id"], "status": "inactive"}},
                  {"member": {"member_id": e4.CANARY["member_id"] + "X", "status": "nope"}},
                  {"fail_next": 7}):
        with contextlib.suppress(sc.ControlMalformed):
            sc.parse(value)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert e4.CANARY["member_id"] not in text and "inactive" not in text, text


# ---------------------------------------------------------------------------- P02c / P07

PRODUCTION_ROOTS = ("haloflow.platform.app", "haloflow.startup", "haloflow.composition",
                    "haloflow.main")
ELIGIBILITY_MODULE_PREFIXES = ("haloflow.m05.eligibility", "haloflow.m05.simulated_clearinghouse",
                               "haloflow.synthetic")


def test_el4_p02c_production_closure_excludes_eligibility_control() -> None:
    """C (extends B1-C1). The production import closure reaches no eligibility module, no
    simulated clearinghouse and nothing synthetic."""
    import fence

    result = fence.closure(PRODUCTION_ROOTS, fence.Tree(e4.s.SRC))
    reached = sorted(m for m in result.modules if m.startswith(ELIGIBILITY_MODULE_PREFIXES))
    assert reached == [], [result.path_to(m, PRODUCTION_ROOTS) for m in reached]
    assert result.unresolved == []


def test_el4_p07_outstanding_ehr_documentation_row() -> None:
    """A (E4-P07, OD-EL5). The delivery tracker carries the outstanding acceptance row
    "simulated EHR documentation — not built (OD-EL5)"."""
    tracker = e4.REPO / "documentation" / "HaloFlow_Module_Delivery_Tracker.md"
    assert e4.OUTSTANDING_EHR_ROW in tracker.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- U10 t127 (OD-EL12)

def test_el4_u10_contact_history_statement_pin() -> None:
    """A (OD-EL12, Codex t127 ALIGN). The statement is a READ under the B4 READ capability,
    calling exactly m05_el_contact_history(item_id); composed only in the eligibility
    catalogue, never the default one."""
    from haloflow.m01.statements import StatementMode
    from haloflow.m05.writeback_statements import READ_CAPABILITY

    st = e4.mod("el4_statements").EL4_STATEMENTS
    assert set(st) == {e4.CONTACT_HISTORY_KEY}
    mode, cap, query = st[e4.CONTACT_HISTORY_KEY]
    assert (mode, cap) == (StatementMode.READ, READ_CAPABILITY)
    assert query == "SELECT m05_el_contact_history(%(item_id)s::uuid)"
    profiles = e4.mod("profiles")
    el_keys = profiles.catalog(e4.ELIGIBILITY).catalog.keys()
    default_keys = profiles.catalog(e4.DEFAULT).catalog.keys()
    assert e4.CONTACT_HISTORY_KEY in el_keys and e4.CONTACT_HISTORY_KEY not in default_keys


def test_el4_u10_contact_history_unit_pin() -> None:
    """A. One unit t127: a SECURITY DEFINER STABLE function owned by the M05 owner, EXECUTE
    for the runtime role only, reading m05_contact_entries / m05_followup_history and writing
    nothing; composed only in the eligibility registry."""
    units = e4.mod("el4_units")
    assert set(units.EL4_TENANT_MIGRATIONS) == {e4.CONTACT_HISTORY_ID}
    unit = units.EL4_TENANT_MIGRATIONS[e4.CONTACT_HISTORY_ID]
    (fn,) = unit.policy["functions"]
    assert (fn["name"], fn["security_definer"], fn["volatility"]) == (
        "m05_el_contact_history", True, "stable")
    assert fn["acl"] == [{"grantee": "haloflow_runtime", "privileges": ["EXECUTE"]}]
    body = unit.policy_verification["functions"][0]["body"]
    assert "m05_contact_entries" in body and "m05_followup_history" in body
    for verb in ("INSERT", "UPDATE", "DELETE", "GRANT"):
        assert verb not in body.upper().replace("UPDATED", ""), verb
    profiles = e4.mod("profiles")
    el_ids = profiles.registry(e4.ELIGIBILITY).migration_ids
    # Eligibility E5 (declared pin move): t127 is the last E4 unit; E5's t128..t135 follow it.
    assert el_ids[e4.E4_TARGET - 1] == e4.CONTACT_HISTORY_ID
    assert len(el_ids) == e4.ELIGIBILITY_TARGET
    assert e4.CONTACT_HISTORY_ID not in profiles.registry(e4.DEFAULT).migration_ids
