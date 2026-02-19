"""
Benachrichtigungen an Chat-Clients senden (Matrix, Discord).
Wird vom /api/notify Endpoint und vom Scheduler genutzt.
Leichtgewichtig: eigene HTTP-Calls, keine laufenden Bot-Instanzen noetig.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from miniassistant.config import load_config

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     [notify] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def send_notification(message: str, client: str | None = None, config: dict[str, Any] | None = None) -> dict[str, str]:
    """
    Sendet eine Nachricht an konfigurierte Chat-Clients.
    client: 'matrix', 'discord' oder None (= alle konfigurierten).
    Returns dict mit Ergebnissen pro Client.
    """
    if config is None:
        config = load_config()
    cc = config.get("chat_clients") or {}
    results: dict[str, str] = {}

    if client is None or client == "matrix":
        mc = cc.get("matrix") or config.get("matrix")
        if mc and mc.get("enabled", True) and mc.get("token") and mc.get("user_id"):
            results["matrix"] = _send_matrix(mc, message, config.get("_config_dir") if config else None)
        elif client == "matrix":
            results["matrix"] = "nicht konfiguriert"

    if client is None or client == "discord":
        dc = cc.get("discord")
        if dc and dc.get("enabled", True) and dc.get("bot_token"):
            results["discord"] = _send_discord(dc, message, config.get("_config_dir") if config else None)
        elif client == "discord":
            results["discord"] = "nicht konfiguriert"

    if not results:
        results["error"] = "Kein Chat-Client konfiguriert"
    return results


def _send_matrix(mc: dict[str, Any], message: str, config_dir: str | None = None) -> str:
    """Sendet via laufenden Matrix-Bot-Client (E2EE-faehig).
    Fallback auf raw HTTP nur wenn Bot-Client nicht verfuegbar (unverschluesselte Raeume)."""

    try:
        from miniassistant.chat_auth import list_authorized
        authorized = list_authorized("matrix", config_dir)
    except Exception:
        authorized = []

    if not authorized:
        return "keine autorisierten Matrix-User"

    target_users: list[str] = []
    for entry in authorized:
        uid = entry.get("user_id", entry) if isinstance(entry, dict) else entry
        if uid and isinstance(uid, str):
            target_users.append(uid)

    if not target_users:
        return "keine autorisierten Matrix-User"

    # Bevorzugt: ueber den laufenden Bot-Client senden (E2EE-faehig)
    try:
        from miniassistant.matrix_bot import send_message_to_user
    except ImportError:
        send_message_to_user = None  # type: ignore

    sent_to: list[str] = []
    for mx_user in target_users:
        if send_message_to_user and send_message_to_user(mx_user, message):
            sent_to.append(mx_user)
            logger.info("Matrix -> %s (via Bot)", mx_user)
        else:
            # Fallback: raw HTTP (nur fuer unverschluesselte Raeume)
            ok = _send_matrix_http(mc, mx_user, message)
            if ok:
                sent_to.append(mx_user)
                logger.info("Matrix -> %s (via HTTP)", mx_user)
            else:
                logger.warning("Matrix -> %s fehlgeschlagen", mx_user)

    return f"gesendet an {len(sent_to)} User" if sent_to else "senden fehlgeschlagen"


def _send_matrix_http(mc: dict[str, Any], mx_user: str, message: str) -> bool:
    """Fallback: raw HTTP fuer unverschluesselte Raeume (ohne Bot-Client)."""
    import urllib.request
    import json
    import uuid as _uuid

    homeserver = (mc.get("homeserver") or "").rstrip("/")
    token = mc.get("token", "")
    if not homeserver or not token:
        return False

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Bestehenden Raum suchen via joined_rooms
    try:
        req = urllib.request.Request(f"{homeserver}/_matrix/client/v3/joined_rooms", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            rooms = json.loads(resp.read()).get("joined_rooms", [])
    except Exception:
        return False

    room_id = None
    for rid in rooms:
        try:
            url = f"{homeserver}/_matrix/client/v3/rooms/{urllib.parse.quote(rid)}/joined_members"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                members = json.loads(resp.read()).get("joined", {})
            if mx_user in members:
                room_id = rid
                break
        except Exception:
            continue

    if not room_id:
        return False

    try:
        content: dict[str, Any] = {"msgtype": "m.text", "body": message}
        try:
            from miniassistant.matrix_bot import markdown_to_matrix_html
            formatted = markdown_to_matrix_html(message)
            if formatted:
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = formatted
        except Exception:
            pass
        send_url = f"{homeserver}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{_uuid.uuid4()}"
        body = json.dumps(content).encode()
        req = urllib.request.Request(send_url, data=body, headers=headers, method="PUT")
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.warning("Matrix HTTP -> %s: %s", mx_user, e)
        return False


def send_image(
    image_path: str,
    caption: str = "",
    client: str | None = None,
    room_id: str | None = None,
    channel_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Sendet ein Bild an Chat-Clients (Matrix/Discord).
    room_id/channel_id: Direkt an diesen Raum/Channel senden (aus chat_context).
    Ohne room_id/channel_id: an alle autorisierten User senden."""
    if config is None:
        config = load_config()
    cc = config.get("chat_clients") or {}
    results: dict[str, str] = {}

    if client is None or client == "matrix":
        mc = cc.get("matrix") or config.get("matrix")
        if mc and mc.get("enabled", True) and mc.get("token") and mc.get("user_id"):
            results["matrix"] = _send_matrix_image(mc, image_path, caption, room_id=room_id, config_dir=config.get("_config_dir"))
        elif client == "matrix":
            results["matrix"] = "nicht konfiguriert"

    if client is None or client == "discord":
        dc = cc.get("discord")
        if dc and dc.get("enabled", True) and dc.get("bot_token"):
            results["discord"] = _send_discord_image(dc, image_path, caption, channel_id=channel_id, config_dir=config.get("_config_dir"))
        elif client == "discord":
            results["discord"] = "nicht konfiguriert"

    if not results:
        results["error"] = "Kein Chat-Client konfiguriert"
    return results


