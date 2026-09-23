"""PHASE-SIGNATURE-01 Gate 3 PART B -- the observer. B-01 .. B-24.

Frozen design: Part A/B cases v3 (16d5530d...), Blocker-2 seams v3 (fe0070d2...),
B-24 two-layer contract v3 (b61529de...), owner freeze OD-SIG-24.

The observer replaces ``_require`` ONLY; ``_fail`` is never patched (Gate 2 section 0
defect 1). Every live fail-closed case (B-14..B-20) induces its condition AFTER the
complete S1..S14 preflight passed, names its diagnosis by EQUALITY, asserts the EXACT
per-call counter signature, shows no accepted result, and shows restoration.

B-24 is the M4 kill. Its assertions live here, in the parent; the "no body ran"
property lives in a child process that carries no assertion (OD-SIG-11).
"""

import gc
import json
import os
import subprocess
import sys
import weakref
from collections.abc import Callable
from typing import Any

import pytest
import sig_child_plugin as child
import sig_overlay as ov

from haloflow.m01.errors import MigrationUnitRejected

NINE_ROWS = (
    'A-wrong-create-schema', 'A-unqualified-create', 'A-wrong-create-name',
    'A-input-type', 'A-input-order', 'A-input-name',
    'A-output-name', 'A-output-order', 'A-wrong-grant-signature',
)

CONTROLS = ('C01', 'C02', 'C03', 'C04', 'C05', 'C06')

CHILD_TIMEOUT_SECONDS = 600


def c01_bytes() -> bytes:
    return ov.canonical_validated_bytes(ov.control_payload('C01'))


def control_bytes(control_id: str) -> bytes:
    return ov.canonical_validated_bytes(ov.control_payload(control_id))


def row_bytes(case_id: str) -> bytes:
    return ov.canonical_validated_bytes(ov.row_payload(case_id))


def failing(record: ov.RunRecord) -> list[ov.Event]:
    return [e for e in record.events if e.outcome == 'fail']


def assert_instruments_restored(resolution: ov.Resolution) -> None:
    for name in ov.INSTRUMENTED_GLOBALS:
        assert resolution.module.__dict__[name] is resolution.originals[name], name
    assert ov.LIVE_SEAMS.all_original()


class FakeFrame:
    """A controlled frame-like object for the frame_provider seam (B-15, B-16)."""

    __slots__ = ('f_code', 'f_lineno', 'f_back', '__weakref__')

    def __init__(self, f_code: Any, f_lineno: int, f_back: Any) -> None:
        self.f_code = f_code
        self.f_lineno = f_lineno
        self.f_back = f_back


def _foreign() -> None:
    """Its code object is foreign to the snapshot."""


# ================================================= B.1 what is patched, what is not


@pytest.mark.sig_case('B-01')
def test_b01_require_is_replaced_and_fail_is_not(resolution, schema):
    seen: dict[str, Any] = {}

    def probe(entry: Callable[..., Any]) -> Callable[..., Any]:
        def during(*args: Any, **kwargs: Any) -> Any:
            module = resolution.module
            seen['require'] = module._require
            seen['fail'] = module._fail
            return entry(*args, **kwargs)
        return during

    ov.run_once(resolution, c01_bytes(), schema, public_entry=probe)
    assert isinstance(seen['require'], ov.Observer)
    assert seen['require'] is not resolution.original_require
    assert seen['fail'] is resolution.original_fail                     # NOT patched
    assert_instruments_restored(resolution)


@pytest.mark.sig_case('B-02')
def test_b02_no_observation_from_requires_indirect_fail(resolution, schema):
    """_require reaches _fail by module-global lookup; _fail is not observed, so that
    indirect call can never produce an event. Every event is one signature _require."""
    record = ov.run_once(resolution, row_bytes('A-wrong-create-name'), schema)
    sig_delegations = [s for s in record.steps if s[1] == 'delegate']
    assert len(record.events) == len(sig_delegations)
    assert all(e.site in ov.SITE_IDS for e in record.events)
    assert len(failing(record)) == 1
    assert_instruments_restored(resolution)


# ================================================== B.2 capture, delegate, record


