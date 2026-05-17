"""Start Lockrail as an MCP server over stdio.

Any MCP-compliant client (Claude Desktop, LangGraph, PydanticAI, ...) can
connect via stdio and see refund/crm_update/send_email behind the full
gate pipeline: idempotency, evidence, policy, approval.
"""
from __future__ import annotations

import asyncio
from typing import Any

from lockrail.config import get_settings
from lockrail.core import Runtime
from lockrail.gates import EvidenceGate, IdempotencyGate, PolicyGate
from lockrail.mcp import (
    LockrailMCPServer,
    ToolRegistry,
    default_policies,
    register_builtin_tools,
)
from lockrail.models import Actor, ToolCall
from lockrail.storage import get_session_factory, make_idempotency_store


async def _main() -> None:
    settings = get_settings()
    tool_registry = ToolRegistry()
    register_builtin_tools(tool_registry)

    async def executor(tool_call: ToolCall) -> dict[str, Any]:
        fn = tool_registry.get_executor(tool_call.tool_name)
        if fn is None:
            raise ValueError(f"no executor for tool {tool_call.tool_name!r}")
        return await fn(tool_call.args)

    store = await make_idempotency_store(
        settings.redis_url,
        ttl_seconds=settings.lockrail_idempotency_ttl_seconds,
    )
    runtime = Runtime(
        gates=[
            IdempotencyGate(store),
            EvidenceGate(tool_registry.to_contract_registry()),
            PolicyGate(default_policies()),
        ],
        executor=executor,
        idempotency_store=store,
        session_factory=get_session_factory(),
    )

    server = LockrailMCPServer(
        runtime=runtime,
        tool_registry=tool_registry,
        default_actor=Actor(agent_id="mcp-stdio", session_id="default"),
    )
    try:
        await server.run_stdio()
    finally:
        await store.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
