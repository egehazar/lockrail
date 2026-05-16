"""Model-level invariants. If these break, everything else is suspect."""
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from lockrail.models import (
    Actor,
    AuditEvent,
    AuditEventType,
    GateDecision,
    GateResult,
    ToolCall,
    TransactionContext,
)


def _make_call(args: dict | None = None) -> ToolCall:
    actor = Actor(agent_id="agent-1", session_id="sess-1")
    return ToolCall(actor=actor, tool_name="refund", args=args or {"amount": 100})


def test_actor_is_frozen() -> None:
    actor = Actor(agent_id="a", session_id="s")
    with pytest.raises(ValidationError):
        actor.agent_id = "b"  # type: ignore[misc]


def test_tool_call_is_frozen() -> None:
    call = _make_call()
    with pytest.raises(ValidationError):
        call.tool_name = "other"  # type: ignore[misc]


def test_fingerprint_is_deterministic_across_arg_order() -> None:
    actor = Actor(agent_id="agent-1", session_id="sess-1")
    a = ToolCall(actor=actor, tool_name="refund",
                 args={"amount": 100, "order_id": "ord-1"})
    b = ToolCall(actor=actor, tool_name="refund",
                 args={"order_id": "ord-1", "amount": 100})
    assert a.fingerprint == b.fingerprint


def test_fingerprint_differs_on_different_args() -> None:
    a = _make_call({"amount": 100})
    b = _make_call({"amount": 200})
    assert a.fingerprint != b.fingerprint


def test_fingerprint_differs_on_different_tool() -> None:
    actor = Actor(agent_id="agent-1", session_id="sess-1")
    a = ToolCall(actor=actor, tool_name="refund", args={"x": 1})
    b = ToolCall(actor=actor, tool_name="charge", args={"x": 1})
    assert a.fingerprint != b.fingerprint


def test_gate_result_is_blocking_only_for_deny_and_approval() -> None:
    base = {"gate_name": "policy", "reason": "ok", "duration_ms": 1.0}
    assert GateResult(decision=GateDecision.DENY, **base).is_blocking
    assert GateResult(decision=GateDecision.REQUIRE_APPROVAL, **base).is_blocking
    assert not GateResult(decision=GateDecision.ALLOW, **base).is_blocking
    assert not GateResult(decision=GateDecision.SKIP, **base).is_blocking


def test_transaction_context_records_and_halts() -> None:
    ctx = TransactionContext(tool_call=_make_call())
    assert ctx.last_decision is None
    assert not ctx.is_halted

    ctx.record_gate(GateResult(
        gate_name="evidence", decision=GateDecision.ALLOW,
        reason="all fields present", duration_ms=0.5,
    ))
    assert not ctx.is_halted

    ctx.record_gate(GateResult(
        gate_name="policy", decision=GateDecision.DENY,
        reason="amount over limit", duration_ms=0.3,
    ))
    assert ctx.is_halted
    assert ctx.last_decision is not None
    assert ctx.last_decision.gate_name == "policy"


def test_audit_event_has_sane_defaults() -> None:
    ctx = TransactionContext(tool_call=_make_call())
    event = AuditEvent(
        transaction_id=ctx.transaction_id,
        sequence_num=0,
        event_type=AuditEventType.TRANSACTION_STARTED,
    )
    assert isinstance(event.id, UUID)
    assert isinstance(event.timestamp, datetime)
    assert event.payload == {}
