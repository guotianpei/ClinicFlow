"""D1 appointment-verification evaluator: shared fixtures and oracles (red packet).

Frozen sources: requirements v1.4 (e16585e8...), architecture v2 (1faea713...), test packet
v2r1 (4f23092e...), contract pins v1 (claude_d1-contract-pins-v1-2026-11-26.md).

The package under test (`haloflow.m05.assessment`) is imported lazily inside each test body
(`assessment()`), never at collection or fixture setup, so a missing implementation makes
each row FAIL in its own body rather than ERROR in setup (the class-B red rule).

Golden expectations here are written from the test packet, independently of any
implementation. No database, network or model is used. The IANA zone data is a frozen,
reviewed fixture (fixtures/tz_2026e_america_new_york.json) passed to the evaluator as
immutable data (T1); the evaluator package itself may not read zone data.
"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

# ---------------------------------------------------------------------------- package


def assessment() -> ModuleType:
    """Import the package under test inside the test body (class-B red rule)."""
    return importlib.import_module("haloflow.m05.assessment")


# ---------------------------------------------------------------------------- time

# Frozen, reviewed zone fixture (Codex red review 49c67ee item 1): America/New_York
# transitions 2026-01-01..2028-01-01 from IANA tzdata 2026e, generated once in the test
# process and committed. Its digest below was computed independently with hashlib over the
# canonical form (sorted keys, no insignificant whitespace, literal UTF-8), NOT through the
# package under test, so a wrong package digest cannot certify itself.
FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TZ_FIXTURE = json.loads((FIXTURES / "tz_2026e_america_new_york.json").read_text("utf-8"))
TZ_RELEASE = "2026e"
TZ_PAYLOAD: dict[str, Any] = _TZ_FIXTURE["payload"]
TZ_DIGEST = "0cfa9719c59e3860c574ec39097e2d3ced0ea949bd94cfceb87c7c47b24334bf"
assert TZ_PAYLOAD["release"] == TZ_RELEASE

# Independent canonical-encoding vector (A3), bytes and SHA-256 computed with the stdlib.
VECTOR = {"b": 1, "a": "\u00e9", "d": {"y": True, "x": None}, "n": -7}
VECTOR_BYTES = b'{"a":"\xc3\xa9","b":1,"d":{"x":null,"y":true},"n":-7}'
VECTOR_DIGEST = "783df830306d76cadc744e42109633cdaf6b70db0739543e13ed3e5ebacdc430"


def instant(text: str) -> str:
    """Canonical instant: UTC RFC 3339, six fractional digits, trailing Z."""
    value = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def zones(pkg: ModuleType, *, release: str | None = None, digest: str | None = None) -> Any:
    """pkg.ZoneData from the frozen fixture with the independently computed digest."""
    transitions = {zone: tuple(tuple(pair) for pair in pairs)
                   for zone, pairs in TZ_PAYLOAD["transitions"].items()}
    return pkg.ZoneData(release=release or TZ_RELEASE, digest=digest or TZ_DIGEST,
                        transitions=transitions)


def tz_reference(pkg: ModuleType) -> dict[str, str]:
    return {"release": TZ_RELEASE, "digest": TZ_DIGEST}


# ---------------------------------------------------------------------------- policy

SEVEN_DAYS = 7 * 24 * 3600


def expr(op: str, **kw: Any) -> dict[str, Any]:
    return {"op": op, **kw}


def field(path: str) -> dict[str, Any]:
    return expr("field", path=path)


def lit(value: Any) -> dict[str, Any]:
    return expr("lit", value=value)


def rule(rule_id: str, criterion: str, when: dict[str, Any], then: dict[str, Any],
         unknown: dict[str, Any], *, types: tuple[str, ...] = ("*",), priority: int = 100,
         depends_on: tuple[str, ...] = (), version: int = 1) -> dict[str, Any]:
    return {"rule_id": rule_id, "version": version, "criterion": criterion,
            "question_types": list(types), "priority": priority,
            "depends_on": list(depends_on), "when": when, "then": then, "unknown": unknown}


def base_bundle(*, max_age_seconds: int = SEVEN_DAYS, version: int = 1) -> dict[str, Any]:
    """Reference HaloFlow-reviewed policy (OD-AV5): membership only for a consult."""
    return {
        "bundle_id": "d1-reference",
        "version": version,
        "limits_version": "d1.1",
        "templates": [
            {"template_id": "consult-base", "category": "consult", "payer": None,
             "questions": [{"type": "M", "requiredness": "required", "depends_on": []}]},
        ],
        "rules": [
            rule("ACC-ENV", "acceptance",
                 expr("eq", left=field("evidence.environment"),
                      right=field("context.environment")),
                 {"outcome": "accept"},
                 {"outcome": "reject", "reason": "environment_unknown"}),
            rule("FRS-AGE", "freshness",
                 expr("and", args=[
                     expr("ge", left=field("evidence.age_seconds"), right=lit(0)),
                     expr("le", left=field("evidence.age_seconds"),
                          right=lit(max_age_seconds)),
                     expr("within", instant=field("context.service_date"),
                          **{"from": field("evidence.valid_from")},
                          to=field("evidence.valid_to"), inclusive=[True, True]),
                 ]),
                 {"outcome": "fresh"},
                 {"outcome": "insufficient", "reason": "observation_time_unknown"},
                 version=version),
            rule("OBL-ROUTE", "obligation",
                 expr("eq", left=field("route"), right=lit("staff_only")),
                 {"outcome": "staff_verification", "queue": "verification"},
                 {"outcome": "unresolved", "reason": "route_unknown"}),
            rule("ACT-PRI", "action_priority",
                 expr("present", path="obligation.type"),
                 {"order": ["CLARIFY_PRIORITY", "CLARIFY_CATEGORY", "CLARIFY_SITE",
                            "RESOLVE_CONFLICT", "VERIFY", "CHECK_B", "REVIEW_REPLY",
                            "HANDOFF_AUTH", "DISPOSE", "DOCUMENT", "WAIT_REPLY",
                            "WAIT_AUTO"]},
                 {"order": []}),
        ],
    }


def require(req: dict[str, Any], qtype: str, *, requiredness: str = "required",
            depends_on: tuple[str, ...] = ("M",), template: str = "consult-base") -> None:
    """Add a question type to a template in the request's policy snapshot."""
    for tpl in req["snapshots"]["policy"]["templates"]:
        if tpl["template_id"] == template:
            tpl["questions"].append({"type": qtype, "requiredness": requiredness,
                                     "depends_on": list(depends_on)})
            return
    raise KeyError(template)


