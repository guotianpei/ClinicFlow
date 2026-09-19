#!/usr/bin/env bash
# CP2-0 D-03 -- THE reusable verified-install stage. v5, for review.
#
#   ./verified-install.sh --python <py3.12> --target <t> --project <dir> \
#                         --locks <dir> --evidence <fresh dir> --envdir <new dir> \
#                         [--expected-source <sha256>] [--source-revision <label>]
#
# WHY THIS IS ITS OWN SCRIPT
# --------------------------
# CI needs the SAME install and the SAME premises as local verification, but a
# different gate set: the workflow runs a PostgreSQL service and must execute the
# postgres-marked tests, which local verification deselects. v4 invited CI to
# re-implement the install inline, which would have meant CI installing into the
# runner's existing environment with none of the checks below. Install and gates
# are now separate stages: this one installs and verifies, and the caller runs
# whatever gates it owns against $ENVDIR afterwards.
#
# It deliberately does NOT run ruff, mypy or pytest, and it does NOT delete
# $ENVDIR -- the caller owns both.
#
# WHY A FRESH VIRTUALENV, EVERY TIME
# ----------------------------------
# Measured: `pip install --require-hashes` reports "Requirement already satisfied"
# and exits 0 when the distribution is already installed, WITHOUT checking the
# hash -- pip 24.0 and 26.2.1 alike. A run in a reused environment passes against
# a deliberately corrupted lock.
#
# WHY A FRESH EVIDENCE DIRECTORY, EVERY TIME
# ------------------------------------------
# A reused directory keeps the previous run's manifest and reports. A later run
# that fails early then leaves a stale SUCCESS manifest sitting next to a
# truncated log, and a comparison would read it as a passing run.
set -euo pipefail

PY_IN="" TARGET="" PROJECT_IN="" LOCKS_IN="" EVIDENCE_IN="" ENVDIR_IN=""
EXPECTED_SOURCE="" SOURCE_REVISION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --python) PY_IN="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        --project) PROJECT_IN="$2"; shift 2 ;;
        --locks) LOCKS_IN="$2"; shift 2 ;;
        --evidence) EVIDENCE_IN="$2"; shift 2 ;;
        --envdir) ENVDIR_IN="$2"; shift 2 ;;
        --expected-source) EXPECTED_SOURCE="$2"; shift 2 ;;
        --source-revision) SOURCE_REVISION="$2"; shift 2 ;;
        *) echo "FAIL: unknown argument $1" >&2; exit 2 ;;
    esac
done
for required in PY_IN TARGET PROJECT_IN LOCKS_IN EVIDENCE_IN ENVDIR_IN; do
    eval "value=\${$required}"
    [ -n "$value" ] || { echo "FAIL: --${required%_IN} is required" >&2; exit 2; }
done

HERE="$(cd "$(dirname "$0")" && pwd)"
abspath() { "$PY_IN" -c 'import os,sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' "$1"; }

PY="$(command -v "$PY_IN" || echo "$PY_IN")"
PROJECT="$(abspath "$PROJECT_IN")"
LOCKS="$(abspath "$LOCKS_IN")"

# A fresh, empty evidence directory. Refused, not silently reused.
if [ -e "$EVIDENCE_IN" ] && [ -n "$(ls -A "$EVIDENCE_IN" 2>/dev/null)" ]; then
    echo "FAIL: evidence directory $EVIDENCE_IN already exists and is not empty;" >&2
    echo "      a reused directory can leave a stale success manifest beside a" >&2
    echo "      failed run. Give each run its own directory." >&2
    exit 1
fi
mkdir -p "$EVIDENCE_IN"
EVIDENCE="$(abspath "$EVIDENCE_IN")"
if [ -e "$ENVDIR_IN" ]; then
    echo "FAIL: --envdir $ENVDIR_IN already exists; the environment must be new" >&2
    exit 1
fi
mkdir -p "$(dirname "$ENVDIR_IN")"
ENVDIR="$(abspath "$(dirname "$ENVDIR_IN")")/$(basename "$ENVDIR_IN")"

# The ORIGINALS. They are provenance only: nothing is installed or parsed from
# them after the snapshot below.
BOOTSTRAP_ORIGIN="$LOCKS/${TARGET}.bootstrap.txt"
MAIN_ORIGIN="$LOCKS/${TARGET}.txt"

