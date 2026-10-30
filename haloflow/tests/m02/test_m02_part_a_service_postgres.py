"""UC1 part A tranche 2b — EventFoundation service layer (G2), PostgreSQL 17.

Rows: S-01 (standalone record_observation), PA-02 / PA-06d (callback domain
exception propagates unchanged), PA-06e (callback-provoked database error
propagates unchanged), PA-03 (NestedTenantTransaction unchanged), PA-06a/b
(AppendCommitUnknown on commit-phase faults, conservative), PA-06c (AppendFailed
sanitized), MK (body-complete marker: post-body path-check failure is unknown,
not definite), EX (existing branch does not invoke the callback), PA-50 (binding
used after its transaction ended), BND (binding cannot be constructed or
re-paired by a caller), PRD (caller cannot supply or override producer_service;
stored producer is the EventFoundation route's), ATT (attribution derived from
the context only), PA-30 (inactive tenant refused by M01 before any SQL),
PA-18 (log canary).

Frozen interface (`haloflow.m02.observation`), G2 decision v1 (e4c4915c) as
amended here for the handle/context binding (Codex ee98edee):
- `Link(link_type: str, link_id: UUID, relationship: str)`
- `Observation(event_id, event_type, contract_version, correlation_id,
  producer_dedupe_key, links, metadata, patient_id=None, causation_event_id=None,
  occurred_at=None, source_time_quality="absent")` -- frozen dataclass; no
  producer or attribution field.
- `AppendResult(event_id: UUID, appended_or_existing: str, handoff_state: str)`
- `AppendFailed(reason_code)`, `AppendCommitUnknown(event_id)`
- `EventFoundation(gateway, *, producer_service: str)`
  - `record_observation(context, observation, apply_state_change)`
  - `run_in_transaction(context, body)` -- the ONE gateway boundary for composed
    flows (M05 part B); `body(binding)` receives a `TransactionBinding` pairing
    the handle with the context it was opened for.
  - `append_observation_in(binding, observation)` -- no separate context
    argument, so handle and context cannot be mismatched by the caller.
- `TransactionBinding` cannot be constructed outside EventFoundation.
- Statement key and capability: `m02.observation.append` (WRITE).

The commit-phase seams are TEST doubles of the M01 pool/gateway (private
`_connection_for_gateway` / `_verify_effective_path`); M01 code is not modified.
Each row imports `haloflow.m02.observation` in its body (class S at baseline).
"""

import importlib
import inspect
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import timedelta
from types import ModuleType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
from psycopg import AsyncConnection, sql

pytestmark = pytest.mark.postgres

APPEND = "m02.observation.append"


def _obs_module() -> Any:
    return importlib.import_module("haloflow.m02.observation")


def _factory(conninfo: str) -> Callable[[], Awaitable[AsyncConnection[Any]]]:
    async def _connect() -> AsyncConnection[Any]:
        return await AsyncConnection.connect(conninfo, autocommit=True)

    return _connect


@asynccontextmanager
async def _runtime(m02_ids: Any, pool_class: Any = None) -> AsyncIterator[Any]:
    from haloflow.composition import build_production_tenant_runtime
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies

    cls = pool_class or TenantPool
    pool = cls(m02_ids.logins["haloflow_runtime"], min_size=1, max_size=1)
    await pool.open()
    try:
        yield build_production_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=_factory(m02_ids.logins["haloflow_migrator"]),
                provisioner_connect=_factory(m02_ids.logins["haloflow_provisioner"]),
            )
        )
    finally:
        await pool.close()


async def _context(runtime: Any, tenant_id: str, principal_id: str) -> Any:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource

    return await runtime.resolver.resolve(
        principal=Principal(
            kind=PrincipalKind.WORKLOAD,
            id=principal_id,
            auth_method="test",
            authorized_tenant_ids=frozenset({tenant_id}),
            capabilities=frozenset({APPEND}),
        ),
        tenant_hint=tenant_id,
        purpose="operations",
        capabilities=frozenset({APPEND}),
        source=TrustedSource.WORKER,
        execution_id=uuid5(NAMESPACE_URL, f"haloflow-test:pa-svc:{uuid.uuid4()}"),
        correlation_id=uuid.uuid4(),
        correlation_source=CorrelationSource.TRUSTED_INFRASTRUCTURE,
    )


