"""Tool call models — the agent's intent to invoke a tool."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Actor(BaseModel):
    """The principal invoking a tool. Identity, not authorization."""
    model_config = ConfigDict(frozen=True)

    agent_id: str
    session_id: str
    run_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None  # human behind the agent, if any


class ToolCall(BaseModel):
    """An agent's intent to invoke a tool. Immutable once created."""
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    actor: Actor
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    parent_call_id: UUID | None = None

    @property
    def fingerprint(self) -> str:
        """Stable SHA-256 hash for idempotency. Same (agent, tool, args) → same fingerprint.

        Critically, this uses sorted JSON keys so {"a": 1, "b": 2} and
        {"b": 2, "a": 1} produce identical fingerprints. Without this, the
        idempotency gate would treat semantically-identical calls as distinct.
        """
        payload = {
            "agent_id": self.actor.agent_id,
            "tool_name": self.tool_name,
            "args": self.args,
        }
        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()
