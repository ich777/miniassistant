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


def _get_chat_response(
    config: dict[str, Any],
    discord_user_id: str,
    user_message: str,
    sessions: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
    channel_id: str | None = None,
) -> str:
    """Synchroner Aufruf: Session für discord_user_id, handle_user_input.
    Gibt ausschließlich den sichtbaren Content zurück – KEIN Thinking."""
    from miniassistant.chat_loop import create_session, handle_user_input
    if discord_user_id not in sessions:
        session = create_session(config, None)
        session["system_prompt"] = (
            session.get("system_prompt", "") +
            "\n\nDiscord: Max 2000 Zeichen/Nachricht. Laengere Antworten mit `---` trennen, werden automatisch aufgeteilt."
        )
        sessions[discord_user_id] = session
    session = sessions[discord_user_id]
    # Chat-Kontext aktualisieren (Channel-ID kann sich ändern)
    if channel_id:
        session["chat_context"] = {"platform": "discord", "channel_id": channel_id, "user_id": discord_user_id}
    result = handle_user_input(session, user_message, allow_new_session=True, images=images)
    sessions[discord_user_id] = result[1]
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

    discord_cfg = (config.get("chat_clients") or {}).get("discord") or config.get("discord")
    if not discord_cfg or not discord_cfg.get("bot_token"):
        return
    if not discord_cfg.get("enabled", True):
        logger.info("Discord-Bot deaktiviert (discord.enabled: false).")
        return

    bot_token = discord_cfg["bot_token"]

    from miniassistant.chat_auth import get_or_generate_code, is_authorized

    # Sessions pro Discord-User
    discord_sessions: dict[str, Any] = {}
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

    @client.event
    async def on_message(message: discord.Message) -> None:
        # Eigene Nachrichten ignorieren
        if message.author == client.user:
            return

        # Nur DMs oder @-Mentions in Channels
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = client.user is not None and client.user.mentioned_in(message)

        if not is_dm and not is_mentioned:
            return

        sender_id = str(message.author.id)
        body = message.content.strip()

        # Bei @-Mention: Bot-Mention aus Text entfernen
        if is_mentioned and client.user is not None:
            body = body.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

        # Bild-Attachments herunterladen
        msg_images: list[dict[str, Any]] = []
        for att in message.attachments:
            ct = (att.content_type or "").lower()
            if ct.startswith("image/") or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                try:
                    import base64 as _b64
                    img_bytes = await att.read()
                    mime = ct if ct.startswith("image/") else "image/png"
                    b64_data = _b64.b64encode(img_bytes).decode("ascii")
                    msg_images.append({"mime_type": mime, "data": b64_data})
                    logger.info("Discord: Bild-Attachment von %s: %s (%s)", sender_id, att.filename, mime)
                except Exception as e:
                    logger.warning("Discord: Bild-Download fehlgeschlagen für %s: %s", att.filename, e)

        # Bild ohne Text → Pending speichern, User fragen
        if msg_images and not body:
            _pending_images.setdefault(sender_id, []).extend(msg_images)
            await message.reply("Bild empfangen. Was soll ich damit machen?")
            return

        if not body and not msg_images:
            return

        # /stop und /abort: Cancellation-Befehle abfangen
        if body.lower() in ("/stop", "/abort"):
            from miniassistant.cancellation import request_cancel
            level = "abort" if body.lower() == "/abort" else "stop"
            request_cancel(sender_id, level)
            logger.info("Discord: %s von %s — Cancellation angefordert (%s)", body, sender_id, level)
            reply = "⏹ Verarbeitung wird abgebrochen…" if level == "abort" else "⏸ Verarbeitung wird nach aktuellem Schritt gestoppt…"
            await message.reply(reply)
            return

        # Pending Images abholen (Bild wurde vorher ohne Text geschickt)
        if not msg_images and sender_id in _pending_images:
            msg_images = _pending_images.pop(sender_id)

        logger.info("Discord: Nachricht von %s (%s): %.80s", message.author, sender_id, body)

        config_dir = config.get("_config_dir")
        if not is_authorized("discord", sender_id, config_dir):
            code = get_or_generate_code("discord", sender_id, config_dir)
            logger.info("Discord: Auth-Code an %s gesendet: %s", sender_id, code)
            await message.reply(
                f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\n"
                f"Gib in der Web-UI ein: `/auth discord {code}`"
            )
            return

        images_param = msg_images if msg_images else None

        # Typing-Indicator + KI-Antwort
        async with message.channel.typing():
            try:
                reply = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda s=sender_id, b=body, imgs=images_param, cid=str(message.channel.id): _get_chat_response(config, s, b, discord_sessions, images=imgs, channel_id=cid),
                )
            except Exception as e:
                logger.exception("Discord KI-Antwort fehlgeschlagen: %s", e)
                reply = f"Fehler bei der Verarbeitung: {e}"

        if not reply:
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
