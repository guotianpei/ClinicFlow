"""Demo settings and start-up refusals (R-2, R-6, R-8, R-9, R-11, R-22).

All refusals raise :class:`StartupRefused` before any listener or connection
exists. Settings are read from an explicit mapping (normally ``os.environ``);
no ``.env`` file is read implicitly and nothing is imported from the legacy
``haloflow.config``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_PG_PORT = 5432
_PG_SCHEMES = ("postgresql", "postgres")


class StartupRefused(Exception):
    """Start-up refused; ``reason`` is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# ── phone ────────────────────────────────────────────────────────────────────

_NON_DIGIT = re.compile(r"[^\d+]")


def normalize_us_phone(raw: str | None) -> str | None:
    """Return a US E.164 number (``+1NXXXXXXXXX``) or ``None`` if invalid."""
    if not raw:
        return None
    s = _NON_DIGIT.sub("", raw.strip())
    if s.startswith("+"):
        if not s.startswith("+1"):
            return None
        digits = s[2:]
    else:
        digits = s[1:] if len(s) == 11 and s.startswith("1") else s
    if len(digits) != 10 or not digits.isdigit() or digits[0] in "01":
        return None
    return "+1" + digits


def mask_phone(e164: str) -> str:
    """``+1********NN``: only the last two digits survive."""
    return "+1********" + e164[-2:]


# ── database target ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DemoDbTarget:
    """A validated demo database. Only :func:`validate_demo_database_url` builds one."""

    host: str | None  # None means a Unix socket directory
    socket_dir: str | None
    port: int
    dbname: str
    user: str | None
    password: str | None = field(repr=False)
    canonical: tuple[str, int, str]


