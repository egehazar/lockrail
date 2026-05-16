"""Policy definitions — business-rule layer enforced by PolicyGate."""
from __future__ import annotations

import fnmatch
from abc import abstractmethod
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .tool_call import ToolCall


class PolicyAction(StrEnum):
    """What a matching policy decides to do.

    DENY: hard block; transaction never executes.
    REQUIRE_APPROVAL: pause for human review.
    LOG_ONLY: matched but doesn't block — for audit-only rules like
      "log every refund over $1k for analytics" that should record their
      hit without interrupting the flow.
    """
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    LOG_ONLY = "log_only"


ComparisonOperator = Literal["gt", "gte", "lt", "lte", "eq"]


class Policy(BaseModel):
    """Abstract policy: matches against a ToolCall, declares an action.

    Concrete subclasses override `matches`. Frozen because a policy is a
    declarative rule — mutating it mid-pipeline would mean the audit log
    cannot trust which version of the rule fired. Pydantic v2's
    ModelMetaclass inherits from ABCMeta, so `@abstractmethod` is
    enforced: instantiating the bare base raises TypeError.
    """
    model_config = ConfigDict(frozen=True)

    name: str
    applies_to: list[str]  # fnmatch patterns matched against tool_name
    action: PolicyAction
    priority: int  # lower value = evaluated first

    def applies(self, tool_name: str) -> bool:
        """True if any pattern in applies_to matches tool_name (case-sensitive fnmatch)."""
        return any(fnmatch.fnmatchcase(tool_name, pat) for pat in self.applies_to)

    @abstractmethod
    def matches(self, tool_call: ToolCall) -> tuple[bool, str]:
        """Return (matched, human-readable reason). Subclasses implement."""
        ...


class AmountThresholdPolicy(Policy):
    """Match when a numeric arg compares to a threshold under a chosen operator.

    Designed for refund/charge limits: "amount gt 500 → REQUIRE_APPROVAL".
    Skips (returns False) when the arg is missing or non-numeric — a malformed
    arg is the EvidenceGate's problem, not policy's, and conflating the two
    would let evidence-stage errors leak into policy reasons.
    """
    arg_name: str
    threshold: float
    operator: ComparisonOperator

    def matches(self, tool_call: ToolCall) -> tuple[bool, str]:
        if self.arg_name not in tool_call.args:
            return False, f"arg '{self.arg_name}' not present"
        value = tool_call.args[self.arg_name]
        # bool is a subclass of int — exclude it so True/False don't
        # accidentally satisfy numeric comparisons.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"arg '{self.arg_name}' is not numeric"

        op = self.operator
        if op == "gt":
            matched = value > self.threshold
        elif op == "gte":
            matched = value >= self.threshold
        elif op == "lt":
            matched = value < self.threshold
        elif op == "lte":
            matched = value <= self.threshold
        else:  # eq — Pydantic Literal guarantees one of the five
            matched = value == self.threshold

        if matched:
            return True, f"{self.arg_name}={value} {op} {self.threshold}"
        return False, f"{self.arg_name}={value} does not satisfy {op} {self.threshold}"


class ArgEqualsPolicy(Policy):
    """Match when args[arg_name] equals an expected value.

    Useful for whitelisting/blacklisting categorical fields, e.g.
    `currency == "USD"` or `account_type == "internal"`.
    """
    arg_name: str
    expected: Any

    def matches(self, tool_call: ToolCall) -> tuple[bool, str]:
        if self.arg_name not in tool_call.args:
            return False, f"arg '{self.arg_name}' not present"
        actual = tool_call.args[self.arg_name]
        if actual == self.expected:
            return True, f"{self.arg_name}={actual!r} equals expected"
        return False, f"{self.arg_name}={actual!r} != {self.expected!r}"


class ToolBlacklistPolicy(Policy):
    """Match whenever the tool is in scope — i.e. "this tool is always blocked."

    No predicate beyond the applies_to filter. Used to mark a tool universally
    unavailable in a given environment (e.g. `delete_customer` in staging).
    """

    def matches(self, tool_call: ToolCall) -> tuple[bool, str]:
        return True, f"tool '{tool_call.tool_name}' is on the blacklist"


class PolicyRegistry:
    """Holds policies and exposes priority-sorted lookup by tool name.

    Mutable on purpose: policies are registered at startup. Not a Pydantic
    model — it owns a list of heterogeneous Policy subclasses that the
    validation layer doesn't need to see.
    """

    def __init__(self, policies: Iterable[Policy] | None = None) -> None:
        self._policies: list[Policy] = []
        if policies:
            for p in policies:
                self.register(p)

    def register(self, policy: Policy) -> None:
        """Register a policy. Raises if a policy with the same name already exists.

        Strict by design — collisions on policy name mean the audit log
        cannot uniquely attribute a decision. Caller must rename or
        construct a fresh registry.
        """
        if any(p.name == policy.name for p in self._policies):
            raise ValueError(f"policy '{policy.name}' is already registered")
        self._policies.append(policy)

    def applicable_to(self, tool_name: str) -> list[Policy]:
        """Policies whose applies_to matches tool_name, sorted by priority ascending.

        Python's `sorted` is stable, so ties break by registration order —
        deterministic and visible in the audit log.
        """
        applicable = [p for p in self._policies if p.applies(tool_name)]
        return sorted(applicable, key=lambda p: p.priority)

    def __len__(self) -> int:
        return len(self._policies)
