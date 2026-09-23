"""PHASE-SIGNATURE-01 kill-run HARNESS self-proofs. K-01 .. K-12.

These are NOT Gate 3 cases and earn no Gate 3 credit. They prove the kill-run harness
itself -- the obligations Codex attached to F2 and to the mutant definitions -- BEFORE any
mutant runs (``sig_killrun`` stage H, after the complete baseline). ``K-nn`` ids are
authoring ids, carried by the same ``sig_case`` marker so one selection mechanism serves
every stage.

  K-01  M3 projection slot: identity baseline; install over identity accepted; second
        install REFUSED; refused install leaves the slot unchanged; restore -> identity
  K-02  malformed raw input is BARRED: nothing installed, projection never entered
        (a throwing sentinel), slot at identity -- four malformed shapes
  K-03  a RAISING projection: the fault propagates; slot restored by ``finally``; the next
        projection is clean
  K-04  call counter == raw SIG.QUALIFIED count, and the full must-not-change table, for
        M3a and M3b on a synthetic 16-event PC-09-shaped ledger; M3b never adds an edge
  K-05  on a REAL C01 run: the identity projection leaves the ledger identical, calls ==
        raw SIG.QUALIFIED count, and the observer and ``_require`` are restored
  K-06  M-O11-L1 substitute fidelity: every enumerated observation equal, the object
        different, built without ``_fail``/``_require``; notes present and absent
  K-07  M-O11-L2 layer: out/back once each; the inner ``finally`` rebinds the marker on
        the RAISE path; the exception passes through unchanged; a missing marker refuses
  K-08  M5 layer: E2 same class and reason code, ``is not`` E1, raised from None;
        normal return untouched; correlation facts
  K-09  composition: ``BindingProbe`` is OUTERMOST around M5 and around M-O11-L2; a
        non-probe public entry is refused
  K-10  ``activated``: installs and restores ``sc.run``; refuses stacking, an unknown
        mutant and an out-of-scope case; apply-once pass-through
  K-11  M1 swap: O08 <-> O09c only, O11 untouched, via ``PHASE_SLOT`` which refuses stacking
  K-12  M2 collapse: COMPARE -> IDENTITY only; everything else and empty pass through;
        the chosen direction is recorded
"""

import types
from collections.abc import Callable
from typing import Any

import pytest
import sig_cases as sc
import sig_mutants as sm
import sig_overlay as ov

from haloflow.m01.errors import MigrationUnitRejected

_Q, _CN, _CI, _CC = ov.SIG_QUALIFIED, ov.SIG_CREATE_NAME, ov.SIG_CREATE_IDENTITY, \
    ov.SIG_CREATE_COMPARE


def _events(ledger: sc.Ledger) -> list[ov.Event]:
    """Synthetic Events from a pinned ledger. The failing event carries its own object."""
    return [ov.Event(site=s, edge=e, outcome=o,
                     exception=MigrationUnitRejected(reason_code='X') if o == 'fail' else None)
            for s, e, o in ledger]


def _identity_slot() -> bool:
    return sm.EDGE_PROJECTION_SLOT.current is sm.EDGE_PROJECTION_SLOT.identity


@pytest.mark.sig_case('K-01')
def test_k01_projection_slot_no_stack():
    slot = sm.EDGE_PROJECTION_SLOT
    assert _identity_slot()
    try:
        assert slot.install(sm.m3a_projection) is True
        assert slot.current is sm.m3a_projection
        assert slot.install(sm.m3b_projection) is False               # refused
        assert slot.current is sm.m3a_projection                      # unchanged
    finally:
        slot.restore()
    assert _identity_slot()


@pytest.mark.sig_case('K-02')
@pytest.mark.parametrize('malformed', [
    [ov.Event(_Q, None, 'pass', None)],                               # QUALIFIED, no edge
    [ov.Event(_Q, 'EDGE.NOT.AN.EDGE', 'pass', None)],                 # invalid edge
    [ov.Event(_CN, ov.EDGE_CREATE, 'pass', None)],                    # edgeless site, edge
    [ov.Event(ov.UNKNOWN, None, 'pass', None)],                       # unresolved site
], ids=['qualified-none', 'invalid-edge', 'edge-on-edgeless', 'unknown-site'])
def test_k02_malformed_input_is_barred(malformed):
    entered = []

    def throwing(validated_edge: str) -> str | None:
        entered.append(validated_edge)
        raise AssertionError('the projection was entered on malformed input')

    with pytest.raises(sm.KillRunAbort) as caught:
        sm.project_events(malformed, throwing)
    assert caught.value.diagnosis == sm.KDiag.PROJECTION_BARRED
    assert entered == []
    assert _identity_slot()


