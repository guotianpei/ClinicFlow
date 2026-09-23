"""PHASE-SIGNATURE-01 Gate 3 STAGED EXECUTOR and KILL-RUN HARNESS (test overlay).

NOT production code. Stage 3 of G-C1. AUTHORED ONLY: nothing in it has been run.
Running it is EXECUTION and needs Rachel's separate authorization (G-X1 environment,
G-X2 staged 3.12 execution, G-5 mutation runs). Codex's review of it is not that
authorization.

Invocation (from ``haloflow/``, only once authorized)::

    python tests/m01/phase_signature/sig_killrun.py --out <new directory> \\
        --suite-manifest-sha256 <hex> \\
        [--g2-evidence <path> --g2-evidence-sha256 <hex> \\
         --g2-credit-receipt <path> --g2-credit-receipt-sha256 <hex>]

Every hex value is the one NAMED BY RACHEL'S EXECUTION AUTHORIZATION, never computed by
the operator from the files at hand (Codex G-C2 Stage 3 v1, blockers 1 and 2).

What it enforces -- Stage 2 forward item F3, work plan v3 section 4, kill-run v7 section 2,
Codex's F4 ruling -- in this order, each step fail-closed:

  P   preconditions: CPython 3.12 exactly; the M4 variable absent; a fresh output
      directory; a FRESH EMPTY per-run bytecode-cache prefix set for the parent and every
      child BEFORE any suite import, and a fail-closed scan for any bytecode artifact under
      tests/m01 and src (v3, blocker 3). Then, BEFORE any suite module is imported and
      before any child: SUITE AUTHENTICATION -- the phase_signature directory holds EXACTLY
      the thirteen expected ``.py`` files; the nine carried Stage 1 v2 / Stage 2 v2 files
      equal their accepted digests (constants below); the canonical manifest of all
      thirteen hashes to the authorization-named ``--suite-manifest-sha256``; the three
      repository test-support files every child loads equal their ``main`` digests. The
      SAME suite authentication is repeated IN FULL in the initial snapshot, at every
      checkpoint and at the closure (v3, blocker 1). Only then is the suite imported, and
      the protected and bound inputs checked against the suite's own constants.
  B1  baseline INSTRUMENT stage: Part A (A-01..A-29) and Part B (B-01..B-23). B-24 is the
      M4 kill itself and runs FIRST among the mutants, never in the baseline.
  B2  backing CONTROLS: PD-01 (mandatory C01), PD-02, PD-03, PD-04.
  G2  G-2 parse-prerequisite CREDIT (OD-SIG-25 path ii). BOTH the independently reviewed
      evidence record AND the disposition / owner-credit receipt must be supplied, each
      with the SHA-256 named by the execution authorization; each file's bytes must hash to
      it; and the receipt must name the evidence record's SHA-256. Identity alone is not
      credit (Codex Q2): the evidence contract is OD-SIG-25 v2's, reviewed by Codex and
      credited by Rachel BEFORE this run; the executor binds what they accepted and never
      judges it. Without both, NO G2-gated case (the nine rows, PE-01) runs: stop, exit 3.
  B3  ROWS PC-01..PC-09, each run only if every one of its BACKING_CASES passed in B2.
  B4  PE-02, only if PD-01 and PD-02 passed.
  B5  PE-01, only if PD-01, PD-02 and PE-02 passed, and G-2 is bound.
  H   HARNESS self-proofs K-01..K-12 (the F2 obligations on the kill-run harness itself).
      Only after B1..B5 and H have ALL passed -- the complete non-mutant set -- may any
      mutant run.
  M   mutants in the order M4, M1, M2, M3a, M3b, M5, M-O11-L1, M-O11-L2. Each is preceded
      by its own BINDING stage (F4, read literally): source hash, then X3a/X3b/X3c for all
      nine rows and C01/C02/C03, then the S1..S14 resolver invariants -- and only then the
      pytest child. The in-case X3 checks are kept. A binding failure STOPS the kill-run.
  Q   INTEGRITY CLOSURE on EVERY terminal path once execution has started: the suite
      re-authorized, test-support files, bytecode scan, protected inputs, bound inputs and
      the G-2 artifacts against their AUTHORIZED hashes, compared with P. The same comparison
      is a CHECKPOINT after every child process; any drift STOPS the run at once. Every
      observation is TOTAL (v3, blocker 2): MISSING / WRONG_KIND / UNREADABLE are data,
      never exceptions, so the closure, report and SHA256SUMS are always produced.

Every stage is a SEPARATE pytest child (explicit minimal environment, as B-24; plugin
autoload disabled; ``-p sig_killrun_plugin``). Order is therefore enforced between
processes by recorded results, never by file order.

Output (all under ``--out``): per stage ``<stage>.results.json``, ``.stdout.txt``,
``.stderr.txt``; ``killrun-report.json``; ``SHA256SUMS``.

The report never says CREDITED. It says whether each design EXPECTATION was MET, and
carries the credit class the accepted design assigns. Credit is the owner's act on
evidence produced under CI-pinned 3.12, with G-2 credited and OI-READY earned.

Exit codes: 0 every stage passed and every expectation met; 1 a baseline case failed, an
expectation was not met, a child's results were invalid, or integrity drifted; 2 a
precondition refused the run; 3 stopped before the rows because G-2 credit was not bound.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HALOFLOW_ROOT = HERE.parents[2]
TESTS_M01 = HERE.parent
SRC = HALOFLOW_ROOT / 'src'
M4_ENV = 'HALOFLOW_SIG_MUTANT'

REQUIRED_PYTHON = (3, 12)
STAGE_TIMEOUT_SECONDS = 1800

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_G2_ABSENT = 3

# ------------------------------------------------------------ suite authentication

# The nine CARRIED files, bound to the digests in the ACCEPTED manifests: Stage 1 v2
# SHA256SUMS 82c4030c... (Codex ACCEPT 20260923T005037Z) as superseded for test_sig_part_b.py
# by Stage 1 v3 SHA256SUMS 3bfc4029... (the -P child launch, kill-run #1 root cause), and
# Stage 2 v2 SHA256SUMS 50fbf526... (Codex ACCEPT 20260923T131030Z). Explicit constants,
# never the files at hand.
CARRIED_SHA256: Mapping[str, str] = types.MappingProxyType({
    'conftest.py': 'f3679242d4aa26c326f89296f24408fbb371c0c67aadb88903c4c07e4969b3b9',
    'sig_child_plugin.py': '108c7128775316ed47c2ad4e829a4b26badaf38433534b81b6ff3153f552dd5c',
    'sig_overlay.py': '36b38533c5dbf5d4264fb1519dc042950cafb04032c0805f36f3724653bd5914',
    'test_sig_part_a.py': '60bb473a641b694cd818f1acae01ddf405029ebd93351894b8423d6220d8f92c',
    'test_sig_part_b.py': 'eb9c02c3d2cd6e706b31aba38417e8ba9fdc292087bbf81348d7ada12554d53b',
    'sig_cases.py': 'e797c624784160e751490658cf986216cc40fb4de0a0373974d5f3d4f1ea77ba',
    'test_sig_part_c.py': 'c8e53344ad06598bafa2462057121372056c67db85b5d95ad9b4b6d80843cefe',
    'test_sig_part_d.py': '4cb12f6a3c5b86b17e72661be705f040ebea14c49cb290a1495a6c51efbe6bac',
    'test_sig_part_e.py': 'f5e8b4bd1f2b9d454a2c7d0d9eec83c7ec229c05d01180fd5ee3a6cf9213d45a',
})

# The four Stage 3 files. Their digests cannot be constants in a file that is one of them;
# they are bound through the canonical suite manifest, whose SHA-256 the execution
# authorization names (``--suite-manifest-sha256``).
STAGE3_FILES: tuple[str, ...] = ('sig_killrun.py', 'sig_killrun_plugin.py', 'sig_mutants.py',
                                 'test_sig_killrun_harness.py')
SUITE_FILES: frozenset[str] = frozenset({*CARRIED_SHA256, *STAGE3_FILES})

# Repository test-support files that EVERY child loads (the parent ``tests/m01`` conftest
# and the two modules it imports), bound to main 0731b520b7ee740223cf606fefd2e7e61106512f.
TEST_SUPPORT_SHA256: Mapping[str, str] = types.MappingProxyType({
    'tests/m01/conftest.py': '9a9362daa86bd33920c32507eb086bd8a031f75668a35614e32aa2003104ba5b',
    'tests/m01/recording.py': '14e3bd3bd70efaeabf966f5f5fc6669e2d278d172f2a9404d4fd7d9d6681fa3c',
    'tests/m01/typed_recording.py':
        '4ad3676095b505ea160eda14473be1218e31aeb5607f5f24f6e4eaba676d8881',
})


def suite_manifest_text(digests: Mapping[str, str]) -> str:
    """Canonical manifest: one ``<sha256>  <name>`` line per file, sorted by name, LF."""
    return ''.join(f'{digests[name]}  {name}\n' for name in sorted(digests))


def observe(path: Path) -> str:
    """TOTAL observation of one file (Codex G-C2 Stage 3 v2, blocker 2): the lowercase SHA-256
    of a regular file, or a marker -- ``MISSING``, ``WRONG_KIND:<kind>`` (a symlink, a
    directory, anything not a regular file) or ``UNREADABLE:<error>``. It NEVER raises, so a
    deleted or unreadable path is DATA, compared like any other value, and can never stop a
    checkpoint or the closure from being taken and saved."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return 'MISSING'
    except OSError as error:
        return f'UNREADABLE:{type(error).__name__}'
    if stat.S_ISLNK(mode):
        return 'WRONG_KIND:symlink'
    if not stat.S_ISREG(mode):
        return 'WRONG_KIND:not-a-regular-file'
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        return f'UNREADABLE:{type(error).__name__}'


