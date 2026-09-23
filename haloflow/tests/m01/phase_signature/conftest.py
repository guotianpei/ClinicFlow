"""PHASE-SIGNATURE-01 Gate 3 suite: shared fixtures and the case-id marker.

Every case carries ``@pytest.mark.sig_case('<id>')``. The id is the frozen design's
case id (A-01..A-29, B-01..B-24, PC-01..PC-09, PD-01..PD-04, PE-01, PE-02). The
B-24 child selects on it, and ``selection.json`` records it.

The session resolution runs the full S1..S14 preflight ONCE for the parent suite.
A PreflightAbort here fails every dependent case at setup -- a closed gate, never a
skip. M4 is never present in the parent: it is delivered to the B-24 child by
launch environment only (B-24 contract v3 section 4a).
"""

from collections.abc import Iterator

import pytest
import sig_overlay as ov


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        'markers', 'sig_case(case_id): PHASE-SIGNATURE-01 Gate 3 frozen case id')


@pytest.fixture(scope='session')
def resolution() -> ov.Resolution:
    """The parent's S1..S14 resolution. Aborts are NEVER converted to skips."""
    try:
        return ov.preflight()
    except ov.PreflightAbort as abort:
        pytest.fail(f'preflight aborted: {abort.diagnosis} ledger={list(abort.ledger)} '
                    f'details={abort.details}', pytrace=False)


@pytest.fixture(scope='session')
def schema() -> str:
    return ov.schema_key()


@pytest.fixture(autouse=True)
def _seams_restored() -> Iterator[None]:
    """X2 / B-22 around every case: every named seam and slot is at its original."""
    assert ov.STATIC_SEAMS.all_original(), 'a static seam was left replaced'
    assert ov.LIVE_SEAMS.all_original(), 'a live seam was left replaced'
    assert ov.PHASE_SLOT.current is ov.PHASE_SLOT.baseline, 'phase projection left replaced'
    assert ov.WITNESS_SLOT.current is ov.WITNESS_SLOT.identity, 'witness slot left replaced'
    yield
    assert ov.STATIC_SEAMS.all_original(), 'a static seam was not restored'
    assert ov.LIVE_SEAMS.all_original(), 'a live seam was not restored'
    assert ov.PHASE_SLOT.current is ov.PHASE_SLOT.baseline, 'phase projection not restored'
    assert ov.WITNESS_SLOT.current is ov.WITNESS_SLOT.identity, 'witness slot not restored'
