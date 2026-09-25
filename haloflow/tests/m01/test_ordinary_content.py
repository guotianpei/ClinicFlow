"""CG-4 -- U layer: `ordinary_content`, the role-bearing ordinary-unit content check.

Traceability: requirements v5 (CG4-R0 to R9, R1b, R8a), architecture v2 section 2-4,
owner rulings CL-1 / CL-2, test cases v1 as amended by v2 (U-01 to U-19).

PRE-CHANGE STATE: RED / INTERFACE, every case. `ordinary_content` does not exist
at `6bb10452`, so each case fails with `ModuleNotFoundError` from its LOCAL import.
That is interface-absence evidence only. It is NOT missing-behaviour evidence
(test cases v2 section 2); the behavioural red set lives in the I and D layers.

IMPORTS ARE LOCAL on purpose (the CP-7a pattern): a module-level import of the
absent module would turn every case into one collection error and hide the
per-case record.

WHAT THIS CANNOT ESTABLISH: anything about the runner, the ledger or PostgreSQL.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from types import SimpleNamespace
from typing import Any

import pglast
import pytest
from pglast import ast as pg_ast

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.codes import PreconditionCode, SanitizedErrorCode
from haloflow.m01.provisioning.units import (
    TenantMigrationUnit,
    UnitDefinition,
    build_tenant_migration_registry,
)

SCHEMA = "tenant_aaaaaaaa"
ROLE = "haloflow_m02_owner"
ROLES = frozenset({ROLE})

# Approved literal codes (test cases v2 section 1.2). U-18 checks membership.
PROHIBITED = "ORDINARY_ROLE_CONTENT_PROHIBITED"
UNPARSEABLE = "ORDINARY_ROLE_CONTENT_UNPARSEABLE"
PARSER_UNAVAILABLE = PreconditionCode.INSTALL_PARSER_UNAVAILABLE.value
PARSER_VERSION_MISMATCH = PreconditionCode.INSTALL_PARSER_VERSION_MISMATCH.value

# The pins, as LITERALS. Deliberately not read from `function_policy.py`: two
# implementations that drift together would still agree with each other.
PINNED_PGLAST_VERSION = "7.17"
PINNED_GRAMMAR_MAJOR = 17

CREATE_ONE = f"CREATE TABLE {SCHEMA}.cg4_probe (id int);"


def _module() -> Any:
    """The module under test, imported at call time (interface-absent before CG-4)."""

    return importlib.import_module("haloflow.m01.provisioning.ordinary_content")


def _role_unit(template: str = "CREATE TABLE {schema}.cg4_probe (id int);") -> TenantMigrationUnit:
    registry = build_tenant_migration_registry(
        {"t001_test_cg4_role": UnitDefinition(template, execution_role=ROLE)},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )
    return registry.units[0]


def _migrator_unit() -> TenantMigrationUnit:
    registry = build_tenant_migration_registry(
        {"t001_test_cg4_migrator": "CREATE TABLE {schema}.cg4_probe (id int);"},
        allow_test_units=True,
    )
    return registry.units[0]


def _typed_unit(seam: Any, payload: dict[str, Any]) -> TenantMigrationUnit:
    registry = build_tenant_migration_registry(
        {payload["migration_id"]: seam.typed_definition(payload)},
        approved_execution_roles=ROLES,
        allow_test_units=True,
    )
    return registry.units[0]


def _refusal(rendered: str, unit: TenantMigrationUnit | None = None) -> str:
    """Call `check_rendered`, require `MigrationUnitRejected`, return its code."""

    module = _module()
    with pytest.raises(MigrationUnitRejected) as caught:
        module.check_rendered(unit=unit or _role_unit(), rendered=rendered)
    return caught.value.reason_code


class _ParseSpy:
    """Forwarding spy on `pglast.parse_sql` (test cases v2 section 1.1)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, returning: Any = None) -> None:
        self.calls: list[str] = []
        self._real = pglast.parse_sql
        self._returning = returning
        monkeypatch.setattr(pglast, "parse_sql", self)

    def __call__(self, text: str) -> Any:
        self.calls.append(text)
        if self._returning is not None:
            return self._returning
        return self._real(text)


