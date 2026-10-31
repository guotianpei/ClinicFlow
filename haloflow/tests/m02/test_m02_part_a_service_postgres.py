"""UC1 part A tranche 2b — EventFoundation service layer (G2), PostgreSQL 17.

Revised per Codex 1ee91aa6. Rows: S-01 (event + test-owned domain state commit
together), ATOM (domain write then exception: neither commits), EX (existing branch
skips callback), PRD/ATT (producer from the foundation route; attribution from the
bound context), PA-06e (the callback's own database exception object propagates),
PA-03 (NestedTenantTransaction unchanged), PA-06a/b (one-shot commit-phase seam,
restored; AppendCommitUnknown; retry converges on the real transaction manager),
MK (post-body path-check failure is unknown), MK-NEG (callback exception before the
marker is the callback's exception, never unknown), PA-06c (AppendFailed policy:
fixed message, no cause/context, no driver canary), PA-50 (binding after its
transaction: M01 RepositoryHandleExpired, zero append dispatch), BND (no caller
construction, no attribute reassignment, cross-foundation refusal, one-boundary
composition with a domain write rolls back), PA-30 (M01 refuses a suspended tenant;
zero append/callback dispatch -- the resolver's own control reads are not counted),
PA-18 (log canary), PA-05b (callback-omission mutant is detected by the domain
oracle).

Frozen interface (`haloflow.m02.observation`), amending G2 decision v1:
- `Link`, `Observation`, `AppendResult`, `AppendFailed(reason_code)`,
  `AppendCommitUnknown(event_id)`, `BindingForeign` (binding from another
  EventFoundation instance), `TransactionBinding` (frozen; not caller-constructible).
- `EventFoundation(gateway, *, producer_service)` with `record_observation(context,
  observation, apply_state_change)`, `run_in_transaction(context, body)`,
  `append_observation_in(binding, observation)`, and the mutation seam method
  `_apply(apply_state_change, handle, result)` (part_a.CALLBACK_SEAM).

The gateway is composed with the PRODUCTION migration registry and a TEST statement
catalogue = production module statements + the test-owned domain statement
(`m02_test.` prefix). Commit-phase seams are test doubles; M01 code is not modified.
"""

import importlib
import inspect
import logging
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, fields
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


def _test_catalog(part_a: ModuleType) -> Any:
    from haloflow.composition import APPROVED_MODULE_STATEMENTS
    from haloflow.m01.statements import StatementMode, build_statement_catalog

    domain = {
        key: (StatementMode(mode), capability, query)
        for key, (mode, capability, query) in part_a.DOMAIN_STATEMENTS_SPEC.items()
    }
    return build_statement_catalog(*APPROVED_MODULE_STATEMENTS, domain)


@asynccontextmanager
async def _runtime(part_a: ModuleType, m02_ids: Any, pool_class: Any = None) -> AsyncIterator[Any]:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_tenant_migrations,
    )
    from haloflow.m01.control_store import PsycopgControlStore
    from haloflow.m01.pool import TenantPool
    from haloflow.m01.runtime import TenantRuntimeDependencies, compose_tenant_runtime

    cls = pool_class or TenantPool
    pool = cls(m02_ids.logins["haloflow_runtime"], min_size=1, max_size=1)
    await pool.open()
    try:
        yield compose_tenant_runtime(
            TenantRuntimeDependencies(
                pool=pool,
                control_store=PsycopgControlStore(pool),
                migrator_connect=_factory(m02_ids.logins["haloflow_migrator"]),
                provisioner_connect=_factory(m02_ids.logins["haloflow_provisioner"]),
            ),
            registry=build_production_tenant_migrations(),
            catalog=_test_catalog(part_a),
            supported_schema_versions=APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        )
    finally:
        await pool.close()