@pytest.mark.sig_case('B-03')
def test_b03_site_and_edge_captured_before_delegation(resolution, schema):
    record = ov.run_once(resolution, c01_bytes(), schema)
    calls = sorted({i for i, _ in record.steps})
    assert calls, 'no signature calls observed'
    for index in calls:
        order = [step for i, step in record.steps if i == index]
        assert order.index('site') < order.index('delegate')
        if 'edge' in order:
            assert order.index('edge') < order.index('delegate')


@pytest.mark.sig_case('B-04')
def test_b04_original_delegated_to_exactly_once(resolution, schema):
    record = ov.run_once(resolution, c01_bytes(), schema)
    calls = sorted({i for i, _ in record.steps})
    for index in calls:
        assert [s for i, s in record.steps if i == index].count('delegate') == 1
    assert record.delegations == len(record.events) + record.passthrough_calls


@pytest.mark.sig_case('B-05')
def test_b05_observer_never_evaluates_ok_itself(resolution, schema):
    """ok is a sentinel. Delegation count == 1, __bool__ count == 1, and the evaluation
    happens INSIDE the original-delegation interval. A zero count is impossible:
    production runs ``if not ok:``."""
    record = ov.RunRecord()
    observer = ov.Observer(resolution, record)
    sentinels: list[tuple[bool, Any]] = []

    class Sentinel:
        def __init__(self, value: Any) -> None:
            self.value = bool(value)
            self.evaluated_inside: list[bool] = []

        def __bool__(self) -> bool:
            self.evaluated_inside.append(observer.in_delegation)
            return self.value

    def shim(ok: Any, *args: Any, **kwargs: Any) -> Any:
        sentinel = Sentinel(ok)
        is_sig = any(a is resolution.sig_code for a in args) or \
            kwargs.get('code') is resolution.sig_code
        sentinels.append((is_sig, sentinel))
        return observer(sentinel, *args, **kwargs)

    def skip_shim() -> Any:
        return sys._getframe(2)            # the shim's frame; its f_back is the real site

    module = resolution.module
    module.__dict__['_require'] = shim
    try:
        with ov.LIVE_SEAMS.replaced('frame_provider', skip_shim):
            module.validate_function_installation(c01_bytes(), schema_key=schema)
    finally:
        module.__dict__['_require'] = resolution.original_require
    sig = [s for is_sig, s in sentinels if is_sig]
    assert sig and len(sig) == len(record.events)
    for sentinel in sig:
        assert sentinel.evaluated_inside == [True]      # once, and inside delegation
    assert record.delegations == len(sentinels)          # each call delegated once
    assert_instruments_restored(resolution)


@pytest.mark.sig_case('B-06')
def test_b06_normal_return_records_pass(resolution, schema):
    record = ov.run_once(resolution, c01_bytes(), schema)
    assert record.accepted
    assert record.events and all(e.outcome == 'pass' and e.exception is None
                                 for e in record.events)


def _spy_original(resolution: ov.Resolution, seen: list[BaseException]) -> Callable[..., Any]:
    """A witness around the GENUINE original: records exactly what it raised."""
    def spy(*args: Any, **kwargs: Any) -> Any:
        try:
            return resolution.original_require(*args, **kwargs)
        except BaseException as raised:
            seen.append(raised)
            raise
    return spy


@pytest.mark.sig_case('B-07')
def test_b07_raise_records_the_exact_exception_object(resolution, schema):
    seen: list[BaseException] = []
    record = ov.run_once(resolution, row_bytes('A-wrong-create-name'), schema,
                         observer_original=_spy_original(resolution, seen))
    [event] = failing(record)
    assert len(seen) == 1
    assert event.exception is seen[0]                   # not a copy, code or repr
    assert type(event.exception) is MigrationUnitRejected


@pytest.mark.sig_case('B-08')
def test_b08_exception_reraised_unchanged(resolution, schema):
    seen: list[BaseException] = []
    record = ov.run_once(resolution, row_bytes('A-wrong-create-name'), schema,
                         observer_original=_spy_original(resolution, seen))
    assert record.public is seen[0]


