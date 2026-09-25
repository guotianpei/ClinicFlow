"""CP2 carried gap CG-1 / PHASE-COMMENT-01 -- which phase refuses an undeclared comment.

WHAT THIS MODULE OBSERVES
-------------------------
Case `A-comment-undeclared`: control C01 declares `comment: null`, and the case appends
`COMMENT ON FUNCTION {schema}.m02_annex_probe(text, uuid) IS '';`. Both O11
(`_validate_local_statements`) and O12 (`_validate_statement_inventory`) can raise
`INSTALL_COMMENT_MISMATCH`, so the code alone cannot say which phase refused.

The pinned validation order was ambiguous for this row. The owner ruled O11
(owner ruling record `claude_owner-rulings-cg2-closure-and-cg1-rq1-rq2-2026-09-25.md`
section 2, sha256 3ca1d7cf...). The expected phase here comes from that ruling, not
from the code. If an assertion here disagrees with it, the disagreement goes to the
owner; the test is not edited to match.

The frozen CP1 oracle `expected-refusals.json` keeps `phase: null` for this row. That
means CP1 asserted no phase. It does not mean this test lacks an expectation.

HOW IT OBSERVES
---------------
Three module attributes are wrapped inside one `pytest.MonkeyPatch.context()`: the O11
and O12 seams and `function_checksum`. The checker calls all three by bare module-global
name. Each run gets a fresh `Observed` record. The first wrapper to see an exception
keeps it as the witness, and no later wrapper overwrites it. Only `MigrationUnitRejected`
is captured; anything else propagates.

This module adds no production seam and edits no existing file. Requirements v2
(70df0c43...), architecture v2 (ef0a4041...), test cases v2 (5e0cd1d5...).
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import function_policy as subject

ROOT = Path(__file__).parent / 'fixtures/function_policy'
CASES = json.loads((ROOT / 'cases.json').read_text())
VARIANTS = json.loads((ROOT / 'sql-fixtures.json').read_text())['variants']
C01_BYTES = (ROOT / 'controls' / 'C01.json').read_bytes()
SCHEMA = CASES['schema_key']

CASE_ID = 'A-comment-undeclared'
CODE = 'INSTALL_COMMENT_MISMATCH'
O11 = '_validate_local_statements'
O12 = '_validate_statement_inventory'
CHECKSUM = 'function_checksum'
WRAPPED = (O11, O12, CHECKSUM)

VARIANT_SHA256 = '09d43241e09d71b262aa929af3356f9af7deaad28b97cd226240a7ceaca6bb77'
C01_SHA256 = '2bdc9c52643cd6d994ddfc24be27c0b39bda808eec0b4c7fef3024bba69299a7'
APPENDED = "\nCOMMENT ON FUNCTION {schema}.m02_annex_probe(text, uuid) IS '';"


def _variant() -> dict[str, Any]:
    matches = [v for v in VARIANTS if v['case_id'] == CASE_ID]
    assert len(matches) == 1, f'expected exactly one {CASE_ID} variant, found {len(matches)}'
    found: dict[str, Any] = matches[0]
    return found


def _c01() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(C01_BYTES)
    return loaded


def encode(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')


@dataclass
class Observed:
    """One run of the checker, as observed. It holds no expected value of its own."""

    accepted: bool = False
    caught: MigrationUnitRejected | None = None
    code: str | None = None
    entries: list[str] = field(default_factory=list)
    returns: list[str] = field(default_factory=list)
    witness: str | None = None
    witness_exception: BaseException | None = None
    exception_events: list[tuple[str, int]] = field(default_factory=list)


@contextmanager
def recorder(
    monkeypatch: pytest.MonkeyPatch,
    outer: dict[str, Callable[[Callable[..., Any]], Callable[..., Any]]] | None = None,
) -> Iterator[Callable[[Any], Observed]]:
    """Wrap O11, O12 and the checksum, and yield a runner that returns a fresh record.

    `outer` installs a layer OUTSIDE a spy, so the spy sees what the real function
    raised before the layer can change what finally escapes.
    """

    layers = outer or {}
    box: dict[str, Observed] = {}

    def wrap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            state = box['state']
            state.entries.append(name)
            try:
                result = real(*args, **kwargs)
            except BaseException as error:
                state.exception_events.append((name, id(error)))
                if state.witness is None:
                    state.witness = name
                    state.witness_exception = error
                raise
            state.returns.append(name)
            return result
        return spy

    with pytest.MonkeyPatch.context() as patcher:
        for name in WRAPPED:
            wrapped = wrap(name, getattr(subject, name))
            if name in layers:
                wrapped = layers[name](wrapped)
            patcher.setattr(subject, name, wrapped)

        def run(payload: Any) -> Observed:
            state = Observed()
            box['state'] = state
            try:
                subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
                state.accepted = True
            except MigrationUnitRejected as error:
                state.caught = error
                state.code = error.reason_code
            return state

        yield run


def assert_observed(observed: Observed, *, code: str, entries: list[str], witness: str) -> None:
    """THE assertion helper. The clause order is fixed so a negative control isolates one."""

    assert not observed.accepted, 'accepted: expected a refusal, the checker accepted'
    assert observed.code == code, f'code: {observed.code!r} != {code!r}'
    assert observed.entries == entries, f'entries: {observed.entries!r} != {entries!r}'
    assert observed.witness == witness, f'phase: witness {observed.witness!r} != {witness!r}'
    assert observed.witness_exception is observed.caught, (
        'identity: the exception the refusing phase raised is not the one the caller caught'
    )
    assert O12 not in observed.entries and CHECKSUM not in observed.entries, (
        f'non-reach: a later step was entered: {observed.entries!r}'
    )


def _originals() -> dict[str, Any]:
    return {name: getattr(subject, name) for name in WRAPPED}


def _assert_restored(originals: dict[str, Any]) -> None:
    for name, original in originals.items():
        assert getattr(subject, name) is original, f'{name} was not restored'


def test_phase_comment_01a_frozen_inputs_are_bound() -> None:
    """TC-01 (CG1-R0, AQ-3). The frozen inputs are exactly the reviewed ones."""

    variant = _variant()
    assert variant['control'] == 'C01'

    canonical = json.dumps(variant['payload'], sort_keys=True, indent=2, ensure_ascii=False)
    digest = hashlib.sha256((canonical + '\n').encode('utf-8')).hexdigest()
    assert digest == VARIANT_SHA256
    assert variant['payload_sha256'] == VARIANT_SHA256

    assert hashlib.sha256(C01_BYTES).hexdigest() == C01_SHA256

    assert isinstance(SCHEMA, str)
    assert SCHEMA == 'tenant_aaaaaaaa'

    payload = variant['payload']
    control = _c01()
    assert sorted(payload) == sorted(control)
    for key in control:
        if key != 'template':
            assert payload[key] == control[key], f'{key} differs from C01'
    assert payload['template'] == control['template'] + APPENDED

    functions = payload['policy']['functions']
    assert len(functions) == 1
    assert functions[0]['comment'] is None
    assert [f['comment'] for f in control['policy']['functions']] == [None]
    assert 'COMMENT' not in control['template']


def test_phase_comment_01b_c01_positive_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-02 (CG1-R1). All three wrappers are live and count, so a zero later means something."""

    with recorder(monkeypatch) as run:
        observed = run(_c01())
    assert observed.accepted is True
    assert observed.caught is None
    assert observed.code is None
    assert observed.entries == [O11, O12, CHECKSUM]
    assert observed.returns == [O11, O12, CHECKSUM]
    assert observed.witness is None
    assert observed.exception_events == []


