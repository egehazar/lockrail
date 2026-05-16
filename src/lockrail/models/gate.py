"""Gate result models — every gate produces one of these."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class GateDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    SKIP = "skip"  # gate not applicable to this call


class GateResult(BaseModel):
    """Outcome of a single gate evaluating a tool call."""
    gate_name: str
    decision: GateDecision
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = Field(ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_blocking(self) -> bool:
        """True if this decision halts the pipeline."""
        return self.decision in (GateDecision.DENY, GateDecision.REQUIRE_APPROVAL)
