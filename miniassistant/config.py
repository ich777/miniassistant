"""
Config-Schema und Laden/Speichern für MiniAssistant.
Speicherort: ~/.config/miniassistant/config.yaml oder ./miniassistant.yaml
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Defaults
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8765
DEFAULT_AGENT_DIR = "agent"
DEFAULT_MAX_CHARS_PER_FILE = 500
CONFIG_FILENAME = "config.yaml"


def get_config_dir() -> str:
    """Config-Verzeichnis (zur Laufzeit aus MINIASSISTANT_CONFIG_DIR oder Default).
    Absicherung: wenn HOME='/' (typisch für root via sysvinit/start-stop-daemon),
    wird /root als Fallback verwendet."""
    env_dir = (os.environ.get("MINIASSISTANT_CONFIG_DIR") or "").strip()
    if env_dir:
        return env_dir
    home = os.path.expanduser("~")
    # Auf manchen Systemen (Devuan sysvinit) ist root's HOME in /etc/passwd '/'
    # statt '/root'. Fallback damit Config nicht unter /.config/ landet.
    if not home or home == "/":
        home = os.environ.get("HOME", "").strip() or "/root"
    return os.path.join(home, ".config", "miniassistant")


def _default_agent_dir() -> str:
    return str(Path(get_config_dir()) / "agent")


def _default_workspace() -> str:
    """Standard-Workspace im Home-Verzeichnis des Users."""
    home = os.environ.get("HOME", "").strip() or "/root"
    if not home or home == "/":
        home = "/root"
    return str(Path(home) / "workspace")


def _default_trash_dir() -> str:
    """Trash im Home-Verzeichnis, versteckt (.trash)."""
    home = os.environ.get("HOME", "").strip() or "/root"
    if not home or home == "/":
        home = "/root"
    return str(Path(home) / ".trash")


def _normalize_search_engines(raw: Any) -> dict[str, dict[str, Any]]:
    """search_engines: { id -> { url: str } }. Leere oder ungültige Einträge ausfiltern."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw.items():
        if not k or not isinstance(v, dict):
            continue
        url = (v.get("url") or "").strip()
        if url:
            out[str(k)] = {"url": url}
    return out


def _default_search_engine_id(engines: dict[str, Any], explicit: str | None) -> str | None:
    """Default-Engine: explicit wenn gesetzt und in engines, sonst erste Engine (bei nur einer ohnehin klar)."""
    if explicit and explicit in engines:
        return explicit
    if engines:
        return next(iter(engines))
    return None