def _send_matrix_image(mc: dict[str, Any], image_path: str, caption: str = "", room_id: str | None = None, config_dir: str | None = None) -> str:
    """Sendet ein Bild via Matrix. Bevorzugt: room_id direkt, sonst an alle autorisierten User."""
    try:
        from miniassistant.matrix_bot import send_image_to_room, send_image_to_user
    except ImportError:
        return "matrix-nio nicht verfügbar"

    if room_id:
        ok = send_image_to_room(room_id, image_path, caption)
        return f"Bild gesendet in Raum {room_id}" if ok else f"Bild-Upload fehlgeschlagen für Raum {room_id}"

    try:
        from miniassistant.chat_auth import list_authorized
        authorized = list_authorized("matrix", config_dir)
    except Exception:
        authorized = []
    if not authorized:
        return "keine autorisierten Matrix-User"

    sent_to: list[str] = []
    for entry in authorized:
        uid = entry.get("user_id", entry) if isinstance(entry, dict) else entry
        if uid and isinstance(uid, str):
            if send_image_to_user(uid, image_path, caption):
                sent_to.append(uid)
    return f"Bild gesendet an {len(sent_to)} User" if sent_to else "Bild senden fehlgeschlagen"


def _send_discord_image(dc: dict[str, Any], image_path: str, caption: str = "", channel_id: str | None = None, config_dir: str | None = None) -> str:
    """Sendet ein Bild via Discord API (multipart upload)."""
    import urllib.request
    import json
    from pathlib import Path as _Path

    bot_token = dc.get("bot_token", "")
    if not bot_token:
        return "bot_token fehlt"

    p = _Path(image_path)
    if not p.exists():
        return f"Datei nicht gefunden: {image_path}"

    channels: list[str] = []
    if channel_id:
        channels.append(channel_id)
    else:
        try:
            from miniassistant.chat_auth import list_authorized
            authorized = list_authorized("discord", config_dir)
        except Exception:
            authorized = []
        for entry in authorized:
            discord_user_id = entry.get("user_id", entry) if isinstance(entry, dict) else entry
            if not discord_user_id or not isinstance(discord_user_id, str):
                continue
            try:
                create_url = "https://discord.com/api/v10/users/@me/channels"
                create_data = json.dumps({"recipient_id": discord_user_id}).encode()
                req = urllib.request.Request(
                    create_url, data=create_data,
                    headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    cid = json.loads(resp.read()).get("id")
                if cid:
                    channels.append(cid)
            except Exception:
                continue

    if not channels:
        return "kein Ziel-Channel gefunden"

    img_bytes = p.read_bytes()
    mime = "image/png"
    suffix = p.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif suffix == ".gif":
        mime = "image/gif"
    elif suffix == ".webp":
        mime = "image/webp"

    sent = 0
    for cid in channels:
        try:
            import uuid as _uuid
            boundary = f"----FormBoundary{_uuid.uuid4().hex[:16]}"
            body_parts = []
            if caption:
                body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"\r\n\r\n{caption}")
            body_parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{p.name}\"\r\nContent-Type: {mime}\r\n\r\n"
            )
            body_bytes = ("\r\n".join(body_parts)).encode("utf-8") + b"\r\n" + img_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
            send_url = f"https://discord.com/api/v10/channels/{cid}/messages"
            req = urllib.request.Request(
                send_url, data=body_bytes,
                headers={
                    "Authorization": f"Bot {bot_token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            urllib.request.urlopen(req, timeout=30)
            sent += 1
        except Exception as e:
            logger.warning("Discord Bild -> Channel %s fehlgeschlagen: %s", cid, e)

    return f"Bild gesendet an {sent} Channel" if sent else "Bild senden fehlgeschlagen"


def _send_discord(dc: dict[str, Any], message: str, config_dir: str | None = None) -> str:
    """Sendet via Discord Bot API (kein laufender Bot noetig)."""
    import urllib.request
    import json

    bot_token = dc.get("bot_token", "")
    if not bot_token:
        return "bot_token fehlt"

    try:
        from miniassistant.chat_auth import list_authorized
        authorized = list_authorized("discord", config_dir)
    except Exception:
        authorized = []

    if not authorized:
        return "keine autorisierten Discord-User"

    sent_to: list[str] = []
    for entry in authorized:
        discord_user_id = entry.get("user_id", entry) if isinstance(entry, dict) else entry
        if not discord_user_id or not isinstance(discord_user_id, str):
            continue
        try:
            # DM-Channel erstellen
            create_url = "https://discord.com/api/v10/users/@me/channels"
            create_data = json.dumps({"recipient_id": discord_user_id}).encode()
            req = urllib.request.Request(
                create_url,
                data=create_data,
                headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                channel_id = json.loads(resp.read()).get("id")

            if not channel_id:
                continue

            # Nachricht senden
            send_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            body = json.dumps({"content": message}).encode()
            req = urllib.request.Request(
                send_url,
                data=body,
                headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            sent_to.append(discord_user_id)
        except Exception as e:
            logger.warning("Discord -> %s fehlgeschlagen: %s", discord_user_id, e)

    return f"gesendet an {len(sent_to)} User" if sent_to else "senden fehlgeschlagen"
