"""Approval domain model — human-in-the-loop pending decision."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ApprovalStatus(StrEnum):
    """Lifecycle of a HITL approval.

    PENDING:  created when a gate decides REQUIRE_APPROVAL; awaiting operator.
    GRANTED:  operator clicked approve; transaction may now be resumed.
    DENIED:   operator clicked deny; transaction will never execute.
    EXPIRED:  approval timed out (sweep job, not implemented in MVP).
    """
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class Approval(BaseModel):
    """A pending human decision attached to a halted transaction.

    Frozen by design: the model is a value-typed snapshot. The DB row is
    mutable (status transitions PENDING → GRANTED/DENIED via repository
    methods), but each `Approval` instance flowing through the app is a
    consistent point-in-time view. That's what the audit log needs.
    """
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    transaction_id: UUID
    fingerprint: str
    tool_name: str
    actor_agent_id: str
    requested_reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolver_id: str | None = None
    notes: str | None = None