def _py_names(directory: Path) -> tuple[set[str], list[str]]:
    """The ``.py`` names directly in ``directory``. Never raises."""
    try:
        return {entry.name for entry in os.scandir(directory)
                if entry.name.endswith('.py')}, []
    except OSError as error:
        return set(), [f'{directory}: UNREADABLE:{type(error).__name__}']


# Bytecode policy (Codex G-C2 Stage 3 v2, blocker 3). ``-B`` / PYTHONDONTWRITEBYTECODE stop
# WRITES but a valid cached ``.pyc`` can still be READ. So: (a) the parent and every child get
# ``sys.pycache_prefix`` / PYTHONPYCACHEPREFIX = a FRESH EMPTY per-run directory created
# before any suite or support import -- with a prefix set, CPython looks up cached bytecode
# only there; (b) because a SOURCELESS ``.pyc`` is importable regardless of the prefix, and
# because the Stage 1 B-24 grandchild builds its own explicit environment without the prefix,
# the run FAILS CLOSED if ANY bytecode artifact exists anywhere under the scanned roots;
# (c) the prefix directory itself must stay empty. Scope, disclosed: stdlib and third-party
# site-packages bytecode is outside this policy.
BYTECODE_SCAN_ROOTS: tuple[Path, ...] = (TESTS_M01, SRC)
BYTECODE_SUFFIXES: tuple[str, ...] = ('.pyc', '.pyo')


def bytecode_artifacts(prefix: Path | None) -> list[str]:
    """Every importable-cache artifact under the scanned roots and the per-run prefix. Never
    raises: a scan error is itself listed, so it fails closed."""
    found: list[str] = []

    def onerror(error: OSError) -> None:
        found.append(f'SCAN_ERROR:{error.filename}:{type(error).__name__}')

    for root in BYTECODE_SCAN_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
            for name in (*dirnames, *filenames):
                if name == '__pycache__' or name.endswith(BYTECODE_SUFFIXES):
                    found.append(str(Path(dirpath, name)))
    if prefix is not None:
        for dirpath, dirnames, filenames in os.walk(prefix, onerror=onerror):
            found += [f'PREFIX_NOT_EMPTY:{Path(dirpath, n)}' for n in (*dirnames, *filenames)]
    return sorted(found)


