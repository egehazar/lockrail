"""EvidenceGate behavior + ContractRegistry semantics."""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from lockrail.core import Runtime
from lockrail.gates import EvidenceGate
from lockrail.models import (
    Actor,
    GateDecision,
    ToolCall,
    TransactionContext,
    TransactionStatus,
)
from lockrail.models.contracts import ContractRegistry, ToolContract


# --- Sample contracts mirroring what a real refund/lookup workload looks like ---


class RefundArgs(BaseModel):
    """Refund tool contract: ticket + order references + justification required."""
    ticket_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    justification: str = Field(min_length=10)


class LookupArgs(BaseModel):
    """Read-only lookup; only a query string is required."""
    query: str = Field(min_length=1)


def _registry() -> ContractRegistry:
    return ContractRegistry(contracts=[
        ToolContract(tool_name="refund", args_model=RefundArgs),
        ToolContract(tool_name="lookup", args_model=LookupArgs),
    ])


def _call(tool: str, args: dict) -> ToolCall:
    return ToolCall(
        actor=Actor(agent_id="agent-1", session_id="sess-1"),
        tool_name=tool,
        args=args,
    )


def _valid_refund_args() -> dict:
    return {
        "ticket_id": "T-1",
        "order_id": "O-1",
        "amount": 49.99,
        "justification": "customer requested refund per ticket",
    }


# --- ContractRegistry tests ---


def test_registry_get_returns_none_for_unknown_tool() -> None:
    reg = ContractRegistry()
    assert reg.get("nonexistent") is None
    assert "nonexistent" not in reg
    assert len(reg) == 0


def test_registry_register_and_get_round_trip() -> None:
    reg = _registry()
    contract = reg.get("refund")
    assert contract is not None
    assert contract.tool_name == "refund"
    assert contract.args_model is RefundArgs
    assert "refund" in reg
    assert len(reg) == 2


def test_registry_rejects_duplicate_registration() -> None:
    reg = _registry()
    with pytest.raises(ValueError, match="already has a registered contract"):
        reg.register(ToolContract(tool_name="refund", args_model=RefundArgs))


def test_tool_contract_is_frozen() -> None:
    from pydantic import ValidationError as PydanticValidationError
    contract = ToolContract(tool_name="refund", args_model=RefundArgs)
    with pytest.raises(PydanticValidationError):
        contract.tool_name = "other"  # type: ignore[misc]


# --- EvidenceGate unit tests ---


async def test_skip_when_no_contract_for_tool() -> None:
    gate = EvidenceGate(_registry())
    ctx = TransactionContext(tool_call=_call("unknown_tool", {"x": 1}))
    result = await gate.run(ctx)
    assert result.decision == GateDecision.SKIP
    assert result.reason == "no contract"
    assert result.details["tool_name"] == "unknown_tool"


async def test_deny_when_required_field_missing() -> None:
    gate = EvidenceGate(_registry())
    # ticket_id, amount, justification all missing
    ctx = TransactionContext(tool_call=_call("refund", {"order_id": "O-1"}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.DENY
    assert result.details["tool_name"] == "refund"
    assert result.details["error_count"] >= 3

    locs = {tuple(e["loc"]) for e in result.details["errors"]}
    assert ("ticket_id",) in locs
    assert ("amount",) in locs
    assert ("justification",) in locs

    for err in result.details["errors"]:
        if tuple(err["loc"]) in {("ticket_id",), ("amount",), ("justification",)}:
            assert err["type"] == "missing"


async def test_deny_when_field_has_wrong_type() -> None:
    gate = EvidenceGate(_registry())
    bad = _valid_refund_args() | {"amount": "not a number"}
    ctx = TransactionContext(tool_call=_call("refund", bad))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.DENY
    amount_errs = [
        e for e in result.details["errors"] if tuple(e["loc"]) == ("amount",)
    ]
    assert len(amount_errs) == 1
    # pydantic's type error code for floats from a non-numeric string
    assert "float" in amount_errs[0]["type"] or "parsing" in amount_errs[0]["type"]


async def test_deny_when_field_violates_constraint() -> None:
    """Constraint violation (justification too short) is treated the same as a type error."""
    gate = EvidenceGate(_registry())
    bad = _valid_refund_args() | {"justification": "too short"}
    ctx = TransactionContext(tool_call=_call("refund", bad))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.DENY
    justif_errs = [
        e for e in result.details["errors"] if tuple(e["loc"]) == ("justification",)
    ]
    assert len(justif_errs) == 1


async def test_allow_when_args_validate() -> None:
    gate = EvidenceGate(_registry())
    ctx = TransactionContext(tool_call=_call("refund", _valid_refund_args()))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.ALLOW
    assert result.reason == "args validated"
    assert result.details["tool_name"] == "refund"
    assert set(result.details["validated_fields"]) == {
        "ticket_id", "order_id", "amount", "justification",
    }


async def test_allow_on_minimal_lookup_contract() -> None:
    gate = EvidenceGate(_registry())
    ctx = TransactionContext(tool_call=_call("lookup", {"query": "refund policy"}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.ALLOW
    assert result.details["validated_fields"] == ["query"]


# --- Integration with Runtime ---


async def test_runtime_blocks_when_evidence_gate_denies() -> None:
    executed = False

    async def executor(tc: ToolCall) -> dict:
        nonlocal executed
        executed = True
        return {"ok": True}

    rt = Runtime(gates=[EvidenceGate(_registry())], executor=executor)
    result = await rt.submit(_call("refund", {"order_id": "O-1"}))

    assert result.status == TransactionStatus.BLOCKED
    assert result.gate_results[-1].decision == GateDecision.DENY
    assert result.gate_results[-1].gate_name == "evidence"
    assert executed is False, "executor must not run when EvidenceGate denies"


async def test_runtime_executes_when_evidence_gate_allows() -> None:
    async def executor(tc: ToolCall) -> dict:
        return {"refund_id": "rf-1", "amount": tc.args["amount"]}

    rt = Runtime(gates=[EvidenceGate(_registry())], executor=executor)
    result = await rt.submit(_call("refund", _valid_refund_args()))

    assert result.status == TransactionStatus.EXECUTED
    assert result.execution_output == {"refund_id": "rf-1", "amount": 49.99}
    assert result.gate_results[0].decision == GateDecision.ALLOW


async def test_runtime_executes_when_no_contract_registered() -> None:
    """SKIP must not halt the pipeline — unknown tools fall through to other gates."""
    async def executor(tc: ToolCall) -> dict:
        return {"ok": True}

    rt = Runtime(gates=[EvidenceGate(ContractRegistry())], executor=executor)
    result = await rt.submit(_call("anything", {"x": 1}))

    assert result.status == TransactionStatus.EXECUTED
    assert result.gate_results[0].decision == GateDecision.SKIP
