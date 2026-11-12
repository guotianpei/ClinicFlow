"""Guarded Alembic for the synthetic installer (Option B B2). SYNTHETIC ONLY.

The unchanged migration scripts run through Alembic's `EnvironmentContext` on the installer's
OWN guarded connection, with the transaction already begun, so `begin_transaction()` is a
no-op and the whole chain is ONE guarded transaction (F2). `env.py` is not used (it only
builds an engine from the environment); row B2-E1 pins equality with stock
`alembic upgrade head`. `ScriptDirectory._upgrade_revs` is private: this driver is
synthetic-only and verified at the locked alembic version.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import psycopg
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, pool, text

from haloflow.synthetic.guard import guard_sync
from haloflow.synthetic.hooks import fire

SCRIPT_LOCATION: Final = str(Path(__file__).resolve().parents[3] / "alembic")


def upgrade_guarded(conninfo: str, expected: str, *,
                    phase: str | None = None) -> tuple[str | None, str]:
    """Upgrade to head in ONE guarded transaction; returns (revision before, revision after)."""

    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    script = ScriptDirectory.from_config(cfg)
    engine = create_engine("postgresql+psycopg://", poolclass=pool.NullPool,
                           creator=lambda: psycopg.connect(conninfo))

    @event.listens_for(engine, "connect")
    def _read_only(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("SET default_transaction_read_only = on")
        cur.close()
        dbapi_conn.commit()

    try:
        with engine.connect() as conn:
            trans = conn.begin()
            driver = conn.connection.driver_connection
            assert driver is not None
            guard_sync(driver, expected)
            pid = int(driver.info.backend_pid)
            exists = conn.execute(text(
                "SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar()
            before = conn.execute(text("SELECT version_num FROM public.alembic_version")
                                  ).scalar() if exists else None

            def upgrade(rev: Any, _ctx: Any) -> Any:
                return script._upgrade_revs("head", rev)

            with EnvironmentContext(cfg, script, fn=upgrade, destination_rev="head") as env:
                env.configure(connection=conn, target_metadata=None)
                with env.begin_transaction():      # no-op: the caller's transaction is open
                    env.run_migrations()
            after = conn.execute(text("SELECT version_num FROM public.alembic_version")
                                 ).scalar()
            fire("migrate.before_commit", {"backend_pid": pid})
            if phase is not None:
                fire(f"phase.{phase}.before_commit", {"backend_pid": pid})
            trans.commit()
            if phase is not None:
                fire(f"phase.{phase}.commit_ack", {"backend_pid": pid})
    finally:
        engine.dispose()
    return before, str(after)