async def _context(part_a: ModuleType, runtime: Any, tenant_id: str, principal_id: str) -> Any:
    from haloflow.m01.context import CorrelationSource, Principal, PrincipalKind, TrustedSource

    capabilities = frozenset({APPEND, part_a.DOMAIN_CAPABILITY})
    return await runtime.resolver.resolve(
        principal=Principal(
            kind=PrincipalKind.WORKLOAD,
            id=principal_id,
            auth_method="test",
            authorized_tenant_ids=frozenset({tenant_id}),
            capabilities=capabilities,
        ),
        tenant_hint=tenant_id,
        purpose="operations",
        capabilities=capabilities,
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
        if not part_a.regclass_exists(conn, f"{schema}.m02_test_domain_state"):
            conn.execute(part_a.TEST_DOMAIN_SQL.format(schema=schema))
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
    tables = (*part_a.COUNTED_TABLES, "m02_test_domain_state")
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        out = []
        for table in tables:
            row = conn.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            ).fetchone()
            assert row is not None
            out.append(int(row[0]))
    return tuple(out)


def _delta(after: tuple[int, ...], before: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a - b for a, b in zip(after, before, strict=True))


def _domain_writer(part_a: ModuleType, value: str = "applied") -> Callable[..., Awaitable[None]]:
    async def apply(handle: Any, result: Any) -> None:
        await handle.execute(
            part_a.DOMAIN_INSERT_KEY, {"event_id": result.event_id, "value": value}
        )

    return apply


def _domain_row(m02_ids: Any, schema: str, event_id: UUID) -> tuple[Any, ...] | None:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            sql.SQL("SELECT value FROM {}.m02_test_domain_state WHERE event_id = %s").format(
                sql.Identifier(schema)
            ),
            (event_id,),
        ).fetchone()
    return None if row is None else tuple(row)


