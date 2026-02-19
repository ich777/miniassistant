"""
Ollama-API: Modelle auflisten, Modell-Details/Caps, Chat mit num_ctx, think und Tools.
"""
from __future__ import annotations

from typing import Any

import httpx


def _auth_headers(api_key: str | None) -> dict[str, str]:
    """Erzeugt Authorization-Header wenn api_key gesetzt."""
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def list_models(base_url: str, api_key: str | None = None) -> list[dict[str, Any]]:
    """GET /api/tags – Liste aller Modelle."""
    url = f"{base_url.rstrip('/')}/api/tags"
    with httpx.Client(timeout=30.0, headers=_auth_headers(api_key)) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    return data.get("models") or []


def show_model(base_url: str, name: str, api_key: str | None = None) -> dict[str, Any]:
    """POST /api/show – Modell-Details inkl. capabilities."""
    url = f"{base_url.rstrip('/')}/api/show"
    with httpx.Client(timeout=30.0, headers=_auth_headers(api_key)) as client:
        r = client.post(url, json={"name": name})
        r.raise_for_status()
        return r.json()


def model_supports_thinking(base_url: str, name: str) -> bool:
    """True wenn das Modell Reasoning/Thinking unterstützt (für Anzeige in der Modellliste)."""
    try:
        info = show_model(base_url, name)
        caps = info.get("capabilities") or []
        if isinstance(caps, list) and ("thinking" in caps or "reasoning" in caps):
            return True
        params = info.get("parameters") or {}
        if isinstance(params, dict) and (params.get("thinking") or params.get("num_ctx")):
            # Manche Modelle exponieren think über Parameter
            pass
        # Heuristik: bekannte Reasoning-Modelle
        n = (name or "").lower()
        if "r1" in n or "reasoning" in n or "deepseek-r1" in n:
            return True
        if "qwen3" in n and ("80k" in n or "14b" in n):
            return True
    except Exception:
        pass
    return False


def model_supports_tools(base_url: str, name: str) -> bool:
    """True wenn das Modell Tool/Function-Calling unterstützt. Sonst Tools nicht mitschicken (z. B. DeepSeek-R1 offiziell → 400)."""
    try:
        info = show_model(base_url, name)
        caps = info.get("capabilities") or []
        if isinstance(caps, list) and ("tool_use" in caps or "tools" in caps):
            return True
        # Blocklist: offizielle DeepSeek-R1 ohne Tool-Support (Community-Varianten wie deepseek-r1-tool-calling haben es)
        n = (name or "").lower()
        if "deepseek-r1" in n and "tool-calling" not in n and "tool_calling" not in n:
            return False
    except Exception:
        pass
    return False


def model_supports_vision(base_url: str, name: str) -> bool:
    """True wenn das Modell Vision/Bildanalyse unterstützt (z.B. llava, gemma3, minicpm-v)."""
    try:
        info = show_model(base_url, name)
        caps = info.get("capabilities") or []
        if isinstance(caps, list) and ("vision" in caps or "image" in caps):
            return True
    except Exception:
        pass
    # Heuristik: bekannte Vision-Modelle
    n = (name or "").lower()
    _VISION_PATTERNS = ("llava", "gemma3", "minicpm-v", "llama3.2-vision", "bakllava", "moondream", "nanollava")
    return any(p in n for p in _VISION_PATTERNS)


def get_vision_models(config: dict[str, Any]) -> list[str]:
    """Gibt die Liste der Vision-Modelle zurück (kann leer sein)."""
    val = config.get("vision") or []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        m = (val.get("model") or "").strip()
        return [m] if m else []
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def get_image_generation_models(config: dict[str, Any]) -> list[str]:
    """Gibt die Liste der Image-Generation-Modelle zurück (kann leer sein)."""
    val = config.get("image_generation") or []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        m = (val.get("model") or "").strip()
        return [m] if m else []
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def _find_provider(providers: dict[str, Any], name: str) -> str | None:
    """Case-insensitive Provider-Lookup. Gibt den echten Key zurück oder None."""
    if not name:
        return None
    if name in providers:
        return name
    lower = name.lower()
    for key in providers:
        if key.lower() == lower:
            return key
    return None


def _split_provider_prefix(model: str) -> tuple[str | None, str]:
    """Extrahiert Provider-Präfix aus Modellname. 'ollama2/llama3:8b' → ('ollama2', 'llama3:8b'). 'qwen3:14b' → (None, 'qwen3:14b')."""
    if not model or "/" not in model:
        return None, model or ""
    prefix, _, name = model.partition("/")
    # Nur als Provider werten wenn kein Punkt/Doppelpunkt im Prefix (sonst ist es z.B. ein Registry-Pfad)
    if "." in prefix or ":" in prefix:
        return None, model
    return prefix, name


