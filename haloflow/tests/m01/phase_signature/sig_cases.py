"""PHASE-SIGNATURE-01 Gate 3 support for Parts C, D and E (test overlay). NOT production code.

Frozen design (owner freeze OD-SIG-24): Parts C/D packet v7 (2cf9ebcf...), kill-run v7
(1f227404...), Part E packet v2 (178ce928...), E0 O11 mutant definitions v2 (29bf2d86...).

This module adds NO seam, NO slot and NO instrument. It uses the Stage 1 instrument in
``sig_overlay`` unchanged. It holds:

* the pinned per-subject expectations (packet v7 sections 3.2, 3.5, 4.1, 4.3);
* the X3 input binding (packet v7 sections 2.2 / 2.2a, OD-SIG-20), evaluated afresh in
  every case, before its run; the bytes hashed for X3c are the object validated;
* the ordered assertion helper ``check`` -- a failing assertion is a
  ``SigAssertionFailed`` carrying its frozen id, so "the first failing assertion" is a
  machine-readable fact (packet v7 section 2.3; Part E v2 section 3 item 3);
* ``CaseAbort`` -- a failed precondition or contract check. It is NOT an
  ``AssertionError``, and it is never reported as an assertion result;
* the execution-order metadata the Stage 3 staged executor enforces. Nothing here
  enforces order: a comment or a table is not enforcement (Codex, G-C2 Stage 1 v2).

Every expected value below is PROVISIONAL Python 3.11.15 (OD-SIG-15 v2, OD-SIG-17); CI
pins 3.12. A 3.11 / 3.12 difference is an observer portability finding, never grounds
to weaken an oracle.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import sig_overlay as ov

# ------------------------------------------------------------------ assertions


class SigAssertionFailed(AssertionError):
    """A frozen assertion failed. ``assertion_id`` is the frozen id (A1..A8, D1..D7,
    PD-nn.Rn, EA1..EA7, EC1..EC7)."""

    def __init__(self, assertion_id: str, detail: str) -> None:
        super().__init__(f'{assertion_id}: {detail}')
        self.assertion_id = assertion_id
        self.detail = detail


def check(assertion_id: str, condition: bool, detail: str) -> None:
    """Evaluate ONE frozen assertion. Only the bool ``True`` passes."""
    if condition is not True:
        raise SigAssertionFailed(assertion_id, detail)


class CaseAbort(Exception):
    """A precondition or contract check failed. Fail closed with a NAMED diagnosis;
    never counted as a pass and never reported as an assertion result."""

    def __init__(self, diagnosis: str, **details: Any) -> None:
        super().__init__(f'{diagnosis} {details}')
        self.diagnosis = diagnosis
        self.details = details


class CaseDiag:
    """Case-level diagnoses. Asserted by EQUALITY only, never by substring."""

    PREFLIGHT_PREFIX = 'SIG.CASE.PREFLIGHT_PREFIX'            # S1..S14 not the full prefix
    SUBJECT_NOT_UNIQUE = 'SIG.CASE.SUBJECT_NOT_UNIQUE'        # row/control not exactly once
    ROW_CONTROL_UNEXPECTED = 'SIG.CASE.ROW_CONTROL_UNEXPECTED'
    X3A_RECORD_MISMATCH = 'SIG.CASE.X3A_RECORD_MISMATCH'      # fixture-hashes.json itself
    X3A_MISMATCH = 'SIG.CASE.X3A_MISMATCH'
    X3B_ENCODING_UNEXPECTED = 'SIG.CASE.X3B_ENCODING_UNEXPECTED'
    X3B_RECORD_UNEXPECTED = 'SIG.CASE.X3B_RECORD_UNEXPECTED'
    X3B_MISMATCH = 'SIG.CASE.X3B_MISMATCH'
    X3C_MISMATCH = 'SIG.CASE.X3C_MISMATCH'
    CONTROL_SOURCE_UNEXPECTED = 'SIG.CASE.CONTROL_SOURCE_UNEXPECTED'
    CONTROL_CORROBORATION_FAILED = 'SIG.CASE.CONTROL_CORROBORATION_FAILED'
    OBSERVATION_ABORTED = 'SIG.CASE.OBSERVATION_ABORTED'      # an ObservationAbort fired
    UNEXPECTED_OUTCOME = 'SIG.CASE.UNEXPECTED_OUTCOME'        # neither accepted nor refused
    UNKNOWN_PRESENT = 'SIG.CASE.UNKNOWN_PRESENT'              # X1
    NOT_RESTORED = 'SIG.CASE.NOT_RESTORED'                    # X2
    I1_NOT_INSTALLED = 'SIG.CASE.I1_NOT_INSTALLED'            # Part E preconditions
    I2_NOT_IDENTITY = 'SIG.CASE.I2_NOT_IDENTITY'
    I4_BINDING_UNVERIFIED = 'SIG.CASE.I4_BINDING_UNVERIFIED'


# ----------------------------------------------------------------- subjects

Ledger = tuple[tuple[str, str | None, str], ...]

_Q, _CN, _CI, _CC = ov.SIG_QUALIFIED, ov.SIG_CREATE_NAME, ov.SIG_CREATE_IDENTITY, \
    ov.SIG_CREATE_COMPARE
_TA, _TS, _TE, _TM = ov.SIG_TARGET_ARGS_UNSPEC, ov.SIG_TARGET_DUAL_SHAPE, \
    ov.SIG_TARGET_DUAL_EQUAL, ov.SIG_TARGET_MEMBERSHIP
_EC, _ET, _EI = ov.EDGE_CREATE, ov.EDGE_TARGET, ov.EDGE_INVENTORY

_L_QUALIFIED_FAIL: Ledger = ((_Q, _EC, 'fail'),)
_L_NAME_FAIL: Ledger = ((_Q, _EC, 'pass'), (_CN, None, 'fail'))
_L_IDENTITY_FAIL: Ledger = ((_Q, _EC, 'pass'), (_CN, None, 'pass'), (_CI, None, 'fail'))
_L_COMPARE_FAIL: Ledger = ((_Q, _EC, 'pass'), (_CN, None, 'pass'), (_CI, None, 'pass'),
                           (_CC, None, 'fail'))
_L_MEMBERSHIP_FAIL: Ledger = (
    (_Q, _EC, 'pass'),      # 0
    (_CN, None, 'pass'),    # 1
    (_CI, None, 'pass'),    # 2
    (_CC, None, 'pass'),    # 3  non-terminal COMPARE (M2 sees it through A8 only)
    (_TA, None, 'pass'),    # 4
    (_Q, _ET, 'pass'),      # 5  TARGET edge (M3b-visible; killer credit QUARANTINED)
    (_TS, None, 'pass'),    # 6
    (_TS, None, 'pass'),    # 7
    (_TE, None, 'pass'),    # 8
    (_TM, None, 'pass'),    # 9
    (_TA, None, 'pass'),    # 10
    (_Q, _ET, 'pass'),      # 11 TARGET edge
    (_TS, None, 'pass'),    # 12
    (_TS, None, 'pass'),    # 13
    (_TE, None, 'pass'),    # 14
    (_TM, None, 'fail'),    # 15 terminal
)


@dataclass(frozen=True)
class RowSpec:
    """One Part C row, packet v7 sections 3.2 and 3.5. Every value PINNED here."""

    case_id: str                   # PC-nn
    row: str                       # sql-fixtures.json variants[].case_id
    control: str                   # the variant's backing control
    site: str                      # A4
    edge: str | None               # A5 -- None is a value, never "not applicable"
    phase: str                     # A7
    ledger: Ledger                 # A8
    x3b: str                       # the variant's recorded payload_sha256
    x3c: str                       # validated-byte digest


ROWS: Mapping[str, RowSpec] = types.MappingProxyType({r.case_id: r for r in (
    RowSpec('PC-01', 'A-wrong-create-schema', 'C01', _Q, _EC, 'O08', _L_QUALIFIED_FAIL,
            'a9e9c3b226dc50c4d7a7c8a9d15b8462af984ec8b4b8399f086791117f9eb884',
            '186df836ba2e9b0ce0d439bfd0d39e19c7973f79faf7b279c7ce0114c6283116'),
    RowSpec('PC-02', 'A-unqualified-create', 'C01', _Q, _EC, 'O08', _L_QUALIFIED_FAIL,
            'cc4b1df4aa53b15735ebe2b99cbc19f0e88926f4dc95b10ca6bbb8ce33daa187',
            'ce8daf6e25752ed8b5f933e92c1e05ad81db11509fe2f6b3c37c538e700c43ba'),
    RowSpec('PC-03', 'A-wrong-create-name', 'C01', _CN, None, 'O08', _L_NAME_FAIL,
            '1b2f56605c4ade9c4cfbdb9850df738cf838acdfb55561c5cbbf54383e3d044c',
            '0feef73f4f4b0670cd5e5d9e858fa39578107b80ebef4a955df93c3d8cabb320'),
    RowSpec('PC-04', 'A-input-type', 'C01', _CI, None, 'O09c', _L_IDENTITY_FAIL,
            'e17dd6a3a9327ef5be5c565eec6b0aa02e4d053563c0f59e3165382a6e991039',
            'a01df4a3950c81384ebcf92eedb53113d3781ec7b1e87218fac73620ff823a04'),
    RowSpec('PC-05', 'A-input-order', 'C01', _CI, None, 'O09c', _L_IDENTITY_FAIL,
            '3fc2b87a9fbcd049a7c5411cfe7557de5a56196523ec83c6cf73ca6befa67d35',
            '0673cd3c9b8d042fdf61dd8d6554ce44d2f6b67d3b6072903e41e1b111ffdf86'),
    RowSpec('PC-06', 'A-input-name', 'C01', _CC, None, 'O09c', _L_COMPARE_FAIL,
            '483cfe1a0c0346cfbdbbf3cd2f55affefa41fb407f7a0a7e1aab8baae0eea9f4',
            '6f9b096ac1f2104fcd0d3f179265f7539866c39a0562d97bf5e6f3087521d9f7'),
    RowSpec('PC-07', 'A-output-name', 'C02', _CC, None, 'O09c', _L_COMPARE_FAIL,
            '33f0483a7b42893104f6d9d7353a3dfa5ef9697a328292afea389cb23cee16a3',
            'f24fb067c6c33d4b12e5a0da07a536bf328b18573fedec7366e8b5ba083be075'),
    RowSpec('PC-08', 'A-output-order', 'C03', _CC, None, 'O09c', _L_COMPARE_FAIL,
            '58e7f0b3f67d87ab3ce3d62846e921831d5b57ee17f7287555ff0a9437cd97c3',
            'd21ec8dc759be80ef32bd8e7fabdea7c4fb7955e7d8ca93ae718564d8bfb0ca7'),
    # PC-09: A7's 'O11' is the recorded pin and DOES NOT ESTABLISH O11 (flag 1). O11
    # comes only from the Part E call-chain witness (PE-01).
    RowSpec('PC-09', 'A-wrong-grant-signature', 'C01', _TM, None, 'O11', _L_MEMBERSHIP_FAIL,
            'c2882e43efaaaed943b695cc797227b8d9692f92fcf7ec282942a0fa368ff465',
            'bb0418651d62ad389f8701fdc91ca6b43b379635ceef442672d567ed0a945867'),
)})

# The variants' recorded X3b encoding, verbatim. X3b re-encodes under THIS and only this.
X3B_ENCODING_RECORDED = (
    'UTF-8 JSON sort_keys=true indent=2 ensure_ascii=false plus LF; artifact fingerprint only'
)

# Controls: X3a + X3c ONLY (OD-SIG-20). X3b is NOT APPLICABLE -- not skipped. The
# cases.json control ``payload_sha256`` is a NON-BINDING measured fact (packet v7 section
# 4.1) and is deliberately never read here: "Do not silently convert it into a control
# preflight assertion."
CONTROL_X3C: Mapping[str, str] = types.MappingProxyType({
    'C01': 'b923dc2073b3e468229c92ea493d43735d15bd4f425d1f3041f316cdaef58872',
    'C02': 'c7ad9584050ccc72ddb2c760b08e5a7cdd174369166e7eef8b8633e6168512c8',
    'C03': 'd51385cbc3b6fc0036de1dccb0436a33fec85a272fd5e8be0540b5e667b17037',
})

ROW_FILE = 'sql-fixtures.json'
HASH_RECORD_FILE = 'fixture-hashes.json'
CASES_FILE = 'cases.json'

# PD-01 D3 and D7 -- exact sets, named, never counts.
D3_EDGE_SET: frozenset[str] = frozenset({_EC, _EI, _ET})
D7_SITE_SET: frozenset[str] = frozenset({_Q, _TA, _TS, _TE, _TM, _CN, _CI, _CC})

# Part D reach-marker matrix, packet v7 section 4.4. "entered" = per-run count >= 1.
# Each entry: (label, marker names; ANY entered satisfies the entry).
MarkerRequirement = tuple[str, tuple[str, ...]]
_M_O08: MarkerRequirement = ('O08 rows', ('_parameters',))
_M_O09C: MarkerRequirement = ('O09c rows', ('_options', '_validate_local_statements'))
_M_O12: MarkerRequirement = ('PC-09 (O12 reached)', ('_validate_statement_inventory',))


@dataclass(frozen=True)
class ControlSpec:
    """One Part D acceptance control, packet v7 section 4.3."""

    case_id: str
    control: str
    markers: tuple[MarkerRequirement, ...]
    evidence_events: int           # provisional 3.11.15 -- EVIDENCE, never asserted
    evidence_target: int           # provisional 3.11.15 -- EVIDENCE, never asserted


CONTROL_CASES: Mapping[str, ControlSpec] = types.MappingProxyType({c.case_id: c for c in (
    ControlSpec('PD-02', 'C01', (_M_O08, _M_O09C, _M_O12), 29, 4),
    ControlSpec('PD-03', 'C02', (_M_O09C,), 25, 4),
    ControlSpec('PD-04', 'C03', (_M_O09C,), 29, 4),
)})
D5_TARGET_EVIDENCE = 4     # PD-01 D5: recorded as EVIDENCE; a deviation is a finding

# ---------------------------------------------- order metadata (Stage 3 enforces)

# Control before row (packet v7 section 2.5; Part E v2 section 3). The listed cases must
# have independently PASSED before the key case may be credited as control-backed.
BACKING_CASES: Mapping[str, tuple[str, ...]] = types.MappingProxyType({
    **{cid: ('PD-01', 'PD-02') for cid in
       ('PC-01', 'PC-02', 'PC-03', 'PC-04', 'PC-05', 'PC-06', 'PC-09')},
    'PC-07': ('PD-03',),
    'PC-08': ('PD-04',),
    'PE-02': ('PD-01', 'PD-02'),
    'PE-01': ('PD-01', 'PD-02', 'PE-02'),
})

# G-2 parse-prerequisite credit must precede each of these (work plan v3 section 4).
G2_GATED: frozenset[str] = frozenset({*ROWS, 'PE-01'})

# ------------------------------------------------------------------ binding


@dataclass(frozen=True)
class BoundInput:
    """A subject whose X3 checks have ALL passed. ``validated_bytes`` is the exact
    object that was hashed for X3c; it is the object passed to the subject."""

    subject: str                   # row name or control id
    validated_bytes: bytes
    x3a: str
    x3b: str | None                # None ONLY for controls: NOT APPLICABLE (OD-SIG-20)
    x3c: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(rel: str) -> bytes:
    return (ov.FIXTURE_DIR / rel).read_bytes()


def _hash_record() -> Mapping[str, str]:
    """``fixture-hashes.json``, itself authenticated against the bound constant first."""
    raw = _read(HASH_RECORD_FILE)
    observed = _sha(raw)
    expected = ov.EXPECTED_INPUT_SHA256[HASH_RECORD_FILE]
    if observed != expected:
        raise CaseAbort(CaseDiag.X3A_RECORD_MISMATCH, expected=expected, observed=observed)
    record: Mapping[str, str] = json.loads(raw.decode('utf-8'))
    return record


def _x3a(rel: str) -> tuple[bytes, str]:
    """X3a: the fixture FILE bytes against the digest recorded in fixture-hashes.json."""
    raw = _read(rel)
    observed = _sha(raw)
    recorded = _hash_record().get(rel)
    if recorded is None or observed != recorded:
        raise CaseAbort(CaseDiag.X3A_MISMATCH, file=rel, recorded=recorded, observed=observed)
    return raw, observed


def x3b_encoding(payload: Any) -> bytes:
    """The variants' recorded X3b encoding: sort_keys, indent=2, ensure_ascii=false, + LF.
    An ARTIFACT FINGERPRINT only -- never the bytes that are validated."""
    return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + '\n') \
        .encode('utf-8')


def bind_row(case_id: str) -> BoundInput:
    """X3a + X3b + X3c for one Part C row (packet v7 section 3.2). Evaluated afresh for
    every case that calls it -- never cached across cases."""
    spec = ROWS[case_id]
    raw, x3a = _x3a(ROW_FILE)
    variants = [v for v in json.loads(raw.decode('utf-8'))['variants']
                if v['case_id'] == spec.row]
    if len(variants) != 1:
        raise CaseAbort(CaseDiag.SUBJECT_NOT_UNIQUE, subject=spec.row, found=len(variants))
    variant = variants[0]
    if variant['control'] != spec.control:
        raise CaseAbort(CaseDiag.ROW_CONTROL_UNEXPECTED, subject=spec.row,
                        expected=spec.control, observed=variant['control'])
    payload = variant['payload']
    # X3b -- the recorded fingerprint, under the recorded encoding.
    if variant['payload_hash_encoding'] != X3B_ENCODING_RECORDED:
        raise CaseAbort(CaseDiag.X3B_ENCODING_UNEXPECTED, subject=spec.row,
                        observed=variant['payload_hash_encoding'])
    if variant['payload_sha256'] != spec.x3b:
        raise CaseAbort(CaseDiag.X3B_RECORD_UNEXPECTED, subject=spec.row,
                        pinned=spec.x3b, recorded=variant['payload_sha256'])
    x3b = _sha(x3b_encoding(payload))
    if x3b != variant['payload_sha256']:
        raise CaseAbort(CaseDiag.X3B_MISMATCH, subject=spec.row,
                        recorded=variant['payload_sha256'], observed=x3b)
    # X3c -- the exact bytes the subject consumes.
    validated = ov.canonical_validated_bytes(payload)
    x3c = _sha(validated)
    if x3c != spec.x3c:
        raise CaseAbort(CaseDiag.X3C_MISMATCH, subject=spec.row, pinned=spec.x3c, observed=x3c)
    return BoundInput(spec.row, validated, x3a, x3b, x3c)


def bind_control(control_id: str) -> BoundInput:
    """X3a + X3c ONLY (OD-SIG-20), by the ruled procedure of packet v7 section 2.2a:
    load from the RECORDED ``.payload_file``, keep key order, serialize unsorted, hash
    THOSE bytes and validate THOSE SAME bytes."""
    cases = json.loads(_read(CASES_FILE).decode('utf-8'))
    records = [c for c in cases['controls'] if c['id'] == control_id]
    if len(records) != 1:
        raise CaseAbort(CaseDiag.SUBJECT_NOT_UNIQUE, subject=control_id, found=len(records))
    record = records[0]
    expected_file = f'controls/{control_id}.json'
    if record['payload_file'] != expected_file:
        raise CaseAbort(CaseDiag.CONTROL_SOURCE_UNEXPECTED, subject=control_id,
                        expected=expected_file, observed=record['payload_file'])
    raw, x3a = _x3a(expected_file)
    payload = json.loads(raw.decode('utf-8'))                    # insertion order kept
    validated = ov.canonical_validated_bytes(payload)
    x3c = _sha(validated)
    if x3c != CONTROL_X3C[control_id]:
        raise CaseAbort(CaseDiag.X3C_MISMATCH, subject=control_id,
                        pinned=CONTROL_X3C[control_id], observed=x3c)
    # Corroboration ONLY: parsed-object equality with the inline payload. Order-insensitive,
    # so it can never stand in for X3c; the inline payload's own digest is never used.
    if record['payload'] != payload:
        raise CaseAbort(CaseDiag.CONTROL_CORROBORATION_FAILED, subject=control_id)
    return BoundInput(control_id, validated, x3a, None, x3c)


# ------------------------------------------------------------------ execution


def run(resolution: ov.Resolution, bound: BoundInput, schema: str, *,
        markers: tuple[str, ...] = (), witness: bool = False,
        public_entry: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
        ) -> ov.RunRecord:
    """The ONE call site through which every Part C/D/E case runs its subject. It adds
    nothing: it passes ``bound.validated_bytes`` -- the very object hashed for X3c --
    to ``ov.run_once`` unchanged. It exists so Stage 3 composes mutant layers at one
    reviewed point instead of editing case bodies."""
    return ov.run_once(resolution, bound.validated_bytes, schema, markers=markers,
                       witness=witness, public_entry=public_entry)


# --------------------------------------------------------------- run checks


def require_full_preflight(resolution: ov.Resolution) -> None:
    """Precondition: S1..S14 passed as the EXACT completed prefix."""
    if resolution.ledger != ov.STAGES:
        raise CaseAbort(CaseDiag.PREFLIGHT_PREFIX, ledger=list(resolution.ledger))


def require_observed(record: ov.RunRecord) -> None:
    """A fired ObservationAbort is a wrong-reason death, never an assertion result."""
    if record.abort is not None:
        raise CaseAbort(CaseDiag.OBSERVATION_ABORTED,
                        observation_diagnosis=record.abort.diagnosis)
    if record.accepted is (record.public is not None):
        raise CaseAbort(CaseDiag.UNEXPECTED_OUTCOME, accepted=record.accepted,
                        public=type(record.public).__name__)


def unknown_events(record: ov.RunRecord) -> list[tuple[int, str, str | None]]:
    """X1 over raw events: a site outside the eight IDs, or an edge that is neither
    ``None`` nor one of the three edge IDs. ``None`` IS a resolved value (M3a writes it)."""
    return [(i, e.site, e.edge) for i, e in enumerate(record.events)
            if e.site == ov.UNKNOWN or e.site not in ov.SITE_IDS
            or not (e.edge is None or e.edge in ov.EDGE_IDS)]


def require_no_unknown(record: ov.RunRecord) -> None:
    """X1 as a contract check (packet v7 section 2.1)."""
    bad = unknown_events(record)
    if bad:
        raise CaseAbort(CaseDiag.UNKNOWN_PRESENT, events=bad)


def unrestored(resolution: ov.Resolution, exclude: tuple[str, ...] = ()) -> list[str]:
    """X2 for the run's own instruments: every instrumented module global holds its
    ORIGINAL object again. Harness-level mutant seams are NOT checked here -- the
    autouse conftest check owns those around every case."""
    module = resolution.module
    return [n for n in ov.INSTRUMENTED_GLOBALS
            if n not in exclude and module.__dict__[n] is not resolution.originals[n]]


def require_restored(resolution: ov.Resolution, exclude: tuple[str, ...] = ()) -> None:
    """X2 as a contract check. Part E excludes the I1 and I4 slots, whose restoration is
    its own frozen assertion (EA7 / EC7) and must not be pre-empted by this check."""
    names = unrestored(resolution, exclude)
    if names:
        raise CaseAbort(CaseDiag.NOT_RESTORED, globals=names)


def failing(record: ov.RunRecord) -> list[ov.Event]:
    return [e for e in record.events if e.outcome == 'fail']


def ledger_of(record: ov.RunRecord) -> Ledger:
    return tuple((e.site, e.edge, e.outcome) for e in record.events)


def successful_qualified_edges(record: ov.RunRecord) -> list[str | None]:
    return [e.edge for e in record.events if e.site == _Q and e.outcome == 'pass']


# ------------------------------------------------------------ Part E binding probe


class BindingProbe:
    """A pass-through ``public_entry`` layer that reads the snapshot module's slots at
    the moment the public call is made -- i.e. AFTER every install, BEFORE the call.
    It is how "I4 installed with its install-time binding verified" and "I1 installed"
    are checked (Part E v2 section 3) without adding a seam. It adds no ``except``; the
    exception, or the result, passes through unchanged.

    Stage 3 note: when a public-entry mutant layer is composed (M5, M-O11-L2), this probe
    must remain the OUTERMOST layer so it observes the install-time binding.
    """

    def __init__(self, resolution: ov.Resolution) -> None:
        self.module = resolution.module
        self.seen: dict[str, Any] = {}
        self.calls = 0

    def __call__(self, entry: Callable[..., Any]) -> Callable[..., Any]:
        def probed(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            self.seen['_validate_local_statements'] = \
                self.module.__dict__['_validate_local_statements']
            self.seen['_validate_statement_inventory'] = \
                self.module.__dict__['_validate_statement_inventory']
            return entry(*args, **kwargs)
        probed.__qualname__ = 'binding_probe[validate_function_installation]'
        return probed


I4_NAME = '_validate_statement_inventory'
I1_NAME = '_validate_local_statements'
I1_QUALNAME = 'o11_witness[_validate_local_statements]'
PART_E_OWN_SLOTS: tuple[str, ...] = (I1_NAME, I4_NAME)   # asserted by EA7 / EC7


def require_part_e_bound(resolution: ov.Resolution, record: ov.RunRecord,
                         probe: BindingProbe) -> ov.WitnessRecord:
    """Part E preconditions, read from the run: I1 installed; I4 bound at install time."""
    witnessed = probe.seen.get(I1_NAME)
    if (probe.calls != 1 or record.witness is None or witnessed is None
            or witnessed is resolution.originals[I1_NAME]
            or getattr(witnessed, '__qualname__', None) != I1_QUALNAME):
        raise CaseAbort(CaseDiag.I1_NOT_INSTALLED, probe_calls=probe.calls,
                        witness_record=record.witness is not None)
    marker = record.installed_markers.get(I4_NAME)
    if marker is None or probe.seen.get(I4_NAME) is not marker:
        raise CaseAbort(CaseDiag.I4_BINDING_UNVERIFIED)
    return record.witness


def require_i2_identity() -> None:
    """Precondition: I2's slot holds identity before the run (Part E v2 section 3)."""
    if ov.WITNESS_SLOT.current is not ov.WITNESS_SLOT.identity:
        raise CaseAbort(CaseDiag.I2_NOT_IDENTITY)
