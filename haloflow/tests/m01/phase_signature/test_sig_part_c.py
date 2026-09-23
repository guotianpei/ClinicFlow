"""PHASE-SIGNATURE-01 Gate 3 PART C -- the nine negative rows. PC-01 .. PC-09.

Frozen design: Parts C/D packet v7 (2cf9ebcf...) sections 2 and 3; kill-run v7
(1f227404...); owner freeze OD-SIG-24; OD-SIG-20 (X3b applies to rows).

Per row, in this order (packet v7 section 2.3 -- "the first failing assertion" is a
mechanically reproducible fact):

  preconditions  S1..S14 exact prefix; X3a + X3b + X3c (bound once, before the run)
  run            one observed run; the bytes validated ARE the bytes hashed for X3c
  contract       no ObservationAbort; X2 restoration of the run's instruments;
                 X1 no UNKNOWN
  A1 .. A8       public refusal, reason code, cardinality, site, edge, identity,
                 phase via ``phase_for_site``, exact full ordered ledger

A3 is cardinality ONLY; site equality is A4's alone. A5 compares ``None`` as a value.
A6 is ``is`` -- never reason_code, type, ``==`` or a stored id(). A7 goes through the
named projection ``ov.phase_for_site`` and is never inlined (M1's only target).

CREDIT. A row passing here is a PROVISIONAL ROW-ONLY measurement until its backing
control has independently passed (packet v7 section 2.5, ``OI-READY``), and no row
assertion may be credited before G-2 (work plan v3 section 4). The Stage 3 staged
executor enforces both from ``sig_cases.BACKING_CASES`` and ``sig_cases.G2_GATED``.

PC-09: A7's 'O11' is the recorded pin and DOES NOT ESTABLISH O11 (flag 1). PC-09 does
not install the inventory marker: its non-entry is Part E's (PE-01), and v7's
"recorded and NOT credited" note stays as written.
"""

import pytest
import sig_cases as sc
import sig_overlay as ov

from haloflow.m01.errors import MigrationUnitRejected

REASON_CODE = 'INSTALL_SIGNATURE_MISMATCH'


def evaluate_row(spec: sc.RowSpec, record: ov.RunRecord) -> None:
    """A1 .. A8, strictly in order."""
    public = record.public
    sc.check('A1', isinstance(public, MigrationUnitRejected) and not record.accepted,
             f'public refusal expected; accepted={record.accepted} '
             f'public={type(public).__name__}')
    sc.check('A2', getattr(public, 'reason_code', None) == REASON_CODE,
             f'reason_code={getattr(public, "reason_code", None)!r}')

    fails = sc.failing(record)
    sc.check('A3', len(fails) == 1, f'failing events={len(fails)}')
    [terminal] = fails

    sc.check('A4', terminal.site == spec.site,
             f'site expected={spec.site} observed={terminal.site}')
    # None is compared as a value by identity; a real edge id by equality.
    edge_ok = terminal.edge is None if spec.edge is None else terminal.edge == spec.edge
    sc.check('A5', edge_ok, f'edge expected={spec.edge} observed={terminal.edge}')
    sc.check('A6', public is terminal.exception,
             'the public exception is not the object the observer recorded')
    phase = ov.phase_for_site(terminal.site)
    sc.check('A7', phase == spec.phase, f'phase expected={spec.phase} observed={phase}')
    observed = sc.ledger_of(record)
    sc.check('A8', observed == spec.ledger,
             f'ledger expected={list(spec.ledger)} observed={list(observed)}')


@pytest.mark.parametrize('case_id', [
    pytest.param(case_id, id=case_id, marks=pytest.mark.sig_case(case_id))
    for case_id in sc.ROWS
])
def test_part_c_row(case_id, resolution, schema, record_property):
    spec = sc.ROWS[case_id]
    sc.require_full_preflight(resolution)
    bound = sc.bind_row(case_id)                      # X3a + X3b + X3c, before the run
    record_property('subject', spec.row)
    record_property('x3', {'x3a': bound.x3a, 'x3b': bound.x3b, 'x3c': bound.x3c})
    record_property('backing_cases', list(sc.BACKING_CASES[case_id]))

    record = sc.run(resolution, bound, schema)
    try:
        sc.require_observed(record)
        sc.require_restored(resolution)
        sc.require_no_unknown(record)
        record_property('observed_ledger', [list(e) for e in sc.ledger_of(record)])
        evaluate_row(spec, record)
    finally:
        del record                  # I6-style release: no run outlives its assertions
