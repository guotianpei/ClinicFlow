"""PHASE-SIGNATURE-01 Gate 3 KILL-RUN MUTANT LAYERS (test overlay). NOT production code.

Stage 3 of G-C1. Frozen design (owner freeze OD-SIG-24): mutant definitions v6
(f11602dd...), kill-run v7 (1f227404...), E0 O11 mutant definitions v2 (29bf2d86...),
Part E packet v2 (178ce928...); owner ruling OD-SIG-26 (59165dde...). Codex G-C2 rulings
on Stage 2 carried: F1/F3 (compose at ``sc.run``, ``BindingProbe`` OUTERMOST), F2 (the
M3a/M3b edge rewrite is a harness-level projection applied after the event is appended and
before anything reads it, with a proven call counter, valid-edge precheck, must-not-change
table, restoration in ``finally`` and no stacking), F4 (separate binding stage -- owned by
``sig_killrun``).

WHAT THIS MODULE IS. The seven IN-PROCESS mutant layers and the ONE composition point
through which they are applied. It is imported only by the kill-run child plugin
(``sig_killrun_plugin``) and by the harness self-proofs (``test_sig_killrun_harness``). It
has NO import-time side effect: importing it installs nothing.

WHAT IT MUTATES. The Gate 3 INSTRUMENT only -- never ``function_policy.py`` (the subject),
never a fixture, never a frozen CP1 test. It adds NO seam to an accepted artifact:

  M1        ``ov.PHASE_SLOT``                    existing slot (A-28), Stage 1
  M2        ``ov.LIVE_SEAMS['live_site_projection']``  existing closed seam, Stage 1
  M3a/M3b   ``event_edge_for_validated_edge``    kill-run HARNESS projection (v6 section 6),
                                                 defined HERE, post-append
  M5        a named ``public_entry`` layer       ``ov.run_once``'s public-entry parameter
  M-O11-L1  ``ov.WITNESS_SLOT`` (I2)             existing slot, Part E
  M-O11-L2  a named ``public_entry`` layer       rebinding ONE module-global slot

M4 is NOT here: it is delivered to the B-24 child by launch environment only (Stage 1,
``sig_overlay.post_resolution_hook_from_env``). The kill-run runs it by running B-24.

Every mutant is applied to the case's FIRST ``sc.run`` call only -- the case's subject run.
Any later ``sc.run`` call in the same case (the EA7/EC7 no-stack proofs) routes to the
ORIGINAL ``sc.run`` unmutated and is counted; ``sig_killrun`` binds that count EXACTLY (2 on
a passing PE-01, 0 wherever the accepted first failure precedes EA7, 0 on rows and PD-01).
Each Part E wrapper keeps its own independent proof (Codex G-C2 Q5, ACCEPTED on that basis).

M1 IS THE ONE EXCEPTION TO "subject run only", stated so it is not misread: its
``PHASE_SLOT`` replacement is active around the WHOLE case body, because A7 reads the phase
after the run returns. Later ``sc.run`` calls still pass through, and ``PHASE_SLOT`` is not
one of the Part E wrapper slots (I1, I2, I4), so M1 can neither satisfy nor stand in for
any EA7/EC7 wrapper proof.

Nothing here is a kill: a kill is a named frozen assertion failing first, and only
``sig_killrun`` compares that against the accepted matrices.
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import sig_cases as sc
import sig_overlay as ov

from haloflow.m01.errors import MigrationUnitRejected

# ------------------------------------------------------------------ names

NONE = 'NONE'              # baseline stages: the plugin installs nothing
M4 = 'M4'
M1 = 'M1'
M2 = 'M2'
M3A = 'M3a'
M3B = 'M3b'
M5 = 'M5'
L1 = 'M-O11-L1'
L2 = 'M-O11-L2'

# Kill-run order. M4 FIRST (kill-run v7 section 2; work plan v3 section 4 step 7).
MUTANT_ORDER: tuple[str, ...] = (M4, M1, M2, M3A, M3B, M5, L1, L2)
IN_PROCESS: frozenset[str] = frozenset({M1, M2, M3A, M3B, M5, L1, L2})

_ROWS: frozenset[str] = frozenset(sc.ROWS)

# The ONLY cases each in-process mutant may be applied to. E0 v2 section 3 term 1: the two
# O11 mutants are applied to the Part E PC-09 witness run ONLY. Part E v2 section 6.2: M1,
# M2, M3a, M3b and M5 are also applied to PE-01. Kill-run v7 section 3: M3a/M3b to PD-01.
MUTANT_SCOPE: Mapping[str, frozenset[str]] = types.MappingProxyType({
    M1: _ROWS | {'PE-01'},
    M2: _ROWS | {'PE-01'},
    M3A: _ROWS | {'PD-01', 'PE-01'},
    M3B: _ROWS | {'PD-01', 'PE-01'},
    M5: _ROWS | {'PE-01'},
    L1: frozenset({'PE-01'}),
    L2: frozenset({'PE-01'}),
})


class KillRunAbort(Exception):
    """The HARNESS could not apply a mutant as defined. Fail closed with a named
    diagnosis. Deliberately NOT an ``AssertionError`` and NOT one of the families the subject
    catches, so it can never be read as a frozen assertion failing, and never as a kill."""

    def __init__(self, diagnosis: str, **details: Any) -> None:
        super().__init__(f'{diagnosis} {details}')
        self.diagnosis = diagnosis
        self.details = details


class KDiag:
    """Harness diagnoses. Asserted by EQUALITY only, never by substring."""

    MUTANT_UNKNOWN = 'SIG.KILLRUN.MUTANT_UNKNOWN'
    OUT_OF_SCOPE = 'SIG.KILLRUN.MUTANT_OUT_OF_SCOPE'
    RUN_SLOT_STACKING = 'SIG.KILLRUN.RUN_SLOT_STACKING'
    PROJECTION_BARRED = 'SIG.KILLRUN.PROJECTION_BARRED'
    PROJECTION_STACKING = 'SIG.KILLRUN.PROJECTION_STACKING'
    PROJECTION_OUTPUT_INVALID = 'SIG.KILLRUN.PROJECTION_OUTPUT_INVALID'
    PROJECTION_COUNT_MISMATCH = 'SIG.KILLRUN.PROJECTION_COUNT_MISMATCH'
    MUST_NOT_CHANGE_VIOLATED = 'SIG.KILLRUN.MUST_NOT_CHANGE_VIOLATED'
    WITNESS_INSTALL_REFUSED = 'SIG.KILLRUN.WITNESS_INSTALL_REFUSED'
    SUBSTITUTE_INFIDELITY = 'SIG.KILLRUN.SUBSTITUTE_INFIDELITY'
    PROBE_ABSENT = 'SIG.KILLRUN.BINDING_PROBE_ABSENT'
    L2_MARKER_NOT_INSTALLED = 'SIG.KILLRUN.L2_MARKER_NOT_INSTALLED'


# ------------------------------------------------------------ per-case context


@dataclass
class MutantContext:
    """One case's mutant bookkeeping. ``facts`` holds plain values only (counts, booleans,
    names) and is what the plugin saves. ``held`` holds the few STRONG references a
    correlation needs during the case (M5's E1/E2) and is emptied by ``release()`` when the
    case body ends -- never an ``id()``."""

    mutant: str
    case_id: str
    primary_runs: int = 0
    passthrough_runs: int = 0
    facts: dict[str, Any] = field(default_factory=dict)
    held: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {'mutant': self.mutant, 'case_id': self.case_id,
                'primary_runs': self.primary_runs, 'passthrough_runs': self.passthrough_runs,
                **self.facts}

    def release(self) -> None:
        self.held.clear()


# ======================================================================== M1


_M1_SWAP: Mapping[str, str] = types.MappingProxyType({'O08': 'O09c', 'O09c': 'O08'})


def m1_phase_for_site(site_id: str) -> str:
    """M1 (v6 section 8; kill-run v7 section 5): swap O08 <-> O09c ONLY. Pure; a lookup in
    the immutable ``PHASE_BY_SITE`` then in an immutable swap map. O11 is untouched, so PC-09
    stays out of the kill set. Never returns UNKNOWN: a missing site raises ``KeyError``."""
    baseline = ov.PHASE_BY_SITE[site_id]
    return _M1_SWAP.get(baseline, baseline)


# ======================================================================== M2

# Kill-run v7 section 6: CHOSEN DIRECTION = SIG.CREATE.IDENTITY. Recorded in every M2 output.
M2_CHOSEN = ov.SIG_CREATE_IDENTITY
M2_COLLAPSED = ov.SIG_CREATE_COMPARE


def make_m2_projection(ctx: MutantContext) -> Callable[[ov.Resolution, str, int], tuple[str, ...]]:
    """M2: downstream of a valid bijective resolver. The resolver (S1..S14) is untouched and
    has already passed; this replaces only the observer's LIVE site projection, which reads
    the already-resolved ``line_to_site``. A COMPARE id is reported as IDENTITY; every other
    id, and an empty result, passes through unchanged."""
    baseline = ov.LIVE_SEAMS.original('live_site_projection')
    ctx.facts['m2_chosen'] = M2_CHOSEN
    ctx.facts['m2_collapsed'] = M2_COLLAPSED
    ctx.facts['m2_rewrites'] = 0

    def m2_collapsed_site_projection(resolution: ov.Resolution, code_name: str,
                                     lineno: int) -> tuple[str, ...]:
        ids = tuple(baseline(resolution, code_name, lineno))
        out = tuple(M2_CHOSEN if sid == M2_COLLAPSED else sid for sid in ids)
        if out != ids:
            ctx.facts['m2_rewrites'] += 1
        return out

    return m2_collapsed_site_projection


# ================================================================ M3a / M3b

# v6 section 5 / 6: the named projection, KILL-RUN HARNESS ONLY, applied post-append.


def event_edge_for_validated_edge(validated_edge: str) -> str | None:
    """BASELINE projection: identity."""
    return validated_edge


def m3a_projection(validated_edge: str) -> str | None:
    """M3a v3: drop the recorded edge. Returns ``None`` -- NEVER a sentinel."""
    return None


def m3b_projection(validated_edge: str) -> str | None:
    """M3b v3: hardcode a VALID edge."""
    return ov.EDGE_CREATE


class EdgeProjectionSlot:
    """Named, separately replaceable projection object (OD-SIG-16 v3 shape): install over
    identity accepted; install over a live projection REFUSED; a refused install leaves the
    slot unchanged; restored to identity in ``finally`` after every projection, including a
    raising one."""

    identity: Callable[[str], str | None] = staticmethod(event_edge_for_validated_edge)

    def __init__(self) -> None:
        self.current: Callable[[str], str | None] = event_edge_for_validated_edge

    def install(self, fn: Callable[[str], str | None]) -> bool:
        if self.current is not event_edge_for_validated_edge:
            return False
        self.current = fn
        return True

    def restore(self) -> None:
        self.current = event_edge_for_validated_edge


EDGE_PROJECTION_SLOT = EdgeProjectionSlot()


def raw_event_violations(events: Sequence[ov.Event]) -> list[str]:
    """The valid-edge PRECHECK (v6 section 5 item 7; F2). Non-circular: read from the RAW
    observer events BEFORE any projection. Every ``SIG.QUALIFIED`` event must carry exactly
    one valid resolved edge; every other site must carry ``edge=None``; every site must be
    one of the eight IDs; every outcome ``pass`` or ``fail``."""
    violations: list[str] = []
    for i, e in enumerate(events):
        if e.site not in ov.SITE_IDS:
            violations.append(f'event[{i}] unresolved site {e.site!r}')
        elif e.site == ov.SIG_QUALIFIED:
            if e.edge not in ov.EDGE_IDS:
                violations.append(f'event[{i}] SIG.QUALIFIED edge {e.edge!r} is not a valid edge')
        elif e.edge is not None:
            violations.append(f'event[{i}] site {e.site} must carry edge=None, has {e.edge!r}')
        if e.outcome not in ('pass', 'fail'):
            violations.append(f'event[{i}] outcome {e.outcome!r}')
    return violations


def must_not_change_violations(before: Sequence[ov.Event],
                               after: Sequence[ov.Event]) -> list[str]:
    """The v6 section 5 must-not-change table, checked per event: count; semantic site;
    outcome; exception IDENTITY; and every non-``SIG.QUALIFIED`` event is the SAME object
    (it bypassed the projection entirely, so its ``edge=None`` cannot have been changed --
    M3b never adds an edge to an edgeless event)."""
    violations: list[str] = []
    if len(before) != len(after):
        return [f'event count {len(before)} -> {len(after)}']
    for i, (b, a) in enumerate(zip(before, after, strict=True)):
        if a.site != b.site:
            violations.append(f'event[{i}] site {b.site} -> {a.site}')
        if a.outcome != b.outcome:
            violations.append(f'event[{i}] outcome {b.outcome} -> {a.outcome}')
        if a.exception is not b.exception:
            violations.append(f'event[{i}] exception object changed')
        if b.site != ov.SIG_QUALIFIED and a is not b:
            violations.append(f'event[{i}] edgeless event was not bypassed')
    return violations


@dataclass(frozen=True)
class ProjectionResult:
    events: list[ov.Event]
    calls: int                  # projection-call counter for this run
    raw_qualified: int          # total raw SIG.QUALIFIED count, counted BEFORE projection


def project_events(events: Sequence[ov.Event],
                   fn: Callable[[str], str | None]) -> ProjectionResult:
    """Apply ``fn`` through the named slot to a COPY of the recorded events.

    Order, each step fail-closed: (1) PRECHECK -- malformed raw input is BARRED: nothing is
    installed and ``fn`` is never entered; (2) install, refusing a stacked install; (3)
    project, restoring the slot in ``finally``; (4) every output is ``None`` or a valid edge
    -- never a sentinel; (5) call counter == raw ``SIG.QUALIFIED`` count; (6) must-not-change.
    The observer's own ``Event`` objects are never mutated: a projected event is a new frozen
    ``Event`` (``dataclasses.replace``) carrying the same exception object."""
    violations = raw_event_violations(events)
    if violations:
        raise KillRunAbort(KDiag.PROJECTION_BARRED, violations=violations)
    raw_qualified = sum(1 for e in events if e.site == ov.SIG_QUALIFIED)
    if not EDGE_PROJECTION_SLOT.install(fn):
        raise KillRunAbort(KDiag.PROJECTION_STACKING)
    calls = 0
    projected: list[ov.Event] = []
    try:
        for e in events:
            if e.site == ov.SIG_QUALIFIED:
                calls += 1
                projected.append(dataclasses.replace(e, edge=EDGE_PROJECTION_SLOT.current(
                    str(e.edge))))
            else:
                projected.append(e)                      # bypass entirely: same object
    finally:
        EDGE_PROJECTION_SLOT.restore()
    bad_output = [(i, e.edge) for i, e in enumerate(projected)
                  if e.site == ov.SIG_QUALIFIED
                  and not (e.edge is None or e.edge in ov.EDGE_IDS)]
    if bad_output:
        raise KillRunAbort(KDiag.PROJECTION_OUTPUT_INVALID, outputs=bad_output)
    if calls != raw_qualified:
        raise KillRunAbort(KDiag.PROJECTION_COUNT_MISMATCH, calls=calls,
                           raw_qualified=raw_qualified)
    changed = must_not_change_violations(events, projected)
    if changed:
        raise KillRunAbort(KDiag.MUST_NOT_CHANGE_VIOLATED, violations=changed)
    return ProjectionResult(projected, calls, raw_qualified)


# ======================================================================== M5


M5_QUALNAME = 'm5_outer_substitution[validate_function_installation]'


def make_m5_layer(ctx: MutantContext) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """M5 (v6 section 12; kill-run v7 section 9; Part E v2 section 6.1): a NAMED OUTER wrapper
    on the PUBLIC entry. It catches E1 AFTER the observer has recorded and re-raised it -- and,
    in Part E, after I1 has recorded it -- then raises E2: same class, same ``reason_code``,
    constructed directly. It replaces neither the observer nor ``_fail``. On a normal return
    it does nothing."""
    ctx.facts['m5_substitutions'] = 0

    def m5_layer(entry: Callable[..., Any]) -> Callable[..., Any]:
        def m5_outer_substitution(*args: Any, **kwargs: Any) -> Any:
            try:
                return entry(*args, **kwargs)
            except MigrationUnitRejected as e1:
                e2 = type(e1)(reason_code=e1.reason_code)
                ctx.facts['m5_substitutions'] += 1
                ctx.held['m5_e1'] = e1
                ctx.held['m5_e2'] = e2
                raise e2 from None
        m5_outer_substitution.__qualname__ = M5_QUALNAME
        return m5_outer_substitution

    return m5_layer


def m5_correlation(ctx: MutantContext, record: ov.RunRecord) -> dict[str, Any]:
    """Saved output must correlate the recorded E1 with the public E2 FROM THE SAME RUN
    (kill-run v7 section 9). Every comparison is ``is``; nothing stores an ``id()``."""
    e1 = ctx.held.get('m5_e1')
    e2 = ctx.held.get('m5_e2')
    fails = [e for e in record.events if e.outcome == 'fail']
    return {
        'm5_e1_is_observer_recorded': (e1 is not None and len(fails) == 1
                                       and fails[0].exception is e1),
        'm5_public_is_e2': e2 is not None and record.public is e2,
        'm5_e2_is_not_e1': e1 is not None and e2 is not None and e2 is not e1,
        'm5_same_class': e1 is not None and type(e2) is type(e1),
        'm5_same_reason_code': (e1 is not None and e2 is not None
                                and getattr(e2, 'reason_code', None)
                                == getattr(e1, 'reason_code', None)),
    }


# ================================================================== M-O11-L1


def build_l1_substitute(e1: BaseException) -> BaseException:
    """E0 v2 section 4: the substitute S, constructed DIRECTLY -- never via ``_fail`` or
    ``_require``, so no observer event is produced, and without calling the class
    ``__init__``. Every enumerated value is COPIED FROM E1 at the moment of substitution,
    never hard-coded: exact type; ``args``; a copy of the full instance ``__dict__`` (which is
    where ``__notes__`` lives when present, so its presence and value follow E1's);
    ``__cause__``; ``__context__``; ``__suppress_context__`` (set LAST, because assigning
    ``__cause__`` sets it); the exact ``__traceback__`` object."""
    cls = type(e1)
    s = cls.__new__(cls, *e1.args)
    s.args = e1.args
    s.__dict__.update(dict(e1.__dict__))
    s.__cause__ = e1.__cause__
    s.__context__ = e1.__context__
    s.__suppress_context__ = e1.__suppress_context__
    s.__traceback__ = e1.__traceback__
    return s


def substitute_infidelities(e1: BaseException, s: BaseException) -> list[str]:
    """The E0 v2 section 4 table, checked. Identity is the only discriminator ACROSS THESE
    ENUMERATED OBSERVATIONS; ``id()`` and the default hash necessarily differ and are not
    compared."""
    bad: list[str] = []
    if s is e1:
        bad.append('substitute IS E1')
    if type(s) is not type(e1):
        bad.append('type')
    if s.args != e1.args:
        bad.append('args')
    if s.__dict__ != e1.__dict__:
        bad.append('__dict__')
    if s.__cause__ is not e1.__cause__:
        bad.append('__cause__')
    if s.__context__ is not e1.__context__:
        bad.append('__context__')
    if s.__suppress_context__ != e1.__suppress_context__:
        bad.append('__suppress_context__')
    if hasattr(s, '__notes__') != hasattr(e1, '__notes__'):
        bad.append('__notes__ presence')
    elif hasattr(e1, '__notes__') and s.__notes__ != e1.__notes__:
        bad.append('__notes__ value')
    if s.__traceback__ is not e1.__traceback__:
        bad.append('__traceback__')
    return bad


def make_l1_projection(ctx: MutantContext) -> Callable[[BaseException], BaseException]:
    """M-O11-L1: the I2 projection ``o11_witness_record`` returns the substitute instead of
    its argument. The witness still re-raises the ORIGINAL E1 (Stage 1 ``_witness``), so only
    the witness's RECORD changes. A substitute that fails the fidelity table is not a
    mutant as defined: it fails closed (a wrong death, never a kill)."""
    ctx.facts['l1_substitutions'] = 0

    def o11_witness_record_substituting(propagating: BaseException) -> BaseException:
        s = build_l1_substitute(propagating)
        bad = substitute_infidelities(propagating, s)
        if bad:
            raise KillRunAbort(KDiag.SUBSTITUTE_INFIDELITY, fields=bad)
        ctx.facts['l1_substitutions'] += 1
        return s

    return o11_witness_record_substituting


# ================================================================== M-O11-L2

L2_QUALNAME = 'm_o11_l2_unbind_inventory[validate_function_installation]'


def make_l2_layer(ctx: MutantContext, module_globals: dict[str, Any], name: str,
                  original: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """M-O11-L2 (E0 v2 section 5): around EXACTLY the one public call, in a DEDICATED inner
    ``try``/``finally``: (1) immediately before the call, the slot ``name`` is rebound to the
    ORIGINAL object; (2) the call runs; (3) the inner ``finally`` rebinds the slot back to the
    object that was there -- the installed marker -- so E1 cannot skip it; no ``except``, so
    E1 propagates UNCHANGED; (4) only then does any outer cleanup or post-run check run.
    ``module_globals`` is the observed module's own globals under form 1."""
    ctx.facts['l2_rebind_out'] = 0
    ctx.facts['l2_rebind_back'] = 0
    ctx.facts['l2_back_in_inner_finally'] = False

    def l2_layer(entry: Callable[..., Any]) -> Callable[..., Any]:
        def m_o11_l2_unbind_inventory(*args: Any, **kwargs: Any) -> Any:
            marker = module_globals[name]
            if marker is original:
                raise KillRunAbort(KDiag.L2_MARKER_NOT_INSTALLED, slot=name)
            module_globals[name] = original
            ctx.facts['l2_rebind_out'] += 1
            try:
                return entry(*args, **kwargs)
            finally:
                module_globals[name] = marker
                ctx.facts['l2_rebind_back'] += 1
                ctx.facts['l2_back_in_inner_finally'] = True
        m_o11_l2_unbind_inventory.__qualname__ = L2_QUALNAME
        return m_o11_l2_unbind_inventory

    return l2_layer


# ============================================================ composition


RunFn = Callable[..., ov.RunRecord]

# The ONE reviewed composition point (Stage 2 D32). Captured at import, when nothing is
# installed; every activation checks ``sc.run`` is still THIS object before installing.
ORIGINAL_RUN: RunFn = sc.run


def _outermost_probe(public_entry: Any) -> sc.BindingProbe:
    """F1 / D29: when a public-entry mutant is composed on a Part E case, the Part E
    ``BindingProbe`` must be the OUTERMOST layer. Anything else is refused."""
    if not isinstance(public_entry, sc.BindingProbe):
        raise KillRunAbort(KDiag.PROBE_ABSENT, public_entry=type(public_entry).__name__)
    return public_entry


def compose_run(mutant: str, ctx: MutantContext, original: RunFn = ORIGINAL_RUN) -> RunFn:
    """Return the ``sc.run`` replacement for one case. The case's FIRST call is its subject
    run and carries the mutant; any later call passes through to ``original`` unchanged and
    is counted (``passthrough_runs``)."""
    if mutant not in IN_PROCESS:
        raise KillRunAbort(KDiag.MUTANT_UNKNOWN, mutant=mutant)

    def killrun_run(resolution: ov.Resolution, bound: sc.BoundInput, schema: str, *,
                    markers: tuple[str, ...] = (), witness: bool = False,
                    public_entry: Callable[[Callable[..., Any]], Callable[..., Any]]
                    | None = None) -> ov.RunRecord:
        if ctx.primary_runs >= 1:
            ctx.passthrough_runs += 1
            return original(resolution, bound, schema, markers=markers, witness=witness,
                            public_entry=public_entry)
        ctx.primary_runs += 1
        ctx.facts['subject'] = bound.subject

        if mutant == M1:
            # M1 acts at the oracle boundary (``phase_for_site``), installed by the plugin
            # around the case body; the subject run itself is unchanged.
            return original(resolution, bound, schema, markers=markers, witness=witness,
                            public_entry=public_entry)

        if mutant == M2:
            projection = make_m2_projection(ctx)
            with ov.LIVE_SEAMS.replaced('live_site_projection', projection):
                record = original(resolution, bound, schema, markers=markers,
                                  witness=witness, public_entry=public_entry)
            ctx.facts['m2_seam_restored'] = (
                ov.LIVE_SEAMS.get('live_site_projection')
                is ov.LIVE_SEAMS.original('live_site_projection'))
            return record

        if mutant in (M3A, M3B):
            record = original(resolution, bound, schema, markers=markers, witness=witness,
                              public_entry=public_entry)
            # POST-APPEND: the run has returned, so the observer has appended every event;
            # nothing has read the list yet. An aborted run is never projected -- the case
            # fails closed on it (OBSERVATION_ABORTED), a wrong death.
            if record.abort is not None:
                ctx.facts['m3_projected'] = False
                return record
            fn = m3a_projection if mutant == M3A else m3b_projection
            result = project_events(record.events, fn)
            record.events = result.events
            ctx.facts['m3_projected'] = True
            ctx.facts['m3_projection_calls'] = result.calls
            ctx.facts['m3_raw_qualified'] = result.raw_qualified
            ctx.facts['m3_slot_restored'] = (
                EDGE_PROJECTION_SLOT.current is EDGE_PROJECTION_SLOT.identity)
            return record

        if mutant == M5:
            m5 = make_m5_layer(ctx)
            if public_entry is None:
                composed: Callable[[Callable[..., Any]], Callable[..., Any]] = m5
            else:
                probe = _outermost_probe(public_entry)

                def composed(entry: Callable[..., Any]) -> Callable[..., Any]:
                    return probe(m5(entry))              # probe OUTERMOST, M5 inside it
            record = original(resolution, bound, schema, markers=markers, witness=witness,
                              public_entry=composed)
            ctx.facts.update(m5_correlation(ctx, record))
            return record

        if mutant == L1:
            if not ov.WITNESS_SLOT.install(make_l1_projection(ctx)):
                raise KillRunAbort(KDiag.WITNESS_INSTALL_REFUSED)
            try:
                record = original(resolution, bound, schema, markers=markers,
                                  witness=witness, public_entry=public_entry)
            finally:
                ov.WITNESS_SLOT.restore()
            ctx.facts['l1_slot_restored'] = ov.WITNESS_SLOT.current is ov.WITNESS_SLOT.identity
            return record

        # mutant == L2
        probe = _outermost_probe(public_entry)
        layer = make_l2_layer(ctx, resolution.module.__dict__, sc.I4_NAME,
                              resolution.originals[sc.I4_NAME])

        def composed_l2(entry: Callable[..., Any]) -> Callable[..., Any]:
            return probe(layer(entry))                   # probe OUTERMOST, L2 inside it
        record = original(resolution, bound, schema, markers=markers, witness=witness,
                          public_entry=composed_l2)
        # E0 v2 section 3 term 3: the dynamic prerequisites ran WHILE L2 was active and must
        # have passed unchanged -- no observation abort, the full measured ledger.
        ctx.facts['l2_observation_abort'] = (None if record.abort is None
                                             else record.abort.diagnosis)
        ctx.facts['l2_event_count'] = len(record.events)
        ctx.facts['l2_ledger_unchanged'] = (sc.ledger_of(record)
                                            == sc.ROWS['PC-09'].ledger)
        return record

    killrun_run.__qualname__ = f'killrun_run[{mutant}]'
    return killrun_run


@contextmanager
def activated(mutant: str, ctx: MutantContext) -> Iterator[None]:
    """Install ONE in-process mutant for ONE case body, and restore it in ``finally``.

    Refuses: an unknown mutant; a case outside the mutant's scope; ``sc.run`` not being the
    original (stacking). M1 additionally enters ``ov.PHASE_SLOT.replaced``, which itself
    refuses stacking. Restoration is recorded in ``ctx.facts`` for the saved output."""
    if mutant not in IN_PROCESS:
        raise KillRunAbort(KDiag.MUTANT_UNKNOWN, mutant=mutant)
    if ctx.case_id not in MUTANT_SCOPE[mutant]:
        raise KillRunAbort(KDiag.OUT_OF_SCOPE, mutant=mutant, case_id=ctx.case_id)
    if sc.run is not ORIGINAL_RUN:
        raise KillRunAbort(KDiag.RUN_SLOT_STACKING)
    sc.run = compose_run(mutant, ctx)
    try:
        if mutant == M1:
            with ov.PHASE_SLOT.replaced(m1_phase_for_site):
                ctx.facts['m1_installed'] = ov.PHASE_SLOT.current is m1_phase_for_site
                yield
            ctx.facts['m1_restored'] = ov.PHASE_SLOT.current is ov.PHASE_SLOT.baseline
        else:
            yield
    finally:
        sc.run = ORIGINAL_RUN
        ctx.facts['run_slot_restored'] = sc.run is ORIGINAL_RUN
