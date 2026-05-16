"""End-to-end approval flow: Runtime + repository + HTTP."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from lockrail.api import create_app
from lockrail.config import get_settings
from lockrail.core import (
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    Runtime,
)
from lockrail.gates import PolicyGate
from lockrail.models import (
    Actor,
    AmountThresholdPolicy,
    Approval,
    ApprovalStatus,
    AuditEventType,
    PolicyAction,
    PolicyRegistry,
    ToolCall,
    TransactionStatus,
)
from lockrail.storage import (
    ApprovalRepository,
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
            "TRUNCATE TABLE approvals, audit_events, transactions "
            "RESTART IDENTITY CASCADE"
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


def _approval_over_500() -> AmountThresholdPolicy:
    return AmountThresholdPolicy(
        name="refund_over_500_needs_approval",
        applies_to=["refund"],
        action=PolicyAction.REQUIRE_APPROVAL,
        priority=50,
        arg_name="amount",
        threshold=500.0,
        operator="gt",
    )


def _call(amount: int = 1500) -> ToolCall:
    return ToolCall(
        actor=Actor(agent_id="agent-1", session_id="sess-1"),
        tool_name="refund",
        args={"order_id": "ORD-42", "amount": amount},
    )


# --- Runtime.resume end-to-end ---


async def test_runtime_resume_executes_after_grant(clean_db, idem_store):
    factory = clean_db
    exec_calls = []

    async def executor(tc: ToolCall) -> dict:
        exec_calls.append(tc)
        return {"refunded": True, "amount": tc.args["amount"]}

    runtime = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
        executor=executor,
        idempotency_store=idem_store,
        session_factory=factory,
    )

    # 1. Submit a call that triggers REQUIRE_APPROVAL.
    submitted = await runtime.submit(_call(amount=1500))
    assert submitted.status == TransactionStatus.PENDING_APPROVAL
    assert exec_calls == [], "executor must not run before approval"

    # 2. Verify the Approval row landed in the DB with the gate's reason.
    async with factory() as session:
        approval_repo = ApprovalRepository(session)
        approval = await approval_repo.get_by_transaction(submitted.transaction_id)
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING
    assert approval.tool_name == "refund"
    assert approval.actor_agent_id == "agent-1"
    assert "amount=1500" in approval.requested_reason
    # Approval fingerprint matches the original tool call's fingerprint.
    assert approval.fingerprint == _call(amount=1500).fingerprint

    # 3. Grant the approval.
    async with factory() as session:
        granted = await ApprovalRepository(session).grant(
            approval.id, resolver_id="ops-user-7", notes="verified ticket",
        )
    assert granted is not None
    assert granted.status == ApprovalStatus.GRANTED
    assert granted.resolver_id == "ops-user-7"
    assert granted.notes == "verified ticket"
    assert granted.resolved_at is not None

    # 4. Resume the transaction.
    resumed = await runtime.resume(submitted.transaction_id)
    assert resumed.status == TransactionStatus.EXECUTED
    assert resumed.execution_output == {"refunded": True, "amount": 1500}
    assert resumed.transaction_id != submitted.transaction_id
    assert exec_calls and exec_calls[0].args["amount"] == 1500

    # 5. The resumed transaction's audit log links back to the original.
    async with factory() as session:
        events = await AuditRepository(session).get_events(resumed.transaction_id)
    types = [e.event_type for e in events]
    assert AuditEventType.TRANSACTION_STARTED in types
    assert AuditEventType.TOOL_EXECUTED in types
    assert AuditEventType.TRANSACTION_COMPLETED in types
    started = next(e for e in events if e.event_type == AuditEventType.TRANSACTION_STARTED)
    assert started.payload["resumed_from"] == str(submitted.transaction_id)
    assert started.payload["resolver_id"] == "ops-user-7"


async def test_resume_before_grant_raises(clean_db, idem_store):
    factory = clean_db

    async def executor(tc: ToolCall) -> dict:
        return {"ok": True}

    runtime = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
        executor=executor,
        idempotency_store=idem_store,
        session_factory=factory,
    )

    submitted = await runtime.submit(_call(amount=1500))
    assert submitted.status == TransactionStatus.PENDING_APPROVAL

    with pytest.raises(ApprovalNotGrantedError):
        await runtime.resume(submitted.transaction_id)


async def test_resume_unknown_transaction_raises(clean_db):
    factory = clean_db
    from uuid import uuid4

    async def executor(tc: ToolCall) -> dict:
        return {"ok": True}

    runtime = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
        executor=executor,
        session_factory=factory,
    )

    with pytest.raises(ApprovalNotFoundError):
        await runtime.resume(uuid4())


async def test_resume_denied_approval_raises(clean_db):
    factory = clean_db

    async def executor(tc: ToolCall) -> dict:
        return {"ok": True}

    runtime = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
        executor=executor,
        session_factory=factory,
    )

    submitted = await runtime.submit(_call(amount=1500))
    async with factory() as session:
        approval = await ApprovalRepository(session).get_by_transaction(
            submitted.transaction_id,
        )
        assert approval is not None
        await ApprovalRepository(session).deny(
            approval.id, resolver_id="ops-user-7", notes="not warranted",
        )

    with pytest.raises(ApprovalNotGrantedError):
        await runtime.resume(submitted.transaction_id)


# --- HTTP flow via httpx + ASGI ---


@pytest.fixture
async def app(clean_db):
    # Build an app with one approval policy wired in.
    registry = PolicyRegistry(policies=[_approval_over_500()])
    fastapi_app = create_app(policy_registry=registry)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_http_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_http_full_approval_cycle(client):
    # 1. Submit a refund that triggers approval.
    submit_resp = await client.post(
        "/transactions",
        json={
            "actor": {"agent_id": "agent-http", "session_id": "sess-http"},
            "tool_name": "refund",
            "args": {"order_id": "ORD-100", "amount": 1500},
            "mode": "execute",
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text
    submitted = submit_resp.json()
    assert submitted["status"] == "pending_approval"
    tx_id = submitted["transaction_id"]

    # 2. List pending approvals — ours should be in there.
    list_resp = await client.get("/approvals")
    assert list_resp.status_code == 200
    pending = list_resp.json()
    matching = [a for a in pending if a["transaction_id"] == tx_id]
    assert len(matching) == 1
    approval_id = matching[0]["id"]
    assert matching[0]["status"] == "pending"
    assert matching[0]["actor_agent_id"] == "agent-http"

    # 3. Resume *before* granting → 409.
    early = await client.post(f"/transactions/{tx_id}/resume")
    assert early.status_code == 409

    # 4. Grant the approval.
    grant_resp = await client.post(
        f"/approvals/{approval_id}/grant",
        json={"resolver_id": "ops-7", "notes": "ticket verified"},
    )
    assert grant_resp.status_code == 200, grant_resp.text
    granted = grant_resp.json()
    assert granted["status"] == "granted"
    assert granted["resolver_id"] == "ops-7"

    # 5. Resume → executes via the stub executor.
    resume_resp = await client.post(f"/transactions/{tx_id}/resume")
    assert resume_resp.status_code == 200, resume_resp.text
    resumed = resume_resp.json()
    assert resumed["status"] == "executed"
    assert resumed["execution_output"]["executed_via"] == "stub"
    assert resumed["execution_output"]["tool"] == "refund"
    new_tx_id = resumed["transaction_id"]
    assert new_tx_id != tx_id

    # 6. Fetching the original transaction shows its audit trail.
    get_orig = await client.get(f"/transactions/{tx_id}")
    assert get_orig.status_code == 200
    orig_view = get_orig.json()
    assert orig_view["status"] == "pending_approval"
    event_types = [e["event_type"] for e in orig_view["audit_events"]]
    assert "transaction_started" in event_types
    assert "approval_requested" in event_types

    # 7. Fetching the resumed transaction shows the resume linkage.
    get_resumed = await client.get(f"/transactions/{new_tx_id}")
    assert get_resumed.status_code == 200
    resumed_view = get_resumed.json()
    assert resumed_view["status"] == "executed"
    started = next(
        e for e in resumed_view["audit_events"]
        if e["event_type"] == "transaction_started"
    )
    assert started["payload"]["resumed_from"] == tx_id


async def test_http_get_unknown_approval_404(client):
    from uuid import uuid4
    r = await client.get(f"/approvals/{uuid4()}")
    assert r.status_code == 404


async def test_http_resume_unknown_transaction_404(client):
    from uuid import uuid4
    r = await client.post(f"/transactions/{uuid4()}/resume")
    assert r.status_code == 404


async def test_http_grant_unknown_404(client):
    from uuid import uuid4
    r = await client.post(
        f"/approvals/{uuid4()}/grant",
        json={"resolver_id": "ops-1"},
    )
    assert r.status_code == 404


async def test_http_deny_endpoint(client):
    # Submit so we have an approval to deny.
    submit_resp = await client.post(
        "/transactions",
        json={
            "actor": {"agent_id": "agent-x", "session_id": "sess-x"},
            "tool_name": "refund",
            "args": {"order_id": "ORD-DEL", "amount": 800},
        },
    )
    tx_id = submit_resp.json()["transaction_id"]
    pending = (await client.get("/approvals")).json()
    approval_id = next(a["id"] for a in pending if a["transaction_id"] == tx_id)

    deny_resp = await client.post(
        f"/approvals/{approval_id}/deny",
        json={"resolver_id": "ops-9", "notes": "duplicate"},
    )
    assert deny_resp.status_code == 200
    body = deny_resp.json()
    assert body["status"] == "denied"
    assert body["notes"] == "duplicate"

    # Resume should now be 409 because status is denied (not granted).
    r = await client.post(f"/transactions/{tx_id}/resume")
    assert r.status_code == 409


async def test_http_list_non_pending_status_400(client):
    r = await client.get("/approvals", params={"status": "granted"})
    assert r.status_code == 400


# --- Approval model sanity ---


def test_approval_is_frozen():
    from pydantic import ValidationError as PydanticValidationError
    from uuid import uuid4
    a = Approval(
        transaction_id=uuid4(),
        fingerprint="x" * 64,
        tool_name="refund",
        actor_agent_id="agent",
        requested_reason="test",
    )
    with pytest.raises(PydanticValidationError):
        a.status = ApprovalStatus.GRANTED  # type: ignore[misc]
