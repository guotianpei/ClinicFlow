"""CP2 PHASE-INTAKE-01 / PHASE-NUL-01 -- refusal-phase observation for the intake layer.

WHAT THIS MODULE CLOSES, AND WHAT IT DOES NOT
---------------------------------------------
The CP1 phase audit carried two gaps. `INSTALL_POLICY_INVALID` is intended at O01,
O03 and O04, and `INSTALL_NUL_FORBIDDEN` at O02 and O05, but for the intake layer
no test observed WHICH phase refused -- the phase was inferred from the code. The
2026-09-19 design closed the AST-layer phases with an innermost-seam spy. Its seam
set is entirely AST-layer, which is why these two stayed open.

This module extends that technique inward. It adds NO production seam, edits NO
frozen CP1 file, and changes NOTHING in `function_policy.py`. B3 stays uncleared.

FIRST-SEEN PROJECTION -- read this before trusting a trace assertion
--------------------------------------------------------------------
The recorder appends on EVERY seam entry and keeps those raw events. What the
assertions compare is a FIRST-SEEN PROJECTION over them: the seam names in order
of first entry, de-duplicated. On an accepted control that is ~257 raw entries
collapsed to 8 names.

The projection therefore CANNOT detect re-entry of a seam already seen, and cannot
detect reordering among seams already seen. It is not an "ordered entry trace" and
must not be described as one. Where later work matters, a case pins FORBIDDEN
seams explicitly rather than relying on the projection to reveal them.

WHY THE PROJECTION IS NEEDED AT ALL
-----------------------------------
`_pairs` is BOTH the `json.loads` object_pairs_hook (O01, a duplicate key in the
source text) AND the mapping builder inside `_normalize` (O03, two keys that
collide only after NFC). Same function, same refusal code, two different phases.
The innermost-seam NAME cannot separate them. The projection can:

    O01 duplicate key        -> ('_pairs',)
    O03 duplicate after NFC  -> ('_pairs', '_nul', '_normalize')

Per Codex's Q-2 ruling this augmentation is INTAKE-ONLY. It does not supersede the
innermost-seam method for the AST phases, and no frozen AST test is touched. The
recorder observes O11/O12 solely so `PHASE-CTRL-01` can validate the instrument
against phases the repository already records.

ON `PHASE-NUL-01` AND UNREACHABILITY
------------------------------------
The owner's ruling is NUL-A with closure CONDITIONAL on these assertions being
written, reviewed and passing. Until all three hold the gap stays on the carried
ledger. Nothing in this module may be cited as closure on its own.

What is claimed: through `validate_function_installation`, no input reaches the
rendered-SQL NUL guard with a NUL, because (a) `_nul` refuses every NUL in the
declaration first, (b) `SCHEMA_KEY_PATTERN` refuses a NUL in the schema key before
rendering, (c) `normalize_body` introduces a NUL from no codepoint.

What is NOT claimed: that the guard is unnecessary; that this holds for callers
other than `validate_function_installation`; that the exhaustive single-codepoint
scan proves arbitrary STRINGS safe -- it does not, which is why `01d_seq` and
`01d_mut` exist; or that the accepted control protects guard RETENTION -- it does
not, which is why `01f` exists.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import function_policy as subject
from haloflow.m01.provisioning.checksum import normalize_body

ROOT = Path(__file__).parent / 'fixtures/function_policy'
CASES = json.loads((ROOT / 'cases.json').read_text())
VARIANTS = json.loads((ROOT / 'sql-fixtures.json').read_text())['variants']
REASONS = json.loads((ROOT / 'expected-refusals.json').read_text())
SCHEMA = CASES['schema_key']

NUL = '\x00'

# Seams the recorder wraps. Intake seams plus render/parse, plus O11/O12 so that
# PHASE-CTRL-01 can check the instrument against the repository's own recorded
# phases. Wrapping a seam is observation; it is not an assertion about it.
SEAMS: tuple[str, ...] = (
    '_pairs', '_constant', '_nul', '_normalize', '_closed_shape',
    '_unique_declarations', '_declaration', '_render_exact_sql', '_load_parser',
    '_parse_exact_sql', '_validate_ast', '_validate_local_statements',
    '_validate_statement_inventory',
)


def encode(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')


def control(identifier: str = 'C01') -> Any:
    return json.loads((ROOT / 'controls' / (identifier + '.json')).read_text())


@dataclass
class Outcome:
    """One run of the checker, as observed. Holds no expected value of its own.

    A FRESH instance per run. An earlier version reset and returned one shared
    object, so two runs under one recorder aliased each other -- the kind of
    contamination that shows up as a passing test rather than a failing one.
    """

    code: str | None = None
    accepted: bool = False
    caught: BaseException | None = None
    events: list[str] = field(default_factory=list)
    exception_events: list[tuple[str, int]] = field(default_factory=list)
    innermost: str | None = None
    innermost_exception: BaseException | None = None

    @property
    def projection(self) -> tuple[str, ...]:
        """First-seen seam names. NOT an ordered trace -- see the module docstring."""
        return tuple(dict.fromkeys(self.events))

    @property
    def substituted(self) -> bool:
        """True when what escaped is not the object the deepest seam witnessed."""
        return (
            self.innermost_exception is not None
            and self.caught is not None
            and self.innermost_exception is not self.caught
        )


@contextmanager
def recorder(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Callable[..., Any]] | None = None,
    outer: dict[str, Callable[[Callable[..., Any]], Callable[..., Any]]] | None = None,
) -> Iterator[Callable[..., Outcome]]:
    """Wrap the seams in process and yield a runner.

    RESTORATION IS GUARANTEED AT CONTEXT EXIT via `monkeypatch.context()`. An
    earlier version relied on pytest's teardown, so a second recorder opened in
    the same test wrapped the first one's wrappers and silently double-counted
    events. De-duplication hid it, which is exactly why it has its own control.

    `overrides` replaces a seam's behaviour (fault injection); the replacement is
    wrapped like the real seam, so it still appears in the events. `outer` wraps a
    seam OUTSIDE the recorder's spy, so the spy witnesses what the real seam raised
    and the outer layer can alter what finally escapes -- that is how exception
    substitution is exercised.
    """

    chosen = overrides or {}
    outermost = outer or {}
    box: dict[str, Outcome] = {}

    def wrap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, _real: Callable[..., Any] = real, _name: str = name,
                **kwargs: Any) -> Any:
            state = box['state']
            state.events.append(_name)
            try:
                return _real(*args, **kwargs)
            except BaseException as error:
                state.exception_events.append((_name, id(error)))
                # DEEPEST WITNESS WINS AND IS NEVER OVERWRITTEN. An earlier version
                # replaced it whenever an outer wrapper saw a DIFFERENT object --
                # erasing the original in precisely the case where a substituted
                # exception must be detected. Preserving the first witness is what
                # makes the identity assertion mean anything.
                if state.innermost is None:
                    state.innermost = _name
                    state.innermost_exception = error
                raise
        return spy

    with pytest.MonkeyPatch.context() as patcher:
        originals = {name: getattr(subject, name) for name in SEAMS}
        for name in SEAMS:
            wrapped = wrap(name, chosen.get(name, originals[name]))
            if name in outermost:
                wrapped = outermost[name](wrapped)
            patcher.setattr(subject, name, wrapped)

        def run(payload: Any, schema_key: Any = SCHEMA) -> Outcome:
            state = Outcome()
            box['state'] = state
            body = payload if isinstance(payload, bytes | bytearray | str) else encode(payload)
            try:
                subject.validate_function_installation(body, schema_key=schema_key)
                state.accepted = True
            except MigrationUnitRejected as error:
                state.code = error.reason_code
                state.caught = error
            return state

        run.originals = originals  # type: ignore[attr-defined]
        yield run


def assert_refusal(
    outcome: Outcome,
    *,
    code: str,
    projection: tuple[str, ...],
    innermost: str | None,
    identity: bool,
    forbidden: Sequence[str] = (),
) -> None:
    """THE assertion helper. Every case uses it, including the negative control.

    `innermost=None` means the refusal is raised inline in
    `validate_function_installation` with no wrapped seam on the raising path. Such a
    row has no inner seam to witness identity, so `identity` must be False for it and
    no inner-identity claim is made.
    """

    assert not outcome.accepted, 'expected a refusal, the checker accepted'
    assert outcome.code == code, f'code {outcome.code!r} != {code!r}'
    assert outcome.projection == projection, (
        f'projection {outcome.projection!r} != {projection!r}'
    )
    assert outcome.innermost == innermost, (
        f'innermost seam {outcome.innermost!r} != {innermost!r}'
    )
    if identity:
        assert innermost is not None, 'identity cannot be witnessed without an inner seam'
        assert not outcome.substituted, (
            'the deepest seam witnessed a different exception object than the one that '
            'escaped -- something between them replaced it'
        )
        assert outcome.innermost_exception is outcome.caught
    else:
        assert innermost is None, 'identity=False is only for inline refusals'
    for name in forbidden:
        assert name not in outcome.events, f'forbidden seam {name!r} was entered'


# ---------------------------------------------------------------------------
# PHASE-CTRL -- the method is checked before it is used to answer anything
# ---------------------------------------------------------------------------

RECORDED = [v for v in VARIANTS if REASONS[v['case_id']]['phase'] is not None]
PHASE_SEAM = {'O11': '_validate_local_statements', 'O12': '_validate_statement_inventory'}


@pytest.mark.parametrize('variant', RECORDED, ids=lambda v: v['case_id'])
def test_phase_ctrl_01_recorder_reproduces_recorded_phases(variant, monkeypatch):
    """PHASE-CTRL-01. The instrument agrees with the repository's own oracle.

    Run against every case whose phase the repository already records. If this does
    not pass, no PHASE-INTAKE or PHASE-NUL result in this module means anything --
    the instrument would be answering questions it cannot answer correctly.
    """

    oracle = REASONS[variant['case_id']]
    with recorder(monkeypatch) as run:
        outcome = run(variant['payload'])
    assert outcome.code == oracle['code']
    assert outcome.innermost == PHASE_SEAM[oracle['phase']]
    assert outcome.innermost_exception is outcome.caught


def test_phase_ctrl_02_the_helper_can_fail(monkeypatch):
    """PHASE-CTRL-02. The assertion helper demonstrably fails on a wrong expectation.

    A measurement that cannot fail is not a measurement. The mutation battery once
    reported 9 of 9 on a tree carrying three real defects, because the harness
    resolved imports to a clean copy and never executed the mutant.

    Both halves run in THIS test against THE SAME helper, so nothing depends on
    pytest collection order and no other test has to run first.
    """

    payload = control()
    payload['migration_id'] += NUL
    with recorder(monkeypatch) as run:
        outcome = run(payload)

    # positive: the true expectation passes
    assert_refusal(
        outcome, code='INSTALL_NUL_FORBIDDEN', projection=('_pairs', '_nul'),
        innermost='_nul', identity=True,
    )

    # negative: a KNOWN WRONG expected phase, through the same helper, must fail
    with pytest.raises(AssertionError):
        assert_refusal(
            outcome, code='INSTALL_NUL_FORBIDDEN',
            projection=('_pairs', '_nul', '_normalize'),
            innermost='_normalize', identity=True,
        )


def test_phase_ctrl_03_identity_rejects_a_substituted_exception(monkeypatch):
    """PHASE-CTRL-03. The identity assertion catches a SAME-CODE substitution.

    `_nul` is recursive, so the substituting layer here carries the SAME seam name
    as the deepest witness and raises the SAME refusal code. Neither the name check
    nor the code check can be what rejects it -- only identity can. Without this
    control, `identity=True` could be a word rather than a test.
    """

    def substitute(inner: Callable[..., Any]) -> Callable[..., Any]:
        def layer(*args: Any, **kwargs: Any) -> Any:
            try:
                return inner(*args, **kwargs)
            except MigrationUnitRejected as original:
                raise MigrationUnitRejected(reason_code=original.reason_code) from None
        return layer

    payload = control()
    payload['migration_id'] += NUL
    with recorder(monkeypatch, outer={'_nul': substitute}) as run:
        outcome = run(payload)

    assert outcome.code == 'INSTALL_NUL_FORBIDDEN'
    assert outcome.innermost == '_nul'
    assert outcome.substituted, 'the substitution was not detected at all'

    # THE DISCRIMINATING ASSERTION. `substituted` alone does not distinguish a
    # recorder that preserves the deepest witness from one that overwrites it --
    # both report True here. What separates them is WHICH object survived: the
    # retained witness must be the FIRST exception any seam saw.
    assert outcome.exception_events, 'no seam witnessed an exception'
    first_seam, first_id = outcome.exception_events[0]
    assert outcome.innermost == first_seam
    assert id(outcome.innermost_exception) == first_id, (
        'the deepest witness was overwritten by a later, different exception'
    )

    with pytest.raises(AssertionError):
        assert_refusal(
            outcome, code='INSTALL_NUL_FORBIDDEN', projection=('_pairs', '_nul'),
            innermost='_nul', identity=True,
        )


def test_phase_ctrl_04_recorder_restores_between_recordings(monkeypatch):
    """PHASE-CTRL-04. Two recorders in one test do not stack.

    The earlier recorder never undid its patches, so a second one wrapped the first
    one's wrappers and double-counted every entry. The projection de-duplicates, so
    it looked right. This asserts restoration directly instead of hoping.
    """

    before = subject._nul
    with recorder(monkeypatch) as run:
        inside = subject._nul
        first = run(control())
    assert subject._nul is before, 'the recorder did not restore on exit'
    assert inside is not before

    with recorder(monkeypatch) as run:
        second = run(control())
    assert subject._nul is before

    assert first is not second, 'each run must return a fresh Outcome'
    assert first.events == second.events, 'the second recording was contaminated'


# ---------------------------------------------------------------------------
# PHASE-NUL-01 -- O02 is the reachable side; the rendered guard is not reachable
# ---------------------------------------------------------------------------

def _with_nul(mutate: Callable[[Any], None]) -> Any:
    payload = control()
    mutate(payload)
    return payload


NUL_ROUTES: list[tuple[str, Callable[[Any], None]]] = [
    ('template', lambda p: p.__setitem__(
        'template', p['template'].replace('BEGIN', 'BEGIN' + NUL))),
    ('migration_id', lambda p: p.__setitem__('migration_id', p['migration_id'] + NUL)),
    ('verification_body', lambda p: p['verification']['functions'][0].__setitem__(
        'body', p['verification']['functions'][0]['body'] + NUL)),
    ('policy_function_name', lambda p: p['policy']['functions'][0].__setitem__(
        'name', p['policy']['functions'][0]['name'] + NUL)),
    ('mapping_key', lambda p: p['policy']['functions'][0].__setitem__(NUL + 'k', 'v')),
]


@pytest.mark.parametrize('label,mutate', NUL_ROUTES, ids=[r[0] for r in NUL_ROUTES])
def test_phase_nul_01a_declaration_routes_refuse_at_o02(label, mutate, monkeypatch):
    """PHASE-NUL-01a. Every route carrying a NUL into the declaration refuses at O02."""

    with recorder(monkeypatch) as run:
        outcome = run(_with_nul(mutate))
    assert_refusal(
        outcome, code='INSTALL_NUL_FORBIDDEN', projection=('_pairs', '_nul'),
        innermost='_nul', identity=True,
        forbidden=('_normalize', '_render_exact_sql', '_load_parser'),
    )


def test_phase_nul_01a_json_escape_route_refuses_at_o02(monkeypatch):
    """PHASE-NUL-01a. A NUL written as a `\\u0000` escape decodes before `_nul` sees it."""

    body = json.dumps(control()).replace('BEGIN', 'BEGIN\\u0000').encode('utf-8')
    with recorder(monkeypatch) as run:
        outcome = run(body)
    assert_refusal(
        outcome, code='INSTALL_NUL_FORBIDDEN', projection=('_pairs', '_nul'),
        innermost='_nul', identity=True,
        forbidden=('_normalize', '_render_exact_sql', '_load_parser'),
    )


@pytest.mark.parametrize('schema_key', [
    'tenant_aaaa' + NUL + 'aaa',
    'tenant_aaaaaaaa' + NUL,
], ids=['nul-inside', 'nul-suffix'])
def test_phase_nul_01b_schema_key_refuses_before_rendering(schema_key, monkeypatch):
    """PHASE-NUL-01b. A NUL in the schema key never reaches the renderer.

    The forbidden seams are the point of this case, not decoration: they are what
    make "before rendering" an assertion rather than a description.
    """

    with recorder(monkeypatch) as run:
        outcome = run(control(), schema_key=schema_key)
    assert_refusal(
        outcome, code='SCHEMA_KEY_INVALID',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape',
                    '_unique_declarations', '_declaration'),
        innermost=None, identity=False,
        forbidden=('_render_exact_sql', '_load_parser', '_parse_exact_sql'),
    )


def test_phase_nul_01c_nul_runs_before_normalize(monkeypatch):
    """PHASE-NUL-01c. O02 precedes normalization, ON THE PATHS EXERCISED HERE.

    Corrected: an earlier version had the risk backwards. If normalization ran
    FIRST, a NUL it introduced would still meet the check and be caught. It is the
    CURRENT order -- NUL check, then normalize -- that leaves anything normalization
    introduces unchecked, which is exactly why `01d` and `01d_seq` matter.

    This is a claim about the paths these cases exercise, not a universal measured
    property of every possible input.
    """

    with recorder(monkeypatch) as run:
        outcome = run(control())
    assert outcome.accepted
    order = outcome.projection
    assert order.index('_nul') < order.index('_normalize')


def _codepoints_producing_nul(normalizer: Callable[[str], str]) -> list[str]:
    found = []
    for point in range(1, 0x110000):
        if 0xD800 <= point <= 0xDFFF:
            continue
        if NUL in normalizer(chr(point)):
            found.append(hex(point))
    return found


def test_phase_nul_01d_normalize_body_never_introduces_nul():
    """PHASE-NUL-01d. Exhaustive single-codepoint scan through the REAL normalizer.

    Exercises `normalize_body`, not `unicodedata.normalize`: the production function
    is what the checker calls, and a future change to it is exactly what this scan
    exists to catch.
    """

    assert _codepoints_producing_nul(normalize_body) == []


SEQUENCES = [
    '\\u0000',          # six literal characters, not an escape today
    '\\\\u0000',
    '\\x00',
    'a\r\nb',
    'é',          # combining sequence, composes under NFC
    '%00',
    '&#0;',
]


def _sequences_producing_nul(normalizer: Callable[[str], str]) -> list[str]:
    return [s for s in SEQUENCES if NUL in normalizer(s)]


def test_phase_nul_01d_seq_no_sequence_unescaping():
    """PHASE-NUL-01d-seq. A singleton scan cannot see sequence-dependent unescaping.

    `01d` proves no single codepoint normalizes to a NUL. It says nothing about a
    normalizer that later learns to interpret a multi-character escape.

    SCOPE: these are REPRESENTATIVE sequence regressions, not the whole class. No
    finite set of strings closes it. What generalizes the result is the source-level
    argument -- `normalize_body` is NFC plus CRLF folding, and `_render_exact_sql` is
    substitution plus strict UTF-8 -- and these cases are what make that argument
    fail loudly if either changes.
    """

    assert _sequences_producing_nul(normalize_body) == []


def test_phase_nul_01d_mut_the_sequence_scan_detects_unescaping():
    """PHASE-NUL-01d-mut. The sequence scan can fail.

    Without this, `01d_seq` is an assertion nobody has seen go red. The mutant is a
    representative normalizer that unescapes `\\u0000`; the SEQUENCE scan must catch
    it. It proves nothing about the singleton scan in `01d`, which is a different
    assertion with its own failure mode.
    """

    def unescaping(value: str) -> str:
        return normalize_body(value).replace('\\u0000', NUL)

    assert _sequences_producing_nul(unescaping) != []


def test_phase_nul_01e_control_reaches_the_parser(monkeypatch):
    """PHASE-NUL-01e. An accepted control runs the whole intake chain.

    WHAT THIS PROVES: the preceding steps, including the rendered-NUL guard, ran in
    the CURRENT source.

    WHAT IT DOES NOT PROVE: that the guard is still present. Delete it and this case
    still passes. Guard retention is `01f`'s job, and conflating the two is how a
    green test comes to stand for something it never checked.
    """

    with recorder(monkeypatch) as run:
        outcome = run(control())
    assert outcome.accepted
    assert '_render_exact_sql' in outcome.projection
    assert '_load_parser' in outcome.projection


def test_phase_nul_01f_rendered_guard_is_retained_fault_injection(monkeypatch):
    """PHASE-NUL-01f. FAULT INJECTION -- forces a NUL out of the renderer.

    This is the only case that pins RETENTION of the rendered-SQL guard. It is
    labelled fault injection because the input is impossible through the public
    entry point, and it does NOT contradict the natural-input unreachability the
    other cases establish. The two claims answer different questions: `01a`-`01d`
    say nothing gets there; this says the guard is still standing if something did.

    It is synthetic checker O05 coverage. It is NOT the runner-side NUL obligation
    in `typed_plan.consume_plan`, which is reachable, separate and still open.

    THE CODE ALONE WOULD NOT BE ENOUGH. `INSTALL_NUL_FORBIDDEN` could in principle
    come from a later refusal, so this asserts WHERE: after the injected renderer
    returned, and before the parser AND before any checksum work. `function_checksum`
    is counted rather than forbidden by exception, so a poisoned stand-in cannot
    become the refusal the case is looking for.
    """

    calls: list[tuple[Any, ...]] = []
    real_checksum = subject.function_checksum   # captured BEFORE patching

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real_checksum(*args, **kwargs)

    def poisoned(template: str, schema_key: str) -> bytes:
        return template.replace('{schema}', schema_key).encode('utf-8') + NUL.encode('utf-8')

    monkeypatch.setattr(subject, 'function_checksum', counted, raising=True)
    with recorder(monkeypatch, overrides={'_render_exact_sql': poisoned}) as run:
        outcome = run(control())

    assert_refusal(
        outcome, code='INSTALL_NUL_FORBIDDEN',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape',
                    '_unique_declarations', '_declaration', '_render_exact_sql'),
        innermost=None, identity=False,
        forbidden=('_load_parser', '_parse_exact_sql'),
    )
    assert calls == [], 'the refusal must precede any checksum work'

    # A zero count means nothing unless the counter can count. Same stand-in, same
    # test, no injection: an accepted control must reach the checksum.
    with recorder(monkeypatch) as run:
        clean = run(control())
    assert clean.accepted
    assert calls != [], 'the checksum stand-in never fired, so the zero above was vacuous'


def test_phase_nul_01g_renderer_output_is_exactly_substitution():
    """PHASE-NUL-01g. The renderer is substitution and strict UTF-8, nothing else.

    `01a`-`01d` reason about the renderer's inputs. That reasoning only holds while
    the renderer stays this simple, so the shape is pinned rather than assumed.
    """

    payload = control()
    rendered = subject._render_exact_sql(payload['template'], SCHEMA)
    assert rendered == payload['template'].replace('{schema}', SCHEMA).encode('utf-8', 'strict')
    assert NUL.encode('utf-8') not in rendered


# ---------------------------------------------------------------------------
# PHASE-INTAKE-01 -- O01 / O03 / O04 / O05 attribution
# ---------------------------------------------------------------------------

def test_phase_intake_01a_o01_duplicate_key(monkeypatch):
    """PHASE-INTAKE-01a. A duplicate key in the SOURCE TEXT refuses in the json hook."""

    body = ('{"migration_id": "x", ' + json.dumps(control())[1:]).encode('utf-8')
    with recorder(monkeypatch) as run:
        outcome = run(body)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID', projection=('_pairs',),
        innermost='_pairs', identity=True,
        forbidden=('_nul', '_normalize'),
    )


def test_phase_intake_01b_o03_duplicate_after_nfc(monkeypatch):
    """PHASE-INTAKE-01b. Two keys that collide only AFTER NFC refuse inside `_normalize`."""

    payload = control()
    payload['policy']['functions'][0]['café'] = 1
    payload['policy']['functions'][0]['café'] = 2
    with recorder(monkeypatch) as run:
        outcome = run(payload)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID',
        projection=('_pairs', '_nul', '_normalize'),
        innermost='_pairs', identity=True,
        forbidden=('_closed_shape',),
    )


def test_phase_intake_01c_same_seam_different_phase(monkeypatch):
    """PHASE-INTAKE-01c. The finding itself, asserted so it cannot regress silently.

    O01 and O03 share `_pairs` and share `INSTALL_POLICY_INVALID`. If a future change
    made the projection stop distinguishing them, every other intake case would still
    pass and the distinction would be lost without a failure. This is that failure.
    """

    duplicate = ('{"migration_id": "x", ' + json.dumps(control())[1:]).encode('utf-8')
    collision = control()
    collision['policy']['functions'][0]['café'] = 1
    collision['policy']['functions'][0]['café'] = 2

    with recorder(monkeypatch) as run:
        o01 = run(duplicate).projection
    with recorder(monkeypatch) as run:
        o03 = run(collision).projection

    assert o01 == ('_pairs',)
    assert o03 == ('_pairs', '_nul', '_normalize')
    assert o01 != o03


def test_phase_intake_01d_o01_nonfinite(monkeypatch):
    """PHASE-INTAKE-01d. A non-finite JSON constant refuses in `_constant`."""

    body = json.dumps(control()).replace(
        '"checksum_version": 3', '"checksum_version": NaN').encode('utf-8')
    with recorder(monkeypatch) as run:
        outcome = run(body)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID', projection=('_constant',),
        innermost='_constant', identity=True,
    )


def test_phase_intake_01e_i_malformed_before_any_object(monkeypatch):
    """PHASE-INTAKE-01e-i. Malformed JSON with NO complete nested object.

    LIMIT, stated because v1 got this wrong: an empty projection attributes THIS
    fixture, not malformed JSON in general. `json.loads` calls the hook as it parses,
    so a payload whose syntax error comes AFTER a complete object enters `_pairs`
    first -- that is `01e-ii`, and v1's single assertion would have failed on it.
    """

    with recorder(monkeypatch) as run:
        outcome = run(b'{"a": ')
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID', projection=(),
        innermost=None, identity=False,
    )


@pytest.mark.parametrize('body', [
    b'{"a": {"b": 1}, "c": ',
    b'{"a": {"b": 1}, "c": {"d": 2}, ',
], ids=['one-object', 'two-objects'])
def test_phase_intake_01e_ii_malformed_after_an_object(body, monkeypatch):
    """PHASE-INTAKE-01e-ii. Malformed JSON AFTER a complete object ENTERS `_pairs`.

    Note the difference between entering a seam and refusing in one. `_pairs` runs --
    it appears in the projection -- and returns normally; the decoder then fails on the
    trailing syntax, inline, with no seam on the raising path. So the innermost raiser
    is None even though the projection is non-empty. Attribution needs both halves.
    """

    with recorder(monkeypatch) as run:
        outcome = run(body)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID', projection=('_pairs',),
        innermost=None, identity=False,
    )


@pytest.mark.parametrize('body', [
    json.dumps(control()),
    bytearray(json.dumps(control()).encode('utf-8')),
    b'{"a": {"b": 1}, "c": "\xff\xfe"}',
], ids=['str-payload', 'bytearray-payload', 'invalid-utf8'])
def test_phase_intake_01e_iii_raw_intake_rejections(body, monkeypatch):
    """PHASE-INTAKE-01e-iii. Non-`bytes` payloads and a decode failure refuse first.

    The third row is the interesting one: invalid UTF-8 gives an EMPTY projection even
    though a complete object precedes it, because the decode runs before the parse.
    """

    with recorder(monkeypatch) as run:
        outcome = run(body)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID', projection=(),
        innermost=None, identity=False,
    )


INTAKE_SHAPE: list[tuple[str, Callable[[Any], None]]] = [
    ('missing-root-field', lambda p: p.pop('execution_role')),
    ('wrong-root-type', lambda p: p.__setitem__('checksum_version', '3')),
    ('extra-root-field', lambda p: p.__setitem__('extra_root_field', 1)),
]


@pytest.mark.parametrize('label,mutate', INTAKE_SHAPE, ids=[r[0] for r in INTAKE_SHAPE])
def test_phase_intake_01f_o03_closed_shape(label, mutate, monkeypatch):
    """PHASE-INTAKE-01f. Root-payload shape failures refuse in `_closed_shape`."""

    payload = control()
    mutate(payload)
    with recorder(monkeypatch) as run:
        outcome = run(payload)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape'),
        innermost='_closed_shape', identity=True,
        forbidden=('_unique_declarations', '_declaration'),
    )


def test_phase_intake_01g_o03_duplicate_identity(monkeypatch):
    """PHASE-INTAKE-01g. Two declarations with one identity refuse in `_unique_declarations`."""

    payload = control()
    payload['policy']['functions'].append(json.loads(json.dumps(payload['policy']['functions'][0])))
    with recorder(monkeypatch) as run:
        outcome = run(payload)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape', '_unique_declarations'),
        innermost='_unique_declarations', identity=True,
        forbidden=('_declaration',),
    )


def test_phase_intake_01h_o04_verification_set_mismatch(monkeypatch):
    """PHASE-INTAKE-01h. A verification set that does not match the policy set is O04."""

    payload = control()
    payload['verification']['functions'] = []
    with recorder(monkeypatch) as run:
        outcome = run(payload)
    assert_refusal(
        outcome, code='INSTALL_POLICY_INVALID',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape',
                    '_unique_declarations', '_declaration'),
        innermost='_declaration', identity=True,
        forbidden=('_render_exact_sql',),
    )


def test_phase_intake_01i_o04_wrong_kind(monkeypatch):
    """PHASE-INTAKE-01i. An unsupported verification `kind` is O04, with its own code.

    `verification["kind"]` is `function_metadata`. It is NOT `typed_function_v3`,
    which is the UNIT's kind in `units.py` -- two vocabularies that have been
    conflated before, and pinning the code here is what makes the conflation loud.
    """

    payload = control()
    payload['verification']['kind'] = 'not_function_metadata'
    with recorder(monkeypatch) as run:
        outcome = run(payload)
    assert_refusal(
        outcome, code='VERIFICATION_KIND_UNKNOWN',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape',
                    '_unique_declarations', '_declaration'),
        innermost='_declaration', identity=True,
        forbidden=('_render_exact_sql',),
    )


@pytest.mark.parametrize('schema_key', ['NOT_A_TENANT', 7], ids=['bad-pattern', 'not-a-string'])
def test_phase_intake_01j_o05_schema_key(schema_key, monkeypatch):
    """PHASE-INTAKE-01j. The schema-key check refuses INLINE, after O04.

    No inner-identity claim: there is no wrapped seam on the raising path to witness
    the exception, so asserting one would be asserting something unobserved.
    """

    with recorder(monkeypatch) as run:
        outcome = run(control(), schema_key=schema_key)
    assert_refusal(
        outcome, code='SCHEMA_KEY_INVALID',
        projection=('_pairs', '_nul', '_normalize', '_closed_shape',
                    '_unique_declarations', '_declaration'),
        innermost=None, identity=False,
        forbidden=('_render_exact_sql', '_load_parser'),
    )
