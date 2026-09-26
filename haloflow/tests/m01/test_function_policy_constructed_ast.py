"""CG-3 (CP1 carried gap): the seven constructed C-* cases, as permanent unit tests.

Each case injects one AST state the parser will not emit into a deep copy of a real parse of an
admitted control, through test-side wrappers around existing private seams of
`validate_function_installation`, and asserts: the exact refusal code; the innermost validator
that raised; that the exception observed there is the one the caller caught; that
`function_checksum` was never reached; the exact single-path change the mutation made; and that
every wrapper, the original parse and class metadata are restored. Controls prove the seam is
transparent and that every mutation has teeth (R7).

Scope: C-return-bounds, C-column-bounds, C-column-setof, C-return-record, C-identity-dual,
C-unknown-enum, C-unknown-slot. C-nul-rendered is out of scope (discharged by citation, Q-1).

Frozen boundary: no production code, fixture, conftest or configuration change; this file only
reads `fixtures/function_policy/cases.json` and `controls/C01|C02|C04.json`.

Traceability: CG-3 requirements v2 (3e011e4c...), architecture v1 (4ce321ad...), test cases v1
(c323e570...); derived from the measured harness v4 (7d299c5d...) with deviations D-1..D-8.
Authorship: written by Claude; independently reviewed by Codex (review, not independent
authorship). A failing case is a finding to escalate; it is never edited to match an observation.
"""

from __future__ import annotations

import copy
import enum
import hashlib
import json
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pglast.ast as A
import pytest
from pglast.enums import FunctionParameterMode as MODE

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import function_policy as S

# ---------------------------------------------------------------- fixed inputs and expectations
ROOT = Path(__file__).parent / "fixtures" / "function_policy"
SCHEMA = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))["schema_key"]
HELPERS = (
    "_node",
    "_typename",
    "_parameters",
    "_options",
    "_validate_local_statements",
    "_validate_statement_inventory",
    "_validate_ast",
)
WRAPPED = ("_parse_exact_sql", "function_checksum") + HELPERS
EXPECT = {  # (control, code, innermost helper) — accepted v3 design + MUTATION_SPEC
    "C-return-bounds": ("C04", "INSTALL_TYPE_SHAPE_FORBIDDEN", "_typename"),
    "C-column-bounds": ("C04", "INSTALL_TYPE_SHAPE_FORBIDDEN", "_typename"),
    "C-column-setof": ("C02", "INSTALL_TYPE_SHAPE_FORBIDDEN", "_typename"),
    "C-return-record": ("C02", "INSTALL_TYPE_SHAPE_FORBIDDEN", "_parameters"),
    "C-identity-dual": ("C02", "INSTALL_SIGNATURE_MISMATCH", "_validate_local_statements"),
    "C-unknown-enum": ("C02", "INSTALL_PARAMETER_MODE_FORBIDDEN", "_parameters"),
    "C-unknown-slot": ("C02", "INSTALL_AST_INVALID", "_node"),
}
VALUE_IDS = (
    "C-column-bounds",
    "C-column-setof",
    "C-identity-dual",
    "C-return-bounds",
    "C-return-record",
    "C-unknown-enum",
)
CONTROLS = ("C01", "C02", "C04")
EXPECTED_SETTER_EXCEPTION = "ValueError"  # v3 §4 observation; a different class is a finding
MUTATION_SPEC_REVOKE_LOCATION = 323


class AdapterInability(Exception):
    """Setter/copy/construction/shape/invariance failure before the validator. Never a refusal."""


class AliasingDetected(Exception):
    """Raised only by the aliasing control, after its exact extra objargs change is confirmed."""

    def __init__(self, diff: list) -> None:
        super().__init__("aliasing control: objargs changed as deliberately induced")
        self.diff = diff


def control(identifier: str) -> dict[str, Any]:
    return json.loads((ROOT / "controls" / (identifier + ".json")).read_text(encoding="utf-8"))


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


# ---------------------------------------------------------------- serializer with coverage check
BOOKKEEPING_SLOTS = frozenset({"ancestors"})  # pglast.ast.Node base slot; not an AST field


