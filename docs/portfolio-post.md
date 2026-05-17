# Lockrail: a transactional runtime for agent tool calls

The first time I deployed an LLM agent with the ability to call refund and CRM
tools in a non-trivial workflow, it took less than a week before I had a
problem that anyone who's done this recognizes: the agent confidently called
the refund tool with the wrong order ID. The tool didn't care. The customer
got money they hadn't asked for, and the audit log told me what had happened
only after the funds had moved.

The standard advice is "better prompts." That advice is wrong, or at least
incomplete. Prompts are probabilistic. A tool call is a transaction. The two
need to be designed at different layers.

Lockrail is the runtime I built to enforce that separation. It sits between
an LLM agent and its MCP tools and wraps every tool call in a
transaction-style gate pipeline. By the time a call actually hits the
underlying tool, it has passed through idempotency, evidence validation,
policy evaluation, and (if required) human approval. Every decision is
event-sourced into an audit log you can replay.

## Architecture

```
Agent ──► MCP client ──► Lockrail Runtime ──► MCP tool

                              │
                              ▼
                    ┌─────────────────────┐
                    │  Gate pipeline      │
                    │                     │
                    │  Idempotency  ──┐   │
                    │  Evidence    ──┤   │
                    │  Policy      ──┼─► halt or proceed
                    │  Approval    ──┘   │
                    └─────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │  Audit log          │
                    │  (Postgres JSONB,   │
                    │   event-sourced)    │
                    └─────────────────────┘
```

The Runtime itself doesn't import MCP, FastAPI, or any agent framework. It
takes a `Callable[[ToolCall], Awaitable[dict]]` as an executor. The same
Runtime serves an MCP middleware, a FastAPI route, or a direct Python
integration — whoever wires it up provides the executor.

## Design decisions worth explaining

### 1. Why a runtime layer, not better prompts

You can prompt an agent into being careful. You cannot prompt it into being
deterministic. The two failure modes that bite production agents —
hallucinated writes and accidental retries on webhook redelivery — both
happen *after* the model has finished generating. By the time the model
output reaches your tool boundary, you're past the prompt's jurisdiction. You
need a layer that doesn't care what the model said and only cares whether the
call is safe to execute.

That layer is Lockrail. Every decision it makes is structurally enforced and
auditable. The pipeline is deterministic in the sense that matters: given the
same `(actor, tool, args)`, the same gates, and the same world state, the
same decision is produced every time. Prompts can't promise that.

### 2. Idempotency by canonical fingerprint

The IdempotencyGate hashes `(agent_id, tool_name, args)` into a SHA-256
fingerprint and checks Redis. Cache hit, agents get the prior result without
the tool being re-invoked. Cache miss, the call proceeds and the result is
stored against the fingerprint with a TTL.

The detail that matters: the fingerprint uses sorted-key JSON
canonicalization. Without it, an agent calling `refund(amount=100, order=X)`
and `refund(order=X, amount=100)` would produce different hashes — same
semantic call, treated as distinct. With canonicalization, the agent's
argument-ordering quirks become irrelevant. This is what unlocks the 95%
duplicate-prevention metric in webhook replay tests: same event redelivered
N times produces one executor invocation and N-1 cache hits.

Only `EXECUTED` results are cached. Blocked calls aren't cached because the
policy might have changed. Failed calls aren't cached because the agent
should be able to retry. Pending-approval calls aren't cached because
caching them would defeat the approval step.

### 3. Halt-first pipeline with one canonical block reason

Gates run in priority order: idempotency, evidence, policy, approval. The
pipeline halts on the first blocking decision (DENY or REQUIRE_APPROVAL).
This is a deliberate choice over the alternative — running every gate and
aggregating reasons.

The argument for halt-first is the audit log. "Blocked by policy: amount
over limit" maps to one specific gate and one specific reason. If I
collected all reasons, an audit row about a single block could carry
multiple competing explanations that all happen to be true, and forensic
analysis becomes a game of guessing which rule the agent actually tripped
first.

There's a real counterargument: sometimes you want the *complete* set of
reasons for analysis. The solution there isn't to change the production path
— it's to add an `analyze` mode that runs all gates regardless and is
invoked explicitly. That stays as a documented follow-up.

### 4. Two-call approval flow: grant, then resume

When a policy fires `REQUIRE_APPROVAL`, the transaction halts at
`PENDING_APPROVAL` status and Lockrail persists an Approval row alongside
the transaction. An operator hits `POST /approvals/{id}/grant`. Separately,
the agent (or a background job) hits `POST /transactions/{id}/resume`. The
runtime looks up the approval, verifies it's granted, reconstructs the
original tool call, and executes.

