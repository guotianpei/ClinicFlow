"""B3b service test support (package v2 + v2r1, Claude/Codex aligned 2026-11-01).

Every PostgreSQL row reaches SQL only through the composed runtime and the real M01
gateway: the staff test registry (target 25), and a TEST statement catalogue =
production module statements + M05_STATEMENTS + STAFF_STATEMENTS. No direct SET ROLE.

`Recorder` wraps the real gateway to count route calls, trace statement keys and
parameters, and (test-only) substitute the rows a statement returns AFTER the real
statement ran, so corrupted-output rows exercise rollback of real writes.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final
from uuid import NAMESPACE_URL, uuid5

import staff_support as st
from psycopg import AsyncConnection

READ: Final = "m05.followup.read"
PREPARE: Final = "m05.followup.prepare"
APPLY: Final = "m05.followup.apply"
SUPERVISE: Final = "m05.followup.supervise"
CREATE: Final = "m05.followup.create"
STAFF_ADMIN: Final = "m01.staff.admin"
STAFF_DISPLAY: Final = "m01.staff.display"

STAFF_CAPS: Final = frozenset({READ, PREPARE, APPLY})
SUPERVISOR_CAPS: Final = STAFF_CAPS | {SUPERVISE}
READ_ONLY_CAPS: Final = frozenset({READ})

# Statement keys (m05/statements.py, m01/statements.py STAFF_STATEMENTS).
K_PREPARE: Final = "m05.followup.prepare"
K_APPLY: Final = "m05.followup.apply"
K_GET: Final = "m05.followup.get"
K_LIST: Final = "m05.followup.list"
K_HISTORY: Final = "m05.followup.history"
K_ADMIN: Final = "m01.staff.admin"
K_DISPLAY: Final = "m01.staff.display"
K_READER: Final = "m01.staff.followup_reader"

SERVICE_TARGET: Final = frozenset({st.STAFF_TEST_TARGET})


def service_catalog() -> Any:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import STAFF_STATEMENTS, build_statement_catalog
    from haloflow.m05.statements import M05_STATEMENTS

    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, M05_STATEMENTS, STAFF_STATEMENTS)


def _factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection[Any]]]:
    async def _connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


@asynccontextmanager
async def runtime(
    install: Any, pool_class: Any = None, login: str | None = None
) -> AsyncIterator[Any]:
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    # The caller installs first (in a worker thread from async rows: the sync install
    # path uses asyncio.run and cannot run inside the test's event loop).
    cls = pool_class or TenantPool
    pool = cls(login or install.ids.logins["haloflow_runtime"], min_size=1, max_size=1)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=_factory(install.ids.logins["haloflow_migrator"]),
                provisioner_connect=_factory(install.ids.logins["haloflow_provisioner"]),
            ),
            registry=st.staff_registry(),
            catalog=service_catalog(),
            supported_schema_versions=SERVICE_TARGET,
        )
    finally:
        await pool.close()


async def context(
    rt: Any,
    install: Any,
    principal_id: str,
    capabilities: frozenset[str],
    *,
    kind: str = "actor",
    auth_method: str = "test",
    purpose: str = "operations",
    request_id: str | None = None,
) -> Any:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource

    return await rt.resolver.resolve(
        principal=Principal(
            kind=PrincipalKind(kind),
            id=principal_id,
            auth_method=auth_method,
            authorized_tenant_ids=frozenset({install.tenant[0]}),
            capabilities=capabilities,
        ),
        tenant_hint=install.tenant[0],
        purpose=purpose,
        capabilities=capabilities,
        source=TrustedSource.HTTP,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:b3b:{uuid.uuid4()}"),
        correlation_id=uuid.uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
        request_id=request_id,
    )


class _Handle:
    def __init__(self, handle: Any, recorder: Recorder) -> None:
        self._handle = handle
        self._recorder = recorder

    def _note(self, key: str, params: Any) -> None:
        # Deep copies (red review 24d1cd1 #2): a service that mutates one shared
        # command object cannot rewrite the recorded history.
        self._recorder.statements.append(key)
        self._recorder.params.append((key, copy.deepcopy(_plain(params))))

    async def execute(self, key: str, params: Any = None) -> Any:
        raise AssertionError("B3b services read every statement with fetch_all")

    async def fetch_one(self, key: str, params: Any = None) -> Any:
        # Red review 24d1cd1 #3: fetch_one would hide extra rows from the validator.
        # The one exception is M02's own append (EventFoundation reads it with
        # fetch_one; that is accepted part-A code, not B3b/B4 service code).
        if key != "m02.observation.append":
            raise AssertionError("B3b services read every statement with fetch_all")
        self._note(key, params)
        await self._recorder._run_hook(self._recorder.before, key)
        row = await self._handle.fetch_one(key, params)
        self._recorder.results.append((key, copy.deepcopy([row] if row is not None else [])))
        await self._recorder._run_hook(self._recorder.after, key)
        return row

    async def fetch_all(self, key: str, params: Any = None) -> Any:
        self._note(key, params)
        await self._recorder._run_hook(self._recorder.before, key)
        rows = await self._handle.fetch_all(key, params)
        self._recorder.results.append((key, copy.deepcopy(list(rows))))
        await self._recorder._run_hook(self._recorder.after, key)
        n = self._recorder._calls_by_key.get(key, 0) + 1
        self._recorder._calls_by_key[key] = n
        if (key, n) in self._recorder.substitute_nth:
            return list(self._recorder.substitute_nth[(key, n)])
        if key in self._recorder.substitute:
            return list(self._recorder.substitute[key])
        return rows


def _plain(params: Any) -> Any:
    if isinstance(params, dict):
        return {k: getattr(v, "obj", v) for k, v in params.items()}
    return params


class Recorder:
    """Duck-typed gateway (the services use only `with_tenant_transaction`).

    - `calls`, `contexts`: every route-level gateway transaction and its context;
    - `statements`, `params` (deep copies), `results` (the REAL rows, before any
      substitution);
    - `substitute[key]`: rows returned instead, AFTER the real statement ran;
    - `before[key]` / `after[key]`: hooks (sync callables run in a worker thread, or
      coroutine functions); each fires once, then is removed; an exception from a
      hook propagates into the service exactly as a statement failure would.
    """

    def __init__(self, gateway: Any, substitute: dict[str, list[tuple[Any, ...]]] | None = None,
                 *, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None):
        self._gateway = gateway
        self.calls = 0
        self.callbacks = 0
        self.contexts: list[Any] = []
        self.statements: list[str] = []
        self.params: list[tuple[str, Any]] = []
        self.results: list[tuple[str, list[Any]]] = []
        self.substitute = dict(substitute or {})
        self.before = dict(before or {})
        self.after = dict(after or {})
        # B4: substitute only the n-th call (1-based) of a key, e.g. prepare #2.
        self.substitute_nth: dict[tuple[str, int], list[tuple[Any, ...]]] = {}
        self._calls_by_key: dict[str, int] = {}

    async def _run_hook(self, hooks: dict[str, Any], key: str) -> None:
        hook = hooks.pop(key, None)
        if hook is None:
            return
        if asyncio.iscoroutinefunction(hook):
            await hook()
        else:
            await asyncio.to_thread(hook)

    async def with_tenant_transaction(self, context: Any, callback: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.contexts.append(context)

        async def wrapped(handle: Any) -> Any:
            self.callbacks += 1
            return await callback(_Handle(handle, self))

        return await self._gateway.with_tenant_transaction(context, wrapped, **kwargs)

    def sent(self, key: str) -> list[Any]:
        """The JSON objects sent for `key` (the single named parameter), in order."""

        return [next(iter(p.values())) for k, p in self.params if k == key]

    def returned(self, key: str) -> list[list[Any]]:
        return [rows for k, rows in self.results if k == key]


# ---------------------------------------------------------------------------
# B4 (package v2 + v2r1; owner O-1 2026-11-01)
# ---------------------------------------------------------------------------

APPEND: Final = "m02.observation.append"
NOTE_READ: Final = "m05.followup.note.read"
RESOLVE_CAPS: Final = STAFF_CAPS | {APPEND}
SUPERVISOR_RESOLVE_CAPS: Final = SUPERVISOR_CAPS | {APPEND}
K_APPEND: Final = "m02.observation.append"
K_NOTE: Final = "m05.followup.note.read"
RESOLUTION_TYPE: Final = "m05_followup_resolution_asserted"
PRODUCER: Final = "m05_followup"


def resolution_dedupe_key(tenant: str, item_id: str, client_action_id: str) -> str:
    """MK-V1 (package v1r1): m05r1: + sha256(compact JSON) hex."""

    import hashlib
    import json

    preimage = json.dumps(["m05.resolve/v1", tenant, item_id, client_action_id],
                          separators=(",", ":"), ensure_ascii=False)
    return "m05r1:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


MK_V1: Final = (("pilot-clinic-01", "00000000-0000-4000-8000-000000000201",
                 "00000000-0000-4000-8000-000000000301"),
                "m05r1:c1d4792e14070a96dca1f76be434c5105cd5a5ccd81cbd0c57d88d3223621253")


class TestNoteKeys:
    """Test-only NoteKeyProvider: fixed keys, a current version, a retained set; any
    version can be retired, and the whole provider can be closed."""

    __test__ = False  # not a pytest class

    def __init__(self) -> None:
        self.hmac_keys = {1: bytes([0x11]) * 32}
        self.enc_keys = {1: bytes([0x22]) * 32}
        self.current = 1
        self.current_unavailable = False  # RP-12: only the CURRENT keys are gone
        self.all_unavailable = False  # KP-1: every key is gone
        self.uses: list[tuple[str, int | None]] = []

    def _unavailable(self) -> Exception:
        from haloflow.m05.notes import KeyUnavailable

        return KeyUnavailable()

    def rotate(self) -> int:
        self.current += 1
        self.hmac_keys[self.current] = bytes([0x30 + self.current]) * 32
        self.enc_keys[self.current] = bytes([0x40 + self.current]) * 32
        return self.current

    def retire(self, version: int) -> None:
        self.hmac_keys.pop(version, None)
        self.enc_keys.pop(version, None)

    def current_hmac(self) -> tuple[int, bytes]:
        self.uses.append(("current_hmac", None))
        if self.all_unavailable or self.current_unavailable or self.current not in self.hmac_keys:
            raise self._unavailable()
        return self.current, self.hmac_keys[self.current]

    def hmac_key(self, version: int) -> bytes:
        self.uses.append(("hmac_key", version))
        if self.all_unavailable or version not in self.hmac_keys:
            raise self._unavailable()
        return self.hmac_keys[version]

    def current_encryption(self) -> tuple[int, bytes]:
        self.uses.append(("current_encryption", None))
        if self.all_unavailable or self.current_unavailable or self.current not in self.enc_keys:
            raise self._unavailable()
        return self.current, self.enc_keys[self.current]

    def encryption_key(self, version: int) -> bytes:
        self.uses.append(("encryption_key", version))
        if self.all_unavailable or version not in self.enc_keys:
            raise self._unavailable()
        return self.enc_keys[version]


M02_TABLES: Final = ("patient_events", "patient_event_links", "event_handoff_outbox")


def full_state(install: Any) -> dict[str, Any]:
    """The B4 oracle: every directory and M05 row (st.state) plus every M02 event,
    link and outbox row, as sorted JSON text."""

    out: dict[str, Any] = dict(st.state(install))
    for table in M02_TABLES:
        out[table] = sorted(st.table_content(install, table))
    return out
