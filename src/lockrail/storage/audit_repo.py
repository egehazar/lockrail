"""Audit repository — writes transaction records + event streams to Postgres."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEvent, ToolCall, TransactionResult
from .models import AuditEventRow, TransactionRow


class AuditRepository:
    """Persists transactions + audit events. One repo per session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        result: TransactionResult,
        tool_call: ToolCall,
        events: list[AuditEvent],
        trace_id: str | None = None,
    ) -> None:
        """Single-commit write: transaction row + all its audit events."""
        self.session.add(TransactionRow(
            transaction_id=result.transaction_id,
            tool_call_id=result.tool_call_id,
            actor_agent_id=tool_call.actor.agent_id,
            actor_session_id=tool_call.actor.session_id,
            actor_run_id=tool_call.actor.run_id,
            actor_user_id=tool_call.actor.user_id,
            actor_tenant_id=tool_call.actor.tenant_id,
            tool_name=tool_call.tool_name,
            args=tool_call.args,
            fingerprint=tool_call.fingerprint,
            status=result.status,
            execution_output=result.execution_output,
            error=result.error,
            duration_ms=result.duration_ms,
            started_at=result.started_at,
            completed_at=result.completed_at,
            trace_id=trace_id,
        ))
        for ev in events:
            self.session.add(AuditEventRow(
                id=ev.id,
                transaction_id=ev.transaction_id,
                sequence_num=ev.sequence_num,
                event_type=ev.event_type,
                timestamp=ev.timestamp,
                actor_id=ev.actor_id,
                payload=ev.payload,
                trace_id=ev.trace_id,
            ))
        await self.session.commit()

    async def get_events(self, transaction_id: UUID) -> list[AuditEvent]:
        stmt = (
            select(AuditEventRow)
            .where(AuditEventRow.transaction_id == transaction_id)
            .order_by(AuditEventRow.sequence_num)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            AuditEvent(
                id=r.id,
                transaction_id=r.transaction_id,
                sequence_num=r.sequence_num,
                event_type=r.event_type,
                timestamp=r.timestamp,
                actor_id=r.actor_id,
                payload=r.payload,
                trace_id=r.trace_id,
            )
            for r in rows
        ]

    async def get_transaction(self, transaction_id: UUID) -> TransactionRow | None:
        stmt = select(TransactionRow).where(TransactionRow.transaction_id == transaction_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()
