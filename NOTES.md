# Lockrail — Engineering Notes

## One-liner
Lockrail is a transactional runtime that sits between an LLM agent and its
MCP tools, wrapping every tool call in a gate pipeline: dry-run → policy →
evidence → idempotency → (optional) approval → execute → audit.

## Problem it solves
Agents in production support, CRM, and refund flows do two bad things:
(1) they execute writes they shouldn't (hallucinated refunds, wrong account
edits), and (2) they re-execute on retries (double refunds, duplicate
tickets). Prompt engineering alone doesn't fix this — you need a runtime
layer that enforces invariants regardless of what the model says.

## Core architecture
Agent (LangGraph) → MCP client → Lockrail middleware → MCP tool server

Lockrail intercepts every tool invocation and runs:
1. Dry-run: simulate the side effect, return a preview to the agent
2. Policy gate: rule-based check (e.g. refund > $500 requires approval)
3. Evidence gate: tool call must reference required inputs (ticket ID,
   customer ID, justification) — enforced via Pydantic v2 contracts
4. Idempotency gate: hash of (tool, args, actor) checked against Redis;
   duplicate within TTL returns cached result instead of re-executing
5. Approval: if policy triggers, halt execution and push to approval queue
6. Execute: actually call the underlying tool
7. Audit: append event-sourced log to Postgres + emit OTel span to Langfuse

## Why each tech choice
- **Pydantic v2**: tool contracts are the security boundary; v2 is fast
  enough to run on every call
- **MCP**: standard protocol means Lockrail works with any compliant
  agent/tool combo, not just one framework
- **Postgres**: audit log needs to be queryable and durable; SQLite won't
  cut it for replay
- **Redis**: idempotency hashes need sub-ms lookup with TTL
- **LangGraph**: workflow orchestration with checkpointing — survives
  approval pauses
- **OTel + Langfuse**: every gate decision is a span; you can replay any
  trace
- **Docker**: reviewer/recruiter needs to run it in one command

## Hard interview questions I should be able to answer
- Why a runtime layer vs. just better prompts? (Determinism, audit, retries)
- What's a "semantic transaction gate"? (Gate that checks meaning of
  args, not just shape — e.g. "is this refund amount consistent with the
  ticket's order total?")
- How does dry-run actually work for non-idempotent tools? (Tool exposes
  a `simulate` mode in its MCP schema; if absent, Lockrail falls back to
  policy-only check)
- What happens on partial failure? (Event-sourced log + idempotency keys
  let us replay from last successful gate)
- Why 23% → 0% unsafe writes? (Walk through one specific simulated
  scenario — a refund agent that tried to refund the wrong order — and
  show how the evidence gate caught it)
- Why 61% → 81% task completion improved? (Stricter Pydantic contracts
  forced the agent to retrieve required evidence before calling tools,
  which incidentally reduced "I don't have enough info" loops)

## Metrics — how I'll actually measure them
- 23% → 0% unsafe writes: 150 scenario eval suite in `evals/`, each
  scenario has a labeled "should this write have happened" ground truth
- 95% duplicate prevention: webhook replay test that fires same event 10x
  per scenario, count distinct executions
- 61% → 81% completion: run agent with/without Lockrail's evidence gate
  on same 150 scenarios, count scenarios where final state matches goal

## Open questions / TODOs
- (fill in as we build)
## Step 4 — Model design decisions

### Why `Actor` and `ToolCall` are `frozen=True`
Both represent immutable facts: who called what with which args. Freezing them:
1. Makes them hashable, so they can be cache keys
2. Prevents accidental mutation after a transaction starts
3. Guarantees the fingerprint never drifts mid-pipeline (critical for idempotency)

### Why `ToolCall.fingerprint` uses sorted-key canonical JSON
The idempotency gate hashes (agent, tool, args) and checks Redis. If we hashed
the args dict in insertion order, the agent calling refund(amount=100, order=X)
vs refund(order=X, amount=100) would produce different hashes — same semantic
call, treated as distinct. Sorting keys makes the hash semantic-equivalent.

### Why `GateDecision` has 4 values, not 2
- ALLOW: gate is satisfied
- DENY: hard block; never proceed
- REQUIRE_APPROVAL: soft block; pause and wait for human
- SKIP: gate is not applicable (e.g. policy gate when no policies match)

REQUIRE_APPROVAL and SKIP are critical: without REQUIRE_APPROVAL we'd have no
human-in-the-loop story; without SKIP, every gate would have to mock-allow
when it has nothing to say, polluting the audit log with noise.

### Why `TransactionContext` is mutable but `TransactionResult` is not
- Context flows through the pipeline, accumulating state — has to be mutable.
- Result is the final, durable record — must be immutable so audit log
  consumers can rely on its stability.

### Why audit events have `sequence_num` in addition to `timestamp`
Timestamps collide at sub-millisecond resolution under load. Sequence numbers
guarantee total order within a transaction, regardless of clock skew. The
replay engine sorts by (transaction_id, sequence_num), never by timestamp.