def _b09_one_path(resolution: ov.Resolution, schema: str, payload: bytes) -> None:
    refs: list[weakref.ref[Any]] = []

    class Tracked(ov.FrameProxy):
        __slots__ = ()

        @property
        def f_back(self) -> Any:
            back = self._frame.f_back
            if back is None:
                return None
            proxy = Tracked(back)
            refs.append(weakref.ref(proxy))
            return proxy

    def provider() -> Any:
        proxy = Tracked(sys._getframe(1))
        refs.append(weakref.ref(proxy))
        return proxy

    with ov.LIVE_SEAMS.replaced('frame_provider', provider):
        record = ov.run_once(resolution, payload, schema)
    kept_exception = record.public                        # kept alive on purpose
    assert refs, 'the provider was never consulted'
    del provider, record
    gc.collect()
    assert all(r() is None for r in refs), 'the observer retained a frame proxy'
    del kept_exception


@pytest.mark.sig_case('B-09')
def test_b09_frame_references_released_on_both_paths(resolution, schema):
    """Frames are not weak-referenceable (measured). The frame_provider seam supplies
    a weak-referenceable PROXY -- the object the observer actually holds. After both
    the return and the raise paths, the proxies are dead once test references are
    dropped, while the raised exception and its traceback are kept SEPARATELY alive."""
    _b09_one_path(resolution, schema, c01_bytes())                          # return path
    _b09_one_path(resolution, schema, row_bytes('A-wrong-create-name'))     # raise path


@pytest.mark.sig_case('B-10')
def test_b10_both_successful_and_failing_events_recorded(resolution, schema):
    record = ov.run_once(resolution, row_bytes('A-input-name'), schema)
    outcomes = [e.outcome for e in record.events]
    assert 'pass' in outcomes and outcomes.count('fail') == 1


@pytest.mark.sig_case('B-11')
def test_b11_other_codes_delegated_untouched_and_not_recorded(resolution, schema):
    """An independent spy stands in for the original. Identity-distinct sentinels prove
    each non-signature call reaches it UNTOUCHED -- same objects, same positional /
    keyword shape, exactly one delegation -- and that nothing is recorded."""
    non_signature = resolution.module.Code.INSTALL_POLICY_INVALID
    assert non_signature is not resolution.sig_code
    received: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    returned = object()

    def spy(*args: Any, **kwargs: Any) -> Any:
        received.append((args, kwargs))
        return returned

    shapes: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        ((object(), non_signature), {}),                  # positional code
        ((object(),), {'code': non_signature}),           # keyword code
        ((object(),), {}),                                # default code
    ]
    for args, kwargs in shapes:
        record = ov.RunRecord()
        observer = ov.Observer(resolution, record, original=spy)
        received.clear()
        result = observer(*args, **kwargs)
        assert result is returned                                   # returned untouched
        assert len(received) == 1                                   # delegated ONCE
        got_args, got_kwargs = received[0]
        assert len(got_args) == len(args)
        assert all(g is a for g, a in zip(got_args, args, strict=True))
        assert set(got_kwargs) == set(kwargs)
        assert all(got_kwargs[k] is kwargs[k] for k in kwargs)
        assert record.events == [] and record.steps == []           # nothing recorded
        assert record.passthrough_calls == 1 and record.delegations == 1

    # and on a real run: every delegation is either a recorded signature call or a
    # pass-through, never both
    record = ov.run_once(resolution, c01_bytes(), schema)
    assert record.passthrough_calls > 0
    assert record.delegations == len(record.events) + record.passthrough_calls


# ============================================================== B.3 the oracle


@pytest.mark.sig_case('B-12')
def test_b12_exactly_one_failing_event_per_refusal_row(resolution, schema):
    for case_id in NINE_ROWS:
        record = ov.run_once(resolution, row_bytes(case_id), schema)
        assert record.public is not None, case_id
        assert len(failing(record)) == 1, case_id
        passing = [e for e in record.events if e.outcome == 'pass']
        assert len(passing) == len(record.events) - 1, case_id   # earlier events kept
    # Accepted controls -- ALL SIX, C01 included -- carry ZERO failing events. At
    # execution this is subject to the staged backing-control gate (work plan v3 step 6).
    for control_id in CONTROLS:
        control = ov.run_once(resolution, control_bytes(control_id), schema)
        assert control.accepted, control_id
        assert failing(control) == [], control_id


