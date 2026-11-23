"""Synthetic scenario fixtures and placement (Option B B5). SYNTHETIC ONLY.

Four packaged, read-only fixtures (S1..S4, JSON next to this module), validated against a
strict schema at load. Content is synthetic by construction: canonical fictional phones
(`+1 555 555-01NN`), first names from a fixed list, provider and department refs from fixed
packaged maps (B5 v1 R-B5-1).

Placement (B5 v1r4 §1; owner decision OD-B5-3, Rachel 2026-11-17): the ONE product zone is
America/New_York. A scenario loads only while local Eastern time is in [11:00, 16:00); a
sendable row starts (local date + 2 days) at 18:00 local, S4 (+10 days) never becomes
sendable. `LOAD_ZONES` is read by `sim_actions.load` at call time; only test fixtures replace
it (with a five-zone TEST set, so CI is hour-independent). No src module assigns it after
this definition, and no request, form, CLI or environment input selects it.

Identifiers are uuid5 of (the install's tenant id, scenario, row key): loading a scenario
twice targets the same rows (B5 v1 R-B5-1; the 409 SCENARIO_PRESENT claim, v1r2 §3).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from types import MappingProxyType
from typing import Any, Final
from zoneinfo import ZoneInfo

LOAD_ZONE: Final = "America/New_York"
LOAD_ZONES: tuple[str, ...] = (LOAD_ZONE,)
LOAD_START: Final = time(11, 0)
LOAD_END: Final = time(16, 0)
START_HOUR: Final = 18
SEND_LEAD_DAYS: Final = 2
FAR_LEAD_DAYS: Final = 10
NAMES: Final = ("S1", "S2", "S3", "S4", "E1", "E2", "E3", "E4", "E5")
# Eligibility E4 (v1 D-E4-4): the E scenarios carry a coverage block and load only on an
# eligibility-profile install; the S scenarios are unchanged.
EL_NAMES: Final = ("E1", "E2", "E3", "E4", "E5")

FIRST_NAMES: Final[tuple[str, ...]] = ("Avery", "Blake", "Casey", "Devon", "Emery", "Finley",
                                       "Harper")
PROVIDERS: dict[str, str] = {"prov-syn-1": "Provider A (synthetic)",
                             "prov-syn-2": "Provider B (synthetic)"}
DEPARTMENTS: Final[frozenset[str]] = frozenset({"dept-syn-1"})

_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "haloflow.synthetic.scenarios")
_PHONE: Final = re.compile(r"\+155555501[0-9]{2}")
_KEY: Final = re.compile(r"[a-z][a-z_]{0,31}")
_ROW_FIELDS: Final = frozenset({"key", "first_name", "phone", "provider_ref", "department_ref",
                                "lead_days", "ehr"})
_LEADS: Final = frozenset({SEND_LEAD_DAYS, FAR_LEAD_DAYS})
_MEMBER_ID: Final = re.compile(r"SYN-[A-Z0-9]{1,12}(?:-[A-Z0-9]{1,8})?")
_PAYER_ID: Final = re.compile(r"SYN-PAYER-[A-Z0-9]{1,8}")
_DOB: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_COVERAGE_PRESENT: Final = frozenset({
    "state", "payer_id", "member_id", "relationship", "patient_name", "patient_dob",
    "subscriber_name", "subscriber_dob", "subscriber_member_id"})


class ScenarioInvalid(ValueError):
    """A fixture refused by the strict schema; `field` names the offending field."""

    def __init__(self, field: str) -> None:
        super().__init__(f"scenario field invalid: {field}")
        self.field = field


class OutsideLoadHours(Exception):
    """No zone of the load set is inside its local load hours right now."""


@dataclass(frozen=True)
class Row:
    key: str
    first_name: str
    phone: str
    provider_ref: str
    department_ref: str
    lead_days: int
    ehr: bool
    coverage: Mapping[str, Any] | None = None     # E4: E scenarios only (the E1 block shape)


@dataclass(frozen=True)
class Scenario:
    name: str
    rows: tuple[Row, ...]


def validate(raw: object) -> Scenario:
    """The strict schema (B5 v1 U-F1..F6): exact top-level and row fields, an allowlisted
    name, unique row keys, canonical phones, listed first names, provider and department refs,
    lead days 2 or 10 (an int, never a bool), a boolean EHR flag."""
    if type(raw) is not dict or set(raw) != {"name", "rows"}:
        raise ScenarioInvalid("top")
    name = raw["name"]
    if type(name) is not str or name not in NAMES:
        raise ScenarioInvalid("name")
    rows_raw = raw["rows"]
    if type(rows_raw) is not list or not rows_raw:
        raise ScenarioInvalid("rows")
    rows: list[Row] = []
    for item in rows_raw:
        if type(item) is not dict:
            raise ScenarioInvalid("row")
        fields = _ROW_FIELDS | {"coverage"} if name in EL_NAMES else _ROW_FIELDS
        if set(item) != fields:
            raise ScenarioInvalid("row" if set(item) - fields else
                                  sorted(fields - set(item))[0])
        key = item["key"]
        if type(key) is not str or not _KEY.fullmatch(key):
            raise ScenarioInvalid("key")
        if type(item["first_name"]) is not str or item["first_name"] not in FIRST_NAMES:
            raise ScenarioInvalid("first_name")
        if type(item["phone"]) is not str or not _PHONE.fullmatch(item["phone"]):
            raise ScenarioInvalid("phone")
        if type(item["provider_ref"]) is not str or item["provider_ref"] not in PROVIDERS:
            raise ScenarioInvalid("provider_ref")
        if type(item["department_ref"]) is not str or item["department_ref"] not in DEPARTMENTS:
            raise ScenarioInvalid("department_ref")
        if type(item["lead_days"]) is not int or item["lead_days"] not in _LEADS:
            raise ScenarioInvalid("lead_days")
        if type(item["ehr"]) is not bool:
            raise ScenarioInvalid("ehr")
        rows.append(Row(key=key, first_name=item["first_name"], phone=item["phone"],
                        provider_ref=item["provider_ref"],
                        department_ref=item["department_ref"],
                        lead_days=item["lead_days"], ehr=item["ehr"],
                        coverage=_coverage(item["coverage"]) if name in EL_NAMES else None))
    keys = [r.key for r in rows]
    if len(set(keys)) != len(keys):
        raise ScenarioInvalid("key")
    return Scenario(name=name, rows=tuple(rows))


def _coverage(raw: object) -> Mapping[str, Any]:
    """The E1 coverage block, synthetic values only (E4): pending | none | present (all nine
    keys; strings or null, <= 64 chars; dobs YYYY-MM-DD or null; a synthetic member id and a
    synthetic payer id). Anything else -> ScenarioInvalid("coverage")."""
    if type(raw) is not dict or raw.get("state") not in ("pending", "none", "present"):
        raise ScenarioInvalid("coverage")
    if raw["state"] != "present":
        if set(raw) != {"state"}:
            raise ScenarioInvalid("coverage")
        return MappingProxyType(dict(raw))
    if set(raw) != _COVERAGE_PRESENT:
        raise ScenarioInvalid("coverage")
    for value in raw.values():
        if value is not None and (type(value) is not str or not 1 <= len(value) <= 64):
            raise ScenarioInvalid("coverage")
    for key in ("patient_dob", "subscriber_dob"):
        if raw[key] is not None and not _DOB.fullmatch(raw[key]):
            raise ScenarioInvalid("coverage")
    for key in ("member_id", "subscriber_member_id"):
        if raw[key] is not None and not _MEMBER_ID.fullmatch(raw[key]):
            raise ScenarioInvalid("coverage")
    if raw["payer_id"] is None or not _PAYER_ID.fullmatch(raw["payer_id"]):
        raise ScenarioInvalid("coverage")
    return MappingProxyType(dict(raw))


_FIXTURES: dict[str, Scenario] | None = None


def fixtures() -> dict[str, Scenario]:
    """The four packaged scenarios, validated once, in NAMES order."""
    global _FIXTURES
    if _FIXTURES is None:
        base = resources.files(__name__)
        loaded: dict[str, Scenario] = {}
        for name in NAMES:
            raw: Any = json.loads(base.joinpath(f"{name}.json").read_text(encoding="utf-8"))
            scenario = validate(raw)
            if scenario.name != name:
                raise ScenarioInvalid("name")
            loaded[name] = scenario
        _FIXTURES = loaded
    return dict(_FIXTURES)


def ids(tenant_id: str, scenario: str, key: str) -> tuple[uuid.UUID, uuid.UUID]:
    """(patient_id, appointment_ref): uuid5, deterministic per (tenant, scenario, row key)."""
    stem = f"{tenant_id}/{scenario}/{key}"
    return uuid.uuid5(_NAMESPACE, stem + "/patient"), uuid.uuid5(_NAMESPACE, stem + "/appointment")


def place(now: datetime, zones: Sequence[str]) -> tuple[str, date]:
    """The first zone whose local time of the aware `now` is in [LOAD_START, LOAD_END), and
    that local date. Raises OutsideLoadHours when none qualifies; ValueError for a naive
    `now`."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    for zone in zones:
        local = now.astimezone(ZoneInfo(zone))
        if LOAD_START <= local.time() < LOAD_END:
            return zone, local.date()
    raise OutsideLoadHours


def start_at(zone: str, local_date: date, lead_days: int) -> datetime:
    """(local_date + lead_days) at START_HOUR:00 local in `zone`, as an aware UTC instant."""
    day = local_date + timedelta(days=lead_days)
    return datetime(day.year, day.month, day.day, START_HOUR,
                    tzinfo=ZoneInfo(zone)).astimezone(UTC)


def scenario_state(present: frozenset[str], ehr: frozenset[str], refs: frozenset[str],
                   ehr_backed: frozenset[str]) -> str:
    """B5 v1r3 §2: not_loaded / partial / ehr_lost / loaded for one scenario, from the refs
    present in the database, the refs the simulated EHR holds, the scenario's refs and its
    EHR-backed refs. Only EHR-backed refs are compared with the EHR."""
    here = present & refs
    if not here:
        return "not_loaded"
    if here != refs:
        return "partial"
    if ehr_backed - ehr:
        return "ehr_lost"
    return "loaded"


def visible_ehr(present: frozenset[str], ehr: frozenset[str]) -> frozenset[str]:
    """The EHR refs the page may list: only those also present in the database, so an
    orphan from a rolled-back load never appears (v1r3 §2)."""
    return frozenset(present & ehr)
