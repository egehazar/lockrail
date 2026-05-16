"""Gate base class. Every gate in the pipeline subclasses this."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from ..models import GateDecision, GateResult, TransactionContext


class Gate(ABC):
    """Abstract gate in the Lockrail pipeline.

    Subclasses set `name` and override `_evaluate`. The public `run` method
    handles timing and exception capture — subclasses focus only on the
    decision logic. Exceptions thrown by `_evaluate` become DENY decisions
    rather than propagating: the runtime should never crash because a gate
    misbehaves.
    """

    name: str  # subclass must set

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "name", None):
            raise TypeError(f"{cls.__name__} must set a `name` class attribute")

    @abstractmethod
    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        """Return a GateResult. Use the helper methods below to construct it.
        The base class will overwrite `duration_ms` automatically.
        """
        ...

    async def run(self, ctx: TransactionContext) -> GateResult:
        """Execute the gate, capturing timing and any exception."""
        start = time.perf_counter()
        try:
            result = await self._evaluate(ctx)
        except Exception as exc:
            result = self.deny(
                f"gate raised {type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
        duration_ms = (time.perf_counter() - start) * 1000
        return result.model_copy(update={"duration_ms": duration_ms})

    # --- Helpers for subclasses ---

    def allow(self, reason: str, **details: Any) -> GateResult:
        return GateResult(
            gate_name=self.name,
            decision=GateDecision.ALLOW,
            reason=reason,
            details=details,
            duration_ms=0,
        )

    def deny(self, reason: str, **details: Any) -> GateResult:
        return GateResult(
            gate_name=self.name,
            decision=GateDecision.DENY,
            reason=reason,
            details=details,
            duration_ms=0,
        )

    def require_approval(self, reason: str, **details: Any) -> GateResult:
        return GateResult(
            gate_name=self.name,
            decision=GateDecision.REQUIRE_APPROVAL,
            reason=reason,
            details=details,
            duration_ms=0,
        )

    def skip(self, reason: str, **details: Any) -> GateResult:
        return GateResult(
            gate_name=self.name,
            decision=GateDecision.SKIP,
            reason=reason,
            details=details,
            duration_ms=0,
        )
