"""Group-Room-Mode Helpers.

Per Matrix-Room/Discord-Channel umschaltbarer Context-Mode (agent|group).
Group-Mode lädt slimen System-Prompt (kein SOUL/USER/Memory/Palace/Prefs),
eine Tool-Whitelist, optionalen Language-Override und nutzt für exec eine bwrap-Sandbox
mit eigenem Workspace unter <workspace>/groups/<sanitized>/.

Persistenz: in config.yaml unter
  chat_clients.matrix.room_settings.<room_id>
  chat_clients.discord.channel_settings.<channel_id>
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# Tools die im Group-Mode überhaupt erlaubt werden dürfen (UI-Whitelist-Quelle).
# Tools die hier nicht stehen, sind im Group-Mode HART verboten — auch wenn versehentlich
# in tools_allow eingetragen.
GROUP_ALLOWED_TOOLS = frozenset({
    "web_search",
    "read_url",
    "check_url",
    "send_image",
    "send_audio",
    "exec",
    "read_recent_messages",
    "search_chat_history",
    "get_user_profile",
    "invoke_model",  # für Image-Gen / Subagent-Calls. Owner muss explizit aktivieren — opt-in via UI.
})

# Default tools_allow bei automatischem Setzen (Bot-Invite in >2-Member-Raum).
# invoke_model NICHT default an — kann teuer/missbraucht werden, Owner aktiviert pro Raum.
DEFAULT_GROUP_TOOLS = ("web_search", "read_url", "check_url", "send_image", "read_recent_messages", "search_chat_history", "get_user_profile")


def get_room_settings(config: dict[str, Any], platform: str, target_id: str | None) -> dict[str, Any]:
    """Liest room_settings (matrix) bzw. channel_settings (discord) für einen konkreten Raum/Channel.
    Gibt {} zurück wenn nichts konfiguriert → fällt zurück auf Agent-Mode."""
    if not target_id:
        return {}
    cc = config.get("chat_clients") or {}
    sect = cc.get(platform) or {}
    if platform == "matrix":
        store = sect.get("room_settings") or {}
    elif platform == "discord":
        store = sect.get("channel_settings") or {}
    else:
        return {}
    val = store.get(target_id)
    return val if isinstance(val, dict) else {}


def sanitize_workspace_subdir(name: str) -> str:
    """Macht aus Room-ID/Channel-ID oder User-Input einen Filesystem-tauglichen Ordnernamen.
    Bsp: '!abc:server.tld' → 'abc_server_tld', '#mychan' → 'mychan'."""
    if not name:
        return "default"
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    s = re.sub(r"_+", "_", s).strip("_-")
    return s[:40] or "default"


def group_workspace_path(config: dict[str, Any], subdir: str) -> Path:
    """Vollpfad zum Group-Workspace; erstellt das Verzeichnis falls nötig.
    Path-Traversal-sicher: enforcet dass der Pfad unter <workspace>/groups/ bleibt."""
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    groups_root = (workspace / "groups").resolve()
    groups_root.mkdir(parents=True, exist_ok=True)
    safe = sanitize_workspace_subdir(subdir)
    target = (groups_root / safe).resolve()
    # Sicherheitsnetz: muss tatsächlich unter groups_root liegen
    if not str(target).startswith(str(groups_root) + "/") and target != groups_root:
        raise ValueError(f"workspace path escape: {target} not under {groups_root}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_group_chat_context(
    base_ctx: dict[str, Any],
    room_settings: dict[str, Any],
) -> dict[str, Any]:
    """Mergt Group-Mode-Flags in den Chat-Kontext.
    base_ctx: bereits gesetzte Felder (platform, room_id/channel_id, user_id, conv_id, ...).
    room_settings: aus get_room_settings().
    Setzt group_mode=False wenn settings.context != 'group' (oder fehlt).
    """
    ctx = dict(base_ctx)
    context_mode = (room_settings.get("context") or "agent").strip().lower()
    if context_mode != "group":
        ctx["group_mode"] = False
        return ctx
    ctx["group_mode"] = True
    # tools_allow auf Hard-Whitelist beschränken — auch wenn Owner Müll einträgt
    raw_allow = room_settings.get("tools_allow") or []
    if not isinstance(raw_allow, (list, tuple, set)):
        raw_allow = []
    ctx["tools_allow"] = sorted({str(t).strip() for t in raw_allow if isinstance(t, str)} & GROUP_ALLOWED_TOOLS)
    # Modell-Switching (Owner-Schalter pro Raum): aus (default) → /model & /models bleiben
    # geblockt, Raum läuft auf Default-Modell. An → models_allow = wählbare Modelle
    # (leer = alle konfigurierten), model = persistiertes Raum-Modell (von /model gesetzt).
    # Validierung gegen allowlist passiert in chat_loop (braucht config).
    ctx["group_model_switch"] = bool(room_settings.get("model_switch"))
    raw_models = room_settings.get("models_allow") or []
    if not isinstance(raw_models, (list, tuple)):
        raw_models = []
    ctx["group_models_allow"] = [str(m).strip() for m in raw_models if isinstance(m, str) and str(m).strip()]
    ctx["group_model"] = (str(room_settings.get("model") or "").strip()) if ctx["group_model_switch"] else ""
    # Sprache: 'auto' (None) | 'de' | 'en' | ...
    lang = (room_settings.get("language") or "auto").strip().lower()
    ctx["language_override"] = lang if lang and lang != "auto" else None
    # Workspace-Subdir explizit oder aus room_id/channel_id ableiten
    sub = (room_settings.get("workspace_subdir") or "").strip()
    if not sub:
        sub = base_ctx.get("room_id") or base_ctx.get("channel_id") or "default"
    ctx["workspace_subdir"] = sanitize_workspace_subdir(sub)
    # docs_in_sandbox: per-Raum Toggle, mountet docs read-only nach /docs
    ctx["docs_in_sandbox"] = bool(room_settings.get("docs_in_sandbox"))
    return ctx


def ensure_default_group_settings(
    config: dict[str, Any],
    platform: str,
    target_id: str,
    is_group: bool,
) -> bool:
    """Wenn der Raum/Channel >2 Member hat (is_group=True) und noch keine room_settings
    eingetragen sind, schreibe sinnvolle Defaults und persistiere. Gibt True zurück wenn
    Eintrag neu erstellt wurde."""
    if not is_group or not target_id:
        return False
    cc = config.setdefault("chat_clients", {})
    sect = cc.setdefault(platform, {})
    if platform == "matrix":
        store_key = "room_settings"
    elif platform == "discord":
        store_key = "channel_settings"
    else:
        return False
    store = sect.setdefault(store_key, {})
    if target_id in store:
        return False
    store[target_id] = {
        "context": "group",
        "language": "auto",
        "tools_allow": list(DEFAULT_GROUP_TOOLS),
    }
    # Atomic save: re-load disk state, set only OUR key, save. Verhindert dass
    # parallele WebUI-Saves (room_modes) durch unseren in-memory-Snapshot überschrieben werden.
    try:
        from miniassistant.config import save_config_atomic as _save_atomic
        _entry = dict(store[target_id])
        def _apply(cfg):
            _cc = cfg.setdefault("chat_clients", {})
            _sect = _cc.setdefault(platform, {})
            _store = _sect.setdefault(store_key, {})
            _store.setdefault(target_id, _entry)  # nur setzen wenn noch nicht vorhanden (kein clobber)
        _save_atomic(_apply)
    except Exception:
        pass
    return True


def session_key(prefix: str, user_id: str, group_mode: bool) -> str:
    """Session-Cache-Key.
    Owner-Mode (group_mode=False): pro (room, user) — separate Session pro Nutzer.
    Group-Mode: pro Raum — geteilte Session unter allen Sprechern (oder stateless wenn so konfiguriert)."""
    if group_mode:
        return f"{prefix or ''}|group"
    return f"{prefix or ''}|{user_id}|a"


# Auto-context defaults: leg fest wie viele Vor-Nachrichten und wie viele Zeichen pro Nachricht
# automatisch in den User-Prompt geprefix werden (im Group-Mode).
AUTO_CONTEXT_DEFAULT_COUNT = 3
AUTO_CONTEXT_DEFAULT_MAX_CHARS = 200
AUTO_CONTEXT_MAX_COUNT = 20
AUTO_CONTEXT_MAX_CHARS_CAP = 5000


def get_auto_context_settings(room_settings: dict[str, Any]) -> tuple[int, int]:
    """Liest auto_context_count / auto_context_max_chars aus room_settings, mit Defaults & Hard-Caps."""
    raw_count = room_settings.get("auto_context_count")
    if raw_count is None:
        count = AUTO_CONTEXT_DEFAULT_COUNT
    else:
        try:
            count = max(0, min(int(raw_count), AUTO_CONTEXT_MAX_COUNT))
        except (TypeError, ValueError):
            count = AUTO_CONTEXT_DEFAULT_COUNT
    raw_max = room_settings.get("auto_context_max_chars")
    if raw_max is None:
        max_chars = AUTO_CONTEXT_DEFAULT_MAX_CHARS
    else:
        try:
            max_chars = max(20, min(int(raw_max), AUTO_CONTEXT_MAX_CHARS_CAP))
        except (TypeError, ValueError):
            max_chars = AUTO_CONTEXT_DEFAULT_MAX_CHARS
    return count, max_chars


def format_auto_context(messages: list[dict[str, Any]], max_chars: int, bot_sender: str = "") -> str:
    """Baut den Auto-Context-Block aus einer Nachrichtenliste (älteste→neueste).
    Truncated jede Nachricht auf max_chars Zeichen mit '…[truncated N/M]'.
    Bot-eigene Nachrichten werden BEHALTEN aber als '[du selbst]' markiert — damit Bot in stateless
    group_mode auf seine vorigen Antworten zugreifen kann ('was meintest du mit X?', Bot-Typos etc.).
    Bot-Bodies werden härter truncated (max_chars/2) um Tokens zu sparen.
    Gibt '' wenn keine relevanten Nachrichten."""
    if not messages:
        return ""
    import datetime as _dt
    now = _dt.datetime.now()
    today = now.date()
    yesterday = today - _dt.timedelta(days=1)
    _GAP_SECONDS = 7200  # >2h zwischen zwei Nachrichten ⇒ wahrscheinlich anderes Gespräch

    def _ts_label(ts_ms: int) -> "tuple[str, _dt.datetime | None]":
        """Timestamp-Label datums-bewusst: heute → 'HH:MM', gestern → 'gestern HH:MM',
        älter (gleiches Jahr) → 'DD.MM. HH:MM', anderes Jahr → 'DD.MM.YYYY HH:MM'.
        Verhindert dass eine Nachricht von gestern 17:29 vor heute 12:42 als zusammenhängend
        gelesen wird (Modell verwechselte sonst altes Thema mit aktuellem)."""
        if not ts_ms:
            return "", None
        try:
            dt = _dt.datetime.fromtimestamp(ts_ms / 1000)
        except Exception:
            return "", None
        d = dt.date()
        hm = dt.strftime("%H:%M")
        if d == today:
            return hm, dt
        if d == yesterday:
            return f"gestern {hm}", dt
        if d.year == today.year:
            return dt.strftime("%d.%m. ") + hm, dt
        return dt.strftime("%d.%m.%Y ") + hm, dt

    lines: list[str] = []
    bot_max = max(60, max_chars // 2)
    _prev_dt: "_dt.datetime | None" = None
    for m in messages:
        sender = m.get("sender") or ""
        body = (m.get("body") or "").strip().replace("\n", " ")
        if not body:
            continue
        is_bot = bool(bot_sender and sender == bot_sender)
        eff_max = bot_max if is_bot else max_chars
        if len(body) > eff_max:
            body = body[:eff_max].rstrip() + f"…[truncated {eff_max}/{len(m.get('body') or '')}]"
        who = m.get("display") or sender.split(":")[0].lstrip("@") or "?"
        if is_bot:
            who = f"{who} (you)"
        tstr, _cur_dt = _ts_label(m.get("ts") or 0)
        # Gesprächslücken-Marker: große Zeitlücke oder Tageswechsel ⇒ Trennzeile, damit das Modell
        # weiß dass die folgenden Nachrichten evtl. ein anderes Thema sind (nicht vermischen).
        if _prev_dt is not None and _cur_dt is not None:
            if (_cur_dt - _prev_dt).total_seconds() > _GAP_SECONDS or _cur_dt.date() != _prev_dt.date():
                lines.append("[⏸ längere Pause — die folgenden Nachrichten sind evtl. ein neues/anderes Thema]")
        if _cur_dt is not None:
            _prev_dt = _cur_dt
        prefix = f"[{tstr}] " if tstr else ""
        lines.append(f"{prefix}{who}: {body}")
    if not lines:
        return ""
    return (
        "[Context — last messages in this room (auto-included; use `read_recent_messages` for more)]\n"
        + "\n".join(lines)
        + "\n[/Context]\n\n"
    )