def get_provider_config(config: dict[str, Any], model: str | None = None) -> tuple[dict[str, Any], str | None]:
    """Gibt (provider_cfg, clean_model_name) zurück. Löst Provider-Präfix auf (case-insensitive).
    'ollama2/llama3:8b' → (providers['ollama2'], 'llama3:8b')
    'qwen3:14b' → (providers['ollama'], 'qwen3:14b')
    None → (providers['ollama'], None)"""
    providers = config.get("providers") or {}
    if not model:
        default_name = next(iter(providers), "ollama")
        return providers.get(default_name) or {}, None
    prefix, clean = _split_provider_prefix(model)
    if prefix:
        real_key = _find_provider(providers, prefix)
        if real_key:
            return providers[real_key], clean
    # Kein Prefix oder Prefix nicht gefunden → Default-Provider
    default_name = next(iter(providers), "ollama")
    return providers.get(default_name) or {}, model


def resolve_model(config: dict[str, Any], model: str | None) -> str | None:
    """Ersetzt Alias durch echten Modellnamen. Provider-Präfix wird durchgereicht (z.B. 'ollama2/big' → 'ollama2/llama3.3:70b').
    Ohne Prefix: sucht Alias in ALLEN Providern. Bei Duplikat → Default-Provider gewinnt."""
    if not model:
        models_cfg = config.get("models") or {}
        default = models_cfg.get("default")
        if not default:
            return None
        # Default kann selbst ein Alias sein → auflösen
        return resolve_model(config, default)
    prefix, clean = _split_provider_prefix(model)
    providers = config.get("providers") or {}
    if prefix:
        real_key = _find_provider(providers, prefix)
        if real_key:
            prov_models = providers[real_key].get("models") or {}
            aliases = prov_models.get("aliases") or {}
            resolved = aliases.get(clean, clean)
            return f"{real_key}/{resolved}"
    # Kein Prefix → Alias in allen Providern suchen
    default_name = next(iter(providers), "ollama")
    # Zuerst Default-Provider prüfen (hat Priorität bei Duplikaten)
    default_prov = providers.get(default_name) or {}
    default_aliases = (default_prov.get("models") or {}).get("aliases") or {}
    if model in default_aliases:
        return default_aliases[model]
    # Dann alle anderen Provider durchsuchen
    for prov_name, prov_cfg in providers.items():
        if prov_name == default_name:
            continue
        prov_aliases = (prov_cfg.get("models") or {}).get("aliases") or {}
        if model in prov_aliases:
            resolved = prov_aliases[model]
            return f"{prov_name}/{resolved}"
    # Kein Alias gefunden → Modellname direkt zurückgeben
    return model


def get_provider_type(config: dict[str, Any], model_name: str) -> str:
    """Provider-Typ für ein Modell: 'ollama' (default), 'claude-code', etc."""
    prov, _ = get_provider_config(config, model_name)
    return str(prov.get("type", "ollama")).lower().strip()


def get_options_for_model(config: dict[str, Any], model_name: str) -> dict[str, Any]:
    """Optionen für ein Modell: Provider-globale options + model_options[model]. Provider wird aus Präfix aufgelöst."""
    prov, clean = get_provider_config(config, model_name)
    base = dict(prov.get("options") or {})
    per_model = prov.get("model_options") or {}
    lookup_name = clean or model_name
    if isinstance(per_model, dict) and lookup_name:
        overlay = per_model.get(lookup_name)
        if isinstance(overlay, dict):
            base = {**base, **overlay}
    # 'think' ist kein Ollama-Option sondern Top-Level-Parameter → nicht in options mitschicken
    base.pop("think", None)
    # num_ctx=0 bedeutet "nicht setzen, Server-Default nutzen"
    if "num_ctx" in base and not base["num_ctx"]:
        base.pop("num_ctx")
    return base


def get_base_url_for_model(config: dict[str, Any], model_name: str) -> str:
    """Base-URL des Providers für ein Modell. Provider wird aus Präfix aufgelöst."""
    prov, _ = get_provider_config(config, model_name)
    return prov.get("base_url", "http://127.0.0.1:11434")


def get_api_key_for_model(config: dict[str, Any], model_name: str) -> str | None:
    """API-Key des Providers für ein Modell. None wenn kein Key konfiguriert."""
    prov, _ = get_provider_config(config, model_name)
    return prov.get("api_key") or None


