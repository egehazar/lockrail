"""Manual demo: submit a refund that triggers approval, grant it, resume it.

Walks through the full HITL flow end-to-end against the live dev DB and Redis.
"""
import asyncio

from lockrail.config import get_settings
from lockrail.core import Runtime
from lockrail.gates import PolicyGate
from lockrail.models import (
    Actor,
    AmountThresholdPolicy,
    PolicyAction,
    PolicyRegistry,
    ToolCall,
)
from lockrail.storage import (
    ApprovalRepository,
    get_session_factory,
    make_idempotency_store,
)


async def main() -> None:
    settings = get_settings()
    store = await make_idempotency_store(settings.redis_url, ttl_seconds=60)
    factory = get_session_factory()

    async def executor(tc: ToolCall) -> dict:
        print(f"  [executor] refunding ${tc.args['amount']} for order {tc.args['order_id']}")
        return {"refunded": True, "amount": tc.args["amount"]}

    approval_over_500 = AmountThresholdPolicy(
        name="refund_over_500_needs_approval",
        applies_to=["refund"],
        action=PolicyAction.REQUIRE_APPROVAL,
        priority=50,
        arg_name="amount",
        threshold=500.0,
        operator="gt",
    )
    runtime = Runtime(
        gates=[PolicyGate(PolicyRegistry(policies=[approval_over_500]))],
        executor=executor,
        idempotency_store=store,
        session_factory=factory,
    )

    actor = Actor(agent_id="demo-agent", session_id="demo-sess")

    print("\n--- 1. Submit a $1500 refund (over the $500 approval threshold) ---")
    call = ToolCall(
        actor=actor,
        tool_name="refund",
        args={"order_id": "ORD-42", "amount": 1500},
    )
    submitted = await runtime.submit(call)
    print(f"  status={submitted.status.value}  tx_id={submitted.transaction_id}")
    if submitted.gate_results:
        last = submitted.gate_results[-1]
        print(f"  halted by gate={last.gate_name!r}  reason={last.reason!r}")

    print("\n--- 2. List pending approvals (operator view) ---")
    async with factory() as session:
        pending = await ApprovalRepository(session).list_pending()
    for a in pending:
        print(f"  approval={a.id}  tool={a.tool_name}  reason={a.requested_reason!r}")

    print("\n--- 3. Grant the approval (operator action) ---")
    target = next(a for a in pending if a.transaction_id == submitted.transaction_id)
    async with factory() as session:
        granted = await ApprovalRepository(session).grant(
            target.id, resolver_id="ops-cli", notes="verified ticket #ORD-42",
        )
    assert granted is not None
    print(f"  status={granted.status.value}  resolver={granted.resolver_id}")

    print("\n--- 4. Resume the transaction ---")
    resumed = await runtime.resume(submitted.transaction_id)
    print(f"  status={resumed.status.value}  output={resumed.execution_output}")
    print(f"  new tx_id={resumed.transaction_id}  resumed_from={submitted.transaction_id}")

    print("\n--- 5. Audit trail across both transactions ---")
    print("Inspect with psql:")
    print('  docker exec -it lockrail-postgres psql -U lockrail -d lockrail -c "\\')
    print(f"   SELECT event_type, sequence_num, payload->>'resumed_from' as resumed_from")
    print(f"   FROM audit_events")
    print(f"   WHERE transaction_id IN ('{submitted.transaction_id}', '{resumed.transaction_id}')")
    print(f'   ORDER BY transaction_id, sequence_num"')
    print()
    print("And the approval row:")
    print('  docker exec -it lockrail-postgres psql -U lockrail -d lockrail -c "\\')
    print(f"   SELECT id, status, resolver_id, notes")
    print(f"   FROM approvals WHERE transaction_id = '{submitted.transaction_id}'\"")

    await store.aclose()


if __name__ == "__main__":
    asyncio.run(main())
