"""Unrun review draft; requires separately approved implementation and parser evidence."""
import copy
import json
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import function_policy as subject
from haloflow.m01.provisioning.function_checksum import function_checksum
from haloflow.m01.provisioning.verification import FunctionExpectation

ROOT = Path(__file__).parent / 'fixtures/function_policy'
CASES = json.loads((ROOT / 'cases.json').read_text())
VARIANTS = json.loads((ROOT / 'sql-fixtures.json').read_text())['variants']
REASONS = json.loads((ROOT / 'expected-refusals.json').read_text())
SCHEMA = CASES['schema_key']

def encode(payload):
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')

def control(identifier='C01'):
    return json.loads((ROOT / 'controls' / (identifier + '.json')).read_text())

def accepted(payload):
    result = subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
    assert result.sql_bytes == payload['template'].replace('{schema}', SCHEMA).encode('utf-8')
    assert result.schema_key == SCHEMA
    assert result.execution_role == payload['execution_role']
    assert result.migration_id == payload['migration_id']
    assert result.checksum == function_checksum(**{k: payload[k] for k in
        ('migration_id', 'template', 'execution_role', 'verification', 'policy')})
    return result

@pytest.mark.parametrize('identifier', ['C01', 'C02', 'C03', 'C04', 'C05', 'C06'])
def test_control(identifier):
    result = accepted(control(identifier))
    with pytest.raises(FrozenInstanceError):
        result.sql_bytes = b'changed'

@pytest.mark.parametrize('variant', VARIANTS, ids=lambda v: v['case_id'])
def test_sql_case(variant, monkeypatch):
    accepted(control(variant['control']))
    oracle = REASONS[variant['case_id']]
    reason = oracle['code']
    observed = []
    # Observe real private validators, preserving the exact exception object.
    for seam, phase in [('_validate_local_statements', 'O11'), ('_validate_statement_inventory', 'O12')]:
        real = getattr(subject, seam)
        def spy(*args, _real=real, _phase=phase, **kwargs):
            try:
                return _real(*args, **kwargs)
            except MigrationUnitRejected as error:
                observed.append((_phase, error))
                raise
        monkeypatch.setattr(subject, seam, spy)
    if reason is None:
        accepted(variant['payload'])
    else:
        with pytest.raises(MigrationUnitRejected) as caught:
            subject.validate_function_installation(encode(variant['payload']), schema_key=SCHEMA)
        assert caught.value.reason_code == reason
        if oracle['phase'] is not None:
            assert observed and observed[0][0] == oracle['phase']
            assert observed[0][1] is caught.value

OWNERS = [c for c in CASES['cases'] if c['id'].startswith('I-owner-') and c['id'] != 'I-owner-not-string']
@pytest.mark.parametrize('case', OWNERS, ids=lambda c: c['id'])
def test_owner_reaches_existing_typed_constructor(case, monkeypatch):
    accepted(control())
    calls = []
    typed_errors = []
    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        assert 'owner' in kwargs, 'adapter must call FunctionExpectation with keyword fields'
        assert kwargs['owner'] == case['edit']['value']
        try:
            return FunctionExpectation(*args, **kwargs)
        except MigrationUnitRejected as error:
            typed_errors.append(error)
            raise
    def forbidden(*args, **kwargs):
        pytest.fail('parser or checksum reached before typed owner refusal')
    monkeypatch.setattr(subject, 'FunctionExpectation', observed)
    monkeypatch.setattr(subject, '_load_parser', forbidden)
    monkeypatch.setattr(subject, 'function_checksum', forbidden)
    payload = control()
    payload['verification']['functions'][0]['owner'] = case['edit']['value']
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
    assert calls, 'A generic JSON/serializer rejection does not prove FP18'
    assert len(typed_errors) == 1 and caught.value is typed_errors[0]
    assert caught.value.reason_code == 'VERIFICATION_IDENTIFIER_INVALID'

