"""pglast 7.17 -> 7.21, D-1a: reproducible fixture/derived-identity transformation.

Run from haloflow/ with the reviewed source/pin/lock edits STASHED (git stash), so step 1
classifies against the original tree; the script pops the stash before step 2. Steps:

1. Classify every 64-hex literal in the test tree (tests/m01 + tests/m02 *.py/*.json,
   excluding tests/m01/fixtures/function_checksum/**) by re-deriving it from the
   ORIGINAL tree with one of these formulas; a literal is "derived" only if a formula
   reproduces it exactly:
     F-FILE  sha256(bytes of a tracked file)
     F-X3B   sha256(json.dumps(p, sort_keys, indent=2, ensure_ascii=False) + "\n")
     F-X3C   sha256(json.dumps(p, ensure_ascii=False, allow_nan=False))
     F-FCS   function-v3 checksum of a payload (migration_id, template, execution_role,
             verification, policy)
   over every policy payload in the fixtures and every derived typed payload.
2. Replace '"parser_version": "7.17"' with '"parser_version": "7.21"' in the 8
   function-policy fixture files (expected 84 occurrences; nothing else in them).
3. Recompute every derived literal from the NEW tree with the SAME formula and the
   SAME subject, replace old->new in place, and iterate until no literal changes
   (file hashes depend on files that contain other hashes).
4. Write transform-report.json: every replacement (file, old, new, formula, subject)
   and every 64-hex literal left unchanged with its classification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
assert (ROOT / "pyproject.toml").exists()
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "m01"))

from haloflow.m01.provisioning.function_checksum import function_checksum  # noqa: E402

POLICY = ROOT / "tests/m01/fixtures/function_policy"
FIX_FILES = [POLICY / "cases.json", POLICY / "sql-fixtures.json"] + [
    POLICY / "controls" / f"C0{i}.json" for i in range(1, 7)]
OLD_DECL = '"parser_version": "7.17"'
NEW_DECL = '"parser_version": "7.21"'
HEX = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
KEYS = ("migration_id", "template", "execution_role", "verification", "policy")


def scan_files() -> list[Path]:
    out = []
    for base in ("tests/m01", "tests/m02"):
        for p in sorted((ROOT / base).rglob("*")):
            if p.suffix in (".py", ".json") and "function_checksum" not in p.parts \
                    and "__pycache__" not in p.parts:
                out.append(p)
    return out


def tracked_files() -> list[Path]:
    names = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                           check=True).stdout.split()
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def x3b(p: object) -> str:
    return sha((json.dumps(p, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode())


def x3c(p: object) -> str:
    return sha(json.dumps(p, ensure_ascii=False, allow_nan=False).encode())


def fcs(p: dict) -> str | None:
    try:
        return function_checksum(**{k: p[k] for k in KEYS})
    except Exception:
        return None


def payload_subjects() -> dict[str, dict]:
    """Every policy payload a test can hash, keyed by a stable subject name."""
    subj: dict[str, dict] = {}
    cases = json.loads((POLICY / "cases.json").read_text())
    for i, c in enumerate(cases["controls"]):
        if "payload" in c:
            subj[f"cases.controls[{i}]({c.get('control_id', c.get('id', i))})"] = c["payload"]
    for n in range(1, 7):
        subj[f"controls/C0{n}.json"] = json.loads((POLICY / "controls" / f"C0{n}.json").read_text())
    for v in json.loads((POLICY / "sql-fixtures.json").read_text())["variants"]:
        subj[f"variant:{v['case_id']}"] = v["payload"]
    import conftest  # tests/m01/conftest.py: the same derivation the tests use
    for name, p in conftest._derive_typed_payloads().items():
        subj[f"typed:{name}"] = p
    return subj


def candidates() -> dict[str, tuple[str, str]]:
    """value -> (formula, subject) over the CURRENT tree."""
    out: dict[str, tuple[str, str]] = {}
    for p in tracked_files():
        out.setdefault(sha(p.read_bytes()), ("F-FILE", str(p.relative_to(ROOT))))
    for name, p in payload_subjects().items():
        out.setdefault(x3b(p), ("F-X3B", name))
        out.setdefault(x3c(p), ("F-X3C", name))
        v = fcs(p)
        if v:
            out.setdefault(v, ("F-FCS", name))
    return out


def recompute(formula: str, subject: str) -> str:
    if formula == "F-FILE":
        return sha((ROOT / subject).read_bytes())
    p = payload_subjects()[subject]
    if formula == "F-X3B":
        return x3b(p)
    if formula == "F-X3C":
        return x3c(p)
    v = fcs(p)
    assert v
    return v


def literals() -> list[tuple[Path, str]]:
    return [(p, h) for p in scan_files() for h in HEX.findall(p.read_text(errors="ignore"))]


def main() -> None:
    for m in [k for k in list(sys.modules) if k == "conftest"]:
        del sys.modules[m]
    cand = candidates()
    classified = {}
    for p, h in literals():
        if h in cand:
            classified[h] = cand[h]
    # 1b. apply the reviewed source/pin/lock edits (kept in the stash while the ORIGINAL
    #     tree was classified), so the source file hashes recompute against them.
    subprocess.run(["git", "stash", "pop"], check=True)
    # 2. the declaration replacement
    total = 0
    for f in FIX_FILES:
        t = f.read_text()
        total += t.count(OLD_DECL)
        f.write_text(t.replace(OLD_DECL, NEW_DECL))
    assert total == 84, total
    # 3. fixpoint
    current = {h: h for h in classified}         # original literal -> its latest value
    replacements = []
    for rnd in range(20):
        if "conftest" in sys.modules:
            del sys.modules["conftest"]
        changed = 0
        for orig, (formula, subject) in classified.items():
            new = recompute(formula, subject)
            old = current[orig]
            if new != old:
                for p in scan_files():
                    t = p.read_text(errors="ignore")
                    if old in t:
                        n = len(HEX.findall(t)) and t.count(old)
                        p.write_text(t.replace(old, new))
                        replacements.append({"round": rnd, "file": str(p.relative_to(ROOT)),
                                             "old": old, "new": new, "count": n,
                                             "formula": formula, "subject": subject})
                current[orig] = new
                changed += 1
        if not changed:
            break
    else:
        raise SystemExit("no fixpoint")
    unchanged = sorted({(str(p.relative_to(ROOT)), h) for p, h in literals()
                        if h not in current.values()})
    report = {
        "declaration_replacements": total,
        "classified": {h: {"formula": f, "subject": s, "final": current[h]}
                       for h, (f, s) in classified.items()},
        "replacements": replacements,
        "unclassified_literals_left_as_is": unchanged,
    }
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2))
    moved = {h: v for h, v in current.items() if v != h}
    print("declarations:", total, "classified:", len(classified), "moved:", len(moved),
          "rounds:", rnd + 1, "unclassified:", len(unchanged))
    for h, v in moved.items():
        print(classified[h][0], classified[h][1], h[:12], "->", v[:12])


if __name__ == "__main__":
    main()
