"""Pure, closed installation policy for function-v3 declarations.

Acceptance binds exact SQL bytes, not database execution authority. Parser loading
is lazy; no SQL is executed here. The existing checksum and verifier stay intact.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
from dataclasses import dataclass, replace
from typing import Any, NoReturn, cast

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.resolver import SCHEMA_KEY_PATTERN

from .checksum import normalize_body
from .codes import PreconditionCode as Code
from .function_checksum import function_checksum
from .verification import AclEntry, FunctionExpectation, FunctionMetadataVerification

_IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]{0,62}')
_MIGRATION = re.compile(r't\d{3}(_test)?_[a-z0-9_]{1,64}')
_ROLE = re.compile(r'^haloflow_[a-z0-9_]{1,48}$')
_TYPES = frozenset({'text', 'uuid', 'jsonb', 'bool', 'int2', 'int4', 'int8', 'timestamptz'})
_CONFIG = ['search_path=pg_catalog, {schema}, pg_temp']
_ROOT = {
    'checksum_version',
    'migration_id',
    'execution_role',
    'template',
    'verification',
    'policy',
}
_POLICY = {
    'policy_format',
    'semantic_version',
    'parser_package',
    'parser_version',
    'grammar_major',
    'functions',
}
_FUNCTION = {
    'schema',
    'name',
    'inputs',
    'outputs',
    'return_type',
    'language',
    'is_procedure',
    'replace',
    'security_definer',
    'volatility',
    'parallel',
    'strict',
    'config',
    'body_sha256',
    'acl',
    'comment',
}
_VERIFICATION = {
    'name',
    'argument_types',
    'owner',
    'security_definer',
    'config',
    'acl',
    'body',
}
_SLOTS = {
    'RawStmt': 'stmt stmt_location stmt_len',
    'CreateFunctionStmt': 'is_procedure replace funcname parameters returnType options sql_body',
    'FunctionParameter': 'name argType mode defexpr',
    'TypeName': 'names setof pct_type typmods typemod arrayBounds location',
    'DefElem': 'defnamespace defname arg defaction location',
    'VariableSetStmt': 'kind name args is_local',
    'A_Const': 'isnull val', 'String': 'sval', 'Boolean': 'boolval', 'Integer': 'ival',
    'GrantStmt': ('is_grant targtype objtype objects privileges grantees '
                  'grant_option grantor behavior'),
    'AccessPriv': 'priv_name cols', 'RoleSpec': 'roletype rolename location',
    'ObjectWithArgs': 'objname objargs objfuncargs args_unspecified',
    'CommentStmt': 'objtype object comment',
}


@dataclass(frozen=True, slots=True)
class FunctionPolicyResult:
    migration_id: str
    schema_key: str
    execution_role: str
    policy_version: int
    checksum: str
    sql_bytes: bytes


@dataclass(frozen=True, slots=True)
class _Declaration:
    payload: dict[str, Any]
    functions: dict[tuple[str, tuple[str, ...]], dict[str, Any]]
    parser: Any = None


def _fail(code: Code) -> NoReturn:
    raise MigrationUnitRejected(reason_code=code.value) from None


def _require(ok: bool, code: Code = Code.INSTALL_POLICY_INVALID) -> None:
    if not ok:
        _fail(code)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _constant(_: str) -> NoReturn:
    _fail(Code.INSTALL_POLICY_INVALID)


def _nul(value: Any) -> None:
    if type(value) is str:
        _require('\x00' not in value, Code.INSTALL_NUL_FORBIDDEN)
    elif type(value) is list:
        for item in value:
            _nul(item)
    elif type(value) is dict:
        for key, item in value.items():
            _nul(key)
            _nul(item)


def _normalize(value: Any) -> Any:
    if type(value) is str:
        value = normalize_body(value)
        value.encode('utf-8', 'strict')
        return value
    if type(value) is dict:
        return _pairs([(_normalize(k), _normalize(v)) for k, v in value.items()])
    if type(value) is list:
        return [_normalize(v) for v in value]
    return value


def _record(value: Any, fields: set[str]) -> None:
    _require(type(value) is dict and set(value) == fields)


def _strings(value: Any) -> None:
    _require(type(value) is list and all(type(v) is str for v in value))


def _acl_shape(value: Any) -> None:
    _require(type(value) is list)
    for item in value:
        _record(item, {'grantee', 'privileges'})
        _require(type(item['grantee']) is str)
        _strings(item['privileges'])


def _closed_shape(payload: Any) -> None:
    """O03: exact types precede every typed constructor, including owner."""
    _record(payload, _ROOT)
    _require(type(payload['checksum_version']) is int)
    for key in ('migration_id', 'execution_role', 'template'):
        _require(type(payload[key]) is str)
    policy = payload['policy']
    _record(policy, _POLICY)
    for key in ('policy_format', 'semantic_version', 'grammar_major'):
        _require(type(policy[key]) is int)
    for key in ('parser_package', 'parser_version'):
        _require(type(policy[key]) is str)
    _require(type(policy['functions']) is list and bool(policy['functions']))
    for f in policy['functions']:
        _record(f, _FUNCTION)
        for key in ('schema', 'name', 'return_type', 'language', 'volatility',
                    'parallel', 'body_sha256'):
            _require(type(f[key]) is str)
        for key in ('is_procedure', 'replace', 'security_definer', 'strict'):
            _require(type(f[key]) is bool)
        _require(f['comment'] is None or type(f['comment']) is str)
        for key in ('inputs', 'outputs'):
            _require(type(f[key]) is list)
            for parameter in f[key]:
                _record(parameter, {'name', 'type'})
                _require(parameter['name'] is None or type(parameter['name']) is str)
                _require(type(parameter['type']) is str)
        _strings(f['config'])
        _acl_shape(f['acl'])
    verification = payload['verification']
    _record(verification, {'kind', 'functions'})
    _require(type(verification['kind']) is str and type(verification['functions']) is list)
    for f in verification['functions']:
        _record(f, _VERIFICATION)
        for key in ('name', 'owner', 'body'):
            _require(type(f[key]) is str)
        _require(type(f['security_definer']) is bool)
        _strings(f['argument_types'])
        _strings(f['config'])
        _acl_shape(f['acl'])


def _unique_declarations(payload: dict[str, Any]) -> None:
    """O03 normalized identities are checked before typed syntax at O04."""
    for section in ('policy', 'verification'):
        seen = set()
        for f in payload[section]['functions']:
            identity = (_identity(f) if section == 'policy'
                        else (f['name'], tuple(f['argument_types'])))
            _require(identity not in seen)
            seen.add(identity)
            roles = [a['grantee'] for a in f['acl']]
            _require(len(set(roles)) == len(roles))
            for a in f['acl']:
                _require(len(set(a['privileges'])) == len(a['privileges']))
            keys = [c.partition('=')[0] for c in f['config']]
            _require(len(set(keys)) == len(keys))


def _identity(f: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return f['name'], tuple(p['type'] for p in f['inputs'])


def _declaration(payload: dict[str, Any]) -> _Declaration:
    policy = payload['policy']
    _require(payload['checksum_version'] == 3)
    _require((policy['policy_format'], policy['semantic_version'], policy['parser_package'],
              policy['parser_version'], policy['grammar_major']) == (1, 1, 'pglast', '7.17', 17))
    _require(bool(_ROLE.fullmatch(payload['execution_role'])), Code.EXECUTION_ROLE_INVALID)
    # Infrastructure membership comes from the existing role vocabulary.
    from .roles import PROVISIONING_ROLES
    _require(payload['execution_role'] not in PROVISIONING_ROLES,
             Code.EXECUTION_ROLE_IS_INFRASTRUCTURE)
    _require(bool(_MIGRATION.fullmatch(payload['migration_id'])), Code.MIGRATION_ID_INVALID)
    _require(bool(payload['template'].strip()), Code.MIGRATION_TEMPLATE_EMPTY)
    _require('{schema}' in payload['template'], Code.MIGRATION_TEMPLATE_UNSCOPED)
    typed = []
    for f in payload['verification']['functions']:
        typed.append(FunctionExpectation(
            name=f['name'], argument_types=tuple(f['argument_types']), owner=f['owner'],
            security_definer=f['security_definer'], config=tuple(f['config']),
            acl=tuple(AclEntry(grantee=a['grantee'], privileges=tuple(a['privileges']))
                      for a in f['acl']), body=f['body']))
    FunctionMetadataVerification(functions=tuple(typed), kind=payload['verification']['kind'])
    verification = {(f.name, f.argument_types): f for f in typed}
    functions = {}
    for f in policy['functions']:
        identity = _identity(f)
        _require(identity not in functions)
        _require(f['schema'] == '{schema}' and bool(_IDENTIFIER.fullmatch(f['name'])))
        for group in ('inputs', 'outputs'):
            names = []
            for p in f[group]:
                name, kind = p['name'], p['type']
                _require(name is None or bool(_IDENTIFIER.fullmatch(name)))
                _require(group != 'outputs' or name is not None)
                _require(kind in _TYPES or (group == 'outputs' and kind.endswith('[]')
                                            and kind[:-2] in _TYPES))
                if name is not None:
                    names.append(name)
            _require(len(set(names)) == len(names))
        outputs = f['outputs']
        if outputs:
            derived = outputs[0]['type'] if len(outputs) == 1 else 'record'
            _require(f['return_type'] == derived)
        else:
            _require(f['return_type'] in _TYPES | {'void', 'record'})
        _require(f['language'] == 'plpgsql' and not f['is_procedure'] and not f['replace'])
        _require(f['security_definer'] and f['volatility'] in {'immutable', 'stable', 'volatile'}
                 and f['parallel'] in {'safe', 'restricted', 'unsafe'})
        _require(f['config'] == _CONFIG)
        _require(bool(re.fullmatch('[0-9a-f]{64}', f['body_sha256'])))
        acl = tuple(AclEntry(grantee=a['grantee'], privileges=tuple(a['privileges']))
                    for a in f['acl'])
        _require(len({a.grantee for a in acl}) == len(acl))
        _require(identity in verification)
        expected = verification[identity]
        _require(expected.security_definer == f['security_definer']
                 and expected.config == tuple(f['config'])
                 and expected.acl == tuple(sorted(acl, key=lambda a: a.grantee)))
        _require(hashlib.sha256(expected.body.encode('utf-8')).hexdigest() == f['body_sha256'])
        functions[identity] = f
    _require(set(functions) == set(verification))
    return _Declaration(payload, functions)


def _render_exact_sql(template: str, schema_key: str) -> bytes:
    return template.replace('{schema}', schema_key).encode('utf-8', 'strict')


def _load_parser() -> Any:
    try:
        parser = importlib.import_module('pglast')
        version = importlib.metadata.version('pglast')
    except (ImportError, importlib.metadata.PackageNotFoundError):
        _fail(Code.INSTALL_PARSER_UNAVAILABLE)
    try:
        compatible = version == '7.17' and parser.get_postgresql_version()[0] == 17
    except Exception:
        _fail(Code.INSTALL_PARSER_VERSION_MISMATCH)
    _require(compatible, Code.INSTALL_PARSER_VERSION_MISMATCH)
    importlib.import_module('pglast.ast')
    return parser


def _parse_exact_sql(parser: Any, sql_bytes: bytes) -> tuple[Any, ...]:
    try:
        text = sql_bytes.decode('utf-8', 'strict')
        _require(text.encode('utf-8') == sql_bytes, Code.INSTALL_PARSE_ERROR)
        nodes = parser.parse_sql(text)
    except Exception:
        _fail(Code.INSTALL_PARSE_ERROR)
    _require(type(nodes) is tuple, Code.INSTALL_AST_INVALID)
    return cast(tuple[Any, ...], nodes)


def _node(value: Any, name: str, d: _Declaration,
          code: Code = Code.INSTALL_AST_INVALID) -> Any:
    cls = getattr(d.parser.ast, name)
    _require(type(value) is cls, code)
    _require(set(type(value).__slots__) == set(_SLOTS[name].split()), Code.INSTALL_AST_INVALID)
    return value


def _seq(value: Any, code: Code = Code.INSTALL_AST_INVALID) -> tuple[Any, ...]:
    if value is None:
        return ()
    _require(type(value) is tuple, code)
    return cast(tuple[Any, ...], value)


def _string(value: Any, d: _Declaration, code: Code = Code.INSTALL_AST_INVALID) -> str:
    value = _node(value, 'String', d, code).sval
    _require(type(value) is str, code)
    return normalize_body(value)


def _qualified(value: Any, d: _Declaration, schema_key: str) -> str:
    names = tuple(_string(v, d) for v in _seq(value))
    _require(len(names) == 2 and names[0] == schema_key, Code.INSTALL_SIGNATURE_MISMATCH)
    return names[1]


def _typename(value: Any, d: _Declaration, *, array: bool = False,
              returns: bool = False, setof: bool = False) -> str:
    t = _node(value, 'TypeName', d)
    code = Code.INSTALL_TYPE_SHAPE_FORBIDDEN
    _require(t.pct_type is False and t.setof is setof and t.typmods is None
             and type(t.typemod) is int and t.typemod == -1, code)
    names = tuple(_string(n, d) for n in _seq(t.names))
    _require(len(names) == 1 or (len(names) == 2 and names[0] == 'pg_catalog'), code)
    kind = names[-1]
    _require(kind in _TYPES or (returns and kind in {'void', 'record'}), code)
    if t.arrayBounds is not None:
        bounds = _seq(t.arrayBounds, code)
        _require(array and kind in _TYPES and len(bounds) == 1, code)
        bound = _node(bounds[0], 'Integer', d, code).ival
        _require(type(bound) is int and bound == -1, code)
        kind += '[]'
    return kind


def _parameters(
    node: Any, d: _Declaration,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    modes = d.parser.enums.FunctionParameterMode
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    parameters = _seq(node.parameters)
    # O09a completes before any defaults or type/signature comparison.
    seen_table = False
    for p in parameters:
        _node(p, 'FunctionParameter', d)
        if p.mode == modes.FUNC_PARAM_TABLE:
            seen_table = True
        else:
            _require(p.mode in (modes.FUNC_PARAM_DEFAULT, modes.FUNC_PARAM_IN)
                     and not seen_table, Code.INSTALL_PARAMETER_MODE_FORBIDDEN)
    for p in parameters:
        table = p.mode == modes.FUNC_PARAM_TABLE
        _require(p.defexpr is None, Code.INSTALL_PARAMETER_DEFAULT_FORBIDDEN)
        _require(p.name is None or type(p.name) is str, Code.INSTALL_AST_INVALID)
        _require(not table or bool(p.name), Code.INSTALL_TYPE_SHAPE_FORBIDDEN)
        kind = _typename(p.argType, d, array=table)
        group = outputs if table else inputs
        group.append({'name': normalize_body(p.name) if p.name is not None else None, 'type': kind})
    _require(len({p['name'] for p in outputs}) == len(outputs), Code.INSTALL_TYPE_SHAPE_FORBIDDEN)
    result = _typename(node.returnType, d, array=bool(outputs), returns=True, setof=bool(outputs))
    if outputs:
        derived = outputs[0]['type'] if len(outputs) == 1 else 'record'
        _require(result == derived, Code.INSTALL_TYPE_SHAPE_FORBIDDEN)
    return inputs, outputs, result


def _options(node: Any, f: dict[str, Any], d: _Declaration, schema_key: str) -> None:
    opts: dict[str, Any] = {}
    sets = []
    enums = d.parser.enums
    for opt in _seq(node.options):
        _node(opt, 'DefElem', d)
        _require(opt.defnamespace is None and opt.defaction == enums.DefElemAction.DEFELEM_UNSPEC,
                 Code.INSTALL_OPTION_MISMATCH)
        if opt.defname == 'set':
            sets.append(opt.arg)
        else:
            _require(opt.defname in {
                'as', 'language', 'security', 'volatility', 'parallel', 'strict'}
                     and opt.defname not in opts, Code.INSTALL_OPTION_MISMATCH)
            opts[opt.defname] = opt.arg
    _require(set(opts) == {'as', 'language', 'security', 'volatility', 'parallel', 'strict'},
             Code.INSTALL_OPTION_MISMATCH)
    for key in ('language', 'volatility', 'parallel'):
        _require(_string(opts[key], d, Code.INSTALL_OPTION_MISMATCH) == f[key],
                 Code.INSTALL_OPTION_MISMATCH)
    for option, field in [('security', 'security_definer'), ('strict', 'strict')]:
        value = _node(opts[option], 'Boolean', d, Code.INSTALL_OPTION_MISMATCH).boolval
        _require(type(value) is bool and value == f[field], Code.INSTALL_OPTION_MISMATCH)
    _require(len(sets) == 1, Code.INSTALL_CONFIG_MISMATCH)
    setting = _node(sets[0], 'VariableSetStmt', d, Code.INSTALL_CONFIG_MISMATCH)
    _require(setting.kind == enums.VariableSetKind.VAR_SET_VALUE and setting.is_local is False
             and setting.name == 'search_path', Code.INSTALL_CONFIG_MISMATCH)
    path = []
    for arg in _seq(setting.args, Code.INSTALL_CONFIG_MISMATCH):
        _node(arg, 'A_Const', d, Code.INSTALL_CONFIG_MISMATCH)
        _require(arg.isnull is False, Code.INSTALL_CONFIG_MISMATCH)
        path.append(_string(arg.val, d, Code.INSTALL_CONFIG_MISMATCH))
    _require(path == ['pg_catalog', schema_key, 'pg_temp'], Code.INSTALL_CONFIG_MISMATCH)
    bodies = _seq(opts['as'], Code.INSTALL_OPTION_MISMATCH)
    _require(len(bodies) == 1, Code.INSTALL_OPTION_MISMATCH)
    body = _string(bodies[0], d, Code.INSTALL_OPTION_MISMATCH)
    _require(bool(body), Code.INSTALL_OPTION_MISMATCH)
    expected = next(v['body'] for v in d.payload['verification']['functions']
                    if (v['name'], tuple(v['argument_types'])) == _identity(f))
    # The declaration digest describes the template body; compare rendered bodies.
    expected = normalize_body(expected).replace('{schema}', schema_key)
    _require(hashlib.sha256(body.encode('utf-8')).digest()
             == hashlib.sha256(expected.encode('utf-8')).digest(), Code.INSTALL_BODY_MISMATCH)


def _target(obj: Any, d: _Declaration, schema_key: str) -> tuple[str, tuple[str, ...]]:
    obj = _node(obj, 'ObjectWithArgs', d)
    _require(obj.args_unspecified is False, Code.INSTALL_SIGNATURE_MISMATCH)
    name = _qualified(obj.objname, d, schema_key)
    args = tuple(_typename(t, d) for t in _seq(obj.objargs))
    if obj.objfuncargs is not None:
        dual = []
        modes = d.parser.enums.FunctionParameterMode
        for p in _seq(obj.objfuncargs):
            _node(p, 'FunctionParameter', d)
            _require(p.name is None and p.defexpr is None
                     and p.mode in (modes.FUNC_PARAM_DEFAULT, modes.FUNC_PARAM_IN),
                     Code.INSTALL_SIGNATURE_MISMATCH)
            dual.append(_typename(p.argType, d))
        _require(tuple(dual) == args, Code.INSTALL_SIGNATURE_MISMATCH)
    _require((name, args) in d.functions, Code.INSTALL_SIGNATURE_MISMATCH)
    return name, args


def _validate_local_statements(nodes: tuple[Any, ...], declaration: _Declaration,
                               *, schema_key: str) -> None:
    """O11: local shape only; caller-set completeness belongs exclusively to O12."""
    d = declaration
    ast, e = d.parser.ast, d.parser.enums
    for raw in nodes:
        node = raw.stmt
        if type(node) is ast.GrantStmt:
            _node(node, 'GrantStmt', d)
            _require(node.targtype == e.GrantTargetType.ACL_TARGET_OBJECT
                     and node.objtype == e.ObjectType.OBJECT_FUNCTION
                     and node.grant_option is False and node.grantor is None
                     and node.behavior == e.DropBehavior.DROP_RESTRICT,
                     Code.INSTALL_ACL_MISMATCH)
            objects = _seq(node.objects)
            _require(len(objects) == 1, Code.INSTALL_ACL_MISMATCH)
            _target(objects[0], d, schema_key)
            roles = _seq(node.grantees)
            _require(bool(roles), Code.INSTALL_ACL_MISMATCH)
            for role in roles:
                _node(role, 'RoleSpec', d)
            if node.is_grant is False:
                _require(node.privileges is None and len(roles) == 1
                         and roles[0].roletype == e.RoleSpecType.ROLESPEC_PUBLIC
                         and roles[0].rolename is None, Code.INSTALL_ACL_MISMATCH)
            else:
                _require(node.is_grant is True, Code.INSTALL_AST_INVALID)
                privileges = _seq(node.privileges)
                _require(len(privileges) == 1, Code.INSTALL_ACL_MISMATCH)
                privilege = _node(privileges[0], 'AccessPriv', d)
                _require(privilege.priv_name == 'execute' and privilege.cols is None,
                         Code.INSTALL_ACL_MISMATCH)
                for role in roles:
                    _require(role.roletype == e.RoleSpecType.ROLESPEC_CSTRING
                             and type(role.rolename) is str
                             and bool(_IDENTIFIER.fullmatch(role.rolename))
                             and role.rolename != 'public', Code.INSTALL_ACL_MISMATCH)
                names = [normalize_body(r.rolename) for r in roles]
                _require(len(set(names)) == len(names), Code.INSTALL_ACL_MISMATCH)
        elif type(node) is ast.CommentStmt:
            _node(node, 'CommentStmt', d)
            _require(node.objtype == e.ObjectType.OBJECT_FUNCTION, Code.INSTALL_COMMENT_MISMATCH)
            identity = _target(node.object, d, schema_key)
            expected = d.functions[identity]['comment']
            _require(type(node.comment) is str and expected is not None
                     and normalize_body(node.comment) == expected.replace('{schema}', schema_key),
                     Code.INSTALL_COMMENT_MISMATCH)


def _validate_statement_inventory(nodes: tuple[Any, ...], declaration: _Declaration,
                                  *, schema_key: str) -> None:
    """O12: count and order parsed statements; never inspect SQL text."""
    d = declaration
    ast = d.parser.ast
    inventory: dict[tuple[str, tuple[str, ...]], dict[str, list[tuple[int, Any]]]] = {
        key: {'create': [], 'revoke': [], 'grant': [], 'comment': []}
        for key in d.functions
    }
    for position, raw in enumerate(nodes):
        node = raw.stmt
        if type(node) is ast.CreateFunctionStmt:
            inputs, _, _ = _parameters(node, d)
            identity = (_qualified(node.funcname, d, schema_key), tuple(p['type'] for p in inputs))
            category = 'create'
        elif type(node) is ast.GrantStmt:
            identity = _target(node.objects[0], d, schema_key)
            category = 'grant' if node.is_grant else 'revoke'
        else:
            identity = _target(node.object, d, schema_key)
            category = 'comment'
        inventory[identity][category].append((position, node))
    for identity, group in inventory.items():
        f = d.functions[identity]
        _require(len(group['create']) == 1 and len(group['revoke']) == 1
                 and len(group['grant']) == (1 if f['acl'] else 0),
                 Code.INSTALL_STATEMENT_COUNT_INVALID)
        _require(len(group['comment']) == (0 if f['comment'] is None else 1),
                 Code.INSTALL_COMMENT_MISMATCH)
        created = group['create'][0][0]
        _require(all(position > created for key in ('revoke', 'grant', 'comment')
                     for position, _ in group[key]), Code.INSTALL_STATEMENT_ORDER_INVALID)
        if group['grant']:
            position, grant = group['grant'][0]
            _require(group['revoke'][0][0] < position, Code.INSTALL_STATEMENT_ORDER_INVALID)
            _require({normalize_body(r.rolename) for r in grant.grantees}
                     == {a['grantee'] for a in f['acl']}, Code.INSTALL_ACL_MISMATCH)


def _validate_ast(nodes: tuple[Any, ...], declaration: _Declaration, *, schema_key: str) -> None:
    d = declaration
    ast, e = d.parser.ast, d.parser.enums
    _require(type(nodes) is tuple, Code.INSTALL_AST_INVALID)
    forbidden = (ast.CreateStmt, ast.CreateTableAsStmt, ast.GrantRoleStmt,
                 ast.AlterDefaultPrivilegesStmt, ast.VariableSetStmt)
    for raw in nodes:
        _node(raw, 'RawStmt', d)
        node = raw.stmt
        if (type(node) in forbidden
                or (type(node) is ast.SelectStmt and node.intoClause is not None)):
            _fail(Code.INSTALL_TOPLEVEL_FORM_FORBIDDEN)
        if type(node) not in (ast.CreateFunctionStmt, ast.GrantStmt, ast.CommentStmt):
            _fail(Code.INSTALL_TOPLEVEL_FORM_UNKNOWN)
        if type(node) is ast.GrantStmt and node.objtype != e.ObjectType.OBJECT_FUNCTION:
            _fail(Code.INSTALL_TOPLEVEL_FORM_FORBIDDEN)
        _node(node, type(node).__name__, d)
    # Complete the CREATE phases before entering O11 and O12 observations.
    for raw in nodes:
        node = raw.stmt
        if type(node) is not ast.CreateFunctionStmt:
            continue
        _require(node.is_procedure is False and node.replace is False and node.sql_body is None,
                 Code.INSTALL_TOPLEVEL_FIELD_MISMATCH)
        name = _qualified(node.funcname, d, schema_key)
        _require(any(key[0] == name for key in d.functions), Code.INSTALL_SIGNATURE_MISMATCH)
        inputs, outputs, result = _parameters(node, d)
        identity = (name, tuple(p['type'] for p in inputs))
        _require(identity in d.functions, Code.INSTALL_SIGNATURE_MISMATCH)
        f = d.functions[identity]
        _require(inputs == f['inputs'] and outputs == f['outputs'] and result == f['return_type'],
                 Code.INSTALL_SIGNATURE_MISMATCH)
        _options(node, f, d, schema_key)
    _validate_local_statements(nodes, d, schema_key=schema_key)
    _validate_statement_inventory(nodes, d, schema_key=schema_key)


def validate_function_installation(payload_json: bytes, *, schema_key: str) -> FunctionPolicyResult:
    """Validate an owned JSON copy and bind exact rendered bytes; no execution."""
    _require(type(payload_json) is bytes)
    try:
        payload = json.loads(payload_json.decode('utf-8', 'strict'),
                             object_pairs_hook=_pairs, parse_constant=_constant)
    except MigrationUnitRejected:
        raise
    except (ValueError, UnicodeError, RecursionError):
        _fail(Code.INSTALL_POLICY_INVALID)
    try:
        _nul(payload)
        payload = _normalize(payload)
        _closed_shape(payload)
        _unique_declarations(payload)
    except MigrationUnitRejected:
        raise
    except (ValueError, UnicodeError, RecursionError):
        _fail(Code.INSTALL_POLICY_INVALID)
    d = _declaration(payload)
    _require(type(schema_key) is str and bool(SCHEMA_KEY_PATTERN.fullmatch(schema_key)),
             Code.SCHEMA_KEY_INVALID)
    try:
        sql = _render_exact_sql(payload['template'], schema_key)
    except (ValueError, UnicodeError):
        _fail(Code.INSTALL_POLICY_INVALID)
    _require(type(sql) is bytes, Code.INSTALL_AST_INVALID)
    _require(b'\x00' not in sql, Code.INSTALL_NUL_FORBIDDEN)
    parser = _load_parser()
    nodes = _parse_exact_sql(parser, sql)
    d = replace(d, parser=parser)
    _validate_ast(nodes, d, schema_key=schema_key)
    try:
        checksum = function_checksum(**{key: payload[key] for key in
            ('migration_id', 'template', 'execution_role', 'verification', 'policy')})
    except (ValueError, TypeError, UnicodeError):
        _fail(Code.INSTALL_POLICY_INVALID)
    return FunctionPolicyResult(payload['migration_id'], schema_key, payload['execution_role'],
                                payload['policy']['semantic_version'], checksum, sql)
