#!/usr/bin/env bash
# CP2-2a gate reachability harness -- rows 2A-C07..C11 (architecture v4 §6.2).
#
# Evidence label for every row: "separate harness invocation in the CI job,
# executing the resolved gate commands". This harness never claims that the
# gate step itself observed a specimen.
#
# It runs from haloflow/ as its own step inside the actual CI job, after the
# three gate steps and in the same verified environment:
#
#   scripts/gate-reachability.sh --venv "$VENV" \
#       --workflow ../.github/workflows/m01.yml --evidence "$RUNNER_TEMP/gate-evidence"
#
# Command resolution fails closed (2A-C10). Each gate `run:` line in the
# workflow must be preceded by exactly one marker comment:
#     # gate-reachability: lint | type | test
# A missing marker, a duplicated marker, a marker not followed by a single-line
# `- run:`, or a resolved command that does not start with the expected tool
# makes the harness exit nonzero before it runs any gate.
#
# Specimen bytes are generated at run time and nothing is committed. A trap
# removes every specimen on any exit. `git status --porcelain` must be empty
# before and after the run (2A-C11).
#
# Self-test (2A-C10/C11 negative controls):
#   scripts/gate-reachability.sh --selftest --venv "$VENV" \
#       --workflow ../.github/workflows/m01.yml --evidence DIR

set -uo pipefail

VENV_DIR=""
WORKFLOW=""
EVIDENCE=""
MODE="run"
RESOLVE_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --venv) VENV_DIR="$2"; shift 2 ;;
        --workflow) WORKFLOW="$2"; shift 2 ;;
        --evidence) EVIDENCE="$2"; shift 2 ;;
        --selftest) MODE="selftest"; shift ;;
        --resolve-only) RESOLVE_ONLY=1; shift ;;
        *) echo "gate-reachability: unknown argument $1" >&2; exit 2 ;;
    esac
done

if [ -z "$VENV_DIR" ] || [ -z "$WORKFLOW" ] || [ -z "$EVIDENCE" ]; then
    echo "gate-reachability: --venv, --workflow and --evidence are required" >&2
    exit 2
fi
if [ ! -f "Makefile" ] || [ ! -d "src/haloflow" ]; then
    echo "gate-reachability: run from the haloflow/ directory" >&2
    exit 2
fi

mkdir -p "$EVIDENCE"
LOG="$EVIDENCE/gate-reachability.log"
export VENV="$VENV_DIR"

log() { printf '%s\n' "$*" | tee -a "$LOG"; }
die() { log "FAIL: $*"; exit 1; }

# --- 2A-C10: fail-closed command resolution ---------------------------------

# resolve_gate <workflow> <gate> <expected-prefix>; prints the command.
resolve_gate() {
    local workflow="$1" gate="$2" prefix="$3"
    local marker="# gate-reachability: $gate"
    local count
    count=$(grep -cE "^[[:space:]]*${marker}[[:space:]]*$" "$workflow")
    if [ "$count" != "1" ]; then
        echo "marker '$marker' found $count times" >&2
        return 1
    fi
    local next
    next=$(grep -A1 -E "^[[:space:]]*${marker}[[:space:]]*$" "$workflow" | tail -n 1)
    if ! printf '%s' "$next" | grep -qE '^[[:space:]]*- run: [^>|]'; then
        echo "marker '$marker' is not followed by a single-line '- run:'" >&2
        return 1
    fi
    local command
    command=$(printf '%s' "$next" | sed -E 's/^[[:space:]]*- run: //')
    case "$command" in
        "$prefix"*) printf '%s\n' "$command" ;;
        *) echo "gate '$gate' resolved to a command not starting with '$prefix'" >&2; return 1 ;;
    esac
}

# The expected prefixes are literal `$VENV/...` text from the workflow.
# shellcheck disable=SC2016
resolve_all() {
    local workflow="$1"
    LINT_CMD=$(resolve_gate "$workflow" lint '$VENV/bin/ruff check ') || return 1
    TYPE_CMD=$(resolve_gate "$workflow" type '$VENV/bin/mypy ') || return 1
    TEST_CMD=$(resolve_gate "$workflow" test '$VENV/bin/pytest ') || return 1
}

# --- self-test -------------------------------------------------------------

