"""PHASE-SIGNATURE-01 Gate 3 PART E -- the O11 seam witness. PE-02, then PE-01.

Frozen design: Part E packet v2 (178ce928...); E0 O11 mutant definitions v2
(29bf2d86...); Parts C/D packet v7 (2cf9ebcf...); owner freeze OD-SIG-24.

Naming, fixed: ``E1`` is ONLY the exception object the observer recorded at PE-01's
single failing event. EA1 .. EA7 are PE-01's assertions; EC1 .. EC7 are PE-02's.

Instruments (all from the Stage 1 instrument, unchanged):
  I1  O11 witness on ``_validate_local_statements``   ov._witness  (run_once witness=True)
  I2  projection slot ``o11_witness_record``          ov.WITNESS_SLOT (identity baseline)
  I3  L2 in-run binding read of the module-global     ov._witness reads it at exit
      ``_validate_statement_inventory`` slot
  I4  ``_validate_statement_inventory`` reach marker   Part D marker, reused
  I5  observer on ``_require``                        Part B, unchanged
  I6  per-run record, strong references, never id()  ov.RunRecord + ov.WitnessRecord
Composition: I4 installed FIRST, I1 SECOND (outer); restored in reverse order.

Preconditions (fail closed with a named diagnosis, never an assertion result): S1..S14
exact prefix; X3 bound; I2 at identity before the run; after the run -- no
ObservationAbort; X2 for every instrumented global EXCEPT the I1 and I4 slots (their
restoration is EA7 / EC7 itself); X1; I1 was installed and I4 was bound at the moment of
the public call (``BindingProbe``).

CREDIT AND ORDER (work plan v3 section 4; Part E v2 section 3). PD-01 and PD-02 pass,
then PE-02, then PE-01; PE-01 also needs G-2 credit first. Until then PE-01 is a
provisional row-only measurement. The Stage 3 staged executor enforces this from
``sig_cases.BACKING_CASES`` / ``sig_cases.G2_GATED``. O11 is established from the call
chain here, never from ``phase_for_site``; PE-01 deliberately asserts no ledger, edge
or phase.
"""

from collections.abc import Callable
from typing import Any

import pytest
import sig_cases as sc
import sig_overlay as ov

from haloflow.m01.errors import MigrationUnitRejected

REASON_CODE = 'INSTALL_SIGNATURE_MISMATCH'
I4_ONLY: tuple[str, ...] = (sc.I4_NAME,)


def run_part_e(resolution: ov.Resolution, bound: sc.BoundInput,
               schema: str) -> tuple[ov.RunRecord, sc.BindingProbe]:
    """One composed run: I5 observer, I4 marker (first), I1 witness (second, outer)."""
    sc.require_i2_identity()
    probe = sc.BindingProbe(resolution)
    record = sc.run(resolution, bound, schema, markers=I4_ONLY, witness=True,
                    public_entry=probe)
    return record, probe


def restoration(resolution: ov.Resolution, bound: sc.BoundInput, schema: str,
                assertion_id: str) -> None:
    """EA7 / EC7, evaluated LAST. Two parts, each done SEPARATELY for I1, I2 and I4 --
    no wrapper's check stands in for another's.

    (a) Restoration: each wrapper's own slot holds its original object again.
    (b) No-stack PROOF, per wrapper (E0 v2 section 2; OD-SIG-16 v3 proof shape):
        I2 -- the WitnessSlot shape: install over identity accepted; a second install
              over the live projection REFUSED; the refused install leaves the first
              projection unchanged; restored in ``finally``; identity afterwards.
        I1, I4 -- the named module-global guard of ``ov.run_once``: with THAT ONE slot
              occupied, an install attempt raises ``StackingRefused`` naming that slot,
              before any subject call (a pass-through ``public_entry`` counter stays 0
              and the occupant is never called); the occupied slot is unchanged; nothing
              else was installed; the original is restored in ``finally``.
    No second subject run is made: the guard fires before the subject is reached."""
    module = resolution.module
    # (a) restoration, per wrapper
    sc.check(assertion_id, module.__dict__[sc.I1_NAME] is resolution.originals[sc.I1_NAME],
             f'I1 slot {sc.I1_NAME} not restored to its original')
    sc.check(assertion_id, ov.WITNESS_SLOT.current is ov.WITNESS_SLOT.identity,
             'I2 slot o11_witness_record not restored to identity')
    sc.check(assertion_id, module.__dict__[sc.I4_NAME] is resolution.originals[sc.I4_NAME],
             f'I4 slot {sc.I4_NAME} not restored to its original')
    # (b) no-stack proof, per wrapper
    global_no_stack_proof(resolution, bound, schema, sc.I1_NAME, 'I1', assertion_id)
    witness_slot_no_stack_proof(assertion_id)
    global_no_stack_proof(resolution, bound, schema, sc.I4_NAME, 'I4', assertion_id)