@pytest.mark.sig_case('K-03')
def test_k03_raising_projection_restores_in_finally():
    class Fault(Exception):
        pass

    def raising(validated_edge: str) -> str | None:
        raise Fault('injected')

    events = _events(sc.ROWS['PC-01'].ledger)
    with pytest.raises(Fault):
        sm.project_events(events, raising)
    assert _identity_slot()
    clean = sm.project_events(events, sm.event_edge_for_validated_edge)
    assert clean.calls == 1 and _identity_slot()


@pytest.mark.sig_case('K-04')
@pytest.mark.parametrize('fn, written', [(sm.m3a_projection, None),
                                         (sm.m3b_projection, ov.EDGE_CREATE)],
                         ids=['M3a', 'M3b'])
def test_k04_counter_and_must_not_change(fn, written):
    raw = _events(sc.ROWS['PC-09'].ledger)
    result = sm.project_events(raw, fn)
    assert result.raw_qualified == 3 == result.calls
    assert len(result.events) == len(raw) == 16
    for before, after in zip(raw, result.events, strict=True):
        assert after.site == before.site and after.outcome == before.outcome
        assert after.exception is before.exception
        if before.site == _Q:
            assert after.edge == written and after is not before
        else:
            assert after is before and after.edge is None               # never an added edge
    assert [e.edge for e in raw if e.site == _Q] == [ov.EDGE_CREATE, ov.EDGE_TARGET,
                                                     ov.EDGE_TARGET]    # raw not mutated
    assert _identity_slot()


@pytest.mark.sig_case('K-05')
def test_k05_identity_projection_on_a_real_c01_run(resolution, schema):
    bound = sc.bind_control('C01')
    record = sc.run(resolution, bound, schema)
    assert record.abort is None and record.accepted is True
    raw_qualified = sum(1 for e in record.events if e.site == _Q)
    result = sm.project_events(record.events, sm.event_edge_for_validated_edge)
    assert result.calls == raw_qualified == result.raw_qualified
    assert sc.ledger_of(record) == tuple((e.site, e.edge, e.outcome) for e in result.events)
    assert resolution.module._require is resolution.original_require
    assert _identity_slot()


def _raised_e1(notes: tuple[str, ...]) -> MigrationUnitRejected:
    try:
        try:
            raise ValueError('inner context')
        except ValueError:
            error = MigrationUnitRejected(reason_code='INSTALL_SIGNATURE_MISMATCH')
            for note in notes:
                error.add_note(note)
            raise error from None
    except MigrationUnitRejected as caught:
        return caught


@pytest.mark.sig_case('K-06')
@pytest.mark.parametrize('notes', [(), ('a note',)], ids=['no-notes', 'notes'])
def test_k06_l1_substitute_fidelity(notes):
    e1 = _raised_e1(notes)
    s = sm.build_l1_substitute(e1)
    assert sm.substitute_infidelities(e1, s) == []
    assert s is not e1 and type(s) is type(e1)
    assert s.args == e1.args and s.__dict__ == e1.__dict__
    assert s.__cause__ is e1.__cause__ and s.__context__ is e1.__context__
    assert s.__suppress_context__ is e1.__suppress_context__ is True
    assert s.__traceback__ is e1.__traceback__
    assert hasattr(s, '__notes__') is bool(notes)
    # the checker itself can fail: a substitute with a different traceback is caught
    other = sm.build_l1_substitute(e1)
    other.__traceback__ = None
    assert sm.substitute_infidelities(e1, other) == ['__traceback__']
    assert sm.substitute_infidelities(e1, e1) == ['substitute IS E1']


