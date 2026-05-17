"""LockrailMCPServer — exposes the Lockrail runtime as an MCP-compliant server.

The MCP `Server` is held by composition: handlers are bound methods on
LockrailMCPServer that we register with the underlying server via its
decorators in `__init__`. Tests can invoke `list_tools()` and `call_tool()`
on a LockrailMCPServer directly without spinning up a transport.
"""
from __future__ import annotations

import json

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ..core import Runtime
from ..models import Actor, ToolCall, TransactionResult, TransactionStatus
from .tool_registry import ToolRegistry


class LockrailMCPServer:
    """Wrap a Runtime + ToolRegistry as an MCP server.

    The Runtime's executor is supplied by the wiring layer (see
    `scripts/run_mcp_server.py`) and is expected to delegate to
    `tool_registry.get_executor(name)`. We pass `validate_input=False` to
    the underlying SDK so EvidenceGate is the single source of truth for
    argument validation — otherwise the SDK's jsonschema check would
    reject malformed args before EvidenceGate could format the per-field
    errors the agent actually needs to self-correct.
    """

    def __init__(
        self,
        runtime: Runtime,
        tool_registry: ToolRegistry,
        default_actor: Actor,
    ) -> None:
        self.runtime = runtime
        self.tool_registry = tool_registry
        self.default_actor = default_actor
        self._server: Server = Server("lockrail")
        self._server.list_tools()(self.list_tools)
        self._server.call_tool(validate_input=False)(self.call_tool)

    async def list_tools(self) -> list[mcp_types.Tool]:
        return self.tool_registry.as_mcp_tools()

    async def call_tool(
        self, name: str, arguments: dict
    ) -> mcp_types.CallToolResult:
        # Short-circuit unknown tools at the MCP layer. They shouldn't
        # enter the runtime pipeline at all — the audit log records gate
        # decisions on real tools, not routing misses on tools that
        # don't exist.
        if name not in self.tool_registry:
            return _error_result(f"unknown tool: {name!r}")

        tool_call = ToolCall(
            actor=self.default_actor,
            tool_name=name,
            args=arguments,
        )
        result = await self.runtime.submit(tool_call)
        return _result_to_call_tool_result(result)

    async def run_stdio(self) -> None:
        """Start the server on stdio transport. Blocks until disconnect."""
        async with stdio_server() as (read, write):
            await self._server.run(
                read,
                write,
                self._server.create_initialization_options(),
            )


def _result_to_call_tool_result(result: TransactionResult) -> mcp_types.CallToolResult:
    """Map TransactionResult.status → CallToolResult.

    Non-success states use isError=True with parseable structured text so
    the agent can distinguish "blocked by policy", "awaiting approval",
    and "tool raised". Throwing protocol-level errors would collapse
    these into a single JSON-RPC failure and the agent would lose the
    ability to decide what to do next (retry with new args / wait for
    a human / abandon).
    """
    if result.status in (TransactionStatus.EXECUTED, TransactionStatus.REPLAYED):
        text = json.dumps(result.execution_output)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)],
            isError=False,
        )

    if result.status == TransactionStatus.BLOCKED:
        last = result.gate_results[-1] if result.gate_results else None
        gate_name = last.gate_name if last else "unknown"
        reason = last.reason if last else "blocked"
        return _error_result(f"blocked by {gate_name}: {reason}")

    if result.status == TransactionStatus.PENDING_APPROVAL:
        last = result.gate_results[-1] if result.gate_results else None
        reason = last.reason if last else "approval required"
        return _error_result(
            f"approval required: {reason} "
            f"(transaction_id={result.transaction_id}; "
            f"resume via Lockrail after operator grants approval)"
        )

    # FAILED
    error = result.error or "tool execution failed"
    return _error_result(error)


def _error_result(message: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=message)],
        isError=True,
    )
