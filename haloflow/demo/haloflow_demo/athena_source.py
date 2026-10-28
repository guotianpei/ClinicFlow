"""athenahealth Preview sandbox READ-ONLY appointment source (package N-ATH v1r1).

Owner decisions (Rachel, 2026-10-28): OD-A1 read-only sandbox, write-back stays
SIMULATED; OD-A2 masked patient IDs only, names never fetched; OD-A3 sends stay
simulated (stub).

Requests: exactly one POST (the OAuth token, exact Preview token URL) and GETs
to ``/v1/195900/appointments/booked`` only. No redirects, no retries, 25 s.
Nothing from a response body, an exception or the URL is logged or raised as
text: failures carry a fixed code only.

A snapshot is one fetch per (process, clinic date). The whole fetch is rejected
(``SourceInvalid``) on any page failure, malformed shape or count, a changing
total, a premature short page or a duplicate appointment id: a partial list is
never presented as complete. Individual records that cannot be used are
excluded and counted, never shown.

Times: ``date`` + ``starttime`` are read as local times in CLINIC_TZ;
nonexistent and ambiguous local times are excluded, never resolved.

N-TZ Step 2: before any booked page, the configured department's detail is read
(``GET /v1/195900/departments/{configured id}``) and its ``timezonename`` is
classified against CLINIC_TZ for the target date (``classify_tz``):
VERIFIED (exact recognized IANA name), MISMATCH (recognized IANA name with a
different UTC offset at some instant of the date — the fetch is rejected with
``SOURCE_TZ_MISMATCH``) or UNDETERMINED (everything else; the UNVERIFIED
assumption stays labelled).
"""

from __future__ import annotations

import asyncio
import re
import time as _time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import httpx

from haloflow_demo.settings import DemoSettings, StartupRefused
from haloflow_demo.synthetic_ehr import (
    ApptStatus,
    Fixtures,
    SyntheticAppointment,
    SyntheticPatient,
)

ATHENA_SOURCE = "athena_preview"
HOST = "api.preview.platform.athenahealth.com"
ORIGIN = f"https://{HOST}"
TOKEN_PATH = "/oauth2/v1/token"
PRACTICE = "195900"  # published Preview sandbox (docs.athenahealth.com "Testing in Sandbox")
BOOKED_PATH = f"/v1/{PRACTICE}/appointments/booked"
DEPARTMENT_PATH = f"/v1/{PRACTICE}/departments/{{}}"
TZ_NAME_RE = re.compile(r"^[A-Za-z0-9_/+\-. ]{1,64}$")
TZ_SAMPLE_STEP = timedelta(minutes=15)
VERIFIED, MISMATCH, UNDETERMINED = "VERIFIED", "MISMATCH", "UNDETERMINED"
SCOPE = "athena/service/Athenanet.MDP.*"
PAGE_LIMIT = 100
MAX_PAGES = 5
TIMEOUT_S = 25.0
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Documented meanings (Appointment API, output field ``appointmentstatus``):
# x cancelled, f filled, o open, 2 checked in, 3 checked out, 4 charge entered.
# Only 'f' has been observed in the sandbox (probe r2). 'o' (open slot) is excluded.
STATUS_MAP = {"f": ApptStatus.SCHEDULED, "x": ApptStatus.CANCELLED,
              "2": ApptStatus.CHECKED_IN, "3": ApptStatus.COMPLETED, "4": ApptStatus.COMPLETED}
INITIAL_CONFIRMATION = "1"  # simulated store baseline, as in the synthetic fixtures
EHR_LABEL = "SANDBOX READ / SIMULATED WRITE"


