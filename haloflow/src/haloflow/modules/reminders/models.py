"""
ORM models for appointment reminders and no-show tracking.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from haloflow.database import Base


class ReminderStatus(str, enum.Enum):
    PENDING = "pending"          # scheduled, not yet sent
    SENT = "sent"                # SMS delivered
    CONFIRMED = "confirmed"      # patient replied YES
    DECLINED = "declined"        # patient replied NO
    NO_RESPONSE = "no_response"  # 24h window passed, no reply
    FAILED = "failed"            # SMS send failure


class ReminderRecord(Base):
    """One SMS reminder per appointment. Only one active record per appt."""
    __tablename__ = "reminder_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    emr_appt_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    emr_patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    appt_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus), default=ReminderStatus.PENDING, nullable=False
    )
    notifyre_message_id: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    reply_text: Mapped[str | None] = mapped_column(Text)

    # Set True after writing confirmation back to EMR
    emr_writeback_done: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    rebook_prompts: Mapped[list["RebookPrompt"]] = relationship(
        "RebookPrompt", back_populates="reminder", cascade="all, delete-orphan"
    )


class RebookPrompt(Base):
    """
    Sent after a no-show, within the configured window (default 24h).
    Separate table so we can track and rate-limit rebooking messages independently.
    """
    __tablename__ = "rebook_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reminder_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reminder_records.id"), nullable=False
    )
    emr_appt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    notifyre_message_id: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="sent")

    reminder: Mapped[ReminderRecord] = relationship(
        "ReminderRecord", back_populates="rebook_prompts"
    )
