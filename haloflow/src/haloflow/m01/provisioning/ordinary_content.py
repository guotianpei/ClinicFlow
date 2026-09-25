"""The content check for role-bearing ordinary migration units (CG-4, B-legacy-bypass).

WHAT THIS MODULE IS FOR
-----------------------
An ordinary unit (not typed) may declare an approved `execution_role`, and the
runner then executes its template under `SET LOCAL ROLE <execution_role>`. Nothing
checked what that template contained, so a role-bearing ordinary unit could
define a function -- or run a `DO` block -- under a module role without the CP1
function policy. This module closes that route (requirements v5, CG4-R0 to R9).

The check applies ONLY to role-bearing ordinary units (CG4-R0). Typed units go
through the frozen CP1 policy; migrator-owned ordinary units are never parsed
(CG4-R6/R8, and OD-04's rejector depends on that).

THE RULE (CG4-R1, R1a, R1b)
---------------------------
A rendered template is admitted only if it parses to at least one top-level
statement and EVERY top-level statement is exactly `pglast.ast.CreateStmt`.
`CreateFunctionStmt` (FUNCTION, PROCEDURE, OR REPLACE) and `DoStmt` are checked
FIRST and refused whatever the allowed list says. Types are matched exactly
(`is`), never with `isinstance`.

ORDER (architecture v2 section 4)
---------------------------------
0. scope guard (internal misuse -> the CL-2 exception, below);
1. NUL byte -> unparseable. Load-bearing: pglast stops reading at a NUL, so
   `CREATE TABLE ...;<NUL> DROP ...` would otherwise parse as one CreateStmt;
2. load and pin the parser (pglast 7.17, PostgreSQL grammar 17);
3. exactly ONE `parse_sql` call (CG4-R9);
4. zero statements -> prohibited (CG4-R1b);
5. per statement: RawStmt required, prohibited set first, then the allowed list.

CODES
-----
* `ORDINARY_ROLE_CONTENT_PROHIBITED` -- parsed, but not admissible.
* `ORDINARY_ROLE_CONTENT_UNPARSEABLE` -- could not be parsed (including a NUL).
* CL-1: a parser that is missing or at the wrong version is an ENVIRONMENT fault,
  reported with the existing `INSTALL_PARSER_UNAVAILABLE` /
  `INSTALL_PARSER_VERSION_MISMATCH`, not a content code.
* CL-2: the step-0 misuse guard raises `ORDINARY_ROLE_CONTENT_PROHIBITED` as an
  explicitly documented exception to that code's content meaning -- no prohibited
  content has been established there. The runner never reaches it.

LIMITS (requirements v5 section 3)
----------------------------------
Static and top-level only. Behaviour an allowed `CREATE TABLE` could trigger at
execution time (defaults, event triggers, functions invoked by DDL) is not
detected. Nothing here constrains migrator-owned units.

IMPLEMENTATION COMMITMENTS THE FROZEN TESTS RELY ON
---------------------------------------------------
Every external name is looked up at CALL time, as a module attribute:
`importlib.import_module("pglast")` / `("pglast.ast")`, `importlib.metadata.version`,
`pglast.get_postgresql_version` and `pglast.parse_sql`; and the lists are read from
this module's globals when `check_rendered` runs.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import TYPE_CHECKING, Any, Final, cast

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning.codes import PreconditionCode

if TYPE_CHECKING:
    from haloflow.m01.provisioning.units import TenantMigrationUnit

__all__ = [
    "ALLOWED_TOPLEVEL_KINDS",
    "PROHIBITED_TOPLEVEL_KINDS",
    "check_rendered",
    "requires_content_check",
]

ALLOWED_TOPLEVEL_KINDS: Final = ("CreateStmt",)
"""CG4-R1a, as ruled on RQ-E: exactly one statement kind."""

PROHIBITED_TOPLEVEL_KINDS: Final = ("CreateFunctionStmt", "DoStmt")
"""CG4-R1: refused whatever the allowed list says."""

_PINNED_PGLAST_VERSION: Final = "7.17"
_PINNED_GRAMMAR_MAJOR: Final = 17


def _reject(code: PreconditionCode) -> MigrationUnitRejected:
    return MigrationUnitRejected(reason_code=code.value)


def requires_content_check(unit: TenantMigrationUnit) -> bool:
    """CG4-R0. The ONLY definition of scope: not typed, and role-bearing."""

    return (not unit.is_typed) and unit.execution_role is not None


def _load_parser() -> tuple[Any, dict[str, type]]:
    """Import and pin the parser, and resolve the classes the rule names (CL-1)."""

    try:
        parser = importlib.import_module("pglast")
    except ImportError:
        raise _reject(PreconditionCode.INSTALL_PARSER_UNAVAILABLE) from None
    try:
        version = importlib.metadata.version("pglast")
    except importlib.metadata.PackageNotFoundError:
        raise _reject(PreconditionCode.INSTALL_PARSER_UNAVAILABLE) from None
    if version != _PINNED_PGLAST_VERSION:
        raise _reject(PreconditionCode.INSTALL_PARSER_VERSION_MISMATCH)
    try:
        grammar = parser.get_postgresql_version()
    except Exception:
        raise _reject(PreconditionCode.INSTALL_PARSER_VERSION_MISMATCH) from None
    if (
        type(grammar) is not tuple
        or not grammar
        or type(grammar[0]) is not int
        or grammar[0] != _PINNED_GRAMMAR_MAJOR
    ):
        raise _reject(PreconditionCode.INSTALL_PARSER_VERSION_MISMATCH)
    try:
        ast = importlib.import_module("pglast.ast")
    except Exception:
        # Architecture v2 section 3: ANY failure importing pglast.ast (not only
        # ImportError; e.g. a RuntimeError from module initialisation) is an
        # environment fault. BaseException (KeyboardInterrupt, SystemExit) is not
        # caught. The top-level `import pglast` row above stays ImportError-only.
        raise _reject(PreconditionCode.INSTALL_PARSER_UNAVAILABLE) from None

    classes: dict[str, type] = {}
    for name in ("RawStmt", *ALLOWED_TOPLEVEL_KINDS, *PROHIBITED_TOPLEVEL_KINDS):
        resolved = getattr(ast, name, None)
        if not isinstance(resolved, type):
            raise _reject(PreconditionCode.INSTALL_PARSER_UNAVAILABLE)
        classes[name] = resolved
    return parser, classes


def check_rendered(*, unit: TenantMigrationUnit, rendered: str) -> str:
    """Admit or refuse one role-bearing ordinary unit's RENDERED template.

    Returns `rendered` itself -- the same object -- which is the only text the
    runner may execute for this unit. Raises `MigrationUnitRejected`.
    """

    if not requires_content_check(unit):
        # CL-2: internal misuse. Fail closed; never parse a migrator-owned unit.
        raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_PROHIBITED)
    if "\x00" in rendered:
        raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_UNPARSEABLE)

    parser, classes = _load_parser()
    try:
        nodes = parser.parse_sql(rendered)
    except Exception:
        # Fail closed: whatever stopped the parser, the template is not admitted.
        raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_UNPARSEABLE) from None
    if type(nodes) is not tuple:
        raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_UNPARSEABLE)
    if not nodes:
        raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_PROHIBITED)

    raw_class = classes["RawStmt"]
    prohibited = tuple(classes[name] for name in PROHIBITED_TOPLEVEL_KINDS)
    allowed = tuple(classes[name] for name in ALLOWED_TOPLEVEL_KINDS)
    for raw in nodes:
        if type(raw) is not raw_class:
            raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_UNPARSEABLE)
        kind = type(cast(Any, raw).stmt)
        if any(kind is cls for cls in prohibited):
            raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_PROHIBITED)
        if not any(kind is cls for cls in allowed):
            raise _reject(PreconditionCode.ORDINARY_ROLE_CONTENT_PROHIBITED)
    return rendered
