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


def cancel_keys_for_ctx(chat_ctx: dict | None) -> list[str]:
    """Liste der Cancel-Schlüssel für einen Chat-Kontext.
    Owner-Mode: nur user_id (sender). Group-Mode: user_id + room:<id> / chan:<id>
    → jeder Teilnehmer kann den laufenden Task per /abort abbrechen."""
    if not chat_ctx:
        return []
    keys: list[str] = []
    uid = chat_ctx.get("user_id")
    if uid:
        keys.append(str(uid))
    if chat_ctx.get("group_mode"):
        rid = chat_ctx.get("room_id")
        cid = chat_ctx.get("channel_id")
        if rid:
            keys.append(f"room:{rid}")
        if cid:
            keys.append(f"chan:{cid}")
    return keys


def check_cancel_for_chat(chat_ctx: dict | None) -> str | None:
    """Prüft cancel-Flag für alle relevanten Keys aus chat_ctx (user + room/channel im Group-Mode)."""
    keys = cancel_keys_for_ctx(chat_ctx)
    with _lock:
        for k in keys:
            v = _flags.get(k)
            if v:
                return v
    return None


def clear_cancel_for_chat(chat_ctx: dict | None) -> None:
    """Räumt alle Cancel-Flags des Chat-Kontexts."""
    keys = cancel_keys_for_ctx(chat_ctx)
    with _lock:
        for k in keys:
            _flags.pop(k, None)
