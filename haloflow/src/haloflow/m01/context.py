"""Immutable, resolver-issued tenant authorization context."""

from collections.abc import Callable
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from haloflow.m01.errors import ContextExpired, ContextInvalid

Clock = Callable[[], datetime]


class PrincipalKind(StrEnum):
    ACTOR = "actor"
    WORKLOAD = "workload"


class TrustedSource(StrEnum):
    HTTP = "http"
    WORKER = "worker"
    INTERNAL_SERVICE = "internal_service"


@dataclass(frozen=True, slots=True)
class Principal:
    """Trusted identity-policy output consumed by TenantResolver.

    The identity adapter, which remains an M01 open integration item, is
    responsible for constructing this object from authenticated claims.
    """

    kind: PrincipalKind
    id: str
    auth_method: str
    authorized_tenant_ids: frozenset[str]
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if not self.id or not self.auth_method:
            raise ContextInvalid(reason_code="PRINCIPAL_INVALID")


_CONTEXT_ISSUER = object()


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Short-lived security object that callers cannot directly construct."""

    tenant_id: str
    schema_key: str = field(repr=False)
    principal: Principal
    capabilities: frozenset[str]
    purpose: str
    operation_id: str
    request_id: str | None
    source: TrustedSource
    issued_at: datetime
    expires_at: datetime
    _issuer: InitVar[object | None] = None

    def __post_init__(self, _issuer: object | None) -> None:
        if _issuer is not _CONTEXT_ISSUER:
            raise ContextInvalid(reason_code="UNTRUSTED_CONTEXT_CONSTRUCTION")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ContextInvalid(reason_code="CONTEXT_TIMEZONE_REQUIRED")
        if not self.tenant_id or not self.schema_key or not self.operation_id:
            raise ContextInvalid(reason_code="CONTEXT_FIELD_MISSING")

    def assert_usable(self, *, clock: Clock | None = None) -> None:
        now = (clock or _utc_now)()
        if now >= self.expires_at:
            raise ContextExpired()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def issue_tenant_context(
    *,
    tenant_id: str,
    schema_key: str,
    principal: Principal,
    capabilities: frozenset[str],
    purpose: str,
    operation_id: str,
    request_id: str | None,
    source: TrustedSource,
    issued_at: datetime,
    expires_at: datetime,
) -> TenantContext:
    """Issue a context after resolver policy and registry validation.

    This function is internal to M01. Repository ownership checks prevent
    imports from application modules.
    """

    return TenantContext(
        tenant_id=tenant_id,
        schema_key=schema_key,
        principal=principal,
        capabilities=capabilities,
        purpose=purpose,
        operation_id=operation_id,
        request_id=request_id,
        source=source,
        issued_at=issued_at,
        expires_at=expires_at,
        _issuer=_CONTEXT_ISSUER,
    )
