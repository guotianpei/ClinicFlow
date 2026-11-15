"""Closed refusal codes of the synthetic installer (Option B B2) and runtime (B3a).
SYNTHETIC ONLY.

Every refusal carries a code from `CODES` and, for some codes, a tuple of class names
(`detail`). Neither ever contains a DSN, a password, a verifier, a key or an object name
that came from the environment: only fixed vocabulary.
"""

from __future__ import annotations

from typing import Final

CODES: Final = frozenset({
    "DEPLOYMENT_PRODUCTION", "ATTESTATION_MISSING", "DSN_INVALID", "DSN_KEY_FORBIDDEN",
    "DSN_MULTI_HOST", "DSN_HOST_NOT_LOCAL", "DSN_SOCKET_UNSAFE", "DSN_PORT_MISSING",
    "DSN_DBNAME_PREFIX", "DSN_ENV_OVERRIDE", "STATE_PATH_INVALID", "STATE_MISSING",
    "STATE_SYMLINK", "STATE_PERMISSIONS", "STATE_CORRUPT", "STATE_LOCKED",
    "STATE_LOCK_REPLACED", "SERVER_VERSION_UNSUPPORTED", "CLUSTER_NOT_FRESH",
    "CLUSTER_MISMATCH", "TARGET_MISMATCH", "NOT_RESERVED", "RESERVED_BY_OTHER",
    "EPOCH_SUPERSEDED", "ISOLATION_NOT_READ_COMMITTED", "STATE_DIVERGED", "INSTALL_LOCKED",
    "INSTALL_LOCK_LOST",
    # Option B B3a (runtime seal, guard, lifecycle):
    "UNGUARDED_STATEMENT", "RUNTIME_NO_DDL", "RUNTIME_STOPPED", "RUNTIME_LEASE_LOST",
    "RUNTIME_STOP_FAILED", "LEASE_CLEANUP_UNKNOWN", "CLOSE_TIMEOUT", "DRAIN_DEADLINE",
    "POOL_NOT_QUIESCENT", "WATCHER_STOP_FAILED", "LEASE_CHECK_INVALID", "BUDGET_INVALID",
    "BUDGET_TOTAL_EXCEEDED", "RUNTIME_NOT_STARTABLE", "BIND_NOT_LOOPBACK",
})


class SyntheticRefused(Exception):
    """A closed-code refusal. `str()` is the code (and class names), nothing else."""

    def __init__(self, code: str, detail: tuple[str, ...] = ()) -> None:
        if code not in CODES:
            raise ValueError("unknown refusal code")
        self.code = code
        self.detail = tuple(detail)
        super().__init__(code if not self.detail else f"{code}:{','.join(self.detail)}")

    def __repr__(self) -> str:
        return f"SyntheticRefused({self.code!r}, {self.detail!r})"


def refused_from(error: BaseException) -> SyntheticRefused | None:
    """The first SyntheticRefused in an exception's cause/context chain (a guard refusal that a
    wrapped component, e.g. the M01 provisioner, re-raised as its own error)."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, SyntheticRefused):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None
