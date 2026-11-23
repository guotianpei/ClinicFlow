"""`python -m haloflow.synthetic init | status | serve` (Option B B2, B3a). SYNTHETIC ONLY.

A thin dispatcher over `provision.init` / `provision.status`:
- `init` requires the operator flag `--i-attest-disposable-cluster` (plus the environment's
  `HALOFLOW_SYNTHETIC_DISPOSABLE=1`, checked by A0 like every other attestation rule);
- success prints one JSON object on stdout and exits 0;
- a refusal prints exactly `refused: <CODE>` on stderr and exits 2 (codes are fixed
  vocabulary, never values from the environment);
- anything unexpected prints a fixed line and exits 1, never a traceback (an exception
  message could carry connection details).
No reset. Not referenced by any packaging entry point (B2-U-F1).

`serve [--host 127.0.0.1] [--port N]` (B3a, Codex Q3) runs the synthetic app with uvicorn.
The host is validated BEFORE anything binds: only a loopback IP literal is accepted; any
other value prints `refused: BIND_NOT_LOOPBACK` (never the value) and exits 2. B3b: port 0 is
refused the same way (the Host allowlist needs a known port); the app gets the same bind, and
uvicorn's graceful shutdown is bounded by the lifecycle's T_total.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from collections.abc import Sequence

from haloflow.synthetic.errors import refused_from

USAGE_EXIT = 2
REFUSED_EXIT = 2
FAILED_EXIT = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m haloflow.synthetic",
                                     description="Synthetic install (disposable cluster only).")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="provision the synthetic clinic (resumable)")
    init.add_argument("--i-attest-disposable-cluster", dest="attest", action="store_true",
                      help="attest that the target cluster is dedicated and disposable")
    init.add_argument("--profile", choices=("default", "eligibility"), default="default",
                      help="install profile (E4): default (target 97) or eligibility (127)")
    commands.add_parser("status", help="read-only status of the synthetic install")
    serve = commands.add_parser("serve", help="run the synthetic app (loopback only)")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve(host: str, port: int) -> int:
    # B3b: port 0 is refused too, because the Host allowlist needs a known port.
    if not _loopback(host) or not 1 <= port <= 65535:
        sys.stderr.write("refused: BIND_NOT_LOOPBACK\n")
        return REFUSED_EXIT
    import uvicorn

    from haloflow.synthetic import app as app_module
    from haloflow.synthetic.lifecycle import LifecycleConfig

    app = app_module.create_synthetic_app(dict(os.environ), bind=(host, port))
    uvicorn.Server(uvicorn.Config(app, host=host, port=port, lifespan="on",
                                  log_level="warning", access_log=False,
                                  timeout_graceful_shutdown=int(LifecycleConfig().t_total))
                   ).run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)          # usage errors: argparse exits 2, no traceback
    if args.command == "serve":
        return _serve(args.host, args.port)
    from haloflow.synthetic import provision

    environ = dict(os.environ)
    try:
        if args.command == "init":
            result: object = {"phase": provision.init(environ, attest_flag=args.attest,
                                                      profile=args.profile).phase}
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
