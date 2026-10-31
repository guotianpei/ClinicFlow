"""CI test-result reporter and expected-red classifier (diagnostic only).

Reads the JUnit XML that the gated pytest step wrote and produces:

1. a complete machine-readable report (JSON) saved with the uploaded evidence;
2. GitHub workflow notices carrying the run status, counts, hashes, and the
   complete list of non-passing / drifted nodes, split into numbered chunks so a
   reader of the check-run annotations can verify the list is complete.

It NEVER changes the job outcome: it always exits 0. The gate is still pytest's
own exit status. The classification is a reading aid, not an acceptance.

Run status (one of):
- ``UNKNOWN``  the result cannot be classified: pytest did not run or was
  cancelled; the session record (ci/ci_pytest_session.py) is missing or
  malformed; pytest's own exit status is not 0 or 1 (interrupted, internal or
  usage error, nothing collected); the exit status disagrees with the step
  outcome or with the raw report (exit 1 with no failure/error, exit 0 with
  one); the report is missing, malformed or empty; its totals disagree with its
  test cases; it does not contain every collected test; or a collection error
  occurred;
- ``PASS``     every collected node passed or was skipped and no manifest entry
  drifted;
- ``EXPECTED_RED_ONLY``  every failure is a reviewed manifest entry, there are no
  errors, and no manifest entry drifted;
- ``NOT_CLEAN``  anything else: an unclassified failure, any error (setup,
  teardown), a manifest entry now passing, skipped or not collected.

Node classes: ``passed``, ``skipped``, ``expected_failed``, ``unclassified_failed``,
``error`` (setup/teardown error; never expected), ``expected_now_passing``,
``expected_skipped``, ``expected_not_collected``.

The manifest (JSON) is pinned and reviewed by hand: ``{"source_ref": str,
"scope": str, "entries": [{"node": str, "rationale": str}, ...]}``. Nothing is
ever added to it automatically. ``source_ref`` (a 40-hex commit) and ``scope``
are validated and shown in the report and the summary notice; any malformed
shape makes the run UNKNOWN. Applicability: the entries were reviewed at
``source_ref``; a reader must confirm the classified head descends from it (the
CI checkout is shallow, so this script does not check ancestry). An empty
manifest classifies every failure as unclassified (conservative).

Stdlib only; run with ``python -I``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "haloflow.ci-test-report/v1"
CHUNK_CHARS = 6000
MAX_CHUNKS = 8

_PRIORITY = {"error": 3, "failure": 2, "skipped": 1}


class ReportUnusable(Exception):
    """The JUnit report cannot be classified; the message is a fixed reason."""


@dataclass
class Parsed:
    outcomes: dict[str, str] = field(default_factory=dict)
    multiple: list[str] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _node_id(case: ET.Element) -> str:
    """pytest node id from an xunit1 testcase (``file`` + ``classname`` + ``name``)."""

    name = case.get("name", "")
    classname = case.get("classname", "")
    file = case.get("file")
    if not file:
        return f"{classname}::{name}" if classname else name
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file
    rest = classname[len(module) + 1 :] if classname.startswith(module + ".") else ""
    parts = [file, *([p for p in rest.split(".") if p] if rest else []), name]
    return "::".join(parts)


def parse_junit(data: bytes) -> Parsed:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ReportUnusable("JUnit report malformed") from error
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ReportUnusable("JUnit report has no testsuite")
    parsed = Parsed()
    declared: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for suite in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            try:
                declared[key] += int(suite.get(key, "0"))
            except ValueError as error:
                raise ReportUnusable("JUnit testsuite totals malformed") from error
        for case in suite.iter("testcase"):
            node = _node_id(case)
            tags = {child.tag for child in case} & set(_PRIORITY)
            for tag in tags:
                seen[{"failure": "failures", "error": "errors", "skipped": "skipped"}[tag]] += 1
            seen["tests"] += 1
            errors = [c for c in case if c.tag == "error"]
            if any((c.get("message") or "").startswith("collection failure") for c in errors):
                parsed.collection_errors.append(node)
            outcome = max(tags, key=_PRIORITY.__getitem__) if tags else "passed"
            if node in parsed.outcomes or len(tags) > 1:
                parsed.multiple.append(node)
                previous = parsed.outcomes.get(node, "passed")
                if _PRIORITY.get(previous, 0) > _PRIORITY.get(outcome, 0):
                    outcome = previous
            parsed.outcomes[node] = outcome
    if seen["tests"] == 0:
        raise ReportUnusable("JUnit report contains no test cases")
    if dict(declared) != {k: seen[k] for k in declared}:
        raise ReportUnusable("JUnit testsuite totals disagree with test cases")
    return parsed


@dataclass
class Manifest:
    source_ref: str
    scope: str
    entries: dict[str, str]


_HEX = set("0123456789abcdef")


def load_manifest(data: bytes) -> Manifest:
    raw = json.loads(data)
    if not isinstance(raw, dict) or set(raw) != {"source_ref", "scope", "entries"}:
        raise ValueError("manifest must have exactly source_ref, scope, entries")
    source_ref, scope = raw["source_ref"], raw["scope"]
    if not isinstance(source_ref, str) or len(source_ref) != 40 or not set(source_ref) <= _HEX:
        raise ValueError("manifest source_ref must be a 40-hex lowercase commit")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("manifest scope must be a non-empty string")
    if not isinstance(raw["entries"], list):
        raise ValueError("manifest entries must be a list")
    entries: dict[str, str] = {}
    for entry in raw["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"node", "rationale"}:
            raise ValueError("manifest entry must have exactly node, rationale")
        node, rationale = entry["node"], entry["rationale"]
        if not isinstance(node, str) or not node or "::" not in node:
            raise ValueError("manifest node must be a full pytest node id")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("manifest entry needs a rationale")
        if node in entries:
            raise ValueError("duplicate manifest node")
        entries[node] = rationale
    return Manifest(source_ref=source_ref, scope=scope, entries=entries)


def load_session(data: bytes) -> dict[str, int]:
    raw = json.loads(data)
    keys = {"exitstatus", "testscollected", "testsfailed"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("session record must have exactly " + ", ".join(sorted(keys)))
    for key in keys:
        if type(raw[key]) is not int or raw[key] < 0:
            raise ValueError("session record values must be non-negative integers")
    return {key: raw[key] for key in keys}


def classify(parsed: Parsed, expected: dict[str, str]) -> dict[str, str]:
    classes: dict[str, str] = {}
    for node, outcome in parsed.outcomes.items():
        if outcome == "error":
            classes[node] = "error"
        elif node in expected:
            classes[node] = {
                "failure": "expected_failed",
                "passed": "expected_now_passing",
                "skipped": "expected_skipped",
            }[outcome]
        elif outcome == "failure":
            classes[node] = "unclassified_failed"
        else:
            classes[node] = outcome
    for node in expected:
        if node not in parsed.outcomes:
            classes[node] = "expected_not_collected"
    return classes


def run_status(classes: dict[str, str]) -> str:
    values = set(classes.values())
    if values <= {"passed", "skipped"}:
        return "PASS"
    if values <= {"passed", "skipped", "expected_failed"}:
        return "EXPECTED_RED_ONLY"
    return "NOT_CLEAN"


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def notice(title: str, message: str) -> str:
    return f"::notice title={_escape_property(title)}::{_escape_data(message)}"


def build(
    *,
    junit: Path,
    session: Path,
    manifest: Path,
    pytest_outcome: str,
    source_sha: str,
    head_sha: str,
) -> tuple[dict[str, Any], list[str]]:
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "source_sha": source_sha,
        "head_sha": head_sha,
        "pytest_outcome": pytest_outcome,
        "pytest_session": None,
        "status": "UNKNOWN",
        "reason": None,
        "junit_sha256": None,
        "manifest_sha256": None,
        "manifest_source_ref": None,
        "manifest_scope": None,
        "manifest_entries": None,
        "counts": {},
        "nodes": {},
        "multiple_outcome_nodes": [],
    }

    def unknown(reason: str) -> tuple[dict[str, Any], list[str]]:
        report["reason"] = reason
        return report, _notices(report)

    try:
        manifest_bytes = manifest.read_bytes()
        report["manifest_sha256"] = _sha256(manifest_bytes)
        loaded = load_manifest(manifest_bytes)
    except (OSError, ValueError, TypeError) as error:
        return unknown(f"manifest unusable: {type(error).__name__}")
    report["manifest_source_ref"] = loaded.source_ref
    report["manifest_scope"] = loaded.scope
    report["manifest_entries"] = len(loaded.entries)
    if pytest_outcome not in ("success", "failure"):
        return unknown(f"pytest step outcome {pytest_outcome or 'absent'}")
    try:
        record = load_session(session.read_bytes())
    except OSError:
        return unknown("pytest session record missing")
    except (ValueError, TypeError):
        return unknown("pytest session record malformed")
    report["pytest_session"] = record
    exitstatus = record["exitstatus"]
    if exitstatus not in (0, 1):
        return unknown(f"pytest exit status {exitstatus} (not a completed run)")
    if (exitstatus == 0) != (pytest_outcome == "success"):
        return unknown(f"pytest exit status {exitstatus} disagrees with step outcome")
    try:
        junit_bytes = junit.read_bytes()
    except OSError:
        return unknown("JUnit report missing")
    report["junit_sha256"] = _sha256(junit_bytes)
    try:
        parsed = parse_junit(junit_bytes)
    except ReportUnusable as error:
        return unknown(str(error))
    classes = classify(parsed, loaded.entries)
    report["nodes"] = dict(sorted(classes.items()))
    report["multiple_outcome_nodes"] = sorted(set(parsed.multiple))
    report["counts"] = dict(sorted(Counter(classes.values()).items()))
    if parsed.collection_errors:
        return unknown("collection error or interruption")
    if len(parsed.outcomes) != record["testscollected"]:
        return unknown(
            f"report has {len(parsed.outcomes)} tests, session collected "
            f"{record['testscollected']} (incomplete run)"
        )
    raw_red = any(outcome in ("failure", "error") for outcome in parsed.outcomes.values())
    if exitstatus == 1 and not raw_red:
        return unknown("pytest failed but the report shows no failure or error")
    if exitstatus == 0 and raw_red:
        return unknown("pytest succeeded but the report shows failures or errors")
    report["status"] = run_status(classes)
    return report, _notices(report)


def _notices(report: dict[str, Any]) -> list[str]:
    listed = [f"{cls} {node}" for node, cls in report["nodes"].items() if cls != "passed"]
    chunks: list[list[str]] = [[]] if listed else []
    size = 0
    shown = 0
    for line in listed:
        if size + len(line) + 1 > CHUNK_CHARS and chunks[-1]:
            if len(chunks) == MAX_CHUNKS:
                break
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1
        shown += 1
    counts = " ".join(f"{k}={v}" for k, v in report["counts"].items()) or "none"
    summary = (
        f"status={report['status']} reason={report['reason']} "
        f"source={report['source_sha']} head={report['head_sha']} "
        f"pytest={report['pytest_outcome']} session={report['pytest_session']} "
        f"manifest_source_ref={report['manifest_source_ref']} "
        f"manifest_entries={report['manifest_entries']} counts: {counts} "
        f"listed={shown}/{len(listed)} chunks={len(chunks)} "
        f"junit_sha256={report['junit_sha256']} manifest_sha256={report['manifest_sha256']}"
    )
    out = [notice("ci-test-report summary", summary)]
    for index, chunk in enumerate(chunks, start=1):
        out.append(notice(f"ci-test-report list {index}/{len(chunks)}", "\n".join(chunk)))
    if shown < len(listed):
        out.append(
            notice(
                "ci-test-report list TRUNCATED",
                f"{len(listed) - shown} nodes not listed; the uploaded JSON is complete",
            )
        )
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pytest-outcome", default="")
    parser.add_argument("--source-sha", default="")
    parser.add_argument("--head-sha", default="")
    args = parser.parse_args(argv)
    report, lines = build(
        junit=args.junit,
        session=args.session,
        manifest=args.manifest,
        pytest_outcome=args.pytest_outcome,
        source_sha=args.source_sha,
        head_sha=args.head_sha,
    )
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    except OSError:
        lines.append(notice("ci-test-report", "could not write the JSON report"))
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