def suite_observation(manifest_sha256: str, prefix: Path | None) -> dict[str, Any]:
    """The suite side of every integrity observation, RE-AUTHORIZED EACH TIME (blocker 1): the
    exact 13-file set; the nine carried files == their accepted constants; the canonical
    manifest of the files present == the authorization-named SHA-256; the test-support files
    == main; no bytecode artifact. Reads bytes only; imports nothing; never raises."""
    present, problems = _py_names(HERE)
    digests = {name: observe(HERE / name) for name in sorted(present)}
    if present != SUITE_FILES:
        problems.append(f'suite file set: missing={sorted(SUITE_FILES - present)} '
                        f'extra={sorted(present - SUITE_FILES)}')
    problems += [f'{name}: {digests.get(name, "MISSING")} != accepted {want}'
                 for name, want in CARRIED_SHA256.items() if digests.get(name) != want]
    manifest_observed = hashlib.sha256(
        suite_manifest_text(digests).encode('utf-8')).hexdigest()
    if manifest_observed != manifest_sha256:
        problems.append(f'suite manifest {manifest_observed} != authorized {manifest_sha256}')
    support = {rel: observe(HALOFLOW_ROOT / rel) for rel in TEST_SUPPORT_SHA256}
    problems += [f'{rel}: {support[rel]} != main {want}'
                 for rel, want in TEST_SUPPORT_SHA256.items() if support[rel] != want]
    bytecode = bytecode_artifacts(prefix)
    problems += [f'bytecode artifact: {b}' for b in bytecode]
    return {'suite': digests, 'suite_manifest_sha256': manifest_observed,
            'test_support': support, 'bytecode': bytecode, 'problems': problems}


# ------------------------------------------------------------------ case sets

A_IDS: tuple[str, ...] = tuple(f'A-{n:02d}' for n in range(1, 30))
B_IDS_BASELINE: tuple[str, ...] = tuple(f'B-{n:02d}' for n in range(1, 24))   # B-24 excluded
ROW_IDS: tuple[str, ...] = tuple(f'PC-{n:02d}' for n in range(1, 10))
CONTROL_IDS: tuple[str, ...] = ('PD-01', 'PD-02', 'PD-03', 'PD-04')
K_IDS: tuple[str, ...] = tuple(f'K-{n:02d}' for n in range(1, 13))

# Ids carried by more than one collected item (Stage 1 README section 2: parametrized ids and
# sub-cases). Every other id must be collected EXACTLY once.
MULTI_ITEM_IDS: Mapping[str, int] = types.MappingProxyType({
    'A-21': 2, 'A-22': 2, 'A-23': 2, 'B-19': 2, 'B-20': 2,
    'K-02': 4, 'K-04': 2, 'K-06': 2,
})

# ------------------------------------------------------------------ expectations


@dataclass(frozen=True)
class Expect:
    """One design expectation for one case under one mutant.

    ``outcome`` 'PASS': every phase of every item passed. 'FAIL': the FIRST failure is the
    frozen assertion ``assertion`` in the CALL phase -- and, where one id carries several
    subchecks, with exactly the ``detail`` text of the named subcheck. Anything else is a
    WRONG-REASON death, never a kill. ``credit`` is the class the ACCEPTED design assigns;
    it is carried into the report, never upgraded."""

    outcome: str
    assertion: str | None = None
    detail: str | None = None
    credit: str = ''


def _pass(credit: str = 'must EXECUTE and PASS') -> Expect:
    return Expect('PASS', credit=credit)


def _fail(assertion: str, credit: str, detail: str | None = None) -> Expect:
    return Expect('FAIL', assertion, detail, credit)


_EA3_IS = 'L1: the object I2 returned at I1 is not E1'
_OD26 = ('A8 INDEPENDENT DISCRIMINATOR -- NOT credited to M3a (OD-SIG-26); '
         'a first failure at A1..A7 is a wrong-reason death')

# Kill-run v7 section 3 + OD-SIG-26; Part E v2 section 6.2; E0 v2. Nothing else.
EXPECTATIONS: Mapping[str, Mapping[str, Expect]] = types.MappingProxyType({
    'M4': types.MappingProxyType({
        'B-24': _pass('KILLER: outer B-24 -- suite-level setup failure, child exit 97, zero '
                      'bodies (kill-run v7 section 4); child A-26'),
    }),
    'M1': types.MappingProxyType({
        **{c: _fail('A7', 'KILLER (kill-run v7 section 5)') for c in ROW_IDS[:8]},
        'PC-09': _pass('must EXECUTE and PASS -- phase pin O11, outside the swap'),
        'PE-01': _pass('must PASS -- no phase asserted (Part E v2 section 6.2)'),
    }),
    'M2': types.MappingProxyType({
        **{c: _pass('must EXECUTE and PASS -- evidence both groups executed')
           for c in ('PC-01', 'PC-02', 'PC-03', 'PC-04', 'PC-05')},
        **{c: _fail('A4', 'KILLER, required first failure (kill-run v7 section 6)')
           for c in ('PC-06', 'PC-07', 'PC-08')},
        'PC-09': _fail('A8', 'A8 INDEPENDENT DISCRIMINATOR at the non-terminal index 3 '
                             '(kill-run v7 sections 6, 10)'),
        'PE-01': _pass('must PASS -- terminal site outside the collapse (Part E v2 6.2)'),
    }),
    'M3a': types.MappingProxyType({
        'PC-01': _fail('A5', 'KILLER, row (weak) (kill-run v7 section 7)'),
        'PC-02': _fail('A5', 'KILLER, row (weak) (kill-run v7 section 7)'),
        **{c: _fail('A8', _OD26) for c in ROW_IDS[2:]},
        'PD-01': _fail('D3', 'KILLER, control (strong) -- M3a FULL kill needs this AND '
                             'PC-01/PC-02 (kill-run v7 section 7, OI-READY)'),
        'PE-01': _pass('must PASS -- no edge asserted (Part E v2 6.2)'),
    }),
    'M3b': types.MappingProxyType({
        **{c: _pass('must EXECUTE and PASS -- every QUALIFIED edge already CREATE')
           for c in ROW_IDS[:8]},
        'PC-09': _fail('A8', 'MEASURED VISIBLE -- KILLER CREDIT QUARANTINED; NOT an accepted '
                             'M3b killer (kill-run v7 section 8)'),
        'PD-01': _fail('D3', 'KILLER -- the only credited M3b killer; C01 mandatory '
                             '(kill-run v7 section 8)'),
        'PE-01': _pass('must PASS -- no edge asserted (Part E v2 6.2)'),
    }),
    'M5': types.MappingProxyType({
        **{c: _fail('A6', 'KILLER (kill-run v7 section 9)') for c in ROW_IDS},
        'PE-01': _fail('EA6', 'KILLER, outer placement (Part E v2 section 6.1)',
                       'L3: the object reaching the public catch is not E1'),
    }),
    'M-O11-L1': types.MappingProxyType({
        'PE-01': _fail('EA3', 'KILLER, the `is` subcheck; counts hold (Part E v2 6.2)',
                       _EA3_IS),
    }),
    'M-O11-L2': types.MappingProxyType({
        'PE-01': _fail('EA4', 'KILLER (Part E v2 section 6.2)',
                       'L2: the inventory slot at I1 exception exit is not the installed '
                       'I4 marker'),
    }),
})

