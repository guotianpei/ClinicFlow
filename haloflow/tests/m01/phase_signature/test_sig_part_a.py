"""PHASE-SIGNATURE-01 Gate 3 PART A -- the binding resolver. A-01 .. A-29.

Frozen design: Part A/B cases v3 (16d5530d...), Blocker-2 seams v3 (fe0070d2...),
owner freeze OD-SIG-24. Part A operates on the captured byte snapshot; nothing in
Part A executes a row, except A-29, which runs one row only to show the raw event
contract carries no phase field.

ORACLE DISCIPLINE. No case asserts a raw line number as its oracle. A-01..A-08
identify a resolved site by the EXACT ast.dump of its guarded condition -- an oracle
independent of the resolver's structural predicates, so a predicate swapped between
two IDs that share an enclosing function is caught. Every aborting case asserts the
named diagnosis by EQUALITY, the ledger as the EXACT COMPLETED PREFIX, and the exact
expected SET where a set is involved -- never a count, never containment.
"""

import ast
import dataclasses
import hashlib
import sys
import types
from collections.abc import Callable, Mapping
from typing import Any

import pytest
import sig_overlay as ov

from haloflow.m01.errors import MigrationUnitRejected

S = ov.STAGES


def prefix(n: int) -> tuple[str, ...]:
    """The exact completed prefix S1..Sn."""
    return S[:n]


def dump(src: str) -> str:
    return ast.dump(ast.parse(src, mode='eval').body, include_attributes=False)


# The independent oracle for A-01..A-08: the exact guarded condition per semantic ID.
EXPECTED_SITE = {
    ov.SIG_QUALIFIED: ('_qualified', "len(names) == 2 and names[0] == schema_key"),
    ov.SIG_TARGET_ARGS_UNSPEC: ('_target', 'obj.args_unspecified is False'),
    ov.SIG_TARGET_DUAL_SHAPE: (
        '_target', 'p.name is None and p.defexpr is None '
                   'and p.mode in (modes.FUNC_PARAM_DEFAULT, modes.FUNC_PARAM_IN)'),
    ov.SIG_TARGET_DUAL_EQUAL: ('_target', 'tuple(dual) == args'),
    ov.SIG_TARGET_MEMBERSHIP: ('_target', '(name, args) in d.functions'),
    ov.SIG_CREATE_NAME: ('_validate_ast', 'any(key[0] == name for key in d.functions)'),
    ov.SIG_CREATE_IDENTITY: ('_validate_ast', 'identity in d.functions'),
    ov.SIG_CREATE_COMPARE: (
        '_validate_ast',
        "inputs == f['inputs'] and outputs == f['outputs'] and result == f['return_type']"),
}

EXPECTED_EDGE_ENCLOSING = {
    ov.EDGE_CREATE: '_validate_ast',
    ov.EDGE_TARGET: '_target',
    ov.EDGE_INVENTORY: '_validate_statement_inventory',
}


def expect_abort(**kwargs: Any) -> ov.PreflightAbort:
    """Run a fresh preflight that MUST abort. A returned resolution is a failure (A-24)."""
    try:
        resolved = ov.preflight(**kwargs)
    except ov.PreflightAbort as abort:
        assert abort.diagnosis != ov.UNKNOWN
        return abort
    raise AssertionError(f'preflight did not abort; ledger={resolved.ledger}')


def replaced_predicate(sid: str, fn: Callable[[ov.LocatedCall], bool]) -> Any:
    table = dict(ov.DEFAULT_SITE_PREDICATES)
    table[sid] = fn
    return types.MappingProxyType(table)


def replaced_edge_predicate(eid: str, fn: Callable[[ov.LocatedCall], bool]) -> Any:
    table = dict(ov.DEFAULT_EDGE_PREDICATES)
    table[eid] = fn
    return types.MappingProxyType(table)


def assert_restored(name: str) -> None:
    assert ov.STATIC_SEAMS.get(name) is ov.STATIC_SEAMS.original(name)


# ============================================================ A.1 positive resolution


