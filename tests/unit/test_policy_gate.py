"""PolicyGate behavior + PolicyRegistry semantics + concrete policy types."""
from __future__ import annotations

import pytest

from lockrail.core import Runtime
from lockrail.gates import PolicyGate
from lockrail.models import (
    Actor,
    AmountThresholdPolicy,
    ArgEqualsPolicy,
    GateDecision,
    Policy,
    PolicyAction,
    PolicyRegistry,
    ToolBlacklistPolicy,
    ToolCall,
    TransactionContext,
    TransactionStatus,
)


# --- Helpers ---


def _call(tool: str = "refund", args: dict | None = None) -> ToolCall:
    return ToolCall(
        actor=Actor(agent_id="agent-1", session_id="sess-1"),
        tool_name=tool,
        args=args or {},
    )


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


def _deny_over_5000() -> AmountThresholdPolicy:
    return AmountThresholdPolicy(
        name="refund_over_5000_denied",
        applies_to=["refund"],
        action=PolicyAction.DENY,
        priority=10,
        arg_name="amount",
        threshold=5000.0,
        operator="gt",
    )


# --- Policy abstract base ---


def test_policy_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        Policy(  # type: ignore[abstract]
            name="x", applies_to=["*"], action=PolicyAction.DENY, priority=0,
        )


def test_policy_is_frozen() -> None:
    from pydantic import ValidationError as PydanticValidationError
    p = _approval_over_500()
    with pytest.raises(PydanticValidationError):
        p.name = "other"  # type: ignore[misc]


def test_policy_applies_uses_fnmatch() -> None:
    p = AmountThresholdPolicy(
        name="x",
        applies_to=["refund*", "charge_?"],
        action=PolicyAction.DENY,
        priority=100,
        arg_name="amount",
        threshold=0,
        operator="gt",
    )
    assert p.applies("refund")
    assert p.applies("refund_v2")
    assert p.applies("charge_a")
    assert not p.applies("charge_ab")  # ? matches single char
    assert not p.applies("lookup")


# --- AmountThresholdPolicy operators ---


@pytest.mark.parametrize("op,value,threshold,expected", [
    ("gt", 600, 500, True),
    ("gt", 500, 500, False),
    ("gte", 500, 500, True),
    ("gte", 499, 500, False),
    ("lt", 100, 500, True),
    ("lt", 500, 500, False),
    ("lte", 500, 500, True),
    ("lte", 501, 500, False),
    ("eq", 500, 500, True),
    ("eq", 501, 500, False),
])
def test_amount_threshold_each_operator(op, value, threshold, expected) -> None:
    p = AmountThresholdPolicy(
        name="t", applies_to=["refund"], action=PolicyAction.DENY,
        priority=100, arg_name="amount", threshold=threshold, operator=op,
    )
    matched, _ = p.matches(_call(args={"amount": value}))
    assert matched is expected


def test_amount_threshold_missing_arg_does_not_match() -> None:
    p = _approval_over_500()
    matched, reason = p.matches(_call(args={"order_id": "O-1"}))
    assert matched is False
    assert "not present" in reason


def test_amount_threshold_non_numeric_does_not_match() -> None:
    p = _approval_over_500()
    matched, reason = p.matches(_call(args={"amount": "lots"}))
    assert matched is False
    assert "not numeric" in reason


def test_amount_threshold_bool_is_not_numeric() -> None:
    """True is_instance(int) in Python — but a bool refund amount is nonsense, exclude it."""
    p = _approval_over_500()
    matched, reason = p.matches(_call(args={"amount": True}))
    assert matched is False
    assert "not numeric" in reason


def test_amount_threshold_float_value() -> None:
    p = _approval_over_500()
    matched, _ = p.matches(_call(args={"amount": 501.5}))
    assert matched is True


# --- ArgEqualsPolicy ---


def test_arg_equals_match_and_non_match() -> None:
    p = ArgEqualsPolicy(
        name="block_internal_account",
        applies_to=["transfer"],
        action=PolicyAction.DENY,
        priority=10,
        arg_name="account_type",
        expected="internal",
    )
    assert p.matches(_call("transfer", {"account_type": "internal"}))[0] is True
    assert p.matches(_call("transfer", {"account_type": "external"}))[0] is False


def test_arg_equals_missing_arg() -> None:
    p = ArgEqualsPolicy(
        name="x", applies_to=["transfer"], action=PolicyAction.DENY,
        priority=10, arg_name="account_type", expected="internal",
    )
    matched, reason = p.matches(_call("transfer", {}))
    assert matched is False
    assert "not present" in reason


