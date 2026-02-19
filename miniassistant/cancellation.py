"""
Cancellation support for chat processing.
Thread-safe flags keyed by user_id, checked between tool rounds.
Two levels: 'stop' (graceful) and 'abort' (immediate).
"""
from __future__ import annotations

import threading
from typing import Literal

_lock = threading.Lock()
# {user_id: "stop" | "abort"}
_flags: dict[str, str] = {}


def request_cancel(user_id: str, level: Literal["stop", "abort"] = "stop") -> None:
    """Set cancellation flag for a user. Thread-safe."""
    with _lock:
        _flags[user_id] = level


def check_cancel(user_id: str) -> str | None:
    """Check if cancellation was requested. Returns 'stop', 'abort', or None."""
    with _lock:
        return _flags.get(user_id)


def clear_cancel(user_id: str) -> None:
    """Clear cancellation flag after processing is done."""
    with _lock:
        _flags.pop(user_id, None)
