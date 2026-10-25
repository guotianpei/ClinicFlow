"""Demo-only tables (package v2 §2.2) on a separate metadata (deviation D-1).

``create_demo_tables`` checks the connected identity, then creates the tables
and the ``match_seq`` sequence with ``checkfirst``. It never issues DROP,
TRUNCATE or DELETE.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haloflow_demo.db import check_identity, read_identity
from haloflow_demo.settings import DemoDbTarget, StartupRefused

SCHEMA_VERSION = 3
# 2: reminder_records.reserved_day; demo_meta phone_key_secret
# 3 (Day 5): contact history, receipt snapshot, match lock, inbound fields


class DemoBase(DeclarativeBase):
    """Declarative base for demo tables only; never shares the legacy metadata."""


# Ordering token for candidate membership (package v1r2). It is drawn only after
# the demo_match_lock row lock is held, so its order is the lock-acquisition
# order. Never reset: a reset truncates synthetic tables, not the sequence.
MATCH_SEQ = Sequence("match_seq", metadata=DemoBase.metadata)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ReminderRecord(DemoBase):
    __tablename__ = "reminder_records"
    __table_args__ = (UniqueConstraint("tenant_id", "emr_appt_id", name="uq_reminder_appt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    emr_appt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    emr_patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    appt_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str | None] = mapped_column(String(16))
    provider_message_id: Mapped[str | None] = mapped_column(String(256))
    reserved_day: Mapped[date | None] = mapped_column(Date)
    phone_key: Mapped[str | None] = mapped_column(String(128))
    contact_seq: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class ContactHistory(DemoBase):
    """Durable record of every send admitted for dispatch (preserved by reset).

    ``reminder_id`` has no foreign key: a reset nulls it so a later reminder that
    reuses nothing can never inherit an old contact. ``outcome`` NULL means in
    flight or crashed; NOT_SENT and REJECTED are certain no-message outcomes.
    """

    __tablename__ = "contact_history"

    contact_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    phone_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reminder_id: Mapped[int | None] = mapped_column(Integer)
    appt_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[str] = mapped_column(String(8), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _ts()


class DemoMatchLock(DemoBase):
    __tablename__ = "demo_match_lock"
    __table_args__ = (CheckConstraint("id = 1", name="ck_demo_match_lock_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class SendQuota(DemoBase):
    __tablename__ = "send_quota"
    __table_args__ = (CheckConstraint("used BETWEEN 1 AND 10", name="ck_send_quota_used"),)

    clinic_day: Mapped[date] = mapped_column(Date, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False)


class DemoSettingsRow(DemoBase):
    __tablename__ = "demo_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_demo_settings_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paused: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    owner_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class OptOut(DemoBase):
    __tablename__ = "opt_outs"

    phone_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = _ts()


class InboundMessage(DemoBase):
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_event_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provenance: Mapped[str] = mapped_column(String(8), nullable=False)
    simulated_correlation: Mapped[int | None] = mapped_column(Integer)
    phone_key: Mapped[str] = mapped_column(String(128), nullable=False)
    masked_from: Mapped[str | None] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text, nullable=False)  # redacted text only
    cls: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    baseline_reminder_id: Mapped[int | None] = mapped_column(Integer)
    baseline_value: Mapped[str | None] = mapped_column(String(32))
    baseline_ok: Mapped[bool | None] = mapped_column(Boolean)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = _ts()


class InboundCandidate(DemoBase):
    """Immutable receipt-time snapshot of the contacts a reply could answer."""

    __tablename__ = "inbound_candidates"
    __table_args__ = (PrimaryKeyConstraint("inbound_id", "contact_seq"),)

    inbound_id: Mapped[int] = mapped_column(Integer, ForeignKey("inbound_messages.id"))
    contact_seq: Mapped[int] = mapped_column(BigInteger)
    reminder_id: Mapped[int | None] = mapped_column(Integer)
    appt_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_at_receipt: Mapped[str] = mapped_column(String(32), nullable=False)


class FollowUpItem(DemoBase):
    __tablename__ = "follow_up_items"
    __table_args__ = (UniqueConstraint("source_key", "kind", name="uq_follow_up_source_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    inbound_id: Mapped[int | None] = mapped_column(Integer)
    reminder_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class WritebackTask(DemoBase):
    __tablename__ = "writeback_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reminder_records.id"), nullable=False, unique=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    target_value: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_before: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class DemoMeta(DemoBase):
    __tablename__ = "demo_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)


DEMO_TABLES: tuple[str, ...] = tuple(DemoBase.metadata.tables)
# Truncated by ``reset-synthetic``; everything else (quota, opt-outs, contact
# history, settings, meta, match lock) is preserved.
SYNTHETIC_TABLES: tuple[str, ...] = ("inbound_candidates", "inbound_messages",
                                     "follow_up_items", "writeback_tasks", "reminder_records")


async def create_demo_tables(engine: AsyncEngine, target: DemoDbTarget) -> None:
    """Identity check, then ``CREATE TABLE`` with ``checkfirst``. Never destructive.

    There is no migration: a demo database created by an older schema version is
    refused (``demo_schema_mismatch``); use a fresh ``*_demo`` database.
    """
    async with engine.begin() as conn:
        check_identity(await read_identity(conn), target)
        await conn.run_sync(DemoBase.metadata.create_all, checkfirst=True)
        await conn.execute(
            text("INSERT INTO demo_meta(key, value) VALUES ('schema_version', :v), "
                 "('phone_key_secret', :s) ON CONFLICT (key) DO NOTHING"),
            {"v": str(SCHEMA_VERSION), "s": secrets.token_hex(32)},
        )
        await conn.execute(text("INSERT INTO demo_match_lock(id) VALUES (1) "
                                "ON CONFLICT (id) DO NOTHING"))
        found = (await conn.execute(
            text("SELECT value FROM demo_meta WHERE key = 'schema_version'")
        )).scalar_one()
        if found != str(SCHEMA_VERSION):
            raise StartupRefused("demo_schema_mismatch", f"database has {found}")