# ---------------------------------------------------------------------------
# U-01  CG4-R0: the scope predicate.
# ---------------------------------------------------------------------------


def test_u01_scope_predicate_is_ordinary_and_role_bearing_only(
    seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """U-01. True only for an ordinary unit with a role; three quadrants are real units."""

    module = _module()
    assert module.requires_content_check(_role_unit()) is True
    assert module.requires_content_check(_migrator_unit()) is False
    assert module.requires_content_check(_typed_unit(seam, typed_payloads["first"])) is False

    # The fourth quadrant (typed, no role) cannot be built: prove that first, then
    # exercise the predicate on a DISCLOSED predicate-only double (test cases v2 s4).
    with pytest.raises(MigrationUnitRejected) as caught:
        build_tenant_migration_registry(
            {
                typed_payloads["first"]["migration_id"]: seam.typed_definition(
                    typed_payloads["first"], execution_role=None
                )
            },
            approved_execution_roles=ROLES,
            allow_test_units=True,
        )
    assert caught.value.reason_code == PreconditionCode.MIGRATION_UNIT_ROLE_REQUIRED.value
    double = SimpleNamespace(is_typed=True, execution_role=None)
    assert module.requires_content_check(double) is False


# ---------------------------------------------------------------------------
# U-02, U-03  CG4-R1a / R9: admitted content is returned unchanged.
# ---------------------------------------------------------------------------


def test_u02_one_create_table_is_admitted_and_the_same_object_is_returned() -> None:
    """U-02. The return value IS the input object -- no copy, no re-render."""

    rendered = "".join([CREATE_ONE])  # a fresh str object, not an interned literal
    assert _module().check_rendered(unit=_role_unit(), rendered=rendered) is rendered


def test_u03_several_create_table_statements_are_admitted() -> None:
    """U-03."""

    rendered = (
        f"CREATE TABLE {SCHEMA}.a (id int);"
        f" CREATE TABLE {SCHEMA}.b (id int);"
    )
    assert _module().check_rendered(unit=_role_unit(), rendered=rendered) is rendered


# ---------------------------------------------------------------------------
# U-04 to U-07  CG4-R1 / R1a / R1b: refusals with the prohibited-content code.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rendered",
    [
        f"CREATE FUNCTION {SCHEMA}.f() RETURNS int LANGUAGE sql AS 'SELECT 1';",
        f"CREATE PROCEDURE {SCHEMA}.p() LANGUAGE sql AS 'SELECT 1';",
        f"CREATE OR REPLACE FUNCTION {SCHEMA}.f() RETURNS int LANGUAGE sql AS 'SELECT 1';",
        "DO $$ BEGIN NULL; END $$;",
    ],
    ids=["create-function", "create-procedure", "create-or-replace", "do-block"],
)
def test_u04_explicitly_prohibited_kinds_are_refused(rendered: str) -> None:
    """U-04. CreateFunctionStmt (all three forms) and DoStmt."""

    assert _refusal(rendered) == PROHIBITED


@pytest.mark.parametrize(
    "rendered",
    [
        f"SELECT has_schema_privilege(current_role, '{SCHEMA}', 'CREATE'), current_role;",
        f"INSERT INTO {SCHEMA}.t VALUES (1);",
        f"GRANT SELECT ON {SCHEMA}.t TO haloflow_runtime;",
        "SET ROLE postgres;",
        f"CREATE TABLE {SCHEMA}.t AS SELECT 1;",
        f"CREATE FOREIGN TABLE {SCHEMA}.t (a int) SERVER s;",
        f"SELECT 1 INTO {SCHEMA}.t;",
    ],
    ids=["select", "insert", "grant", "set-role", "ctas", "foreign-table", "select-into"],
)
def test_u05_kinds_not_on_the_allowed_list_are_refused(rendered: str) -> None:
    """U-05. Closed list: anything that is not exactly CreateStmt fails closed."""

    assert _refusal(rendered) == PROHIBITED


