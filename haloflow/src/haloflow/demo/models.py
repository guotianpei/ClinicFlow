"""Demo-only tables (package v2 §2.2) on a separate metadata (deviation D-1).

``create_demo_tables`` checks the connected identity, then creates the eight
tables with ``checkfirst``. It never issues DROP, TRUNCATE or DELETE.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haloflow.demo.db import check_identity, read_identity
from haloflow.demo.settings import DemoDbTarget

SCHEMA_VERSION = 1


class DemoBase(DeclarativeBase):
    """Declarative base for demo tables only; never shares the legacy metadata."""


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
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


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
    phone_key: Mapped[str | None] = mapped_column(String(128))
    masked_from: Mapped[str | None] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    received_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class FollowUpItem(DemoBase):
    __tablename__ = "follow_up_items"
    __table_args__ = (UniqueConstraint("source_key", "kind", name="uq_follow_up_source_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
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


async def create_demo_tables(engine: AsyncEngine, target: DemoDbTarget) -> None:
    """Identity check, then ``CREATE TABLE`` with ``checkfirst``. Never destructive."""
    async with engine.begin() as conn:
        check_identity(await read_identity(conn), target)
        await conn.run_sync(DemoBase.metadata.create_all, checkfirst=True)
        await conn.execute(
            text("INSERT INTO demo_meta(key, value) VALUES ('schema_version', :v) "
                 "ON CONFLICT (key) DO NOTHING"),
            {"v": str(SCHEMA_VERSION)},
        )
