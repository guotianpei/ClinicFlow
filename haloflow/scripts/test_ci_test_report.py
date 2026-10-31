"""Synthetic controls for scripts/ci_test_report.py (Codex review, 2026-10-31 item 5).

Runs outside the CI gate scope by design (the gated pytest scope is unchanged):
    python -I -m pytest -q -p no:cacheprovider scripts/test_ci_test_report.py
Real pytest JUnit reports are generated from tiny throwaway test files in
tmp_path, so the parser is exercised on pytest's own xunit1 output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_test_report as r  # noqa: E402

PASS_FILE = "def test_ok():\n    pass\n"


def _junit(tmp_path: Path, files: dict[str, str]) -> tuple[Path, int]:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    out = tmp_path / "junit.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={out}",
            *files,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return out, proc.returncode


def _manifest(tmp_path: Path, nodes: list[str]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "source_ref": "test",
                "scope": "synthetic",
                "entries": [{"node": n, "rationale": "synthetic"} for n in nodes],
            }
        )
    )
    return path


def _run(
    tmp_path: Path, junit: Path, manifest: Path, outcome: str
) -> tuple[dict[str, Any], list[str]]:
    return r.build(
        junit=junit, manifest=manifest, pytest_outcome=outcome, source_sha="s", head_sha="h"
    )


def _outcome(rc: int) -> str:
    return "success" if rc == 0 else "failure"


def test_all_pass(tmp_path: Path) -> None:
    junit, rc = _junit(tmp_path, {"test_a.py": PASS_FILE})
    report, lines = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert report["status"] == "PASS"
    assert report["nodes"] == {"test_a.py::test_ok": "passed"}
    assert "chunks=0" in lines[0]


def test_unclassified_assertion_failure_is_not_clean(tmp_path: Path) -> None:
    junit, rc = _junit(tmp_path, {"test_a.py": "def test_bad():\n    assert 1 == 2\n"})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert report["status"] == "NOT_CLEAN"
    assert report["nodes"] == {"test_a.py::test_bad": "unclassified_failed"}


def test_expected_failure_only(tmp_path: Path) -> None:
    junit, rc = _junit(tmp_path, {"test_a.py": PASS_FILE + "def test_bad():\n    assert 1 == 2\n"})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, ["test_a.py::test_bad"]), "failure")
    assert rc != 0
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
    junit, rc = _junit(tmp_path, {"test_a.py": fixture})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, ["test_a.py::test_x"]), _outcome(rc))
    assert report["status"] == "NOT_CLEAN"
    assert report["nodes"]["test_a.py::test_x"] == "error"


def test_failure_plus_teardown_error_counts_once_as_error(tmp_path: Path) -> None:
    body = (
        "import pytest\n@pytest.fixture\ndef f():\n    yield\n    raise RuntimeError('t')\n"
        "def test_x(f):\n    assert 1 == 2\n"
    )
    junit, rc = _junit(tmp_path, {"test_a.py": body})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert report["nodes"] == {"test_a.py::test_x": "error"}
    assert report["multiple_outcome_nodes"] == ["test_a.py::test_x"]
    assert report["status"] == "NOT_CLEAN"


def test_collection_error_is_unknown(tmp_path: Path) -> None:
    junit, rc = _junit(
        tmp_path, {"test_a.py": PASS_FILE, "test_b.py": "import no_such_module_xyz\n"}
    )
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert report["status"] == "UNKNOWN"
    assert report["reason"] == "collection error or interruption"


def test_missing_report_is_unknown(tmp_path: Path) -> None:
    report, lines = _run(tmp_path, tmp_path / "absent.xml", _manifest(tmp_path, []), "failure")
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
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, []), "failure")
    assert (report["status"], report["reason"]) == ("UNKNOWN", reason), label


@pytest.mark.parametrize("outcome", ["", "skipped", "cancelled"])
def test_pytest_not_run_is_unknown_even_with_a_stale_report(tmp_path: Path, outcome: str) -> None:
    junit, _ = _junit(tmp_path, {"test_a.py": PASS_FILE})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, []), outcome)
    assert report["status"] == "UNKNOWN"
    assert report["reason"].startswith("pytest step outcome")


def test_pytest_exit_disagreeing_with_report_is_unknown(tmp_path: Path) -> None:
    junit, _ = _junit(tmp_path, {"test_a.py": PASS_FILE})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, []), "failure")
    assert report["status"] == "UNKNOWN"
    assert report["reason"] == "pytest failed but the report shows no failure or error"


def test_pytest_success_with_a_failing_report_is_unknown(tmp_path: Path) -> None:
    junit, _ = _junit(tmp_path, {"test_a.py": "def test_bad():\n    assert 1 == 2\n"})
    report, _ = _run(tmp_path, junit, _manifest(tmp_path, ["test_a.py::test_bad"]), "success")
    assert report["status"] == "UNKNOWN"
    assert report["reason"] == "pytest succeeded but the report shows failures or errors"


def test_drift_now_passing_skipped_and_not_collected(tmp_path: Path) -> None:
    body = "import pytest\ndef test_ok():\n    pass\n@pytest.mark.skip\ndef test_s():\n    pass\n"
    junit, rc = _junit(tmp_path, {"test_a.py": body})
    manifest = _manifest(
        tmp_path, ["test_a.py::test_ok", "test_a.py::test_s", "test_a.py::test_gone"]
    )
    report, _ = _run(tmp_path, junit, manifest, _outcome(rc))
    assert report["status"] == "NOT_CLEAN"
    assert report["nodes"] == {
        "test_a.py::test_gone": "expected_not_collected",
        "test_a.py::test_ok": "expected_now_passing",
        "test_a.py::test_s": "expected_skipped",
    }


def test_unexpected_skip_is_reported_but_clean(tmp_path: Path) -> None:
    body = "import pytest\n@pytest.mark.skip\ndef test_s():\n    pass\n" + PASS_FILE
    junit, rc = _junit(tmp_path, {"test_a.py": body})
    report, lines = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert report["status"] == "PASS"
    assert "skipped test_a.py::test_s" in lines[1]


def test_class_and_escaped_parameter_ids(tmp_path: Path) -> None:
    body = (
        "import pytest\nclass TestK:\n"
        "    @pytest.mark.parametrize('v', ['a,b', 'c:d', '50%', 'x\\ny'])\n"
        "    def test_p(self, v):\n        assert False\n"
    )
    junit, rc = _junit(tmp_path, {"test_a.py": body})
    report, lines = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert all(n.startswith("test_a.py::TestK::test_p[") for n in report["nodes"])
    assert len(report["nodes"]) == 4
    for line in lines:
        assert "\n" not in line and "\r" not in line
        title = line.split("::", 2)[1]
        assert "," not in title.split("=", 1)[1] and ":" not in title.split("=", 1)[1]
    listing = lines[1].split("::", 2)[2]
    assert "%25" in listing  # '%' escaped; the decoded lines are recoverable
    decoded = listing.replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")
    assert len(decoded.split("\n")) == 4


def test_listing_chunks_and_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "CHUNK_CHARS", 200)
    monkeypatch.setattr(r, "MAX_CHUNKS", 2)
    body = (
        "import pytest\n@pytest.mark.parametrize('i', range(30))\n"
        "def test_f(i):\n    assert False\n"
    )
    junit, rc = _junit(tmp_path, {"test_a.py": body})
    report, lines = _run(tmp_path, junit, _manifest(tmp_path, []), _outcome(rc))
    assert len(report["nodes"]) == 30  # the JSON report is complete
    titles = [line.split("::", 2)[1] for line in lines]
    assert titles[1:3] == [
        "notice title=ci-test-report list 1/2",
        "notice title=ci-test-report list 2/2",
    ]
    assert titles[-1] == "notice title=ci-test-report list TRUNCATED"
    shown = sum(len(line.split("::", 2)[2].split("%0A")) for line in lines[1:3])
    assert f"listed={shown}/30" in lines[0]


@pytest.mark.parametrize(
    "manifest",
    [
        {"source_ref": "x", "scope": "y"},
        {"source_ref": "x", "scope": "y", "entries": [{"node": "a::b"}]},
        {"source_ref": "x", "scope": "y", "entries": [{"node": "tests/", "rationale": "r"}]},
        {"source_ref": "x", "scope": "y", "entries": [{"node": "a::b", "rationale": " "}]},
        {
            "source_ref": "x",
            "scope": "y",
            "entries": [{"node": "a::b", "rationale": "r"}, {"node": "a::b", "rationale": "r"}],
        },
    ],
)
def test_bad_manifest_is_unknown_never_permissive(tmp_path: Path, manifest: dict[str, Any]) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest))
    junit, rc = _junit(tmp_path, {"test_a.py": "def test_bad():\n    assert 1 == 2\n"})
    report, _ = _run(tmp_path, junit, path, _outcome(rc))
    assert report["status"] == "UNKNOWN"
    assert report["reason"].startswith("manifest unusable")


def test_cli_always_exits_zero_and_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "evidence" / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(r.__file__)),
            "--junit",
            str(tmp_path / "absent.xml"),
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
