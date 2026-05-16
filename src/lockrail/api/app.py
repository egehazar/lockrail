"""FastAPI app factory.

Wires the Runtime, Redis idempotency store, and gates together inside a
lifespan handler so a test can stand up the same app with custom
registries via dependency injection.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from ..config import get_settings
from ..core import Runtime, ToolExecutor
from ..gates import EvidenceGate, IdempotencyGate, PolicyGate
from ..models import PolicyRegistry, ToolCall
from ..models.contracts import ContractRegistry
from ..storage import get_session_factory, make_idempotency_store
from .routes import approvals, health, transactions


async def _stub_executor(tool_call: ToolCall) -> dict[str, Any]:
    """Default executor — echoes the call. Real tools come in a later step."""
    return {
        "executed_via": "stub",
        "tool": tool_call.tool_name,
        "args": tool_call.args,
    }


def create_app(
    *,
    contract_registry: ContractRegistry | None = None,
    policy_registry: PolicyRegistry | None = None,
    executor: ToolExecutor | None = None,
) -> FastAPI:
    """Construct a fresh Lockrail app.

    All injectable pieces have empty/stub defaults so the app boots without
    configuration; tests and real deployments override what they need.
    """
    contract_registry_ = contract_registry or ContractRegistry()
    policy_registry_ = policy_registry or PolicyRegistry()
    executor_: ToolExecutor = executor or _stub_executor

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        store = await make_idempotency_store(
            settings.redis_url,
            ttl_seconds=settings.lockrail_idempotency_ttl_seconds,
        )
        runtime = Runtime(
            gates=[
                IdempotencyGate(store),
                EvidenceGate(contract_registry_),
                PolicyGate(policy_registry_),
            ],
            executor=executor_,
            idempotency_store=store,
            session_factory=get_session_factory(),
        )
        app.state.runtime = runtime
        app.state.idempotency_store = store
        app.state.contract_registry = contract_registry_
        app.state.policy_registry = policy_registry_
        try:
            yield
        finally:
            await store.aclose()

    app = FastAPI(title="Lockrail", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(transactions.router)
    app.include_router(approvals.router)
    return app
