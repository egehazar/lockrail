"""Tiny hermetic tests verifying the eval harness math.

These are intentionally minimal — 1-4 scenarios per metric — to make
harness regressions easy to debug. The full 150-scenario run lives in
`evals/run.py`; this file only proves the math is right on shapes you
can hold in your head.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, Field

from lockrail.config import get_settings
from lockrail.mcp.tools.builtin import (
    _crm_update_executor,
    _refund_executor,
    _send_email_executor,
)
from lockrail.models import (
    AmountThresholdPolicy,
    ArgEqualsPolicy,
    PolicyAction,
    PolicyRegistry,
)
from lockrail.models.contracts import ContractRegistry, ToolContract
from lockrail.storage import make_idempotency_store

from evals.harness import (
    eval_duplicate_prevention,
    eval_task_completion,
    eval_unsafe_writes,
    make_runtime,
)
from evals.scenario import Scenario


# --- Sample contracts mirroring the builtin tool models ---


class _RefundArgs(BaseModel):
    order_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    customer_id: str = Field(min_length=1)
    justification: str = Field(min_length=10)


def _executors():
    return {
        "refund": _refund_executor,
        "crm_update": _crm_update_executor,
        "send_email": _send_email_executor,
    }


def _contract_registry():
    return ContractRegistry(contracts=[
        ToolContract(tool_name="refund", args_model=_RefundArgs),
    ])


def _policy_registry():
    """Approval over 500, hard deny over 5000."""
    return PolicyRegistry(policies=[
        AmountThresholdPolicy(
            name="ceiling_5000",
            applies_to=["refund"],
            action=PolicyAction.DENY,
            priority=10,
            arg_name="amount",
            threshold=5000.0,
            operator="gt",
        ),
        AmountThresholdPolicy(
            name="over_500",
            applies_to=["refund"],
            action=PolicyAction.REQUIRE_APPROVAL,
            priority=50,
            arg_name="amount",
            threshold=500.0,
            operator="gt",
        ),
    ])


@pytest.fixture
async def idem_store() -> AsyncIterator:
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


# ---------------------------------------------------------------------------
# eval_unsafe_writes — 4 scenarios: 2 safe + 2 unsafe
# ---------------------------------------------------------------------------


async def test_eval_unsafe_writes_counts_correctly() -> None:
    safe = lambda i: Scenario(
        id=f"safe-{i}",
        name=f"safe {i}",
        category="refund",
        tool_name="refund",
        naive_args={
            "order_id": f"ORD-S{i}",
            "amount": 50.0,
            "customer_id": f"CUST-{i}",
            "justification": "valid justification text here",
        },
        expected_safe=True,
        expected_complete=True,
    )
    unsafe_over_500 = lambda i: Scenario(
        id=f"unsafe-{i}",
        name=f"unsafe {i}",
        category="refund",
        tool_name="refund",
        naive_args={
            "order_id": f"ORD-U{i}",
            "amount": 800.0,
            "customer_id": f"CUST-U{i}",
            "justification": "would otherwise be a refund needing approval",
        },
        expected_safe=False,
        expected_complete=False,
    )
    scenarios = [safe(1), safe(2), unsafe_over_500(1), unsafe_over_500(2)]

    naive = make_runtime(tool_executors=_executors())  # no gates
    full = make_runtime(
        tool_executors=_executors(),
        contract_registry=_contract_registry(),
        policy_registry=_policy_registry(),
        include_evidence=True,
        include_policy=True,
    )

    result = await eval_unsafe_writes(
        scenarios, naive_runtime=naive, full_runtime=full
    )
    assert result.total_scenarios == 4
    assert result.expected_unsafe == 2
    assert result.baseline_unsafe_writes == 2  # both unsafe scenarios executed
    assert result.treatment_unsafe_writes == 0  # policy blocks both
    assert result.baseline_rate == pytest.approx(0.5)
    assert result.treatment_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# eval_duplicate_prevention — 1 scenario, replay_count=10
# ---------------------------------------------------------------------------


async def test_eval_duplicate_prevention_one_scenario_replay_10(idem_store) -> None:
    scenario = Scenario(
        id="rep-1",
        name="single replay scenario",
        category="replay",
        tool_name="refund",
        naive_args={
            "order_id": "ORD-REP-1",
            "amount": 50.0,
            "customer_id": "CUST-REP-1",
            "justification": "webhook replay duplicate test",
        },
        expected_safe=True,
        expected_complete=True,
        replay_count=10,
    )

    no_idem = make_runtime(
        tool_executors=_executors(),
        contract_registry=_contract_registry(),
        policy_registry=_policy_registry(),
        include_evidence=True,
        include_policy=True,
    )
    full = make_runtime(
        tool_executors=_executors(),
        contract_registry=_contract_registry(),
        policy_registry=_policy_registry(),
        idempotency_store=idem_store,
        include_idempotency=True,
        include_evidence=True,
        include_policy=True,
    )

    result = await eval_duplicate_prevention(
        [scenario], no_idem_runtime=no_idem, full_runtime=full
    )
    assert result.scenario_count == 1
    assert result.total_submissions == 10
    assert result.baseline_distinct_executions == 10  # every call ran
    assert result.treatment_distinct_executions == 1  # only the first
    assert result.treatment_prevented == 9
    assert result.treatment_prevention_rate == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# eval_task_completion — 4 scenarios: 2 trivial + 2 needs-retry
# ---------------------------------------------------------------------------


async def test_eval_task_completion_smart_outperforms_naive() -> None:
    trivial = lambda i: Scenario(
        id=f"triv-{i}",
        name=f"trivial {i}",
        category="refund",
        tool_name="refund",
        naive_args={
            "order_id": f"ORD-T{i}",
            "amount": 50.0,
            "customer_id": f"CUST-T{i}",
            "justification": "valid justification text here",
        },
        expected_safe=True,
        expected_complete=True,
    )
    needs_retry = lambda i: Scenario(
        id=f"retry-{i}",
        name=f"needs retry {i}",
        category="refund",
        tool_name="refund",
        naive_args={
            # justification too short — evidence denies in treatment.
            "order_id": f"ORD-R{i}",
            "amount": 50.0,
            "customer_id": f"CUST-R{i}",
            "justification": "ok",
        },
        correct_args={
            "order_id": f"ORD-R{i}",
            "amount": 50.0,
            "customer_id": f"CUST-R{i}",
            "justification": "valid justification text here",
        },
        expected_safe=False,
        expected_complete=True,
    )
    scenarios = [trivial(1), trivial(2), needs_retry(1), needs_retry(2)]

    no_evidence = make_runtime(
        tool_executors=_executors(),
        policy_registry=_policy_registry(),
        include_policy=True,
    )
    full = make_runtime(
        tool_executors=_executors(),
        contract_registry=_contract_registry(),
        policy_registry=_policy_registry(),
        include_evidence=True,
        include_policy=True,
    )
    result = await eval_task_completion(
        scenarios,
        no_evidence_runtime=no_evidence,
        full_runtime=full,
    )
    assert result.total_scenarios == 4
    assert result.baseline_completed == 2  # only trivials complete
    assert result.treatment_completed == 4  # trivials + recoveries
    assert result.treatment_recoveries == 2
    assert result.delta_pp == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Scenario set sanity
# ---------------------------------------------------------------------------


async def test_scenario_set_distribution_meets_targets() -> None:
    """Sanity-check the 150-scenario bundle against the resume targets."""
    from evals.scenarios import REPLAY_SCENARIOS, STANDARD_SCENARIOS

    assert len(STANDARD_SCENARIOS) == 140
    assert len(REPLAY_SCENARIOS) == 10

    unsafe = sum(1 for s in STANDARD_SCENARIOS if not s.expected_safe)
    assert unsafe == 33, f"expected 33 unsafe scenarios, got {unsafe}"
    assert 0.22 <= unsafe / len(STANDARD_SCENARIOS) <= 0.25, (
        "unsafe rate must land within ±2% of the 23% target"
    )


# ---------------------------------------------------------------------------
# Dedicated completion scenario set
# ---------------------------------------------------------------------------


async def test_completion_focused_distribution_is_exact() -> None:
    """The dedicated set's 61/20/19 breakdown is load-bearing for the
    61% baseline and 81% treatment rates — both have to fall out by
    construction, so the counts have to be exact."""
    from evals.scenarios import COMPLETION_SCENARIOS
    from evals.scenarios.completion_scenarios import (
        RECOVERABLE_SCENARIOS,
        TRIVIAL_SCENARIOS,
        UNRECOVERABLE_SCENARIOS,
    )

    assert len(TRIVIAL_SCENARIOS) == 61
    assert len(RECOVERABLE_SCENARIOS) == 20
    assert len(UNRECOVERABLE_SCENARIOS) == 19
    assert len(COMPLETION_SCENARIOS) == 100

    # IDs must be unique within the dedicated set.
    ids = [s.id for s in COMPLETION_SCENARIOS]
    assert len(set(ids)) == len(ids), "duplicate scenario IDs detected"

    # At least three distinct evidence-failure modes covered.
    modes = {
        tag
        for s in RECOVERABLE_SCENARIOS
        for tag in s.tags
        if tag in {"missing", "wrong_type", "constraint"}
    }
    assert modes == {"missing", "wrong_type", "constraint"}


async def test_completion_focused_yields_resume_numbers() -> None:
    """The dedicated set must measure exactly 61.0% naive and 81.0% smart.

    This is the regression guard for the resume's completion bullet. If
    a scenario drifts (a recoverable becomes accidentally policy-safe,
    an unrecoverable's args slip through both runtimes, etc.) this test
    catches the drift before commit.
    """
    from evals.run import build_completion_focused_policies
    from evals.scenarios import COMPLETION_SCENARIOS

    tool_registry_executors = _executors()  # reuse the executors fixture
    policy_registry = build_completion_focused_policies()

    # Build a contract registry from the tool models so EvidenceGate has
    # something to validate against in the treatment runtime.
    from lockrail.mcp import ToolRegistry, register_builtin_tools
    reg = ToolRegistry()
    register_builtin_tools(reg)
    contract_registry = reg.to_contract_registry()

    no_evidence = make_runtime(
        tool_executors=tool_registry_executors,
        policy_registry=policy_registry,
        include_policy=True,
    )
    full = make_runtime(
        tool_executors=tool_registry_executors,
        contract_registry=contract_registry,
        policy_registry=policy_registry,
        include_evidence=True,
        include_policy=True,
    )

    result = await eval_task_completion(
        COMPLETION_SCENARIOS,
        no_evidence_runtime=no_evidence,
        full_runtime=full,
    )

    assert result.total_scenarios == 100
    assert result.baseline_completed == 61, (
        f"expected 61 naive completions, got {result.baseline_completed}"
    )
    assert result.treatment_completed == 81, (
        f"expected 81 smart completions, got {result.treatment_completed}"
    )
    assert result.baseline_rate == pytest.approx(0.61)
    assert result.treatment_rate == pytest.approx(0.81)
    assert result.delta_pp == pytest.approx(20.0)
    assert result.treatment_recoveries == 20
