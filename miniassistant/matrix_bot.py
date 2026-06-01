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
_bot_send_audio_fn: Any = None  # async fn(client, room_id, wav_bytes)

# Cache: room_id -> inviter user_id. None means "looked up, no inviter found".
_inviter_cache: dict[str, str | None] = {}


def _inviter_cache_path() -> Any:
    try:
        from miniassistant.config import get_config_dir
        from pathlib import Path
        return Path(get_config_dir()) / "matrix_inviter_cache.json"
    except Exception:
        return None


def _load_inviter_cache() -> None:
    p = _inviter_cache_path()
    if p is None or not p.exists():
        return
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str):
                    _inviter_cache[k] = v if isinstance(v, str) else None
            logger.info("Matrix: %d Inviter-Einträge aus Cache geladen", len(_inviter_cache))
    except Exception as e:
        logger.debug("inviter cache load failed: %s", e)


def _persist_inviter_cache() -> None:
    p = _inviter_cache_path()
    if p is None:
        return
    try:
        import json as _json
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(_inviter_cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("inviter cache persist failed: %s", e)


async def _fetch_inviter(client: Any, room_id: str, bot_user_id: str) -> str | None:
    """Fetch the user_id who invited the bot to this room. Cached + persisted to disk.
    Strategy: (1) read bot's current m.room.member event, follow unsigned.replaces_state to invite event.
              (2) fallback: walk room timeline backwards via room_messages.
    """
    if room_id in _inviter_cache:
        return _inviter_cache[room_id]
    # Strategy 1: room_get_state liefert ALLE state events mit unsigned data
    try:
        resp = await client.room_get_state(room_id)
        events = getattr(resp, "events", None)
        if isinstance(events, list):
            member_ev = next(
                (ev for ev in events
                 if isinstance(ev, dict) and ev.get("type") == "m.room.member" and ev.get("state_key") == bot_user_id),
                None,
            )
            if member_ev:
                unsigned = member_ev.get("unsigned") or {}
                prev_sender = unsigned.get("prev_sender")
                prev_content = unsigned.get("prev_content") or {}
                replaces_state = unsigned.get("replaces_state")
                if prev_sender and prev_content.get("membership") == "invite" and prev_sender != bot_user_id:
                    _inviter_cache[room_id] = prev_sender
                    _persist_inviter_cache()
                    logger.info("Matrix: Inviter für %s = %s (via prev_sender)", room_id, prev_sender)
                    return prev_sender
                if replaces_state:
                    ev_resp = await client.room_get_event(room_id, replaces_state)
                    ev_obj = getattr(ev_resp, "event", None)
                    ev_src = getattr(ev_obj, "source", None) if ev_obj else None
                    sender = None
                    if isinstance(ev_src, dict):
                        sender = ev_src.get("sender")
                    elif ev_obj and hasattr(ev_obj, "sender"):
                        sender = getattr(ev_obj, "sender", None)
                    if sender and sender != bot_user_id:
                        _inviter_cache[room_id] = sender
                        _persist_inviter_cache()
                        logger.info("Matrix: Inviter für %s = %s (via replaces_state)", room_id, sender)
                        return sender
    except Exception as e:
        logger.debug("inviter lookup (room_get_state) failed for %s: %s", room_id, e)
    # Try walking timeline backwards. Bot's invite event has type m.room.member, state_key=bot, membership=invite, sender=inviter.
    invite_sender: str | None = None
    try:
        from nio import MessageDirection
        # Need a sync token to start from. The room object has 'prev_batch' after sync.
        rooms = getattr(client, "rooms", {}) or {}
        room = rooms.get(room_id)
        start_token = getattr(room, "prev_batch", None) if room else None
        if not start_token:
            # Try fresh sync token via sync() — but that's heavy. Skip if no token.
            logger.debug("inviter: no prev_batch for %s, skip", room_id)
            _inviter_cache[room_id] = None
            _persist_inviter_cache()
            return None
        next_token = start_token
        for _ in range(20):  # up to 20 pages back
            resp = await client.room_messages(room_id, start=next_token, direction=MessageDirection.back, limit=100)
            chunk = getattr(resp, "chunk", None) or []
            for ev in chunk:
                # State events on the timeline carry source dict with 'type'/'state_key'/'sender'/'content'
                src = getattr(ev, "source", None)
                if not isinstance(src, dict):
                    continue
                if src.get("type") != "m.room.member":
                    continue
                if src.get("state_key") != bot_user_id:
                    continue
                content = src.get("content") or {}
                if content.get("membership") != "invite":
                    continue
                s = src.get("sender")
                if s and s != bot_user_id:
                    invite_sender = s
                    break
            if invite_sender:
                break
            next_token = getattr(resp, "end", None)
            if not next_token or next_token == start_token:
                break
            start_token = next_token  # noqa
    except Exception as e:
        logger.debug("inviter lookup (timeline walk) failed for %s: %s", room_id, e)
    _inviter_cache[room_id] = invite_sender
    _persist_inviter_cache()
    if invite_sender:
        logger.info("Matrix: Inviter für Raum %s = %s (gefunden via Timeline)", room_id, invite_sender)
    else:
        logger.debug("Matrix: kein Inviter für Raum %s gefunden (Bot wurde evtl. selbst beigetreten oder Historie zu kurz)", room_id)
    return invite_sender


def get_room_inviter(room_id: str) -> str | None:
    """Sync, cached-only lookup. Returns None if not yet cached."""
    return _inviter_cache.get(room_id)


def get_inviter_cache() -> dict[str, str | None]:
    return dict(_inviter_cache)


def list_joined_rooms() -> list[dict[str, Any]]:
    """List currently joined Matrix rooms with display name + member count + auth info for non-bot users.
    Returns [] if bot is not running. Safe to call from any thread."""
    cl = _bot_client
    if not cl:
        return []
    try:
        from miniassistant.chat_auth import is_authorized
    except Exception:
        is_authorized = None  # type: ignore
    try:
        from miniassistant.config import get_config_dir as _cfgdir
        config_dir = _cfgdir()
    except Exception:
        config_dir = None
    bot_user_id = getattr(cl, "user_id", None) or getattr(cl, "user", None) or ""
    out: list[dict[str, Any]] = []
    try:
        rooms = getattr(cl, "rooms", {}) or {}
        for rid, room in rooms.items():
            name = (
                getattr(room, "display_name", None)
                or getattr(room, "name", None)
                or getattr(room, "canonical_alias", None)
                or rid
            )
            members = getattr(room, "users", {}) or {}
            try:
                member_count = len(members)
            except Exception:
                member_count = 0
            encrypted = bool(getattr(room, "encrypted", False))
            others: list[dict[str, Any]] = []
            try:
                for uid in members.keys():
                    if uid == bot_user_id:
                        continue
                    authed = bool(is_authorized("matrix", uid, config_dir)) if is_authorized else False
                    others.append({"user_id": uid, "authed": authed})
            except Exception:
                pass
            auth_count = sum(1 for o in others if o["authed"])
            inviter = _inviter_cache.get(rid)  # None if unknown / not cached
            inviter_authed = bool(is_authorized("matrix", inviter, config_dir)) if (inviter and is_authorized) else False
            room_trusted = inviter_authed  # whole room is "trusted" if inviter is authed
            out.append({
                "id": rid,
                "name": str(name),
                "members": member_count,
                "encrypted": encrypted,
                "others": others,
                "auth_count": auth_count,
                "others_count": len(others),
                "inviter": inviter,
                "inviter_authed": inviter_authed,
                "room_trusted": room_trusted,
            })
    except Exception as e:
        logger.warning("list_joined_rooms failed: %s", e)
    out.sort(key=lambda r: (r["members"] != 2, r["name"].lower()))
    return out


def leave_room(room_id: str) -> tuple[bool, str]:
    """Bot leaves a Matrix room + forgets it (no longer in joined-list). Thread-safe."""
    if not _bot_client or not _bot_loop:
        return False, "Matrix-Bot läuft nicht"
    cl = _bot_client

    async def _do_leave() -> tuple[bool, str]:
        try:
            resp = await cl.room_leave(room_id)
            from nio.responses import RoomLeaveError
            if isinstance(resp, RoomLeaveError):
                msg = getattr(resp, "message", None) or getattr(resp, "body", None) or str(resp)
                return False, str(msg)
        except Exception as e:
            return False, str(e)
        # Optional: forget room so server stops tracking it (frees state for re-invite later).
        try:
            await cl.room_forget(room_id)
        except Exception as e:
            logger.debug("room_forget after leave failed (non-fatal): %s", e)
        # Force-remove from client's local room dict — nio waits for next sync otherwise.
        try:
            rooms_dict = getattr(cl, "rooms", None)
            if isinstance(rooms_dict, dict):
                rooms_dict.pop(room_id, None)
            invited_dict = getattr(cl, "invited_rooms", None)
            if isinstance(invited_dict, dict):
                invited_dict.pop(room_id, None)
        except Exception as e:
            logger.debug("local room dict cleanup failed: %s", e)
        # Drop inviter cache entry
        _inviter_cache.pop(room_id, None)
        try:
            _persist_inviter_cache()
        except Exception:
            pass
        return True, "ok"

    try:
        future = asyncio.run_coroutine_threadsafe(_do_leave(), _bot_loop)
        return future.result(timeout=30)
    except Exception as e:
        return False, str(e)


def _download_mxc_bytes_sync(mxc_url: str) -> tuple[bytes, str] | None:
    """Modul-Level mxc-Download (für Profile-Avatare etc.). Returns (bytes, content_type) oder None.
    Keine E2EE-Decryption — Avatare sind immer plain."""
    cl = _bot_client
    loop = _bot_loop
    if not cl or not loop or not mxc_url:
        return None

    async def _do() -> tuple[bytes, str] | None:
        try:
            resp = await cl.download(mxc_url)
            if not (hasattr(resp, "body") and resp.body):
                return None
            ct = getattr(resp, "content_type", "") or ""
            if not ct or ct == "application/octet-stream":
                bs = resp.body
                if bs[:8] == b'\x89PNG\r\n\x1a\n':
                    ct = "image/png"
                elif bs[:2] == b'\xff\xd8':
                    ct = "image/jpeg"
                elif bs[:4] == b'GIF8':
                    ct = "image/gif"
                elif bs[:4] == b'RIFF' and bs[8:12] == b'WEBP':
                    ct = "image/webp"
                else:
                    ct = "image/png"
            return resp.body, ct
        except Exception as e:
            logger.warning("Matrix: mxc-download failed for %s: %s", mxc_url, e)
            return None

    try:
        fut = asyncio.run_coroutine_threadsafe(_do(), loop)
        return fut.result(timeout=20)
    except Exception as e:
        logger.warning("Matrix: mxc-download-sync failed for %s: %s", mxc_url, e)
        return None


def get_user_profile(user_id: str, save_dir: str, room_id: str | None = None) -> dict[str, Any]:
    """Holt display_name + avatar von einem Matrix-Nutzer. Avatar wird nach save_dir/<sanitized>.png geschrieben.
    Returns {display_name, avatar_path (host-Pfad), avatar_url (mxc://)}.
    room_id: wenn gesetzt, wird user_id gegen die Mitgliederliste des Raums geprüft —
    Profile von Nicht-Mitgliedern werden NICHT aufgelöst (kein globaler Lookup im Group-Mode)."""
    cl = _bot_client
    loop = _bot_loop
    if not cl or not loop:
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "matrix bot not running"}
    if not user_id or not user_id.startswith("@"):
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "invalid matrix user_id (expected @user:server)"}
    # Membership-Gate: nur Profile von Nutzern IN diesem Raum auflösen.
    if room_id:
        _room = (getattr(cl, "rooms", {}) or {}).get(room_id)
        _members = set((getattr(_room, "users", {}) or {}).keys()) if _room else set()
        if user_id not in _members:
            return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": "No row found: user is not in this room"}

    async def _do() -> dict[str, Any]:
        try:
            resp = await cl.get_profile(user_id)
        except Exception as e:
            return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": f"get_profile failed: {e}"}
        display = getattr(resp, "displayname", "") or ""
        mxc = getattr(resp, "avatar_url", "") or ""
        if not display and not mxc:
            err = getattr(resp, "message", "") or "no profile data"
            return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": err}
        return {"display_name": display, "mxc": mxc}

    try:
        fut = asyncio.run_coroutine_threadsafe(_do(), loop)
        meta = fut.result(timeout=20)
    except Exception as e:
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": f"profile fetch timeout: {e}"}
    if meta.get("error"):
        return {"display_name": "", "avatar_path": "", "avatar_url": "", "error": meta["error"]}
    out: dict[str, Any] = {"display_name": meta.get("display_name", ""), "avatar_path": "", "avatar_url": meta.get("mxc", "")}
    mxc = meta.get("mxc", "")
    if mxc and mxc.startswith("mxc://"):
        dl = _download_mxc_bytes_sync(mxc)
        if dl:
            data, ct = dl
            ext = ".png"
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
            elif "webp" in ct:
                ext = ".webp"
            elif "gif" in ct:
                ext = ".gif"
            import re as _re
            safe = _re.sub(r"[^a-zA-Z0-9_-]", "_", user_id.lstrip("@"))[:60]
            p = Path(save_dir) / f"{safe}{ext}"
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                out["avatar_path"] = str(p)
            except Exception as e:
                out["error"] = f"avatar save failed: {e}"
    return out


