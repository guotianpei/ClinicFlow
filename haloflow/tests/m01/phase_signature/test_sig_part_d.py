"""PHASE-SIGNATURE-01 Gate 3 PART D -- reach-beyond controls. PD-01 .. PD-04.

Frozen design: Parts C/D packet v7 (2cf9ebcf...) section 4; kill-run v7 (1f227404...);
owner freeze OD-SIG-24; OD-SIG-20 (controls bind X3a + X3c ONLY; X3b NOT APPLICABLE).

Controls are loaded from the RECORDED ``.payload_file`` by the procedure Codex ruled
(packet v7 section 2.2a): key order kept, serialized unsorted, those bytes hashed AND
those same bytes validated. The inline cases.json payload is a corroborating
parsed-object check only; the cases.json control ``payload_sha256`` is a non-binding
measured fact and is never read.

PD-01 -- C01, the MANDATORY named exact-three-edge control. D1 .. D7 in order:
  D1 accepted; D2 zero failing; D3 the SET of edges on successful SIG.QUALIFIED events
  EQUALS {CREATE, INVENTORY, TARGET} exactly; D4 CREATE == 1 and INVENTORY == 1;
  D5 TARGET >= 1 (the measured 4 is EVIDENCE -- a deviation is a finding, not a
  failure); D6 no UNKNOWN or unresolved site or edge; D7 the SET of sites exercised
  successfully EQUALS the eight measured IDs exactly.

  "C01 is a named mandatory control. If it is removed, substituted, skipped, or weakened
  to counts or distinctness, M3b evidence is INVALID." C02 and C03 are not substitutes.

PD-02 / PD-03 / PD-04 -- C01 / C02 / C03 acceptance plus the section 4.4 reach-marker
matrix for the rows each control backs. Assertion ids PD-nn.R1 .. R6 are authoring ids
(the frozen section 4.3 is a table without ids); the total event count is EVIDENCE only.

No control here is claimed as passed. The table values are provisional 3.11.15.
"""

from collections.abc import Callable

import pytest
import sig_cases as sc
import sig_overlay as ov

# ------------------------------------------------------------------------ PD-01


def evaluate_pd01(record: ov.RunRecord, record_property: Callable[[str, object], None]) -> None:
    """D1 .. D7, strictly in order."""
    sc.check('D1', record.accepted is True and record.public is None,
             f'C01 must be ACCEPTED; public={type(record.public).__name__}')
    fails = sc.failing(record)
    sc.check('D2', fails == [], f'failing events={len(fails)}')

    edges = sc.successful_qualified_edges(record)
    observed_set = frozenset(edges)
    sc.check('D3', observed_set == sc.D3_EDGE_SET,
             f'missing={sorted(sc.D3_EDGE_SET - observed_set, key=str)} '
             f'extra={sorted(observed_set - sc.D3_EDGE_SET, key=str)}')
    create = sum(1 for e in edges if e == ov.EDGE_CREATE)
    inventory = sum(1 for e in edges if e == ov.EDGE_INVENTORY)
    sc.check('D4', create == 1, f'{ov.EDGE_CREATE} count={create}, expected exactly 1')
    sc.check('D4', inventory == 1, f'{ov.EDGE_INVENTORY} count={inventory}, expected exactly 1')
    target = sum(1 for e in edges if e == ov.EDGE_TARGET)
    record_property('D5.target_count', target)
    record_property('D5.target_evidence', sc.D5_TARGET_EVIDENCE)
    record_property('D5.deviation_is_finding', target != sc.D5_TARGET_EVIDENCE)
    sc.check('D5', target >= 1, f'{ov.EDGE_TARGET} count={target}, expected at least 1')

    bad = sc.unknown_events(record)
    sc.check('D6', bad == [], f'unknown or unresolved site/edge at {bad}')

    sites = frozenset(e.site for e in record.events if e.outcome == 'pass')
    sc.check('D7', sites == sc.D7_SITE_SET,
             f'missing={sorted(sc.D7_SITE_SET - sites)} extra={sorted(sites - sc.D7_SITE_SET)}')


@pytest.mark.sig_case('PD-01')
def test_pd01_c01_exact_three_edge_control(resolution, schema, record_property):
    sc.require_full_preflight(resolution)
    bound = sc.bind_control('C01')                    # X3a + X3c; X3b NOT APPLICABLE
    record_property('x3', {'x3a': bound.x3a, 'x3b': 'NOT APPLICABLE (OD-SIG-20)',
                           'x3c': bound.x3c})
    record = sc.run(resolution, bound, schema)
    try:
        sc.require_observed(record)
        sc.require_restored(resolution)
        # X1 is asserted IN POSITION as D6 here, not pre-checked, so the frozen D-order
        # holds (an UNKNOWN is still never accepted: D6 fails the case).
        record_property('event_count', len(record.events))
        evaluate_pd01(record, record_property)
    finally:
        del record


# ------------------------------------------------------------------ PD-02 .. 04


def evaluate_acceptance(spec: sc.ControlSpec, record: ov.RunRecord) -> None:
    """PD-nn.R1 .. R6, strictly in order."""
    cid = spec.case_id
    sc.check(f'{cid}.R1', record.accepted is True and record.public is None,
             f'{spec.control} must be ACCEPTED; public={type(record.public).__name__}')
    fails = sc.failing(record)
    sc.check(f'{cid}.R2', fails == [], f'failing events={len(fails)}')
    edges = sc.successful_qualified_edges(record)
    create = sum(1 for e in edges if e == ov.EDGE_CREATE)
    inventory = sum(1 for e in edges if e == ov.EDGE_INVENTORY)
    target = sum(1 for e in edges if e == ov.EDGE_TARGET)
    sc.check(f'{cid}.R3', create == 1, f'CREATE count={create}, expected exactly 1')
    sc.check(f'{cid}.R4', inventory == 1, f'INVENTORY count={inventory}, expected exactly 1')
    sc.check(f'{cid}.R5', target >= 1, f'TARGET count={target}, expected at least 1')
    for label, names in spec.markers:
        entered = {n: record.markers.get(n, 0) for n in names}
        sc.check(f'{cid}.R6', any(count >= 1 for count in entered.values()),
                 f'reach marker for {label} not entered on {spec.control}: {entered}')


@pytest.mark.parametrize('case_id', [
    pytest.param(case_id, id=case_id, marks=pytest.mark.sig_case(case_id))
    for case_id in sc.CONTROL_CASES
])
def test_pd_acceptance_control(case_id, resolution, schema, record_property):
    spec = sc.CONTROL_CASES[case_id]
    sc.require_full_preflight(resolution)
    bound = sc.bind_control(spec.control)             # X3a + X3c; X3b NOT APPLICABLE
    record_property('x3', {'x3a': bound.x3a, 'x3b': 'NOT APPLICABLE (OD-SIG-20)',
                           'x3c': bound.x3c})
    record = sc.run(resolution, bound, schema, markers=ov.MARKER_TARGETS)
    try:
        sc.require_observed(record)
        sc.require_restored(resolution)
        sc.require_no_unknown(record)
        edges = sc.successful_qualified_edges(record)
        record_property('evidence.event_count', len(record.events))
        record_property('evidence.event_count_measured_3_11', spec.evidence_events)
        record_property('evidence.target_count',
                        sum(1 for e in edges if e == ov.EDGE_TARGET))
        record_property('evidence.target_measured_3_11', spec.evidence_target)
        record_property('markers', dict(record.markers))
        evaluate_acceptance(spec, record)
    finally:
        del record
