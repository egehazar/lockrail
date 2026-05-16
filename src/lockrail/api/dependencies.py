"""FastAPI dependencies — small helpers so route signatures stay clean."""
from __future__ import annotations

from fastapi import Request

from ..core import Runtime


def get_runtime(request: Request) -> Runtime:
    """The Runtime is created by the app factory and lives on app.state."""
    return request.app.state.runtime