def _cases(pairs: list[tuple[str, str]]) -> list[Any]:
    return [pytest.param(value, id=case_id, marks=pytest.mark.sig_case(case_id))
            for case_id, value in pairs]


@pytest.mark.parametrize('sid', _cases([
    ('A-01', ov.SIG_QUALIFIED),
    ('A-02', ov.SIG_TARGET_ARGS_UNSPEC),
    ('A-03', ov.SIG_TARGET_DUAL_SHAPE),
    ('A-04', ov.SIG_TARGET_DUAL_EQUAL),
    ('A-05', ov.SIG_TARGET_MEMBERSHIP),
    ('A-06', ov.SIG_CREATE_NAME),
    ('A-07', ov.SIG_CREATE_IDENTITY),
    ('A-08', ov.SIG_CREATE_COMPARE),
]))
def test_a01_a08_semantic_id_resolves_to_the_exact_site(sid, resolution):
    site = resolution.sites[sid]
    enclosing, condition = EXPECTED_SITE[sid]
    # exactly one site: the resolution maps each ID to ONE LocatedCall after the
    # complete S1..S14 prefix passed
    assert resolution.ledger == ov.STAGES
    assert site.enclosing == enclosing
    # the exact site, by structure -- NOT cardinality plus enclosure (finding 2)
    assert site.first_arg_dump == dump(condition)
    # no other ID resolved to the same site
    assert [k for k, v in resolution.sites.items() if v.key == site.key] == [sid]


@pytest.mark.parametrize('eid', _cases([
    ('A-09', ov.EDGE_CREATE),
    ('A-10', ov.EDGE_TARGET),
    ('A-11', ov.EDGE_INVENTORY),
]))
def test_a09_a11_edge_resolves_exactly_once_by_enclosing_function(eid, resolution):
    edge = resolution.edges[eid]
    assert edge.enclosing == EXPECTED_EDGE_ENCLOSING[eid]
    assert [k for k, v in resolution.edges.items() if v.key == edge.key] == [eid]


@pytest.mark.sig_case('A-12')
def test_a12_argument_expression_does_not_discriminate(resolution):
    create = resolution.edges[ov.EDGE_CREATE]
    inventory = resolution.edges[ov.EDGE_INVENTORY]
    target = resolution.edges[ov.EDGE_TARGET]
    assert create.first_arg_dump == inventory.first_arg_dump == dump('node.funcname')
    assert target.first_arg_dump == dump('obj.objname')
    # only the enclosing function separates CREATE from INVENTORY
    assert create.enclosing != inventory.enclosing


# ================================================================== A.2 closure


@pytest.mark.sig_case('A-13')
def test_a13_exactly_eight_signature_requires(resolution):
    located = ov.default_require_site_locator(resolution.parsed)
    assert len(located) == 8
    assert {s.key for s in located} == {s.key for s in resolution.sites.values()}


@pytest.mark.sig_case('A-14')
def test_a14_zero_direct_fail_carries_signature_mismatch(resolution):
    assert ov.default_direct_fail_locator(resolution.parsed) == ()


@pytest.mark.sig_case('A-15')
def test_a15_three_qualified_sites_map_onto_three_edges_each_once(resolution):
    qualified = ov.default_qualified_call_locator(resolution.parsed)
    assert len(qualified) == 3
    assert {q.key for q in qualified} == {e.key for e in resolution.edges.values()}
    assert set(resolution.edges) == set(ov.EDGE_IDS)


# ========================================================= A.3 fail-closed aborts


@pytest.mark.sig_case('A-16')
def test_a16_semantic_id_resolving_to_zero_sites(resolution):
    table = replaced_predicate(ov.SIG_TARGET_DUAL_EQUAL, lambda site: False)
    with ov.STATIC_SEAMS.replaced('site_predicates', table):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.SITE_UNRESOLVED
    assert abort.ledger == prefix(8)
    assert abort.details['semantic_id'] == ov.SIG_TARGET_DUAL_EQUAL
    assert abort.details['sites_found'] == 0
    assert_restored('site_predicates')