@pytest.mark.parametrize(
    "rendered",
    [
        f"CREATE TABLE {SCHEMA}.a (x bigint NOT NULL);"
        f" INSERT INTO {SCHEMA}.a VALUES (pg_current_xact_id()::text::bigint);",
        f"CREATE TABLE {SCHEMA}.a (id integer PRIMARY KEY); SELECT 1 / 0;",
        f"CREATE TABLE {SCHEMA}.m (x bigint);"
        f"INSERT INTO {SCHEMA}.m VALUES (1);"
        f"CREATE FUNCTION {SCHEMA}.f(integer, text[]) RETURNS integer LANGUAGE sql"
        f" SECURITY DEFINER SET search_path = pg_catalog AS $b$SELECT 1$b$;"
        f"REVOKE ALL ON FUNCTION {SCHEMA}.f(integer, text[]) FROM PUBLIC;",
    ],
    ids=["rf2-create-insert", "rf3-create-select", "rf4-cp7b-shape"],
)
def test_u06_an_allowed_first_statement_does_not_admit_the_rest(rendered: str) -> None:
    """U-06. Mixed templates: the whole template is refused."""

    assert _refusal(rendered) == PROHIBITED


def test_u07_zero_statements_are_refused() -> None:
    """U-07. CG4-R1b: a comment-only template is refused, not admitted vacuously."""

    assert _refusal(f"-- {SCHEMA}: nothing but a comment\n") == PROHIBITED


# ---------------------------------------------------------------------------
# U-08 to U-10  CG4-R5 / R6 / R9: NUL guard, parse failure, exactly one parse.
# ---------------------------------------------------------------------------


def test_u08_a_nul_is_refused_before_any_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """U-08. pglast truncates at a NUL, so the byte guard must run first."""

    spy = _ParseSpy(monkeypatch)
    rendered = f"CREATE TABLE {SCHEMA}.t (id int);\x00 DROP TABLE {SCHEMA}.x;"

    assert _refusal(rendered) == UNPARSEABLE
    assert spy.calls == []


def test_u09_a_syntax_error_is_unparseable() -> None:
    """U-09."""

    assert _refusal("CREATE TABLE (;") == UNPARSEABLE


def test_u10_the_parser_is_called_exactly_once_with_the_exact_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-10. CG4-R9: one parse, shared by detection and the statement-kind check."""

    spy = _ParseSpy(monkeypatch)
    rendered = (
        f"CREATE TABLE {SCHEMA}.a (id int);"
        f" CREATE TABLE {SCHEMA}.b (id int);"
        f" CREATE TABLE {SCHEMA}.c (id int);"
    )

    assert _module().check_rendered(unit=_role_unit(), rendered=rendered) is rendered
    assert spy.calls == [rendered]


# ---------------------------------------------------------------------------
# U-11 to U-14  CG4-R1 / R1a: list discipline and exact types.
# ---------------------------------------------------------------------------


def test_u11_prohibited_kinds_win_even_if_the_allowed_list_names_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-11. "Whatever the allowed list says": the prohibited set is checked first."""

    module = _module()
    monkeypatch.setattr(
        module, "ALLOWED_TOPLEVEL_KINDS", ("CreateStmt", "CreateFunctionStmt")
    )

    rendered = f"CREATE FUNCTION {SCHEMA}.f() RETURNS int LANGUAGE sql AS 'SELECT 1';"
    assert _refusal(rendered) == PROHIBITED


def test_u12_the_lists_are_exactly_as_ruled_and_disjoint() -> None:
    """U-12. CG4-R1a (RQ-E ruling) and CG4-R1, as literals."""

    module = _module()
    assert module.ALLOWED_TOPLEVEL_KINDS == ("CreateStmt",)
    assert module.PROHIBITED_TOPLEVEL_KINDS == ("CreateFunctionStmt", "DoStmt")
    assert set(module.ALLOWED_TOPLEVEL_KINDS) & set(module.PROHIBITED_TOPLEVEL_KINDS) == set()