def test_arg_equals_supports_any_type() -> None:
    p = ArgEqualsPolicy(
        name="x", applies_to=["t"], action=PolicyAction.LOG_ONLY,
        priority=10, arg_name="flag", expected=True,
    )
    assert p.matches(_call("t", {"flag": True}))[0] is True
    assert p.matches(_call("t", {"flag": False}))[0] is False


# --- ToolBlacklistPolicy ---


def test_tool_blacklist_always_matches_in_scope() -> None:
    p = ToolBlacklistPolicy(
        name="no_deletes",
        applies_to=["delete_*"],
        action=PolicyAction.DENY,
        priority=0,
    )
    matched, reason = p.matches(_call("delete_customer", {}))
    assert matched is True
    assert "blacklist" in reason


# --- PolicyRegistry ---


def test_registry_applicable_to_filters_by_fnmatch() -> None:
    reg = PolicyRegistry(policies=[
        _approval_over_500(),
        AmountThresholdPolicy(
            name="lookup_threshold", applies_to=["lookup*"],
            action=PolicyAction.LOG_ONLY, priority=100,
            arg_name="amount", threshold=0, operator="gt",
        ),
    ])
    refund_policies = reg.applicable_to("refund")
    lookup_policies = reg.applicable_to("lookup_v2")
    other = reg.applicable_to("transfer")

    assert [p.name for p in refund_policies] == ["refund_over_500_needs_approval"]
    assert [p.name for p in lookup_policies] == ["lookup_threshold"]
    assert other == []


def test_registry_applicable_to_sorts_by_priority_ascending() -> None:
    reg = PolicyRegistry(policies=[
        _approval_over_500(),  # priority=50
        _deny_over_5000(),     # priority=10
    ])
    out = reg.applicable_to("refund")
    assert [p.priority for p in out] == [10, 50]
    assert out[0].name == "refund_over_5000_denied"


def test_registry_register_rejects_duplicate_name() -> None:
    reg = PolicyRegistry(policies=[_approval_over_500()])
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_approval_over_500())


def test_registry_len() -> None:
    reg = PolicyRegistry()
    assert len(reg) == 0
    reg.register(_approval_over_500())
    assert len(reg) == 1


# --- PolicyGate ---


async def test_gate_skips_when_no_policies_apply() -> None:
    gate = PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))
    ctx = TransactionContext(tool_call=_call("lookup", {"query": "x"}))
    result = await gate.run(ctx)
    assert result.decision == GateDecision.SKIP
    assert "no policies apply" in result.reason
    assert result.details["tool_name"] == "lookup"


async def test_gate_allows_when_policies_apply_but_none_match() -> None:
    gate = PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))
    ctx = TransactionContext(tool_call=_call("refund", {"amount": 100}))  # < 500
    result = await gate.run(ctx)
    assert result.decision == GateDecision.ALLOW
    assert result.reason == "no policies matched"


