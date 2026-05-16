"""ORM models. Mirrors of the Pydantic domain models, optimized for persistence.

Why denormalize Actor fields onto TransactionRow rather than store as JSON?
Because every meaningful query — "all transactions by agent X", "all calls
in tenant Y" — needs an indexable column, not a JSON path. Denormalization
costs 4 columns and buys us first-class indexes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..models.audit import AuditEventType
from ..models.transaction import TransactionStatus
from .db import Base


class TransactionRow(Base):
    __tablename__ = "transactions"

    transaction_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tool_call_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), index=True)

    # Actor (denormalized for query performance)
    actor_agent_id: Mapped[str] = mapped_column(String(255), index=True)
    actor_session_id: Mapped[str] = mapped_column(String(255), index=True)
    actor_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    actor_tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Tool call
    tool_name: Mapped[str] = mapped_column(String(255), index=True)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)

    # Outcome
    status: Mapped[TransactionStatus] = mapped_column(
        SAEnum(TransactionStatus, name="transaction_status"),
        index=True,
    )
    execution_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    # Timing + tracing
    duration_ms: Mapped[float]
    started_at: Mapped[datetime] = mapped_column(index=True)
    completed_at: Mapped[datetime]
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    audit_events: Mapped[list[AuditEventRow]] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="AuditEventRow.sequence_num",
    )


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    transaction_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("transactions.transaction_id", ondelete="CASCADE"),
        index=True,
    )
    sequence_num: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[AuditEventType] = mapped_column(
        SAEnum(AuditEventType, name="audit_event_type"),
        index=True,
    )
    timestamp: Mapped[datetime] = mapped_column(index=True)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    transaction: Mapped[TransactionRow] = relationship(back_populates="audit_events")

    __table_args__ = (
        # (transaction_id, sequence_num) must be unique — guarantees total order
        # within a transaction even under clock skew.
        Index("ix_audit_tx_sequence", "transaction_id", "sequence_num", unique=True),
    )
