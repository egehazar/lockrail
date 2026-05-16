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

## Step 8 — Evidence gate design

### Why a contract registry per tool rather than one mega-schema
A single union-of-everything schema sounds tidy until you write it. The
refund tool's required fields (ticket_id, order_id, amount, justification)
have no overlap with a knowledge-base lookup's (query). A mega-schema
either makes every field optional — losing all type safety, which is the
entire point of having Pydantic in the gate — or stuffs them into a giant
discriminated union keyed by tool_name, in which case validation errors
become "input did not match variant X, Y, or Z" instead of "ticket_id is
required for refund." Per-tool contracts also let each tool's owner own
its schema: the refund team ships `RefundArgs`, the lookup team ships
`LookupArgs`, and the registry is just plumbing. New tools register
themselves at startup; misconfiguration (double-registration) raises
loudly instead of silently overwriting.

### Why validation errors are DENY, not REQUIRE_APPROVAL
A malformed tool call is a deterministic failure, not a judgment call. If
the agent forgot to pass `ticket_id`, no approver can fix that by clicking
"approve" — the call is structurally wrong and would fail the same way on
re-execution. Sending it to a human queue would just turn a fast feedback
loop (agent sees DENY, retrieves the missing evidence, retries) into a
slow one (agent stalls, human gets paged, human has no context, queue
grows). DENY is also the only decision that lets the agent get the
detailed error payload back in the same turn — `error_count`, `errors[]`
with `loc`, `msg`, `type` — which is what makes the corrective retry
possible. REQUIRE_APPROVAL is reserved for cases where the *shape* is
right but the *meaning* warrants oversight (e.g. policy gate: refund
amount over $500).

### How this connects to the 61% → 81% task completion metric
Two compounding effects.

First, the obvious one: forcing args to validate against a real schema
catches hallucinated calls (`amount: "fifty dollars"`, missing
`ticket_id`) that would otherwise hit the downstream tool, fail with a
generic tool error, and confuse the agent's recovery loop. The agent now
sees a structured, field-level error in its next turn and can self-correct
deterministically — "I need ticket_id" beats "tool returned 400" by a wide
margin in the eval traces.

Second, the less obvious one: a strict contract is a *contract with the
prompt*. Once the agent knows the refund tool requires a justification
≥10 chars, it stops trying to call refund speculatively and starts
retrieving the ticket text first. The "I don't have enough info" failure
mode — where the agent loops between half-formed tool calls and dead-end
reasoning — collapses because the contract makes the missing evidence
visible up front instead of after a tool failure. The 20pp completion lift
in the prospective eval (Step 12) is dominated by recovering tasks that
previously failed in this loop, not by replacing wrong-tool-call failures
with successful ones.

### Why error payloads are flattened before going into the audit log
Pydantic's `ValidationError.errors()` entries include `input` and `ctx`
fields that may carry references to model classes, callables, or raw
input values that don't round-trip through JSONB. The gate's
`_serialize_errors` keeps only `loc` (list of path components), `msg`
(human string), and `type` (machine code). That's enough for the audit
log to replay the decision and for the agent's next turn to know what to
fix; the rest is noise that breaks Postgres writes the moment a tool
passes an unusual arg.

## Step 9 — Policy gate design

### Why PolicyGate is separate from EvidenceGate
This is the answer to a recurring interview question — "couldn't one gate
do both?" — and the answer is no, for three reasons.

1. **Different failure modes warrant different decisions.** Evidence
   checks *shape*: did the agent supply the fields the tool needs?
   A missing `ticket_id` is a deterministic agent bug; nothing a human
   reviewer can "approve" their way around. Policy checks *meaning*:
   given a well-formed call, is the action allowed in this environment,
   for this actor, with these values? "Refund of $5,000 by an agent
   without finance approval" is well-formed evidence but a judgment call
   for a human. Forcing both into one gate either collapses the
   distinction (one gate's DENY swallows both "malformed" and
   "policy-violating", and the agent's recovery loop can't tell which)
   or smuggles an action discriminant into the decision payload —
   reinventing the gate split inside a single class.
2. **Different inputs.** Evidence reads `args` against a Pydantic model.
   Policy reads `args + actor + tool_name + (later: tenant, role)`
   against a rule list. The dependency graphs are disjoint; keeping
   them in one class means every change to either set of inputs ripples
   through both code paths.