# ---------------------------------------------------------------------------- request

EVAL = "2026-11-26T17:00:00.000000Z"
OBSERVED = "2026-11-25T17:00:00.000000Z"


def membership(**over: Any) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "evidence_id": "E-M1", "version": 1, "question_type": "M",
        "subject": {"patient": "P", "coverage_id": "V1", "coverage_version": 1},
        "scope": {"service_date": "2026-11-30", "payer": "PAYER_A", "product": "PROD_A"},
        "environment": "simulated", "source_class": "emr", "origin": "emr-sim",
        "upstream_txn": "U-M1", "observed_at": OBSERVED, "retrieved_at": OBSERVED,
        "valid_from": "2026-11-01", "valid_to": "2026-11-30",
        "value": {"kind": "active"},
    }
    ev.update(over)
    return ev


def evidence(evidence_id: str, qtype: str, value: dict[str, Any], **over: Any) -> dict[str, Any]:
    ev = membership(evidence_id=evidence_id, question_type=qtype, value=value,
                    upstream_txn=f"U-{evidence_id}")
    ev.update(over)
    return ev


def base_request(pkg: ModuleType) -> dict[str, Any]:
    """The v2r1 base fixture: T/C/A rev 1, P, V1 payer A, consult, service 2026-11-30 ET."""
    return {
        "schema": "d1.request.v1",
        "evaluation_instant": EVAL,
        "trigger": {"kind": "fixture", "reason": "d1 base"},
        "context": {
            "tenant": "T", "clinic": "C", "environment": "simulated",
            "source_system": "emr-sim", "appointment_id": "A", "appointment_version": 1,
            "patient": "P", "status": "booked", "clinic_zone": "America/New_York",
            "service_date": "2026-11-30", "provider": "R1", "site": "H1",
            "coverages": [{"coverage_id": "V1", "version": 1, "payer": "PAYER_A",
                           "product": "PROD_A", "relationship": "self", "priority": 1}],
            "stated_reason": "follow-up visit", "booked_category": "consult",
            "services": [{"service_id": "S1", "category": "consult", "confirmed": True,
                          "origin": "emr-sim", "codes": []}],
            "suggestions": [],
        },
        "evidence": [membership()],
        "work": {"obligations": [], "dispositions": []},
        "snapshots": {
            "policy": base_bundle(),
            "routes": {"M": "staff_only", "B": "staff_only", "N": "staff_only",
                       "A": "staff_only", "category": "staff_only", "priority": "staff_only",
                       "site": "staff_only"},
            "permissions": ["VERIFY", "CLARIFY_CATEGORY", "CLARIFY_PRIORITY", "CLARIFY_SITE",
                            "RESOLVE_CONFLICT", "REVIEW_REPLY", "WAIT_REPLY", "DISPOSE",
                            "DOCUMENT", "HANDOFF_AUTH"],
            "reference": {"tzdata": tz_reference(pkg), "currency_scale": {"USD": 2}},
        },
    }


