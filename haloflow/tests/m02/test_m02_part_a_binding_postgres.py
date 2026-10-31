"""UC1 part A — PA-40 (two-tenant enqueue binding) and PA-33 (valid pair), PostgreSQL 17.

Codex ALIGN 20261031T165210Z: closeout gaps. Two production tenants, A (the
module's `production_tenant`) and B (a per-test tenant provisioned through the same
production registry). Every row runs in BOTH directions.

PA-40 (tenant-binding decision TB-4):
- each tenant's installed `m02_enqueue_event_handoffs` body is exactly the
  `FunctionExpectation.render_body(schema_key)` of the t010 unit's own frozen
  declaration for ITS schema, and contains its own schema-key literal;
- the rendering for the OTHER schema does not match it (cross-binding fails at the
  catalogue level);
- calling one tenant's function with the other tenant's registered id as the
  setting is refused `tenant binding mismatch`, after a positive control on the same
  tenant; neither tenant's rows change on the refusal (no foreign-tenant mutation).

PA-33: for each tenant, the enqueued outbox row's `tenant_id`, its payload tenant and
its V1-form M11 key are the REGISTRY value read from `shared.tenants` by schema key.

Expected values come from the unit declaration, the registry and the M11 oracle in
part_a_support, never from the installed function under test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import m02_support as m02
import psycopg
import pytest
from psycopg import sql
from test_m02_part_a_append_postgres import (
    _admin_one,
    _append,
    _command,
    _counts,
    _ready,
    _refused,
)

pytestmark = pytest.mark.postgres

T010 = "t010_m02_enqueue"
ENQUEUE = "m02_enqueue_event_handoffs(uuid, jsonb)"


@pytest.fixture
def pair(
    production_tenant: tuple[str, str], m02_tenant: tuple[str, str], m02_ids: Any
) -> Iterator[tuple[tuple[str, str], tuple[str, str]]]:
    m02.provision_sync(
        m02_ids, m02.production_registry(), m02_tenant, supported=m02.PRODUCTION_SUPPORTED
    )
    assert production_tenant[1] != m02_tenant[1]
    yield production_tenant, m02_tenant


def _expectation() -> Any:
    """The t010 unit's own frozen declaration, built as M01 builds it."""

    from haloflow.m01.provisioning.verification import AclEntry, FunctionExpectation

    unit = next(u for u in m02.production_registry() if u.migration_id == T010)
    (function,) = unit.policy_verification["functions"]
    return FunctionExpectation(
        name=function["name"],
        argument_types=tuple(function["argument_types"]),
        owner=function["owner"],
        security_definer=function["security_definer"],
        config=tuple(function["config"]),
        acl=tuple(
            AclEntry(grantee=a["grantee"], privileges=tuple(a["privileges"]))
            for a in function["acl"]
        ),
        body=function["body"],
    )


def _digest(body: str) -> bytes:
    from haloflow.m01.provisioning.checksum import normalize_body

    return hashlib.sha256(normalize_body(body).encode("utf-8")).digest()


def _installed_body(m02_ids: Any, schema: str) -> str:
    row = _admin_one(
        m02_ids,
        sql.SQL("SELECT prosrc FROM pg_catalog.pg_proc WHERE oid = {}::regprocedure").format(
            sql.Literal(f"{schema}.{ENQUEUE}")
        ),
    )
    assert row is not None, f"{schema}.{ENQUEUE} is not installed"
    body: str = row[0]
    return body


def _registry_tenant_id(m02_ids: Any, schema: str) -> str:
    row = _admin_one(
        m02_ids,
        sql.SQL("SELECT tenant_id FROM shared.tenants WHERE schema_key = %s"),
        (schema,),
    )
    assert row is not None
    tenant_id: str = row[0]
    return tenant_id


def _literal(schema: str) -> str:
    return f"WHERE t.schema_key = '{schema}'"


# --- PA-40: rendered literal and render_body, both tenants ---------------------------


