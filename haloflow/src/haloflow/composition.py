"""The single production composition root for the tenant statement catalogue.

ADR-011 D-11.18 (B2). Statements are composed exactly once, at startup, from
approved module definition sets. This module is the only place in production
code permitted to call ``build_statement_catalog``; repository-control tests
enforce that, and the statement-catalogue manifest pins whatever this function
produces. Without a single composition path the manifest would pin a constant
rather than the catalogue the application actually runs.
"""

from haloflow.m01.statements import (
    M01_STATEMENTS,
    CompiledCatalog,
    StatementDefinitions,
    build_statement_catalog,
)

# Approved module definition sets, in composition order. A new module is added
# here and nowhere else, and doing so forces a manifest update in the same
# commit because the manifest test composes through this tuple.
APPROVED_MODULE_STATEMENTS: tuple[StatementDefinitions, ...] = (M01_STATEMENTS,)


def build_production_catalog() -> CompiledCatalog:
    """Compose the production statement catalogue. Startup-only."""

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS)
