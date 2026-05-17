"""Scenario data model — the unit of measurement for Lockrail's eval suite.

A `Scenario` is what a hypothetical agent task looks like after we strip
away the model and the prompt: just the tool name and the args the agent
ended up generating, plus a ground-truth label for whether that action
should have run and whether a competent system would complete the task.

Two args fields, not one:
  - `naive_args` is what an unaided agent might produce. May be
    semantically wrong, malformed, or unsafe.
  - `correct_args` is what a competent system reaches after Lockrail's
    EvidenceGate denial walks the agent back to the right shape. `None`
    when `naive_args` are already correct.

The two ground-truth flags are independent:
  - `expected_safe`: should the *naive* submission be allowed to execute?
    False covers both "policy says no" (over-threshold refunds, protected
    fields) and "shape says no" (malformed args that would cause an
    unsafe write if the executor blindly accepted them).
  - `expected_complete`: should a competent system complete this task at
    all? False for hard-deny scenarios (over $5k refund) and for
    approval-required scenarios in the MVP eval (we don't simulate
    operator approval grants here, so PENDING_APPROVAL is treated as
    "not autonomously completed").
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ScenarioCategory = Literal["refund", "crm", "email", "replay"]


class Scenario(BaseModel):
    """One eval scenario. Frozen for the same reason every other Lockrail
    declarative thing is frozen: it's a reproducible fact about a test
    case, and silently mutating it would mean the report numbers can't
    be reproduced by re-reading the scenario file.
    """
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    category: ScenarioCategory
    tool_name: str
    naive_args: dict[str, Any]
    correct_args: dict[str, Any] | None = None
    expected_safe: bool
    expected_complete: bool
    replay_count: int = 1
    tags: list[str] = Field(default_factory=list)

    @property
    def args_to_submit(self) -> dict[str, Any]:
        """The args we hand to the runtime in non-completion evals.

        Falls back to `naive_args` when `correct_args` is missing — i.e.
        for scenarios where the naive attempt is already correct (the
        40+42+25 trivial-safe and the 12+4+4 policy-blocked scenarios).
        """
        return self.correct_args if self.correct_args is not None else self.naive_args
