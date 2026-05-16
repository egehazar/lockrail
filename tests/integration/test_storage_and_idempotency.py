"""Integration tests against real Postgres + Redis (via docker-compose)."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from lockrail.config import get_settings
from lockrail.core import Runtime
from lockrail.gates import Gate, IdempotencyGate
from lockrail.models import (
    Actor,
    AuditEventType,
    GateResult,
    ToolCall,
    TransactionContext,
    TransactionStatus,
)
from lockrail.storage import (
    AuditRepository,
    get_session_factory,
    make_idempotency_store,
)


# --- Fixtures ---


@pytest.fixture
async def clean_db():
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(text(
            "TRUNCATE TABLE transactions, audit_events RESTART IDENTITY CASCADE"
        ))
        await session.commit()
    yield factory


@pytest.fixture
async def idem_store():
    settings = get_settings()
    store = await make_idempotency_store(settings.redis_url, ttl_seconds=60)
    keys = [k async for k in store.client.scan_iter("lockrail:idem:*")]
    if keys:
        await store.client.delete(*keys)
    yield store
    keys = [k async for k in store.client.scan_iter("lockrail:idem:*")]
    if keys:
        await store.client.delete(*keys)
    await store.aclose()


class AllowGate(Gate):
    name = "allow"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        return self.allow("test allow")


def _call(amount: int = 100, order_id: str = "ord-1") -> ToolCall:
    return ToolCall(
        actor=Actor(agent_id="agent-1", session_id="sess-1"),
        tool_name="refund",
        args={"order_id": order_id, "amount": amount},
    )


# --- Tests ---


async def test_audit_repo_persists_and_round_trips(clean_db):
    factory = clean_db

    async def executor(tc):
        return {"refunded": True, "amount": tc.args["amount"]}

    runtime = Runtime(gates=[AllowGate()], executor=executor, session_factory=factory)
    call = _call()
    result = await runtime.submit(call)

    assert result.status == TransactionStatus.EXECUTED

    async with factory() as session:
        repo = AuditRepository(session)
        tx = await repo.get_transaction(result.transaction_id)
        events = await repo.get_events(result.transaction_id)

    assert tx is not None
    assert tx.tool_name == "refund"
    assert tx.status == TransactionStatus.EXECUTED
    assert tx.execution_output == {"refunded": True, "amount": 100}

    # Expect: TRANSACTION_STARTED, GATE_EVALUATED, TOOL_EXECUTED, TRANSACTION_COMPLETED
    types = [e.event_type for e in events]
    assert AuditEventType.TRANSACTION_STARTED in types
    assert AuditEventType.GATE_EVALUATED in types
    assert AuditEventType.TOOL_EXECUTED in types
    assert AuditEventType.TRANSACTION_COMPLETED in types

    # Sequence numbers should be 0..N-1 contiguous
    assert [e.sequence_num for e in events] == list(range(len(events)))


async def test_idempotency_replay_on_identical_call(clean_db, idem_store):
    factory = clean_db
    exec_count = 0

    async def executor(tc):
        nonlocal exec_count
        exec_count += 1
        return {"refunded": True, "amount": tc.args["amount"], "exec_count": exec_count}

    runtime = Runtime(
        gates=[IdempotencyGate(idem_store), AllowGate()],
        executor=executor,
        idempotency_store=idem_store,
        session_factory=factory,
    )

    first = await runtime.submit(_call())
    second = await runtime.submit(_call())  # identical args → cache hit

    assert first.status == TransactionStatus.EXECUTED
    assert second.status == TransactionStatus.REPLAYED
    assert exec_count == 1, "executor must run only once"
    assert second.execution_output == first.execution_output


async def test_idempotency_misses_on_different_args(clean_db, idem_store):
    factory = clean_db
    exec_count = 0

    async def executor(tc):
        nonlocal exec_count
        exec_count += 1
        return {"refunded": True, "amount": tc.args["amount"]}

    runtime = Runtime(
        gates=[IdempotencyGate(idem_store), AllowGate()],
        executor=executor,
        idempotency_store=idem_store,
        session_factory=factory,
    )

    a = await runtime.submit(_call(amount=100))
    b = await runtime.submit(_call(amount=200))  # different args → not a hit

    assert a.status == TransactionStatus.EXECUTED
    assert b.status == TransactionStatus.EXECUTED
    assert exec_count == 2


async def test_replayed_call_emits_idempotency_hit_event(clean_db, idem_store):
    factory = clean_db

    async def executor(tc):
        return {"ok": True}

    runtime = Runtime(
        gates=[IdempotencyGate(idem_store), AllowGate()],
        executor=executor,
        idempotency_store=idem_store,
        session_factory=factory,
    )

    await runtime.submit(_call())
    second = await runtime.submit(_call())

    async with factory() as session:
        repo = AuditRepository(session)
        events = await repo.get_events(second.transaction_id)

    types = [e.event_type for e in events]
    assert AuditEventType.IDEMPOTENCY_HIT in types
    assert AuditEventType.TOOL_EXECUTED not in types