class SourceInvalid(Exception):
    """A whole-fetch rejection or a refused request. Carries a fixed code only."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail          # only ever a recognized IANA zone name


@dataclass(frozen=True)
class AthenaConfig:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    department_id: str


@dataclass(frozen=True)
class Snapshot:
    fixtures: Fixtures = field(repr=False)
    counts: dict[str, int]
    truncated: bool
    taken_at: datetime
    tz_status: str = UNDETERMINED
    dept_tz: str | None = None


def check_athena_mode(s: DemoSettings, env: Mapping[str, str]) -> AthenaConfig | None:
    """None in synthetic mode. In athena_preview mode: the A-1' guard and the
    identity gate; refusals carry a reason only and make no request."""
    if s.ehr_source != ATHENA_SOURCE:
        return None
    if s.sms_mode != "stub":
        raise StartupRefused("athena_requires_stub_sms")
    if s.callback_mode != "simulated":
        raise StartupRefused("athena_requires_simulated_callback")
    if env.get("DEMO_LIVE_TEST", ""):
        raise StartupRefused("athena_live_test_forbidden")
    if not ID_RE.match(s.athena_department_id):
        raise StartupRefused("athena_department")
    base = env.get("ATHENA_BASE_URL", "")
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        raise StartupRefused("athena_gate") from None
    if (parts.scheme != "https" or parts.hostname != HOST or port not in (None, 443)
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise StartupRefused("athena_gate")
    if env.get("ATHENA_PRACTICE_ID", "") != PRACTICE:
        raise StartupRefused("athena_gate")
    cid, secret = env.get("ATHENA_CLIENT_ID", ""), env.get("ATHENA_CLIENT_SECRET", "")
    if not cid or not secret:
        raise StartupRefused("athena_gate")
    return AthenaConfig(client_id=cid, client_secret=secret,
                        department_id=s.athena_department_id)


class AthenaPreviewClient:
    """Guarded transport: token POST + booked GET only (A-2)."""

    def __init__(self, config: AthenaConfig,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._cfg = config
        self._c = httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S,
                                    follow_redirects=False)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._c.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        allowed = ((method == "POST" and path == TOKEN_PATH)
                   or (method == "GET" and path == BOOKED_PATH)
                   or (method == "GET"
                       and path == DEPARTMENT_PATH.format(self._cfg.department_id)))
        if not allowed:
            raise SourceInvalid("DISALLOWED")
        try:
            r = await self._c.request(method, ORIGIN + path, **kw)
        except httpx.HTTPError:
            raise SourceInvalid("NETWORK") from None
        if 300 <= r.status_code < 400:
            raise SourceInvalid("REDIRECT")
        if r.status_code != 200:
            raise SourceInvalid(f"HTTP_{r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise SourceInvalid("BAD_JSON") from None

    async def token(self) -> str:
        async with self._token_lock:
            if self._token is not None and _time.monotonic() < self._token_expires:
                return self._token
            body = await self._request(
                "POST", TOKEN_PATH, auth=(self._cfg.client_id, self._cfg.client_secret),
                data={"grant_type": "client_credentials", "scope": SCOPE})
            tok = body.get("access_token") if isinstance(body, dict) else None
            if not isinstance(tok, str) or not tok:
                raise SourceInvalid("SHAPE")
            life = body.get("expires_in")
            ttl = float(life) if isinstance(life, int) and not isinstance(life, bool) else 300.0
            self._token, self._token_expires = tok, _time.monotonic() + max(ttl - 60.0, 0.0)
            return tok

    async def department(self, token: str) -> Any:
        return await self._request(
            "GET", DEPARTMENT_PATH.format(self._cfg.department_id),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    async def booked_page(self, token: str, target: date, department_id: str,
                          offset: int) -> Any:
        day = target.strftime("%m/%d/%Y")
        return await self._request(
            "GET", BOOKED_PATH,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"startdate": day, "enddate": day, "departmentid": department_id,
                    "limit": str(PAGE_LIMIT), "offset": str(offset),
                    "showpatientdetail": "false", "showinsurance": "false",
                    "showcopay": "false", "showclaimdetail": "false"})


def _iana(name: str | None) -> str | None:
    if name is None or name not in available_timezones():
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def extract_tz(body: Any, department_id: str) -> str | None:
    """The department's bounded ``timezonename`` if it is a recognized IANA zone,
    else None. A malformed body or a conflicting ``departmentid`` rejects the fetch."""
    if isinstance(body, list):
        if len(body) != 1:
            raise SourceInvalid("SOURCE_INVALID")
        body = body[0]
    if not isinstance(body, dict):
        raise SourceInvalid("SOURCE_INVALID")
    if "departmentid" in body:
        dep = body["departmentid"]
        if isinstance(dep, bool) or not isinstance(dep, str | int) or str(dep) != department_id:
            raise SourceInvalid("SOURCE_INVALID")
    name = body.get("timezonename")
    if not isinstance(name, str) or not TZ_NAME_RE.match(name):
        return None
    return _iana(name)


def classify_tz(name: str | None, clinic_tz: str, target: date) -> str:
    """VERIFIED only on exact equality; MISMATCH when the offsets differ at some
    sampled instant of the clinic date (a conservative detector: no observed
    difference never upgrades a different name); otherwise UNDETERMINED."""
    if _iana(name) is None:
        return UNDETERMINED
    assert name is not None
    if name == clinic_tz:
        return VERIFIED
    clinic, dept = ZoneInfo(clinic_tz), ZoneInfo(name)
    start = datetime.combine(target, time(0), tzinfo=clinic).astimezone(UTC)
    end = datetime.combine(target + timedelta(days=1), time(0), tzinfo=clinic).astimezone(UTC)
    t = start
    while t <= end:
        if t.astimezone(clinic).utcoffset() != t.astimezone(dept).utcoffset():
            return MISMATCH
        t += TZ_SAMPLE_STEP
    return UNDETERMINED


def _local_start(d: date, t: time, tz: str) -> tuple[datetime | None, str | None]:
    """UTC start, or an exclusion code for a nonexistent / ambiguous local time."""
    zone = ZoneInfo(tz)
    naive = datetime.combine(d, t)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        return None, "NONEXISTENT_TIME"
    if first.utcoffset() != second.utcoffset():
        return None, "AMBIGUOUS_TIME"
    return first.astimezone(UTC), None


def _map(r: dict[str, Any], target: date, tz: str, department_id: str,
         epoch: int) -> tuple[SyntheticAppointment | None, str | None, str]:
    aid, pid = str(r.get("appointmentid", "")), str(r.get("patientid", ""))
    if not ID_RE.match(aid) or not ID_RE.match(pid):
        return None, "INVALID_ID", pid
    d_raw, t_raw, dept = r.get("date"), r.get("starttime"), r.get("departmentid")
    dm = DATE_RE.match(d_raw) if isinstance(d_raw, str) else None
    tm = TIME_RE.match(t_raw) if isinstance(t_raw, str) else None
    dept_ok = (isinstance(dept, str) or (isinstance(dept, int) and not isinstance(dept, bool)))
    if dm is None or tm is None or not dept_ok:
        return None, "MALFORMED_RECORD", pid
    try:
        d = date(int(dm.group(3)), int(dm.group(1)), int(dm.group(2)))
    except ValueError:
        return None, "MALFORMED_RECORD", pid
    status = r.get("appointmentstatus")
    if status == "o":
        return None, "OPEN_SLOT", pid
    if not isinstance(status, str) or status not in STATUS_MAP:
        return None, "UNDOCUMENTED_STATUS", pid
    if d != target:
        return None, "WRONG_DATE", pid
    if str(dept) != department_id:
        return None, "WRONG_DEPARTMENT", pid
    start, why = _local_start(d, time(int(tm.group(1)), int(tm.group(2))), tz)
    if start is None:
        return None, why, pid
    return SyntheticAppointment(f"ath-e{epoch}-{aid}", f"athp-{pid}", start,
                                STATUS_MAP[status], INITIAL_CONFIRMATION), None, pid


async def fetch_snapshot(client: AthenaPreviewClient, target: date, tz: str,
                         department_id: str, epoch: int) -> Snapshot:
    token = await client.token()
    dept_tz = extract_tz(await client.department(token), department_id)
    tz_status = classify_tz(dept_tz, tz, target)
    if tz_status == MISMATCH:
        raise SourceInvalid("SOURCE_TZ_MISMATCH", detail=dept_tz)
    records: list[dict[str, Any]] = []
    total: int | None = None
    truncated = False
    for page in range(MAX_PAGES):
        body = await client.booked_page(token, target, department_id, page * PAGE_LIMIT)
        if not isinstance(body, dict) or not isinstance(body.get("appointments"), list):
            raise SourceInvalid("SOURCE_INVALID")
        tc = body.get("totalcount")
        if type(tc) is not int or tc < 0:
            raise SourceInvalid("SOURCE_INVALID")
        if total is None:
            total = tc
        elif tc != total:
            raise SourceInvalid("SOURCE_INVALID")     # count changed between pages
        items = body["appointments"]
        if len(items) > PAGE_LIMIT:
            raise SourceInvalid("SOURCE_INVALID")     # F-ATH-I2: oversized page
        if not all(isinstance(x, dict) for x in items):
            raise SourceInvalid("SOURCE_INVALID")
        records.extend(items)
        if len(records) > total:
            raise SourceInvalid("SOURCE_INVALID")
        if len(records) == total:
            break
        if len(items) < PAGE_LIMIT:
            raise SourceInvalid("SOURCE_INVALID")     # premature short/empty page
    else:
        truncated = total is not None and len(records) < total
    seen: set[str] = set()
    for r in records:
        key = str(r.get("appointmentid"))
        if key in seen:
            raise SourceInvalid("SOURCE_INVALID")     # duplicate/conflicting id
        seen.add(key)
    counts: dict[str, int] = {}
    appointments: list[SyntheticAppointment] = []
    patients: dict[str, SyntheticPatient] = {}
    for r in records:
        appt, why, pid = _map(r, target, tz, department_id, epoch)
        if appt is None:
            assert why is not None
            counts[why] = counts.get(why, 0) + 1
            continue
        appointments.append(appt)
        patients.setdefault(appt.emr_patient_id, SyntheticPatient(
            appt.emr_patient_id, f"Sandbox patient …{pid[-4:]}", "sandbox"))
    return Snapshot(Fixtures(patients=patients, appointments=appointments), counts,
                    truncated, datetime.now(tz=UTC), tz_status, dept_tz)
