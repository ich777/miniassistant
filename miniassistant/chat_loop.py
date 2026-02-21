"""
Chat-Loop: System-Prompt, Nachrichten, /model-Wechsel, Tool-Ausführung (exec, web_search).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

_log = logging.getLogger("miniassistant")

from miniassistant.agent_loader import build_system_prompt
from miniassistant.config import load_config
from miniassistant.ollama_client import (
    chat as ollama_chat,
    chat_stream as ollama_chat_stream,
    get_api_key_for_model,
    get_base_url_for_model,
    get_num_ctx_for_model,
    get_options_for_model,
    get_provider_config,
    get_provider_type,
    get_think_for_model,
    get_tools_schema,
    get_vision_models,
    list_models as ollama_list_models,
    model_supports_tools,
    model_supports_vision,
    resolve_model,
)
from miniassistant.tools import run_exec, web_search as tool_web_search, check_url as tool_check_url, read_url as tool_read_url
from miniassistant.memory import append_exchange
import miniassistant.agent_actions_log as _aal
import miniassistant.context_log as _ctx_log

try:
    from miniassistant.scheduler import add_scheduled_job, list_scheduled_jobs, remove_scheduled_job
except ImportError:
    add_scheduled_job = None
    list_scheduled_jobs = None
    remove_scheduled_job = None


def _resolve_vision_model(config: dict[str, Any], current_model: str) -> str | None:
    """Prüft ob das aktuelle Modell Vision unterstützt. Wenn nicht, wird das konfigurierte Vision-Modell zurückgegeben.
    Returns: Modellname (vision-fähig) oder None wenn kein Vision-Modell verfügbar."""
    # Google/OpenAI/Anthropic sind nativ multimodal → kein Wechsel nötig
    provider_type = get_provider_type(config, current_model)
    if provider_type in ("google", "openai", "anthropic"):
        return current_model
    # Ollama: Capabilities prüfen
    base_url = get_base_url_for_model(config, current_model)
    if model_supports_vision(base_url, current_model):
        return current_model
    # Aktuelles Modell hat keine Vision → konfiguriertes Vision-Modell nutzen
    vision_models = get_vision_models(config)
    if vision_models:
        vm = vision_models[0]
        _log.info("Vision: Modell %s hat keine Vision-Unterstützung, wechsle zu %s", current_model, vm)
        return vm
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Multi-Provider Dispatch (Ollama, Anthropic, Google)
# ═══════════════════════════════════════════════════════════════════════════

def _dispatch_chat(
    config: dict[str, Any],
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    think: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Ruft den richtigen Chat-Client basierend auf provider_type auf.
    Gibt einheitliches Response-Dict zurück (message, model, done, provider)."""
    provider_type = get_provider_type(config, model_name)
    base_url = get_base_url_for_model(config, model_name)
    api_key = get_api_key_for_model(config, model_name)
    _, api_model = get_provider_config(config, model_name)
    api_model = api_model or model_name

    if provider_type == "google":
        from miniassistant.google_client import api_chat as google_chat
        return google_chat(
            messages, api_key=api_key, model=api_model,
            system=system, thinking=think, tools=tools,
            options=options, base_url=base_url or "https://generativelanguage.googleapis.com",
            timeout=int(timeout),
        )
    if provider_type == "openai" or provider_type == "deepseek":
        from miniassistant.openai_client import api_chat as openai_chat
        _default_url = "https://api.deepseek.com" if provider_type == "deepseek" else "https://api.openai.com"
        return openai_chat(
            messages, api_key=api_key, model=api_model,
            system=system, thinking=think, tools=tools,
            options=options, base_url=base_url or _default_url,
            timeout=int(timeout),
        )
    if provider_type == "anthropic":
        from miniassistant.claude_client import api_chat as anthropic_chat
        return anthropic_chat(
            messages, api_key=api_key, model=api_model,
            system=system, thinking=think, tools=tools,
            base_url=base_url or "https://api.anthropic.com",
            timeout=int(timeout),
        )
    if provider_type == "claude-code":
        from miniassistant.claude_client import cli_chat
        # cli_chat nimmt einen einzelnen String — letzten User-Message extrahieren
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        if not last_user:
            last_user = "Hello"
        return cli_chat(
            last_user,
            system=system,
            model=api_model,
            timeout=int(timeout),
        )
    # Default: Ollama
    return ollama_chat(
        base_url, messages, model=api_model,
        system=system, think=think, tools=tools,
        options=options or None, api_key=api_key, timeout=timeout,
    )


def _dispatch_chat_stream(
    config: dict[str, Any],
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    think: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    timeout: float = 300.0,
):
    """Streaming-Variante des Dispatch. Yields Chunks im einheitlichen Format."""
    provider_type = get_provider_type(config, model_name)
    base_url = get_base_url_for_model(config, model_name)
    api_key = get_api_key_for_model(config, model_name)
    _, api_model = get_provider_config(config, model_name)
    api_model = api_model or model_name

    if provider_type == "google":
        from miniassistant.google_client import api_chat_stream as google_stream
        yield from google_stream(
            messages, api_key=api_key, model=api_model,
            system=system, thinking=think, tools=tools,
            options=options, base_url=base_url or "https://generativelanguage.googleapis.com",
        )
        return
    if provider_type == "openai" or provider_type == "deepseek":
        from miniassistant.openai_client import api_chat_stream as openai_stream
        _default_url = "https://api.deepseek.com" if provider_type == "deepseek" else "https://api.openai.com"
        yield from openai_stream(
            messages, api_key=api_key, model=api_model,
            system=system, thinking=think, tools=tools,
            options=options, base_url=base_url or _default_url,
        )
        return
    if provider_type == "anthropic":
        from miniassistant.claude_client import api_chat_stream as anthropic_stream
        yield from anthropic_stream(
            messages, api_key=api_key, model=api_model,
            system=system, thinking=think, tools=tools,
            base_url=base_url or "https://api.anthropic.com",
        )
        return
    if provider_type == "claude-code":
        # CLI unterstützt kein Streaming — vollständige Antwort als einzelnen Chunk liefern
        resp = _dispatch_chat(
            config, model_name, messages,
            system=system, think=think, tools=tools,
            options=options, timeout=timeout,
        )
        msg = resp.get("message") or {}
        if msg.get("thinking"):
            yield {"message": {"thinking": msg["thinking"]}, "done": False}
        yield {"message": {"content": msg.get("content", "")}, "done": False}
        yield {"done": True}
        return
    # Default: Ollama
    yield from ollama_chat_stream(
        base_url, messages, model=api_model,
        system=system, think=think, tools=tools,
        options=options or None, api_key=api_key, timeout=timeout,
    )


def _provider_supports_tools(config: dict[str, Any], model_name: str) -> bool:
    """Prüft ob ein Modell Tool-Calling unterstützt (Provider-übergreifend)."""
    provider_type = get_provider_type(config, model_name)
    if provider_type == "google":
        from miniassistant.google_client import model_supports_tools as google_tools_check
        _, api_model = get_provider_config(config, model_name)
        return google_tools_check(api_model or model_name)
    if provider_type == "openai" or provider_type == "deepseek":
        from miniassistant.openai_client import model_supports_tools as openai_tools_check
        _, api_model = get_provider_config(config, model_name)
        return openai_tools_check(api_model or model_name)
    if provider_type == "anthropic":
        return True  # Anthropic Tool-Calling via claude_client._convert_tools
    if provider_type == "claude-code":
        return False  # Claude Code hat eigene interne Tools, kein miniassistant Tool-Format
    # Ollama
    base_url = get_base_url_for_model(config, model_name)
    _, api_model = get_provider_config(config, model_name)
    return model_supports_tools(base_url, api_model or model_name)


def _ollama_available_models(config: dict[str, Any], provider_name: str | None = None) -> tuple[list[str], str]:
    """Liefert (Liste der verfügbaren Modellnamen, Fehlermeldung oder '').
    Unterstützt Ollama, Google, OpenAI, Anthropic und Claude-Code Provider.
    provider_name: wenn gesetzt, nur Modelle dieses Providers (case-insensitive). Sonst default provider."""
    from miniassistant.ollama_client import _find_provider
    providers = config.get("providers") or {}
    if provider_name:
        real_key = _find_provider(providers, provider_name)
        prov = providers.get(real_key) if real_key else None
    else:
        default_name = next(iter(providers), "ollama")
        prov = providers.get(default_name)
    if not prov:
        prov = {}
    prov_type = str(prov.get("type", "ollama")).lower().strip()
    api_key = prov.get("api_key") or None

    if prov_type == "google":
        try:
            from miniassistant.google_client import api_list_models as google_list
            raw = google_list(api_key or "", base_url=prov.get("base_url", "https://generativelanguage.googleapis.com"))
            names = [m.get("name", "") for m in raw if m.get("name")]
            return (names, "")
        except Exception as e:
            return ([], str(e).strip() or "Google Gemini API nicht erreichbar")
    elif prov_type in ("openai", "deepseek"):
        try:
            from miniassistant.openai_client import api_list_models as openai_list
            _default_url = "https://api.deepseek.com" if prov_type == "deepseek" else "https://api.openai.com"
            raw = openai_list(api_key or "", base_url=prov.get("base_url", _default_url))
            names = [m.get("name", "") for m in raw if m.get("name")]
            return (names, "")
        except Exception as e:
            _label = "DeepSeek" if prov_type == "deepseek" else "OpenAI"
            return ([], str(e).strip() or f"{_label} API nicht erreichbar")
    elif prov_type == "anthropic":
        try:
            from miniassistant.claude_client import api_list_models
            raw = api_list_models(api_key or "", base_url=prov.get("base_url", "https://api.anthropic.com"))
            names = [m.get("name", "") for m in raw if m.get("name")]
            return (names, "")
        except Exception as e:
            return ([], str(e).strip() or "Anthropic API nicht erreichbar")
    elif prov_type == "claude-code":
        try:
            from miniassistant.claude_client import cli_list_models
            return (cli_list_models(), "")
        except Exception as e:
            return ([], str(e).strip() or "claude-code: ANTHROPIC_API_KEY nicht gesetzt")
    else:
        # Ollama (default)
        base_url = prov.get("base_url", "http://127.0.0.1:11434")
        try:
            raw = ollama_list_models(base_url, api_key=api_key)
            names = [m.get("name") or m.get("model") or "" for m in (raw or []) if (m.get("name") or m.get("model"))]
            return (names, "")
        except Exception as e:
            return ([], str(e).strip() or "Ollama nicht erreichbar")


def _configured_model_names(config: dict[str, Any]) -> list[str]:
    """Liefert die konfigurierten Modellnamen (Standard, list, Alias-Namen) aller Provider für Fehlermeldungen bei /model.
    Nicht-Default-Provider bekommen einen Prefix (z.B. 'chipspc/llama-chips')."""
    providers = config.get("providers") or {}
    default_prov = next(iter(providers), "ollama")
    out: list[str] = []
    for prov_name, prov_cfg in providers.items():
        if not isinstance(prov_cfg, dict):
            continue
        prov_models = prov_cfg.get("models") or {}
        prefix = "" if prov_name == default_prov else f"{prov_name}/"
        default = (prov_models.get("default") or "").strip()
        if default:
            display = f"{prefix}{default}"
            if display not in out:
                out.append(display)
        for m in (prov_models.get("list") or []):
            name = (m or "").strip()
            if name:
                display = f"{prefix}{name}"
                if display not in out:
                    out.append(display)
        for alias in (prov_models.get("aliases") or {}):
            if alias:
                display = f"{prefix}{alias}"
                if display not in out:
                    out.append(display)
    return out


# Kontext-Kürzung: Token-Schätzung (konservativ ~3 Zeichen/Token) und Trim auf num_ctx
def _estimate_tokens(text: str) -> int:
    """Konservative Token-Schätzung ohne Tokenizer (ca. 3 Zeichen/Token für gemischte Sprachen)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.0))


def _message_tokens_estimate(m: dict[str, Any]) -> int:
    """Geschätzte Token-Anzahl für eine einzelne Nachricht (role, content, thinking, tool_calls)."""
    part = (m.get("role") or "") + (m.get("content") or "") + (m.get("thinking") or "")
    if m.get("tool_calls"):
        part += json.dumps(m.get("tool_calls"), ensure_ascii=False)
    return _estimate_tokens(part)


def _messages_token_estimate(msgs: list[dict[str, Any]]) -> int:
    """Geschätzte Token-Anzahl für eine Nachrichtenliste (role, content, thinking, tool_calls)."""
    return sum(_message_tokens_estimate(m) for m in msgs)


def _log_estimated_tokens(config: dict[str, Any], system_prompt: str, msgs: list[dict[str, Any]], tools: list | None = None) -> None:
    """Log estimated token usage BEFORE Ollama call (if show_estimated_tokens is enabled)."""
    if not (config.get("server") or {}).get("show_estimated_tokens"):
        return
    sys_tok = _estimate_tokens(system_prompt or "")
    msg_tok = _messages_token_estimate(msgs)
    tools_tok = _estimate_tokens(json.dumps(tools or [], ensure_ascii=False))
    total = sys_tok + msg_tok + tools_tok
    line = "Estimated tokens – system: %d, messages: %d, tools: %d, total: %d" % (sys_tok, msg_tok, tools_tok, total)
    # uvicorn.error logger für Web-Kontext, print als Fallback für Matrix/Discord
    import sys
    uv_log = logging.getLogger("uvicorn.error")
    if uv_log.handlers:
        uv_log.info(line)
    else:
        print("INFO:     " + line, file=sys.stderr, flush=True)


def _trim_messages_to_fit(
    system_prompt: str,
    messages: list[dict[str, Any]],
    max_ctx: int,
    reserve_tokens: int = 1024,
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    System-Prompt, Tools-Schema und aktuelle User-Nachricht (letzte Message) bleiben immer.
    Nur die History (ältere Nachrichten) wird bei Bedarf von vorn gekürzt.
    Rechnung: system_tokens + tools_tokens + current_prompt_tokens + history_tokens <= max_ctx - reserve.
    """
    system_tokens = _estimate_tokens(system_prompt or "")
    tools_tokens = _estimate_tokens(json.dumps(tools or [], ensure_ascii=False))
    if not messages:
        return list(messages)
    # Aktueller Prompt = letzte Nachricht (bleibt immer)
    current_message = messages[-1]
    current_tokens = _message_tokens_estimate(current_message)
    history = list(messages[:-1])
    # Budget für History: was nach System, Tools, aktuellem Prompt und Reserve noch übrig ist
    budget = max(0, max_ctx - reserve_tokens - system_tokens - tools_tokens - current_tokens)
    history_tokens = _messages_token_estimate(history)
    if history_tokens <= budget:
        return list(messages)
    # Älteste Nachrichten (vorn) weglassen, bis History ins Budget passt
    out = list(history)
    while out and _messages_token_estimate(out) > budget:
        out.pop(0)
    return out + [current_message]


# ---------------------------------------------------------------------------
#  Smart Chat Compacting
# ---------------------------------------------------------------------------

_COMPACT_SYSTEM = (
    "Du bist ein Zusammenfassungs-Assistent. Fasse den Chatverlauf kurz und präzise zusammen.\n"
    "Behalte: Fakten, Entscheidungen, offene Aufgaben, User-Präferenzen, wichtige Ergebnisse, Tool-Aufrufe und deren Resultate.\n"
    "Format: Stichpunkte, max 400 Wörter. Antworte NUR mit der Zusammenfassung, keine Einleitung."
)


def _exec_env(config: dict[str, Any]) -> dict[str, str] | None:
    """Baut Extra-Umgebungsvariablen für run_exec aus der Config (z. B. GitHub-Token)."""
    env: dict[str, str] = {}
    github_token = (config.get("github_token") or "").strip()
    if github_token:
        env["GH_TOKEN"] = github_token
        env["GITHUB_TOKEN"] = github_token
    return env or None


