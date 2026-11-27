"""Rule bundle validation (pins P3): shape, operators, limits, dependencies, coverage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .expressions import ExpressionInvalid, validate

REQUIRED_CRITERIA: Final = ("acceptance", "freshness", "obligation", "action_priority")
KNOWN_CRITERIA: Final = frozenset(REQUIRED_CRITERIA) | {"applicability"}
_RULE_KEYS: Final = ("rule_id", "version", "criterion", "question_types", "priority",
                     "depends_on", "when", "then")


class BundleRejected(ValueError):
    def __init__(self, code: str, rule_id: str | None = None) -> None:
        super().__init__(code if rule_id is None else f"{code}: {rule_id}")
        self.code = code
        self.rule_id = rule_id


def _check_rule(rule: Any) -> str:
    if not isinstance(rule, Mapping) or any(k not in rule for k in _RULE_KEYS):
        rid = rule.get("rule_id") if isinstance(rule, Mapping) else None
        raise BundleRejected("malformed_rule", rid if isinstance(rid, str) else None)
    rid = rule["rule_id"]
    if not isinstance(rid, str) or not rid:
        raise BundleRejected("malformed_rule")
    if "unknown" not in rule or not isinstance(rule["unknown"], Mapping):
        raise BundleRejected("missing_unknown_outcome", rid)
    if (not isinstance(rule["version"], int) or isinstance(rule["version"], bool)
            or not isinstance(rule["priority"], int) or isinstance(rule["priority"], bool)
            or rule["criterion"] not in KNOWN_CRITERIA
            or not isinstance(rule["question_types"], list)
            or not isinstance(rule["depends_on"], list)
            or not isinstance(rule["then"], Mapping)):
        raise BundleRejected("malformed_rule", rid)
    try:
        validate(rule["when"])
    except ExpressionInvalid as exc:
        raise BundleRejected(exc.code, rid) from exc
    return rid


def validate_bundle(bundle: Mapping[str, Any]) -> None:
    """Raise BundleRejected for an unusable bundle; return None when it is valid."""
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("rules"), list) \
            or not isinstance(bundle.get("templates"), list):
        raise BundleRejected("malformed_rule")
    rules: dict[str, Mapping[str, Any]] = {}
    for rule in bundle["rules"]:
        rid = _check_rule(rule)
        if rid in rules and rules[rid] != rule:
            raise BundleRejected("malformed_rule", rid)
        rules[rid] = rule
    for template in bundle["templates"]:
        if not isinstance(template, Mapping) or not isinstance(template.get("questions"), list) \
                or "category" not in template:
            raise BundleRejected("malformed_rule")
    for rid, rule in sorted(rules.items()):
        for dep in rule["depends_on"]:
            if dep not in rules:
                raise BundleRejected("missing_dependency", rid)
    _check_acyclic(rules)
    present = {rule["criterion"] for rule in rules.values()}
    for criterion in REQUIRED_CRITERIA:
        if criterion not in present:
            raise BundleRejected("missing_criterion")


def _check_acyclic(rules: Mapping[str, Mapping[str, Any]]) -> None:
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    for start in sorted(rules):
        if state.get(start) == 2:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        state[start] = 1
        while stack:
            node, index = stack[-1]
            deps = sorted(rules[node]["depends_on"])
            if index < len(deps):
                stack[-1] = (node, index + 1)
                dep = deps[index]
                if state.get(dep) == 1:
                    raise BundleRejected("dependency_cycle", dep)
                if state.get(dep) is None:
                    state[dep] = 1
                    stack.append((dep, 0))
            else:
                state[node] = 2
                stack.pop()