@pytest.mark.parametrize('field', ['template', 'body', 'comment', 'config', 'suffix'])
def test_nul_intake(field, monkeypatch):
    accepted(control())
    payload = control()
    if field == 'template':
        payload['template'] = payload['template'].replace('CREATE FUNCTION', 'CREATE\0 FUNCTION', 1)
    elif field == 'suffix':
        payload['template'] += '\0CREATE TABLE forbidden_suffix(a int);'
    elif field == 'body':
        payload['verification']['functions'][0]['body'] += '\0'
    elif field == 'comment':
        payload['policy']['functions'][0]['comment'] = '\0'
    else:
        payload['policy']['functions'][0]['config'][0] += '\0'
    def forbidden(*args, **kwargs):
        pytest.fail('NUL intake reached parser/checksum')
    monkeypatch.setattr(subject, '_load_parser', forbidden)
    monkeypatch.setattr(subject, 'function_checksum', forbidden)
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
    assert caught.value.reason_code == 'INSTALL_NUL_FORBIDDEN'

@pytest.mark.parametrize('key,value', [('policy_format', 2), ('semantic_version', 2), ('parser_version', '7.16')])
def test_policy_metadata(key, value):
    payload = control()
    payload['policy'][key] = value
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
    assert caught.value.reason_code == 'INSTALL_POLICY_INVALID'

def test_schema():
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(control()), schema_key='tenant_a;')
    assert caught.value.reason_code == 'SCHEMA_KEY_INVALID'

def test_parser_error_is_sanitized(caplog):
    caplog.set_level(1)
    payload = control()
    marker = 'sensitive_parser_diagnostic_7ef109'
    payload['template'] = 'CREATE FUNCTION {schema}.' + marker + '('
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
    assert caught.value.reason_code == 'INSTALL_PARSE_ERROR'
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert marker not in caplog.text
    assert all(marker not in repr(record.__dict__) for record in caplog.records)
    assert marker not in ''.join(traceback.format_exception(caught.value))

@pytest.mark.parametrize('condition,reason', [
    ('absent', 'INSTALL_PARSER_UNAVAILABLE'),
    ('version', 'INSTALL_PARSER_VERSION_MISMATCH'),
])
def test_dependency_gate(condition, reason, monkeypatch):
    # Proposed private loader uses these standard-library APIs lazily.
    import importlib
    import importlib.metadata
    real_import = importlib.import_module
    real_version = importlib.metadata.version
    def load(name, *args, **kwargs):
        if name == 'pglast' and condition == 'absent':
            raise ModuleNotFoundError('sanitized test dependency absence')
        return real_import(name, *args, **kwargs)
    def version(name):
        if name == 'pglast':
            if condition == 'absent':
                raise importlib.metadata.PackageNotFoundError(name)
            return '7.16'
        return real_version(name)
    monkeypatch.setattr(importlib, 'import_module', load)
    monkeypatch.setattr(importlib.metadata, 'version', version)
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(control()), schema_key=SCHEMA)
    assert caught.value.reason_code == reason


def test_owner_not_string_is_declaration_type_refusal(monkeypatch):
    accepted(control())
    case = next(c for c in CASES['cases'] if c['id'] == 'I-owner-not-string')
    payload = control()
    payload['verification']['functions'][0]['owner'] = case['edit']['value']
    assert payload['verification']['functions'][0]['owner'] is None
    def forbidden(*args, **kwargs):
        pytest.fail('O03 declaration-type refusal reached constructor/parser/checksum')
    monkeypatch.setattr(subject, 'FunctionExpectation', forbidden)
    monkeypatch.setattr(subject, '_load_parser', forbidden)
    monkeypatch.setattr(subject, 'function_checksum', forbidden)
    with pytest.raises(MigrationUnitRejected) as caught:
        subject.validate_function_installation(encode(payload), schema_key=SCHEMA)
    assert caught.value.reason_code == 'INSTALL_POLICY_INVALID'
