"""The synthetic application (Option B B3a + B3b). SYNTHETIC ONLY.

`create_synthetic_app()` builds the app and opens nothing. Its lifespan starts the
`SyntheticLifecycle` (when the lifecycle is still NEW) and, on exit, requests the stop and
awaits the coordinator's completion (never the drain steps directly).

Routes (R-B3b-1, closed): `GET /synthetic/health` (B3a), `GET /synthetic/session` (the
per-lifespan CSRF token, `Cache-Control: no-store`), the six staff routes (`staff_routes`) and
the two simulator routes (`simulator`), the B4 staff screen (`ui`: four HTML routes and
one stylesheet), and the B5 simulator actions (`sim_actions`: load-scenario, tick, outbox) and
simulator page (`sim_ui`: the page and its four form routes). The S-G1a ingress is NOT
mounted. Every request passes the local exposure gate first (`gate`, built from the loopback
`bind`), and every response is labelled SIMULATED (`labels`). No docs or OpenAPI routes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.responses import Response

from haloflow.synthetic.gate import LocalGate, allowed_hosts
from haloflow.synthetic.labels import EXCEPTION_HANDLERS, labelled
from haloflow.synthetic.lifecycle import SyntheticLifecycle
from haloflow.synthetic.sim_actions import install_sim_actions
from haloflow.synthetic.sim_ui import install_sim_ui
from haloflow.synthetic.simulator import install_simulator
from haloflow.synthetic.staff_routes import install_staff_routes
from haloflow.synthetic.ui import install_ui

HEALTH_ROUTE: Final = "/synthetic/health"
SESSION_ROUTE: Final = "/synthetic/session"
DEFAULT_BIND: Final = ("127.0.0.1", 8765)
NO_STORE: Final = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def create_synthetic_app(environ: Mapping[str, str] | None = None, *,
                         lifecycle: SyntheticLifecycle | None = None,
                         bind: tuple[str, int] = DEFAULT_BIND) -> FastAPI:
    allowed = allowed_hosts(bind)          # refuses a non-loopback bind or an unknown port
    lc = lifecycle if lifecycle is not None else SyntheticLifecycle(
        dict(os.environ) if environ is None else environ)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        started_here = lc.state == "NEW"
        if started_here:
            await lc.start()
        try:
            yield
        finally:
            if started_here:
                lc.request_stop("LIFESPAN")
                await lc.wait_stopped()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  exception_handlers=dict(EXCEPTION_HANDLERS))

    @app.get(HEALTH_ROUTE, include_in_schema=False)
    async def health() -> JSONResponse:
        if lc.admission_open:
            return JSONResponse({"simulated": True, "state": lc.state})
        return JSONResponse({"simulated": True, "state": lc.state, "refused": lc.stop_code},
                            status_code=503)

    @app.get(SESSION_ROUTE, include_in_schema=False)
    async def session() -> Response:
        return labelled({"csrf": lc.csrf_token}, headers=NO_STORE)

    install_staff_routes(app, lc)
    install_simulator(app, lc)
    install_sim_actions(app, lc)
    install_sim_ui(app, lc)
    install_ui(app, lc)
    if getattr(lc, "profile", None) == "fax":   # FX3a/FX3b: fax simulator, routes, screen
        from haloflow.synthetic.fax_simulator import install_fax_simulator
        from haloflow.synthetic.fax_staff_routes import install_fax_staff_routes
        from haloflow.synthetic.fax_ui import install_fax_ui

        install_fax_simulator(app, lc)
        install_fax_staff_routes(app, lc)
        install_fax_ui(app, lc)            # FX3b: the fax staff screen
    app.add_middleware(LocalGate, lifecycle=lc, allowed=allowed)
    app.state.lifecycle = lc
    return app