def fetch_recent_messages(room_id: str, limit: int = 20, skip_event_id: str | None = None) -> dict[str, Any]:
    """Fetcht die letzten `limit` Text/Notice-Nachrichten aus einem Raum.
    Returns dict: {messages: list, diagnostic: str | None, encrypted_count: int, total_scanned: int, room_encrypted: bool}
    Messages: älteste→neueste. Jede: {sender, display, body, ts (epoch ms), event_id}.
    Thread-safe; bei Fehler/leerem Raum kommt aussagekräftige Diagnose im 'diagnostic' Feld.
    """
    cl = _bot_client
    loop = _bot_loop
    if not cl or not loop:
        return {"messages": [], "diagnostic": "matrix bot not running", "encrypted_count": 0, "total_scanned": 0, "room_encrypted": False}
    if limit <= 0:
        return {"messages": [], "diagnostic": None, "encrypted_count": 0, "total_scanned": 0, "room_encrypted": False}
    limit = min(limit, 100)

    async def _do_fetch() -> dict[str, Any]:
        try:
            from nio import MessageDirection
        except Exception:
            return {"messages": [], "diagnostic": "nio import failed", "encrypted_count": 0, "total_scanned": 0, "room_encrypted": False}
        rooms = getattr(cl, "rooms", {}) or {}
        room = rooms.get(room_id)
        if not room:
            return {"messages": [], "diagnostic": f"room {room_id} not in client.rooms (not joined or not synced)", "encrypted_count": 0, "total_scanned": 0, "room_encrypted": False}
        users = getattr(room, "users", {}) or {}
        room_encrypted = bool(getattr(room, "encrypted", False))
        start_token = getattr(room, "prev_batch", None)
        if not start_token:
            # Fallback: latest sync token. nio setzt room.prev_batch nur wenn frische Timeline-Batches eintreffen.
            # Bei stillem Raum (keine Nachrichten seit Bot-Start) ist es None — sync-Token funktioniert auch für back-pagination.
            start_token = getattr(cl, "next_batch", None) or getattr(cl, "loaded_sync_token", None)
        if not start_token:
            return {"messages": [], "diagnostic": "no pagination token available (initial sync not complete)", "encrypted_count": 0, "total_scanned": 0, "room_encrypted": room_encrypted}
        collected: list[dict[str, Any]] = []
        encrypted_count = 0
        total_scanned = 0
        next_token = start_token
        # Bis zu 3 Pages (300 events) zurück um `limit` Text-Nachrichten zu finden
        for _ in range(3):
            try:
                resp = await cl.room_messages(room_id, start=next_token, direction=MessageDirection.back, limit=100)
            except Exception as e:
                logger.debug("fetch_recent_messages room_messages failed: %s", e)
                break
            chunk = getattr(resp, "chunk", None) or []
            for ev in chunk:
                total_scanned += 1
                src = getattr(ev, "source", None)
                if not isinstance(src, dict):
                    continue
                ev_type = src.get("type")
                # E2EE-Events ohne erfolgreiche Decryption tauchen als m.room.encrypted auf
                if ev_type == "m.room.encrypted":
                    encrypted_count += 1
                    continue
                if ev_type != "m.room.message":
                    continue
                ev_id = src.get("event_id")
                if skip_event_id and ev_id == skip_event_id:
                    continue
                content = src.get("content") or {}
                msgtype = content.get("msgtype") or ""
                # Nur Text/Notice; Bilder/Audio/Files separat behandeln (überspringen für jetzt)
                if msgtype not in ("m.text", "m.notice", "m.emote"):
                    continue
                sender = src.get("sender") or ""
                body = (content.get("body") or "").strip()
                ts = src.get("origin_server_ts") or 0
                # Display-Name aus room.users[uid]
                display = ""
                u = users.get(sender)
                if u is not None:
                    display = getattr(u, "display_name", "") or getattr(u, "name", "") or ""
                collected.append({
                    "sender": sender,
                    "display": display or sender.split(":")[0].lstrip("@"),
                    "body": body,
                    "ts": int(ts),
                    "event_id": ev_id or "",
                })
                if len(collected) >= limit:
                    break
            if len(collected) >= limit:
                break
            new_token = getattr(resp, "end", None)
            if not new_token or new_token == next_token:
                break
            next_token = new_token
        collected.reverse()  # oldest → newest
        diag = None
        if not collected:
            if encrypted_count > 0:
                diag = f"all {encrypted_count} scrollback events are encrypted and bot has no keys for them (joined room AFTER messages were sent — Matrix shares Megolm keys only with present-at-send members)"
            elif total_scanned == 0:
                diag = "scrollback empty (no events on server)"
            else:
                diag = f"scanned {total_scanned} events, none were plain text messages (likely state/joins/reactions only)"
        return {
            "messages": collected,
            "diagnostic": diag,
            "encrypted_count": encrypted_count,
            "total_scanned": total_scanned,
            "room_encrypted": room_encrypted,
        }

    try:
        future = asyncio.run_coroutine_threadsafe(_do_fetch(), loop)
        return future.result(timeout=20)
    except Exception as e:
        logger.warning("fetch_recent_messages failed for %s: %s", room_id, e)
        return {"messages": [], "diagnostic": f"exception: {e}", "encrypted_count": 0, "total_scanned": 0, "room_encrypted": False}