Mutation = Callable[[dict[str, Any]], None]


def build(pkg: ModuleType, *mutations: Mutation) -> dict[str, Any]:
    req = base_request(pkg)
    for mutate in mutations:
        mutate(req)
    return req


# ---------------------------------------------------------------------------- oracles

ZERO_EFFECTS = {"sends": 0, "db_writes": 0, "staff_mutations": 0, "ehr_writes": 0}


def run(pkg: ModuleType, req: dict[str, Any], **zone_over: Any) -> dict[str, Any]:
    """Evaluate and assert the invariants every case shares: input unchanged, zero effects."""
    before = copy.deepcopy(req)
    result = pkg.evaluate(req, zones=zones(pkg, **zone_over))
    assert req == before, "evaluate() mutated its input"
    assert result["effects"] == ZERO_EFFECTS
    return result


def question(result: Mapping[str, Any], qtype: str, **scope: Any) -> dict[str, Any]:
    matches = [q for q in result["questions"] if q["type"] == qtype
               and all(q["scope"].get(k) == v for k, v in scope.items())]
    assert len(matches) == 1, f"expected one {qtype} question {scope}, got {len(matches)}"
    return matches[0]


def assert_q(result: Mapping[str, Any], qtype: str, finding: str, adequacy: str,
             reason: str, **scope: Any) -> dict[str, Any]:
    q = question(result, qtype, **scope)
    assert (q["finding"], q["adequacy"], q["reason"]) == (finding, adequacy, reason), q
    return q


def codes(result: Mapping[str, Any]) -> list[str]:
    return [a["code"] for a in result["actions"]]


def open_obligations(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [o for o in result["obligations"] if o["state"] != "closed"]


def assert_profile(result: Mapping[str, Any], profile: str, action: str | None = None) -> None:
    """The v2r1 aggregate profiles C/G/S/P/U/D/X (pins P4)."""
    if profile == "X":
        assert result["status"] == "rejected"
        assert result["questions"] == [] and result["actions"] == []
        return
    assert result["status"] == "assessed"
    expected: dict[str, tuple[bool, str, list[str], str, str | None]] = {
        # completion, follow_up, actions, primary_state, (open obligation type, state)
        "C": (True, "no", [], "none", None),
        "G": (False, "yes", [action or "VERIFY"], "action", "verification:ready"),
        "S": (True, "yes", [action or "DISPOSE"], "action", "disposition:ready"),
        "P": (False, "undetermined", ["WAIT_AUTO"], "action", "verification:waiting"),
        "U": (False, "undetermined", [], "blocked", None),
        "D": (True, "yes", [action or "DOCUMENT"], "action", "documentation:ready"),
    }
    completion, follow_up, actions, primary_state, obligation = expected[profile]
    assert result["completion"] is completion
    assert result["follow_up"] == follow_up
    assert codes(result) == actions
    assert result["primary_state"] == primary_state
    if actions:
        assert result["primary"] == {"code": actions[0]}
    else:
        assert result["primary"] is None
    if profile == "U":
        assert result["applicability"] == "unresolved"
    opened = open_obligations(result)
    if obligation is None:
        assert opened == [], opened
    else:
        otype, ostate = obligation.split(":")
        if profile == "G" and action not in (None, "VERIFY"):
            otype = {"CLARIFY_CATEGORY": "clarification", "CLARIFY_SITE": "clarification",
                     "CLARIFY_PRIORITY": "clarification",
                     "RESOLVE_CONFLICT": "verification"}.get(action, otype)
        assert [(o["type"], o["state"]) for o in opened] == [(otype, ostate)], opened
