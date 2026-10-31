"""Synthetic controls for scripts/ci_test_report.py and ci/ci_pytest_session.py.

Codex reviews 2026-10-31 (proposal review item 5; 2ea4ddb REVISE F-CIR1).
Runs outside the CI gate scope by design (the gated pytest scope is unchanged):
    python -I -m pytest -q -p no:cacheprovider scripts/test_ci_test_report.py
Real pytest runs are made on tiny throwaway test files in tmp_path, with the
session plugin loaded exactly as the workflow loads it (``-p ci_pytest_session``,
``ci/`` on PYTHONPATH, CI_PYTEST_SESSION_FILE set), so the reporter is
exercised on pytest's own xunit1 report and the plugin's own session record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent
PLUGIN_DIR = SCRIPTS.parent / "ci"
sys.path.insert(0, str(SCRIPTS))
import ci_test_report as r  # noqa: E402

PASS_FILE = "def test_ok():\n    pass\n"
FAIL_FILE = "def test_bad():\n    assert 1 == 2\n"
SHA = "3ae9404534cf6d41c67dd6a2e4a40253a7947499"


@dataclass
class Run:
    junit: Path
    session: Path
    rc: int


def _pytest(tmp_path: Path, files: dict[str, str], *, plugin: bool = True) -> Run:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    junit = tmp_path / "junit.xml"
    session = tmp_path / "session.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if plugin:
        env["PYTHONPATH"] = str(PLUGIN_DIR)
        env["CI_PYTEST_SESSION_FILE"] = str(session)
        args += ["-p", "ci_pytest_session"]
    args += ["-o", "junit_family=xunit1", f"--junitxml={junit}"]
    args += [name for name in files if name.startswith("test_")]
    proc = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    return Run(junit=junit, session=session, rc=proc.returncode)


def _manifest(tmp_path: Path, nodes: list[str], **override: Any) -> Path:
    path = tmp_path / "manifest.json"
    body: dict[str, Any] = {
        "source_ref": SHA,
        "scope": "synthetic",
        "entries": [{"node": n, "rationale": "synthetic"} for n in nodes],
    }
    body.update(override)
    path.write_text(json.dumps(body))
    return path


def _session(tmp_path: Path, exitstatus: int, collected: int, failed: int) -> Path:
    path = tmp_path / "session-synthetic.json"
    record = {"exitstatus": exitstatus, "testscollected": collected, "testsfailed": failed}
    path.write_text(json.dumps(record))
    return path


def _build(
    run: Run, manifest: Path, *, outcome: str | None = None, session: Path | None = None
) -> tuple[dict[str, Any], list[str]]:
    return r.build(
        junit=run.junit,
        session=run.session if session is None else session,
        manifest=manifest,
        pytest_outcome=("success" if run.rc == 0 else "failure") if outcome is None else outcome,
        source_sha="s",
        head_sha="h",
    )


# --- ordinary outcomes -----------------------------------------------------------


def test_all_pass(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": PASS_FILE})
    report, lines = _build(run, _manifest(tmp_path, []))
    assert report["status"] == "PASS"
    assert report["nodes"] == {"test_a.py::test_ok": "passed"}
    assert report["pytest_session"] == {"exitstatus": 0, "testscollected": 1, "testsfailed": 0}
    assert "chunks=0" in lines[0]


def test_unclassified_assertion_failure_is_not_clean(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    report, _ = _build(run, _manifest(tmp_path, []))
    assert run.rc == 1
    assert report["status"] == "NOT_CLEAN"
    assert report["nodes"] == {"test_a.py::test_bad": "unclassified_failed"}


def test_expected_failure_only(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": PASS_FILE + FAIL_FILE})
    report, _ = _build(run, _manifest(tmp_path, ["test_a.py::test_bad"]))
    assert run.rc == 1
    assert report["status"] == "EXPECTED_RED_ONLY"
    assert report["nodes"]["test_a.py::test_bad"] == "expected_failed"


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_setup_and_teardown_errors_are_never_expected(tmp_path: Path, phase: str) -> None:
    fixture = (
        "import pytest\n@pytest.fixture\ndef f():\n"
        + (
            "    raise RuntimeError('boom')\n"
            if phase == "setup"
            else "    yield\n    raise RuntimeError('boom')\n"
        )
        + "def test_x(f):\n    pass\n"
    )
    run = _pytest(tmp_path, {"test_a.py": fixture})
    report, _ = _build(run, _manifest(tmp_path, ["test_a.py::test_x"]))
    assert report["status"] == "NOT_CLEAN"
    assert report["nodes"]["test_a.py::test_x"] == "error"


def test_failure_plus_teardown_error_counts_once_as_error(tmp_path: Path) -> None:
    body = (
        "import pytest\n@pytest.fixture\ndef f():\n    yield\n    raise RuntimeError('t')\n"
        "def test_x(f):\n    assert 1 == 2\n"
    )
    run = _pytest(tmp_path, {"test_a.py": body})
    report, _ = _build(run, _manifest(tmp_path, []))
    assert report["nodes"] == {"test_a.py::test_x": "error"}
    assert report["multiple_outcome_nodes"] == ["test_a.py::test_x"]
    assert report["status"] == "NOT_CLEAN"


def test_drift_now_passing_skipped_and_not_collected(tmp_path: Path) -> None:
    body = "import pytest\ndef test_ok():\n    pass\n@pytest.mark.skip\ndef test_s():\n    pass\n"
    run = _pytest(tmp_path, {"test_a.py": body})
    manifest = _manifest(
        tmp_path, ["test_a.py::test_ok", "test_a.py::test_s", "test_a.py::test_gone"]
    )
    report, _ = _build(run, manifest)
    assert run.rc == 0
    assert report["status"] == "NOT_CLEAN"
    assert report["nodes"] == {
        "test_a.py::test_gone": "expected_not_collected",
        "test_a.py::test_ok": "expected_now_passing",
        "test_a.py::test_s": "expected_skipped",
    }


def test_unexpected_skip_is_listed_but_clean(tmp_path: Path) -> None:
    body = "import pytest\n@pytest.mark.skip\ndef test_s():\n    pass\n" + PASS_FILE
    run = _pytest(tmp_path, {"test_a.py": body})
    report, lines = _build(run, _manifest(tmp_path, []))
    assert report["status"] == "PASS"
    assert _decoded_listing(lines) == [("skipped", "test_a.py::test_s")]


# --- incomplete or abnormal sessions (F-CIR1) ---------------------------------------


def test_interrupt_between_tests_after_expected_failure_is_unknown(tmp_path: Path) -> None:
    """Codex probe: the first test fails as expected, then KeyboardInterrupt is raised
    between tests. The JUnit report is internally consistent (one failure) but two
    collected tests never ran."""

    files = {
        "conftest.py": (
            "def pytest_runtest_logfinish(nodeid, location):\n    raise KeyboardInterrupt\n"
        ),
        "test_probe.py": (
            "def test_a():\n    assert False\ndef test_b():\n    pass\ndef test_c():\n    pass\n"
        ),
    }
    run = _pytest(tmp_path, files)
    assert run.rc == 2
    report, _ = _build(run, _manifest(tmp_path, ["test_probe.py::test_a"]))
    assert report["status"] == "UNKNOWN"
    assert report["reason"] == "pytest exit status 2 (not a completed run)"
    assert report["pytest_session"]["testscollected"] == 3


def test_interrupt_inside_a_test_is_unknown(tmp_path: Path) -> None:
    body = "def test_a():\n    raise KeyboardInterrupt\ndef test_b():\n    pass\n"
    run = _pytest(tmp_path, {"test_a.py": body})
    assert run.rc == 2
    report, _ = _build(run, _manifest(tmp_path, []))
    assert (report["status"], report["reason"]) == (
        "UNKNOWN",
        "pytest exit status 2 (not a completed run)",
    )


def test_no_tests_collected_is_unknown(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": "x = 1\n"})
    assert run.rc == 5
    report, _ = _build(run, _manifest(tmp_path, []))
    assert report["reason"] == "pytest exit status 5 (not a completed run)"


def test_collection_error_is_unknown(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": PASS_FILE, "test_b.py": "import no_such_module_xyz\n"})
    report, _ = _build(run, _manifest(tmp_path, []))
    assert report["status"] == "UNKNOWN"
    assert report["reason"] in (
        "collection error or interruption",
        "pytest exit status 2 (not a completed run)",
    )


def test_report_missing_collected_tests_is_unknown(tmp_path: Path) -> None:
    """Exit status 1 and a consistent report, but fewer tests than were collected."""

    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    report, _ = _build(
        run, _manifest(tmp_path, ["test_a.py::test_bad"]), session=_session(tmp_path, 1, 3, 1)
    )
    assert report["status"] == "UNKNOWN"
    assert report["reason"] == "report has 1 tests, session collected 3 (incomplete run)"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "pytest session record missing"),
        (b"{", "pytest session record malformed"),
        (b"[]", "pytest session record malformed"),
        (b'{"exitstatus": 1, "testscollected": 1}', "pytest session record malformed"),
        (
            b'{"exitstatus": "1", "testscollected": 1, "testsfailed": 1}',
            "pytest session record malformed",
        ),
        (
            b'{"exitstatus": true, "testscollected": 1, "testsfailed": 1}',
            "pytest session record malformed",
        ),
    ],
)
def test_session_record_missing_or_malformed_is_unknown(
    tmp_path: Path, content: bytes | None, reason: str
) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE}, plugin=False)
    assert not run.session.exists()
    session = tmp_path / "bad-session.json"
    if content is not None:
        session.write_bytes(content)
    report, _ = _build(run, _manifest(tmp_path, ["test_a.py::test_bad"]), session=session)
    assert (report["status"], report["reason"]) == ("UNKNOWN", reason)


@pytest.mark.parametrize(("outcome", "exitstatus"), [("success", 1), ("failure", 0)])
def test_exit_status_disagreeing_with_step_outcome_is_unknown(
    tmp_path: Path, outcome: str, exitstatus: int
) -> None:
    run = _pytest(tmp_path, {"test_a.py": PASS_FILE})
    report, _ = _build(
        run, _manifest(tmp_path, []), outcome=outcome, session=_session(tmp_path, exitstatus, 1, 0)
    )
    assert report["reason"] == f"pytest exit status {exitstatus} disagrees with step outcome"


def test_exit_one_with_no_raw_red_is_unknown_even_with_manifest_drift(tmp_path: Path) -> None:
    """Codex: the exit/report check must use raw outcomes, so drift (an expected node
    now passing) cannot mask an exit 1 that the report does not explain."""

    run = _pytest(tmp_path, {"test_a.py": PASS_FILE})
    report, _ = _build(
        run,
        _manifest(tmp_path, ["test_a.py::test_ok"]),
        outcome="failure",
        session=_session(tmp_path, 1, 1, 0),
    )
    assert (report["status"], report["reason"]) == (
        "UNKNOWN",
        "pytest failed but the report shows no failure or error",
    )


def test_exit_zero_with_raw_red_is_unknown(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    report, _ = _build(
        run,
        _manifest(tmp_path, ["test_a.py::test_bad"]),
        outcome="success",
        session=_session(tmp_path, 0, 1, 0),
    )
    assert report["reason"] == "pytest succeeded but the report shows failures or errors"


@pytest.mark.parametrize("outcome", ["", "skipped", "cancelled"])
def test_pytest_step_not_run_is_unknown_even_with_a_stale_report(
    tmp_path: Path, outcome: str
) -> None:
    run = _pytest(tmp_path, {"test_a.py": PASS_FILE})
    report, _ = _build(run, _manifest(tmp_path, []), outcome=outcome)
    assert report["status"] == "UNKNOWN"
    assert report["reason"].startswith("pytest step outcome")


def test_missing_report_is_unknown(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    run.junit.unlink()
    report, lines = _build(run, _manifest(tmp_path, []))
    assert (report["status"], report["reason"]) == ("UNKNOWN", "JUnit report missing")
    assert lines[0].startswith("::notice title=ci-test-report summary::status=UNKNOWN")


@pytest.mark.parametrize(
    ("label", "content", "reason"),
    [
        ("truncated", b"<testsuites><testsuite tests='1'><testcase", "JUnit report malformed"),
        ("empty_suite", b"<testsuite tests='0'/>", "JUnit report contains no test cases"),
        (
            "totals_disagree",
            b"<testsuite tests='2' failures='0' errors='0' skipped='0'>"
            b"<testcase classname='t' name='a' file='t.py'/></testsuite>",
            "JUnit testsuite totals disagree with test cases",
        ),
    ],
)
def test_malformed_or_inconsistent_report_is_unknown(
    tmp_path: Path, label: str, content: bytes, reason: str
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_bytes(content)
    run = Run(junit=junit, session=_session(tmp_path, 1, 1, 1), rc=1)
    report, _ = _build(run, _manifest(tmp_path, []))
    assert (report["status"], report["reason"]) == ("UNKNOWN", reason), label


# --- node ids, escaping, chunking ------------------------------------------------


def _decode(text: str) -> str:
    return text.replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")


def _decoded_listing(lines: list[str]) -> list[tuple[str, str]]:
    """What a reader of the annotations API gets back: decode each list notice and
    parse it with the reporter's own inverse."""

    out: list[tuple[str, str]] = []
    for line in lines[1:]:
        title = line.split("::", 2)[1]
        if title.startswith("notice title=ci-test-report list") and "TRUNCATED" not in title:
            out += r.parse_listing(_decode(line.split("::", 2)[2]))
    return out


