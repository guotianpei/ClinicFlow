"""L-1S package S-A — static rows (no database). Interface pins v1 sections 1, 2, 6, 8.

- SA-S01  CONTROL (green on main): production is unchanged by S-A — supported
          versions {36}, no t037..t042 in the production registry, no
          `m05.intake.*` key in the production catalogue, `haloflow.composition`
          names no S-A module.
- SA-S02  the S-A unit set: exact ids in order; t037 an ordinary migrator unit;
          t038..t042 typed units run as the M05 owner; the S-A registry target 42.
- SA-S03  runtime gateways t038..t041: SECURITY DEFINER, VOLATILE, PARALLEL UNSAFE,
          not strict, jsonb -> jsonb, pinned search_path, EXECUTE to the runtime only.
- SA-S04  t042 activation functions: EXECUTE to NO role but the owner (empty ACL
          declaration: no runtime, no PUBLIC); no statement key anywhere (D-S1
          "enable none"; restricted execution).
- SA-S05  SA_STATEMENTS equals the pinned literal; every key WRITE; the S-A test
          catalogue derives the four capabilities as write capabilities.
- SA-S06  exact-boundary rule (Codex 2155 item 1): t040 and t041 compare time to a
          deadline ONLY through `intake_reply_is_timely(`; no bare comparison of
          `deadline` in either body.
- SA-S07  R-SA-10 static half: the S-A Python modules import no SMS/EHR adapter and
          no network library.
- SA-S08  public command types carry no trusted field (tenant, patient, principal,
          attribution, phone-derived identity) and malformed values raise
          `CommandMalformed` at construction, before any gateway is reachable.

RED TRANCHE: SA-S02..S08 fail until `haloflow.m05.intake_units`,
`haloflow.m05.intake_statements` and `haloflow.m05.intake` exist.
"""

from __future__ import annotations

import ast
import re
import uuid
from dataclasses import fields
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

import pytest
import sa_support as sa

SRC = Path(__file__).resolve().parents[2] / "src" / "haloflow"
SA_MODULES = ("m05/intake.py", "m05/intake_units.py", "m05/intake_statements.py")
RUNTIME_UNITS = tuple(sa.RUNTIME_FUNCTIONS)


def _unit(migration_id: str) -> Any:
    units = sa.sa_units()
    return units[migration_id]


# --- SA-S01 (control) ---------------------------------------------------------------


def test_sa_s01_production_unchanged() -> None:
    from haloflow.composition import (
        APPROVED_SUPPORTED_SCHEMA_VERSIONS,
        build_production_catalog,
        build_production_tenant_migrations,
    )

    assert frozenset({sa.PRODUCTION_TARGET}) == APPROVED_SUPPORTED_SCHEMA_VERSIONS
    registry = build_production_tenant_migrations()
    assert registry.target_version == sa.PRODUCTION_TARGET
    assert not set(registry.migration_ids) & set(sa.SA_UNIT_IDS)
    keys = build_production_catalog().catalog.keys()
    assert not [k for k in keys if k.startswith("m05.intake")]
    text = (SRC / "composition.py").read_text()
    assert "intake_units" not in text and "intake_statements" not in text


# --- SA-S02 ---------------------------------------------------------------------------


def test_sa_s02_unit_set_and_registry() -> None:
    units = sa.sa_units()
    assert tuple(units) == sa.SA_UNIT_IDS
    tables = units["t037_m05_intake_tables"]
    assert isinstance(tables, str) or getattr(tables, "kind", None) is None
    assert getattr(tables, "execution_role", None) is None  # migrator by absence
    from haloflow.m01.provisioning.units import TYPED_FUNCTION_KIND

    for migration_id in sa.SA_UNIT_IDS[1:]:
        unit = units[migration_id]
        assert unit.kind == TYPED_FUNCTION_KIND, migration_id
        assert unit.execution_role == sa.M05_OWNER, migration_id
    assert set(sa.sa_profiles()) == set(sa.SA_UNIT_IDS[1:])
    registry = sa.sa_registry()
    assert registry.target_version == sa.SA_TARGET
    assert registry.migration_ids[-6:] == sa.SA_UNIT_IDS


# --- SA-S03 / SA-S04 ---------------------------------------------------------------------


def _policy_functions(migration_id: str) -> list[dict[str, Any]]:
    policy = _unit(migration_id).policy
    assert policy is not None, migration_id
    functions: list[dict[str, Any]] = list(policy["functions"])
    return functions


@pytest.mark.parametrize("migration_id", RUNTIME_UNITS)
def test_sa_s03_runtime_gateway_declarations(migration_id: str) -> None:
    (function,) = _policy_functions(migration_id)
    name = sa.RUNTIME_FUNCTIONS[migration_id][0]
    assert function["name"] == name
    assert [i["type"] for i in function["inputs"]] == ["jsonb"]
    assert function["return_type"] == "jsonb"
    assert function["security_definer"] is True
    assert function["volatility"] == "volatile"
    assert function["parallel"] == "unsafe"
    assert function["strict"] is False
    assert function["config"] == ["search_path=pg_catalog, {schema}, pg_temp"]
    assert function["acl"] == [{"grantee": sa.RUNTIME, "privileges": ["EXECUTE"]}]
    profile = sa.sa_profiles()[migration_id]
    assert profile.installed_acl == sa.FUNCTION_ACL_RUNTIME
    assert profile.owner == sa.M05_OWNER and profile.security_definer is True