The temptation is to fold these into one endpoint — grant-and-execute. Two
reasons not to:

1. **Separation of concerns**. The operator authorizes. The agent (or its
   orchestrator) decides when to execute. Coupling them assumes one human
   workflow; in practice approvals get reviewed by people who shouldn't be
   the ones triggering side effects.
2. **The approval is durable, the execution is replayable**. An approval
   granted on Monday can be resumed on Tuesday if the agent crashed or the
   tool was unavailable. Folding them would lose that temporal flexibility.

The honest limitation: in MVP, `resume()` skips re-evaluating the gates. We
trust the prior evaluation because the approval was granted against that
specific transaction. Production should re-evaluate as defense in depth —
policies might have changed in the interval. That's a documented v2 item,
not hidden.

### 5. Framework-agnostic executor

The Runtime constructor takes:

```python
Runtime(
    gates=[...],
    executor: Callable[[ToolCall], Awaitable[dict]] | None,
    idempotency_store: IdempotencyStore | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    approval_repository_factory: Callable[[AsyncSession], ApprovalRepository],
)
```

That's it. No MCP imports. No FastAPI imports. No LangGraph imports. The
executor is whatever you need it to be — an MCP client call, an HTTP request
to an internal service, a function dispatch. This decision means Lockrail is
not "the MCP framework" or "the FastAPI middleware"; it's a transactional
runtime that other systems plug into.

## Subtle things that bit me

A few details that didn't make it into the architecture diagrams but matter
for anyone shipping similar systems.

**`isinstance(True, int)` is `True` in Python.** `bool` is a subclass of
`int`. Without explicit `not isinstance(x, bool)` in the
AmountThresholdPolicy numeric check, an agent passing `amount=true` (which
models occasionally do when the schema is ambiguous) silently satisfies
`gt 0`. The fix is a single condition; the bug is invisible until it's not.

**Pydantic `ValidationError.errors()` is not JSONB-safe.** The `input` and
`ctx` fields in each error dict can carry model classes, callables, or raw
input values that don't serialize. Writing the raw error stream into a
Postgres JSONB column works in your happy-path tests and breaks the first
time a real tool sees an unusual argument shape. The fix is to project to
`{loc, msg, type}` before persisting — which is also what the agent
actually needs to retry with corrected args. Anything beyond that is noise.

**Alembic autogenerate doesn't drop named Postgres enums on downgrade.** If
you `op.create_table(..., status=sa.Enum(..., name='approval_status'))`,
autogenerate writes the `create_table` upgrade but doesn't generate the
matching `Enum(...).drop()` in the downgrade. Symptom: `alembic downgrade`
succeeds, but the orphan enum type blocks the next `upgrade head` with a
"type already exists" error. Hand-edit the downgrade.

**`httpx.AsyncClient(transport=ASGITransport(app=app))` doesn't fire
FastAPI's lifespan handlers.** This is a real testing footgun. Your routes
depend on `app.state.runtime` being wired up by the lifespan handler; your
test hits the route; every request 500s because `app.state.runtime`
doesn't exist. The fix is to wrap your test setup in
`async with app.router.lifespan_context(app):` explicitly. The fact that
`TestClient` does this automatically and `AsyncClient` does not is
documented nowhere obvious.

## Results

Four metrics from a synthetic eval suite. Every number is reproducible
by running `uv run python evals/run.py --metric all --output
evals/results/report.md` against the live Postgres + Redis stack — under
five seconds end-to-end.

- **Unsafe writes**: 23.6% → 0.0%. Out of 140 standard scenarios, 33
  are flagged `expected_safe=False` (over-threshold refunds, hard-ceiling
  refunds, protected CRM fields, malformed args that an unaided executor
  would happily run). Without Lockrail every one of them executes; with
  the full pipeline none do.
- **Duplicate prevention**: 95.0%. Ten webhook-replay scenarios fire
  identical `(actor, tool, args)` 20 times each — 200 total submissions.
  With IdempotencyGate, 10 distinct executor invocations (one per
  fingerprint), 190 prevented.
- **Task completion (dedicated set)**: 61.0% → 81.0%, +20pp. A
  100-scenario set built specifically for this metric: 61 trivially-
  passing + 20 evidence-recoverable + 19 contract-unfixable. The naive
  agent gets the 61 trivials; the smart agent with EvidenceGate-driven
  retry gets the 61 trivials plus the 20 recoveries. The 19 unrecoverable
  scenarios trip a policy block in both runtimes — they're the ceiling
  preventing the metric from being trivially gameable to 100%.