def _context_budget(config: dict[str, Any], num_ctx: int) -> int:
    """Max erlaubte Tokens (system + tools + messages) basierend auf context_quota."""
    quota = float((config.get("chat") or {}).get("context_quota", 0.85) or 0.85)
    return int(num_ctx * min(max(quota, 0.5), 0.95))


def _needs_compacting(
    config: dict[str, Any],
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list | None,
    num_ctx: int,
) -> bool:
    """Prüft ob system + tools + messages die context_quota von num_ctx überschreiten.
    Mindestens 6 Messages nötig (sonst lohnt Compacting nicht / Loop-Gefahr)."""
    if len(messages) < 6:
        return False
    budget = _context_budget(config, num_ctx)
    used = (
        _estimate_tokens(system_prompt)
        + _estimate_tokens(json.dumps(tools or [], ensure_ascii=False))
        + _messages_token_estimate(messages)
    )
    return used > budget


def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Formatiert Messages als lesbaren Text für die Zusammenfassung.
    Tool-Calls (leerer content aber tool_calls-Array) werden explizit erfasst."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = (m.get("content") or "").strip()
        tool_calls = m.get("tool_calls") or []
        if role == "tool":
            if content:
                lines.append(f"[Tool-Ergebnis]: {content[:800]}")
        elif role == "assistant":
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "?")
                args = json.dumps(fn.get("arguments") or {}, ensure_ascii=False)
                lines.append(f"[Tool-Aufruf: {name}({args[:300]})]")
            if content:
                lines.append(f"Assistant: {content[:800]}")
        elif role == "user":
            if content:
                lines.append(f"User: {content[:1000]}")
        elif role == "system":
            if content:
                lines.append(f"[System]: {content[:300]}")
    return "\n".join(lines)


