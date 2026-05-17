"""Tool registry — one Pydantic model drives both MCP inputSchema and EvidenceGate.

A registered tool's `args_model` is the single source of truth that:
  - emits the JSON Schema advertised to the MCP client (`list_tools`)
  - emits the ToolContract that EvidenceGate validates against on every call

Keeping these in one place means the protocol-level schema and the runtime
validation schema cannot drift apart. Drift between them would itself be the
bug we're trying to prevent — an agent could send args that satisfy the
protocol contract but fail Pydantic (or vice versa), and the carefully
formatted EvidenceGate error would never reach the agent because the
protocol layer would have already rejected it.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as mcp_types
from pydantic import BaseModel, ConfigDict

from ..models.contracts import ContractRegistry, ToolContract

ToolFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolDefinition(BaseModel):
    """Binds a tool name to its Pydantic args model and its executor.

    Frozen for the same reason ToolContract is: a tool definition is a
    declarative fact registered at startup, and silently mutating it
    mid-run would mean the audit log can't trust which version of the
    contract the agent actually called against.
    """
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str
    args_model: type[BaseModel]
    executor: ToolFn


class ToolRegistry:
    """Mutable name → ToolDefinition lookup.

    Plain class, mirroring ContractRegistry and PolicyRegistry: it owns
    a heterogeneous dict that the validation layer doesn't need to see,
    so a Pydantic model would be the wrong shape.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool. Strict: duplicate names raise.

        Silently overwriting a registration is exactly the kind of
        misconfiguration that lets the wrong code run with the right
        contract attached. Better to fail loudly at startup than
        quietly at decision time.
        """
        if tool.name in self._by_name:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._by_name[tool.name] = tool

    def get_executor(self, name: str) -> ToolFn | None:
        tool = self._by_name.get(name)
        return tool.executor if tool is not None else None

    def as_mcp_tools(self) -> list[mcp_types.Tool]:
        """Project registered tools to MCP protocol `Tool` objects."""
        return [
            mcp_types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.args_model.model_json_schema(),
            )
            for t in self._by_name.values()
        ]

    def to_contract_registry(self) -> ContractRegistry:
        """Build a ContractRegistry from the same args_models.

        EvidenceGate now validates against the exact same Pydantic models
        that produced the MCP inputSchema. Drift is impossible by
        construction.
        """
        return ContractRegistry(contracts=[
            ToolContract(tool_name=t.name, args_model=t.args_model)
            for t in self._by_name.values()
        ])

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)
