"""Evidence gate — validates tool args against a registered Pydantic contract."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..models import GateResult, TransactionContext
from ..models.contracts import ContractRegistry
from .base import Gate


class EvidenceGate(Gate):
    """Enforce that a tool call carries the evidence its contract requires.

    Decision policy:
      - No contract registered for the tool → SKIP. The gate refuses to
        speak about tools it doesn't know about; an unknown tool is the
        policy gate's problem, not evidence's.
      - Args fail validation → DENY. A malformed call is a deterministic
        failure — no human approval can rescue it, and re-running with the
        same args will fail the same way. Sending it to an approval queue
        would just turn a fast feedback loop into a slow one.
      - Args validate → ALLOW, attaching the validated field names so the
        audit log records exactly which evidence the agent supplied.
    """
    name = "evidence"

    def __init__(self, registry: ContractRegistry) -> None:
        super().__init__()
        self.registry = registry

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        tool_name = ctx.tool_call.tool_name
        contract = self.registry.get(tool_name)
        if contract is None:
            return self.skip("no contract", tool_name=tool_name)

        try:
            validated = contract.validate_args(ctx.tool_call.args)
        except ValidationError as exc:
            errors = _serialize_errors(exc.errors(include_url=False))
            return self.deny(
                "args failed contract validation",
                tool_name=tool_name,
                error_count=len(errors),
                errors=errors,
            )

        validated_fields = sorted(validated.model_dump().keys())
        return self.allow(
            "args validated",
            tool_name=tool_name,
            validated_fields=validated_fields,
        )


def _serialize_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip pydantic error dicts to JSON-safe primitives.

    Audit events round-trip through Postgres JSONB; ValidationError entries
    carry raw `input` values and `ctx` objects that may not serialize. We
    keep the fields auditors actually care about: location, message, type.
    """
    out: list[dict[str, Any]] = []
    for err in errors:
        out.append({
            "loc": list(err.get("loc", [])),
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        })
    return out