def _compact_history(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    system_prompt: str,
    tools: list | None,
    num_ctx: int,
) -> list[dict[str, Any]]:
    """
    Smart Compacting: Fasst ältere Messages zusammen, behält neuere unverändert.
    Budget = num_ctx * context_quota. Reserve für neueste Messages = 15% von num_ctx.
    Gibt komprimierte Message-Liste zurück: [summary_msg] + recent_messages.
    Bei Fehler: Fallback auf harte Kürzung (älteste Messages entfernen).
    """
    budget = _context_budget(config, num_ctx)
    fixed_tokens = (
        _estimate_tokens(system_prompt)
        + _estimate_tokens(json.dumps(tools or [], ensure_ascii=False))
    )
    msg_budget = max(0, budget - fixed_tokens)

    if _messages_token_estimate(messages) <= msg_budget:
        return messages

    # Reserve: 15% von num_ctx für neueste Messages (skaliert mit Modellgröße)
    reserve = int(num_ctx * 0.15)

    # Split: neueste Messages behalten (innerhalb reserve), Rest zusammenfassen
    recent: list[dict[str, Any]] = []
    recent_tokens = 0
    for msg in reversed(messages):
        t = _message_tokens_estimate(msg)
        if recent_tokens + t > reserve and recent:
            break
        recent.insert(0, msg)
        recent_tokens += t

    old_count = len(messages) - len(recent)
    old = messages[:old_count]
    if not old:
        return messages

    conversation_text = _format_messages_for_summary(old)
    if not conversation_text.strip():
        return recent

    # Model-Aufruf für Summary (Provider-übergreifend)
    try:
        response = _dispatch_chat(
            config, model,
            [{"role": "user", "content": f"Fasse diesen Chatverlauf zusammen:\n\n{conversation_text}"}],
            system=_COMPACT_SYSTEM,
        )
        summary = ((response.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        _log.warning("Chat compacting failed: %s — falling back to hard trim", e)
        out = list(messages)
        while out and _messages_token_estimate(out) > msg_budget:
            out.pop(0)
        return out

    if not summary:
        out = list(messages)
        while out and _messages_token_estimate(out) > msg_budget:
            out.pop(0)
        return out

    summary_msg = {
        "role": "system",
        "content": f"[Zusammenfassung des bisherigen Gesprächs]\n{summary}",
    }
    _log.info(
        "Chat compacted: %d messages → summary (%d tokens) + %d recent messages (budget: %d, num_ctx: %d)",
        old_count, _estimate_tokens(summary), len(recent), budget, num_ctx,
    )
    return [summary_msg] + recent


def _format_schedules() -> str:
    """Formatiert die Liste der geplanten Jobs als Markdown."""
    if not list_scheduled_jobs:
        return "*Scheduler nicht verfuegbar (pip install apscheduler).*"
    jobs = list_scheduled_jobs()
    if not jobs:
        return "*Keine geplanten Jobs.*"
    lines = ["**Geplante Jobs:**", ""]
    for j in jobs:
        trigger = j.get("trigger", "?")
        args = j.get("trigger_args") or {}
        jid = j.get("id", "")[:8]
        if trigger == "cron":
            when = f'{args.get("minute","*")} {args.get("hour","*")} {args.get("day","*")} {args.get("month","*")} {args.get("day_of_week","*")}'
        elif trigger == "date":
            when = args.get("run_date", "?")[:16]
        else:
            when = str(args)
        desc_parts = []
        if j.get("prompt"):
            desc_parts.append(f"Prompt: {j['prompt'][:50]}")
        if j.get("command"):
            desc_parts.append(f"Cmd: {j['command'][:40]}")
        if j.get("client"):
            desc_parts.append(f"-> {j['client']}")
        if j.get("once"):
            desc_parts.append("einmalig")
        desc = " | ".join(desc_parts) if desc_parts else "?"
        lines.append(f"- `{when}` | {desc} | ID: {jid}")
    return "\n".join(lines)


def parse_model_switch(user_input: str) -> tuple[str | None, str]:
    """
    Erkennt /model MODELLNAME oder /model ALIAS. Gibt (modellname, rest) zurück.
    Wenn kein Wechsel: (None, user_input).
    """
    raw = user_input.strip()
    if raw.startswith("/model ") and len(raw) > 7:
        rest = raw[7:].strip()
        if rest:
            return rest, ""
        return None, raw
    if raw == "/model":
        return None, raw
    return None, user_input


def parse_models_command(user_input: str) -> tuple[bool, str | None]:
    """
    Erkennt /models oder /models PROVIDER. Gibt (True, provider) zurück bei Treffer;
    provider=None bedeutet alle Anbieter, sonst z.B. 'ollama'. Bei keinem Treffer (False, None).
    """
    raw = user_input.strip()
    if raw == "/models":
        return True, None
    if raw.startswith("/models ") and len(raw) > 8:
        return True, raw[8:].strip() or None
    return False, None


def get_models_markdown(config: dict[str, Any], provider_filter: str | None, current_model: str | None = None) -> str:
    """
    Liefert eine Markdown-Liste der konfigurierten Modelle ALLER Provider.
    Bei /models <provider>: zusätzlich alle bei Ollama verfügbaren Modelle dieses Providers.
    """
    from miniassistant.ollama_client import list_models, _find_provider
    providers = config.get("providers") or {}
    default_prov_name = next(iter(providers), "ollama")
    lines: list[str] = []

    # Aktuelles Modell anzeigen
    if current_model:
        lines.append(f"**Aktuelles Modell:** `{current_model}`")
        lines.append("")

    # Welche Provider anzeigen?
    if provider_filter:
        real_key = _find_provider(providers, provider_filter)
        show_providers = {real_key: providers[real_key]} if real_key else {}
        if not real_key:
            lines.append(f"*Provider `{provider_filter}` nicht gefunden. Verfügbar: {', '.join(providers.keys())}*")
    else:
        show_providers = providers

    for prov_name, prov_cfg in show_providers.items():
        if not isinstance(prov_cfg, dict):
            continue
        prov_models = prov_cfg.get("models") or {}
        is_default = (prov_name == default_prov_name)
        prefix = "" if is_default else f"{prov_name}/"
        header = f"**Provider: {prov_name}**" + (" *(default)*" if is_default else "")
        base_url = prov_cfg.get("base_url", "")

        # Nur Header wenn mehrere Provider oder expliziter Filter
        if len(show_providers) > 1 or provider_filter:
            lines.append(header)
            if base_url:
                lines.append(f"URL: `{base_url}`")
            lines.append("")

        # Standard-Modell
        prov_default = (prov_models.get("default") or "").strip()
        if prov_default:
            lines.append(f"**Standard:** `{prefix}{prov_default}`")
            lines.append("")

        # Aliase
        prov_aliases = prov_models.get("aliases") or {}
        if prov_aliases:
            lines.append("**Aliase:**")
            for alias, target in prov_aliases.items():
                target_clean = target or ""
                lines.append(f"- `{prefix}{alias}` → `{prefix}{target_clean}`")
            lines.append("")

        # Konfigurierte Modell-Liste
        prov_list = prov_models.get("list") or []
        if prov_list:
            lines.append("**Konfigurierte Modelle:**")
            for m in prov_list:
                m_clean = (m or "").strip()
                if not m_clean:
                    continue
                display = f"{prefix}{m_clean}"
                marker = " *(aktuell)*" if current_model and (m_clean == current_model or display == current_model) else ""
                lines.append(f"- `{display}`{marker}")
            lines.append("")

        # Bei /models <provider>: alle verfügbaren Modelle dieses Providers anzeigen
        if provider_filter and base_url:
            try:
                prov_api_key = prov_cfg.get("api_key") or None
                raw = list_models(base_url, api_key=prov_api_key)
                names = [m.get("name") or m.get("model") or "" for m in raw if (m.get("name") or m.get("model"))]
            except Exception as e:
                names = []
                lines.append(f"*{prov_name}: Fehler – " + str(e) + "*")
            if names:
                lines.append(f"**Alle bei {prov_name} verfuegbar:**")
                for n in names:
                    display = f"{prefix}{n}"
                    marker = " *(aktuell)*" if current_model and (n == current_model or display == current_model) else ""
                    lines.append(f"- `{display}`{marker}")
                lines.append("")

    if not lines or (len(lines) == 2 and current_model):
        lines.append("*Kein Modell konfiguriert. Nutze `/model MODELLNAME` zum Wechseln.*")
    lines.append("")
    lines.append("*Wechseln: `/model NAME`, `/model ALIAS` oder `/model provider/NAME`*")
    return "\n".join(lines).strip()


# Erlaubte Ollama ModelOptions (alles was Ollama in body.options akzeptiert)
_VALID_OLLAMA_OPTIONS = frozenset({
    "temperature", "top_p", "top_k", "num_ctx", "num_predict", "seed",
    "min_p", "stop", "repeat_penalty", "repetition_penalty",
    "repeat_last_n", "tfs_z", "mirostat", "mirostat_eta", "mirostat_tau",
    "num_gpu", "num_thread", "numa",
    # think ist erlaubt in model_options (wird separat behandelt, nicht in options gesendet)
    "think",
})

# Erlaubte Top-Level-Keys pro Provider
_VALID_PROVIDER_KEYS = frozenset({
    "type", "base_url", "api_key", "num_ctx", "think", "options", "model_options", "models",
})

# Erlaubte Keys unter providers.*.models
_VALID_MODELS_KEYS = frozenset({
    "default", "aliases", "list", "fallbacks", "subagents",
})


def _validate_provider_config(merged: dict) -> str | None:
    """Validiert die Provider-Config nach dem Merge. Gibt Fehlermeldung zurück oder None wenn ok."""
    providers = merged.get("providers")
    if not isinstance(providers, dict):
        return None  # Kein providers-Block → nichts zu validieren
    # github ist kein Ollama-Provider — github_token ist ein Top-Level-Key
    if "github" in providers:
        return (
            "There is no 'github' provider. "
            "To save a GitHub token use the top-level key: save_config with {github_token: 'YOUR_TOKEN'}. "
            "Do NOT put it under providers."
        )
    for prov_name, prov_cfg in providers.items():
        if not isinstance(prov_cfg, dict):
            continue
        # Top-Level Provider Keys prüfen
        unknown_prov = set(prov_cfg.keys()) - _VALID_PROVIDER_KEYS
        if unknown_prov:
            return f"Provider '{prov_name}' has unknown keys: {', '.join(sorted(unknown_prov))}. Valid: {', '.join(sorted(_VALID_PROVIDER_KEYS))}"
        # think muss bool oder null sein, keine Zahl
        think_val = prov_cfg.get("think")
        if think_val is not None and not isinstance(think_val, bool):
            return f"Provider '{prov_name}': 'think' must be true, false, or null — got {think_val!r}"
        # options: nur bekannte Keys
        opts = prov_cfg.get("options")
        if isinstance(opts, dict):
            unknown_opts = set(opts.keys()) - _VALID_OLLAMA_OPTIONS
            if unknown_opts:
                return f"Provider '{prov_name}'.options has unknown keys: {', '.join(sorted(unknown_opts))}. Valid Ollama options: {', '.join(sorted(_VALID_OLLAMA_OPTIONS - {'think'}))}"
        # model_options: Keys müssen Modellnamen sein, Values müssen bekannte Options haben
        mopts = prov_cfg.get("model_options")
        if isinstance(mopts, dict):
            # Bekannte Modellnamen sammeln (aus aliases-Werten, list, und vorhandenen model_options)
            models_block = prov_cfg.get("models") or {}
            known_models: set[str] = set()
            if isinstance(models_block, dict):
                # Alias-Ziele
                for target in (models_block.get("aliases") or {}).values():
                    clean = target
                    if isinstance(clean, str) and "/" in clean:
                        clean = clean.split("/", 1)[1]
                    known_models.add(clean)
                # model list
                for m in (models_block.get("list") or []):
                    clean = m
                    if isinstance(clean, str) and "/" in clean:
                        clean = clean.split("/", 1)[1]
                    known_models.add(clean)
                # default (aufgelöst)
                default = models_block.get("default")
                if isinstance(default, str):
                    aliases = models_block.get("aliases") or {}
                    resolved = aliases.get(default, default)
                    if isinstance(resolved, str) and "/" in resolved:
                        resolved = resolved.split("/", 1)[1]
                    known_models.add(resolved)
            for model_key, model_opts in mopts.items():
                if not isinstance(model_key, str):
                    return f"Provider '{prov_name}'.model_options: key must be a model name string, got {model_key!r}"
                if not isinstance(model_opts, dict):
                    return f"Provider '{prov_name}'.model_options.'{model_key}': value must be a dict of options, got {type(model_opts).__name__}"
                # Modellname prüfen (muss bekannt sein, falls models-Block vorhanden)
                if known_models and model_key not in known_models:
                    return (f"Provider '{prov_name}'.model_options: model '{model_key}' not found in configured models. "
                            f"Known: {', '.join(sorted(known_models))}. Add it to aliases or list first.")
                # Option-Keys prüfen
                unknown_mopts = set(model_opts.keys()) - _VALID_OLLAMA_OPTIONS
                if unknown_mopts:
                    return (f"Provider '{prov_name}'.model_options.'{model_key}' has unknown options: {', '.join(sorted(unknown_mopts))}. "
                            f"Valid: {', '.join(sorted(_VALID_OLLAMA_OPTIONS))}")
                # think in model_options muss bool oder null sein
                mthink = model_opts.get("think")
                if mthink is not None and not isinstance(mthink, bool):
                    return f"Provider '{prov_name}'.model_options.'{model_key}'.think must be true, false, or null — got {mthink!r}"
        # models-Block validieren
        models_block = prov_cfg.get("models")
        if isinstance(models_block, dict):
            unknown_mkeys = set(models_block.keys()) - _VALID_MODELS_KEYS
            if unknown_mkeys:
                hint = ""
                if "model_options" in unknown_mkeys:
                    hint = f" HINT: model_options belongs under providers.{prov_name} (same level as models), NOT inside models."
                if "options" in unknown_mkeys:
                    hint = f" HINT: options belongs under providers.{prov_name} (same level as models), NOT inside models."
                return (f"Provider '{prov_name}'.models has unknown keys: {', '.join(sorted(unknown_mkeys))}. "
                        f"Valid keys under models: {', '.join(sorted(_VALID_MODELS_KEYS))}.{hint}")
            # default muss string oder null sein, kein dict
            default = models_block.get("default")
            if default is not None and not isinstance(default, str):
                return f"Provider '{prov_name}'.models.default must be a model name string or null — got {type(default).__name__}: {default!r}"
            # subagents muss bool sein
            sub = models_block.get("subagents")
            if sub is not None and not isinstance(sub, bool):
                return f"Provider '{prov_name}'.models.subagents must be true or false — got {sub!r}"
            # Alias-Werte validieren: müssen reine Modellnamen sein (kein Komma, =, Leerzeichen usw.)
            aliases = models_block.get("aliases")
            if isinstance(aliases, dict):
                for alias_key, alias_val in aliases.items():
                    if not isinstance(alias_val, str):
                        return f"Provider '{prov_name}'.models.aliases.'{alias_key}': value must be a model name string — got {type(alias_val).__name__}"
                    # Ungültige Zeichen in Alias-Werten (Komma, Gleichheitszeichen = Parameter-Syntax)
                    for bad_char in (",", "=", " ", ";", "{", "}", "[", "]"):
                        if bad_char in alias_val:
                            return (f"Provider '{prov_name}'.models.aliases.'{alias_key}': value '{alias_val}' contains invalid character '{bad_char}'. "
                                    f"Alias values must be plain model names (e.g. 'qwen3:14b'). To set options use model_options, not aliases.")
            # fallbacks validieren: muss Liste von Strings sein
            fallbacks = models_block.get("fallbacks")
            if fallbacks is not None and not isinstance(fallbacks, list):
                return f"Provider '{prov_name}'.models.fallbacks must be a list — got {type(fallbacks).__name__}"
            if isinstance(fallbacks, list):
                for i, fb in enumerate(fallbacks):
                    if not isinstance(fb, str):
                        return f"Provider '{prov_name}'.models.fallbacks[{i}] must be a model name string — got {fb!r}"
    return None


def _run_tool(
    name: str,
    arguments: dict[str, Any] | str,
    config: dict[str, Any],
    project_dir: str | None = None,
) -> str:
    """Führt ein Tool aus und gibt das Ergebnis als String zurück."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "save_config":
        import yaml as _yaml
        from miniassistant.config import validate_config_raw, write_config_raw, load_config_raw
        content = (arguments.get("yaml_content") or "").strip()
        proj = project_dir
        if not content:
            return "save_config requires yaml_content (YAML with the keys to add/change)."
        # Parse LLM's YAML
        try:
            new_data = _yaml.safe_load(content)
            if not isinstance(new_data, dict):
                return "save_config: yaml_content must be a YAML mapping (key: value), not a scalar or list."
        except _yaml.YAMLError as e:
            return f"save_config: invalid YAML: {e}"
        # Load existing config as dict (preserve all existing keys)
        existing_raw = load_config_raw(proj)
        existing_data: dict = {}
        if existing_raw:
            try:
                existing_data = _yaml.safe_load(existing_raw) or {}
            except Exception:
                existing_data = {}
        # Deep-merge: LLM changes override, missing keys are preserved
        def _deep_merge(base: dict, override: dict) -> dict:
            result = dict(base)
            for k, v in override.items():
                if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                    result[k] = _deep_merge(result[k], v)
                else:
                    result[k] = v
            return result
        merged = _deep_merge(existing_data, new_data)
        # --- Provider-Config Validierung (nach Merge) ---
        prov_err = _validate_provider_config(merged)
        if prov_err:
            return f"save_config rejected: {prov_err}"
        merged_yaml = _yaml.safe_dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False)
        ok, err = validate_config_raw(merged_yaml)
        if not ok:
            return f"Config validation failed after merge: {err}. Fix and retry."
        try:
            written = write_config_raw(merged_yaml, proj)
            return f"Config saved to {written} (merged with existing config). Up to 4 .bak backups created. Tell user to restart the service."
        except Exception as e:
            return f"Config write failed: {e}"
    if name == "exec":
        cmd = arguments.get("command", "")
        workspace = (config.get("workspace") or "").strip() or None
        result = run_exec(cmd, cwd=workspace, extra_env=_exec_env(config))
        return f"returncode: {result['returncode']}\nstdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"
    if name == "web_search":
        query = arguments.get("query", "")
        from miniassistant.config import get_search_engine_url
        url = get_search_engine_url(config, arguments.get("engine"))
        if not url:
            return "web_search not configured (no search_engines or invalid engine)"
        result = tool_web_search(url, query)
        if result.get("error"):
            return f"Error: {result['error']}"
        lines = []
        for r in result.get("results") or []:
            lines.append(f"- {r.get('title', '')} | {r.get('url', '')}\n  {r.get('snippet', '')}")
        return "\n".join(lines) if lines else "No results"
    if name == "check_url":
        url_arg = arguments.get("url", "").strip()
        if not url_arg:
            return "check_url requires url"
        result = tool_check_url(url_arg)
        parts = [f"reachable: {result.get('reachable', False)}", f"status_code: {result.get('status_code', '')}"]
        if result.get("final_url"):
            parts.append(f"final_url: {result['final_url']}")
        if result.get("error"):
            parts.append(f"error: {result['error']}")
        return "\n".join(parts)
    if name == "read_url":
        url_arg = arguments.get("url", "").strip()
        if not url_arg:
            return "read_url requires url"
        result = tool_read_url(url_arg)
        if result.get("ok"):
            return result.get("content", "")
        return f"Error reading URL: {result.get('error', 'unknown error')}"
    if name == "send_image":
        from pathlib import Path as _Path
        image_path = arguments.get("image_path", "").strip()
        caption = arguments.get("caption", "").strip()
        if not image_path:
            return "send_image requires image_path"
        if not _Path(image_path).exists():
            return f"File not found: {image_path}"
        chat_ctx = config.get("_chat_context") or {}
        platform = chat_ctx.get("platform")
        room_id = chat_ctx.get("room_id")
        channel_id = chat_ctx.get("channel_id")
        try:
            from miniassistant.notify import send_image as _send_img
            results = _send_img(
                image_path, caption,
                client=platform,
                room_id=room_id,
                channel_id=channel_id,
                config=config,
            )
            parts = [f"{k}: {v}" for k, v in results.items()]
            return "\n".join(parts) if parts else f"Bild gespeichert: {image_path} (kein Chat-Client im Kontext)"
        except Exception as e:
            return f"send_image failed: {e}"
    if name == "status_update":
        msg = arguments.get("message", "").strip()
        if not msg:
            return "status_update requires 'message'"
        chat_ctx = config.get("_chat_context") or {}
        platform = chat_ctx.get("platform")
        room_id = chat_ctx.get("room_id")
        channel_id = chat_ctx.get("channel_id")
        if platform == "matrix" and room_id:
            try:
                from miniassistant.matrix_bot import send_message_to_room
                ok = send_message_to_room(room_id, msg)
                return "sent" if ok else "send failed (Matrix bot not running?)"
            except Exception as e:
                return f"send failed: {e}"
        elif platform == "discord" and channel_id:
            try:
                from miniassistant.discord_bot import send_message_to_channel
                ok = send_message_to_channel(channel_id, msg)
                return "sent" if ok else "send failed (Discord bot not running?)"
            except Exception as e:
                return f"send failed: {e}"
        else:
            return "status_update: no active chat context (only available in Matrix/Discord chats)"
    if name == "schedule":
        action = arguments.get("action", "create").lower()
        if action == "list":
            if not list_scheduled_jobs:
                return "Scheduler nicht verfuegbar."
            jobs = list_scheduled_jobs()
            if not jobs:
                return "Keine geplanten Jobs."
            lines = []
            for j in jobs:
                jid = j.get("id", "")[:8]
                t = j.get("trigger", "?")
                a = j.get("trigger_args") or {}
                when_str = f'{a.get("minute","*")} {a.get("hour","*")} {a.get("day","*")} {a.get("month","*")} {a.get("day_of_week","*")}' if t == "cron" else a.get("run_date", "?")[:16]
                parts = []
                if j.get("prompt"):
                    parts.append(f'prompt="{j["prompt"][:50]}"')
                if j.get("command"):
                    parts.append(f'cmd="{j["command"][:40]}"')
                if j.get("client"):
                    parts.append(f'client={j["client"]}')
                if j.get("model"):
                    parts.append(f'model={j["model"]}')
                if j.get("once"):
                    parts.append("once")
                if j.get("room_id"):
                    parts.append(f'room={j["room_id"]}')
                if j.get("channel_id"):
                    parts.append(f'channel={j["channel_id"]}')
                lines.append(f"- ID:{jid} | {when_str} | {' '.join(parts)}")
            return "\n".join(lines)
        if action == "remove":
            if not remove_scheduled_job:
                return "Scheduler nicht verfuegbar."
            job_id = arguments.get("id", "").strip()
            if not job_id:
                return "schedule(action='remove') requires 'id' (job ID or prefix, use action='list' to see IDs)"
            ok, msg = remove_scheduled_job(job_id)
            return f"Removed: {msg}" if ok else f"Remove failed: {msg}"
        # action == "create" (default)
        if not add_scheduled_job:
            return "schedule not available (pip install apscheduler)"
        when = arguments.get("when", "")
        if not when:
            return "schedule(action='create') requires 'when'"
        cmd = arguments.get("command", "")
        prompt = arguments.get("prompt", "")
        client = arguments.get("client")
        once = arguments.get("once", False)
        sched_model = arguments.get("model", "").strip() or None
        if not cmd and not prompt:
            return "schedule requires 'command' and/or 'prompt'"
        # Room/Channel aus chat_context automatisch uebernehmen
        chat_ctx = config.get("_chat_context") or {}
        sched_room_id = chat_ctx.get("room_id") if chat_ctx.get("platform") == "matrix" else None
        sched_channel_id = chat_ctx.get("channel_id") if chat_ctx.get("platform") == "discord" else None
        ok, msg = add_scheduled_job(when, command=cmd, prompt=prompt, client=client or chat_ctx.get("platform"), once=bool(once), model=sched_model, room_id=sched_room_id, channel_id=sched_channel_id)
        return f"Scheduled: {msg}" if ok else f"Schedule failed: {msg}"
    if name == "debate":
        topic = arguments.get("topic", "").strip()
        perspective_a = arguments.get("perspective_a", "").strip()
        perspective_b = arguments.get("perspective_b", "").strip()
        sub_model = arguments.get("model", "").strip()
        if not topic or not perspective_a or not perspective_b or not sub_model:
            return "debate requires 'topic', 'perspective_a', 'perspective_b', and 'model'"
        model_b = arguments.get("model_b", "").strip() or sub_model
        rounds = max(1, min(10, int(arguments.get("rounds", 3) or 3)))
        language = arguments.get("language", "").strip() or "Deutsch"
        return _run_debate(config, topic, perspective_a, perspective_b, sub_model, model_b, rounds, language)
    if name == "invoke_model":
        # Subagents: global config oder per-provider subagents: true
        subagent_list = config.get("subagents") or []
        any_subagents = bool(subagent_list) or any(
            (p.get("models") or {}).get("subagents")
            for p in (config.get("providers") or {}).values()
            if isinstance(p, dict)
        )
        if not any_subagents:
            return "invoke_model not enabled (set subagents list in config or providers.<name>.models.subagents: true)"
        sub_model = arguments.get("model", "").strip()
        sub_msg = arguments.get("message", "").strip()
        if not sub_model or not sub_msg:
            return "invoke_model requires 'model' and 'message'"
        resolved = resolve_model(config, sub_model) or sub_model
        provider_type = get_provider_type(config, resolved)
        _, api_model_sub = get_provider_config(config, resolved)
        api_model_sub = api_model_sub or resolved
        _aal.log_subagent_start(config, resolved, sub_msg)
        # Subagent System-Prompt aus basic_rules/subagent.md (wird bei Aufruf injiziert, nicht im Hauptagent-Kontext)
        from miniassistant.basic_rules.loader import get_rule as _get_sub_rule
        sub_system = _get_sub_rule("subagent.md") or (
            "You are a subagent of MiniAssistant. Answer the task precisely and concisely. "
            "If you cannot answer, say so clearly. Stay on topic."
        )
        # Datum + Knowledge-Cutoff-Warnung an Subagent weitergeben
        # (ohne das halluziniert der Subagent veraltete Infos, z.B. "Produkt X existiert noch nicht")
        from datetime import datetime as _dt_sub
        _today_sub = _dt_sub.now().strftime("%B %d, %Y")
        sub_system += f"\n\nToday is **{_today_sub}**. Your training data has a cutoff — anything after that may be outdated."
        _kv_rule = _get_sub_rule("knowledge_verification.md")
        if _kv_rule:
            sub_system += f"\n{_kv_rule}"
        # Runtime-Info an Subagent weitergeben (root/sudo)
        from miniassistant.agent_loader import _is_root
        if _is_root():
            sub_system += "\n\nRunning as **root** (euid 0) — no sudo needed."
        else:
            sub_system += "\n\nNot running as root — use **sudo** when needed."
        # Workspace-Info an Subagent weitergeben
        _ws = (config.get("workspace") or "").strip()
        if _ws:
            sub_system = sub_system.replace("{workspace}", _ws)
            sub_system += f"\n\nWorking directory (cwd for exec): `{_ws}`. All exec commands run in this directory. Save ALL generated files (images, reports, etc.) inside this workspace directory. Use relative paths when possible."
        try:
            if provider_type == "claude-code":
                # Claude Code CLI – eigener Connector, keine Ollama-API
                return _run_subagent_claude_code(
                    config, api_model_sub, sub_system, sub_msg, resolved,
                )
            elif provider_type == "anthropic":
                # Anthropic Messages API – eigener Connector
                base_url = get_base_url_for_model(config, resolved)
                sub_api_key = get_api_key_for_model(config, resolved)
                think = get_think_for_model(config, resolved)
                return _run_subagent_anthropic(
                    config, api_model_sub, sub_system, sub_msg,
                    sub_api_key, base_url, think, resolved,
                )
            elif provider_type == "google":
                # Google Gemini API – Subagent mit Tool-Support
                return _run_subagent_google(
                    config, api_model_sub, sub_system, sub_msg, resolved,
                )
            elif provider_type in ("openai", "deepseek"):
                # OpenAI / DeepSeek API – Subagent mit Tool-Support
                return _run_subagent_openai(
                    config, api_model_sub, sub_system, sub_msg, resolved,
                )
            else:
                # Ollama-basierter Subagent (default)
                base_url = get_base_url_for_model(config, resolved)
                sub_api_key = get_api_key_for_model(config, resolved)
                think = get_think_for_model(config, resolved)
                options = get_options_for_model(config, resolved)
                from miniassistant.ollama_client import get_subagent_tools_schema
                sub_tools = get_subagent_tools_schema(config)
                # Tools nur mitschicken wenn Modell sie unterstützt
                if not model_supports_tools(base_url, api_model_sub):
                    _log.info("Subagent %s: model does not support tools, calling without tools", resolved)
                    sub_tools = []
                return _run_subagent_with_tools(
                    config, base_url, api_model_sub, sub_system, sub_msg,
                    sub_tools, think, options, sub_api_key, resolved,
                )
        except Exception as e:
            err = f"invoke_model failed ({resolved}): {e}"
            _aal.log_subagent_result(config, resolved, err, "")
            return err
    return f"Unknown tool: {name}"


# ═══════════════════════════════════════════════════════════════════════════
#  Debate: Structured multi-round discussion between two AI perspectives
# ═══════════════════════════════════════════════════════════════════════════

def _append_to_file(path, text: str) -> None:
    """Hängt Text an eine Datei an (UTF-8)."""
    from pathlib import Path as _P
    _P(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _send_debate_status(config: dict[str, Any], message: str) -> None:
    """Sendet ein Status-Update an den aktuellen Chat (Matrix/Discord), falls verfügbar."""
    chat_ctx = config.get("_chat_context") or {}
    platform = chat_ctx.get("platform")
    room_id = chat_ctx.get("room_id")
    channel_id = chat_ctx.get("channel_id")
    try:
        if platform == "matrix" and room_id:
            from miniassistant.matrix_bot import send_message_to_room
            send_message_to_room(room_id, message)
        elif platform == "discord" and channel_id:
            from miniassistant.discord_bot import send_message_to_channel
            send_message_to_channel(channel_id, message)
    except Exception:
        pass


def _set_debate_typing(config: dict[str, Any]) -> None:
    """Setzt den Typing-Indikator für Matrix/Discord während der Debatte.
    Wird vor jedem Subagent-Call aufgerufen, damit der User sieht dass gearbeitet wird."""
    chat_ctx = config.get("_chat_context") or {}
    platform = chat_ctx.get("platform")
    room_id = chat_ctx.get("room_id")
    channel_id = chat_ctx.get("channel_id")
    try:
        if platform == "matrix" and room_id:
            from miniassistant.matrix_bot import set_typing
            set_typing(room_id, True)
        elif platform == "discord" and channel_id:
            from miniassistant.discord_bot import set_channel_typing
            set_channel_typing(channel_id)
    except Exception:
        pass


def _debate_call(
    config: dict[str, Any],
    model_name: str,
    system: str,
    message: str,
) -> str:
    """Einzelner Debattenzug: ruft ein Subagent-Modell MIT Tools (web_search, exec, check_url) auf.

    Nutzt die gleiche Dispatch-Logik wie invoke_model, damit Debattierer
    bei aktuellen Themen (Wetter, News, …) eine Web-Suche machen können.
    """
    from datetime import datetime as _dt_sub
    from miniassistant.agent_loader import _is_root

    # Datum an System-Prompt anhängen (damit Modell Web-Ergebnisse zeitlich einordnen kann)
    _today = _dt_sub.now().strftime("%B %d, %Y")
    enriched_system = system + f"\n\nToday is **{_today}**. Use `web_search` if you need current facts."
    if _is_root():
        enriched_system += "\nRunning as **root** — no sudo needed."
    _ws = (config.get("workspace") or "").strip()
    if _ws:
        enriched_system += f"\nWorking directory: `{_ws}`."

    provider_type = get_provider_type(config, model_name)
    _, api_model = get_provider_config(config, model_name)
    api_model = api_model or model_name

    try:
        if provider_type == "claude-code":
            return _run_subagent_claude_code(config, api_model, enriched_system, message, model_name)
        elif provider_type == "anthropic":
            base_url = get_base_url_for_model(config, model_name)
            api_key = get_api_key_for_model(config, model_name)
            think = get_think_for_model(config, model_name)
            return _run_subagent_anthropic(config, api_model, enriched_system, message, api_key, base_url, think, model_name)
        elif provider_type == "google":
            return _run_subagent_google(config, api_model, enriched_system, message, model_name)
        elif provider_type in ("openai", "deepseek"):
            return _run_subagent_openai(config, api_model, enriched_system, message, model_name)
        else:
            # Ollama (default)
            base_url = get_base_url_for_model(config, model_name)
            api_key = get_api_key_for_model(config, model_name)
            think = get_think_for_model(config, model_name)
            options = get_options_for_model(config, model_name)
            from miniassistant.ollama_client import get_subagent_tools_schema
            sub_tools = get_subagent_tools_schema(config)
            if not model_supports_tools(base_url, api_model):
                _log.info("Debate model %s: no tool support, calling without tools", model_name)
                sub_tools = []
            return _run_subagent_with_tools(
                config, base_url, api_model, enriched_system, message,
                sub_tools, think, options, api_key, model_name,
            )
    except Exception as e:
        _log.warning("Debate call failed (%s): %s", model_name, e)
        return f"(Fehler: {e})"


def _debate_summarize(
    config: dict[str, Any],
    model_name: str,
    text: str,
    language: str = "Deutsch",
) -> str:
    """Fasst einen Debattenverlauf kurz zusammen (für Kontextweitergabe an kleine Modelle)."""
    system = (
        f"Du bist ein neutraler Zusammenfasser. Fasse den Debattenverlauf kurz und präzise zusammen. "
        f"Max 150 Wörter. Nur die Zusammenfassung, keine Einleitung. Sprache: {language}"
    )
    try:
        r = _dispatch_chat(
            config, model_name,
            [{"role": "user", "content": text}],
            system=system,
            timeout=60.0,
        )
        return ((r.get("message") or {}).get("content") or "").strip() or "(Keine Zusammenfassung)"
    except Exception as e:
        _log.warning("Debate summary failed: %s", e)
        return f"(Zusammenfassung fehlgeschlagen: {e})"


def _run_debate(
    config: dict[str, Any],
    topic: str,
    perspective_a: str,
    perspective_b: str,
    model_a_name: str,
    model_b_name: str,
    rounds: int,
    language: str,
) -> str:
    """Führt eine strukturierte Debatte zwischen zwei KI-Perspektiven durch.

    Ablauf pro Runde:
      1. Seite A argumentiert (bekommt Zusammenfassung + letztes B-Argument)
      2. Seite B antwortet (bekommt Zusammenfassung + A-Argument)
      3. Runde wird zusammengefasst → Kontext für nächste Runde
    Alles wird in eine Markdown-Datei geschrieben. Am Ende: Fazit.
    """
    import re as _re
    from pathlib import Path as _Path

    workspace = (config.get("workspace") or "").strip()
    if not workspace:
        return "debate requires a configured workspace directory"

    # Modelle auflösen
    resolved_a = resolve_model(config, model_a_name) or model_a_name
    resolved_b = resolve_model(config, model_b_name) or model_b_name

    # Debattendatei anlegen
    slug = _re.sub(r'[^a-z0-9]+', '-', topic.lower().strip())[:40].strip('-') or "debate"
    ts = int(time.time())
    debate_file = _Path(workspace) / f"debate-{slug}-{ts}.md"

    header = (
        f"# Debatte: {topic}\n\n"
        f"- **Seite A:** {perspective_a} (Modell: `{resolved_a}`)\n"
        f"- **Seite B:** {perspective_b} (Modell: `{resolved_b}`)\n"
        f"- **Runden:** {rounds}\n"
        f"- **Sprache:** {language}\n\n---\n\n"
    )
    debate_file.write_text(header, encoding="utf-8")

    _aal.log_debate_start(config, topic, perspective_a, perspective_b, resolved_a, resolved_b, rounds)

    # System-Prompts für die Debattierer
    system_a = (
        f"Du bist Debattierer A in einer strukturierten Debatte.\n"
        f"Deine Position: **{perspective_a}**\n"
        f"Thema: {topic}\n\n"
        f"Regeln:\n"
        f"- Argumentiere überzeugend für deine Position mit Fakten und Logik\n"
        f"- Wenn Gegenargumente gegeben werden, gehe direkt darauf ein\n"
        f"- Bringe in jeder Runde mindestens ein neues Argument\n"
        f"- Bleibe beim Thema, keine Abschweifungen\n"
        f"- Maximal 300 Wörter pro Argument\n"
        f"- Sprache: {language}\n"
        f"- Gib NUR dein Argument aus, keine Meta-Kommentare wie 'Als Debattierer A...'"
    )
    system_b = (
        f"Du bist Debattierer B in einer strukturierten Debatte.\n"
        f"Deine Position: **{perspective_b}**\n"
        f"Thema: {topic}\n\n"
        f"Regeln:\n"
        f"- Argumentiere überzeugend für deine Position mit Fakten und Logik\n"
        f"- Wenn Gegenargumente gegeben werden, gehe direkt darauf ein\n"
        f"- Bringe in jeder Runde mindestens ein neues Argument\n"
        f"- Bleibe beim Thema, keine Abschweifungen\n"
        f"- Maximal 300 Wörter pro Argument\n"
        f"- Sprache: {language}\n"
        f"- Gib NUR dein Argument aus, keine Meta-Kommentare wie 'Als Debattierer B...'"
    )

    summary_so_far = ""
    last_b_argument = ""
    rounds_completed = 0

    for round_num in range(1, rounds + 1):
        # Cancellation prüfen
        cancel_user = (config.get("_chat_context") or {}).get("user_id")
        if cancel_user:
            from miniassistant.cancellation import check_cancel
            if check_cancel(cancel_user):
                _append_to_file(debate_file, f"\n---\n\n*Debatte abgebrochen in Runde {round_num}.*\n")
                _aal.log_debate_end(config, topic, rounds_completed, str(debate_file))
                return f"Debatte abgebrochen in Runde {round_num}. Datei: `{debate_file}`"

        # Status-Update senden
        _send_debate_status(config, f"🗣️ Debatte Runde {round_num}/{rounds} …")

        # --- Seite A argumentiert ---
        if round_num == 1:
            msg_a = (
                f"Eröffne die Debatte zum Thema: {topic}\n"
                f"Deine Position: {perspective_a}\n"
                f"Bringe dein stärkstes Eröffnungsargument."
            )
        else:
            msg_a = (
                f"Debatte Runde {round_num}/{rounds}.\n"
                f"Bisheriger Verlauf (Zusammenfassung):\n{summary_so_far}\n\n"
                f"Letzte Antwort von Seite B ({perspective_b}):\n{last_b_argument}\n\n"
                f"Antworte auf die Argumente von Seite B und bringe neue Punkte für deine Position."
            )

        _set_debate_typing(config)
        response_a = _debate_call(config, resolved_a, system_a, msg_a)
        _append_to_file(debate_file, f"## Runde {round_num} — Seite A: {perspective_a}\n\n{response_a}\n\n")
        _aal.log_debate_round(config, round_num, "A", resolved_a, response_a)

        # Cancellation zwischen A und B prüfen
        if cancel_user:
            from miniassistant.cancellation import check_cancel
            if check_cancel(cancel_user):
                _append_to_file(debate_file, f"\n---\n\n*Debatte abgebrochen nach Seite A, Runde {round_num}.*\n")
                _aal.log_debate_end(config, topic, rounds_completed, str(debate_file))
                return f"Debatte abgebrochen in Runde {round_num}. Datei: `{debate_file}`"

        # --- Seite B antwortet ---
        msg_b = f"Debatte Runde {round_num}/{rounds}.\n"
        if summary_so_far:
            msg_b += f"Bisheriger Verlauf (Zusammenfassung):\n{summary_so_far}\n\n"
        msg_b += (
            f"Aktuelles Argument von Seite A ({perspective_a}):\n{response_a}\n\n"
            f"Antworte auf die Argumente von Seite A und bringe Punkte für deine Position."
        )

        _set_debate_typing(config)
        response_b = _debate_call(config, resolved_b, system_b, msg_b)
        _append_to_file(debate_file, f"## Runde {round_num} — Seite B: {perspective_b}\n\n{response_b}\n\n---\n\n")
        _aal.log_debate_round(config, round_num, "B", resolved_b, response_b)

        last_b_argument = response_b
        rounds_completed = round_num

        # --- Runde zusammenfassen (immer — wird auch fürs Fazit benötigt) ---
        round_text = (
            f"Runde {round_num}:\n"
            f"Seite A ({perspective_a}): {response_a[:600]}\n"
            f"Seite B ({perspective_b}): {response_b[:600]}"
        )
        round_summary = _debate_summarize(config, resolved_a, round_text, language)
        summary_so_far = (summary_so_far + f"\n{round_summary}").strip() if summary_so_far else round_summary

    # --- Fazit generieren ---
    _send_debate_status(config, "📝 Debatte abgeschlossen — erstelle Fazit …")
    _set_debate_typing(config)
    # Fazit bekommt Zusammenfassung UND die letzten Original-Argumente
    conclusion_prompt = (
        f"Fasse diese Debatte zusammen und bewerte die Argumente beider Seiten neutral.\n"
        f"Was waren die stärksten Argumente? Wo gab es Übereinstimmungen, wo Differenzen?\n"
        f"Sprache: {language}\n\n"
        f"Thema: {topic}\n"
        f"Seite A ({perspective_a}) vs. Seite B ({perspective_b})\n\n"
        f"Debattenverlauf:\n{summary_so_far}\n\n"
        f"Letzte Argumente (Runde {rounds_completed}):\n"
        f"Seite A: {response_a[:800]}\n"
        f"Seite B: {last_b_argument[:800]}"
    )
    conclusion_system = (
        f"Du bist ein neutraler Moderator. Fasse die Debatte fair zusammen. "
        f"Bewerte die Qualität der Argumente beider Seiten. Sprache: {language}"
    )
    try:
        r = _dispatch_chat(
            config, resolved_a,
            [{"role": "user", "content": conclusion_prompt}],
            system=conclusion_system,
            timeout=90.0,
        )
        conclusion = ((r.get("message") or {}).get("content") or "").strip() or "(Kein Fazit generiert)"
    except Exception as e:
        conclusion = f"(Fazit-Generierung fehlgeschlagen: {e})"

    _append_to_file(debate_file, f"## Fazit\n\n{conclusion}\n")
    _aal.log_debate_end(config, topic, rounds_completed, str(debate_file))

    return (
        f"Debatte abgeschlossen ({rounds_completed} Runden).\n"
        f"Transkript: `{debate_file}`\n\n"
        f"## Zusammenfassung\n{conclusion}"
    )


def _run_subagent_claude_code(
    config: dict[str, Any],
    api_model: str,
    system: str,
    user_msg: str,
    resolved_name: str,
) -> str:
    """Führt einen Subagent-Call über Claude Code CLI aus.
    Claude Code hat eigene Tools (exec, web_search, etc.) — wir delegieren komplett.
    max_turns=3 begrenzt die Agentic-Runden in Claude Code selbst."""
    from miniassistant.claude_client import chat as claude_chat, is_available as claude_available
    if not claude_available():
        err = (
            "Claude Code CLI nicht verfügbar. Installieren: "
            "npm install -g @anthropic-ai/claude-code && claude login"
        )
        _aal.log_subagent_result(config, resolved_name, err, "")
        return err
    # Claude Code bekommt System-Prompt + Aufgabe, handled Tools selbst
    # model=None nutzt das Default-Modell von Claude Code, sonst den konfigurierten
    model_arg = api_model if api_model and api_model != resolved_name else None
    r = claude_chat(
        user_msg,
        system=system,
        model=model_arg,
        max_turns=3,
        timeout=300,
    )
    content = (r.get("message") or {}).get("content", "").strip()
    result = content or "(Keine Antwort)"
    _aal.log_subagent_result(config, resolved_name, result, "")
    return result


def _run_subagent_anthropic(
    config: dict[str, Any],
    api_model: str,
    system: str,
    user_msg: str,
    api_key: str | None,
    base_url: str | None,
    think: bool | None,
    resolved_name: str,
) -> str:
    """Führt einen Subagent-Call über die Anthropic Messages API aus.
    Mit Tool-Support (exec, web_search, check_url). Max 15 Tool-Runden."""
    from miniassistant.claude_client import api_chat, ANTHROPIC_API_URL
    from miniassistant.ollama_client import get_subagent_tools_schema
    if not api_key:
        err = "Anthropic API: api_key erforderlich (in Provider-Config setzen)"
        _aal.log_subagent_result(config, resolved_name, err, "")
        return err
    sub_tools = get_subagent_tools_schema(config)
    msgs: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    total_content = ""
    total_thinking = ""
    _ALLOWED_SUB_TOOLS = {"exec", "web_search", "check_url", "read_url"}
    rounds_used = 0
    for _round in range(15):
        try:
            r = api_chat(
                msgs, api_key=api_key, model=api_model,
                system=system, thinking=think, tools=sub_tools,
                base_url=base_url or ANTHROPIC_API_URL,
            )
        except Exception as e:
            total_content += f"[Anthropic API error: {e}]"
            break
        msg = r.get("message") or {}
        if msg.get("thinking"):
            total_thinking += msg["thinking"]
        if msg.get("content"):
            total_content += msg["content"]
        tool_calls = _extract_tool_calls(msg)
        if not tool_calls:
            break
        msgs.append(msg)
        # Cancellation check for Anthropic subagent tool rounds
        _cancel_user = (config.get("_chat_context") or {}).get("user_id")
        if _cancel_user:
            from miniassistant.cancellation import check_cancel
            if check_cancel(_cancel_user):
                _log.info("Subagent %s (anthropic): cancellation detected — aborting after round %d", resolved_name, rounds_used)
                if not total_content.strip():
                    total_content = "(Subagent abgebrochen)"
                break
        for tc_name, tc_args in tool_calls:
            if tc_name not in _ALLOWED_SUB_TOOLS:
                tool_result = f"Tool '{tc_name}' is not available for subagents."
            elif tc_name == "exec":
                _ws = (config.get("workspace") or "").strip() or None
                _exec_r = run_exec(tc_args.get("command", ""), cwd=_ws, extra_env=_exec_env(config))
                tool_result = f"returncode: {_exec_r['returncode']}\nstdout:\n{_exec_r['stdout']}\nstderr:\n{_exec_r['stderr']}"
            elif tc_name == "web_search":
                from miniassistant.config import get_search_engine_url
                _ws_url = get_search_engine_url(config, tc_args.get("engine"))
                if not _ws_url:
                    tool_result = "web_search not configured"
                else:
                    _ws_res = tool_web_search(_ws_url, tc_args.get("query", ""))
                    if _ws_res.get("error"):
                        tool_result = f"Error: {_ws_res['error']}"
                    else:
                        _ws_lines = [f"- {_r.get('title', '')} | {_r.get('url', '')}\n  {_r.get('snippet', '')}" for _r in _ws_res.get("results") or []]
                        tool_result = "\n".join(_ws_lines) if _ws_lines else "No results"
            elif tc_name == "check_url":
                _cu_r = tool_check_url(tc_args.get("url", ""))
                _cu_parts = [f"reachable: {_cu_r.get('reachable', False)}", f"status_code: {_cu_r.get('status_code', '')}"]
                if _cu_r.get("final_url"): _cu_parts.append(f"final_url: {_cu_r['final_url']}")
                if _cu_r.get("error"): _cu_parts.append(f"error: {_cu_r['error']}")
                tool_result = "\n".join(_cu_parts)
            elif tc_name == "read_url":
                _ru_r = tool_read_url(tc_args.get("url", ""))
                tool_result = _ru_r.get("content", "") if _ru_r.get("ok") else f"Error reading URL: {_ru_r.get('error', 'unknown error')}"
            else:
                tool_result = f"Unknown tool: {tc_name}"
            _aal.log_tool_call(config, f"subagent:{tc_name}", tc_args, tool_result)
            msgs.append({"role": "tool", "tool_name": tc_name, "content": str(tool_result)})
        rounds_used += 1
    result = total_content.strip() or total_thinking.strip() or "(Keine Antwort)"
    _aal.log_subagent_result(config, resolved_name, result, total_thinking)
    return result


def _run_subagent_google(
    config: dict[str, Any],
    api_model: str,
    system: str,
    user_msg: str,
    resolved_name: str,
) -> str:
    """Führt einen Subagent-Call über die Google Gemini API aus.
    Mit Tool-Support (exec, web_search, check_url). Max 15 Tool-Runden.
    Unterstützt Image Generation bei entsprechenden Modellen."""
    from miniassistant.google_client import api_chat as google_chat, GOOGLE_API_URL, model_supports_image_generation as _google_img_gen
    from miniassistant.ollama_client import get_subagent_tools_schema
    api_key = get_api_key_for_model(config, resolved_name)
    base_url = get_base_url_for_model(config, resolved_name)
    think = get_think_for_model(config, resolved_name)
    if not api_key:
        err = "Google Gemini API: api_key erforderlich (in Provider-Config setzen)"
        _aal.log_subagent_result(config, resolved_name, err, "")
        return err
    is_img_gen = _google_img_gen(api_model)
    sub_tools = get_subagent_tools_schema(config) if not is_img_gen else []
    msgs: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    total_content = ""
    total_thinking = ""
    _ALLOWED_SUB_TOOLS = {"exec", "web_search", "check_url", "read_url"}
    rounds_used = 0
    for _round in range(15):
        try:
            r = google_chat(
                msgs, api_key=api_key, model=api_model,
                system=system, thinking=think, tools=sub_tools,
                base_url=base_url or GOOGLE_API_URL,
                image_generation=is_img_gen,
            )
        except Exception as e:
            total_content += f"[Google API error: {e}]"
            break
        msg = r.get("message") or {}
        if msg.get("thinking"):
            total_thinking += msg["thinking"]
        if msg.get("content"):
            total_content += msg["content"]
        tool_calls = _extract_tool_calls(msg)
        if not tool_calls:
            break
        msgs.append(msg)
        # Cancellation check for Google subagent tool rounds
        _cancel_user = (config.get("_chat_context") or {}).get("user_id")
        if _cancel_user:
            from miniassistant.cancellation import check_cancel
            if check_cancel(_cancel_user):
                _log.info("Subagent %s (google): cancellation detected — aborting after round %d", resolved_name, rounds_used)
                if not total_content.strip():
                    total_content = "(Subagent abgebrochen)"
                break
        for tc_name, tc_args in tool_calls:
            if tc_name not in _ALLOWED_SUB_TOOLS:
                tool_result = f"Tool '{tc_name}' is not available for subagents."
            elif tc_name == "exec":
                _ws = (config.get("workspace") or "").strip() or None
                _exec_r = run_exec(tc_args.get("command", ""), cwd=_ws, extra_env=_exec_env(config))
                tool_result = f"returncode: {_exec_r['returncode']}\nstdout:\n{_exec_r['stdout']}\nstderr:\n{_exec_r['stderr']}"
            elif tc_name == "web_search":
                from miniassistant.config import get_search_engine_url
                _ws_url = get_search_engine_url(config, tc_args.get("engine"))
                if not _ws_url:
                    tool_result = "web_search not configured"
                else:
                    _ws_res = tool_web_search(_ws_url, tc_args.get("query", ""))
                    if _ws_res.get("error"):
                        tool_result = f"Error: {_ws_res['error']}"
                    else:
                        _ws_lines = [f"- {_r.get('title', '')} | {_r.get('url', '')}\n  {_r.get('snippet', '')}" for _r in _ws_res.get("results") or []]
                        tool_result = "\n".join(_ws_lines) if _ws_lines else "No results"
            elif tc_name == "check_url":
                _cu_r = tool_check_url(tc_args.get("url", ""))
                _cu_parts = [f"reachable: {_cu_r.get('reachable', False)}", f"status_code: {_cu_r.get('status_code', '')}"]
                if _cu_r.get("final_url"): _cu_parts.append(f"final_url: {_cu_r['final_url']}")
                if _cu_r.get("error"): _cu_parts.append(f"error: {_cu_r['error']}")
                tool_result = "\n".join(_cu_parts)
            elif tc_name == "read_url":
                _ru_r = tool_read_url(tc_args.get("url", ""))
                tool_result = _ru_r.get("content", "") if _ru_r.get("ok") else f"Error reading URL: {_ru_r.get('error', 'unknown error')}"
            else:
                tool_result = f"Unknown tool: {tc_name}"
            _aal.log_tool_call(config, f"subagent:{tc_name}", tc_args, tool_result)
            msgs.append({"role": "tool", "tool_name": tc_name, "content": str(tool_result)})
        rounds_used += 1
    # Image Generation: Bilder aus Response extrahieren und speichern
    if is_img_gen and msg.get("images"):
        import base64 as _b64
        from pathlib import Path as _Path
        import time as _time
        workspace = (config.get("workspace") or "").strip()
        img_dir = _Path(workspace) / "images" if workspace else _Path("images")
        img_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        for i, img in enumerate(msg["images"]):
            mime = img.get("mime_type", "image/png")
            ext = "png" if "png" in mime else "jpg" if "jpeg" in mime or "jpg" in mime else "webp" if "webp" in mime else "png"
            ts = int(_time.time())
            fname = f"{ts}-generated-{i}.{ext}"
            fpath = img_dir / fname
            try:
                fpath.write_bytes(_b64.b64decode(img.get("data", "")))
                saved_paths.append(str(fpath))
                _log.info("Image generation: saved %s", fpath)
            except Exception as e:
                _log.warning("Image generation save failed: %s", e)
        if saved_paths:
            paths_str = ", ".join(f"`{p}`" for p in saved_paths)
            total_content += f"\n\nBild(er) gespeichert: {paths_str}"

    result = total_content.strip() or total_thinking.strip() or "(Keine Antwort)"
    _aal.log_subagent_result(config, resolved_name, result, total_thinking)
    return result


def _run_subagent_openai(
    config: dict[str, Any],
    api_model: str,
    system: str,
    user_msg: str,
    resolved_name: str,
) -> str:
    """Führt einen Subagent-Call über die OpenAI API aus.
    Mit Tool-Support (exec, web_search, check_url). Max 15 Tool-Runden.
    Unterstützt DALL-E Image Generation."""
    from miniassistant.openai_client import api_chat as openai_chat, OPENAI_API_URL, model_supports_image_generation as _oai_img_gen
    from miniassistant.ollama_client import get_subagent_tools_schema
    api_key = get_api_key_for_model(config, resolved_name)
    base_url = get_base_url_for_model(config, resolved_name)
    think = get_think_for_model(config, resolved_name)
    if not api_key:
        err = "OpenAI API: api_key erforderlich (in Provider-Config setzen)"
        _aal.log_subagent_result(config, resolved_name, err, "")
        return err

    # DALL-E: eigener Endpoint für Bildgenerierung
    if _oai_img_gen(api_model):
        try:
            from miniassistant.openai_client import api_generate_image
            import base64 as _b64
            from pathlib import Path as _Path
            import time as _time
            r = api_generate_image(
                user_msg, api_key=api_key, model=api_model,
                base_url=base_url or OPENAI_API_URL,
            )
            workspace = (config.get("workspace") or "").strip()
            img_dir = _Path(workspace) / "images" if workspace else _Path("images")
            img_dir.mkdir(parents=True, exist_ok=True)
            ts = int(_time.time())
            fpath = img_dir / f"{ts}-generated-0.png"
            b64_data = r.get("b64_json", "")
            if b64_data:
                fpath.write_bytes(_b64.b64decode(b64_data))
                _log.info("DALL-E image generation: saved %s", fpath)
                result = f"Bild generiert und gespeichert: `{fpath}`"
                if r.get("revised_prompt"):
                    result += f"\n\nRevisierter Prompt: {r['revised_prompt']}"
            else:
                result = f"Bild-URL: {r.get('url', '(keine)')}"
            _aal.log_subagent_result(config, resolved_name, result, "")
            return result
        except Exception as e:
            err = f"DALL-E Image Generation Fehler: {e}"
            _aal.log_subagent_result(config, resolved_name, err, "")
            return err

    sub_tools = get_subagent_tools_schema(config)
    msgs: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    total_content = ""
    total_thinking = ""
    _ALLOWED_SUB_TOOLS = {"exec", "web_search", "check_url", "read_url"}
    rounds_used = 0
    for _round in range(15):
        try:
            r = openai_chat(
                msgs, api_key=api_key, model=api_model,
                system=system, thinking=think, tools=sub_tools,
                base_url=base_url or OPENAI_API_URL,
            )
        except Exception as e:
            total_content += f"[OpenAI API error: {e}]"
            break
        msg = r.get("message") or {}
        if msg.get("thinking"):
            total_thinking += msg["thinking"]
        if msg.get("content"):
            total_content += msg["content"]
        tool_calls = _extract_tool_calls(msg)
        if not tool_calls:
            break
        msgs.append(msg)
        # Cancellation check for OpenAI subagent tool rounds
        _cancel_user = (config.get("_chat_context") or {}).get("user_id")
        if _cancel_user:
            from miniassistant.cancellation import check_cancel
            if check_cancel(_cancel_user):
                _log.info("Subagent %s (openai): cancellation detected — aborting after round %d", resolved_name, rounds_used)
                if not total_content.strip():
                    total_content = "(Subagent abgebrochen)"
                break
        for tc_name, tc_args in tool_calls:
            if tc_name not in _ALLOWED_SUB_TOOLS:
                tool_result = f"Tool '{tc_name}' is not available for subagents."
            elif tc_name == "exec":
                _ws = (config.get("workspace") or "").strip() or None
                _exec_r = run_exec(tc_args.get("command", ""), cwd=_ws, extra_env=_exec_env(config))
                tool_result = f"returncode: {_exec_r['returncode']}\nstdout:\n{_exec_r['stdout']}\nstderr:\n{_exec_r['stderr']}"
            elif tc_name == "web_search":
                from miniassistant.config import get_search_engine_url
                _ws_url = get_search_engine_url(config, tc_args.get("engine"))
                if not _ws_url:
                    tool_result = "web_search not configured"
                else:
                    _ws_res = tool_web_search(_ws_url, tc_args.get("query", ""))
                    if _ws_res.get("error"):
                        tool_result = f"Error: {_ws_res['error']}"
                    else:
                        _ws_lines = [f"- {_r.get('title', '')} | {_r.get('url', '')}\n  {_r.get('snippet', '')}" for _r in _ws_res.get("results") or []]
                        tool_result = "\n".join(_ws_lines) if _ws_lines else "No results"
            elif tc_name == "check_url":
                _cu_r = tool_check_url(tc_args.get("url", ""))
                _cu_parts = [f"reachable: {_cu_r.get('reachable', False)}", f"status_code: {_cu_r.get('status_code', '')}"]
                if _cu_r.get("final_url"): _cu_parts.append(f"final_url: {_cu_r['final_url']}")
                if _cu_r.get("error"): _cu_parts.append(f"error: {_cu_r['error']}")
                tool_result = "\n".join(_cu_parts)
            elif tc_name == "read_url":
                _ru_r = tool_read_url(tc_args.get("url", ""))
                tool_result = _ru_r.get("content", "") if _ru_r.get("ok") else f"Error reading URL: {_ru_r.get('error', 'unknown error')}"
            else:
                tool_result = f"Unknown tool: {tc_name}"
            _aal.log_tool_call(config, f"subagent:{tc_name}", tc_args, tool_result)
            msgs.append({"role": "tool", "tool_name": tc_name, "content": str(tool_result)})
        rounds_used += 1
    result = total_content.strip() or total_thinking.strip() or "(Keine Antwort)"
    _aal.log_subagent_result(config, resolved_name, result, total_thinking)
    return result


def _run_subagent_with_tools(
    config: dict[str, Any],
    base_url: str,
    api_model: str,
    system: str,
    user_msg: str,
    tools: list[dict[str, Any]],
    think: bool | None,
    options: dict[str, Any] | None,
    api_key: str | None,
    resolved_name: str,
    max_rounds: int = 15,
) -> str:
    """Führt einen Subagent-Call mit eigener Tool-Loop aus (exec, web_search, check_url).
    Kein save_config, schedule, invoke_model. Max 15 Tool-Runden + Nudge bei leerem Content."""
    msgs: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    total_content = ""
    total_thinking = ""
    # Erlaubte Tools für Subagents (kein save_config, schedule, invoke_model)
    _ALLOWED_SUB_TOOLS = {"exec", "web_search", "check_url", "read_url"}
    rounds_used = 0
    for _round in range(max_rounds):
        r = None
        for _attempt in range(3):
            try:
                r = ollama_chat(
                    base_url,
                    msgs,
                    model=api_model,
                    system=system,
                    think=think,
                    tools=tools,
                    options=options or None,
                    api_key=api_key,
                    timeout=300.0,
                )
                break
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RemoteProtocolError) as e:
                if _attempt < 2:
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    if isinstance(e, (httpx.TimeoutException, httpx.RemoteProtocolError)) or code == 400:
                        _log.warning("Subagent %s: API call failed (attempt %d/3): %s — retrying", resolved_name, _attempt + 1, e)
                        time.sleep(2)
                        continue
                raise
        if r is None:
            total_content += "[API error: all retries failed]"
            break
        # API-Fehler abfangen (Ollama gibt {"error": "..."} bei Problemen)
        if r.get("error"):
            total_content += f"[API error: {r['error']}]"
            break
        msg = r.get("message") or {}
        if msg.get("thinking"):
            total_thinking += msg["thinking"]
        if msg.get("content"):
            total_content += msg["content"]
        tool_calls = _extract_tool_calls(msg)
        if not tool_calls:
            break
        # Tool-Calls ausführen (nur erlaubte)
        msgs.append(msg)
        # Cancellation check for subagent tool rounds
        _cancel_user = (config.get("_chat_context") or {}).get("user_id")
        if _cancel_user:
            from miniassistant.cancellation import check_cancel
            if check_cancel(_cancel_user):
                _log.info("Subagent %s: cancellation detected — aborting after round %d", resolved_name, rounds_used)
                if not total_content.strip():
                    total_content = "(Subagent abgebrochen)"
                break
        for tc_name, tc_args in tool_calls:
            if tc_name not in _ALLOWED_SUB_TOOLS:
                tool_result = f"Tool '{tc_name}' is not available for subagents."
            elif tc_name == "exec":
                _ws = (config.get("workspace") or "").strip() or None
                _exec_r = run_exec(tc_args.get("command", ""), cwd=_ws, extra_env=_exec_env(config))
                tool_result = f"returncode: {_exec_r['returncode']}\nstdout:\n{_exec_r['stdout']}\nstderr:\n{_exec_r['stderr']}"
            elif tc_name == "web_search":
                from miniassistant.config import get_search_engine_url
                _ws_url = get_search_engine_url(config, tc_args.get("engine"))
                if not _ws_url:
                    tool_result = "web_search not configured (no search_engines or invalid engine)"
                else:
                    _ws_res = tool_web_search(_ws_url, tc_args.get("query", ""))
                    if _ws_res.get("error"):
                        tool_result = f"Error: {_ws_res['error']}"
                    else:
                        _ws_lines = []
                        for _r in _ws_res.get("results") or []:
                            _ws_lines.append(f"- {_r.get('title', '')} | {_r.get('url', '')}\n  {_r.get('snippet', '')}")
                        tool_result = "\n".join(_ws_lines) if _ws_lines else "No results"
            elif tc_name == "check_url":
                _cu_r = tool_check_url(tc_args.get("url", ""))
                _cu_parts = [f"reachable: {_cu_r.get('reachable', False)}", f"status_code: {_cu_r.get('status_code', '')}"]
                if _cu_r.get("final_url"): _cu_parts.append(f"final_url: {_cu_r['final_url']}")
                if _cu_r.get("error"): _cu_parts.append(f"error: {_cu_r['error']}")
                tool_result = "\n".join(_cu_parts)
            elif tc_name == "read_url":
                _ru_r = tool_read_url(tc_args.get("url", ""))
                tool_result = _ru_r.get("content", "") if _ru_r.get("ok") else f"Error reading URL: {_ru_r.get('error', 'unknown error')}"
            else:
                tool_result = f"Unknown tool: {tc_name}"
            _aal.log_tool_call(config, f"subagent:{tc_name}", tc_args, tool_result)
            msgs.append({"role": "tool", "content": str(tool_result)})
        rounds_used += 1
    # Stuck-Prevention: wenn nach Tool-Runden kein Content, Nudge senden
    if not total_content.strip() and rounds_used > 0:
        _log.info("Subagent %s: empty content after %d tool rounds — sending nudge", resolved_name, rounds_used)
        msgs.append({"role": "user", "content": "You have not provided a text response yet. Please summarize your findings and give your final answer now."})
        try:
            nudge_r = ollama_chat(
                base_url, msgs, model=api_model, system=system,
                think=think, options=options or None, api_key=api_key,
            )
            nudge_msg = nudge_r.get("message") or {}
            if nudge_msg.get("thinking"):
                total_thinking += nudge_msg["thinking"]
            if nudge_msg.get("content"):
                total_content += nudge_msg["content"]
        except Exception as nudge_err:
            _log.warning("Subagent nudge failed (%s): %s", resolved_name, nudge_err)
    result = total_content.strip()
    if not result and total_thinking.strip():
        result = total_thinking.strip()
    result = result or "(Keine Antwort)"
    _aal.log_subagent_result(config, resolved_name, result, total_thinking)
    return result


def _extract_tool_calls(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Extrahiert (name, arguments) aus message.tool_calls.
    Fallback: Parst <tool_call> XML-Tags aus message.content (manche Modelle wie qwen3 geben Tool-Calls als Text aus)."""
    out = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append((name, args))
    # Fallback: <tool_call> XML im Content parsen (qwen3, deepseek, etc.)
    if not out:
        content = message.get("content") or ""
        if "<tool_call>" in content:
            import re as _re
            for m in _re.finditer(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', content, _re.DOTALL):
                try:
                    obj = json.loads(m.group(1))
                    name = obj.get("name", "")
                    args = obj.get("arguments") or obj.get("parameters") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if name and isinstance(args, dict):
                        out.append((name, args))
                        _log.info("Tool-Call aus <tool_call> XML extrahiert: %s", name)
                except json.JSONDecodeError:
                    pass
    return out


def chat_round(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    system_prompt: str,
    model: str,
    user_content: str,
    project_dir: str | None = None,
    *,
    max_tool_rounds: int | None = None,
    images: list[dict[str, Any]] | None = None,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any] | None, dict[str, str] | None]:
    """
    Eine Runde: user_content anhängen, Ollama aufrufen, bei tool_calls ausführen und wiederholen.
    Bei Fehler (z.B. 400, Timeout) werden models.fallbacks nacheinander versucht.
    Gibt (content, thinking, new_messages, debug_info, switch_info) zurück.
    switch_info = {"model": str, "reason": str} wenn auf Fallback gewechselt wurde.
    """
    tools_schema = get_tools_schema(config)
    debug = (config.get("server") or {}).get("debug", False)
    models_cfg = config.get("models") or {}
    per_prov_fb = [resolve_model(config, fb) or fb for fb in (models_cfg.get("fallbacks") or []) if fb]
    global_fb = [resolve_model(config, fb) or fb for fb in (config.get("fallbacks") or []) if fb]
    fallbacks = per_prov_fb + [m for m in global_fb if m not in per_prov_fb]
    if max_tool_rounds is None:
        max_tool_rounds = int(config.get("max_tool_rounds", 15))

    models_to_try = [model] + [m for m in fallbacks if m and m != model]

    total_thinking = ""
    total_content = ""
    msgs_final: list[dict[str, Any]] = []
    last_response: dict[str, Any] = {}
    last_msgs_before_call: list[dict[str, Any]] = []
    effective_model = model
    switch_info: dict[str, str] | None = None
    last_error: Exception | None = None

    # Smart Compacting: History zusammenfassen wenn Quota überschritten
    compacted_messages = list(messages)
    _compact_num_ctx = get_num_ctx_for_model(config, model)
    if _needs_compacting(config, system_prompt, compacted_messages, tools_schema, _compact_num_ctx):
        compacted_messages = _compact_history(config, compacted_messages, model, system_prompt, tools_schema, _compact_num_ctx)

    for try_model in models_to_try:
        # Provider-Präfix auflösen: base_url + clean model name + api_key für API
        base_url = get_base_url_for_model(config, try_model)
        model_api_key = get_api_key_for_model(config, try_model)
        _, api_model = get_provider_config(config, try_model)
        api_model = api_model or try_model
        msgs = list(compacted_messages)
        user_msg: dict[str, Any] = {"role": "user", "content": user_content}
        if images:
            user_msg["images"] = images
        msgs.append(user_msg)
        total_thinking = ""
        total_content = ""
        _sent_image = False
        rounds = 0
        think = get_think_for_model(config, try_model)
        try:
            while rounds < max_tool_rounds:
                last_msgs_before_call = list(msgs)
                options = get_options_for_model(config, try_model)
                tools = tools_schema if _provider_supports_tools(config, try_model) else []
                system_effective = system_prompt
                if not tools:
                    system_effective = (
                        system_prompt
                        + "\n\n[Wichtig: Diesem Modell stehen keine Tools (exec, schedule, web_search) zur Verfügung. Antworte nur mit Text; schlage keine Tool-Aufrufe oder konkreten schedule/exec-Beispiele vor.]"
                    )
                num_ctx = get_num_ctx_for_model(config, try_model)
                msgs = _trim_messages_to_fit(system_effective, msgs, num_ctx, reserve_tokens=1024, tools=tools)
                last_msgs_before_call = list(msgs)
                if rounds == 0:
                    _log_estimated_tokens(config, system_effective, msgs, tools)
                    _aal.log_prompt(config, try_model, user_content, len(system_effective), len(msgs))
                    _ctx_log.log_context(config, try_model, system_effective, msgs, tools=tools, num_ctx=num_ctx, think=think)
                _api_timeout = float(config.get("api_timeout") or 600)
                response = None
                for attempt in range(3):
                    try:
                        response = _dispatch_chat(
                            config, try_model, msgs,
                            system=system_effective, think=think,
                            tools=tools, options=options or None,
                            timeout=_api_timeout,
                        )
                        break
                    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RemoteProtocolError, RuntimeError) as e:
                        if attempt < 2:
                            code = getattr(getattr(e, "response", None), "status_code", None)
                            if isinstance(e, (httpx.TimeoutException, httpx.RemoteProtocolError)) or code == 400:
                                time.sleep(2)
                                continue
                        raise
                if response is None:
                    raise RuntimeError("API-Aufruf fehlgeschlagen")
                last_response = response
                msg = response.get("message") or {}
                total_thinking += (msg.get("thinking") or "")
                total_content += (msg.get("content") or "")
                tool_calls = _extract_tool_calls(msg)

                if not tool_calls:
                    msgs.append({"role": "assistant", "content": msg.get("content") or "", "thinking": msg.get("thinking") or ""})
                    _aal.log_thinking(config, msg.get("thinking") or "")
                    _aal.log_response(config, msg.get("content") or "")
                    break

                msgs.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "thinking": msg.get("thinking") or "",
                    "tool_calls": response.get("message", {}).get("tool_calls") or [],
                })
                # Cancellation check between tool rounds
                _cancel_user = (config.get("_chat_context") or {}).get("user_id")
                if _cancel_user:
                    from miniassistant.cancellation import check_cancel, clear_cancel
                    _cancel_level = check_cancel(_cancel_user)
                    if _cancel_level:
                        clear_cancel(_cancel_user)
                        _log.info("Cancellation (%s) für %s — breche nach Runde %d ab", _cancel_level, _cancel_user, rounds)
                        if not total_content.strip():
                            total_content += "\n\n*(Verarbeitung abgebrochen)*"
                        else:
                            total_content += "\n\n*(Verarbeitung abgebrochen)*"
                        msgs.append({"role": "assistant", "content": total_content.strip()})
                        msgs_final = msgs
                        effective_model = try_model
                        break
                for name, args in tool_calls:
                    result = _run_tool(name, args, config, project_dir)
                    _aal.log_tool_call(config, name, args, result)
                    msgs.append({"role": "tool", "tool_name": name, "content": result})
                    if name == "send_image":
                        _sent_image = True
                rounds += 1

            # Wenn durch Cancellation abgebrochen, nicht weiter verarbeiten
            if msgs_final:
                break

            # Max-Rounds-Exhaustion: Agent wollte noch weiterarbeiten aber hat keine Runden mehr
            if rounds >= max_tool_rounds and not _sent_image:
                _log.info("Max tool rounds (%d) exhausted — sending wrap-up nudge", max_tool_rounds)
                msgs.append({"role": "user", "content": (
                    "SYSTEM: You have used ALL your tool rounds — no more tool calls are possible. "
                    "Nothing is running. No subworker is active. No background task exists. "
                    "Give your FINAL answer NOW based ONLY on results you already received. "
                    "Summarize honestly: what was completed, what is still pending. "
                    "FORBIDDEN phrases: 'still running', 'waiting for results', 'in progress', 'wartet auf', 'läuft noch', 'wird gerade'. "
                    "If the task is incomplete, say: 'Aufgabe nicht vollständig abgeschlossen. Bitte sag mir dass ich weitermachen soll.'"
                )})
                try:
                    wrapup_resp = _dispatch_chat(
                        config, try_model, msgs,
                        system=system_effective, think=think,
                        options=options or None,
                    )
                    wrapup_msg = wrapup_resp.get("message") or {}
                    total_thinking += (wrapup_msg.get("thinking") or "")
                    # Ersetze bisherigen Content komplett durch Wrap-Up (der alte Content war irreführend)
                    wrapup_content = (wrapup_msg.get("content") or "").strip()
                    if wrapup_content:
                        total_content = wrapup_content
                    msgs.append({"role": "assistant", "content": wrapup_content, "thinking": wrapup_msg.get("thinking") or ""})
                    last_response = wrapup_resp
                except Exception as wrapup_err:
                    _log.warning("Wrap-up nudge failed: %s", wrapup_err)

            # Stuck-Prevention: wenn kein Content generiert wurde, Nudge senden
            # Aber NICHT wenn send_image erfolgreich war (Bild IST die Antwort)
            elif not total_content.strip() and not _sent_image:
                _log.info("Empty response after %d rounds — sending nudge", rounds)
                msgs.append({"role": "user", "content": "You have not provided a text response yet. Please give your final answer to the user now."})
                try:
                    nudge_resp = _dispatch_chat(
                        config, try_model, msgs,
                        system=system_effective, think=think,
                        options=options or None,
                    )
                    nudge_msg = nudge_resp.get("message") or {}
                    total_thinking += (nudge_msg.get("thinking") or "")
                    total_content += (nudge_msg.get("content") or "")
                    msgs.append({"role": "assistant", "content": nudge_msg.get("content") or "", "thinking": nudge_msg.get("thinking") or ""})
                    last_response = nudge_resp
                except Exception as nudge_err:
                    _log.warning("Nudge call failed: %s", nudge_err)

            # Response loggen wenn es noch nicht innerhalb der Schleife geloggt wurde
            # (passiert wenn max_rounds erreicht oder Nudge die Antwort liefert)
            if total_content.strip() and rounds > 0:
                _aal.log_response(config, total_content.strip())

            # send_image war erfolgreich → Content unterdrücken (Bild IST die Antwort, kein Text nötig)
            if _sent_image and total_content.strip():
                _log.info("send_image erfolgreich – unterdrücke Text-Content: %.60s", total_content.strip())
                total_content = ""

            effective_model = try_model
            if try_model != model and last_error:
                reason = str(last_error)
                try:
                    if hasattr(last_error, "response") and last_error.response is not None:
                        reason = f"HTTP {last_error.response.status_code} – {reason}"
                except Exception:
                    pass
                switch_info = {"model": try_model, "reason": reason}
            elif try_model != model:
                switch_info = {"model": try_model, "reason": "Antwort ungültig oder leer"}
            msgs_final = msgs
            break
        except Exception as e:
            last_error = e
            continue

    if not msgs_final:
        if last_error:
            raise last_error
        raise RuntimeError("Kein Modell hat geantwortet.")

    debug_info: dict[str, Any] | None = None
    if debug and last_response:
        opts_debug = get_options_for_model(config, effective_model)
        num_ctx_debug = get_num_ctx_for_model(config, effective_model)
        tools_for_request = tools_schema if _provider_supports_tools(config, effective_model) else []
        context_used_estimate = (
            _estimate_tokens(system_prompt)
            + _estimate_tokens(json.dumps(tools_for_request, ensure_ascii=False))
            + _messages_token_estimate(last_msgs_before_call)
        )
        debug_info = {
            "request": {
                "model": effective_model,
                "num_ctx": num_ctx_debug,
                "context_used_estimate": context_used_estimate,
                "system": (system_prompt[:3000] + "…") if len(system_prompt) > 3000 else system_prompt,
                "messages": last_msgs_before_call,
            },
            "response": last_response,
            "message": last_response.get("message") or {},
        }
        if switch_info:
            debug_info["model_switched"] = switch_info
        try:
            from miniassistant.debug_log import log_chat
            log_chat(
                {"model": effective_model, "system": system_prompt, "messages": last_msgs_before_call, "think": think, "tools": bool(tools_schema) and _provider_supports_tools(config, effective_model), "options": opts_debug},
                last_response, config, project_dir, label="chat",
            )
        except Exception:
            pass
    return total_content.strip(), total_thinking.strip(), msgs_final, debug_info, switch_info