@pytest.mark.sig_case('A-17')
def test_a17_semantic_id_resolving_to_more_than_one_site(resolution):
    own = resolution.sites[ov.SIG_TARGET_DUAL_EQUAL]
    other = resolution.sites[ov.SIG_TARGET_MEMBERSHIP]
    table = replaced_predicate(ov.SIG_TARGET_DUAL_EQUAL,
                               lambda site: site.key in {own.key, other.key})
    with ov.STATIC_SEAMS.replaced('site_predicates', table):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.SITE_AMBIGUOUS
    assert abort.ledger == prefix(8)
    assert abort.details['semantic_id'] == ov.SIG_TARGET_DUAL_EQUAL
    assert abort.details['sites_found'] == 2
    assert abort.details['sites'] == frozenset({own.key, other.key})   # exact pair
    assert_restored('site_predicates')


@pytest.mark.sig_case('A-18')
def test_a18_located_site_claimed_by_no_id(resolution):
    identity_site = resolution.sites[ov.SIG_CREATE_IDENTITY]
    compare_site = resolution.sites[ov.SIG_CREATE_COMPARE]
    table = replaced_predicate(ov.SIG_CREATE_IDENTITY,
                               ov.DEFAULT_SITE_PREDICATES[ov.SIG_CREATE_COMPARE])
    with ov.STATIC_SEAMS.replaced('site_predicates', table):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.SITE_UNCLAIMED
    assert abort.ledger == prefix(9)           # the ONLY A.3 case that passes S9
    assert abort.details['unclaimed'] == frozenset({identity_site.key})
    assert abort.details['doubly_claimed'] == frozenset({compare_site.key})
    assert abort.details['claimants'] == frozenset({ov.SIG_CREATE_IDENTITY,
                                                    ov.SIG_CREATE_COMPARE})
    assert_restored('site_predicates')


@pytest.mark.sig_case('A-19')
def test_a19_located_count_is_not_eight(resolution):
    removed = resolution.sites[ov.SIG_TARGET_MEMBERSHIP]
    baseline = ov.default_require_site_locator

    def seven(parsed: ov.ParsedSnapshot) -> tuple[ov.LocatedCall, ...]:
        return tuple(s for s in baseline(parsed) if s.key != removed.key)   # removal only

    with ov.STATIC_SEAMS.replaced('require_site_locator', seven):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.SITE_COUNT_INVALID
    assert abort.ledger == prefix(4)           # the shortest in Part A.3 after A-23
    assert abort.details['located'] == 7
    assert abort.details['expected'] == 8
    expected_set = {s.key for s in resolution.sites.values()} - {removed.key}
    assert abort.details['located_sites'] == frozenset(expected_set)   # exact set
    assert_restored('require_site_locator')


@pytest.mark.sig_case('A-20')
def test_a20_direct_fail_carrying_signature_mismatch(resolution):
    # A real, named location in the snapshot: the _fail call inside _require.
    fail_in_require = [
        ov._located(c, dict(resolution.parsed.enclosing))
        for c in ov._named_calls(resolution.parsed.tree, '_fail')
        if resolution.parsed.enclosing.get(id(c)) == '_require'
    ]
    assert len(fail_in_require) == 1
    finding = fail_in_require[0]
    with ov.STATIC_SEAMS.replaced('direct_fail_locator', lambda parsed: (finding,)):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.DIRECT_FAIL_PRESENT
    assert abort.ledger == prefix(6)           # BEGINS with S1 and S2: not a source mismatch
    assert abort.ledger[:2] == ('S1', 'S2')
    assert abort.details['locations'] == (finding.key,)
    assert_restored('direct_fail_locator')


@pytest.mark.sig_case('A-21')
def test_a21a_edge_resolving_to_zero(resolution):
    table = replaced_edge_predicate(ov.EDGE_TARGET, lambda site: False)
    with ov.STATIC_SEAMS.replaced('edge_predicates', table):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.EDGE_UNRESOLVED
    assert abort.ledger == prefix(12)          # S12 verified the three-site universe first
    assert abort.details['edge_id'] == ov.EDGE_TARGET
    assert abort.details['sites_found'] == 0
    assert_restored('edge_predicates')
    assert_restored('qualified_call_locator')