def test_u13_a_subclass_of_create_stmt_is_not_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """U-13. Exact type (`is`), not `isinstance`.

    The fixture is asserted BEFORE the refusal, so a constructor failure can
    never pass as a rejection (test cases v2 section 4).
    """

    class SubCreateStmt(pg_ast.CreateStmt):  # type: ignore[misc]
        pass

    raw = pg_ast.RawStmt(stmt=SubCreateStmt())
    assert type(raw) is pg_ast.RawStmt
    assert type(raw.stmt) is SubCreateStmt
    assert SubCreateStmt is not pg_ast.CreateStmt

    _ParseSpy(monkeypatch, returning=(raw,))
    assert _refusal(CREATE_ONE) == PROHIBITED


@pytest.mark.parametrize(
    "parse_result",
    [
        [pg_ast.RawStmt(stmt=pg_ast.CreateStmt())],
        (pg_ast.CreateStmt(),),
    ],
    ids=["list-not-tuple", "element-not-rawstmt"],
)
def test_u14_an_unexpected_parse_result_is_unparseable(
    monkeypatch: pytest.MonkeyPatch, parse_result: Any
) -> None:
    """U-14. Anything but a tuple of RawStmt fails closed as unparseable."""

    _ParseSpy(monkeypatch, returning=parse_result)
    assert _refusal(CREATE_ONE) == UNPARSEABLE


# ---------------------------------------------------------------------------
# U-15  CL-2: the misuse guard.
# ---------------------------------------------------------------------------


def test_u15_out_of_scope_units_are_refused_without_parsing(
    monkeypatch: pytest.MonkeyPatch, seam: Any, typed_payloads: dict[str, Any]
) -> None:
    """U-15. CL-2's documented exception: an internal-fault use of the content code."""

    spy = _ParseSpy(monkeypatch)

    assert _refusal(CREATE_ONE, unit=_migrator_unit()) == PROHIBITED
    assert _refusal(CREATE_ONE, unit=_typed_unit(seam, typed_payloads["first"])) == PROHIBITED
    assert spy.calls == []


# ---------------------------------------------------------------------------
# U-16, U-17  CL-1 / CG4-R9: the parser loader.
# ---------------------------------------------------------------------------


def test_u16_the_installed_parser_matches_the_literal_pins_and_is_used() -> None:
    """U-16. The environment carries exactly the pinned parser, and the check works on it.

    AUTHORING NOTE (disclosed for the assertion review): v1 phrased U-16 as "the
    loader returns the four classes". The loader has no approved public name, so
    this case asserts the same thing through behaviour: CreateStmt is admitted and
    DoStmt / CreateFunctionStmt are refused, which requires all four classes to
    have resolved.
    """

    assert importlib.metadata.version("pglast") == PINNED_PGLAST_VERSION
    assert pglast.get_postgresql_version()[0] == PINNED_GRAMMAR_MAJOR

    assert _module().check_rendered(unit=_role_unit(), rendered=CREATE_ONE) == CREATE_ONE
    assert _refusal("DO $$ BEGIN NULL; END $$;") == PROHIBITED


def _fake_version(value: str | None) -> Any:
    real = importlib.metadata.version

    def version(name: str) -> str:
        if name == "pglast":
            if value is None:
                raise importlib.metadata.PackageNotFoundError(name)
            return value
        return real(name)

    return version


def _raise(*_: Any, **__: Any) -> Any:
    raise RuntimeError("injected")


def _import_raising_runtimeerror_for(target: str) -> Any:
    """`importlib.import_module` that raises a NON-ImportError for `target` only.

    Architecture v2 section 3 says "`import pglast.ast` raises" -- any ordinary
    exception, not just ImportError (Codex finding 5ca50f5e; owner decision D-1a).
    """

    real = importlib.import_module

    def import_module(name: str, package: str | None = None) -> Any:
        if name == target:
            raise RuntimeError("injected")
        return real(name, package)

    return import_module