def search_chat_history(
    room_id: str,
    query: str,
    max_scan: int = 200,
    context_lines: int = 2,
) -> dict[str, Any]:
    """Sucht in der Raum-History nach `query` (case-insensitive substring; mit `/.../` regex).
    Scrollt bis zu max_scan Events zurück, returnt Hits mit ±context_lines Kontext.
    Returns {hits: [{ts, sender, display, body, ev_id, is_hit}], scanned: int, encrypted_skipped: int, query: str}.
    """
    cl = _bot_client
    loop = _bot_loop
    if not cl or not loop or not query:
        return {"hits": [], "scanned": 0, "encrypted_skipped": 0, "query": query, "diagnostic": "no client or empty query"}
    max_scan = max(10, min(int(max_scan), 500))
    context_lines = max(0, min(int(context_lines), 5))

    import re as _re_s
    is_regex = len(query) >= 3 and query.startswith("/") and query.endswith("/")
    pat = None
    if is_regex:
        try:
            pat = _re_s.compile(query[1:-1], _re_s.IGNORECASE)
        except _re_s.error:
            return {"hits": [], "scanned": 0, "encrypted_skipped": 0, "query": query, "diagnostic": "invalid regex"}
    q_lower = query.lower() if not is_regex else None

    async def _do_search() -> dict[str, Any]:
        try:
            from nio import MessageDirection
        except Exception:
            return {"hits": [], "scanned": 0, "encrypted_skipped": 0, "query": query, "diagnostic": "nio missing"}
        rooms = getattr(cl, "rooms", {}) or {}
        room = rooms.get(room_id)
        if not room:
            return {"hits": [], "scanned": 0, "encrypted_skipped": 0, "query": query, "diagnostic": f"room {room_id} not joined"}
        users = getattr(room, "users", {}) or {}
        start_token = getattr(room, "prev_batch", None) or getattr(cl, "next_batch", None)
        if not start_token:
            return {"hits": [], "scanned": 0, "encrypted_skipped": 0, "query": query, "diagnostic": "no pagination token"}

        all_messages: list[dict[str, Any]] = []
        encrypted_skipped = 0
        next_token = start_token
        # max_scan / 100 Pages, mindestens 1, höchstens 5
        max_pages = max(1, min((max_scan + 99) // 100, 5))
        for _ in range(max_pages):
            if len(all_messages) >= max_scan:
                break
            try:
                resp = await cl.room_messages(room_id, start=next_token, direction=MessageDirection.back, limit=100)
            except Exception as e:
                logger.debug("search_chat_history room_messages failed: %s", e)
                break
            chunk = getattr(resp, "chunk", None) or []
            for ev in chunk:
                if len(all_messages) >= max_scan:
                    break
                src = getattr(ev, "source", None)
                if not isinstance(src, dict):
                    continue
                ev_type = src.get("type")
                if ev_type == "m.room.encrypted":
                    encrypted_skipped += 1
                    continue
                if ev_type != "m.room.message":
                    continue
                content = src.get("content") or {}
                if (content.get("msgtype") or "") not in ("m.text", "m.notice", "m.emote"):
                    continue
                sender = src.get("sender") or ""
                body = (content.get("body") or "").strip()
                ts = src.get("origin_server_ts") or 0
                display = ""
                u = users.get(sender)
                if u is not None:
                    display = getattr(u, "display_name", "") or getattr(u, "name", "") or ""
                all_messages.append({
                    "sender": sender,
                    "display": display or sender.split(":")[0].lstrip("@"),
                    "body": body,
                    "ts": int(ts),
                    "event_id": src.get("event_id") or "",
                })
            new_token = getattr(resp, "end", None)
            if not new_token or new_token == next_token:
                break
            next_token = new_token

        all_messages.reverse()  # oldest → newest

        def _match(body: str) -> bool:
            if not body:
                return False
            if pat is not None:
                return bool(pat.search(body))
            return q_lower in body.lower()

        # Find hits + collect ±context_lines around each
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
        # Hard cap on output size — bot doesn't need 100 lines
        if len(hits) > 40:
            hits = hits[:40]
        return {
            "hits": hits,
            "match_count": len(hit_indices),
            "scanned": len(all_messages),
            "encrypted_skipped": encrypted_skipped,
            "query": query,
            "diagnostic": None,
        }

    try:
        future = asyncio.run_coroutine_threadsafe(_do_search(), loop)
        return future.result(timeout=30)
    except Exception as e:
        logger.warning("search_chat_history failed for %s: %s", room_id, e)
        return {"hits": [], "scanned": 0, "encrypted_skipped": 0, "query": query, "diagnostic": f"exception: {e}"}


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


def send_audio_to_room(room_id: str, wav_bytes: bytes) -> bool:
    """Thread-safe: Sendet Audio in einen bestimmten Raum über den laufenden Bot-Client."""
    if not _bot_client or not _bot_loop or not _bot_send_audio_fn:
        return False

    async def _do_send() -> bool:
        await _bot_send_audio_fn(_bot_client, room_id, wav_bytes)
        return True

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _bot_loop)
        return future.result(timeout=60)
    except Exception as e:
        logger.warning("Matrix send_audio_to_room fehlgeschlagen: %s", e)
        return False


def send_audio_to_user(target_user_id: str, wav_bytes: bytes) -> bool:
    """Thread-safe: Sendet Audio an einen User (sucht passenden Raum)."""
    if not _bot_client or not _bot_loop or not _bot_send_audio_fn:
        return False

    async def _do_send() -> bool:
        cl = _bot_client
        rooms = getattr(cl, "rooms", {}) or {}
        for rid, room in rooms.items():
            members = getattr(room, "users", {}) or {}
            if target_user_id in members:
                await _bot_send_audio_fn(cl, rid, wav_bytes)
                return True
        for rid, room in rooms.items():
            invited = getattr(room, "invited_users", {}) or {}
            if target_user_id in invited:
                await _bot_send_audio_fn(cl, rid, wav_bytes)
                return True
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), _bot_loop)
        return future.result(timeout=60)
    except Exception as e:
        logger.warning("Matrix send_audio_to_user fehlgeschlagen: %s", e)
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
        from nio.events import RoomMessageAudio
    except ImportError:
        RoomMessageAudio = None  # ältere matrix-nio
    try:
        from nio.events import RoomMessageFile
    except ImportError:
        RoomMessageFile = None  # ältere matrix-nio
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
    RoomMessageAudio = None  # type: ignore
    RoomMessageFile = None  # type: ignore
    MegolmEvent = None  # type: ignore


