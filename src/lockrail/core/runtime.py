"""Transaction runtime — orchestrates gates, emits audit events, persists."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gates.base import Gate
from ..models import (
    Actor,
    Approval,
    ApprovalStatus,
    AuditEvent,
    AuditEventType,
    GateDecision,
    MetadataKeys,
    ToolCall,
    TransactionContext,
    TransactionMode,
    TransactionResult,
    TransactionStatus,
)
from ..storage import ApprovalRepository, AuditRepository, IdempotencyStore
from .exceptions import ApprovalNotFoundError, ApprovalNotGrantedError

ToolExecutor = Callable[[ToolCall], Awaitable[dict[str, Any]]]
ApprovalRepositoryFactory = Callable[[AsyncSession], ApprovalRepository]

logger = logging.getLogger(__name__)


class Runtime:
    """Lockrail transaction runtime.

    submit() pipeline:
      1. Emit TRANSACTION_STARTED audit event.
      2. Run gates in order. After each: emit GATE_EVALUATED event.
         Halt on first blocking decision.
      3. If IdempotencyGate signaled a cache hit: short-circuit to REPLAYED,
         use cached output, emit IDEMPOTENCY_HIT.
      4. Otherwise if not halted and mode==EXECUTE: invoke executor.
         Emit TOOL_EXECUTED or TRANSACTION_FAILED.
      5. Build TransactionResult. Emit TRANSACTION_COMPLETED.
      6. Side effects: write to idempotency cache (if EXECUTED), persist
         transaction + audit events, and (if PENDING_APPROVAL) plant the
         approval queue row.

    resume() flow:
      1. Look up original transaction + approval rows.
      2. Validate approval status == GRANTED; else raise.
      3. Reconstruct the original ToolCall from stored fields.
      4. Execute the tool directly (no gates — see Step 10 notes).
      5. Persist a new transaction linked to the original via
         `resumed_from` in the audit log.
    """

    def __init__(
        self,
        gates: list[Gate],
        executor: ToolExecutor | None = None,
        idempotency_store: IdempotencyStore | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        approval_repository_factory: ApprovalRepositoryFactory = ApprovalRepository,
    ) -> None:
        if not gates:
            raise ValueError("Runtime requires at least one gate")
        self.gates = gates
        self.executor = executor
        self.idempotency_store = idempotency_store
        self.session_factory = session_factory
        self.approval_repository_factory = approval_repository_factory

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

    async def resume(self, transaction_id: UUID) -> TransactionResult:
        """Resume a previously-halted transaction after approval was granted.

        Skips the gate pipeline. The original transaction has already
        accumulated audit events for the gates that ran the first time;
        re-evaluating here would create a second pipeline trace with
        possibly different outcomes (policies might have changed between
        submit and grant). For MVP we trust the operator's approval is
        scoped to the call as it was originally evaluated; production
        would want defense-in-depth re-evaluation — see Step 10 notes.
        """
        if self.session_factory is None:
            raise RuntimeError("resume requires a session_factory")
        if self.executor is None:
            raise RuntimeError("resume requires an executor")

        started_at = datetime.now(timezone.utc)

        # 1. Look up original tx + approval (one short-lived session).
        async with self.session_factory() as session:
            audit_repo = AuditRepository(session)
            approval_repo = self.approval_repository_factory(session)
            original_tx = await audit_repo.get_transaction(transaction_id)
            if original_tx is None:
                raise ApprovalNotFoundError(
                    f"transaction {transaction_id} not found"
                )
            approval = await approval_repo.get_by_transaction(transaction_id)
            if approval is None:
                raise ApprovalNotFoundError(
                    f"no approval found for transaction {transaction_id}"
                )
            if approval.status != ApprovalStatus.GRANTED:
                raise ApprovalNotGrantedError(
                    f"approval {approval.id} for transaction "
                    f"{transaction_id} is {approval.status.value}, not granted"
                )

        # 2. Reconstruct ToolCall from stored fields. Re-use original
        #    tool_call_id so the agent-side identity is stable across
        #    submit and resume.
        tool_call = ToolCall(
            id=original_tx.tool_call_id,
            actor=Actor(
                agent_id=original_tx.actor_agent_id,
                session_id=original_tx.actor_session_id,
                run_id=original_tx.actor_run_id,
                tenant_id=original_tx.actor_tenant_id,
                user_id=original_tx.actor_user_id,
            ),
            tool_name=original_tx.tool_name,
            args=original_tx.args,
        )

        # 3. Build the resumed transaction's audit event stream by hand
        #    (no gate pipeline runs here, so we can't reuse TransactionContext
        #    machinery without faking gate results).
        new_tx_id = uuid4()
        audit_events: list[AuditEvent] = []

        def emit(event_type: AuditEventType, payload: dict[str, Any] | None = None) -> None:
            audit_events.append(AuditEvent(
                transaction_id=new_tx_id,
                sequence_num=len(audit_events),
                event_type=event_type,
                payload=payload or {},
                actor_id=tool_call.actor.agent_id,
            ))

        emit(AuditEventType.TRANSACTION_STARTED, payload={
            "tool_name": tool_call.tool_name,
            "fingerprint": tool_call.fingerprint[:12],
            "mode": TransactionMode.EXECUTE.value,
            "resumed_from": str(transaction_id),
            "approval_id": str(approval.id),
            "resolver_id": approval.resolver_id,
        })

        # 4. Execute directly. No gate pipeline.
        try:
            output = await self.executor(tool_call)
            emit(AuditEventType.TOOL_EXECUTED, payload={"tool_name": tool_call.tool_name})
            emit(AuditEventType.TRANSACTION_COMPLETED, payload={
                "status": TransactionStatus.EXECUTED.value,
                "resumed_from": str(transaction_id),
            })
            status = TransactionStatus.EXECUTED
            error: str | None = None
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            emit(AuditEventType.TRANSACTION_FAILED, payload={
                "error": err,
                "resumed_from": str(transaction_id),
            })
            status = TransactionStatus.FAILED
            output = None
            error = err

        completed_at = datetime.now(timezone.utc)
        duration_ms = (completed_at - started_at).total_seconds() * 1000

        new_result = TransactionResult(
            transaction_id=new_tx_id,
            tool_call_id=tool_call.id,
            status=status,
            gate_results=[],  # gates skipped on resume
            execution_output=output,
            error=error,
            duration_ms=duration_ms,
            started_at=started_at,
            completed_at=completed_at,
        )

        # 5. Cache + persist using the same paths as submit.
        await self._cache_if_executed(tool_call, new_result)
        try:
            async with self.session_factory() as session:
                audit_repo = AuditRepository(session)
                await audit_repo.record(new_result, tool_call, audit_events)
        except Exception as exc:
            logger.error(
                "resume_persist_failed: tx=%s err=%s",
                new_tx_id, exc, exc_info=True,
            )

        return new_result

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
                audit_repo = AuditRepository(session)
                await audit_repo.record(result, tool_call, ctx.audit_events, trace_id=trace_id)
                # Plant the approval row on the same session right after the
                # audit log lands. Separate commit, but the transaction row
                # is already durable — so the operator never sees a halted
                # transaction without a queue row to act on (modulo the
                # narrow window in which this second commit fails; logged).
                if result.status == TransactionStatus.PENDING_APPROVAL:
                    await self._record_approval(session, ctx, result, tool_call)
        except Exception as exc:
            # Audit persistence failure is logged but does not fail the transaction.
            # In prod we'd alert on this; the in-memory result is still returned.
            logger.error(
                "audit_persist_failed: tx=%s err=%s",
                ctx.transaction_id, exc, exc_info=True,
            )

    async def _record_approval(
        self,
        session: AsyncSession,
        ctx: TransactionContext,
        result: TransactionResult,
        tool_call: ToolCall,
    ) -> None:
        last = ctx.last_decision
        # Defensive: PENDING_APPROVAL implies a require_approval gate result.
        requested_reason = last.reason if last is not None else "approval required"
        approval = Approval(
            transaction_id=result.transaction_id,
            fingerprint=tool_call.fingerprint,
            tool_name=tool_call.tool_name,
            actor_agent_id=tool_call.actor.agent_id,
            requested_reason=requested_reason,
        )
        repo = self.approval_repository_factory(session)
        await repo.create(approval)
        await session.commit()