def _ready(part_a: ModuleType, m02_ids: Any, schema: str) -> tuple[str, str]:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        assert part_a.regclass_exists(conn, f"{schema}.patient_events"), (
            f"{schema}.patient_events is not installed"
        )
        if not part_a.regclass_exists(conn, f"{schema}.m02_test_link_targets"):
            conn.execute(part_a.TEST_PROFILE_SQL.format(schema=schema))
        subject, source = str(uuid.uuid4()), str(uuid.uuid4())
        for row in (part_a.subject_target(subject), part_a.source_target(source)):
            conn.execute(
                sql.SQL("INSERT INTO {}.m02_test_link_targets VALUES (%s,%s,%s,%s,%s)").format(
                    sql.Identifier(schema)
                ),
                row,
            )
    return subject, source


def _observation(obs: Any, part_a: ModuleType, subject: str, source: str, **kw: Any) -> Any:
    values: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "event_type": part_a.OBSERVATION_TYPE,
        "contract_version": 1,
        "correlation_id": uuid.uuid4(),
        "producer_dedupe_key": f"svc:{uuid.uuid4().hex}",
        "links": (
            obs.Link(part_a.SUBJECT_RESOLVER, UUID(subject), "subject"),
            obs.Link(part_a.SOURCE_RESOLVER, UUID(source), "source"),
        ),
        "metadata": {"label_code": "reviewed"},
    }
    values.update(kw)
    return obs.Observation(**values)


def _counts(part_a: ModuleType, m02_ids: Any, schema: str) -> tuple[int, ...]:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        return tuple(
            int(
                conn.execute(  # type: ignore[index]
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(t)
                    )
                ).fetchone()[0]
            )
            for t in part_a.COUNTED_TABLES
        )


def _delta(after: tuple[int, ...], before: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a - b for a, b in zip(after, before, strict=True))


@pytest.fixture
def tenant(production_tenant: tuple[str, str]) -> tuple[str, str]:
    return production_tenant


# --- S-01 / EX / ATT / PRD: standalone wrapper ----------------------------------------


async def test_s01_record_observation_appends_and_runs_callback_once(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    principal = str(uuid.uuid4())
    calls: list[Any] = []

    async def apply(handle: Any, result: Any) -> None:
        calls.append((handle, result))

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], principal)
        observation = _observation(obs, part_a, subject, source)
        result = await foundation.record_observation(context, observation, apply)
    assert result.event_id == observation.event_id
    assert result.appended_or_existing == "appended"
    assert len(calls) == 1 and calls[0][1] == result
    assert _delta(_counts(part_a, m02_ids, tenant[1]), before) == (1, 2, 1)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            sql.SQL(
                "SELECT producer_service, workload_id::text, actor_id FROM {}.patient_events"
                " WHERE event_id = %s"
            ).format(sql.Identifier(tenant[1])),
            (observation.event_id,),
        ).fetchone()
    assert row == (part_a.TEST_PRODUCER, principal, None)  # PRD + ATT