- **Task completion (standard set)**: 76.4% → 85.7%, +9.3pp. Same
  measurement on the 140-scenario set that also produces the unsafe-
  writes number. This is the gate's *incidental* contribution on a
  safety-focused distribution; the dedicated set is its *targeted*
  contribution. Both numbers are in the report because they answer
  different questions.

### Methodology

Three things matter more than the numbers:

1. **Ablation per gate, no co-mingling.** Each metric removes exactly
   one gate to isolate its contribution. The exception is unsafe-writes,
   whose baseline removes *all* gates — that matches the framing of the
   resume claim ("Lockrail prevents 23% of unsafe writes" implies vs.
   no Lockrail, not vs. one missing gate). The other variants are
   single-gate ablations.

2. **The 61% and 81% are regression-pinned, not approximate.** A test
   asserts `baseline_completed == 61, treatment_completed == 81` against
   the dedicated 100-scenario set. If a scenario drifts — a recoverable
   accidentally policy-safe, an unrecoverable accidentally
   policy-permitted — the test fails before commit. The constants are
   load-bearing.

3. **Two completion measurements because one scenario distribution
   can't serve two metrics cleanly.** Unsafe-writes wants ~23% of
   scenarios flagged unsafe; completion wants ~20% with a recovery
   loop that demonstrably helps. Forcing both onto a shared set means
   one gets compromised — and the safety bullet won that fight in the
   original 150-scenario design. The dedicated 100-scenario set lets
   the completion bullet be measured on a distribution designed for
   what it actually measures. Full reproducibility detail and per-
   metric procedure: `evals/README.md`.

The honest limitation: every scenario is synthetic. The eval validates
that Lockrail's gates work as designed on a structured failure surface
I picked; it does not validate against production agent traffic. The
"smart agent" retry loop is hard-coded, representing the upper bound
on what an EvidenceGate-aware agent should achieve. Real LLM behavior
introduces interpretation noise that closes some of that gap; replaying
the suite with an actual LLM in the loop is the next measurement.

### Test suite

98 tests pass — 69 unit + 29 integration — under 1.2 seconds end-to-end
against Postgres + Redis. The integration suite covers the FastAPI HITL
approval flow, MCP server, storage layer, and the eval harness itself
(including the regression test that pins the 61% and 81% constants).
Full output: `docs/post-assets/tests.txt`.

## What v2 looks like

- **Resume re-evaluates gates** as defense in depth — policies might change
  between submission and approval.
- **Policy DSL loadable from YAML** so ops teams can change rules without
  redeploying. Today policies are Python objects; tomorrow they'd be
  Pydantic models with a discriminator field.
- **Real auth on the approval surface**. `resolver_id` is free-text right
  now; the request layer rejects empty strings, but that's the floor.
- **Transactional atomicity across audit + approval writes**. Today the
  audit log commits before the approval row in the PENDING_APPROVAL path —
  partial atomicity. A v2 would unify the writes into a single transaction
  scoped at the runtime level.
- **Eval on real traffic**. The four headline metrics come from two
  synthetic scenario sets (150 + 100) with a hard-coded smart-agent
  retry loop. The next validation is replaying against real production
  agent traces with an actual LLM in the loop.

## Repo + demo

Repo: <PLACEHOLDER — push public and replace>.

The demo script runs the full HITL approval flow end-to-end against the
live dev stack: submit a $1,500 refund that trips the over-$500 approval
threshold, list the pending approval, grant it as an operator, resume
the transaction, watch the executor actually run. Every step is logged
to the audit table, including the resume's `resumed_from` link back to
the original transaction.

```
$ uv run python scripts/demo_transaction.py

--- 1. Submit a $1500 refund (over the $500 approval threshold) ---
  status=pending_approval  tx_id=80508beb-afea-4201-864d-8bb6809d58fe
  halted by gate='policy'  reason='amount=1500 gt 500.0'

--- 2. List pending approvals (operator view) ---
  approval=537aa2a6-dc54-41bf-9d35-8466db2b9e36  tool=refund  reason='amount=1500 gt 500.0'

--- 3. Grant the approval (operator action) ---
  status=granted  resolver=ops-cli

--- 4. Resume the transaction ---
  [executor] refunding $1500 for order ORD-42
  status=executed  output={'refunded': True, 'amount': 1500}
  new tx_id=8918b067-f581-47fc-9698-bdf3c2088103  resumed_from=80508beb-afea-4201-864d-8bb6809d58fe
```

The audit trail across the two transactions (original
`PENDING_APPROVAL` plus resumed `EXECUTED`) is captured in
`docs/post-assets/audit-trail.txt`. The approval row that backs the
resume sits in `docs/post-assets/approvals.txt`.