_LOADER_FAULTS = {
    # id: (installer, expected code)
    "a-import-pglast": (
        lambda mp: mp.setitem(sys.modules, "pglast", None),
        PARSER_UNAVAILABLE,
    ),
    "b-no-distribution": (
        lambda mp: mp.setattr(importlib.metadata, "version", _fake_version(None)),
        PARSER_UNAVAILABLE,
    ),
    "c-version-7.16": (
        lambda mp: mp.setattr(importlib.metadata, "version", _fake_version("7.16")),
        PARSER_VERSION_MISMATCH,
    ),
    "d-version-7.17.1": (
        lambda mp: mp.setattr(importlib.metadata, "version", _fake_version("7.17.1")),
        PARSER_VERSION_MISMATCH,
    ),
    "e-grammar-raises": (
        lambda mp: mp.setattr(pglast, "get_postgresql_version", _raise),
        PARSER_VERSION_MISMATCH,
    ),
    "f-grammar-16": (
        lambda mp: mp.setattr(pglast, "get_postgresql_version", lambda: (16, 9)),
        PARSER_VERSION_MISMATCH,
    ),
    "g-grammar-not-tuple": (
        lambda mp: mp.setattr(pglast, "get_postgresql_version", lambda: [17, 7]),
        PARSER_VERSION_MISMATCH,
    ),
    "h-grammar-str-major": (
        lambda mp: mp.setattr(pglast, "get_postgresql_version", lambda: ("17", 7)),
        PARSER_VERSION_MISMATCH,
    ),
    "i-import-ast": (
        lambda mp: mp.setitem(sys.modules, "pglast.ast", None),
        PARSER_UNAVAILABLE,
    ),
    "j-createstmt-missing": (
        lambda mp: mp.delattr(pg_ast, "CreateStmt"),
        PARSER_UNAVAILABLE,
    ),
    "k-dostmt-not-a-class": (
        lambda mp: mp.setattr(pg_ast, "DoStmt", object()),
        PARSER_UNAVAILABLE,
    ),
    "l-import-ast-raises-runtimeerror": (
        lambda mp: mp.setattr(
            importlib, "import_module", _import_raising_runtimeerror_for("pglast.ast")
        ),
        PARSER_UNAVAILABLE,
    ),
}


@pytest.mark.parametrize("fault", sorted(_LOADER_FAULTS))
def test_u17_every_loader_fault_fails_closed_with_the_environment_code(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """U-17. One row per architecture v2 section 3 condition (CL-1).

    The unit and module are obtained BEFORE the fault is installed, so the fault
    reaches the loader and not the test's own imports.
    """

    module = _module()
    unit = _role_unit()
    install, expected = _LOADER_FAULTS[fault]
    parse = _ParseSpy(monkeypatch)
    install(monkeypatch)

    with pytest.raises(MigrationUnitRejected) as caught:
        module.check_rendered(unit=unit, rendered=CREATE_ONE)

    assert caught.value.reason_code == expected
    assert parse.calls == [], "no partial parse after a loader fault"


# ---------------------------------------------------------------------------
# U-18  CG4-R5: the two codes.
# ---------------------------------------------------------------------------


def test_u18_the_two_content_codes_are_precondition_codes() -> None:
    """U-18. Members of PreconditionCode with exactly these values; not ledger codes."""

    assert PreconditionCode.ORDINARY_ROLE_CONTENT_PROHIBITED.value == PROHIBITED
    assert PreconditionCode.ORDINARY_ROLE_CONTENT_UNPARSEABLE.value == UNPARSEABLE
    ledger = {code.value for code in SanitizedErrorCode}
    assert PROHIBITED not in ledger
    assert UNPARSEABLE not in ledger


# ---------------------------------------------------------------------------
# U-19  v5 section 3: the known admissions, pinned so narrowing is deliberate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rendered",
    [
        "CREATE TEMP TABLE cg4_tmp (id int);",
        f"CREATE UNLOGGED TABLE {SCHEMA}.u (id int);",
        f"CREATE TABLE IF NOT EXISTS {SCHEMA}.i (id int);",
        f"CREATE TABLE {SCHEMA}.c PARTITION OF {SCHEMA}.p FOR VALUES IN (1);",
    ],
    ids=["temp", "unlogged", "if-not-exists", "partition-of"],
)
def test_u19_known_create_stmt_variants_are_admitted(rendered: str) -> None:
    """U-19. These parse as CreateStmt and v5 admits them; recorded, not endorsed."""

    assert _module().check_rendered(unit=_role_unit(), rendered=rendered) is rendered