def chat_round_stream(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    system_prompt: str,
    model: str,
    user_content: str,
    project_dir: str | None = None,
    *,
    max_tool_rounds: int | None = None,
    images: list[dict[str, Any]] | None = None,
):
    """
    Wie chat_round, aber streamt Thinking und Content live.
    Generiert dicts: {"type": "thinking", "delta": str} | {"type": "content", "delta": str}
    | {"type": "tool_call"} | {"type": "status", "message": str}
    | {"type": "done", "thinking", "content", "new_messages", "debug_info", "switch_info"}.
    """
    tools_schema = get_tools_schema(config)
    models_cfg = config.get("models") or {}
    per_prov_fb = [resolve_model(config, fb) or fb for fb in (models_cfg.get("fallbacks") or []) if fb]
    global_fb = [resolve_model(config, fb) or fb for fb in (config.get("fallbacks") or []) if fb]
    fallbacks = per_prov_fb + [m for m in global_fb if m not in per_prov_fb]
    if max_tool_rounds is None:
        max_tool_rounds = int(config.get("max_tool_rounds", 15))
    models_to_try = [model] + [m for m in fallbacks if m and m != model]
    debug = (config.get("server") or {}).get("debug", False)
    last_response: dict[str, Any] = {}
    debug_info: dict[str, Any] | None = None
    switch_info: dict[str, str] | None = None
    effective_model = model

    msgs = list(messages)
    # Smart Compacting: History zusammenfassen wenn Quota überschritten
    _compact_num_ctx = get_num_ctx_for_model(config, model)
    if _needs_compacting(config, system_prompt, msgs, tools_schema, _compact_num_ctx):
        yield {"type": "status", "message": "Chat-Verlauf wird komprimiert…"}
        msgs = _compact_history(config, msgs, model, system_prompt, tools_schema, _compact_num_ctx)
        yield {"type": "status", "message": "Verlauf komprimiert."}
    user_msg: dict[str, Any] = {"role": "user", "content": user_content}
    if images:
        user_msg["images"] = images
    msgs.append(user_msg)
    total_thinking = ""
    total_content = ""
    _sent_image = False
    rounds = 0

    while rounds < max_tool_rounds:
        try_model = models_to_try[0] if rounds == 0 else effective_model
        effective_model = try_model
        # Provider-Präfix auflösen: base_url + clean model name + api_key für API
        base_url = get_base_url_for_model(config, try_model)
        stream_api_key = get_api_key_for_model(config, try_model)
        _, api_model = get_provider_config(config, try_model)
        api_model = api_model or try_model
        think = get_think_for_model(config, try_model)
        options = get_options_for_model(config, try_model)
        tools = tools_schema if _provider_supports_tools(config, try_model) else []
        system_effective = system_prompt
        if not tools:
            system_effective = (
                system_prompt
                + "\n\n[Wichtig: Diesem Modell stehen keine Tools (exec, schedule, web_search) zur Verfügung. Antworte nur mit Text; schlage keine Tool-Aufrufe oder konkreten schedule/exec-Beispiele vor.]"
            )
        num_ctx = get_num_ctx_for_model(config, try_model)
        msgs = _trim_messages_to_fit(system_effective, msgs, num_ctx, reserve_tokens=1024, tools=tools)
        if rounds == 0:
            _log_estimated_tokens(config, system_effective, msgs, tools)
            _aal.log_prompt(config, try_model, user_content, len(system_effective), len(msgs))
            _ctx_log.log_context(config, try_model, system_effective, msgs, tools=tools, num_ctx=num_ctx, think=think)
        round_thinking = ""
        round_content = ""
        round_tool_calls_raw: list[dict[str, Any]] = []
        try:
            for attempt in range(3):
                round_thinking = ""
                round_content = ""
                round_tool_calls_raw = []
                _stream_timeout = float(config.get("api_timeout") or 300)
                try:
                    for chunk in _dispatch_chat_stream(
                        config, try_model, msgs,
                        system=system_effective, think=think,
                        tools=tools, options=options or None,
                        timeout=_stream_timeout,
                    ):
                        msg = chunk.get("message") or {}
                        if msg.get("thinking"):
                            round_thinking += msg["thinking"]
                            yield {"type": "thinking", "delta": msg["thinking"]}
                        if msg.get("content"):
                            round_content += msg["content"]
                            yield {"type": "content", "delta": msg["content"]}
                        # Tool-Calls aus JEDEM Chunk akkumulieren – Ollama streamt sie
                        # in Zwischen-Chunks, der Done-Chunk hat sie oft NICHT mehr.
                        for tc in msg.get("tool_calls") or []:
                            round_tool_calls_raw.append(tc)
                        if chunk.get("done"):
                            last_response = chunk
                            break
                    break
                except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                    if attempt < 2:
                        code = getattr(getattr(e, "response", None), "status_code", None)
                        if isinstance(e, httpx.TimeoutException) or code == 400:
                            yield {"type": "status", "message": "Connection failed, retrying…"}
                            time.sleep(2)
                            continue
                    raise
        except Exception as e:
            yield {"type": "done", "error": str(e), "thinking": total_thinking, "content": total_content, "new_messages": msgs, "debug_info": None, "switch_info": switch_info}
            return

        total_thinking += round_thinking
        total_content += round_content

        # Tool-Calls: aus Zwischen-Chunks ODER Done-Chunk (Fallback)
        full_msg = last_response.get("message") or {}
        all_tool_calls_raw = round_tool_calls_raw or (full_msg.get("tool_calls") or [])
        tool_calls = _extract_tool_calls({"tool_calls": all_tool_calls_raw})

        if not tool_calls:
            # Content/Thinking aus den gestreamten Deltas verwenden (Done-Chunk hat oft leere Werte)
            msgs.append({"role": "assistant", "content": round_content or full_msg.get("content") or "", "thinking": round_thinking or full_msg.get("thinking") or ""})
            _aal.log_thinking(config, round_thinking or full_msg.get("thinking") or "")
            _aal.log_response(config, round_content or full_msg.get("content") or "")

            # Stuck-Prevention: wenn kein Content, Nudge senden
            # Aber NICHT wenn send_image erfolgreich war (Bild IST die Antwort)
            if not total_content.strip() and not _sent_image:
                _log.info("Empty stream response after %d rounds — sending nudge", rounds)
                msgs.append({"role": "user", "content": "You have not provided a text response yet. Please give your final answer to the user now."})
                try:
                    nudge_resp = _dispatch_chat(
                        config, try_model, msgs,
                        system=system_effective, think=think, options=options or None,
                    )
                    nudge_msg = nudge_resp.get("message") or {}
                    total_thinking += (nudge_msg.get("thinking") or "")
                    total_content += (nudge_msg.get("content") or "")
                    msgs.append({"role": "assistant", "content": nudge_msg.get("content") or "", "thinking": nudge_msg.get("thinking") or ""})
                    if nudge_msg.get("content"):
                        yield {"type": "content", "delta": nudge_msg["content"]}
                except Exception as nudge_err:
                    _log.warning("Stream nudge failed: %s", nudge_err)

            if debug and last_response:
                opts_debug = get_options_for_model(config, try_model)
                context_used_estimate = (
                    _estimate_tokens(system_effective)
                    + _estimate_tokens(json.dumps(tools or [], ensure_ascii=False))
                    + _messages_token_estimate(msgs)
                )
                debug_info = {
                    "request": {
                        "model": try_model,
                        "num_ctx": num_ctx,
                        "context_used_estimate": context_used_estimate,
                        "messages": msgs[:-1],
                    },
                    "response": last_response,
                    "message": full_msg,
                }
            # send_image war erfolgreich → Content unterdrücken (Bild IST die Antwort)
            _done_content = "" if _sent_image else total_content.strip()
            yield {"type": "done", "thinking": total_thinking.strip(), "content": _done_content, "new_messages": msgs, "debug_info": debug_info, "switch_info": switch_info}
            return

        _tool_names = [n for n, _ in tool_calls]
        yield {"type": "tool_call", "tools": _tool_names}
        msgs.append({
            "role": "assistant",
            "content": round_content or full_msg.get("content") or "",
            "thinking": round_thinking or full_msg.get("thinking") or "",
            "tool_calls": all_tool_calls_raw,
        })
        # Cancellation check between tool rounds (stream)
        _cancel_user = (config.get("_chat_context") or {}).get("user_id")
        if _cancel_user:
            from miniassistant.cancellation import check_cancel, clear_cancel
            _cancel_level = check_cancel(_cancel_user)
            if _cancel_level:
                clear_cancel(_cancel_user)
                _log.info("Stream cancellation (%s) für %s — breche nach Runde %d ab", _cancel_level, _cancel_user, rounds)
                total_content += "\n\n*(Verarbeitung abgebrochen)*"
                msgs.append({"role": "assistant", "content": total_content.strip()})
                _final_content = "" if _sent_image else total_content.strip()
                yield {"type": "content", "delta": "\n\n*(Verarbeitung abgebrochen)*"}
                yield {"type": "done", "thinking": total_thinking.strip(), "content": _final_content, "new_messages": msgs, "debug_info": debug_info, "switch_info": switch_info}
                return
        for name, args in tool_calls:
            yield {"type": "status", "message": f"Tool: {name}"}
            result = _run_tool(name, args, config, project_dir)
            _aal.log_tool_call(config, name, args, result)
            msgs.append({"role": "tool", "tool_name": name, "content": result})
            if name == "send_image":
                _sent_image = True
        rounds += 1

    # Max-Rounds-Exhaustion: Agent wollte noch weiterarbeiten aber hat keine Runden mehr
    if rounds >= max_tool_rounds and not _sent_image:
        _log.info("Stream: Max tool rounds (%d) exhausted — sending wrap-up nudge", max_tool_rounds)
        msgs.append({"role": "user", "content": (
            "SYSTEM: You have used ALL your tool rounds — no more tool calls are possible. "
            "Nothing is running. No subworker is active. No background task exists. "
            "Give your FINAL answer NOW based ONLY on results you already received. "
            "Summarize honestly: what was completed, what is still pending. "
            "FORBIDDEN phrases: 'still running', 'waiting for results', 'in progress', 'wartet auf', 'läuft noch', 'wird gerade'. "
            "If the task is incomplete, say: 'Aufgabe nicht vollständig abgeschlossen. Bitte sag mir dass ich weitermachen soll.'"
        )})
        try:
            wrapup_resp = _dispatch_chat(
                config, effective_model, msgs,
                system=system_effective, think=think, options=options or None,
            )
            wrapup_msg = wrapup_resp.get("message") or {}
            total_thinking += (wrapup_msg.get("thinking") or "")
            wrapup_content = (wrapup_msg.get("content") or "").strip()
            if wrapup_content:
                total_content = wrapup_content
                yield {"type": "content", "delta": wrapup_content}
            msgs.append({"role": "assistant", "content": wrapup_content, "thinking": wrapup_msg.get("thinking") or ""})
        except Exception as wrapup_err:
            _log.warning("Stream wrap-up nudge failed: %s", wrapup_err)
    # Stuck-Prevention: wenn kein Content generiert wurde, Nudge senden
    # Aber NICHT wenn send_image erfolgreich war (Bild IST die Antwort)
    elif not total_content.strip() and not _sent_image:
        _log.info("Empty stream response after max rounds — sending nudge")
        msgs.append({"role": "user", "content": "You have not provided a text response yet. Please give your final answer to the user now."})
        try:
            nudge_resp = _dispatch_chat(
                config, effective_model, msgs,
                system=system_effective, think=think, options=options or None,
            )
            nudge_msg = nudge_resp.get("message") or {}
            total_thinking += (nudge_msg.get("thinking") or "")
            total_content += (nudge_msg.get("content") or "")
            msgs.append({"role": "assistant", "content": nudge_msg.get("content") or "", "thinking": nudge_msg.get("thinking") or ""})
            if nudge_msg.get("content"):
                yield {"type": "content", "delta": nudge_msg["content"]}
        except Exception as nudge_err:
            _log.warning("Stream nudge (max rounds) failed: %s", nudge_err)

    # Response loggen wenn es noch nicht innerhalb der Schleife geloggt wurde
    if total_content.strip():
        _aal.log_response(config, total_content.strip())

    # send_image war erfolgreich → Content unterdrücken (Bild IST die Antwort)
    _final_content = "" if _sent_image else total_content.strip()
    yield {"type": "done", "thinking": total_thinking.strip(), "content": _final_content, "new_messages": msgs, "debug_info": debug_info, "switch_info": switch_info}