@pytest.mark.sig_case('A-21')
def test_a21b_edge_mapping_more_than_once(resolution):
    create = resolution.edges[ov.EDGE_CREATE]
    inventory = resolution.edges[ov.EDGE_INVENTORY]
    table = replaced_edge_predicate(ov.EDGE_CREATE,
                                    lambda site: site.key in {create.key, inventory.key})
    with ov.STATIC_SEAMS.replaced('edge_predicates', table):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.EDGE_AMBIGUOUS
    assert abort.ledger == prefix(12)
    assert abort.details['edge_id'] == ov.EDGE_CREATE
    assert abort.details['sites_found'] == 2
    assert abort.details['sites'] == frozenset({create.key, inventory.key})   # exact pair
    assert_restored('edge_predicates')


@pytest.mark.sig_case('A-22')
@pytest.mark.parametrize('form', ['positional', 'keyword'])
def test_a22_code_argument_cannot_be_classified(form, resolution):
    named = resolution.sites[ov.SIG_TARGET_MEMBERSHIP]
    baseline = ov.default_code_classifier

    def classifier(parsed: ov.ParsedSnapshot, site: ov.LocatedCall) -> Any:
        if site.key == named.key:
            return ov.Unclassifiable(form)
        return baseline(parsed, site)

    with ov.STATIC_SEAMS.replaced('code_classifier', classifier):
        abort = expect_abort()
    assert abort.diagnosis == ov.Diag.CODE_UNCLASSIFIABLE
    assert abort.ledger == prefix(5)           # S4 located the site: not an unknown site
    assert abort.details['site'] == named.key
    assert abort.details['argument_form'] == form
    assert_restored('code_classifier')


def _protected_hashes() -> dict[str, str]:
    return {rel: hashlib.sha256((ov.HALOFLOW_ROOT / rel).read_bytes()).hexdigest()
            for rel in ov.PROTECTED_SHA256}


@pytest.mark.sig_case('A-23')
def test_a23a_snapshot_byte_mismatch():
    wrong = ov.Expected(snapshot_sha256='0' * 64)
    abort = expect_abort(expected=wrong)
    assert abort.diagnosis == ov.Diag.SNAPSHOT_MISMATCH
    assert abort.ledger == ()                  # nothing passed
    assert abort.details['expected'] == '0' * 64
    assert _protected_hashes() == dict(ov.PROTECTED_SHA256)   # achieved without damage


@pytest.mark.sig_case('A-23')
def test_a23b_input_hash_mismatch():
    table = dict(ov.EXPECTED_INPUT_SHA256)
    table['controls/C03.json'] = 'f' * 64
    abort = expect_abort(expected=ov.Expected(input_sha256=types.MappingProxyType(table)))
    assert abort.diagnosis == ov.Diag.INPUT_HASH_MISMATCH
    assert abort.ledger == ('S1',)
    assert abort.details['input'] == 'controls/C03.json'
    assert _protected_hashes() == dict(ov.PROTECTED_SHA256)