def _search_engines_merged(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _normalize_search_engines(data.get("search_engines"))


def _default_search_engine_merged(data: dict[str, Any]) -> str | None:
    engines = _normalize_search_engines(data.get("search_engines"))
    return _default_search_engine_id(engines, data.get("default_search_engine"))


def get_search_engine_url(config: dict[str, Any], engine_id: str | None = None) -> str | None:
    """URL für eine Suchmaschine. engine_id=None = konfigurierte Default-Engine."""
    engines = config.get("search_engines") or {}
    if not engines:
        return None
    eid = engine_id or config.get("default_search_engine") or next(iter(engines), None)
    if not eid or eid not in engines:
        return None
    return (engines[eid].get("url") or "").strip() or None


def config_path(project_dir: str | None = None) -> Path:
    """Config-Datei: project_dir/miniassistant.yaml wenn project_dir gesetzt, sonst get_config_dir()/config.yaml."""
    if project_dir:
        return Path(project_dir).resolve() / "miniassistant.yaml"
    return Path(get_config_dir()) / CONFIG_FILENAME


def load_config(project_dir: str | None = None) -> dict[str, Any]:
    path = config_path(project_dir)
    if not path.exists():
        return _default_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        msg = str(e)
        if "@" in msg:
            msg += ' Hinweis: Werte mit "@" (z. B. matrix.user_id) in Anführungszeichen setzen: user_id: "@bot:example.org"'
        raise RuntimeError(msg) from e
    merged = _merge_with_defaults(data)
    # Verzeichnis der geladenen Config (für Matrix-Store etc.); wird nicht in die Datei geschrieben
    merged["_config_dir"] = str(path.parent.resolve())
    # Wichtige Verzeichnisse sicherstellen (workspace, agent_dir)
    for _dir_key in ("workspace", "agent_dir", "trash_dir"):
        _dir_val = (merged.get(_dir_key) or "").strip()
        if _dir_val:
            Path(_dir_val).expanduser().mkdir(parents=True, exist_ok=True)
    return merged


def load_config_raw(project_dir: str | None = None) -> str:
    """Liest die Config-Datei als Roh-Text (für Anzeige/Bearbeitung). Leer wenn keine Datei."""
    path = config_path(project_dir)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def backup_config(path: Path, max_backups: int = 4) -> None:
    """Rotiert Backups: path.bak (neueste Kopie), path.bak.1 … path.bak.3. Älteste entfällt."""
    if not path.exists():
        return
    path = path.resolve()
    import shutil
    # Älteste entfernen
    oldest = Path(str(path) + f".bak.{max_backups - 1}")
    if oldest.exists():
        oldest.unlink()
    # Von hinten rotieren: .bak.2 -> .bak.3, .bak.1 -> .bak.2, .bak -> .bak.1
    for i in range(max_backups - 2, -1, -1):
        src = Path(str(path) + (".bak" if i == 0 else f".bak.{i}"))
        dst = Path(str(path) + f".bak.{i + 1}")
        if src.exists():
            shutil.copy2(src, dst)
    # Aktuelle Datei nach .bak
    Path(str(path) + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def write_config_raw(content: str, project_dir: str | None = None) -> Path:
    """Schreibt Roh-Text in die Config-Datei. Vorher mit validate_config_raw prüfen. Erstellt bis zu 4 .bak."""
    path = config_path(project_dir)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_config(path)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def validate_config_raw(content: str) -> tuple[bool, str]:
    """
    Prüft Roh-YAML: Parse + Merge mit Defaults. Returns (ok, error_message).
    Bei Erfolg ist error_message leer.
    """
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as e:
        return False, f"Ungültiges YAML: {e}"
    try:
        _merge_with_defaults(data)
    except Exception as e:
        return False, f"Format/Struktur: {e}"
    return True, ""


def _default_config() -> dict[str, Any]:
    merged = _merge_with_defaults({})
    merged["_config_dir"] = get_config_dir()
    return merged


def _normalize_matrix(matrix: Any) -> dict[str, Any] | None:
    """Matrix-Config: enabled, homeserver, bot_name, user_id, token, device_id (optional), encrypted_rooms (bool).
    Akzeptiert auch häufige Alias-Feldnamen (access_token → token, homeserver_url → homeserver, enable_e2ee → encrypted_rooms)."""
    if not matrix or not isinstance(matrix, dict):
        return None
    m = matrix
    # Alias-Migration: häufige falsche Feldnamen akzeptieren
    homeserver = (m.get("homeserver") or m.get("homeserver_url") or "").strip()
    token = (m.get("token") or m.get("access_token") or "").strip()
    if not homeserver or not token:
        return None
    user_id = (m.get("user_id") or "").strip() or None
    # encrypted_rooms: auch enable_e2ee / e2ee / encryption akzeptieren
    encrypted_rooms = m.get("encrypted_rooms")
    if encrypted_rooms is None:
        encrypted_rooms = m.get("enable_e2ee")
    if encrypted_rooms is None:
        encrypted_rooms = m.get("e2ee")
    if encrypted_rooms is None:
        encrypted_rooms = m.get("encryption")
    if encrypted_rooms is None:
        encrypted_rooms = True
    return {
        "enabled": bool(m.get("enabled", True)),
        "homeserver": homeserver,
        "bot_name": (m.get("bot_name") or "MiniAssistant").strip() or "MiniAssistant",
        "user_id": user_id,
        "token": token,
        "device_id": (m.get("device_id") or "").strip() or None,
        "encrypted_rooms": bool(encrypted_rooms),
    }


def _normalize_discord(discord: Any) -> dict[str, Any] | None:
    """Discord-Config: enabled, bot_token, command_prefix (optional)."""
    if not discord or not isinstance(discord, dict):
        return None
    d = discord
    bot_token = (d.get("bot_token") or "").strip()
    if not bot_token:
        return None
    return {
        "enabled": bool(d.get("enabled", True)),
        "bot_token": bot_token,
        "command_prefix": (d.get("command_prefix") or "!").strip() or "!",
    }


def _normalize_chat_clients(data: dict[str, Any]) -> dict[str, Any]:
    """Normalisiert chat_clients; migriert top-level matrix: automatisch."""
    cc = data.get("chat_clients") or {}
    if not isinstance(cc, dict):
        cc = {}
    # Abwärtskompatibilität: top-level matrix: → chat_clients.matrix
    matrix_raw = cc.get("matrix") or data.get("matrix")
    discord_raw = cc.get("discord") or data.get("discord")
    return {
        "matrix": _normalize_matrix(matrix_raw),
        "discord": _normalize_discord(discord_raw),
    }


def _parse_provider(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse ein einzelnes Provider-Dict in ein normalisiertes Format. type bestimmt das Protokoll."""
    prov_type = raw.get("type", "ollama")
    options = raw.get("options")
    if not isinstance(options, dict):
        options = {}
    if raw.get("num_ctx") and "num_ctx" not in options:
        options = {**options, "num_ctx": raw.get("num_ctx")}
    model_options = raw.get("model_options")
    if not isinstance(model_options, dict):
        model_options = {}
    models_raw = raw.get("models") or {}
    models = {
        "default": models_raw.get("default"),
        "aliases": models_raw.get("aliases") or {},
        "list": models_raw.get("list"),
        "fallbacks": models_raw.get("fallbacks") or [],
        "subagents": bool(models_raw.get("subagents")),
    }
    return {
        "type": prov_type,
        "base_url": raw.get("base_url", DEFAULT_OLLAMA_BASE_URL),
        "api_key": raw.get("api_key") or None,
        "num_ctx": raw.get("num_ctx"),
        "think": raw.get("think"),
        "options": options,
        "model_options": model_options,
        "models": models,
    }


def _parse_model_ref(raw: Any) -> dict[str, Any] | None:
    """Parst eine vision Config-Referenz.
    Akzeptiert str ('llava:13b') oder dict ({model: 'llava:13b', num_ctx: 32768}).
    Gibt None zurück wenn nicht gesetzt."""
    if not raw:
        return None
    if isinstance(raw, str):
        return {"model": raw.strip()} if raw.strip() else None
    if isinstance(raw, dict):
        model = (raw.get("model") or "").strip()
        if not model:
            return None
        out: dict[str, Any] = {"model": model}
        if raw.get("num_ctx"):
            out["num_ctx"] = int(raw["num_ctx"])
        return out
    return None


def _parse_model_ref_list(raw: Any) -> list[str]:
    """Parst image_generation Config – gibt Liste von Modellnamen zurück.
    Akzeptiert str ('dall-e-3'), list (['dall-e-3', 'gemini-2.0-flash-exp']),
    oder dict ({model: 'dall-e-3'})."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict) and (item.get("model") or "").strip():
                out.append(item["model"].strip())
        return out
    if isinstance(raw, dict):
        model = (raw.get("model") or "").strip()
        return [model] if model else []
    return []


def _merge_with_defaults(data: dict[str, Any]) -> dict[str, Any]:
    providers_data = data.get("providers") or {}
    server = data.get("server") or {}
    # Alle Provider parsen (type bestimmt Protokoll: ollama, openai, ...)
    all_providers: dict[str, Any] = {}
    for key, val in providers_data.items():
        if not key or not isinstance(key, str) or not isinstance(val, dict):
            continue
        all_providers[key] = _parse_provider(val)
    # Wenn kein Provider definiert → leerer ollama-Default
    if not all_providers:
        all_providers["ollama"] = _parse_provider({})
    # Default-Provider = erster Provider (normalerweise "ollama")
    default_prov_name = next(iter(all_providers))
    models_merged = all_providers[default_prov_name]["models"]
    return {
        "providers": all_providers,
        "server": {
            "host": server.get("host", DEFAULT_BIND_HOST),
            "port": server.get("port", DEFAULT_BIND_PORT),
            "token": server.get("token"),  # None = generate on first run
            "debug": server.get("debug", False),  # true = Request/Response-JSON in API-Antwort
            "show_estimated_tokens": server.get("show_estimated_tokens", False),
            "log_agent_actions": server.get("log_agent_actions", False),
            "show_context": server.get("show_context", False),
        },
        "agent_dir": data.get("agent_dir") or _default_agent_dir(),
        "workspace": data.get("workspace") or _default_workspace(),
        "trash_dir": data.get("trash_dir") or _default_trash_dir(),
        "models": models_merged,  # Shortcut: models des Default-Providers
        "search_engines": _search_engines_merged(data),
        "default_search_engine": _default_search_engine_merged(data),
        "max_chars_per_file": data.get("max_chars_per_file", DEFAULT_MAX_CHARS_PER_FILE),
        "scheduler": data.get("scheduler") if data.get("scheduler") else False,  # false | { enabled: true }
        "chat_clients": _normalize_chat_clients(data),
        "matrix": _normalize_chat_clients(data).get("matrix"),  # Alias für Abwärtskompatibilität
        "onboarding_complete": bool(data.get("onboarding_complete", False)),  # erst true nach Speichern in UI oder CLI config
        "memory": {
            "max_chars_per_line": int((data.get("memory") or {}).get("max_chars_per_line", 300) or 300),
            "days": int((data.get("memory") or {}).get("days", 2) or 2),
            "max_tokens": int((data.get("memory") or {}).get("max_tokens", 4000) or 4000),
        },
        "chat": {
            "context_quota": float((data.get("chat") or {}).get("context_quota", 0.85) or 0.85),
        },
        "subagents": list(data.get("subagents") or []),
        "fallbacks": list(data.get("fallbacks") or []),
        "vision": _parse_model_ref_list(data.get("vision")),
        "image_generation": _parse_model_ref_list(data.get("image_generation")),
        "avatar": (data.get("avatar") or "").strip() or None,
        "github_token": (data.get("github_token") or "").strip() or None,
    }


def save_config(config: dict[str, Any], project_dir: str | None = None) -> Path:
    path = config_path(project_dir)
    if project_dir:
        path = Path(project_dir).resolve() / "miniassistant.yaml"
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_config(path)
    # Alle Provider speichern (ollama, ollama2, ...)
    all_providers = config.get("providers") or {}
    models_cfg = config.get("models") or {}
    out_providers: dict[str, Any] = {}
    for prov_name, prov_cfg in all_providers.items():
        if not prov_name or not isinstance(prov_name, str) or not isinstance(prov_cfg, dict):
            continue
        out_prov: dict[str, Any] = {
            "type": prov_cfg.get("type", "ollama"),
        }
        # Nur gesetzte Felder schreiben (kein null-Spam)
        if prov_cfg.get("base_url"):
            out_prov["base_url"] = prov_cfg["base_url"]
        if prov_cfg.get("api_key"):
            out_prov["api_key"] = prov_cfg["api_key"]
        if prov_cfg.get("num_ctx"):
            out_prov["num_ctx"] = prov_cfg["num_ctx"]
        if prov_cfg.get("think") is not None:
            out_prov["think"] = prov_cfg["think"]
        if prov_cfg.get("options"):
            out_prov["options"] = prov_cfg["options"]
        if prov_cfg.get("model_options"):
            out_prov["model_options"] = prov_cfg["model_options"]
        p_models = prov_cfg.get("models") or {}
        if p_models.get("default") or p_models.get("aliases") or p_models.get("list") or p_models.get("fallbacks") or p_models.get("subagents"):
            out_prov["models"] = {
                "default": p_models.get("default"),
                "aliases": p_models.get("aliases") or {},
                "list": p_models.get("list"),
                "fallbacks": p_models.get("fallbacks") or [],
                "subagents": p_models.get("subagents", False),
            }
        out_providers[prov_name] = out_prov
    if not out_providers:
        out_providers["ollama"] = {"type": "ollama"}
    out: dict[str, Any] = {
        "providers": out_providers,
        "server": {
            "host": config["server"].get("host", DEFAULT_BIND_HOST),
            "port": config["server"].get("port", DEFAULT_BIND_PORT),
            "token": config["server"].get("token"),
            "debug": config["server"].get("debug", False),
            "show_estimated_tokens": config["server"].get("show_estimated_tokens", False),
            "log_agent_actions": config["server"].get("log_agent_actions", False),
            "show_context": config["server"].get("show_context", False),
        },
        "agent_dir": config["agent_dir"],
        "workspace": config.get("workspace"),
        "search_engines": config.get("search_engines") or {},
        "default_search_engine": config.get("default_search_engine"),
        "max_chars_per_file": config.get("max_chars_per_file", DEFAULT_MAX_CHARS_PER_FILE),
        "scheduler": config.get("scheduler") or False,
        "chat_clients": {k: v for k, v in (config.get("chat_clients") or {}).items() if v} or False,
        "onboarding_complete": bool(config.get("onboarding_complete", False)),
        "memory": {
            "max_chars_per_line": (config.get("memory") or {}).get("max_chars_per_line", 300),
            "days": (config.get("memory") or {}).get("days", 2),
            "max_tokens": (config.get("memory") or {}).get("max_tokens", 4000),
        },
        "chat": {
            "context_quota": (config.get("chat") or {}).get("context_quota", 0.85),
        },
        "subagents": list(config.get("subagents") or []),
        "fallbacks": list(config.get("fallbacks") or []),
    }
    # vision / image_generation nur schreiben wenn gesetzt (als Liste)
    if config.get("vision"):
        out["vision"] = config["vision"]
    if config.get("image_generation"):
        out["image_generation"] = config["image_generation"]
    if config.get("avatar"):
        out["avatar"] = config["avatar"]
    if config.get("github_token"):
        out["github_token"] = config["github_token"]
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    path.chmod(0o600)
    return path


def ensure_token(config: dict[str, Any]) -> str:
    """Stellt sicher, dass server.token gesetzt ist; generiert einen und speichert Config."""
    import secrets
    token = (config.get("server") or {}).get("token")
    if not token:
        token = secrets.token_urlsafe(32)
        config.setdefault("server", {})["token"] = token
        save_config(config)
    return token