if [ "$MODE" = "selftest" ]; then
    : > "$LOG"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    resolve_all "$WORKFLOW" || die "selftest: the real workflow does not resolve"
    log "selftest: real workflow resolves (positive control)"

    # C10a missing marker, C10b duplicated marker, C10c wrong tool.
    grep -vE '# gate-reachability: lint' "$WORKFLOW" > "$tmp/missing.yml"
    sed -E 's/(# gate-reachability: type)/\1\n      \1/' "$WORKFLOW" > "$tmp/duplicate.yml"
    # shellcheck disable=SC2016
    sed -E 's#\$VENV/bin/mypy #$VENV/bin/true #' "$WORKFLOW" > "$tmp/wrongtool.yml"
    for case in missing duplicate wrongtool; do
        if resolve_all "$tmp/$case.yml" 2>>"$LOG"; then
            die "selftest C10 $case: resolution did not fail closed"
        fi
        log "selftest C10 $case: failed closed (expected)"
    done

    # C11 injected failure: the trap must remove the specimen, and git status
    # must stay clean.
    before=$(git status --porcelain)
    [ -z "$before" ] || die "selftest C11: working tree not clean before"
    GATE_HARNESS_INJECT_FAILURE=after-first-specimen \
        "$0" --venv "$VENV_DIR" --workflow "$WORKFLOW" --evidence "$tmp/inject" \
        >>"$LOG" 2>&1
    rc=$?
    [ "$rc" != "0" ] || die "selftest C11: injected failure did not fail the harness"
    after=$(git status --porcelain)
    [ -z "$after" ] || die "selftest C11: specimen left behind: $after"
    for leftover in src/haloflow/m02/_c07_specimen.py src/haloflow/m01/_c07_twin.py \
        src/haloflow/m02/_c08_specimen.py src/haloflow/m01/_c08_twin.py \
        tests/m02/test_c09_specimen.py tests/m01/test_c09_twin.py; do
        [ ! -e "$leftover" ] || die "selftest C11: $leftover remains"
    done
    log "selftest C11: injected failure exited $rc; trap removed specimens; tree clean"
    log "SELFTEST PASS"
    exit 0
fi

# --- run -------------------------------------------------------------------

: > "$LOG"
log "evidence label: separate harness invocation in the CI job, executing the resolved gate commands"
resolve_all "$WORKFLOW" 2>>"$LOG" || die "C10: gate command resolution failed closed"
log "commit: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "resolved lint: $LINT_CMD"
log "resolved type: $TYPE_CMD"
log "resolved test: $TEST_CMD"
log "python: $("$VENV/bin/python" --version 2>&1)"
log "ruff: $("$VENV/bin/ruff" --version 2>&1)"
log "mypy: $("$VENV/bin/mypy" --version 2>&1)"
log "pytest: $("$VENV/bin/pytest" --version 2>&1 | head -n 1)"
if [ "$RESOLVE_ONLY" = "1" ]; then
    exit 0
fi

SPECIMENS=()
cleanup() {
    local path
    for path in "${SPECIMENS[@]:-}"; do
        [ -n "$path" ] && rm -f "$path"
    done
}
trap cleanup EXIT

status=$(git status --porcelain)
[ -z "$status" ] || die "C11: working tree not clean before the run: $status"

# run_capture <label> <command...>; sets OUT and RC, never swallows the status.
run_capture() {
    local label="$1"; shift
    OUT=$("$@" 2>&1)
    RC=$?
    printf '%s\n' "--- $label (exit $RC) ---" "$OUT" >> "$LOG"
}

write_specimen() {
    local path="$1" content="$2"
    [ ! -e "$path" ] || die "specimen path already exists: $path"
    SPECIMENS+=("$path")
    printf '%s' "$content" > "$path"
    if [ "${GATE_HARNESS_INJECT_FAILURE:-}" = "after-first-specimen" ]; then
        log "injected failure after writing $path"
        exit 3
    fi
}

remove_specimen() { rm -f "$1"; }

expect_clean() {
    local label="$1"; shift
    run_capture "$label" "$@"
    [ "$RC" = "0" ] || die "$label: clean run exited $RC"
    log "$label: clean (exit 0)"
}

