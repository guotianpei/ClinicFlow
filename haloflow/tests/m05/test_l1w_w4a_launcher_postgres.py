"""L-1W W4a E-L1: the pinned launcher, as a real OS process (PostgreSQL 17).

Plan v1 R-W4-1, architecture v2 A1/A9 E-L1, Codex Q-W4-5 (bounded subprocess
launcher on an ephemeral port; startup-failure evidence preserved).

Method. ``<python> -I -m uvicorn haloflow.platform.app:app --lifespan on`` on
127.0.0.1 and a port the parent reserved and released just before the spawn, with
the configuration in the child's environment only (PINNED names below). The parent
probes the port with plain TCP connects every 50 ms until the child exits or a
deadline passes (bounded; a timeout is a failure). Captured stdout/stderr are kept
in the assertion message for the evidence log, never parsed for the verdict.

PINNED CONFIGURATION (W4a contract; ``haloflow.platform.config.load_config``):
- ``HALOFLOW_PLATFORM_DEPLOYMENT``: ``production`` | ``non_production``
- ``HALOFLOW_PLATFORM_SERVED_TENANTS``: comma-separated tenant ids, at least one
- ``HALOFLOW_PLATFORM_RUNTIME_DATABASE_URL`` (tenant pool AND admission pool),
  ``HALOFLOW_PLATFORM_MIGRATOR_DATABASE_URL``,
  ``HALOFLOW_PLATFORM_PROVISIONER_DATABASE_URL``: ``postgresql://`` conninfo
- ``HALOFLOW_PLATFORM_AUTH_ISSUER``, ``HALOFLOW_PLATFORM_AUTH_AUDIENCE``,
  ``HALOFLOW_PLATFORM_AUTH_JWKS_URL``, ``HALOFLOW_PLATFORM_AUTH_ALGORITHMS``
  (comma-separated), optional ``HALOFLOW_PLATFORM_AUTH_ASSURANCE_AMR`` and
  ``HALOFLOW_PLATFORM_TRUSTED_PROXIES`` (comma-separated)
- Startup performs NO key-set fetch (the key set is fetched on first use), so the
  test JWKS URL is never contacted here.
- A refused startup writes exactly one ERROR line through the ``haloflow.platform``
  logger: ``STARTUP_REFUSED reason_code=<code>`` (B5 ``StartupRefused.reason_code``),
  or ``STARTUP_REFUSED reason_code=CONFIG_INVALID field=<VARIABLE NAME>`` for
  configuration. No tenant id, conninfo or driver text is logged. This line is the
  CAUSE check that keeps E-L1a/b from passing at red (an import failure also exits
  non-zero without accepting).

Rows:
- E-L1a: the served tenant is suspended (TENANT_NOT_ACTIVE at B5 startup): the
  process exits non-zero within the deadline, and no TCP connect ever succeeded.
- E-L1b: a missing required variable: non-zero exit, no TCP accept.
- E-L1c (positive control): the same tenant active: the port accepts, and
  ``GET /healthz`` returns exactly ``{"status":"ok"}``; SIGTERM then exits 0.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import b5_support as b5
import m02_support as m02
import pytest

pytestmark = pytest.mark.postgres

DEADLINE = 60.0
HALOFLOW_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def launch_tenant(m02_ids: m02.Identities, m05_reset: Any) -> Iterator[tuple[str, str]]:
    tenant = m02.tenant_for("l1w:w4a:launcher")
    m05_reset(tenant)
    try:
        m02.provision_sync(m02_ids, m02.production_registry(), tenant,
                           supported=range(b5.PRODUCTION_TARGET, b5.PRODUCTION_TARGET + 1))
        yield tenant
    finally:
        m05_reset(tenant)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _environment(ids: m02.Identities, tenant_id: str, **drop: bool) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HALOFLOW_PLATFORM_DEPLOYMENT": "production",
        "HALOFLOW_PLATFORM_SERVED_TENANTS": tenant_id,
        "HALOFLOW_PLATFORM_RUNTIME_DATABASE_URL": ids.logins["haloflow_runtime"],
        "HALOFLOW_PLATFORM_MIGRATOR_DATABASE_URL": ids.logins["haloflow_migrator"],
        "HALOFLOW_PLATFORM_PROVISIONER_DATABASE_URL": ids.logins["haloflow_provisioner"],
        "HALOFLOW_PLATFORM_AUTH_ISSUER": "https://issuer.test.invalid/",
        "HALOFLOW_PLATFORM_AUTH_AUDIENCE": "https://api.haloflow.test.invalid",
        "HALOFLOW_PLATFORM_AUTH_JWKS_URL": "https://issuer.test.invalid/jwks",
        "HALOFLOW_PLATFORM_AUTH_ALGORITHMS": "ES256,RS256",
    }
    for name, flag in drop.items():
        if flag:
            env.pop(name, None)
    return env


@contextlib.contextmanager
def _launch(env: dict[str, str], port: int) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "uvicorn", "haloflow.platform.app:app",
         "--lifespan", "on", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=HALOFLOW_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _evidence(proc: subprocess.Popen[bytes]) -> str:
    out, err = proc.communicate(timeout=10)
    return f"exit={proc.returncode}\nstdout:\n{out.decode(errors='replace')}\n" \
           f"stderr:\n{err.decode(errors='replace')}"


def _expect_refused_start(env: dict[str, str], line: str, *absent: str) -> None:
    port = _free_port()
    with _launch(env, port) as proc:
        accepted = 0
        deadline = time.monotonic() + DEADLINE
        while proc.poll() is None and time.monotonic() < deadline:
            accepted += _accepts(port)
            time.sleep(0.05)
        assert proc.poll() is not None, "launcher still running at the deadline"
        evidence = _evidence(proc)
        assert proc.returncode != 0, evidence
        assert accepted == 0, f"TCP accepted {accepted} time(s) before exit\n{evidence}"
        assert not _accepts(port)
        assert evidence.count("STARTUP_REFUSED") == 1 and line in evidence, evidence
        for secret in absent:
            assert secret not in evidence


def test_el1a_unservable_tenant_exits_nonzero_without_accepting(
    m02_ids: m02.Identities, launch_tenant: tuple[str, str],
) -> None:
    with m02.connect_admin(m02_ids) as conn:
        changed = conn.execute(
            "UPDATE shared.tenants SET lifecycle_state = 'suspended'"
            " WHERE tenant_id = %s AND lifecycle_state = 'active'", (launch_tenant[0],)
        ).rowcount
    assert changed == 1
    try:
        _expect_refused_start(_environment(m02_ids, launch_tenant[0]),
                              "STARTUP_REFUSED reason_code=TENANT_NOT_ACTIVE",
                              launch_tenant[0], launch_tenant[1],
                              m02_ids.logins["haloflow_runtime"])
    finally:
        with m02.connect_admin(m02_ids) as conn:
            conn.execute("UPDATE shared.tenants SET lifecycle_state = 'active'"
                         " WHERE tenant_id = %s", (launch_tenant[0],))


@pytest.mark.parametrize("missing", ["HALOFLOW_PLATFORM_SERVED_TENANTS",
                                     "HALOFLOW_PLATFORM_AUTH_ISSUER",
                                     "HALOFLOW_PLATFORM_RUNTIME_DATABASE_URL"])
def test_el1b_missing_configuration_exits_nonzero_without_accepting(
    m02_ids: m02.Identities, launch_tenant: tuple[str, str], missing: str,
) -> None:
    _expect_refused_start(_environment(m02_ids, launch_tenant[0], **{missing: True}),
                          f"STARTUP_REFUSED reason_code=CONFIG_INVALID field={missing}")


def test_el1c_good_configuration_serves_after_startup_and_exits_cleanly(
    m02_ids: m02.Identities, launch_tenant: tuple[str, str],
) -> None:
    import httpx

    port = _free_port()
    with _launch(_environment(m02_ids, launch_tenant[0]), port) as proc:
        deadline = time.monotonic() + DEADLINE
        while not _accepts(port):
            assert proc.poll() is None, _evidence(proc)
            assert time.monotonic() < deadline, "launcher never accepted"
            time.sleep(0.05)
        response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5.0)
        assert response.status_code == 200 and response.content == b'{"status":"ok"}'
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=DEADLINE)
        assert proc.returncode == 0, _evidence(proc)
