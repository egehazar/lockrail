"""Approval repository — persists and resolves HITL approval rows."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.approval import Approval, ApprovalStatus
from .models import ApprovalRow


class ApprovalRepository:
    """One repo per session. Reads return domain Approval objects."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, approval: Approval) -> None:
        """Insert a new approval row. Caller is responsible for committing.

        No commit here so that the runtime can flush the audit log and the
        approval row in the same logical write — the operator should never
        see a transaction in PENDING_APPROVAL status without a row to grant.
        """
        self.session.add(ApprovalRow(
            id=approval.id,
            transaction_id=approval.transaction_id,
            fingerprint=approval.fingerprint,
            tool_name=approval.tool_name,
            actor_agent_id=approval.actor_agent_id,
            requested_reason=approval.requested_reason,
            status=approval.status,
            requested_at=approval.requested_at,
            resolved_at=approval.resolved_at,
            resolver_id=approval.resolver_id,
            notes=approval.notes,
        ))

    async def get(self, approval_id: UUID) -> Approval | None:
        row = await self.session.get(ApprovalRow, approval_id)
        return _row_to_domain(row) if row else None

    async def get_by_transaction(self, transaction_id: UUID) -> Approval | None:
        stmt = select(ApprovalRow).where(ApprovalRow.transaction_id == transaction_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return _row_to_domain(row) if row else None

    async def list_pending(self, limit: int = 100) -> list[Approval]:
        """Oldest pending first — operator queue is FIFO by default."""
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.status == ApprovalStatus.PENDING)
            .order_by(ApprovalRow.requested_at)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_row_to_domain(r) for r in rows]

    async def grant(
        self,
        approval_id: UUID,
        resolver_id: str,
        notes: str | None = None,
    ) -> Approval | None:
        return await self._resolve(approval_id, ApprovalStatus.GRANTED, resolver_id, notes)

    async def deny(
        self,
        approval_id: UUID,
        resolver_id: str,
        notes: str | None = None,
    ) -> Approval | None:
        return await self._resolve(approval_id, ApprovalStatus.DENIED, resolver_id, notes)

    async def _resolve(
        self,
        approval_id: UUID,
        new_status: ApprovalStatus,
        resolver_id: str,
        notes: str | None,
    ) -> Approval | None:
        row = await self.session.get(ApprovalRow, approval_id)
        if row is None:
            return None
        row.status = new_status
        row.resolved_at = datetime.now(timezone.utc)
        row.resolver_id = resolver_id
        row.notes = notes
        await self.session.commit()
        return _row_to_domain(row)


def _row_to_domain(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        transaction_id=row.transaction_id,
        fingerprint=row.fingerprint,
        tool_name=row.tool_name,
        actor_agent_id=row.actor_agent_id,
        requested_reason=row.requested_reason,
        status=row.status,
        requested_at=row.requested_at,
        resolved_at=row.resolved_at,
        resolver_id=row.resolver_id,
        notes=row.notes,
    )
