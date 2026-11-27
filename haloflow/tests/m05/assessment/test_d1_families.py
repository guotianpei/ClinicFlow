"""D1 fixture families F01a..F71d (test packet v2r1 "D1 fixture inputs and expected values").

One parametrised test; parameter id == v2r1 row id (S2). Each row states its input delta from
the base fixture and the expected question fields and aggregate profile from v2r1. The
`Trace boundaries` clauses (->D2/D3/D4) are not asserted here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import d1_support as s
import pytest

Check = Callable[[ModuleType, dict[str, Any], dict[str, Any]], None]


@dataclass(frozen=True)
class Row:
    row_id: str
    mutations: tuple[s.Mutation, ...]
    check: Check
    zone_over: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ mutation helpers


def ctx(**over: Any) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["context"].update(over)
    return m


def at(instant: str) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["evaluation_instant"] = instant
    return m


def need(qtype: str, **kw: Any) -> s.Mutation:
    return lambda req: s.require(req, qtype, **kw)


def add_ev(*items: dict[str, Any]) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["evidence"].extend(items)
    return m


def set_ev(*items: dict[str, Any]) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["evidence"] = list(items)
    return m


def route(qtype: str, value: str) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["snapshots"]["routes"][qtype] = value
    return m


def permit(*action_codes: str) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["snapshots"]["permissions"].extend(action_codes)
    return m


def add_rule(r: dict[str, Any]) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["snapshots"]["policy"]["rules"].append(r)
    return m


def add_template(tpl: dict[str, Any]) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["snapshots"]["policy"]["templates"].append(tpl)
    return m


def obligation(**o: Any) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        base: dict[str, Any] = {"type": "verification", "question_type": "B", "scope": {},
                                "state": "open", "due": None, "review": None,
                                "recovery": None, "contact": None}
        base.update(o)
        req["work"]["obligations"].append(base)
    return m


def disposition(**d: Any) -> s.Mutation:
    def m(req: dict[str, Any]) -> None:
        req["work"]["dispositions"].append(d)
    return m


def inactive_m() -> s.Mutation:
    return set_ev(s.membership(value={"kind": "inactive"}))


DISPOSE_RULE = s.rule("OBL-DISPOSE", "obligation",
                      s.expr("eq", left=s.field("question.finding"), right=s.lit("inactive")),
                      {"outcome": "disposition", "queue": "clinic"},
                      {"outcome": "unresolved", "reason": "finding_unknown"},
                      types=("M",), priority=50)
HANDOFF_RULE = s.rule("OBL-HANDOFF", "obligation",
                      s.expr("eq", left=s.field("question.finding"), right=s.lit("required")),
                      {"outcome": "handoff_auth", "queue": "authorization"},
                      {"outcome": "unresolved", "reason": "finding_unknown"},
                      types=("A",), priority=50)
CLARIFY_RULE = s.rule("OBL-CLARIFY", "obligation",
                      s.expr("eq", left=s.field("question.applicability"),
                             right=s.lit("unresolved")),
                      {"outcome": "clarification", "queue": "verification"},
                      {"outcome": "unresolved", "reason": "applicability_unknown"},
                      types=("policy",), priority=50)
REVIEW_RULE = s.rule("OBL-REVIEW", "obligation",
                     s.expr("ge", left=s.field("evaluation_instant"),
                            right=s.field("obligation.review")),
                     {"outcome": "review_due", "queue": "verification"},
                     {"outcome": "waiting", "reason": "review_not_set"},
                     types=("B",), priority=50)


def b_ev(value: dict[str, Any], evidence_id: str = "E-B1", **over: Any) -> dict[str, Any]:
    scope = {"service_id": "S1", "service_date": "2026-11-30", "payer": "PAYER_A",
             "product": "PROD_A"}
    scope.update(over.pop("scope", {}))
    return s.evidence(evidence_id, "B", value, scope=scope, **over)


# ------------------------------------------------------------------ check helpers


def m_unchanged(result: dict[str, Any]) -> dict[str, Any]:
    q = s.assert_q(result, "M", "active", "adequate", "current_member")
    assert q["accepted"] == ["E-M1"], q
    return q


def gaps(result: dict[str, Any]) -> int:
    return sum(1 for q in result["questions"] if q["requiredness"] == "required"
               and q["adequacy"] not in ("adequate", "justified"))


def checks(*fns: Callable[[dict[str, Any]], Any], profile: str | None = None,
           action: str | None = None) -> Check:
    def check(pkg: ModuleType, result: dict[str, Any], req: dict[str, Any]) -> None:
        for fn in fns:
            fn(result)
        if profile is not None:
            s.assert_profile(result, profile, action)
    return check


def q(qtype: str, finding: str, adequacy: str, reason: str,
      **scope: Any) -> Callable[[dict[str, Any]], Any]:
    return lambda r: s.assert_q(r, qtype, finding, adequacy, reason, **scope)


# ------------------------------------------------------------------ custom checks


def _f02a(r: dict[str, Any]) -> None:
    assert r["completion"] is False and r["follow_up"] == "undetermined"
    assert s.codes(r) == ["CHECK_B"]
    assert (r["actions"][0]["mode"], r["actions"][0]["state"]) == ("system", "ready")
    assert [(o["type"], o["state"]) for o in s.open_obligations(r)] == [
        ("verification", "ready")]


def _f03a(r: dict[str, Any]) -> None:
    d = s.assert_q(r, "deductible", "unknown", "informational", "optional_absent")
    assert d.get("amount") is None


def _no_procedure(r: dict[str, Any]) -> None:
    """Category-level only: no procedure, code or diagnosis question is invented."""
    assert {q_["type"] for q_ in r["questions"]} == {"M"}, r["questions"]


def _f05b(r: dict[str, Any]) -> None:
    _no_procedure(r)
    assert "unconfirmed_order" in json.dumps(r["trace"])


def _f06a(r: dict[str, Any]) -> None:
    s.assert_q(r, "category", "unknown", "unresolved", "ambiguous_category")
    assert "annual physical" in json.dumps(r["trace"])


def _f07a(r: dict[str, Any]) -> None:
    m_unchanged(r)
    s.assert_q(r, "B", "unknown", "missing", "new_service", service_id="S2")


def _f08_common(r: dict[str, Any]) -> list[dict[str, Any]]:
    s.assert_q(r, "B", "covered", "adequate", "scope_match", service_id="S1", coverage_id="V1")
    for svc, cov in (("S1", "V2"), ("S2", "V1"), ("S2", "V2")):
        s.assert_q(r, "B", "unknown", "missing", "no_matching_scope",
                   service_id=svc, coverage_id=cov)
    by_key = {o["key"]: o for o in r["obligations"]}
    verify = [a for a in r["actions"] if a["code"] == "VERIFY"]
    order = [(by_key[a["obligation_key"]]["scope"]["service_id"],
              by_key[a["obligation_key"]]["scope"]["coverage_id"]) for a in verify]
    assert order == [("S1", "V2"), ("S2", "V1"), ("S2", "V2")], order
    return verify


def _f08a(r: dict[str, Any]) -> None:
    verify = _f08_common(r)
    assert s.codes(r) == ["VERIFY", "VERIFY", "VERIFY"]
    assert all(a["state"] == "ready" for a in verify)
    assert r["completion"] is False and r["follow_up"] == "yes"
    assert len([o for o in s.open_obligations(r) if o["type"] == "verification"]) == 3


def _f08b(r: dict[str, Any]) -> None:
    s.assert_q(r, "priority", "unknown", "unresolved", "order_unknown")
    verify = _f08_common(r)
    assert s.codes(r) == ["CLARIFY_PRIORITY", "VERIFY", "VERIFY", "VERIFY"]
    assert r["actions"][0]["state"] == "ready"
    assert all(a["state"] == "blocked" for a in verify)
    assert r["follow_up"] == "yes" and r["completion"] is False


def _f11(r: dict[str, Any]) -> None:
    m = s.assert_q(r, "M", "disputed", "conflicting", "opposed_sources")
    assert sorted(m["accepted"]) == ["E-M1", "E-M2"], m  # both retained; neither wins
    assert m["rejected"] == []


def _rejected(evidence_id: str, reason: str) -> Callable[[dict[str, Any]], None]:
    def fn(r: dict[str, Any]) -> None:
        m = s.question(r, "M")
        assert {"evidence_id": evidence_id, "reason": reason}.items() <= next(
            e for e in m["rejected"] if e["evidence_id"] == evidence_id).items()
    return fn


def _f13a(r: dict[str, Any]) -> None:
    s.assert_q(r, "M", "inactive", "adequate", "current_negative")
    s.assert_q(r, "B", "inapplicable", "justified", "dependency_inactive")
    s.assert_q(r, "A", "inapplicable", "justified", "dependency_inactive")


def _f37c(r: dict[str, Any]) -> None:
    m = s.assert_q(r, "M", "unknown", "insufficient", "environment_mismatch")
    rej = next(e for e in m["rejected"] if e["evidence_id"] == "E-M1")
    assert rej["environment"] == "sandbox"


def _x(code: str) -> Callable[[dict[str, Any]], None]:
    def fn(r: dict[str, Any]) -> None:
        assert r["rejection"] == {"code": code}, r["rejection"]
    return fn


def _f41a(r: dict[str, Any]) -> None:
    s.assert_q(r, "M", "unknown", "insufficient", "subject_mismatch")
    assert "P2" not in json.dumps(r["questions"])


def _f43a(r: dict[str, Any]) -> None:
    m = s.assert_q(r, "M", "active", "adequate", "current_member")
    assert sorted(m["linked_origins"]) == ["emr-sim", "staff-feed"]
    assert m["accepted"] == ["E-M1"]  # one independent observation, lineage U-M1


def _f45a(r: dict[str, Any]) -> None:
    s.assert_q(r, "N", "unknown", "missing", "participation_absent")
    b = s.question(r, "B")
    assert b["amount"] == {"minor": 2500, "currency": "USD", "period": "visit",
                           "scope": "individual", "tier": "in_network"}


def _f47a(r: dict[str, Any]) -> None:
    s.assert_q(r, "policy", "unknown", "unresolved", "applicability_unknown")
    assert not [q_ for q_ in r["questions"] if q_["requiredness"] == "required"
                and q_["adequacy"] == "adequate"]


def _local_date(value: str) -> Callable[[dict[str, Any]], None]:
    def fn(r: dict[str, Any]) -> None:
        assert r["trace"]["evaluation_local_date"] == value
    return fn


def _f48e(r: dict[str, Any]) -> None:
    assert r["trace"]["evaluation_local_date"] == "2027-03-14"
    assert r["trace"]["evaluation_local_time"] == "03:00:00"


def _f48f(r: dict[str, Any]) -> None:
    m_unchanged(r)
    assert [o["type"] for o in s.open_obligations(r)] == ["documentation"]
    assert gaps(r) == 0


def _f51a(r: dict[str, Any]) -> None:
    m_unchanged(r)
    assert r["trace"]["criteria"], "trace must link every evaluated criterion"
    assert all(c["rule_id"] and c["rule_version"] for c in r["trace"]["criteria"])


def _f51c(r: dict[str, Any]) -> None:
    m_unchanged(r)
    exc = r["configuration_exception"]
    assert exc is not None and exc["rule_ids"] == ["ACC-N-1", "ACC-N-2"]
    assert r["completion"] is False and r["actions"] == []
    assert r["primary_state"] == "blocked" and r["follow_up"] == "yes"
    s.assert_q(r, "B", "unknown", "missing", "answer_absent")


def _f52a(r: dict[str, Any]) -> None:
    s.assert_q(r, "M", "active", "stale", "window_expired")
    assert any(c["rule_id"] == "FRS-AGE" and c["rule_version"] == 2
               for c in r["trace"]["criteria"])


def _f67b(r: dict[str, Any]) -> None:
    s.assert_q(r, "A", "required", "adequate", "authorization_required")
    assert r["completion"] is True and r["follow_up"] == "yes"
    assert s.codes(r) == ["HANDOFF_AUTH"]
    assert [(o["type"], o["state"], o["queue"]) for o in s.open_obligations(r)] == [
        ("handoff_auth", "ready", "authorization")]
    assert not any("SUBMIT" in c for c in s.codes(r))


def _f68a(r: dict[str, Any]) -> None:
    s.assert_q(r, "B", "unknown", "missing", "awaiting_reply")
    assert r["completion"] is False and r["follow_up"] == "yes"
    assert s.codes(r) == ["WAIT_REPLY"]
    [o] = s.open_obligations(r)
    assert (o["type"], o["state"], o["review"]) == (
        "verification", "waiting", "2026-11-27T17:00:00.000000Z")


def _f68b(r: dict[str, Any]) -> None:
    s.assert_q(r, "B", "unknown", "missing", "review_due")
    assert s.codes(r) == ["REVIEW_REPLY"] and r["follow_up"] == "yes"
    [o] = s.open_obligations(r)
    assert (o["type"], o["state"]) == ("verification", "ready")


def _f70b(r: dict[str, Any]) -> None:
    s.assert_q(r, "M", "inactive", "adequate", "current_negative")
    closed = [o for o in r["obligations"] if o["state"] == "closed"]
    assert [o["basis"] for o in closed] == ["disposition"]


def _f70c(r: dict[str, Any]) -> None:
    s.assert_q(r, "B", "unknown", "missing", "answer_absent")
    assert r["completion"] is False and r["follow_up"] == "no"
    assert r["actions"] == [] and r["primary_state"] == "none"
    assert [o["basis"] for o in r["obligations"] if o["state"] == "closed"] == ["disposition"]


def _f71a(r: dict[str, Any]) -> None:
    s.assert_q(r, "B", "unknown", "missing", "answer_absent")
    s.assert_q(r, "N", "unknown", "missing", "recovery_pending")
    assert s.codes(r) == ["VERIFY", "DOCUMENT", "WAIT_AUTO"] and r["follow_up"] == "yes"
    assert gaps(r) == 2


def _f71b(r: dict[str, Any]) -> None:
    s.assert_q(r, "B", "covered", "adequate", "current_benefit")
    s.assert_q(r, "N", "unknown", "missing", "recovery_pending")
    assert s.codes(r) == ["DOCUMENT", "WAIT_AUTO"] and r["follow_up"] == "yes"
    assert gaps(r) == 1


def _f71c(r: dict[str, Any]) -> None:
    _f71b(r)
    assert len([o for o in s.open_obligations(r) if o["type"] == "documentation"]) == 1
    handoffs = [o for o in r["obligations"] if o["type"] == "handoff_auth"]
    assert [o["state"] for o in handoffs] == ["closed"]


# ------------------------------------------------------------------ rows

B_COVERED = b_ev({"kind": "covered"})
CHART_FAIL = obligation(type="documentation", question_type="documentation",
                        scope={"destination": "chart"}, state="open")

ROWS: list[Row] = [
    Row("F01a", (), checks(m_unchanged, profile="C")),
    Row("F02a", (need("B"), route("B", "automatic_available"), permit("CHECK_B")),
        checks(m_unchanged, q("B", "unknown", "missing", "answer_absent"), _f02a)),
    Row("F02b", (need("B"),),
        checks(m_unchanged, q("B", "unknown", "missing", "answer_absent"), profile="G")),
    Row("F03a", (need("deductible", requiredness="informational"),),
        checks(m_unchanged, _f03a, profile="C")),
    Row("F04a", (need("B"), add_ev(b_ev({"kind": "generic_membership"},
                                        requested_scope={"service_id": "S1"}))),
        checks(q("M", "active", "adequate", "current_member"),
               q("B", "unknown", "missing", "generic_return"),
               lambda r: "CHECK_B" not in s.codes(r), profile="G")),
    Row("F05a", (ctx(stated_reason="cough", booked_category="consult"),),
        checks(m_unchanged, _no_procedure, profile="C")),
    Row("F05b", (ctx(stated_reason="cough", suggestions=[
        {"service_category": "imaging", "source": "model", "basis": "cough"}]),),
        checks(m_unchanged, _f05b, profile="C")),
    Row("F06a", (ctx(booked_category="annual physical", stated_reason="annual physical",
                     services=[{"service_id": "S1", "category": "annual physical",
                                "confirmed": True, "origin": "emr-sim", "codes": []}]),
                 add_template({"template_id": "ambiguous-annual", "category": "annual physical",
                               "payer": None, "ambiguous": True,
                               "questions": [{"type": "category", "requiredness": "required",
                                              "depends_on": []}]})),
        checks(_f06a, profile="G", action="CLARIFY_CATEGORY")),
    Row("F07a", (ctx(appointment_version=2, services=[
        {"service_id": "S1", "category": "consult", "confirmed": True, "origin": "emr-sim",
         "codes": []},
        {"service_id": "S2", "category": "procedure", "confirmed": True, "origin": "emr-sim",
         "codes": ["PROC-1"]}]),
                 add_template({"template_id": "procedure", "category": "procedure",
                               "payer": None, "questions": [
                                   {"type": "B", "requiredness": "required",
                                    "depends_on": ["M"]}]})),
        checks(_f07a, profile="G")),
]

_TWO_SERVICES = ctx(services=[
    {"service_id": "S1", "category": "consult", "confirmed": True, "origin": "emr-sim",
     "codes": []},
    {"service_id": "S2", "category": "consult", "confirmed": True, "origin": "emr-sim",
     "codes": []}])
_TWO_COVERAGES = ctx(coverages=[
    {"coverage_id": "V1", "version": 1, "payer": "PAYER_A", "product": "PROD_A",
     "relationship": "self", "priority": 1},
    {"coverage_id": "V2", "version": 1, "payer": "PAYER_B", "product": "PROD_B",
     "relationship": "self", "priority": 2}])
_M_V2 = add_ev(s.membership(evidence_id="E-M-V2", upstream_txn="U-M-V2",
                            subject={"patient": "P", "coverage_id": "V2",
                                     "coverage_version": 1},
                            scope={"service_date": "2026-11-30", "payer": "PAYER_B",
                                   "product": "PROD_B"}))
_B_S1V1 = add_ev(b_ev({"kind": "covered"}, scope={"coverage_id": "V1"}))
_F08_DUE = (
    obligation(scope={"service_id": "S1", "coverage_id": "V2"}, due="2026-11-27T15:00:00.000000Z"),
    obligation(scope={"service_id": "S2", "coverage_id": "V1"}, due="2026-11-27T16:00:00.000000Z"),
    obligation(scope={"service_id": "S2", "coverage_id": "V2"}, due="2026-11-27T17:00:00.000000Z"),
)
_UNKNOWN_ORDER = ctx(coverages=[
    {"coverage_id": "V1", "version": 1, "payer": "PAYER_A", "product": "PROD_A",
     "relationship": "self", "priority": None},
    {"coverage_id": "V2", "version": 1, "payer": "PAYER_B", "product": "PROD_B",
     "relationship": "self", "priority": None}])

ROWS += [
    Row("F08a", (_TWO_SERVICES, _TWO_COVERAGES, need("B"), _M_V2, _B_S1V1, *_F08_DUE),
        checks(_f08a)),
    Row("F08b", (_TWO_SERVICES, _UNKNOWN_ORDER, need("priority", depends_on=()),
                 need("B", depends_on=("M", "priority")), _M_V2, _B_S1V1, *_F08_DUE),
        checks(_f08b)),
    Row("F09a", (set_ev(s.membership(observed_at="2026-11-18T17:00:00.000000Z",
                                     retrieved_at=s.EVAL)),
                 need("B"), add_ev(B_COVERED)),
        checks(q("M", "active", "stale", "window_expired"),
               q("B", "covered", "adequate", "current_benefit"), profile="G")),
    Row("F10a", (ctx(appointment_version=2, provider="R2"), need("N"),
                 add_ev(s.evidence("E-N1", "N", {"kind": "participating"},
                                   scope={"provider": "R1", "site": "H1",
                                          "service_date": "2026-11-30", "payer": "PAYER_A",
                                          "product": "PROD_A"}))),
        checks(m_unchanged, q("N", "unknown", "insufficient", "provider_mismatch"),
               profile="G")),
    Row("F11-D1", (add_ev(s.membership(evidence_id="E-M2", upstream_txn="U-M2",
                                       origin="emr-sim-2", value={"kind": "inactive"})),),
        checks(_f11, profile="G", action="RESOLVE_CONFLICT")),
    Row("F12a", (add_ev(s.membership(evidence_id="E-M3", upstream_txn="U-M3",
                                     value={"kind": "inactive"},
                                     scope={"service_date": "2026-12-01", "payer": "PAYER_A",
                                            "product": "PROD_A"},
                                     valid_from="2026-12-01", valid_to="2026-12-01")),),
        checks(m_unchanged, _rejected("E-M3", "service_date_mismatch"), profile="C")),
    Row("F12b", (add_ev(s.membership(evidence_id="E-M4", upstream_txn="U-M4",
                                     value={"kind": "inactive"},
                                     scope={"service_date": "2026-11-30", "payer": "PAYER_B",
                                            "product": "PROD_B"})),),
        checks(m_unchanged, _rejected("E-M4", "product_mismatch"), profile="C")),
    Row("F13a", (inactive_m(), need("B"), need("A")), checks(_f13a, profile="C")),
    Row("F13b", (inactive_m(), need("B"), need("A"), add_rule(DISPOSE_RULE)),
        checks(_f13a, profile="S")),
    Row("F14a", (need("B"),), checks(q("B", "unknown", "missing", "answer_absent"),
                                     profile="G")),
    Row("F14b", (need("B"), add_ev(b_ev({"kind": "unsupported"}))),
        checks(q("B", "unsupported", "insufficient", "operation_unsupported"), profile="G")),
    Row("F14c", (need("B"), add_ev(b_ev({"kind": "unavailable", "cause": "timeout"}))),
        checks(q("B", "unavailable", "insufficient", "attempt_timeout"), profile="G")),
    Row("F14d", (need("B"), add_ev(b_ev({"kind": "excluded"}))),
        checks(q("B", "excluded", "adequate", "explicit_exclusion"), profile="C")),
    Row("F15a", (ctx(site={"unknown": "not_recorded"}), need("A"),
                 add_ev(s.evidence("E-A1", "A", {"kind": "conditional",
                                                 "conditions": {"site": "H"}},
                                   scope={"service_id": "S1", "service_date": "2026-11-30",
                                          "payer": "PAYER_A", "product": "PROD_A"}))),
        checks(q("A", "conditional", "insufficient", "site_unknown"),
               lambda r: "approved" not in json.dumps(s.question(r, "A")),
               profile="G", action="CLARIFY_SITE")),
    Row("F37a", (ctx(environment="live"),), checks(_x("environment_mismatch"), profile="X")),
    Row("F37b", (ctx(environment="live"), set_ev(s.membership(environment="sandbox"))),
        checks(_x("environment_mismatch"), profile="X")),
    Row("F37c", (set_ev(s.membership(environment="sandbox")),), checks(_f37c, profile="G")),
    Row("F41a", (set_ev(s.membership(subject={"patient": "P2", "coverage_id": "V1",
                                              "coverage_version": 1})),),
        checks(_f41a, profile="G")),
    Row("F41b", (set_ev(s.membership(subject={"patient": "P", "coverage_id": "V1",
                                              "coverage_version": 2})),),
        checks(q("M", "unknown", "insufficient", "coverage_version_mismatch"), profile="G")),
    Row("F41c", (need("B"), add_ev(b_ev({"kind": "covered"}, scope={"service_id": "S2"}))),
        checks(q("B", "unknown", "insufficient", "service_mismatch", service_id="S1"),
               profile="G")),
    Row("F43a", (add_ev(s.membership(evidence_id="E-M1-staff", origin="staff-feed",
                                     source_class="staff", upstream_txn="U-M1")),),
        checks(_f43a, profile="C")),
]

_COPAY = add_ev(b_ev({"kind": "covered"}, amount={"minor": 2500, "currency": "USD",
                                                  "period": "visit", "scope": "individual",
                                                  "tier": "in_network"}))

ROWS += [
    Row("F45a", (need("N"), need("B", requiredness="informational"), _COPAY),
        checks(_f45a, profile="G")),
    Row("F45b", (need("N"), need("B", requiredness="informational"), _COPAY,
                 need("utilization", requiredness="informational")),
        checks(_f45a, q("utilization", "unknown", "informational", "optional_absent"),
               profile="G")),
    Row("F47a", (ctx(booked_category="rash check", services=[
        {"service_id": "S1", "category": "rash check", "confirmed": True, "origin": "emr-sim",
         "codes": []}]),), checks(_f47a, profile="U")),
    Row("F47b", (lambda req: req["snapshots"]["policy"]["templates"][0].update(questions=[]),
                 set_ev()),
        checks(q("policy", "applicable", "adequate", "template_match"), profile="C")),
    Row("F47c", (ctx(booked_category="rash check", services=[
        {"service_id": "S1", "category": "rash check", "confirmed": True, "origin": "emr-sim",
         "codes": []}]), add_rule(CLARIFY_RULE)),
        checks(_f47a, profile="G", action="CLARIFY_CATEGORY")),
    Row("F48a", (ctx(service_date="2026-12-01"),
                 set_ev(s.membership(observed_at="2026-11-23T17:00:00.000000Z",
                                     retrieved_at="2026-11-23T17:00:00.000000Z",
                                     valid_to="2026-12-01",
                                     scope={"service_date": "2026-12-01", "payer": "PAYER_A",
                                            "product": "PROD_A"}))),
        checks(q("M", "active", "adequate", "current_member"), profile="C")),
    Row("F48b", (set_ev(s.membership(observed_at=None)),),
        checks(q("M", "active", "insufficient", "observation_time_unknown"), profile="G")),
    Row("F48c", (at("2026-11-30T04:59:59.999999Z"),
                 set_ev(s.membership(observed_at="2026-11-29T17:00:00.000000Z",
                                     retrieved_at="2026-11-29T17:00:00.000000Z",
                                     valid_from="2026-11-30", valid_to="2026-11-30"))),
        checks(q("M", "active", "adequate", "current_member"), _local_date("2026-11-29"),
               profile="C")),
    Row("F48d", (at("2026-11-30T05:00:00.000000Z"),
                 set_ev(s.membership(observed_at="2026-11-29T17:00:00.000000Z",
                                     retrieved_at="2026-11-29T17:00:00.000000Z",
                                     valid_from="2026-11-30", valid_to="2026-11-30"))),
        checks(q("M", "active", "adequate", "current_member"), _local_date("2026-11-30"),
               profile="C")),
    Row("F48e", (ctx(service_date="2027-03-14"), at("2027-03-14T07:00:00.000000Z"),
                 set_ev(s.membership(observed_at="2027-03-13T17:00:00.000000Z",
                                     retrieved_at="2027-03-13T17:00:00.000000Z",
                                     valid_from="2027-03-14", valid_to="2027-03-14",
                                     scope={"service_date": "2027-03-14", "payer": "PAYER_A",
                                            "product": "PROD_A"}))),
        checks(q("M", "active", "adequate", "current_member"), _f48e, profile="C")),
    Row("F48f", (CHART_FAIL,), checks(_f48f, profile="D")),
    Row("F50a", (), checks(m_unchanged, profile="C")),
    Row("F50b", (), checks(m_unchanged, profile="C")),
    Row("F51a", (), checks(_f51a, profile="C")),
    Row("F51b", (lambda req: req["snapshots"]["policy"].update(rules=[
        r for r in req["snapshots"]["policy"]["rules"] if r["criterion"] != "freshness"]),),
        checks(_x("missing_criterion"), profile="X")),
    Row("F51c", (need("B"), need("N", requiredness="informational"),
                 add_ev(s.evidence("E-N1", "N", {"kind": "participating"},
                                   scope={"provider": "R1", "site": "H1",
                                          "service_date": "2026-11-30", "payer": "PAYER_A",
                                          "product": "PROD_A"})),
                 add_rule(s.rule("ACC-N-2", "acceptance", s.lit(True), {"outcome": "reject",
                                 "reason": "policy"}, {"outcome": "reject",
                                 "reason": "policy"}, types=("N",), priority=50)),
                 add_rule(s.rule("ACC-N-1", "acceptance", s.lit(True), {"outcome": "accept"},
                                 {"outcome": "reject", "reason": "policy"}, types=("N",),
                                 priority=50))),
        checks(_f51c)),
    Row("F51d", (add_rule(s.rule("DEP-X", "acceptance", s.lit(True), {"outcome": "accept"},
                                 {"outcome": "reject", "reason": "x"},
                                 depends_on=("NO-SUCH-RULE",))),),
        checks(_x("missing_dependency"), profile="X")),
    Row("F51e", (add_rule(s.rule("SELF", "acceptance", s.lit(True), {"outcome": "accept"},
                                 {"outcome": "reject", "reason": "x"},
                                 depends_on=("SELF",))),),
        checks(_x("dependency_cycle"), profile="X")),
    Row("F51f", (add_rule(s.rule("LOOP", "acceptance", s.expr("loop", over=s.lit([1])),
                                 {"outcome": "accept"}, {"outcome": "reject", "reason": "x"})),),
        checks(_x("forbidden_operator"), profile="X")),
    Row("F51g", (add_rule(s.rule("READ", "acceptance", s.expr("read", path="/etc/hosts"),
                                 {"outcome": "accept"}, {"outcome": "reject", "reason": "x"})),),
        checks(_x("forbidden_operator"), profile="X")),
    Row("F51h", (add_rule(s.rule("CLOCK", "acceptance", s.expr("now"),
                                 {"outcome": "accept"}, {"outcome": "reject", "reason": "x"})),),
        checks(_x("forbidden_operator"), profile="X")),
    Row("F52a", (lambda req: req["snapshots"].update(
        policy=s.base_bundle(max_age_seconds=12 * 3600, version=2)),),
        checks(_f52a, profile="G")),
    Row("F67a", (inactive_m(), add_rule(DISPOSE_RULE)),
        checks(q("M", "inactive", "adequate", "current_negative"), profile="S")),
    Row("F67b", (need("A"), add_rule(HANDOFF_RULE),
                 add_ev(s.evidence("E-A1", "A", {"kind": "required", "on_file": "pending"},
                                   scope={"service_id": "S1", "service_date": "2026-11-30",
                                          "payer": "PAYER_A", "product": "PROD_A"}))),
        checks(_f67b)),
    Row("F68a", (need("B"), obligation(scope={"service_id": "S1"}, state="waiting",
                                       contact="awaiting_reply",
                                       review="2026-11-27T17:00:00.000000Z")),
        checks(_f68a)),
    Row("F68b", (at("2026-11-27T17:00:00.000000Z"), need("B"), add_rule(REVIEW_RULE),
                 obligation(scope={"service_id": "S1"}, state="waiting",
                            contact="awaiting_reply", review="2026-11-27T17:00:00.000000Z")),
        checks(_f68b)),
    Row("F69a", (need("B"), route("B", "automatic_available"),
                 obligation(scope={"service_id": "S1"}, state="waiting", recovery="pending")),
        checks(q("B", "unknown", "missing", "recovery_pending"), profile="P")),
    Row("F69b", (need("B"), route("B", "automatic_available"),
                 obligation(scope={"service_id": "S1"}, state="open", recovery="exhausted")),
        checks(q("B", "unknown", "missing", "recovery_exhausted"), profile="G")),
    Row("F70a", (lambda req: req.update(work={"obligations": [], "dispositions": []}),),
        checks(m_unchanged, profile="C")),
    Row("F70b", (inactive_m(), add_rule(DISPOSE_RULE),
                 obligation(type="disposition", question_type="M", scope={}, state="closed"),
                 disposition(type="clinic_disposition", actor="staff-1",
                             at="2026-11-26T16:00:00.000000Z", closes=[{}])),
        checks(_f70b, profile="C")),
    Row("F70c", (need("B"), obligation(scope={"service_id": "S1"}, state="closed"),
                 disposition(type="clinic_disposition", actor="staff-1",
                             at="2026-11-26T16:00:00.000000Z",
                             closes=[{"service_id": "S1"}])),
        checks(_f70c)),
]

_F71 = (need("B"), need("N"), route("N", "automatic_available"),
        obligation(question_type="N", scope={"provider": "R1"}, state="waiting",
                   recovery="pending"),
        CHART_FAIL)
_B_DONE = (add_ev(B_COVERED), obligation(scope={"service_id": "S1"}, state="closed"))

ROWS += [
    Row("F71a", _F71, checks(_f71a)),
    Row("F71b", (*_F71, *_B_DONE), checks(_f71b)),
    Row("F71c", (*_F71, *_B_DONE, CHART_FAIL,
                 obligation(type="handoff_auth", question_type="A", scope={},
                            state="closed")), checks(_f71c)),
    Row("F71d", (CHART_FAIL,), checks(_f48f, profile="D")),
]


# F13a "repeat evaluation", F50a/F50b repetition and permutation, F54a/F54b replay and
# F51i operator truth table need more than one evaluation; they are handled below.
REPEATED = {"F13a", "F50a"}


def _permute(obj: Any) -> Any:
    """Reverse object key order and set-valued lists (evidence, rules, templates)."""
    if isinstance(obj, dict):
        return {k: _permute(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [_permute(v) for v in obj]
    return obj


@pytest.mark.parametrize("row", ROWS, ids=[r.row_id for r in ROWS])
def test_d1_family(row: Row) -> None:
    pkg = s.assessment()
    req = s.build(pkg, *row.mutations)
    result = s.run(pkg, req, **row.zone_over)
    row.check(pkg, result, req)
    if row.row_id in REPEATED:
        again = s.run(pkg, s.build(pkg, *row.mutations), **row.zone_over)
        assert again == result, "repeat evaluation changed the result"
    if row.row_id == "F50b":
        permuted = _permute(req)
        permuted["evidence"] = list(reversed(permuted["evidence"]))
        permuted["snapshots"]["policy"]["rules"] = list(
            reversed(permuted["snapshots"]["policy"]["rules"]))
        other = s.run(pkg, permuted)
        assert other == result
        assert other["trace"]["input_digest"] == result["trace"]["input_digest"]


ROW_IDS = [r.row_id for r in ROWS]


# ------------------------------------------------------------------ multi-evaluation rows

TRUTH: list[tuple[dict[str, Any], str]] = [
    # (expression with an unknown operand, expected: "T" / "F" / "U")
    (s.expr("eq", left=s.field("missing"), right=s.lit(1)), "U"),
    (s.expr("lt", left=s.field("missing"), right=s.lit(1)), "U"),
    (s.expr("le", left=s.field("missing"), right=s.lit(1)), "U"),
    (s.expr("gt", left=s.field("missing"), right=s.lit(1)), "U"),
    (s.expr("ge", left=s.field("missing"), right=s.lit(1)), "U"),
    (s.expr("in", item=s.field("missing"), set=s.lit(["a", "b"])), "U"),
    (s.expr("within", instant=s.field("missing"), **{"from": s.lit("2026-11-01")},
            to=s.lit("2026-11-30"), inclusive=[True, True]), "U"),
    (s.expr("present", path="missing"), "F"),
    (s.expr("present", path="here"), "T"),
    (s.expr("and", args=[s.lit(False), s.field("missing")]), "F"),
    (s.expr("and", args=[s.lit(True), s.field("missing")]), "U"),
    (s.expr("or", args=[s.lit(True), s.field("missing")]), "T"),
    (s.expr("or", args=[s.lit(False), s.field("missing")]), "U"),
    (s.expr("not", arg=s.field("missing")), "U"),
]


def test_d1_family_F51i() -> None:
    """F51i: operator truth table with Unknown operands; repeated with the same inputs.

    Each result is compared by identity with True, False or pkg.UNKNOWN, so no other value
    can pass as Unknown (Codex red review 49c67ee item 2).
    """
    pkg = s.assessment()
    view = {"here": 1}
    expected_value = {"T": True, "F": False}
    for expression, expected in TRUTH:
        got = pkg.eval_expr(expression, view)
        again = pkg.eval_expr(expression, view)
        want = pkg.UNKNOWN if expected == "U" else expected_value[expected]
        assert got is want, (expression, got)
        assert again is want, (expression, again)
    # Unknown applicability binds to unresolved (U); a dominating True selects the base (C).
    resolved = s.build(pkg, add_rule(s.rule(
        "APP-OR", "applicability", s.expr("or", args=[s.lit(True), s.field("missing")]),
        {"outcome": "applicable"}, {"outcome": "unresolved"})))
    s.assert_profile(s.run(pkg, resolved), "C")
    unresolved = s.build(pkg, add_rule(s.rule(
        "APP-AND", "applicability", s.expr("and", args=[s.lit(True), s.field("missing")]),
        {"outcome": "applicable"}, {"outcome": "unresolved"})))
    s.assert_profile(s.run(pkg, unresolved), "U")


def test_d1_family_F54a(monkeypatch: pytest.MonkeyPatch) -> None:
    """F54a: replay of the base with the caller clock advanced 10 days is identical.

    The caller's clock is advanced independently (time.time/time_ns and datetime.datetime
    replaced in the test process); a control proves the perturbation took effect before the
    replay (Codex red review 49c67ee item 3).
    """
    import datetime as datetime_module
    import time as time_module

    pkg = s.assessment()
    original = s.run(pkg, s.build(pkg))

    shift = 10 * 24 * 3600
    real_time, real_time_ns = time_module.time, time_module.time_ns
    real_datetime = datetime_module.datetime

    class AdvancedDatetime(real_datetime):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return real_datetime.now(tz) + datetime_module.timedelta(seconds=shift)

        @classmethod
        def utcnow(cls) -> Any:
            return real_datetime.now(datetime_module.UTC).replace(tzinfo=None) + \
                datetime_module.timedelta(seconds=shift)

        @classmethod
        def today(cls) -> Any:
            return cls.now()

    before = real_time()
    monkeypatch.setattr(time_module, "time", lambda: real_time() + shift)
    monkeypatch.setattr(time_module, "time_ns", lambda: real_time_ns() + shift * 10**9)
    monkeypatch.setattr(datetime_module, "datetime", AdvancedDatetime)
    # Control: the caller clock really moved by ~10 days.
    assert time_module.time() - before >= shift - 5
    assert (datetime_module.datetime.now(datetime_module.UTC)
            - real_datetime.now(datetime_module.UTC)).total_seconds() >= shift - 5

    replayed = pkg.replay(original["replay_record"], zones=s.zones(pkg))
    assert replayed == original
    assert replayed["trace"]["evaluation_instant"] == s.EVAL


def test_d1_family_F54b() -> None:
    """F54b: with policy v2 current, replaying the v1 record keeps the v1 digest and trace."""
    pkg = s.assessment()
    original = s.run(pkg, s.build(pkg))
    current = s.run(pkg, s.build(pkg, lambda req: req["snapshots"].update(
        policy=s.base_bundle(max_age_seconds=12 * 3600, version=2))))
    assert current["trace"]["bundle_digest"] != original["trace"]["bundle_digest"]
    replayed = pkg.replay(original["replay_record"], zones=s.zones(pkg))
    assert replayed["trace"]["bundle_digest"] == original["trace"]["bundle_digest"]
    assert replayed == original