def get_num_ctx_for_model(config: dict[str, Any], model_name: str) -> int:
    """Context-Größe (num_ctx) für ein Modell: model_options[model].num_ctx, sonst Provider-num_ctx, sonst 32768.
    0 bedeutet 'nicht gesetzt / Server-Default' und wird ignoriert."""
    opts = get_options_for_model(config, model_name)
    num = opts.get("num_ctx")
    if num is not None and int(num) > 0:
        return int(num)
    prov, _ = get_provider_config(config, model_name)
    num = prov.get("num_ctx")
    if num is not None and int(num) > 0:
        return int(num)
    return 32768


def get_think_for_model(config: dict[str, Any], model_name: str) -> bool | None:
    """Think-Modus für ein Modell: model_options[model].think überschreibt Provider.think. None = nicht gesetzt.
    Bei think=True wird zusätzlich geprüft ob das Modell Thinking unterstützt."""
    # Erst aus model_options lesen (think dort wurde schon aus options entfernt, direkt aus model_options holen)
    prov, clean = get_provider_config(config, model_name)
    per_model = prov.get("model_options") or {}
    lookup = clean or model_name
    model_cfg = per_model.get(lookup) if isinstance(per_model, dict) and lookup else None
    val = None
    if isinstance(model_cfg, dict):
        val = model_cfg.get("think")
    if val is None:
        val = prov.get("think")
    if val is None:
        return None
    if val is False:
        # Explizit deaktiviert → immer senden (z.B. für deepseek-r1)
        return False
    # think=True: nur senden wenn Modell Thinking unterstützt
    base_url = prov.get("base_url", "http://127.0.0.1:11434")
    api_name = clean or model_name
    if not model_supports_thinking(base_url, api_name):
        return None
    return True


