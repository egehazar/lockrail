"""Idempotency gate — checks Redis for prior execution of this fingerprint."""
from __future__ import annotations

from ..models import GateResult, MetadataKeys, TransactionContext
from ..storage import IdempotencyStore
from .base import Gate


class IdempotencyGate(Gate):
    """First-line gate. On cache hit, signals the Runtime via ctx.metadata
    to short-circuit execution and return the prior result as REPLAYED.

    Always returns ALLOW: idempotency is not a security control, it's a
    duplicate-suppression mechanism. The Runtime decides what to do with
    the signal.
    """
    name = "idempotency"

    def __init__(self, store: IdempotencyStore) -> None:
        super().__init__()
        self.store = store

    async def _evaluate(self, ctx: TransactionContext) -> GateResult:
        fingerprint = ctx.tool_call.fingerprint
        cached = await self.store.get(fingerprint)
        if cached is None:
            return self.allow("no prior execution", fingerprint=fingerprint[:12])

        ctx.metadata[MetadataKeys.IDEMPOTENCY_CACHE_HIT] = True
        ctx.metadata[MetadataKeys.IDEMPOTENCY_CACHED_OUTPUT] = cached.get("output")
        ctx.metadata[MetadataKeys.IDEMPOTENCY_PRIOR_TX_ID] = cached.get("transaction_id")
        return self.allow(
            "cache hit — runtime will replay prior result",
            fingerprint=fingerprint[:12],
            prior_transaction_id=cached.get("transaction_id"),
        )
