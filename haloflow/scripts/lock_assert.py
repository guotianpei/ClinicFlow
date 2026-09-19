#!/usr/bin/env python3
"""CP2-0 D-03 -- the premises a verification run must satisfy, and its manifest.

Runs with the VERIFIED environment's interpreter, because several checks ask that
environment about itself. Every failure is collected and printed together: one
run should report everything wrong with it, not the first thing.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--main", required=True)
    # Provenance only. Never read: the snapshot is what was consumed.
    parser.add_argument("--bootstrap-origin", default="")
    parser.add_argument("--main-origin", default="")
    parser.add_argument("--project", required=True)
    parser.add_argument("--envdir", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--module-dir", required=True)
    parser.add_argument("--expected-source", default="")
    parser.add_argument("--source-revision", default="")
    args = parser.parse_args()

    sys.path.insert(0, args.module_dir)
    from lockfile import (  # noqa: E402
        LockError,
        check_declarations_against_lock,
        coverage_problems,
        file_digest,
        lock_union,
        parse_lock,
        parse_report,
        read_installed,
        source_manifest,
        source_state,
        write_manifest,
    )

    evidence = Path(args.evidence)
    try:
        bootstrap = parse_lock(args.bootstrap)
        main_lock = parse_lock(args.main)
        union = lock_union(bootstrap, main_lock)
        reports = {}
        report_versions = {}
        for name in ("report-bootstrap.json", "report-main.json"):
            report_versions[name], reports[name] = parse_report(evidence / name)
    except LockError as error:
        print(f"FAIL: {error}")
        return 1

    installed = read_installed(evidence / "installed.json", drop="haloflow")
    project_version = None
    for entry in json.loads((evidence / "installed.json").read_text()):
        if entry["name"].lower() == "haloflow":
            project_version = entry["version"]

    problems: list[str] = []
    if project_version is None:
        problems.append("the project itself is not installed")

    # (a) coverage, in BOTH directions, plus versions and artifact hashes.
    problems.extend(coverage_problems(union, reports, installed))

    def pip(*argv):
        return subprocess.run(
            [f"{args.envdir}/bin/python", "-m", "pip", *argv],
            capture_output=True,
            text=True,
        )

    # (b) dependency satisfaction WITHOUT re-resolving. This is what catches a
    #     distribution missing from the lock AND from the environment, which no
    #     set comparison between the two can catch.
    check = pip("check")
    if check.returncode != 0:
        problems.append(f"pip check: {check.stdout.strip() or check.stderr.strip()}")

    # (c) the declared dev extra is satisfied too, again without re-resolving.
    extra = subprocess.run(
        [
            f"{args.envdir}/bin/python",
            "-c",
            "import importlib.metadata as m,packaging.requirements as r;"
            "reqs=m.distribution('haloflow').requires or [];"
            "missing=[]\n"
            "for spec in reqs:\n"
            "    req=r.Requirement(spec)\n"
            "    if req.marker and not req.marker.evaluate({'extra':'dev'}): continue\n"
            "    try: v=m.version(req.name)\n"
            "    except m.PackageNotFoundError: missing.append(req.name); continue\n"
            "    if req.specifier and not req.specifier.contains(v, prereleases=True):\n"
            "        missing.append(f'{req.name} {v} fails {req.specifier}')\n"
            "print('|'.join(missing))",
        ],
        capture_output=True,
        text=True,
    )
    if extra.returncode != 0:
        problems.append(f"extra metadata check failed to run: {extra.stderr.strip()}")
    elif extra.stdout.strip():
        problems.append(f"declared requirements unsatisfied: {extra.stdout.strip()}")

    # (d) WHERE it was installed from. A distribution called haloflow being present
    #     says nothing about which tree produced it. This is a PATH question and is
    #     deliberately kept separate from the content question in (e).
    origin = None
    for path in Path(f"{args.envdir}/lib").rglob("haloflow-*.dist-info/direct_url.json"):
        origin = json.loads(path.read_text())
        break
    resolved_project = os.path.realpath(args.project)
    editable = False
    if origin is None:
        problems.append("no direct_url.json: cannot establish which tree was installed")
    else:
        url = origin.get("url", "")
        parsed = urlparse(url)
        actual = (
            os.path.realpath(unquote(parsed.path)) if parsed.scheme == "file" else None
        )
        if actual != resolved_project:
            problems.append(f"installed from {url!r}, expected {resolved_project!r}")
        editable = bool(origin.get("dir_info", {}).get("editable"))
        if not editable:
            problems.append("the project is installed, but not as an editable install")

    # (e-pre) THE DECLARATIONS AGAINST THE LOCK.
    #     Be exact about what this adds over (b), because an earlier draft of mine
    #     was not. MEASURED, same tree, same environment:
    #
    #       a new UNCONDITIONAL runtime declaration  -> pip check DOES catch it:
    #         "haloflow 0.1.0 requires orjson, which is not installed."  exit 1
    #       the same addition in the dev EXTRA only  -> pip check says
    #         "No broken requirements found."                           exit 0
    #
    #     Because step 5 reinstalls the project editable, its dist-info carries the
    #     current Requires-Dist, so pip check is NOT blind to runtime dependencies.
    #     Extras are conditional, so it is blind to those. This check covers the
    #     selected extra, and states declared-versus-lock consistency directly
    #     rather than inferring it from the environment.
    #
    #     Deliberately NOT done here: regenerating the lock, and failing merely
    #     because a newer version exists upstream. Consistency and update policy
    #     are different questions.
    declared = check_declarations_against_lock(args.project, union, extra="dev")
    problems.extend(declared)

    # (e) WHAT was in the tree. A framed manifest over every file except the
    #     recorded exclusions -- not only *.py, because pyproject.toml, the alembic
    #     configuration, the SQL fixtures and the test data all change what is built
    #     and what is tested. Asserted only when an expected value was supplied;
    #     otherwise recorded and explicitly NOT asserted, because printing a digest
    #     nothing is compared against is not a check.
    source = source_manifest(args.project)
    expected = args.expected_source.strip()
    source_asserted = bool(expected)
    if source_asserted and source["sha256"] != expected:
        problems.append(
            f"source content sha256 {source['sha256']} does not match the expected "
            f"{expected}"
        )

    coverage_ok = not problems
    write_manifest(
        evidence,
        {
            "target": args.target,
            # `path` records where the lock came from; `snapshot` is the copy kept
            # inside this evidence directory. A later comparison reads the
            # snapshot, so it does not depend on a path outside the evidence that
            # may since have changed or disappeared.
            "locks": {
                "bootstrap": {
                    "consumed": os.path.realpath(args.bootstrap),
                    "origin_path": args.bootstrap_origin or None,
                    "snapshot": f"locks/{args.target}.bootstrap.txt",
                    "sha256": file_digest(args.bootstrap),
                    "count": len(bootstrap),
                },
                "main": {
                    "consumed": os.path.realpath(args.main),
                    "origin_path": args.main_origin or None,
                    "snapshot": f"locks/{args.target}.txt",
                    "sha256": file_digest(args.main),
                    "count": len(main_lock),
                },
                "union_count": len(union),
            },
            "reports": {
                name: {
                    "sha256": file_digest(evidence / name),
                    "report_version": report_versions[name],
                    "entries": len(reports[name]),
                }
                for name in reports
            },
            "installed": {
                "count": len(installed),
                "set": sorted(installed.items()),
                "project_version": project_version,
                "editable": editable,
            },
            "artifacts": sorted(
                (name, version, digest)
                for entries in reports.values()
                for name, (version, digest) in entries.items()
            ),
            "source": {
                **source,
                "asserted": source_asserted,
                "expected": expected or None,
                "revision": args.source_revision or None,
            },
            "project_path": resolved_project,
            "coverage_ok": coverage_ok,
            "problems": problems,
        },
    )

    if problems:
        for line in problems:
            print("FAIL:", line)
        print(f"   manifest written with coverage_ok=false ({len(problems)} problems)")
        return 1

    print(f"   OK: {len(union)} locked distributions, covered in both directions:")
    print("       every locked name appears in exactly one install report,")
    print("       every reported name is locked, versions match, every installed")
    print("       artifact hash is among the pinned hashes for that distribution;")
    print(f"       installed set is exactly those {len(installed)} names;")
    print("       pip check clean; declared requirements satisfied;")
    print(f"       project {project_version} installed editable from {resolved_project};")
    print(f"       source content sha256 {source['sha256']} over {source['files']} files")
    print(f"       -- {source_state({**source, 'asserted': source_asserted, 'revision': args.source_revision})}")
    if not source_asserted:
        print("       This run verifies DEPENDENCIES. It says nothing about which")
        print("       reviewed source tree it was pointed at.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
