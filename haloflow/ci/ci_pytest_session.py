"""pytest plugin: record how the session ended (diagnostic, CI only).

Loaded only by the gated CI pytest step, through that step's environment
(``-p ci_pytest_session`` with ``ci/`` on ``PYTHONPATH``; that directory holds
only this plugin and the expected-red manifest, so nothing else is shadowed), and only when
``CI_PYTEST_SESSION_FILE`` names an output path. It changes nothing about how
tests run or how pytest exits; at session finish it writes one small JSON file:

    {"exitstatus": int, "testscollected": int, "testsfailed": int}

``exitstatus`` is pytest's own final exit status as passed to
``pytest_sessionfinish`` (0 ok, 1 tests failed, 2 interrupted, 3 internal error,
4 usage error, 5 no tests collected). scripts/ci_test_report.py uses it to tell
an ordinary red run from an interrupted or incomplete one, which the GitHub step
outcome ("failure" for every non-zero exit) cannot.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

import pytest

ENV = "CI_PYTEST_SESSION_FILE"

_F = TypeVar("_F", bound=Callable[..., object])
# Run after the other sessionfinish implementations, so their effect on the run
# is already final when the record is written.
_trylast = cast(Callable[[_F], _F], pytest.hookimpl(trylast=True))


@_trylast
def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    target = os.environ.get(ENV)
    if not target:
        return
    record = {
        "exitstatus": int(exitstatus),
        "testscollected": int(session.testscollected),
        "testsfailed": int(session.testsfailed),
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True) + "\n")
