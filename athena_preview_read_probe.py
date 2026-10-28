#!/usr/bin/env python3
"""athenahealth Preview READ probe — N-EHR package v1r1 (Codex ALIGN 2026-10-28).

Reads only, on the published Preview sandbox (practice 195900). Exactly one POST
(the OAuth token request); every other request is a GET to an allowlisted path.
Output is shape-only: field names/types from documented key sets, counts and
fixed outcome codes. No returned value, identifier, name, token, error body or
URL is printed or written. Raw payloads are never written to disk.

Usage (from the haloflow venv, with the local .env holding ATHENA_* settings):
    python athena_preview_read_probe.py --env-file <path to haloflow/.env> --out <dir>
Exit codes: 0 completed (see outcome classes), 1 request/shape failure,
2 identity/configuration gate refused (zero requests sent).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx

HOST = "api.preview.platform.athenahealth.com"
ORIGIN = f"https://{HOST}"
TOKEN_URL = ORIGIN + "/oauth2/v1/token"
PRACTICE = "195900"  # published Preview sandbox (docs.athenahealth.com "Testing in Sandbox")
SCOPE = "athena/service/Athenanet.MDP.*"
MAX_REQUESTS = 8
MAX_SINGLE = 3
WINDOW_DAYS = 14
TIMEOUT_S = 25.0
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
_ID = r"[1-9][0-9]{0,11}"
ALLOWED_GET = tuple(re.compile(p) for p in (
    rf"^/v1/{PRACTICE}/departments$",
    rf"^/v1/{PRACTICE}/appointments/booked$",
    rf"^/v1/{PRACTICE}/appointments/{_ID}$",
    rf"^/v1/{PRACTICE}/appointments/{_ID}/confirmationstatus$",
    rf"^/v1/{PRACTICE}/reference/appointmentconfirmationstatus$",
))
BOOKED_PARAMS_FIXED = {"limit": "50", "showpatientdetail": "false", "showinsurance": "false",
                       "showcopay": "false", "showclaimdetail": "false"}
STATUS_CODES = ("x", "f", "o", "2", "3", "4")  # documented appointmentstatus values
# Documented top-level output fields of GET /v1/{practiceid}/appointments/{appointmentid}
# (docs.athenahealth.com/api/api-ref/appointment, read 2026-10-28). Other keys are
# counted under UNDOCUMENTED_KEY and never named.
DOC_APPT_KEYS = frozenset((
    "appointmentcopay appointmentid appointmentmessage appointmentstatus appointmenttype "
    "appointmenttypeid chargeentrynotrequired chargeentrynotrequiredreason claims copay "
    "customfields date departmentid duration encounterid encounterprep encounterstate "
    "encounterstatus frozen hl7providerid insurances nativeathenatelehealth patient "
    "patientappointmenttypename patientid patientlocationid providerid referringproviderid "
    "renderingproviderid rescheduledappointmentid startcheckin starttime stopcheckin "
    "stopexamdatetime stopsignoff supervisingproviderid urgent useexpectedprocedurecodes "
    "visitid").split())
DOC_CONFIRMATION_KEYS = frozenset(("confirmationname", "shortname"))
MATCH_KEYS = ("appointmentid", "patientid", "departmentid", "date", "starttime")
UNDOCUMENTED = "UNDOCUMENTED"


class GateRefused(Exception):
    """Identity/configuration gate: no request may be sent."""


class Stop(Exception):
    """A fixed outcome code; carries no data from the response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int | float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "other"


