"""ORM models for insurance eligibility checks."""
import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from haloflow.database import Base


class EligibilityCheckStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"               # Coverage confirmed
    INACTIVE = "inactive"           # Coverage lapsed/not found
    MANUAL_REQUIRED = "manual"      # Non-priority payer — staff must verify
    ERROR = "error"                 # API or parsing failure


class EligibilityCheck(Base):
    """
    One record per (appointment, payer) eligibility check.
    Raw 271 response stored in `raw_response` for audit trail.
    """
    __tablename__ = "eligibility_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    emr_appt_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    emr_patient_id: Mapped[str] = mapped_column(String(128), nullable=False)
    appt_date: Mapped[date] = mapped_column(Date, nullable=False)

    payer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payer_name: Mapped[str] = mapped_column(String(256), default="")
    member_id: Mapped[str | None] = mapped_column(String(128))
    group_number: Mapped[str | None] = mapped_column(String(128))
    plan_name: Mapped[str | None] = mapped_column(String(256))
    office_visit_copay: Mapped[str | None] = mapped_column(String(32))
    coverage_begin: Mapped[date | None] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date)

    status: Mapped[EligibilityCheckStatus] = mapped_column(
        Enum(EligibilityCheckStatus),
        default=EligibilityCheckStatus.PENDING,
        nullable=False,
    )
    is_priority_payer: Mapped[bool] = mapped_column(Boolean, default=False)
    office_notified: Mapped[bool] = mapped_column(Boolean, default=False)  # for INACTIVE results

    raw_response: Mapped[dict | None] = mapped_column(JSON)  # full 271 payload
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