def test_pa40_each_enqueue_body_is_its_own_schemas_rendering(
    m02_ids: Any, pair: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    expectation = _expectation()
    (_, schema_a), (_, schema_b) = pair
    for own, other in ((schema_a, schema_b), (schema_b, schema_a)):
        installed = _installed_body(m02_ids, own)
        # Positive: the installed body is exactly the declaration rendered for itself.
        assert _digest(installed) == _digest(expectation.render_body(own)), own
        assert _literal(own) in installed, own
        # Cross-binding fails: the other schema's rendering is not this body.
        assert _digest(installed) != _digest(expectation.render_body(other)), own
        assert _literal(other) not in installed, own
        assert other not in installed, own


# --- PA-40 behavioural cross-binding + PA-33 valid pair, both directions ---------------


@pytest.mark.parametrize("direction", ["a_with_b_setting", "b_with_a_setting"])
def test_pa40_pa33_cross_binding_refused_after_a_registry_derived_valid_append(
    part_a: ModuleType,
    m02_ids: Any,
    pair: tuple[tuple[str, str], tuple[str, str]],
    direction: str,
) -> None:
    first, second = pair if direction == "a_with_b_setting" else (pair[1], pair[0])
    for tenant in (first, second):
        _ready(part_a, m02_ids, tenant[1])
    registry_first = _registry_tenant_id(m02_ids, first[1])
    registry_second = _registry_tenant_id(m02_ids, second[1])
    assert (registry_first, registry_second) == (first[0], second[0])
    assert registry_first != registry_second

    # PA-33 (valid pair): setting equals the registry value -> appended, and the
    # enqueue row's identity is the registry tenant of THIS schema.
    before_first = _counts(part_a, m02_ids, first[1])
    before_second = _counts(part_a, m02_ids, second[1])
    command = _command(part_a, m02_ids, first[1])
    result = _append(m02_ids, first, command)
    assert (result["appended_or_existing"], result["handoff_state"]) == ("appended", "enqueued")
    row = _admin_one(
        m02_ids,
        sql.SQL(
            "SELECT tenant_id, payload, dedupe_key FROM {}.event_handoff_outbox"
            " WHERE source_event_id = %s"
        ).format(sql.Identifier(first[1])),
        (command["event_id"],),
    )
    assert row is not None
    assert row[0] == registry_first
    assert row[1]["tenant_id"] == registry_first
    assert row[2] == part_a.m11_key(
        registry_first, command["event_id"], "event_signal", "m02.event.appended"
    )
    after_valid = _counts(part_a, m02_ids, first[1])
    assert tuple(a - b for a, b in zip(after_valid, before_first, strict=True)) == (1, 2, 1)

    # PA-40 cross-binding: this schema's function with the OTHER tenant's registered,
    # active id as the setting is refused, and nothing changes in either tenant.
    foreign = _command(part_a, m02_ids, first[1])
    _refused(m02_ids, first, foreign, part_a.MSG_BINDING_MISMATCH, setting=registry_second)
    assert _counts(part_a, m02_ids, first[1]) == after_valid
    assert _counts(part_a, m02_ids, second[1]) == before_second
    absent = _admin_one(
        m02_ids,
        sql.SQL("SELECT count(*) FROM {}.patient_events WHERE event_id = %s").format(
            sql.Identifier(first[1])
        ),
        (foreign["event_id"],),
    )
    assert absent == (0,)


def test_pa40_the_rendered_literal_is_the_only_tenant_reference(
    m02_ids: Any, pair: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    """The body names its own schema exactly where the declaration has `{schema}`
    (the registry lookup and its qualified object names), and embeds no tenant id:
    the tenant identity is read from shared.tenants, never rendered in."""

    placeholders = _expectation().body.count("{schema}")
    assert placeholders >= 1
    for tenant_id, schema in pair:
        installed = _installed_body(m02_ids, schema)
        assert installed.count(schema) == placeholders, schema
        assert tenant_id not in installed, schema


def test_pa40_control_both_tenants_are_distinct_and_active(
    m02_ids: Any, pair: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    with psycopg.connect(m02_ids.admin, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tenant_id, schema_key, lifecycle_state, schema_version FROM shared.tenants"
            " WHERE schema_key = ANY(%s) ORDER BY schema_key",
            ([pair[0][1], pair[1][1]],),
        ).fetchall()
    assert sorted(rows) == sorted(
        [(tenant_id, schema, "active", 11) for tenant_id, schema in pair]
    )