@pytest.mark.sig_case('B-13')
def test_b13_no_unknown_in_raw_events_or_oracle(resolution, schema):
    subjects = [c01_bytes()] + [row_bytes(c) for c in NINE_ROWS]
    for payload in subjects:
        record = ov.run_once(resolution, payload, schema)
        assert record.abort is None
        for event in record.events:
            assert event.site in ov.SITE_IDS and event.site != ov.UNKNOWN
            assert event.edge is None or event.edge in ov.EDGE_IDS
            assert ov.phase_for_site(event.site) != ov.UNKNOWN


# ========================================================= B.4 live fail-closed aborts


def _expect_obs_abort(resolution: ov.Resolution, schema: str, seam: str, value: Any,
                      payload: bytes | None = None) -> ov.ObservationAbort:
    assert resolution.ledger == ov.STAGES                        # full S1..S14 passed
    with ov.LIVE_SEAMS.replaced(seam, value):
        record = ov.run_once(resolution, payload or c01_bytes(), schema)
    assert ov.LIVE_SEAMS.get(seam) is ov.LIVE_SEAMS.original(seam)
    assert record.abort is not None, 'no fail-closed abort'
    assert not record.accepted and record.public is None        # no accepted result
    assert_instruments_restored(resolution)
    return record.abort


def counters(**ones: int) -> dict[str, int]:
    return {name: ones.get(name, 0) for name in ov.COUNTER_NAMES}


@pytest.mark.sig_case('B-14')
def test_b14_currentframe_returns_none(resolution, schema):
    abort = _expect_obs_abort(resolution, schema, 'frame_provider', lambda: None)
    assert abort.diagnosis == ov.Diag.FRAME_ABSENT
    assert abort.counters == counters()


@pytest.mark.sig_case('B-15')
def test_b15_unexpected_first_frame(resolution, schema):
    def provider() -> Any:
        observer_frame = sys._getframe(1)
        genuine_second = ov.FrameProxy(observer_frame.f_back.f_back)
        return FakeFrame(None, 0, FakeFrame(_foreign.__code__, 1, genuine_second))

    abort = _expect_obs_abort(resolution, schema, 'frame_provider', provider)
    assert abort.diagnosis == ov.Diag.FRAME1_UNEXPECTED
    assert abort.details['code'] is _foreign.__code__
    assert abort.counters == counters()


@pytest.mark.sig_case('B-16')
def test_b16_unexpected_second_frame(resolution, schema):
    def provider() -> Any:
        genuine_first = sys._getframe(1).f_back
        return FakeFrame(None, 0, FakeFrame(genuine_first.f_code, genuine_first.f_lineno,
                                            None))

    abort = _expect_obs_abort(resolution, schema, 'frame_provider', provider)
    assert abort.diagnosis == ov.Diag.FRAME2_UNEXPECTED
    assert abort.counters == counters(frame1_resolved=1)


@pytest.mark.sig_case('B-17')
def test_b17_live_site_maps_to_zero_ids(resolution, schema):
    """The zero-live-mapping path NC4 measured: SIG.CREATE.NAME / _validate_ast."""
    base = ov.default_live_site_projection
    removed = resolution.sites[ov.SIG_CREATE_NAME]

    def projection(res: ov.Resolution, code_name: str, lineno: int) -> tuple[str, ...]:
        ids = base(res, code_name, lineno)
        return () if ids == (ov.SIG_CREATE_NAME,) else ids

    abort = _expect_obs_abort(resolution, schema, 'live_site_projection', projection)
    assert abort.diagnosis == ov.Diag.OBS_SITE_UNMAPPED
    code_name, lineno = abort.details['live_site']
    assert code_name == removed.enclosing and removed.lineno <= lineno <= removed.end_lineno
    assert abort.counters == counters(frame1_resolved=1, frame2_resolved=1,
                                      site_lookup_entered=1)


@pytest.mark.sig_case('B-18')
def test_b18_live_site_maps_to_more_than_one_id(resolution, schema):
    base = ov.default_live_site_projection
    pair = (ov.SIG_CREATE_IDENTITY, ov.SIG_CREATE_COMPARE)

    def projection(res: ov.Resolution, code_name: str, lineno: int) -> tuple[str, ...]:
        ids = base(res, code_name, lineno)
        return pair if ids == (ov.SIG_CREATE_IDENTITY,) else ids

    abort = _expect_obs_abort(resolution, schema, 'live_site_projection', projection)
    assert abort.diagnosis == ov.Diag.OBS_SITE_AMBIGUOUS
    assert abort.details['ids_found'] == 2
    assert abort.details['ids'] == frozenset(pair)                   # the exact pair
    assert abort.counters == counters(frame1_resolved=1, frame2_resolved=1,
                                      site_lookup_entered=1)