def contains_unknown(value: Any) -> bool:
    """STRUCTURAL search for UNKNOWN at any depth: strings, mappings (keys and values),
    sequences, sets, and dataclass fields that take part in equality. Never str()."""
    if isinstance(value, str):
        return value == ov.UNKNOWN
    if isinstance(value, Mapping):
        return any(contains_unknown(k) or contains_unknown(v) for k, v in value.items())
    if isinstance(value, tuple | list | set | frozenset):
        return any(contains_unknown(v) for v in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return any(contains_unknown(getattr(value, f.name))
                   for f in dataclasses.fields(value) if f.compare)
    return False


def with_unknown_enclosing(sites: tuple[ov.LocatedCall, ...],
                           target: ov.LocatedCall) -> tuple[ov.LocatedCall, ...]:
    return tuple(dataclasses.replace(s, enclosing=ov.UNKNOWN) if s.key == target.key else s
                 for s in sites)


@pytest.mark.sig_case('A-24')
def test_a24_unknown_is_never_an_accepted_resolution(resolution):
    """UNKNOWN is induced at each named resolver boundary. Every stimulus FAILS CLOSED:
    no Resolution is returned, the diagnosis and exact completed prefix are the named
    ones, and UNKNOWN appears nowhere in the abort, at any depth."""
    # the structural search can fail -- it is not vacuous
    assert contains_unknown(('x', frozenset({ov.UNKNOWN})))
    assert contains_unknown({'k': ({'n': [ov.UNKNOWN]},)})
    assert not contains_unknown(('x', frozenset({'y'})))

    membership = resolution.sites[ov.SIG_TARGET_MEMBERSHIP]
    target_edge = resolution.edges[ov.EDGE_TARGET]
    base_locator = ov.default_require_site_locator
    base_qualified = ov.default_qualified_call_locator

    def unknown_predicate(site: ov.LocatedCall) -> Any:
        return ov.UNKNOWN                       # truthy, but not True

    # (seam, stimulus, diagnosis, exact prefix, detail key, detail value)
    stimuli: list[tuple[str, Any, str, tuple[str, ...], str, Any]] = [
        ('site_predicates', replaced_predicate(ov.SIG_TARGET_DUAL_EQUAL, unknown_predicate),
         ov.Diag.SITE_UNRESOLVED, prefix(8), 'semantic_id', ov.SIG_TARGET_DUAL_EQUAL),
        ('require_site_locator',
         lambda p: with_unknown_enclosing(base_locator(p), membership),
         ov.Diag.SITE_UNRESOLVED, prefix(8), 'semantic_id', ov.SIG_TARGET_MEMBERSHIP),
        ('code_classifier', lambda p, s: ov.UNKNOWN,
         ov.Diag.CODE_UNCLASSIFIABLE, prefix(5), 'argument_form', 'positional'),
        ('qualified_call_locator',
         lambda p: with_unknown_enclosing(base_qualified(p), target_edge),
         ov.Diag.EDGE_UNRESOLVED, prefix(12), 'edge_id', ov.EDGE_TARGET),
        ('edge_predicates', replaced_edge_predicate(ov.EDGE_INVENTORY, unknown_predicate),
         ov.Diag.EDGE_UNRESOLVED, prefix(12), 'edge_id', ov.EDGE_INVENTORY),
    ]
    for seam, stimulus, diagnosis, ledger, key, value in stimuli:
        with ov.STATIC_SEAMS.replaced(seam, stimulus):
            abort = expect_abort()
        assert abort.diagnosis == diagnosis, seam
        assert abort.ledger == ledger, seam
        assert abort.details[key] == value, seam
        assert not contains_unknown(abort.details), seam
        assert_restored(seam)

    # the post-resolution boundary (after S13, before S14), in process. NOT M4: a
    # different binding, no environment variable, and A-24 is never in the B-24 child.
    abort = expect_abort(post_resolution=lambda sites: {**sites,
                                                        ov.SIG_TARGET_DUAL_EQUAL: ov.UNKNOWN})
    assert abort.diagnosis == ov.Diag.BINDING_UNRESOLVED
    assert abort.ledger == prefix(13)
    assert abort.details == {'unresolved_binding': ov.SIG_TARGET_DUAL_EQUAL}

    # the baseline resolution carries no UNKNOWN in any usable value
    assert resolution.ledger == ov.STAGES
    for part in (resolution.sites, resolution.edges, resolution.line_to_site,
                 resolution.ledger):
        assert not contains_unknown(part)


# ======================================================== A.4 provenance, binding


@pytest.mark.sig_case('A-25')
def test_a25_form_1_provenance(resolution):
    module = resolution.module
    assert hashlib.sha256(resolution.raw).hexdigest() == ov.EXPECTED_SNAPSHOT_SHA256
    from haloflow.m01.provisioning import function_policy as imported
    assert module is not imported                  # the snapshot is its own module object
    assert sys.modules[ov.SNAPSHOT_MODULE_NAME].__name__ == ov.SNAPSHOT_MODULE_NAME
    for name in ('_require', '_fail', '_qualified', '_target', '_validate_ast',
                 '_validate_local_statements', '_validate_statement_inventory',
                 '_parameters', '_options', 'validate_function_installation'):
        assert getattr(module, name).__code__.co_filename == str(ov.POLICY_PATH)
    # one exception class, so identity assertions compare within one class
    assert module.MigrationUnitRejected is MigrationUnitRejected
    # path equality is recorded as a DIAGNOSTIC, never as the binding
    print(f'[A-25 diagnostic] snapshot __file__ = {module.__file__}')


@pytest.mark.sig_case('A-26')
def test_a26_ten_input_hashes_equal_their_bound_constants():
    """DESIGNATED B-24 BODY. File reads and hash comparisons ONLY: no seam, no
    subprocess, no shared state, no shared hash cache, no fixture."""
    names = {'cases.json', 'sql-fixtures.json', 'expected-refusals.json',
             'fixture-hashes.json'} | {f'controls/C0{n}.json' for n in range(1, 7)}
    assert set(ov.EXPECTED_INPUT_SHA256) == names
    assert len(ov.EXPECTED_INPUT_SHA256) == 10
    for rel, want in ov.EXPECTED_INPUT_SHA256.items():
        got = hashlib.sha256((ov.FIXTURE_DIR / rel).read_bytes()).hexdigest()
        assert got == want, rel


@pytest.mark.sig_case('A-27')
def test_a27_protected_inputs_unchanged():
    """Hashes are the authority; git status is only a secondary diagnostic."""
    assert _protected_hashes() == dict(ov.PROTECTED_SHA256)


# ======================================================== A.5 phase projection


@pytest.mark.sig_case('A-28')
def test_a28_phase_projection_exists_is_pure_and_separately_replaceable():
    assert isinstance(ov.PHASE_BY_SITE, types.MappingProxyType)          # immutable
    assert set(ov.PHASE_BY_SITE) == set(ov.SITE_IDS)
    before = dict(ov.PHASE_BY_SITE)
    first = [ov.phase_for_site(s) for s in ov.SITE_IDS]
    second = [ov.phase_for_site(s) for s in ov.SITE_IDS]
    assert first == second                                                # pure
    assert dict(ov.PHASE_BY_SITE) == before                               # unmutated
    with pytest.raises(TypeError):
        ov.PHASE_BY_SITE[ov.SIG_QUALIFIED] = 'O09c'                       # type: ignore[index]

    def swapped(site_id: str) -> str:
        return {'O08': 'O09c', 'O09c': 'O08'}.get(ov.PHASE_BY_SITE[site_id],
                                                  ov.PHASE_BY_SITE[site_id])

    with ov.PHASE_SLOT.replaced(swapped):
        assert ov.phase_for_site(ov.SIG_CREATE_NAME) == 'O09c'
        with pytest.raises(RuntimeError), ov.PHASE_SLOT.replaced(swapped):
            pass                                                          # no stacking
    assert ov.PHASE_SLOT.current is ov.PHASE_SLOT.baseline
    assert ov.phase_for_site(ov.SIG_CREATE_NAME) == 'O08'


@pytest.mark.sig_case('A-29')
def test_a29_event_contract_carries_no_phase_field(resolution, schema):
    assert tuple(f.name for f in dataclasses.fields(ov.Event)) == (
        'site', 'edge', 'outcome', 'exception')

    def poisoned(site_id: str) -> str:
        raise AssertionError('the observer consulted the phase projection')

    payload = ov.canonical_validated_bytes(ov.row_payload('A-wrong-create-name'))
    with ov.PHASE_SLOT.replaced(poisoned):
        record = ov.run_once(resolution, payload, schema)
    assert record.abort is None
    assert record.events, 'the row produced no events'
    for event in record.events:
        assert not hasattr(event, 'phase')
        assert event.site in ov.SITE_IDS
