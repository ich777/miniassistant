"""
Discord-Bot: Verbindung via discord.py, bei Nachricht (DM oder @-Mention)
entweder Auth-Code senden oder (wenn autorisiert) KI-Antwort.
Typing-Indicator, Markdown (native Discord-Unterstützung).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level references for thread-safe access from outside (status_update, cancellation)
_discord_client: Any = None
_discord_loop: asyncio.AbstractEventLoop | None = None

# Cache: guild_id (int) -> inviter user_id (str) of the bot. None = looked up, no inviter found.
_guild_inviter_cache: dict[int, str | None] = {}


def get_guild_inviter(guild_id: int) -> str | None:
    """Sync cached lookup of the user who added the bot to a Discord guild."""
    return _guild_inviter_cache.get(int(guild_id))


def get_guild_inviter_cache() -> dict[int, str | None]:
    return dict(_guild_inviter_cache)


def leave_guild(guild_id: str) -> tuple[bool, str]:
    """Bot leaves a Discord guild (server). Thread-safe."""
    cl = _discord_client
    loop = _discord_loop
    if not cl or not loop:
        return False, "Discord-Bot läuft nicht"

    async def _do_leave() -> tuple[bool, str]:
        try:
            guild = cl.get_guild(int(guild_id))
            if not guild:
                return False, "Guild nicht gefunden"
            await guild.leave()
            return True, "ok"
        except Exception as e:
            return False, str(e)

    try:
        future = asyncio.run_coroutine_threadsafe(_do_leave(), loop)
        return future.result(timeout=30)
    except Exception as e:
        return False, str(e)


def list_channels() -> list[dict[str, Any]]:
    """List Discord text channels + DM channels the bot can see. Returns [] if not running."""
    cl = _discord_client
    if not cl:
        return []
    out: list[dict[str, Any]] = []
    try:
        from miniassistant.chat_auth import is_authorized
        from miniassistant.config import get_config_dir as _cfgdir
        config_dir = _cfgdir()
    except Exception:
        is_authorized = None  # type: ignore
        config_dir = None
    try:
        for guild in getattr(cl, "guilds", []) or []:
            guild_name = getattr(guild, "name", None) or str(getattr(guild, "id", ""))
            guild_id = str(getattr(guild, "id", ""))
            inviter = _guild_inviter_cache.get(int(guild.id))
            inviter_authed = bool(is_authorized("discord", inviter, config_dir)) if (inviter and is_authorized) else False
            guild_trusted = bool(inviter_authed)
            for ch in getattr(guild, "text_channels", []) or []:
                out.append({
                    "id": str(getattr(ch, "id", "")),
                    "name": str(getattr(ch, "name", "") or getattr(ch, "id", "")),
                    "guild": guild_name,
                    "guild_id": guild_id,
                    "kind": "text",
                    "inviter": inviter,
                    "inviter_authed": inviter_authed,
                    "guild_trusted": guild_trusted,
                })
        for dm in getattr(cl, "private_channels", []) or []:
            recipient = getattr(dm, "recipient", None)
            rname = getattr(recipient, "name", None) if recipient else None
            out.append({
                "id": str(getattr(dm, "id", "")),
                "name": str(rname or "DM"),
                "guild": "(DM)",
                "kind": "dm",
            })
    except Exception as e:
        logger.warning("list_channels failed: %s", e)
    out.sort(key=lambda c: (c["kind"] != "dm", c["guild"].lower(), c["name"].lower()))
    return out

# Optional: discord.py (pip install miniassistant[discord])
_DISCORD_IMPORT_ERROR: str | None = None
try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError as e:
    DISCORD_AVAILABLE = False
    _DISCORD_IMPORT_ERROR = str(e)
    discord = None  # type: ignore


def send_message_to_channel(channel_id: str, message: str) -> bool:
    """Thread-safe: Sendet eine Textnachricht in einen bestimmten Discord-Channel.
    Wird von status_update Tool aufgerufen. Gibt True bei Erfolg zurueck.
    Stellt Typing-Indikator nach dem Senden sofort wieder her."""
    if not _discord_client or not _discord_loop:
        return False

    async def _do_send() -> bool:
        ch = _discord_client.get_channel(int(channel_id))
        if not ch:
            return False
        if len(message) <= 2000:
            await ch.send(message)
        else:
            for chunk in _split_message(message, 2000):
                await ch.send(chunk)
        # Typing-Indikator sofort wiederherstellen (Senden löscht ihn serverseitig)
        try:
            await ch.trigger_typing()
        except Exception:
            pass
        return True

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _discord_loop)
        return future.result(timeout=30)
    except Exception as e:
        logger.warning("Discord send_message_to_channel fehlgeschlagen: %s", e)
        return False


def fetch_recent_messages(channel_id: str, limit: int = 20, skip_message_id: str | None = None) -> list[dict[str, Any]]:
    """Fetcht die letzten `limit` Text-Nachrichten aus einem Discord-Channel.
    Liefert Liste älteste→neueste. Jede Nachricht: {sender, display, body, ts, message_id}.
    skip_message_id: lasse diese eine ID aus (z.B. die Trigger-Nachricht selbst).
    Thread-safe; gibt [] wenn Bot nicht läuft, Channel unbekannt, oder Fehler."""
    if not _discord_client or not _discord_loop:
        return []
    if limit <= 0:
        return []
    limit = min(limit, 100)

    async def _do_fetch() -> list[dict[str, Any]]:
        try:
            ch = _discord_client.get_channel(int(channel_id))
        except Exception:
            return []
        if not ch:
            return []
        collected: list[dict[str, Any]] = []
        try:
            # discord.py: channel.history liefert neueste zuerst (oldest_first=False default)
            async for msg in ch.history(limit=limit + (1 if skip_message_id else 0)):
                if skip_message_id and str(msg.id) == str(skip_message_id):
                    continue
                body = (msg.content or "").strip()
                if not body:
                    continue
                author = msg.author
                sender = f"{author.name}#{getattr(author, 'discriminator', '0')}" if hasattr(author, "name") else str(author)
                display = getattr(author, "display_name", None) or getattr(author, "name", None) or str(author)
                ts_ms = int(msg.created_at.timestamp() * 1000) if hasattr(msg, "created_at") else 0
                collected.append({
                    "sender": sender,
                    "display": str(display),
                    "body": body,
                    "ts": ts_ms,
                    "message_id": str(msg.id),
                })
                if len(collected) >= limit:
                    break
        except Exception as e:
            logger.debug("Discord fetch_recent_messages fehlgeschlagen: %s", e)
        collected.reverse()  # oldest → newest
        return collected

    try:
        future = asyncio.run_coroutine_threadsafe(_do_fetch(), _discord_loop)
        return future.result(timeout=20)
    except Exception as e:
        logger.warning("Discord fetch_recent_messages exception: %s", e)
        return []


def search_chat_history(
    channel_id: str,
    query: str,
    max_scan: int = 200,
    context_lines: int = 2,
) -> dict[str, Any]:
    """Sucht in Discord-Channel-History nach `query` (case-insensitive substring; mit `/.../` regex).
    Scrollt bis zu max_scan Nachrichten zurück, returnt Hits mit ±context_lines Kontext."""
    if not _discord_client or not _discord_loop or not query:
        return {"hits": [], "scanned": 0, "query": query, "diagnostic": "no client or empty query"}
    max_scan = max(10, min(int(max_scan), 500))
    context_lines = max(0, min(int(context_lines), 5))

    import re as _re_s
    is_regex = len(query) >= 3 and query.startswith("/") and query.endswith("/")
    pat = None
    if is_regex:
        try:
            pat = _re_s.compile(query[1:-1], _re_s.IGNORECASE)
        except _re_s.error:
            return {"hits": [], "scanned": 0, "query": query, "diagnostic": "invalid regex"}
    q_lower = query.lower() if not is_regex else None

    async def _do_search() -> dict[str, Any]:
        try:
            ch = _discord_client.get_channel(int(channel_id))
        except Exception:
            return {"hits": [], "scanned": 0, "query": query, "diagnostic": "invalid channel"}
        if not ch:
            return {"hits": [], "scanned": 0, "query": query, "diagnostic": "channel not found"}
        all_messages: list[dict[str, Any]] = []
        try:
            async for msg in ch.history(limit=max_scan):
                body = (msg.content or "").strip()
                if not body:
                    continue
                author = msg.author
                sender = f"{author.name}#{getattr(author, 'discriminator', '0')}" if hasattr(author, "name") else str(author)
                display = getattr(author, "display_name", None) or getattr(author, "name", None) or str(author)
                ts_ms = int(msg.created_at.timestamp() * 1000) if hasattr(msg, "created_at") else 0
                all_messages.append({
                    "sender": sender,
                    "display": str(display),
                    "body": body,
                    "ts": ts_ms,
                    "message_id": str(msg.id),
                })
        except Exception as e:
            logger.debug("Discord search_chat_history failed: %s", e)
        all_messages.reverse()

        def _match(body: str) -> bool:
            if not body:
                return False
            if pat is not None:
                return bool(pat.search(body))
            return q_lower in body.lower()

        hit_indices = [i for i, m in enumerate(all_messages) if _match(m["body"])]
        included = set()
        for i in hit_indices:
            for j in range(max(0, i - context_lines), min(len(all_messages), i + context_lines + 1)):
                included.add(j)
        hits = []
        for i in sorted(included):
            m = dict(all_messages[i])
            m["is_hit"] = i in hit_indices
            hits.append(m)
        if len(hits) > 40:
            hits = hits[:40]
        return {
            "hits": hits,
            "match_count": len(hit_indices),
            "scanned": len(all_messages),
            "query": query,
            "diagnostic": None,
        }

    try:
        future = asyncio.run_coroutine_threadsafe(_do_search(), _discord_loop)
        return future.result(timeout=30)
    except Exception as e:
        logger.warning("Discord search_chat_history exception: %s", e)
        return {"hits": [], "scanned": 0, "query": query, "diagnostic": f"exception: {e}"}


def set_channel_typing(channel_id: str) -> bool:
    """Thread-safe: Triggert den Typing-Indikator in einem Discord-Channel.
    Wird z.B. vom Debate-Tool aufgerufen, um zwischen Runden den Typing-Status zu halten."""
    if not _discord_client or not _discord_loop:
        return False

    async def _do_typing() -> bool:
        ch = _discord_client.get_channel(int(channel_id))
        if ch:
            await ch.trigger_typing()
        return True

    try:
        future = asyncio.run_coroutine_threadsafe(_do_typing(), _discord_loop)
        return future.result(timeout=10)
    except Exception:
        return False


def get_user_profile(user_id: str, save_dir: str, channel_id: str | None = None) -> dict[str, Any]:
    """Holt display_name + avatar von einem Discord-Nutzer. Avatar wird nach save_dir/<id>.png geschrieben.
    Returns {display_name, avatar_path (host-Pfad), avatar_url}.
    channel_id: wenn gesetzt, wird user_id gegen die Guild-Mitglieder des Channels geprüft —
    Profile von Nicht-Mitgliedern werden NICHT aufgelöst (kein globaler Lookup im Group-Mode)."""
    cl = _discord_client
    loop = _discord_loop
    if not cl or not loop:
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "discord bot not running"}
    try:
        uid_int = int(user_id)
    except (TypeError, ValueError):
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "invalid discord user_id (expected numeric)"}

    async def _do() -> dict[str, Any]:
        # Membership-Gate: nur Profile von Mitgliedern DIESER Guild auflösen.
        if channel_id:
            try:
                ch = cl.get_channel(int(channel_id))
            except (TypeError, ValueError):
                ch = None
            guild = getattr(ch, "guild", None)
            if guild is None:
                return {"error": "No row found: cannot verify room membership"}
            member = guild.get_member(uid_int)
            if member is None:
                try:
                    member = await guild.fetch_member(uid_int)
                except Exception:
                    member = None
            if member is None:
                return {"error": "No row found: user is not in this room"}
            display = getattr(member, "display_name", None) or getattr(member, "global_name", None) or getattr(member, "name", None) or str(uid_int)
            av = getattr(member, "display_avatar", None) or getattr(member, "avatar", None)
            url = str(getattr(av, "url", "")) if av else ""
            return {"display_name": str(display), "avatar_url": url}
        try:
            u = await cl.fetch_user(uid_int)
        except Exception as e:
            return {"error": f"fetch_user failed: {e}"}
        display = getattr(u, "display_name", None) or getattr(u, "global_name", None) or getattr(u, "name", None) or str(uid_int)
        av = getattr(u, "display_avatar", None) or getattr(u, "avatar", None)
        url = str(getattr(av, "url", "")) if av else ""
        return {"display_name": str(display), "avatar_url": url}

    try:
        fut = asyncio.run_coroutine_threadsafe(_do(), loop)
        meta = fut.result(timeout=20)
    except Exception as e:
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": f"profile fetch timeout: {e}"}
    if meta.get("error"):
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": meta["error"]}
    out: dict[str, Any] = {"display_name": meta.get("display_name", ""), "avatar_path": "", "avatar_url": meta.get("avatar_url", "")}
    url = out["avatar_url"]
    if url:
        try:
            import httpx as _httpx
            r = _httpx.get(url, timeout=15, follow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            ext = ".png"
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
            elif "webp" in ct:
                ext = ".webp"
            elif "gif" in ct:
                ext = ".gif"
            from pathlib import Path as _P
            p = _P(save_dir) / f"{uid_int}{ext}"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
            out["avatar_path"] = str(p)
        except Exception as e:
            out["error"] = f"avatar download failed: {e}"
    return out


def _get_chat_response(
    config: dict[str, Any],
    discord_user_id: str,
    user_message: str,
    sessions: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
    channel_id: str | None = None,
    is_group: bool = False,
) -> str:
    """Synchroner Aufruf: Session per (channel_id, discord_user_id), handle_user_input.
    Gibt ausschließlich den sichtbaren Content zurück – KEIN Thinking.

    Session-Key kombiniert Channel und User: gleicher User in zwei Channels → zwei separate Sessions.
    """
    # Pro Turn shallow-copy des Config-Dicts: verhindert Race auf config["_chat_context"]
    # zwischen parallelen Triggern (Alice in Channel A vs Bob in Channel B im selben Moment).
    # Top-level Keys werden unabhängig — nested dicts (providers, server, …) bleiben geteilt,
    # was ok ist weil zur Laufzeit nur top-level _-Keys mutiert werden.
    config = dict(config)
    from miniassistant.chat_loop import create_session, handle_user_input, is_chat_command
    from miniassistant.slot_cache import derive_conv_id as _sc_derive
    from miniassistant.group_rooms import (
        get_room_settings, build_group_chat_context, session_key as _sess_key,
        ensure_default_group_settings, get_auto_context_settings, format_auto_context,
        wrap_current_message,
    )
    if channel_id and is_group:
        ensure_default_group_settings(config, "discord", channel_id, is_group=True)
    rs = get_room_settings(config, "discord", channel_id)
    _sc_conv_id = _sc_derive("discord", channel_id=channel_id, user_id=discord_user_id) if channel_id else None
    base_ctx: dict[str, Any] = {"platform": "discord", "channel_id": channel_id, "user_id": discord_user_id}
    # Display-Name des Senders via Client lookup
    try:
        if _discord_client and discord_user_id:
            _u = _discord_client.get_user(int(discord_user_id))
            if _u is not None:
                _dn = getattr(_u, "display_name", None) or getattr(_u, "global_name", None) or getattr(_u, "name", None) or ""
                if _dn:
                    base_ctx["user_display"] = str(_dn)
    except Exception:
        pass
    if _sc_conv_id:
        base_ctx["conv_id"] = _sc_conv_id
        base_ctx["slot_cache_endpoint"] = "discord"
    ctx = build_group_chat_context(base_ctx, rs) if channel_id else base_ctx
    # Auto-Context im Group-Mode: letzte N Nachrichten vor user_message prependen.
    # NICHT bei Slash-Befehlen (/new, /help, …): das Prepend würde den ^-verankerten
    # Command-Parser in handle_user_input aushebeln → Befehl landet als Prompt beim LLM.
    if ctx.get("group_mode") and channel_id and not is_chat_command(user_message):
        ac_count, ac_max = get_auto_context_settings(rs)
        if ac_count > 0:
            try:
                bot_sender = ""
                try:
                    if _discord_client and getattr(_discord_client, "user", None):
                        u = _discord_client.user
                        bot_sender = f"{u.name}#{getattr(u, 'discriminator', '0')}"
                except Exception:
                    pass
                prev = fetch_recent_messages(channel_id, limit=ac_count + 1)
                if prev and (prev[-1].get("body") or "").strip() == user_message.strip():
                    prev = prev[:-1]
                prev = prev[-ac_count:] if len(prev) > ac_count else prev
                blk = format_auto_context(prev, max_chars=ac_max, bot_sender=bot_sender)
                if blk:
                    _who_now = base_ctx.get("user_display") or discord_user_id
                    user_message = wrap_current_message(blk, _who_now, user_message)
            except Exception as _ac_err:
                logger.debug("Discord auto-context fetch failed: %s", _ac_err)
    session_key = _sess_key(channel_id, discord_user_id, bool(ctx.get("group_mode")))
    if channel_id:
        config["_chat_context"] = ctx
    # Group-Mode: stateless — jeder Turn frische Session (auto-context + read_recent_messages liefern Kontext).
    if ctx.get("group_mode") or session_key not in sessions:
        session = create_session(config, None)
        session["system_prompt"] = (
            session.get("system_prompt", "") +
            "\n\nDiscord: Max 2000 Zeichen/Nachricht. Laengere Antworten mit `---` trennen, werden automatisch aufgeteilt."
        )
        sessions[session_key] = session
    session = sessions[session_key]
    if channel_id:
        session["chat_context"] = ctx
    result = handle_user_input(session, user_message, allow_new_session=True, images=images)
    if ctx.get("group_mode"):
        sessions.pop(session_key, None)
    else:
        sessions[session_key] = result[1]
    ai_content = result[4] if len(result) > 4 else None
    thinking = result[3] if len(result) > 3 else None
    if ai_content:
        return ai_content.strip()
    if not thinking:
        # Kein Thinking → Command-Antwort, result[0] ist sicher
        return (result[0] or "").strip()
    # KI hat nur gedacht, kein sichtbarer Content
    return ""


async def run_discord_bot(config: dict[str, Any]) -> None:
    """
    Läuft als asyncio-Task: verbindet mit Discord, bei Nachricht Auth-Code oder KI.
    Beendet sich bei Fehler oder wenn config keine gültige Discord-Config hat.
    """
    if not DISCORD_AVAILABLE:
        detail = f" ({_DISCORD_IMPORT_ERROR})" if _DISCORD_IMPORT_ERROR else ""
        logger.warning(
            "discord.py nicht installiert. Discord-Bot deaktiviert.%s "
            "Installieren: pip install -e '.[discord]' oder pip install discord.py",
            detail,
        )
        return

    discord_cfg = (config.get("chat_clients") or {}).get("discord")
    if not discord_cfg or not discord_cfg.get("bot_token"):
        return
    if not discord_cfg.get("enabled", True):
        logger.info("Discord-Bot deaktiviert (discord.enabled: false).")
        return

    bot_token = discord_cfg["bot_token"]

    from miniassistant.chat_auth import get_or_generate_code, is_authorized
    from miniassistant.chat_loop import SessionLRU

    def _is_trusted(sender_id: str, channel: Any, config_dir: str | None) -> tuple[bool, str | None]:
        """Returns (trusted, reason). True wenn User selbst authed ODER Guild-Trust (manuell oder via Inviter)."""
        if is_authorized("discord", sender_id, config_dir):
            return True, None
        guild = getattr(channel, "guild", None)
        if guild is None:
            return False, None  # DM
        inviter = _guild_inviter_cache.get(int(guild.id))
        if inviter and is_authorized("discord", inviter, config_dir):
            return True, inviter
        return False, None

    # Sessions pro (channel, discord_user) — LRU mit Cap, sonst wachsen sie unbegrenzt
    discord_sessions: Any = SessionLRU(max_size=200)
    # Pending Images: User hat Bild ohne Text geschickt → nächste Textnachricht bekommt das Bild
    _pending_images: dict[str, list[dict[str, Any]]] = {}

    # Discord-Client mit Intents
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        global _discord_client, _discord_loop
        _discord_client = client
        _discord_loop = asyncio.get_event_loop()
        logger.info("Discord-Bot gestartet als %s (ID: %s)", client.user, client.user.id if client.user else "?")
        # Inviter pro Guild aus Audit-Log holen — wenn Audit-Log Permission gegeben.
        bot_id = client.user.id if client.user else None
        for guild in getattr(client, "guilds", []) or []:
            try:
                inviter_id: str | None = None
                async for entry in guild.audit_logs(limit=50, action=discord.AuditLogAction.bot_add):
                    target = getattr(entry, "target", None)
                    if target and bot_id and int(getattr(target, "id", 0)) == int(bot_id):
                        user_obj = getattr(entry, "user", None)
                        if user_obj and getattr(user_obj, "id", None) is not None:
                            inviter_id = str(user_obj.id)
                            break
                _guild_inviter_cache[int(guild.id)] = inviter_id
                if inviter_id:
                    logger.info("Discord: Guild '%s' (%s) eingeladen von User-ID %s", guild.name, guild.id, inviter_id)
                else:
                    logger.info("Discord: Guild '%s' (%s) — kein bot_add Audit-Log-Eintrag gefunden", guild.name, guild.id)
            except discord.Forbidden:
                _guild_inviter_cache[int(guild.id)] = None
                logger.warning("Discord: Audit-Log für Guild '%s' nicht zugänglich (Bot hat keine VIEW_AUDIT_LOG Permission) — Guild-Trust deaktiviert.", guild.name)
            except Exception as e:
                _guild_inviter_cache[int(guild.id)] = None
                logger.warning("Discord: Audit-Log Fetch für Guild '%s' fehlgeschlagen: %s", guild.name, e)

    @client.event
    async def on_message(message: discord.Message) -> None:
        # Eigene Nachrichten ignorieren
        if message.author == client.user:
            return

        # Channel-Mode: always / mention / off. Default: mention (alter Discord-Default), DMs immer always.
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = client.user is not None and client.user.mentioned_in(message)
        ch_modes = ((config.get("chat_clients") or {}).get("discord") or {}).get("channel_modes") or {}
        ch_id = str(getattr(message.channel, "id", ""))
        mode = (ch_modes.get(ch_id) or "").strip().lower()
        if mode not in ("always", "mention", "off"):
            mode = "always" if is_dm else "mention"
        if mode == "off":
            return
        if mode == "mention" and not (is_dm or is_mentioned):
            return

        sender_id = str(message.author.id)
        body = message.content.strip()

        # Bei @-Mention: Bot-Mention aus Text entfernen
        if is_mentioned and client.user is not None:
            body = body.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

        # Bild-, Audio- und Dokument-Attachments herunterladen
        msg_images: list[dict[str, Any]] = []
        msg_docs: list[dict[str, Any]] = []
        audio_bytes: bytes | None = None
        from miniassistant.documents import is_supported as _doc_supported, extract_document as _doc_extract
        _doc_max_chars = int(config.get("doc_max_chars") or 200000)
        _doc_max_pages = int(config.get("doc_max_pages_render") or 10)
        for att in message.attachments:
            ct = (att.content_type or "").lower()
            fname = att.filename or ""
            if ct.startswith("image/") or fname.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                try:
                    import base64 as _b64
                    img_bytes = await att.read()
                    mime = ct if ct.startswith("image/") else "image/png"
                    b64_data = _b64.b64encode(img_bytes).decode("ascii")
                    msg_images.append({"mime_type": mime, "data": b64_data})
                    logger.info("Discord: Bild-Attachment von %s: %s (%s)", sender_id, fname, mime)
                except Exception as e:
                    logger.warning("Discord: Bild-Download fehlgeschlagen für %s: %s", fname, e)
            elif ct.startswith("audio/") or fname.lower().endswith((".ogg", ".mp3", ".wav", ".m4a", ".webm")):
                try:
                    audio_bytes = await att.read()
                    logger.info("Discord: Audio-Attachment von %s: %s (%d bytes)", sender_id, fname, len(audio_bytes))
                except Exception as e:
                    logger.warning("Discord: Audio-Download fehlgeschlagen für %s: %s", fname, e)
            elif _doc_supported(ct, fname):
                try:
                    doc_bytes = await att.read()
                    doc = _doc_extract(doc_bytes, fname, ct, max_chars=_doc_max_chars, max_pages_render=_doc_max_pages)
                    if doc.get("error"):
                        logger.warning("Discord: Dokument-Extraktion fehlgeschlagen %s: %s", fname, doc["error"])
                        await message.reply(f"Dokument `{fname}` konnte nicht gelesen werden: {doc['error']}")
                        return
                    msg_docs.append(doc)
                    if doc.get("images"):
                        msg_images.extend(doc["images"])
                    logger.info("Discord: Dokument-Attachment %s: %d Zeichen, %d Seiten-PNGs", fname, len(doc.get("text") or ""), len(doc.get("images") or []))
                except Exception as e:
                    logger.warning("Discord: Dokument-Download fehlgeschlagen für %s: %s", fname, e)

        # Reply-to-Image (Discord Reply-Feature): wenn User auf eine ältere Nachricht antwortet
        # und die Originalnachricht Image-Attachments hatte → diese auch fetchen + anhängen,
        # damit Bot das gequotete Bild sehen kann.
        try:
            _ref = getattr(message, "reference", None)
            _ref_mid = getattr(_ref, "message_id", None) if _ref is not None else None
            if _ref_mid:
                try:
                    _ref_msg = await message.channel.fetch_message(int(_ref_mid))
                except Exception as _e_fetch:
                    _ref_msg = None
                    logger.debug("Discord: fetch_message(%s) failed: %s", _ref_mid, _e_fetch)
                if _ref_msg is not None and getattr(_ref_msg, "attachments", None):
                    import base64 as _b64q
                    for _att_q in _ref_msg.attachments:
                        _ctq = (_att_q.content_type or "").lower()
                        _fnq = _att_q.filename or ""
                        if _ctq.startswith("image/") or _fnq.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                            try:
                                _ibq = await _att_q.read()
                                _mq = _ctq if _ctq.startswith("image/") else "image/png"
                                msg_images.append({"mime_type": _mq, "data": _b64q.b64encode(_ibq).decode("ascii")})
                                logger.info("Discord: reply-to-image dereferenziert (ref_msg=%s, file=%s, %s)", _ref_mid, _fnq, _mq)
                            except Exception as _e_q:
                                logger.debug("Discord: reply-to-image read failed for %s: %s", _fnq, _e_q)
        except Exception as _qerr_d:
            logger.debug("Discord: reply-to-image processing failed: %s", _qerr_d)

        # Audio-Nachricht: STT → Agent → TTS → Antwort senden
        if audio_bytes is not None:
            from miniassistant.config import get_voice_stt_url, get_voice_tts_url, get_voice_language, get_voice_tts_voice
            config_dir = config.get("_config_dir")
            trusted, via_inviter = _is_trusted(sender_id, message.channel, config_dir)
            if not trusted:
                code = get_or_generate_code("discord", sender_id, config_dir)
                await message.reply(f"Nicht freigeschaltet. Auth-Code: **{code}**")
                return
            if via_inviter:
                logger.debug("Discord: User %s trusted via Guild-Inviter %s", sender_id, via_inviter)
            stt_url = get_voice_stt_url(config)
            if not stt_url:
                await message.reply("Sprachfunktion nicht konfiguriert (voice.stt.url fehlt).")
                return
            try:
                from miniassistant import wyoming_client as _wyoming
                lang = get_voice_language(config)
                transcript = _wyoming.transcribe(audio_bytes, stt_url, language=lang)
            except Exception as e:
                logger.exception("Discord Audio: STT fehlgeschlagen")
                await message.reply(f"Spracherkennung fehlgeschlagen: {e}")
                return
            if not transcript:
                await message.reply("Konnte Sprachnachricht nicht erkennen.")
                return
            logger.info("Discord Audio: Transkript von %s: %s", sender_id, transcript[:80])
            # Typing über gesamten Flow: Agent → TTS → Upload (sonst „still" während TTS-Synthese)
            async with message.channel.typing():
                try:
                    response = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: _get_chat_response(config, sender_id, f"[Voice] {transcript}", discord_sessions, channel_id=str(message.channel.id), is_group=(not is_dm)),
                    )
                except Exception as e:
                    logger.exception("Discord Audio: Agent fehlgeschlagen")
                    await message.reply(f"Fehler: {e}")
                    return
                if not response:
                    return
                from miniassistant.voice_format import format_for_voice
                voice_text, visual_content = format_for_voice(response)
                tts_url = get_voice_tts_url(config)
                if tts_url and voice_text:
                    try:
                        tts_voice = get_voice_tts_voice(config)
                        wav_bytes = _wyoming.synthesize(voice_text, tts_url, voice=tts_voice)
                        import io as _io
                        await message.reply(file=discord.File(_io.BytesIO(wav_bytes), filename="response.wav"))
                        from miniassistant import agent_actions_log as _aal
                        _aal.log_voice_sent(config, chars=len(voice_text), voice=tts_voice or "", bytes_sent=len(wav_bytes))
                    except Exception as e:
                        logger.exception("Discord Audio: TTS fehlgeschlagen")
                        await message.reply(voice_text[:2000])
                else:
                    await message.reply(voice_text[:2000])
                if visual_content:
                    for chunk in _split_message(visual_content, 2000):
                        await message.channel.send(chunk)
            return

        # Bild ohne Text → Pending speichern, User fragen
        # (nur bei reinem Bild ohne Dokument; Dokument hat eigenen Text-Inhalt)
        if msg_images and not body and not msg_docs:
            _pending_images.setdefault(sender_id, []).extend(msg_images)
            await message.reply("Bild empfangen. Was soll ich damit machen?")
            return

        if not body and not msg_images and not msg_docs:
            return

        # /stop, /abort, /abbruch: Token-basiert (egal ob mit @-mention, display-name, oder ':' statt '/').
        import re as _re_cancel_d
        _cancel_tokens = set(_re_cancel_d.split(r"\s+", body.strip().lower()))
        _cancel_tokens |= {("/" + t[1:]) for t in _cancel_tokens if t.startswith(":") and len(t) > 1}
        _cancel_hit = _cancel_tokens & {"/stop", "/abort", "/abbruch"}
        if _cancel_hit:
            _cancel_cmd = sorted(_cancel_hit)[0]
            from miniassistant.cancellation import request_cancel
            level = "stop" if _cancel_cmd == "/stop" else "abort"
            # Gruppen-Channels: room-wide cancel
            channel_id = str(message.channel.id) if hasattr(message, "channel") else ""
            cancel_key = f"chan:{channel_id}" if (not is_dm and channel_id) else sender_id
            request_cancel(cancel_key, level)
            logger.info("Discord: %s von %s — Cancellation angefordert (%s, key=%s)", body[:40], sender_id, level, cancel_key)
            reply = "⏹ Verarbeitung wird abgebrochen…" if level == "abort" else "⏸ Verarbeitung wird nach aktuellem Schritt gestoppt…"
            await message.reply(reply)
            return

        # Pending Images abholen (Bild wurde vorher ohne Text geschickt)
        if not msg_images and sender_id in _pending_images:
            msg_images = _pending_images.pop(sender_id)

        logger.info("Discord: Nachricht von %s (%s): %.80s", message.author, sender_id, body)

        config_dir = config.get("_config_dir")
        trusted, via_inviter = _is_trusted(sender_id, message.channel, config_dir)
        if not trusted:
            code = get_or_generate_code("discord", sender_id, config_dir)
            logger.info("Discord: Auth-Code an %s gesendet (Code redacted)", sender_id)
            await message.reply(
                f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\n"
                f"Gib in der Web-UI ein: `/auth discord {code}`"
            )
            return
        if via_inviter:
            logger.debug("Discord: %s trusted via Guild-Inviter %s", sender_id, via_inviter)

        images_param = msg_images if msg_images else None

        # Dokument-Bloecke an User-Text anhaengen (chat_loop strippt sie beim History-Save)
        if msg_docs:
            from miniassistant.documents import format_document_block as _fmt_doc
            doc_blocks = [_fmt_doc(d) for d in msg_docs]
            doc_text = "\n\n".join(b for b in doc_blocks if b)
            if doc_text:
                body = f"{doc_text}\n\n{body}".strip() if body else f"{doc_text}\n\nBitte uebersetze oder fasse das Dokument zusammen."

        # Typing-Indicator + KI-Antwort
        async with message.channel.typing():
            try:
                reply = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda s=sender_id, b=body, imgs=images_param, cid=str(message.channel.id), grp=(not is_dm): _get_chat_response(config, s, b, discord_sessions, images=imgs, channel_id=cid, is_group=grp),
                )
            except Exception as e:
                logger.exception("Discord KI-Antwort fehlgeschlagen: %s", e)
                reply = f"Fehler bei der Verarbeitung: {e}"

        if not reply:
            return
        from miniassistant.scheduler import _SILENT_SENTINELS
        if reply.strip() in _SILENT_SENTINELS:
            logger.info("Discord: Antwort ist Silent-Sentinel (%s) — kein Send", reply.strip())
            return
        # Discord hat ein 2000-Zeichen-Limit pro Nachricht
        if len(reply) <= 2000:
            await message.reply(reply)
        else:
            # In Chunks aufteilen
            chunks = _split_message(reply, 2000)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)

    logger.info("Discord-Bot startet (Token: %s…)", bot_token[:8] if len(bot_token) > 8 else "***")
    try:
        await client.start(bot_token)
    except discord.LoginFailure:
        logger.error("Discord: Login fehlgeschlagen – ungültiges Bot-Token.")
    except Exception as e:
        logger.exception("Discord-Bot Fehler: %s", e)
    finally:
        if not client.is_closed():
            await client.close()


def _split_message(text: str, max_len: int = 2000) -> list[str]:
    """Teilt eine lange Nachricht in Chunks. Bevorzugt: --- Trenner, dann Zeilenumbruch, dann Leerzeichen."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Bevorzugt an --- trennen
        split_at = text.rfind("\n---", 0, max_len)
        if split_at > 0:
            chunks.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip("\n-").lstrip()
            continue
        # Dann an Zeilenumbruch
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind(" ", 0, max_len)
        if split_at < max_len // 4:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
