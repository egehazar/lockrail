"""Transaction endpoints — submit, fetch, resume."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...core import ApprovalNotFoundError, ApprovalNotGrantedError, Runtime
from ...models import Actor, AuditEvent, ToolCall, TransactionMode, TransactionResult
from ...storage import AuditRepository, TransactionRow
from ...storage.db import get_session
from ..dependencies import get_runtime

router = APIRouter(prefix="/transactions", tags=["transactions"])


class ActorBody(BaseModel):
    agent_id: str
    session_id: str
    run_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None


class SubmitBody(BaseModel):
    actor: ActorBody
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    mode: TransactionMode = TransactionMode.EXECUTE
    trace_id: str | None = None


class TransactionView(BaseModel):
    """Read-shape of a persisted transaction row, joined with its audit events."""
    transaction_id: UUID
    tool_call_id: UUID
    actor: ActorBody
    tool_name: str
    args: dict[str, Any]
    fingerprint: str
    status: str
    execution_output: dict[str, Any] | None
    error: str | None
    duration_ms: float
    started_at: str
    completed_at: str
    trace_id: str | None
    audit_events: list[AuditEvent]


@router.post("", response_model=TransactionResult)
async def submit_transaction(
    body: SubmitBody,
    runtime: Runtime = Depends(get_runtime),
) -> TransactionResult:
    tool_call = ToolCall(
        actor=Actor(**body.actor.model_dump()),
        tool_name=body.tool_name,
        args=body.args,
    )
    return await runtime.submit(tool_call, mode=body.mode, trace_id=body.trace_id)


@router.get("/{transaction_id}", response_model=TransactionView)
async def get_transaction(
    transaction_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> TransactionView:
    repo = AuditRepository(session)
    tx = await repo.get_transaction(transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    events = await repo.get_events(transaction_id)
    return _to_view(tx, events)


@router.post("/{transaction_id}/resume", response_model=TransactionResult)
async def resume_transaction(
    transaction_id: UUID,
    runtime: Runtime = Depends(get_runtime),
) -> TransactionResult:
    try:
        return await runtime.resume(transaction_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalNotGrantedError as exc:
        # 409 Conflict — the resource is in a state that disallows the operation.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _to_view(tx: TransactionRow, events: list[AuditEvent]) -> TransactionView:
    return TransactionView(
        transaction_id=tx.transaction_id,
        tool_call_id=tx.tool_call_id,
        actor=ActorBody(
            agent_id=tx.actor_agent_id,
            session_id=tx.actor_session_id,
            run_id=tx.actor_run_id,
            user_id=tx.actor_user_id,
            tenant_id=tx.actor_tenant_id,
        ),
        tool_name=tx.tool_name,
        args=tx.args,
        fingerprint=tx.fingerprint,
        status=tx.status.value,
        execution_output=tx.execution_output,
        error=tx.error,
        duration_ms=tx.duration_ms,
        started_at=tx.started_at.isoformat(),
        completed_at=tx.completed_at.isoformat(),
        trace_id=tx.trace_id,
        audit_events=events,
    )