class _Spy:
    """Counts append-statement dispatches through any M01 repository handle."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from haloflow.m01.gateway import TenantRepositoryHandle

        self.appends = 0
        original = TenantRepositoryHandle.fetch_one
        spy = self

        async def fetch_one(handle: Any, key: str, params: Any = None) -> Any:
            if key == APPEND:
                spy.appends += 1
            return await original(handle, key, params)

        monkeypatch.setattr(TenantRepositoryHandle, "fetch_one", fetch_one)


class _DomainError(Exception):
    pass


@pytest.fixture
def tenant(production_tenant: tuple[str, str]) -> tuple[str, str]:
    return production_tenant


# --- S-01 / ATOM / EX / PRD / ATT -----------------------------------------------------


async def test_s01_event_and_domain_state_commit_together(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    principal = str(uuid.uuid4())
    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], principal)
        observation = _observation(obs, part_a, subject, source)
        result = await foundation.record_observation(context, observation, _domain_writer(part_a))
    assert (result.event_id, result.appended_or_existing) == (observation.event_id, "appended")
    assert _delta(_counts(part_a, m02_ids, tenant[1]), before) == (1, 2, 1, 1)
    assert _domain_row(m02_ids, tenant[1], observation.event_id) == ("applied",)
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        row = conn.execute(
            sql.SQL(
                "SELECT producer_service, workload_id::text, actor_id FROM {}.patient_events"
                " WHERE event_id = %s"
            ).format(sql.Identifier(tenant[1])),
            (observation.event_id,),
        ).fetchone()
    assert row == (part_a.TEST_PRODUCER, principal, None)  # PRD + ATT from bound context


async def test_atom_domain_write_then_exception_neither_commits(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    raised = _DomainError("after domain write")
    writer = _domain_writer(part_a)

    async def apply(handle: Any, result: Any) -> None:
        await writer(handle, result)
        raise raised

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(_DomainError) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert caught.value is raised  # PA-02 / PA-06d: same object
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_ex_existing_branch_does_not_invoke_callback(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    calls: list[Any] = []
    writer = _domain_writer(part_a)

    async def apply(handle: Any, result: Any) -> None:
        calls.append(result)
        await writer(handle, result)

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
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


# --- PA-06e / PA-03 -------------------------------------------------------------------


async def test_pa06e_callback_database_exception_object_propagates(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    captured: list[BaseException] = []

    async def apply(handle: Any, result: Any) -> None:
        params = {"event_id": result.event_id, "value": "first"}
        await handle.execute(part_a.DOMAIN_INSERT_KEY, params)
        try:
            await handle.execute(part_a.DOMAIN_INSERT_KEY, params)  # duplicate PK
        except BaseException as error:
            captured.append(error)
            raise

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(psycopg.errors.UniqueViolation) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert len(captured) == 1 and caught.value is captured[0]
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_pa03_nested_gateway_in_callback_unchanged_type(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    from haloflow.m01.errors import NestedTenantTransaction

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))

        async def apply(handle: Any, result: Any) -> None:
            await runtime.gateway.with_tenant_transaction(context, _noop)

        with pytest.raises(NestedTenantTransaction):
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def _noop(handle: Any) -> None:
    return None


# --- PA-06a / PA-06b / MK / MK-NEG: commit-phase classification -------------------------


class _Rollback(Exception):
    pass


def _faulting_pool(mode: str, fired: list[str]) -> Any:
    """One-shot TenantPool double (Codex 1ee91aa6 pt 1).

    The first gateway connection's `transaction` is replaced for exactly one use and
    RESTORED in `finally` before the connection returns to the pool, so a retry on
    the same pooled connection uses the real transaction manager. `fired` records
    each activation.
    """

    from haloflow.m01.pool import TenantPool

    class _Pool(TenantPool):
        armed = True

        @asynccontextmanager
        async def _connection_for_gateway(self) -> AsyncIterator[Any]:
            async with super()._connection_for_gateway() as connection:
                if not type(self).armed:
                    yield connection
                    return
                type(self).armed = False
                real = connection.transaction

                @asynccontextmanager
                async def transaction(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                    fired.append(mode)
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
                try:
                    yield connection
                finally:
                    del connection.transaction  # restore the class method
                    assert connection.transaction.__func__ is type(connection).transaction

    return _Pool


@pytest.mark.parametrize(
    ("mode", "retry_outcome", "committed_after_fault"),
    [("commit_then_raise", "existing", 1), ("raise_no_commit", "appended", 0)],
)
async def test_pa06ab_one_shot_commit_phase_fault_then_real_retry(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    mode: str,
    retry_outcome: str,
    committed_after_fault: int,
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    fired: list[str] = []
    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids, _faulting_pool(mode, fired)) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        observation = _observation(obs, part_a, subject, source)
        with pytest.raises(obs.AppendCommitUnknown) as caught:
            await foundation.record_observation(context, observation, _domain_writer(part_a))
        assert caught.value.event_id == observation.event_id
        assert caught.value.__cause__ is None
        assert fired == [mode]
        assert _delta(_counts(part_a, m02_ids, tenant[1]), before)[0] == committed_after_fault
        retry = await foundation.record_observation(context, observation, _domain_writer(part_a))
        assert fired == [mode]  # the seam fired exactly once; the retry was real
    assert (retry.appended_or_existing, retry.event_id) == (retry_outcome, observation.event_id)
    assert _delta(_counts(part_a, m02_ids, tenant[1]), before) == (1, 2, 1, 1)


async def test_mk_post_body_path_check_failure_is_unknown_not_definite(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    callback_done: list[bool] = []
    writer = _domain_writer(part_a)

    async def apply(handle: Any, result: Any) -> None:
        await writer(handle, result)
        callback_done.append(True)

    async with _runtime(part_a, m02_ids) as runtime:
        gateway = runtime.gateway
        original = gateway._verify_effective_path
        calls = {"n": 0}

        async def verify(connection: Any, schema_key: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # M01's post-body check, after the callback returned
                raise RuntimeError("simulated post-body path check failure")
            await original(connection, schema_key)

        monkeypatch.setattr(gateway, "_verify_effective_path", verify)
        foundation = obs.EventFoundation(gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(obs.AppendCommitUnknown):
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert callback_done == [True]
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_mk_neg_callback_exception_before_marker_is_not_unknown(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()

    async def apply(handle: Any, result: Any) -> None:
        raise _DomainError("before marker")

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        with pytest.raises(_DomainError) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert not isinstance(caught.value, obs.AppendCommitUnknown)


async def test_pa06c_append_failure_policy(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    """Policy, not source spelling (Codex 1ee91aa6 pt 4): fixed message, reason code,
    no cause or unsuppressed context, no driver exception anywhere in the chain, and
    an injected canary value never rendered."""

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    canary = "CANARY-7f3a91"
    before = _counts(part_a, m02_ids, tenant[1])

    async def apply(handle: Any, result: Any) -> None:
        raise AssertionError("callback must not run after a failed append")

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        bad = _observation(obs, part_a, subject, source, metadata={"label_code": canary})
        with pytest.raises(obs.AppendFailed) as caught:
            await foundation.record_observation(context, bad, apply)
    error = caught.value
    assert error.reason_code == "COMMAND_MALFORMED"
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    chain: list[BaseException] = []
    link: BaseException | None = error
    while link is not None:
        chain.append(link)
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    assert not any(isinstance(item, psycopg.Error) for item in chain)
    rendered = "".join(traceback.format_exception(error)) + repr(error) + str(error)
    assert canary not in rendered
    assert _counts(part_a, m02_ids, tenant[1]) == before


# --- PA-50 / BND: binding -------------------------------------------------------------------


async def test_pa50_binding_after_transaction_is_handle_expired_no_dispatch(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from haloflow.m01.errors import RepositoryHandleExpired

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    kept: list[Any] = []

    async def body(binding: Any) -> None:
        kept.append(binding)

    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        await foundation.run_in_transaction(context, body)
        spy = _Spy(monkeypatch)
        with pytest.raises(RepositoryHandleExpired):
            await foundation.append_observation_in(
                kept[0], _observation(obs, part_a, subject, source)
            )
    assert spy.appends == 0
    assert _counts(part_a, m02_ids, tenant[1]) == before


def test_bnd_binding_cannot_be_constructed_by_caller() -> None:
    obs = _obs_module()
    with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable; success is not
        obs.TransactionBinding(object(), object())


async def test_bnd_binding_attributes_cannot_be_reassigned(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    outcomes: list[str] = []

    async def body(binding: Any) -> None:
        for name in ("context", "handle"):
            try:
                setattr(binding, name, object())
            except (FrozenInstanceError, AttributeError):
                outcomes.append(name)

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        await foundation.run_in_transaction(context, body)
    assert outcomes == ["context", "handle"]


async def test_bnd_binding_from_another_foundation_refused(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids) as runtime:
        first = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        second = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER_B)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        spy = _Spy(monkeypatch)

        async def body(binding: Any) -> None:
            await second.append_observation_in(binding, _observation(obs, part_a, subject, source))

        with pytest.raises(obs.BindingForeign):
            await first.run_in_transaction(context, body)
    assert spy.appends == 0
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_bnd_one_boundary_composition_with_domain_write_rolls_back(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))

        async def body(binding: Any) -> None:
            result = await foundation.append_observation_in(
                binding, _observation(obs, part_a, subject, source)
            )
            await binding.handle.execute(
                part_a.DOMAIN_INSERT_KEY, {"event_id": result.event_id, "value": "composed"}
            )
            raise _DomainError("after append and domain write")

        with pytest.raises(_DomainError):
            await foundation.run_in_transaction(context, body)
    assert _counts(part_a, m02_ids, tenant[1]) == before


# --- PA-30 -------------------------------------------------------------------------------------


async def test_pa30_suspended_tenant_refused_with_zero_append_dispatch(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver's own control-data reads are expected and NOT counted; the
    assertion is zero append dispatch and zero callback invocation (Codex pt 6)."""

    from haloflow.m01.errors import TenantUnavailable

    _ready(part_a, m02_ids, tenant[1])
    spy = _Spy(monkeypatch)
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
        async with _runtime(part_a, m02_ids) as runtime:
            with pytest.raises(TenantUnavailable):
                await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
    finally:
        with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
            conn.execute("SET session_replication_role = replica")
            conn.execute(
                "UPDATE shared.tenants SET lifecycle_state = %s WHERE tenant_id = %s",
                (original, tenant[0]),
            )
    assert spy.appends == 0


