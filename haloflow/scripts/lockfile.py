"""CP2-0 D-03 -- the single parser, and the single place the premises live.

WHY THIS MODULE EXISTS
----------------------
v3 carried two independent lock parsers: one in verify-lock.sh's sanity step and
one in its assertion step, with a third shape in generate-lock.sh. They disagreed
-- duplicate lines were rejected in one and silently collapsed in another. A
premise that is implemented twice is a premise that is checked once, at best.
Everything that reads a lock, a pip install report or an installed set now reads
it through here.

WHAT A "PREMISE" MEANS HERE
---------------------------
A comparison of two runs establishes nothing unless both runs are known to have
been the same experiment: same target, same input locks, complete coverage of
those locks. Two empty report sets compare equal. So every run writes a manifest
(`manifest()`), and any comparison refuses to report agreement until the
manifests agree on target and lock identity and each run independently passed its
own coverage checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# The two approved targets, and only these two. A same-host cp311 label must fail.
APPROVED_TARGETS = {
    "linux-x86_64-cp312": ("linux", "x86_64", "cp312"),
    "macos-arm64-cp312": ("macosx", "arm64", "cp312"),
}

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 2
SUPPORTED_REPORT_VERSIONS = {"1"}

_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s]+)"
    r"(?P<hashes>(?:\s+--hash=sha256:[^\s]+)+)\s*$"
)
_HASH = re.compile(r"--hash=sha256:([^\s]+)")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Content identity deliberately excludes build/run detritus and the local
# virtualenv. The rule is recorded in the manifest so an exclusion cannot be
# changed silently between runs.
EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
     ".ruff_cache", ".tox", "node_modules"}
)
EXCLUDED_NAMES = frozenset({".env", ".DS_Store"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


class LockError(Exception):
    """A premise did not hold. Never a warning."""


def canonical(name: str) -> str:
    """PEP 503 normalisation. `Mypy_Extensions` and `mypy-extensions` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: str | Path) -> dict[str, tuple[str, frozenset[str]]]:
    """Parse one lock file. Raises on anything that is not exactly well formed.

    Rejects, before any installation can happen:
      * a line that is not `name==version --hash=sha256:<64 hex>` (repeatable)
      * a sha256 that is not 64 lowercase hex characters
      * a canonical name appearing twice IN THIS FILE, whether or not the two
        lines agree -- a lock that says a thing twice is a lock someone edited
    """
    path = Path(path)
    if not path.exists():
        raise LockError(f"{path}: no such lock file")
    pins: dict[str, tuple[str, frozenset[str]]] = {}
    origin: dict[str, int] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            raise LockError(f"{path}:{number}: malformed lock line: {line!r}")
        digests = _HASH.findall(match.group("hashes"))
        for digest in digests:
            if not _SHA256.match(digest):
                raise LockError(
                    f"{path}:{number}: {digest!r} is not a 64-character lowercase "
                    f"hex sha256"
                )
        name = canonical(match.group("name"))
        if name in pins:
            raise LockError(
                f"{path}:{number}: {name} already appears at line {origin[name]}; "
                f"duplicate canonical names are refused whether or not they agree"
            )
        origin[name] = number
        pins[name] = (match.group("version"), frozenset(digests))
    if not pins:
        raise LockError(f"{path}: contains no pinned distributions")
    return pins


def lock_union(
    bootstrap: dict[str, tuple[str, frozenset[str]]],
    main: dict[str, tuple[str, frozenset[str]]],
) -> dict[str, tuple[str, frozenset[str]]]:
    """Merge the two locks. Overlap ACROSS files is allowed; disagreement is not.

    The overlap is the one place a divergence could hide: the same name pinned to
    two versions, or to the same version with different artifacts.
    """
    for name in sorted(set(bootstrap) & set(main)):
        if bootstrap[name] != main[name]:
            raise LockError(
                f"{name}: the bootstrap and main locks disagree "
                f"({bootstrap[name][0]} vs {main[name][0]}, or on hashes)"
            )
    return {**bootstrap, **main}


