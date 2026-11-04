"""Platform configuration from the process environment (L-1W W4a, R-W4-1).

Provider-neutral names only; no vendor default ships. A missing or invalid variable
raises ``ConfigInvalid(field)`` with the variable NAME as ``field``; values are never
echoed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from psycopg.conninfo import conninfo_to_dict

from haloflow.platform.keys import SUPPORTED_ALGORITHMS, KeySetConfig
from haloflow.platform.verify import VerifierConfig
from haloflow.startup import Deployment

PREFIX: Final = "HALOFLOW_PLATFORM_"
DEPLOYMENT: Final = PREFIX + "DEPLOYMENT"
SERVED_TENANTS: Final = PREFIX + "SERVED_TENANTS"
RUNTIME_URL: Final = PREFIX + "RUNTIME_DATABASE_URL"
MIGRATOR_URL: Final = PREFIX + "MIGRATOR_DATABASE_URL"
PROVISIONER_URL: Final = PREFIX + "PROVISIONER_DATABASE_URL"
AUTH_ISSUER: Final = PREFIX + "AUTH_ISSUER"
AUTH_AUDIENCE: Final = PREFIX + "AUTH_AUDIENCE"
AUTH_JWKS_URL: Final = PREFIX + "AUTH_JWKS_URL"
AUTH_ALGORITHMS: Final = PREFIX + "AUTH_ALGORITHMS"
AUTH_ASSURANCE_AMR: Final = PREFIX + "AUTH_ASSURANCE_AMR"
TRUSTED_PROXIES: Final = PREFIX + "TRUSTED_PROXIES"


class ConfigInvalid(Exception):
    """``field`` is the environment variable name; the value is never included."""

    def __init__(self, field: str) -> None:
        super().__init__("CONFIG_INVALID")
        self.reason_code = "CONFIG_INVALID"
        self.field = field


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    deployment: Deployment
    served_tenants: tuple[str, ...]
    runtime_database_url: str
    migrator_database_url: str
    provisioner_database_url: str
    verifier: VerifierConfig
    keyset: KeySetConfig
    trusted_proxies: frozenset[str]


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if type(value) is not str or not value.strip():
        raise ConfigInvalid(name)
    return value.strip()


def _list(environ: Mapping[str, str], name: str, *, required: bool) -> tuple[str, ...]:
    raw = _required(environ, name) if required else environ.get(name, "")
    if type(raw) is not str:
        raise ConfigInvalid(name)
    items = tuple(part.strip() for part in raw.split(",")) if raw.strip() else ()
    if any(not item for item in items) or len(set(items)) != len(items):
        raise ConfigInvalid(name)
    if required and not items:
        raise ConfigInvalid(name)
    return items


def _conninfo(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    try:
        conninfo_to_dict(value)
    except Exception:  # noqa: BLE001 - the driver message may contain the value
        raise ConfigInvalid(name) from None
    return value


def load_config(environ: Mapping[str, str]) -> PlatformConfig:
    """Read every variable once. Order of checks is the order of the fields."""

    raw_deployment = _required(environ, DEPLOYMENT)
    try:
        deployment = Deployment(raw_deployment)
    except ValueError:
        raise ConfigInvalid(DEPLOYMENT) from None
    served = _list(environ, SERVED_TENANTS, required=True)
    runtime = _conninfo(environ, RUNTIME_URL)
    migrator = _conninfo(environ, MIGRATOR_URL)
    provisioner = _conninfo(environ, PROVISIONER_URL)
    issuer = _required(environ, AUTH_ISSUER)
    audience = _required(environ, AUTH_AUDIENCE)
    jwks_url = _required(environ, AUTH_JWKS_URL)
    if not jwks_url.startswith("https://"):
        raise ConfigInvalid(AUTH_JWKS_URL)
    algorithms = frozenset(_list(environ, AUTH_ALGORITHMS, required=True))
    if not algorithms <= set(SUPPORTED_ALGORITHMS):
        raise ConfigInvalid(AUTH_ALGORITHMS)
    amr = frozenset(_list(environ, AUTH_ASSURANCE_AMR, required=False))
    proxies = frozenset(_list(environ, TRUSTED_PROXIES, required=False))
    return PlatformConfig(
        deployment=deployment,
        served_tenants=served,
        runtime_database_url=runtime,
        migrator_database_url=migrator,
        provisioner_database_url=provisioner,
        verifier=VerifierConfig(issuer=issuer, audience=audience, algorithms=algorithms,
                                assurance_amr=amr),
        keyset=KeySetConfig(jwks_url=jwks_url, allowed_algorithms=algorithms),
        trusted_proxies=proxies,
    )