# --- PA-18 -------------------------------------------------------------------------------------


async def test_pa18_no_metadata_or_driver_text_in_logs(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    caplog.set_level(logging.DEBUG)
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        await foundation.record_observation(
            context,
            _observation(obs, part_a, subject, source, metadata={"label_code": "unreviewed"}),
            _domain_writer(part_a),
        )
        with pytest.raises(obs.AppendFailed):
            await foundation.record_observation(
                context,
                _observation(obs, part_a, subject, source, metadata={"label_code": "CANARY-91"}),
                _domain_writer(part_a),
            )
    text = "\n".join(r.getMessage() for r in caplog.records)
    for leak in ("unreviewed", "CANARY-91", subject, source, "P0001"):
        assert leak not in text, leak


# --- PA-05(b): callback-omission mutant is detected -----------------------------------------


def _domain_oracle(m02_ids: Any, schema: str, event_ids: list[UUID]) -> list[UUID]:
    """Event ids that committed WITHOUT their owning domain state."""

    return [eid for eid in event_ids if _domain_row(m02_ids, schema, eid) is None]


@pytest.mark.parametrize("variant", ["reviewed", "mutant_skip_callback"])
async def test_pa05b_callback_omission_detected_by_domain_oracle(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    assert hasattr(obs.EventFoundation, part_a.CALLBACK_SEAM), "callback seam missing"
    if variant == "mutant_skip_callback":

        async def skip(self: Any, apply_state_change: Any, handle: Any, result: Any) -> None:
            return None

        monkeypatch.setattr(obs.EventFoundation, part_a.CALLBACK_SEAM, skip)
    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        observation = _observation(obs, part_a, subject, source)
        await foundation.record_observation(context, observation, _domain_writer(part_a))
    missing = _domain_oracle(m02_ids, tenant[1], [observation.event_id])
    if variant == "reviewed":
        assert missing == []
    else:
        assert missing == [observation.event_id]  # the oracle detects the mutant


# --- F-OBS1 (Codex 20261031T152545Z): cancellation before and after the marker ----------


def _cancelling_pool(mode: str, fired: list[str]) -> Any:
    """One-shot TenantPool double, as `_faulting_pool`, raising `CancelledError` at
    transaction exit: after COMMIT (`commit_then_cancel`) or with the transaction
    rolled back (`cancel_no_commit`). It models a cancellation that arrives while
    the gateway is committing, after the body (and so the marker) completed."""

    import asyncio

    from haloflow.m01.pool import TenantPool

    class _Pool(TenantPool):
        armed = True

        @asynccontextmanager
        async def _connection_for_gateway(self) -> AsyncIterator[Any]:
            async with super()._connection_for_gateway() as connection:
                if not type(self).armed:
                    yield connection
                    return
                type(self).armed = False
                real = connection.transaction

                @asynccontextmanager
                async def transaction(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                    fired.append(mode)
                    if mode == "commit_then_cancel":
                        async with real(*args, **kwargs) as tx:
                            yield tx
                        raise asyncio.CancelledError()
                    try:
                        async with real(*args, **kwargs) as tx:
                            yield tx
                            raise _Rollback()
                    except _Rollback:
                        pass
                    raise asyncio.CancelledError()

                connection.transaction = transaction  # type: ignore[method-assign]
                try:
                    yield connection
                finally:
                    del connection.transaction
                    assert connection.transaction.__func__ is type(connection).transaction

    return _Pool


@pytest.mark.parametrize(
    ("mode", "retry_outcome", "committed_after_fault"),
    [("commit_then_cancel", "existing", 1), ("cancel_no_commit", "appended", 0)],
)
async def test_fobs1_post_marker_cancellation_is_unknown_then_retry_converges(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    mode: str,
    retry_outcome: str,
    committed_after_fault: int,
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    fired: list[str] = []
    before = _counts(part_a, m02_ids, tenant[1])
    async with _runtime(part_a, m02_ids, _cancelling_pool(mode, fired)) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        observation = _observation(obs, part_a, subject, source)
        with pytest.raises(obs.AppendCommitUnknown) as caught:
            await foundation.record_observation(context, observation, _domain_writer(part_a))
        assert caught.value.event_id == observation.event_id
        assert caught.value.__cause__ is None
        assert fired == [mode]
        delta = _delta(_counts(part_a, m02_ids, tenant[1]), before)
        assert delta[0] == committed_after_fault and delta[3] == committed_after_fault
        retry = await foundation.record_observation(context, observation, _domain_writer(part_a))
        assert fired == [mode]  # the retry used the real transaction manager
    assert (retry.appended_or_existing, retry.event_id) == (retry_outcome, observation.event_id)
    # One event and exactly one domain effect, whichever branch the retry took.
    assert _delta(_counts(part_a, m02_ids, tenant[1]), before) == (1, 2, 1, 1)
    assert _domain_row(m02_ids, tenant[1], observation.event_id) == ("applied",)


async def test_fobs1_pre_marker_cancellation_propagates_and_rolls_back(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str]
) -> None:
    """A real task cancellation while the callback is running (before the marker):
    the same CancelledError type propagates, nothing commits, and a later call with
    the same observation appends normally."""

    import asyncio

    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    writer = _domain_writer(part_a)
    entered = asyncio.Event()

    async def apply(handle: Any, result: Any) -> None:
        await writer(handle, result)
        entered.set()
        await asyncio.Event().wait()  # held until cancelled

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        observation = _observation(obs, part_a, subject, source)
        task = asyncio.create_task(foundation.record_observation(context, observation, apply))
        # Bounded: if the callback is never reached (a mutant), fail instead of hanging.
        waiter = asyncio.ensure_future(entered.wait())
        await asyncio.wait({task, waiter}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        reached = entered.is_set()
        task.cancel()
        if not reached:
            await asyncio.gather(task, return_exceptions=True)
        assert reached, "the callback was not reached"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert _counts(part_a, m02_ids, tenant[1]) == before
        again = await foundation.record_observation(context, observation, writer)
    assert again.appended_or_existing == "appended"
    assert _delta(_counts(part_a, m02_ids, tenant[1]), before) == (1, 2, 1, 1)


# --- Fallback reason codes (Codex 20261031T155616Z): the bounded error contract -------------
#
# These rows inject at M02's own append call: the spy-style seam replaces
# TenantRepositoryHandle.fetch_one for the append key only. The database errors are
# REAL psycopg errors, raised by PostgreSQL for the stated SQLSTATE and message.


def _real_db_error(m02_ids: Any, sqlstate: str, message: str) -> psycopg.Error:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        try:
            probe = sql.SQL(
                "DO $$BEGIN RAISE EXCEPTION USING ERRCODE = {}, MESSAGE = {}; END$$"
            ).format(sql.Literal(sqlstate), sql.Literal(message))
            conn.execute(probe)
        except psycopg.Error as error:
            assert error.sqlstate == sqlstate
            return error
    raise AssertionError("the probe did not raise")


def _seam_append(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[int]:
    """Replace the append dispatch only: raise `outcome` if it is an exception,
    else return it as the row. Other statements (the callback's) are untouched."""

    from haloflow.m01.gateway import TenantRepositoryHandle

    calls: list[int] = []
    original = TenantRepositoryHandle.fetch_one

    async def fetch_one(handle: Any, key: str, params: Any = None) -> Any:
        if key != APPEND:
            return await original(handle, key, params)
        calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(TenantRepositoryHandle, "fetch_one", fetch_one)
    return calls


def _assert_sanitized(error: BaseException, *leaks: str) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    chain: list[BaseException] = []
    link: BaseException | None = error
    while link is not None:
        chain.append(link)
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    assert not any(isinstance(item, psycopg.Error) for item in chain)
    rendered = "".join(traceback.format_exception(error)) + repr(error) + str(error)
    for leak in leaks:
        assert leak not in rendered, leak


@pytest.mark.parametrize(
    ("sqlstate", "message", "reason"),
    [
        ("P0001", "tenant binding absent", "TENANT_BINDING_ABSENT"),
        ("P0001", "tenant binding inactive", "TENANT_BINDING_INACTIVE"),
        ("P0001", "tenant binding mismatch", "TENANT_BINDING_MISMATCH"),
        ("22023", "tenant context invalid", "TENANT_CONTEXT_INVALID"),
        ("P0001", "CANARY-unlisted-code-5e1", "APPEND_UNAVAILABLE"),
        ("40001", "CANARY-serialization-9b2", "APPEND_UNAVAILABLE"),
        ("XX000", "CANARY-internal-3c7", "APPEND_UNAVAILABLE"),
    ],
    ids=["binding-absent", "binding-inactive", "binding-mismatch", "context-22023",
         "unlisted-p0001", "other-40001", "other-xx000"],
)
async def test_map_database_error_to_fixed_reason_code(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
    message: str,
    reason: str,
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    injected = _real_db_error(m02_ids, sqlstate, message)
    before = _counts(part_a, m02_ids, tenant[1])
    callback: list[Any] = []

    async def apply(handle: Any, result: Any) -> None:
        callback.append(result)

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        calls = _seam_append(monkeypatch, injected)
        with pytest.raises(obs.AppendFailed) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert calls == [1]
    assert caught.value.reason_code == reason
    _assert_sanitized(caught.value, message, sqlstate)
    assert callback == []
    assert _counts(part_a, m02_ids, tenant[1]) == before


_GOOD_RESULT = {
    "schema_version": 1,
    "event_id": "00000000-0000-4000-8000-0000000000aa",
    "appended_or_existing": "appended",
    "handoff_state": "enqueued",
}


@pytest.mark.parametrize(
    "row",
    [
        None,
        (),
        ("not-an-object",),
        ({},),
        ({**_GOOD_RESULT, "extra": 1},),
        ({**_GOOD_RESULT, "schema_version": 2},),
        ({**_GOOD_RESULT, "event_id": "CANARY-not-a-uuid"},),
        ({**_GOOD_RESULT, "appended_or_existing": "maybe"},),
        ({**_GOOD_RESULT, "handoff_state": None},),
        (_GOOD_RESULT, _GOOD_RESULT),
    ],
    ids=["no-row", "empty-row", "string", "empty-object", "extra-key", "version-2",
         "bad-uuid", "bad-outcome", "null-handoff", "two-columns"],
)
async def test_map_malformed_function_result_is_result_invalid(
    part_a: ModuleType,
    m02_ids: Any,
    tenant: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    row: Any,
) -> None:
    subject, source = _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    before = _counts(part_a, m02_ids, tenant[1])
    callback: list[Any] = []

    async def apply(handle: Any, result: Any) -> None:
        callback.append(result)

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        calls = _seam_append(monkeypatch, row)
        with pytest.raises(obs.AppendFailed) as caught:
            await foundation.record_observation(
                context, _observation(obs, part_a, subject, source), apply
            )
    assert calls == [1]
    assert caught.value.reason_code == "APPEND_RESULT_INVALID"
    _assert_sanitized(caught.value, "CANARY-not-a-uuid")
    assert callback == []
    assert _counts(part_a, m02_ids, tenant[1]) == before


async def test_map_control_a_well_formed_seam_result_is_accepted(
    part_a: ModuleType, m02_ids: Any, tenant: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the seam: a well-formed row is parsed and the callback runs
    (the domain write is the test's own; no event row exists, so nothing commits that
    the counted tables would see other than the domain row)."""

    _ready(part_a, m02_ids, tenant[1])
    obs = _obs_module()
    seen: list[Any] = []

    async def apply(handle: Any, result: Any) -> None:
        seen.append(result)

    async with _runtime(part_a, m02_ids) as runtime:
        foundation = obs.EventFoundation(runtime.gateway, producer_service=part_a.TEST_PRODUCER)
        context = await _context(part_a, runtime, tenant[0], str(uuid.uuid4()))
        _seam_append(monkeypatch, (_GOOD_RESULT,))
        result = await foundation.record_observation(
            context, _observation(obs, part_a, str(uuid.uuid4()), str(uuid.uuid4())), apply
        )
    assert (str(result.event_id), result.appended_or_existing, result.handoff_state) == (
        _GOOD_RESULT["event_id"], "appended", "enqueued"
    )
    assert seen == [result]
