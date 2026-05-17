"""Scenario aggregations — standard (140) and replay (10), totalling 150."""
from ..scenario import Scenario
from . import crm_scenarios, email_scenarios, refund_scenarios, replay_scenarios

STANDARD_SCENARIOS: list[Scenario] = [
    *refund_scenarios.SCENARIOS,
    *crm_scenarios.SCENARIOS,
    *email_scenarios.SCENARIOS,
]
REPLAY_SCENARIOS: list[Scenario] = replay_scenarios.SCENARIOS
ALL_SCENARIOS: list[Scenario] = [*STANDARD_SCENARIOS, *REPLAY_SCENARIOS]

assert len(STANDARD_SCENARIOS) == 140, (
    f"expected 140 standard scenarios, got {len(STANDARD_SCENARIOS)}"
)
assert len(REPLAY_SCENARIOS) == 10, (
    f"expected 10 replay scenarios, got {len(REPLAY_SCENARIOS)}"
)
_unsafe_count = sum(1 for s in STANDARD_SCENARIOS if not s.expected_safe)
assert _unsafe_count == 33, (
    f"expected 33 expected_safe=False standard scenarios, got {_unsafe_count}"
)