def test_sa_s04_activation_functions_owner_only() -> None:
    functions = _policy_functions("t042_m05_activation")
    assert sorted(f["name"] for f in functions) == sorted(
        name for name, _, _ in sa.ACTIVATION_FUNCTIONS
    )
    for function in functions:
        assert function["acl"] == [], function["name"]  # no runtime, no PUBLIC
        assert function["security_definer"] is True
    from haloflow.composition import build_production_catalog

    queries = [s.query for s in sa.sa_catalog().catalog.statements()]
    queries += [s.query for s in build_production_catalog().catalog.statements()]
    assert not [qy for qy in queries if "m05_activation" in qy]


# --- SA-S05 ---------------------------------------------------------------------------------


def test_sa_s05_statements_exact() -> None:
    statements = sa.sa_statements()
    assert {k: (m.value, c, qy) for k, (m, c, qy) in statements.items()} == sa.STATEMENTS
    catalog = sa.sa_catalog()
    for key in sa.STATEMENTS:
        assert catalog.catalog.resolve(key).mode.value == "write"
    assert {c for _, c, _ in sa.STATEMENTS.values()} <= set(catalog.write_capabilities)


# --- SA-S06 ---------------------------------------------------------------------------------

_BARE_DEADLINE = re.compile(r"(deadline\s*(<=|>=|<|>|=|<>))|((<=|>=|<|>|=|<>)\s*\w*\.?deadline)")


@pytest.mark.parametrize("migration_id", ("t040_m05_register_inbound", "t041_m05_close_window"))
def test_sa_s06_boundary_only_through_predicate(migration_id: str) -> None:
    template = _unit(migration_id).template
    body = template.split("$body$")[1]
    assert f"{sa.HELPER_FUNCTION}(" in body
    # `deadline = sent_at + interval` lives in t039/t037, never in these bodies.
    hits = [m.group(0) for m in _BARE_DEADLINE.finditer(body)]
    assert not hits, hits


# --- SA-S07 ---------------------------------------------------------------------------------

FORBIDDEN_IMPORT_PREFIXES = (
    "haloflow.modules", "haloflow.integrations", "httpx", "requests", "aiohttp", "urllib",
    "http", "socket", "smtplib", "notifyre", "twilio",
)


@pytest.mark.parametrize("relative", SA_MODULES)
def test_sa_s07_no_sms_ehr_or_network_imports(relative: str) -> None:
    path = SRC / relative
    assert path.exists(), relative
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__":
            names.append("<dynamic>")
    bad = [n for n in names if n == "<dynamic>"
           or any(n == p or n.startswith(p + ".") for p in FORBIDDEN_IMPORT_PREFIXES)]
    assert not bad, bad
    assert importlib_util.find_spec("haloflow.m05.intake") is not None


# --- SA-S08 ---------------------------------------------------------------------------------

TRUSTED = {"tenant", "tenant_id", "patient", "patient_id", "principal", "principal_id",
           "attribution", "actor", "actor_id", "workload_id", "received_at", "sent_at",
           "deadline", "digest", "fingerprint"}
U = uuid.UUID("00000000-0000-4000-8000-00000005c001")


def test_sa_s08_command_fields() -> None:
    from haloflow.m05.intake import DispatchCommand, InboundCommand

    assert {f.name for f in fields(DispatchCommand)} == {"reminder_id", "appointment_ref"}
    assert {f.name for f in fields(InboundCommand)} == {
        "channel", "provider_message_id", "phone", "reply_class",
    }
    for cls in (DispatchCommand, InboundCommand):
        assert not {f.name for f in fields(cls)} & TRUSTED


MALFORMED_DISPATCH: dict[str, dict[str, Any]] = {
    "reminder-str": {"reminder_id": str(U)},
    "appointment-none": {"appointment_ref": None},
}
MALFORMED_INBOUND: dict[str, dict[str, Any]] = {
    "channel-voice": {"channel": "voice"},
    "message-empty": {"provider_message_id": ""},
    "message-too-long": {"provider_message_id": "x" * 129},
    "phone-not-e164": {"phone": "5550100001"},
    "phone-letters": {"phone": "+1555CALLME"},
    "class-unknown": {"reply_class": "yes"},
    "class-upper": {"reply_class": "STOP"},
    "class-not-str": {"reply_class": 1},
}


@pytest.mark.parametrize("case", sorted(MALFORMED_DISPATCH))
def test_sa_s08_dispatch_malformed(case: str) -> None:
    from haloflow.m05.intake import CommandMalformed, DispatchCommand

    values: dict[str, Any] = {"reminder_id": U, "appointment_ref": U}
    values.update(MALFORMED_DISPATCH[case])
    with pytest.raises(CommandMalformed):
        DispatchCommand(**values)


@pytest.mark.parametrize("case", sorted(MALFORMED_INBOUND))
def test_sa_s08_inbound_malformed(case: str) -> None:
    from haloflow.m05.intake import CommandMalformed, InboundCommand

    values: dict[str, Any] = {"channel": "sms", "provider_message_id": "SM-1",
                              "phone": "+15550100001", "reply_class": "confirm"}
    values.update(MALFORMED_INBOUND[case])
    with pytest.raises(CommandMalformed):
        InboundCommand(**values)


def test_sa_s08_digest_vectors() -> None:
    parts, preimage, digest = sa.DISPATCH_VECTOR
    assert sa._compact(["m05.dispatch/v1", *parts]) == preimage
    assert sa.dispatch_fingerprint(*parts).hex() == digest
    for parts_i, preimage_i, digest_i in sa.INBOUND_VECTORS:
        assert sa._compact(["m05.inbound/v1", *parts_i]) == preimage_i
        assert sa.inbound_fingerprint(*parts_i).hex() == digest_i