def _canonical(url: str) -> tuple[str, int, str, str | None, str | None, str | None, str | None]:
    """(host_class, port, dbname, host, socket_dir, user, password) or raise."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    if scheme not in _PG_SCHEMES:
        raise StartupRefused("demo_db_scheme")
    query = parse_qs(parts.query)
    hosts_q = query.get("host", [])
    host = parts.hostname
    socket_dir: str | None = None
    if hosts_q:
        qhost = unquote(hosts_q[-1])
        if qhost.startswith("/"):
            socket_dir = qhost
            if host is not None:
                # an authority host plus a socket override is ambiguous
                raise StartupRefused("demo_db_not_local", "host and socket both given")
        else:
            if host is not None and qhost != host:
                raise StartupRefused("demo_db_not_local", "query host overrides authority")
            host = qhost
    if "," in (host or "") or len(hosts_q) > 1:
        raise StartupRefused("demo_db_not_local", "multiple hosts")
    if socket_dir is None and host is None:
        host_class = "socket:default"
    elif socket_dir is not None:
        host_class = "socket:" + socket_dir
    else:
        assert host is not None
        if host not in LOOPBACK_HOSTS:
            raise StartupRefused("demo_db_not_local")
        host_class = "loopback"
    try:
        port = parts.port or int(query.get("port", [str(_DEFAULT_PG_PORT)])[-1])
    except ValueError as exc:
        raise StartupRefused("demo_db_url_invalid", "port") from exc
    dbname = unquote(parts.path.lstrip("/")) or unquote(query.get("dbname", [""])[-1])
    return (host_class, port, dbname, host, socket_dir,
            unquote(parts.username) if parts.username else None,
            unquote(parts.password) if parts.password else None)


def _local_key(url: str) -> tuple[int, str] | None:
    """(port, dbname) of a local PostgreSQL URL; ``None`` if certainly not local.

    Conservative alias rule (Codex checkpoint-1 finding 2): every local endpoint
    (127.0.0.1, localhost, ::1, any Unix-socket directory, the default socket)
    with the same port and database name is treated as the same database.
    Anything that cannot be compared fails closed.
    """
    try:
        _hc, port, dbname, _h, _sd, user, _pw = _canonical(url)
    except StartupRefused as exc:
        if exc.reason == "demo_db_scheme" or str(exc) == "demo_db_not_local":
            return None  # a different scheme, or a plainly remote host
        raise StartupRefused("demo_db_compare_failed", exc.reason) from exc
    if not dbname:
        if not user:
            raise StartupRefused("demo_db_compare_failed", "no database name")
        dbname = user  # libpq default: database name = user name
    return (port, dbname)


def _same_database(demo_url: str, other_url: str) -> bool:
    other = _local_key(other_url)
    if other is None:
        return False
    demo = _local_key(demo_url)
    return demo == other


# ── settings ──────────────────────────────────────────────────────────────────


def _bind(raw: str, default: tuple[str, int]) -> tuple[str, int]:
    if not raw:
        return default
    if raw.startswith("["):
        host, _, port = raw[1:].partition("]:")
    else:
        host, _, port = raw.rpartition(":")
    try:
        return host, int(port)
    except ValueError as exc:
        raise StartupRefused("bind", raw) from exc


@dataclass(frozen=True)
class DemoSettings:
    database_url: str = field(repr=False)
    legacy_database_url: str = field(repr=False)
    test_database_url: str = field(repr=False)
    clinic_tz: str
    sms_mode: str
    sms_daily_cap_raw: str
    sms_test_number: str | None = field(repr=False)
    sms_sender: str | None = field(repr=False)  # our registered number (inbound recipient)
    callback_mode: str
    callback_secret: str = field(repr=False)
    staff_host: str
    staff_port: int
    callback_host: str
    callback_port: int
    dispatcher: bool
    ehr_write: str
    reminder_days_before: int = 2
    # N-ATH (OD-A1): "synthetic" (default) or "athena_preview" (read-only sandbox).
    ehr_source: str = "synthetic"
    athena_department_id: str = ""

    @property
    def sms_daily_cap(self) -> int:
        try:
            return int(self.sms_daily_cap_raw)
        except ValueError as exc:
            raise StartupRefused("sms_daily_cap", self.sms_daily_cap_raw) from exc

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DemoSettings:
        g = env.get
        staff = _bind(g("DEMO_STAFF_BIND", ""), ("127.0.0.1", 8080))
        cb = _bind(g("DEMO_CALLBACK_BIND", ""), ("127.0.0.1", 8081))
        return cls(
            database_url=g("DEMO_DATABASE_URL", ""),
            legacy_database_url=g("DATABASE_URL", ""),
            test_database_url=g("HALOFLOW_TEST_DATABASE_URL", ""),
            clinic_tz=g("CLINIC_TZ", "") or "America/New_York",
            sms_mode="real" if g("DEMO_SMS_MODE", "") == "real" else "stub",
            sms_daily_cap_raw=g("DEMO_SMS_DAILY_CAP", "") or "10",
            sms_test_number=normalize_us_phone(g("DEMO_SMS_TEST_NUMBER", "")),
            sms_sender=normalize_us_phone(g("DEMO_SMS_SENDER", "")),
            callback_mode="real" if g("DEMO_CALLBACK_MODE", "") == "real" else "simulated",
            callback_secret=g("DEMO_CALLBACK_SECRET", ""),
            staff_host=staff[0],
            staff_port=staff[1],
            callback_host=cb[0],
            callback_port=cb[1],
            dispatcher=g("DEMO_DISPATCHER", "") == "on",
            ehr_write="real" if g("DEMO_EHR_WRITE", "") == "real" else "simulated",
            ehr_source=g("DEMO_EHR_SOURCE", "") or "synthetic",
            athena_department_id=g("DEMO_ATHENA_DEPARTMENT_ID", ""),
        )


def validate_demo_database_url(s: DemoSettings) -> DemoDbTarget:
    """R-8: local only, name ends in ``_demo``, distinct from legacy and test DBs."""
    if not s.database_url:
        raise StartupRefused("demo_db_missing")
    host_class, port, dbname, host, socket_dir, user, password = _canonical(s.database_url)
    if not dbname.endswith("_demo") or not re.fullmatch(r"[a-z0-9_]+", dbname):
        raise StartupRefused("demo_db_name")
    if s.legacy_database_url and _same_database(s.database_url, s.legacy_database_url):
        raise StartupRefused("demo_db_equals_legacy")
    if s.test_database_url and _same_database(s.database_url, s.test_database_url):
        raise StartupRefused("demo_db_equals_test")
    return DemoDbTarget(
        host=host, socket_dir=socket_dir, port=port, dbname=dbname, user=user,
        password=password, canonical=(host_class, port, dbname),
    )


def validate_startup(s: DemoSettings) -> DemoDbTarget:
    """Every start-up refusal that needs no socket. Returns the DB target."""
    if s.ehr_source not in ("synthetic", "athena_preview"):
        raise StartupRefused("ehr_source")
    cap = s.sms_daily_cap
    if not 1 <= cap <= 10:
        raise StartupRefused("sms_daily_cap", str(cap))
    if s.staff_host not in LOOPBACK_HOSTS:
        raise StartupRefused("staff_bind_not_loopback")
    if s.sms_mode == "real":
        # Notifyre send contract (endpoint, payload, response id, sender
        # registration) not yet verified; only the labelled stub may run.
        raise StartupRefused("sms_real_unverified")
    if s.callback_mode == "real" and s.callback_secret.strip() in ("", "changeme"):
        raise StartupRefused("callback_secret")
    if s.callback_mode == "real":
        # Real ingress (Notifyre-Signature over live events, public HTTPS exposure
        # of the callback route only) is not yet verified; refused (P-NS).
        raise StartupRefused("callback_real_unverified")
    try:
        ZoneInfo(s.clinic_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise StartupRefused("clinic_tz") from exc
    return validate_demo_database_url(s)


def build_server_configs(
    s: DemoSettings, staff_app: Any, callback_app: Any, *, workers: int = 1, reload: bool = False
) -> tuple[Any, Any]:
    """R-2: two single-worker, non-reloading uvicorn configs; staff on loopback."""
    if workers != 1:
        raise StartupRefused("workers")
    if reload:
        raise StartupRefused("reload")
    validate_startup(s)
    import uvicorn

    common: dict[str, Any] = {"workers": 1, "reload": False, "lifespan": "off",
                              "access_log": False, "server_header": False,
                              "timeout_graceful_shutdown": 5}
    staff_cfg = uvicorn.Config(staff_app, host=s.staff_host, port=s.staff_port, **common)
    cb_cfg = uvicorn.Config(callback_app, host=s.callback_host, port=s.callback_port, **common)
    return staff_cfg, cb_cfg
