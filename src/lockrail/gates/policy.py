"""Policy gate — applies business rules to a ToolCall."""
from __future__ import annotations

from typing import Any

from ..models import GateResult, TransactionContext
from ..models.policy import PolicyAction, PolicyRegistry
from .base import Gate


class PolicyGate(Gate):
    """Evaluate the registered policies against a tool call.

    Walks applicable policies in priority order (lowest int first):
      - No applicable policies → SKIP. The gate has nothing to say about
        this tool, so don't pollute the audit log with a no-op ALLOW.
      - First DENY match → return DENY immediately (halt pipeline).
      - First REQUIRE_APPROVAL match → return REQUIRE_APPROVAL immediately.
      - LOG_ONLY match → record and keep walking; later policies can still
        decide. This is what lets "audit-only" rules co-exist with blocking
        ones at the same scope.
      - End of walk: if any LOG_ONLY matched → ALLOW with `matched_log_only`
        carrying the per-policy reasons. Otherwise ALLOW with "no policies
        matched."

    Sibling-of-EvidenceGate, by design. Evidence checks *shape*; policy
    checks *meaning*. Keeping them separate makes "blocked by evidence" and
    "blocked by policy" two distinct rows in the audit log with structurally
    different reason payloads — which is what interview questions about
    Lockrail's audit trail actually want to see.
    """
    name = "policy"

    def __init__(self, registry: PolicyRegistry) -> None:
        super().__init__()
        self.registry = registry

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        tool_name = ctx.tool_call.tool_name
        applicable = self.registry.applicable_to(tool_name)
        if not applicable:
            return self.skip("no policies apply to this tool", tool_name=tool_name)

        log_only_matches: list[dict[str, Any]] = []
        for policy in applicable:
            matched, reason = policy.matches(ctx.tool_call)
            if not matched:
                continue

            if policy.action == PolicyAction.DENY:
                return self.deny(
                    reason,
                    policy=policy.name,
                    action=PolicyAction.DENY.value,
                    priority=policy.priority,
                    tool_name=tool_name,
                )
            if policy.action == PolicyAction.REQUIRE_APPROVAL:
                return self.require_approval(
                    reason,
                    policy=policy.name,
                    action=PolicyAction.REQUIRE_APPROVAL.value,
                    priority=policy.priority,
                    tool_name=tool_name,
                )
            # LOG_ONLY — record and continue.
            log_only_matches.append({
                "policy": policy.name,
                "reason": reason,
                "priority": policy.priority,
            })

        if log_only_matches:
            return self.allow(
                "log-only policies matched",
                tool_name=tool_name,
                matched_log_only=log_only_matches,
            )
        return self.allow("no policies matched", tool_name=tool_name)
