"""Transaction context and result."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditEvent, AuditEventType
from .gate import GateResult
from .tool_call import ToolCall


class TransactionMode(StrEnum):
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


class TransactionStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"
    FAILED = "failed"
    REPLAYED = "replayed"


class MetadataKeys:
    """Well-known keys gates use to communicate with the Runtime via ctx.metadata."""
    IDEMPOTENCY_CACHE_HIT = "_idempotency_cache_hit"
    IDEMPOTENCY_CACHED_OUTPUT = "_idempotency_cached_output"
    IDEMPOTENCY_PRIOR_TX_ID = "_idempotency_prior_tx_id"


class TransactionContext(BaseModel):
    """Mutable workspace that flows through the gate pipeline."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    transaction_id: UUID = Field(default_factory=uuid4)
    tool_call: ToolCall
    mode: TransactionMode = TransactionMode.EXECUTE
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    gate_results: list[GateResult] = Field(default_factory=list)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None

    def record_gate(self, result: GateResult) -> None:
        self.gate_results.append(result)

    def emit_event(
        self,
        event_type: AuditEventType,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append a new audit event. Sequence number is auto-assigned."""
        event = AuditEvent(
            transaction_id=self.transaction_id,
            sequence_num=len(self.audit_events),
            event_type=event_type,
            payload=payload or {},
            actor_id=self.tool_call.actor.agent_id,
            trace_id=self.trace_id,
        )
        self.audit_events.append(event)
        return event

    @property
    def last_decision(self) -> GateResult | None:
        return self.gate_results[-1] if self.gate_results else None

    @property
    def is_halted(self) -> bool:
        last = self.last_decision
        return last is not None and last.is_blocking


class TransactionResult(BaseModel):
    """Final outcome of a transaction."""
    transaction_id: UUID
    tool_call_id: UUID
    status: TransactionStatus
    gate_results: list[GateResult]
    execution_output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = Field(ge=0)
    started_at: datetime
    completed_at: datetime