def file_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_report(path: str | Path) -> tuple[str, dict[str, tuple[str, str]]]:
    """Parse a `pip install --report` file into canonical (version, sha256).

    Raises on an unsupported report version -- the schema is what the coverage
    checks are written against, and a future version could move the fields this
    reads without any of them going missing.
    """
    path = Path(path)
    if not path.exists():
        raise LockError(f"{path}: install report was not written")
    data = json.loads(path.read_text())
    version = str(data.get("version"))
    if version not in SUPPORTED_REPORT_VERSIONS:
        raise LockError(
            f"{path}: install report version {version!r} is not one of "
            f"{sorted(SUPPORTED_REPORT_VERSIONS)}; the coverage checks are written "
            f"against the known schema and will not be applied to an unknown one"
        )
    entries: dict[str, tuple[str, str]] = {}
    for item in data.get("install", []):
        name = canonical(item["metadata"]["name"])
        digest = (
            item.get("download_info", {})
            .get("archive_info", {})
            .get("hashes", {})
            .get("sha256")
        )
        if digest is None:
            raise LockError(f"{path}: {name} was installed with no recorded sha256")
        if not _SHA256.match(digest):
            raise LockError(f"{path}: {name} records {digest!r}, not a sha256")
        if name in entries:
            raise LockError(f"{path}: {name} appears twice in one install report")
        entries[name] = (item["metadata"]["version"], digest)
    return version, entries


def read_installed(path: str | Path, drop: str | None = None) -> dict[str, str]:
    installed = {
        canonical(d["name"]): d["version"] for d in json.loads(Path(path).read_text())
    }
    if drop is not None:
        installed.pop(canonical(drop), None)
    return installed


def source_manifest(project_dir: str | Path) -> dict:
    """A deterministic, framed content identity for the project tree.

    Framed, not concatenated: each entry contributes its path length, its path,
    its content length and its content, so two different file layouts cannot
    produce the same bytes. Covers every file under the tree except the recorded
    exclusions -- NOT just `*.py`, since packaging metadata, SQL, alembic
    configuration and fixtures all change what gets built and what gets tested.

    Local path is deliberately NOT part of this digest. Where a tree lives and
    what is in it are two different questions and are checked separately.
    """
    project_dir = Path(project_dir).resolve()
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project_dir)
        if EXCLUDED_DIRS & set(relative.parts):
            continue
        if relative.name in EXCLUDED_NAMES or path.suffix in EXCLUDED_SUFFIXES:
            continue
        key = str(relative).encode()
        body = path.read_bytes()
        digest.update(b"%d:" % len(key))
        digest.update(key)
        digest.update(b"%d:" % len(body))
        digest.update(body)
        count += 1
        total += len(body)
    return {
        "sha256": digest.hexdigest(),
        "files": count,
        "bytes": total,
        "excluded_dirs": sorted(EXCLUDED_DIRS),
        "excluded_names": sorted(EXCLUDED_NAMES),
        "excluded_suffixes": sorted(EXCLUDED_SUFFIXES),
    }


def coverage_problems(
    union: dict[str, tuple[str, frozenset[str]]],
    reports: dict[str, dict[str, tuple[str, str]]],
    installed: dict[str, str],
) -> list[str]:
    """The premises a run must satisfy before its numbers mean anything.

    An entry-by-entry check over whatever the reports happen to contain does not
    establish coverage: entries omitted from every report pass it trivially. These
    are set comparisons in both directions.
    """
    problems: list[str] = []

    seen: dict[str, list[str]] = {}
    for report_name, entries in reports.items():
        for name in entries:
            seen.setdefault(name, []).append(report_name)

    duplicated = sorted(n for n, where in seen.items() if len(where) > 1)
    if duplicated:
        problems.append(
            f"installed more than once across the reports: {duplicated} -- the "
            f"install sequence should touch each distribution exactly once"
        )

    missing = sorted(set(union) - set(seen))
    if missing:
        problems.append(f"locked but absent from every install report: {missing}")
    extra = sorted(set(seen) - set(union))
    if extra:
        problems.append(f"present in an install report but in no lock: {extra}")

    for report_name, entries in reports.items():
        for name, (version, digest) in sorted(entries.items()):
            if name not in union:
                continue
            locked_version, locked_hashes = union[name]
            if version != locked_version:
                problems.append(
                    f"{report_name}: {name} installed {version}, locked {locked_version}"
                )
            if digest not in locked_hashes:
                problems.append(
                    f"{report_name}: {name} installed artifact {digest[:12]}… is not "
                    f"among the pinned hashes"
                )

    not_installed = sorted(set(union) - set(installed))
    if not_installed:
        problems.append(f"locked but not present in the installed set: {not_installed}")
    unexpected = sorted(set(installed) - set(union))
    if unexpected:
        problems.append(f"installed but in no lock: {unexpected}")
    for name in sorted(set(union) & set(installed)):
        if union[name][0] != installed[name]:
            problems.append(
                f"{name}: locked {union[name][0]}, installed {installed[name]}"
            )
    return problems