STEPS="$EVIDENCE/steps.log"
{
    echo "verified-install.sh v5"
    echo "target: $TARGET"
    echo "interpreter: $PY"
    echo "project: $PROJECT"
    echo "locks: $LOCKS"
    echo "evidence: $EVIDENCE"
    echo "envdir: $ENVDIR"
    echo "expected source sha256: ${EXPECTED_SOURCE:-<none supplied>}"
    echo "source revision: ${SOURCE_REVISION:-<unspecified>}"
    echo "start ET: $(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "start UTC: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "---"
} > "$STEPS"

# Every step's command, exit status and FULL output is retained as it happens.
step() {
    local label="$1"; shift
    local log="$EVIDENCE/step-${label}.log"
    {
        echo "command: $*"
        echo "start UTC: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "---"
    } > "$log"
    set +e
    "$@" >> "$log" 2>&1
    local code=$?
    set -e
    echo "--- exit: $code" >> "$log"
    sed -n '4,$p' "$log"
    echo "$label exit $code" >> "$STEPS"
    if [ "$code" -ne 0 ]; then
        echo "FAIL: step '$label' exited $code; full output in $log" >&2
        exit "$code"
    fi
}

echo "== isolate pip configuration"
. "$HERE/lock-common.sh"
lock_isolate_pip

# The approved-set check comes first and depends on no lock file existing. It is
# captured through the step logger like every other step, so a refused target
# leaves the same durable record a refused install does.
echo "== 0. target check"
step target lock_check_target "$PY" "$TARGET"

# SNAPSHOT FIRST, THEN CONSUME THE SNAPSHOT.
#
# v5's first cut copied the locks into the evidence and then installed from the
# ORIGINALS. The evidence was therefore provenance for a file that was not
# provably the file installed: the original is outside the evidence and can change
# between the copy and the install. Everything below -- parse, both installs, and
# every premise -- reads the snapshot. The original path is recorded and never
# read again.
mkdir -p "$EVIDENCE/locks"
cp "$BOOTSTRAP_ORIGIN" "$EVIDENCE/locks/${TARGET}.bootstrap.txt"
cp "$MAIN_ORIGIN" "$EVIDENCE/locks/${TARGET}.txt"
BOOTSTRAP="$EVIDENCE/locks/${TARGET}.bootstrap.txt"
MAIN="$EVIDENCE/locks/${TARGET}.txt"
echo "   input locks snapshotted into $EVIDENCE/locks; the snapshot is what is consumed"

echo "== 1. parse both locks with the one shared parser (from the snapshot)"
step parse "$PY" "$HERE/lock_sanity.py" "$BOOTSTRAP" "$MAIN"

echo "== 2. clean environment"
step venv "$PY" -m venv "$ENVDIR"
echo "   preinstalled: $("$ENVDIR/bin/python" -m pip list --format=freeze | tr '\n' ' ')"

echo "== 3. bootstrap: installer and build backend, hash-verified"
step install-bootstrap "$ENVDIR/bin/python" -m pip install --require-hashes \
    --force-reinstall --only-binary :all: \
    --report "$EVIDENCE/report-bootstrap.json" -r "$BOOTSTRAP"

echo "== 4. third-party distributions, hash-verified, wheels only"
step install-main "$ENVDIR/bin/python" -m pip install --require-hashes \
    --only-binary :all: --report "$EVIDENCE/report-main.json" -r "$MAIN"

echo "== 5. the project itself: editable, no deps, no build isolation, no hashes"
step install-project "$ENVDIR/bin/python" -m pip install --no-deps \
    --no-build-isolation -e "$PROJECT"

"$ENVDIR/bin/python" -m pip list --format=json > "$EVIDENCE/installed.json"

echo "== 6. premises, and the run manifest"
step premises "$ENVDIR/bin/python" "$HERE/lock_assert.py" \
    --bootstrap "$BOOTSTRAP" --main "$MAIN" \
    --bootstrap-origin "$BOOTSTRAP_ORIGIN" --main-origin "$MAIN_ORIGIN" \
    --project "$PROJECT" \
    --envdir "$ENVDIR" --evidence "$EVIDENCE" --target "$TARGET" \
    --module-dir "$HERE" --expected-source "$EXPECTED_SOURCE" \
    --source-revision "$SOURCE_REVISION"

echo "$ENVDIR" > "$EVIDENCE/envdir.txt"
echo "verified-install: the environment at $ENVDIR is installed and verified"
echo "   gates are the caller's; this stage runs none."
