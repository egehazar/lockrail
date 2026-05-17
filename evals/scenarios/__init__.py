"""Scenario aggregations.

Three independent sets:
  - STANDARD_SCENARIOS  (140): drives unsafe-writes + completion-standard
  - REPLAY_SCENARIOS    ( 10): drives duplicate-prevention
  - COMPLETION_SCENARIOS(100): drives completion-focused (dedicated set)

COMPLETION_SCENARIOS is intentionally NOT folded into STANDARD_SCENARIOS
or ALL_SCENARIOS — it's a separate measurement context. See
`evals/README.md` and `NOTES.md` Step 12 addendum for why.
"""
from ..scenario import Scenario
from . import (
    completion_scenarios,
    crm_scenarios,
    email_scenarios,
    refund_scenarios,
    replay_scenarios,
)

STANDARD_SCENARIOS: list[Scenario] = [
    *refund_scenarios.SCENARIOS,
    *crm_scenarios.SCENARIOS,
    *email_scenarios.SCENARIOS,
]
REPLAY_SCENARIOS: list[Scenario] = replay_scenarios.SCENARIOS
COMPLETION_SCENARIOS: list[Scenario] = completion_scenarios.SCENARIOS
ALL_SCENARIOS: list[Scenario] = [*STANDARD_SCENARIOS, *REPLAY_SCENARIOS]

assert len(STANDARD_SCENARIOS) == 140, (
    f"expected 140 standard scenarios, got {len(STANDARD_SCENARIOS)}"
)
assert len(REPLAY_SCENARIOS) == 10, (
    f"expected 10 replay scenarios, got {len(REPLAY_SCENARIOS)}"
)
assert len(COMPLETION_SCENARIOS) == 100, (
    f"expected 100 completion scenarios, got {len(COMPLETION_SCENARIOS)}"
)
_unsafe_count = sum(1 for s in STANDARD_SCENARIOS if not s.expected_safe)
assert _unsafe_count == 33, (
    f"expected 33 expected_safe=False standard scenarios, got {_unsafe_count}"
)