@pytest.mark.sig_case('B-19')
def test_b19a_qualified_caller_maps_to_zero_edges(resolution, schema):
    abort = _expect_obs_abort(resolution, schema, 'live_edge_projection',
                              lambda res, caller: ())
    assert abort.diagnosis == ov.Diag.OBS_EDGE_UNMAPPED
    assert abort.details['edges_found'] == 0
    assert abort.counters == counters(frame1_resolved=1, frame2_resolved=1,
                                      site_lookup_entered=1, edge_lookup_entered=1)


@pytest.mark.sig_case('B-19')
def test_b19b_qualified_caller_maps_to_more_than_one_edge(resolution, schema):
    pair = (ov.EDGE_CREATE, ov.EDGE_INVENTORY)
    abort = _expect_obs_abort(resolution, schema, 'live_edge_projection',
                              lambda res, caller: pair)
    assert abort.diagnosis == ov.Diag.OBS_EDGE_AMBIGUOUS
    assert abort.details['edges'] == frozenset(pair)                 # the exact pair
    assert abort.counters == counters(frame1_resolved=1, frame2_resolved=1,
                                      site_lookup_entered=1, edge_lookup_entered=1)


@pytest.mark.sig_case('B-20')
@pytest.mark.parametrize('form', ['positional', 'keyword'])
def test_b20_runtime_code_classification_fails(form, resolution, schema):
    """The named call is the run's first signature call, SIG.QUALIFIED, whose frame
    chain, live site and live edge have ALREADY resolved. No invalid code object is
    constructed or delegated."""
    abort = _expect_obs_abort(resolution, schema, 'runtime_code_classifier',
                              lambda res, args, kwargs: ov.Unclassifiable(form))
    assert abort.diagnosis == ov.Diag.OBS_CODE_UNCLASSIFIABLE
    assert abort.details['argument_form'] == form
    assert abort.details['call_index'] == 0
    assert abort.counters == counters(frame1_resolved=1, frame2_resolved=1,
                                      site_lookup_entered=1, edge_lookup_entered=1,
                                      code_classify_entered=1)


# ============================================================== B.5 isolation


@pytest.mark.sig_case('B-21')
def test_b21_fresh_buffer_and_outcome_per_run(resolution, schema):
    """Every row and every control, in one sequence. Each run gets its own RunRecord
    and its own events list; no earlier buffer grows or changes; each run's outcome is
    its own, never a previous run's."""
    subjects = [(cid, row_bytes(cid)) for cid in NINE_ROWS] + \
        [(cid, control_bytes(cid)) for cid in CONTROLS]
    history: list[tuple[str, ov.RunRecord, tuple[ov.Event, ...], BaseException | None]] = []
    for label, payload in subjects:
        record = ov.run_once(resolution, payload, schema)
        for _, earlier, frozen, earlier_public in history:
            assert record is not earlier, label
            assert record.events is not earlier.events, label
            assert tuple(earlier.events) == frozen, label             # not aliased, not grown
            if record.public is not None:
                assert record.public is not earlier_public, label     # outcome is its own
        if label in NINE_ROWS:
            [event] = failing(record)
            assert record.public is event.exception and not record.accepted, label
        else:
            assert record.accepted and record.public is None and failing(record) == [], label
        history.append((label, record, tuple(record.events), record.public))
    assert len(history) == len(NINE_ROWS) + len(CONTROLS) == 15


@pytest.mark.sig_case('B-22')
def test_b22_restoration_and_no_stacking(resolution, schema):
    ov.run_once(resolution, c01_bytes(), schema, markers=ov.MARKER_TARGETS, witness=True)
    assert_instruments_restored(resolution)
    module = resolution.module
    module.__dict__['_require'] = lambda *a, **k: resolution.original_require(*a, **k)
    try:
        with pytest.raises(ov.StackingRefused):
            ov.run_once(resolution, c01_bytes(), schema)
    finally:
        module.__dict__['_require'] = resolution.original_require
    assert_instruments_restored(resolution)


# ============================================== B.6 identity to the public boundary


