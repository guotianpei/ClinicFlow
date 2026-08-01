"""ORM models for fax routing — inbound and outbound."""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from haloflow.database import Base


class FaxDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class FaxQueueStatus(str, enum.Enum):
    RECEIVED = "received"       # inbound: arrived
    ROUTED = "routed"           # inbound: assigned to a queue
    UNROUTED = "unrouted"       # inbound: no routing rule matched — staff review
    QUEUED = "queued"           # outbound: in fax API queue
    SENT = "sent"               # outbound: delivery confirmed
    FAILED = "failed"


class FaxRoutingRule(Base):
    """
    Maps inbound sender numbers (or number prefixes) to destination queues.
    Managed by staff; seeded during Tier 1 provisioning.

    Examples:
      - from_number=+15405551234  → queue="labs" (Quest Diagnostics local)
      - from_number_prefix=+1800  → queue="insurance"
      - catch_all=True            → queue="general"
    """
    __tablename__ = "fax_routing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_number: Mapped[str | None] = mapped_column(String(32), index=True)
    from_number_prefix: Mapped[str | None] = mapped_column(String(16))
    sender_org_name: Mapped[str | None] = mapped_column(String(256))
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    is_catch_all: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = higher priority
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FaxRecord(Base):
    """
    One record per fax, inbound or outbound.
    No fax content stored here — content stays in Notifyre storage until retrieved.
    """
    __tablename__ = "fax_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[FaxDirection] = mapped_column(Enum(FaxDirection), nullable=False)
    status: Mapped[FaxQueueStatus] = mapped_column(
        Enum(FaxQueueStatus), nullable=False
    )

    # Fax API identifiers
    external_fax_id: Mapped[str | None] = mapped_column(String(256))
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    pages: Mapped[int | None] = mapped_column(Integer)

    # Routing (inbound)
    routed_to_queue: Mapped[str | None] = mapped_column(String(64))
    routing_rule_id: Mapped[int | None] = mapped_column(Integer)

    # Outbound metadata (populated from EMR cover sheet data)
    emr_patient_id: Mapped[str | None] = mapped_column(String(128))
    subject: Mapped[str | None] = mapped_column(String(256))
    receiving_org: Mapped[str | None] = mapped_column(String(256))

    # Delivery confirmation
    delivery_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    emr_logged: Mapped[bool] = mapped_column(Boolean, default=False)  # writeback to chart

    notes: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
