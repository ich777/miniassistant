"""
Matrix-Bot: Verbindung mit matrix-nio, Sync, bei Nachricht entweder Auth-Code senden
oder (wenn autorisiert) KI-Antwort. Verschlüsselte Räume optional (encrypted_rooms).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def markdown_to_matrix_html(text: str) -> str | None:
    """Konvertiert Markdown zu HTML für Matrix (org.matrix.custom.html). Bei fehlendem mistune: None."""
    if not (text or "").strip():
        return None
    try:
        import mistune
        html = mistune.html(text)
        return html.strip() if html else None
    except Exception:
        return None


# Globale Referenzen fuer Notify-Integration (Scheduler -> Bot-Client mit E2EE)
_bot_client: Any = None
_bot_loop: Any = None
_bot_send_fn: Any = None  # async fn(client, room_id, body)
_bot_send_image_fn: Any = None  # async fn(client, room_id, image_path, caption)


def send_message_to_user(target_user_id: str, message: str) -> bool:
    """Thread-safe: Sendet eine Nachricht ueber den laufenden Bot-Client (mit E2EE).
    Wird von notify.py aufgerufen. Gibt True bei Erfolg zurueck."""
    if not _bot_client or not _bot_loop or not _bot_send_fn:
        return False
    import concurrent.futures

    async def _do_send() -> bool:
        cl = _bot_client
        # Raum finden: joined_rooms durchsuchen
        rooms = getattr(cl, "rooms", {}) or {}
        for rid, room in rooms.items():
            members = getattr(room, "users", {}) or {}
            if target_user_id in members:
                await _bot_send_fn(cl, rid, message)
                return True
        # Fallback: invited_members pruefen
        for rid, room in rooms.items():
            invited = getattr(room, "invited_users", {}) or {}
            if target_user_id in invited:
                await _bot_send_fn(cl, rid, message)
                return True
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _bot_loop)
        return future.result(timeout=60)
    except Exception as e:
        logger.warning("Matrix send_message_to_user fehlgeschlagen: %s", e)
        return False


def send_message_to_room(room_id: str, message: str, keep_typing: bool = True) -> bool:
    """Thread-safe: Sendet eine Textnachricht in einen bestimmten Raum.
    keep_typing=True (default): Typing-Indikator nach dem Senden wiederherstellen (für status_update mid-processing).
    keep_typing=False: Typing-Indikator nach dem Senden ausschalten (für finale Nachrichten, z.B. Scheduler)."""
    if not _bot_client or not _bot_loop or not _bot_send_fn:
        return False

    async def _do_send() -> bool:
        await _bot_send_fn(_bot_client, room_id, message)
        room_typing = getattr(_bot_client, "room_typing", None)
        if callable(room_typing):
            try:
                await room_typing(room_id, keep_typing)
            except Exception:
                pass
        return True

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _bot_loop)
        return future.result(timeout=30)
    except Exception as e:
        logger.warning("Matrix send_message_to_room fehlgeschlagen: %s", e)
        return False


def send_image_to_room(room_id: str, image_path: str, caption: str = "") -> bool:
    """Thread-safe: Sendet ein Bild in einen bestimmten Raum ueber den laufenden Bot-Client.
    Wird von chat_loop._run_tool (send_image) aufgerufen. Gibt True bei Erfolg zurueck.
    Stellt Typing-Indikator nach dem Senden wieder her."""
    if not _bot_client or not _bot_loop or not _bot_send_image_fn:
        return False

    async def _do_send() -> bool:
        await _bot_send_image_fn(_bot_client, room_id, image_path, caption)
        # Typing-Indikator wiederherstellen (Senden löscht ihn serverseitig)
        room_typing = getattr(_bot_client, "room_typing", None)
        if callable(room_typing):
            try:
                await room_typing(room_id, True)
            except Exception:
                pass
        return True

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _bot_loop)
        return future.result(timeout=120)
    except Exception as e:
        logger.warning("Matrix send_image_to_room fehlgeschlagen: %s", e)
        return False


def set_typing(room_id: str, typing: bool = True) -> bool:
    """Thread-safe: Setzt den Typing-Indikator in einem Matrix-Raum.
    Wird z.B. vom Debate-Tool aufgerufen, um zwischen Runden den Typing-Status zu halten."""
    if not _bot_client or not _bot_loop:
        return False

    async def _do_typing() -> bool:
        room_typing = getattr(_bot_client, "room_typing", None)
        if callable(room_typing):
            await room_typing(room_id, typing)
        return True

    try:
        future = asyncio.run_coroutine_threadsafe(_do_typing(), _bot_loop)
        return future.result(timeout=10)
    except Exception:
        return False


def send_image_to_user(target_user_id: str, image_path: str, caption: str = "") -> bool:
    """Thread-safe: Sendet ein Bild an einen User (sucht passenden Raum)."""
    if not _bot_client or not _bot_loop or not _bot_send_image_fn:
        return False

    async def _do_send() -> bool:
        cl = _bot_client
        rooms = getattr(cl, "rooms", {}) or {}
        for rid, room in rooms.items():
            members = getattr(room, "users", {}) or {}
            if target_user_id in members:
                await _bot_send_image_fn(cl, rid, image_path, caption)
                return True
        for rid, room in rooms.items():
            invited = getattr(room, "invited_users", {}) or {}
            if target_user_id in invited:
                await _bot_send_image_fn(cl, rid, image_path, caption)
                return True
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _bot_loop)
        return future.result(timeout=120)
    except Exception as e:
        logger.warning("Matrix send_image_to_user fehlgeschlagen: %s", e)
        return False


# Optional: matrix-nio (pip install miniassistant[matrix])
_NIO_IMPORT_ERROR: str | None = None
try:
    from nio import AsyncClient
    try:
        from nio import ClientConfig
    except ImportError:
        ClientConfig = None  # ältere matrix-nio
    from nio.events import RoomMessageText
    try:
        from nio.events import RoomMessageNotice
    except ImportError:
        RoomMessageNotice = None  # ältere matrix-nio
    try:
        from nio.events import RoomMessageImage
    except ImportError:
        RoomMessageImage = None  # ältere matrix-nio
    try:
        from nio.events.room_events import RoomMessage as _RoomMessageBase
    except ImportError:
        _RoomMessageBase = None
    try:
        from nio.events import MegolmEvent
    except ImportError:
        MegolmEvent = None  # optional
    NIO_AVAILABLE = True
except ImportError as e:
    NIO_AVAILABLE = False
    _NIO_IMPORT_ERROR = str(e)
    AsyncClient = None  # type: ignore
    RoomMessageText = None  # type: ignore
    RoomMessageNotice = None  # type: ignore
    RoomMessageImage = None  # type: ignore
    MegolmEvent = None  # type: ignore


def _get_chat_response(
    config: dict[str, Any],
    matrix_user_id: str,
    user_message: str,
    sessions: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
) -> str:
    """Synchroner Aufruf: Session für matrix_user_id, handle_user_input.
    Gibt **ausschließlich** den sichtbaren Content zurück – KEIN Thinking für Matrix."""
    from miniassistant.chat_loop import create_session, handle_user_input
    if matrix_user_id not in sessions:
        sessions[matrix_user_id] = create_session(config, None)
    session = sessions[matrix_user_id]
    # Chat-Kontext aktualisieren (Room-ID kann sich ändern)
    if room_id:
        session["chat_context"] = {"platform": "matrix", "room_id": room_id, "user_id": matrix_user_id}
    result = handle_user_input(session, user_message, allow_new_session=True, images=images)
    sessions[matrix_user_id] = result[1]
    # result[4] = reiner Content (ohne Thinking) für KI-Antworten.
    # result[3] = Thinking-Text (nur bei KI-Antworten gesetzt, bei Commands immer None).
    # Bei Commands ist result[4] None aber result[3] ebenfalls None → result[0] ist sicher.
    # Bei KI-Antworten mit Thinking aber ohne Content → result[0] enthält [Thinking]-Block → nicht verwenden.
    ai_content = result[4] if len(result) > 4 else None
    thinking = result[3] if len(result) > 3 else None
    if ai_content:
        return ai_content.strip()
    if not thinking:
        # Kein Thinking → Command-Antwort, result[0] ist sicher
        return (result[0] or "").strip()
    # KI hat nur gedacht, kein sichtbarer Content
    return ""


async def run_matrix_bot(config: dict[str, Any]) -> None:
    """
    Läuft als asyncio-Task: verbindet mit Matrix, sync-Loop, bei Nachricht Auth-Code oder KI.
    Beendet sich bei Fehler oder wenn config keine gültige Matrix-Config hat.
    """
    if not NIO_AVAILABLE:
        detail = f" ({_NIO_IMPORT_ERROR})" if _NIO_IMPORT_ERROR else ""
        logger.warning(
            "matrix-nio nicht installiert. Matrix-Bot deaktiviert.%s "
            "Im gleichen venv installieren: pip install -e '.[matrix]' oder pip install matrix-nio",
            detail,
        )
        return
    matrix_cfg = (config.get("chat_clients") or {}).get("matrix") or config.get("matrix")
    if not matrix_cfg or not matrix_cfg.get("homeserver") or not matrix_cfg.get("token"):
        return
    if not matrix_cfg.get("enabled", True):
        logger.info("Matrix-Bot deaktiviert (matrix.enabled: false).")
        return
    homeserver = matrix_cfg["homeserver"].rstrip("/")
    user_id = matrix_cfg.get("user_id")
    if not user_id:
        logger.warning("Matrix: user_id fehlt in der Config (z.B. @bot:example.org). Bot deaktiviert.")
        return
    token = matrix_cfg["token"]
    device_id = matrix_cfg.get("device_id") or "miniassistant"
    encrypted_rooms = matrix_cfg.get("encrypted_rooms", True)
    if device_id == "miniassistant" and encrypted_rooms:
        logger.warning(
            "Matrix: device_id ist der Default „miniassistant“. Für zuverlässige E2EE-Entschlüsselung "
            "sollte in der Config die device_id aus der Login-Antwort stehen (siehe docs/CONFIGURATION.md)."
        )

    # nio-Crypto-Meldungen (undecryptable Megolm, missing session, …) nicht anzeigen
    logging.getLogger("nio").setLevel(logging.ERROR)

    from miniassistant.chat_auth import get_or_generate_code, is_authorized

    # E2EE-Store im gleichen Verzeichnis wie die geladene Config (oder get_config_dir()), damit Keys persistent sind
    store_path = ""
    if encrypted_rooms:
        try:
            from miniassistant.config import get_config_dir
            base_dir = config.get("_config_dir") or get_config_dir()
            matrix_store_dir = Path(base_dir).expanduser().resolve() / "matrix"
            matrix_store_dir.mkdir(parents=True, exist_ok=True)
            store_path = str(matrix_store_dir)
            logger.info("Matrix E2EE-Store: %s (Keys/Sessions werden hier gespeichert)", store_path)
        except Exception as e:
            logger.warning("Matrix E2EE store_path konnte nicht gesetzt werden: %s. Bot läuft ohne Verschlüsselung.", e)

    client_config = None
    if store_path and ClientConfig is not None:
        try:
            client_config = ClientConfig(store_sync_tokens=True, encryption_enabled=True)
        except TypeError:
            client_config = ClientConfig(store_sync_tokens=True)
    client = AsyncClient(
        homeserver, user_id, device_id=device_id, store_path=store_path or "",
        config=client_config,
    )
    try:
        client.restore_login(user_id, device_id, token)
    except Exception as e:
        logger.error("Matrix restore_login fehlgeschlagen: %s", e)
        return
    # E2EE-Diagnose: olm wird von restore_login via load_store() geladen, wenn ENCRYPTION_ENABLED und store_path gesetzt
    try:
        from nio.crypto import ENCRYPTION_ENABLED
    except ImportError:
        ENCRYPTION_ENABLED = False
    if getattr(client, "olm", None):
        logger.info("Matrix: E2EE aktiv (Olm-Store unter %s) – verschlüsselte Nachrichten werden entschlüsselt.", store_path or "(nicht gesetzt)")
    else:
        logger.info(
            "Matrix: E2EE nicht verfügbar (ENCRYPTION_ENABLED=%s, store_path=%s). "
            "Für Entschlüsselung: pip install matrix-nio[e2e] und libolm (apt install libolm-dev), dann Bot neu starten.",
            ENCRYPTION_ENABLED, store_path or "(leer)",
        )

    # Sessions pro Matrix-User (für Chat-Verlauf)
    matrix_sessions: dict[str, Any] = {}
    # Pending Images: User hat Bild ohne Text geschickt → nächste Textnachricht bekommt das Bild
    _pending_images: dict[str, list[dict[str, Any]]] = {}

    async def _send_room_message(cl: Any, room_id: str, body: str) -> None:
        """Sendet eine Textnachricht im Raum. Mit mistune: m.text + org.matrix.custom.html (formatted_body)."""
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        formatted = markdown_to_matrix_html(body)
        if formatted:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = formatted
        try:
            await cl.room_send(
                room_id, "m.room.message", content,
                ignore_unverified_devices=True,
            )
        except TypeError:
            await cl.room_send(room_id, "m.room.message", content)
        except Exception as e:
            logger.exception("Matrix Senden fehlgeschlagen: %s", e)

    async def _send_room_image(cl: Any, room_id: str, image_path: str, caption: str = "") -> None:
        """Lädt ein Bild hoch (media repo) und sendet es als m.image im Raum."""
        import io as _io
        p = Path(image_path)
        if not p.exists():
            logger.warning("Matrix: Bilddatei nicht gefunden: %s", image_path)
            if caption:
                await _send_room_message(cl, room_id, f"{caption}\n\n_(Bild nicht gefunden: {image_path})_")
            return
        img_bytes = p.read_bytes()
        mime = "image/png"
        suffix = p.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif suffix == ".gif":
            mime = "image/gif"
        elif suffix == ".webp":
            mime = "image/webp"
        # Bilddimensionen aus Header lesen (Element zeigt ohne w/h als Attachment statt inline)
        img_w, img_h = 0, 0
        try:
            import struct as _struct
            if img_bytes[:8] == b'\x89PNG\r\n\x1a\n' and len(img_bytes) >= 24:
                # PNG: IHDR chunk ab Byte 16
                img_w, img_h = _struct.unpack('>II', img_bytes[16:24])
            elif img_bytes[:2] == b'\xff\xd8':
                # JPEG: SOF0/SOF2 Marker suchen
                _off = 2
                while _off < len(img_bytes) - 9:
                    if img_bytes[_off] != 0xFF:
                        break
                    marker = img_bytes[_off + 1]
                    seg_len = _struct.unpack('>H', img_bytes[_off + 2:_off + 4])[0]
                    if marker in (0xC0, 0xC2):
                        img_h, img_w = _struct.unpack('>HH', img_bytes[_off + 5:_off + 9])
                        break
                    _off += 2 + seg_len
        except Exception:
            pass
        try:
            resp, _keys = await cl.upload(
                _io.BytesIO(img_bytes),
                content_type=mime,
                filename=p.name,
                filesize=len(img_bytes),
            )
            mxc_uri = getattr(resp, "content_uri", None)
            if not mxc_uri:
                logger.warning("Matrix: Bild-Upload fehlgeschlagen: %s", resp)
                await _send_room_message(cl, room_id, f"Bild-Upload fehlgeschlagen: {resp}")
                return
            img_info: dict[str, Any] = {"mimetype": mime, "size": len(img_bytes)}
            if img_w and img_h:
                img_info["w"] = img_w
                img_info["h"] = img_h
            img_content: dict[str, Any] = {
                "msgtype": "m.image",
                "body": p.name,
                "url": mxc_uri,
                "info": img_info,
            }
            if caption:
                img_content["filename"] = p.name
                img_content["body"] = caption
            try:
                await cl.room_send(room_id, "m.room.message", img_content, ignore_unverified_devices=True)
            except TypeError:
                await cl.room_send(room_id, "m.room.message", img_content)
            logger.info("Matrix: Bild gesendet in %s: %s (%s, %d bytes)", room_id, mxc_uri, p.name, len(img_bytes))
        except Exception as e:
            logger.exception("Matrix: Bild-Upload/Senden fehlgeschlagen: %s", e)
            await _send_room_message(cl, room_id, f"Bild konnte nicht gesendet werden: {e}")

    def _has_body(event: Any) -> bool:
        return isinstance(event, (RoomMessageText, RoomMessageNotice)) or bool(getattr(event, "body", None))

    async def _download_mxc_image(mxc_url: str, file_info: dict[str, Any] | None = None, event_mime: str = "") -> dict[str, Any] | None:
        """Lädt ein Bild von einer mxc:// URL herunter und gibt {mime_type, data} zurück.
        Bei verschlüsselten Bildern (file_info mit key/iv/hashes) wird entschlüsselt.
        event_mime: MIME-Type aus dem Matrix-Event (zuverlässiger bei E2EE als Download-Response)."""
        import base64 as _b64
        try:
            resp = await client.download(mxc_url)
            if not (hasattr(resp, "body") and resp.body):
                logger.warning("Matrix: Bild-Download fehlgeschlagen für %s", mxc_url)
                return None
            img_bytes = resp.body
            content_type = getattr(resp, "content_type", "") or ""
            # Verschlüsseltes Attachment entschlüsseln
            if file_info and file_info.get("key") and file_info.get("iv"):
                try:
                    from nio.crypto.attachments import decrypt_attachment
                    key_str = file_info["key"].get("k", "")
                    iv_str = file_info.get("iv", "")
                    hash_str = (file_info.get("hashes") or {}).get("sha256", "")
                    img_bytes = decrypt_attachment(img_bytes, key_str, hash_str, iv_str)
                    logger.debug("Matrix: Verschlüsseltes Bild entschlüsselt (%d bytes)", len(img_bytes))
                except ImportError:
                    logger.warning("Matrix: nio.crypto.attachments nicht verfügbar – pip install matrix-nio[e2e]")
                    return None
                except Exception as e:
                    logger.warning("Matrix: Entschlüsselung fehlgeschlagen: %s", e)
                    return None
            # Validierung: Entschlüsselte Daten müssen mit einem gültigen Bild-Header beginnen
            _valid_headers = (b'\x89PNG', b'\xff\xd8', b'GIF8', b'RIFF', b'BM')
            if not any(img_bytes[:4].startswith(h) for h in _valid_headers):
                logger.warning("Matrix: Entschlüsselte Daten haben keinen gültigen Bild-Header (erste 8 bytes: %s, %d bytes gesamt)",
                               img_bytes[:8].hex(), len(img_bytes))
                # Trotzdem weitermachen – evtl. unbekanntes Format
            # MIME-Type bestimmen: 1) Event-Metadaten (zuverlässigster bei E2EE), 2) Header-Magic-Bytes, 3) Download-Response
            if event_mime and event_mime.startswith("image/"):
                content_type = event_mime
                logger.debug("Matrix: MIME-Type aus Event-Metadaten: %s", content_type)
            elif not content_type or content_type == "application/octet-stream":
                if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                    content_type = "image/png"
                elif img_bytes[:2] == b'\xff\xd8':
                    content_type = "image/jpeg"
                elif img_bytes[:4] == b'GIF8':
                    content_type = "image/gif"
                elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
                    content_type = "image/webp"
                else:
                    content_type = "image/png"  # Fallback
                logger.debug("Matrix: MIME-Type aus Header erkannt: %s", content_type)
            logger.info("Matrix: Bild heruntergeladen: %d bytes, mime=%s", len(img_bytes), content_type)
            b64_data = _b64.b64encode(img_bytes).decode("ascii")
            return {"mime_type": content_type, "data": b64_data}
        except Exception as e:
            logger.warning("Matrix: Bild-Download Fehler für %s: %s", mxc_url, e)
        return None

    async def on_image(room_id: str, event: Any) -> None:
        """Wird bei m.image Events aufgerufen – Bild herunterladen und als Pending speichern.
        Unterstützt verschlüsselte (E2EE) und unverschlüsselte Bilder."""
        sender = getattr(event, "sender", None) or ""
        if not sender or sender == user_id:
            return
        config_dir = config.get("_config_dir")
        if not is_authorized("matrix", sender, config_dir):
            code = get_or_generate_code("matrix", sender, config_dir)
            await _send_room_message(client, room_id, f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\nGib in der Web-UI ein: `/auth matrix {code}`")
            return
        # URL und Verschlüsselungsinfo extrahieren
        # Quellen: 1) event-Attribute (nio RoomMessageImage), 2) source.content.file (verschlüsselt)
        mxc_url = getattr(event, "url", None) or ""
        file_info: dict[str, Any] | None = None
        source = getattr(event, "source", None) or {}
        content = source.get("content") or {} if isinstance(source, dict) else {}
        # Verschlüsselungsinfo aus source.content.file extrahieren (IMMER, auch wenn event.url gesetzt)
        if isinstance(content, dict) and content.get("file"):
            enc_file = content["file"]
            if not mxc_url:
                mxc_url = enc_file.get("url", "")
            file_info = enc_file
        # Fallback: Verschlüsselungsinfo direkt vom Event-Objekt (nio setzt key/iv/hashes als Attribute)
        if not file_info:
            ev_key = getattr(event, "key", None)
            ev_iv = getattr(event, "iv", None)
            ev_hashes = getattr(event, "hashes", None)
            if ev_key and ev_iv:
                file_info = {"key": ev_key, "iv": ev_iv, "hashes": ev_hashes or {}}
        if not mxc_url:
            logger.debug("Matrix: Bild-Event ohne URL ignoriert (source: %s)", list(content.keys()) if content else "leer")
            return
        # MIME-Type und Größe aus Event-Metadaten (zuverlässiger als Download-Response bei E2EE)
        event_info = content.get("info") or {} if isinstance(content, dict) else {}
        event_mime = event_info.get("mimetype", "") if isinstance(event_info, dict) else ""
        event_size = event_info.get("size", 0) if isinstance(event_info, dict) else 0
        logger.info("Matrix: Bild von %s in %s: %s (verschlüsselt: %s, mime: %s, size: %s)",
                     sender, room_id, mxc_url, bool(file_info), event_mime or "?", event_size or "?")
        img_data = await _download_mxc_image(mxc_url, file_info=file_info, event_mime=event_mime)
        if not img_data:
            await _send_room_message(client, room_id, "Konnte das Bild nicht herunterladen.")
            return
        # Pending Image speichern – nächste Textnachricht bekommt es
        _pending_images.setdefault(sender, []).append(img_data)
        await _send_room_message(client, room_id, "Bild empfangen. Was soll ich damit machen? (Schreib mir eine Nachricht dazu)")

    async def on_message(room_id: str, event: Any) -> None:
        # Bild-Events abfangen: RoomMessageImage, msgtype=m.image, oder body+url Heuristik
        _is_image = (RoomMessageImage and isinstance(event, RoomMessageImage))
        if not _is_image:
            source = getattr(event, "source", None) or {}
            src_content = source.get("content") or {} if isinstance(source, dict) else {}
            if isinstance(src_content, dict) and src_content.get("msgtype") == "m.image":
                _is_image = True
        if not _is_image:
            # Fallback: event.url oder event.source.content.file vorhanden → wahrscheinlich ein Bild
            if getattr(event, "url", None):
                _is_image = True
            else:
                source = getattr(event, "source", None) or {}
                sc = source.get("content") or {} if isinstance(source, dict) else {}
                if isinstance(sc, dict) and sc.get("file"):
                    _is_image = True
        if _is_image:
            logger.info("Matrix: Bild-Event in on_message erkannt (%s), delegiere an on_image", type(event).__name__)
            await on_image(room_id, event)
            return
        if not _has_body(event):
            logger.debug("Matrix: Ereignis ignoriert (kein Text/Notice): %s", type(event).__name__)
            return
        sender = getattr(event, "sender", None) or ""
        body = (getattr(event, "body") or "").strip()
        logger.info("Matrix: Nachricht von %s in %s: %.80s", sender, room_id, body or "(leer)")
        if not body or sender == user_id:
            if sender == user_id:
                logger.debug("Matrix: eigene Nachricht übersprungen")
            return
        # /stop und /abort: Cancellation-Befehle abfangen
        if body.lower() in ("/stop", "/abort"):
            from miniassistant.cancellation import request_cancel
            level = "abort" if body.lower() == "/abort" else "stop"
            request_cancel(sender, level)
            logger.info("Matrix: %s von %s — Cancellation angefordert (%s)", body, sender, level)
            reply = "⏹ Verarbeitung wird abgebrochen…" if level == "abort" else "⏸ Verarbeitung wird nach aktuellem Schritt gestoppt…"
            await _send_room_message(client, room_id, reply)
            return
        config_dir = config.get("_config_dir")
        if not is_authorized("matrix", sender, config_dir):
            code = get_or_generate_code("matrix", sender, config_dir)
            logger.info("Matrix: Auth-Code an %s gesendet: %s", sender, code)
            reply = (
                f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\n"
                f"Gib in der Web-UI ein: `/auth matrix {code}`"
            )
        else:
            # Pending Images abholen (Bild wurde vorher ohne Text geschickt)
            msg_images = _pending_images.pop(sender, None) or None
            # Typing-Indikator: Bot „tippt", solange er denkt/schreibt
            room_typing = getattr(client, "room_typing", None)
            typing_task: asyncio.Task | None = None
            if callable(room_typing):
                async def _keep_typing(_rid: str = room_id) -> None:
                    try:
                        while True:
                            await room_typing(_rid, True)
                            await asyncio.sleep(15)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                typing_task = asyncio.create_task(_keep_typing())
            try:
                reply = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda s=sender, b=body, imgs=msg_images, rid=room_id: _get_chat_response(config, s, b, matrix_sessions, images=imgs, room_id=rid),
                )
            except Exception as e:
                logger.exception("Matrix KI-Antwort fehlgeschlagen: %s", e)
                reply = f"Fehler bei der Verarbeitung: {e}"
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
            if callable(room_typing):
                try:
                    await room_typing(room_id, False)
                except Exception:
                    pass
        if reply:
            await _send_room_message(client, room_id, reply)

    async def on_encrypted_message(room_id: str, event: Any) -> None:
        """Wird nur aufgerufen, wenn nio den Event als MegolmEvent übergibt – also nach gescheitertem Entschlüsselungsversuch (Key fehlt). Raum bleibt verschlüsselt; wir markieren ihn nirgends als unverschlüsselt. Key-Request senden, dann Hinweis oder Auth-Code."""
        sender = getattr(event, "sender", None) or ""
        if not sender or sender == user_id:
            return
        logger.info(
            "Matrix: Verschlüsselte Nachricht von %s in %s (Entschlüsselung fehlgeschlagen – Raumschlüssel fehlt)",
            sender, room_id,
        )
        # Explizit Room-Key-Request an den Absender senden (nio macht das nicht immer automatisch nach sync)
        try:
            sender_device_id = getattr(event, "device_id", None)
            session_id = getattr(event, "session_id", None)
            algorithm = getattr(event, "algorithm", None) or "m.megolm.v1.aes-sha2"
            if sender_device_id and session_id and hasattr(client, "room_key_request"):
                from nio.event_builders.direct_messages import RoomKeyRequestMessage
                request_id = str(uuid.uuid4())
                msg = RoomKeyRequestMessage(request_id, session_id, room_id, algorithm)
                resp = await client.room_key_request(sender, sender_device_id, msg)
                if resp and getattr(resp, "status_code", None):
                    logger.debug("Matrix: Room-Key-Request an %s gesendet (Antwort: %s)", sender, resp)
                else:
                    logger.debug("Matrix: Room-Key-Request an %s gesendet", sender)
        except Exception as e:
            logger.debug("Matrix: Room-Key-Request konnte nicht gesendet werden: %s", e)
        if is_authorized("matrix", sender, config.get("_config_dir")):
            reply = (
                "Du bist freigeschaltet; bei dieser Nachricht hatte ich (noch) keinen Raumschlüssel – ich konnte sie deshalb nicht lesen. "
                "**Verschlüsselung** (E2EE) und **Verifizierung** sind unterschiedlich: Ich brauche keine Verifizierung, nur den Schlüsselaustausch. "
                "In Element unter **Einstellungen → Sicherheit & Datenschutz** die Option **„Verschlüsselte Nachrichten nie an unverifizierte Sitzungen senden“** **deaktivieren**. Dann schickt Element die Raumschlüssel auch an mein Gerät; ich habe bereits einen Key-Request gesendet – oft reicht eine weitere Nachricht, dann klappt es. "
                "Als Ausweichmöglichkeit: in einem neuen **unverschlüsselten** Raum schreiben."
            )
        else:
            code = get_or_generate_code("matrix", sender, config.get("_config_dir"))
            logger.info("Matrix: Auth-Code an %s gesendet: %s", sender, code)
            reply = (
                "Bei dieser Nachricht fehlte mir der Raumschlüssel – ich konnte sie nicht lesen und habe den Absender um den Schlüssel gebeten. "
                "Für die erste Freischaltung am besten einen **unverschlüsselten** Raum nutzen (oder hier nochmal schreiben, falls der Schlüssel nachkommt).\n\n"
                f"Dein Auth-Code: **{code}**\n\n"
                f"Antworte hier mit: `/auth {code}`"
            )
        await _send_room_message(client, room_id, reply)

    def _on_room_message(room: Any, event: Any) -> None:
        try:
            room_id = getattr(room, "room_id", None) or ""
            loop = asyncio.get_running_loop()
            loop.create_task(on_message(room_id, event))
        except Exception as e:
            logger.debug("Matrix callback: %s", e)

    def _on_encrypted(room: Any, event: Any) -> None:
        try:
            room_id = getattr(room, "room_id", None) or ""
            loop = asyncio.get_running_loop()
            loop.create_task(on_encrypted_message(room_id, event))
        except Exception as e:
            logger.debug("Matrix callback (encrypted): %s", e)

    # ---------- E2EE Key-Management (analog sync_forever) ----------
    async def _e2ee_keys() -> None:
        """Upload / Query / Claim der Device-Keys – nötig damit andere Clients
        dem Bot Megolm-Sessions teilen.  sync_forever() macht das automatisch,
        bei manuellem sync()-Loop muss man es selbst aufrufen."""
        if not getattr(client, "olm", None):
            return
        try:
            if getattr(client, "should_upload_keys", False):
                await client.keys_upload()
                logger.debug("Matrix: Device-Keys hochgeladen (keys_upload)")
        except Exception as exc:
            logger.debug("Matrix keys_upload: %s", exc)
        try:
            if getattr(client, "should_query_keys", False):
                await client.keys_query()
                logger.debug("Matrix: Device-Keys anderer Nutzer abgefragt (keys_query)")
        except Exception as exc:
            logger.debug("Matrix keys_query: %s", exc)
        try:
            claim_fn = getattr(client, "keys_claim", None)
            users_fn = getattr(client, "get_users_for_key_claiming", None)
            if getattr(client, "should_claim_keys", False) and claim_fn and users_fn:
                users = users_fn()
                if users:
                    await claim_fn(users)
                    logger.debug("Matrix: Olm-Sessions aufgebaut (keys_claim)")
        except Exception as exc:
            logger.debug("Matrix keys_claim: %s", exc)

    # Initialer Sync + Key-Upload BEVOR Callbacks registriert werden,
    # damit (a) alte Nachrichten nicht erneut beantwortet werden und
    # (b) die Device-Keys des Bots am Server bekannt sind.
    if getattr(client, "olm", None):
        logger.info("Matrix: Initialer Sync + E2EE Key-Upload …")
        try:
            await client.sync(timeout=30000, full_state=True)
            await _e2ee_keys()
            logger.info(
                "Matrix: E2EE-Keys am Server registriert – "
                "Bot kann jetzt verschlüsselte Nachrichten empfangen."
            )
        except Exception as e:
            logger.warning("Matrix: Initialer E2EE-Setup fehlgeschlagen: %s – Bot startet trotzdem.", e)

    # Einen breiten Callback für ALLE RoomMessage-Typen registrieren.
    # In verschlüsselten Räumen kommen entschlüsselte Bilder manchmal nicht als RoomMessageImage
    # durch den spezifischen Callback, sondern als generischer RoomMessage-Typ.
    # on_message erkennt Bilder via msgtype-Check und delegiert an on_image.
    if _RoomMessageBase:
        client.add_event_callback(_on_room_message, _RoomMessageBase)
        logger.info("Matrix: Callback für alle RoomMessage-Typen registriert (inkl. Bilder)")
    else:
        _message_types = (RoomMessageText,) + ((RoomMessageNotice,) if RoomMessageNotice else ()) + ((RoomMessageImage,) if RoomMessageImage else ())
        client.add_event_callback(_on_room_message, _message_types)
        logger.info("Matrix: Callback für %d Message-Typen registriert", len(_message_types))
    if MegolmEvent:
        client.add_event_callback(_on_encrypted, MegolmEvent)

    logger.info(
        "Matrix-Bot gestartet (user_id=%s, device_id=%s, encrypted_rooms=%s)",
        user_id, device_id, encrypted_rooms,
    )

    # Globale Referenzen fuer Notify setzen (Scheduler kann ueber Bot senden)
    global _bot_client, _bot_loop, _bot_send_fn, _bot_send_image_fn
    _bot_client = client
    _bot_loop = asyncio.get_running_loop()
    _bot_send_fn = _send_room_message
    _bot_send_image_fn = _send_room_image

    _rooms = getattr(client, "rooms", {}) or {}
    if _rooms:
        logger.info("Matrix: %s Raum/Räume bekannt (nach erstem Sync kommen Timeline-Nachrichten)", len(_rooms))
    # Räume, bei denen Beitritt dauerhaft fehlschlägt (z. B. M_UNKNOWN / "no servers") – nicht ewig retry, Einladung ablehnen
    join_failed_rooms: set[str] = set()
    # Fehler-Text, ab dem wir die Einladung als "nicht beitretbar" ablehnen
    _unrecoverable_join_errors = ("no servers", "M_UNKNOWN", "M_FORBIDDEN", "M_NOT_FOUND")

    try:
        while True:
            try:
                await client.sync(timeout=30000, full_state=False)
                await _e2ee_keys()
                invited = getattr(client, "invited_rooms", {}) or {}
                for room_id in list(invited.keys()):
                    if room_id in join_failed_rooms:
                        continue
                    try:
                        resp = await client.join(room_id)
                        if resp and getattr(resp, "room_id", None):
                            logger.info("Matrix: Raum beigetreten: %s", room_id)
                        else:
                            err_msg = str(resp) if resp else "unknown"
                            if any(x in err_msg for x in _unrecoverable_join_errors):
                                join_failed_rooms.add(room_id)
                                try:
                                    await client.leave(room_id)
                                    logger.info("Matrix: Einladung abgelehnt (nicht beitretbar): %s", room_id)
                                except Exception:
                                    pass
                            else:
                                logger.warning("Matrix: Beitritt fehlgeschlagen für %s: %s", room_id, resp)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        err_msg = str(e)
                        if any(x in err_msg for x in _unrecoverable_join_errors):
                            join_failed_rooms.add(room_id)
                            try:
                                await client.leave(room_id)
                                logger.info("Matrix: Einladung abgelehnt (nicht beitretbar): %s", room_id)
                            except Exception:
                                pass
                        else:
                            logger.warning("Matrix: Beitritt zu Raum %s fehlgeschlagen: %s", room_id, e)
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Matrix Sync-Fehler: %s", e)
                await asyncio.sleep(5)
    finally:
        # aiohttp-Session schließen, damit beim Server-Shutdown keine "Unclosed client session"-Warnung entsteht
        session = getattr(client, "client_session", None)
        if session is not None and not session.closed:
            await session.close()
