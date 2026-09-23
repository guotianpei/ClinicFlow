"""PHASE-SIGNATURE-01 Gate 3 instrument (test overlay). NOT production code.

This module is the Gate 3 INSTRUMENT frozen by owner ruling OD-SIG-24:

* Part A -- the binding resolver, run as the ordered preflight S1..S14
  (Blocker-2 seams v3 section 2, ACCEPTED with four binding conditions);
* Part B -- the observer, which replaces ``_require`` ONLY (``_fail`` is never
  patched) and records EVERY signature-coded call;
* the named, closed set of replaceable seams (Blocker-2 seams v3 section 1);
* the phase projection ``phase_for_site`` over the immutable ``PHASE_BY_SITE``
  (A-28 / A-29 -- a test-overlay seam, not an event field);
* the Part D reach markers and the Part E O11 witness instruments (I1..I4).

FORM 1 PROVENANCE. The module under observation is produced by compiling the
captured byte snapshot of ``function_policy.py`` and executing it into a fresh
module object. The code observed IS the snapshot by construction; path equality is
a diagnostic only (A-25).

Nothing here edits a file, a fixture, a protected input or production code. The
snapshot module is a separate module object; the normally imported
``haloflow.m01.provisioning.function_policy`` is never patched.

Every value measured so far is provisional Python 3.11.15; CI pins 3.12. A 3.11 /
3.12 difference is an observer portability finding, never grounds to weaken an
oracle.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import types
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haloflow.m01.errors import MigrationUnitRejected

# --------------------------------------------------------------------- locations

HERE = Path(__file__).resolve().parent
HALOFLOW_ROOT = HERE.parents[2]
POLICY_PATH = HALOFLOW_ROOT / 'src/haloflow/m01/provisioning/function_policy.py'
TESTS_M01 = HERE.parent
FIXTURE_DIR = TESTS_M01 / 'fixtures/function_policy'

# Distinct module name for the form-1 snapshot. dataclasses resolves
# ``cls.__module__`` through ``sys.modules``, so the snapshot module must be
# registered under its own name BEFORE execution. The real module keeps its entry.
SNAPSHOT_MODULE_NAME = 'haloflow.m01.provisioning.function_policy__sig_snapshot'
SNAPSHOT_PACKAGE = 'haloflow.m01.provisioning'

# ------------------------------------------------------------ expected constants
# Bound to main = 0731b520b7ee740223cf606fefd2e7e61106512f. Printing a hash is not
# binding it (A-26); these constants ARE the binding.

EXPECTED_SNAPSHOT_SHA256 = 'ecf57cab4a3e0d6128047c9654d4605e47267d7ff7f6b424fc48a7541a5afdb0'

# The ten inputs of A-26 / S2: four fixture JSONs and all six controls, keyed by
# path relative to FIXTURE_DIR. Ordered so a diagnosis names the first mismatch
# deterministically.
EXPECTED_INPUT_SHA256: Mapping[str, str] = types.MappingProxyType({
    'cases.json': '273781f27b35affa288f3dec459d52c32a84faa95cd5e41db8c2cc6d8c48dbd3',
    'sql-fixtures.json': '3d695b5cbcbd3bf7576cbf59a28d323e5dfe4eceee10bb7b014266dab94bb805',
    'expected-refusals.json':
        '7c0fe15ca54ad356f2483b17ca194223ac3cbec485051973f74eafca833a8899',
    'fixture-hashes.json': '9bae5c2ecd50d7077536348f7b2838d756af9a73d405e5b72f665e615991e29b',
    'controls/C01.json': '2bdc9c52643cd6d994ddfc24be27c0b39bda808eec0b4c7fef3024bba69299a7',
    'controls/C02.json': '2ad94899272b72ae02a6277cd1ae4f28d8c1414abad5aa57bae0dfdd6edd63d9',
    'controls/C03.json': '5778391e947ce166b60ae71ef1f099d2c4c82a3f0d642eb9bfb46ba610cb6013',
    'controls/C04.json': '660991a1d84820a3f2569ce2dd9ca276bd6b9928424346c1cef49ffcfd61b01b',
    'controls/C05.json': 'c92f3b2a8cd173847a56b0b0afa910ed69c439fbcf0bd3c04d63531c44eb28c7',
    'controls/C06.json': '76dd7ec1e0d3e798f189ee9a7144d7e53e7687b40e2b2593fe160aa684578573',
})

# A-27: protected inputs. Paths relative to HALOFLOW_ROOT.
PROTECTED_SHA256: Mapping[str, str] = types.MappingProxyType({
    'src/haloflow/m01/provisioning/function_policy.py': EXPECTED_SNAPSHOT_SHA256,
    'tests/m01/fixtures/function_policy/expected-refusals.json':
        '7c0fe15ca54ad356f2483b17ca194223ac3cbec485051973f74eafca833a8899',
    'tests/m01/fixtures/function_policy/cases.json':
        '273781f27b35affa288f3dec459d52c32a84faa95cd5e41db8c2cc6d8c48dbd3',
    'tests/m01/fixtures/function_policy/sql-fixtures.json':
        '3d695b5cbcbd3bf7576cbf59a28d323e5dfe4eceee10bb7b014266dab94bb805',
    'tests/m01/test_function_policy.py':
        'df240e641f324c0084766b57a3db8a2bd16880c6075757e7973c1c25ed07bf45',
    'tests/m01/test_function_policy_phases.py':
        '9062a5029fdc020dace20897501de3b81e493a4c44e44c5216fcbbb100fc203c',
})

# -------------------------------------------------------------- the vocabulary

UNKNOWN = 'UNKNOWN'   # never an accepted value anywhere (A-24, B-13, X1)

SIG_QUALIFIED = 'SIG.QUALIFIED'
SIG_TARGET_ARGS_UNSPEC = 'SIG.TARGET.ARGS_UNSPEC'
SIG_TARGET_DUAL_SHAPE = 'SIG.TARGET.DUAL_SHAPE'
SIG_TARGET_DUAL_EQUAL = 'SIG.TARGET.DUAL_EQUAL'
SIG_TARGET_MEMBERSHIP = 'SIG.TARGET.MEMBERSHIP'
SIG_CREATE_NAME = 'SIG.CREATE.NAME'
SIG_CREATE_IDENTITY = 'SIG.CREATE.IDENTITY'
SIG_CREATE_COMPARE = 'SIG.CREATE.COMPARE'

SITE_IDS: tuple[str, ...] = (
    SIG_QUALIFIED, SIG_TARGET_ARGS_UNSPEC, SIG_TARGET_DUAL_SHAPE, SIG_TARGET_DUAL_EQUAL,
    SIG_TARGET_MEMBERSHIP, SIG_CREATE_NAME, SIG_CREATE_IDENTITY, SIG_CREATE_COMPARE,
)

EDGE_CREATE = 'EDGE.CREATE.QUALIFIED'
EDGE_TARGET = 'EDGE.TARGET.QUALIFIED'
EDGE_INVENTORY = 'EDGE.INVENTORY.QUALIFIED'
EDGE_IDS: tuple[str, ...] = (EDGE_CREATE, EDGE_TARGET, EDGE_INVENTORY)

STAGES: tuple[str, ...] = tuple(f'S{n}' for n in range(1, 15))

ARGUMENT_FORMS: frozenset[str] = frozenset({'positional', 'keyword'})


class Diag:
    """Named diagnosis constants. Asserted by EQUALITY only, never by substring."""

    # preflight (Blocker-2 seams v3 section 3; B-24 contract v3 section 2.2)
    SNAPSHOT_MISMATCH = 'SIG.PREFLIGHT.SNAPSHOT_MISMATCH'          # S1  (A-23a)
    INPUT_HASH_MISMATCH = 'SIG.PREFLIGHT.INPUT_HASH_MISMATCH'      # S2  (A-23b)
    PARSE_FAILED = 'SIG.PREFLIGHT.PARSE_FAILED'                    # S3
    SITE_COUNT_INVALID = 'SIG.PREFLIGHT.SITE_COUNT_INVALID'        # S5  (A-19)
    CODE_UNCLASSIFIABLE = 'SIG.PREFLIGHT.CODE_UNCLASSIFIABLE'      # S6  (A-22)
    DIRECT_FAIL_PRESENT = 'SIG.PREFLIGHT.DIRECT_FAIL_PRESENT'      # S7  (A-20)
    SITE_UNRESOLVED = 'SIG.PREFLIGHT.SITE_UNRESOLVED'              # S9  (A-16)
    SITE_AMBIGUOUS = 'SIG.PREFLIGHT.SITE_AMBIGUOUS'                # S9  (A-17)
    SITE_UNCLAIMED = 'SIG.PREFLIGHT.SITE_UNCLAIMED'                # S10 (A-18)
    EDGE_UNIVERSE_INVALID = 'SIG.PREFLIGHT.EDGE_UNIVERSE_INVALID'  # S12
    EDGE_UNRESOLVED = 'SIG.PREFLIGHT.EDGE_UNRESOLVED'              # S13 (A-21a)
    EDGE_AMBIGUOUS = 'SIG.PREFLIGHT.EDGE_AMBIGUOUS'                # S13 (A-21b)
    BINDING_UNRESOLVED = 'SIG.PREFLIGHT.BINDING_UNRESOLVED'        # S14 (M4 / B-24)

    # observation (Blocker-2 seams v3 section 4)
    FRAME_ABSENT = 'SIG.OBS.FRAME_ABSENT'                          # B-14
    FRAME1_UNEXPECTED = 'SIG.OBS.FRAME1_UNEXPECTED'                # B-15
    FRAME2_UNEXPECTED = 'SIG.OBS.FRAME2_UNEXPECTED'                # B-16
    OBS_SITE_UNMAPPED = 'SIG.OBS.SITE_UNMAPPED'                    # B-17
    OBS_SITE_AMBIGUOUS = 'SIG.OBS.SITE_AMBIGUOUS'                  # B-18
    OBS_EDGE_UNMAPPED = 'SIG.OBS.EDGE_UNMAPPED'                    # B-19a
    OBS_EDGE_AMBIGUOUS = 'SIG.OBS.EDGE_AMBIGUOUS'                  # B-19b
    OBS_CODE_UNCLASSIFIABLE = 'SIG.OBS.CODE_UNCLASSIFIABLE'        # B-20


class PreflightAbort(Exception):
    """A preflight stage failed. Carries the EXACT COMPLETED PREFIX of stages."""

    def __init__(self, diagnosis: str, ledger: tuple[str, ...], **details: Any) -> None:
        super().__init__(diagnosis)
        self.diagnosis = diagnosis
        self.ledger = ledger
        self.details = details


class ObservationAbort(Exception):
    """A live observation could not be resolved. Fail closed; never a result.

    Deliberately NOT a subclass of MigrationUnitRejected, ValueError, UnicodeError
    or RecursionError, the only families the subject catches and translates.
    """

    def __init__(self, diagnosis: str, counters: Mapping[str, int], **details: Any) -> None:
        super().__init__(diagnosis)
        self.diagnosis = diagnosis
        self.counters = dict(counters)
        self.details = details


# ----------------------------------------------------------- the static model


@dataclass(frozen=True)
class LocatedCall:
    """One located call in the snapshot. Identity is (enclosing, lineno, col)."""

    enclosing: str
    lineno: int
    end_lineno: int
    col_offset: int
    first_arg_dump: str   # ast.dump(args[0], include_attributes=False) -- the oracle key
    first_arg_src: str    # ast.unparse -- READABLE DIAGNOSTICS ONLY, never matched
    # The ORIGINAL AST node of the guarded condition, as parsed from the snapshot. The
    # structural predicates read THIS; they never re-parse the diagnostic text. Excluded
    # from equality and hashing: identity is (enclosing, lineno, col).
    condition: ast.expr = field(default_factory=lambda: ast.Constant(value=None),
                                compare=False, hash=False, repr=False)

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.enclosing, self.lineno, self.col_offset)


@dataclass(frozen=True)
class CodeForm:
    """Classification of a ``code`` argument: 'positional' or 'keyword'."""

    argument_form: str


@dataclass(frozen=True)
class Unclassifiable:
    """The classifier could not classify. Carries the syntactic form it was given."""

    argument_form: str


def _enclosing_map(tree: ast.AST) -> dict[int, str]:
    enclosing: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                enclosing.setdefault(id(node), fn.name)
    return enclosing


def _is_sig_code_node(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == 'INSTALL_SIGNATURE_MISMATCH'
            and isinstance(node.value, ast.Name) and node.value.id == 'Code')


def _named_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def _carries_sig(call: ast.Call) -> bool:
    return (any(_is_sig_code_node(a) for a in call.args)
            or any(_is_sig_code_node(k.value) for k in call.keywords))


def _located(call: ast.Call, enclosing: dict[int, str]) -> LocatedCall:
    first = call.args[0] if call.args else ast.Constant(value=None)
    return LocatedCall(
        enclosing=enclosing.get(id(call), '<module>'),
        lineno=call.lineno,
        end_lineno=call.end_lineno if call.end_lineno is not None else call.lineno,
        col_offset=call.col_offset,
        first_arg_dump=ast.dump(first, include_attributes=False),
        first_arg_src=ast.unparse(first),
        condition=first,
    )


@dataclass(frozen=True)
class ParsedSnapshot:
    tree: ast.Module
    enclosing: Mapping[int, str]
    sig_require_nodes: tuple[ast.Call, ...]


# --- static seam defaults (Blocker-2 seams v3 section 1.1) -------------------


def default_require_site_locator(parsed: ParsedSnapshot) -> tuple[LocatedCall, ...]:
    """S4: every ``_require`` call carrying INSTALL_SIGNATURE_MISMATCH, any form."""
    calls = [c for c in _named_calls(parsed.tree, '_require') if _carries_sig(c)]
    return tuple(_located(c, dict(parsed.enclosing)) for c in calls)


def default_code_classifier(parsed: ParsedSnapshot, site: LocatedCall) -> CodeForm | Unclassifiable:
    """S6: positional ``_require(ok, Code.X)`` or keyword ``_require(ok, code=Code.X)``."""
    for call in parsed.sig_require_nodes:
        if (call.lineno, call.col_offset) != (site.lineno, site.col_offset):
            continue
        positional = len(call.args) == 2 and _is_sig_code_node(call.args[1])
        keyword = (len(call.args) == 1 and len(call.keywords) == 1
                   and call.keywords[0].arg == 'code' and _is_sig_code_node(call.keywords[0].value))
        if positional and not call.keywords:
            return CodeForm('positional')
        if keyword:
            return CodeForm('keyword')
        return Unclassifiable('keyword' if call.keywords else 'positional')
    return Unclassifiable('positional')


def syntactic_form(parsed: ParsedSnapshot, site: LocatedCall) -> str:
    """The call's syntactic code-argument form, read from the AST: 'keyword' if the
    located call passes any keyword, else 'positional'. Used only when a classifier
    returns something that is neither CodeForm nor Unclassifiable (e.g. UNKNOWN), so
    the diagnosis never carries the UNKNOWN value itself."""
    for call in parsed.sig_require_nodes:
        if (call.lineno, call.col_offset) == (site.lineno, site.col_offset):
            return 'keyword' if call.keywords else 'positional'
    return 'positional'


def default_direct_fail_locator(parsed: ParsedSnapshot) -> tuple[LocatedCall, ...]:
    """S7: direct ``_fail`` calls carrying INSTALL_SIGNATURE_MISMATCH."""
    return tuple(_located(c, dict(parsed.enclosing))
                 for c in _named_calls(parsed.tree, '_fail') if _carries_sig(c))


def default_qualified_call_locator(parsed: ParsedSnapshot) -> tuple[LocatedCall, ...]:
    """S11: every ``_qualified`` call site."""
    return tuple(_located(c, dict(parsed.enclosing))
                 for c in _named_calls(parsed.tree, '_qualified'))


# Structural predicates per semantic ID. They match on AST FEATURES of the guarded
# condition (operator and operand shapes), not on its full text; the A-01..A-08 cases
# check the resolved site against an INDEPENDENT oracle -- the exact ast.dump of the
# expected condition -- so a predicate swapped between two IDs is caught.


def _cond(site: LocatedCall) -> ast.expr:
    """The original structural AST node. Never the diagnostic text."""
    return site.condition


def _pred_qualified(site: LocatedCall) -> bool:
    return site.enclosing == '_qualified'


def _pred_args_unspec(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_target' and isinstance(c, ast.Compare)
            and isinstance(c.left, ast.Attribute) and c.left.attr == 'args_unspecified')


def _pred_dual_shape(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_target' and isinstance(c, ast.BoolOp)
            and any(isinstance(v, ast.Compare) and isinstance(v.left, ast.Attribute)
                    and v.left.attr == 'defexpr' for v in c.values))


def _pred_dual_equal(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_target' and isinstance(c, ast.Compare)
            and isinstance(c.left, ast.Call) and isinstance(c.left.func, ast.Name)
            and c.left.func.id == 'tuple')


def _pred_membership(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_target' and isinstance(c, ast.Compare)
            and isinstance(c.left, ast.Tuple) and isinstance(c.ops[0], ast.In))


def _pred_create_name(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_validate_ast' and isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name) and c.func.id == 'any'
            and len(c.args) == 1 and isinstance(c.args[0], ast.GeneratorExp))


def _pred_create_identity(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_validate_ast' and isinstance(c, ast.Compare)
            and isinstance(c.left, ast.Name) and c.left.id == 'identity'
            and isinstance(c.ops[0], ast.In))


def _pred_create_compare(site: LocatedCall) -> bool:
    c = _cond(site)
    return (site.enclosing == '_validate_ast' and isinstance(c, ast.BoolOp)
            and isinstance(c.op, ast.And) and len(c.values) == 3)


DEFAULT_SITE_PREDICATES: Mapping[str, Callable[[LocatedCall], bool]] = types.MappingProxyType({
    SIG_QUALIFIED: _pred_qualified,
    SIG_TARGET_ARGS_UNSPEC: _pred_args_unspec,
    SIG_TARGET_DUAL_SHAPE: _pred_dual_shape,
    SIG_TARGET_DUAL_EQUAL: _pred_dual_equal,
    SIG_TARGET_MEMBERSHIP: _pred_membership,
    SIG_CREATE_NAME: _pred_create_name,
    SIG_CREATE_IDENTITY: _pred_create_identity,
    SIG_CREATE_COMPARE: _pred_create_compare,
})

# Edges are resolved BY ENCLOSING FUNCTION (Gate 2 section 1a). The argument
# expression does not discriminate: CREATE and INVENTORY both pass node.funcname.
EDGE_ENCLOSING: Mapping[str, str] = types.MappingProxyType({
    EDGE_CREATE: '_validate_ast',
    EDGE_TARGET: '_target',
    EDGE_INVENTORY: '_validate_statement_inventory',
})

DEFAULT_EDGE_PREDICATES: Mapping[str, Callable[[LocatedCall], bool]] = types.MappingProxyType({
    edge_id: (lambda site, _enc=enc: site.enclosing == _enc)
    for edge_id, enc in EDGE_ENCLOSING.items()
})


# ----------------------------------------------------------- the seam registry


class SeamRegistry:
    """The named, CLOSED set of replaceable overlay seams.

    Replaced only through ``replaced(name, value)``, a context manager that restores
    the original BY IDENTITY. Every case asserts ``registry.get(name) is original``
    after its run. Unknown names are refused: the set is closed.
    """

    def __init__(self, originals: Mapping[str, Any]) -> None:
        self._originals = dict(originals)
        self._current = dict(originals)

    def get(self, name: str) -> Any:
        return self._current[name]

    def original(self, name: str) -> Any:
        return self._originals[name]

    def names(self) -> frozenset[str]:
        return frozenset(self._originals)

    def all_original(self) -> bool:
        return all(self._current[n] is self._originals[n] for n in self._originals)

    @contextmanager
    def replaced(self, name: str, value: Any) -> Iterator[None]:
        if name not in self._originals:
            raise KeyError(f'not a named seam: {name!r}')
        if self._current[name] is not self._originals[name]:
            raise RuntimeError(f'seam {name!r} is already replaced; stacking is refused')
        self._current[name] = value
        try:
            yield
        finally:
            self._current[name] = self._originals[name]


# ----------------------------------------------------- the phase projection (A-28)

# The phase pin per semantic site, used ONLY at the oracle boundary (A7). Raw events
# carry NO phase field (A-29). SIG.QUALIFIED maps to O08 because A7 is evaluated only
# on the terminal failing event, and in every row that fails at SIG.QUALIFIED the
# terminal event carries EDGE.CREATE.QUALIFIED -- asserted separately by A5. The four
# SIG.TARGET.* sites map to O11 as the recorded pin of A-wrong-grant-signature; this
# pin DOES NOT ESTABLISH O11 (PC-09 flag 1). O11 is established only by the Part E
# call-chain witness.
PHASE_BY_SITE: Mapping[str, str] = types.MappingProxyType({
    SIG_QUALIFIED: 'O08',
    SIG_CREATE_NAME: 'O08',
    SIG_CREATE_IDENTITY: 'O09c',
    SIG_CREATE_COMPARE: 'O09c',
    SIG_TARGET_ARGS_UNSPEC: 'O11',
    SIG_TARGET_DUAL_SHAPE: 'O11',
    SIG_TARGET_DUAL_EQUAL: 'O11',
    SIG_TARGET_MEMBERSHIP: 'O11',
})


def _baseline_phase_for_site(site_id: str) -> str:
    """Pure: a lookup in an immutable mapping. Never returns UNKNOWN; raises instead."""
    return PHASE_BY_SITE[site_id]


class _PhaseSlot:
    """Named, separately replaceable slot for the phase projection (M1's seam)."""

    baseline: Callable[[str], str] = staticmethod(_baseline_phase_for_site)

    def __init__(self) -> None:
        self.current: Callable[[str], str] = _baseline_phase_for_site

    @contextmanager
    def replaced(self, fn: Callable[[str], str]) -> Iterator[None]:
        if self.current is not _baseline_phase_for_site:
            raise RuntimeError('phase projection already replaced; stacking is refused')
        self.current = fn
        try:
            yield
        finally:
            self.current = _baseline_phase_for_site


PHASE_SLOT = _PhaseSlot()


def phase_for_site(site_id: str) -> str:
    """The NAMED projection A7 calls. Never inline PHASE_BY_SITE at the oracle."""
    result = PHASE_SLOT.current(site_id)
    if result == UNKNOWN:
        raise AssertionError('phase projection returned UNKNOWN (B-13)')
    return result


# ---------------------------------------------------------------- M4 boundary

MUTANT_ENV = 'HALOFLOW_SIG_MUTANT'


def _identity_post_resolution(sites: dict[str, Any]) -> dict[str, Any]:
    return sites


def _m4_post_resolution(sites: dict[str, Any]) -> dict[str, Any]:
    """M4 v2, BOTH halves: SIG.CREATE.NAME forced unresolved AFTER a valid pass, and
    the abort suppressed by assigning UNKNOWN so setup continues. Only S14 stands."""
    forced = dict(sites)
    forced[SIG_CREATE_NAME] = UNKNOWN
    return forced


def post_resolution_hook_from_env() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """M4 is delivered to the B-24 child by launch environment ONLY."""
    if os.environ.get(MUTANT_ENV) == 'M4':
        return _m4_post_resolution
    return _identity_post_resolution


# Read ONCE, at import (B-24 contract v3 section 4a). The parent never carries it.
POST_RESOLUTION_AT_IMPORT = post_resolution_hook_from_env()


# ------------------------------------------------------------------ resolution


@dataclass(frozen=True)
class Expected:
    snapshot_sha256: str = EXPECTED_SNAPSHOT_SHA256
    input_sha256: Mapping[str, str] = field(default_factory=lambda: EXPECTED_INPUT_SHA256)


@dataclass(frozen=True)
class Resolution:
    """The already-resolved, immutable projection that S14 checks."""

    module: types.ModuleType
    raw: bytes
    parsed: ParsedSnapshot
    sites: Mapping[str, LocatedCall]
    edges: Mapping[str, LocatedCall]
    line_to_site: Mapping[tuple[str, int], str]
    ledger: tuple[str, ...]
    original_require: Callable[..., Any]
    original_fail: Callable[..., Any]
    sig_code: Any
    default_code: Any
    originals: Mapping[str, Any]


def load_snapshot_module(raw: bytes) -> types.ModuleType:
    """FORM 1: compile the captured bytes and execute them as the observed module."""
    import haloflow.m01.provisioning  # noqa: F401  parents must exist for relative imports

    spec = importlib.util.spec_from_file_location(SNAPSHOT_MODULE_NAME, str(POLICY_PATH))
    if spec is None:
        raise RuntimeError('could not build a module spec for the snapshot')
    module = importlib.util.module_from_spec(spec)
    module.__package__ = SNAPSHOT_PACKAGE
    code = compile(raw, str(POLICY_PATH), 'exec')
    sys.modules[SNAPSHOT_MODULE_NAME] = module
    exec(code, module.__dict__)
    return module


def sha256_file(path: Path) -> str:
    """Read and hash. No cache: A-26 must not populate or read a shared hash cache."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _abort(diagnosis: str, ledger: list[str], **details: Any) -> PreflightAbort:
    return PreflightAbort(diagnosis, tuple(ledger), **details)


def preflight(
    *,
    registry: SeamRegistry | None = None,
    expected: Expected | None = None,
    post_resolution: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Resolution:
    """The ordered preflight S1..S14. A stage is appended ONLY after it fully passes.

    Raises PreflightAbort carrying the EXACT COMPLETED PREFIX. UNKNOWN is an abort at
    every stage, never a reported result.
    """
    seams = registry if registry is not None else STATIC_SEAMS
    exp = expected if expected is not None else Expected()
    post = post_resolution if post_resolution is not None else POST_RESOLUTION_AT_IMPORT
    ledger: list[str] = []

    # S1 -- runtime-byte binding (form 1)
    raw = POLICY_PATH.read_bytes()
    observed_sha = hashlib.sha256(raw).hexdigest()
    if observed_sha != exp.snapshot_sha256:
        raise _abort(Diag.SNAPSHOT_MISMATCH, ledger, observed=observed_sha,
                     expected=exp.snapshot_sha256)
    module = load_snapshot_module(raw)
    for name in ('_require', '_fail', '_qualified', '_target', '_validate_ast',
                 '_validate_local_statements', '_validate_statement_inventory',
                 '_parameters', '_options', 'validate_function_installation'):
        fn = getattr(module, name, None)
        if fn is None or fn.__code__.co_filename != str(POLICY_PATH):
            raise _abort(Diag.SNAPSHOT_MISMATCH, ledger, missing_or_foreign=name)
    if getattr(module, 'MigrationUnitRejected', None) is not MigrationUnitRejected:
        raise _abort(Diag.SNAPSHOT_MISMATCH, ledger, exception_class='not shared')
    ledger.append('S1')

    # S2 -- all ten input hashes
    for rel, want in exp.input_sha256.items():
        got = sha256_file(FIXTURE_DIR / rel)
        if got != want:
            raise _abort(Diag.INPUT_HASH_MISMATCH, ledger, input=rel, observed=got,
                         expected=want)
    ledger.append('S2')

    # S3 -- parse the snapshot
    try:
        tree = ast.parse(raw, filename=str(POLICY_PATH))
    except SyntaxError as error:
        raise _abort(Diag.PARSE_FAILED, ledger, error=str(error)) from None
    enclosing = _enclosing_map(tree)
    sig_nodes = tuple(c for c in _named_calls(tree, '_require') if _carries_sig(c))
    parsed = ParsedSnapshot(tree=tree, enclosing=types.MappingProxyType(enclosing),
                            sig_require_nodes=sig_nodes)
    ledger.append('S3')

    # S4 -- locate SIGNATURE_MISMATCH _require calls
    located = tuple(seams.get('require_site_locator')(parsed))
    ledger.append('S4')

    # S5 -- located count == 8
    if len(located) != 8:
        raise _abort(Diag.SITE_COUNT_INVALID, ledger, located=len(located), expected=8,
                     located_sites=frozenset(s.key for s in located))
    ledger.append('S5')

    # S6 -- every located call's code argument classifies
    classifier = seams.get('code_classifier')
    for site in located:
        form = classifier(parsed, site)
        if not isinstance(form, CodeForm) or form.argument_form not in ARGUMENT_FORMS:
            # Fail closed. UNKNOWN, or any foreign value, is never a classification.
            if isinstance(form, Unclassifiable) and form.argument_form in ARGUMENT_FORMS:
                argument_form = form.argument_form
            else:
                argument_form = syntactic_form(parsed, site)
            raise _abort(Diag.CODE_UNCLASSIFIABLE, ledger, site=site.key,
                         argument_form=argument_form)
    ledger.append('S6')

    # S7 -- no direct _fail carries SIGNATURE_MISMATCH
    direct = tuple(seams.get('direct_fail_locator')(parsed))
    if direct:
        raise _abort(Diag.DIRECT_FAIL_PRESENT, ledger,
                     locations=tuple(s.key for s in direct))
    ledger.append('S7')

    # S8 -- resolve each semantic ID through site_predicates
    predicates = seams.get('site_predicates')
    matches: dict[str, list[LocatedCall]] = {
        # Only the boolean True is a match. UNKNOWN, or any other truthy non-bool, is
        # NOT a match, so it fails closed as an unresolved ID at S9.
        sid: [s for s in located if predicates[sid](s) is True] for sid in SITE_IDS
    }
    ledger.append('S8')

    # S9 -- no semantic ID resolves to zero or more than one site
    for sid in SITE_IDS:
        found = matches[sid]
        if len(found) == 0:
            raise _abort(Diag.SITE_UNRESOLVED, ledger, semantic_id=sid, sites_found=0)
        if len(found) > 1:
            raise _abort(Diag.SITE_AMBIGUOUS, ledger, semantic_id=sid,
                         sites_found=len(found), sites=frozenset(s.key for s in found))
    sites = {sid: matches[sid][0] for sid in SITE_IDS}
    ledger.append('S9')

    # S10 -- no located site is claimed by zero IDs
    claimed: dict[tuple[str, int, int], list[str]] = {}
    for sid, site in sites.items():
        claimed.setdefault(site.key, []).append(sid)
    unclaimed = [s for s in located if s.key not in claimed]
    if unclaimed:
        doubly = {k: tuple(v) for k, v in claimed.items() if len(v) > 1}
        raise _abort(Diag.SITE_UNCLAIMED, ledger,
                     unclaimed=frozenset(s.key for s in unclaimed),
                     doubly_claimed=frozenset(doubly),
                     claimants=frozenset(i for ids in doubly.values() for i in ids))
    ledger.append('S10')

    # S11 -- locate _qualified call sites
    qualified = tuple(seams.get('qualified_call_locator')(parsed))
    ledger.append('S11')

    # S12 -- static edge universe == 3
    if len(qualified) != 3:
        raise _abort(Diag.EDGE_UNIVERSE_INVALID, ledger, found=len(qualified), expected=3)
    ledger.append('S12')

    # S13 -- symmetric edge closure: each site one edge, each edge once
    edge_predicates = seams.get('edge_predicates')
    edges: dict[str, LocatedCall] = {}
    for eid in EDGE_IDS:
        hits = [q for q in qualified if edge_predicates[eid](q) is True]   # True only
        if len(hits) == 0:
            raise _abort(Diag.EDGE_UNRESOLVED, ledger, edge_id=eid, sites_found=0)
        if len(hits) > 1:
            raise _abort(Diag.EDGE_AMBIGUOUS, ledger, edge_id=eid, sites_found=len(hits),
                         sites=frozenset(q.key for q in hits))
        edges[eid] = hits[0]
    if len({e.key for e in edges.values()}) != 3:
        raise _abort(Diag.EDGE_AMBIGUOUS, ledger, edge_id=None,
                     sites=frozenset(e.key for e in edges.values()))
    ledger.append('S13')

    # ---- the named M4 boundary: AFTER S13, BEFORE S14 (binding condition 3) ----
    bound: dict[str, Any] = post(dict(sites))

    # S14 -- post-resolution binding integrity of the already-resolved projection.
    # NOT a second resolver and NOT a second source of truth (binding condition 4).
    for sid in SITE_IDS:
        value = bound.get(sid)
        if value is not sites[sid]:
            raise _abort(Diag.BINDING_UNRESOLVED, ledger, unresolved_binding=sid)
    ledger.append('S14')

    line_to_site: dict[tuple[str, int], str] = {}
    for sid, site in sites.items():
        for line in range(site.lineno, site.end_lineno + 1):
            line_to_site[(site.enclosing, line)] = sid

    original_require = module._require
    return Resolution(
        module=module,
        raw=raw,
        parsed=parsed,
        sites=types.MappingProxyType(sites),
        edges=types.MappingProxyType(edges),
        line_to_site=types.MappingProxyType(line_to_site),
        ledger=tuple(ledger),
        original_require=original_require,
        original_fail=module._fail,
        sig_code=module.Code.INSTALL_SIGNATURE_MISMATCH,
        default_code=inspect.signature(original_require).parameters['code'].default,
        originals=types.MappingProxyType({n: module.__dict__[n] for n in INSTRUMENTED_GLOBALS}),
    )


# ------------------------------------------------------------ live observation


class FrameProxy:
    """Weak-referenceable stand-in for a frame (frames are not weak-referenceable).

    Forwards exactly the attributes the observer reads. B-09 asserts a proxy handed
    to the observer is dead after both the return and the raise paths.
    """

    __slots__ = ('_frame', '__weakref__')

    def __init__(self, frame: types.FrameType) -> None:
        self._frame = frame

    @property
    def f_code(self) -> types.CodeType:
        return self._frame.f_code

    @property
    def f_lineno(self) -> int:
        return self._frame.f_lineno

    @property
    def f_back(self) -> FrameProxy | None:
        back = self._frame.f_back
        return FrameProxy(back) if back is not None else None


def default_frame_provider() -> Any:
    """Returns the OBSERVER's frame (the caller of this provider)."""
    frame = inspect.currentframe()
    try:
        return frame.f_back if frame is not None else None
    finally:
        del frame


def default_live_site_projection(resolution: Resolution, code_name: str,
                                 lineno: int) -> tuple[str, ...]:
    sid = resolution.line_to_site.get((code_name, lineno))
    return (sid,) if sid is not None else ()


def default_live_edge_projection(resolution: Resolution, caller_name: str) -> tuple[str, ...]:
    return tuple(eid for eid in EDGE_IDS if EDGE_ENCLOSING[eid] == caller_name
                 and eid in resolution.edges)


def default_runtime_code_classifier(resolution: Resolution, args: tuple[Any, ...],
                                    kwargs: Mapping[str, Any]) -> CodeForm | Unclassifiable:
    if len(args) == 2 and args[1] is resolution.sig_code and 'code' not in kwargs:
        return CodeForm('positional')
    if len(args) == 1 and kwargs.get('code') is resolution.sig_code:
        return CodeForm('keyword')
    return Unclassifiable('keyword' if 'code' in kwargs else 'positional')


STATIC_SEAMS = SeamRegistry({
    'snapshot_provider': POLICY_PATH.read_bytes,   # never replaced (A-23 replaces constants)
    'require_site_locator': default_require_site_locator,
    'code_classifier': default_code_classifier,
    'direct_fail_locator': default_direct_fail_locator,
    'site_predicates': DEFAULT_SITE_PREDICATES,
    'qualified_call_locator': default_qualified_call_locator,
    'edge_predicates': DEFAULT_EDGE_PREDICATES,
})

LIVE_SEAMS = SeamRegistry({
    'frame_provider': default_frame_provider,
    'live_site_projection': default_live_site_projection,
    'live_edge_projection': default_live_edge_projection,
    'runtime_code_classifier': default_runtime_code_classifier,
})

# Every module global an instrument may replace. Each must hold its ORIGINAL object
# before any install and after every restore (B-22, X2).
INSTRUMENTED_GLOBALS: tuple[str, ...] = (
    '_require', '_parameters', '_options', '_validate_local_statements',
    '_validate_statement_inventory', 'validate_function_installation',
)

COUNTER_NAMES: tuple[str, ...] = (
    'frame1_resolved', 'frame2_resolved', 'site_lookup_entered', 'edge_lookup_entered',
    'code_classify_entered',
)


@dataclass(frozen=True)
class Event:
    """One raw signature event. NO phase field (A-29). ``exception`` is the exact
    object the original raised, held strongly, never a copy, code or repr (B-07)."""

    site: str
    edge: str | None
    outcome: str                       # 'pass' | 'fail'
    exception: BaseException | None


@dataclass
class RunRecord:
    """One run. A FRESH instance per run (B-21); nothing aliases a previous run."""

    events: list[Event] = field(default_factory=list)
    public: BaseException | None = None
    returned: Any = None
    accepted: bool = False
    abort: ObservationAbort | None = None
    passthrough_calls: int = 0
    delegations: int = 0
    steps: list[tuple[int, str]] = field(default_factory=list)   # (call index, step)
    markers: dict[str, int] = field(default_factory=dict)
    installed_markers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    witness: WitnessRecord | None = None


def _original_code(resolution: Resolution, name: str) -> types.CodeType:
    """Code objects of the ORIGINAL snapshot functions, never of an installed wrapper."""
    fn = resolution.originals.get(name) or resolution.module.__dict__[name]
    code: types.CodeType = fn.__code__
    return code


def _expected_frame1_codes(resolution: Resolution) -> frozenset[types.CodeType]:
    names = ('_qualified', '_target', '_validate_ast')
    return frozenset(_original_code(resolution, n) for n in names)


def _expected_frame2_codes(resolution: Resolution) -> Mapping[str, frozenset[types.CodeType]]:
    def codes(*names: str) -> frozenset[types.CodeType]:
        return frozenset(_original_code(resolution, n) for n in names)
    return {
        '_qualified': codes('_validate_ast', '_target', '_validate_statement_inventory'),
        '_target': codes('_validate_local_statements', '_validate_statement_inventory'),
        '_validate_ast': codes('validate_function_installation'),
    }


class Observer:
    """Replaces ``_require`` ONLY. Per call: capture site and edge BEFORE delegating;
    delegate ONCE without evaluating ``ok``; record pass or the exact exception;
    re-raise unchanged; release frame references in ``finally``."""

    def __init__(self, resolution: Resolution, record: RunRecord,
                 original: Callable[..., Any] | None = None) -> None:
        self.resolution = resolution
        self.record = record
        self.original = original if original is not None else resolution.original_require
        self.frame1_codes = _expected_frame1_codes(resolution)
        self.frame2_codes = _expected_frame2_codes(resolution)
        self.in_delegation = False
        self._calls = 0

    def _is_signature_call(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> bool:
        sig = self.resolution.sig_code
        return any(a is sig for a in args[1:]) or kwargs.get('code') is sig

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        rec = self.record
        if not self._is_signature_call(args, kwargs):
            rec.passthrough_calls += 1
            rec.delegations += 1
            return self.original(*args, **kwargs)                     # B-11: untouched

        index = self._calls
        self._calls += 1
        counters = dict.fromkeys(COUNTER_NAMES, 0)
        frame: Any = None
        frame1: Any = None
        frame2: Any = None
        try:
            frame = LIVE_SEAMS.get('frame_provider')()
            if frame is None:
                raise ObservationAbort(Diag.FRAME_ABSENT, counters, call_index=index)
            frame1 = frame.f_back
            if frame1 is None or frame1.f_code not in self.frame1_codes:
                raise ObservationAbort(
                    Diag.FRAME1_UNEXPECTED, counters, call_index=index,
                    code=None if frame1 is None else frame1.f_code)
            counters['frame1_resolved'] += 1
            rec.steps.append((index, 'frame1'))
            frame2 = frame1.f_back
            allowed = self.frame2_codes.get(frame1.f_code.co_name, frozenset())
            if frame2 is None or frame2.f_code not in allowed:
                raise ObservationAbort(
                    Diag.FRAME2_UNEXPECTED, counters, call_index=index,
                    code=None if frame2 is None else frame2.f_code)
            counters['frame2_resolved'] += 1
            rec.steps.append((index, 'frame2'))

            counters['site_lookup_entered'] += 1
            live_key = (frame1.f_code.co_name, frame1.f_lineno)
            ids = tuple(LIVE_SEAMS.get('live_site_projection')(self.resolution, *live_key))
            if len(ids) == 0:
                raise ObservationAbort(Diag.OBS_SITE_UNMAPPED, counters, call_index=index,
                                       live_site=live_key)
            if len(ids) > 1 or UNKNOWN in ids:
                raise ObservationAbort(Diag.OBS_SITE_AMBIGUOUS, counters, call_index=index,
                                       live_site=live_key, ids=frozenset(ids),
                                       ids_found=len(ids))
            site = ids[0]
            rec.steps.append((index, 'site'))

            edge: str | None = None
            if site == SIG_QUALIFIED:
                counters['edge_lookup_entered'] += 1
                edges = tuple(LIVE_SEAMS.get('live_edge_projection')(
                    self.resolution, frame2.f_code.co_name))
                if len(edges) == 0:
                    raise ObservationAbort(Diag.OBS_EDGE_UNMAPPED, counters, call_index=index,
                                           caller=frame2.f_code.co_name, edges_found=0)
                if len(edges) > 1 or UNKNOWN in edges:
                    raise ObservationAbort(Diag.OBS_EDGE_AMBIGUOUS, counters,
                                           call_index=index, caller=frame2.f_code.co_name,
                                           edges=frozenset(edges), edges_found=len(edges))
                edge = edges[0]
                rec.steps.append((index, 'edge'))

            counters['code_classify_entered'] += 1
            form = LIVE_SEAMS.get('runtime_code_classifier')(self.resolution, args, kwargs)
            if not isinstance(form, CodeForm) or form.argument_form not in ARGUMENT_FORMS:
                if isinstance(form, Unclassifiable) and form.argument_form in ARGUMENT_FORMS:
                    argument_form = form.argument_form
                else:
                    argument_form = 'keyword' if 'code' in kwargs else 'positional'
                raise ObservationAbort(Diag.OBS_CODE_UNCLASSIFIABLE, counters,
                                       call_index=index, argument_form=argument_form)
            rec.steps.append((index, 'code'))
        except ObservationAbort as abort:
            rec.abort = abort
            raise
        finally:
            del frame, frame1, frame2

        rec.steps.append((index, 'delegate'))
        rec.delegations += 1
        self.in_delegation = True
        try:
            result = self.original(*args, **kwargs)
        except MigrationUnitRejected as error:
            self.in_delegation = False
            rec.events.append(Event(site=site, edge=edge, outcome='fail', exception=error))
            rec.steps.append((index, 'record'))
            raise                                                      # B-08: unchanged
        self.in_delegation = False
        rec.events.append(Event(site=site, edge=edge, outcome='pass', exception=None))
        rec.steps.append((index, 'record'))
        return result


# ------------------------------------------------------ reach markers (Part D)

MARKER_TARGETS: tuple[str, ...] = (
    '_parameters', '_options', '_validate_local_statements', '_validate_statement_inventory',
)


def _marker(name: str, real: Callable[..., Any], record: RunRecord) -> Callable[..., Any]:
    """Exception-transparent, entry-only reach marker (packet v7 section 4.4)."""
    def marked(*args: Any, **kwargs: Any) -> Any:
        record.markers[name] = record.markers.get(name, 0) + 1
        return real(*args, **kwargs)
    marked.__qualname__ = f'reach_marker[{name}]'
    return marked


# ------------------------------------------------- Part E instruments I1..I3


def _identity_witness_record(propagating: BaseException) -> BaseException:
    """I2 baseline projection: identity."""
    return propagating


class WitnessSlot:
    """I2: named, separately replaceable projection ``o11_witness_record``.

    OD-SIG-16 v3 no-stack shape: install over identity accepted; install over a live
    projection refused; a refused install leaves the slot unchanged; the slot is back
    at identity after every run, including a raising run.
    """

    identity: Callable[[BaseException], BaseException] = staticmethod(_identity_witness_record)

    def __init__(self) -> None:
        self.current: Callable[[BaseException], BaseException] = _identity_witness_record

    def install(self, fn: Callable[[BaseException], BaseException]) -> bool:
        if self.current is not _identity_witness_record:
            return False
        self.current = fn
        return True

    def restore(self) -> None:
        self.current = _identity_witness_record


WITNESS_SLOT = WitnessSlot()


@dataclass
class WitnessRecord:
    """I6 (part): strong references only; never id()."""

    entries: int = 0
    normal_returns: int = 0
    exception_exits: int = 0
    projection_calls: int = 0
    recorded: BaseException | None = None          # I2 output on exception exit
    inventory_slot_object: Any = None              # I3 read at exit


def _witness(real: Callable[..., Any], resolution: Resolution,
             wrec: WitnessRecord) -> Callable[..., Any]:
    """I1: exception-transparent O11 witness on ``_validate_local_statements``."""
    def witnessed(*args: Any, **kwargs: Any) -> Any:
        wrec.entries += 1
        try:
            result = real(*args, **kwargs)
        except BaseException as propagating:
            wrec.exception_exits += 1
            wrec.projection_calls += 1
            wrec.recorded = WITNESS_SLOT.current(propagating)
            wrec.inventory_slot_object = resolution.module.__dict__.get(
                '_validate_statement_inventory')                           # I3
            raise                                    # the ORIGINAL propagating object
        wrec.normal_returns += 1
        wrec.inventory_slot_object = resolution.module.__dict__.get(
            '_validate_statement_inventory')                               # I3
        return result
    witnessed.__qualname__ = 'o11_witness[_validate_local_statements]'
    return witnessed


# -------------------------------------------------------------------- one run


class _Installed:
    """Installs module-global replacements and restores them by identity, reverse order."""

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module
        self._saved: list[tuple[str, Any]] = []

    def put(self, name: str, value: Any) -> None:
        self._saved.append((name, self.module.__dict__[name]))
        self.module.__dict__[name] = value

    def restore(self) -> None:
        while self._saved:
            name, value = self._saved.pop()
            self.module.__dict__[name] = value


class StackingRefused(RuntimeError):
    """An instrument was already installed on the snapshot module."""


def run_once(
    resolution: Resolution,
    validated_bytes: bytes,
    schema_key: str,
    *,
    markers: tuple[str, ...] = (),
    witness: bool = False,
    observer_original: Callable[..., Any] | None = None,
    public_entry: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
) -> RunRecord:
    """One observed run with a FRESH record (B-21), restored in ``finally`` (B-22).

    ``markers``: Part D reach markers to install (I4 when it includes the inventory).
    ``witness``: install the Part E O11 witness (I1) OUTER to any markers.
    ``public_entry``: a named outer layer applied to the PUBLIC entry only (M5's
    placement: outside I1 and ``_validate_local_statements``).
    """
    module = resolution.module
    for name in INSTRUMENTED_GLOBALS:
        if module.__dict__[name] is not resolution.originals[name]:
            raise StackingRefused(f'{name} is not the original before install (B-22)')
    record = RunRecord()
    installed = _Installed(module)
    try:
        installed.put('_require', Observer(resolution, record, observer_original))
        for name in markers:
            marker = _marker(name, module.__dict__[name], record)
            record.installed_markers[name] = marker
            installed.put(name, marker)
        if witness:
            record.witness = WitnessRecord()
            installed.put('_validate_local_statements',
                          _witness(module.__dict__['_validate_local_statements'],
                                   resolution, record.witness))
        entry: Callable[..., Any] = module.validate_function_installation
        if public_entry is not None:
            entry = public_entry(entry)
        try:
            record.returned = entry(validated_bytes, schema_key=schema_key)
            record.accepted = True
        except MigrationUnitRejected as public:
            record.public = public
        except ObservationAbort as abort:
            record.abort = abort
    finally:
        installed.restore()
    return record


# ------------------------------------------------------------ fixture inputs


def canonical_validated_bytes(payload: Any) -> bytes:
    """X3c encoding: unsorted, so key order matters. Hash THESE bytes and validate THESE
    bytes; never re-serialize between the two."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')


def load_json(rel: str) -> Any:
    return json.loads((FIXTURE_DIR / rel).read_text(encoding='utf-8'))


def schema_key() -> str:
    return str(load_json('cases.json')['schema_key'])


def row_variant(case_id: str) -> Any:
    """The nine rows: the sql-fixtures.json variant, each case_id exactly once."""
    variants = [v for v in load_json('sql-fixtures.json')['variants'] if v['case_id'] == case_id]
    if len(variants) != 1:
        raise AssertionError(f'{case_id} appears {len(variants)}x in variants, expected 1')
    return variants[0]


def row_payload(case_id: str) -> Any:
    return row_variant(case_id)['payload']


def control_payload(control_id: str) -> Any:
    """Controls: loaded from the RECORDED cases.json .controls[id].payload_file."""
    controls = [c for c in load_json('cases.json')['controls'] if c['id'] == control_id]
    if len(controls) != 1:
        raise AssertionError(f'control {control_id} appears {len(controls)}x')
    return load_json(str(controls[0]['payload_file']))
