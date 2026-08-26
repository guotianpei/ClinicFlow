"""Fail-closed tenant resolution against the shared control registry."""

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from haloflow.m01.context import (
    Principal,
    TenantContext,
    TrustedSource,
    issue_tenant_context,
)
from haloflow.m01.errors import TenantDenied, TenantUnavailable

SCHEMA_KEY_PATTERN = re.compile(r"^tenant_[a-z0-9]{8,32}$")
TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
PURPOSE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{1,63}$")
DEFAULT_PURPOSES = frozenset({"treatment", "payment", "operations"})


class LifecycleState(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVAL_PENDING = "archival_pending"
    ARCHIVED = "archived"
    DECOMMISSIONED = "decommissioned"


@dataclass(frozen=True, slots=True)
class TenantRegistryRecord:
    tenant_id: str
    schema_key: str
    lifecycle_state: LifecycleState
    schema_version: int


class ControlStore(Protocol):
    async def get_tenant(self, tenant_id: str) -> TenantRegistryRecord | None: ...


class TenantResolver:
    """Creates the only trusted TenantContext used by runtime gateways."""

    def __init__(
        self,
        control_store: ControlStore,
        *,
        supported_schema_versions: Collection[int],
        allowed_purposes: Collection[str] = DEFAULT_PURPOSES,
        context_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._control_store = control_store
        self._supported_schema_versions = frozenset(supported_schema_versions)
        self._allowed_purposes = frozenset(allowed_purposes)
        self._context_ttl = context_ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    async def resolve(
        self,
        *,
        principal: Principal,
        tenant_hint: str,
        purpose: str,
        capability: str,
        source: TrustedSource,
        operation_id: str,
        request_id: str | None = None,
    ) -> TenantContext:
        self._authorize_request_shape(
            principal=principal,
            tenant_hint=tenant_hint,
            purpose=purpose,
            capability=capability,
            operation_id=operation_id,
        )

        record = await self._control_store.get_tenant(tenant_hint)
        if record is None:
            raise TenantDenied(reason_code="TENANT_NOT_AUTHORIZED")
        if record.tenant_id != tenant_hint:
            raise TenantUnavailable(reason_code="REGISTRY_TENANT_MISMATCH")
        if not SCHEMA_KEY_PATTERN.fullmatch(record.schema_key):
            raise TenantUnavailable(reason_code="SCHEMA_KEY_INVALID")
        if record.lifecycle_state is not LifecycleState.ACTIVE:
            raise TenantUnavailable(reason_code="TENANT_NOT_ACTIVE")
        if record.schema_version not in self._supported_schema_versions:
            raise TenantUnavailable(reason_code="SCHEMA_VERSION_INCOMPATIBLE")

        issued_at = self._clock()
        return issue_tenant_context(
            tenant_id=record.tenant_id,
            schema_key=record.schema_key,
            principal=principal,
            capabilities=frozenset({capability}),
            purpose=purpose,
            operation_id=operation_id,
            request_id=request_id,
            source=source,
            issued_at=issued_at,
            expires_at=issued_at + self._context_ttl,
        )

    def _authorize_request_shape(
        self,
        *,
        principal: Principal,
        tenant_hint: str,
        purpose: str,
        capability: str,
        operation_id: str,
    ) -> None:
        if not TENANT_ID_PATTERN.fullmatch(tenant_hint):
            raise TenantDenied(reason_code="TENANT_HINT_INVALID")
        if tenant_hint not in principal.authorized_tenant_ids:
            raise TenantDenied(reason_code="TENANT_BINDING_MISMATCH")
        if capability not in principal.capabilities:
            raise TenantDenied(reason_code="CAPABILITY_DENIED")
        if purpose not in self._allowed_purposes or not PURPOSE_PATTERN.fullmatch(purpose):
            raise TenantDenied(reason_code="PURPOSE_DENIED")
        try:
            parsed_operation_id = UUID(operation_id)
        except (ValueError, AttributeError):
            raise TenantDenied(reason_code="OPERATION_ID_INVALID") from None
        if str(parsed_operation_id) != operation_id.lower():
            raise TenantDenied(reason_code="OPERATION_ID_INVALID")
