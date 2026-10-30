#!/usr/bin/env python3
"""athenahealth Preview confirmation read-shape probe -- E2 plan v1r2.

Reads only, on the published Preview sandbox (practice 195900). Exactly one POST
(the OAuth token request); every other request is a GET to an allowlisted path.
Output is shape-only: documented key names (plan v1r2 section 4a), JSON types,
counts, flags and fixed outcome codes. No returned value, identifier, name, token,
error body, URL or exception text is printed or written; undocumented key names
are counted, never echoed. Raw payloads are never written to disk.

Budget: 1 token POST + departments + booked + <=3 confirmation reads + <=4
vocabulary pages = MAX_REQUESTS 10. No redirects, no retries, 25 s timeout.

Usage (from the haloflow venv, with the local .env holding ATHENA_* settings):
    python e2/athena_confirmation_shape_e2.py --env-file <path to haloflow/.env> --out <dir>
Exit codes: 0 completed (see outcome classes), 1 request/shape failure,
2 identity/configuration gate refused (zero requests sent).
"""

from __future__ import annotations

import argparse
import hashlib
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
PRACTICE = "195900"  # published Preview sandbox
SCOPE = "athena/service/Athenanet.MDP.*"
MAX_REQUESTS = 10
MAX_CONF_READS = 3
VOCAB_LIMIT = 100
VOCAB_MAX_PAGES = 4
WINDOW_DAYS = 14
TIMEOUT_S = 25.0
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
_ID = r"[1-9][0-9]{0,11}"
REF_PATH = f"/v1/{PRACTICE}/reference/appointmentconfirmationstatus"
ALLOWED_GET = tuple(re.compile(p) for p in (
    rf"^/v1/{PRACTICE}/departments$",
    rf"^/v1/{PRACTICE}/appointments/booked$",
    rf"^/v1/{PRACTICE}/appointments/{_ID}/confirmationstatus$",
    rf"^{REF_PATH}$",
))
BOOKED_PARAMS_FIXED = {"limit": "50", "showpatientdetail": "false", "showinsurance": "false",
                       "showcopay": "false", "showclaimdetail": "false"}
DIAG_PARAM_NAMES = ("departmentid", "startdate", "enddate", "limit", "offset",
                    "showpatientdetail", "showinsurance", "showcopay", "showclaimdetail",
                    "practiceid", "appointmentid")
# Documented schemas (plan v1r2 section 4a; docs portal JSON entry, Appointments.yaml).
CONF_KEYS = {"confirmationname": "string", "shortname": "string"}
PAGE_KEYS = frozenset(("status", "totalcount", "next", "previous"))
ELEMENT_KEYS = {"statusid": "integer", "name": "string"}
UNAVAILABLE = "UNAVAILABLE"


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
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "other"


def gate(env: Mapping[str, str]) -> tuple[str, str]:
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
    if not ID_RE.fullmatch(practice) or practice != PRACTICE:
        raise GateRefused("practice")
    cid, secret = env.get("ATHENA_CLIENT_ID", ""), env.get("ATHENA_CLIENT_SECRET", "")
    if not cid or not secret:
        raise GateRefused("credentials_missing")
    return cid, secret


