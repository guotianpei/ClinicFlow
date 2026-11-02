"""Fixtures for the L-1W W2 PostgreSQL tests (plan v1 §2).

The session fixture creates only the runtime and staff-owner fixture logins, whose
group roles exist before revision 008. Nothing here touches a W2 object, so on a
tree without 008 each W2 test fails in its own body.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from l1w_db_support import (
    RUNTIME,
    STAFF_OWNER,
    Bound,
    L1WEnv,
    admin_bind,
    identity,
    l1w_environment,
)


@pytest.fixture(scope="session")
def env(migrated_database: str) -> Iterator[L1WEnv]:
    with l1w_environment(migrated_database) as handle:
        handle.login("runtime", RUNTIME)
        handle.login("staff_owner", STAFF_OWNER)
        yield handle


@pytest.fixture
def runtime(env: L1WEnv) -> Iterator[psycopg.Connection[Any]]:
    with env.connect("runtime") as conn:
        yield conn


@pytest.fixture
def owner(env: L1WEnv) -> Iterator[psycopg.Connection[Any]]:
    with env.connect("staff_owner") as conn:
        yield conn


@pytest.fixture
def bind(env: L1WEnv) -> Callable[..., Bound]:
    """Create a tenant (unless given), a staff id and an identity, bound by an admin."""

    def make(tenant: str | None = None) -> Bound:
        tenant_id = tenant or env.tenant()
        staff = uuid.uuid4()
        ident = identity(env)
        actor = uuid.uuid4()
        with env.connect("staff_owner") as conn:
            assert admin_bind(conn, tenant_id, staff, ident, actor) == "BOUND"
        return Bound(tenant_id, staff, ident, actor)

    return make


@pytest.fixture
def now(env: L1WEnv) -> Callable[[], datetime]:
    return env.db_now


@pytest.fixture
def ago(env: L1WEnv) -> Callable[..., datetime]:
    def make(**delta: float) -> datetime:
        return env.db_now() - timedelta(**delta)

    return make
