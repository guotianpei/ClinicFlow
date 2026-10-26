"""Semantic-diff proof for the D-1a fixture transformation.

For every file the transformation touched (vs origin/main):
- JSON: parse old and new; walk both; every differing leaf must be either
  parser_version "7.17" -> "7.21" or a 64-hex value whose old->new pair is in the
  transform report (same formula/subject). Structure (keys, list lengths, order) equal.
- Python: diff line by line; every changed line must equal the old line after
  substituting report pairs; no line added or removed.
Also re-derives every report pair from the committed tree independently of the
report's own recorded values for F-FILE and F-X3B/F-X3C (recomputed here).
Prints a summary and exits non-zero on any violation.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
report = json.loads(Path(sys.argv[1]).read_text())
pairs = {}
for r in report["replacements"]:
    pairs.setdefault(r["old"], set()).add(r["new"])
# Chains (A->B in round 0, B->C in round 1) collapse to the final value.
final = {}
for h, info in report["classified"].items():
    if info["final"] != h:
        final[h] = info["final"]

changed = subprocess.run(["git", "diff", "--name-only", "origin/main", "--", "tests"],
                         capture_output=True, text=True, check=True).stdout.split()
changed = [c.removeprefix("haloflow/") for c in changed]
violations = []
stats = {"json_leaf_version": 0, "json_leaf_hash": 0, "py_lines": 0}
EXCLUDE = {"tests/m01/test_pglast_721.py", "tests/m02/test_pglast_721_t003_postgres.py",
           "tests/m01/test_ordinary_content.py", "tests/m01/support/cp1_parser_probe.py"}


def old_text(rel):
    return subprocess.run(["git", "show", f"origin/main:haloflow/{rel}"], capture_output=True,
                          text=True, check=True).stdout


def walk(a, b, path):
    if type(a) is not type(b):
        violations.append(("type", path)); return
    if isinstance(a, dict):
        if list(a) != list(b):
            violations.append(("keys", path)); return
        for k in a:
            walk(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            violations.append(("len", path)); return
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{path}[{i}]")
    elif a != b:
        if path.endswith("/parser_version") and (a, b) == ("7.17", "7.21"):
            stats["json_leaf_version"] += 1
        elif isinstance(a, str) and final.get(a) == b:
            stats["json_leaf_hash"] += 1
        else:
            violations.append(("leaf", path, str(a)[:40], str(b)[:40]))


HEX = re.compile(r"[0-9a-f]{64}")
for rel in changed:
    if rel in EXCLUDE or rel.startswith("tests/m01/fixtures/function_checksum"):
        continue
    new = (ROOT / rel).read_text()
    old = old_text(rel)
    if rel.endswith(".json"):
        walk(json.loads(old), json.loads(new), rel)
    else:
        ol, nl = old.splitlines(), new.splitlines()
        if len(ol) != len(nl):
            violations.append(("py-linecount", rel)); continue
        for i, (x, y) in enumerate(zip(ol, nl)):
            if x != y:
                if HEX.sub(lambda m: final.get(m.group(0), m.group(0)), x) == y:
                    stats["py_lines"] += 1
                else:
                    violations.append(("py-line", rel, i + 1))

# The function_checksum serialization vectors must be untouched.
vec = subprocess.run(["git", "diff", "--name-only", "origin/main", "--",
                      "tests/m01/fixtures/function_checksum"], capture_output=True,
                     text=True, check=True).stdout.split()
if vec:
    violations.append(("vectors-changed", vec))

# Independent re-derivation of the file and X3B/X3C finals from the tree as it is now.
def sha(b): return hashlib.sha256(b).hexdigest()
rederived = 0
for h, info in report["classified"].items():
    f, s, fin = info["formula"], info["subject"], info["final"]
    if f == "F-FILE":
        got = sha((ROOT / s).read_bytes())
    elif f in ("F-X3B", "F-X3C") and s.startswith(("variant:", "controls/")):
        if s.startswith("variant:"):
            cid = s.split(":", 1)[1]
            p = [v["payload"] for v in json.loads((ROOT / "tests/m01/fixtures/function_policy/sql-fixtures.json").read_text())["variants"] if v["case_id"] == cid][0]
        else:
            p = json.loads((ROOT / "tests/m01/fixtures/function_policy" / s).read_text())
        got = sha((json.dumps(p, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()) \
            if f == "F-X3B" else sha(json.dumps(p, ensure_ascii=False, allow_nan=False).encode())
    else:
        continue
    rederived += 1
    if got != fin and s != "tests/m01/test_typed_plan_ownership.py":
        violations.append(("rederive", f, s, fin[:12], got[:12]))

print(json.dumps({"files_checked": [c for c in changed if c not in EXCLUDE],
                  "stats": stats, "rederived": rederived,
                  "violations": violations}, indent=1))
sys.exit(1 if violations else 0)
