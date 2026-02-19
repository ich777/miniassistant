"""
Context Logger – schreibt den vollständigen System-Prompt, Messages-Kontext,
Token-Schätzung und verbleibende Tokens in $config_dir/logs/context.log.

Aktiviert durch server.show_context: true in der Config (default: false).
Format analog zu agent_actions.log mit Zeitstempel.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _log_path(config: dict[str, Any]) -> Path | None:
    """Gibt den Pfad zur context.log zurück, oder None wenn deaktiviert."""
    if not (config.get("server") or {}).get("show_context"):
        return None
    config_dir = config.get("_config_dir") or ""
    if not config_dir:
        from miniassistant.config import get_config_dir
        config_dir = get_config_dir()
    log_dir = Path(config_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "context.log"


def _write(path: Path, text: str) -> None:
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_context(
    config: dict[str, Any],
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list | None = None,
    num_ctx: int = 8192,
    think: bool | None = None,
) -> None:
    """Loggt den vollständigen Kontext vor dem Ollama-Call."""
    path = _log_path(config)
    if not path:
        return

    # Token-Schätzung (gleiche Methode wie chat_loop)
    def _est(text: str) -> int:
        return max(1, int(len(text) / 3.0))

    import json
    sys_tok = _est(system_prompt or "")
    msg_tok = sum(
        _est((m.get("role") or "") + (m.get("content") or "") + (m.get("thinking") or "")
             + (json.dumps(m.get("tool_calls"), ensure_ascii=False) if m.get("tool_calls") else ""))
        for m in messages
    )
    tools_json = json.dumps(tools or [], ensure_ascii=False)
    tools_tok = _est(tools_json)
    total_tok = sys_tok + msg_tok + tools_tok
    remaining = max(0, num_ctx - total_tok)

    # Messages-Zusammenfassung (Rollen + gekürzte Inhalte)
    msg_lines = []
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = (m.get("content") or "")[:200]
        if len(m.get("content") or "") > 200:
            content += "…"
        tc = m.get("tool_calls")
        tc_info = f"  [+{len(tc)} tool_calls]" if tc else ""
        msg_lines.append(f"  [{i}] {role}: {content}{tc_info}")

    lines = [
        f"\n---\n",
        f"[{_ts()}] CONTEXT  model={model}  num_ctx={num_ctx}  think={think}\n",
        f"[{_ts()}] TOKENS   system={sys_tok}  messages={msg_tok}  tools={tools_tok}  "
        f"total={total_tok}  remaining={remaining}  "
        f"(~{remaining} tokens for response/thinking)\n",
        f"[{_ts()}] SYSTEM PROMPT ({sys_tok} tokens, {len(system_prompt)} chars):\n",
        f"{system_prompt}\n",
        f"[{_ts()}] MESSAGES ({len(messages)} msgs, {msg_tok} tokens):\n",
    ]
    lines.extend(line + "\n" for line in msg_lines)
    if tools:
        lines.append(f"[{_ts()}] TOOLS ({len(tools)} tools, {tools_tok} tokens)\n")

    _write(path, "".join(lines))