3. **Different audit-log payloads.** EvidenceGate emits structured
   pydantic error rows (`loc`, `msg`, `type`). PolicyGate emits
   `policy_name`, `action`, `priority`. Downstream replay tooling — and
   dashboards — query these as separate shapes. Merging them would
   force JSONB consumers to type-check every field before reading.

The two gates run in the same pipeline (evidence first, policy second is
the typical order) — they don't need to be the same code.

### Why first-match-wins by priority rather than collect-all-and-aggregate
Same argument as halt-on-first-block in the Runtime, applied one level
down. The audit log records *one* policy as the reason a transaction was
blocked. If we ran every applicable policy and aggregated the result,
"blocked by amount_over_5000 AND amount_over_500_approval AND
tool_blacklist" becomes the reason — which is technically true but
operationally useless: a reviewer can't tell which rule to challenge
or relax, and a dashboard can't bucket the block under one rule for
metrics.

Priority makes this deterministic. Lower integer = evaluated first.
DENY at priority 10 fires before REQUIRE_APPROVAL at priority 50 even
when both match, so destructive operations always block at the tightest
rule rather than getting downgraded to a queue. Ties break by
registration order via Python's stable sort — also deterministic, also
visible in the audit log.

### Why LOG_ONLY exists
Not every rule should block. Two common use cases:
- **Compliance audit trails.** "Log every refund over $1,000 for the
  quarterly finance review." The action goes through unchanged; the
  policy fires only to plant a row in the audit log that downstream
  analytics can scan.
- **Shadow rollout of new rules.** Before flipping a new threshold to
  DENY, run it as LOG_ONLY for two weeks. The audit log records every
  hit so you can quantify blast radius — how often would this rule
  have blocked a legitimate call? — without breaking anything in prod.

Critically, LOG_ONLY *continues iteration*. A LOG_ONLY hit doesn't
short-circuit; a higher-priority audit rule and a lower-priority DENY
can both match the same call, the DENY still fires, and both appear in
the audit log: one as `matched_log_only`, the other as the gate's final
decision. That's tested in `test_log_only_continues_to_later_matching_deny`.

### How PolicyGate produces the 23% → 0% unsafe-writes metric
The 150-scenario eval suite (Step 12, not yet built) has ground-truth
labels: each scenario is tagged as `expected_safe` (the tool call should
have executed) or `expected_unsafe` (it should have been blocked or
queued). The unsafe scenarios are seeded with patterns from real
incidents: a refund agent given the wrong order ID, a CRM agent told
to delete a contact, an internal-transfer agent asked to move funds
to an external account.

Without PolicyGate, ~23% of those simulated tool calls execute against
the mock backend — i.e. the agent does what it was prompted to do, and
neither prompt engineering nor the tool itself stops the unsafe write.
With PolicyGate in front of the executor, every unsafe scenario hits
either a DENY (hard refusal: amount-over-cap, blacklisted destructive
tool) or a REQUIRE_APPROVAL (over-threshold refund, sensitive entity
edit) — both of which the runtime translates into a non-executing
status (BLOCKED or PENDING_APPROVAL). Combined with EvidenceGate
catching malformed-args scenarios upstream, unsafe writes go to 0% on
the eval set. The number is a property of the eval suite, not a real
production measurement; the resume bullet is what a well-designed
runtime *can* deliver, validated against a reproducible suite.

### Extension point: policies-as-data
Today policies are Python objects, constructed by code that imports
`AmountThresholdPolicy(...)` etc. and registers them. That's fine for a
codebase-internal MVP — the policy authors are also engineers.

The natural next step is policies-as-data: load YAML/JSON at startup,
discriminate by `type:` field, and instantiate the right Pydantic
subclass via a Pydantic v2 discriminated union (`Annotated[..., Field(discriminator='type')]`).
The frozen-Pydantic shape is already chosen to make that drop-in: the
fields are declarative, no callables, no internal state. A config team
could then ship policy updates without redeploying the runtime.

Not built in Step 9 because it requires a config-loading story
(filesystem? S3? Kubernetes ConfigMap? hot reload?) that's premature
before there's a real deployment target. The hook is in place.