expect_detect() {
    local label="$1" needle1="$2" needle2="$3"; shift 3
    run_capture "$label" "$@"
    [ "$RC" != "0" ] || die "$label: specimen not detected (exit 0)"
    printf '%s' "$OUT" | grep -qF -- "$needle1" || die "$label: output lacks '$needle1'"
    printf '%s' "$OUT" | grep -qF -- "$needle2" || die "$label: output lacks '$needle2'"
    log "$label: detected (exit $RC; '$needle1'; '$needle2')"
}

expect_test_failure() {
    local label="$1" node="$2"; shift 2
    run_capture "$label" "$@"
    [ "$RC" != "0" ] || die "$label: specimen not detected (exit 0)"
    printf '%s' "$OUT" | grep -qF -- "FAILED $node" || die "$label: '$node' not reported failed"
    if printf '%s' "$OUT" | grep -qE "ERROR collecting|errors? during collection"; then
        die "$label: a collection error, not an assertion failure"
    fi
    log "$label: '$node' failed on its assertion (exit $RC)"
}

ci() { bash -c "$1"; }
mk() { make VENV="$VENV" "$1"; }

LINT_SPECIMEN=$'import os\n'
TYPE_SPECIMEN=$'x: int = "s"\n'
TEST_SPECIMEN=$'def test_specimen() -> None:\n    assert 1 == 2\n'

# --- 2A-C07: lint ------------------------------------------------------------
for path in ci:"$LINT_CMD" mk:lint; do
    kind="${path%%:*}"; target="${path#*:}"
    runner=( ci "$target" ); [ "$kind" = "mk" ] && runner=( mk "$target" )
    row="2A-C07${kind}"
    expect_clean "$row baseline" "${runner[@]}"
    for spec in src/haloflow/m02/_c07_specimen.py src/haloflow/m01/_c07_twin.py; do
        write_specimen "$spec" "$LINT_SPECIMEN"
        expect_detect "$row $spec" "$spec" "F401" "${runner[@]}"
        remove_specimen "$spec"
    done
    expect_clean "$row post-run" "${runner[@]}"
done

# --- 2A-C08: type ------------------------------------------------------------
for path in ci:"$TYPE_CMD" mk:type; do
    kind="${path%%:*}"; target="${path#*:}"
    runner=( ci "$target" ); [ "$kind" = "mk" ] && runner=( mk "$target" )
    row="2A-C08${kind}"
    expect_clean "$row baseline" "${runner[@]}"
    for spec in src/haloflow/m02/_c08_specimen.py src/haloflow/m01/_c08_twin.py; do
        write_specimen "$spec" "$TYPE_SPECIMEN"
        expect_detect "$row $spec" "$spec" "[assignment]" "${runner[@]}"
        printf '%s' "$OUT" | grep -qF "Incompatible types in assignment" \
            || die "$row $spec: mypy message missing"
        remove_specimen "$spec"
    done
    expect_clean "$row post-run" "${runner[@]}"
done

# --- 2A-C09: test ------------------------------------------------------------
for path in ci:"$TEST_CMD" mk:test mu:test-unit; do
    kind="${path%%:*}"; target="${path#*:}"
    runner=( ci "$target" ); [ "$kind" != "ci" ] && runner=( mk "$target" )
    row="2A-C09${kind}"
    expect_clean "$row baseline" "${runner[@]}"
    for spec in tests/m02/test_c09_specimen.py tests/m01/test_c09_twin.py; do
        write_specimen "$spec" "$TEST_SPECIMEN"
        expect_test_failure "$row $spec" "$spec::test_specimen" "${runner[@]}"
        remove_specimen "$spec"
    done
    expect_clean "$row post-run" "${runner[@]}"
done

# --- 2A-F03 (via the C07 path lists): tests/conftest.py is in both lint scopes
printf '%s' "$LINT_CMD" | grep -qE '(^| )tests/conftest\.py( |$)' \
    || die "2A-F03: tests/conftest.py is not in the CI lint command"
grep -E '^LINT_PATHS[[:space:]]*=' Makefile | grep -qE '(^| )tests/conftest\.py( |$)' \
    || die "2A-F03: tests/conftest.py is not in Makefile LINT_PATHS"
log "2A-F03: tests/conftest.py is in the CI lint command and Makefile LINT_PATHS"

status=$(git status --porcelain)
[ -z "$status" ] || die "C11: working tree not clean after the run: $status"
log "C11: working tree clean before and after"
log "PASS"
