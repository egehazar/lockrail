"""Exceptions raised by the Lockrail core runtime.

Subclassed from standard exception types so generic handlers
(LookupError → 404, ValueError → 400/409) still pick them up if the
runtime is called from frameworks that don't know about these classes.
"""
from __future__ import annotations


class ApprovalNotFoundError(LookupError):
    """The transaction does not exist or has no approval row attached."""


class ApprovalNotGrantedError(ValueError):
    """The approval row exists but is not in GRANTED status — cannot resume."""
