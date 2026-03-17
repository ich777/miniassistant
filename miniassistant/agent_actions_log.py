"""
Agent Actions Logger – schreibt Prompt, Thinking, Tool-Calls und Ergebnisse
in $config_dir/logs/agent_actions.log (wenn server.log_agent_actions aktiviert).

Jeder Eintrag wird durch eine Trennlinie (---) abgegrenzt.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _log_path(config: dict[str, Any]) -> Path | None:
    """Gibt den Pfad zur agent_actions.log zurück, oder None wenn deaktiviert."""
    if not (config.get("server") or {}).get("log_agent_actions"):
        return None
    config_dir = config.get("_config_dir") or ""
    if not config_dir:
        from miniassistant.config import get_config_dir
        config_dir = get_config_dir()
    log_dir = Path(config_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "agent_actions.log"


def _write(path: Path, text: str) -> None:
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_prompt(config: dict[str, Any], model: str, user_content: str, system_prompt_len: int, messages_count: int) -> None:
    """Loggt den User-Prompt und Kontext-Info."""
    path = _log_path(config)
    if not path:
        return
    lines = [
        f"\n---\n",
        f"[{_ts()}] PROMPT  model={model}  system_chars={system_prompt_len}  history={messages_count} msgs\n",
        f"User: {user_content}\n",
    ]
    _write(path, "".join(lines))


def log_thinking(config: dict[str, Any], thinking: str) -> None:
    """Loggt das Thinking der KI."""
    path = _log_path(config)
    if not path or not thinking:
        return
    # Kürzen auf max 2000 Zeichen
    t = thinking if len(thinking) <= 2000 else thinking[:2000] + "…"
    _write(path, f"[{_ts()}] THINKING\n{t}\n")


def extract_tps(response: dict[str, Any], elapsed_s: float, content: str = "", thinking: str = "") -> tuple[float, bool] | None:
    """Berechnet Tokens/Sekunde aus der API-Antwort.
    Gibt (tps, exact) zurück:
      exact=True  → Ollama lokal: eval_count / (eval_duration / 1e9)
      exact=False → Näherung: token_count / elapsed_s  oder  chars/4 / elapsed_s

    thinking: optionaler Thinking-Text — wird in die Schätzung einbezogen, da
    elapsed_s die gesamte Denkzeit enthält und ohne Thinking-Token die t/s-Zahl
    stark unterschätzt würde.
    """
    # Ollama lokal: eval_count (Tokens) und eval_duration (Nanosekunden) — exakt
    # eval_count enthält alle generierten Token inkl. Thinking → korrekte Hardwarerate
    eval_count = response.get("eval_count")
    eval_duration = response.get("eval_duration")
    if eval_count and eval_duration and eval_duration > 0:
        return (eval_count / (eval_duration / 1e9), True)
    # Andere Provider: token count aus usage + Wall-Clock-Zeit — Näherung
    if elapsed_s and elapsed_s > 0:
        usage = response.get("usage") or {}
        tokens = (
            usage.get("completion_tokens")   # OpenAI, vLLM, DeepSeek
            or usage.get("output_tokens")    # Anthropic
            or usage.get("candidatesTokenCount")  # Google
        )
        if tokens and tokens > 0:
            return (tokens / elapsed_s, False)
        # Letzter Fallback: Content + Thinking-Länge schätzen (~4 chars/token)
        # Thinking-Token mitzählen, da elapsed_s auch die Denkzeit enthält
        total_chars = len(content) + len(thinking)
        if total_chars > 0:
            estimated = total_chars / 4
            return (estimated / elapsed_s, False)
    return None


def log_response(config: dict[str, Any], content: str, tps: tuple[float, bool] | None = None) -> None:
    """Loggt die Antwort der KI."""
    path = _log_path(config)
    if not path or not content:
        return
    c = content if len(content) <= 3000 else content[:3000] + "…"
    if tps is not None:
        value, exact = tps
        tps_str = f"  ({value:.1f} t/s)" if exact else f"  (~{value:.1f} t/s)"
    else:
        tps_str = ""
    _write(path, f"[{_ts()}] RESPONSE{tps_str}\n{c}\n")


def log_tool_call(config: dict[str, Any], tool_name: str, arguments: dict[str, Any], result: str) -> None:
    """Loggt einen Tool-Call (exec, web_search, schedule, etc.)."""
    path = _log_path(config)
    if not path:
        return
    import json
    args_str = json.dumps(arguments, ensure_ascii=False, indent=None)
    if len(args_str) > 1000:
        args_str = args_str[:1000] + "…"
    res_str = result if len(result) <= 1000 else result[:1000] + "…"
    lines = [
        f"[{_ts()}] TOOL  {tool_name}\n",
        f"  args: {args_str}\n",
        f"  result: {res_str}\n",
    ]
    _write(path, "".join(lines))


def log_image_received(config: dict[str, Any], num_images: int, mime_types: list[str], vision_model: str = "") -> None:
    """Loggt den Empfang von Bildern und ggf. den Vision-Modell-Wechsel."""
    path = _log_path(config)
    if not path:
        return
    mimes = ", ".join(mime_types) if mime_types else "?"
    lines = [f"[{_ts()}] IMAGE_RECEIVED  count={num_images}  mime={mimes}\n"]
    if vision_model:
        lines.append(f"  vision_model: {vision_model}\n")
    _write(path, "".join(lines))


def log_debate_start(config: dict[str, Any], topic: str, perspective_a: str, perspective_b: str, model_a: str, model_b: str, rounds: int) -> None:
    """Loggt den Start einer Debatte."""
    path = _log_path(config)
    if not path:
        return
    _write(path, (
        f"\n---\n[{_ts()}] DEBATE_START  topic={topic[:200]}\n"
        f"  A: {perspective_a[:100]} (model={model_a})\n"
        f"  B: {perspective_b[:100]} (model={model_b})\n"
        f"  rounds={rounds}\n"
    ))


def log_debate_round(config: dict[str, Any], round_num: int, side: str, model: str, argument: str) -> None:
    """Loggt eine einzelne Debattenrunde (Seite A oder B)."""
    path = _log_path(config)
    if not path:
        return
    arg = argument if len(argument) <= 1500 else argument[:1500] + "…"
    _write(path, f"[{_ts()}] DEBATE_ROUND {round_num} side={side} model={model}\n  {arg}\n")


def log_debate_end(config: dict[str, Any], topic: str, rounds_completed: int, file_path: str) -> None:
    """Loggt das Ende einer Debatte."""
    path = _log_path(config)
    if not path:
        return
    _write(path, f"[{_ts()}] DEBATE_END  topic={topic[:200]}  rounds={rounds_completed}  file={file_path}\n")


def log_voice_sent(config: dict[str, Any], chars: int, voice: str = "", bytes_sent: int = 0) -> None:
    """Loggt das Senden einer Sprachantwort via TTS."""
    path = _log_path(config)
    if not path:
        return
    parts = [f"chars={chars}"]
    if voice:
        parts.append(f"voice={voice}")
    if bytes_sent:
        parts.append(f"bytes={bytes_sent}")
    _write(path, f"[{_ts()}] VOICE_SENT  {'  '.join(parts)}\n")


def log_compact(config: dict[str, Any], old_count: int, summary_tokens: int, recent_count: int, budget: int) -> None:
    """Loggt eine Chat-History-Komprimierung."""
    path = _log_path(config)
    if not path:
        return
    _write(path, f"[{_ts()}] COMPACT  {old_count} msgs → summary ({summary_tokens} tokens) + {recent_count} recent  budget={budget}\n")


def log_subagent_start(config: dict[str, Any], model: str, message: str) -> None:
    """Loggt den Start eines Subagent-Aufrufs."""
    path = _log_path(config)
    if not path:
        return
    msg = message if len(message) <= 1000 else message[:1000] + "…"
    _write(path, f"[{_ts()}] SUBAGENT  model={model}\n  prompt: {msg}\n")


def log_subagent_result(config: dict[str, Any], model: str, result: str, thinking: str = "") -> None:
    """Loggt das Ergebnis eines Subagent-Aufrufs inkl. Thinking."""
    path = _log_path(config)
    if not path:
        return
    lines = []
    if thinking:
        t = thinking if len(thinking) <= 2000 else thinking[:2000] + "…"
        lines.append(f"[{_ts()}] SUBAGENT_THINKING  model={model}\n{t}\n")
    res = result if len(result) <= 2000 else result[:2000] + "…"
    lines.append(f"[{_ts()}] SUBAGENT_RESULT  model={model}\n  result: {res}\n")
    _write(path, "".join(lines))
