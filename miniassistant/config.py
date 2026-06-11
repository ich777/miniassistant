"""
Config-Schema und Laden/Speichern für MiniAssistant.
Speicherort: ~/.config/miniassistant/config.yaml oder ./miniassistant.yaml
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
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

# Top-level Tuning-/Reliability-Keys, die als optionale Passthrough-Keys behandelt werden:
# beim Laden nur übernommen wenn in der YAML gesetzt, beim Speichern nur geschrieben wenn gesetzt.
# EINE Quelle für Laden UND Speichern — sonst droppt save_config Keys, die _merge_with_defaults
# akzeptiert (genau diese Bug-Klasse: image_edit_*, stream_loop_*, research_gate, … gingen über
# die Form-/save_config-UI verloren, obwohl die Raw-YAML-Bearbeitung sie behielt).
_PASSTHROUGH_TUNING_KEYS = (
    "api_timeout", "subagent_api_timeout", "invoke_model_timeout",
    "tool_execution_timeout", "subagent_execution_timeout", "schedule_timeout",
    "stream_stall_timeout", "stream_thinking_timeout", "stream_thinking_hard_timeout",
    "stream_round_timeout", "stream_loop_max_consecutive", "stream_loop_recovery_max",
    "stream_loop_freq_window", "stream_loop_freq_threshold", "stream_thinking_token_budget",
    "max_tool_rounds", "exec_max_output_chars", "exec_timeout_seconds",
    "search_engine_strategy", "prefs_max_chars", "prefs_max_chars_per_file",
    "respond_in_input_language",
    "doc_max_chars", "doc_max_pages_render", "doc_response_reserve",
    # Reliability/Context-Knobs: Guards + Lazy-Load + Reflection
    "url_hallucination_guard", "research_gate", "research_gate_max",
    "research_gate_keywords", "lazy_tools", "research_reflection",
    "link_resolution_guard", "tool_call_dedup",
    # Image-Edit-Tuning: strength-Default (1.0 = voller Denoise, nötig damit qwen-image-edit
    # & Co. die Instruktion anwenden); max_edge cappt Quell-Auflösung.
    "image_edit_strength", "image_edit_max_edge",
)

# ---------------------------------------------------------------------------
# Config-RAM-Cache: vermeidet Disk-I/O + YAML-Parse bei jedem Aufruf.
# Invalidiert automatisch nach _CONFIG_CACHE_TTL Sekunden und bei save_config.
# ---------------------------------------------------------------------------
_config_cache: dict[str, Any] | None = None
_config_cache_path: str = ""       # Pfad der gecachten Config
_config_cache_mtime: float = 0.0   # mtime der Datei beim Lesen
_config_cache_time: float = 0.0    # Zeitpunkt des letzten Lesens
_config_cache_lock = threading.Lock()
_CONFIG_CACHE_TTL = 10.0           # Sekunden — alle programmatischen Änderungen invalidieren sofort


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


def get_search_engine_for_request(config: dict[str, Any], engine_id: str | None = None) -> tuple[str | None, str | None]:
    """Gibt (url, eid) für eine Suchanfrage zurück, basierend auf search_engine_strategy.

    Strategien (config: search_engine_strategy):
      'first'    (default): bevorzuge erste/default Engine
      'random':             wähle zufällig eine Engine
      'specific':           nur default Engine, kein Fallback

    engine_id: expliziter Override, ignoriert Strategy.
    """
    import random as _random
    engines = config.get("search_engines") or {}
    if not engines:
        return None, None
    if engine_id:
        url = (engines.get(engine_id, {}).get("url") or "").strip()
        return (url or None), engine_id
    strategy = (config.get("search_engine_strategy") or "first").strip().lower()
    if strategy == "random":
        eid = _random.choice(list(engines.keys()))
    else:  # "first" oder "specific"
        eid = config.get("default_search_engine") or next(iter(engines), None)
    if not eid or eid not in engines:
        return None, None
    url = (engines[eid].get("url") or "").strip()
    return (url or None), eid


def get_voice_stt_url(config: dict[str, Any]) -> str | None:
    """Wyoming STT URL aus config.voice.stt.url, oder None wenn nicht konfiguriert."""
    voice = config.get("voice") or {}
    return (voice.get("stt") or {}).get("url") or None


def get_voice_tts_url(config: dict[str, Any]) -> str | None:
    """Wyoming TTS URL aus config.voice.tts.url, oder None wenn nicht konfiguriert."""
    voice = config.get("voice") or {}
    return (voice.get("tts") or {}).get("url") or None


def get_voice_language(config: dict[str, Any]) -> str:
    """STT-Sprache aus config.voice.language (oder voice.stt.language als Fallback), Default 'de'."""
    voice = config.get("voice") or {}
    return voice.get("language") or (voice.get("stt") or {}).get("language") or "de"


def get_voice_tts_voice(config: dict[str, Any]) -> str | None:
    """Voice-Name aus config.voice.tts_voice (oder voice.tts.voice als Fallback), oder None für Default."""
    voice = config.get("voice") or {}
    return voice.get("tts_voice") or (voice.get("tts") or {}).get("voice") or None


def get_voice_tts_model(config: dict[str, Any]) -> str | None:
    """TTS-Modellname aus config.voice.tts.model (z.B. 'vibevoice', 'kokoro'), oder None für Default."""
    voice = config.get("voice") or {}
    return (voice.get("tts") or {}).get("model") or None


def get_voice_tts_language(config: dict[str, Any]) -> str | None:
    """TTS-Sprache aus config.voice.tts.language, Fallback auf voice.language. None wenn nicht gesetzt."""
    voice = config.get("voice") or {}
    return (voice.get("tts") or {}).get("language") or voice.get("language") or None


def get_voice_tts_options(config: dict[str, Any]) -> dict[str, Any]:
    """Synthesis-Optionen für TTS aus config.voice.tts.*
    Gibt nur gesetzte Werte zurück (keine Defaults — der Server verwendet seine eigenen Defaults).

    Wyoming/Piper-Keys (float): noise_scale, noise_w, length_scale, sentence_silence
    OpenAI-compat/Chatterbox-Keys: seed (int), speed (float), response_format (str)
    Chatterbox-native-Keys (HTTP-Pfad /tts): voice_mode (str), cfg_weight (float),
        exaggeration (float), temperature (float), chunk_size (int), split_text (bool),
        speed_factor (float)
    Unbekannte Server ignorieren ihre fremden Felder — Forwarding ist gefahrlos.
    """
    tts = (config.get("voice") or {}).get("tts") or {}
    opts: dict[str, Any] = {}
    float_keys = (
        "noise_scale", "noise_w", "length_scale", "sentence_silence",
        "speed", "speed_factor", "cfg_weight", "exaggeration", "temperature",
    )
    int_keys = ("seed", "chunk_size")
    str_keys = ("response_format", "voice_mode")
    bool_keys = ("split_text",)
    for key in float_keys:
        val = tts.get(key)
        if val is not None:
            try:
                opts[key] = float(val)
            except (TypeError, ValueError):
                pass
    for key in int_keys:
        val = tts.get(key)
        if val is not None:
            try:
                opts[key] = int(val)
            except (TypeError, ValueError):
                pass
    for key in str_keys:
        val = tts.get(key)
        if val is not None:
            opts[key] = str(val)
    for key in bool_keys:
        val = tts.get(key)
        if val is not None:
            opts[key] = bool(val)
    return opts


def config_path(project_dir: str | None = None) -> Path:
    """Config-Datei: project_dir/miniassistant.yaml wenn project_dir gesetzt, sonst get_config_dir()/config.yaml."""
    if project_dir:
        return Path(project_dir).resolve() / "miniassistant.yaml"
    return Path(get_config_dir()) / CONFIG_FILENAME


def load_config(project_dir: str | None = None) -> dict[str, Any]:
    global _config_cache, _config_cache_path, _config_cache_mtime, _config_cache_time
    path = config_path(project_dir)
    path_str = str(path)

    # --- Cache prüfen (ohne Lock für den häufigen Hit-Pfad) ---
    now = time.monotonic()
    if (
        _config_cache is not None
        and _config_cache_path == path_str
        and now - _config_cache_time < _CONFIG_CACHE_TTL
    ):
        # Shallow-Copy: Caller dürfen temporäre Keys setzen (z.B. _chat_context)
        # ohne den Cache dauerhaft zu mutieren.
        return dict(_config_cache)

    with _config_cache_lock:
        # Double-check nach Lock-Acquire
        now = time.monotonic()
        if (
            _config_cache is not None
            and _config_cache_path == path_str
            and now - _config_cache_time < _CONFIG_CACHE_TTL
        ):
            return dict(_config_cache)

        if not path.exists():
            result = _default_config()
            _config_cache = result
            _config_cache_path = path_str
            _config_cache_mtime = 0.0
            _config_cache_time = now
            return dict(result)

        # mtime prüfen: wenn Datei sich nicht geändert hat, Cache verlängern
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        if (
            _config_cache is not None
            and _config_cache_path == path_str
            and current_mtime == _config_cache_mtime
        ):
            _config_cache_time = now
            return dict(_config_cache)

        # Datei lesen und parsen
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            msg = str(e)
            if "@" in msg:
                msg += ' Hinweis: Werte mit "@" (z. B. matrix.user_id) in Anführungszeichen setzen: user_id: "@bot:example.org"'
            raise RuntimeError(msg) from e
        merged = _merge_with_defaults(data)
        merged["_config_dir"] = str(path.parent.resolve())
        # Wichtige Verzeichnisse sicherstellen (workspace, agent_dir)
        for _dir_key in ("workspace", "agent_dir", "trash_dir"):
            _dir_val = (merged.get(_dir_key) or "").strip()
            if _dir_val:
                Path(_dir_val).expanduser().mkdir(parents=True, exist_ok=True)

        _config_cache = merged
        _config_cache_path = path_str
        _config_cache_mtime = current_mtime
        _config_cache_time = now
        return dict(merged)


def invalidate_config_cache() -> None:
    """Cache sofort invalidieren (nach Config-Speicherung)."""
    global _config_cache, _config_cache_time
    with _config_cache_lock:
        _config_cache = None
        _config_cache_time = 0.0


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
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".config_tmp_", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_str, 0o600)
        os.replace(tmp_str, path)
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise
    invalidate_config_cache()
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
    """Matrix-Config: enabled, homeserver, bot_name, user_id, token, device_id (optional), encrypted_rooms (bool)."""
    if not matrix or not isinstance(matrix, dict):
        return None
    m = matrix
    homeserver = (m.get("homeserver") or "").strip()
    token = (m.get("token") or "").strip()
    if not homeserver or not token:
        return None
    user_id = (m.get("user_id") or "").strip() or None
    encrypted_rooms = m.get("encrypted_rooms")
    if encrypted_rooms is None:
        encrypted_rooms = True
    out = {
        "enabled": bool(m.get("enabled", True)),
        "homeserver": homeserver,
        "bot_name": (m.get("bot_name") or "MiniAssistant").strip() or "MiniAssistant",
        "user_id": user_id,
        "token": token,
        "device_id": (m.get("device_id") or "").strip() or None,
        "encrypted_rooms": bool(encrypted_rooms),
    }
    # Per-room response modes: {room_id: "always"|"mention"|"off"}
    rm = m.get("room_modes")
    if isinstance(rm, dict) and rm:
        clean = {str(k): str(v).strip().lower() for k, v in rm.items()
                 if isinstance(k, str) and str(v).strip().lower() in ("always", "mention", "off")}
        if clean:
            out["room_modes"] = clean
    # Per-room group-mode settings: {room_id: {context, language, tools_allow, workspace_subdir,
    #   auto_context_count, auto_context_max_chars, docs_in_sandbox}}
    rs = m.get("room_settings")
    if isinstance(rs, dict) and rs:
        clean_rs = {str(k): v for k, v in rs.items() if isinstance(k, str) and isinstance(v, dict)}
        if clean_rs:
            out["room_settings"] = clean_rs
    return out


def _normalize_discord(discord: Any) -> dict[str, Any] | None:
    """Discord-Config: enabled, bot_token, command_prefix (optional)."""
    if not discord or not isinstance(discord, dict):
        return None
    d = discord
    bot_token = (d.get("bot_token") or "").strip()
    if not bot_token:
        return None
    out = {
        "enabled": bool(d.get("enabled", True)),
        "bot_token": bot_token,
        "command_prefix": (d.get("command_prefix") or "!").strip() or "!",
    }
    cm = d.get("channel_modes")
    if isinstance(cm, dict) and cm:
        clean = {str(k): str(v).strip().lower() for k, v in cm.items()
                 if isinstance(k, str) and str(v).strip().lower() in ("always", "mention", "off")}
        if clean:
            out["channel_modes"] = clean
    # Per-channel group-mode settings: same shape as room_settings für Matrix
    cs = d.get("channel_settings")
    if isinstance(cs, dict) and cs:
        clean_cs = {str(k): v for k, v in cs.items() if isinstance(k, str) and isinstance(v, dict)}
        if clean_cs:
            out["channel_settings"] = clean_cs
    return out


def _normalize_email_account(raw: Any) -> dict[str, Any] | None:
    """Ein einzelnes E-Mail-Konto normalisieren."""
    if not raw or not isinstance(raw, dict):
        return None
    username = (raw.get("username") or "").strip()
    password = (raw.get("password") or "").strip()
    if not username or not password:
        return None
    ssl_val = raw.get("ssl")
    return {
        "imap_server": (raw.get("imap_server") or "").strip(),
        "imap_port": int(raw.get("imap_port") or 993),
        "smtp_server": (raw.get("smtp_server") or "").strip(),
        "smtp_port": int(raw.get("smtp_port") or 587),
        "username": username,
        "password": password,
        "ssl": bool(ssl_val if ssl_val is not None else True),
        "name": (raw.get("name") or username).strip(),
    }


def _normalize_email(data: dict[str, Any]) -> dict[str, Any] | None:
    """email: { accounts: {name: {...}}, default: name } normalisieren."""
    raw = data.get("email")
    if not raw or not isinstance(raw, dict):
        return None
    accounts_raw = raw.get("accounts") or {}
    if not isinstance(accounts_raw, dict) or not accounts_raw:
        return None
    accounts: dict[str, Any] = {}
    for k, v in accounts_raw.items():
        if not k or not isinstance(k, str):
            continue
        acc = _normalize_email_account(v)
        if acc:
            accounts[k] = acc
    if not accounts:
        return None
    default = (raw.get("default") or "").strip() or next(iter(accounts))
    if default not in accounts:
        default = next(iter(accounts))
    return {"accounts": accounts, "default": default}


def _normalize_webhooks_cfg(raw: Any) -> dict[str, Any] | bool:
    """Normalize webhooks: section. Returns False when disabled, dict otherwise.
    Defaults: enabled=False, rate_limit_per_min=10, output_keep_last=10, parallel=True, max_retries=3.
    """
    if raw is None or raw is False:
        return False
    if raw is True:
        raw = {"enabled": True}
    if not isinstance(raw, dict):
        return False
    return {
        "enabled": bool(raw.get("enabled", False)),
        "rate_limit_per_min": int(raw.get("rate_limit_per_min", 10) or 10),
        "output_keep_last": int(raw.get("output_keep_last", 10) or 0),
        "parallel": bool(raw.get("parallel", True)),
        "max_retries": int(raw.get("max_retries", 3) or 3),
    }


def _normalize_chat_clients(data: dict[str, Any]) -> dict[str, Any]:
    """Normalisiert chat_clients."""
    cc = data.get("chat_clients") or {}
    if not isinstance(cc, dict):
        cc = {}
    return {
        "matrix": _normalize_matrix(cc.get("matrix")),
        "discord": _normalize_discord(cc.get("discord")),
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
        "no_api_tools": bool(raw.get("no_api_tools", False)),
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


def _normalize_mempalace_language(raw: Any) -> list[str]:
    """mempalace.language: str ("de") oder list (["de","en"]) → list. Leer = []."""
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return parts
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    return []


def _clean_mempalace_for_save(mp: dict[str, Any]) -> dict[str, Any]:
    """Bereinigt mempalace-Dict für YAML-Output: leere Strings/Listen weglassen,
    language mit einem Eintrag als String schreiben statt als Liste."""
    out: dict[str, Any] = {}
    for k, v in mp.items():
        if v in ("", [], None, {}):
            continue
        if k == "language" and isinstance(v, list) and len(v) == 1:
            out[k] = v[0]
        else:
            out[k] = v
    return out


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
            # X-Forwarded-For/X-Real-IP nur hinter vertrauenswürdigem Reverse-Proxy trusten.
            # Default False: ohne Proxy darf kein Client seine IP spoofen (Rate-Limit/Brute-Force-Bypass).
            "trust_forwarded": bool(server.get("trust_forwarded", False)),
            # Requests/Minute pro IP für / und /v1 (0 = aus). raw_proxy hat eigenes Limit.
            "rate_limit": int(server.get("rate_limit", 100) or 0),
            # Bekannte Config-Secrets im LLM-Output + Tool-Ergebnissen maskieren (Defense-in-Depth).
            "mask_secrets_in_output": bool(server.get("mask_secrets_in_output", True)),
            "debug": server.get("debug", False),  # true = Request/Response-JSON in API-Antwort
            "show_estimated_tokens": server.get("show_estimated_tokens", False),
            "log_agent_actions": server.get("log_agent_actions", False),
            "show_context": server.get("show_context", False),
            "track_usage": server.get("track_usage", False),
        },
        "agent_dir": data.get("agent_dir") or _default_agent_dir(),
        "workspace": data.get("workspace") or _default_workspace(),
        "trash_dir": data.get("trash_dir") or _default_trash_dir(),
        "models": models_merged,  # Shortcut: models des Default-Providers
        "search_engines": _search_engines_merged(data),
        "default_search_engine": _default_search_engine_merged(data),
        "max_chars_per_file": data.get("max_chars_per_file", DEFAULT_MAX_CHARS_PER_FILE),
        "scheduler": data.get("scheduler") if data.get("scheduler") else False,  # false | { enabled: true }
        "webhooks": _normalize_webhooks_cfg(data.get("webhooks")),
        "chat_clients": _normalize_chat_clients(data),
        "onboarding_complete": bool(data.get("onboarding_complete", False)),  # erst true nach Speichern in UI oder CLI config
        "memory": {
            "enabled": bool((data.get("memory") or {}).get("enabled", True)),
            "max_chars_per_line": int((data.get("memory") or {}).get("max_chars_per_line", 300) or 300),
            "days": int((data.get("memory") or {}).get("days", 2) or 2),
            "max_tokens": int((data.get("memory") or {}).get("max_tokens", 4000) or 4000),
            "track_user_id": bool((data.get("memory") or {}).get("track_user_id", False)),
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
        "email": _normalize_email(data),
        "voice": data.get("voice") or None,
        "read_url": data.get("read_url") or {},
        "raw_proxy": {
            "enabled": bool((data.get("raw_proxy") or {}).get("enabled", False)),
            "token": (data.get("raw_proxy") or {}).get("token"),
            "rate_limit": int((data.get("raw_proxy") or {}).get("rate_limit", 100) or 100),
            "allowed_models": list((data.get("raw_proxy") or {}).get("allowed_models") or []),
            # Slot-Cache am /raw/v1 default OFF (User-controlled prompts → wenig stabiler Prefix)
            "slot_cache": bool((data.get("raw_proxy") or {}).get("slot_cache", False)),
        },
        "slot_cache": {
            "enabled": bool((data.get("slot_cache") or {}).get("enabled", False)),
            "ttl_days": int((data.get("slot_cache") or {}).get("ttl_days", 14) or 14),
            "max_files": int((data.get("slot_cache") or {}).get("max_files", 40) or 40),
            "min_tokens_to_cache": int((data.get("slot_cache") or {}).get("min_tokens_to_cache", 10000) or 10000),
            "llama_swap_url": ((data.get("slot_cache") or {}).get("llama_swap_url") or "").strip(),
            "remote_slot_path": ((data.get("slot_cache") or {}).get("remote_slot_path") or "/slots").strip() or "/slots",
        },
        "mempalace": {
            # Memory-Master-Switch: wenn memory.enabled=false, ist mempalace zwangsweise aus.
            "enabled": bool((data.get("memory") or {}).get("enabled", True)) and bool((data.get("mempalace") or {}).get("enabled", False)),
            "wing": (data.get("mempalace") or {}).get("wing", "miniassistant"),
            "default_room": (data.get("mempalace") or {}).get("default_room", "conversations"),
            "max_tokens": int((data.get("mempalace") or {}).get("max_tokens", 900) or 900),
            "palace_path": (data.get("mempalace") or {}).get("palace_path", ""),
            "identity_path": (data.get("mempalace") or {}).get("identity_path", ""),
            "language": _normalize_mempalace_language((data.get("mempalace") or {}).get("language")),
        },
        # Top-level Tuning-Keys: nur einfügen wenn in YAML gesetzt (Key fehlt → Caller-Default greift)
        **{k: data[k] for k in _PASSTHROUGH_TUNING_KEYS if k in data and data[k] is not None},
    }


_save_lock = threading.Lock()


def save_config_atomic(updater, project_dir: str | None = None) -> Path:
    """Race-safe config save: lock + re-load from disk + apply updater(config) + save.

    Verhindert Lost-Update wenn mehrere Pfade gleichzeitig speichern wollen
    (z.B. matrix_bot ensure_default_group_settings + webui PATCH /api/rooms).

    `updater(config)` muss `config` in-place mutieren. Rückgabewert ignoriert.
    """
    with _save_lock:
        config = load_config(project_dir)
        updater(config)
        return save_config(config, project_dir)


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
        if prov_cfg.get("no_api_tools"):
            out_prov["no_api_tools"] = prov_cfg["no_api_tools"]
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
            "trust_forwarded": bool(config["server"].get("trust_forwarded", False)),
            "rate_limit": int(config["server"].get("rate_limit", 100) or 0),
            "mask_secrets_in_output": bool(config["server"].get("mask_secrets_in_output", True)),
            "debug": config["server"].get("debug", False),
            "show_estimated_tokens": config["server"].get("show_estimated_tokens", False),
            "log_agent_actions": config["server"].get("log_agent_actions", False),
            "show_context": config["server"].get("show_context", False),
            "track_usage": config["server"].get("track_usage", False),
        },
        "agent_dir": config["agent_dir"],
        "workspace": config.get("workspace"),
        "search_engines": config.get("search_engines") or {},
        "default_search_engine": config.get("default_search_engine"),
        "max_chars_per_file": config.get("max_chars_per_file", DEFAULT_MAX_CHARS_PER_FILE),
        "scheduler": config.get("scheduler") or False,
        "webhooks": config.get("webhooks") or False,
        "chat_clients": {k: v for k, v in _normalize_chat_clients(config).items() if v} or False,
        "onboarding_complete": bool(config.get("onboarding_complete", False)),
        "memory": {
            "enabled": bool((config.get("memory") or {}).get("enabled", True)),
            "max_chars_per_line": (config.get("memory") or {}).get("max_chars_per_line", 300),
            "days": (config.get("memory") or {}).get("days", 2),
            "max_tokens": (config.get("memory") or {}).get("max_tokens", 4000),
            "track_user_id": (config.get("memory") or {}).get("track_user_id", False),
        },
        "chat": {
            "context_quota": (config.get("chat") or {}).get("context_quota", 0.85),
        },
        "subagents": list(config.get("subagents") or []),
        "fallbacks": list(config.get("fallbacks") or []),
        "raw_proxy": config.get("raw_proxy") or {},
        "mempalace": _clean_mempalace_for_save(config.get("mempalace") or {}),
    }
    if config.get("trash_dir"):
        out["trash_dir"] = config["trash_dir"]
    if config.get("read_url"):
        out["read_url"] = config["read_url"]
    # vision / image_generation nur schreiben wenn gesetzt (als Liste)
    if config.get("vision"):
        out["vision"] = config["vision"]
    if config.get("image_generation"):
        out["image_generation"] = config["image_generation"]
    if config.get("avatar"):
        out["avatar"] = config["avatar"]
    if config.get("github_token"):
        out["github_token"] = config["github_token"]
    if config.get("voice"):
        out["voice"] = config["voice"]
    # Top-level Tuning-Keys: nur schreiben wenn gesetzt (kein Default-Spam in YAML).
    # Gleiche Key-Liste wie beim Laden (_PASSTHROUGH_TUNING_KEYS) — sonst droppt Speichern Keys,
    # die das Laden akzeptiert (z. B. image_edit_*, stream_loop_*, research_gate via Form-UI).
    for _k in _PASSTHROUGH_TUNING_KEYS:
        _v = config.get(_k)
        if _v is not None:
            out[_k] = _v
    email_norm = _normalize_email(config)
    if email_norm:
        out["email"] = email_norm
    # Atomarer Schreibvorgang: erst temp-Datei, dann os.replace() (POSIX-atomar)
    # Verhindert halb-geschriebene Config bei parallelem Start (z.B. serve + token)
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".config_tmp_", suffix=".yaml")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(out, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    invalidate_config_cache()
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


def ensure_raw_proxy_token(config: dict[str, Any]) -> str | None:
    """Stellt sicher, dass raw_proxy.token gesetzt ist wenn raw_proxy.enabled=True.
    Gibt None zurück wenn raw_proxy nicht aktiviert ist."""
    import secrets
    raw_cfg = config.get("raw_proxy") or {}
    if not raw_cfg.get("enabled", False):
        return None
    token = raw_cfg.get("token")
    if not token:
        token = secrets.token_urlsafe(32)
        config.setdefault("raw_proxy", {})["token"] = token
        save_config(config)
    return token