def _expected_listing(report: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted((cls, node) for node, cls in report["nodes"].items() if cls != "passed")


def test_class_and_escaped_parameter_ids(tmp_path: Path) -> None:
    body = (
        "import pytest\nclass TestK:\n"
        "    @pytest.mark.parametrize('v', ['a,b', 'c:d', '50%', 'x\\ny'])\n"
        "    def test_p(self, v):\n        assert False\n"
    )
    run = _pytest(tmp_path, {"test_a.py": body})
    report, lines = _build(run, _manifest(tmp_path, []))
    assert all(n.startswith("test_a.py::TestK::test_p[") for n in report["nodes"])
    assert len(report["nodes"]) == 4
    for line in lines:
        assert "\n" not in line and "\r" not in line
        title = line.split("::", 2)[1].split("=", 1)[1]
        assert "," not in title and ":" not in title
    assert "%25" in lines[1]  # '%' escaped in the data
    assert sorted(_decoded_listing(lines)) == _expected_listing(report)


def test_listing_round_trips_within_the_measured_api_limit(tmp_path: Path) -> None:
    """Real limits (no monkeypatch): long node ids across several files and classes;
    every chunk's decoded message stays under the measured 4096-character API limit,
    and the decoded chunks give back exactly the non-passing nodes."""

    long = "x" * 120
    files = {
        f"test_{i}_{long}.py": (
            "import pytest\n@pytest.mark.parametrize('v', range(25))\n"
            f"def test_{long}(v):\n    assert False\n"
        )
        for i in range(4)
    }
    run = _pytest(tmp_path, files)
    report, lines = _build(run, _manifest(tmp_path, []))
    assert len(report["nodes"]) == 100
    list_lines = [ln for ln in lines if "ci-test-report list " in ln.split("::", 2)[1]]
    assert len(list_lines) > 1
    for line in list_lines:
        assert len(_decode(line.split("::", 2)[2]).encode()) < 4096
    assert "TRUNCATED" not in " ".join(lines)
    assert sorted(_decoded_listing(lines)) == _expected_listing(report)
    titles = [ln.split("::", 2)[1] for ln in list_lines]
    assert sum(int(t.rsplit(" ", 1)[1]) for t in titles) == 100
    assert "listed=100/100" in lines[0]


def test_listing_chunks_and_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "CHUNK_CHARS", 200)
    monkeypatch.setattr(r, "MAX_CHUNKS", 2)
    body = (
        "import pytest\n@pytest.mark.parametrize('i', range(30))\n"
        "def test_f(i):\n    assert False\n"
    )
    run = _pytest(tmp_path, {"test_a.py": body})
    report, lines = _build(run, _manifest(tmp_path, []))
    assert len(report["nodes"]) == 30  # the JSON report is complete
    titles = [line.split("::", 2)[1] for line in lines]
    assert titles[1].startswith("notice title=ci-test-report list 1/2 nodes ")
    assert titles[2].startswith("notice title=ci-test-report list 2/2 nodes ")
    assert titles[-1] == "notice title=ci-test-report list TRUNCATED"
    shown = len(_decoded_listing(lines))
    assert shown < 30
    assert shown == sum(int(t.rsplit(" ", 1)[1]) for t in titles[1:3])
    assert f"listed={shown}/30" in lines[0]
    for line in lines[1:3]:
        assert len(_decode(line.split("::", 2)[2]).encode()) <= 200