def is_chat_command(user_input: str) -> bool:
    """True wenn die Eingabe ein Befehl ist (/model, /models, /auth, /new), der ohne Stream behandelt wird."""
    raw = (user_input or "").strip()
    if not raw:
        return True
    if raw.lower() == "/model":
        return True
    if parse_model_switch(raw)[0] is not None:
        return True
    if parse_models_command(raw)[0]:
        return True
    if raw.lower() == "/new":
        return True
    if raw.lower() == "/schedules":
        return True
    if raw.lower().startswith("/schedule remove "):
        return True
    if raw.startswith("/auth ") and len(raw) > 6:
        return True
    return False


def create_session(config: dict[str, Any] | None = None, project_dir: str | None = None) -> dict[str, Any]:
    """Erstellt eine neue Session: Config, System-Prompt, leere Nachrichten, aktuelles Modell."""
    if config is None:
        config = load_config(project_dir)
    model = resolve_model(config, None)
    system_prompt = build_system_prompt(config, project_dir)
    return {
        "config": config,
        "project_dir": project_dir,
        "system_prompt": system_prompt,
        "messages": [],
        "model": model or "",
    }


def handle_user_input(
    session: dict[str, Any],
    user_input: str,
    *,
    allow_new_session: bool = True,
    images: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any] | None, str | None, str | None, dict[str, Any] | None]:
    """
    Verarbeitet User-Eingabe: /model-Wechsel oder normale Nachricht.
    Gibt (Antwort-Text, Session, debug_info, thinking, content, switch_info) zurück.
    allow_new_session: Wenn False (z. B. Matrix/Discord), wird /new ignoriert und nur Hinweis zurückgegeben.
    """
    config = session["config"]
    project_dir = session.get("project_dir")

    # /model ohne Argument → aktuelles Modell anzeigen
    if user_input.strip().lower() == "/model":
        current = session.get("model") or resolve_model(config, None) or "(keins)"
        return f"Aktuelles Modell: `{current}`\n\n*Wechseln: `/model NAME` oder `/model ALIAS`*", session, None, None, None, None

    model_switch, rest = parse_model_switch(user_input)

    if model_switch is not None:
        resolved = resolve_model(config, model_switch)
        if not resolved:
            resolved = model_switch
        # Provider-Präfix extrahieren für korrekte Modellprüfung
        _, api_name = get_provider_config(config, resolved)
        api_name = api_name or resolved
        # Provider-Name für _ollama_available_models ermitteln
        from miniassistant.ollama_client import _split_provider_prefix
        prov_prefix, _ = _split_provider_prefix(resolved)
        available, err_msg = _ollama_available_models(config, provider_name=prov_prefix)
        if err_msg:
            return f"Modellwechsel abgebrochen: {err_msg}. Bitte Ollama starten oder base_url prüfen.", session, None, None, None, None
        if api_name not in available:
            configured = _configured_model_names(config)
            avail_str = ", ".join(f"`{n}`" for n in configured) if configured else "(keine konfiguriert)"
            return f"Modell `{resolved}` nicht bei Ollama gefunden. Konfiguriert: {avail_str}. Wechsel abgebrochen.", session, None, None, None, None
        old_model = session.get("model") or ""
        session["model"] = resolved
        session["system_prompt"] = build_system_prompt(config, project_dir)
        session["messages"] = []  # neuer „Sprecher“ → Verlauf löschen, wie bei /new
        try:
            content, thinking, _msgs, _debug, _switch = chat_round(
                config,
                [],
                session["system_prompt"],
                resolved,
                "Say hello briefly in one short sentence.",
                project_dir,
            )
            reply = (content or "Modell geladen.").strip()
            reply_with_model = f"{reply}\n\n*(Modell: {resolved})*"
            return reply_with_model, session, None, thinking, reply_with_model, None
        except Exception as e:
            err = str(e).strip() or type(e).__name__
            if old_model and old_model != resolved:
                return f"Modell gewechselt: `{old_model}` → `{resolved}`. Verlauf gelöscht.\n\n*(Warmup fehlgeschlagen: {err})*", session, None, None, None, None
            return f"Modell: `{resolved}`. Verlauf gelöscht.\n\n*(Warmup fehlgeschlagen: {err})*", session, None, None, None, None

    is_models, provider = parse_models_command(user_input)
    if is_models:
        md = get_models_markdown(config, provider, current_model=session.get("model"))
        return md, session, None, None, None, None

    raw = user_input.strip()
    if raw.lower() == "/new" and not allow_new_session:
        return "", session, None, None, None, None
    if raw.lower() == "/new":
        # create_session runs agent_loader (build_system_prompt) first, then we warmup with one prompt
        new_session = create_session(config, project_dir)
        new_session["model"] = session.get("model") or new_session.get("model")
        warmup_model = new_session.get("model") or resolve_model(new_session["config"], project_dir)
        if not warmup_model:
            return "Neue Session gestartet. Kein Modell konfiguriert – bitte /model NAME oder in der Config ein default-Modell setzen.", new_session, None, None, None, None
        try:
            content, thinking, _msgs, _debug, _switch = chat_round(
                new_session["config"],
                [],
                new_session["system_prompt"],
                warmup_model,
                "Say hello briefly in one short sentence.",
                project_dir,
            )
            new_session["messages"] = []  # keep context empty; warmup was only to load model
            reply = (content or "Neue Session gestartet.").strip()
            reply_with_model = f"{reply}\n\n*(Modell: {warmup_model})*"
            return reply_with_model, new_session, None, thinking, reply_with_model, None
        except Exception as e:
            err = str(e).strip() or type(e).__name__
            return f"Neue Session gestartet. Der vorherige Verlauf ist nicht mehr im Kontext.\n\n*(Warmup fehlgeschlagen: {err})*", new_session, None, None, None, None

    if raw.lower() == "/schedules":
        return _format_schedules(), session, None, None, None, None

    if raw.lower().startswith("/schedule remove "):
        job_id = raw[17:].strip()
        if not job_id:
            return "Nutzung: `/schedule remove <ID>` (IDs mit `/schedules` anzeigen)", session, None, None, None, None
        if not remove_scheduled_job:
            return "*Scheduler nicht verfuegbar.*", session, None, None, None, None
        ok, msg = remove_scheduled_job(job_id)
        if ok:
            return f"Job `{msg}` entfernt.", session, None, None, None, None
        return f"Fehler: {msg}", session, None, None, None, None

    if raw.startswith("/auth ") and len(raw) > 6:
        auth_rest = raw[6:].strip()
        try:
            from miniassistant.chat_auth import consume_code
            config_dir = (config.get("_config_dir") or "").strip() or None
            result = consume_code(auth_rest, config_dir)
            if result:
                platform, user_id = result
                return f"{platform.capitalize()} freigeschaltet fuer `{user_id}`.", session, None, None, None, None
            return "Code nicht gefunden (bereits eingelöst oder abgelaufen?). Im Matrix-/Discord-Chat einen neuen Code anfordern.", session, None, None, None, None
        except Exception as e:
            return f"Auth-Fehler: {e}", session, None, None, None, None

    if not rest.strip():
        return "", session, None, None, None, None

    model = session.get("model") or resolve_model(config, None)
    if not model:
        return "Kein Modell konfiguriert. Bitte in der Config ein default-Modell oder /model MODELLNAME setzen.", session, None, None, None, None

    # Vision: wenn Bilder vorhanden, automatisch zum Vision-Modell wechseln (falls nötig)
    if images:
        original_model = model
        model = _resolve_vision_model(config, model)
        if not model:
            return "Kein Vision-Modell konfiguriert. Bitte `vision` in der Config setzen (z.B. `vision: llava:13b`).", session, None, None, None, None
        mime_types = [img.get("mime_type", "?") if isinstance(img, dict) else "?" for img in images]
        _aal.log_image_received(config, len(images), mime_types, vision_model=model if model != original_model else "")
        if model != original_model:
            _log.info("Vision: %d Bild(er) empfangen, wechsle %s → %s", len(images), original_model, model)

    # Chat-Kontext (room_id/channel_id) in System-Prompt injizieren
    effective_system_prompt = session["system_prompt"]
    chat_ctx = session.get("chat_context")
    if chat_ctx:
        ctx_lines = ["\n\n## Current Chat Context"]
        if chat_ctx.get("platform"):
            ctx_lines.append(f"Platform: {chat_ctx['platform']}")
        if chat_ctx.get("room_id"):
            ctx_lines.append(f"Matrix Room ID: `{chat_ctx['room_id']}`")
        if chat_ctx.get("channel_id"):
            ctx_lines.append(f"Discord Channel ID: `{chat_ctx['channel_id']}`")
        effective_system_prompt += "\n".join(ctx_lines)

    # Compacting-Check vor chat_round (für Notification bei non-streaming Clients)
    _notify_num_ctx = get_num_ctx_for_model(config, model)
    _notify_tools = get_tools_schema(config)
    did_compact = _needs_compacting(config, effective_system_prompt, session["messages"], _notify_tools, _notify_num_ctx)

    # Chat-Kontext für Tools (send_image, status_update) in config injizieren
    config["_chat_context"] = session.get("chat_context")

    # Stale cancellation flags bereinigen
    _cancel_uid = (session.get("chat_context") or {}).get("user_id")
    if _cancel_uid:
        from miniassistant.cancellation import clear_cancel
        clear_cancel(_cancel_uid)

    content, thinking, new_messages, debug_info, switch_info = chat_round(
        config,
        session["messages"],
        effective_system_prompt,
        model,
        rest,
        project_dir=project_dir,
        images=images,
    )
    # Bilder aus Messages entfernen (base64-Daten verschwenden Kontext-Platz)
    for _msg in new_messages:
        if _msg.get("images"):
            del _msg["images"]
            if _msg.get("role") == "user" and "[Bild]" not in (_msg.get("content") or ""):
                _msg["content"] = "[Bild angehängt] " + (_msg.get("content") or "")
    session["messages"] = new_messages

    # Memory: nur Inhalt speichern (kein Thinking), täglich, für späteren Auszug
    try:
        append_exchange(rest, content or "", project_dir=project_dir)
    except Exception:
        pass

    response_text = f"[Thinking]\n{thinking}\n\n{content}" if (thinking and content) else (thinking if thinking else (content or "(Keine Antwort)"))
    if did_compact:
        response_text = f"*Chat-Verlauf wurde komprimiert.*\n\n{response_text}"
    if switch_info:
        response_text = f"**Hinweis:** Wechsel zu Modell `{switch_info['model']}` (Grund: {switch_info['reason']}).\n\n{response_text}"
    return response_text, session, debug_info, thinking or None, content or None, switch_info or None