def test_phase_comment_01c_undeclared_comment_is_refused_at_o11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-03 (CG1-R2 to R6). The undeclared comment is refused at O11, per the owner ruling.

    The expected phase O11 comes from the owner ruling (3ca1d7cf..., section 2), not from
    observing the code. `expected-refusals.json` keeps `phase: null` for this row: that
    means the frozen CP1 oracle asserted no phase. Any unexpected trace or phase here,
    whichever clause fails first, is escalated to the owner; the test is not edited.
    """

    with recorder(monkeypatch) as run:
        observed = run(_variant()['payload'])
    assert_observed(observed, code=CODE, entries=[O11], witness=O11)
    assert observed.returns == []


def test_phase_comment_01d_wrong_phase_expectation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-04 (CG1-R7). With code, entries and identity correct, only the phase clause fails."""

    with recorder(monkeypatch) as run:
        observed = run(_variant()['payload'])

    assert_observed(observed, code=CODE, entries=[O11], witness=O11)

    with pytest.raises(AssertionError, match='^phase:'):
        assert_observed(observed, code=CODE, entries=[O11], witness=O12)


def test_phase_comment_01e_substituted_exception_fails_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-05 (CG1-R7). A same-code, same-phase substitute is caught only by identity."""

    def substitute(inner: Callable[..., Any]) -> Callable[..., Any]:
        def layer(*args: Any, **kwargs: Any) -> Any:
            try:
                return inner(*args, **kwargs)
            except MigrationUnitRejected as original:
                raise MigrationUnitRejected(reason_code=original.reason_code) from None
        return layer

    with recorder(monkeypatch, outer={O11: substitute}) as run:
        observed = run(_variant()['payload'])

    assert observed.code == CODE
    assert observed.witness == O11
    assert observed.exception_events[0] == (O11, id(observed.witness_exception))
    assert observed.witness_exception is not observed.caught
    assert observed.entries == [O11]

    with pytest.raises(AssertionError, match='^identity:'):
        assert_observed(observed, code=CODE, entries=[O11], witness=O11)


class _Sentinel(Exception):
    pass


def test_phase_comment_01f_recorder_restores_and_isolates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-06 (CG1-R7, R5a). Per-run records, no stacking, restoration on every exit."""

    originals = _originals()

    with recorder(monkeypatch) as run:
        for name, original in originals.items():
            assert getattr(subject, name) is not original, f'{name} was not wrapped'
        first = run(_c01())
        snapshot = (list(first.entries), list(first.returns), list(first.exception_events))
        second = run(_c01())
        assert first is not second
        assert first.entries is not second.entries
        assert first.returns is not second.returns
        assert first.exception_events is not second.exception_events
        for observed in (first, second):
            assert observed.entries == [O11, O12, CHECKSUM]
            assert observed.returns == [O11, O12, CHECKSUM]
        assert (first.entries, first.returns, first.exception_events) == snapshot
    _assert_restored(originals)

    with recorder(monkeypatch) as run:
        third = run(_c01())
    assert third.entries == [O11, O12, CHECKSUM]
    assert third.entries == first.entries
    _assert_restored(originals)

    with pytest.raises(_Sentinel), recorder(monkeypatch):
        raise _Sentinel
    _assert_restored(originals)


def test_phase_comment_01g_unexpected_exception_is_not_recorded_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-07 (AD-3a). Only MigrationUnitRejected is captured; anything else propagates."""

    originals = _originals()

    def raise_runtime(inner: Callable[..., Any]) -> Callable[..., Any]:
        def layer(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError('probe')
        return layer

    with (
        pytest.raises(RuntimeError, match='probe'),
        recorder(monkeypatch, outer={O11: raise_runtime}) as run,
    ):
        run(_c01())
    _assert_restored(originals)
