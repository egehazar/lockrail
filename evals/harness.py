"""Eval harness — three independent metrics over the scenario set.

Each metric runs the same scenarios through two Runtime variants and
returns a structured result. The harness consumes only the public
Runtime + gate APIs; nothing in src/lockrail is modified.

Metric definitions:

  - unsafe_writes
      baseline  = a `naive_runtime` that has no enforcement gates (an
                  unaided agent talking straight to the executor).
      treatment = a `full_runtime` with the complete idempotency +
                  evidence + policy pipeline.
      submit    = `naive_args` for every scenario (the agent's first
                  attempt; no retry).
      success   = a scenario is an "unsafe write" iff the runtime
                  returned EXECUTED *and* the scenario was flagged
                  `expected_safe=False`. Rate = unsafe-writes / total.

  - duplicate_prevention
      baseline  = a runtime without IdempotencyGate.
      treatment = a runtime with IdempotencyGate.
      submit    = each replay scenario is submitted `replay_count` times
                  with identical Actor + args (so the fingerprint is the
                  same across calls).
      count     = total submissions vs distinct EXECUTED outcomes.

  - task_completion
      baseline  = a runtime *without* EvidenceGate (policy and
                  idempotency still present) — the "naive agent": one
                  shot, no retry.
      treatment = full pipeline plus a smart-agent retry loop that, on a
                  BLOCKED outcome whose blocking gate is `evidence` and
                  whose scenario has `correct_args`, resubmits with
                  `correct_args`.
      success   = EXECUTED *and* the submitted args equal `correct_args`
                  (or `naive_args` when `correct_args` is None). Per
                  spec: "executed AND args were the correct ones".
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lockrail.core import Runtime
from lockrail.gates import EvidenceGate, IdempotencyGate, PolicyGate
from lockrail.models import Actor, ToolCall, TransactionStatus
from lockrail.models.contracts import ContractRegistry
from lockrail.models.policy import PolicyRegistry
from lockrail.storage import IdempotencyStore

from .scenario import Scenario


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class CategoryStat(BaseModel):
    model_config = ConfigDict(frozen=True)
    category: str
    total: int
    counted: int


class UnsafeWritesResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    total_scenarios: int
    expected_unsafe: int
    baseline_unsafe_writes: int
    treatment_unsafe_writes: int
    baseline_rate: float
    treatment_rate: float
    baseline_by_category: list[CategoryStat]
    treatment_by_category: list[CategoryStat]


class DupPreventionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    scenario_count: int
    total_submissions: int
    baseline_distinct_executions: int
    treatment_distinct_executions: int
    treatment_prevented: int
    treatment_prevention_rate: float


class CompletionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    total_scenarios: int
    baseline_completed: int
    treatment_completed: int
    baseline_rate: float
    treatment_rate: float
    delta_pp: float
    treatment_recoveries: int  # scenarios where evidence-retry rescued a failure
    baseline_by_category: list[CategoryStat]
    treatment_by_category: list[CategoryStat]


# ---------------------------------------------------------------------------
# Runtime construction helpers
# ---------------------------------------------------------------------------


ToolFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _make_actor(scenario: Scenario, replay_index: int | None = None) -> Actor:
    """Stable actor per scenario. Replays share the same actor so the
    fingerprint matches across calls — that's what triggers cache hits
    in the duplicate-prevention metric. Different scenarios get distinct
    actors so they don't accidentally collide.
    """
    return Actor(
        agent_id=f"eval-agent",
        session_id=f"eval-{scenario.id}",
    )


def _executor_for(tool_executors: dict[str, ToolFn]) -> Callable[[ToolCall], Awaitable[dict[str, Any]]]:
    async def _run(tc: ToolCall) -> dict[str, Any]:
        fn = tool_executors.get(tc.tool_name)
        if fn is None:
            raise ValueError(f"no executor registered for tool {tc.tool_name!r}")
        return await fn(tc.args)
    return _run


def make_runtime(
    *,
    tool_executors: dict[str, ToolFn],
    contract_registry: ContractRegistry | None = None,
    policy_registry: PolicyRegistry | None = None,
    idempotency_store: IdempotencyStore | None = None,
    include_evidence: bool = False,
    include_policy: bool = False,
    include_idempotency: bool = False,
) -> Runtime:
    """Build a Runtime with the chosen subset of gates.

    Runtime requires at least one gate. When *no* enforcement is wanted
    (the unsafe-writes baseline), we use an empty PolicyGate — it always
    skips, so it's a no-op pass-through that satisfies the constructor.
    """
    gates = []
    if include_idempotency:
        assert idempotency_store is not None, "idempotency requires a store"
        gates.append(IdempotencyGate(idempotency_store))
    if include_evidence:
        assert contract_registry is not None, "evidence requires a contract registry"
        gates.append(EvidenceGate(contract_registry))
    if include_policy:
        assert policy_registry is not None, "policy requires a policy registry"
        gates.append(PolicyGate(policy_registry))
    if not gates:
        # No-op stub: empty policy registry → PolicyGate.skip() on every call.
        gates.append(PolicyGate(PolicyRegistry()))
    return Runtime(
        gates=gates,
        executor=_executor_for(tool_executors),
        idempotency_store=idempotency_store if include_idempotency else None,
        # session_factory=None: persistence is not exercised by evals.
    )


# ---------------------------------------------------------------------------
# eval_unsafe_writes
# ---------------------------------------------------------------------------


async def eval_unsafe_writes(
    scenarios: list[Scenario],
    *,
    naive_runtime: Runtime,
    full_runtime: Runtime,
) -> UnsafeWritesResult:
    """Measure the policy + evidence stack's effect on unsafe writes.

    A scenario is an "unsafe write" if its `expected_safe` is False and
    the runtime returned EXECUTED.
    """
    expected_unsafe = sum(1 for s in scenarios if not s.expected_safe)
    baseline_unsafe = 0
    treatment_unsafe = 0
    baseline_by_cat: dict[str, int] = defaultdict(int)
    treatment_by_cat: dict[str, int] = defaultdict(int)
    cat_totals: dict[str, int] = defaultdict(int)

    for s in scenarios:
        cat_totals[s.category] += 1
        actor = _make_actor(s)

        # Baseline: submit naive_args through the unenforced runtime.
        b_result = await naive_runtime.submit(
            ToolCall(actor=actor, tool_name=s.tool_name, args=s.naive_args)
        )
        if (
            b_result.status in (TransactionStatus.EXECUTED, TransactionStatus.REPLAYED)
            and not s.expected_safe
        ):
            baseline_unsafe += 1
            baseline_by_cat[s.category] += 1

        # Treatment: submit naive_args through the full pipeline.
        t_result = await full_runtime.submit(
            ToolCall(actor=actor, tool_name=s.tool_name, args=s.naive_args)
        )
        if (
            t_result.status in (TransactionStatus.EXECUTED, TransactionStatus.REPLAYED)
            and not s.expected_safe
        ):
            treatment_unsafe += 1
            treatment_by_cat[s.category] += 1

    return UnsafeWritesResult(
        total_scenarios=len(scenarios),
        expected_unsafe=expected_unsafe,
        baseline_unsafe_writes=baseline_unsafe,
        treatment_unsafe_writes=treatment_unsafe,
        baseline_rate=baseline_unsafe / len(scenarios) if scenarios else 0.0,
        treatment_rate=treatment_unsafe / len(scenarios) if scenarios else 0.0,
        baseline_by_category=[
            CategoryStat(category=c, total=cat_totals[c], counted=baseline_by_cat[c])
            for c in sorted(cat_totals)
        ],
        treatment_by_category=[
            CategoryStat(category=c, total=cat_totals[c], counted=treatment_by_cat[c])
            for c in sorted(cat_totals)
        ],
    )


# ---------------------------------------------------------------------------
# eval_duplicate_prevention
# ---------------------------------------------------------------------------


async def eval_duplicate_prevention(
    replay_scenarios: list[Scenario],
    *,
    no_idem_runtime: Runtime,
    full_runtime: Runtime,
) -> DupPreventionResult:
    """Measure IdempotencyGate's effect on webhook-replay duplicate calls.

    Each scenario is submitted `replay_count` times with identical
    Actor + args. In the no-idempotency runtime, every submission
    executes. In the full-pipeline runtime, the first executes and the
    rest come back as REPLAYED (cache hit) — distinct execution count
    equals the scenario count, total submissions equals
    sum(replay_count).
    """
    total_submissions = 0
    baseline_distinct = 0
    treatment_distinct = 0

    for s in replay_scenarios:
        actor = _make_actor(s)
        for _ in range(s.replay_count):
            total_submissions += 1
            tc_b = ToolCall(actor=actor, tool_name=s.tool_name, args=s.naive_args)
            b_result = await no_idem_runtime.submit(tc_b)
            if b_result.status == TransactionStatus.EXECUTED:
                baseline_distinct += 1

            tc_t = ToolCall(actor=actor, tool_name=s.tool_name, args=s.naive_args)
            t_result = await full_runtime.submit(tc_t)
            if t_result.status == TransactionStatus.EXECUTED:
                treatment_distinct += 1

    prevented = total_submissions - treatment_distinct
    rate = prevented / total_submissions if total_submissions else 0.0
    return DupPreventionResult(
        scenario_count=len(replay_scenarios),
        total_submissions=total_submissions,
        baseline_distinct_executions=baseline_distinct,
        treatment_distinct_executions=treatment_distinct,
        treatment_prevented=prevented,
        treatment_prevention_rate=rate,
    )


# ---------------------------------------------------------------------------
# eval_task_completion
# ---------------------------------------------------------------------------


def _last_blocking_gate(tx_result: Any) -> str | None:
    """Pull the name of the gate that halted the transaction (if any)."""
    if not tx_result.gate_results:
        return None
    last = tx_result.gate_results[-1]
    if last.is_blocking:
        return last.gate_name
    return None


async def _submit(
    runtime: Runtime, scenario: Scenario, actor: Actor, args: dict[str, Any]
) -> Any:
    return await runtime.submit(
        ToolCall(actor=actor, tool_name=scenario.tool_name, args=args)
    )


async def eval_task_completion(
    scenarios: list[Scenario],
    *,
    no_evidence_runtime: Runtime,
    full_runtime: Runtime,
) -> CompletionResult:
    """Measure EvidenceGate + smart-retry's effect on task completion.

    Success per scenario = the runtime ended in EXECUTED *and* the args
    that ran equaled `correct_args` (or `naive_args` if `correct_args`
    is None). The naive agent submits once and never retries; the smart
    agent retries with `correct_args` whenever the EvidenceGate denies
    `naive_args`.
    """
    baseline_completed = 0
    treatment_completed = 0
    treatment_recoveries = 0
    baseline_by_cat: dict[str, int] = defaultdict(int)
    treatment_by_cat: dict[str, int] = defaultdict(int)
    cat_totals: dict[str, int] = defaultdict(int)

    for s in scenarios:
        cat_totals[s.category] += 1
        actor = _make_actor(s)
        intended = s.correct_args if s.correct_args is not None else s.naive_args

        # Naive agent on baseline runtime: one shot with naive_args.
        b_result = await _submit(no_evidence_runtime, s, actor, s.naive_args)
        if (
            b_result.status == TransactionStatus.EXECUTED
            and s.naive_args == intended
        ):
            baseline_completed += 1
            baseline_by_cat[s.category] += 1

        # Smart agent on full runtime: try naive_args; if evidence denied
        # and we know the fix, retry with correct_args.
        t_result = await _submit(full_runtime, s, actor, s.naive_args)
        succeeded_args: dict[str, Any] | None = None
        if t_result.status == TransactionStatus.EXECUTED:
            succeeded_args = s.naive_args
        elif (
            _last_blocking_gate(t_result) == "evidence"
            and s.correct_args is not None
        ):
            retry = await _submit(full_runtime, s, actor, s.correct_args)
            if retry.status == TransactionStatus.EXECUTED:
                succeeded_args = s.correct_args
                treatment_recoveries += 1
        if succeeded_args is not None and succeeded_args == intended:
            treatment_completed += 1
            treatment_by_cat[s.category] += 1

    total = len(scenarios)
    baseline_rate = baseline_completed / total if total else 0.0
    treatment_rate = treatment_completed / total if total else 0.0
    return CompletionResult(
        total_scenarios=total,
        baseline_completed=baseline_completed,
        treatment_completed=treatment_completed,
        baseline_rate=baseline_rate,
        treatment_rate=treatment_rate,
        delta_pp=(treatment_rate - baseline_rate) * 100.0,
        treatment_recoveries=treatment_recoveries,
        baseline_by_category=[
            CategoryStat(category=c, total=cat_totals[c], counted=baseline_by_cat[c])
            for c in sorted(cat_totals)
        ],
        treatment_by_category=[
            CategoryStat(category=c, total=cat_totals[c], counted=treatment_by_cat[c])
            for c in sorted(cat_totals)
        ],
    )