def get_tools_schema(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool-Schema für exec, optional web_search, optional schedule, optional invoke_model (Subagenten)."""
    any_subagents = bool(config.get("subagents")) or any(
        (p.get("models") or {}).get("subagents")
        for p in (config.get("providers") or {}).values()
        if isinstance(p, dict)
    )
    return _tools_schema(
        config,
        config.get("scheduler"),
        subagents=any_subagents,
    )


def _tools_schema(
    config: dict[str, Any],
    scheduler_cfg: dict[str, Any] | None = None,
    *,
    subagents: bool = False,
) -> list[dict[str, Any]]:
    """Ollama-kompatibles Tool-Schema für exec, optional web_search, schedule, invoke_model (Subagenten)."""
    schema = [
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Execute a shell command. Use for file operations, package management, services, system queries. Do not use sudo when running as root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run (e.g. ls -la, cat /etc/hostname)"},
                    },
                    "required": ["command"],
                },
            },
        },
    ]
    search_engines = config.get("search_engines") or {}
    if search_engines:
        engine_ids = list(search_engines.keys())
        default_id = config.get("default_search_engine") or (engine_ids[0] if engine_ids else None)
        desc = "Search the web via SearXNG. Use for current information, lookups. Optional: engine (one of: " + ", ".join(engine_ids) + "). Default is '" + (default_id or "") + "'."
        if any("vpn" in k.lower() for k in engine_ids):
            desc += " If an engine id contains 'vpn' (e.g. vpn, searxng_vpn): use it only when the user explicitly asks for VPN/secure search or when prefs say so."
        schema.append({
            "type": "function",
            "function": {
                "name": "web_search",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "engine": {"type": "string", "description": "Optional. Engine id (e.g. main, vpn). Omit to use default."},
                    },
                    "required": ["query"],
                },
            },
        })
    schema.append({
        "type": "function",
        "function": {
            "name": "check_url",
            "description": "Check if a URL is reachable (HTTP request, follows redirects). Use this tool ONLY when the user explicitly asks to verify, check or test links/URLs (e.g. 'check these links', 'verify the URLs'). Do not use for every link you mention; only when link verification is requested.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to check (e.g. https://example.org). Do not add www unless the user gave it."},
                },
                "required": ["url"],
            },
        },
    })
    if scheduler_cfg in (None, False) or scheduler_cfg is True or (isinstance(scheduler_cfg, dict) and scheduler_cfg.get("enabled", True)):
        schema.append({
            "type": "function",
            "function": {
                "name": "schedule",
                "description": "Manage scheduled tasks. ALWAYS use this tool instead of cron/crontab! action='create': new job. action='list': show all. action='remove': delete by id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["create", "list", "remove"], "description": "create (default), list, or remove"},
                        "prompt": {"type": "string", "description": "Work instruction the bot executes later (e.g. 'Get current weather for City X'). NOT a pre-formatted answer!"},
                        "command": {"type": "string", "description": "Shell command to run (optional). Output is included in prompt context if both set."},
                        "when": {"type": "string", "description": "Cron 5 fields in local system time (e.g. '30 7 * * *' = 7:30) or 'in 30 minutes' / 'in 1 hour'"},
                        "client": {"type": "string", "description": "Target chat client: 'matrix', 'discord', or omit for all."},
                        "once": {"type": "boolean", "description": "Delete after first execution. Default false. 'in N minutes' triggers are always once."},
                        "model": {"type": "string", "description": "Model name or alias for the prompt (e.g. 'qwen3', 'ollama-online/kimi-k2.5'). Default: current default model. Use this to control cost and capability."},
                        "id": {"type": "string", "description": "Job ID (or prefix) for action='remove'."},
                    },
                    "required": [],
                },
            },
        })
    schema.append({
        "type": "function",
        "function": {
            "name": "save_config",
            "description": "Update the app's config file. Your YAML is deep-merged into the existing config (existing keys are preserved). Validates, creates .bak backups, then writes. After saving, tell user to restart. Structure: providers.ollama.models.aliases for model aliases, providers.ollama.models.default for default model, chat_clients.matrix/discord for bots, search_engines for SearXNG.",
            "parameters": {
                "type": "object",
                "properties": {
                    "yaml_content": {"type": "string", "description": "YAML with only the keys to add or change. Example for aliases: 'providers:\\n  ollama:\\n    models:\\n      aliases:\\n        fast: llama3.2\\n        coder: qwen2.5-coder:14b'"},
                },
                "required": ["yaml_content"],
            },
        },
    })
    schema.append({
        "type": "function",
        "function": {
            "name": "send_image",
            "description": "Send an image file to the current chat (Matrix room or Discord channel). Use after generating or downloading an image. The image is uploaded via the bot client (E2EE-capable). For Web-UI: returns the file path instead. IMPORTANT: When this tool succeeds, the user already sees the image — do NOT send an additional text confirmation. The image IS the response. Only reply with text if the tool fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Absolute path to the image file on disk"},
                    "caption": {"type": "string", "description": "Optional caption/description for the image"},
                },
                "required": ["image_path"],
            },
        },
    })
    # status_update: nur verfügbar wenn chat_clients konfiguriert sind
    cc = config.get("chat_clients") or {}
    has_clients = any(
        (cc.get(k) or config.get(k) or {}).get("enabled", True) and ((cc.get(k) or config.get(k) or {}).get("token") or (cc.get(k) or config.get(k) or {}).get("bot_token"))
        for k in ("matrix", "discord")
    )
    if has_clients:
        schema.append({
            "type": "function",
            "function": {
                "name": "status_update",
                "description": "Send an intermediate status message to the user in the current chat. Use during multi-step tasks or plan execution to report progress, ask for input, or share interim findings. The message is sent immediately — do NOT wait until the end. Keep updates short.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Status message to send (Markdown supported)"},
                    },
                    "required": ["message"],
                },
            },
        })
    if subagents:
        schema.append({
            "type": "function",
            "function": {
                "name": "invoke_model",
                "description": "Delegate a task to another model (subagent) by name or alias. Use e.g. for compiling (qwen-coder), code review, or specialized tasks. Returns the other model's reply.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "model": {"type": "string", "description": "Model name or alias (e.g. qwen2.5-coder:14b or alias 'compiler')"},
                        "message": {"type": "string", "description": "Task or question for the other model"},
                    },
                    "required": ["model", "message"],
                },
            },
        })
        schema.append({
            "type": "function",
            "function": {
                "name": "debate",
                "description": "Start a structured multi-round debate/discussion between two AI perspectives. "
                    "USE THIS when the user says: 'diskutiere mit subworker', 'halte eine Diskussion', 'debattiere', "
                    "'lass zwei Modelle diskutieren', 'hole zwei Meinungen ein', or similar. "
                    "Both sides are argued by subagent(s). Transcript saved to Markdown file. "
                    "Between rounds, arguments are summarized so small models keep context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "The debate topic or question"},
                        "perspective_a": {"type": "string", "description": "Position/viewpoint of side A (e.g. 'Pro Kernenergie')"},
                        "perspective_b": {"type": "string", "description": "Position/viewpoint of side B (e.g. 'Contra Kernenergie')"},
                        "model": {"type": "string", "description": "Subagent model for side A (and B if model_b not set)"},
                        "model_b": {"type": "string", "description": "Optional: different subagent model for side B. Defaults to model."},
                        "rounds": {"type": "integer", "description": "Number of back-and-forth rounds (1-10, default 3)"},
                        "language": {"type": "string", "description": "Response language (default: Deutsch)"},
                    },
                    "required": ["topic", "perspective_a", "perspective_b", "model"],
                },
            },
        })
    return schema


def get_subagent_tools_schema(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool-Schema für Subagents: exec + web_search + check_url. Kein save_config, schedule, invoke_model."""
    schema = [
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Execute a shell command. Use for file operations, system queries. NEVER use rm -rf — move to trash instead. NEVER install system packages without explicit instruction.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run"},
                    },
                    "required": ["command"],
                },
            },
        },
    ]
    search_engines = config.get("search_engines") or {}
    if search_engines:
        engine_ids = list(search_engines.keys())
        default_id = config.get("default_search_engine") or (engine_ids[0] if engine_ids else None)
        desc = "Search the web via SearXNG. Default engine: '" + (default_id or "") + "'."
        schema.append({
            "type": "function",
            "function": {
                "name": "web_search",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "engine": {"type": "string", "description": "Optional engine id."},
                    },
                    "required": ["query"],
                },
            },
        })
    schema.append({
        "type": "function",
        "function": {
            "name": "check_url",
            "description": "Check if a URL is reachable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to check"},
                },
                "required": ["url"],
            },
        },
    })
    return schema


