"""Transaction runtime — orchestrates gates, emits audit events, persists."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gates.base import Gate
from ..models import (
    AuditEventType,
    GateDecision,
    MetadataKeys,
    ToolCall,
    TransactionContext,
    TransactionMode,
    TransactionResult,
    TransactionStatus,
)
from ..storage import AuditRepository, IdempotencyStore

ToolExecutor = Callable[[ToolCall], Awaitable[dict[str, Any]]]

logger = logging.getLogger(__name__)


class Runtime:
    """Lockrail transaction runtime.

    Pipeline:
      1. Emit TRANSACTION_STARTED audit event.
      2. Run gates in order. After each: emit GATE_EVALUATED event.
         Halt on first blocking decision.
      3. If IdempotencyGate signaled a cache hit: short-circuit to REPLAYED,
         use cached output, emit IDEMPOTENCY_HIT.
      4. Otherwise if not halted and mode==EXECUTE: invoke executor.
         Emit TOOL_EXECUTED or TRANSACTION_FAILED.
      5. Build TransactionResult. Emit TRANSACTION_COMPLETED.
      6. Side effects: write to idempotency cache (if EXECUTED), persist
         transaction + audit events (if session_factory provided).
    """

    def __init__(
        self,
        gates: list[Gate],
        executor: ToolExecutor | None = None,
        idempotency_store: IdempotencyStore | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if not gates:
            raise ValueError("Runtime requires at least one gate")
        self.gates = gates
        self.executor = executor
        self.idempotency_store = idempotency_store
        self.session_factory = session_factory

    async def submit(
        self,
        tool_call: ToolCall,
        mode: TransactionMode = TransactionMode.EXECUTE,
        trace_id: str | None = None,
    ) -> TransactionResult:
        ctx = TransactionContext(tool_call=tool_call, mode=mode, trace_id=trace_id)
        started_at = ctx.started_at

        ctx.emit_event(AuditEventType.TRANSACTION_STARTED, payload={
            "tool_name": tool_call.tool_name,
            "fingerprint": tool_call.fingerprint[:12],
            "mode": mode.value,
        })

        # --- Gate pipeline ---
        for gate in self.gates:
            result = await gate.run(ctx)
            ctx.record_gate(result)
            ctx.emit_event(AuditEventType.GATE_EVALUATED, payload={
                "gate_name": result.gate_name,
                "decision": result.decision.value,
                "reason": result.reason,
                "details": result.details,
                "duration_ms": result.duration_ms,
            })
            if result.is_blocking:
                break

        # --- Determine outcome ---
        status, output, error = await self._finalize(ctx)
        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - started_at).total_seconds() * 1000

        result = TransactionResult(
            transaction_id=ctx.transaction_id,
            tool_call_id=tool_call.id,
            status=status,
            gate_results=list(ctx.gate_results),
            execution_output=output,
            error=error,
            duration_ms=duration_ms,
            started_at=started_at,
            completed_at=completed_at,
        )

        # --- Side effects: cache + persist ---
        await self._cache_if_executed(tool_call, result)
        await self._persist(ctx, result, tool_call, trace_id)

        return result

    async def _finalize(
        self, ctx: TransactionContext
    ) -> tuple[TransactionStatus, dict[str, Any] | None, str | None]:
        # Cache hit short-circuit (set by IdempotencyGate)
        if ctx.metadata.get(MetadataKeys.IDEMPOTENCY_CACHE_HIT):
            ctx.emit_event(AuditEventType.IDEMPOTENCY_HIT, payload={
                "fingerprint": ctx.tool_call.fingerprint[:12],
                "prior_transaction_id": ctx.metadata.get(MetadataKeys.IDEMPOTENCY_PRIOR_TX_ID),
            })
            return (
                TransactionStatus.REPLAYED,
                ctx.metadata.get(MetadataKeys.IDEMPOTENCY_CACHED_OUTPUT),
                None,
            )

        if ctx.is_halted:
            last = ctx.last_decision
            assert last is not None
            if last.decision == GateDecision.DENY:
                ctx.emit_event(AuditEventType.TRANSACTION_FAILED, payload={
                    "reason": "blocked_by_gate",
                    "gate": last.gate_name,
                    "details": last.details,
                })
                return TransactionStatus.BLOCKED, None, None
            ctx.emit_event(AuditEventType.APPROVAL_REQUESTED, payload={
                "gate": last.gate_name,
                "reason": last.reason,
            })
            return TransactionStatus.PENDING_APPROVAL, None, None

        if ctx.mode == TransactionMode.DRY_RUN or self.executor is None:
            ctx.emit_event(AuditEventType.TRANSACTION_COMPLETED, payload={
                "status": TransactionStatus.EXECUTED.value,
                "executed": False,
                "reason": "dry_run" if ctx.mode == TransactionMode.DRY_RUN else "no_executor",
            })
            return TransactionStatus.EXECUTED, None, None

        try:
            output = await self.executor(ctx.tool_call)
            ctx.emit_event(AuditEventType.TOOL_EXECUTED, payload={
                "tool_name": ctx.tool_call.tool_name,
            })
            ctx.emit_event(AuditEventType.TRANSACTION_COMPLETED, payload={
                "status": TransactionStatus.EXECUTED.value,
            })
            return TransactionStatus.EXECUTED, output, None
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            ctx.emit_event(AuditEventType.TRANSACTION_FAILED, payload={"error": err})
            return TransactionStatus.FAILED, None, err

    async def _cache_if_executed(self, tool_call: ToolCall, result: TransactionResult) -> None:
        if self.idempotency_store is None:
            return
        if result.status != TransactionStatus.EXECUTED:
            return
        try:
            await self.idempotency_store.set(tool_call.fingerprint, result)
        except Exception as exc:
            # Cache write failure must not fail the transaction.
            logger.warning("idempotency_cache_write_failed: %s", exc, exc_info=True)

    async def _persist(
        self,
        ctx: TransactionContext,
        result: TransactionResult,
        tool_call: ToolCall,
        trace_id: str | None,
    ) -> None:
        if self.session_factory is None:
            return
        try:
            async with self.session_factory() as session:
                repo = AuditRepository(session)
                await repo.record(result, tool_call, ctx.audit_events, trace_id=trace_id)
        except Exception as exc:
            # Audit persistence failure is logged but does not fail the transaction.
            # In prod we'd alert on this; the in-memory result is still returned.
            logger.error(
                "audit_persist_failed: tx=%s err=%s",
                ctx.transaction_id, exc, exc_info=True,
            )