class GuardedClient:
    """Allowlist, method guard, request budget, no redirects."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._c = httpx.Client(transport=transport, timeout=TIMEOUT_S, follow_redirects=False)
        self.count = 0
        self.log: list[dict[str, Any]] = []

    def close(self) -> None:
        self._c.close()

    def _send(self, name: str, method: str, url: str, **kw: Any) -> httpx.Response:
        # urlsplit silently strips \n, \r and \t, so check the raw string first:
        # no control characters, spaces or DEL anywhere in the URL.
        if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
            raise Stop("DISALLOWED_URL")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != HOST or parts.query or parts.fragment:
            raise Stop("DISALLOWED_URL")
        if method == "POST":
            if url != TOKEN_URL:
                raise Stop("DISALLOWED_METHOD")
        elif method == "GET":
            if not any(p.fullmatch(parts.path) for p in ALLOWED_GET):
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
            entry: dict[str, Any] = {"name": name, "http_status": r.status_code, "outcome": code}
            if 400 <= r.status_code < 500:
                try:
                    low = r.text.lower()
                except Exception:  # noqa: BLE001
                    low = ""
                entry["error_mentions"] = [n for n in DIAG_PARAM_NAMES
                                           if re.search(rf"\b{n}\b", low)]
            self.log.append(entry)
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


def _valid_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _conf_shape(conf: Any) -> tuple[dict[str, Any], str | None]:
    """Shape of one confirmation read. Wrong documented types or a non-object -> SHAPE.
    Returns the shape record and the confirmation name (kept in memory only)."""
    if not isinstance(conf, dict):
        raise Stop("SHAPE")
    keys: dict[str, str] = {}
    undocumented = nulls = 0
    for k, v in conf.items():
        if k in CONF_KEYS:
            if v is None:
                nulls += 1  # present-null differs from a missing optional key; never verified
            elif _jtype(v) != CONF_KEYS[k]:
                raise Stop("SHAPE")
            keys[k] = _jtype(v)
        else:
            undocumented += 1
    name = conf.get("confirmationname")
    name = name if isinstance(name, str) else None
    empty = name is None
    return ({"keys": dict(sorted(keys.items())), "undocumented_key_count": undocumented,
             "null_key_count": nulls, "empty": empty, "verified": not empty and nulls == 0,
             "q5_match": UNAVAILABLE}, name)


def _vocabulary(c: GuardedClient, tok: str) -> tuple[dict[str, Any], list[str] | None]:
    """Offset paging only; next/previous never followed. Returns the shape record and,
    only when VOCABULARY_COMPLETE, the in-memory list of names (never written)."""
    v: dict[str, Any] = {"pages": 0, "entries_seen": 0, "total_reached": False,
                         "complete": False, "incomplete_reason": None,
                         "element_keys": {k: {"present": 0, "missing": 0, "wrong_type": 0}
                                          for k in ELEMENT_KEYS},
                         "undocumented_page_key_count": 0,
                         "undocumented_element_key_count": 0,
                         "duplicate_statusid": UNAVAILABLE, "names_unique": UNAVAILABLE}
    total: int | None = None
    signatures: set[str] = set()
    ids: list[int] = []
    names: list[str] = []
    elements_ok = True
    reason: str | None = None
    for page_no in range(VOCAB_MAX_PAGES):
        body = c.get("reference", REF_PATH, tok,
                     {"limit": str(VOCAB_LIMIT), "offset": str(page_no * VOCAB_LIMIT)})
        v["pages"] += 1
        if not isinstance(body, dict) or not isinstance(body.get("status"), list):
            reason = "MALFORMED"
            break
        v["undocumented_page_key_count"] += sum(1 for k in body if k not in PAGE_KEYS)
        t = body.get("totalcount")
        if not _valid_int(t) or t < 0:
            reason = "BAD_TOTAL"
            break
        if total is None:
            total = t
        elif t != total:
            reason = "TOTAL_CHANGED"
            break
        items = body["status"]
        if not all(isinstance(e, dict) for e in items):
            reason = "MALFORMED"
            break
        sig = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        if items and sig in signatures:
            reason = "REPEATED_PAGE"
            break
        signatures.add(sig)
        dup = False
        for e in items:
            for k, typ in ELEMENT_KEYS.items():
                if k not in e:
                    v["element_keys"][k]["missing"] += 1
                    elements_ok = False
                elif _jtype(e[k]) != typ:
                    v["element_keys"][k]["wrong_type"] += 1
                    elements_ok = False
                else:
                    v["element_keys"][k]["present"] += 1
            v["undocumented_element_key_count"] += sum(1 for k in e if k not in ELEMENT_KEYS)
            sid = e.get("statusid")
            if _valid_int(sid):
                if sid in ids:
                    dup = True
                ids.append(sid)
            nm = e.get("name")
            if isinstance(nm, str):
                names.append(nm)
        v["entries_seen"] += len(items)
        if dup:
            reason = "DUPLICATE_ID"
            break
        if v["entries_seen"] > total:
            reason = "OVERRUN"
            break
        if v["entries_seen"] == total:
            v["total_reached"] = True
            break
        if len(items) < VOCAB_LIMIT:
            reason = "SHORT_PAGE"
            break
    else:
        reason = "CAP"
    v["incomplete_reason"] = reason
    if v["total_reached"] and reason is None and elements_ok:
        v["complete"] = True
        v["duplicate_statusid"] = 0
        v["names_unique"] = len(set(names)) == len(names)
        return v, names
    if v["total_reached"] and reason is None:
        v["incomplete_reason"] = None  # total reached, but elements fail the schema
    return v, None


def probe(env: Mapping[str, str], today: date,
          transport: httpx.BaseTransport | None = None) -> tuple[int, dict[str, Any]]:
    out: dict[str, Any] = {
        "probe": "athena_confirmation_shape_e2 (E2 plan v1r2)",
        "generated_utc": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "practice": "NOT CHECKED", "requests": [], "outcome_classes": [], "limitations": [],
        "write": "NOT TESTED (no write in this probe)",
    }
    try:
        cid, secret = gate(env)
    except GateRefused as exc:
        out["result"] = f"GATE_REFUSED:{exc.args[0]}"
        out["practice"] = "identity gate REFUSED"
        return 2, out
    out["practice"] = "published Preview sandbox (identity gate passed)"
    c = GuardedClient(transport)
    rc = 0
    try:
        tok = c.token(cid, secret)
        deps = c.get("departments", f"/v1/{PRACTICE}/departments", tok)
        if not isinstance(deps, dict) or not isinstance(deps.get("departments"), list):
            raise Stop("SHAPE")
        dep_ids = [d["departmentid"] for d in deps["departments"]
                   if isinstance(d, dict) and isinstance(d.get("departmentid"), str)
                   and ID_RE.fullmatch(d["departmentid"])]
        if not dep_ids:
            raise Stop("NO_DEPARTMENT")
        start, end = today, today + timedelta(days=WINDOW_DAYS - 1)
        booked = c.get("booked", f"/v1/{PRACTICE}/appointments/booked", tok, {
            "startdate": start.strftime("%m/%d/%Y"), "enddate": end.strftime("%m/%d/%Y"),
            "departmentid": dep_ids[0], **BOOKED_PARAMS_FIXED})
        if (not isinstance(booked, dict) or not isinstance(booked.get("appointments"), list)
                or not all(isinstance(a, dict) for a in booked["appointments"])):
            raise Stop("SHAPE")
        appts = booked["appointments"]
        ids: list[str] = []
        malformed = duplicates = 0
        for a in appts:
            i = a.get("appointmentid")
            if not isinstance(i, str) or not ID_RE.fullmatch(i):
                malformed += 1
            elif i in ids:
                duplicates += 1
            else:
                ids.append(i)
        out["booked"] = {"returned_count": len(appts), "valid_distinct_ids": len(ids),
                         "malformed_ids": malformed, "duplicate_ids": duplicates}
        reads: list[dict[str, Any]] = []
        conf_names: list[str | None] = []
        for i in ids[:MAX_CONF_READS]:
            rec, nm = _conf_shape(c.get("confirmationstatus",
                                        f"/v1/{PRACTICE}/appointments/{i}/confirmationstatus",
                                        tok))
            reads.append(rec)
            conf_names.append(nm)
        if not ids:
            out["limitations"].append("no valid appointment id; confirmation reads skipped")
        out["confirmation_reads"] = reads
        vocab, names = _vocabulary(c, tok)
        out["vocabulary"] = vocab
        for rec, nm in zip(reads, conf_names, strict=True):
            if names is not None and nm is not None:
                n = names.count(nm)
                rec["q5_match"] = "0" if n == 0 else "1" if n == 1 else "multiple"
        if reads and all(r["verified"] for r in reads):
            out["outcome_classes"].append("CONFIRMATION_READ_SHAPE_VERIFIED")
        elif reads:
            out["limitations"].append("confirmation read empty or unpopulated in "
                                      f"{sum(1 for r in reads if not r['verified'])} of "
                                      f"{len(reads)} sampled reads; shape NOT verified")
        if vocab["total_reached"]:
            out["outcome_classes"].append("TOTAL_REACHED")
        if vocab["complete"]:
            out["outcome_classes"].append("VOCABULARY_COMPLETE")
            if vocab["entries_seen"] == 0:
                # zero samples cannot verify the element schema (Codex qualification)
                out["limitations"].append(
                    "vocabulary empty; element schema NOT verified (0 samples)")
            elif vocab["undocumented_element_key_count"] == 0:
                out["outcome_classes"].append("VOCABULARY_ELEMENT_SHAPE_VERIFIED")
        else:
            out["outcome_classes"].append("VOCABULARY_INCOMPLETE")
        out["result"] = "COMPLETED"
    except Stop as s:
        out["result"] = s.code
        rc = 1
    except Exception:  # noqa: BLE001 -- never print exception text (may carry data)
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
        rc, out = probe(env, today or datetime.now().date(), transport)
    except Exception:  # noqa: BLE001
        rc, out = 1, {"result": "INTERNAL"}
    a.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    text = json.dumps(out, indent=2, sort_keys=True)
    (a.out / f"athena-confirmation-shape-e2-{stamp}.json").write_text(text + "\n")
    stdout.write(text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