def _normalize_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Konvertiert Bilder im internen Format {mime_type, data} zu reinen base64-Strings für Ollama."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        images = msg.get("images")
        if not images:
            out.append(msg)
            continue
        converted: list[str] = []
        for img in images:
            if isinstance(img, dict):
                converted.append(img.get("data") or "")
            elif isinstance(img, str):
                converted.append(img)
        if converted:
            msg = dict(msg)
            msg["images"] = converted
        out.append(msg)
    return out


def _chat_body(
    model: str,
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    num_ctx: int | None = None,
    think: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Baut den Request-Body für /api/chat. System-Prompt als system-role Message (Ollama /api/chat Standard)."""
    msgs = _normalize_images(list(messages))
    if system:
        msgs = [{"role": "system", "content": system}] + msgs
    body: dict[str, Any] = {"model": model, "messages": msgs, "stream": stream}
    opts: dict[str, Any] = dict(options) if options else {}
    if num_ctx is not None:
        opts["num_ctx"] = num_ctx
    if opts:
        body["options"] = opts
    if think is not None:
        body["think"] = think
    # Tools nur mitschicken wenn nicht leer – Modelle ohne Tool-Support (z. B. DeepSeek-R1) liefern sonst 400
    if tools:
        body["tools"] = tools
    return body


def chat(
    base_url: str,
    messages: list[dict[str, Any]],
    *,
    model: str,
    system: str | None = None,
    num_ctx: int | None = None,
    think: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """
    POST /api/chat (non-streaming). Gibt die komplette JSON-Antwort zurück.
    """
    import json as _json
    url = f"{base_url.rstrip('/')}/api/chat"
    body = _chat_body(model=model, messages=messages, system=system, num_ctx=num_ctx, think=think, tools=tools, options=options, stream=False)
    _timeout = httpx.Timeout(connect=30.0, read=timeout, write=60.0, pool=30.0)
    with httpx.Client(timeout=_timeout, headers=_auth_headers(api_key)) as client:
        r = client.post(url, json=body)
        r.raise_for_status()
        return r.json()


def chat_stream(
    base_url: str,
    messages: list[dict[str, Any]],
    *,
    model: str,
    system: str | None = None,
    num_ctx: int | None = None,
    think: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
):
    """POST /api/chat mit stream=True. Generiert Chunk-Dicts (NDJSON)."""
    import json as _json
    url = f"{base_url.rstrip('/')}/api/chat"
    body = _chat_body(model=model, messages=messages, system=system, num_ctx=num_ctx, think=think, tools=tools, options=options, stream=True)
    # connect: Verbindung zu Ollama; read: max. Pause zwischen Tokens (Inaktivitätserkennung)
    _timeout = httpx.Timeout(connect=30.0, read=timeout, write=60.0, pool=30.0)
    with httpx.Client(timeout=_timeout, headers=_auth_headers(api_key)) as client:
        with client.stream("POST", url, json=body) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                yield _json.loads(line)
