"""The demo runner: one process, one loop, two uvicorn listeners (R-2, R-3, R-5).

Order (correction R3):

  start-up refusals → ownership (process lock, DB lock, tables, epoch,
  recovery) → listeners → wait for SIGTERM/SIGINT, owner loss, or a listener
  ending → ``closing`` (new external calls refused; requests get 503) →
  listeners stopped → drain tracked external calls → DB lock released →
  process lock released

If drain cannot confirm that every external call has ended, the runner returns
:data:`EXIT_DRAIN_INCOMPLETE` WITHOUT releasing either lock, and
:func:`exit_process` ends the process with ``os._exit`` so the kernel releases
the lock only when no code in this process can run any more.

Signals: the runner installs its SIGTERM/SIGINT handlers before the servers
start. uvicorn's ``Server.serve()`` captures those signals while serving and,
when it stops, restores the previous handler (the runner's) and re-raises the
signal into it, so a signal still ends up as the runner's ``stop`` event. A
signal arriving after the handlers are removed (during drain) gets the default
action and kills the process; the kernel then releases the lock with no code
left running, which keeps the exclusion invariant.

Exit codes: 0 clean; 2 start-up refused; 3 owner lost (clean drain);
4 drain incomplete; 5 a listener failed to start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import NoReturn

import uvicorn

from haloflow_demo.ownership import Ownership, acquire_ownership
from haloflow_demo.service import DbStaffService
from haloflow_demo.settings import (
    DemoSettings,
    StartupRefused,
    build_server_configs,
    validate_startup,
)
from haloflow_demo.web import build_callback_app, build_staff_app

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_OWNER_LOST = 3
EXIT_DRAIN_INCOMPLETE = 4
EXIT_LISTENER_FAILED = 5


class _EventSink(list[str]):
    def __init__(self, emit: Callable[[str], None]) -> None:
        super().__init__()
        self._emit = emit

    def append(self, item: str) -> None:
        super().append(item)
        self._emit(item)


async def run(
    settings: DemoSettings,
    *,
    on_started: Callable[[Ownership], None] | None = None,
    on_event: Callable[[str], None] | None = None,
    watchdog_interval: float = 2.0,
    drain_timeout: float = 10.0,
    cancel_grace: float = 2.0,
) -> int:
    events = _EventSink(on_event or (lambda _e: None))
    try:
        target = validate_startup(settings)
        own = await acquire_ownership(settings, target, events=events)
    except StartupRefused as exc:
        logger.error("demo start-up refused: %s", exc.reason)
        events.append(f"refused:{exc.reason}")
        return EXIT_REFUSED

    def gate() -> bool:
        return own.owner_lost or own.closing

    staff_app = build_staff_app(DbStaffService(own, settings), csrf_secret=secrets.token_bytes(32),
                                unavailable=gate, staff_port=settings.staff_port)
    callback_app = build_callback_app(unavailable=gate)
    staff_cfg, cb_cfg = build_server_configs(settings, staff_app, callback_app)
    servers = [uvicorn.Server(staff_cfg), uvicorn.Server(cb_cfg)]

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    own.start_watchdog(watchdog_interval)
    server_tasks = [loop.create_task(s.serve()) for s in servers]
    waiters: list[asyncio.Task[object]] = []
    code = EXIT_OK
    try:
        while not all(s.started for s in servers):
            if any(t.done() for t in server_tasks):
                break
            await asyncio.sleep(0.01)
        if all(s.started for s in servers):
            events.append("listening")
            if on_started is not None:
                on_started(own)
            waiters = [loop.create_task(stop.wait()), loop.create_task(own.lost.wait())]
            await asyncio.wait([*waiters, *server_tasks], return_when=asyncio.FIRST_COMPLETED)
        else:
            code = EXIT_LISTENER_FAILED
            events.append("listener_failed")

        if own.owner_lost:
            code = EXIT_OWNER_LOST
            events.append("owner_lost")
        own.begin_closing()
        events.append("closing")
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*server_tasks, return_exceptions=True)
        events.append("listeners_stopped")
    finally:
        for w in waiters:
            w.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    if not await own.drain(drain_timeout, cancel_grace):
        logger.error("drain incomplete: %d external call(s) still running", own.inflight)
        events.append("drain_incomplete")
        return EXIT_DRAIN_INCOMPLETE  # no lock release: the process must exit now
    events.append("drained")
    if own.owner_lost and code == EXIT_OK:
        code = EXIT_OWNER_LOST
    await own.close()
    return code


def exit_process(main: Callable[[], Awaitable[int]]) -> NoReturn:
    """Run ``main`` and exit with its code. On drain-incomplete, ``os._exit`` so
    no surviving task runs after this point and the kernel releases the lock."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = loop.run_until_complete(main())
    if code == EXIT_DRAIN_INCOMPLETE:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    sys.exit(code)
