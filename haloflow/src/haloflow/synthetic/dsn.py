"""Admin DSN handling for the synthetic installer (Option B B2). SYNTHETIC ONLY.

The admin DSN comes from the environment only and is never stored, logged or echoed. It is
parsed with libpq's own parser, restricted to an allowlist of keys, and its endpoint is
checked and PINNED:
- a literal loopback IP, or a hostname that resolves ONLY to loopback (pinned to the
  resolved address so libpq cannot re-resolve differently); or
- an absolute Unix-socket directory, owned by the current uid, with no symlink in any
  component of the path.
Any libpq `PG*` variable in the environment is refused, because libpq would apply it.

Refusals are raised OUTSIDE any `except` block, so no exception carrying DSN text is ever
chained to them (B2-U-R1).
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Final

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from haloflow.synthetic.errors import SyntheticRefused

ALLOWED_KEYS: Final = frozenset({"host", "port", "dbname", "user", "password", "sslmode"})
DB_PREFIX: Final = "haloflow_test"
CONNECT_TIMEOUT: Final = 5
_PG_ENV: Final = re.compile(r"^PG[A-Z_]+$")

Resolver = Callable[..., list[tuple[Any, ...]]]


@dataclass(frozen=True)
class AdminEndpoint:
    """A checked, pinned admin endpoint. The password never appears in repr()."""

    host: str
    port: int
    target: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    sslmode: str | None = None

    def conninfo(self, dbname: str) -> str:
        parts: dict[str, Any] = {"host": self.host, "port": str(self.port), "dbname": dbname}
        if self.user is not None:
            parts["user"] = self.user
        if self.password is not None:
            parts["password"] = self.password
        if self.sslmode is not None:
            parts["sslmode"] = self.sslmode
        parts["connect_timeout"] = str(CONNECT_TIMEOUT)
        return make_conninfo(**parts)


_KEYWORD: Final = re.compile(r"(?:^|\s)([A-Za-z_]+)\s*=")
_URI_PARAM: Final = re.compile(r"[?&]([A-Za-z_]+)=")


def _libpq_keywords() -> frozenset[str]:
    from psycopg import pq

    names = {o.keyword.decode() for o in pq.Conninfo.get_defaults()}
    return frozenset(names | {"service", "servicefile"})


def _syntactic_keys(dsn: str) -> set[str]:
    """libpq keywords found WITHOUT libpq parsing (which would act on some, e.g. read a
    servicefile). Unknown words are left to libpq, which rejects them (DSN_INVALID)."""

    keys = set(_URI_PARAM.findall(dsn)) if "://" in dsn else set(_KEYWORD.findall(dsn))
    return {k.lower() for k in keys} & _libpq_keywords()


def _parse(dsn: str) -> dict[str, Any] | None:
    try:
        return dict(conninfo_to_dict(dsn))
    except Exception:  # noqa: BLE001 - mapped to a closed code below, never chained
        return None


def _socket_dir_safe(path: str, uid: int) -> bool:
    p = PurePosixPath(path)
    if not p.is_absolute():
        return False
    current = PurePosixPath("/")
    for part in p.parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return False
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid


def _loopback(value: str) -> str | None:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(ip) if ip.is_loopback else ""


def parse_admin_dsn(dsn: str, environ: Mapping[str, str], *,
                    resolve: Resolver = socket.getaddrinfo,
                    uid: int | None = None) -> AdminEndpoint:
    owner = os.getuid() if uid is None else uid
    if any(_PG_ENV.match(k) for k in environ):
        raise SyntheticRefused("DSN_ENV_OVERRIDE")
    if isinstance(dsn, str) and _syntactic_keys(dsn) - ALLOWED_KEYS:
        raise SyntheticRefused("DSN_KEY_FORBIDDEN")
    params = _parse(dsn) if isinstance(dsn, str) and dsn.strip() else None
    if not params:
        raise SyntheticRefused("DSN_INVALID")
    if set(params) - ALLOWED_KEYS:
        raise SyntheticRefused("DSN_KEY_FORBIDDEN")
    host, port = params.get("host"), params.get("port")
    if (host and "," in str(host)) or (port and "," in str(port)):
        raise SyntheticRefused("DSN_MULTI_HOST")
    dbname = str(params.get("dbname") or "")
    if not dbname.startswith(DB_PREFIX):
        raise SyntheticRefused("DSN_DBNAME_PREFIX")
    if not port:
        raise SyntheticRefused("DSN_PORT_MISSING")
    if not str(port).isdigit() or not 0 < int(port) < 65536:
        raise SyntheticRefused("DSN_INVALID")
    if not host:
        raise SyntheticRefused("DSN_HOST_NOT_LOCAL")
    host = str(host)
    pinned: str
    if "/" in host:
        if not _socket_dir_safe(host, owner):
            raise SyntheticRefused("DSN_SOCKET_UNSAFE")
        pinned = host
    else:
        literal = _loopback(host)
        if literal == "":
            raise SyntheticRefused("DSN_HOST_NOT_LOCAL")
        if literal is not None:
            pinned = literal
        else:
            addresses = _resolve(resolve, host, int(port))
            if not addresses or any(_loopback(a) in (None, "") for a in addresses):
                raise SyntheticRefused("DSN_HOST_NOT_LOCAL")
            ipv4 = [a for a in addresses if ":" not in a]
            pinned = (ipv4 or addresses)[0]
    return AdminEndpoint(host=pinned, port=int(port), target=dbname,
                         user=params.get("user"), password=params.get("password"),
                         sslmode=params.get("sslmode"))


def _resolve(resolve: Resolver, host: str, port: int) -> list[str]:
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except Exception:  # noqa: BLE001 - unresolvable is simply not local
        return []
    return [str(info[4][0]) for info in infos]