def _declared(klass: type) -> tuple[str, ...]:
    own = klass.__dict__.get("__slots__", ())
    if isinstance(own, str):
        own = (own,)
    return tuple(own)  # pglast concrete classes declare a dict of field -> SlotTypeInfo


def slots_of(cls: type) -> tuple[str, ...]:
    """The concrete class's own declared fields, with strict MRO coverage.

    Every slot declared anywhere else in the MRO must be a known bookkeeping slot
    (only `ancestors`); anything else is an unsupported shape.
    """
    own = _declared(cls)
    inherited = {n for k in cls.__mro__[1:] for n in _declared(k)} - {"__weakref__", "__dict__"}
    extra = inherited - set(own) - BOOKKEEPING_SLOTS
    if extra:
        raise AdapterInability(
            f"unsupported shape: {cls.__name__} inherits undeclared slots {sorted(extra)}"
        )
    return own


def _check_bookkeeping(value: Any) -> None:
    for name in BOOKKEEPING_SLOTS:
        if name in _declared(type(value)):
            continue
        try:
            getattr(value, name)
        except AttributeError:
            continue  # verified unset
        raise AdapterInability(
            f"unsupported shape: bookkeeping slot {name!r} is set on {type(value).__name__}"
        )


def ser(value: Any) -> Any:
    if isinstance(value, A.Node):
        if hasattr(value, "__dict__") and value.__dict__:
            raise AdapterInability(
                f"unsupported shape: {type(value).__name__} has instance __dict__"
            )
        _check_bookkeeping(value)
        return {
            "@": type(value).__name__,
            **{s: ser(getattr(value, s)) for s in slots_of(type(value))},
        }
    if isinstance(value, (list, tuple)):
        return [ser(v) for v in value]
    if isinstance(value, enum.Enum):
        return {"enum": type(value).__name__, "name": value.name}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise AdapterInability(f"unsupported value type in tree: {type(value).__name__}")