# --- manifest -------------------------------------------------------------------


def test_manifest_source_and_scope_are_reported(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    report, lines = _build(run, _manifest(tmp_path, ["test_a.py::test_bad"]))
    assert (report["manifest_source_ref"], report["manifest_scope"]) == (SHA, "synthetic")
    assert report["manifest_entries"] == 1
    assert f"manifest_source_ref={SHA}" in lines[0] and "manifest_entries=1" in lines[0]


@pytest.mark.parametrize(
    "override",
    [
        {"entries": {}},
        {"entries": None},
        {"entries": [["a::b", "r"]]},
        {"entries": [{"node": "a::b"}]},
        {"entries": [{"node": "tests/", "rationale": "r"}]},
        {"entries": [{"node": "a::b", "rationale": " "}]},
        {"entries": [{"node": 7, "rationale": "r"}]},
        {"entries": [{"node": "a::b", "rationale": "r"}, {"node": "a::b", "rationale": "r"}]},
        {"source_ref": "3ae9404"},
        {"source_ref": SHA.upper()},
        {"source_ref": None},
        {"scope": ""},
        {"scope": None},
        {"extra": 1},
    ],
)
def test_bad_manifest_is_unknown_never_permissive(tmp_path: Path, override: dict[str, Any]) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    report, _ = _build(run, _manifest(tmp_path, [], **override))
    assert report["status"] == "UNKNOWN"
    assert report["reason"].startswith("manifest unusable")


def test_manifest_not_a_json_object_is_unknown(tmp_path: Path) -> None:
    run = _pytest(tmp_path, {"test_a.py": FAIL_FILE})
    path = tmp_path / "m.json"
    for content in ("null", "[]", "not json"):
        path.write_text(content)
        report, _ = _build(run, path)
        assert report["reason"].startswith("manifest unusable"), content


# --- plugin and CLI ---------------------------------------------------------------


def test_plugin_writes_nothing_without_its_environment_variable(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "CI_PYTEST"))}
    env["PYTHONPATH"] = str(PLUGIN_DIR)
    (tmp_path / "test_a.py").write_text(PASS_FILE)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "ci_pytest_session"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".json") == []


def test_plugin_does_not_change_the_exit_status(tmp_path: Path) -> None:
    for name in ("with", "without"):
        (tmp_path / name).mkdir()
    for body, expected in ((PASS_FILE, 0), (FAIL_FILE, 1)):
        with_plugin = _pytest(tmp_path / "with", {"test_a.py": body})
        without = _pytest(tmp_path / "without", {"test_a.py": body}, plugin=False)
        assert with_plugin.rc == without.rc == expected


def test_cli_always_exits_zero_and_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "evidence" / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(r.__file__)),
            "--junit",
            str(tmp_path / "absent.xml"),
            "--session",
            str(tmp_path / "absent.json"),
            "--manifest",
            str(_manifest(tmp_path, [])),
            "--out",
            str(out),
            "--pytest-outcome",
            "failure",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(out.read_text())["status"] == "UNKNOWN"
    assert proc.stdout.startswith("::notice title=ci-test-report summary::status=UNKNOWN")