@pytest.mark.sig_case('K-07')
def test_k07_l2_layer_rebinds_in_inner_finally():
    original = object()
    marker = object()
    slots: dict[str, Any] = {'inv': marker}
    ctx = sm.MutantContext(sm.L2, 'PE-01')
    seen_during: list[Any] = []
    e1 = MigrationUnitRejected(reason_code='X')

    def entry(*args: Any, **kwargs: Any) -> Any:
        seen_during.append(slots['inv'])
        raise e1

    layer = sm.make_l2_layer(ctx, slots, 'inv', original)
    with pytest.raises(MigrationUnitRejected) as caught:
        layer(entry)(b'x', schema_key='k')
    assert caught.value is e1                                          # unchanged
    assert seen_during == [original]                                   # unbound in the window
    assert slots['inv'] is marker                                      # back, on the raise path
    assert (ctx.facts['l2_rebind_out'], ctx.facts['l2_rebind_back']) == (1, 1)
    assert ctx.facts['l2_back_in_inner_finally'] is True
    slots['inv'] = original
    with pytest.raises(sm.KillRunAbort) as refused:
        layer(entry)(b'x', schema_key='k')
    assert refused.value.diagnosis == sm.KDiag.L2_MARKER_NOT_INSTALLED


@pytest.mark.sig_case('K-08')
def test_k08_m5_layer():
    ctx = sm.MutantContext(sm.M5, 'PC-01')
    e1 = MigrationUnitRejected(reason_code='INSTALL_SIGNATURE_MISMATCH')

    def raising(*args: Any, **kwargs: Any) -> Any:
        raise e1

    wrapped = sm.make_m5_layer(ctx)(raising)
    assert wrapped.__qualname__ == sm.M5_QUALNAME
    with pytest.raises(MigrationUnitRejected) as caught:
        wrapped(b'x', schema_key='k')
    e2 = caught.value
    assert e2 is not e1 and type(e2) is type(e1) and e2.reason_code == e1.reason_code
    assert e2.__cause__ is None and e2.__suppress_context__ is True
    assert ctx.held['m5_e1'] is e1 and ctx.held['m5_e2'] is e2
    record = ov.RunRecord(events=[ov.Event(ov.SIG_TARGET_MEMBERSHIP, None, 'fail', e1)],
                          public=e2)
    assert all(sm.m5_correlation(ctx, record).values())
    ctx.release()
    assert ctx.held == {}
    untouched = sm.MutantContext(sm.M5, 'PC-01')
    assert sm.make_m5_layer(untouched)(lambda *a, **k: 'ok')(b'x', schema_key='k') == 'ok'
    assert untouched.facts['m5_substitutions'] == 0


class _Resolution:
    """Minimal stand-in: the two attributes BindingProbe and the L2 layer read."""

    def __init__(self, module: types.ModuleType, originals: dict[str, Any]) -> None:
        self.module = module
        self.originals = originals


@pytest.mark.sig_case('K-09')
def test_k09_probe_outermost():
    module = types.ModuleType('k09')
    marker, original = object(), object()
    module.__dict__.update({sc.I1_NAME: object(), sc.I4_NAME: marker})
    res: Any = _Resolution(module, {sc.I4_NAME: original})
    order: list[str] = []
    probe = sc.BindingProbe(res)

    def entry(*args: Any, **kwargs: Any) -> Any:
        order.append('entry')
        return 'ok'

    captured: dict[str, Any] = {}

    def fake_original(resolution: Any, bound: Any, schema: str, *, markers: Any = (),
                      witness: bool = False,
                      public_entry: Callable[[Callable[..., Any]], Callable[..., Any]]
                      | None = None) -> Any:
        assert public_entry is not None
        captured['fn'] = public_entry(entry)
        captured['result'] = captured['fn'](b'x', schema_key='k')
        return ov.RunRecord()

    bound = sc.BoundInput('A-wrong-grant-signature', b'x', 'a', 'b', 'c')
    ctx = sm.MutantContext(sm.L2, 'PE-01')
    sm.compose_run(sm.L2, ctx, fake_original)(res, bound, 'k', public_entry=probe)
    # the probe ran FIRST and saw the MARKER (install-time binding); L2 then unbound it
    assert probe.calls == 1 and probe.seen[sc.I4_NAME] is marker
    assert module.__dict__[sc.I4_NAME] is marker
    assert (ctx.facts['l2_rebind_out'], ctx.facts['l2_rebind_back']) == (1, 1)
    ctx5 = sm.MutantContext(sm.M5, 'PE-01')
    probe5 = sc.BindingProbe(res)
    sm.compose_run(sm.M5, ctx5, fake_original)(res, bound, 'k', public_entry=probe5)
    assert probe5.calls == 1 and captured['fn'].__qualname__ == \
        'binding_probe[validate_function_installation]'
    with pytest.raises(sm.KillRunAbort) as refused:
        sm.compose_run(sm.L2, sm.MutantContext(sm.L2, 'PE-01'), fake_original)(
            res, bound, 'k', public_entry=lambda e: e)
    assert refused.value.diagnosis == sm.KDiag.PROBE_ABSENT