def _get_chat_response(
    config: dict[str, Any],
    matrix_user_id: str,
    user_message: str,
    sessions: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
    member_count: int = 0,
) -> str:
    """Synchroner Aufruf: Session per (room_id, matrix_user_id), handle_user_input.
    Gibt **ausschließlich** den sichtbaren Content zurück – KEIN Thinking für Matrix.

    Session-Key kombiniert Room und User: gleicher User in zwei Räumen → zwei separate Sessions.
    """
    # Pro Turn shallow-copy des Config-Dicts: verhindert Race auf config["_chat_context"]
    # zwischen parallelen Triggern (Alice in Raum A vs Bob in Raum B im selben Moment).
    # Top-level Keys werden unabhängig — nested dicts (providers, server, …) bleiben geteilt,
    # was ok ist weil zur Laufzeit nur top-level _-Keys mutiert werden.
    config = dict(config)
    from miniassistant.chat_loop import create_session, handle_user_input, is_chat_command
    from miniassistant.slot_cache import derive_conv_id as _sc_derive
    from miniassistant.group_rooms import (
        get_room_settings, build_group_chat_context, session_key as _sess_key,
        ensure_default_group_settings, get_auto_context_settings, format_auto_context,
    )
    if room_id and member_count > 2:
        ensure_default_group_settings(config, "matrix", room_id, is_group=True)
    rs = get_room_settings(config, "matrix", room_id)
    _sc_conv_id = _sc_derive("matrix", room_id=room_id, user_id=matrix_user_id) if room_id else None
    base_ctx: dict[str, Any] = {"platform": "matrix", "room_id": room_id, "user_id": matrix_user_id}
    # Display-Name des Senders aus room.users[uid] holen — damit Bot dich beim Namen ansprechen kann
    try:
        if _bot_client and room_id:
            _room_obj = (getattr(_bot_client, "rooms", {}) or {}).get(room_id)
            if _room_obj is not None:
                _u = (getattr(_room_obj, "users", {}) or {}).get(matrix_user_id)
                if _u is not None:
                    _dn = getattr(_u, "display_name", "") or getattr(_u, "name", "") or ""
                    if _dn:
                        base_ctx["user_display"] = _dn
    except Exception:
        pass
    if _sc_conv_id:
        base_ctx["conv_id"] = _sc_conv_id
        base_ctx["slot_cache_endpoint"] = "matrix"
    ctx = build_group_chat_context(base_ctx, rs) if room_id else base_ctx
    # Auto-Context im Group-Mode: letzte N Nachrichten vor user_message prependen.
    # NICHT bei Slash-Befehlen (/new, /help, …): das Prepend würde den ^-verankerten
    # Command-Parser in handle_user_input aushebeln → Befehl landet als Prompt beim LLM.
    if ctx.get("group_mode") and room_id and not is_chat_command(user_message):
        ac_count, ac_max = get_auto_context_settings(rs)
        if ac_count > 0:
            try:
                bot_user_id = getattr(_bot_client, "user_id", None) or getattr(_bot_client, "user", None) or ""
                _frm = fetch_recent_messages(room_id, limit=ac_count + 1)
                prev = _frm.get("messages") or [] if isinstance(_frm, dict) else (_frm or [])
                # Trigger-Nachricht (letzte vom aktuellen Sender mit gleichem Body) entfernen falls anwesend
                if prev and prev[-1].get("sender") == matrix_user_id and (prev[-1].get("body") or "").strip() == user_message.strip():
                    prev = prev[:-1]
                prev = prev[-ac_count:] if len(prev) > ac_count else prev
                blk = format_auto_context(prev, max_chars=ac_max, bot_sender=bot_user_id)
                if blk:
                    # Aktuelle Message explizit mit Sender-Marker wrappen — verhindert dass Bot
                    # den Request einem User aus dem Auto-Context zuschreibt (z.B. dem der zuerst @clawi mention'te).
                    _who_now = base_ctx.get("user_display") or matrix_user_id
                    user_message = blk + f"[Current message from {_who_now}]:\n" + user_message
            except Exception as _ac_err:
                logger.debug("Matrix auto-context fetch failed: %s", _ac_err)
    session_key = _sess_key(room_id, matrix_user_id, bool(ctx.get("group_mode")))
    # chat_context BEFORE create_session — wird in build_system_prompt + get_tools_schema gelesen
    if room_id:
        config["_chat_context"] = ctx
    # Group-Mode: stateless — jeder Turn frische Session (auto-context + read_recent_messages liefern Kontext).
    if ctx.get("group_mode") or session_key not in sessions:
        sessions[session_key] = create_session(config, None)
    session = sessions[session_key]
    if room_id:
        session["chat_context"] = ctx
    result = handle_user_input(session, user_message, allow_new_session=True, images=images)
    # Bei group_mode keine Session persistieren (bleibt stateless für nächsten Turn)
    if ctx.get("group_mode"):
        sessions.pop(session_key, None)
    else:
        sessions[session_key] = result[1]
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
    matrix_cfg = (config.get("chat_clients") or {}).get("matrix")
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

    # Sessions pro (room, matrix_user) — LRU mit Cap, sonst wachsen sie unbegrenzt
    from miniassistant.chat_loop import SessionLRU
    matrix_sessions: Any = SessionLRU(max_size=200)
    # Pending Images: User hat Bild ohne Text geschickt → nächste Textnachricht bekommt das Bild
    _pending_images: dict[str, list[dict[str, Any]]] = {}
    # Pending Documents: PDF/DOCX/Text-Anhang ohne Text → naechste Textnachricht bekommt das Dokument
    _pending_docs: dict[str, list[dict[str, Any]]] = {}
    # Group-Mode TTL für Pending-Attachments (Sekunden). Nach Ablauf ohne Trigger werden
    # in-RAM-Bilder/Docs verworfen damit wir keine ungenutzten Uploads horten.
    # DM-Pending bleibt unbegrenzt (1:1 Konversation, Bot wartet auf User).
    _PENDING_GROUP_TTL = 300  # 5 Minuten
    # Hard-Cap: max N Group-Pendings GLOBAL über alle Sender. Schutz gegen RAM-Flood
    # (z.B. Spam-Upload von vielen Bildern bevor TTL-Prune greift).
    _PENDING_GROUP_MAX = 10

    def _prune_stale_pending() -> None:
        """Entfernt abgelaufene Group-Pending-Attachments. Nur Einträge mit _group_pending=True
        und _pending_ts werden geprüft — DM-Pending unangetastet. Erzwingt zusätzlich globalen Cap."""
        import time as _t_prune
        now = _t_prune.time()
        for store in (_pending_images, _pending_docs):
            # TTL-Prune
            for sender in list(store.keys()):
                items = store.get(sender) or []
                kept = [
                    it for it in items
                    if not it.get("_group_pending") or (now - it.get("_pending_ts", now)) < _PENDING_GROUP_TTL
                ]
                dropped = len(items) - len(kept)
                if dropped > 0:
                    logger.info("Matrix: %d stale group-pending Attachment(s) von %s verworfen (>%ds)", dropped, sender, _PENDING_GROUP_TTL)
                if not kept:
                    store.pop(sender, None)
                elif len(kept) != len(items):
                    store[sender] = kept
            # Hard-Cap-Prune: globale Anzahl Group-Pendings begrenzen (FIFO: älteste raus)
            _all_group: list[tuple[float, str, dict[str, Any]]] = []
            for sender, items in store.items():
                for it in items:
                    if it.get("_group_pending"):
                        _all_group.append((it.get("_pending_ts") or 0, sender, it))
            if len(_all_group) > _PENDING_GROUP_MAX:
                _all_group.sort(key=lambda x: x[0])  # oldest first
                _evict = _all_group[: len(_all_group) - _PENDING_GROUP_MAX]
                for _, sender, it in _evict:
                    items = store.get(sender) or []
                    try:
                        items.remove(it)
                        if not items:
                            store.pop(sender, None)
                        else:
                            store[sender] = items
                    except ValueError:
                        pass
                logger.info("Matrix: %d group-pending Attachment(s) per Hard-Cap (>%d global) verdrängt", len(_evict), _PENDING_GROUP_MAX)
    # Busy-Tracking pro User (matrix_user_id). Verhindert parallele KI-Verarbeitung
    # desselben Users — auch in Gruppenräumen (Schlüssel = sender, nicht room_id).
    # Wird nur im asyncio-Loop-Thread gemutiert (in on_message vor/nach Executor),
    # daher kein Lock nötig.
    _busy_users: set[str] = set()

    async def _send_room_message(cl: Any, room_id: str, body: str) -> None:
        """Sendet eine Textnachricht im Raum. Mit mistune: m.text + org.matrix.custom.html (formatted_body).
        Defensive filter in Group-Mode: @-Mentions/matrix.to-Links zu Non-Room-Mitgliedern werden zu
        Plaintext degraded (verhindert Ping nach außen wenn Bot Regel ignoriert)."""
        try:
            from miniassistant.group_rooms import get_room_settings as _grs
            _rs_send = _grs(config, "matrix", room_id) if config else {}
            _is_grp_send = (_rs_send.get("context") or "").lower() == "group"
            if not _is_grp_send:
                _room_obj_s = (getattr(cl, "rooms", {}) or {}).get(room_id)
                _members_s = getattr(_room_obj_s, "users", {}) or {} if _room_obj_s else {}
                _is_grp_send = len(_members_s) > 2
            if _is_grp_send:
                _room_obj = (getattr(cl, "rooms", {}) or {}).get(room_id)
                _members = set((getattr(_room_obj, "users", {}) or {}).keys()) if _room_obj else set()
                import re as _re_send
                # Pattern 1: markdown link [text](https://matrix.to/#/@user:server)
                def _strip_ext_link(m: "_re_send.Match[str]") -> str:
                    text = m.group(1)
                    uid = m.group(2)
                    return text if uid not in _members else m.group(0)
                body = _re_send.sub(r"\[([^\]]+)\]\(https://matrix\.to/#/(@[^):]+:[^)]+)\)", _strip_ext_link, body)
                # Pattern 2: bare @user:server outside any markdown — neutralize @ to "at " for non-members
                def _strip_bare(m: "_re_send.Match[str]") -> str:
                    uid = "@" + m.group(1)
                    return uid if uid in _members else f"(external user: {m.group(1)})"
                body = _re_send.sub(r"(?<![\w/])@([a-zA-Z0-9._=\-/+]+:[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", _strip_bare, body)
        except Exception as _e_filt:
            logger.debug("Matrix outgoing ping-filter failed: %s", _e_filt)
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

    async def _send_room_audio(cl: Any, room_id: str, wav_bytes: bytes) -> None:
        """Lädt WAV hoch (media repo) und sendet es als m.audio im Raum."""
        import io as _io
        try:
            upload_resp = await cl.upload(
                _io.BytesIO(wav_bytes),
                content_type="audio/wav",
                filename="response.wav",
                filesize=len(wav_bytes),
            )
            mxc = getattr(upload_resp, "content_uri", None)
            if isinstance(upload_resp, tuple):
                mxc = mxc or getattr(upload_resp[0], "content_uri", None)
            if not mxc:
                logger.warning("Matrix: Audio-Upload fehlgeschlagen: %s", upload_resp)
                return
            audio_content: dict[str, Any] = {
                "msgtype": "m.audio",
                "body": "Sprachnachricht",
                "url": mxc,
                "info": {"mimetype": "audio/wav", "size": len(wav_bytes)},
            }
            try:
                await cl.room_send(room_id, "m.room.message", audio_content, ignore_unverified_devices=True)
            except TypeError:
                await cl.room_send(room_id, "m.room.message", audio_content)
            logger.info("Matrix: Audio gesendet in %s (%d bytes)", room_id, len(wav_bytes))
        except Exception as e:
            logger.exception("Matrix: Audio-Upload/Senden fehlgeschlagen: %s", e)

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

    async def _download_mxc_file(mxc_url: str, file_info: dict[str, Any] | None = None) -> bytes | None:
        """Generischer Datei-Download (PDF/DOCX/Text). Mit E2EE-Decrypt falls noetig.
        Im Gegensatz zu _download_mxc_image keine Bild-Header-Validierung."""
        try:
            resp = await client.download(mxc_url)
            if not (hasattr(resp, "body") and resp.body):
                logger.warning("Matrix: Datei-Download fehlgeschlagen fuer %s", mxc_url)
                return None
            data = resp.body
            if file_info and file_info.get("key") and file_info.get("iv"):
                try:
                    from nio.crypto.attachments import decrypt_attachment
                    key_str = file_info["key"].get("k", "")
                    iv_str = file_info.get("iv", "")
                    hash_str = (file_info.get("hashes") or {}).get("sha256", "")
                    data = decrypt_attachment(data, key_str, hash_str, iv_str)
                except ImportError:
                    logger.warning("Matrix: nio.crypto.attachments nicht verfuegbar – pip install matrix-nio[e2e]")
                    return None
                except Exception as e:
                    logger.warning("Matrix: Datei-Entschluesselung fehlgeschlagen: %s", e)
                    return None
            return data
        except Exception as e:
            logger.warning("Matrix: Datei-Download Fehler fuer %s: %s", mxc_url, e)
            return None

    async def on_file(room_id: str, event: Any) -> None:
        """m.file Events: Dokument (PDF/DOCX/Text) herunterladen, extrahieren, als Pending speichern."""
        sender = getattr(event, "sender", None) or ""
        if not sender or sender == user_id:
            return
        config_dir = config.get("_config_dir")
        if not is_authorized("matrix", sender, config_dir):
            # Room-Trust via Inviter (gleiche Logik wie in on_message): wenn Bot von einem
            # authed User eingeladen wurde, dürfen alle Raum-Mitglieder den Bot benutzen.
            _inv = await _fetch_inviter(client, room_id, user_id)
            if not (_inv and is_authorized("matrix", _inv, config_dir)):
                code = get_or_generate_code("matrix", sender, config_dir)
                await _send_room_message(client, room_id, f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\nGib in der Web-UI ein: `/auth matrix {code}`")
                return
        # URL und Datei-Info extrahieren (analog on_image)
        mxc_url = getattr(event, "url", None) or ""
        file_info: dict[str, Any] | None = None
        source = getattr(event, "source", None) or {}
        content = source.get("content") or {} if isinstance(source, dict) else {}
        if isinstance(content, dict) and content.get("file"):
            enc_file = content["file"]
            if not mxc_url:
                mxc_url = enc_file.get("url", "")
            file_info = enc_file
        if not file_info:
            ev_key = getattr(event, "key", None)
            ev_iv = getattr(event, "iv", None)
            if ev_key and ev_iv:
                file_info = {"key": ev_key, "iv": ev_iv, "hashes": getattr(event, "hashes", None) or {}}
        if not mxc_url:
            await _send_room_message(client, room_id, "Konnte Dateianhang nicht lesen (keine URL).")
            return
        # Filename + MIME aus Event
        info = content.get("info") or {} if isinstance(content, dict) else {}
        event_mime = (info.get("mimetype") if isinstance(info, dict) else "") or ""
        filename = (content.get("body") if isinstance(content, dict) else "") or getattr(event, "body", "") or "anhang"
        # Filter: nur unterstuetzte Typen verarbeiten
        from miniassistant.documents import is_supported as _doc_supported, extract_document as _doc_extract
        if not _doc_supported(event_mime, filename):
            await _send_room_message(client, room_id, f"Dateityp `{event_mime or filename}` wird nicht unterstuetzt (PDF, DOCX, Text).")
            return
        logger.info("Matrix: Dokument-Anhang von %s in %s: %s (mime: %s)", sender, room_id, filename, event_mime or "?")
        data = await _download_mxc_file(mxc_url, file_info=file_info)
        if not data:
            await _send_room_message(client, room_id, "Konnte das Dokument nicht herunterladen.")
            return
        max_chars = int(config.get("doc_max_chars") or 200000)
        max_pages = int(config.get("doc_max_pages_render") or 10)
        doc = _doc_extract(data, filename, event_mime, max_chars=max_chars, max_pages_render=max_pages)
        if doc.get("error"):
            await _send_room_message(client, room_id, f"Dokument konnte nicht gelesen werden: {doc['error']}")
            return
        # In Gruppenräumen: TTL-mark + silent
        try:
            from miniassistant.group_rooms import get_room_settings as _grs_doc
            _rs_doc = _grs_doc(config, "matrix", room_id)
            _room_obj_d = (getattr(client, "rooms", {}) or {}).get(room_id)
            _room_members_d = getattr(_room_obj_d, "users", {}) or {} if _room_obj_d else {}
            _is_group_doc = (len(_room_members_d) > 2) or ((_rs_doc.get("context") or "").lower() == "group")
        except Exception:
            _is_group_doc = False
        if _is_group_doc:
            import time as _t_p_d
            doc["_group_pending"] = True
            doc["_pending_ts"] = _t_p_d.time()
            _prune_stale_pending()
        _pending_docs.setdefault(sender, []).append(doc)
        n_chars = len(doc.get("text") or "")
        n_imgs = len(doc.get("images") or [])
        info_msg = f"Dokument empfangen ({n_chars} Zeichen"
        if n_imgs:
            info_msg += f", {n_imgs} Seiten als Bild"
        if doc.get("truncated"):
            info_msg += ", gekuerzt"
        info_msg += "). Was soll ich damit machen?"
        if not _is_group_doc:
            await _send_room_message(client, room_id, info_msg)
        else:
            logger.info("Matrix: Dokument von %s in Group-Raum %s pending — silent (TTL %ds)", sender, room_id, _PENDING_GROUP_TTL)

    async def on_image(room_id: str, event: Any) -> None:
        """Wird bei m.image Events aufgerufen – Bild herunterladen und als Pending speichern.
        Unterstützt verschlüsselte (E2EE) und unverschlüsselte Bilder."""
        sender = getattr(event, "sender", None) or ""
        if not sender or sender == user_id:
            return
        config_dir = config.get("_config_dir")
        if not is_authorized("matrix", sender, config_dir):
            # Room-Trust via Inviter (gleiche Logik wie in on_message)
            _inv = await _fetch_inviter(client, room_id, user_id)
            if not (_inv and is_authorized("matrix", _inv, config_dir)):
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
        # In Gruppenräumen (>2 Mitglieder ODER context=group): SILENT + TTL-mark.
        # Nur in DMs/Owner-Räumen: "Bild empfangen"-Prompt, weil dort 1:1 mit Bot kommuniziert wird.
        try:
            from miniassistant.group_rooms import get_room_settings as _grs_img
            _rs_img = _grs_img(config, "matrix", room_id)
            _room_obj = (getattr(client, "rooms", {}) or {}).get(room_id)
            _room_members = getattr(_room_obj, "users", {}) or {} if _room_obj else {}
            _is_group_img = (len(_room_members) > 2) or ((_rs_img.get("context") or "").lower() == "group")
        except Exception:
            _is_group_img = False
        if _is_group_img:
            import time as _t_p
            img_data["_group_pending"] = True
            img_data["_pending_ts"] = _t_p.time()
            _prune_stale_pending()  # opportunistisch alte raustun
        # Pending Image speichern – nächste Textnachricht bekommt es
        _pending_images.setdefault(sender, []).append(img_data)
        if not _is_group_img:
            await _send_room_message(client, room_id, "Bild empfangen. Was soll ich damit machen? (Schreib mir eine Nachricht dazu)")
        else:
            logger.info("Matrix: Bild von %s in Group-Raum %s pending — kein Confirm-Reply (silent mode, TTL %ds)", sender, room_id, _PENDING_GROUP_TTL)

    async def on_audio(room_id: str, event: Any) -> None:
        """Sprachnachricht empfangen: STT → Agent → TTS → Audio senden."""
        from miniassistant.config import get_voice_stt_url, get_voice_tts_url, get_voice_language, get_voice_tts_voice
        sender = getattr(event, "sender", None) or ""
        if not sender or sender == user_id:
            return
        config_dir = config.get("_config_dir")
        if not is_authorized("matrix", sender, config_dir):
            _inv = await _fetch_inviter(client, room_id, user_id)
            if not (_inv and is_authorized("matrix", _inv, config_dir)):
                code = get_or_generate_code("matrix", sender, config_dir)
                await _send_room_message(client, room_id, f"Nicht freigeschaltet. Auth-Code: **{code}**")
                return
        stt_url = get_voice_stt_url(config)
        if not stt_url:
            await _send_room_message(client, room_id, "Sprachfunktion nicht konfiguriert (voice.stt.url fehlt).")
            return
        # MXC-URL aus Event extrahieren
        mxc_url = getattr(event, "url", None) or ""
        source = getattr(event, "source", None) or {}
        content = source.get("content") or {} if isinstance(source, dict) else {}
        file_info = content.get("file") if isinstance(content, dict) else None
        if file_info and not mxc_url:
            mxc_url = (file_info or {}).get("url", "")
        if not mxc_url:
            await _send_room_message(client, room_id, "Konnte Audio-URL nicht lesen.")
            return
        # Typing-Indikator sofort starten (läuft durch Download + STT + LLM + TTS)
        room_typing = getattr(client, "room_typing", None)
        typing_task: asyncio.Task | None = None
        if callable(room_typing):
            async def _keep_typing_audio(_rid: str = room_id) -> None:
                try:
                    while True:
                        await room_typing(_rid, True)
                        await asyncio.sleep(15)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            typing_task = asyncio.create_task(_keep_typing_audio())

        async def _stop_typing() -> None:
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

        # Audio herunterladen
        try:
            resp = await client.download(mxc_url)
            if not (hasattr(resp, "body") and resp.body):
                raise RuntimeError("Kein Body in Download-Response")
            audio_bytes = resp.body
            # E2EE entschlüsseln falls nötig
            if file_info and file_info.get("key") and file_info.get("iv"):
                try:
                    from nio.crypto.attachments import decrypt_attachment
                    audio_bytes = decrypt_attachment(audio_bytes, file_info["key"].get("k", ""), (file_info.get("hashes") or {}).get("sha256", ""), file_info.get("iv", ""))
                except Exception as e:
                    logger.warning("Matrix Audio: Entschlüsselung fehlgeschlagen: %s", e)
        except Exception as e:
            logger.exception("Matrix Audio: Download fehlgeschlagen")
            await _stop_typing()
            await _send_room_message(client, room_id, f"Audio-Download fehlgeschlagen: {e}")
            return
        # STT
        try:
            from miniassistant import wyoming_client as _wyoming
            lang = get_voice_language(config)
            transcript = _wyoming.transcribe(audio_bytes, stt_url, language=lang)
        except Exception as e:
            logger.exception("Matrix Audio: STT fehlgeschlagen")
            await _stop_typing()
            await _send_room_message(client, room_id, f"Spracherkennung fehlgeschlagen: {e}")
            return
        if not transcript:
            await _stop_typing()
            await _send_room_message(client, room_id, "Konnte Sprachnachricht nicht erkennen.")
            return
        logger.info("Matrix Audio: Transkript von %s: %s", sender, transcript[:80])
        # Agent aufrufen (mit [Voice]-Prefix)
        _room_obj_v = (getattr(client, "rooms", {}) or {}).get(room_id)
        _mc_v = len(getattr(_room_obj_v, "users", {}) or {}) if _room_obj_v else 0
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda mc=_mc_v: _get_chat_response(config, sender, f"[Voice] {transcript}", matrix_sessions, room_id=room_id, member_count=mc),
        )
        if not response:
            await _stop_typing()
            return
        # Voice formatieren: visuellen Inhalt herausfiltern
        from miniassistant.voice_format import format_for_voice
        voice_text, visual_content = format_for_voice(response)
        # TTS
        tts_url = get_voice_tts_url(config)
        if tts_url and voice_text:
            try:
                tts_voice = get_voice_tts_voice(config)
                wav_bytes = _wyoming.synthesize(voice_text, tts_url, voice=tts_voice)
                # Audio hochladen und als m.audio senden
                import io as _io
                upload_resp = await client.upload(_io.BytesIO(wav_bytes), content_type="audio/wav", filename="response.wav", filesize=len(wav_bytes))
                mxc_audio = getattr(upload_resp, "content_uri", None) or (upload_resp[0].content_uri if isinstance(upload_resp, tuple) else None)
                if mxc_audio:
                    await _stop_typing()
                    await client.room_send(room_id, "m.room.message", {
                        "msgtype": "m.audio",
                        "url": mxc_audio,
                        "body": "Sprachantwort",
                        "info": {"mimetype": "audio/wav", "size": len(wav_bytes)},
                    })
                    from miniassistant import agent_actions_log as _aal
                    _aal.log_voice_sent(config, chars=len(voice_text), voice=tts_voice or "", bytes_sent=len(wav_bytes))
                else:
                    await _stop_typing()
                    await _send_room_message(client, room_id, voice_text)
            except Exception as e:
                logger.exception("Matrix Audio: TTS/Upload fehlgeschlagen")
                await _stop_typing()
                await _send_room_message(client, room_id, voice_text)
        else:
            await _stop_typing()
            await _send_room_message(client, room_id, voice_text)
        # Visuellen Inhalt (Tabellen, Code) als Text senden
        if visual_content:
            await _send_room_message(client, room_id, visual_content)

    async def on_message(room_id: str, event: Any) -> None:
        # Opportunistisches Prune abgelaufener Group-Pendings am Eingang jedes Events
        _prune_stale_pending()
        # msgtype früh auslesen — wird für Bild- und Audio-Erkennung benötigt
        _src_content: dict = {}
        _ev_source = getattr(event, "source", None) or {}
        if isinstance(_ev_source, dict):
            _src_content = _ev_source.get("content") or {}
        _msgtype: str = _src_content.get("msgtype") or ""

        # Audio-Events: RoomMessageAudio, msgtype=m.audio — VOR Bild-Fallback prüfen
        _is_audio = (RoomMessageAudio and isinstance(event, RoomMessageAudio)) or _msgtype == "m.audio"
        if _is_audio:
            logger.info("Matrix: Audio-Event erkannt, delegiere an on_audio")
            await on_audio(room_id, event)
            return

        # File-Events: RoomMessageFile, msgtype=m.file — VOR Bild-Fallback (sonst greift body+url Heuristik)
        _is_file = (RoomMessageFile and isinstance(event, RoomMessageFile)) or _msgtype == "m.file"
        if _is_file:
            logger.info("Matrix: File-Event erkannt, delegiere an on_file")
            await on_file(room_id, event)
            return

        # Bild-Events abfangen: RoomMessageImage, msgtype=m.image, oder body+url Heuristik
        _is_image = (RoomMessageImage and isinstance(event, RoomMessageImage)) or _msgtype == "m.image"
        if not _is_image:
            # Fallback: event.url oder event.source.content.file vorhanden → wahrscheinlich ein Bild
            # Nur wenn kein anderer msgtype bekannt (verhindert false-positives bei m.audio etc.)
            if not _msgtype:
                if getattr(event, "url", None):
                    _is_image = True
                elif isinstance(_src_content, dict) and _src_content.get("file"):
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
        # Antwort-Mode pro Raum: always / mention / off.
        # Default: always in DMs (2 Mitglieder), mention in Gruppen — kompatibel mit altem Verhalten.
        room_obj = (getattr(client, "rooms", {}) or {}).get(room_id)
        room_members = getattr(room_obj, "users", {}) or {} if room_obj else {}
        room_modes = ((config.get("chat_clients") or {}).get("matrix") or {}).get("room_modes") or {}
        mode = (room_modes.get(room_id) or "").strip().lower()
        if mode not in ("always", "mention", "off"):
            mode = "mention" if len(room_members) > 2 else "always"
        if mode == "off":
            logger.debug("Matrix: Raum %s mode=off — ignoriert", room_id)
            return
        if mode == "mention":
            import re as _re_men
            source = getattr(event, "source", None) or {}
            src_content = source.get("content") or {} if isinstance(source, dict) else {}
            mentions = (src_content.get("m.mentions") or {}) if isinstance(src_content, dict) else {}
            mentioned_ids = (mentions.get("user_ids") or []) if isinstance(mentions, dict) else []
            _rel = (src_content.get("m.relates_to") or {}) if isinstance(src_content, dict) else {}
            _in_reply = (_rel.get("m.in_reply_to") or {}) if isinstance(_rel, dict) else {}
            _reply_to_id = _in_reply.get("event_id") if isinstance(_in_reply, dict) else None
            _is_reply = bool(_reply_to_id)
            _bot_uid = (user_id or "").lower()

            # Mention-Erkennung in Schichten — robust über Client-Unterschiede hinweg, weil
            # Clients @-Mentions verschieden kodieren (Plaintext-@, Pill ohne @, nur Metadata):
            # 1) m.mentions-Metadata (MSC3952) — autoritativ, moderne Clients setzen es.
            is_mentioned = _bot_uid in [str(m).lower() for m in mentioned_ids]

            # 2) formatted_body-Pill: <a href=".../@bot:server"> — Clients die Pills statt
            #    Plaintext-@ senden (body enthält dann nur den Display-Namen ohne @).
            if not is_mentioned and isinstance(src_content, dict):
                _fb = (src_content.get("formatted_body") or "")
                if _bot_uid and _bot_uid in _fb.lower():
                    is_mentioned = True

            # 3) Reply/Quote auf eine Bot-Nachricht → triggern, auch ohne Namensnennung.
            #    Schnellpfad: Fallback-Zitat "> <@bot:server> …" parsen (kein Netzwerk);
            #    sonst Event autoritativ auflösen und Sender prüfen.
            if not is_mentioned and _is_reply:
                _q_sender = None
                _m = _re_men.match(r">\s*<(@[^>]+)>", body)
                if _m:
                    _q_sender = _m.group(1)
                else:
                    try:
                        _resp = await client.room_get_event(room_id, _reply_to_id)
                        _qe = getattr(_resp, "event", None)
                        _q_sender = getattr(_qe, "sender", None) if _qe else None
                    except Exception as _re_err:
                        logger.debug("Matrix: reply-to-bot lookup failed: %s", _re_err)
                if _q_sender and str(_q_sender).lower() == _bot_uid:
                    is_mentioned = True

            # 4) Text-Fallback: Nutzer tippt explizit "@clawi" (oder volle mxid) als Plaintext.
            #    @ ist PFLICHT — bloßes "clawi" im Gespräch (über den Bot reden) triggert NICHT.
            #    Reply-Fallback-Zeilen ("> <@user> …") vorher strippen, sonst triggert
            #    fremder Quote-Text den Bot fälschlich. Name kommt aus der Bot-mxid (user_id)
            #    + optional chat_clients.matrix.bot_name — NICHT hardcoded.
            if not is_mentioned:
                _mention_body = body
                if _is_reply and _mention_body.startswith(">"):
                    _ls = _mention_body.split("\n")
                    _i = 0
                    while _i < len(_ls) and _ls[_i].startswith(">"):
                        _i += 1
                    if _i < len(_ls) and _ls[_i].strip() == "":
                        _i += 1
                    if _i < len(_ls):
                        _mention_body = "\n".join(_ls[_i:])
                _body_l = _mention_body.lower()
                _localpart = _bot_uid.split(":", 1)[0].lstrip("@")  # "clawi"
                _bot_name = ((config.get("chat_clients") or {}).get("matrix") or {}).get("bot_name") or ""
                _names = {_localpart}
                if _bot_name.strip():
                    _names.add(_bot_name.strip().lower())
                is_mentioned = (
                    (_bot_uid and _bot_uid in _body_l)
                    or any(_re_men.search(r'(?:^|[^0-9a-z_])@' + _re_men.escape(_n) + r'(?:$|[^0-9a-z_])', _body_l)
                           for _n in _names if _n)
                )
            if not is_mentioned:
                logger.debug("Matrix: Raum %s mode=mention – kein @mention/Reply-auf-Bot, ignoriert", room_id)
                return

        # /stop, /abort, /abbruch: Cancellation-Befehle abfangen.
        # Robust: prüfe ob eines der Tokens IRGENDWO als whitespace-getrenntes Token vorkommt.
        # Auch ':abort' (statt '/abort') erkannt. Strip Bot-Mention(s) bzw. Display-Name passiert
        # implizit durch Token-Split — egal ob '@clawi /abort' oder 'clawi /abort' oder '/abort @clawi'.
        import re as _re_cancel
        _cancel_tokens = set(_re_cancel.split(r"\s+", body.strip().lower()))
        # Normalisiere ':xxx' → '/xxx'
        _cancel_tokens |= {("/" + t[1:]) for t in _cancel_tokens if t.startswith(":") and len(t) > 1}
        _cancel_hit = _cancel_tokens & {"/stop", "/abort", "/abbruch"}
        if _cancel_hit:
            _cancel_cmd = sorted(_cancel_hit)[0]  # deterministisch falls mehrere
            from miniassistant.cancellation import request_cancel
            level = "stop" if _cancel_cmd == "/stop" else "abort"
            # Gruppenräume (>2 Mitglieder ODER context=group): room-wide cancel
            # damit jeder Teilnehmer den laufenden Task abbrechen kann.
            from miniassistant.group_rooms import get_room_settings as _grs
            _rs = _grs(config, "matrix", room_id)
            _is_group = (len(room_members) > 2) or ((_rs.get("context") or "").lower() == "group")
            cancel_key = f"room:{room_id}" if _is_group else sender
            request_cancel(cancel_key, level)
            logger.info("Matrix: %s von %s — Cancellation angefordert (%s, key=%s)", body[:40], sender, level, cancel_key)
            reply = "⏹ Verarbeitung wird abgebrochen…" if level == "abort" else "⏸ Verarbeitung wird nach aktuellem Schritt gestoppt…"
            await _send_room_message(client, room_id, reply)
            return
        config_dir = config.get("_config_dir")
        sender_authed = is_authorized("matrix", sender, config_dir)
        # Room-Trust: Bot wurde von authed User eingeladen → ganzer Raum vertraut.
        room_trusted = False
        if not sender_authed:
            inviter = await _fetch_inviter(client, room_id, user_id)
            if inviter and is_authorized("matrix", inviter, config_dir):
                room_trusted = True
                logger.debug("Matrix: Raum %s trusted via Inviter %s — Zugriff für %s", room_id, inviter, sender)
        if not sender_authed and not room_trusted:
            code = get_or_generate_code("matrix", sender, config_dir)
            logger.info("Matrix: Auth-Code an %s gesendet (Code redacted)", sender)
            reply = (
                f"Du bist noch nicht freigeschaltet. Dein Auth-Code: **{code}**\n\n"
                f"Gib in der Web-UI ein: `/auth matrix {code}`"
            )
        elif sender in _busy_users:
            logger.info("Matrix: %s ist busy — neue Nachricht in %s verworfen", sender, room_id)
            reply = (
                "MiniAssistant ist noch beschäftigt — bitte versuche es gleich nochmal "
                "oder sende `/abort` bzw. `/abbruch` zum Abbrechen."
            )
        else:
            _busy_users.add(sender)
            try:
                # Pending Images + Documents abholen
                msg_images = _pending_images.pop(sender, None) or None
                msg_docs = _pending_docs.pop(sender, None) or None
                # Reply-to-Image: wenn User dieses Event als Reply auf ein älteres m.image
                # gesendet hat (Matrix-Quote-UI), das gequotete Bild auflösen + an msg_images anhängen
                # damit Bot sieht WAS gequotet wurde.
                try:
                    _src = getattr(event, "source", None) or {}
                    _ec = _src.get("content") or {} if isinstance(_src, dict) else {}
                    _rt = _ec.get("m.relates_to") or {}
                    _ir = _rt.get("m.in_reply_to") or {}
                    _quoted_id = _ir.get("event_id") if isinstance(_ir, dict) else None
                    if _quoted_id:
                        _resp = await client.room_get_event(room_id, _quoted_id)
                        _qe = getattr(_resp, "event", None)
                        if _qe is not None:
                            _qsrc = getattr(_qe, "source", None) or {}
                            _qc = _qsrc.get("content") or {} if isinstance(_qsrc, dict) else {}
                            _qmsgtype = _qc.get("msgtype") or ""
                            if _qmsgtype == "m.image":
                                _qmxc = _qc.get("url") or ""
                                _qfile = _qc.get("file") if isinstance(_qc.get("file"), dict) else None
                                if _qfile and not _qmxc:
                                    _qmxc = _qfile.get("url", "")
                                _qinfo = _qc.get("info") or {} if isinstance(_qc.get("info"), dict) else {}
                                _qmime = _qinfo.get("mimetype", "") if isinstance(_qinfo, dict) else ""
                                if _qmxc:
                                    _qimg = await _download_mxc_image(_qmxc, file_info=_qfile, event_mime=_qmime)
                                    if _qimg:
                                        if msg_images is None:
                                            msg_images = []
                                        msg_images.append(_qimg)
                                        logger.info("Matrix: reply-to-image dereferenziert (event=%s, mime=%s, %d bytes b64)",
                                                    _quoted_id, _qimg.get("mime_type", "?"), len(_qimg.get("data", "")))
                except Exception as _qerr:
                    logger.debug("Matrix: reply-to-image fetch failed for %s: %s", room_id, _qerr)
                # Dokument-PNGs (gescannte PDFs) zu Bilderliste hinzufuegen
                if msg_docs:
                    for _d in msg_docs:
                        if _d.get("images"):
                            if msg_images is None:
                                msg_images = []
                            msg_images.extend(_d["images"])
                    # Doc-Bloecke an body anhaengen (chat_loop strippt sie beim History-Save)
                    from miniassistant.documents import format_document_block as _fmt_doc
                    _doc_blocks = [_fmt_doc(d) for d in msg_docs]
                    _doc_text = "\n\n".join(b for b in _doc_blocks if b)
                    if _doc_text:
                        body = f"{_doc_text}\n\n{body}".strip()
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
                        lambda s=sender, b=body, imgs=msg_images, rid=room_id, mc=len(room_members): _get_chat_response(config, s, b, matrix_sessions, images=imgs, room_id=rid, member_count=mc),
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
            finally:
                _busy_users.discard(sender)
        if reply:
            from miniassistant.scheduler import _SILENT_SENTINELS
            if reply.strip() in _SILENT_SENTINELS:
                logger.info("Matrix: Antwort ist Silent-Sentinel (%s) — kein Send", reply.strip())
            else:
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
            logger.info("Matrix: Auth-Code an %s gesendet (Code redacted)", sender)
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

    # Persistierten Inviter-Cache laden (überlebt Bot-Restarts).
    # null-Einträge werden NICHT geladen — so retry'd lazy lookup nach jedem Restart.
    _load_inviter_cache()
    for _rid in [k for k, v in list(_inviter_cache.items()) if v is None]:
        _inviter_cache.pop(_rid, None)

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
        _message_types = (RoomMessageText,) + ((RoomMessageNotice,) if RoomMessageNotice else ()) + ((RoomMessageImage,) if RoomMessageImage else ()) + ((RoomMessageFile,) if RoomMessageFile else ())
        client.add_event_callback(_on_room_message, _message_types)
        logger.info("Matrix: Callback für %d Message-Typen registriert", len(_message_types))
    if MegolmEvent:
        client.add_event_callback(_on_encrypted, MegolmEvent)

    logger.info(
        "Matrix-Bot gestartet (user_id=%s, device_id=%s, encrypted_rooms=%s)",
        user_id, device_id, encrypted_rooms,
    )

    # Globale Referenzen fuer Notify setzen (Scheduler kann ueber Bot senden)
    global _bot_client, _bot_loop, _bot_send_fn, _bot_send_image_fn, _bot_send_audio_fn
    _bot_client = client
    _bot_loop = asyncio.get_running_loop()
    _bot_send_fn = _send_room_message
    _bot_send_image_fn = _send_room_image
    _bot_send_audio_fn = _send_room_audio

    _rooms = getattr(client, "rooms", {}) or {}
    if _rooms:
        logger.info("Matrix: %s Raum/Räume bekannt (nach erstem Sync kommen Timeline-Nachrichten)", len(_rooms))

    # Prefetch inviter info for all joined rooms — populates _inviter_cache so the /rooms UI is fast.
    async def _prefetch_inviters():
        for _rid in list((getattr(client, "rooms", {}) or {}).keys()):
            try:
                await _fetch_inviter(client, _rid, user_id)
            except Exception:
                pass
    asyncio.create_task(_prefetch_inviters())
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
                    # Inviter aus invite_state (best-effort — Synapse liefert oft 0 events,
                    # daher post-join fetch unten als primärer Pfad).
                    try:
                        invite_room = invited.get(room_id)
                        invite_state = getattr(invite_room, "invite_state", None) or []
                        for ev in invite_state:
                            ev_type = getattr(ev, "type", None)
                            state_key = getattr(ev, "state_key", None)
                            sender = getattr(ev, "sender", None)
                            content = getattr(ev, "content", None) or {}
                            membership = content.get("membership") if isinstance(content, dict) else None
                            if ev_type == "m.room.member" and state_key == user_id and membership == "invite" and sender and sender != user_id:
                                _inviter_cache[room_id] = sender
                                _persist_inviter_cache()
                                logger.info("Matrix: Inviter für %s = %s (via invite_state)", room_id, sender)
                                break
                    except Exception as e:
                        logger.debug("inviter capture (invite_state) failed for %s: %s", room_id, e)
                    try:
                        resp = await client.join(room_id)
                        if resp and getattr(resp, "room_id", None):
                            logger.info("Matrix: Raum beigetreten: %s", room_id)
                            # Falls invite_state-capture fehlschlug → jetzt via state event holen
                            if room_id not in _inviter_cache or _inviter_cache.get(room_id) is None:
                                _inviter_cache.pop(room_id, None)  # clear null so fetcher re-tries
                                inv = await _fetch_inviter(client, room_id, user_id)
                                if inv:
                                    logger.info("Matrix: post-join inviter capture für %s = %s", room_id, inv)
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