async def test_gate_denies_on_first_matching_deny() -> None:
    gate = PolicyGate(PolicyRegistry(policies=[_approval_over_500(), _deny_over_5000()]))
    # amount=10000: both policies match. deny has priority=10, approval=50.
    ctx = TransactionContext(tool_call=_call("refund", {"amount": 10000}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.DENY
    assert result.details["policy"] == "refund_over_5000_denied"
    assert result.details["action"] == "deny"
    assert result.details["priority"] == 10


async def test_gate_requires_approval_on_first_matching_approval() -> None:
    gate = PolicyGate(PolicyRegistry(policies=[_approval_over_500(), _deny_over_5000()]))
    # amount=600: only the approval policy matches (under deny threshold).
    ctx = TransactionContext(tool_call=_call("refund", {"amount": 600}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.REQUIRE_APPROVAL
    assert result.details["policy"] == "refund_over_500_needs_approval"
    assert result.details["action"] == "require_approval"


async def test_priority_order_determines_first_match() -> None:
    """When DENY and APPROVAL both match, lower priority value wins."""
    deny_low_priority = AmountThresholdPolicy(
        name="deny_low", applies_to=["refund"], action=PolicyAction.DENY,
        priority=200, arg_name="amount", threshold=100, operator="gt",
    )
    approval_high_priority = AmountThresholdPolicy(
        name="approval_high", applies_to=["refund"],
        action=PolicyAction.REQUIRE_APPROVAL,
        priority=10, arg_name="amount", threshold=100, operator="gt",
    )
    gate = PolicyGate(PolicyRegistry(
        policies=[deny_low_priority, approval_high_priority],
    ))
    ctx = TransactionContext(tool_call=_call("refund", {"amount": 200}))
    result = await gate.run(ctx)

    # Both match; priority=10 (approval) wins despite registration order.
    assert result.decision == GateDecision.REQUIRE_APPROVAL
    assert result.details["policy"] == "approval_high"


async def test_log_only_does_not_block_but_is_recorded() -> None:
    audit_rule = AmountThresholdPolicy(
        name="log_refunds_over_1k",
        applies_to=["refund"],
        action=PolicyAction.LOG_ONLY,
        priority=100,
        arg_name="amount",
        threshold=1000,
        operator="gt",
    )
    gate = PolicyGate(PolicyRegistry(policies=[audit_rule]))
    ctx = TransactionContext(tool_call=_call("refund", {"amount": 2000}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.ALLOW
    assert result.reason == "log-only policies matched"
    matches = result.details["matched_log_only"]
    assert len(matches) == 1
    assert matches[0]["policy"] == "log_refunds_over_1k"


async def test_log_only_continues_to_later_matching_deny() -> None:
    """A LOG_ONLY match must not short-circuit; later DENY still fires."""
    audit_rule = AmountThresholdPolicy(
        name="log_over_500", applies_to=["refund"],
        action=PolicyAction.LOG_ONLY, priority=10,
        arg_name="amount", threshold=500, operator="gt",
    )
    deny_rule = AmountThresholdPolicy(
        name="deny_over_5000", applies_to=["refund"],
        action=PolicyAction.DENY, priority=20,
        arg_name="amount", threshold=5000, operator="gt",
    )
    gate = PolicyGate(PolicyRegistry(policies=[audit_rule, deny_rule]))
    ctx = TransactionContext(tool_call=_call("refund", {"amount": 10000}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.DENY
    assert result.details["policy"] == "deny_over_5000"


async def test_blacklist_blocks_all_calls_to_tool() -> None:
    blacklist = ToolBlacklistPolicy(
        name="no_destructive",
        applies_to=["delete_*"],
        action=PolicyAction.DENY,
        priority=0,
    )
    gate = PolicyGate(PolicyRegistry(policies=[blacklist]))
    ctx = TransactionContext(tool_call=_call("delete_customer", {"id": "X"}))
    result = await gate.run(ctx)

    assert result.decision == GateDecision.DENY
    assert result.details["policy"] == "no_destructive"


# --- Runtime integration ---


async def test_runtime_blocks_when_policy_denies() -> None:
    executed = False

    async def executor(tc: ToolCall) -> dict:
        nonlocal executed
        executed = True
        return {"ok": True}

    rt = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_deny_over_5000()]))],
        executor=executor,
    )
    result = await rt.submit(_call("refund", {"amount": 10_000}))

    assert result.status == TransactionStatus.BLOCKED
    assert result.gate_results[-1].decision == GateDecision.DENY
    assert result.gate_results[-1].gate_name == "policy"
    assert executed is False


async def test_runtime_pends_approval_when_policy_requires() -> None:
    rt = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
    )
    result = await rt.submit(_call("refund", {"amount": 600}))

    assert result.status == TransactionStatus.PENDING_APPROVAL
    assert result.gate_results[-1].decision == GateDecision.REQUIRE_APPROVAL


async def test_runtime_executes_when_policy_allows() -> None:
    async def executor(tc: ToolCall) -> dict:
        return {"refunded": True}

    rt = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
        executor=executor,
    )
    # 100 < 500 threshold → no policy matches → ALLOW → executes.
    result = await rt.submit(_call("refund", {"amount": 100}))

    assert result.status == TransactionStatus.EXECUTED
    assert result.execution_output == {"refunded": True}


async def test_runtime_skip_does_not_halt() -> None:
    """A registry that knows about other tools must SKIP and let execution continue."""
    async def executor(tc: ToolCall) -> dict:
        return {"ok": True}

    rt = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[_approval_over_500()]))],
        executor=executor,
    )
    result = await rt.submit(_call("lookup", {"query": "x"}))

    assert result.status == TransactionStatus.EXECUTED
    assert result.gate_results[0].decision == GateDecision.SKIP
