"""Tool contracts — per-tool Pydantic schemas the EvidenceGate enforces."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolContract(BaseModel):
    """Binds a tool name to the Pydantic model its args must satisfy.

    Each tool gets its own contract instead of one mega-schema across all
    tools: a refund's required fields (ticket_id, amount, justification) are
    structurally unrelated to a knowledge-base lookup's (query), and forcing
    them into one model loses type safety and produces noisy validation
    errors. One contract per tool keeps the failure message scoped to the
    call the agent actually made.
    """
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool_name: str
    args_model: type[BaseModel]

    def validate_args(self, args: dict[str, Any]) -> BaseModel:
        """Validate args against the bound Pydantic model.

        Raises pydantic.ValidationError on failure — callers (EvidenceGate)
        translate that into a DENY.
        """
        return self.args_model.model_validate(args)


class ContractRegistry:
    """Tool-name → ToolContract lookup.

    Mutable on purpose: tools are registered at startup as the server wires
    them up. The registry is *not* a Pydantic model because it owns state
    (the dict) that the validation layer doesn't need to see.
    """

    def __init__(self, contracts: Iterable[ToolContract] | None = None) -> None:
        self._by_tool: dict[str, ToolContract] = {}
        if contracts:
            for contract in contracts:
                self.register(contract)

    def register(self, contract: ToolContract) -> None:
        """Register a contract. Raises if the tool already has one.

        Strict by design — silently overwriting a contract is the kind of
        misconfiguration that lets unsafe calls through. Callers who want
        replacement semantics can construct a fresh registry.
        """
        if contract.tool_name in self._by_tool:
            raise ValueError(
                f"tool '{contract.tool_name}' already has a registered contract"
            )
        self._by_tool[contract.tool_name] = contract

    def get(self, tool_name: str) -> ToolContract | None:
        return self._by_tool.get(tool_name)

    def __contains__(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name in self._by_tool

    def __len__(self) -> int:
        return len(self._by_tool)
