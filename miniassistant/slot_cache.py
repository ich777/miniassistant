"""
Slot Cache — persistiert llama.cpp KV-Cache pro Conversation.

Kontext:
- llama.cpp/llama-swap hat /slots-API mit save/restore-Aktionen.
- KV-Cache pro Slot wird auf disk persistiert (auf LLM-Server unter --slot-save-path).
- MA tracked Mapping conv_id → filename in JSON-File.
- Bei nächstem Request derselben Conversation → restore → schneller prompt-eval.

Architektur:
- MA hat KEINEN Filesystem-Zugriff auf Slot-Files (separate Maschine).
- Alles via HTTP-API: POST /upstream/{model}/slots/{id}?action=save|restore&filename=X
- Storage-Cleanup auf LLM-Server-Seite via cron empfohlen
  (file-delete via API existiert nicht in llama.cpp).

Default OFF — opt-in via config: slot_cache.enabled=true.

Siehe miniassistant/docs/plan_slot_cache.md für vollständigen Plan.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from miniassistant.ollama_client import resolve_model, get_base_url_for_model

_log = logging.getLogger("miniassistant.slot_cache")

_lock = threading.Lock()
_slot_cache_response_cache: dict[str, tuple[float, Any]] = {}  # url -> (ts, slots)
_SLOTS_CACHE_TTL = 3.0  # s — kurz, damit parallel-Requests gemeinsam einen GET nutzen

# Modelle die llama.cpp 501 zurückgibt (multimodal, etc.) → in dieser Session nicht mehr
# versuchen. Wird beim Restart geleert (intentional, falls llama-swap Config geändert wurde).
_unsupported_models: set[str] = set()
_unsupported_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Instance ID
# ---------------------------------------------------------------------------

def _instance_id_path(config: dict[str, Any]) -> Path:
    config_dir = config.get("_config_dir") or ""
    if not config_dir:
        from miniassistant.config import get_config_dir
        config_dir = get_config_dir()
    return Path(config_dir) / "instance_id"


def get_instance_id(config: dict[str, Any]) -> str:
    """Liest oder erstellt die Instance-ID. UUID4-Hex (8 Zeichen)."""
    p = _instance_id_path(config)
    if p.exists():
        try:
            iid = p.read_text(encoding="utf-8").strip()
            if iid:
                return iid
        except OSError:
            pass
    iid = uuid.uuid4().hex[:8]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(iid + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as e:
        _log.warning("slot_cache: failed to persist instance_id: %s", e)
    return iid


# ---------------------------------------------------------------------------
# JSON-State
# ---------------------------------------------------------------------------

def _state_path(config: dict[str, Any]) -> Path:
    config_dir = config.get("_config_dir") or ""
    if not config_dir:
        from miniassistant.config import get_config_dir
        config_dir = get_config_dir()
    return Path(config_dir) / "slot_cache.json"


def _empty_state(instance_id: str) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "entries": [],
        "stats": {
            "hits_7d": 0,
            "misses_7d": 0,
            "saves_7d": 0,
            "last_reset": int(time.time()),
        },
    }


def _load_state(config: dict[str, Any]) -> dict[str, Any]:
    """Lädt State. Returns immer ein gültiges Dict — bei Korruption: leerer Reset."""
    iid = get_instance_id(config)
    p = _state_path(config)
    with _lock:
        if not p.exists():
            return _empty_state(iid)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _empty_state(iid)
            data.setdefault("instance_id", iid)
            data.setdefault("entries", [])
            data.setdefault("stats", {"hits_7d": 0, "misses_7d": 0, "saves_7d": 0, "last_reset": int(time.time())})
            if not isinstance(data["entries"], list):
                data["entries"] = []
            return data
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("slot_cache: state file corrupt, resetting (%s)", e)
            return _empty_state(iid)


def _save_state(config: dict[str, Any], data: dict[str, Any]) -> None:
    """Atomic write des State-Files."""
    p = _state_path(config)
    with _lock:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            _log.warning("slot_cache: failed to save state: %s", e)


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------

def _maybe_reset_stats(stats: dict[str, Any]) -> None:
    """Reset 7-Tages-Counter wenn last_reset > 7d her."""
    now = int(time.time())
    if now - int(stats.get("last_reset", 0)) > 7 * 86400:
        stats["hits_7d"] = 0
        stats["misses_7d"] = 0
        stats["saves_7d"] = 0
        stats["last_reset"] = now


def _bump_stat(config: dict[str, Any], key: str) -> None:
    """Inkrementiert einen Stat-Counter im State."""
    try:
        data = _load_state(config)
        _maybe_reset_stats(data["stats"])
        data["stats"][key] = int(data["stats"].get(key, 0)) + 1
        _save_state(config, data)
    except Exception as e:
        _log.debug("slot_cache: stat bump failed: %s", e)


# ---------------------------------------------------------------------------
# Config-Resolution
# ---------------------------------------------------------------------------

def _slot_cache_global(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("slot_cache") or {}


def is_globally_enabled(config: dict[str, Any]) -> bool:
    return bool(_slot_cache_global(config).get("enabled", False))


def _is_enabled_for_model(config: dict[str, Any], model: str) -> bool:
    """Resolve: global enabled AND (model_options.slot_cache OR provider.slot_cache OR True)."""
    if not is_globally_enabled(config):
        return False
    # Provider + Model auflösen
    from miniassistant.ollama_client import get_provider_config
    prov_cfg, clean = get_provider_config(config, model)
    if not isinstance(prov_cfg, dict):
        return True  # kein Provider → default an wenn global an
    # Per-model override
    per_model = prov_cfg.get("model_options") or {}
    lookup = clean or model
    if isinstance(per_model, dict) and lookup in per_model:
        m_opts = per_model[lookup]
        if isinstance(m_opts, dict) and "slot_cache" in m_opts:
            return bool(m_opts["slot_cache"])
    # Provider-default
    if "slot_cache" in prov_cfg:
        return bool(prov_cfg["slot_cache"])
    # Global an, kein override → an
    return True


def is_enabled_for(config: dict[str, Any], model: str, endpoint: str) -> bool:
    """Endpoint-Toggle + Model-Toggle prüfen.

    endpoint: 'web' (track=true), 'api' (/v1), 'raw' (/raw/v1), 'matrix', 'discord'.
    """
    if not _is_enabled_for_model(config, model):
        return False
    if endpoint == "raw":
        # /raw/v1 default OFF (User-controlled prompts → wenig stabiler Prefix)
        return bool((config.get("raw_proxy") or {}).get("slot_cache", False))
    # Andere Endpoints: an wenn Modell an
    return True


def _provider_type_for(config: dict[str, Any], model: str) -> str:
    """Returns provider type ('openai-compat', 'openai', 'ollama', etc.)."""
    from miniassistant.ollama_client import get_provider_type
    return get_provider_type(config, model)


def normalize_model_name(config: dict[str, Any], model: str) -> str:
    """Auflösung: alias → real name, ohne provider-prefix."""
    real = resolve_model(config, model) or model
    if "/" in real:
        return real.split("/", 1)[-1]
    return real


def llama_swap_url_for(config: dict[str, Any], model: str) -> str | None:
    """Base-URL des llama-swap-Servers für das Modell. None wenn unklar."""
    explicit = (_slot_cache_global(config).get("llama_swap_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    # Aus Provider-Config ableiten
    base = get_base_url_for_model(config, model)
    return base.rstrip("/") if base else None


# ---------------------------------------------------------------------------
# conv_id Derivation
# ---------------------------------------------------------------------------

def derive_conv_id(platform: str, **kwargs: Any) -> str | None:
    """Erzeugt eine deterministische conv_id pro Platform.

    web:    derive_conv_id('web', session_id=...)
    api:    derive_conv_id('api', messages=[...])  → hash über history
    matrix: derive_conv_id('matrix', room_id=..., user_id=...)
    discord:derive_conv_id('discord', channel_id=..., user_id=...)
    """
    if platform == "web":
        sid = kwargs.get("session_id")
        if not sid:
            return None
        return f"web:{sid}"
    if platform == "api":
        messages = kwargs.get("messages") or []
        if not messages:
            return None
        h = hashlib.sha256()
        for m in messages:
            if isinstance(m, dict):
                h.update(f"{m.get('role','')}::{m.get('content','')}".encode("utf-8", errors="replace"))
                h.update(b"\n")
        return f"api:{h.hexdigest()[:16]}"
    if platform == "matrix":
        rid = kwargs.get("room_id"); uid = kwargs.get("user_id")
        if not rid or not uid:
            return None
        return f"matrix:{rid}:{uid}"
    if platform == "discord":
        cid = kwargs.get("channel_id"); uid = kwargs.get("user_id")
        if not cid or not uid:
            return None
        return f"discord:{cid}:{uid}"
    return None


def _filename_for(instance_id: str, conv_id: str) -> str:
    """{instance_id}_{conv_hash}.bin — eindeutig pro MA-Instance."""
    h = hashlib.sha256(conv_id.encode("utf-8")).hexdigest()[:16]
    return f"{instance_id}_{h}.bin"


# ---------------------------------------------------------------------------
# Slot-Discovery via /slots
# ---------------------------------------------------------------------------

def _fetch_slots(llama_swap_base: str, model_normalized: str) -> list[dict[str, Any]] | None:
    """GET /upstream/{model}/slots, mit kurzem in-process Cache."""
    url = f"{llama_swap_base}/upstream/{model_normalized}/slots"
    now = time.monotonic()
    cached = _slot_cache_response_cache.get(url)
    if cached and (now - cached[0]) < _SLOTS_CACHE_TTL:
        return cached[1]
    try:
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            _slot_cache_response_cache[url] = (now, data)
            return data
    except Exception as e:
        _log.debug("slot_cache: /slots fetch failed (%s) %s", url, e)
    return None


def _common_prefix_len(a: str, b: str) -> int:
    """Längstes gemeinsames Prefix in chars."""
    if not a or not b:
        return 0
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _pick_slot_for_save(slots: list[dict[str, Any]], current_prompt: str, min_match: int = 256) -> int | None:
    """Findet den Slot der zuletzt benutzt wurde (höchstes id_task, idle).

    Hintergrund: neuere llama.cpp-Versionen geben kein 'prompt' mehr im /slots-Response zurück
    (immer leer). Deshalb fallback auf id_task: monotonic-counter, idle slot mit höchstem
    id_task ist mit hoher Wahrscheinlichkeit der, der unseren Request gerade abgearbeitet hat.

    Wenn falscher Slot gespeichert wird: kein Korrektheits-Schaden — beim Restore mismatcht
    llama.cpp's Prefix-Cache und re-evaluiert. Nur weniger Cache-Hit-Rate bei Multi-Instance.

    current_prompt wird falls nicht-leerer prompt-string in slots vorhanden trotzdem zum Match
    genutzt (für ältere llama.cpp Versionen).
    """
    # Versuch 1: prompt-string-match (alte llama.cpp)
    best_id: int | None = None
    best_match = 0
    for s in slots:
        if not isinstance(s, dict):
            continue
        sp = s.get("prompt")
        if isinstance(sp, str) and sp:
            m = _common_prefix_len(sp, current_prompt)
            if m > best_match and m >= min_match:
                best_match = m
                best_id = int(s.get("id", -1))
    if best_id is not None:
        return best_id
    # Versuch 2: höchster id_task, idle (neue llama.cpp)
    best_task = -1
    for s in slots:
        if not isinstance(s, dict):
            continue
        if s.get("is_processing", False):
            continue
        tid = s.get("id_task")
        if tid is None:
            continue
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        if tid_int > best_task:
            best_task = tid_int
            best_id = int(s.get("id", -1))
    return best_id


def _pick_free_slot_for_restore(slots: list[dict[str, Any]]) -> int | None:
    """Findet einen freien Slot zum Reinladen."""
    for s in slots:
        if not isinstance(s, dict):
            continue
        if not s.get("is_processing", False):
            return int(s.get("id", -1))
    return None


# ---------------------------------------------------------------------------
# Public: save / restore / invalidate
# ---------------------------------------------------------------------------

def save_after_round(
    config: dict[str, Any],
    conv_id: str,
    model: str,
    prompt_token_count: int,
    current_prompt: str,
    endpoint: str = "api",
) -> bool:
    """Save current slot to disk for later restore. Returns True wenn save erfolgreich.
    Sollte in eigenem Thread aufgerufen werden (fire-and-forget).
    """
    try:
        if not is_enabled_for(config, model, endpoint):
            return False
        gcfg = _slot_cache_global(config)
        min_tok = int(gcfg.get("min_tokens_to_cache", 10000) or 10000)
        if prompt_token_count < min_tok:
            return False

        # Nur openai-compat (llama.cpp) — bei OpenAI/Anthropic kein Slot-Konzept.
        if _provider_type_for(config, model) != "openai-compat":
            return False

        base = llama_swap_url_for(config, model)
        if not base:
            return False
        model_norm = normalize_model_name(config, model)

        slots = _fetch_slots(base, model_norm)
        if slots is None:
            return False

        slot_id = _pick_slot_for_save(slots, current_prompt)
        if slot_id is None:
            _log.debug("slot_cache: no matching slot found for save (conv=%s)", conv_id)
            return False

        iid = get_instance_id(config)
        filename = _filename_for(iid, conv_id)

        # Modell vorher 501 → skip (llama.cpp multimodal: slot-save not supported)
        with _unsupported_lock:
            if model_norm in _unsupported_models:
                return False

        url = f"{base}/upstream/{model_norm}/slots/{slot_id}"
        try:
            # llama.cpp /slots erwartet JSON-Body mit filename
            r = httpx.post(url, params={"action": "save"},
                           headers={"Content-Type": "application/json"},
                           json={"filename": filename}, timeout=30.0)
            if r.status_code == 501:
                _log.warning("slot_cache: model %s does not support slot save (501) — disabling for this session", model_norm)
                with _unsupported_lock:
                    _unsupported_models.add(model_norm)
                return False
            if r.status_code != 200:
                _log.debug("slot_cache: save returned %s for %s body=%s", r.status_code, filename, r.text[:200])
                return False
        except Exception as e:
            _log.warning("slot_cache: save HTTP failed: %s", e)
            return False

        # Update State
        data = _load_state(config)
        now = int(time.time())
        # Existing entry für (conv_id, model_norm) entfernen, neuen anhängen
        data["entries"] = [
            e for e in data["entries"]
            if not (e.get("conv_id") == conv_id and e.get("model") == model_norm)
        ]
        data["entries"].append({
            "conv_id": conv_id,
            "model": model_norm,
            "filename": filename,
            "prompt_token_count": int(prompt_token_count),
            "last_used_ts": now,
            "created_ts": now,
        })
        _maybe_reset_stats(data["stats"])
        data["stats"]["saves_7d"] = int(data["stats"].get("saves_7d", 0)) + 1
        _save_state(config, data)
        _log.info("slot_cache: saved %s tokens=%d slot=%d", filename, prompt_token_count, slot_id)
        # Cleanup nach Save (LRU + TTL)
        cleanup_lru_and_ttl(config)
        return True
    except Exception as e:
        _log.warning("slot_cache: save_after_round error: %s", e)
        return False


def restore_before_round(
    config: dict[str, Any],
    conv_id: str,
    model: str,
    endpoint: str = "api",
) -> bool:
    """Restore Slot-File für conv_id. Returns True bei Erfolg.
    Synchron (User wartet ~30-200ms), spart aber 5-30s Re-Eval.
    """
    try:
        if not is_enabled_for(config, model, endpoint):
            return False
        if _provider_type_for(config, model) != "openai-compat":
            return False

        model_norm = normalize_model_name(config, model)
        data = _load_state(config)
        entry = next(
            (e for e in data["entries"] if e.get("conv_id") == conv_id and e.get("model") == model_norm),
            None,
        )
        if not entry:
            _bump_stat(config, "misses_7d")
            return False

        base = llama_swap_url_for(config, model)
        if not base:
            return False

        slots = _fetch_slots(base, model_norm)
        if slots is None:
            return False
        free_id = _pick_free_slot_for_restore(slots)
        if free_id is None:
            _log.debug("slot_cache: no free slot for restore (conv=%s)", conv_id)
            return False

        # Modell als unsupported markiert → skip
        with _unsupported_lock:
            if model_norm in _unsupported_models:
                _bump_stat(config, "misses_7d")
                return False

        url = f"{base}/upstream/{model_norm}/slots/{free_id}"
        filename = entry["filename"]
        try:
            r = httpx.post(url, params={"action": "restore"},
                           headers={"Content-Type": "application/json"},
                           json={"filename": filename}, timeout=30.0)
            if r.status_code == 501:
                _log.warning("slot_cache: model %s does not support slot restore (501) — disabling for this session", model_norm)
                with _unsupported_lock:
                    _unsupported_models.add(model_norm)
                _bump_stat(config, "misses_7d")
                return False
            if r.status_code != 200:
                _log.info("slot_cache: restore failed %s status=%s — dropping entry", filename, r.status_code)
                # File evtl. weg auf LLM-Server → DB-Entry löschen
                data["entries"] = [e for e in data["entries"] if e is not entry]
                _save_state(config, data)
                _bump_stat(config, "misses_7d")
                return False
        except Exception as e:
            _log.warning("slot_cache: restore HTTP failed: %s", e)
            _bump_stat(config, "misses_7d")
            return False

        # touch last_used_ts
        entry["last_used_ts"] = int(time.time())
        _maybe_reset_stats(data["stats"])
        data["stats"]["hits_7d"] = int(data["stats"].get("hits_7d", 0)) + 1
        _save_state(config, data)
        _log.info("slot_cache: restored %s into slot=%d", filename, free_id)
        return True
    except Exception as e:
        _log.warning("slot_cache: restore_before_round error: %s", e)
        return False


def invalidate(config: dict[str, Any], conv_id: str, model: str | None = None) -> int:
    """Entfernt Cache-Entry(s). Wenn model=None, alle Modelle für conv_id.
    Returns Anzahl entfernter Entries.
    """
    try:
        data = _load_state(config)
        before = len(data["entries"])
        if model:
            model_norm = normalize_model_name(config, model)
            data["entries"] = [
                e for e in data["entries"]
                if not (e.get("conv_id") == conv_id and e.get("model") == model_norm)
            ]
        else:
            data["entries"] = [e for e in data["entries"] if e.get("conv_id") != conv_id]
        removed = before - len(data["entries"])
        if removed:
            _save_state(config, data)
            _log.info("slot_cache: invalidated %d entries for conv=%s", removed, conv_id)
        return removed
    except Exception as e:
        _log.warning("slot_cache: invalidate error: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_lru_and_ttl(config: dict[str, Any]) -> int:
    """LRU + TTL Cleanup. Returns Anzahl entfernter Entries.
    NOTE: löscht nur in MA-State. Files auf LLM-Server bleiben (kein API-Delete) —
    dort User-Cron einrichten: find /slots -mtime +N -delete.
    """
    try:
        gcfg = _slot_cache_global(config)
        max_files = int(gcfg.get("max_files", 40) or 40)
        ttl_days = int(gcfg.get("ttl_days", 14) or 14)
        ttl_cutoff = int(time.time()) - ttl_days * 86400

        data = _load_state(config)
        before = len(data["entries"])
        # TTL
        data["entries"] = [e for e in data["entries"] if int(e.get("last_used_ts", 0)) >= ttl_cutoff]
        # LRU bei Überschreitung von max_files
        if len(data["entries"]) > max_files:
            data["entries"].sort(key=lambda e: int(e.get("last_used_ts", 0)), reverse=True)
            data["entries"] = data["entries"][:max_files]
        removed = before - len(data["entries"])
        if removed:
            _save_state(config, data)
            _log.info("slot_cache: cleanup removed %d entries (TTL+LRU)", removed)
        return removed
    except Exception as e:
        _log.warning("slot_cache: cleanup error: %s", e)
        return 0


def cleanup_unknown_models(config: dict[str, Any]) -> int:
    """Entfernt Entries deren Modell nicht mehr in config.providers existiert."""
    try:
        known: set[str] = set()
        for prov_name, prov_cfg in (config.get("providers") or {}).items():
            if not isinstance(prov_cfg, dict):
                continue
            models = prov_cfg.get("models") or {}
            default = (models.get("default") or "").strip()
            if default:
                known.add(default)
            for m in (models.get("list") or []):
                if isinstance(m, str) and m:
                    known.add(m)
            for tgt in (models.get("aliases") or {}).values():
                if isinstance(tgt, str) and tgt:
                    known.add(tgt)
        if not known:
            return 0
        data = _load_state(config)
        before = len(data["entries"])
        data["entries"] = [e for e in data["entries"] if e.get("model") in known]
        removed = before - len(data["entries"])
        if removed:
            _save_state(config, data)
            _log.info("slot_cache: removed %d entries for unknown models", removed)
        return removed
    except Exception as e:
        _log.warning("slot_cache: cleanup_unknown_models error: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Read-API für Stats / UI
# ---------------------------------------------------------------------------

def list_all(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list(_load_state(config).get("entries", []))


def get_stats(config: dict[str, Any]) -> dict[str, Any]:
    data = _load_state(config)
    stats = dict(data.get("stats", {}))
    stats["total_files"] = len(data.get("entries", []))
    stats["instance_id"] = data.get("instance_id", "")
    return stats
