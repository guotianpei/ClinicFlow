"""The synthetic application (Option B B3a): lifespan + one read-only health route.
SYNTHETIC ONLY.

`create_synthetic_app()` builds the app and opens nothing. Its lifespan starts the
`SyntheticLifecycle` (when the lifecycle is still NEW) and, on exit, requests the stop and
awaits the coordinator's completion (never the drain steps directly). The only route is
`GET /synthetic/health`: 200 `{"simulated": true, "state": "RUNNING"}` while admission is
open, otherwise 503 with a closed code (`RUNTIME_STOPPED` / `RUNTIME_LEASE_LOST`), from the
moment a stop is requested. B3b adds the staff and simulator routes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from haloflow.synthetic.lifecycle import SyntheticLifecycle

HEALTH_ROUTE = "/synthetic/health"


def create_synthetic_app(environ: Mapping[str, str] | None = None, *,
                         lifecycle: SyntheticLifecycle | None = None) -> FastAPI:
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

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get(HEALTH_ROUTE)
    async def health() -> JSONResponse:
        if lc.admission_open:
            return JSONResponse({"simulated": True, "state": lc.state})
        return JSONResponse({"simulated": True, "state": lc.state, "refused": lc.stop_code},
                            status_code=503)

    app.state.lifecycle = lc
    return app