class _Occupant:
    """Occupies ONE module-global slot for a refused-install attempt. Never to be called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        raise AssertionError('the occupying object must never be called')


def global_no_stack_proof(resolution: ov.Resolution, bound: sc.BoundInput, schema: str,
                          name: str, label: str, assertion_id: str) -> None:
    """I1 / I4: the per-slot install guard, demonstrated for THIS slot alone."""
    module = resolution.module
    original = resolution.originals[name]
    others = [n for n in ov.INSTRUMENTED_GLOBALS if n != name]
    sc.check(assertion_id, all(module.__dict__[n] is resolution.originals[n] for n in others),
             f'{label}: another instrumented slot is occupied before the {name} proof')
    occupant = _Occupant()
    entry_calls = [0]

    def counting_entry(entry: Callable[..., Any]) -> Callable[..., Any]:
        """Counts only when the subject call is being built -- which the guard precedes."""
        entry_calls[0] += 1
        return entry

    refused: BaseException | None = None
    module.__dict__[name] = occupant
    try:
        try:
            sc.run(resolution, bound, schema, markers=I4_ONLY, witness=True,
                   public_entry=counting_entry)
        except ov.StackingRefused as error:
            refused = error
        still_occupied = module.__dict__[name] is occupant
        others_untouched = all(module.__dict__[n] is resolution.originals[n] for n in others)
    finally:
        module.__dict__[name] = original
    sc.check(assertion_id, refused is not None,
             f'{label}: install over an occupied {name} was NOT refused')
    sc.check(assertion_id,
             str(refused) == f'{name} is not the original before install (B-22)',
             f'{label}: the refusal did not name {name}: {refused!s}')
    sc.check(assertion_id, entry_calls[0] == 0 and occupant.calls == 0,
             f'{label}: the subject was reached (entry={entry_calls[0]}, '
             f'occupant={occupant.calls})')
    sc.check(assertion_id, still_occupied,
             f'{label}: the refused install changed the occupied {name} slot')
    sc.check(assertion_id, others_untouched,
             f'{label}: the refused install left another slot installed')
    sc.check(assertion_id, module.__dict__[name] is original,
             f'{label}: {name} not back at its original after the proof')


def witness_slot_no_stack_proof(assertion_id: str) -> None:
    """I2: the OD-SIG-16 v3 / E0 v2 section 2 proof shape on ``ov.WITNESS_SLOT``."""
    slot = ov.WITNESS_SLOT

    def first(propagating: BaseException) -> BaseException:
        return propagating

    def second(propagating: BaseException) -> BaseException:
        return propagating

    try:
        accepted = slot.install(first)
        live_after_first = slot.current is first
        refused = slot.install(second) is False
        unchanged = slot.current is first
    finally:
        slot.restore()
    sc.check(assertion_id, accepted is True and live_after_first,
             'I2: install over identity was not accepted')
    sc.check(assertion_id, refused, 'I2: install over a live projection was NOT refused')
    sc.check(assertion_id, unchanged, 'I2: the refused install changed the live projection')
    sc.check(assertion_id, slot.current is slot.identity,
             'I2: slot not back at identity after the proof')


# ------------------------------------------------------------------------ PE-02


def evaluate_pe02(resolution: ov.Resolution, record: ov.RunRecord, wrec: ov.WitnessRecord,
                  bound: sc.BoundInput, schema: str) -> None:
    """EC1 .. EC7, strictly in order."""
    sc.check('EC1', record.accepted is True and record.public is None,
             f'C01 must be ACCEPTED; public={type(record.public).__name__}')
    fails = sc.failing(record)
    sc.check('EC2', fails == [], f'failing events={len(fails)}')
    sc.check('EC3', wrec.entries == 1, f'I1 entries={wrec.entries}')
    sc.check('EC3', wrec.normal_returns == 1, f'I1 normal returns={wrec.normal_returns}')
    sc.check('EC3', wrec.exception_exits == 0, f'I1 exception exits={wrec.exception_exits}')
    sc.check('EC4', wrec.projection_calls == 0, f'I2 projection calls={wrec.projection_calls}')
    sc.check('EC5', wrec.inventory_slot_object is record.installed_markers[sc.I4_NAME],
             'I3: the inventory slot at I1 normal return is not the installed I4 marker')
    entered = record.markers.get(sc.I4_NAME, 0)
    sc.check('EC6', entered == 1, f'I4 entered={entered}, expected exactly 1')
    restoration(resolution, bound, schema, 'EC7')


@pytest.mark.sig_case('PE-02')
def test_pe02_c01_witness_control(resolution, schema, record_property):
    """Composed-instrument normal path, same-slot binding, I4 liveness WITH I1 present,
    independent restoration. A CONTROL, not a mutation kill."""
    sc.require_full_preflight(resolution)
    bound = sc.bind_control('C01')                    # X3a + X3c; X3b NOT APPLICABLE
    record_property('x3', {'x3a': bound.x3a, 'x3b': 'NOT APPLICABLE (OD-SIG-20)',
                           'x3c': bound.x3c})
    record: ov.RunRecord | None = None
    probe: sc.BindingProbe | None = None
    wrec: ov.WitnessRecord | None = None
    try:
        record, probe = run_part_e(resolution, bound, schema)
        sc.require_observed(record)
        sc.require_restored(resolution, exclude=sc.PART_E_OWN_SLOTS)
        sc.require_no_unknown(record)
        wrec = sc.require_part_e_bound(resolution, record, probe)
        evaluate_pe02(resolution, record, wrec, bound, schema)
    finally:
        del record, probe, wrec      # I6: every strong reference dropped at run teardown


# ------------------------------------------------------------------------ PE-01


def evaluate_pe01(resolution: ov.Resolution, record: ov.RunRecord, wrec: ov.WitnessRecord,
                  bound: sc.BoundInput, schema: str) -> None:
    """EA1 .. EA7, strictly in order. Every identity comparison is ``is``."""
    public = record.public
    sc.check('EA1', isinstance(public, MigrationUnitRejected) and not record.accepted,
             f'public refusal expected; public={type(public).__name__}')
    sc.check('EA1', getattr(public, 'reason_code', None) == REASON_CODE,
             f'reason_code={getattr(public, "reason_code", None)!r}')

    fails = sc.failing(record)
    sc.check('EA2', len(fails) == 1, f'failing events={len(fails)}')
    sc.check('EA2', fails[0].site == ov.SIG_TARGET_MEMBERSHIP,
             f'failing site={fails[0].site}, expected {ov.SIG_TARGET_MEMBERSHIP}')
    e1 = fails[0].exception                           # E1 is bound HERE, strongly
    sc.check('EA2', e1 is not None, 'the observer recorded no exception object')

    try:
        # L1 -- counts first, then the same-object check (M-O11-L1's first failure).
        sc.check('EA3', wrec.entries == 1, f'I1 entries={wrec.entries}')
        sc.check('EA3', wrec.normal_returns == 0, f'I1 normal returns={wrec.normal_returns}')
        sc.check('EA3', wrec.exception_exits == 1,
                 f'I1 exception exits={wrec.exception_exits}')
        sc.check('EA3', wrec.projection_calls == 1,
                 f'I2 projection calls={wrec.projection_calls}')
        sc.check('EA3', wrec.recorded is e1,
                 'L1: the object I2 returned at I1 is not E1')
        # L2 -- binding / anti-bypass, then non-entry. EA5 alone is insufficient.
        sc.check('EA4', wrec.inventory_slot_object is record.installed_markers[sc.I4_NAME],
                 'L2: the inventory slot at I1 exception exit is not the installed I4 marker')
        entered = record.markers.get(sc.I4_NAME, 0)
        sc.check('EA5', entered == 0, f'L2: I4 entered={entered}, expected 0')
        # L3 -- E1 unchanged at the public catch (M5's first failure).
        sc.check('EA6', public is e1, 'L3: the object reaching the public catch is not E1')
        restoration(resolution, bound, schema, 'EA7')
    finally:
        del e1


@pytest.mark.sig_case('PE-01')
def test_pe01_o11_witness_on_wrong_grant_signature(resolution, schema, record_property):
    """The Gate 2 section 8.1 witness on A-wrong-grant-signature (the PC-09 variant)."""
    sc.require_full_preflight(resolution)
    bound = sc.bind_row('PC-09')                      # same input as PC-09: X3a+X3b+X3c
    record_property('x3', {'x3a': bound.x3a, 'x3b': bound.x3b, 'x3c': bound.x3c})
    record: ov.RunRecord | None = None
    probe: sc.BindingProbe | None = None
    wrec: ov.WitnessRecord | None = None
    try:
        record, probe = run_part_e(resolution, bound, schema)
        sc.require_observed(record)
        sc.require_restored(resolution, exclude=sc.PART_E_OWN_SLOTS)
        sc.require_no_unknown(record)
        wrec = sc.require_part_e_bound(resolution, record, probe)   # holds I2's E1 on PE-01
        record_property('event_count', len(record.events))
        evaluate_pe01(resolution, record, wrec, bound, schema)
    finally:
        del record, probe, wrec      # I6: every strong reference dropped at run teardown
