"""
HaloFlow — FastAPI application entry point.

Mounts all Tier 2 routers and wires the APScheduler.
"""
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from haloflow.config import get_settings
from haloflow.modules.care_gaps.router import router as care_gaps_router
from haloflow.modules.eligibility.router import router as eligibility_router
from haloflow.modules.fax.router import router as fax_router
from haloflow.modules.reminders.router import router as reminders_router
from haloflow.scheduler import create_scheduler

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(
    title="HaloFlow",
    description="HaloVox Clinic Automation — Track B, Tier 2",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — tighten this to the actual staff dashboard origin before production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else [],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(reminders_router)
app.include_router(eligibility_router)
app.include_router(fax_router)
app.include_router(care_gaps_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "tenant": settings.tenant_id, "env": settings.app_env}
