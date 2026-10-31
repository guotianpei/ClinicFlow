"""Mutation controls for haloflow.m02.observation (UC1 part A, Codex 20261031T152545Z).

Runs the service test module once against the unmodified source (the control; it
must pass), then once per mutant with exactly one textual change applied to
`observation.py`. Every mutant must make the module fail. The source file is
restored after every run, and its SHA-256 is checked against the original before
the script exits.

Usage (from haloflow/):
    python3 -I scripts/m02_observation_mutants.py --pytest "$VENV/bin/pytest" \
        --evidence "$RUNNER_TEMP/verify-evidence/mutation"

Exit 0 only if the control passed, every mutant was killed and the source was
restored byte-identically. Writes results.json to the evidence directory and prints
one GitHub `::notice` line with the summary. Database access comes from the same
environment the test suite uses (HALOFLOW_TEST_DATABASE_URL).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

TARGET = Path("src/haloflow/m02/observation.py")
TESTS = "tests/m02/test_m02_part_a_service_postgres.py"

# (id, description, exact old text, replacement). Each old text must occur once.
MUTANTS: tuple[tuple[str, str, str, str], ...] = (
    ("M1", "callback also on existing",
     'if result.appended_or_existing == "appended":\n                await self._apply',
     "if True:\n                await self._apply"),
    ("M2", "no unknown classification after the marker",
     "            if not body_complete:\n                raise",
     "            raise"),
    ("M3", "marker set before the body runs",
     "            nonlocal body_complete\n",
     "            nonlocal body_complete\n            body_complete = True\n"),
    ("M4", "AppendFailed chained to the driver error",
     "            failure = _reason_for(error)\n            row = None",
     "            raise AppendFailed(_reason_for(error)) from error"),
    ("M5", "no binding liveness check",
     "        if not binding._liveness.active:\n            raise RepositoryHandleExpired()\n",
     ""),
    ("M6", "no binding owner check",
     " or binding._owner is not self",
     ""),
    ("M7", "_apply does not call the callback",
     "        await apply_state_change(handle, result)",
     "        return None"),
    ("M8", "attribution principal not from the context",
     '"principal_id": principal.id,',
     '"principal_id": "00000000-0000-4000-8000-000000000001",'),
    ("M9", "callback exception wrapped",
     "                await self._apply(apply_state_change, handle, result)",
     "                try:\n"
     "                    await self._apply(apply_state_change, handle, result)\n"
     "                except Exception:\n"
     '                    raise RuntimeError("wrapped") from None'),
    ("M10", "refusal code collapsed",
     "        if message in _REFUSAL_CODES:\n            return message",
     "        if message in _REFUSAL_CODES:\n            return _APPEND_UNAVAILABLE"),
    ("M11", "post-marker cancellation not classified (F-OBS1)",
     "        except (Exception, asyncio.CancelledError):",
     "        except Exception:"),
)

_FAILED = re.compile(r"^FAILED \S+::([A-Za-z0-9_]+)", re.MULTILINE)


def _run(pytest: str) -> tuple[int, list[str], str]:
    completed = subprocess.run(
        [pytest, TESTS, "-q", "-rf", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    output = completed.stdout + completed.stderr
    # pytest's own result line is the last line of stdout ("N passed in ...").
    lines = completed.stdout.strip().splitlines()
    return completed.returncode, sorted(set(_FAILED.findall(output))), lines[-1] if lines else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)

    original = TARGET.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8")
    results: dict[str, object] = {"target": str(TARGET), "target_sha256": original_sha,
                                  "tests": TESTS, "mutants": []}
    ok = True
    try:
        rc, failed, summary = _run(args.pytest)
        results["control"] = {"exit": rc, "failed": failed, "summary": summary}
        if rc != 0:
            ok = False
        for mutant_id, description, old, new in MUTANTS:
            count = text.count(old)
            entry: dict[str, object] = {"id": mutant_id, "description": description}
            if count != 1:
                entry.update(applied=False, occurrences=count, killed=False)
                ok = False
            else:
                TARGET.write_text(text.replace(old, new), encoding="utf-8")
                try:
                    rc, failed, summary = _run(args.pytest)
                finally:
                    TARGET.write_bytes(original)
                entry.update(applied=True, exit=rc, killed=rc != 0, killed_by=failed,
                             summary=summary)
                if rc == 0:
                    ok = False
            results["mutants"].append(entry)  # type: ignore[attr-defined]
    finally:
        TARGET.write_bytes(original)
    restored = hashlib.sha256(TARGET.read_bytes()).hexdigest() == original_sha
    results["restored"] = restored
    ok = ok and restored
    results["ok"] = ok
    (evidence / "results.json").write_text(json.dumps(results, indent=2) + "\n")

    mutants = results["mutants"]
    killed = sum(1 for m in mutants if m.get("killed"))  # type: ignore[union-attr]
    control = results["control"]
    parts = [
        f"ok={ok} control_exit={control['exit']} ({control['summary']})",  # type: ignore[index]
        f"killed={killed}/{len(MUTANTS)} restored={restored} target_sha256={original_sha}",
    ]
    for m in mutants:  # type: ignore[union-attr]
        parts.append(f"{m['id']} killed={m.get('killed')} by={','.join(m.get('killed_by', []))}")
    print("::notice title=m02-observation-mutants::" + " %0A".join(parts))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
