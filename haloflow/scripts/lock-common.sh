# CP2-0 D-03 -- shared preamble for generate-lock.sh and verify-lock.sh. v4, for review.
# Sourced, not executed.

# Neutralise ambient pip configuration.
#
# ORDER MATTERS, and getting it wrong is how the v2 draft silently did nothing:
# `PIP_CONFIG_FILE` itself matches `PIP_*`, so exporting it BEFORE the unset loop
# meant the loop deleted it and the caller's pip.conf was honoured after all. Unset
# first, then export, then prove it.
lock_isolate_pip() {
    local name
    for name in $(env | sed -n 's/^\(PIP_[A-Za-z0-9_]*\)=.*/\1/p'); do
        unset "$name"
    done
    unset PYTHONPATH PYTHONHOME || true
    export PIP_CONFIG_FILE=/dev/null
    if [ "${PIP_CONFIG_FILE:-}" != "/dev/null" ]; then
        echo "FAIL: PIP_CONFIG_FILE was not retained; pip config is not neutralised" >&2
        return 1
    fi
}

# Refuse to act on anything but the two APPROVED targets.
#
# v3 accepted four platform labels and any cpXY that happened to match the running
# interpreter. The approved set is exactly two: linux-x86_64-cp312 and
# macos-arm64-cp312. A cp311 label on a cp311 host is now refused as well --
# matching the host is not the test; being one of the two approved targets is.
lock_check_target() {
    local py="$1" target="$2"
    "$py" - "$target" "$(dirname "${BASH_SOURCE[0]}")" <<'PY'
import platform, sys, sysconfig
sys.path.insert(0, sys.argv[2])
from lockfile import APPROVED_TARGETS

target = sys.argv[1]
if target not in APPROVED_TARGETS:
    raise SystemExit(
        f"FAIL: {target!r} is not an approved target; the approved set is exactly "
        f"{sorted(APPROVED_TARGETS)}"
    )
want_os, want_arch, want_tag = APPROVED_TARGETS[target]

if platform.python_implementation() != "CPython":
    raise SystemExit(
        f"FAIL: {target} names CPython but this is {platform.python_implementation()}"
    )
tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
if tag != want_tag:
    raise SystemExit(f"FAIL: {target} names {want_tag} but this interpreter is {tag}")

actual = sysconfig.get_platform()           # e.g. linux-x86_64, macosx-26.0-arm64
parts = actual.split("-")
if not parts[0].startswith(want_os) or parts[-1] != want_arch:
    raise SystemExit(
        f"FAIL: {target} names {want_os}/{want_arch} but this platform is {actual}"
    )
print(f"   OK: CPython {want_tag} on {actual} may own {target}")
PY
}