def schema_of(value: Any, out: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Class -> declared fields, for every node class present (recorded as evidence)."""
    out = {} if out is None else out
    if isinstance(value, A.Node):
        out.setdefault(type(value).__name__, list(slots_of(type(value))))
        for s in slots_of(type(value)):
            schema_of(getattr(value, s), out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            schema_of(v, out)
    return out


def diff(a: Any, b: Any, path: tuple = ()) -> list[tuple[tuple, Any, Any]]:
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        return [d for k in a for d in diff(a[k], b[k], path + (k,))]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [
            d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in diff(x, y, path + (i,))
        ]
    return [] if a == b else [(path, a, b)]


def require(cond: bool, what: str) -> None:
    if not cond:
        raise AdapterInability(what)


def inventory(nodes: Any, column: str | None) -> None:
    """One CREATE, then REVOKE, then GRANT; with `column`, exactly one TABLE column so named."""
    require(len(nodes) == 3, f"expected 3 statements, found {len(nodes)}")
    kinds = [type(n.stmt).__name__ for n in nodes]
    require(kinds == ["CreateFunctionStmt", "GrantStmt", "GrantStmt"], f"inventory {kinds!r}")
    require(
        nodes[1].stmt.is_grant is False and nodes[2].stmt.is_grant is True, "REVOKE/GRANT order"
    )
    if column is not None:
        table_param(nodes[0].stmt, column)


def table_param(stmt: Any, column: str) -> tuple[int, Any]:
    hits = [
        (i, p)
        for i, p in enumerate(stmt.parameters or ())
        if p.mode == MODE.FUNC_PARAM_TABLE and p.name == column
    ]
    require(len(hits) == 1, f"expected one TABLE column {column!r}, found {len(hits)}")
    return hits[0]


# ---------------------------------------------------------------- the observed run
class Patches:
    def __init__(self) -> None:
        self.saved: list[tuple[Any, str, Any]] = []

    def set(self, obj: Any, name: str, value: Any) -> None:
        self.saved.append(
            (obj, name, obj.__dict__[name] if isinstance(obj, type) else getattr(obj, name))
        )
        setattr(obj, name, value)

    def restore(self) -> None:
        """Attempt every restoration; re-raise the first error only after all attempts."""
        first: BaseException | None = None
        for obj, name, value in reversed(self.saved):
            try:
                setattr(obj, name, value)
            except BaseException as exc:
                first = first or exc
        self.saved.clear()
        if first is not None:
            raise first


def run(
    payload: dict[str, Any], seam: Callable, state: dict[str, Any], *, count_checksum: bool = True
) -> dict[str, Any]:
    """Real checker, `seam` replacing `_parse_exact_sql`; always restores; truthful categories."""
    obs: dict[str, Any] = {
        "category": None,
        "code": None,
        "raised_in": [],
        "checksum_calls": 0,
        "identity": None,
        "detail": None,
    }
    originals = {name: getattr(S, name) for name in WRAPPED}
    patches = Patches()
    real_parse, real_checksum = originals["_parse_exact_sql"], originals["function_checksum"]

    def counting(*args: Any, **kwargs: Any) -> Any:
        if count_checksum:
            obs["checksum_calls"] += 1
        return real_checksum(*args, **kwargs)

    try:
        patches.set(S, "_parse_exact_sql", lambda p, s: seam(real_parse, p, s))
        patches.set(S, "function_checksum", counting)
        for name in HELPERS:

            def spy(*args: Any, _real: Any = originals[name], _name: str = name, **kw: Any) -> Any:
                try:
                    return _real(*args, **kw)
                except MigrationUnitRejected as error:
                    obs["raised_in"].append((_name, id(error)))
                    raise

            patches.set(S, name, spy)
        try:
            S.validate_function_installation(encode(payload), schema_key=SCHEMA)
            obs["category"] = "accepted"
        except MigrationUnitRejected as error:
            obs["category"] = "refused"
            obs["code"] = str(error.reason_code)
            obs["identity"] = bool(obs["raised_in"]) and obs["raised_in"][0][1] == id(error)
        except AliasingDetected as exc:
            obs["category"] = "aliasing_detected"
            obs["detail"] = exc.diff
        except AdapterInability as exc:
            obs["category"] = "adapter_inability"
            obs["detail"] = str(exc)
        except Exception:  # anything else is reported as found, with its traceback
            obs["category"] = "unexpected_exception"
            obs["detail"] = traceback.format_exc()
    finally:
        try:
            patches.restore()
        finally:  # the slot restore runs even if another restoration raised
            state.get("restore", lambda: None)()
    obs["wrappers_restored"] = all(getattr(S, n) is originals[n] for n in WRAPPED)
    if "parent" in state:  # post-checker parent invariance, on every path
        obs["parent_unchanged_after_checker"] = ser(state["parent"]) == state["parent_snap"]
    obs["innermost"] = obs["raised_in"][0][0] if obs["raised_in"] else None
    obs["raised_in"] = [n for n, _ in obs["raised_in"]]
    return obs


def case_ok(obs: dict[str, Any], case: str) -> bool:
    """The exact predicate every case, and every R7 experiment, is judged by."""
    _, code, helper = EXPECT[case]
    return (
        obs["category"] == "refused"
        and obs["code"] == code
        and obs["innermost"] == helper
        and obs["identity"] is True
        and obs["checksum_calls"] == 0
        and obs["wrappers_restored"] is True
        and obs.get("parent_unchanged_after_checker") is True
    )


def transparency_ok(obs: dict[str, Any]) -> bool:
    return (
        obs["category"] == "accepted"
        and obs["checksum_calls"] == 1
        and obs["wrappers_restored"] is True
        and obs.get("parent_unchanged_after_checker") is True
    )


def value_seam(mutate: Callable | None, state: dict[str, Any], column: str | None) -> Callable:
    def seam(real_parse: Any, parser: Any, sql: bytes) -> Any:
        parent = real_parse(parser, sql)
        state["parent"], state["parent_snap"] = parent, ser(parent)
        inventory(parent, column)
        nodes = copy.deepcopy(parent)
        if mutate is not None:
            try:
                mutate(nodes)
            except (AdapterInability, AliasingDetected):
                raise
            except Exception as exc:
                raise AdapterInability(
                    f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                ) from exc
        require(ser(parent) == state["parent_snap"], "original parent changed by the adapter")
        return nodes

    return seam


def expect_diff(before: Any, after: Any, expected: list, case: str) -> list:
    got = diff(before, after)
    require(got == expected, f"{case}: serialized diff {got!r} != expected {expected!r}")
    return got


# ---------------------------------------------------------------- six value adapters
def m_return_bounds(nodes: Any) -> None:
    rt = nodes[0].stmt.returnType
    require(
        rt.arrayBounds is not None
        and len(rt.arrayBounds) == 1
        and type(rt.arrayBounds[0]) is A.Integer
        and rt.arrayBounds[0].ival == -1,
        "returnType.arrayBounds is not exactly one Integer(-1)",
    )
    before = ser(nodes)
    rt.arrayBounds[0].ival = 2
    expect_diff(
        before,
        ser(nodes),
        [((0, "stmt", "returnType", "arrayBounds", 0, "ival"), -1, 2)],
        "C-return-bounds",
    )


def m_column_bounds(nodes: Any) -> None:
    i, p = table_param(nodes[0].stmt, "items")
    b = p.argType.arrayBounds
    require(
        b is not None and len(b) == 1 and type(b[0]) is A.Integer and b[0].ival == -1,
        "column arrayBounds is not exactly one Integer(-1)",
    )
    before = ser(nodes)
    b[0].ival = 2
    expect_diff(
        before,
        ser(nodes),
        [((0, "stmt", "parameters", i, "argType", "arrayBounds", 0, "ival"), -1, 2)],
        "C-column-bounds",
    )


def m_column_setof(nodes: Any) -> None:
    i, p = table_param(nodes[0].stmt, "operation_id")
    require(
        p.argType.setof is False and nodes[0].stmt.returnType.setof is True, "setof preconditions"
    )
    before = ser(nodes)
    p.argType.setof = True
    expect_diff(
        before,
        ser(nodes),
        [((0, "stmt", "parameters", i, "argType", "setof"), False, True)],
        "C-column-setof",
    )


def m_return_record(nodes: Any) -> None:
    rt = nodes[0].stmt.returnType
    require([n.sval for n in rt.names] == ["uuid"], "returnType.names is not exactly [uuid]")
    before = ser(nodes)
    rt.names = (A.String(sval="pg_catalog"), A.String(sval="record"))
    expect_diff(
        before,
        ser(nodes),
        [
            (
                (0, "stmt", "returnType", "names"),
                before[0]["stmt"]["returnType"]["names"],
                ser(rt.names),
            )
        ],
        "C-return-record",
    )


IDENTITY_PATH = (1, "stmt", "objects", 0, "objfuncargs", 0, "argType", "names", 0, "sval")
ALIAS_PATH = (1, "stmt", "objects", 0, "objargs", 0, "names", 0, "sval")


def m_identity_dual(nodes: Any, *, alias_control: bool = False) -> None:
    stmt = nodes[1].stmt
    require(type(stmt) is A.GrantStmt and stmt.is_grant is False, "ast[1] is not the REVOKE")
    require(
        len(stmt.objects) == 1 and type(stmt.objects[0]) is A.ObjectWithArgs, "one ObjectWithArgs"
    )
    obj = stmt.objects[0]
    require(obj.objname[-1].sval == "m02_lock_operation", "wrong function identity")
    require(
        bool(obj.objargs) and bool(obj.objfuncargs) and obj.args_unspecified is False,
        "identity representations not both populated",
    )
    original = obj.objfuncargs[0].argType
    fresh = A.TypeName(
        names=(A.String(sval="text"),),
        setof=False,
        pct_type=False,
        typmods=None,
        typemod=-1,
        arrayBounds=None,
        location=original.location,
    )
    require(fresh is not original and fresh is not obj.objargs[0], "fresh TypeName aliases")
    before = ser(nodes)
    obj.objfuncargs[0].argType = fresh
    expected = [(IDENTITY_PATH, "uuid", "text")]
    if not alias_control:
        expect_diff(before, ser(nodes), expected, "C-identity-dual")
        return
    obj.objargs[0].names = (A.String(sval="text"),)  # deliberate aliasing-style corruption
    got = diff(before, ser(nodes))
    require(
        sorted(got, key=repr) == sorted(expected + [(ALIAS_PATH, "uuid", "text")], key=repr),
        f"aliasing control: diff {got!r} is not exactly the identity plus the objargs change",
    )
    raise AliasingDetected(got)


def m_unknown_enum(nodes: Any) -> None:
    i, p = table_param(nodes[0].stmt, "operation_id")
    before = ser(nodes)
    object.__setattr__(p, "mode", 99)
    require(p.mode == 99, "injection did not place 99")
    expect_diff(
        before,
        ser(nodes),
        [
            (
                (0, "stmt", "parameters", i, "mode"),
                {"enum": "FunctionParameterMode", "name": "FUNC_PARAM_TABLE"},
                99,
            )
        ],
        "C-unknown-enum",
    )


ADAPTERS = {
    "C-return-bounds": (m_return_bounds, "items"),
    "C-column-bounds": (m_column_bounds, "items"),
    "C-column-setof": (m_column_setof, "operation_id"),
    "C-return-record": (m_return_record, "operation_id"),
    "C-identity-dual": (m_identity_dual, "operation_id"),
    "C-unknown-enum": (m_unknown_enum, "operation_id"),
}
COLUMN = {"C01": None, "C02": "operation_id", "C04": "items"}  # C01 RETURNS void: no TABLE column


# ---------------------------------------------------------------- slot adapter (R4a)
def typename_meta() -> dict[str, Any]:
    d = A.TypeName.__dict__
    return {
        "slots_obj": d["__slots__"],
        "slots": tuple(d["__slots__"]),
        "ids": {k: id(v) for k, v in d.items()},
    }


def slot_seam(state: dict[str, Any], *, apply_patch: bool, fail_inside: bool = False) -> Callable:
    patches = Patches()
    state["restore"] = patches.restore

    def seam(real_parse: Any, parser: Any, sql: bytes) -> Any:
        parent = real_parse(parser, sql)
        state["parent"], state["parent_snap"] = parent, ser(parent)
        inventory(parent, "operation_id")
        nodes = copy.deepcopy(parent)
        state["nodes"], state["nodes_snap"] = nodes, ser(nodes)
        state["meta_before"] = typename_meta()
        if apply_patch:  # window: only the checker/observation path runs until restore
            patches.set(
                A.TypeName, "__slots__", state["meta_before"]["slots"] + ("unrecognised_slot",)
            )
            require(
                tuple(A.TypeName.__slots__)
                == state["meta_before"]["slots"] + ("unrecognised_slot",),
                "slot patch not exactly original + one",
            )
            if fail_inside:
                raise AdapterInability("deliberate failure inside the patched window")
        return nodes

    return seam


def slot_restored(state: dict[str, Any]) -> dict[str, bool]:
    after = typename_meta()
    before = state["meta_before"]
    typenames = []

    def walk(v: Any) -> None:
        if isinstance(v, A.Node):
            _check_bookkeeping(v)
            if type(v).__name__ == "TypeName":
                typenames.append(v)
            for s in slots_of(type(v)):
                walk(getattr(v, s))
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(state["nodes"])
    return {
        "slots_object_identity": after["slots_obj"] is before["slots_obj"],
        "slots_tuple": after["slots"] == before["slots"],
        "class_dict_identities": after["ids"] == before["ids"],
        "parent_unchanged": ser(state["parent"]) == state["parent_snap"],
        "instances_unchanged": ser(state["nodes"]) == state["nodes_snap"],
        "instance_types_exact": all(type(t) is A.TypeName for t in typenames) and bool(typenames),
    }


# ================================================================ tests
CONTROL_SHA256 = {
    "C01": "2bdc9c52643cd6d994ddfc24be27c0b39bda808eec0b4c7fef3024bba69299a7",
    "C02": "2ad94899272b72ae02a6277cd1ae4f28d8c1414abad5aa57bae0dfdd6edd63d9",
    "C04": "660991a1d84820a3f2569ce2dd9ca276bd6b9928424346c1cef49ffcfd61b01b",
}


def test_tc00_inputs_bound() -> None:
    for cid, digest in CONTROL_SHA256.items():
        data = (ROOT / "controls" / (cid + ".json")).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, cid
    assert SCHEMA == "tenant_aaaaaaaa"


@pytest.mark.parametrize("cid", CONTROLS)
def test_tc01_control_admits_plain(cid: str) -> None:
    S.validate_function_installation(encode(control(cid)), schema_key=SCHEMA)


@pytest.mark.parametrize("cid", CONTROLS)
def test_tc02_control_transparent(cid: str) -> None:
    state: dict[str, Any] = {}
    obs = run(control(cid), value_seam(None, state, COLUMN[cid]), state)
    assert transparency_ok(obs), obs


@pytest.mark.parametrize("cid", CONTROLS)
def test_tc03_counter_teeth(cid: str) -> None:
    state: dict[str, Any] = {}
    obs = run(control(cid), value_seam(None, state, COLUMN[cid]), state, count_checksum=False)
    assert obs["category"] == "accepted", obs
    assert obs["checksum_calls"] == 0, obs
    assert obs["wrappers_restored"] is True, obs
    assert obs.get("parent_unchanged_after_checker") is True, obs
    assert transparency_ok(obs) is False, obs


@pytest.mark.parametrize("cid", CONTROLS)
def test_tc04_serializer_coverage(cid: str) -> None:
    state: dict[str, Any] = {}
    obs = run(control(cid), value_seam(None, state, COLUMN[cid]), state)
    assert obs["category"] == "accepted", obs  # a serializer failure is kept in obs["detail"]
    schema = schema_of(state["parent"])
    assert schema, f"no node classes observed; schema={schema!r}"


@pytest.mark.parametrize("case", VALUE_IDS)
def test_tc10_value_case(case: str) -> None:
    adapter, column = ADAPTERS[case]
    state: dict[str, Any] = {}
    obs = run(control(EXPECT[case][0]), value_seam(adapter, state, column), state)
    assert case_ok(obs, case), obs


def test_tc11_unknown_slot() -> None:
    state: dict[str, Any] = {}
    obs = run(control("C02"), slot_seam(state, apply_patch=True), state)
    restored = slot_restored(state)
    assert case_ok(obs, "C-unknown-slot"), obs
    assert all(restored.values()), restored


def test_tc12_slot_restoration_on_failure() -> None:
    state: dict[str, Any] = {}
    obs = run(control("C02"), slot_seam(state, apply_patch=True, fail_inside=True), state)
    restored = slot_restored(state)
    assert obs["category"] == "adapter_inability", obs
    assert obs["wrappers_restored"] is True, obs
    assert all(restored.values()), restored


def test_tc13_enum_setter_rejected() -> None:
    parent = S._parse_exact_sql(
        S._load_parser(), S._render_exact_sql(control("C02")["template"], SCHEMA)
    )
    probe = copy.deepcopy(table_param(parent[0].stmt, "operation_id")[1])
    snap = ser(probe)
    raised = None
    try:
        probe.mode = 99
    except Exception as exc:
        raised = type(exc).__name__
    assert raised == EXPECTED_SETTER_EXCEPTION, raised
    assert ser(probe) == snap


@pytest.mark.parametrize("case", VALUE_IDS)
def test_tc20_mutation_omitted(case: str) -> None:
    state: dict[str, Any] = {}
    obs = run(control(EXPECT[case][0]), value_seam(None, state, ADAPTERS[case][1]), state)
    assert obs["category"] == "accepted" and obs["code"] is None, obs
    assert case_ok(obs, case) is False, obs
    assert transparency_ok(obs), obs


def test_tc21_slot_patch_omitted() -> None:
    state: dict[str, Any] = {}
    obs = run(control("C02"), slot_seam(state, apply_patch=False), state)
    assert obs["category"] == "accepted", obs
    assert case_ok(obs, "C-unknown-slot") is False, obs
    assert transparency_ok(obs), obs


def test_tc22_aliasing_control() -> None:
    normal_state: dict[str, Any] = {}
    normal = run(
        control("C02"), value_seam(m_identity_dual, normal_state, "operation_id"), normal_state
    )
    assert case_ok(normal, "C-identity-dual"), normal
    state: dict[str, Any] = {}
    alias = run(
        control("C02"),
        value_seam(lambda n: m_identity_dual(n, alias_control=True), state, "operation_id"),
        state,
    )
    assert alias["category"] == "aliasing_detected", alias
    assert alias["wrappers_restored"] is True, alias
    assert sorted(alias["detail"], key=repr) == sorted(
        [(IDENTITY_PATH, "uuid", "text"), (ALIAS_PATH, "uuid", "text")], key=repr
    ), alias
