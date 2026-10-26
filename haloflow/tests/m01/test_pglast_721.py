"""pglast 7.17 -> 7.21 repair (D-1a). Tests written before the change.

Package: Shared Workspace ``Work Session 2026-10-26/claude_pglast-721-package-2026-10-26-v1.md``,
D-1a retained by Codex (codex_pglast-exploration-review.md). Owner scope:
claude_owner-decision-pglast-scope-2026-10-26.md.

pglast 7.17 is no longer obtainable from PyPI. 7.21 (libpg_query 17-6.2.5) adds one
field to one AST node the function-policy validator pins exactly: ``DefElem`` gains
``arg_location``, a ``ParseLoc`` source position (pglast 7.21 ``ast.py``: slot type
'ParseLoc', and listed with ``location`` in ``_ATTRS_TO_IGNORE_IN_COMPARISON``).

D-1a: the declared policy identity moves to ('pglast', '7.21', 17). The T003 unit's
function-v3 checksum therefore changes; a tenant whose ledger holds the 7.17-era
checksum is refused as drift (see tests/m02/test_pglast_721_t003_postgres.py).
No PostgreSQL needed here.
"""

from __future__ import annotations

import copy
import importlib.metadata
import json
import types
from pathlib import Path
from typing import Any

import pglast
import pytest

from haloflow.m01.errors import MigrationUnitRejected
from haloflow.m01.provisioning import function_policy as fp
from haloflow.m01.provisioning import ordinary_content as oc
from haloflow.m01.provisioning.function_checksum import function_checksum

ROOT = Path(__file__).parent / "fixtures/function_policy"
SCHEMA = json.loads((ROOT / "cases.json").read_text())["schema_key"]

PINNED = "7.21"
GRAMMAR = 17
DEFELEM_SLOTS = {"defnamespace", "defname", "arg", "defaction", "location", "arg_location"}

# T003 identities. OLD is the value main 0367601 records (7.17 declaration); NEW is the
# same declaration with only parser_version = "7.21", computed with the unchanged
# function-v3 checksum before any source change on this branch.
T003_OLD_CHECKSUM = "fff7b9cebb6cf85d8327535d27934163ed3b7375a2064f82857dc9e9af5e2a6d"
T003_NEW_CHECKSUM = "6adea880c1cd8ee54028e520348427ec19b173a0ce6d36854ac2008488289843"
_CHECKSUM_KEYS = ("migration_id", "template", "execution_role", "verification", "policy")


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _control(identifier: str = "C01") -> dict[str, Any]:
    return json.loads((ROOT / "controls" / f"{identifier}.json").read_text())  # type: ignore[no-any-return]


def _fake_version(value: str) -> Any:
    real = importlib.metadata.version

    def version(name: str) -> str:
        return value if name == "pglast" else real(name)

    return version


# ── PGT-3 / PGT-4: the runtime parser pin ────────────────────────────────────


def test_pgl01_installed_parser_is_the_pinned_one() -> None:
    assert importlib.metadata.version("pglast") == PINNED
    assert pglast.get_postgresql_version()[0] == GRAMMAR


def test_pgl02_function_policy_loads_the_pinned_parser() -> None:
    assert fp._load_parser() is not None


