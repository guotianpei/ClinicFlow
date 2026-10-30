#!/usr/bin/env python3
"""Mutation controls for e2/athena_confirmation_shape_e2.py (raw evidence for Codex).

Each mutation is applied alone to a temporary copy of the runner, and the frozen test
file is run against it. KILLED = at least one test fails. The working runner is never
modified. Run from the repo root:  python3 e2/review-evidence/mutation_controls.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "athena_confirmation_shape_e2.py"
TESTS = ROOT / "test_athena_confirmation_shape_e2.py"

MUTATIONS = [
    ("M1 bool totalcount accepted",
     "return isinstance(v, int) and not isinstance(v, bool)", "return isinstance(v, int)"),
    ("M2 repeated page not detected", "if items and sig in signatures:", "if False:"),
    ("M3 short page before total not refused", "if len(items) < VOCAB_LIMIT:", "if False:"),
    ("M4 confirmation read cap raised 3 -> 4", "MAX_CONF_READS = 3", "MAX_CONF_READS = 4"),
    ("M5 undocumented key name echoed", "undocumented += 1", "undocumented += 1; keys[k] = 'x'"),
    ("M6 non-string appointment id accepted",
     "if not isinstance(i, str) or not ID_RE.fullmatch(i):",
     "if not ID_RE.fullmatch(str(i)):"),
    ("M7 duplicate statusid not refused", 'reason = "DUPLICATE_ID"', "pass"),
    ("M8 vocabulary names written to output",
     'v["names_unique"] = len(set(names)) == len(names)', 'v["names_unique"] = names'),
    ("M9 (F1) zero-sample vocabulary verifies element schema",
     'if vocab["entries_seen"] == 0:', "if False:"),
    ("M10 (F2) present-null counted as verified",
     '"verified": not empty and nulls == 0', '"verified": not empty'),
    ("M11 (F3) id regex prefix match instead of full match",
     "not ID_RE.fullmatch(i)", "not ID_RE.match(i)"),
    # M12 is expected to SURVIVE: every pattern ends in "$", which differs from a full
    # match only before a trailing newline, and the M13 guard rejects any newline first.
    # It is an equivalent mutant while M13 stands; fullmatch is kept as defence in depth.
    ("M12 (F3) allowlist prefix match instead of full match [equivalent while M13 holds]",
     "p.fullmatch(parts.path)", "p.match(parts.path)"),
    ("M13 (F3) control-character guard removed",
     "if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):", "if False:"),
    ("M14 vocabulary page cap raised 4 -> 5",
     "for page_no in range(VOCAB_MAX_PAGES):",
     "for page_no in range(VOCAB_MAX_PAGES + 1):"),
]


def main() -> int:
    src = RUNNER.read_text()
    survived = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        shutil.copy(TESTS, tmp / TESTS.name)
        for label, old, new in MUTATIONS:
            if src.count(old) != 1:
                print(f"{label}: ANCHOR NOT FOUND EXACTLY ONCE ({src.count(old)})")
                survived += 1
                continue
            (tmp / RUNNER.name).write_text(src.replace(old, new, 1))
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                str(tmp / TESTS.name)], capture_output=True, text=True,
                               cwd=tmp)
            last = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
            verdict = "KILLED" if r.returncode != 0 else "SURVIVED"
            survived += verdict == "SURVIVED" and "[equivalent" not in label
            print(f"{label}: {verdict} | {last}")
        (tmp / RUNNER.name).write_text(src)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            str(tmp / TESTS.name)], capture_output=True, text=True, cwd=tmp)
        print("UNMUTATED:", [ln for ln in r.stdout.splitlines() if ln.strip()][-1])
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
