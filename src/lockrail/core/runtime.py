"""Transaction runtime — orchestrates the gate pipeline around a tool call."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ..gates.base import Gate
from ..models import (
    GateDecision,
    ToolCall,
    TransactionContext,
    TransactionMode,
    TransactionResult,
    TransactionStatus,
)

ToolExecutor = Callable[[ToolCall], Awaitable[dict[str, Any]]]


class Runtime:
    """Lockrail's transaction runtime.

    Wraps a tool call in a gate pipeline. Pipeline halts on the first
    blocking decision (DENY or REQUIRE_APPROVAL). If all gates pass and
    mode is EXECUTE, the configured executor is invoked.

    The runtime itself does NOT know about MCP, agents, or specific tools.
    It only sees `ToolCall` and an opaque async `executor` callable. This
    keeps Lockrail framework-agnostic: the same runtime works for MCP
    middleware, FastAPI tool routes, or direct Python integration.
    """

    def __init__(
        self,
        gates: list[Gate],
        executor: ToolExecutor | None = None,
    ) -> None:
        if not gates:
            raise ValueError("Runtime requires at least one gate")
        self.gates = gates
        self.executor = executor

    async def submit(
        self,
        tool_call: ToolCall,
        mode: TransactionMode = TransactionMode.EXECUTE,
        trace_id: str | None = None,
    ) -> TransactionResult:
        ctx = TransactionContext(tool_call=tool_call, mode=mode, trace_id=trace_id)
        started_at = ctx.started_at

        # --- Gate pipeline ---
        for gate in self.gates:
            result = await gate.run(ctx)
            ctx.record_gate(result)
            if result.is_blocking:
                break

        # --- Determine outcome ---
        status, output, error = await self._finalize(ctx)
        completed_at = datetime.now(timezone.utc)

        return TransactionResult(
            transaction_id=ctx.transaction_id,
            tool_call_id=tool_call.id,
            status=status,
            gate_results=list(ctx.gate_results),
            execution_output=output,
            error=error,
            duration_ms=(completed_at - started_at).total_seconds() * 1000,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _finalize(
        self, ctx: TransactionContext
    ) -> tuple[TransactionStatus, dict[str, Any] | None, str | None]:
        """Decide final status, optionally invoking the executor."""
        if ctx.is_halted:
            last = ctx.last_decision
            assert last is not None  # is_halted implies last_decision exists
            status = (
                TransactionStatus.BLOCKED
                if last.decision == GateDecision.DENY
                else TransactionStatus.PENDING_APPROVAL
            )
            return status, None, None

        # No blocking gate — proceed.
        if ctx.mode == TransactionMode.DRY_RUN or self.executor is None:
            # Dry-run never invokes the executor; same when no executor configured.
            return TransactionStatus.EXECUTED, None, None

        try:
            output = await self.executor(ctx.tool_call)
            return TransactionStatus.EXECUTED, output, None
        except Exception as exc:
            return TransactionStatus.FAILED, None, f"{type(exc).__name__}: {exc}"
