"""Audit event models — append-only event-sourced log."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class AuditEventType(StrEnum):
    TRANSACTION_STARTED = "transaction_started"
    GATE_EVALUATED = "gate_evaluated"
    TOOL_DRY_RUN = "tool_dry_run"
    TOOL_EXECUTED = "tool_executed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    IDEMPOTENCY_HIT = "idempotency_hit"
    TRANSACTION_COMPLETED = "transaction_completed"
    TRANSACTION_FAILED = "transaction_failed"


class AuditEvent(BaseModel):
    """A single append-only event in the audit log.

    Audit events together form a replayable trace. Given the full sequence
    of events for a transaction, you can reconstruct exactly what Lockrail
    decided and why — which is the entire point of the audit gate.
    """
    id: UUID = Field(default_factory=uuid4)
    transaction_id: UUID
    sequence_num: int = Field(ge=0)  # ordering within transaction
    event_type: AuditEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