@pytest.mark.sig_case('K-10')
def test_k10_activation_install_restore_and_refusals():
    assert sc.run is sm.ORIGINAL_RUN
    ctx = sm.MutantContext(sm.M5, 'PC-01')
    with sm.activated(sm.M5, ctx):
        assert sc.run is not sm.ORIGINAL_RUN
        assert sc.run.__qualname__ == 'killrun_run[M5]'
        with pytest.raises(sm.KillRunAbort) as stacked, \
                sm.activated(sm.M5, sm.MutantContext(sm.M5, 'PC-01')):
            pass
        assert stacked.value.diagnosis == sm.KDiag.RUN_SLOT_STACKING
    assert sc.run is sm.ORIGINAL_RUN and ctx.facts['run_slot_restored'] is True
    for mutant, case_id, diagnosis in ((sm.L1, 'PC-09', sm.KDiag.OUT_OF_SCOPE),
                                       (sm.M3A, 'PD-02', sm.KDiag.OUT_OF_SCOPE),
                                       (sm.M4, 'PC-01', sm.KDiag.MUTANT_UNKNOWN),
                                       ('M9', 'PC-01', sm.KDiag.MUTANT_UNKNOWN)):
        with pytest.raises(sm.KillRunAbort) as refused, \
                sm.activated(mutant, sm.MutantContext(mutant, case_id)):
            pass
        assert refused.value.diagnosis == diagnosis
        assert sc.run is sm.ORIGINAL_RUN
    calls: list[str] = []

    def fake_original(*args: Any, **kwargs: Any) -> Any:
        calls.append('run')
        return ov.RunRecord()

    once = sm.MutantContext(sm.M1, 'PC-01')
    run = sm.compose_run(sm.M1, once, fake_original)
    bound = sc.BoundInput('s', b'x', 'a', 'b', 'c')
    run(None, bound, 'k')
    run(None, bound, 'k')
    assert (once.primary_runs, once.passthrough_runs, len(calls)) == (1, 1, 2)


@pytest.mark.sig_case('K-11')
def test_k11_m1_swap():
    swapped = {sid: sm.m1_phase_for_site(sid) for sid in ov.SITE_IDS}
    for sid, phase in ov.PHASE_BY_SITE.items():
        want = {'O08': 'O09c', 'O09c': 'O08'}.get(phase, phase)
        assert swapped[sid] == want and swapped[sid] != ov.UNKNOWN
    with ov.PHASE_SLOT.replaced(sm.m1_phase_for_site):
        assert ov.phase_for_site(ov.SIG_QUALIFIED) == 'O09c'
        assert ov.phase_for_site(ov.SIG_TARGET_MEMBERSHIP) == 'O11'
        with pytest.raises(RuntimeError), ov.PHASE_SLOT.replaced(sm.m1_phase_for_site):
            pass
    assert ov.PHASE_SLOT.current is ov.PHASE_SLOT.baseline


@pytest.mark.sig_case('K-12')
def test_k12_m2_collapse(resolution):
    ctx = sm.MutantContext(sm.M2, 'PC-06')
    projection = sm.make_m2_projection(ctx)
    compare = resolution.sites[_CC]
    identity = resolution.sites[_CI]
    assert projection(resolution, compare.enclosing, compare.lineno) == (_CI,)
    assert projection(resolution, identity.enclosing, identity.lineno) == (_CI,)
    for sid in ov.SITE_IDS:
        if sid in (_CC, _CI):
            continue
        site = resolution.sites[sid]
        assert projection(resolution, site.enclosing, site.lineno) == (sid,)
    assert projection(resolution, '<nowhere>', -1) == ()
    assert ctx.facts['m2_rewrites'] == 1
    assert ctx.facts['m2_chosen'] == ov.SIG_CREATE_IDENTITY
    assert ov.LIVE_SEAMS.all_original()

