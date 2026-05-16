"""Approval endpoints — operator queue and resolution."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import Approval, ApprovalStatus
from ...storage import ApprovalRepository
from ...storage.db import get_session

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ResolveBody(BaseModel):
    resolver_id: str = Field(min_length=1)
    notes: str | None = None


@router.get("", response_model=list[Approval])
async def list_approvals(
    status: ApprovalStatus = Query(default=ApprovalStatus.PENDING),
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> list[Approval]:
    repo = ApprovalRepository(session)
    if status == ApprovalStatus.PENDING:
        return await repo.list_pending(limit=limit)
    # Other statuses aren't part of the MVP operator queue. Returning 400
    # rather than silently empty so callers learn the filter is bounded.
    raise HTTPException(
        status_code=400,
        detail=f"listing approvals by status='{status.value}' not supported in MVP",
    )


@router.get("/{approval_id}", response_model=Approval)
async def get_approval(
    approval_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Approval:
    approval = await ApprovalRepository(session).get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval


@router.post("/{approval_id}/grant", response_model=Approval)
async def grant_approval(
    approval_id: UUID,
    body: ResolveBody,
    session: AsyncSession = Depends(get_session),
) -> Approval:
    approval = await ApprovalRepository(session).grant(
        approval_id, body.resolver_id, body.notes,
    )
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval


@router.post("/{approval_id}/deny", response_model=Approval)
async def deny_approval(
    approval_id: UUID,
    body: ResolveBody,
    session: AsyncSession = Depends(get_session),
) -> Approval:
    approval = await ApprovalRepository(session).deny(
        approval_id, body.resolver_id, body.notes,
    )
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval
