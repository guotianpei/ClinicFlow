#!/usr/bin/env python3
"""N-TZ Step 1 — bounded read of the sandbox department's timezone metadata.

Package v1 + Codex review (codex_n-tz-v1-review.md). Read-only, Preview sandbox
practice 195900 only. Exactly 3 requests, no retries, no redirects:
  1. POST /oauth2/v1/token
  2. GET  /v1/195900/departments               (DEMO_ATHENA_DEPARTMENT_ID must be listed;
                                                if unset, the first valid id; memory only)
  3. GET  /v1/195900/departments/{id}
Records ONLY four administrative fields of that department — ``timezonename``,
``timezone``, ``timezoneoffset``, ``doesnotobservedst`` — each bounded by type
and size; anything else (or an out-of-bounds value) becomes a fixed marker.
No raw body, id, token or secret is written or printed.

Classification (CLINIC_TZ from the env, default America/New_York):
  VERIFIED                 timezonename is a recognised IANA zone exactly equal to CLINIC_TZ
  UNDETERMINED_OTHER_IANA  recognised IANA zone, different spelling (may be an alias)
  UNDETERMINED_NON_IANA    present but not a recognised IANA zone (e.g. a display name)
  UNDETERMINED_MISSING     timezonename absent / null / rejected
Offset/DST values are recorded as context only; they never establish VERIFIED.

Usage (haloflow venv): python athena_department_tz_probe.py --env-file .env --out <dir>
Exit 0 completed (any classification), 1 stopped (fixed code), 2 gate refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import httpx

HOST = "api.preview.platform.athenahealth.com"
PRACTICE = "195900"
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_/+\-. ]{1,64}$")
MAX_REQUESTS = 3
REJECTED = "REJECTED_VALUE"


class Stop(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _allowed(method: str, path: str) -> bool:
    if method == "POST":
        return path == "/oauth2/v1/token"
    if method != "GET":
        return False
    if path == f"/v1/{PRACTICE}/departments":
        return True
    m = re.fullmatch(rf"/v1/{PRACTICE}/departments/([^/]+)", path)
    return bool(m and ID_RE.match(m.group(1)))


class Guarded(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, log: list[dict[str, Any]]) -> None:
        self._inner, self._log, self.used = inner, log, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != HOST or not _allowed(request.method, request.url.path):
            raise Stop("DISALLOWED")
        if self.used >= MAX_REQUESTS:
            raise Stop("BUDGET")
        self.used += 1
        resp = await self._inner.handle_async_request(request)
        cat = ("token" if request.url.path.endswith("/token") else
               "departments" if request.url.path.endswith("/departments") else "department")
        self._log.append({"method": request.method, "category": cat, "status": resp.status_code})
        return resp

    async def aclose(self) -> None:
        await self._inner.aclose()


def gate(env: Mapping[str, str]) -> None:
    try:
        p = urlsplit(env.get("ATHENA_BASE_URL", ""))
        port = p.port
    except ValueError:
        raise Stop("GATE") from None
    want = env.get("DEMO_ATHENA_DEPARTMENT_ID", "")
    if want and not ID_RE.match(want):
        raise Stop("GATE")
    if (p.scheme != "https" or p.hostname != HOST or port not in (None, 443)
            or p.username or p.password or p.path not in ("", "/") or p.query or p.fragment
            or env.get("ATHENA_PRACTICE_ID") != PRACTICE
            or not env.get("ATHENA_CLIENT_ID") or not env.get("ATHENA_CLIENT_SECRET")):
        raise Stop("GATE")


def _num(v: Any) -> Any:
    if isinstance(v, bool):
        return REJECTED
    if isinstance(v, int | float) and abs(v) <= 14:
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,2}(\.\d{1,2})?", v) and abs(float(v)) <= 14:
        return v
    return REJECTED


def _flag(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower()
    return REJECTED


def extract(dept: Any) -> dict[str, Any]:
    """The four bounded fields (value or fixed marker) plus presence/type."""
    if isinstance(dept, list):
        if len(dept) != 1 or not isinstance(dept[0], dict):
            raise Stop("SHAPE")
        dept = dept[0]
    if not isinstance(dept, dict):
        raise Stop("SHAPE")
    out: dict[str, Any] = {}
    for key, check in (("timezonename", None), ("timezone", _num),
                       ("timezoneoffset", _num), ("doesnotobservedst", _flag)):
        if key not in dept:
            out[key] = {"present": False}
            continue
        v = dept[key]
        if check is None:
            val = v if isinstance(v, str) and NAME_RE.match(v) else REJECTED
        else:
            val = check(v)
        out[key] = {"present": True, "json_type": type(v).__name__ if v is not None else "null",
                    "value": None if v is None else val}
    return out


def classify(fields: dict[str, Any], clinic_tz: str) -> str:
    tzn = fields.get("timezonename", {})
    name = tzn.get("value") if tzn.get("present") else None
    if not isinstance(name, str) or name == REJECTED:
        return "UNDETERMINED_MISSING"
    if name not in available_timezones():
        return "UNDETERMINED_NON_IANA"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UNDETERMINED_NON_IANA"
    return "VERIFIED" if name == clinic_tz else "UNDETERMINED_OTHER_IANA"


async def probe(env: Mapping[str, str],
                transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
                ) -> tuple[int, dict[str, Any]]:
    log: list[dict[str, Any]] = []
    out: dict[str, Any] = {"probe": "athena_department_tz_probe v1",
                           "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "clinic_tz": env.get("CLINIC_TZ") or "America/New_York",
                           "requests": log}
    try:
        gate(env)
    except Stop:
        out["result"] = "GATE_REFUSED"
        return 2, out
    t = Guarded(transport_factory() if transport_factory else httpx.AsyncHTTPTransport(), log)
    rc = 0
    try:
        async with httpx.AsyncClient(transport=t, timeout=25.0, follow_redirects=False) as c:
            base = f"https://{HOST}"

            async def call(method: str, path: str, **kw: Any) -> Any:
                try:
                    r = await c.request(method, base + path, **kw)
                except httpx.HTTPError:
                    raise Stop("NETWORK") from None
                if r.status_code != 200:
                    raise Stop(f"HTTP_{r.status_code}")
                try:
                    return r.json()
                except ValueError:
                    raise Stop("BAD_JSON") from None

            tok = (await call("POST", "/oauth2/v1/token",
                              auth=(env["ATHENA_CLIENT_ID"], env["ATHENA_CLIENT_SECRET"]),
                              data={"grant_type": "client_credentials",
                                    "scope": "athena/service/Athenanet.MDP.*"}))
            token = tok.get("access_token") if isinstance(tok, dict) else None
            if not isinstance(token, str) or not token:
                raise Stop("SHAPE")
            hdr = {"Authorization": f"Bearer {token}"}
            deps = await call("GET", f"/v1/{PRACTICE}/departments", headers=hdr)
            lst = deps.get("departments") if isinstance(deps, dict) else None
            ids = [str(d.get("departmentid")) for d in (lst if isinstance(lst, list) else [])
                   if isinstance(d, dict) and ID_RE.match(str(d.get("departmentid", "")))]
            if not ids:
                raise Stop("NO_DEPARTMENT")
            want = env.get("DEMO_ATHENA_DEPARTMENT_ID", "")
            if want:
                if want not in ids:
                    raise Stop("DEPARTMENT_NOT_LISTED")
                chosen, out["department_source"] = want, "configured"
            else:
                chosen, out["department_source"] = ids[0], "first_listed"
            dept = await call("GET", f"/v1/{PRACTICE}/departments/{chosen}", headers=hdr)
            fields = extract(dept)
            out["fields"] = fields
            out["classification"] = classify(fields, out["clinic_tz"])
            out["result"] = "COMPLETED"
    except Stop as s:
        out["result"] = s.code
        rc = 1
    except Exception:  # noqa: BLE001 — never print exception text
        out["result"] = "INTERNAL"
        rc = 1
    out["request_count"] = t.used
    return rc, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    from dotenv import dotenv_values
    env = {k: v for k, v in dotenv_values(a.env_file).items() if v is not None}
    rc, out = asyncio.run(probe(env))
    a.out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, indent=2, sort_keys=True)
    (a.out / "department-tz.json").write_text(text + "\n")
    print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
