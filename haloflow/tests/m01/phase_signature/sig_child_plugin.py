"""B-24 INNER RUN plugin. Loaded ONLY in the B-24 child via ``-p sig_child_plugin``.

The child is a fresh, complete Gate 3 invocation under M4 whose only job is to die at
preflight. It carries NO assertion about M4 -- every assertion lives in the parent
(B-24 contract v3 section 1). Its required lifecycle (section 2.5), in order:

1. create the zero sentinel at session start;
2. import and collection complete successfully (a child that dies here is NOT a kill;
   its own exit code is retained);
3. resolve and persist the FULL selection set;
4. run M4-aware preflight AFTER collection but BEFORE any call or body hook;
5. atomically persist valid artifacts;
6. translate ONLY the named preflight abort to the reserved exit code 97.

M4 reaches this process through the launch environment only. The parent's
``_require`` is never touched.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

ARTIFACT_DIR_ENV = 'HALOFLOW_SIG_ARTIFACT_DIR'
EXPECTED_SNAPSHOT_ENV = 'HALOFLOW_SIG_EXPECTED_SNAPSHOT'

# Harness-private reserved exit code. Only the named preflight-abort encoder emits it.
PREFLIGHT_ABORT_EXIT = 97

# Any OTHER preflight death in the child. pytest's own internal-error code: never 97,
# so it can never be read as the M4 kill. The artifact is still written for diagnosis.
OTHER_PREFLIGHT_DEATH_EXIT = int(pytest.ExitCode.INTERNAL_ERROR)

# B-24 contract v3 section 2.3: the closed, explicitly named inner selection. A-26 is
# also the designated body -- harmless if preflight wrongly succeeds, and its entry
# makes bodies_entered > 0, which fails outer B-24 WITHOUT recursion.
CHILD_SELECTION: tuple[str, ...] = ('A-26',)

BODY_ENTRY = 'body-entry.json'
PREFLIGHT = 'preflight.json'
SELECTION = 'selection.json'


def _artifact_dir() -> Path:
    return Path(os.environ[ARTIFACT_DIR_ENV])


def _write_atomic(name: str, payload: Any) -> None:
    target = _artifact_dir() / name
    partial = target.with_name(target.name + '.partial')
    partial.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
    os.replace(partial, target)


def _case_id(item: pytest.Item) -> str | None:
    marker = item.get_closest_marker('sig_case')
    return str(marker.args[0]) if marker is not None and marker.args else None


# 1 -------------------------------------------------------------------------------
def pytest_sessionstart(session: pytest.Session) -> None:
    _write_atomic(BODY_ENTRY, {'bodies_entered': 0})


# 3 -------------------------------------------------------------------------------
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item],
) -> None:
    kept = [i for i in items if _case_id(i) in CHILD_SELECTION]
    dropped = [i for i in items if _case_id(i) not in CHILD_SELECTION]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
    _write_atomic(SELECTION, sorted({cid for cid in map(_case_id, kept) if cid is not None}))


# 4, 5, 6 -------------------------------------------------------------------------
def pytest_collection_finish(session: pytest.Session) -> None:
    if session.testsfailed:
        return          # 2: collection failed; keep pytest's own exit code, never 97
    import sig_overlay as ov

    # Determinism (section 5.2): the parent's measured snapshot hash, re-asserted here.
    expected = os.environ[EXPECTED_SNAPSHOT_ENV]
    observed = hashlib.sha256(ov.POLICY_PATH.read_bytes()).hexdigest()
    if expected != ov.EXPECTED_SNAPSHOT_SHA256 or observed != expected:
        return          # a different snapshot is not this measurement; no 97
    try:
        ov.preflight()                   # M4 arrives via the environment at import
    except ov.PreflightAbort as abort:
        _write_atomic(PREFLIGHT, {
            'diagnosis': abort.diagnosis,
            'unresolved_binding': abort.details.get('unresolved_binding'),
            'invariants_passed': list(abort.ledger),
            'stage': 'preflight',
        })
        if is_named_m4_abort(abort):
            pytest.exit(f'preflight aborted: {abort.diagnosis}',
                        returncode=PREFLIGHT_ABORT_EXIT)
        # A different preflight death is NOT translated to 97 (lifecycle step 6).
        pytest.exit(f'unexpected preflight death: {abort.diagnosis}',
                    returncode=OTHER_PREFLIGHT_DEATH_EXIT)


def is_named_m4_abort(abort: Any) -> bool:
    """The ONE abort that may be encoded as 97: M4's S14 death, exactly."""
    import sig_overlay as ov

    return (abort.diagnosis == ov.Diag.BINDING_UNRESOLVED
            and abort.details.get('unresolved_binding') == ov.SIG_CREATE_NAME
            and tuple(abort.ledger) == ov.STAGES[:13])


# the single named body hook -----------------------------------------------------
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    """ONE hook at the top of every case body. Not per-case code."""
    path = _artifact_dir() / BODY_ENTRY
    current = json.loads(path.read_text(encoding='utf-8'))
    _write_atomic(BODY_ENTRY, {'bodies_entered': int(current['bodies_entered']) + 1})
