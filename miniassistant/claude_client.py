"""
Claude / Anthropic Client – Zwei Modi:

1. **type: claude-code** – Claude Code CLI (`claude --print`), Auth via `claude login`
2. **type: anthropic** – Anthropic Messages API (https://docs.anthropic.com/en/docs/api-reference)
   Auth via `api_key` in Provider-Config.

Beide Modi liefern ein einheitliches Response-Format zurück, kompatibel mit
dem Rest von MiniAssistant (chat_loop, subagent, etc.).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Generator

import httpx

_log = logging.getLogger("miniassistant.claude_client")

# Anthropic API Defaults
ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"
_TIMEOUT = 120

# Typische npm user-install Pfade (wenn claude nicht im globalen PATH liegt)
_CLAUDE_BIN_SEARCH_PATHS = [
    "~/.local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.local/share/npm/bin/claude",
]

_UNSET = object()
_claude_bin_cache: object = _UNSET

# Env-Vars die Claude Code setzt um verschachtelte Sessions zu verhindern
_CLAUDE_NESTED_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def _subprocess_env() -> dict[str, str]:
    """os.environ ohne Claude-Code-Session-Variablen.

    Wenn miniassistant innerhalb einer Claude-Code-Sitzung läuft, verhindert
    CLAUDECODE jeden weiteren `claude`-Aufruf. Wir entfernen diese Vars
    aus der Kind-Umgebung damit `claude api get …` und `claude --print`
    funktionieren.
    """
    env = os.environ.copy()
    for var in _CLAUDE_NESTED_VARS:
        env.pop(var, None)
    return env


# ═══════════════════════════════════════════════════════════════════════════
# Claude Code CLI (type: claude-code)
# ═══════════════════════════════════════════════════════════════════════════

def cli_find_binary() -> str | None:
    """
    Sucht die claude-Binary: erst systemweit via PATH, dann in typischen
    npm user-install Pfaden (~/.local/bin, ~/.npm-global/bin, …).
    Ergebnis wird gecacht.
    """
    global _claude_bin_cache
    if _claude_bin_cache is not _UNSET:
        return _claude_bin_cache  # type: ignore[return-value]

    found = shutil.which("claude")
    if found:
        _claude_bin_cache = found
        return found

    for path_template in _CLAUDE_BIN_SEARCH_PATHS:
        expanded = os.path.expanduser(path_template)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            _claude_bin_cache = expanded
            return expanded

    _claude_bin_cache = None
    return None


def cli_is_available() -> bool:
    """Prüft ob die `claude` CLI verfügbar ist (PATH oder bekannte Installationspfade)."""
    return cli_find_binary() is not None


def cli_get_version() -> str | None:
    """Gibt die Claude Code Version zurück oder None."""
    exe = cli_find_binary()
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def cli_chat(
    message: str,
    *,
    system: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    timeout: int = 300,
    allowed_tools: list[str] | None = None,
) -> dict[str, Any]:
    """
    Claude Code CLI im nicht-interaktiven Modus (`claude --print`).
    Auth wird von Claude Code selbst gehandelt (claude login / ANTHROPIC_API_KEY).

    Returns: Einheitliches Response-Dict.
    """
    exe = cli_find_binary()
    if not exe:
        raise RuntimeError(
            "Claude Code CLI nicht gefunden. Installieren: npm install -g @anthropic-ai/claude-code "
            "und authentifizieren: claude login"
        )

    cmd = [exe, "--print", "--output-format", "json"]

    if system:
        cmd.extend(["--system-prompt", system])
    if model:
        cmd.extend(["--model", model])
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    cmd.append(message)
    _log.debug("Claude Code CLI: %s", " ".join(cmd[:6]) + "...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_subprocess_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude Code CLI Timeout nach {timeout}s")
    except Exception as e:
        raise RuntimeError(f"Claude Code CLI Fehler: {e}")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "not authenticated" in stderr.lower() or "login" in stderr.lower():
            raise RuntimeError(
                "Claude Code nicht authentifiziert. Bitte `claude login` ausführen "
                "oder ANTHROPIC_API_KEY setzen."
            )
        raise RuntimeError(f"Claude Code CLI Exit {result.returncode}: {stderr or result.stdout.strip()}")

    content, thinking = _parse_cli_response(result.stdout.strip())

    return {
        "message": {"role": "assistant", "content": content, "thinking": thinking},
        "model": model or "claude-code",
        "done": True,
        "provider": "claude-code",
    }


def _parse_cli_response(raw: str) -> tuple[str, str]:
    """Parst CLI JSON-Output. Gibt (content, thinking) zurück."""
    content = raw
    thinking = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            c = parsed.get("result", "") or parsed.get("content", "") or raw
            if isinstance(c, list):
                content, thinking = _extract_content_blocks(c, raw)
            else:
                content = str(c)
        elif isinstance(parsed, list):
            content, thinking = _extract_content_blocks(parsed, raw)
    except (json.JSONDecodeError, TypeError):
        pass
    return content, thinking


def _extract_content_blocks(blocks: list, fallback: str) -> tuple[str, str]:
    """Extrahiert Text und Thinking aus Content-Blöcken."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            thinking_parts.append(block.get("thinking", ""))
    content = "\n".join(text_parts) if text_parts else fallback
    thinking = "\n".join(thinking_parts)
    return content, thinking


