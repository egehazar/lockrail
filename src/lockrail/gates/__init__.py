"""Lockrail gates — modular decision points in the transaction pipeline."""
from .base import Gate
from .idempotency import IdempotencyGate

__all__ = ["Gate", "IdempotencyGate"]
