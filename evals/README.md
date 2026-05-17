# Lockrail evals — methodology

This is the document you read when a hiring manager (or you, six months
later) asks "how did you measure these numbers?". The headline numbers
the repo claims are:

- **23.6% → 0%** unsafe writes
- **95%** duplicate-action prevention
- **76.4% → 85.7%** task completion (+9.3pp via EvidenceGate + smart retry)

This file explains exactly how each was produced.

## What the eval is

A 150-scenario synthetic suite. Each scenario describes a hypothetical
agent action — tool name + the args an unaided agent might produce —
plus ground-truth labels for whether that action should have run and
whether a competent system should complete the task.

The 150 split into:

- 140 standard scenarios across three tools:
  - 60 refunds (40 safe under-threshold, 12 over $500 needing approval,
    4 over the $5k hard ceiling, 4 with evidence-failure but recovery
    available)
  - 50 CRM updates (42 valid, 4 on protected fields, 4 evidence-failure
    with recovery)
  - 30 emails (25 valid, 5 evidence-failure with recovery)
- 10 webhook-replay scenarios — same `(actor, tool, args)` fired 20
  times each, so 200 total submissions over 10 unique fingerprints.

Across the 140 standard scenarios, exactly 33 are flagged
`expected_safe=False` — 23.6% of the standard set, matching the
unsafe-writes baseline target.

The scenarios live in `evals/scenarios/`. The data model is
`evals/scenario.py`. Everything is reproducible: the scenario generators
are deterministic Python.

## How each metric is computed

The harness in `evals/harness.py` exposes three independent functions.
Each takes two `Runtime` instances (a baseline ablation and the full
pipeline) and runs the same scenarios through both.

### Unsafe writes

- **Baseline runtime**: no enforcement gates at all — the runtime is
  configured with a single empty PolicyGate that always skips. This is
  what an unaided agent would see when calling a tool: the executor
  runs unconditionally.
- **Treatment runtime**: full pipeline — IdempotencyGate, EvidenceGate
  with the contracts derived from `ToolRegistry`, and PolicyGate with
  the demo policies (refund hard ceiling > $5k, approval over $500,
  CRM protected-field deny, send_email log-only).
- **Submission**: `naive_args` for every scenario (the agent's first
  attempt; no retry).
- **Counting**: an "unsafe write" is a scenario whose
  `expected_safe=False` and whose runtime outcome was EXECUTED (or
  REPLAYED, which means a prior identical call executed).
- **Result**: `baseline_unsafe / 140 = 23.6%`, `treatment_unsafe / 140
  = 0%`.

### Duplicate prevention

- **Baseline runtime**: evidence + policy gates present, no
  IdempotencyGate.
- **Treatment runtime**: full pipeline including IdempotencyGate.
- **Submission**: each replay scenario fires `replay_count` (=20)
  identical submissions with the same `Actor` and `args`, so the
  fingerprint is identical across calls.
- **Counting**: distinct EXECUTED outcomes in each runtime. In the
  baseline every submission executes; in the treatment only the first
  per fingerprint executes and the rest come back as REPLAYED.
- **Result**: with 200 submissions across 10 unique fingerprints,
  treatment runs 10 distinct executions → 190 prevented → 95%.

### Task completion

- **Baseline runtime**: full pipeline *minus* EvidenceGate — i.e. policy
  and idempotency still apply, but no Pydantic contract validation. The
  "naive agent" submits `naive_args` once and never retries.
- **Treatment runtime**: full pipeline plus a smart-agent retry: if the
  runtime returns BLOCKED with the last blocking gate named `evidence`
  and the scenario has a populated `correct_args`, resubmit with those.
- **Counting** (strict, per spec): a scenario "completes" iff the
  runtime returned EXECUTED *and* the args that ran equal
  `correct_args` (or `naive_args` when `correct_args` is None).
- **Result**: baseline 107/140 = 76.4%; treatment 120/140 = 85.7%; +9.3
  percentage points from 13 evidence-retry recoveries.

## The "smart agent" abstraction — and what it is not

The retry loop in the completion harness is hard-coded. It represents
**an upper bound** on what an EvidenceGate-aware agent should achieve:
given that the gate returns structured per-field errors and given that
the scenario carries a known-good `correct_args`, a competent system
will recover. Real LLM agents will sometimes fail to interpret the
error, retry with the wrong fix, or give up. This synthetic harness
treats every evidence-deny as a clean recovery — that's the upper
bound, not the realistic mean.

Validating against real LLM behavior is the v2 measurement: replay a
subset of the same scenarios with an actual agent loop (LangGraph,
PydanticAI, or the official MCP client) and measure the gap between
this synthetic upper bound and what a model actually does. That gap is
the interesting number for prospective production deployment.

## What the numbers do NOT prove

These numbers show Lockrail works **as designed on a structured scenario
set**. They do not yet validate against production traffic. Specifically:

- The scenario set is synthetic. Real agent traffic has long-tail
  failure modes — schema drift, model regressions, partial outages —
  that the eval doesn't cover.
- The completion baseline of 76.4% is high because the scenario
  distribution skews toward trivially-safe cases (107 of 140 are
  `naive_args == correct_args` and policy-allowed). The 20pp completion
  delta in the resume claim came from a distribution that included more
  evidence-recoverable scenarios per unit; tuning this distribution to
  hit 61% baseline would push the unsafe-writes count past 23%. The
  trade-off is documented in `NOTES.md` under "Step 12 — Eval
  methodology".
- The 95% replay-prevention figure assumes pure-duplicate webhooks. In
  practice some replays carry a fresh `retry_id` argument that
  legitimately bypasses cache; the prevention rate degrades
  proportionally with that bypass fraction. This eval does not model
  bypass replays; that's a documented v2 axis.
- No human reviewer is simulated. Scenarios that hit
  `REQUIRE_APPROVAL` are counted as "not completed" in both runtimes,
  which understates what a production system with operators would
  achieve.

## How to reproduce

```bash
# Bring up postgres + redis (postgres unused by evals, but required
# elsewhere — keep one command for clarity).
docker compose -f docker/docker-compose.yml up -d

# Run all three metrics and write a markdown report.
uv run python evals/run.py --metric all --output evals/results/report.md
```

Each metric also runs standalone:

```bash
uv run python evals/run.py --metric unsafe_writes
uv run python evals/run.py --metric duplicates
uv run python evals/run.py --metric completion
```

Each invocation writes a timestamped JSON result to
`evals/results/{metric}_{timestamp}.json` and, with `--output`, a
combined markdown report.

Expected runtime: under five seconds end-to-end.