# --- Onboarding: guided first-time setup of agent files ---

def _detect_timezone() -> tuple[str, str]:
    """Detect system timezone. Returns (tz_name, current_time_str)."""
    from datetime import datetime
    now = datetime.now().astimezone()
    tz_name = now.strftime("%Z") or now.strftime("%z") or "UTC"
    # Try to get the IANA name (e.g. Europe/Vienna) from /etc/timezone or timedatectl
    iana_tz = ""
    try:
        from pathlib import Path
        etc_tz = Path("/etc/timezone")
        if etc_tz.exists():
            iana_tz = etc_tz.read_text().strip()
    except Exception:
        pass
    if not iana_tz:
        try:
            from pathlib import Path
            localtime = Path("/etc/localtime")
            if localtime.is_symlink():
                target = str(localtime.resolve())
                # e.g. /usr/share/zoneinfo/Europe/Vienna
                if "zoneinfo/" in target:
                    iana_tz = target.split("zoneinfo/", 1)[1]
        except Exception:
            pass
    display = iana_tz or tz_name
    current_time = now.strftime("%H:%M:%S")
    return display, current_time


def _onboarding_system_prompt(detected_system: dict[str, str]) -> str:
    """Build onboarding system prompt with detected system and fixed questions."""
    tz_display, current_time = _detect_timezone()
    sys_line = (
        f"System (detected – **do not ask**): "
        f"{detected_system.get('os', '')}, {detected_system.get('distro', '') or detected_system.get('os', '')}, "
        f"Package manager: {detected_system.get('package_manager', '')}, Init: {detected_system.get('init_system', '')}. "
        "Use this for TOOLS.md. Do NOT ask for the OS."
    )
    tz_line = (
        f"Detected timezone: **{tz_display}** (current time: {current_time}). "
        "Use this as the default timezone for USER.md. "
        "Confirm with the user — if they want a different timezone, write their preferred one into USER.md "
        "and tell them to run: `sudo timedatectl set-timezone <IANA_TZ>` (e.g. `Europe/Vienna`). "
        "If timedatectl is not available: `sudo ln -sf /usr/share/zoneinfo/<IANA_TZ> /etc/localtime`."
    )
    return f"""You are the **onboarding assistant** for **MiniAssistant**. Your only job: fill the four agent files with the user's answers. Do not invent – ask the **fixed questions** (below) and put answers into the four blocks.

{sys_line}
{tz_line}

**What the four files are (so you ask targeted questions):**

- **IDENTITY.md** – The assistant's **identity**: name, **response language** (which language the assistant must use for all replies), emoji, vibe.
- **SOUL.md** – The assistant's **soul** (limits & stance): run harmless commands without asking; answer briefly and factually; never expose tokens/passwords/private data.
- **TOOLS.md** – Environment, paths, hints. You already have the OS above; optional extra from user.
- **USER.md** – **User data**: name, nickname, pronouns, timezone, optional preferences (short/long answers).

**Fixed questions (ask in this order, do not invent):**
1. **IDENTITY:** What should the assistant be called? **Which language should the assistant use for its replies?** (e.g. Deutsch, English) (optional: emoji e.g. 🤖; optional: vibe in one sentence)
2. **SOUL:** Use default limits? (Run harmless commands without asking; answer briefly and factually; never expose tokens/passwords/private data) – or add/change something?
3. **USER:** What should I call you? (Name, nickname), pronouns (Du/Sie or you/they). Timezone: show the detected timezone and ask if it's correct (if not, note the correct one and tell the user how to change it on the system). **Country (optional):** Which country are you in? (e.g. Austria, Germany, Switzerland, USA). This helps the assistant search with local context (prices, shops, domains). If the user skips this, simply omit the country field from USER.md. Optional preferences? Also ask: Would you like to tell me something about yourself? (hobbies, interests, job – anything that helps the assistant understand you better). **Important: USER.md has a 500 character limit.** Keep it concise.
4. **AVATAR (optional):** Do you have a profile picture/avatar for the bot? (PNG file path or URL, e.g. `~/avatar.png` or `https://example.org/bot.png`). Best format: PNG, square (256x256 or 512x512). If provided as URL, validate with `check_url` first, then download to `agent_dir/avatar.png`. If a file path, copy to `agent_dir/avatar.png`. Save path in config via `save_config({{avatar: "<path>"}})`. If skipped, the default logo is used.

Optional: Any special paths or hints for the environment (TOOLS)? Otherwise the detected system above is enough.

**Flow:**
- On "Beginne das Onboarding" / "Start onboarding": Ask the first 2–3 questions and WAIT. Do not output file blocks yet.
- After each user reply: ask the next question OR, when you have everything, output the four sections.
- Fill the four sections only with **real** user input; do not invent.

You only provide content; the user saves via button. Exact headings: "## SOUL.md", "## IDENTITY.md", "## TOOLS.md", "## USER.md".

**Write all four file contents in English** (SOUL.md, IDENTITY.md, TOOLS.md, USER.md). Reply to the user in their language (e.g. German).

**Format of the four sections** (2–5 sentences per block), once you have enough info:

## IDENTITY.md
[Name. Response language: **LANGUAGE** (e.g. Deutsch or English). Emoji, vibe.]

## SOUL.md
[Limits: run harmless commands without asking; answer briefly and factually; never expose tokens/passwords/private data; push back or disagree when necessary; any user additions]

## TOOLS.md
[Environment: detected system above; optional paths/hints from user. **No meta text like 'Onboarding done' – only real environment info.**]

## USER.md
[Name, nickname, pronouns, timezone, country; optional preferences (e.g. short/long answers)]

**Important:** The four blocks must contain ONLY the actual file content. Do NOT add commentary, status messages, or text like "Onboarding complete" inside any block.
"""