@pytest.mark.parametrize("version", ["7.17", "7.21.1", "7.2", "7.210", "8.7", ""])
def test_pgl03_function_policy_refuses_any_other_parser_version(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", _fake_version(version))
    with pytest.raises(MigrationUnitRejected) as caught:
        fp._load_parser()
    assert caught.value.reason_code == "INSTALL_PARSER_VERSION_MISMATCH"


@pytest.mark.parametrize("version", ["7.17", "7.21.1", "8.7"])
def test_pgl04_ordinary_content_refuses_any_other_parser_version(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", _fake_version(version))
    with pytest.raises(MigrationUnitRejected):
        oc._load_parser()


def test_pgl05_ordinary_content_pin_is_7_21() -> None:
    assert oc._PINNED_PGLAST_VERSION == PINNED
    assert oc._PINNED_GRAMMAR_MAJOR == GRAMMAR


# ── F-1: DefElem shape ───────────────────────────────────────────────────────


def test_pgl06_installed_defelem_shape_is_exactly_the_pinned_shape() -> None:
    assert set(pglast.ast.DefElem.__slots__) == DEFELEM_SLOTS
    assert set(fp._SLOTS["DefElem"].split()) == DEFELEM_SLOTS


def test_pgl07_arg_location_is_position_metadata_only() -> None:
    """Installed-source facts: slot type ParseLoc, ignored in comparisons like location;
    parsed values are integers."""
    assert pglast.ast.DefElem.__slots__["arg_location"] == "ParseLoc"
    assert "arg_location" in pglast.ast.DefElem._ATTRS_TO_IGNORE_IN_COMPARISON
    sql = _control()["template"].replace("{schema}", SCHEMA)
    seen = 0
    for raw in pglast.parse_sql(sql):
        stmt = raw.stmt
        for opt in getattr(stmt, "options", None) or ():
            if isinstance(opt, pglast.ast.DefElem):
                assert isinstance(opt.arg_location, int)
                seen += 1
    assert seen > 0


def _decl() -> Any:
    return types.SimpleNamespace(parser=types.SimpleNamespace(ast=pglast.ast))


def test_pgl08_validator_admits_a_real_7_21_defelem() -> None:
    node = pglast.ast.DefElem(defname="language", arg=pglast.ast.String("plpgsql"))
    assert fp._node(node, "DefElem", _decl()) is node


@pytest.mark.parametrize("slots", [
    ("defnamespace", "defname", "arg", "defaction", "location"),                    # 7.17 shape
    ("defnamespace", "defname", "arg", "defaction", "location", "arg_location",
     "unexpected"),                                                                  # extra field
])
def test_pgl09_validator_refuses_any_other_defelem_shape(slots: tuple[str, ...]) -> None:
    fake_cls = type("DefElem", (), {"__slots__": slots})
    decl = types.SimpleNamespace(parser=types.SimpleNamespace(ast=types.SimpleNamespace(
        DefElem=fake_cls)))
    with pytest.raises(MigrationUnitRejected) as caught:
        fp._node(fake_cls(), "DefElem", decl)
    assert caught.value.reason_code == "INSTALL_AST_INVALID"


# ── D-1a: declared policy identity ───────────────────────────────────────────


def test_pgl10_a_7_17_declaration_is_now_refused_as_policy_invalid() -> None:
    payload = _control()
    payload["policy"]["parser_version"] = "7.17"
    with pytest.raises(MigrationUnitRejected) as caught:
        fp.validate_function_installation(_encode(payload), schema_key=SCHEMA)
    assert caught.value.reason_code == "INSTALL_POLICY_INVALID"


def test_pgl11_every_control_now_declares_7_21_and_is_admitted() -> None:
    for identifier in ("C01", "C02", "C03", "C04", "C05", "C06"):
        payload = _control(identifier)
        assert payload["policy"]["parser_version"] == PINNED
        fp.validate_function_installation(_encode(payload), schema_key=SCHEMA)


def test_pgl12_t003_declares_7_21_with_the_new_checksum() -> None:
    from haloflow.composition import build_production_tenant_migrations
    from haloflow.m02.units import T003_DEFINITION, T003_MIGRATION_ID

    assert T003_DEFINITION.policy is not None
    assert T003_DEFINITION.policy["parser_version"] == PINNED
    unit = [u for u in build_production_tenant_migrations().units
            if u.migration_id == T003_MIGRATION_ID][0]
    assert unit.checksum == T003_NEW_CHECKSUM
    old = copy.deepcopy(unit.declaration_payload)
    old["policy"]["parser_version"] = "7.17"
    assert function_checksum(**{k: old[k] for k in _CHECKSUM_KEYS}) == T003_OLD_CHECKSUM
    assert T003_OLD_CHECKSUM != T003_NEW_CHECKSUM
