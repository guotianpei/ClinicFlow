"""CP2-2b 2B-D30 (RQ-5, R-B2): refusal BEFORE any table access -- the contention oracle.

STATUS: PENDING (test cases v3). A feasibility-gated oracle, never a PASS until it
has been shown to work on the CI postgres:17 path. If it proves infeasible the row
stays PENDING, and closing CP2-2b with it pending is Rachel's risk decision. An
exploratory PostgreSQL 16 scratch run under O-1 is never counted.

Oracle (architecture v3 section 6): an independent ADMIN session holds ACCESS
EXCLUSIVE on `<schema>.operation_registry`. A fresh runtime transaction that has
read nothing calls the gateway under a bounded statement_timeout.

  correct gateway, NULL / unset / empty / malformed   -> exact SQLSTATE, PROMPTLY
  valid call (blocking control)                       -> waits (Lock) and times out
  reordered mutant (table access first), NULL call    -> waits and times out
  release and retry                                   -> the valid call succeeds

The hypothesis under test is that plpgsql prepares the SELECT only when it is
reached. Post-error lock absence is not used anywhere.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import m02_gateway_support as gw
import m02_support as m02
import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.postgres

TIMEOUT_MS = 3000
PROMPT_S = 1.0


@contextmanager
def exclusive_lock(ids: Any, schema: str) -> Iterator[None]:
    """ADMIN holds ACCESS EXCLUSIVE on the registry for the duration (setup, labelled).

    Acquisition is BOUNDED (`lock_timeout`), and cleanup covers startup too: whatever
    happens after the thread starts, `release` is set and the thread is joined with a
    bound. A holder failure is propagated, never swallowed.
    """

    held = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with psycopg.connect(ids.admin) as conn:
                conn.execute("SET lock_timeout = '5s'")
                conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                    sql.Identifier(schema, m02.TABLE)))
                held.set()
                release.wait(timeout=60)
                conn.rollback()
        except BaseException as error:
            failure.append(error)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        acquired = held.wait(timeout=10)
        if failure:
            raise AssertionError("the lock holder failed") from failure[0]
        assert acquired, "the ACCESS EXCLUSIVE holder did not acquire within its bound"
        yield
    finally:
        release.set()
        thread.join(timeout=15)
        assert not thread.is_alive(), "the lock holder did not terminate"
    if failure:
        raise AssertionError("the lock holder failed") from failure[0]


REFUSALS = [
    ("null", "any", None, "22004"),
    ("unset", None, "seeded", "22023"),
    ("empty", "", "seeded", "22023"),
    ("malformed", "A", "seeded", "22023"),
]


@pytest.mark.parametrize(("label", "context", "which", "sqlstate"), REFUSALS,
                         ids=[r[0] for r in REFUSALS])
def test_2b_d30_a_refusal_returns_promptly_under_an_exclusive_lock(
    m02_ids: Any, m02_tenant: tuple[str, str], label: str, context: str | None,
    which: str | None, sqlstate: str,
) -> None:
    m02.provision_sync(m02_ids, m02.production_registry(), m02_tenant)
    tenant_id, schema = m02_tenant
    seeded = gw.seed(m02_ids, schema, f"d30-{label}")
    tenant = tenant_id if context == "any" else context
    operation_id = seeded if which == "seeded" else None
    with exclusive_lock(m02_ids, schema):
        refused = gw.probe_call(m02_ids, schema, tenant, operation_id, timeout_ms=TIMEOUT_MS)
        blocked = gw.probe_call(m02_ids, schema, tenant_id, seeded, timeout_ms=TIMEOUT_MS)
    # Blocking control: the valid call WAITED on a lock (observed on its own PID) and
    # timed out. Without this the holder might not block access at all.
    assert (blocked.sqlstate, blocked.lock_wait_seen) == ("57014", True)
    # The refusal under test: exact SQLSTATE, promptly, and it never waited on a lock.
    assert refused.elapsed is not None
    assert (refused.sqlstate, refused.elapsed < PROMPT_S, refused.lock_wait_seen) == (
        sqlstate, True, False,
    )
    released = gw.probe_call(m02_ids, schema, tenant_id, seeded, timeout_ms=TIMEOUT_MS)
    assert (released.sqlstate, released.value) == (None, seeded), "release and retry"


MUTANT = """
CREATE FUNCTION {schema}.m02_d30_mutant(p_operation_id uuid) RETURNS uuid
  LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE CALLED ON NULL INPUT SECURITY DEFINER
  SET search_path = pg_catalog, {schema}, pg_temp
AS $body$
DECLARE v_id uuid;
BEGIN
  SELECT r.operation_id INTO v_id FROM {schema}.operation_registry AS r
   WHERE r.operation_id = p_operation_id FOR UPDATE;
  IF p_operation_id IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '22004', MESSAGE = 'operation id required';
  END IF;
  RETURN v_id;
END;
$body$;
GRANT EXECUTE ON FUNCTION {schema}.m02_d30_mutant(uuid) TO haloflow_runtime;
"""


def test_2b_d30_the_reordered_mutant_waits_under_the_same_lock(
    m02_ids: Any, m02_tenant: tuple[str, str]
) -> None:
    """ME, constructed mutant (table access before the NULL check): it must NOT be
    prompt. If it were, the oracle could not distinguish the orders."""

    m02.provision_sync(m02_ids, m02.production_registry(), m02_tenant)
    tenant_id, schema = m02_tenant
    with m02.connect_as(m02_ids, "LOCK") as conn:
        conn.execute(MUTANT.replace("{schema}", schema))  # type: ignore[call-overload]
    with exclusive_lock(m02_ids, schema):
        mutant = gw.probe_call(m02_ids, schema, tenant_id, None, timeout_ms=TIMEOUT_MS,
                               function="m02_d30_mutant")
    assert (mutant.sqlstate, mutant.lock_wait_seen) == ("57014", True)
