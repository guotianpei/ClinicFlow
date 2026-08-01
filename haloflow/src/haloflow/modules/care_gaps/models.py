"""ORM models for preventive care / care-gap outreach."""
import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from haloflow.database import Base


class CareGapSource(str, enum.Enum):
    EMR_DUE_DATE = "emr_due_date"    # pulled from EMR's preventive care due-date fields
    PAYER_LIST = "payer_list"         # uploaded payer care-gap file


class OutreachStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    RESPONDED = "responded"       # patient called/replied to schedule
    SCHEDULED = "scheduled"       # appointment subsequently booked (future)
    SUPPRESSED = "suppressed"     # opted out or already scheduled
    FAILED = "failed"


class CareGapMeasure(Base):
    """
    A preventive care measure definition.
    Seeded at provisioning; staff can add measures.

    Examples: AWV (Annual Wellness Visit), TCM (Transitional Care Management),
    BRCA (breast cancer screening), CORC (colorectal screening),
    DM_A1C (diabetes A1c), BP_FOLLOW (blood pressure follow-up).
    """
    __tablename__ = "care_gap_measures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "AWV"
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    sms_label: Mapped[str] = mapped_column(String(128), nullable=False)  # used in SMS body
    interval_months: Mapped[int | None] = mapped_column(Integer)  # null = payer-list-only
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CareGapRecord(Base):
    """
    One outreach record per (patient, measure, campaign cycle).
    Prevents duplicate outreach within the same cycle.
    """
    __tablename__ = "care_gap_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    emr_patient_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    measure_code: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[CareGapSource] = mapped_column(Enum(CareGapSource), nullable=False)

    # From payer list upload
    payer_list_upload_id: Mapped[int | None] = mapped_column(Integer, index=True)
    payer_id: Mapped[str | None] = mapped_column(String(64))

    # Due date (from EMR or payer list)
    due_date: Mapped[date | None] = mapped_column(Date)

    status: Mapped[OutreachStatus] = mapped_column(
        Enum(OutreachStatus), default=OutreachStatus.PENDING, nullable=False
    )
    notifyre_message_id: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PayerListUpload(Base):
    """
    Tracks each payer care-gap CSV/XLSX file uploaded by staff.
    Outreach runs against the patient list in the file.
    """
    __tablename__ = "payer_list_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payer_name: Mapped[str] = mapped_column(String(256), default="")
    measure_code: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    outreach_sent: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