def check_declarations_against_lock(
    project_dir: str | Path,
    union: dict[str, tuple[str, frozenset[str]]],
    extra: str | None = None,
) -> list[str]:
    """Are the project's own declared requirements covered by the LOCK?

    What this adds over `pip check`, measured rather than assumed. Because the
    project is reinstalled editable before the checks run, its dist-info carries
    the current `Requires-Dist`, so `pip check` DOES report a newly declared
    unconditional runtime dependency that is missing:

        haloflow 0.1.0 requires orjson, which is not installed.     exit 1

    An addition to an EXTRA is conditional, and `pip check` is silent about it:

        No broken requirements found.                               exit 0

    So this is not "pip check is blind to missing dependencies" -- it is not. This
    covers the SELECTED EXTRA, and states declared-versus-lock consistency
    directly instead of inferring it from the environment.

    What this deliberately does NOT do: regenerate anything, or complain that a
    newer version exists upstream. A lock pinning an older release than the index
    offers is a lock doing its job.
    """
    import tomllib

    from packaging.markers import UndefinedEnvironmentName
    from packaging.requirements import Requirement

    path = Path(project_dir) / "pyproject.toml"
    if not path.exists():
        return [f"{path}: not found, so declarations cannot be checked against the lock"]
    data = tomllib.loads(path.read_text())
    project = data.get("project", {})
    specs = list(project.get("dependencies") or [])
    if extra:
        specs += list((project.get("optional-dependencies") or {}).get(extra) or [])

    problems: list[str] = []
    for spec in specs:
        requirement = Requirement(spec)
        if requirement.marker is not None:
            try:
                applies = requirement.marker.evaluate({"extra": extra or ""})
            except UndefinedEnvironmentName:
                applies = True
            if not applies:
                continue
        name = canonical(requirement.name)
        if name not in union:
            problems.append(
                f"declared requirement {spec!r} is in no lock; the lock is stale "
                f"relative to pyproject.toml"
            )
            continue
        locked_version = union[name][0]
        if requirement.specifier and not requirement.specifier.contains(
            locked_version, prereleases=True
        ):
            problems.append(
                f"declared requirement {spec!r} is not satisfied by the locked "
                f"{name}=={locked_version}"
            )
    return problems


def source_policy(source: dict) -> tuple:
    """The exclusion rule a source digest was computed under.

    Two digests are only comparable if they were taken under the same rule: a
    digest that skipped `tests/` and one that did not are different measurements
    of different things, and comparing them says nothing.
    """
    return (
        tuple(source.get("excluded_dirs") or ()),
        tuple(source.get("excluded_names") or ()),
        tuple(source.get("excluded_suffixes") or ()),
    )


def compare_source(a: dict, b: dict) -> list[str]:
    """Problems that stop two runs from being called the same-source experiment.

    v4 printed "source content asserted in both runs" whenever both runs had an
    expected value, WITHOUT comparing the two digests -- so two runs over
    different trees read as agreeing. Measured and fixed.
    """
    problems: list[str] = []
    if source_policy(a) != source_policy(b):
        problems.append(
            "the two runs computed their source digests under different exclusion "
            "policies, so the digests are not comparable"
        )
    if a.get("sha256") != b.get("sha256"):
        problems.append(
            f"source content differs: A {str(a.get('sha256'))[:12]}… vs "
            f"B {str(b.get('sha256'))[:12]}…"
        )
    return problems


def source_state(source: dict) -> str:
    """How a run's source identity should be described, in one line.

    A run with no expected value has verified its DEPENDENCIES and nothing about
    which reviewed source it was pointed at. It must not read as a source PASS.
    """
    if source.get("asserted"):
        return (
            f"source content asserted against the supplied expected value "
            f"(revision {source.get('revision') or 'unspecified'})"
        )
    return (
        "dependency verification only; reviewed-source binding pending "
        "(no expected source digest was supplied)"
    )


def write_manifest(evidence_dir: str | Path, payload: dict) -> Path:
    payload = {"manifest_version": MANIFEST_VERSION, **payload}
    path = Path(evidence_dir) / MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def read_manifest(evidence_dir: str | Path) -> dict:
    path = Path(evidence_dir) / MANIFEST_NAME
    if not path.exists():
        raise LockError(
            f"{path}: no run manifest. A run that did not record its target, its "
            f"lock identities and its coverage result cannot be compared against "
            f"another run"
        )
    data = json.loads(path.read_text())
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise LockError(
            f"{path}: manifest version {data.get('manifest_version')!r}, expected "
            f"{MANIFEST_VERSION}"
        )
    for field in ("target", "locks", "installed", "coverage_ok", "reports", "source"):
        if field not in data:
            raise LockError(f"{path}: manifest is missing {field!r}")
    return data
