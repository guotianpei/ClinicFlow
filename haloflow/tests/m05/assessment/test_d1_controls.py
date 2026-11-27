"""D1 architecture controls I1..I8 and resource limits (test packet v2r1).

I1/I2 static controls scan `src/haloflow/m05/assessment/` with the AST and carry their own
negative fixtures, so the checker is proven to fire (scanning alone is not evidence).
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import d1_support as s
import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "haloflow" / "m05" / "assessment"

# ---------------------------------------------------------------- I1 / I2 static control

FORBIDDEN_MODULES = (
    "psycopg", "psycopg_pool", "sqlalchemy", "asyncpg", "sqlite3",       # database drivers
    "haloflow.m01", "haloflow.platform.db",                              # M01 / DB runtime
    "socket", "ssl", "http", "urllib", "httpx", "requests", "aiohttp",   # network / HTTP
    "openai", "anthropic", "google.generativeai", "vertexai",            # model SDKs
    "zoneinfo", "tzdata", "dateutil.tz", "pytz",                         # system zone data
    "importlib", "subprocess", "shutil", "tempfile", "glob", "pathlib",  # dynamic / fs
    "time", "random", "secrets", "os",
)
FORBIDDEN_CALLS = {
    ("datetime", "now"), ("datetime", "today"), ("datetime", "utcnow"), ("date", "today"),
    ("time", "time"), ("os", "getenv"), ("ZoneInfo", None),
}
FORBIDDEN_BUILTINS = {"open", "__import__", "eval", "exec", "compile", "input"}
FORBIDDEN_ATTRS = {"environ", "getenv", "now", "today", "utcnow"}


def violations(source: str) -> list[str]:
    """Static isolation check for one module's source (aliases resolved)."""
    tree = ast.parse(source)
    found: list[str] = []
    aliases: dict[str, str] = {}

    def forbidden(name: str) -> bool:
        return any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name
                if forbidden(a.name):
                    found.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and forbidden(mod):
                found.append(f"from {mod} import")
            for a in node.names:
                full = f"{mod}.{a.name}" if mod else a.name
                aliases[a.asname or a.name] = full
                if node.level == 0 and forbidden(full):
                    found.append(f"from {mod} import {a.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                target = aliases.get(fn.id, fn.id)
                if fn.id in FORBIDDEN_BUILTINS:
                    found.append(f"call {fn.id}")
                if target.split(".")[-1] == "ZoneInfo" or target in (
                        "datetime.datetime.now", "datetime.date.today", "time.time",
                        "os.getenv"):
                    found.append(f"call {target}")
            elif isinstance(fn, ast.Attribute):
                owner = fn.value
                owner_name = owner.id if isinstance(owner, ast.Name) else (
                    owner.attr if isinstance(owner, ast.Attribute) else "")
                resolved = aliases.get(owner_name, owner_name).split(".")[-1]
                if (resolved, fn.attr) in FORBIDDEN_CALLS or (resolved, None) in FORBIDDEN_CALLS:
                    found.append(f"call {resolved}.{fn.attr}")
                if fn.attr in ("now", "today", "utcnow", "getenv"):
                    found.append(f"call .{fn.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            found.append(f"attr .{node.attr}")
    return sorted(set(found))


NEGATIVE_CONTROLS = {
    "db-direct": "import psycopg\n",
    "db-alias": "import psycopg_pool as p\n",
    "m01-runtime": "from haloflow.m01 import composition\n",
    "model-sdk": "import anthropic\n",
    "http": "import urllib.request\n",
    "http-alias": "from httpx import Client as C\nC()\n",
    "env-getenv": "import os\nos.getenv('X')\n",
    "env-environ-alias": "from os import environ as E\nE['X']\n",
    "fs-open": "open('/etc/hosts')\n",
    "fs-pathlib": "from pathlib import Path\nPath('x').read_text()\n",
    "clock-now": "from datetime import datetime\ndatetime.now()\n",
    "clock-alias": "from datetime import datetime as D\nD.now()\n",
    "clock-today": "import datetime\ndatetime.date.today()\n",
    "clock-time": "import time\ntime.time()\n",
    "dynamic-import": "import importlib\nimportlib.import_module('x')\n",
    "dunder-import": "__import__('os')\n",
    "zoneinfo-direct": "from zoneinfo import ZoneInfo\nZoneInfo('America/New_York')\n",
    "zoneinfo-alias": "import zoneinfo as z\nz.ZoneInfo('UTC')\n",
}
ALLOWED_CONTROL = ("from __future__ import annotations\nimport hashlib\nimport json\n"
                   "from dataclasses import dataclass\nfrom datetime import date, datetime\n"
                   "from .canonical import digest\n"
                   "def f(x: str) -> str:\n    return hashlib.sha256(x.encode()).hexdigest()\n")


@pytest.mark.parametrize("label", sorted(NEGATIVE_CONTROLS))
def test_i1_negative_controls_fail_the_static_check(label: str) -> None:
    assert violations(NEGATIVE_CONTROLS[label]), f"{label} was not caught"


def test_i1_allowed_pure_helper_passes_the_static_check() -> None:
    assert violations(ALLOWED_CONTROL) == []


def test_i1_assessment_package_is_isolated() -> None:
    """Every module in the package (and any project helper it imports) passes the check."""
    assert PACKAGE.is_dir(), f"{PACKAGE} does not exist"
    modules = sorted(PACKAGE.rglob("*.py"))
    assert modules, "assessment package has no modules"
    problems: dict[str, list[str]] = {}
    for path in modules:
        source = path.read_text(encoding="utf-8")
        found = violations(source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (
                    node.module or "").startswith("haloflow.") and not (
                    node.module or "").startswith("haloflow.m05.assessment"):
                found.append(f"project helper outside package: {node.module}")
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("haloflow.") and not a.name.startswith(
                            "haloflow.m05.assessment"):
                        found.append(f"project helper outside package: {a.name}")
        if found:
            problems[str(path.relative_to(PACKAGE))] = found
    assert problems == {}


# ---------------------------------------------------------------- I2 injected zones (T1)


def test_i2_evaluates_without_system_zone_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = s.assessment()
    req = s.build(pkg)
    zones = s.zones(pkg)  # built by the caller, before the lookup is disabled

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("system zone lookup used inside the evaluator")

    import zoneinfo
    monkeypatch.setattr(zoneinfo, "ZoneInfo", refuse)
    result = pkg.evaluate(req, zones=zones)
    s.assert_profile(result, "C")
    assert result["trace"]["tzdata"] == s.tz_reference(pkg)


@pytest.mark.parametrize("which", ["release", "digest"])
def test_i2_replay_with_mismatched_zone_data_is_refused(which: str) -> None:
    pkg = s.assessment()
    original = s.run(pkg, s.build(pkg))
    over = {"release": "other-2027z"} if which == "release" else {"digest": "0" * 64}
    with pytest.raises(pkg.ReplayError) as err:
        pkg.replay(original["replay_record"], zones=s.zones(pkg, **over))
    assert err.value.code == "tzdata_mismatch"


# ---------------------------------------------------------------- I3 assessment-wide conflict


def _conflict(order: tuple[str, str], same_output: bool = False) -> list[Any]:
    rules = {
        "ACC-N-1": s.rule("ACC-N-1", "acceptance", s.lit(True), {"outcome": "accept"},
                          {"outcome": "reject", "reason": "policy"}, types=("N",),
                          priority=50),
        "ACC-N-2": s.rule("ACC-N-2", "acceptance", s.lit(True),
                          {"outcome": "accept"} if same_output else
                          {"outcome": "reject", "reason": "policy"},
                          {"outcome": "reject", "reason": "policy"}, types=("N",),
                          priority=50),
    }

    def m(req: dict[str, Any]) -> None:
        s.require(req, "B")
        s.require(req, "N", requiredness="informational")
        req["evidence"].append(s.evidence("E-N1", "N", {"kind": "participating"},
                                          scope={"provider": "R1", "site": "H1",
                                                 "service_date": "2026-11-30",
                                                 "payer": "PAYER_A", "product": "PROD_A"}))
        req["snapshots"]["policy"]["rules"].extend(rules[k] for k in order)
    return [m]


@pytest.mark.parametrize("order", [("ACC-N-1", "ACC-N-2"), ("ACC-N-2", "ACC-N-1")],
                         ids=["forward", "reversed"])
def test_i3_conflict_withholds_all_action_authority(order: tuple[str, str]) -> None:
    pkg = s.assessment()
    result = s.run(pkg, s.build(pkg, *_conflict(order)))
    s.assert_q(result, "M", "active", "adequate", "current_member")
    s.assert_q(result, "B", "unknown", "missing", "answer_absent")
    assert result["configuration_exception"]["rule_ids"] == ["ACC-N-1", "ACC-N-2"]
    assert result["completion"] is False
    assert result["actions"] == [] and result["primary_state"] == "blocked"


def test_i3_reversal_is_identical() -> None:
    pkg = s.assessment()
    a = s.run(pkg, s.build(pkg, *_conflict(("ACC-N-1", "ACC-N-2"))))
    b = s.run(pkg, s.build(pkg, *_conflict(("ACC-N-2", "ACC-N-1"))))
    for key in ("configuration_exception", "questions", "obligations", "actions",
                "follow_up", "completion", "primary_state"):
        assert a[key] == b[key], key


def test_i3_identical_outputs_coalesce_with_both_rules_traced() -> None:
    pkg = s.assessment()
    result = s.run(pkg, s.build(pkg, *_conflict(("ACC-N-1", "ACC-N-2"), same_output=True)))
    assert result["configuration_exception"] is None
    traced = {c["rule_id"] for c in result["trace"]["criteria"]
              if c["criterion"] == "acceptance" and c["rule_id"].startswith("ACC-N")}
    assert traced == {"ACC-N-1", "ACC-N-2"}


# ---------------------------------------------------------------- I4 routes and permissions


@pytest.mark.parametrize("route,allowed,expected", [
    ("automatic_available", True, (["CHECK_B"], "system")),
    ("staff_only", True, (["VERIFY"], "staff")),
    ("staff_only", False, ([], None)),
    ("none", True, (["VERIFY"], "staff")),
    ("none", False, ([], None)),
])
def test_i4_route_and_permission_fixtures(route: str, allowed: bool,
                                          expected: tuple[list[str], str | None]) -> None:
    pkg = s.assessment()

    def m(req: dict[str, Any]) -> None:
        s.require(req, "B")
        req["snapshots"]["routes"]["B"] = route
        perms = [p for p in req["snapshots"]["permissions"] if p != "VERIFY"]
        if allowed:
            perms += ["VERIFY", "CHECK_B"]
        req["snapshots"]["permissions"] = perms

    result = s.run(pkg, s.build(pkg, m))
    action_codes, mode = expected
    assert s.codes(result) == action_codes
    if mode:
        assert result["actions"][0]["mode"] == mode
    if route != "automatic_available":
        # A known staff obligation survives permission denial, blocked with its reason.
        opened = s.open_obligations(result)
        assert [o["type"] for o in opened] == ["verification"]
        assert opened[0]["state"] == ("ready" if allowed else "blocked")


@pytest.mark.parametrize("missing", ["routes", "permissions"])
def test_i4_missing_route_or_permission_data_cannot_authorize(missing: str) -> None:
    pkg = s.assessment()

    def m(req: dict[str, Any]) -> None:
        s.require(req, "B")
        if missing == "routes":
            req["snapshots"]["routes"] = {}
        else:
            req["snapshots"]["permissions"] = []

    result = s.run(pkg, s.build(pkg, m))
    assert all(a["state"] != "ready" for a in result["actions"]), result["actions"]


# ---------------------------------------------------------------- I5 canonical identity


def test_i5_equivalent_sets_and_objects_have_identical_bytes_digest_and_output() -> None:
    pkg = s.assessment()
    two = [s.membership(), s.membership(evidence_id="E-M9", upstream_txn="U-M9")]
    a = s.build(pkg, lambda r: r.update(evidence=copy.deepcopy(two)))
    b = s.build(pkg, lambda r: r.update(evidence=list(reversed(copy.deepcopy(two)))))
    b = json.loads(json.dumps(b), object_pairs_hook=lambda kv: dict(reversed(kv)))
    assert pkg.canonical_bytes(a) == pkg.canonical_bytes(b)
    assert pkg.digest(a) == pkg.digest(b)
    ra, rb = s.run(pkg, a), s.run(pkg, b)
    assert [q["key"] for q in ra["questions"]] == [q["key"] for q in rb["questions"]]
    assert ra == rb


def test_i5_duplicate_identical_set_entry_is_removed() -> None:
    pkg = s.assessment()
    a = s.build(pkg)
    b = s.build(pkg, lambda r: r["evidence"].append(copy.deepcopy(r["evidence"][0])))
    assert pkg.canonical_bytes(a) == pkg.canonical_bytes(b)


def test_i5_sequence_order_stays_encoded() -> None:
    pkg = s.assessment()
    e1 = s.expr("and", args=[s.lit(True), s.lit(False)])
    e2 = s.expr("and", args=[s.lit(False), s.lit(True)])
    assert pkg.canonical_bytes(e1) != pkg.canonical_bytes(e2)


@pytest.mark.parametrize("dimension", ["provider", "coverage"])
def test_i5_only_keys_depending_on_a_changed_material_dimension_change(dimension: str) -> None:
    pkg = s.assessment()

    def need_n(req: dict[str, Any]) -> None:
        s.require(req, "N")

    base = s.run(pkg, s.build(pkg, need_n))
    if dimension == "provider":
        changed = s.run(pkg, s.build(pkg, need_n, lambda r: r["context"].update(provider="R2")))
        same, moved = "M", "N"
    else:
        def cov(r: dict[str, Any]) -> None:
            r["context"]["coverages"][0]["version"] = 2
            r["evidence"][0]["subject"]["coverage_version"] = 2
        changed = s.run(pkg, s.build(pkg, need_n, cov))
        same, moved = None, "M"
    key = {q["type"]: q["key"] for q in base["questions"]}
    key2 = {q["type"]: q["key"] for q in changed["questions"]}
    assert key[moved] != key2[moved]
    if same:
        assert key[same] == key2[same]


def test_i5_multiple_evidence_links_to_one_scoped_obligation_yield_one_obligation() -> None:
    pkg = s.assessment()

    def m(req: dict[str, Any]) -> None:
        s.require(req, "B")
        for i in (1, 2):
            req["evidence"].append(s.evidence(f"E-B{i}", "B", {"kind": "unknown"},
                                              scope={"service_id": "S1",
                                                     "service_date": "2026-11-30",
                                                     "payer": "PAYER_A",
                                                     "product": "PROD_A"}))

    result = s.run(pkg, s.build(pkg, m))
    assert len([o for o in s.open_obligations(result) if o["type"] == "verification"]) == 1


# ---------------------------------------------------------------- I6 types and replay


def test_i6_zero_amount_keeps_currency_and_scale_absent_is_distinct() -> None:
    pkg = s.assessment()
    zero = {"minor": 0, "currency": "USD", "period": "visit", "scope": "individual",
            "tier": "in_network"}

    def with_amount(amount: Any) -> Any:
        def m(req: dict[str, Any]) -> None:
            s.require(req, "B", requiredness="informational")
            ev = s.evidence("E-B1", "B", {"kind": "covered"},
                            scope={"service_id": "S1", "service_date": "2026-11-30",
                                   "payer": "PAYER_A", "product": "PROD_A"})
            if amount is not None:
                ev["amount"] = amount
            req["evidence"].append(ev)
        return m

    zero_q = s.question(s.run(pkg, s.build(pkg, with_amount(zero))), "B")
    absent_q = s.question(s.run(pkg, s.build(pkg, with_amount(None))), "B")
    assert zero_q["amount"] == zero
    assert absent_q.get("amount") is None


@pytest.mark.parametrize("raw,code", [
    (b'{"a": 1.5}', "float_value"),
    (b'{"a": 1, "a": 2}', "duplicate_key"),
    (b'{"a": "\xff\xfe"}', "malformed_unicode"),
], ids=["float", "duplicate-key", "malformed-utf8"])
def test_i6_strict_reader_rejects_ambiguous_encodings(raw: bytes, code: str) -> None:
    pkg = s.assessment()
    with pytest.raises(pkg.CanonicalError) as err:
        pkg.parse_json(raw)
    assert err.value.code == code


def test_i6_binary_float_and_over_precision_instant_are_rejected() -> None:
    pkg = s.assessment()
    with pytest.raises(pkg.CanonicalError) as err:
        pkg.canonical_bytes({"a": 0.1})
    assert err.value.code == "float_value"
    req = s.build(pkg, lambda r: r.update(evaluation_instant="2026-11-26T17:00:00.0000001Z"))
    result = pkg.evaluate(req, zones=s.zones(pkg))
    assert result["status"] == "rejected" and result["rejection"] == {
        "code": "instant_precision"}


def test_i6_conflicting_versions_under_the_same_id_are_rejected() -> None:
    pkg = s.assessment()
    req = s.build(pkg, lambda r: r["evidence"].append(
        s.membership(value={"kind": "inactive"})))  # same evidence_id E-M1, version 1
    result = pkg.evaluate(req, zones=s.zones(pkg))
    assert result["status"] == "rejected"
    assert result["rejection"] == {"code": "conflicting_record_version"}


def test_i6_missing_snapshot_on_replay_is_a_replay_error() -> None:
    pkg = s.assessment()
    original = s.run(pkg, s.build(pkg))
    record = copy.deepcopy(original["replay_record"])
    del record["request"]["snapshots"]["policy"]
    with pytest.raises(pkg.ReplayError) as err:
        pkg.replay(record, zones=s.zones(pkg))
    assert err.value.code == "missing_content"


# ---------------------------------------------------------------- I7 unknown rule results


def test_i7_rule_without_unknown_mapping_is_rejected() -> None:
    pkg = s.assessment()
    bundle = s.base_bundle()
    del bundle["rules"][1]["unknown"]
    with pytest.raises(pkg.BundleRejected) as err:
        pkg.validate_bundle(bundle)
    assert err.value.code == "missing_unknown_outcome"


@pytest.mark.parametrize("evidence_over,reason", [
    ({"observed_at": None}, "observation_time_unknown"),
])
def test_i7_valid_rule_returns_its_declared_unknown_outcome(evidence_over: dict[str, Any],
                                                            reason: str) -> None:
    pkg = s.assessment()
    result = s.run(pkg, s.build(pkg, lambda r: r.update(
        evidence=[s.membership(**evidence_over)])))
    s.assert_q(result, "M", "active", "insufficient", reason)


@pytest.mark.parametrize("value,finding,adequacy,reason", [
    ({"kind": "unknown"}, "unknown", "missing", "answer_absent"),
    ({"kind": "unsupported"}, "unsupported", "insufficient", "operation_unsupported"),
])
def test_i7_absent_and_unsupported_inputs_stay_distinct(value: dict[str, Any], finding: str,
                                                        adequacy: str, reason: str) -> None:
    pkg = s.assessment()
    result = s.run(pkg, s.build(pkg, lambda r: r.update(evidence=[s.membership(value=value)])))
    s.assert_q(result, "M", finding, adequacy, reason)


def test_i7_logic_with_unknown() -> None:
    pkg = s.assessment()
    view: dict[str, Any] = {}
    assert pkg.eval_expr(s.expr("or", args=[s.lit(True), s.field("x")]), view) is True
    assert pkg.eval_expr(s.expr("and", args=[s.lit(False), s.field("x")]), view) is False
    assert pkg.eval_expr(s.expr("not", arg=s.field("x")), view) is pkg.UNKNOWN


# ---------------------------------------------------------------- I8 time boundary


@pytest.mark.parametrize("observed,expected", [
    ("2026-11-19T17:00:00.000000Z", ("active", "adequate", "current_member")),   # exactly 7 d
    ("2026-11-19T16:59:59.999999Z", ("active", "stale", "window_expired")),      # 7 d + 1 us
])
def test_i8_seven_day_inclusive_boundary(observed: str, expected: tuple[str, str, str]) -> None:
    pkg = s.assessment()
    result = s.run(pkg, s.build(pkg, lambda r: r.update(evidence=[
        s.membership(observed_at=observed, retrieved_at=observed)])))
    s.assert_q(result, "M", *expected)


def test_i8_reimport_of_over_limit_observation_is_still_stale() -> None:
    pkg = s.assessment()
    result = s.run(pkg, s.build(pkg, lambda r: r.update(evidence=[
        s.membership(observed_at="2026-11-19T16:59:59.999999Z", retrieved_at=s.EVAL)])))
    s.assert_q(result, "M", "active", "stale", "window_expired")


# ---------------------------------------------------------------- resource limits


def _depth_tree(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = s.lit(True)
    for _ in range(depth - 1):
        node = s.expr("not", arg=node)
    return node


def _node_tree(nodes: int) -> dict[str, Any]:
    """Shallow balanced tree: one `and` root over (nodes - 1) literal leaves."""
    return s.expr("and", args=[s.lit(True)] * (nodes - 1))


def _collection(size: int) -> dict[str, Any]:
    return s.expr("in", item=s.lit("x"), set=s.lit([f"v{i}" for i in range(size)]))


def _bundle_with(when: dict[str, Any]) -> dict[str, Any]:
    bundle = s.base_bundle()
    bundle["rules"].append(s.rule("LIMIT", "acceptance", when, {"outcome": "accept"},
                                  {"outcome": "reject", "reason": "x"}, types=("N",)))
    return bundle


def test_limits_are_pinned_to_the_evaluator_version() -> None:
    pkg = s.assessment()
    assert dict(pkg.LIMITS) == {"depth": 16, "nodes": 256, "collection": 128}
    assert pkg.EVALUATOR_VERSION == "d1.1"


@pytest.mark.parametrize("kind,build,below,at,above", [
    ("depth", _depth_tree, 15, 16, 17),
    ("nodes", _node_tree, 255, 256, 257),
    ("collection", _collection, 127, 128, 129),
])
def test_limits_below_at_and_above(kind: str, build: Any, below: int, at: int,
                                   above: int) -> None:
    pkg = s.assessment()
    pkg.validate_bundle(_bundle_with(build(below)))
    pkg.validate_bundle(_bundle_with(build(at)))
    with pytest.raises(pkg.BundleRejected) as err:
        pkg.validate_bundle(_bundle_with(build(above)))
    assert err.value.code == f"limit_{kind}"


@pytest.mark.parametrize("mutate,code", [
    (lambda b: b["rules"].append({"rule_id": "BAD", "criterion": "acceptance"}),
     "malformed_rule"),
    (lambda b: b["rules"].append(s.rule("UNK", "acceptance", s.expr("frobnicate"),
                                        {"outcome": "accept"},
                                        {"outcome": "reject", "reason": "x"})),
     "unknown_operator"),
    (lambda b: b["rules"].extend([
        s.rule("CYC-1", "acceptance", s.lit(True), {"outcome": "accept"},
               {"outcome": "reject", "reason": "x"}, depends_on=("CYC-2",)),
        s.rule("CYC-2", "acceptance", s.lit(True), {"outcome": "accept"},
               {"outcome": "reject", "reason": "x"}, depends_on=("CYC-1",))]),
     "dependency_cycle"),
    (lambda b: b["rules"].append(s.rule("DEP", "acceptance", s.lit(True),
                                        {"outcome": "accept"},
                                        {"outcome": "reject", "reason": "x"},
                                        depends_on=("ABSENT",))),
     "missing_dependency"),
    (lambda b: b["rules"].append(s.rule("TYPE", "acceptance",
                                        s.expr("lt", left=s.lit("a"), right=s.lit(1)),
                                        {"outcome": "accept"},
                                        {"outcome": "reject", "reason": "x"})),
     "type_mismatch"),
], ids=["malformed", "unknown-operator", "cycle", "unavailable-dependency", "type-mismatch"])
def test_bundle_rejection_cases(mutate: Any, code: str) -> None:
    pkg = s.assessment()
    bundle = s.base_bundle()
    mutate(bundle)
    with pytest.raises(pkg.BundleRejected) as err:
        pkg.validate_bundle(bundle)
    assert err.value.code == code
