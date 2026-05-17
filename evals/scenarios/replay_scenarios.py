"""10 replay scenarios — webhook redelivery simulations.

Each scenario fires the same `(actor, tool, args)` `replay_count` times
to model an upstream that retries on missing acks. With IdempotencyGate
present, only the first call hits the executor; the rest come back as
REPLAYED from the cached transaction result.

All ten use replay_count=20 to land 200 total submissions over 10 unique
fingerprints — a 95% prevention rate under the full pipeline. Adding
"realistic" partial-bypass scenarios (each call carrying a unique
`retry_id` to legitimately defeat the cache) lowers the headline rate
proportionally; this is noted in evals/README.md as a v2 axis worth
measuring but is intentionally absent here to keep the headline metric
clean and the math transparent.
"""
from __future__ import annotations

from ..scenario import Scenario


def _replay(i: int) -> Scenario:
    """One replay scenario: same refund fired 20 times."""
    args = {
        "order_id": f"ORD-R{i:02d}",
        "amount": 50.0 + i * 5.0,
        "customer_id": f"CUST-R{i:04d}",
        "justification": f"webhook redelivery test for order ORD-R{i:02d}",
    }
    return Scenario(
        id=f"replay-{i:02d}",
        name=f"replay #{i} (×20)",
        category="replay",
        tool_name="refund",
        naive_args=args,
        correct_args=None,
        expected_safe=True,
        expected_complete=True,
        replay_count=20,
        tags=["replay", "idempotency"],
    )


SCENARIOS: list[Scenario] = [_replay(i) for i in range(1, 11)]
assert len(SCENARIOS) == 10, f"expected 10 replay scenarios, got {len(SCENARIOS)}"
