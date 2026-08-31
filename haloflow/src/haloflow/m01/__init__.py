"""M01 tenant context and tenant-aware data-access boundary."""

from haloflow.m01.context import (
    CorrelationSource,
    Principal,
    TenantContext,
    TrustedSource,
)
from haloflow.m01.resolver import TenantResolver
from haloflow.m01.statements import CompiledCatalog

# `build_statement_catalog` is deliberately NOT re-exported here. Composition is
# restricted to the production composition root (B2.11); a convenience re-export
# on the package would be a second path to it, and the statement-catalogue
# manifest can only be a review gate if there is exactly one.
__all__ = [
    "CompiledCatalog",
    "CorrelationSource",
    "Principal",
    "TenantContext",
    "TenantResolver",
    "TrustedSource",
]
