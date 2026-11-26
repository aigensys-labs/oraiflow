"""Privacy-safe correlation context shared by Agent Runtime async stages."""

from __future__ import annotations

from contextvars import ContextVar

_trace_id: ContextVar[str] = ContextVar("oraiflow_agent_trace_id", default="-")


def set_trace_id(value: str):
    """Set the trace identifier for the current asynchronous execution context."""
    return _trace_id.set(value)


def reset_trace_id(token) -> None:
    """Restore the previous trace identifier using its context token."""
    _trace_id.reset(token)


def current_trace_id() -> str:
    """Return the trace identifier associated with the current execution context."""
    return _trace_id.get()
