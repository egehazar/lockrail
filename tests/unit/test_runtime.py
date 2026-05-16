"""Runtime + Gate pipeline behavior. Verifies the architectural keystone."""
from __future__ import annotations

import pytest

from lockrail.core import Runtime
from lockrail.gates import Gate
from lockrail.models import (
    Actor,
    GateDecision,
    GateResult,
    ToolCall,
    TransactionContext,
    TransactionMode,
    TransactionStatus,
)


# --- Test helpers: minimal gates for pipeline shape verification ---


class AllowGate(Gate):
    name = "allow"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        return self.allow("test allow")


class DenyGate(Gate):
    name = "deny"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        return self.deny("test deny")


class ApprovalGate(Gate):
    name = "approval"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        return self.require_approval("test approval")


class SkipGate(Gate):
    name = "skip"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        return self.skip("not applicable")


class ExplodingGate(Gate):
    name = "exploding"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        raise RuntimeError("boom")


class RecordingGate(Gate):
    """Tracks whether it ran — used to verify halt-on-block semantics."""
    name = "recording"

    def __init__(self) -> None:
        super().__init__()
        self.ran = False

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        self.ran = True
        return self.allow("recorded")


def _call() -> ToolCall:
    return ToolCall(
        actor=Actor(agent_id="a", session_id="s"),
        tool_name="refund",
        args={"amount": 100},
    )


# --- Tests ---


async def test_empty_gates_raises() -> None:
    with pytest.raises(ValueError):
        Runtime(gates=[])


async def test_all_allow_executes_when_no_executor() -> None:
    rt = Runtime(gates=[AllowGate(), AllowGate()])
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.EXECUTED
    assert len(result.gate_results) == 2
    assert all(r.decision == GateDecision.ALLOW for r in result.gate_results)


async def test_deny_blocks_and_halts_pipeline() -> None:
    recorder = RecordingGate()
    rt = Runtime(gates=[AllowGate(), DenyGate(), recorder])
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.BLOCKED
    assert result.gate_results[-1].decision == GateDecision.DENY
    assert recorder.ran is False, "gate after DENY must not run"


async def test_approval_halts_and_sets_pending() -> None:
    recorder = RecordingGate()
    rt = Runtime(gates=[ApprovalGate(), recorder])
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.PENDING_APPROVAL
    assert recorder.ran is False


async def test_skip_does_not_halt() -> None:
    rt = Runtime(gates=[SkipGate(), AllowGate()])
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.EXECUTED
    assert result.gate_results[0].decision == GateDecision.SKIP
    assert result.gate_results[1].decision == GateDecision.ALLOW


async def test_exception_in_gate_becomes_deny() -> None:
    rt = Runtime(gates=[ExplodingGate()])
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.BLOCKED
    last = result.gate_results[-1]
    assert last.decision == GateDecision.DENY
    assert "RuntimeError" in last.reason
    assert last.details.get("error_type") == "RuntimeError"


async def test_dry_run_mode_skips_executor() -> None:
    calls = []

    async def executor(tc: ToolCall) -> dict:
        calls.append(tc)
        return {"executed": True}

    rt = Runtime(gates=[AllowGate()], executor=executor)
    result = await rt.submit(_call(), mode=TransactionMode.DRY_RUN)
    assert result.status == TransactionStatus.EXECUTED
    assert result.execution_output is None
    assert calls == [], "executor must not be called in DRY_RUN mode"


async def test_execute_mode_invokes_executor() -> None:
    async def executor(tc: ToolCall) -> dict:
        return {"executed": True, "tool": tc.tool_name}

    rt = Runtime(gates=[AllowGate()], executor=executor)
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.EXECUTED
    assert result.execution_output == {"executed": True, "tool": "refund"}


async def test_executor_exception_becomes_failed() -> None:
    async def executor(tc: ToolCall) -> dict:
        raise ValueError("tool unavailable")

    rt = Runtime(gates=[AllowGate()], executor=executor)
    result = await rt.submit(_call())
    assert result.status == TransactionStatus.FAILED
    assert result.error is not None
    assert "ValueError" in result.error
    assert "tool unavailable" in result.error


async def test_gate_duration_is_populated() -> None:
    rt = Runtime(gates=[AllowGate()])
    result = await rt.submit(_call())
    assert result.gate_results[0].duration_ms >= 0
    assert result.duration_ms >= 0


async def test_subclass_without_name_fails() -> None:
    with pytest.raises(TypeError):
        class BadGate(Gate):  # type: ignore[unused-ignore]
            async def _evaluate(self, ctx: TransactionContext) -> GateResult:
                return self.allow("ok")
