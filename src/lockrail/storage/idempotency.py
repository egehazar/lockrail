"""Redis-backed idempotency cache.

Maps ToolCall.fingerprint → cached result payload, with TTL. A cache hit
means: an identical (agent, tool, args) call already executed within the
TTL window; return the prior result instead of executing again.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis_async

from ..models import TransactionResult, TransactionStatus


class IdempotencyStore:
    KEY_PREFIX = "lockrail:idem:"

    def __init__(self, client: redis_async.Redis, ttl_seconds: int) -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds

    def _key(self, fingerprint: str) -> str:
        return f"{self.KEY_PREFIX}{fingerprint}"

    async def get(self, fingerprint: str) -> dict[str, Any] | None:
        raw = await self.client.get(self._key(fingerprint))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, fingerprint: str, result: TransactionResult) -> None:
        """Cache only EXECUTED results. Failures and blocks are never cached
        — we want the agent to be able to retry after a transient failure
        and we don't want to "remember" a block that should be re-evaluated.
        """
        if result.status != TransactionStatus.EXECUTED:
            return
        payload = {
            "transaction_id": str(result.transaction_id),
            "tool_call_id": str(result.tool_call_id),
            "output": result.execution_output,
            "completed_at": result.completed_at.isoformat(),
        }
        await self.client.set(
            self._key(fingerprint),
            json.dumps(payload),
            ex=self.ttl_seconds,
        )

    async def delete(self, fingerprint: str) -> None:
        await self.client.delete(self._key(fingerprint))

    async def aclose(self) -> None:
        await self.client.aclose()


async def make_idempotency_store(redis_url: str, ttl_seconds: int) -> IdempotencyStore:
    client = redis_async.from_url(redis_url, decode_responses=True)
    return IdempotencyStore(client, ttl_seconds)
