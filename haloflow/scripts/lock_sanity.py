#!/usr/bin/env python3
"""CP2-0 D-03 -- parse both locks before anything is installed.

Separate from the assertion step on purpose: this runs with the CALLER's
interpreter, before a virtualenv exists, so a malformed or duplicated lock is
refused before pip is ever invoked. v3 ran its duplicate check after the installs
had already happened.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lockfile import LockError, lock_union, parse_lock  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: lock_sanity.py <bootstrap-lock> <main-lock>", file=sys.stderr)
        return 2
    try:
        bootstrap = parse_lock(argv[1])
        main_lock = parse_lock(argv[2])
        union = lock_union(bootstrap, main_lock)
    except LockError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    overlap = sorted(set(bootstrap) & set(main_lock))
    print(
        f"   OK: {len(bootstrap)} bootstrap + {len(main_lock)} main = "
        f"{len(union)} distinct distributions"
    )
    print(
        f"       well-formed, sha256 syntax valid, no duplicate canonical name "
        f"within a file"
    )
    print(f"       cross-file overlap (allowed, pins agree): {overlap or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
