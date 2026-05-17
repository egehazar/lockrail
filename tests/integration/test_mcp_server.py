"""LockrailMCPServer — invoke handlers directly without a transport.

These tests deliberately wire the Runtime with `session_factory=None` and
no IdempotencyGate so the MCP integration can be exercised without the
postgres/redis stack. Persistence behavior is covered by
`test_approval_flow.py`; here we only care that the MCP-layer handlers
shape responses correctly off each TransactionStatus.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from lockrail.core import Runtime
from lockrail.gates import EvidenceGate, PolicyGate
from lockrail.mcp import (
    LockrailMCPServer,
    ToolRegistry,
    default_policies,
    register_builtin_tools,
)
from lockrail.models import Actor, ToolCall


# --- Fixtures ---


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    return reg


@pytest.fixture
def mcp_server(tool_registry: ToolRegistry) -> LockrailMCPServer:
    async def executor(tc: ToolCall) -> dict[str, Any]:
        fn = tool_registry.get_executor(tc.tool_name)
        assert fn is not None, f"no executor for {tc.tool_name}"
        return await fn(tc.args)

    runtime = Runtime(
        gates=[
            EvidenceGate(tool_registry.to_contract_registry()),
            PolicyGate(default_policies()),
        ],
        executor=executor,
        # No idempotency_store / session_factory — keeps these tests
        # hermetic. The Runtime's persistence and caching paths short-
        # circuit when those are None.
    )
    return LockrailMCPServer(
        runtime=runtime,
        tool_registry=tool_registry,
        default_actor=Actor(agent_id="test-agent", session_id="test-sess"),
    )


# --- list_tools ---


async def test_list_tools_returns_three_tools_with_input_schemas(
    mcp_server: LockrailMCPServer,
) -> None:
    tools = await mcp_server.list_tools()
    assert sorted(t.name for t in tools) == ["crm_update", "refund", "send_email"]
    for t in tools:
        assert t.description, f"tool {t.name} has no description"
        assert isinstance(t.inputSchema, dict)
        assert t.inputSchema.get("properties"), (
            f"tool {t.name} has an empty inputSchema"
        )


# --- call_tool: happy path ---


async def test_call_tool_valid_refund_under_500_returns_success(
    mcp_server: LockrailMCPServer,
) -> None:
    result = await mcp_server.call_tool("refund", {
        "order_id": "ORD-7",
        "amount": 100,
        "customer_id": "CUST-1",
        "justification": "verified ticket #ORD-7 with customer",
    })
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["refunded"] is True
    assert payload["amount"] == 100
    assert payload["refund_id"].startswith("rf_")


# --- call_tool: evidence gate denial ---


async def test_call_tool_short_justification_returns_evidence_gate_error(
    mcp_server: LockrailMCPServer,
) -> None:
    result = await mcp_server.call_tool("refund", {
        "order_id": "ORD-7",
        "amount": 100,
        "customer_id": "CUST-1",
        "justification": "ok",  # too short — fails RefundArgs.justification min_length=10
    })
    assert result.isError is True
    body = result.content[0].text.lower()
    assert "evidence" in body, body
    assert "contract validation" in body or "args failed" in body


# --- call_tool: policy approval requirement ---


async def test_call_tool_refund_over_500_returns_approval_required(
    mcp_server: LockrailMCPServer,
) -> None:
    result = await mcp_server.call_tool("refund", {
        "order_id": "ORD-7",
        "amount": 1000,
        "customer_id": "CUST-1",
        "justification": "high-value refund for VIP customer #ORD-7",
    })
    assert result.isError is True
    body = result.content[0].text
    assert "approval" in body.lower()
    assert "transaction_id=" in body


# --- call_tool: unknown tool ---


async def test_call_tool_unknown_tool_returns_error(
    mcp_server: LockrailMCPServer,
) -> None:
    result = await mcp_server.call_tool("nonexistent", {})
    assert result.isError is True
    assert "unknown tool" in result.content[0].text.lower()


# --- ToolRegistry behavior ---


async def test_duplicate_registration_raises() -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    with pytest.raises(ValueError, match="already registered"):
        register_builtin_tools(reg)


async def test_to_contract_registry_validates_consistently() -> None:
    """The same args_models must validate identically whether they're
    advertised as inputSchema or used by EvidenceGate."""
    reg = ToolRegistry()
    register_builtin_tools(reg)
    contracts = reg.to_contract_registry()

    refund = contracts.get("refund")
    assert refund is not None

    # Valid args validate without error.
    validated = refund.validate_args({
        "order_id": "X",
        "amount": 10.0,
        "customer_id": "C",
        "justification": "a valid reason here",
    })
    assert validated.model_dump()["amount"] == 10.0

    # Invalid args raise.
    with pytest.raises(ValidationError):
        refund.validate_args({
            "order_id": "X",
            "amount": -5,  # gt=0 fails
            "customer_id": "C",
            "justification": "too short",  # also too short
        })