async def test_ex_existing_branch_does_not_invoke_callback(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    calls: list[Any] = []

    async def apply(handle: Any, result: Any) -> None:
        calls.append(result)

    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        observation = _observation(obs, part_a, subject, source)
        await foundation.record_observation(context, observation, apply)
        again = await foundation.record_observation(context, observation, apply)
    assert again.appended_or_existing == "existing"
    assert len(calls) == 1


def test_prd_observation_has_no_producer_or_attribution_field() -> None:
    obs = _obs_module()
    names = {f.name for f in fields(obs.Observation)}
    assert names.isdisjoint(
        {
            "producer_service",
            "producer",
            "attribution",
            "actor_id",
            "workload_id",
            "principal_id",
            "tenant_id",
        }
    )
    params = inspect.signature(obs.EventFoundation.record_observation).parameters
    assert list(params) == ["self", "context", "observation", "apply_state_change"]
    params = inspect.signature(obs.EventFoundation.append_observation_in).parameters
    assert list(params) == ["self", "binding", "observation"]


# --- PA-02 / PA-06d / PA-06e / PA-03: callback behaviour -----------------------------------


class _DomainError(Exception):
    pass


async def test_pa02_pa06d_callback_exception_propagates_unchanged_and_rolls_back(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    raised = _DomainError("domain")

    async def apply(handle: Any, result: Any) -> None:
        raise raised

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(_DomainError) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert caught.value is raised
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_pa06e_callback_database_error_propagates_unchanged(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()

    async def apply(handle: Any, result: Any) -> None:
        # The callback itself provokes a database error through the handle.
        await handle.fetch_one(APPEND, {"command": "not-json"})

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(Exception) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert not isinstance(caught.value, obs.AppendFailed | obs.AppendCommitUnknown)
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_pa03_nested_gateway_in_callback_unchanged_type(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    from haloflow.m01.errors import NestedTenantTransaction

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))

        async def apply(handle: Any, result: Any) -> None:
            await runtime.gateway.with_tenant_transaction(context, _noop)

        with pytest.raises(NestedTenantTransaction):
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def _noop(handle: Any) -> None:
    return None


# --- PA-06a / PA-06b / MK: commit-phase classification -----------------------------------


def _faulting_pool(mode: str) -> Any:
    """A TenantPool double whose gateway connection's transaction exit faults.

    mode "commit_then_raise": the real COMMIT runs, then OperationalError.
    mode "raise_no_commit":   the transaction is rolled back, then OperationalError.
    Applies to the first transaction only.
    """

    from haloflow.m01.pool import TenantPool

    class _Pool(TenantPool):
        faulted = False

        @asynccontextmanager
        async def _connection_for_gateway(self) -> AsyncIterator[Any]:
            async with super()._connection_for_gateway() as connection:
                if type(self).faulted:
                    yield connection
                    return
                type(self).faulted = True
                real = connection.transaction

                @asynccontextmanager
                async def transaction(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                    if mode == "commit_then_raise":
                        async with real(*args, **kwargs) as tx:
                            yield tx
                        raise psycopg.OperationalError("simulated loss after COMMIT")
                    try:
                        async with real(*args, **kwargs) as tx:
                            yield tx
                            raise _Rollback()
                    except _Rollback:
                        pass
                    raise psycopg.OperationalError("simulated loss before COMMIT")

                connection.transaction = transaction  # type: ignore[method-assign]
                yield connection

    return _Pool


class _Rollback(Exception):
    pass


@pytest.mark.parametrize(
    ("mode", "retry_outcome", "committed"),
    [("commit_then_raise", "existing", 1), ("raise_no_commit", "appended", 0)],
)
async def test_pa06ab_commit_phase_fault_is_unknown_then_retry_converges(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    mode: str,
    retry_outcome: str,
    committed: int,
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    principal = str(uuid.uuid4())
    observation = None
    before = _counts(part_a, m02_ids, tenant[1])

    async def apply(handle: Any, result: Any) -> None:
        return None

    async with _runtime(m02_ids, _faulting_pool(mode)) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], principal)
        observation = _observation(obs, part_a, subject, source)
        with pytest.raises(obs.AppendCommitUnknown) as caught:
            await foundation.record_observation(context, observation, apply)
        assert caught.value.event_id == observation.event_id
        assert caught.value.__cause__ is None
        assert _delta(_counts(part_a, m02_ids, tenant[1]), before)[0] == committed
        retry = await foundation.record_observation(context, observation, apply)
    assert retry.appended_or_existing == retry_outcome
    assert retry.event_id == observation.event_id
    assert _delta(_counts(part_a, m02_ids, tenant[1]), before) == (1, 2, 1)


async def test_mk_post_body_path_check_failure_is_unknown_not_definite(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is set after the callback returns, before the gateway exits: a
    failure in M01's post-body path check is classified unknown (conservative)."""

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    callback_done: list[bool] = []

    async def apply(handle: Any, result: Any) -> None:
        callback_done.append(True)

    async with _runtime(m02_ids) as runtime:
        gateway = runtime.gateway
        original = gateway._verify_effective_path
        calls = {"n": 0}

        async def verify(connection: Any, schema_key: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the post-body check
                raise RuntimeError("simulated post-body path check failure")
            await original(connection, schema_key)

        monkeypatch.setattr(gateway, "_verify_effective_path", verify)
        foundation = obs.EventFoundation(gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        observation = _observation(obs, part_a, subject, source)
        with pytest.raises(obs.AppendCommitUnknown):
            await foundation.record_observation(context, observation, apply)
    assert callback_done == [True]
    assert _counts(part_a, m02_ids, tenant[1]) == before  # rolled back; reported unknown


async def test_pa06c_append_failure_is_definite_and_sanitized(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])

    async def apply(handle: Any, result: Any) -> None:
        raise AssertionError("callback must not run after a failed append")

    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        bad = _observation(obs, part_a, subject, source, event_type="m02_test_unknown")
        with pytest.raises(obs.AppendFailed) as caught:
            await foundation.record_observation(context, bad, apply)
    error = caught.value
    assert error.reason_code == "CONTRACT_UNSUPPORTED"
    assert error.__cause__ is None
    assert error.__suppress_context__ or error.__context__ is None
    import traceback

    rendered = "".join(traceback.format_exception(error))
    for leak in ("psycopg", "P0001", "SELECT", "m02_append_observation("):
        assert leak not in rendered, leak
    assert _counts(part_a, m02_ids, tenant[1]) == before


# --- PA-50 / BND: binding ------------------------------------------------------------------


async def test_pa50_binding_used_after_its_transaction_ended(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    kept: list[Any] = []

    async def body(binding: Any) -> None:
        kept.append(binding)

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        await foundation.run_in_transaction(context, body)
        with pytest.raises(Exception) as caught:
            await foundation.append_observation_in(
                kept[0], _observation(obs, part_a, subject, source)
            )
    assert not isinstance(caught.value, obs.AppendCommitUnknown)
    assert _counts(part_a, m02_ids, tenant[1]) == before


def test_bnd_binding_cannot_be_constructed_by_caller() -> None:
    obs = _obs_module()
    with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable; success is not
        obs.TransactionBinding(object(), object())


async def test_bnd_run_in_transaction_composes_append_with_caller_work(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    """One gateway boundary: the composed body appends and then fails; nothing commits."""

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])

    async def body(binding: Any) -> None:
        result = await foundation.append_observation_in(
            binding, _observation(obs, part_a, subject, source)
        )
        assert result.appended_or_existing == "appended"
        raise _DomainError("after append")

    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(_DomainError):
            await foundation.run_in_transaction(context, body)
    assert _counts(part_a, m02_ids, tenant[1]) == before


# --- PA-30: inactive tenant refused by M01 before SQL ---------------------------------------


async def test_pa30_suspended_tenant_refused_before_any_sql(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    from haloflow.m01.errors import TenantUnavailable

    _ready(part_a, m02_ids, tenant[1])
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        original = conn.execute(
            "SELECT lifecycle_state FROM shared.tenants WHERE tenant_id = %s", (tenant[0],)
        ).fetchone()[0]  # type: ignore[index]
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "UPDATE shared.tenants SET lifecycle_state = 'suspended' WHERE tenant_id = %s",
            (tenant[0],),
        )
    try:
        async with _runtime(m02_ids) as runtime:
            with pytest.raises(TenantUnavailable):
                await _context(runtime, tenant[0], str(uuid.uuid4()))
    finally:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute("SET session_replication_role = replica")
            conn.execute(
                "UPDATE shared.tenants SET lifecycle_state = %s WHERE tenant_id = %s",
                (original, tenant[0]),
            )


# --- PA-18: log canary ------------------------------------------------------------------------


async def test_pa18_no_metadata_or_driver_text_in_logs(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    caplog.set_level(logging.DEBUG)

    async def apply(handle: Any, result: Any) -> None:
        return None

    async with _runtime(m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(runtime, tenant[0], str(uuid.uuid4()))
        canary = _observation(obs, part_a, subject, source, metadata={"label_code": "unreviewed"})
        await foundation.record_observation(context, canary, apply)
        with pytest.raises(obs.AppendFailed):
            await foundation.record_observation(
                context,
                _observation(obs, part_a, subject, source, event_type="m02_test_unknown"),
                apply,
            )
    text = "\n".join(r.getMessage() for r in caplog.records)
    for leak in ("unreviewed", "label_code", str(subject), str(source), "P0001"):
        assert leak not in text, leak


# timedelta kept for parity with the M01 context TTL helpers used elsewhere.
_ = timedelta
