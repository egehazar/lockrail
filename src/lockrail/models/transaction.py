"""Transaction context and result — the workspace and final outcome."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

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
    REPLAYED = "replayed"  # served from idempotency cache


class TransactionContext(BaseModel):
    """Mutable workspace that flows through the gate pipeline.

    Each gate appends its result. The pipeline halts on the first blocking
    decision (DENY or REQUIRE_APPROVAL).
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    transaction_id: UUID = Field(default_factory=uuid4)
    tool_call: ToolCall
    mode: TransactionMode = TransactionMode.EXECUTE
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    gate_results: list[GateResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None

    def record_gate(self, result: GateResult) -> None:
        self.gate_results.append(result)

    @property
    def last_decision(self) -> GateResult | None:
        return self.gate_results[-1] if self.gate_results else None

    @property
    def is_halted(self) -> bool:
        last = self.last_decision
        return last is not None and last.is_blocking


class TransactionResult(BaseModel):
    """Final, immutable outcome of a transaction."""
    transaction_id: UUID
    tool_call_id: UUID
    status: TransactionStatus
    gate_results: list[GateResult]
    execution_output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = Field(ge=0)
    started_at: datetime
    completed_at: datetime
