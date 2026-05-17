"""Eval CLI — run one or all metrics, write JSON + optional markdown report.

Usage (from repo root):
    uv run python evals/run.py --metric all
    uv run python evals/run.py --metric unsafe_writes
    uv run python evals/run.py --metric duplicates
    uv run python evals/run.py --metric completion
    uv run python evals/run.py --metric all --output evals/results/report.md

Requires docker compose stack up (postgres + redis), but only Redis is
actually used by these evals. Postgres is touched only by other tests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow `uv run python evals/run.py` from the repo root by ensuring the
# repo is importable as a sibling of the `evals` package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import BaseModel

from lockrail.config import get_settings
from lockrail.mcp import ToolRegistry, default_policies, register_builtin_tools
from lockrail.mcp.tools.builtin import (
    _crm_update_executor,
    _refund_executor,
    _send_email_executor,
)
from lockrail.storage import make_idempotency_store

from evals.harness import (
    CompletionResult,
    DupPreventionResult,
    UnsafeWritesResult,
    eval_duplicate_prevention,
    eval_task_completion,
    eval_unsafe_writes,
    make_runtime,
)
from evals.scenarios import REPLAY_SCENARIOS, STANDARD_SCENARIOS

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "evals" / "results"


def _build_tool_executors() -> dict[str, Any]:
    """Reuse the executor stubs from mcp/tools/builtin.py (per spec)."""
    return {
        "refund": _refund_executor,
        "crm_update": _crm_update_executor,
        "send_email": _send_email_executor,
    }


def _build_tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_builtin_tools(reg)
    return reg


async def _flush_idempotency(store) -> None:
    """Wipe lockrail:idem:* keys so a fresh metric run isn't influenced
    by cached results from a prior metric (or a prior process)."""
    keys = [k async for k in store.client.scan_iter("lockrail:idem:*")]
    if keys:
        await store.client.delete(*keys)


def _eval_policies_with_crm_protection():
    """Default policies plus an ArgEqualsPolicy that denies any
    crm_update where `field == "ssn"`. This is the policy our CRM
    "protected field" scenarios trip.
    """
    from lockrail.models import ArgEqualsPolicy, PolicyAction

    registry = default_policies()
    registry.register(ArgEqualsPolicy(
        name="crm_protected_field_ssn",
        applies_to=["crm_update"],
        action=PolicyAction.DENY,
        priority=5,  # higher precedence than other crm policies
        arg_name="field",
        expected="ssn",
    ))
    return registry


# ---------------------------------------------------------------------------
# Metric drivers
# ---------------------------------------------------------------------------


async def run_unsafe_writes(store) -> UnsafeWritesResult:
    tool_executors = _build_tool_executors()
    tool_registry = _build_tool_registry()
    contract_registry = tool_registry.to_contract_registry()
    policy_registry = _eval_policies_with_crm_protection()

    await _flush_idempotency(store)
    naive = make_runtime(tool_executors=tool_executors)  # no gates
    full = make_runtime(
        tool_executors=tool_executors,
        contract_registry=contract_registry,
        policy_registry=policy_registry,
        idempotency_store=store,
        include_idempotency=True,
        include_evidence=True,
        include_policy=True,
    )
    return await eval_unsafe_writes(
        STANDARD_SCENARIOS,
        naive_runtime=naive,
        full_runtime=full,
    )


async def run_duplicate_prevention(store) -> DupPreventionResult:
    tool_executors = _build_tool_executors()
    tool_registry = _build_tool_registry()
    contract_registry = tool_registry.to_contract_registry()
    policy_registry = _eval_policies_with_crm_protection()

    await _flush_idempotency(store)
    no_idem = make_runtime(
        tool_executors=tool_executors,
        contract_registry=contract_registry,
        policy_registry=policy_registry,
        include_evidence=True,
        include_policy=True,
    )
    full = make_runtime(
        tool_executors=tool_executors,
        contract_registry=contract_registry,
        policy_registry=policy_registry,
        idempotency_store=store,
        include_idempotency=True,
        include_evidence=True,
        include_policy=True,
    )
    return await eval_duplicate_prevention(
        REPLAY_SCENARIOS,
        no_idem_runtime=no_idem,
        full_runtime=full,
    )


async def run_completion(store) -> CompletionResult:
    tool_executors = _build_tool_executors()
    tool_registry = _build_tool_registry()
    contract_registry = tool_registry.to_contract_registry()
    policy_registry = _eval_policies_with_crm_protection()

    await _flush_idempotency(store)
    no_evidence = make_runtime(
        tool_executors=tool_executors,
        policy_registry=policy_registry,
        include_policy=True,
    )
    full = make_runtime(
        tool_executors=tool_executors,
        contract_registry=contract_registry,
        policy_registry=policy_registry,
        idempotency_store=store,
        include_idempotency=True,
        include_evidence=True,
        include_policy=True,
    )
    return await eval_task_completion(
        STANDARD_SCENARIOS,
        no_evidence_runtime=no_evidence,
        full_runtime=full,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _write_json(name: str, payload: BaseModel) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"{name}_{ts}.json"
    out.write_text(payload.model_dump_json(indent=2))
    return out


def _format_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _format_report(
    *,
    unsafe: UnsafeWritesResult | None,
    dups: DupPreventionResult | None,
    completion: CompletionResult | None,
) -> str:
    lines: list[str] = []
    lines.append("# Lockrail eval report")
    lines.append("")
    lines.append(
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    lines.append("")
    lines.append("## Headline numbers")
    lines.append("")
    headline: list[str] = []
    if unsafe is not None:
        headline.append(
            f"- **Unsafe writes**: {_format_pct(unsafe.baseline_rate)} → "
            f"{_format_pct(unsafe.treatment_rate)} "
            f"({unsafe.baseline_unsafe_writes} → {unsafe.treatment_unsafe_writes} "
            f"of {unsafe.total_scenarios} scenarios)."
        )
    if dups is not None:
        headline.append(
            f"- **Duplicate prevention**: {_format_pct(dups.treatment_prevention_rate)} "
            f"of {dups.total_submissions} replays prevented "
            f"({dups.treatment_distinct_executions} distinct executions)."
        )
    if completion is not None:
        headline.append(
            f"- **Task completion**: {_format_pct(completion.baseline_rate)} → "
            f"{_format_pct(completion.treatment_rate)} "
            f"(+{completion.delta_pp:.1f}pp; {completion.treatment_recoveries} "
            f"recoveries via evidence-retry)."
        )
    lines.extend(headline)
    lines.append("")

    if unsafe is not None:
        lines.append("## Unsafe writes")
        lines.append("")
        lines.append(
            f"Total scenarios: {unsafe.total_scenarios}. Of these, "
            f"{unsafe.expected_unsafe} are flagged `expected_safe=False`."
        )
        lines.append("")
        lines.append("| Category | Total | Baseline unsafe | Treatment unsafe |")
        lines.append("|----------|-------|-----------------|-------------------|")
        treat = {c.category: c.counted for c in unsafe.treatment_by_category}
        for c in unsafe.baseline_by_category:
            lines.append(
                f"| {c.category} | {c.total} | {c.counted} | {treat.get(c.category, 0)} |"
            )
        lines.append("")

    if dups is not None:
        lines.append("## Duplicate prevention")
        lines.append("")
        lines.append(
            f"- {dups.scenario_count} replay scenarios fired "
            f"{dups.total_submissions} total submissions."
        )
        lines.append(
            f"- Without IdempotencyGate: {dups.baseline_distinct_executions} "
            "distinct executions (every submission ran)."
        )
        lines.append(
            f"- With IdempotencyGate: {dups.treatment_distinct_executions} "
            f"distinct executions, {dups.treatment_prevented} prevented "
            f"({_format_pct(dups.treatment_prevention_rate)})."
        )
        lines.append("")

    if completion is not None:
        lines.append("## Task completion")
        lines.append("")
        lines.append(
            f"Total scenarios: {completion.total_scenarios}. "
            f"Baseline completed: {completion.baseline_completed}; "
            f"treatment completed: {completion.treatment_completed} "
            f"({completion.treatment_recoveries} via evidence-deny + retry)."
        )
        lines.append("")
        lines.append("| Category | Total | Baseline completed | Treatment completed |")
        lines.append("|----------|-------|--------------------|----------------------|")
        treat = {c.category: c.counted for c in completion.treatment_by_category}
        for c in completion.baseline_by_category:
            lines.append(
                f"| {c.category} | {c.total} | {c.counted} | {treat.get(c.category, 0)} |"
            )
        lines.append("")

    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "See `evals/README.md` for the full methodology — what baseline + "
        "treatment runtimes look like for each metric, what counts as success, "
        "and the honest framing of what these numbers do and don't prove."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = await make_idempotency_store(settings.redis_url, ttl_seconds=300)

    unsafe = dups = completion = None
    try:
        if args.metric in ("all", "unsafe_writes"):
            print("→ eval_unsafe_writes", file=sys.stderr)
            unsafe = await run_unsafe_writes(store)
            path = _write_json("unsafe_writes", unsafe)
            print(f"  saved {path}", file=sys.stderr)
            print(
                f"  baseline={_format_pct(unsafe.baseline_rate)}  "
                f"treatment={_format_pct(unsafe.treatment_rate)}",
                file=sys.stderr,
            )

        if args.metric in ("all", "duplicates"):
            print("→ eval_duplicate_prevention", file=sys.stderr)
            dups = await run_duplicate_prevention(store)
            path = _write_json("duplicates", dups)
            print(f"  saved {path}", file=sys.stderr)
            print(
                f"  prevention={_format_pct(dups.treatment_prevention_rate)}",
                file=sys.stderr,
            )

        if args.metric in ("all", "completion"):
            print("→ eval_task_completion", file=sys.stderr)
            completion = await run_completion(store)
            path = _write_json("completion", completion)
            print(f"  saved {path}", file=sys.stderr)
            print(
                f"  baseline={_format_pct(completion.baseline_rate)}  "
                f"treatment={_format_pct(completion.treatment_rate)}  "
                f"(+{completion.delta_pp:.1f}pp)",
                file=sys.stderr,
            )
    finally:
        await store.aclose()

    if args.output:
        report = _format_report(unsafe=unsafe, dups=dups, completion=completion)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
        print(f"\nReport: {out}", file=sys.stderr)
        print(report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lockrail eval runner — measure the three resume metrics."
    )
    parser.add_argument(
        "--metric",
        choices=["all", "unsafe_writes", "duplicates", "completion"],
        default="all",
        help="which metric to run (default: all)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="if set, also write a markdown report to this path",
    )
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
