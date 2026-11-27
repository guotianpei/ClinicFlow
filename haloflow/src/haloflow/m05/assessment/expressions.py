"""The bounded rule expression language (architecture v2 "Architecture decision on rules").

Non-Turing-complete, side-effect-free and total over validated inputs: literals, declared
field reads, typed equality, ordered comparisons, finite literal-set membership, presence
tests, boolean combinations and explicit date/instant windows. No loops, recursion, I/O,
randomness or ambient clock. Unknown is a distinct value with Kleene semantics: False
dominates conjunction, True dominates disjunction, negation preserves Unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Any, Final


class _Unknown:
    """The typed Unknown truth value (a singleton)."""

    _instance: _Unknown | None = None

    def __new__(cls) -> _Unknown:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        raise TypeError("UNKNOWN has no boolean value")


UNKNOWN: Final[_Unknown] = _Unknown()

Truth = bool | _Unknown

LIMITS: Final[Mapping[str, int]] = {"depth": 16, "nodes": 256, "collection": 128}

KNOWN_OPS: Final = frozenset({"lit", "field", "eq", "lt", "le", "gt", "ge", "in", "present",
                              "and", "or", "not", "within"})
FORBIDDEN_OPS: Final = frozenset({"loop", "map", "read", "fetch", "now", "random", "call"})
_LEAF_OPS: Final = frozenset({"lit", "field", "present"})


class ExpressionInvalid(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _children(expr: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    op = expr.get("op")
    if op in ("eq", "lt", "le", "gt", "ge"):
        return [expr["left"], expr["right"]]
    if op == "in":
        return [expr["item"], expr["set"]]
    if op in ("and", "or"):
        args = expr["args"]
        if not isinstance(args, list) or not args:
            raise ExpressionInvalid("malformed_rule")
        return list(args)
    if op == "not":
        return [expr["arg"]]
    if op == "within":
        return [expr["instant"], expr["from"], expr["to"]]
    return []


def validate(expr: Any) -> None:
    """Static checks: operators, shape, limits and literal type agreement."""
    nodes = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal nodes
        if not isinstance(node, Mapping) or not isinstance(node.get("op"), str):
            raise ExpressionInvalid("malformed_rule")
        op = node["op"]
        if op in FORBIDDEN_OPS:
            raise ExpressionInvalid("forbidden_operator")
        if op not in KNOWN_OPS:
            raise ExpressionInvalid("unknown_operator")
        if depth > LIMITS["depth"]:
            raise ExpressionInvalid("limit_depth")
        nodes += 1
        if nodes > LIMITS["nodes"]:
            raise ExpressionInvalid("limit_nodes")
        if op == "lit":
            if "value" not in node:
                raise ExpressionInvalid("malformed_rule")
            value = node["value"]
            if isinstance(value, list) and len(value) > LIMITS["collection"]:
                raise ExpressionInvalid("limit_collection")
        elif op in ("field", "present"):
            if not isinstance(node.get("path"), str):
                raise ExpressionInvalid("malformed_rule")
        elif op == "within":
            inclusive = node.get("inclusive")
            if not (isinstance(inclusive, list) and len(inclusive) == 2
                    and all(isinstance(b, bool) for b in inclusive)):
                raise ExpressionInvalid("malformed_rule")
        try:
            children = _children(node)
        except KeyError as exc:
            raise ExpressionInvalid("malformed_rule") from exc
        if op in ("eq", "lt", "le", "gt", "ge"):
            _check_literal_types(op, children[0], children[1])
        for child in children:
            walk(child, depth + 1)

    walk(expr, 1)


def _literal_kind(node: Mapping[str, Any]) -> str | None:
    if node.get("op") != "lit":
        return None
    value = node.get("value")
    if isinstance(value, bool) or value is None:
        return "bool" if isinstance(value, bool) else "null"
    if isinstance(value, int):
        return "number"
    if isinstance(value, str):
        return "text"
    return "other"


def _check_literal_types(op: str, left: Any, right: Any) -> None:
    if not (isinstance(left, Mapping) and isinstance(right, Mapping)):
        return
    lk, rk = _literal_kind(left), _literal_kind(right)
    if lk is None or rk is None:
        return
    if lk != rk:
        raise ExpressionInvalid("type_mismatch")
    if op != "eq" and lk not in ("number", "text"):
        raise ExpressionInvalid("type_mismatch")


def _value(node: Mapping[str, Any], view: Mapping[str, Any]) -> Any:
    op = node["op"]
    if op == "lit":
        return node["value"]
    if op == "field":
        return view.get(node["path"], UNKNOWN)
    return eval_expr(node, view)


def _is_unknown(value: object) -> bool:
    return value is UNKNOWN


def _comparable(a: Any, b: Any) -> bool:
    numeric = (int, Fraction)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, numeric) and isinstance(b, numeric):
        return True
    return isinstance(a, str) and isinstance(b, str)


def eval_expr(expr: Mapping[str, Any], view: Mapping[str, Any]) -> Truth:
    """Evaluate a validated expression to True, False or UNKNOWN (never raises on data)."""
    op = str(expr["op"])
    if op == "lit":
        value = expr["value"]
        return value if isinstance(value, bool) else UNKNOWN
    if op == "field":
        value = view.get(expr["path"], UNKNOWN)
        return value if isinstance(value, bool) else UNKNOWN
    if op == "present":
        return expr["path"] in view and view[expr["path"]] is not UNKNOWN
    if op == "not":
        inner = eval_expr(expr["arg"], view)
        return UNKNOWN if inner is UNKNOWN else not inner
    if op in ("and", "or"):
        dominant: bool = op == "or"
        results = [eval_expr(arg, view) for arg in expr["args"]]
        if any(r is dominant for r in results):
            return dominant
        if any(r is UNKNOWN for r in results):
            return UNKNOWN
        return not dominant
    if op in ("eq", "lt", "le", "gt", "ge"):
        left: Any = _value(expr["left"], view)
        right: Any = _value(expr["right"], view)
        if _is_unknown(left) or _is_unknown(right):
            return UNKNOWN
        if op == "eq":
            return bool(left == right) if _comparable(left, right) else False
        if not _comparable(left, right) or isinstance(left, bool):
            return UNKNOWN
        if op == "lt":
            return bool(left < right)
        if op == "le":
            return bool(left <= right)
        if op == "gt":
            return bool(left > right)
        return bool(left >= right)
    if op == "in":
        item, members = _value(expr["item"], view), _value(expr["set"], view)
        if item is UNKNOWN or members is UNKNOWN or not isinstance(members, list):
            return UNKNOWN
        return item in members
    if op == "within":
        instant = _value(expr["instant"], view)
        low, high = _value(expr["from"], view), _value(expr["to"], view)
        if UNKNOWN in (instant, low, high) or not all(
                isinstance(v, str) for v in (instant, low, high)):
            return UNKNOWN
        low_ok = instant >= low if expr["inclusive"][0] else instant > low
        high_ok = instant <= high if expr["inclusive"][1] else instant < high
        return bool(low_ok and high_ok)
    return UNKNOWN