# Harness facts each mutant's run must show, per case, or the observation is not the
# defined mutant and is a wrong-reason result. Each returns a list of failures.
FactCheck = Callable[[str, str, Mapping[str, Any]], list[str]]


def _eq(facts: Mapping[str, Any], key: str, want: Any) -> list[str]:
    got = facts.get(key)
    return [] if got == want and type(got) is type(want) else [f'{key}={got!r}, required {want!r}']


def expected_passthrough(mutant: str, case_id: str) -> int:
    """Exact later ``sc.run`` calls (Codex G-C2 hardening; Q5). A PASSING PE-01 reaches EA7,
    whose two global no-stack proofs (I1, I4) each call ``sc.run`` once: 2. Every accepted
    PE-01 first failure (EA3, EA4, EA6) precedes EA7: 0. Rows and PD-01 call it once: 0."""
    if case_id == 'PE-01' and EXPECTATIONS[mutant][case_id].outcome == 'PASS':
        return 2
    return 0


def _common(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    return [*_eq(f, 'mutant', mutant), *_eq(f, 'case_id', case_id),
            *_eq(f, 'primary_runs', 1), *_eq(f, 'run_slot_restored', True),
            *_eq(f, 'passthrough_runs', expected_passthrough(mutant, case_id))]


def _facts_m1(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    return [*_common(mutant, case_id, f), *_eq(f, 'm1_installed', True),
            *_eq(f, 'm1_restored', True)]


# COMPARE events in each subject's pinned ledger (packet v7 section 3.5): the number of
# rewrites M2 must make. Zero on PC-01..PC-05 is the evidence nothing else was touched.
M2_EXPECTED_REWRITES: Mapping[str, int] = types.MappingProxyType({
    'PC-01': 0, 'PC-02': 0, 'PC-03': 0, 'PC-04': 0, 'PC-05': 0,
    'PC-06': 1, 'PC-07': 1, 'PC-08': 1, 'PC-09': 1, 'PE-01': 1,
})


def _facts_m2(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    return [*_common(mutant, case_id, f), *_eq(f, 'm2_chosen', 'SIG.CREATE.IDENTITY'),
            *_eq(f, 'm2_seam_restored', True),
            *_eq(f, 'm2_rewrites', M2_EXPECTED_REWRITES[case_id])]


def _facts_m3(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    out = [*_common(mutant, case_id, f), *_eq(f, 'm3_projected', True),
           *_eq(f, 'm3_slot_restored', True)]
    if f.get('m3_projection_calls') != f.get('m3_raw_qualified'):
        out.append('projection calls != raw SIG.QUALIFIED count')
    return out


def _facts_m5(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    return [*_common(mutant, case_id, f), *_eq(f, 'm5_substitutions', 1),
            *(k for key in ('m5_e1_is_observer_recorded', 'm5_public_is_e2', 'm5_e2_is_not_e1',
                            'm5_same_class', 'm5_same_reason_code') for k in _eq(f, key, True))]


def _facts_l1(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    return [*_common(mutant, case_id, f), *_eq(f, 'l1_substitutions', 1),
            *_eq(f, 'l1_slot_restored', True)]


def _facts_l2(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    return [*_common(mutant, case_id, f), *_eq(f, 'l2_rebind_out', 1),
            *_eq(f, 'l2_rebind_back', 1), *_eq(f, 'l2_back_in_inner_finally', True),
            *_eq(f, 'l2_observation_abort', None), *_eq(f, 'l2_event_count', 16),
            *_eq(f, 'l2_ledger_unchanged', True)]


def _facts_m4(mutant: str, case_id: str, f: Mapping[str, Any]) -> list[str]:
    """M4 runs B-24 with the plugin in NONE mode: ``sc.run`` must be untouched."""
    return [*_eq(f, 'mutant', 'NONE'), *_eq(f, 'run_slot_original_before', True),
            *_eq(f, 'run_slot_original_after', True)]


FACT_CHECKS: Mapping[str, FactCheck] = types.MappingProxyType({
    'M4': _facts_m4, 'M1': _facts_m1, 'M2': _facts_m2, 'M3a': _facts_m3, 'M3b': _facts_m3,
    'M5': _facts_m5, 'M-O11-L1': _facts_l1, 'M-O11-L2': _facts_l2,
})

# ------------------------------------------------------------------ helpers


def _import_suite() -> tuple[types.ModuleType, types.ModuleType]:
    """Only ever called AFTER the suite observation passed and the bytecode prefix is set."""
    for p in (str(HERE), str(TESTS_M01), str(SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import sig_cases as sc
    import sig_overlay as ov

    return ov, sc


@dataclass(frozen=True)
class Bound:
    """The suite's bound constants, copied out ONCE after import, so an observation never
    has to touch a module (and a later observation cannot be affected by one)."""

    manifest_sha256: str
    protected: Mapping[str, str]
    inputs: Mapping[str, str]
    fixture_dir: Path
    g2: Mapping[str, str]                 # authorized path -> authorized sha256
    prefix: Path | None


def integrity_snapshot(bound: Bound) -> dict[str, Any]:
    """Everything whose bytes the evidence depends on, observed now, nothing cached, TOTAL:
    the suite side re-authorized in full (blocker 1), the protected inputs, the bound fixture
    inputs, and the G-2 artifacts against their AUTHORIZED hashes. ``mismatches`` lists every
    departure from an authorized or bound value. Never raises (blocker 2)."""
    try:
        suite = suite_observation(bound.manifest_sha256, bound.prefix)
        protected = {rel: observe(HALOFLOW_ROOT / rel) for rel in bound.protected}
        inputs = {rel: observe(bound.fixture_dir / rel) for rel in bound.inputs}
        g2 = {path: observe(Path(path)) for path in bound.g2}
        mismatches = list(suite['problems'])
        mismatches += [f'{rel}: {protected[rel]}' for rel, want in bound.protected.items()
                       if protected[rel] != want]
        mismatches += [f'fixtures/{rel}: {inputs[rel]}' for rel, want in bound.inputs.items()
                       if inputs[rel] != want]
        mismatches += [f'G-2 {path}: {g2[path]}' for path, want in bound.g2.items()
                       if g2[path] != want]
        return {**suite, 'protected': protected, 'inputs': inputs, 'g2': g2,
                'mismatches': mismatches}
    except Exception as error:        # defensive: an observation is never an exception
        return {'mismatches': [f'SNAPSHOT_ERROR:{type(error).__name__}:{error}']}


INTEGRITY_KEYS: tuple[str, ...] = ('suite', 'suite_manifest_sha256', 'test_support',
                                   'bytecode', 'protected', 'inputs', 'g2')


def integrity_drift(before: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """Departures from the start AND from every authorized value. Never raises."""
    drift = [f'{key}: changed' for key in INTEGRITY_KEYS if now.get(key) != before.get(key)]
    return drift + [f'mismatch: {m}' for m in now.get('mismatches', ['SNAPSHOT_MISSING'])]


def binding_stage(ov: types.ModuleType, sc: types.ModuleType) -> dict[str, Any]:
    """F4, read literally: source hash, THEN X3a/X3b/X3c for every subject, THEN the
    S1..S14 resolver invariants. Returns evidence; ``ok`` False means STOP."""
    evidence: dict[str, Any] = {'ok': False, 'order': []}
    observed = observe(ov.POLICY_PATH)
    evidence['source_sha256'] = observed
    evidence['order'].append('source')
    if observed != ov.EXPECTED_SNAPSHOT_SHA256:
        evidence['failure'] = 'SOURCE_HASH_MISMATCH'
        return evidence
    x3: dict[str, Any] = {}
    try:
        for case_id in sc.ROWS:
            b = sc.bind_row(case_id)
            x3[case_id] = {'subject': b.subject, 'x3a': b.x3a, 'x3b': b.x3b, 'x3c': b.x3c}
        for control in ('C01', 'C02', 'C03'):
            b = sc.bind_control(control)
            x3[control] = {'subject': b.subject, 'x3a': b.x3a,
                           'x3b': 'NOT APPLICABLE (OD-SIG-20)', 'x3c': b.x3c}
    except sc.CaseAbort as abort:
        evidence['x3'] = x3
        evidence['failure'] = f'X3:{abort.diagnosis}'
        return evidence
    evidence['x3'] = x3
    evidence['order'].append('x3')
    try:
        resolution = ov.preflight()
    except ov.PreflightAbort as abort:
        evidence['failure'] = f'PREFLIGHT:{abort.diagnosis}'
        evidence['ledger'] = list(abort.ledger)
        return evidence
    evidence['ledger'] = list(resolution.ledger)
    if tuple(resolution.ledger) != tuple(ov.STAGES):
        evidence['failure'] = 'PREFLIGHT_PREFIX'
        return evidence
    evidence['order'].append('resolver')
    evidence['ok'] = True
    return evidence


def child_env(mutant: str, cases: Sequence[str], results: Path,
              prefix: Path) -> dict[str, str]:
    """Explicit minimal environment, as B-24 (Stage 1 D20). The M4 variable is never set.
    PYTHONPYCACHEPREFIX is the per-run EMPTY prefix (bytecode policy)."""
    return {
        'PATH': os.environ.get('PATH', ''),
        'PYTHONHASHSEED': '0',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'PYTHONPATH': os.pathsep.join([str(HERE), str(TESTS_M01), str(SRC)]),
        'HALOFLOW_SIG_KILLRUN_MUTANT': mutant,
        'HALOFLOW_SIG_KILLRUN_CASES': ','.join(cases),
        'HALOFLOW_SIG_KILLRUN_RESULTS': str(results),
        'PYTHONPYCACHEPREFIX': str(prefix),
    }


_PHASES = ('setup', 'call', 'teardown')


def results_schema_problems(results: Any) -> list[str]:
    """The child results file, validated by SHAPE before anything reads it. Any problem is a
    named run failure, never an exception and never a result."""
    if not isinstance(results, dict):
        return ['results is not an object']
    problems: list[str] = []
    types_required: Mapping[str, type] = {'mutant': str, 'requested': list, 'collected': dict,
                                          'items': dict, 'exitstatus': int, 'python': str,
                                          'executable': str}
    for key, kind in types_required.items():
        if type(results.get(key)) is not kind:
            problems.append(f'{key}: expected {kind.__name__}')
    if problems:
        return problems
    for cid, count in results['collected'].items():
        if type(cid) is not str or type(count) is not int:
            problems.append(f'collected[{cid!r}] malformed')
    for nodeid, item in results['items'].items():
        if not isinstance(item, dict) or not isinstance(item.get('phases'), dict):
            problems.append(f'items[{nodeid}] malformed')
            continue
        if type(item.get('case_id')) is not str:
            problems.append(f'items[{nodeid}].case_id malformed')
        for when, phase in item['phases'].items():
            if when not in _PHASES or not isinstance(phase, dict) \
                    or type(phase.get('outcome')) is not str \
                    or not (phase.get('failure') is None or isinstance(phase.get('failure'), dict)):
                problems.append(f'items[{nodeid}].phases[{when}] malformed')
        facts = item.get('facts')
        if 'call' in item['phases'] and not (facts is None or isinstance(facts, dict)):
            problems.append(f'items[{nodeid}].facts malformed')
        if not isinstance(item.get('b24_kill_lines', []), list):
            problems.append(f'items[{nodeid}].b24_kill_lines malformed')
    return problems


def run_child(stage: str, mutant: str, cases: Sequence[str], out: Path,
              timeout: int, prefix: Path) -> dict[str, Any]:
    results = out / f'{stage}.results.json'
    command = [sys.executable, '-B', '-P', '-m', 'pytest', '-p', 'sig_killrun_plugin',
               '-p', 'no:cacheprovider', '-q', 'tests/m01/phase_signature']
    record: dict[str, Any] = {'stage': stage, 'mutant': mutant, 'cases': list(cases),
                              'command': command}
    try:
        proc = subprocess.run(command, cwd=HALOFLOW_ROOT,
                              env=child_env(mutant, cases, results, prefix),
                              capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        record['failure'] = 'CHILD_TIMEOUT'          # a hung child is never a result
        return record
    (out / f'{stage}.stdout.txt').write_text(proc.stdout, encoding='utf-8')
    (out / f'{stage}.stderr.txt').write_text(proc.stderr, encoding='utf-8')
    record['returncode'] = proc.returncode
    if not results.exists():
        record['failure'] = 'RESULTS_MISSING'
        return record
    try:
        loaded = json.loads(results.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        record['failure'] = f'RESULTS_INVALID_JSON: {type(error).__name__}'
        return record
    schema = results_schema_problems(loaded)
    if schema:
        record['failure'] = f'RESULTS_SCHEMA_INVALID: {schema[:20]}'
        return record
    # Metadata by EQUALITY (Codex G-C2 hardening), besides the collection check.
    metadata = [f'{key}: {loaded[key]!r} != {want!r}' for key, want in (
        ('mutant', mutant), ('requested', list(cases)), ('exitstatus', proc.returncode),
        ('python', sys.version), ('executable', sys.executable)) if loaded[key] != want]
    if metadata:
        record['failure'] = f'RESULTS_METADATA_MISMATCH: {metadata}'
        return record
    record['results'] = loaded
    return record


def selection_problems(requested: Sequence[str], collected: Mapping[str, int]) -> list[str]:
    """The child's selection, by EQUALITY: every requested id collected, the exact number of
    items, and nothing else."""
    problems = [f'{cid}: collected {collected.get(cid, 0)}, expected '
                f'{MULTI_ITEM_IDS.get(cid, 1)}'
                for cid in requested if collected.get(cid, 0) != MULTI_ITEM_IDS.get(cid, 1)]
    problems += [f'{cid}: collected but not requested' for cid in collected
                 if cid not in requested]
    return problems


def case_outcomes(child: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per case id: PASS iff every item passed setup, call and teardown; otherwise the
    first failure of the first failing item, in collection order, with its phase."""
    results = child.get('results') or {}
    per_case: dict[str, dict[str, Any]] = {}
    for nodeid, item in (results.get('items') or {}).items():
        cid = item.get('case_id')
        case = per_case.setdefault(cid, {'outcome': 'PASS', 'items': [], 'facts': [],
                                         'b24_kill_lines': []})
        case['items'].append(nodeid)
        case['facts'].append(item.get('facts'))
        case['b24_kill_lines'] += item.get('b24_kill_lines') or []
        phases = item.get('phases') or {}
        for when in _PHASES:
            phase = phases.get(when)
            if phase is None:
                if when == 'call' and (phases.get('setup') or {}).get('outcome') != 'passed':
                    break                           # setup failed: no call phase, recorded
                if case['outcome'] == 'PASS':
                    case.update(outcome='FAIL', phase=when, failure={'kind': 'missing_phase'})
                break
            if phase.get('outcome') != 'passed' and case['outcome'] == 'PASS':
                case.update(outcome='FAIL', phase=when,
                            failure=phase.get('failure') or {'kind': phase.get('outcome')})
    return per_case


def judge(mutant: str, case_id: str, observed: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare ONE observation with ONE accepted expectation. Returns a verdict:
    EXPECTATION_MET or WRONG (with a named reason). Never 'credited'."""
    expect = EXPECTATIONS[mutant][case_id]
    verdict: dict[str, Any] = {'case': case_id, 'expected': dataclasses.asdict(expect),
                               'credit': expect.credit}
    if observed is None:
        return {**verdict, 'verdict': 'WRONG', 'reason': 'NOT_EXECUTED'}
    verdict['observed'] = {k: observed.get(k) for k in ('outcome', 'phase', 'failure')}
    facts_problems: list[str] = []
    for facts in observed.get('facts') or [None]:
        facts_problems += (['no harness facts recorded'] if facts is None
                           else FACT_CHECKS[mutant](mutant, case_id, facts))
    if mutant == 'M4' and len(observed.get('b24_kill_lines') or []) != 1:
        facts_problems.append('B-24 kill line not printed exactly once')
    if facts_problems:
        return {**verdict, 'verdict': 'WRONG', 'reason': 'MUTANT_NOT_AS_DEFINED',
                'facts_problems': facts_problems}
    if expect.outcome == 'PASS':
        if observed['outcome'] == 'PASS':
            return {**verdict, 'verdict': 'EXPECTATION_MET'}
        return {**verdict, 'verdict': 'WRONG', 'reason': 'UNEXPECTED_FAILURE'}
    if observed['outcome'] == 'PASS':
        return {**verdict, 'verdict': 'WRONG', 'reason': 'SURVIVED'}
    failure = observed.get('failure') or {}
    if observed.get('phase') != 'call' or failure.get('kind') != 'assertion':
        return {**verdict, 'verdict': 'WRONG', 'reason': 'WRONG_REASON_DEATH'}
    if failure.get('id') != expect.assertion:
        return {**verdict, 'verdict': 'WRONG', 'reason': 'WRONG_FIRST_ASSERTION'}
    if expect.detail is not None and failure.get('detail') != expect.detail:
        return {**verdict, 'verdict': 'WRONG', 'reason': 'WRONG_SUBCHECK'}
    return {**verdict, 'verdict': 'EXPECTATION_MET'}


# ------------------------------------------------------------------ executor


@dataclass(frozen=True)
class G2Binding:
    """The authorization-named G-2 artifacts (OD-SIG-25 path ii; Codex Q2)."""

    evidence: Path
    evidence_sha256: str
    receipt: Path
    receipt_sha256: str


class IntegrityStop(Exception):
    """Integrity drift after execution started. Stops the run at once."""


class Executor:
    def __init__(self, out: Path, timeout: int, suite_manifest_sha256: str,
                 g2: G2Binding | None, prefix: Path) -> None:
        self.out = out
        self.timeout = timeout
        self.suite_manifest_sha256 = suite_manifest_sha256
        self.g2 = g2
        self.prefix = prefix
        self.report: dict[str, Any] = {'stages': [], 'mutants': [], 'checkpoints': [],
                                       'verdict': None}
        self.passed: dict[str, bool] = {}
        self.bound: Bound | None = None
        self.before: dict[str, Any] | None = None       # set once execution may start

    def _checkpoint(self, label: str) -> None:
        """After EVERY child: the full, total integrity comparison, the suite re-authorized.
        Drift stops the run at once."""
        assert self.bound is not None and self.before is not None
        now = integrity_snapshot(self.bound)
        drift = integrity_drift(self.before, now)
        self.report['checkpoints'].append({'after': label, 'drift': drift})
        if drift:
            raise IntegrityStop(f'integrity drift after {label}: {drift}')

    def _stage(self, stage: str, mutant: str, cases: Sequence[str]) -> dict[str, dict[str, Any]]:
        child = run_child(stage, mutant, cases, self.out, self.timeout, self.prefix)
        problems = ([child['failure']] if 'failure' in child else
                    selection_problems(cases, child['results']['collected']))
        # pytest exit 0 = all passed, 1 = some failed. A baseline stage must exit 0; a mutant
        # stage 0 or 1. Anything else (interrupted, internal, usage, nothing collected) is a
        # problem with the run, never a result.
        allowed = (0,) if mutant == 'NONE' and stage != 'M-M4' else (0, 1)
        if 'failure' not in child and child.get('returncode') not in allowed:
            problems.append(f'CHILD_EXIT_{child.get("returncode")}')
        outcomes = case_outcomes(child) if not problems else {}
        entry = {'stage': stage, 'mutant': mutant, 'cases': list(cases),
                 'returncode': child.get('returncode'), 'problems': problems,
                 'outcomes': outcomes}
        self.report['stages'].append(entry)
        if mutant == 'NONE':
            for cid in cases:
                ok = (not problems and outcomes.get(cid, {}).get('outcome') == 'PASS'
                      and all(f is not None and f.get('run_slot_original_before') is True
                              and f.get('run_slot_original_after') is True
                              for f in outcomes.get(cid, {}).get('facts') or [None]))
                self.passed[cid] = ok
        self._checkpoint(stage)
        return outcomes if not problems else {}

    def _finish(self, verdict: str, code: int) -> int:
        """Every terminal path. Once execution has started, the integrity CLOSURE is taken and
        saved here, whatever the verdict. Every observation is total, so the closure, the
        report and SHA256SUMS are always produced (blocker 2); a failure to WRITE them is
        reported on stderr and exits non-zero."""
        if self.bound is not None and self.before is not None:
            after = integrity_snapshot(self.bound)
            drift = integrity_drift(self.before, after)
            self.report['integrity_after'] = after
            self.report['integrity_closure'] = {'ok': not drift, 'drift': drift}
            if drift and code in (EXIT_OK, EXIT_G2_ABSENT):
                verdict, code = f'FAILED: integrity closure drift ({verdict})', EXIT_FAILED
        self.report['verdict'] = verdict
        self.report['exit_code'] = code
        try:
            (self.out / 'killrun-report.json').write_text(
                json.dumps(self.report, sort_keys=True, indent=1, default=str),
                encoding='utf-8')
            files = sorted(p for p in self.out.iterdir()
                           if p.name != 'SHA256SUMS' and not p.is_dir())
            sums = [f'{observe(p)}  {p.name}' for p in files]
            (self.out / 'SHA256SUMS').write_text('\n'.join(sums) + '\n', encoding='utf-8')
        except OSError as error:
            print(f'REPORT NOT WRITTEN: {type(error).__name__}: {error}; verdict was '
                  f'{verdict!r} (exit {code})', file=sys.stderr)
            return EXIT_FAILED
        return code

    def run(self) -> int:
        try:
            return self._run()
        except IntegrityStop as stop:
            return self._finish(f'STOPPED: {stop}', EXIT_FAILED)
        except Exception as error:          # never an unreported death once execution starts
            return self._finish(f'FAILED: executor error {type(error).__name__}: {error}',
                                EXIT_FAILED)

    def _run(self) -> int:
        self.report['bytecode_policy'] = {
            'pycache_prefix': str(self.prefix), 'parent_sys_pycache_prefix': sys.pycache_prefix,
            'children_PYTHONPYCACHEPREFIX': str(self.prefix),
            'dont_write_bytecode': sys.dont_write_bytecode,
            'fail_closed_scan_roots': [str(r) for r in BYTECODE_SCAN_ROOTS],
            'scope_excluded': 'stdlib and third-party site-packages bytecode'}
        # P -- the suite side, BEFORE any suite import or child (blockers 1 and 3).
        auth = suite_observation(self.suite_manifest_sha256, self.prefix)
        self.report['suite_authentication'] = auth
        if auth['problems']:
            return self._finish('REFUSED: suite authentication failed', EXIT_REFUSED)
        g2_expected: dict[str, str] = {}
        if self.g2 is not None:
            evidence = observe(self.g2.evidence)
            receipt = observe(self.g2.receipt)
            try:
                names_evidence = self.g2.evidence_sha256.encode('ascii') in \
                    self.g2.receipt.read_bytes()
            except OSError:
                names_evidence = False
            bound_ok = (evidence == self.g2.evidence_sha256
                        and receipt == self.g2.receipt_sha256 and names_evidence)
            self.report['g2'] = {**dataclasses.asdict(self.g2), 'bound': bound_ok,
                                 'evidence_observed': evidence, 'receipt_observed': receipt,
                                 'receipt_names_evidence': names_evidence}
            if not bound_ok:
                return self._finish('REFUSED: G-2 artifacts do not match the authorization, '
                                    'or the receipt does not name the evidence', EXIT_REFUSED)
            g2_expected = {str(self.g2.evidence): self.g2.evidence_sha256,
                           str(self.g2.receipt): self.g2.receipt_sha256}
        ov, sc = _import_suite()
        bound = Bound(self.suite_manifest_sha256, dict(ov.PROTECTED_SHA256),
                      dict(ov.EXPECTED_INPUT_SHA256), Path(ov.FIXTURE_DIR), g2_expected,
                      self.prefix)
        before = integrity_snapshot(bound)
        self.report['integrity_before'] = before
        if before['mismatches']:
            return self._finish('REFUSED: authorized or bound value mismatch before the run',
                                EXIT_REFUSED)
        self.bound, self.before = bound, before   # execution starts: closure on every path

        # B1 instrument, B2 controls -- neither needs G-2 (work plan v3 section 4 step 5:
        # "non-row setup and other independently safe checks may precede it"; Codex Q3).
        self._stage('B1-instrument', 'NONE', (*A_IDS, *B_IDS_BASELINE))
        if not all(self.passed.get(c) for c in (*A_IDS, *B_IDS_BASELINE)):
            return self._finish('FAILED: baseline instrument stage', EXIT_FAILED)
        self._stage('B2-controls', 'NONE', CONTROL_IDS)

        # G2 -- credit binding BEFORE any G2-gated case (blocker 2).
        if self.g2 is None:
            self.report['g2'] = {'bound': False}
            return self._finish('STOPPED: G-2 credit not bound; no row and no PE-01 run',
                                EXIT_G2_ABSENT)
        g2_gated = set(sc.G2_GATED)

        def runnable(cid: str) -> bool:
            return all(self.passed.get(b) for b in sc.BACKING_CASES[cid])

        # OI-READY (packet v7 section 2.5): a row is CONTROL-BACKED only once every backing
        # case has independently passed -- here, in this run, in an earlier process.
        self.report['control_backed'] = {c: runnable(c) for c in (*ROW_IDS, 'PE-02', 'PE-01')}
        rows = [c for c in ROW_IDS if runnable(c) and c in g2_gated]
        self.report['blocked'] = [c for c in ROW_IDS if c not in rows]
        for cid in self.report['blocked']:
            self.passed[cid] = False
        if rows:
            self._stage('B3-rows', 'NONE', rows)
        if runnable('PE-02'):
            self._stage('B4-pe02', 'NONE', ('PE-02',))
        else:
            self.passed['PE-02'] = False
        if runnable('PE-01') and 'PE-01' in g2_gated:
            self._stage('B5-pe01', 'NONE', ('PE-01',))
        else:
            self.passed['PE-01'] = False

        self._stage('H-harness', 'NONE', K_IDS)
        non_mutant = (*A_IDS, *B_IDS_BASELINE, *CONTROL_IDS, *ROW_IDS, 'PE-02', 'PE-01', *K_IDS)
        self.report['non_mutant_complete'] = all(self.passed.get(c) for c in non_mutant)
        if not self.report['non_mutant_complete']:
            self.report['non_mutant_failed'] = [c for c in non_mutant if not self.passed.get(c)]
            return self._finish('FAILED: the complete non-mutant set did not pass; '
                                'no mutant was run', EXIT_FAILED)

        # M -- mutants, M4 FIRST, each after its own binding stage. After an unmet
        # expectation the remaining mutants still run (Codex Q4: acceptable ONLY with a full
        # integrity checkpoint after each mutant child -- ``_stage`` takes it); binding or
        # integrity drift stops at once.
        all_met = True
        for mutant in ('M4', 'M1', 'M2', 'M3a', 'M3b', 'M5', 'M-O11-L1', 'M-O11-L2'):
            binding = binding_stage(ov, sc)
            entry: dict[str, Any] = {'mutant': mutant, 'binding': binding}
            self.report['mutants'].append(entry)
            if not binding['ok']:
                return self._finish(f'STOPPED: binding stage failed before {mutant}',
                                    EXIT_FAILED)
            cases = tuple(EXPECTATIONS[mutant])
            plugin_mutant = 'NONE' if mutant == 'M4' else mutant   # M4 reaches ONLY B-24's child
            outcomes = self._stage(f'M-{mutant}', plugin_mutant, cases)
            entry['verdicts'] = [judge(mutant, c, outcomes.get(c)) for c in cases]
            met = all(v['verdict'] == 'EXPECTATION_MET' for v in entry['verdicts'])
            entry['all_expectations_met'] = met
            all_met = all_met and met

        if not all_met:
            return self._finish('FAILED: at least one kill-run expectation was not met',
                                EXIT_FAILED)
        return self._finish('ALL EXPECTATIONS MET -- evidence only; credit is the owner\'s act',
                            EXIT_OK)


_HEX64 = frozenset('0123456789abcdef')


def _is_sha256(value: str | None) -> bool:
    return value is not None and len(value) == 64 and set(value) <= _HEX64


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0] if __doc__ else None)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--suite-manifest-sha256', required=True)
    parser.add_argument('--g2-evidence', type=Path)
    parser.add_argument('--g2-evidence-sha256')
    parser.add_argument('--g2-credit-receipt', type=Path)
    parser.add_argument('--g2-credit-receipt-sha256')
    parser.add_argument('--stage-timeout', type=int, default=STAGE_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True           # the executor writes nothing into the suite
    g2_args = (args.g2_evidence, args.g2_evidence_sha256, args.g2_credit_receipt,
               args.g2_credit_receipt_sha256)
    refusal = None
    if sys.version_info[:2] != REQUIRED_PYTHON:
        refusal = f'interpreter {sys.version.split()[0]} is not CPython 3.12'
    elif sys.implementation.name != 'cpython':
        refusal = f'implementation {sys.implementation.name} is not CPython'
    elif M4_ENV in os.environ:
        refusal = f'{M4_ENV} is set in the executor environment'
    elif args.out.exists():
        refusal = f'output directory {args.out} already exists; a run never reuses one'
    elif not _is_sha256(args.suite_manifest_sha256):
        refusal = '--suite-manifest-sha256 is not a lowercase SHA-256'
    elif any(a is None for a in g2_args) and not all(a is None for a in g2_args):
        refusal = 'the four --g2-* arguments must be given together, or not at all'
    elif args.g2_evidence is not None and not (
            _is_sha256(args.g2_evidence_sha256) and _is_sha256(args.g2_credit_receipt_sha256)
            and args.g2_evidence.is_file() and args.g2_credit_receipt.is_file()):
        refusal = 'a --g2-* hash is malformed or a G-2 file does not exist'
    if refusal is not None:
        print(f'REFUSED: {refusal}', file=sys.stderr)
        return EXIT_REFUSED
    g2 = None if args.g2_evidence is None else G2Binding(
        args.g2_evidence.resolve(), args.g2_evidence_sha256,
        args.g2_credit_receipt.resolve(), args.g2_credit_receipt_sha256)
    args.out.mkdir(parents=True)
    out = args.out.resolve()
    # Bytecode policy: a FRESH EMPTY per-run cache root, set BEFORE any suite import.
    prefix = out / 'bytecode-cache-empty'
    prefix.mkdir()
    sys.pycache_prefix = str(prefix)
    executor = Executor(out, args.stage_timeout, args.suite_manifest_sha256, g2, prefix)
    executor.report['interpreter'] = {'executable': sys.executable, 'version': sys.version,
                                      'platform': sys.platform}
    code = executor.run()
    print(f'{executor.report["verdict"]} (exit {code}); report: '
          f'{args.out / "killrun-report.json"}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
