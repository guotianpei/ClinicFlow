"""``python -m haloflow_demo reset-synthetic``: clear synthetic demo state.

This is an operator command, separate from start-up (bootstrap stays
non-destructive, T13). It:
- refuses while the host process lock is held (the server is running);
- checks the connected identity and the schema version.

Truncated: the synthetic tables only, namely reminders, replies, candidate
snapshots, follow-up items and write-back tasks. Identities are never
restarted, so no ID is reused.

Preserved: ``send_quota`` (today's allowance cannot be regained),
``opt_outs``, ``contact_history`` (its reminder references are nulled, so no
later row inherits a contact), ``demo_meta`` including ``phone_key_secret``,
``demo_settings``, ``demo_match_lock`` and the ``match_seq`` sequence.

Older schemas: no preserving upgrade exists. A database at another schema
version is refused (``demo_schema_upgrade_unavailable``), and this command does
not offer to rebuild it. Every v2 build refused real SMS and real callbacks, so
a v2 database can hold only stub-provenance state. Any schema change made after
a real send must ship a preserving upgrade before it is used.

Limit: deleting the whole database by hand is outside what this code can
prevent.
"""

from __future__ import annotations

from sqlalchemy import text

from haloflow_demo.db import check_identity, make_demo_engine, read_identity
from haloflow_demo.models import SCHEMA_VERSION, SYNTHETIC_TABLES
from haloflow_demo.process_lock import ProcessLock
from haloflow_demo.settings import DemoSettings, StartupRefused, validate_demo_database_url


async def reset_synthetic(settings: DemoSettings) -> None:
    target = validate_demo_database_url(settings)
    lock = ProcessLock()
    lock.acquire()  # StartupRefused("process_lock_held") while the server runs
    try:
        engine = make_demo_engine(target)
        try:
            async with engine.begin() as conn:
                check_identity(await read_identity(conn), target)
                found = (await conn.execute(text(
                    "SELECT value FROM demo_meta WHERE key = 'schema_version'"))).scalar_one()
                if found != str(SCHEMA_VERSION):
                    # No preserving upgrade exists for older schemas. See the module docstring.
                    raise StartupRefused("demo_schema_upgrade_unavailable",
                                         f"database has {found}")
                await conn.execute(text("UPDATE contact_history SET reminder_id = NULL"))
                await conn.execute(text("TRUNCATE " + ", ".join(SYNTHETIC_TABLES)))
        finally:
            await engine.dispose()
    finally:
        lock.release()
