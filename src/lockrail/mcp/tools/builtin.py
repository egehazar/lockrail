"""Three demo tools — refund, crm_update, send_email — plus default policies.

Wires Lockrail to a refund/CRM/email-shaped MCP server with realistic
contracts: refunds need a justification ≥10 chars, CRM updates need a
field/value pair, emails need recipient and body. These are the args
models that EvidenceGate enforces and that the MCP protocol advertises.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from ...models import (
    AmountThresholdPolicy,
    PolicyAction,
    PolicyRegistry,
    ToolBlacklistPolicy,
)
from ..tool_registry import ToolDefinition, ToolRegistry


class RefundArgs(BaseModel):
    """Args for the refund tool.

    `justification` ≥10 chars is the evidence requirement that turns
    "I'd like to refund this" into a contract — agents calling without
    a real reason hit DENY at the evidence gate before any money moves.
    """
    order_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    customer_id: str = Field(min_length=1)
    justification: str = Field(min_length=10)


class CrmUpdateArgs(BaseModel):
    """Args for the crm_update tool — change one field on one customer."""
    customer_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: str


class SendEmailArgs(BaseModel):
    """Args for the send_email tool."""
    to: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


async def _refund_executor(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "refunded": True,
        "refund_id": f"rf_{uuid4().hex[:8]}",
        "amount": args["amount"],
    }


async def _crm_update_executor(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "updated": True,
        "customer_id": args["customer_id"],
        "field": args["field"],
    }


async def _send_email_executor(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "sent": True,
        "message_id": f"em_{uuid4().hex[:8]}",
    }


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register refund, crm_update, send_email on the given registry."""
    registry.register(ToolDefinition(
        name="refund",
        description=(
            "Issue a refund for an order. Requires a customer-facing "
            "justification of at least 10 characters."
        ),
        args_model=RefundArgs,
        executor=_refund_executor,
    ))
    registry.register(ToolDefinition(
        name="crm_update",
        description="Update a single field on a customer record.",
        args_model=CrmUpdateArgs,
        executor=_crm_update_executor,
    ))
    registry.register(ToolDefinition(
        name="send_email",
        description="Send an email. Audit-logged but not human-gated.",
        args_model=SendEmailArgs,
        executor=_send_email_executor,
    ))


def default_policies() -> PolicyRegistry:
    """Demo policies covering the three builtin tools.

    Priority order (lowest first) chosen so that the hard ceiling wins over
    the require-approval threshold for huge refunds: a $10k refund hits
    DENY at priority 10 and never reaches the priority-50 approval rule.
    """
    return PolicyRegistry(policies=[
        AmountThresholdPolicy(
            name="refund_hard_ceiling_5000",
            applies_to=["refund"],
            action=PolicyAction.DENY,
            priority=10,
            arg_name="amount",
            threshold=5000.0,
            operator="gt",
        ),
        AmountThresholdPolicy(
            name="refund_over_500_requires_approval",
            applies_to=["refund"],
            action=PolicyAction.REQUIRE_APPROVAL,
            priority=50,
            arg_name="amount",
            threshold=500.0,
            operator="gt",
        ),
        ToolBlacklistPolicy(
            name="send_email_log_only",
            applies_to=["send_email"],
            action=PolicyAction.LOG_ONLY,
            priority=100,
        ),
    ])