@pytest.mark.sig_case('B-23')
def test_b23_public_catch_receives_the_recorded_object(resolution, schema):
    record = ov.run_once(resolution, row_bytes('A-wrong-create-name'), schema)
    [event] = failing(record)
    assert record.public is event.exception                     # identity; never ==/code


# ============================================================ B.7 the M4 kill


@pytest.mark.sig_case('B-24')
def test_b24_m4_kill_suite_level_setup_failure(tmp_path, resolution):
    """OUTER B-24. Launches the inner run under M4 and carries EVERY assertion."""
    artifacts = tmp_path / 'child-artifacts'
    artifacts.mkdir()
    env = {
        'PATH': os.environ.get('PATH', ''),
        'PYTHONHASHSEED': '0',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',       # only -p plugins load; deterministic
        'PYTHONPATH': os.pathsep.join([str(ov.HERE), str(ov.TESTS_M01),
                                       str(ov.HALOFLOW_ROOT / 'src')]),
        ov.MUTANT_ENV: 'M4',
        child.ARTIFACT_DIR_ENV: str(artifacts),
        child.EXPECTED_SNAPSHOT_ENV: ov.EXPECTED_SNAPSHOT_SHA256,
    }
    command = [sys.executable, '-B', '-P', '-m', 'pytest', '-p', 'sig_child_plugin',
               '-p', 'no:cacheprovider', '-q', 'tests/m01/phase_signature']
    try:
        proc = subprocess.run(command, cwd=ov.HALOFLOW_ROOT, env=env, capture_output=True,
                              text=True, timeout=CHILD_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail('B-24 FAILED: CHILD_TIMEOUT -- a hung child is never a kill',
                    pytrace=False)

    detail = f'\n--- child stdout ---\n{proc.stdout}\n--- child stderr ---\n{proc.stderr}'
    # 1. exit status == 97 exactly (excludes 0 success/skip, 1 failures, 2/4 import or
    #    collection/usage, 3 internal, 5 nothing collected, and any crash or signal)
    assert proc.returncode == child.PREFLIGHT_ABORT_EXIT, detail
    # 2. the sentinel exists and reads exactly 0; a missing file is a FAILURE
    body = json.loads((artifacts / child.BODY_ENTRY).read_text(encoding='utf-8'))
    assert body == {'bodies_entered': 0}
    preflight = json.loads((artifacts / child.PREFLIGHT).read_text(encoding='utf-8'))
    # 3. diagnosis by equality
    assert preflight['diagnosis'] == ov.Diag.BINDING_UNRESOLVED
    # 4. the forced-drift target
    assert preflight['unresolved_binding'] == ov.SIG_CREATE_NAME
    # 5. stage
    assert preflight['stage'] == 'preflight'
    # 6. EXACTLY the completed prefix S1..S13
    assert preflight['invariants_passed'] == list(ov.STAGES[:13])
    # 7. the closed inner selection, by equality
    selection = json.loads((artifacts / child.SELECTION).read_text(encoding='utf-8'))
    assert selection == ['A-26']
    # 8. the B-24 child artifacts are confined to the per-run directory (bounded claim)
    produced = {p.name for p in artifacts.iterdir()}
    assert produced == {child.BODY_ENTRY, child.PREFLIGHT, child.SELECTION}
    assert all(p.resolve().parent == artifacts.resolve() for p in artifacts.iterdir())
    # 9. saved output names outer B-24 as the killer and records the zero body count
    kill_record = {'killer': 'B-24', 'mutant': 'M4', 'inner_bodies_entered': 0,
                   'child_exit': proc.returncode,
                   'diagnosis': preflight['diagnosis'],
                   'invariants_passed': preflight['invariants_passed']}
    (tmp_path / 'b24-kill-record.json').write_text(json.dumps(kill_record, sort_keys=True),
                                                   encoding='utf-8')
    print(f'[B-24] KILLED BY B-24: {json.dumps(kill_record, sort_keys=True)}')
    # 10. the parent is undisturbed: M4 never entered this process
    assert ov.MUTANT_ENV not in os.environ
    fresh = ov.preflight()
    assert fresh.ledger == ov.STAGES
    assert fresh.sites[ov.SIG_CREATE_NAME] is not None
    assert resolution.module._require is resolution.original_require
