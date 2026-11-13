"""`python -m haloflow.synthetic init | status` (Option B B2). SYNTHETIC ONLY.

A thin dispatcher over `provision.init` / `provision.status`:
- `init` requires the operator flag `--i-attest-disposable-cluster` (plus the environment's
  `HALOFLOW_SYNTHETIC_DISPOSABLE=1`, checked by A0 like every other attestation rule);
- success prints one JSON object on stdout and exits 0;
- a refusal prints exactly `refused: <CODE>` on stderr and exits 2 (codes are fixed
  vocabulary, never values from the environment);
- anything unexpected prints a fixed line and exits 1, never a traceback (an exception
  message could carry connection details).
No reset, no application startup. Not referenced by any packaging entry point (B2-U-F1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from haloflow.synthetic.errors import refused_from

USAGE_EXIT = 2
REFUSED_EXIT = 2
FAILED_EXIT = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m haloflow.synthetic",
                                     description="Synthetic install (disposable cluster only).")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="provision the synthetic clinic (resumable)")
    init.add_argument("--i-attest-disposable-cluster", dest="attest", action="store_true",
                      help="attest that the target cluster is dedicated and disposable")
    commands.add_parser("status", help="read-only status of the synthetic install")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)          # usage errors: argparse exits 2, no traceback
    from haloflow.synthetic import provision

    environ = dict(os.environ)
    try:
        if args.command == "init":
            result: object = {"phase": provision.init(environ, attest_flag=args.attest).phase}
        else:
            result = provision.status(environ)
    except Exception as error:   # noqa: BLE001 - every outcome maps to a fixed, safe line
        refused = refused_from(error)
        if refused is not None:
            sys.stderr.write(f"refused: {refused.code}\n")
            return REFUSED_EXIT
        sys.stderr.write("failed: unexpected error (no details are printed)\n")
        return FAILED_EXIT
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