# ═══════════════════════════════════════════════════════════════════════════
# Anthropic Messages API (type: anthropic)
# ═══════════════════════════════════════════════════════════════════════════

def _api_headers(api_key: str) -> dict[str, str]:
    """Standard-Header für Anthropic API."""
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }


def api_list_models(
    api_key: str,
    base_url: str = ANTHROPIC_API_URL,
) -> list[dict[str, Any]]:
    """
    Listet verfügbare Modelle über GET /v1/models.
    Returns: Liste von {id, display_name, created_at, type} Dicts.
    """
    if not api_key:
        raise RuntimeError("Anthropic API: api_key erforderlich")
    url = f"{base_url.rstrip('/')}/v1/models"
    try:
        r = httpx.get(url, headers=_api_headers(api_key), timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        models = data.get("data") or []
        return [
            {
                "name": m.get("id", ""),
                "display_name": m.get("display_name", m.get("id", "")),
                "created_at": m.get("created_at", ""),
                "type": m.get("type", "model"),
            }
            for m in models
            if m.get("id")
        ]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise RuntimeError("Anthropic API: Ungültiger API-Key (401 Unauthorized)")
        raise RuntimeError(f"Anthropic API Fehler: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        raise RuntimeError(f"Anthropic API nicht erreichbar: {e}")


def api_chat(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    system: str | None = None,
    max_tokens: int = 8192,
    thinking: bool | str | None = None,
    thinking_budget: int = 10000,
    tools: list[dict[str, Any]] | None = None,
    base_url: str = ANTHROPIC_API_URL,
    timeout: int = _TIMEOUT,
) -> dict[str, Any]:
    """
    Anthropic Messages API – POST /v1/messages.

    Args:
        messages: [{role: "user"/"assistant", content: "..."}]
        api_key: Anthropic API Key
        model: Modell-ID (z.B. claude-sonnet-4-6)
        system: System-Prompt (optional)
        max_tokens: Max. Output-Tokens (Default: 8192)
        thinking: Extended Thinking aktivieren (True/False/None)
        thinking_budget: Token-Budget für Thinking (Default: 10000)
        tools: Tool-Schema (Ollama/OpenAI-Format, wird konvertiert)
        base_url: API Base-URL (Default: https://api.anthropic.com)

    Returns: Einheitliches Response-Dict (kompatibel mit Ollama-Format).
    """
    if not api_key:
        raise RuntimeError("Anthropic API: api_key erforderlich")

    url = f"{base_url.rstrip('/')}/v1/messages"

    # Messages in Anthropic-Format konvertieren
    api_msgs = _convert_messages(messages)

    body: dict[str, Any] = {
        "model": model,
        "messages": api_msgs,
        "max_tokens": max_tokens,
    }

    if system:
        body["system"] = system

    # Tools konvertieren und hinzufügen
    anthropic_tools = _convert_tools(tools or [])
    if anthropic_tools:
        body["tools"] = anthropic_tools

    # Extended Thinking
    if thinking:
        # budget_tokens muss < max_tokens sein – automatisch anpassen
        if thinking_budget >= max_tokens:
            max_tokens = thinking_budget + 4096
            body["max_tokens"] = max_tokens
        body["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget,
        }

    headers = _api_headers(api_key)
    _log.debug("Anthropic API: model=%s, msgs=%d, tools=%d, thinking=%s",
               model, len(api_msgs), len(anthropic_tools or []), thinking)

    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        resp = r.json()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = e.response.text[:300]
        if status == 401:
            raise RuntimeError("Anthropic API: Ungültiger API-Key (401)")
        if status == 429:
            raise RuntimeError(f"Anthropic API: Rate Limit erreicht (429). {detail}")
        if status == 529:
            raise RuntimeError(f"Anthropic API: Überlastet (529). {detail}")
        raise RuntimeError(f"Anthropic API {status}: {detail}")
    except Exception as e:
        raise RuntimeError(f"Anthropic API nicht erreichbar: {e}")

    # Response parsen → einheitliches Format
    message = _parse_api_response(resp)

    return {
        "message": message,
        "model": resp.get("model", model),
        "done": True,
        "provider": "anthropic",
        "usage": resp.get("usage"),
        "stop_reason": resp.get("stop_reason"),
    }


def api_chat_stream(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    system: str | None = None,
    max_tokens: int = 8192,
    thinking: bool | str | None = None,
    thinking_budget: int = 10000,
    tools: list[dict[str, Any]] | None = None,
    base_url: str = ANTHROPIC_API_URL,
    timeout: int = _TIMEOUT,
) -> Generator[dict[str, Any], None, None]:
    """
    Anthropic Messages API mit Streaming (SSE).
    Yields Chunks im einheitlichen Format.
    Tool-Calls werden akkumuliert und am Ende als einzelner Chunk gesendet.
    """
    if not api_key:
        raise RuntimeError("Anthropic API: api_key erforderlich")

    url = f"{base_url.rstrip('/')}/v1/messages"
    api_msgs = _convert_messages(messages)

    body: dict[str, Any] = {
        "model": model,
        "messages": api_msgs,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if system:
        body["system"] = system

    # Tools konvertieren und hinzufügen
    anthropic_tools = _convert_tools(tools or [])
    if anthropic_tools:
        body["tools"] = anthropic_tools

    if thinking:
        # budget_tokens muss < max_tokens sein – automatisch anpassen
        if thinking_budget >= max_tokens:
            body["max_tokens"] = thinking_budget + 4096
        body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    headers = _api_headers(api_key)

    # Akkumulierte Tool-Calls (Anthropic streamt tool_use-Blöcke)
    _tool_calls_acc: list[dict[str, Any]] = []
    _current_tool: dict[str, Any] | None = None
    _current_tool_input_json = ""

    with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        resp.raise_for_status()
        current_type = None
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "content_block_start":
                block = event.get("content_block") or {}
                current_type = block.get("type", "text")
                if current_type == "tool_use":
                    _current_tool = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                    }
                    _current_tool_input_json = ""
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if current_type == "thinking":
                    text = delta.get("thinking", "")
                    if text:
                        yield {"message": {"thinking": text}, "done": False}
                elif current_type == "text":
                    text = delta.get("text", "")
                    if text:
                        yield {"message": {"content": text}, "done": False}
                elif current_type == "tool_use":
                    # input_json_delta – akkumulieren
                    partial = delta.get("partial_json", "")
                    if partial:
                        _current_tool_input_json += partial
            elif etype == "content_block_stop":
                if current_type == "tool_use" and _current_tool:
                    # Tool-Call fertig – Input parsen und akkumulieren
                    try:
                        args = json.loads(_current_tool_input_json) if _current_tool_input_json else {}
                    except json.JSONDecodeError:
                        args = {}
                    _tool_calls_acc.append({
                        "id": _current_tool["id"],
                        "function": {
                            "name": _current_tool["name"],
                            "arguments": args,
                        },
                    })
                    _current_tool = None
                    _current_tool_input_json = ""
                current_type = None
            elif etype == "message_stop":
                # Akkumulierte Tool-Calls als finalen Chunk senden
                if _tool_calls_acc:
                    yield {"message": {"tool_calls": _tool_calls_acc}, "done": False}
                yield {"done": True}


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Konvertiert Ollama/OpenAI-kompatibles Tool-Schema ins Anthropic-Format.
    Ollama: [{type: function, function: {name, description, parameters}}]
    Anthropic: [{name, description, input_schema}]
    """
    if not tools:
        return None
    anthropic_tools: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        anthropic_tools.append({
            "name": name,
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return anthropic_tools or None


def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Konvertiert interne Messages ins Anthropic-Format.
    Filtert system-role raus (wird separat als 'system' Parameter übergeben).
    Konvertiert assistant-Messages mit tool_calls zu Anthropic content-blocks (text + tool_use).
    Konvertiert tool-role zu Anthropic tool_result Format mit korrektem tool_use_id.
    """
    api_msgs: list[dict[str, Any]] = []
    _tool_call_counter = 0

    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                # Anthropic: content ist ein Array von Blöcken (text + tool_use)
                content_blocks: list[dict[str, Any]] = []
                text = msg.get("content", "")
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tc_id = tc.get("id", "")
                    if not tc_id:
                        _tool_call_counter += 1
                        tc_id = f"toolu_gen_{_tool_call_counter}"
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                api_msgs.append({"role": "assistant", "content": content_blocks})
            else:
                content = msg.get("content", "")
                api_msgs.append({"role": "assistant", "content": content})
            continue

        if role == "tool":
            # Anthropic: tool_result als user-Message mit tool_use_id
            tool_name = msg.get("tool_name", "tool")
            tool_use_id = msg.get("tool_call_id", "")

            if not tool_use_id:
                # ID aus vorheriger assistant-Message rekonstruieren (nach Position)
                # Zähle wie viele tool-Messages seit dem letzten assistant kamen
                tool_idx = 0
                for prev in reversed(api_msgs):
                    if prev.get("role") == "user" and isinstance(prev.get("content"), list):
                        # Vorherige tool_results → zählen
                        tool_idx += sum(1 for b in prev["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
                    elif prev.get("role") == "assistant":
                        break

                # Finde das passende tool_use-Block in der letzten assistant-Message
                for prev in reversed(api_msgs):
                    if prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                        tool_use_blocks = [b for b in prev["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
                        if tool_idx < len(tool_use_blocks):
                            tool_use_id = tool_use_blocks[tool_idx].get("id", "")
                        break

            if not tool_use_id:
                _tool_call_counter += 1
                tool_use_id = f"toolu_gen_{_tool_call_counter}"

            result_block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": msg.get("content", ""),
            }
            # Consecutive tool results → merge into single user message
            if api_msgs and api_msgs[-1].get("role") == "user" and isinstance(api_msgs[-1].get("content"), list):
                all_tool_results = all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in api_msgs[-1]["content"]
                )
                if all_tool_results:
                    api_msgs[-1]["content"].append(result_block)
                    continue
            api_msgs.append({"role": "user", "content": [result_block]})
            continue

        # user (oder unbekannt → user)
        content = msg.get("content", "")
        api_msgs.append({"role": "user", "content": content})

    return api_msgs


def _parse_api_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Parst Anthropic API Response → einheitliches Format (kompatibel mit Ollama).
    Extrahiert text, thinking, tool_use aus content-blocks."""
    content_blocks = resp.get("content") or []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            thinking_parts.append(block.get("thinking", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "function": {
                    "name": block.get("name", ""),
                    "arguments": block.get("input") or {},
                },
            })

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts),
        "thinking": "\n".join(thinking_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return message


# ═══════════════════════════════════════════════════════════════════════════
# Compat-Wrapper (für chat_loop.py – einheitliche Schnittstelle)
# ═══════════════════════════════════════════════════════════════════════════

# Legacy aliases (für bestehenden Code)
is_available = cli_is_available
chat = cli_chat
ask_cli = lambda msg, **kw: (cli_chat(msg, **kw).get("message") or {}).get("content", "").strip()


def cli_list_models() -> list[str]:
    """
    Gibt eine statische Liste bekannter Claude-Modelle zurück die mit Claude Code
    nutzbar sind. `claude api` ist in Claude Code v2.x kein gültiger Subcommand
    (startet stattdessen den interaktiven Modus), daher wird die Liste manuell gepflegt.
    """
    return [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
    ]
