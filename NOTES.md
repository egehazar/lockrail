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

## Step 5 — Gate abstraction and runtime design

### Why a base `Gate` ABC with `_evaluate` + public `run`
Template method pattern. Every gate needs the same plumbing: time itself,
catch exceptions, build a `GateResult`. Forcing each gate to handle this
manually would mean inconsistent error semantics across gates — exactly
the bug Lockrail is supposed to prevent in the agent it wraps. The ABC
guarantees every gate produces a well-formed `GateResult` even when it
crashes.

### Why exceptions become DENY, not propagate
The runtime is the last line of defense. If a gate has a bug and throws,
the safe default is to block the action, not let an unrelated stack trace
crash the orchestrator and potentially fail-open. This is one of the
"design-level coordination failures" Lockrail prevents: a misbehaving
component should fail closed.

### Why halt-on-first-block instead of running all gates
- **Determinism**: "blocked by policy" maps to one specific gate, not a
  set of competing reasons. This makes the audit log unambiguous.
- **Performance**: gates that hit Redis or Postgres have latency; running
  all of them when the first denies is waste.
- **Counterpoint**: for forensic analysis you sometimes want to see
  *everything* that would have blocked. Solution for later: an `analyze`
  mode that runs every gate regardless. Not in MVP.

### Why the runtime takes an opaque `executor` callable
Lockrail is framework-agnostic. The runtime doesn't import MCP, FastAPI,
or LangGraph. It only knows `ToolCall in → TransactionResult out`. The
executor is a `Callable[[ToolCall], Awaitable[dict]]` injected by whoever
wires Lockrail into a real system. Same runtime serves:
- MCP middleware (executor = MCP client.call_tool)
- FastAPI tool routes (executor = call internal handler)
- Tests (executor = mock dict)

### Why `Runtime` doesn't write audit events yet
Audit persistence requires storage (Postgres). Step 5 is in-memory only;
gate decisions accumulate in `TransactionContext.gate_results` and end up
in the final `TransactionResult`. Step 6 adds the audit repository and
wires audit emission into the runtime as a side effect of each gate run.

## Step 6 — Storage layer design

### Why two tables, not one big audit log
- `transactions`: one row per submitted tool call. Summary state, the
  row you'd show in a dashboard. Indexed by actor, tool, fingerprint,
  status, start time.
- `audit_events`: many rows per transaction. Append-only event stream
  forming the replayable trace. Indexed by transaction_id + sequence_num
  (unique together).

A single denormalized log would mean every dashboard query joins through
a huge table. Splitting summary from stream gives us O(1) status lookup
and a separate optimized scan path for forensic replay.

### Why denormalize Actor fields onto TransactionRow
Storing actor as JSONB would make "transactions by agent X" a JSONB-path
query — slow even with GIN indexes, awkward for analytics. Promoting
agent_id, session_id, user_id, tenant_id to indexed columns costs 4
columns and buys us first-class query performance.

### Why `JSONB` instead of `JSON`
JSONB stores parsed binary form. Operations (key existence, containment,
indexing) are 10–100x faster on JSONB. There's a small write cost for
the parse, but our audit-log workload is write-once, read-many.

### Why `(transaction_id, sequence_num)` is a unique index
Sequence numbers guarantee total order within a transaction even when
clock skew makes timestamps tie. Making the index unique means replay
can never see two events claiming the same position — protects us from
double-emit bugs in upstream code.

### Why Alembic env.py reads `Settings.database_url`
If alembic.ini and the running app drift in their DB URL, migrations
land on the wrong DB. Reading from Settings means there's one source of
truth: change the `.env`, both move together.

### Why pool_size=10, max_overflow=20
For a tool-call middleware, request fan-out is modest — most calls are
serial within an agent run. 10 base + 20 overflow gives us 30 concurrent
DB connections, plenty for the eval workload (single-process, ~10
parallel scenarios max). In prod we'd tune based on load.

## Step 7 — Idempotency + audit persistence: first working slice

### Why the IdempotencyGate signals via metadata, not a new GateDecision
We could have added a `CACHE_HIT` decision to the enum, but that contaminates
the gate vocabulary. Every other decision is about *should this proceed* —
ALLOW/DENY/REQUIRE_APPROVAL/SKIP. Cache hit isn't a decision, it's a
side-channel signal saying "the answer already exists, runtime please use it."
Using `ctx.metadata` keeps the decision enum clean and makes the short-
circuit logic visible in one place (Runtime._finalize) instead of scattered.

### Why only EXECUTED results are cached
- BLOCKED: re-evaluate next time, the policy might have changed
- PENDING_APPROVAL: waiting on a human; caching means future calls skip
  the approval, defeating the point
- FAILED: transient errors should let the agent retry
- REPLAYED: already cached upstream

Only deterministic, successful executions go into the cache.

### Why audit write failures don't fail the transaction
The runtime's job is to make a safe decision and execute. If the audit
write fails (DB blip, network glitch), the transaction itself was still
correct — the side effect already happened or was correctly blocked. Failing
the transaction because of a logging issue would mean *more* unsafe writes,
not fewer. We log loudly and continue; in prod we'd alert on this metric.

### Why `_persist` uses its own session via session_factory
The runtime is invoked from many contexts (MCP middleware, FastAPI route,
test). Threading an `AsyncSession` through every layer would couple the
runtime to whatever HTTP framework is on top. The session_factory pattern
means: "give me something that produces a session when I need one." Each
transaction owns its session lifecycle.

### Where the 95% duplicate-prevention metric comes from
Webhook replay test: fire the same event N times, count distinct executor
invocations. With IdempotencyGate in place, the executor sees the first
event only; the other N-1 are REPLAYED. So duplicate prevention is
(N-1)/N. The 95% figure assumes a few legitimate retries that arrive
with subtle arg differences (e.g. different retry_id) — those bypass the
cache. We'll quantify this in the eval suite (Step 12).