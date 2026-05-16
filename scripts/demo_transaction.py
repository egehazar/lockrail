"""Manual demo: submit a tool call twice, observe deduplication + audit log."""
import asyncio

from lockrail.config import get_settings
from lockrail.core import Runtime
from lockrail.gates import Gate, IdempotencyGate
from lockrail.models import Actor, GateResult, ToolCall, TransactionContext
from lockrail.storage import get_session_factory, make_idempotency_store


class AlwaysAllow(Gate):
    name = "always_allow"

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        return self.allow("demo gate")


async def main() -> None:
    settings = get_settings()
    store = await make_idempotency_store(settings.redis_url, ttl_seconds=60)

    async def executor(tc: ToolCall) -> dict:
        print(f"  [executor] running real refund for order {tc.args.get('order_id')}")
        return {"refunded": True, "amount": tc.args["amount"]}

    runtime = Runtime(
        gates=[IdempotencyGate(store), AlwaysAllow()],
        executor=executor,
        idempotency_store=store,
        session_factory=get_session_factory(),
    )

    actor = Actor(agent_id="demo-agent", session_id="demo-sess")
    call_args = {"order_id": "ORD-42", "amount": 99}

    print("\n--- First call ---")
    r1 = await runtime.submit(ToolCall(actor=actor, tool_name="refund", args=call_args))
    print(f"  status={r1.status.value}  output={r1.execution_output}")

    print("\n--- Second call (identical args) ---")
    r2 = await runtime.submit(ToolCall(actor=actor, tool_name="refund", args=call_args))
    print(f"  status={r2.status.value}  output={r2.execution_output}")

    print(f"\nTransaction IDs:\n  first  = {r1.transaction_id}\n  second = {r2.transaction_id}")
    print("\nInspect with psql:")
    print('  docker exec -it lockrail-postgres psql -U lockrail -d lockrail \\')
    print('    -c "SELECT transaction_id, tool_name, status FROM transactions ORDER BY started_at"')

    await store.aclose()


if __name__ == "__main__":
    asyncio.run(main())
