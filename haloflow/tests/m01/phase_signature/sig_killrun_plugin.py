"""KILL-RUN CHILD plugin. Loaded ONLY via ``-p sig_killrun_plugin`` in a child pytest that
``sig_killrun`` launches. Never loaded by CI's ``pytest tests/m01`` and never by the B-24
child (which has its own explicit environment and ``-p sig_child_plugin``).

Its whole job, per child:

1. read the launch environment ONCE at configure: the mutant name (``NONE`` for a baseline
   stage), the exact case-id selection, and the results path; refuse to start if the M4
   variable ``HALOFLOW_SIG_MUTANT`` is present -- M4 reaches ONLY the B-24 child;
2. select exactly the requested ``sig_case`` ids, deselect everything else, and record the
   collected id -> item count so the executor can verify the selection by equality;
3. for an in-process mutant, install it around each selected case BODY only (the
   ``pytest_runtest_call`` phase) through ``sig_mutants.activated``, and restore it in
   ``finally`` -- so the Stage 1 autouse check sees every slot at its original both before
   (setup) and after (teardown) the body;
4. record per item and per phase the outcome and the FIRST failure, classified: a frozen
   assertion (``SigAssertionFailed`` -> its id and detail), a case precondition
   (``CaseAbort`` -> its diagnosis), a harness refusal (``KillRunAbort`` -> its diagnosis),
   or anything else (type and message);
5. write the results atomically at session finish.

It carries NO kill judgement. ``sig_killrun`` compares the saved results against the
accepted matrices. It adds no assertion and edits no case body.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

ENV_MUTANT = 'HALOFLOW_SIG_KILLRUN_MUTANT'
ENV_CASES = 'HALOFLOW_SIG_KILLRUN_CASES'
ENV_RESULTS = 'HALOFLOW_SIG_KILLRUN_RESULTS'
M4_ENV = 'HALOFLOW_SIG_MUTANT'           # sig_overlay.MUTANT_ENV; checked BEFORE importing it

# B-24 prints exactly one line with this prefix when outer B-24 passes (Stage 1 v2).
B24_KILL_LINE_PREFIX = '[B-24] KILLED BY B-24: '

_STATE: dict[str, Any] = {
    'mutant': None,
    'requested': (),
    'results_path': None,
    'collected': {},
    'items': {},
    'facts': {},
    'refused': None,
}


def _case_id(item: pytest.Item) -> str | None:
    marker = item.get_closest_marker('sig_case')
    return str(marker.args[0]) if marker is not None and marker.args else None


def _write_atomic(path: Path, payload: Any) -> None:
    partial = path.with_name(path.name + '.partial')
    partial.write_text(json.dumps(payload, sort_keys=True, indent=1, default=str),
                       encoding='utf-8')
    os.replace(partial, path)


# 1 ----------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    if M4_ENV in os.environ:
        _STATE['refused'] = 'M4 environment variable present in a kill-run child'
        pytest.exit(_STATE['refused'], returncode=int(pytest.ExitCode.USAGE_ERROR))
    _STATE['mutant'] = os.environ[ENV_MUTANT]
    _STATE['requested'] = tuple(c for c in os.environ[ENV_CASES].split(',') if c)
    _STATE['results_path'] = Path(os.environ[ENV_RESULTS])
    import sig_mutants as sm

    if _STATE['mutant'] not in sm.IN_PROCESS | {sm.NONE}:
        _STATE['refused'] = f'mutant {_STATE["mutant"]!r} is not an in-process mutant or NONE'
        pytest.exit(_STATE['refused'], returncode=int(pytest.ExitCode.USAGE_ERROR))


# 2 ----------------------------------------------------------------------------
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item],
) -> None:
    requested = set(_STATE['requested'])
    kept = [i for i in items if _case_id(i) in requested]
    dropped = [i for i in items if _case_id(i) not in requested]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
    collected: dict[str, int] = {}
    for item in kept:
        cid = _case_id(item)
        if cid is not None:
            collected[cid] = collected.get(cid, 0) + 1
    _STATE['collected'] = collected


# 3 ----------------------------------------------------------------------------
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, None]:
    import sig_mutants as sm

    mutant = _STATE['mutant']
    cid = _case_id(item) or ''
    if mutant == sm.NONE:
        before = sm.ORIGINAL_RUN is _current_run()
        yield
        _STATE['facts'][item.nodeid] = {'mutant': sm.NONE, 'case_id': cid,
                                        'run_slot_original_before': before,
                                        'run_slot_original_after':
                                            sm.ORIGINAL_RUN is _current_run()}
        return
    ctx = sm.MutantContext(mutant, cid)
    try:
        with sm.activated(mutant, ctx):
            yield
    finally:
        _STATE['facts'][item.nodeid] = ctx.summary()
        ctx.release()                  # drop every strong reference the case held


def _current_run() -> Any:
    import sig_cases as sc

    return sc.run


# 4 ----------------------------------------------------------------------------
def _classify(excinfo: pytest.ExceptionInfo[BaseException] | None) -> dict[str, Any] | None:
    if excinfo is None:
        return None
    import sig_cases as sc
    import sig_mutants as sm

    error = excinfo.value
    if isinstance(error, sc.SigAssertionFailed):
        return {'kind': 'assertion', 'id': error.assertion_id, 'detail': error.detail}
    if isinstance(error, sc.CaseAbort):
        return {'kind': 'case_abort', 'diagnosis': error.diagnosis}
    if isinstance(error, sm.KillRunAbort):
        return {'kind': 'harness_abort', 'diagnosis': error.diagnosis}
    if excinfo.errisinstance(pytest.skip.Exception):
        return {'kind': 'skip', 'message': str(error)[:2000]}
    return {'kind': 'other', 'type': f'{type(error).__module__}.{type(error).__qualname__}',
            'message': str(error)[:2000]}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None],
) -> Generator[None, Any, None]:
    outcome = yield
    report = outcome.get_result()
    entry = _STATE['items'].setdefault(item.nodeid, {'case_id': _case_id(item), 'phases': {}})
    entry['phases'][call.when] = {'outcome': report.outcome,
                                  'failure': _classify(call.excinfo)}
    if call.when == 'call':
        entry['facts'] = _STATE['facts'].pop(item.nodeid, None)
        entry['b24_kill_lines'] = [line for line in report.capstdout.splitlines()
                                   if line.startswith(B24_KILL_LINE_PREFIX)]


# 5 ----------------------------------------------------------------------------
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    path = _STATE['results_path']
    if path is None:
        return
    _write_atomic(path, {
        'mutant': _STATE['mutant'],
        'requested': list(_STATE['requested']),
        'collected': _STATE['collected'],
        'items': _STATE['items'],
        'exitstatus': int(exitstatus),
        'python': sys.version,
        'executable': sys.executable,
    })