def gate(env: Mapping[str, str]) -> tuple[str, str]:
    """E-1: refuse unless the base URL is exactly the Preview origin and the
    practice is the published sandbox. Returns (client_id, client_secret)."""
    base = env.get("ATHENA_BASE_URL", "")
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError as exc:
        raise GateRefused("base_url") from exc
    if (parts.scheme != "https" or parts.hostname != HOST or port not in (None, 443)
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise GateRefused("base_url")
    practice = env.get("ATHENA_PRACTICE_ID", "")
    if not ID_RE.match(practice) or practice != PRACTICE:
        raise GateRefused("practice")
    cid, secret = env.get("ATHENA_CLIENT_ID", ""), env.get("ATHENA_CLIENT_SECRET", "")
    if not cid or not secret:
        raise GateRefused("credentials_missing")
    return cid, secret


class GuardedClient:
    """Allowlist, method guard, request budget, no redirects (E-2, E-5)."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._c = httpx.Client(transport=transport, timeout=TIMEOUT_S, follow_redirects=False)
        self.count = 0
        self.log: list[dict[str, Any]] = []

    def close(self) -> None:
        self._c.close()

    def _send(self, name: str, method: str, url: str, **kw: Any) -> httpx.Response:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != HOST or parts.query or parts.fragment:
            raise Stop("DISALLOWED_URL")
        if method == "POST":
            if url != TOKEN_URL:
                raise Stop("DISALLOWED_METHOD")
        elif method == "GET":
            if not any(p.match(parts.path) for p in ALLOWED_GET):
                raise Stop("DISALLOWED_URL")
        else:
            raise Stop("DISALLOWED_METHOD")
        if self.count >= MAX_REQUESTS:
            raise Stop("BUDGET")
        self.count += 1
        try:
            r = self._c.request(method, url, **kw)
        except httpx.HTTPError:
            self.log.append({"name": name, "http_status": None, "outcome": "NETWORK"})
            raise Stop("NETWORK") from None
        if 300 <= r.status_code < 400:
            self.log.append({"name": name, "http_status": r.status_code, "outcome": "REDIRECT"})
            raise Stop("REDIRECT")
        if r.status_code != 200:
            code = f"HTTP_{r.status_code}"
            self.log.append({"name": name, "http_status": r.status_code, "outcome": code})
            raise Stop(code)
        self.log.append({"name": name, "http_status": 200, "outcome": "OK"})
        return r

    def token(self, cid: str, secret: str) -> str:
        r = self._send("token", "POST", TOKEN_URL, auth=(cid, secret),
                       data={"grant_type": "client_credentials", "scope": SCOPE})
        body = _json(r)
        tok = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(tok, str) or not tok:
            raise Stop("SHAPE")
        return tok

    def get(self, name: str, path: str, token: str,
            params: Mapping[str, str] | None = None) -> Any:
        r = self._send(name, "GET", ORIGIN + path,
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                       params=dict(params or {}))
        return _json(r)


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        raise Stop("BAD_JSON") from None


def _fields(objs: list[dict[str, Any]], documented: frozenset[str]) -> dict[str, Any]:
    """Top-level documented keys -> sorted json types; undocumented keys counted only."""
    seen: dict[str, set[str]] = {}
    undocumented = 0
    for o in objs:
        for k, v in o.items():
            if k in documented:
                seen.setdefault(k, set()).add(_jtype(v))
            else:
                undocumented += 1
    return {"fields": [{"field": k, "json_types": sorted(t)} for k, t in sorted(seen.items())],
            "undocumented_key_occurrences": undocumented}


def probe(env: Mapping[str, str], today: date,
          transport: httpx.BaseTransport | None = None) -> tuple[int, dict[str, Any]]:
    out: dict[str, Any] = {
        "probe": "athena_preview_read_probe v1 (N-EHR v1r1)",
        "generated_utc": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "practice": "NOT CHECKED",
        "requests": [], "outcome_classes": [], "limitations": [],
        "write": "NOT TESTED (no write in this probe)",
    }
    try:
        cid, secret = gate(env)
    except GateRefused as exc:
        out["result"] = f"GATE_REFUSED:{exc.args[0]}"
        out["practice"] = "identity gate REFUSED"
        out["requests"] = []
        return 2, out
    out["practice"] = "published Preview sandbox (identity gate passed)"
    c = GuardedClient(transport)
    rc = 0
    try:
        tok = c.token(cid, secret)
        deps = c.get("departments", f"/v1/{PRACTICE}/departments", tok)
        if not isinstance(deps, dict) or not isinstance(deps.get("departments"), list):
            raise Stop("SHAPE")
        out["outcome_classes"].append("ACCESS_VERIFIED")
        start, end = today, today + timedelta(days=WINDOW_DAYS - 1)
        booked = c.get("booked", f"/v1/{PRACTICE}/appointments/booked", tok, {
            "startdate": start.strftime("%m/%d/%Y"), "enddate": end.strftime("%m/%d/%Y"),
            **BOOKED_PARAMS_FIXED})
        if not isinstance(booked, dict) or not isinstance(booked.get("appointments"), list):
            raise Stop("SHAPE")
        if not all(isinstance(a, dict) for a in booked["appointments"]):
            raise Stop("SHAPE")  # F-P1: malformed entries are never an empty success
        appts: list[dict[str, Any]] = booked["appointments"]
        out["booked"] = {
            "totalcount_type": _jtype(booked.get("totalcount")),
            "returned_count": len(booked["appointments"]),
            "window_days": WINDOW_DAYS,
        }
        if not appts:
            out["limitations"].append("appointment shape NOT TESTED (0 appointments returned)")
        else:
            out["outcome_classes"].append("READ_SHAPE_VERIFIED")
            out["appointment_fields"] = _fields(appts, DOC_APPT_KEYS)
            codes = dict.fromkeys((*STATUS_CODES, UNDOCUMENTED), 0)
            for a in appts:
                s = a.get("appointmentstatus")
                codes[s if isinstance(s, str) and s in STATUS_CODES else UNDOCUMENTED] += 1
            out["appointmentstatus_codes"] = codes
            out["matching_keys"] = {}
            for k in MATCH_KEYS:
                present = [a for a in appts if k in a]
                nulls = sum(1 for a in present if a[k] is None)
                out["matching_keys"][k] = {
                    "present": len(present), "absent": len(appts) - len(present),
                    "null": nulls,
                    "json_types": sorted({_jtype(a[k]) for a in present if a[k] is not None})}
                if len(present) < len(appts) or nulls:
                    out["limitations"].append(f"matching key {k} missing or null in some records")
            ids = [str(a.get("appointmentid")) for a in appts
                   if ID_RE.match(str(a.get("appointmentid", "")))][:MAX_SINGLE]
            single: list[dict[str, Any]] = []
            for i in ids:
                d = c.get("single", f"/v1/{PRACTICE}/appointments/{i}", tok)
                entries = d if isinstance(d, list) else [d]
                if not entries or not all(isinstance(x, dict) for x in entries):
                    raise Stop("SHAPE")  # F-P1: invalid single-appointment response
                single.extend(entries)
            if single:
                out["single_appointment_fields"] = _fields(single, DOC_APPT_KEYS)
            if ids:
                conf = c.get("confirmationstatus",
                             f"/v1/{PRACTICE}/appointments/{ids[0]}/confirmationstatus", tok)
                if not isinstance(conf, dict):
                    raise Stop("SHAPE")
                out["confirmationstatus_fields"] = _fields([conf], DOC_CONFIRMATION_KEYS)
                # F-P1: documented types (both strings). Present with a wrong type is a
                # SHAPE failure; confirmationname missing/null or shortname null is a
                # limitation and never a verified class. shortname may be absent ("if
                # defined then the status is a positive confirmation").
                for k in DOC_CONFIRMATION_KEYS:
                    if k in conf and conf[k] is not None and not isinstance(conf[k], str):
                        raise Stop("SHAPE")
                name_ok = isinstance(conf.get("confirmationname"), str)
                short_ok = "shortname" not in conf or isinstance(conf["shortname"], str)
                if name_ok and short_ok:
                    out["outcome_classes"].append("CONFIRMATION_READ_SHAPE_VERIFIED")
                else:
                    out["limitations"].append(
                        "confirmationstatus fields missing or null; confirmation read shape"
                        " NOT verified")
            else:
                out["limitations"].append(
                    "no valid appointment id; single/confirmation reads skipped")
        ref = c.get("reference",
                    f"/v1/{PRACTICE}/reference/appointmentconfirmationstatus", tok)
        if isinstance(ref, dict) and isinstance(ref.get("status"), list):
            out["reference_statuses"] = {"returned_count": len(ref["status"]),
                                         "totalcount_type": _jtype(ref.get("totalcount"))}
        else:
            out["limitations"].append("reference confirmation status list shape unexpected")
        out["result"] = "COMPLETED"
    except Stop as s:
        out["result"] = s.code
        rc = 1
    except Exception:  # noqa: BLE001 — never print exception text (may carry data)
        out["result"] = "INTERNAL"
        rc = 1
    finally:
        out["requests"] = c.log
        out["request_count"] = c.count
        c.close()
    return rc, out


def _load_env_file(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values  # local haloflow venv dependency
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def main(argv: list[str] | None = None, stdout: TextIO = sys.stdout,
         env_loader: Callable[[Path], dict[str, str]] = _load_env_file,
         transport: httpx.BaseTransport | None = None, today: date | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        env = {**os.environ, **env_loader(a.env_file)}
        rc, out = probe(env, today or datetime.now(tz=UTC).date(), transport)
    except Exception:  # noqa: BLE001
        rc, out = 1, {"result": "INTERNAL"}
    a.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    text = json.dumps(out, indent=2, sort_keys=True)
    (a.out / f"athena-read-probe-{stamp}.json").write_text(text + "\n")
    stdout.write(text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