def _parse_agent_blocks(text: str) -> dict[str, str] | None:
    """Extrahiert SOUL.md, IDENTITY.md, TOOLS.md, USER.md aus Antwort-Text (## DATEI.md ...)."""
    import re
    blocks: dict[str, str] = {}
    pattern = r"##\s*(SOUL\.md|IDENTITY\.md|TOOLS\.md|USER\.md)\s*\n(.*?)(?=\n##\s|\Z)"
    for m in re.finditer(pattern, text, re.DOTALL):
        blocks[m.group(1)] = m.group(2).strip()
    if len(blocks) == 4:
        return blocks
    return None


def run_onboarding_round(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    user_content: str,
    project_dir: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, str] | None, dict[str, Any] | None, str, str]:
    """
    Eine Runde Onboarding-Chat: System-Prompt inkl. erkanntem OS und klaren Datei-Beschreibungen, keine Tools.
    Gibt (response_text, new_messages, suggested_files oder None, debug_info oder None, thinking, content) zurück.
    """
    from miniassistant.agent_loader import _detect_system
    from miniassistant.ollama_client import chat as ollama_chat, resolve_model, get_options_for_model, get_base_url_for_model as _get_base_url

    detected_system = _detect_system()
    system_prompt = _onboarding_system_prompt(detected_system)
    model = resolve_model(config, None)
    if not model:
        return "Kein Modell konfiguriert (z.B. default in models setzen).", messages, None, None, "", ""

    options = get_options_for_model(config, model)
    think = get_think_for_model(config, model)
    debug = (config.get("server") or {}).get("debug", False)

    msgs = list(messages)
    msgs.append({"role": "user", "content": user_content})

    response = _dispatch_chat(
        config, model, msgs,
        system=system_prompt, think=think,
        tools=[],  # keine Tools beim Onboarding
        options=options or None,
    )
    msg = response.get("message") or {}
    content = (msg.get("content") or "").strip()
    thinking = (msg.get("thinking") or "").strip()
    full = f"[Thinking]\n{thinking}\n\n{content}" if thinking else content

    msgs.append({"role": "assistant", "content": content, "thinking": thinking})
    suggested = _parse_agent_blocks(content)

    debug_info: dict[str, Any] | None = None
    if debug:
        debug_info = {
            "request": {"model": model, "system": system_prompt, "messages": msgs[:-1]},
            "response": response,
            "message": msg,
        }
        try:
            from miniassistant.debug_log import log_chat
            req = {"model": model, "system": system_prompt, "messages": msgs[:-1], "think": think, "tools": []}
            log_chat(req, response, config, project_dir, label="onboarding")
        except Exception:
            pass
    return full, msgs, suggested, debug_info, thinking, content
