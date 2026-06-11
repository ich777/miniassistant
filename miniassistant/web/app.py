"""
Minimalistisches Web-UI und API für MiniAssistant.
- Config anzeigen/bearbeiten (Ollama-URL, Modelle, num_ctx, Bind, Token)
- Chat (mit /model MODELLNAME), exec und web_search als Tools
- Token-Auth: Header Authorization: Bearer <token> oder ?token=...
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_log = logging.getLogger("uvicorn.error")

# Dedizierter Threadpool für Chat-Requests (verhindert Blockieren bei Modell-Pulls)
# Standard Executor hat nur ~5 Threads, wir brauchen mehr für parallele Requests
_chat_executor: ThreadPoolExecutor | None = None
_CHAT_EXECUTOR_MAX_WORKERS = 20

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse

from fastapi.staticfiles import StaticFiles

from miniassistant.config import load_config, save_config, ensure_token, config_path, load_config_raw, write_config_raw, validate_config_raw
from miniassistant.chat_loop import create_session, handle_user_input, run_onboarding_round, chat_round_stream, is_chat_command
from miniassistant.ollama_client import resolve_model

# Projekt-Root für Templates (ein Verzeichnis über miniassistant/)
ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

def _is_path_within(target: Path, base: Path) -> bool:
    """Sichere Pfad-Prüfung: True wenn target innerhalb von base liegt.
    Verwendet resolve() + is_relative_to() statt unsicherem startswith()."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


# In-Memory-Sessions (session_id -> session dict)
_sessions: dict[str, dict] = {}
_session_last_access: dict[str, float] = {}  # session_id -> timestamp (letzter Zugriff)
_session_locks: dict[str, threading.Lock] = {}  # session_id -> Lock (verhindert gleichzeitige Modifikation)
_session_meta_lock = threading.Lock()  # Schützt Zugriff auf _session_locks dict
_SESSION_TTL = 86400.0  # 24 Stunden
_config_save_lock = threading.Lock()  # Verhindert gleichzeitige Config-Speichervorgänge
_onboarding_sessions: dict[str, dict] = {}  # session_id -> { "messages": [...] }

# ---------------------------------------------------------------------------
# Client-seitige Tool-Ausführung (Round-Trip-Protokoll)
# ---------------------------------------------------------------------------
_pending_tool_requests: dict[str, dict] = {}  # tool_id -> {"event": Event, "result": str|None, "session_id": str}
_tool_requests_lock = threading.Lock()
_TOOL_REQUEST_TIMEOUT = 60.0  # Sekunden bis Fallback auf serverseitige Ausführung
_TOOL_RESULT_MAX_BYTES = 100_000  # Max. Größe eines Tool-Results (100 KB)


def _make_tool_hook(session_id: str) -> "Callable[[str, str, dict], str | None]":
    """Gibt einen Hook zurück, der den Generator blockiert bis der Client antwortet."""
    def hook(req_id: str, tool_name: str, args: dict) -> "str | None":
        ev = threading.Event()
        with _tool_requests_lock:
            _pending_tool_requests[req_id] = {"event": ev, "result": None, "session_id": session_id}
        # Warte auf Client-Antwort (POST /api/chat/tool_result)
        got = ev.wait(timeout=_TOOL_REQUEST_TIMEOUT)
        with _tool_requests_lock:
            entry = _pending_tool_requests.pop(req_id, {})
        if not got:
            _log.warning("tool_request %s (%s) Timeout nach %.0fs — Fallback serverseitig", req_id, tool_name, _TOOL_REQUEST_TIMEOUT)
            return None
        return entry.get("result") or ""
    return hook  # type: ignore[return-value]


from typing import Callable  # noqa: E402 (nach den anderen Importen)


def _get_session_lock(session_id: str) -> threading.Lock:
    """Gibt den Lock für eine Session zurück, erstellt ihn bei Bedarf."""
    with _session_meta_lock:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Rate-Limiting (sliding window, per IP, kein externes Paket)
# ---------------------------------------------------------------------------
_rate_buckets: dict[str, list[float]] = {}   # ip -> liste von Request-Timestamps
_rate_lock = threading.Lock()
_rate_config_cache: tuple[float, int, int] = (0.0, 100, 100)  # (cached_at, server_limit, raw_proxy_limit)
_trust_forwarded_cache: tuple[float, bool] = (0.0, False)  # (cached_at, value)
_RATE_WINDOW = 60.0  # Sekunden


def _get_trust_forwarded() -> bool:
    """Cached server.trust_forwarded — vermeidet load_config()-YAML-Parse pro Request."""
    global _trust_forwarded_cache
    now = time.monotonic()
    cached_at, val = _trust_forwarded_cache
    if now - cached_at < 30.0:
        return val
    try:
        val = bool((load_config().get("server") or {}).get("trust_forwarded"))
    except Exception:
        val = False
    _trust_forwarded_cache = (now, val)
    return val

# Auth Brute-Force Protection: separate Buckets für fehlgeschlagene Auth-Versuche (401)
_auth_fail_buckets: dict[str, list[float]] = {}
_AUTH_FAIL_WINDOW = 3600.0  # 1 Stunde
_AUTH_FAIL_MAX = 20          # max fehlgeschlagene Versuche → danach 1h Ban


def _check_auth_rate_limit(ip: str) -> bool:
    """True = erlaubt, False = zu viele fehlgeschlagene Auth-Versuche."""
    now = time.time()
    with _rate_lock:
        attempts = _auth_fail_buckets.get(ip, [])
        attempts = [t for t in attempts if now - t < _AUTH_FAIL_WINDOW]
        _auth_fail_buckets[ip] = attempts
        return len(attempts) < _AUTH_FAIL_MAX


def _record_auth_failure(ip: str) -> None:
    """Fehlgeschlagenen Auth-Versuch aufzeichnen."""
    now = time.time()
    with _rate_lock:
        attempts = _auth_fail_buckets.get(ip, [])
        attempts = [t for t in attempts if now - t < _AUTH_FAIL_WINDOW]
        attempts.append(now)
        _auth_fail_buckets[ip] = attempts


def _get_rate_limit(path: str = "/") -> int:
    """Liest rate_limit aus der Config; cached 30 Sekunden. Unterscheidet zwischen /v1/ und /raw/v1/."""
    global _rate_config_cache
    now = time.monotonic()
    cached_at, server_limit, raw_limit = _rate_config_cache
    if now - cached_at < 30.0:
        return raw_limit if path.startswith("/raw/v1") else server_limit
    try:
        cfg = load_config()
        server_limit = int((cfg.get("server") or {}).get("rate_limit", 100) or 100)
        raw_limit = int((cfg.get("raw_proxy") or {}).get("rate_limit", 100) or 100)
    except Exception:
        server_limit = 100
        raw_limit = 100
    _rate_config_cache = (now, server_limit, raw_limit)
    return raw_limit if path.startswith("/raw/v1") else server_limit


def _check_rate_limit(ip: str, path: str = "/") -> bool:
    """True = Anfrage erlaubt, False = Rate-Limit überschritten."""
    limit = _get_rate_limit(path)
    if limit <= 0:
        return True  # 0 = deaktiviert
    now = time.time()
    with _rate_lock:
        timestamps = _rate_buckets.get(ip, [])
        # Einträge außerhalb des Fensters entfernen
        timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
        if len(timestamps) >= limit:
            _rate_buckets[ip] = timestamps
            return False
        timestamps.append(now)
        _rate_buckets[ip] = timestamps
    return True

app = FastAPI(title="MiniAssistant", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# OpenAI-kompatible API (/v1/models, /v1/chat/completions)
from miniassistant.web.openai_compat import router as _openai_router
app.include_router(_openai_router)

# Raw OpenAI Proxy (/raw/v1/models, /raw/v1/chat/completions)
from miniassistant.web.raw_proxy import router as _raw_proxy_router
app.include_router(_raw_proxy_router)


@app.get("/favicon.ico", include_in_schema=False)
async def _favicon():
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    return JSONResponse(status_code=404, content={"detail": "not found"})


_matrix_bot_task: asyncio.Task | None = None
_discord_bot_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    """Bei server.debug: Startup in debug/serve.log schreiben. Chat-Bots starten falls konfiguriert."""
    global _matrix_bot_task, _discord_bot_task, _session_cleanup_task, _chat_executor, _slot_cache_cleanup_task
    _session_cleanup_task = asyncio.create_task(_cleanup_expired_sessions())
    _slot_cache_cleanup_task = asyncio.create_task(_slot_cache_cleanup_loop())
    _chat_executor = ThreadPoolExecutor(max_workers=_CHAT_EXECUTOR_MAX_WORKERS, thread_name_prefix="chat")
    # Webhook output retention sweep (config-gated)
    try:
        from miniassistant import webhooks as _wh_mod
        if _wh_mod.is_enabled():
            _wh_mod.sweep_all_outputs()
    except Exception:
        pass
    try:
        project_dir = getattr(app.state, "project_dir", None)
        config = load_config(project_dir)
        if (config.get("server") or {}).get("debug"):
            from miniassistant.debug_log import log_serve
            log_serve("Application startup", config)
        cc = config.get("chat_clients") or {}
        # Matrix-Bot
        mc = cc.get("matrix")
        if mc:
            if mc.get("enabled", True) and mc.get("token") and mc.get("user_id"):
                try:
                    from miniassistant.matrix_bot import run_matrix_bot
                    _matrix_bot_task = asyncio.create_task(run_matrix_bot(config))
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("Matrix-Bot konnte nicht gestartet werden: %s", e)
            else:
                import logging
                if not mc.get("enabled", True):
                    logging.getLogger(__name__).info("Matrix-Bot nicht gestartet: matrix.enabled ist false.")
                elif not mc.get("token") or not mc.get("user_id"):
                    logging.getLogger(__name__).info("Matrix-Bot nicht gestartet: matrix.token oder matrix.user_id fehlt.")
        # Discord-Bot
        dc = cc.get("discord")
        if dc:
            if dc.get("enabled", True) and dc.get("bot_token"):
                try:
                    from miniassistant.discord_bot import run_discord_bot
                    _discord_bot_task = asyncio.create_task(run_discord_bot(config))
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("Discord-Bot konnte nicht gestartet werden: %s", e)
    except Exception:
        pass


_session_cleanup_task: asyncio.Task | None = None


async def _cleanup_expired_sessions() -> None:
    """Entfernt Sessions die länger als _SESSION_TTL nicht genutzt wurden (läuft alle 10 Minuten)."""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        expired = [sid for sid, ts in _session_last_access.items() if now - ts > _SESSION_TTL]
        for sid in expired:
            _sessions.pop(sid, None)
            _session_last_access.pop(sid, None)
            _onboarding_sessions.pop(sid, None)
            with _session_meta_lock:
                _session_locks.pop(sid, None)
        if expired:
            _log.info("Session-Cleanup: %d abgelaufene Session(s) entfernt", len(expired))


_slot_cache_cleanup_task: asyncio.Task | None = None


async def _slot_cache_cleanup_loop() -> None:
    """Stündlich: TTL+LRU + unknown-models Sweep für Slot-Cache.
    No-op wenn slot_cache.enabled=false."""
    while True:
        await asyncio.sleep(3600)
        try:
            from miniassistant.config import load_config
            from miniassistant import slot_cache
            cfg = load_config()
            if not slot_cache.is_globally_enabled(cfg):
                continue
            removed_lru = slot_cache.cleanup_lru_and_ttl(cfg)
            removed_unk = slot_cache.cleanup_unknown_models(cfg)
            if removed_lru or removed_unk:
                _log.info("Slot-Cache-Cleanup: %d (TTL+LRU), %d (unknown models)", removed_lru, removed_unk)
        except Exception as e:
            _log.warning("Slot-Cache-Cleanup error: %s", e)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Chat-Executor cleanup und Bot-Tasks abbrechen."""
    global _chat_executor, _matrix_bot_task, _discord_bot_task, _session_cleanup_task, _slot_cache_cleanup_task
    if _chat_executor:
        _chat_executor.shutdown(wait=False)
    if _slot_cache_cleanup_task:
        _slot_cache_cleanup_task.cancel()
        _chat_executor = None
    if _session_cleanup_task and not _session_cleanup_task.done():
        _session_cleanup_task.cancel()
    for task in (_matrix_bot_task, _discord_bot_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _matrix_bot_task = None
    _discord_bot_task = None


# Body-size limits per route prefix (bytes). First match wins; default applies otherwise.
# Large endpoints accept base64-encoded images/audio/documents — generous cap.
# Config/API endpoints accept small payloads — tight cap.
_BODY_LIMIT_DEFAULT = 1 * 1024 * 1024          # 1 MB
_BODY_LIMIT_LARGE = 35 * 1024 * 1024            # 35 MB (chat with big images, long voice messages, etc.)
_BODY_LIMIT_BY_PREFIX: list[tuple[str, int]] = [
    ("/api/chat/stream", _BODY_LIMIT_LARGE),
    ("/api/chat", _BODY_LIMIT_LARGE),
    ("/api/onboarding", _BODY_LIMIT_LARGE),
    ("/v1/chat/completions", _BODY_LIMIT_LARGE),
    ("/v1/completions", _BODY_LIMIT_LARGE),
    ("/raw/v1/chat/completions", _BODY_LIMIT_LARGE),
    ("/raw/v1/completions", _BODY_LIMIT_LARGE),
    ("/webhook/", _BODY_LIMIT_LARGE),
]


def _body_limit_for_path(path: str) -> int:
    for prefix, limit in _BODY_LIMIT_BY_PREFIX:
        if path.startswith(prefix):
            return limit
    return _BODY_LIMIT_DEFAULT


@app.middleware("http")
async def _body_size_limit_middleware(request: Request, call_next):
    """Enforce per-path body-size cap via Content-Length header.
    Chunked uploads without Content-Length: enforced post-hoc when first read."""
    method = request.method.upper()
    if method in ("GET", "HEAD", "DELETE", "OPTIONS"):
        return await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)
    limit = _body_limit_for_path(path)
    cl_raw = request.headers.get("content-length")
    if cl_raw:
        try:
            cl = int(cl_raw)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
        if cl > limit:
            return JSONResponse(
                status_code=413,
                content={"detail": f"request body too large ({cl} bytes; limit {limit})"},
            )
    return await call_next(request)


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    """Sliding-Window Rate-Limiter pro IP + Auth-Brute-Force-Schutz (401-Tracking)."""
    # Statische Dateien und Favicon ausnehmen
    path = request.url.path
    if path.startswith("/static/") or path == "/favicon.ico":
        return await call_next(request)
    # x-forwarded-for / x-real-ip nur trusten wenn server.trust_forwarded=true (Reverse-Proxy-Setup).
    # Sonst kann jeder Client den Header spoofen und Rate-Limit/Brute-Force-Schutz umgehen.
    if _get_trust_forwarded():
        ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "").strip()
            or (request.client.host if request.client else None)
            or "unknown"
        )
    else:
        ip = (request.client.host if request.client else None) or "unknown"
    if not _check_rate_limit(ip, path):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded – too many requests. Please slow down."},
            headers={"Retry-After": "60"},
        )
    # Auth-Brute-Force: zu viele fehlgeschlagene Versuche → 1h Ban
    if not _check_auth_rate_limit(ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed authentication attempts. Banned for 1 hour."},
            headers={"Retry-After": "3600"},
        )
    response = await call_next(request)
    # 401-Responses tracken für Brute-Force-Schutz
    if response.status_code == 401:
        _record_auth_failure(ip)
    return response


@app.middleware("http")
async def _debug_log_requests(request: Request, call_next):
    """Bei server.debug: Jeden Request in debug/serve.log schreiben."""
    response = await call_next(request)
    try:
        config = load_config()
        if (config.get("server") or {}).get("debug"):
            from miniassistant.debug_log import log_serve
            log_serve(f"{request.method} {request.url.path}", config)
    except Exception:
        pass
    return response


def _get_token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:].strip()
    q = request.query_params.get("token")
    if q:
        return q
    return request.cookies.get("ma_token")


def _require_token(request: Request) -> str:
    import secrets as _secrets
    config = load_config()
    expected = (config.get("server") or {}).get("token")
    if not expected:
        return ""  # no token configured yet -> allow (e.g. first run)
    token = _get_token_from_request(request)
    if token and _secrets.compare_digest(token, expected):
        return token
    raise HTTPException(status_code=401, detail="Invalid or missing token")


_TOKEN_COOKIE_PAGES = {"/", "/chat", "/chats", "/config", "/config/raw", "/schedules", "/webhooks", "/rooms", "/onboarding", "/logs", "/nutzung", "/workspace", "/agent"}


@app.middleware("http")
async def _token_cookie_middleware(request: Request, call_next):
    """Wenn Token als URL-Param kommt und Seite ein HTML-Page ist: Cookie setzen, redirect ohne Token in URL.
    Cookie wird nur gesetzt wenn der URL-Token tatsächlich gegen die Server-Konfiguration validiert."""
    if request.method == "GET" and request.url.path in _TOKEN_COOKIE_PAGES:
        url_token = request.query_params.get("token", "").strip()
        cookie_token = request.cookies.get("ma_token", "").strip()
        if url_token and url_token != cookie_token:
            import secrets as _secrets
            try:
                expected = (load_config().get("server") or {}).get("token") or ""
            except Exception:
                expected = ""
            # Nur bei gültigem Token Cookie setzen; sonst durchlassen damit /chat etc. einen sauberen 401 wirft.
            if expected and _secrets.compare_digest(url_token, expected):
                from starlette.responses import RedirectResponse as _Redirect
                clean_url = str(request.url).split("?")[0]
                from urllib.parse import urlencode
                params = dict(request.query_params)
                params.pop("token", None)
                if params:
                    clean_url += "?" + urlencode(params)
                resp = _Redirect(url=clean_url, status_code=302)
                is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
                resp.set_cookie("ma_token", url_token, httponly=True, samesite="strict", secure=is_https, max_age=365 * 24 * 3600)
                return resp
    response = await call_next(request)
    return response


def _token_query(token: str | None) -> str:
    """Query-String mit Token für URLs (z. B. ?token=xyz)."""
    if not token or not token.strip():
        return ""
    return "?token=" + _url_escape(token.strip())


def _onboarding_complete() -> bool:
    """True nur wenn in der Config onboarding_complete gesetzt ist (nach Speichern in UI oder CLI config). Kein Datei-Check – vermeidet Race mit erstem serve."""
    cfg = load_config()
    return bool(cfg.get("onboarding_complete", False))


def _url_escape(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


_COMMON_CSS = """
:root {
  --bg: #f5f6fa; --card: #fff; --text: #2d3436; --muted: #636e72;
  --primary: #0984e3; --primary-hover: #0770c4; --border: #dfe6e9;
  --success: #00b894; --warning: #fdcb6e; --danger: #d63031;
  --radius: 8px; --shadow: 0 2px 8px rgba(0,0,0,0.08);
  --input-bg: #fff;
}
[data-theme="dark"] {
  --bg: #1a1a2e; --card: #16213e; --text: #e0e0e0; --muted: #a0a0a0;
  --primary: #4dabf7; --primary-hover: #339af0; --border: #2a2a4a;
  --success: #51cf66; --warning: #fcc419; --danger: #ff6b6b;
  --shadow: 0 2px 8px rgba(0,0,0,0.3);
  --input-bg: #1a1a2e;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1a1a2e; --card: #16213e; --text: #e0e0e0; --muted: #a0a0a0;
    --primary: #4dabf7; --primary-hover: #339af0; --border: #2a2a4a;
    --success: #51cf66; --warning: #fcc419; --danger: #ff6b6b;
    --shadow: 0 2px 8px rgba(0,0,0,0.3);
    --input-bg: #1a1a2e;
  }
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: var(--bg); color: var(--text); margin: 0; }
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 680px; margin: 0 auto; padding: 1.5em; }
.card { background: var(--card); border-radius: var(--radius); box-shadow: var(--shadow); padding: 1.5em; margin-bottom: 1em; }
.btn { display: inline-block; padding: 0.5em 1.2em; border: none; border-radius: var(--radius);
       font-size: 0.95em; cursor: pointer; transition: background 0.15s, transform 0.1s; color: var(--text); }
.btn:active { transform: scale(0.97); }
.btn-primary { background: var(--primary); color: #fff; }
.btn-primary:hover { background: var(--primary-hover); color: #fff; text-decoration: none; }
.btn-outline { background: transparent; border: 1.5px solid var(--primary); color: var(--primary); }
.btn-outline:hover { background: var(--primary); color: #fff; text-decoration: none; }
.setup-hint { background: #fff9e6; padding: 0.8em 1em; border-radius: var(--radius); border-left: 4px solid var(--warning); margin-bottom: 1em; }
[data-theme="dark"] .setup-hint { background: #2a2a1e; }
.logo-row { display: flex; align-items: center; gap: 0.7em; margin-bottom: 0.5em; }
.logo-row img { width: 48px; height: 48px; border-radius: 10px; }
.logo-row h1 { margin: 0; font-size: 1.6em; }
input[type="password"], input[type="text"], textarea, select {
  padding: 0.5em 0.7em; border: 1.5px solid var(--border); border-radius: var(--radius);
  font-size: 0.95em; outline: none; transition: border-color 0.15s;
  background: var(--input-bg); color: var(--text); }
input:focus, textarea:focus, select:focus { border-color: var(--primary); }
.token-row { display: flex; align-items: center; gap: 0.4em; flex-wrap: wrap; }
.token-row input { flex: 1; min-width: 12em; }
.eye-btn { background: var(--card); border: 1.5px solid var(--border); border-radius: var(--radius);
           cursor: pointer; padding: 0.4em 0.55em; font-size: 1.1em; line-height: 1; color: var(--text); }
.eye-btn:hover { background: var(--border); }
.nav-links { list-style: none; padding: 0; margin: 0.8em 0; }
.nav-links li { margin: 0.4em 0; }
.nav-links li a { display: inline-flex; align-items: center; gap: 0.3em; }
.text-muted { color: var(--muted); font-size: 0.85em; }
.theme-toggle { position: fixed; top: 0.6em; right: 0.8em; background: var(--card); border: 1.5px solid var(--border);
  border-radius: var(--radius); cursor: pointer; padding: 0.35em 0.5em; font-size: 1.1em; line-height: 1;
  z-index: 999; color: var(--text); box-shadow: var(--shadow); }
.theme-toggle:hover { background: var(--border); }
"""

_THEME_JS = """
<button class="theme-toggle" id="theme-toggle" title="Dark/Light Mode">&#127769;</button>
<script>
(function(){
  var root=document.documentElement, btn=document.getElementById("theme-toggle");
  function getCookie(n){var m=document.cookie.match(new RegExp("(?:^|;\\\\s*)"+n+"=([^;]*)"));return m?m[1]:null;}
  function setCookie(n,v){document.cookie=n+"="+v+";path=/;max-age=31536000;samesite=strict";}
  var saved=getCookie("ma_theme");
  if(saved){root.setAttribute("data-theme",saved);}
  function update(){var t=root.getAttribute("data-theme");btn.textContent=t==="dark"?"\\u2600\\uFE0F":"\\uD83C\\uDF19";}
  update();
  btn.addEventListener("click",function(){
    var cur=root.getAttribute("data-theme");
    if(cur==="dark"){root.setAttribute("data-theme","light");setCookie("ma_theme","light");}
    else{root.setAttribute("data-theme","dark");setCookie("ma_theme","dark");}
    update();
  });
})();
</script>
"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Startseite: Token eingeben, Links zu Chat/Config."""
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    show_onboarding = not _onboarding_complete()
    cfg = load_config()
    server_token = (cfg.get("server") or {}).get("token") or ""
    has_token = bool(server_token)
    # Token aus Cookie oder URL für Anzeige
    cookie_token = request.cookies.get("ma_token", "")
    effective_token = token or cookie_token
    is_authed = effective_token and effective_token == server_token
    token_esc = _escape(effective_token) if effective_token else ""
    config_links = ""
    if has_token:
        wh_cfg = cfg.get("webhooks") or {}
        wh_link = ('<li><a href="/webhooks' + tq + '">Webhooks</a></li>') if (isinstance(wh_cfg, dict) and wh_cfg.get("enabled")) else ""
        cc = cfg.get("chat_clients") or {}
        rooms_visible = bool((cc.get("matrix") or {}).get("enabled") or (cc.get("discord") or {}).get("enabled"))
        rooms_link = ('<li><a href="/rooms' + tq + '">Räume &amp; Channels</a></li>') if rooms_visible else ""
        config_links = '<li><a href="/config' + tq + '">Konfiguration</a></li><li><a href="/nutzung' + tq + '">Nutzung</a></li><li><a href="/schedules' + tq + '">Geplante Jobs</a></li>' + wh_link + rooms_link + '<li><a href="/agent' + tq + '">Vorgaben &amp; Dateien</a></li><li><a href="/workspace' + tq + '">Workspace Explorer</a></li><li><a href="/logs' + tq + '">Logs</a></li>'
    logout_btn = '<button type="button" class="btn btn-outline" id="logout-btn" style="margin-left:0.5em;">Logout</button>' if is_authed else ""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>MiniAssistant</title>
    <link rel="icon" href="/favicon.ico">
    <style>{_COMMON_CSS}</style>
    </head>
    <body>
    <div class="container">
    <div class="card">
      <div class="logo-row">
        <img src="/static/miniassistant.png" alt="MiniAssistant">
        <h1>MiniAssistant</h1>
      </div>
      <p style="margin-top:0;">Schlanker lokaler Assistent. Chat und API erfordern ein Token.</p>
      {"<div class=\"setup-hint\"><strong>Setup noch nicht abgeschlossen.</strong> Bitte zuerst <a href=\"/onboarding" + tq + "\">Onboarding / Setup</a> ausfuehren.</div>" if show_onboarding else ""}
      <p style="margin-bottom:0.3em;"><strong>Token:</strong></p>
      <form method="get" action="/chat" style="margin:0;">
      <div class="token-row">
        <input type="password" id="token-input" name="token" placeholder="Token eingeben" value="{token_esc}" autocomplete="current-password" />
        <button type="button" class="eye-btn" id="eye-btn" title="Token anzeigen/verbergen" aria-label="Token anzeigen">&#128065;</button>
        <button type="submit" class="btn btn-primary">Zum Chat</button>
        {logout_btn}
      </div>
      </form>
      {"<p style=\"margin-top:0.8em;\"><a href=\"/onboarding" + tq + "\" class=\"btn btn-outline\">Zuerst zum Onboarding / Setup</a></p>" if show_onboarding else ""}
    </div>
    <div class="card">
      <ul class="nav-links">
        <li><a href="/chat{tq}">Chat</a></li>
        {"<li><a href=\"/chat" + tq + ("&" if tq else "?") + "track=1\">Chat mit Verlauf</a></li>" if has_token else ""}
        {"<li><a href=\"/chats" + tq + "\">Gespeicherte Chats</a></li>" if has_token else ""}
        {"<li><a href=\"/onboarding" + tq + "\"><strong>Onboarding / Setup</strong> (noch ausstehend)</a></li>" if show_onboarding else ""}
        {config_links}
      </ul>
      <p class="text-muted">Token anzeigen (CLI): <code>miniassistant token</code></p>
    </div>
    {"<div class='card'><button type='button' class='btn btn-outline' id='restart-btn' style='color:var(--warning);border-color:var(--warning);'>Service neustarten</button> <span id='restart-msg' class='text-muted'></span></div>" if is_authed else ""}
    </div>
    <script>
    (function() {{
      var inp = document.getElementById("token-input");
      var btn = document.getElementById("eye-btn");
      if (inp && btn) {{
        btn.addEventListener("click", function() {{
          if (inp.type === "password") {{ inp.type = "text"; btn.textContent = "\\u2014"; btn.setAttribute("aria-label", "Token verbergen"); }}
          else {{ inp.type = "password"; btn.innerHTML = "&#128065;"; btn.setAttribute("aria-label", "Token anzeigen"); }}
        }});
      }}
      var logoutBtn = document.getElementById("logout-btn");
      if (logoutBtn) {{
        logoutBtn.addEventListener("click", function() {{
          window.location.href = "/logout";
        }});
      }}
      var restartBtn = document.getElementById("restart-btn");
      var restartMsg = document.getElementById("restart-msg");
      if (restartBtn) {{
        restartBtn.addEventListener("click", function() {{
          if (!confirm("Service wirklich neustarten?")) return;
          restartBtn.disabled = true;
          restartMsg.textContent = "Restart wird ausgelöst…";
          fetch("/api/restart", {{method: "POST", credentials: "same-origin"}}).then(function(r) {{
            return r.json();
          }}).then(function(data) {{
            restartMsg.textContent = data.message || "Restart ausgelöst. Seite wird gleich neu geladen…";
            setTimeout(function() {{ window.location.reload(); }}, 4000);
          }}).catch(function(e) {{
            restartMsg.textContent = "Fehler: " + e.message;
            restartBtn.disabled = false;
          }});
        }});
      }}
    }})();
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/config/raw", response_class=HTMLResponse)
async def config_page(request: Request):
    """Config-Seite (Raw YAML): YAML in Textarea anzeigen, editierbar. Nur zugänglich mit gültigem Token (oder wenn noch keiner gesetzt)."""
    _require_token(request)  # 401 wenn Token in Config gesetzt, aber fehlt oder falsch
    import base64
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    raw = load_config_raw()
    # Sensitive Werte maskieren (nur Anzeige, beim Speichern bleibt Original wenn nicht geändert)
    import re
    def _mask_secrets(text: str) -> str:
        pattern = r'((?:api_key|token|bot_token|github_token|password|secret):\s*)(\S+)'
        return re.sub(pattern, lambda m: m.group(1) + m.group(2)[:4] + '****' if len(m.group(2)) > 4 else m.group(0), text)
    display_raw = _mask_secrets(raw)
    raw_b64 = base64.b64encode(display_raw.encode("utf-8")).decode("ascii")
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Konfiguration – MiniAssistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="/favicon.ico">
    <style>
    {_COMMON_CSS}
    .config-wrap {{ max-width: 900px; margin: 0 auto; padding: 1.2em 1em; }}
    .config-header {{ display: flex; align-items: center; gap: 0.6em; margin-bottom: 0.4em; }}
    .config-header img {{ width: 40px; height: 40px; border-radius: 8px; }}
    .config-header h1 {{ margin: 0; font-size: 1.4em; }}
    .config-desc {{ color: var(--muted); margin-bottom: 1em; font-size: 0.92em; }}
    #config-yaml {{ width: 100%; min-height: 400px; font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
      font-size: 13px; padding: 0.8em; box-sizing: border-box; white-space: pre; resize: vertical;
      border: 1.5px solid var(--border); border-radius: var(--radius); background: var(--card);
      color: var(--text); outline: none; transition: border-color 0.15s; line-height: 1.5; }}
    #config-yaml:focus {{ border-color: var(--primary); }}
    .tabs {{ display: flex; gap: 0.4em; margin-bottom: 1em; border-bottom: 1.5px solid var(--border); }}
    .tab {{ padding: 0.55em 1em; border-radius: var(--radius) var(--radius) 0 0; background: transparent;
       border: 1.5px solid transparent; border-bottom: none; cursor: pointer; color: var(--muted); font-weight: 500; text-decoration: none; }}
    .tab.active {{ background: var(--card); border-color: var(--border); color: var(--text); margin-bottom: -1.5px; }}
    .actions-bar {{ position: sticky; bottom: 0; background: var(--card); border-top: 1px solid var(--border);
       padding: 0.7em 1em; margin: 1em -1em -1em; display: flex; gap: 0.7em; align-items: center; flex-wrap: wrap; }}
    #msg {{ font-size: 0.9em; font-weight: 500; }}
    #msg.success {{ color: var(--success); }}
    #msg.error {{ color: var(--danger); }}
    </style>
    </head>
    <body>
    <div class="config-wrap">
      <div class="config-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Konfiguration</h1>
      </div>
      <p class="config-desc">YAML bearbeiten und speichern. Die Datei wird auf gueltiges YAML und erwartete Struktur geprueft.</p>
      <div class="tabs">
        <a href="/config{tq}" class="tab">Form</a>
        <a href="/config/raw{tq}" class="tab active">Raw YAML</a>
      </div>
      <form id="f">
        <textarea id="config-yaml" name="yaml" spellcheck="false"></textarea>
      </form>
      <div class="actions-bar">
        <button type="button" id="save-btn" class="btn btn-primary">Speichern</button>
        <button type="button" id="reload-btn" class="btn btn-outline">Neu laden</button>
        <button type="button" id="restart-btn" class="btn btn-outline" style="color:var(--warning);border-color:var(--warning);">Service neustarten</button>
        <a href="/{tq}" class="btn btn-outline">Startseite</a>
        <span id="msg"></span>
      </div>
    </div>
    <div id="config-raw" data-yaml="{raw_b64}" style="display:none;"></div>
    <script>
    (function() {{
      var el = document.getElementById("config-raw");
      if (el && el.getAttribute("data-yaml")) {{
        try {{
          document.getElementById("config-yaml").value = atob(el.getAttribute("data-yaml"));
        }} catch (_) {{}}
      }}
    }})();
    var TOKEN = new URLSearchParams(window.location.search).get("token") || "";
    function tokenQS() {{ return TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""; }}
    document.getElementById("save-btn").addEventListener("click", function() {{
      var msg = document.getElementById("msg");
      msg.textContent = "";
      msg.className = "";
      var yaml = document.getElementById("config-yaml").value;
      fetch("/api/config" + tokenQS(), {{
        method: "POST",
        headers: {{ "Content-Type": "text/plain; charset=utf-8" }},
        body: yaml
      }}).then(function(r) {{
        return r.json().then(function(data) {{
          if (r.ok) {{ msg.innerHTML = "Gespeichert. <strong>Dienst muss neu gestartet werden</strong>, damit die \u00c4nderungen wirksam werden."; msg.className = "success"; }}
          else {{ msg.textContent = data.error || data.detail || "Fehler"; msg.className = "error"; }}
        }}).catch(function() {{
          if (r.ok) {{ msg.innerHTML = "Gespeichert. <strong>Dienst muss neu gestartet werden.</strong>"; msg.className = "success"; }}
          else {{ msg.textContent = "Fehler " + r.status; msg.className = "error"; }}
        }});
      }});
    }});
    document.getElementById("reload-btn").addEventListener("click", function() {{
      window.location.reload();
    }});
    document.getElementById("restart-btn").addEventListener("click", function() {{
      if (!confirm("Service wirklich neustarten?")) return;
      var msg = document.getElementById("msg");
      var btn = document.getElementById("restart-btn");
      btn.disabled = true;
      msg.textContent = "Restart wird ausgel\u00f6st\u2026";
      msg.className = "";
      fetch("/api/restart" + tokenQS(), {{ method: "POST", credentials: "same-origin" }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          msg.textContent = data.message || "Restart ausgel\u00f6st. Seite wird gleich neu geladen\u2026";
          msg.className = "success";
          setTimeout(function() {{ window.location.reload(); }}, 4000);
        }})
        .catch(function(e) {{
          msg.textContent = "Fehler: " + e.message;
          msg.className = "error";
          btn.disabled = false;
        }});
    }});
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/config", response_class=HTMLResponse)
async def config_form_page(request: Request):
    """Form-basierter Config-Editor (Tabellen-Ansicht, Standard). Daten kommen via JS aus /api/config/form."""
    _require_token(request)
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Konfiguration (Form) – MiniAssistant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.ico">
<style>
{_COMMON_CSS}
.config-wrap {{ max-width: 1000px; margin: 0 auto; padding: 1.2em 1em; }}
.config-header {{ display: flex; align-items: center; gap: 0.6em; margin-bottom: 0.4em; }}
.config-header img {{ width: 40px; height: 40px; border-radius: 8px; }}
.config-header h1 {{ margin: 0; font-size: 1.4em; }}
.config-desc {{ color: var(--muted); margin-bottom: 1em; font-size: 0.92em; }}
.tabs {{ display: flex; gap: 0.4em; margin-bottom: 1em; border-bottom: 1.5px solid var(--border); }}
.tab {{ padding: 0.55em 1em; border-radius: var(--radius) var(--radius) 0 0; background: transparent;
       border: 1.5px solid transparent; border-bottom: none; cursor: pointer; color: var(--muted); font-weight: 500; }}
.tab.active {{ background: var(--card); border-color: var(--border); color: var(--text); margin-bottom: -1.5px; }}
.section {{ background: var(--card); border-radius: var(--radius); border: 1px solid var(--border); margin-bottom: 0.8em; }}
.section > .sec-header {{ padding: 0.7em 1em; font-weight: 600; cursor: pointer; user-select: none;
       display: flex; align-items: center; justify-content: space-between; }}
.section > .sec-header:hover {{ background: var(--bg); }}
.section > .sec-body {{ padding: 0.6em 1em 1em; border-top: 1px solid var(--border); display: none; }}
.section.open > .sec-body {{ display: block; }}
.section .sec-chev {{ transition: transform 0.15s; }}
.section.open .sec-chev {{ transform: rotate(90deg); }}
.field {{ margin-bottom: 0.8em; }}
.field label {{ display: block; font-size: 0.88em; font-weight: 500; margin-bottom: 0.25em; color: var(--text); }}
.field .desc {{ color: var(--muted); font-size: 0.82em; margin-top: 0.2em; }}
.field input[type="text"], .field input[type="password"], .field input[type="number"], .field select, .field textarea {{
       width: 100%; max-width: 540px; }}
.field textarea {{ font-family: 'SF Mono', monospace; font-size: 0.85em; min-height: 80px; }}
.field.bool {{ display: flex; align-items: center; gap: 0.5em; }}
.field.bool label {{ margin: 0; }}
.dict-item {{ background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
       padding: 0.7em 0.9em; margin-bottom: 0.6em; }}
.dict-item-header {{ display: flex; gap: 0.5em; align-items: center; margin-bottom: 0.5em; }}
.dict-item-header input {{ flex: 1; max-width: 320px; }}
.dict-item-header .btn-remove {{ padding: 0.3em 0.7em; background: var(--danger); color: #fff; }}
.dict-item-header .btn-remove:hover {{ background: var(--danger); opacity: 0.85; }}
.btn-add {{ padding: 0.35em 0.9em; background: transparent; border: 1.5px dashed var(--primary);
       color: var(--primary); font-size: 0.9em; }}
.btn-add:hover {{ background: var(--primary); color: #fff; }}
.secret-badge {{ display: inline-block; padding: 0.1em 0.5em; border-radius: 999px; font-size: 0.75em;
       background: var(--success); color: #fff; margin-left: 0.4em; vertical-align: middle; }}
.secret-badge.unset {{ background: var(--muted); }}
.ro-tag {{ display: inline-block; padding: 0.1em 0.5em; border-radius: 999px; font-size: 0.72em;
       background: var(--warning); color: #000; margin-left: 0.4em; vertical-align: middle; }}
.actions-bar {{ position: sticky; bottom: 0; background: var(--card); border-top: 1px solid var(--border);
       padding: 0.7em 1em; margin: 1em -1em -1em; display: flex; gap: 0.7em; align-items: center; flex-wrap: wrap; }}
#msg.success {{ color: var(--success); font-weight: 500; }}
#msg.error {{ color: var(--danger); font-weight: 500; }}
.row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.6em 1em; }}
@media (max-width: 700px) {{ .row-2 {{ grid-template-columns: 1fr; }} }}
.subsec {{ border-top: 1px dashed var(--border); margin-top: 0.8em; padding-top: 0.7em; }}
.subsec h4 {{ margin: 0 0 0.5em; font-size: 0.95em; }}
.providers-list {{ display: flex; flex-direction: column; gap: 0.5em; }}
.providers-list .prov {{ background: var(--bg); padding: 0.6em 0.9em; border-radius: var(--radius); font-size: 0.9em; }}
.providers-list .prov .pname {{ font-weight: 600; }}
.providers-list .prov .pkv {{ color: var(--muted); font-size: 0.85em; }}
.list-rows .lrow {{ display: flex; gap: 0.4em; align-items: center; margin-bottom: 0.35em; }}
.list-rows .lrow input {{ flex: 1; max-width: 540px; }}
.list-rows .lrow .btn-remove {{ padding: 0.25em 0.55em; background: var(--danger); color: #fff; }}
.toggle-disabled {{ opacity: 0.55; pointer-events: none; }}
</style>
</head>
<body>
<div class="config-wrap">
  <div class="config-header">
    <img src="/static/miniassistant.png" alt="Logo">
    <h1>Konfiguration</h1>
  </div>
  <p class="config-desc">Form-basierter Editor. Leere Secret-Felder bleiben unverändert. Provider sind read-only (im Raw-Editor bearbeiten).</p>
  <div class="tabs">
    <a href="/config{tq}" class="tab active">Form</a>
    <a href="/config/raw{tq}" class="tab">Raw YAML</a>
  </div>
  <div id="config-form">Lade Config…</div>
  <div class="actions-bar">
    <button id="save-btn" class="btn btn-primary" type="button">Speichern</button>
    <button id="reload-btn" class="btn btn-outline" type="button">Neu laden</button>
    <button id="restart-btn" class="btn btn-outline" type="button" style="color:var(--warning);border-color:var(--warning);">Service neustarten</button>
    <a href="/{tq}" class="btn btn-outline">Startseite</a>
    <span id="msg"></span>
  </div>
</div>
<script>
(function() {{
  var TOKEN = new URLSearchParams(window.location.search).get("token") || "";
  function qs(p) {{ return TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""; }}

  // ---- Schema: beschreibt alle editierbaren Sektionen ----
  // Pfad-Syntax: "a.b.c" → cfg.a.b.c
  // Felder können fd.revealPath: "..." haben (statischer Pfad zum Reveal-Endpoint).
  // Für dynamische Pfade (email-Account-Password) markiert "{{NAME}}" den Konto-Namen.
  var SCHEMA = [
    {{ id: "server", title: "Server", path: "server", fields: [
        {{ n:"host", t:"text", l:"Host", desc:"Bind-Adresse (z.B. 127.0.0.1 oder 0.0.0.0)" }},
        {{ n:"port", t:"int", l:"Port" }},
        {{ n:"token", t:"password", l:"API-Token", secretFlag:"_token_set", revealPath:"server.token", desc:"Leer = unverändert. Auge: aktuellen Wert anzeigen." }},
        {{ n:"debug", t:"bool", l:"Debug (Request/Response im JSON)" }},
        {{ n:"show_estimated_tokens", t:"bool", l:"Token-Schätzung im Stream zeigen" }},
        {{ n:"log_agent_actions", t:"bool", l:"Agent-Actions loggen" }},
        {{ n:"show_context", t:"bool", l:"Kontext anzeigen" }},
        {{ n:"track_usage", t:"bool", l:"Usage-Tracking" }},
    ]}},

    {{ id: "paths", title: "Verzeichnisse & Top-Level", path: "", fields: [
        {{ n:"agent_dir", t:"text", l:"Agent-Verzeichnis" }},
        {{ n:"workspace", t:"text", l:"Workspace" }},
        {{ n:"trash_dir", t:"text", l:"Papierkorb" }},
        {{ n:"avatar", t:"text", l:"Avatar (Pfad oder URL)" }},
        {{ n:"max_chars_per_file", t:"int", l:"Max. Zeichen pro Datei" }},
        {{ n:"github_token", t:"password", l:"GitHub-Token", secretFlag:"_github_token_set", revealPath:"github_token" }},
    ]}},

    {{ id: "providers", title: "Provider (read-only)", type:"providers_ro" }},

    {{ id: "models_top", title: "Modell-Auswahl (Default-Provider Shortcut)", path: "models", fields: [
        {{ n:"default", t:"text", l:"Default-Modell" }},
        {{ n:"list", t:"list_inline", l:"Modell-Whitelist (leer = alle)" }},
        {{ n:"subagents", t:"bool", l:"Als Subagent-Quelle nutzen" }},
    ]}},
    {{ id: "models_aliases", title: "Modell-Aliase", path: "models.aliases", type:"dict_kv", keyLabel:"Alias", valueLabel:"Modell-Name" }},
    {{ id: "models_fallbacks", title: "Modell-Fallbacks (Liste)", path: "models.fallbacks", type:"list" }},

    {{ id: "subagents_top", title: "Subagents (Liste)", path: "subagents", type:"list" }},
    {{ id: "fallbacks_top", title: "Globale Fallback-Modelle", path: "fallbacks", type:"list" }},
    {{ id: "vision_top", title: "Vision-Modelle", path: "vision", type:"list" }},
    {{ id: "image_gen", title: "Image-Generation-Modelle", path: "image_generation", type:"list" }},

    {{ id: "matrix", title: "Matrix-Bot", path: "chat_clients.matrix",
       desc:"Per-Room-Modi (always/mention/off) werden auf der Seite /rooms verwaltet.",
       fields: [
        {{ n:"enabled", t:"bool", l:"Aktiv" }},
        {{ n:"homeserver", t:"text", l:"Homeserver-URL" }},
        {{ n:"bot_name", t:"text", l:"Bot-Name" }},
        {{ n:"user_id", t:"text", l:"User-ID (@bot:server.tld)" }},
        {{ n:"token", t:"password", l:"Access-Token", secretFlag:"_token_set", revealPath:"chat_clients.matrix.token" }},
        {{ n:"device_id", t:"text", l:"Device-ID (optional)" }},
        {{ n:"encrypted_rooms", t:"bool", l:"E2EE-Räume zulassen" }},
    ]}},

    {{ id: "discord", title: "Discord-Bot", path: "chat_clients.discord",
       desc:"Per-Channel-Modi (always/mention/off) werden auf der Seite /rooms verwaltet.",
       fields: [
        {{ n:"enabled", t:"bool", l:"Aktiv" }},
        {{ n:"bot_token", t:"password", l:"Bot-Token", secretFlag:"_bot_token_set", revealPath:"chat_clients.discord.bot_token" }},
        {{ n:"command_prefix", t:"text", l:"Befehls-Präfix (z.B. !)" }},
    ]}},

    {{ id: "email", title: "E-Mail", path: "email", fields: [
        {{ n:"default", t:"text", l:"Default-Konto" }},
    ], dictObj:{{ key:"accounts", title:"Konten", keyLabel:"Konto-Name", item:[
        {{ n:"imap_server", t:"text", l:"IMAP-Server" }},
        {{ n:"imap_port", t:"int", l:"IMAP-Port (993)" }},
        {{ n:"smtp_server", t:"text", l:"SMTP-Server" }},
        {{ n:"smtp_port", t:"int", l:"SMTP-Port (587)" }},
        {{ n:"username", t:"text", l:"Benutzer / Adresse" }},
        {{ n:"password", t:"password", l:"Passwort", secretFlag:"_password_set", revealPath:"email.accounts.{{NAME}}.password" }},
        {{ n:"ssl", t:"bool", l:"SSL/TLS" }},
        {{ n:"name", t:"text", l:"Anzeigename" }},
    ] }} }},

    {{ id: "voice_top", title: "Voice (allgemein)", path: "voice", fields: [
        {{ n:"language", t:"text", l:"Sprache (z.B. de)" }},
        {{ n:"tts_voice", t:"text", l:"TTS-Stimme (Shortcut)" }},
    ]}},
    {{ id: "voice_stt", title: "Voice → STT", path: "voice.stt", fields: [
        {{ n:"url", t:"text", l:"STT-URL (Wyoming)" }},
        {{ n:"language", t:"text", l:"Sprache (Fallback)" }},
    ]}},
    {{ id: "voice_tts", title: "Voice → TTS", path: "voice.tts", fields: [
        {{ n:"url", t:"text", l:"TTS-URL" }},
        {{ n:"model", t:"text", l:"Modell (piper/vibevoice/kokoro/…)" }},
        {{ n:"language", t:"text", l:"Sprache" }},
        {{ n:"voice", t:"text", l:"Stimme" }},
        {{ n:"speed", t:"float", l:"speed" }},
        {{ n:"length_scale", t:"float", l:"length_scale (Piper)" }},
        {{ n:"noise_scale", t:"float", l:"noise_scale (Piper)" }},
        {{ n:"noise_w", t:"float", l:"noise_w (Piper)" }},
        {{ n:"sentence_silence", t:"float", l:"sentence_silence" }},
        {{ n:"seed", t:"int", l:"seed" }},
        {{ n:"response_format", t:"text", l:"response_format" }},
        {{ n:"voice_mode", t:"text", l:"voice_mode (Chatterbox)" }},
        {{ n:"cfg_weight", t:"float", l:"cfg_weight" }},
        {{ n:"exaggeration", t:"float", l:"exaggeration" }},
        {{ n:"temperature", t:"float", l:"temperature" }},
        {{ n:"chunk_size", t:"int", l:"chunk_size" }},
        {{ n:"split_text", t:"bool", l:"split_text" }},
        {{ n:"speed_factor", t:"float", l:"speed_factor" }},
    ]}},

    {{ id: "search_engines", title: "Suchmaschinen (Searx-NG)", path: "search_engines", type:"dict_obj_inline",
       keyLabel:"Engine-ID", item:[{{ n:"url", t:"text", l:"URL (z.B. https://searx.example/search?q=)" }}] }},
    {{ id: "default_search_engine", title: "Default-Suchmaschine", path: "", fields: [
        {{ n:"default_search_engine", t:"text", l:"ID der Default-Engine" }},
        {{ n:"search_engine_strategy", t:"enum", l:"Strategie", options:["first","roundrobin","fallback","random","specific"] }},
    ]}},

    {{ id: "memory", title: "Memory", path: "memory", fields: [
        {{ n:"enabled", t:"bool", l:"Aktiv" }},
        {{ n:"max_chars_per_line", t:"int", l:"Max. Zeichen pro Zeile" }},
        {{ n:"days", t:"int", l:"Tage Retention" }},
        {{ n:"max_tokens", t:"int", l:"Max. Tokens" }},
        {{ n:"track_user_id", t:"bool", l:"User-ID tracken" }},
    ]}},

    {{ id: "mempalace", title: "MemPalace (Semantic Memory)", path: "mempalace", fields: [
        {{ n:"enabled", t:"bool", l:"Aktiv (memory.enabled muss true sein)" }},
        {{ n:"wing", t:"text", l:"Wing" }},
        {{ n:"default_room", t:"text", l:"Default-Room" }},
        {{ n:"max_tokens", t:"int", l:"Max. Tokens" }},
        {{ n:"language", t:"list_inline", l:"Sprachen" }},
    ]}},

    {{ id: "chat_section", title: "Chat", path: "chat", fields: [
        {{ n:"context_quota", t:"float", l:"Context-Quota (0..1)" }},
    ]}},

    {{ id: "scheduler", title: "Scheduler", path: "scheduler", type:"falsy_or_form",
       fields:[{{n:"enabled", t:"bool", l:"Aktiv"}}] }},

    {{ id: "webhooks", title: "Webhooks", path: "webhooks", type:"falsy_or_form", fields: [
        {{ n:"enabled", t:"bool", l:"Aktiv" }},
        {{ n:"rate_limit_per_min", t:"int", l:"Rate-Limit/min" }},
        {{ n:"output_keep_last", t:"int", l:"Letzte N Outputs behalten" }},
        {{ n:"parallel", t:"bool", l:"Parallele Ausführung" }},
        {{ n:"max_retries", t:"int", l:"Max. Retries" }},
    ]}},

    {{ id: "raw_proxy", title: "Raw-Proxy (/raw/v1)", path: "raw_proxy", fields: [
        {{ n:"enabled", t:"bool", l:"Aktiv" }},
        {{ n:"token", t:"password", l:"Proxy-Token", secretFlag:"_token_set", revealPath:"raw_proxy.token" }},
        {{ n:"rate_limit", t:"int", l:"Rate-Limit/min" }},
        {{ n:"allowed_models", t:"list_inline", l:"Erlaubte Modelle (leer = alle)" }},
    ]}},

    {{ id: "tuning", title: "Tuning (Timeouts & Limits)", path: "", fields: [
        {{ n:"api_timeout", t:"int", l:"api_timeout (s)" }},
        {{ n:"subagent_api_timeout", t:"int", l:"subagent_api_timeout (s)" }},
        {{ n:"invoke_model_timeout", t:"int", l:"invoke_model_timeout (s)" }},
        {{ n:"tool_execution_timeout", t:"int", l:"tool_execution_timeout (s)" }},
        {{ n:"subagent_execution_timeout", t:"int", l:"subagent_execution_timeout (s)" }},
        {{ n:"schedule_timeout", t:"int", l:"schedule_timeout (s)" }},
        {{ n:"stream_stall_timeout", t:"int", l:"stream_stall_timeout (s)" }},
        {{ n:"stream_thinking_timeout", t:"int", l:"stream_thinking_timeout (s)" }},
        {{ n:"stream_thinking_hard_timeout", t:"int", l:"stream_thinking_hard_timeout (s)" }},
        {{ n:"stream_round_timeout", t:"int", l:"stream_round_timeout (s)" }},
        {{ n:"stream_loop_max_consecutive", t:"int", l:"stream_loop_max_consecutive" }},
        {{ n:"stream_loop_recovery_max", t:"int", l:"stream_loop_recovery_max" }},
        {{ n:"max_tool_rounds", t:"int", l:"max_tool_rounds" }},
        {{ n:"exec_max_output_chars", t:"int", l:"exec_max_output_chars" }},
        {{ n:"prefs_max_chars", t:"int", l:"prefs_max_chars" }},
        {{ n:"prefs_max_chars_per_file", t:"int", l:"prefs_max_chars_per_file" }},
        {{ n:"respond_in_input_language", t:"bool", l:"respond_in_input_language" }},
        {{ n:"doc_max_chars", t:"int", l:"doc_max_chars" }},
        {{ n:"doc_max_pages_render", t:"int", l:"doc_max_pages_render" }},
        {{ n:"image_edit_strength", t:"float", l:"image_edit_strength (global, 0-1; 1.0 = voller Denoise. Pro Modell via model_options im Raw-YAML)" }},
        {{ n:"image_edit_max_edge", t:"int", l:"image_edit_max_edge (max. Kantenlänge Quellbild beim Edit, Default 2048)" }},
    ]}},
  ];

  // ---- Pfad-Helpers ----
  function getPath(obj, path) {{
    if (!path) return obj;
    var parts = path.split("."); var cur = obj;
    for (var i=0; i<parts.length; i++) {{
      if (!cur || typeof cur !== "object") return undefined;
      cur = cur[parts[i]];
    }}
    return cur;
  }}
  function setPath(obj, path, val) {{
    if (!path) {{ Object.assign(obj, val || {{}}); return; }}
    var parts = path.split("."); var cur = obj;
    for (var i=0; i<parts.length-1; i++) {{
      if (!cur[parts[i]] || typeof cur[parts[i]] !== "object") cur[parts[i]] = {{}};
      cur = cur[parts[i]];
    }}
    cur[parts[parts.length-1]] = val;
  }}

  // ---- Renderer ----
  // revealPathFn: optional function returning a string path; click on eye → fetch + show
  function makeField(fd, value, secretSet, revealPathFn) {{
    var wrap = document.createElement("div");
    wrap.className = "field" + (fd.t === "bool" ? " bool" : "");
    var id = "f_" + Math.random().toString(36).slice(2,9);
    if (fd.t === "bool") {{
      var cb = document.createElement("input"); cb.type = "checkbox"; cb.id = id;
      cb.checked = !!value;
      wrap.appendChild(cb);
      var lab = document.createElement("label"); lab.htmlFor = id; lab.textContent = fd.l;
      wrap.appendChild(lab);
      wrap._get = function() {{ return cb.checked; }};
      if (fd.desc) {{ var d0 = document.createElement("div"); d0.className = "desc"; d0.textContent = fd.desc; wrap.appendChild(d0); }}
      return wrap;
    }}
    if (fd.t === "list_inline") {{
      // Inline list-of-strings — Zeile pro Eintrag, +Add Button
      var lab1 = document.createElement("label"); lab1.textContent = fd.l; wrap.appendChild(lab1);
      var listHost = document.createElement("div"); wrap.appendChild(listHost);
      var listGet = makeList(listHost, Array.isArray(value) ? value : []);
      wrap._get = function() {{ return listGet(); }};
      if (fd.desc) {{ var d1 = document.createElement("div"); d1.className = "desc"; d1.textContent = fd.desc; wrap.appendChild(d1); }}
      return wrap;
    }}
    var lab2 = document.createElement("label"); lab2.htmlFor = id; lab2.textContent = fd.l;
    if (fd.t === "password" && fd.secretFlag) {{
      var badge = document.createElement("span");
      badge.className = "secret-badge" + (secretSet ? "" : " unset");
      badge.textContent = secretSet ? "gesetzt" : "nicht gesetzt";
      lab2.appendChild(badge);
    }}
    wrap.appendChild(lab2);
    var inp;
    if (fd.t === "enum") {{
      inp = document.createElement("select"); inp.id = id;
      (fd.options || []).forEach(function(o) {{
        var op = document.createElement("option"); op.value = o; op.textContent = o; inp.appendChild(op);
      }});
      if (value != null) inp.value = String(value);
      wrap.appendChild(inp);
    }} else {{
      inp = document.createElement("input"); inp.id = id;
      inp.type = (fd.t === "password") ? "password" : (fd.t === "int" || fd.t === "float") ? "number" : "text";
      if (fd.t === "float") inp.step = "any";
      if (fd.t === "password") inp.placeholder = secretSet ? "•••• (leer = unverändert)" : "";
      if (value != null && fd.t !== "password") inp.value = String(value);
      if (fd.t === "password" && revealPathFn) {{
        // Eye-Toggle: zeigt aktuellen (oder gerade eingegebenen) Wert
        var row = document.createElement("div");
        row.className = "token-row";
        row.style.cssText = "display:flex; gap:0.4em; align-items:center; max-width:540px;";
        row.appendChild(inp);
        var eye = document.createElement("button");
        eye.type = "button"; eye.className = "eye-btn"; eye.textContent = "\\u{{1F441}}"; eye.title = "Wert anzeigen";
        var revealed = false;
        eye.addEventListener("click", function() {{
          if (revealed) {{ inp.type = "password"; revealed = false; eye.textContent = "\\u{{1F441}}"; return; }}
          if (inp.value === "" && secretSet) {{
            var p = revealPathFn(); if (!p) return;
            fetch("/api/config/reveal?path=" + encodeURIComponent(p) + (TOKEN ? "&token=" + encodeURIComponent(TOKEN) : ""), {{ credentials: "same-origin" }})
              .then(function(r) {{ if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }})
              .then(function(d) {{ inp.value = d.value || ""; inp.type = "text"; revealed = true; eye.textContent = "\\u{{1F576}}"; }})
              .catch(function() {{ /* still leer lassen */ }});
          }} else {{
            inp.type = "text"; revealed = true; eye.textContent = "\\u{{1F576}}";
          }}
        }});
        row.appendChild(eye);
        wrap.appendChild(row);
      }} else {{
        wrap.appendChild(inp);
      }}
    }}
    wrap._get = function() {{
      var v = inp.value;
      if (fd.t === "int") {{ if (v === "") return null; var n = parseInt(v, 10); return isNaN(n) ? null : n; }}
      if (fd.t === "float") {{ if (v === "") return null; var f = parseFloat(v); return isNaN(f) ? null : f; }}
      if (fd.t === "password") {{ return v; }}
      return v;
    }};
    if (fd.desc) {{
      var d = document.createElement("div"); d.className = "desc"; d.textContent = fd.desc; wrap.appendChild(d);
    }}
    return wrap;
  }}

  function makeDictKV(host, sec, data) {{
    // dict<string, scalar/enum>
    var dictVal = data || {{}};
    var wrap = document.createElement("div");
    var rows = document.createElement("div"); rows.className = "list-rows"; wrap.appendChild(rows);
    function addRow(k, v) {{
      var row = document.createElement("div"); row.className = "lrow";
      var ki = document.createElement("input"); ki.type = "text"; ki.placeholder = sec.keyLabel || "Key"; ki.value = k || "";
      var vi;
      if (sec.t === "enum") {{
        vi = document.createElement("select");
        (sec.options || []).forEach(function(o) {{ var op = document.createElement("option"); op.value = o; op.textContent = o; vi.appendChild(op); }});
        vi.value = v || (sec.options && sec.options[0]) || "";
      }} else {{
        vi = document.createElement("input"); vi.type = "text"; vi.value = v == null ? "" : String(v);
      }}
      var rm = document.createElement("button"); rm.type = "button"; rm.className = "btn btn-remove"; rm.textContent = "Entfernen";
      rm.addEventListener("click", function() {{ rows.removeChild(row); }});
      row.appendChild(ki); row.appendChild(vi); row.appendChild(rm);
      rows.appendChild(row);
    }}
    Object.keys(dictVal).forEach(function(k) {{ addRow(k, dictVal[k]); }});
    var addBtn = document.createElement("button"); addBtn.type = "button"; addBtn.className = "btn btn-add"; addBtn.textContent = "+ Hinzufügen";
    addBtn.addEventListener("click", function() {{ addRow("", sec.t === "enum" ? (sec.options && sec.options[0]) : ""); }});
    wrap.appendChild(addBtn);
    host.appendChild(wrap);
    return function() {{
      var out = {{}};
      rows.querySelectorAll(".lrow").forEach(function(r) {{
        var ins = r.querySelectorAll("input, select");
        var k = (ins[0].value || "").trim();
        var v = ins[1].value;
        if (k) out[k] = v;
      }});
      return out;
    }};
  }}

  function makeList(host, data) {{
    var arr = Array.isArray(data) ? data : [];
    var wrap = document.createElement("div");
    var rows = document.createElement("div"); rows.className = "list-rows"; wrap.appendChild(rows);
    function addRow(v) {{
      var row = document.createElement("div"); row.className = "lrow";
      var inp = document.createElement("input"); inp.type = "text"; inp.value = v == null ? "" : String(v);
      var rm = document.createElement("button"); rm.type = "button"; rm.className = "btn btn-remove"; rm.textContent = "Entfernen";
      rm.addEventListener("click", function() {{ rows.removeChild(row); }});
      row.appendChild(inp); row.appendChild(rm); rows.appendChild(row);
    }}
    arr.forEach(addRow);
    var add = document.createElement("button"); add.type = "button"; add.className = "btn btn-add"; add.textContent = "+ Hinzufügen";
    add.addEventListener("click", function() {{ addRow(""); }});
    wrap.appendChild(add);
    host.appendChild(wrap);
    return function() {{
      var out = [];
      rows.querySelectorAll(".lrow input").forEach(function(i) {{ var v = i.value.trim(); if (v) out.push(v); }});
      return out;
    }};
  }}

  function makeDictObj(host, sec, data) {{
    // dict<string, {{fields}}>
    var dictVal = (data && typeof data === "object") ? data : {{}};
    var wrap = document.createElement("div");
    var items = document.createElement("div"); wrap.appendChild(items);
    var getters = [];
    function addItem(name, val) {{
      val = val || {{}};
      var box = document.createElement("div"); box.className = "dict-item";
      var hdr = document.createElement("div"); hdr.className = "dict-item-header";
      var nameInp = document.createElement("input"); nameInp.type = "text"; nameInp.placeholder = sec.keyLabel || "Name"; nameInp.value = name || "";
      hdr.appendChild(nameInp);
      var rm = document.createElement("button"); rm.type = "button"; rm.className = "btn btn-remove"; rm.textContent = "Entfernen";
      rm.addEventListener("click", function() {{ items.removeChild(box); getters = getters.filter(function(g) {{ return g.box !== box; }}); }});
      hdr.appendChild(rm);
      box.appendChild(hdr);
      var fieldGetters = [];
      (sec.item || []).forEach(function(fd) {{
        var v = val[fd.n];
        var secretSet = fd.secretFlag ? !!val[fd.secretFlag] : false;
        var revealFn = null;
        if (fd.revealPath && fd.revealPath.indexOf("{{NAME}}") >= 0) {{
          revealFn = function() {{ var nm = nameInp.value.trim(); if (!nm) return ""; return fd.revealPath.replace("{{NAME}}", nm); }};
        }} else if (fd.revealPath) {{
          revealFn = function() {{ return fd.revealPath; }};
        }}
        var fdNode = makeField(fd, v, secretSet, revealFn);
        box.appendChild(fdNode);
        fieldGetters.push({{ fd: fd, get: fdNode._get }});
      }});
      items.appendChild(box);
      getters.push({{ box: box, name: function() {{ return nameInp.value.trim(); }}, fields: fieldGetters }});
    }}
    Object.keys(dictVal).forEach(function(k) {{ addItem(k, dictVal[k]); }});
    var add = document.createElement("button"); add.type = "button"; add.className = "btn btn-add"; add.textContent = "+ Hinzufügen";
    add.addEventListener("click", function() {{ addItem("", {{}}); }});
    wrap.appendChild(add);
    host.appendChild(wrap);
    return function() {{
      var out = {{}};
      getters.forEach(function(g) {{
        var n = g.name(); if (!n) return;
        var obj = {{}};
        g.fields.forEach(function(fg) {{ obj[fg.fd.n] = fg.get(); }});
        out[n] = obj;
      }});
      return out;
    }};
  }}

  function renderProvidersRO(host, cfg) {{
    var p = document.createElement("p"); p.className = "desc";
    p.textContent = "Provider werden im Raw-YAML-Editor bearbeitet (zu viele Sonderfälle: type, options, model_options, …). Vollständige Ansicht (api_keys maskiert):";
    host.appendChild(p);
    // Provider-Daten: api_key bleibt geleert (Sicherheit), aber _api_key_set:bool zeigt Stand.
    var pre = document.createElement("pre");
    pre.style.cssText = "background:var(--bg); padding:0.8em 1em; border-radius:var(--radius); overflow:auto; font-size:0.82em; max-height:540px; white-space:pre; margin:0; border:1px solid var(--border);";
    // Zeige Klartext-JSON, ersetze api_key durch sichtbare Marker je nach _api_key_set
    var view = {{}};
    Object.keys(cfg.providers || {{}}).forEach(function(name) {{
      var src = (cfg.providers || {{}})[name] || {{}};
      var out = {{}};
      Object.keys(src).forEach(function(k) {{
        if (k === "_api_key_set") return;
        if (k === "api_key") {{ out.api_key = src._api_key_set ? "<gesetzt>" : null; return; }}
        out[k] = src[k];
      }});
      view[name] = out;
    }});
    pre.textContent = JSON.stringify(view, null, 2);
    host.appendChild(pre);
  }}

  function renderSection(sec, cfg) {{
    var box = document.createElement("div"); box.className = "section";
    var hdr = document.createElement("div"); hdr.className = "sec-header";
    var titleSpan = document.createElement("span"); titleSpan.textContent = sec.title;
    if (sec.type === "providers_ro") {{
      var tag = document.createElement("span"); tag.className = "ro-tag"; tag.textContent = "read-only"; titleSpan.appendChild(tag);
    }}
    hdr.appendChild(titleSpan);
    var chev = document.createElement("span"); chev.className = "sec-chev"; chev.textContent = "▶"; hdr.appendChild(chev);
    box.appendChild(hdr);
    var body = document.createElement("div"); body.className = "sec-body";
    box.appendChild(body);
    hdr.addEventListener("click", function() {{ box.classList.toggle("open"); }});

    var saver = null;

    if (sec.type === "providers_ro") {{
      renderProvidersRO(body, cfg);
      box._save = function(out) {{ /* read-only — providers übernimmt der Server aus Original */ }};
      return box;
    }}
    if (sec.type === "list") {{
      var listGet = makeList(body, getPath(cfg, sec.path));
      box._save = function(out) {{ setPath(out, sec.path, listGet()); }};
      return box;
    }}
    if (sec.type === "dict_kv") {{
      var kvGet = makeDictKV(body, sec, getPath(cfg, sec.path));
      box._save = function(out) {{ setPath(out, sec.path, kvGet()); }};
      return box;
    }}
    if (sec.type === "dict_obj_inline") {{
      var objGet = makeDictObj(body, sec, getPath(cfg, sec.path));
      box._save = function(out) {{ setPath(out, sec.path, objGet()); }};
      return box;
    }}

    // form / falsy_or_form
    var falsyTip = null;
    if (sec.type === "falsy_or_form") {{
      falsyTip = document.createElement("p"); falsyTip.className = "desc";
      falsyTip.textContent = "Wenn 'Aktiv' deaktiviert ist, wird die Sektion als false gespeichert.";
      body.appendChild(falsyTip);
    }}
    if (sec.desc) {{
      var sd = document.createElement("p"); sd.className = "desc"; sd.textContent = sec.desc;
      body.appendChild(sd);
    }}
    var data = getPath(cfg, sec.path);
    if (data === true) data = {{ enabled: true }};
    if (!data || typeof data !== "object" || Array.isArray(data)) data = {{}};
    var fieldGetters = [];
    (sec.fields || []).forEach(function(fd) {{
      var val = data[fd.n];
      var secretSet = fd.secretFlag ? !!data[fd.secretFlag] : false;
      var revealFn = fd.revealPath ? (function(p) {{ return function() {{ return p; }}; }})(fd.revealPath) : null;
      var node = makeField(fd, val, secretSet, revealFn);
      body.appendChild(node);
      fieldGetters.push({{ fd: fd, get: node._get }});
    }});
    if (sec.dict) {{
      var sub = document.createElement("div"); sub.className = "subsec";
      var h = document.createElement("h4"); h.textContent = sec.dict.title; sub.appendChild(h);
      var dictData = data[sec.dict.key];
      var dictGet = makeDictKV(sub, sec.dict, dictData);
      body.appendChild(sub);
      fieldGetters.push({{ dictKey: sec.dict.key, get: dictGet }});
    }}
    if (sec.dictObj) {{
      var sub2 = document.createElement("div"); sub2.className = "subsec";
      var h2 = document.createElement("h4"); h2.textContent = sec.dictObj.title; sub2.appendChild(h2);
      var dictObjData = data[sec.dictObj.key];
      var dictObjGet = makeDictObj(sub2, sec.dictObj, dictObjData);
      body.appendChild(sub2);
      fieldGetters.push({{ dictKey: sec.dictObj.key, get: dictObjGet }});
    }}
    box._save = function(out) {{
      var collected = {{}};
      fieldGetters.forEach(function(fg) {{
        if (fg.dictKey) collected[fg.dictKey] = fg.get();
        else collected[fg.fd.n] = fg.get();
      }});
      if (sec.type === "falsy_or_form") {{
        if (!collected.enabled) {{ setPath(out, sec.path, false); return; }}
      }}
      if (sec.path === "") {{
        // Top-level fields direkt mergen
        Object.keys(collected).forEach(function(k) {{ out[k] = collected[k]; }});
      }} else {{
        var existing = getPath(out, sec.path);
        if (existing && typeof existing === "object" && !Array.isArray(existing)) {{
          Object.assign(existing, collected);
        }} else {{
          setPath(out, sec.path, collected);
        }}
      }}
    }};
    return box;
  }}

  var CFG = null; var ROOT = document.getElementById("config-form"); var SECTION_NODES = [];
  function render(cfg) {{
    CFG = cfg;
    ROOT.innerHTML = "";
    SECTION_NODES = [];
    SCHEMA.forEach(function(sec) {{
      var node = renderSection(sec, cfg);
      ROOT.appendChild(node);
      SECTION_NODES.push({{ sec: sec, node: node }});
    }});
  }}

  function loadConfig() {{
    var msg = document.getElementById("msg"); msg.textContent = "Lade…"; msg.className = "";
    fetch("/api/config/form" + qs(), {{ credentials: "same-origin" }})
      .then(function(r) {{ if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }})
      .then(function(cfg) {{ render(cfg); msg.textContent = ""; }})
      .catch(function(e) {{ msg.textContent = "Fehler: " + e.message; msg.className = "error"; }});
  }}

  function gatherAndSave() {{
    var msg = document.getElementById("msg"); msg.textContent = ""; msg.className = "";
    var out = {{}};
    SECTION_NODES.forEach(function(s) {{ if (typeof s.node._save === "function") s.node._save(out); }});
    fetch("/api/config/form" + qs(), {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      credentials: "same-origin",
      body: JSON.stringify(out)
    }}).then(function(r) {{
      return r.json().then(function(data) {{
        if (r.ok) {{
          msg.innerHTML = "Gespeichert. <strong>Dienst neu starten</strong>, damit die Änderungen wirksam werden.";
          msg.className = "success";
          loadConfig();
        }} else {{
          msg.textContent = data.detail || data.error || "Fehler";
          msg.className = "error";
        }}
      }}).catch(function() {{
        if (r.ok) {{ msg.textContent = "Gespeichert."; msg.className = "success"; loadConfig(); }}
        else {{ msg.textContent = "Fehler " + r.status; msg.className = "error"; }}
      }});
    }}).catch(function(e) {{
      msg.textContent = "Netzwerk-Fehler: " + e.message; msg.className = "error";
    }});
  }}

  function restartService() {{
    var msg = document.getElementById("msg");
    if (!confirm("Dienst wirklich neu starten? Laufende Chats und Streams werden abgebrochen.")) return;
    var btn = document.getElementById("restart-btn");
    btn.disabled = true; msg.className = ""; msg.textContent = "Restart wird ausgelöst…";
    fetch("/api/restart" + qs(), {{ method: "POST", credentials: "same-origin" }})
      .then(function(r) {{ return r.json().then(function(d) {{ return {{ok: r.ok, d: d}}; }}); }})
      .then(function(res) {{
        if (res.ok) {{
          msg.className = "success";
          msg.textContent = (res.d && res.d.message) || "Restart ausgelöst. Seite wird in 4s neu geladen…";
          setTimeout(function() {{ window.location.reload(); }}, 4000);
        }} else {{
          msg.className = "error";
          msg.textContent = (res.d && (res.d.detail || res.d.error)) || "Restart fehlgeschlagen";
          btn.disabled = false;
        }}
      }})
      .catch(function(e) {{ msg.className = "error"; msg.textContent = "Fehler: " + e.message; btn.disabled = false; }});
  }}

  document.getElementById("save-btn").addEventListener("click", gatherAndSave);
  document.getElementById("reload-btn").addEventListener("click", loadConfig);
  document.getElementById("restart-btn").addEventListener("click", restartService);
  loadConfig();
}})();
</script>
{_THEME_JS}
</body></html>"""
    return HTMLResponse(html)


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    """Zeigt geplante Jobs an."""
    _require_token(request)
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    config = load_config()
    try:
        from miniassistant.scheduler import list_scheduled_jobs
        jobs = list_scheduled_jobs()
    except Exception:
        jobs = []
    rows = ""
    if not jobs:
        rows = '<tr><td colspan="5" style="text-align:center;color:var(--muted);font-style:italic;">Keine geplanten Jobs.</td></tr>'
    else:
        for j in jobs:
            trigger = j.get("trigger", "?")
            args = j.get("trigger_args") or {}
            jid = j.get("id", "")[:8]
            added = j.get("added_at", "")[:16].replace("T", " ")
            if trigger == "cron":
                when = f'{args.get("minute","*")} {args.get("hour","*")} {args.get("day","*")} {args.get("month","*")} {args.get("day_of_week","*")}'
            elif trigger == "date":
                when = args.get("run_date", "?")[:19].replace("T", " ")
            else:
                when = str(args)
            # Aufgabe zusammenbauen
            task_parts = []
            if j.get("watch"):
                # Watch-Jobs: sauber aus Metadaten anzeigen statt internem LLM-Prompt
                wcheck = _escape(j.get("watch_check") or "?")
                wmsg = _escape(j.get("watch_message") or "")
                wtimeout = _escape(j.get("watch_timeout") or "")
                wrecur = " · recurring" if j.get("watch_recurring") else ""
                task_parts.append(
                    f'<span style="font-size:0.85em;color:var(--muted);">Bedingung:</span> <code>{wcheck}</code>'
                )
                if wmsg:
                    task_parts.append(f'<span style="font-size:0.85em;color:var(--muted);">Nachricht:</span> {wmsg}')
                if wtimeout:
                    task_parts.append(f'<span style="font-size:0.85em;color:var(--muted);">Timeout:{wrecur}</span> {wtimeout}')
            else:
                if j.get("prompt"):
                    full_prompt = _escape(j["prompt"])
                    if len(j["prompt"]) > 80:
                        short = _escape(j["prompt"][:80])
                        task_parts.append(f'<details><summary>{short}…</summary><div class="prompt-full">{full_prompt}</div></details>')
                    else:
                        task_parts.append(full_prompt)
                if j.get("command"):
                    full_cmd = _escape(j["command"])
                    if len(j["command"]) > 60:
                        short_cmd = _escape(j["command"][:60])
                        task_parts.append(f'<details><summary><code>{short_cmd}…</code></summary><div class="prompt-full"><code>{full_cmd}</code></div></details>')
                    else:
                        task_parts.append(f'<code>{full_cmd}</code>')
            task = "<br>".join(task_parts) if task_parts else "?"
            client_str = j.get("client") or "alle"
            if j.get("room_id"):
                _nm = _resolve_target_name(config, room_id=j["room_id"]) or "?"
                client_str += f' <span style="font-size:0.85em;color:var(--muted);" title="{_escape(j["room_id"])}">📍 {_escape(_nm)}</span>'
            elif j.get("channel_id"):
                _nm = _resolve_target_name(config, channel_id=j["channel_id"]) or "?"
                client_str += f' <span style="font-size:0.85em;color:var(--muted);" title="{_escape(j["channel_id"])}">📍 {_escape(_nm)}</span>'
            client = client_str
            model = _escape(j.get("model") or "default")
            once_tag = ' <span style="color:var(--muted);font-size:0.8em;">einmalig</span>' if j.get("once") else ""
            watch_badge = ' <span class="badge-watch" title="Watch-Job">👁 Watch</span>' if j.get("watch") else ""
            full_id = _escape(j.get("id", ""))
            import html as _html
            raw_when = when if trigger == "cron" else ""
            edit_btn = (
                f'<button class="btn-edit" data-id="{full_id}"'
                f' data-prompt="{_html.escape(j.get("prompt") or "", quote=True)}"'
                f' data-when="{_html.escape(raw_when, quote=True)}"'
                f' data-model="{_html.escape(j.get("model") or "", quote=True)}"'
                f' data-room="{_html.escape(j.get("room_id") or "", quote=True)}"'
                f' data-channel="{_html.escape(j.get("channel_id") or "", quote=True)}"'
                f' title="Bearbeiten">&#9998;</button>'
            ) if (j.get("prompt") or trigger == "cron") and not j.get("watch") else ""
            rows += (
                f'<tr><td><code>{_escape(when)}</code>{once_tag}{watch_badge}</td><td>{task}</td><td>{client}</td><td>{model}</td><td>{added}</td>'
                f'<td><code>{jid}</code> <button class="btn-run" data-id="{full_id}" title="Jetzt ausführen">&#9654;</button>{edit_btn}<button class="btn-del" data-id="{full_id}" title="Loeschen">&#10005;</button></td></tr>'
            )
    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>Schedules – MiniAssistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="/favicon.ico">
    <style>
    {_COMMON_CSS}
    .sched-wrap {{ max-width: 900px; margin: 0 auto; padding: 1.2em 1em; }}
    .sched-header {{ display: flex; align-items: center; gap: 0.6em; margin-bottom: 1em; }}
    .sched-header img {{ width: 40px; height: 40px; border-radius: 8px; }}
    .sched-header h1 {{ margin: 0; font-size: 1.4em; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
    th {{ text-align: left; padding: 0.5em; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }}
    td {{ padding: 0.5em; border-bottom: 1px solid var(--border); }}
    code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.9em; }}
    .btn-del {{ background: none; border: 1.5px solid var(--danger); color: var(--danger); border-radius: 4px;
      cursor: pointer; padding: 0.15em 0.4em; font-size: 0.85em; line-height: 1; transition: background 0.15s; }}
    .btn-del:hover {{ background: var(--danger); color: #fff; }}
    .badge-watch {{ display: inline-block; font-size: 0.75em; background: #e8f0fe; color: #1a56db;
      border: 1px solid #a4c2f4; border-radius: 4px; padding: 0.05em 0.35em; margin-left: 0.3em; vertical-align: middle; }}
    details {{ cursor: pointer; }}
    details summary {{ display: inline; }}
    details .prompt-full {{ margin-top: 0.4em; white-space: pre-wrap; word-break: break-word; padding: 0.4em; background: var(--bg-secondary, #f5f5f5); border-radius: 4px; font-size: 0.9em; }}
    .btn-edit {{ background: none; border: 1.5px solid #888; color: #555; border-radius: 4px;
      cursor: pointer; padding: 0.15em 0.4em; font-size: 0.85em; line-height: 1; margin-right: 0.3em; transition: background 0.15s; }}
    .btn-edit:hover {{ background: #eee; }}
    .btn-run {{ background: none; border: 1.5px solid #2d8a4e; color: #2d8a4e; border-radius: 4px;
      cursor: pointer; padding: 0.15em 0.4em; font-size: 0.85em; line-height: 1; margin-right: 0.3em; transition: background 0.15s; }}
    .btn-run:hover {{ background: #2d8a4e; color: #fff; }}
    .btn-run:disabled {{ opacity: 0.5; cursor: default; }}
    .modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000; align-items:center; justify-content:center; }}
    .modal-overlay.open {{ display:flex; }}
    .modal-box {{ background:#fff; border-radius:8px; padding:1.5em; max-width:540px; width:90%; box-shadow:0 4px 24px rgba(0,0,0,0.18); }}
    .modal-box h2 {{ margin:0 0 0.8em; font-size:1.1em; }}
    .modal-box textarea {{ width:100%; box-sizing:border-box; min-height:120px; font-size:0.95em; padding:0.5em; border:1px solid #ccc; border-radius:4px; resize:vertical; }}
    .modal-box label {{ display:block; font-size:0.88em; color:var(--muted); margin-bottom:0.25em; margin-top:0.7em; }}
    .modal-box input[type=text], .modal-box select {{ width:100%; box-sizing:border-box; font-size:0.93em; padding:0.4em 0.5em; border:1px solid #ccc; border-radius:4px; }}
    .modal-actions {{ display:flex; gap:0.6em; justify-content:flex-end; margin-top:0.8em; }}
    </style>
    </head><body>
    <div class="sched-wrap">
      <div class="sched-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Geplante Jobs</h1>
      </div>
      <div class="card">
        <table>
          <thead><tr><th>Zeitplan</th><th>Aufgabe</th><th>Client</th><th>Modell</th><th>Erstellt</th><th>ID</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div style="margin-top:1em;">
        <a href="/{tq}" class="btn btn-outline">Startseite</a>
        <span class="text-muted" style="margin-left:1em;">Im Chat: <code>/schedules</code>, <code>/schedule remove &lt;ID&gt;</code></span>
      </div>
    </div>
    <div class="modal-overlay" id="editModal">
      <div class="modal-box">
        <h2>Job bearbeiten</h2>
        <div id="editWhenRow">
          <label for="editWhenText">Zeitplan (Cron, z.B. <code>30 7 * * *</code>)</label>
          <input type="text" id="editWhenText" placeholder="30 7 * * *" autocomplete="off">
        </div>
        <label for="editModelSelect">Modell</label>
        <select id="editModelSelect"><option value="">Standard</option></select>
        <label for="editTargetSelect">Ziel (Raum / Channel)</label>
        <select id="editTargetSelect">{_build_target_options(config)}</select>
        <label for="editPromptText">Prompt</label>
        <textarea id="editPromptText" rows="5"></textarea>
        <div class="modal-actions">
          <button class="btn btn-outline" id="editCancel">Abbrechen</button>
          <button class="btn" id="editSave">Speichern</button>
        </div>
      </div>
    </div>
    <script>
    var _editJobId = null;
    var token = new URLSearchParams(window.location.search).get("token") || "";
    // Modelle laden
    (function() {{
      var url = "/v1/models" + (token ? "?token=" + encodeURIComponent(token) : "");
      fetch(url).then(function(r) {{ return r.json(); }}).then(function(d) {{
        var sel = document.getElementById("editModelSelect");
        (d.data || d.models || []).forEach(function(m) {{
          var id = m.id || m;
          var opt = document.createElement("option"); opt.value = id; opt.textContent = id; sel.appendChild(opt);
        }});
      }}).catch(function() {{}});
    }})();
    document.querySelectorAll(".btn-edit").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        _editJobId = this.getAttribute("data-id");
        var when = this.getAttribute("data-when") || "";
        var model = this.getAttribute("data-model") || "";
        document.getElementById("editPromptText").value = this.getAttribute("data-prompt") || "";
        document.getElementById("editWhenText").value = when;
        document.getElementById("editWhenRow").style.display = when ? "" : "none";
        var sel = document.getElementById("editModelSelect");
        sel.value = model;
        if (sel.value !== model) {{
          // Modell noch nicht in Liste (z.B. Provider-Prefix) – als Option hinzufügen
          if (model) {{
            var opt = document.createElement("option"); opt.value = model; opt.textContent = model; sel.insertBefore(opt, sel.children[1]);
          }}
          sel.value = model;
        }}
        // Preselect target dropdown based on stored room/channel
        var roomVal = this.getAttribute("data-room") || "";
        var chanVal = this.getAttribute("data-channel") || "";
        var tgtSel = document.getElementById("editTargetSelect");
        if (roomVal) tgtSel.value = "matrix:" + roomVal;
        else if (chanVal) tgtSel.value = "discord:" + chanVal;
        else tgtSel.value = "";
        document.getElementById("editModal").classList.add("open");
        document.getElementById("editPromptText").focus();
      }});
    }});
    document.getElementById("editCancel").addEventListener("click", function() {{
      document.getElementById("editModal").classList.remove("open");
    }});
    document.getElementById("editSave").addEventListener("click", function() {{
      var newPrompt = document.getElementById("editPromptText").value.trim();
      var newWhen = document.getElementById("editWhenText").value.trim();
      var whenRow = document.getElementById("editWhenRow").style.display !== "none";
      var newModel = document.getElementById("editModelSelect").value;
      var tgt = document.getElementById("editTargetSelect").value || "";
      if (!newPrompt && !newModel && !(whenRow && newWhen) && tgt === "") {{ alert("Bitte mindestens ein Feld ausfüllen."); return; }}
      var payload = {{}};
      if (newPrompt) payload.prompt = newPrompt;
      if (newModel !== undefined) payload.model = newModel;
      if (whenRow && newWhen) payload.when = newWhen;
      // Target: explicit prefix-based split. Empty string clears both room_id and channel_id.
      if (tgt === "") {{
        payload.room_id = null; payload.channel_id = null; payload.client = null;
      }} else if (tgt.indexOf("matrix:") === 0) {{
        payload.room_id = tgt.substring(7); payload.channel_id = null; payload.client = "matrix";
      }} else if (tgt.indexOf("discord:") === 0) {{
        payload.channel_id = tgt.substring(8); payload.room_id = null; payload.client = "discord";
      }}
      fetch("/api/schedule/" + encodeURIComponent(_editJobId) + (token ? "?token=" + encodeURIComponent(token) : ""), {{
        method: "PATCH",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(payload)
      }}).then(function(r) {{
        if (r.ok) location.reload();
        else r.json().then(function(d) {{ alert(d.detail || d.error || "Fehler"); }});
      }});
    }});
    document.getElementById("editModal").addEventListener("click", function(e) {{
      if (e.target === this) this.classList.remove("open");
    }});
    document.querySelectorAll(".btn-run").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var id = this.getAttribute("data-id");
        var b = this;
        b.disabled = true; b.textContent = "…";
        fetch("/api/schedule/" + encodeURIComponent(id) + "/run" + (token ? "?token=" + encodeURIComponent(token) : ""), {{
          method: "POST"
        }}).then(function(r) {{
          if (r.ok) {{ b.textContent = "✓"; setTimeout(function() {{ b.disabled = false; b.innerHTML = "&#9654;"; }}, 2000); }}
          else r.json().then(function(d) {{ alert(d.detail || d.error || "Fehler"); b.disabled = false; b.innerHTML = "&#9654;"; }});
        }}).catch(function() {{ b.disabled = false; b.innerHTML = "&#9654;"; }});
      }});
    }});
    document.querySelectorAll(".btn-del").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var id = this.getAttribute("data-id");
        if (!confirm("Job " + id.slice(0,8) + " loeschen?")) return;
        fetch("/api/schedule/" + encodeURIComponent(id) + (token ? "?token=" + encodeURIComponent(token) : ""), {{
          method: "DELETE"
        }}).then(function(r) {{
          if (r.ok) location.reload();
          else r.json().then(function(d) {{ alert(d.detail || d.error || "Fehler"); }});
        }});
      }});
    }});
    </script>
    {_THEME_JS}
    </body></html>
    """
    return HTMLResponse(html)


def _escape(s: str) -> str:
    import html as _html
    return _html.escape(s, quote=True)


def _js_escape(s: str) -> str:
    """Für sichere Nutzung in JavaScript-Strings (z. B. in HTML)."""
    import json
    return json.dumps(s)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Chat-Seite: Markdown, Thinking optional in Spoiler, aufgehuebschtes Design."""
    from fastapi.responses import RedirectResponse
    _require_token(request)  # 401 wenn Token konfiguriert aber fehlt/ungültig (first-run: kein Token → durchgelassen)
    token = request.query_params.get("token", "")
    token_q = f"?token={token}" if token else ""
    show_onboarding = not _onboarding_complete()
    if show_onboarding:
        return RedirectResponse(url=f"/onboarding{token_q}", status_code=302)
    onboarding_link = ""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Chat – MiniAssistant</title>
    <link rel="icon" href="/favicon.ico">
    <script src="/static/marked.umd.js"></script>
    <script src="/static/purify.min.js"></script>
    <style>
    {_COMMON_CSS}
    .chat-wrap {{ display: flex; flex-direction: column; height: 100vh; max-width: min(900px, 98vw); margin: 0 auto; padding: 0.8em 1em; }}
    .chat-header {{ display: flex; align-items: center; gap: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); margin-bottom: 0.5em; flex-shrink: 0; }}
    .chat-header img {{ width: 32px; height: 32px; border-radius: 6px; }}
    .chat-header h1 {{ margin: 0; font-size: 1.2em; }}
    .chat-header .cmds {{ font-size: 0.75em; color: var(--muted); margin-left: auto; max-width: 50%; text-align: right; }}
    #log {{ flex: 1; overflow-y: auto; padding: 0.3em 0; }}
    .msg {{ padding: 0.5em 0; }}
    .msg + .msg {{ margin-top: 0.1em; }}
    .msg-sep {{ border: none; border-top: 1px solid var(--border); margin: 0.5em 0; }}
    .msg-role {{ font-weight: 700; font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.25em; }}
    .msg-role.user {{ color: var(--primary); text-align: right; }}
    .msg-role.assistant {{ color: var(--success); }}
    .msg.msg-user {{ text-align: right; padding-left: 3em; border-right: 3px solid var(--primary); padding-right: 0.7em; }}
    .msg.msg-assistant {{ padding-right: 0; border-left: 3px solid var(--success); padding-left: 0.7em; }}
    .msg .content {{ line-height: 1.6; }}
    .msg .content.markdown p {{ margin: 0.3em 0; }}
    .msg .content.markdown pre {{ background: var(--bg); padding: 0.6em; border-radius: 6px; overflow-x: auto; font-size: 0.9em; }}
    .msg .content.markdown code {{ background: var(--bg); padding: 0.15em 0.35em; border-radius: 4px; font-size: 0.9em; }}
    .msg .content.markdown pre code {{ background: none; padding: 0; }}
    details.thinking {{ margin-top: 0.3em; font-size: 0.88em; color: var(--muted); }}
    details.thinking summary {{ cursor: pointer; font-weight: 500; }}
    .thinking-placeholder {{ color: var(--muted); font-style: italic; }}
    .onboarding-notice {{ background: #fff9e6; padding: 0.6em 0.8em; border-radius: var(--radius); border-left: 4px solid var(--warning); font-size: 0.9em; margin-bottom: 0.5em; }}
    .chat-input {{ display: flex; gap: 0.5em; align-items: flex-end; padding-top: 0.6em; border-top: 1px solid var(--border); flex-shrink: 0; flex-wrap: wrap; }}
    .chat-input-row {{ display: flex; gap: 0.5em; align-items: flex-end; width: 100%; }}
    .chat-input textarea {{ flex: 1; padding: 0.55em 0.7em; border: 1.5px solid var(--border); border-radius: var(--radius);
                            font-family: inherit; font-size: 0.95em; resize: none; outline: none; min-height: 2.5em; max-height: 8em; transition: border-color 0.15s; }}
    .chat-input textarea:focus {{ border-color: var(--primary); }}
    .chat-input button {{ height: 2.5em; }}
    .chat-footer {{ font-size: 0.8em; color: var(--muted); padding-top: 0.3em; text-align: center; flex-shrink: 0; }}
    .btn-img-upload {{ background: none; border: 1.5px solid var(--border); border-radius: var(--radius); cursor: pointer; font-size: 1.2em; padding: 0.3em 0.5em; color: var(--muted); transition: border-color 0.15s, color 0.15s; height: 2.5em; }}
    .btn-img-upload:hover {{ border-color: var(--primary); color: var(--primary); }}
    #img-preview {{ display: none; gap: 0.4em; padding: 0.3em 0; width: 100%; flex-wrap: wrap; }}
    #img-preview .img-thumb {{ position: relative; display: inline-block; }}
    #img-preview .img-thumb img {{ max-height: 80px; max-width: 120px; border-radius: 6px; border: 1px solid var(--border); object-fit: cover; }}
    #img-preview .img-thumb .img-remove {{ position: absolute; top: -6px; right: -6px; background: var(--danger); color: #fff; border: none; border-radius: 50%; width: 18px; height: 18px; font-size: 12px; line-height: 18px; text-align: center; cursor: pointer; padding: 0; }}
    .msg .content img.chat-image, .msg .content .markdown img {{ max-width: min(100%, 520px); max-height: min(360px, 45vh); border-radius: 8px; margin: 0.4em 0; cursor: zoom-in; display: block; }}
    .msg.msg-user .user-images {{ display: flex; gap: 0.4em; flex-wrap: wrap; justify-content: flex-end; margin-bottom: 0.3em; }}
    .msg.msg-user .user-images img {{ max-height: 80px; max-width: 120px; border-radius: 6px; object-fit: cover; cursor: zoom-in; }}
    .chat-lightbox {{ position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:9999; display:flex; align-items:center; justify-content:center; cursor:zoom-out; }}
    .chat-lightbox img {{ max-width:95vw; max-height:95vh; object-fit:contain; border-radius:8px; box-shadow:0 0 40px rgba(0,0,0,0.5); }}
    .chat-input.drag-over {{ border-color: var(--primary); background: rgba(59,130,246,0.05); }}
    .thinking-live {{ white-space: pre-wrap; font-size: 0.88em; color: var(--muted); margin: 0.3em 0; }}
    .typing-dots {{ display: inline-flex; align-items: center; gap: 0.2em; margin: 0.3em 0; color: var(--muted); font-size: 1.2em; }}
    .typing-dots span {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: typing-bounce 0.6s ease-in-out infinite both; }}
    .typing-dots span:nth-child(2) {{ animation-delay: 0.15s; }}
    .typing-dots span:nth-child(3) {{ animation-delay: 0.3s; }}
    @keyframes typing-bounce {{ 0%, 80%, 100% {{ transform: scale(0.6); opacity: 0.5; }} 40% {{ transform: scale(1); opacity: 1; }} }}
    .processing-indicator {{ display: inline-flex; align-items: center; gap: 0.4em; margin: 0.3em 0; color: var(--muted); font-size: 0.88em; }}
    .processing-indicator .proc-dots {{ display: inline-flex; gap: 0.2em; align-items: center; }}
    .processing-indicator .proc-dots span {{ width: 5px; height: 5px; border-radius: 50%; background: currentColor; animation: typing-bounce 0.6s ease-in-out infinite both; }}
    .processing-indicator .proc-dots span:nth-child(2) {{ animation-delay: 0.15s; }}
    .processing-indicator .proc-dots span:nth-child(3) {{ animation-delay: 0.3s; }}
    </style>
    </head>
    <body>
    <div class="chat-wrap">
    <div class="chat-header">
      <img src="/static/miniassistant.png" alt="MiniAssistant">
      <h1>Chat</h1>
      <span id="track-badge" style="display:none;font-size:0.72em;background:var(--primary);color:#fff;padding:0.15em 0.5em;border-radius:10px;margin-left:0.2em">💾 wird gespeichert</span>
      <span class="cmds">/help · /model · /models · /new · /abort · /stop · /schedules · /schedule &lt;text&gt; · /webhook &lt;text&gt; · /dazu &lt;text&gt; · /auth — oder mit : statt /</span>
    </div>
    {"<div class=\"onboarding-notice\">Setup noch nicht abgeschlossen. <a href=\"/onboarding" + token_q + "\">Onboarding / Setup</a></div>" if show_onboarding else ""}
    <div id="log"></div>
    <form id="f" class="chat-input">
      <div id="img-preview"></div>
      <div class="chat-input-row">
        <button type="button" class="btn-img-upload" id="btn-img" title="Bild oder Dokument hochladen (PDF, DOCX, Text). Drag&amp;Drop / Ctrl+V">&#128247;</button>
        <input type="file" id="img-input" accept="image/*,.pdf,.docx,.txt,.md,.csv,.json,.xml,.log,.rst" multiple style="display:none;">
        <textarea id="msg" placeholder="Nachricht… (Enter = Senden, Shift+Enter = Zeile)" rows="2" autocomplete="off"></textarea>
        <button type="submit" class="btn btn-primary" id="btn-send">Senden</button>
        <button type="button" class="btn btn-outline" id="btn-cancel" style="display:none;">Abbrechen</button>
      </div>
    </form>
    <div class="chat-footer"><span id="no-save-hint" style="opacity:0.7;">Konversationen werden nicht gespeichert (Seite neu laden = neuer Chat).</span> {onboarding_link}<a href="/{token_q}" class="btn btn-outline" style="padding:0.3em 0.7em;font-size:0.85em;">Startseite</a></div>
    </div>
    <script>
    const _purifyConfig = {{ ADD_TAGS: ["audio"], ADD_ATTR: ["controls", "src", "autoplay"] }};
    function sanitize(html) {{ return DOMPurify.sanitize(html, _purifyConfig); }}
    const params = new URLSearchParams(window.location.search);
    const token = params.get("token") || "";
    const log = document.getElementById("log");
    const form = document.getElementById("f");
    const msgEl = document.getElementById("msg");
    msgEl.addEventListener("keydown", function(e) {{
      if (e.key === "Enter" && !e.shiftKey) {{ e.preventDefault(); form.requestSubmit(); }}
    }});
    /* Auto-resize textarea */
    msgEl.addEventListener("input", function() {{
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 200) + "px";
    }});
    let sessionId = sessionStorage.getItem("miniassistant_session") || "";
    let currentAbort = null;
    const btnSend = document.getElementById("btn-send");
    const btnCancel = document.getElementById("btn-cancel");
    btnCancel.addEventListener("click", function() {{
      if (currentAbort) {{ currentAbort.abort(); currentAbort = null; }}
    }});
    /* --- Bild-Upload --- */
    const imgInput = document.getElementById("img-input");
    const imgPreview = document.getElementById("img-preview");
    const btnImg = document.getElementById("btn-img");
    let pendingImages = []; /* {{data: base64, mime_type: string, name: string}} */
    let pendingDocuments = []; /* {{data: base64, mime_type: string, name: string}} */
    const MAX_IMG_SIZE = 20 * 1024 * 1024; /* 20MB */
    const MAX_DOC_SIZE = 25 * 1024 * 1024; /* 25MB */
    const ALLOWED_IMG_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];
    const DOC_EXTS = [".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".xml", ".log", ".rst"];
    function isDocument(f) {{
      if (f.type === "application/pdf") return true;
      if (f.type && f.type.startsWith("text/")) return true;
      if (f.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document") return true;
      if (f.type === "application/json" || f.type === "application/xml") return true;
      const lower = (f.name || "").toLowerCase();
      return DOC_EXTS.some(function(ext) {{ return lower.endsWith(ext); }});
    }}
    btnImg.addEventListener("click", function() {{ imgInput.click(); }});
    imgInput.addEventListener("change", function() {{ handleFiles(this.files); this.value = ""; }});
    function handleFiles(files) {{
      for (const f of files) {{
        const isImg = ALLOWED_IMG_TYPES.includes(f.type);
        const isDoc = !isImg && isDocument(f);
        if (!isImg && !isDoc) {{ alert("Dateityp nicht unterstuetzt: " + (f.type || f.name)); continue; }}
        const limit = isImg ? MAX_IMG_SIZE : MAX_DOC_SIZE;
        if (f.size > limit) {{ alert("Datei zu gross (max " + (limit / 1024 / 1024) + "MB): " + f.name); continue; }}
        const reader = new FileReader();
        reader.onload = (function(file, asImg) {{ return function(ev) {{
          const dataUrl = ev.target.result;
          const base64 = dataUrl.split(",")[1];
          const mime = file.type || (asImg ? "image/png" : "application/octet-stream");
          if (asImg) pendingImages.push({{ data: base64, mime_type: mime, name: file.name }});
          else pendingDocuments.push({{ data: base64, mime_type: mime, name: file.name }});
          renderPreview();
        }}; }})(f, isImg);
        reader.readAsDataURL(f);
      }}
    }}
    function renderPreview() {{
      imgPreview.innerHTML = "";
      if (!pendingImages.length && !pendingDocuments.length) {{ imgPreview.style.display = "none"; return; }}
      imgPreview.style.display = "flex";
      pendingImages.forEach(function(img, idx) {{
        const thumb = document.createElement("div");
        thumb.className = "img-thumb";
        const el = document.createElement("img");
        el.src = "data:" + img.mime_type + ";base64," + img.data;
        el.title = img.name || "Bild";
        thumb.appendChild(el);
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "img-remove";
        rm.textContent = "x";
        rm.addEventListener("click", function() {{ pendingImages.splice(idx, 1); renderPreview(); }});
        thumb.appendChild(rm);
        imgPreview.appendChild(thumb);
      }});
      pendingDocuments.forEach(function(doc, idx) {{
        const chip = document.createElement("div");
        chip.className = "img-thumb";
        chip.style.cssText = "display:flex;align-items:center;gap:0.4em;padding:0.4em 0.6em;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:0.85em";
        chip.innerHTML = "&#128196; " + (doc.name || "doc");
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "img-remove";
        rm.textContent = "x";
        rm.addEventListener("click", function() {{ pendingDocuments.splice(idx, 1); renderPreview(); }});
        chip.appendChild(rm);
        imgPreview.appendChild(chip);
      }});
    }}
    /* Drag & Drop */
    form.addEventListener("dragover", function(e) {{ e.preventDefault(); form.classList.add("drag-over"); }});
    form.addEventListener("dragleave", function(e) {{ e.preventDefault(); form.classList.remove("drag-over"); }});
    form.addEventListener("drop", function(e) {{
      e.preventDefault(); form.classList.remove("drag-over");
      if (e.dataTransfer && e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
    }});
    /* Ctrl+V / Paste */
    document.addEventListener("paste", function(e) {{
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      const imageFiles = [];
      for (const item of items) {{
        if (item.type && item.type.startsWith("image/")) {{
          const f = item.getAsFile();
          if (f) imageFiles.push(f);
        }}
      }}
      if (imageFiles.length) {{ e.preventDefault(); handleFiles(imageFiles); }}
    }});
    if (params.get("onboarding_saved") === "1") {{
      sessionStorage.removeItem("miniassistant_session");
      sessionId = "";
      const notice = document.createElement("div");
      notice.className = "onboarding-notice";
      notice.textContent = "Setup gespeichert. Neue Session gestartet.";
      log.appendChild(notice);
      if (history.replaceState) history.replaceState({{}}, "", "/chat" + (token ? "?token=" + encodeURIComponent(token) : ""));
    }}
    if (params.get("track") === "1" || params.get("resume_session")) {{
      document.getElementById("track-badge").style.display = "";
      document.getElementById("no-save-hint").style.display = "none";
    }}
    if (params.get("resume_session")) {{
      sessionId = params.get("resume_session");
      sessionStorage.setItem("miniassistant_session", sessionId);
      const resumeStem = params.get("resume_stem");
      if (resumeStem) {{
        const histUrl = "/api/chats/file?stem=" + encodeURIComponent(resumeStem) + (token ? "&token=" + encodeURIComponent(token) : "");
        fetch(histUrl).then(function(r) {{ return r.json(); }}).then(function(d) {{
          const exchanges = d.exchanges || [];
          if (exchanges.length) {{
            const wrap = document.createElement("div");
            wrap.style.cssText = "opacity:0.6;border-bottom:2px dashed var(--border);padding-bottom:0.8rem;margin-bottom:0.8rem;font-size:0.9em";
            const lbl = document.createElement("div");
            lbl.style.cssText = "font-size:0.75rem;color:var(--muted);margin-bottom:0.4rem";
            lbl.textContent = "Vorheriger Verlauf";
            wrap.appendChild(lbl);
            exchanges.forEach(function(ex) {{
              const uDiv = document.createElement("div");
              uDiv.innerHTML = "<b>Du:</b> " + (ex.user || "").replace(/</g,"&lt;");
              uDiv.style.marginBottom = "0.3rem";
              wrap.appendChild(uDiv);
              const aDiv = document.createElement("div");
              aDiv.className = "markdown";
              aDiv.innerHTML = typeof marked !== "undefined" ? sanitize(marked.parse(ex.assistant || "")) : (ex.assistant || "");
              aDiv.style.marginBottom = "0.6rem";
              wrap.appendChild(aDiv);
            }});
            log.appendChild(wrap);
            log.scrollTop = log.scrollHeight;
          }}
        }}).catch(function() {{}});
      }}
      if (history.replaceState) history.replaceState({{}}, "", "/chat" + (token ? "?token=" + encodeURIComponent(token) : ""));
    }}

    function escapeHtml(s) {{
      const div = document.createElement("div");
      div.textContent = s;
      return div.innerHTML;
    }}
    function addLog(role, text, isMarkdown) {{
      if (role === "Du" && log.children.length > 0) {{
        const sep = document.createElement("hr"); sep.className = "msg-sep"; log.appendChild(sep);
      }}
      const p = document.createElement("div");
      p.className = "msg " + (role === "Du" ? "msg-user" : "msg-assistant");
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role " + (role === "Du" ? "user" : "");
      roleDiv.textContent = role;
      p.appendChild(roleDiv);
      const content = document.createElement("div");
      content.className = "content" + (isMarkdown ? " markdown" : "");
      if (isMarkdown && typeof marked !== "undefined")
        content.innerHTML = sanitize(marked.parse(text || ""));
      else
        content.textContent = text || "";
      p.appendChild(content);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}
    function addAssistantLog(fullText, userRequest, thinkingText, contentText) {{
      const p = document.createElement("div");
      p.className = "msg msg-assistant";
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role assistant";
      roleDiv.textContent = "Assistant";
      p.appendChild(roleDiv);
      const wrap = document.createElement("div");
      wrap.className = "content";
      let thinking = thinkingText || "";
      let answer = contentText !== undefined && contentText !== null ? contentText : "";
      if (!thinking && !answer && fullText) {{
        if (fullText.startsWith("[Thinking]")) {{
          const idx = fullText.indexOf("\\n\\n", 10);
          if (idx > 0) {{ thinking = fullText.slice(10, idx).trim(); answer = fullText.slice(idx + 2).trim(); }}
          else {{ thinking = fullText.slice(10).trim(); answer = ""; }}
        }} else {{ answer = fullText; }}
      }}
      if (thinking) {{
        const details = document.createElement("details");
        details.className = "thinking";
        details.innerHTML = "<summary>Denkvorgang</summary><div style='white-space:pre-wrap;margin-top:0.3em;'>" + escapeHtml(thinking) + "</div>";
        wrap.appendChild(details);
      }}
      if (answer) {{
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? sanitize(marked.parse(answer)) : escapeHtml(answer);
        wrap.appendChild(md);
      }}
      p.appendChild(wrap);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}

    function showStreamContainer(userRequest) {{
      const p = document.createElement("div");
      p.className = "msg msg-assistant";
      p.id = "stream-container";
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role assistant";
      roleDiv.textContent = "Assistant";
      p.appendChild(roleDiv);
      const contentWrap = document.createElement("div");
      contentWrap.className = "content";
      contentWrap.innerHTML = "<div id='stream-typing' class='typing-dots'><span></span><span></span><span></span></div><div id='stream-thinking' class='thinking-live'></div><div id='stream-content' class='markdown' style='margin-top:0.3em;'></div>";
      p.appendChild(contentWrap);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
      form.querySelector("button[type=submit]").disabled = true;
    }}
    function finishStreamContainer(doneData) {{
      const container = document.getElementById("stream-container");
      if (!container) return;
      const typingEl = document.getElementById("stream-typing");
      if (typingEl) typingEl.remove();
      const processingEl = document.getElementById("stream-processing");
      if (processingEl) processingEl.remove();
      const thinkingEl = document.getElementById("stream-thinking");
      const contentEl = document.getElementById("stream-content");
      const wrap = container.querySelector(".content");
      if (!wrap) return;
      // doneData.content ist bereinigt (thinking/tool_call-XML entfernt) → bevorzugen
      // doneData.thinking enthält extrahierten Denktext (vLLM: aus Content, Ollama: native)
      var contentText  = (doneData && doneData.content)  ? doneData.content.trim()  : (contentEl  ? contentEl.textContent.trim()  : "");
      var thinkingText = (doneData && doneData.thinking) ? doneData.thinking.trim() : (thinkingEl ? thinkingEl.textContent.trim() : "");
      // Fallback: falls </think> noch im contentText steckt (vLLM hat <think> gefiltert aber
      // server-seitig nicht gesplittet), hier client-seitig splitten.
      if (!thinkingText && contentText.includes('</think>')) {{
        const thinkEnd = contentText.indexOf('</think>');
        thinkingText = contentText.slice(0, thinkEnd).trim();
        contentText  = contentText.slice(thinkEnd + 8).trim();
      }}
      if (thinkingEl) thinkingEl.remove();
      if (contentEl)  contentEl.remove();
      if (thinkingText && contentText) {{
        const details = document.createElement("details");
        details.className = "thinking";
        details.innerHTML = "<summary>Denkvorgang</summary><div style='white-space:pre-wrap;margin-top:0.3em;'>" + escapeHtml(thinkingText) + "</div>";
        wrap.appendChild(details);
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? sanitize(marked.parse(contentText)) : escapeHtml(contentText);
        wrap.appendChild(md);
      }} else if (contentText) {{
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? sanitize(marked.parse(contentText)) : escapeHtml(contentText);
        wrap.appendChild(md);
      }} else if (thinkingText) {{
        const note = document.createElement("p");
        note.style.cssText = "color:var(--muted);font-size:0.85em;font-style:italic;margin:0.2em 0 0.4em;";
        note.textContent = "(Kein separater Antworttext)";
        wrap.appendChild(note);
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? sanitize(marked.parse(thinkingText)) : escapeHtml(thinkingText);
        wrap.appendChild(md);
      }} else if (!doneData || !doneData.error) {{
        const empty = document.createElement("p");
        empty.style.cssText = "color:var(--muted);font-style:italic;";
        empty.textContent = "(Keine Antwort)";
        wrap.appendChild(empty);
      }}
      if (doneData && (doneData.debug_info || doneData._debug)) {{
        const d = document.createElement("details");
        d.className = "thinking";
        d.innerHTML = "<summary>Debug</summary><pre style='white-space:pre-wrap;font-size:11px;'>" + JSON.stringify(doneData.debug_info || doneData._debug, null, 2).replace(/</g, "&lt;") + "</pre>";
        wrap.appendChild(d);
      }}
      if (doneData && doneData.error) {{
        const err = document.createElement("p");
        err.style.color = "var(--danger)";
        err.textContent = "Fehler: " + doneData.error;
        wrap.appendChild(err);
      }}
      if (doneData && doneData.tps) {{
        const tpsEl = document.createElement("p");
        tpsEl.style.cssText = "color:var(--muted);font-size:0.75em;margin:0.3em 0 0;text-align:right;";
        const tpsVal = doneData.tps[0], tpsExact = doneData.tps[1];
        tpsEl.textContent = (tpsExact ? "" : "~") + Math.round(tpsVal) + " t/s";
        wrap.appendChild(tpsEl);
      }}
      container.id = "";
      log.scrollTop = log.scrollHeight;
      form.querySelector("button[type=submit]").disabled = false;
    }}
    /* Lightbox: Klick auf Bilder im Chat → Vollbild */
    log.addEventListener("click", function(e) {{
      const img = e.target.closest(".markdown img, .chat-image, .user-images img");
      if (!img || !img.src) return;
      const overlay = document.createElement("div");
      overlay.className = "chat-lightbox";
      overlay.innerHTML = '<img src="' + img.src.replace(/"/g, "&quot;") + '">';
      overlay.addEventListener("click", function() {{ overlay.remove(); }});
      function onKey(ev) {{ if (ev.key === "Escape") {{ overlay.remove(); document.removeEventListener("keydown", onKey); }} }}
      document.addEventListener("keydown", onKey);
      document.body.appendChild(overlay);
    }});
    form.addEventListener("submit", async (e) => {{
      e.preventDefault();
      const content = msgEl.value.trim();
      if (!content && !pendingImages.length && !pendingDocuments.length) return;
      msgEl.value = "";
      msgEl.style.height = "auto";
      /* User-Message mit Bild-Thumbnails anzeigen */
      const sentImages = pendingImages.slice();
      if (sentImages.length) {{
        const msgDiv = document.createElement("div");
        msgDiv.className = "msg msg-user";
        const roleDiv = document.createElement("div");
        roleDiv.className = "msg-role user";
        roleDiv.textContent = "Du";
        msgDiv.appendChild(roleDiv);
        const imagesDiv = document.createElement("div");
        imagesDiv.className = "user-images";
        sentImages.forEach(function(img) {{
          const el = document.createElement("img");
          el.src = "data:" + img.mime_type + ";base64," + img.data;
          el.title = img.name || "Bild";
          imagesDiv.appendChild(el);
        }});
        msgDiv.appendChild(imagesDiv);
        if (content) {{
          const contentDiv = document.createElement("div");
          contentDiv.className = "content";
          contentDiv.textContent = content;
          msgDiv.appendChild(contentDiv);
        }}
        if (log.children.length > 0) {{
          const sep = document.createElement("hr"); sep.className = "msg-sep"; log.appendChild(sep);
        }}
        log.appendChild(msgDiv);
        log.scrollTop = log.scrollHeight;
      }} else {{
        addLog("Du", content, false);
      }}
      showStreamContainer(content);
      btnSend.disabled = true; btnCancel.style.display = "";
      currentAbort = new AbortController();
      const sentDocs = pendingDocuments.slice();
      const url = "/api/chat/stream" + (token ? "?token=" + encodeURIComponent(token) : "");
      const body = {{ message: content || (sentDocs.length ? "(Dokument verarbeiten)" : "(Bild analysieren / bearbeiten)") }};
      if (sentImages.length) body.images = sentImages.map(function(img) {{ return {{ data: img.data, mime_type: img.mime_type }}; }});
      if (sentDocs.length) body.documents = sentDocs.map(function(d) {{ return {{ data: d.data, mime_type: d.mime_type, name: d.name }}; }});
      pendingImages = []; pendingDocuments = []; renderPreview();
      if (sessionId) body.session_id = sessionId;
      if (params.get("track") === "1") body.track = true;
      try {{
        const r = await fetch(url, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify(body), signal: currentAbort.signal }});
        if (!r.ok) {{ finishStreamContainer({{}}); addLog("Fehler", r.status + " " + (await r.text()), false); return; }}
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        let doneData = null;
        while (true) {{
          const {{ value, done }} = await reader.read();
          if (done) break;
          buf += decoder.decode(value, {{ stream: true }});
          const lines = buf.split("\\n");
          buf = lines.pop();
          for (const line of lines) {{
            if (!line.trim()) continue;
            try {{
              const data = JSON.parse(line);
              if (data.session_id) {{ sessionId = data.session_id; sessionStorage.setItem("miniassistant_session", sessionId); }}
              if (data.type === "thinking" && data.delta) {{
                const typingEl = document.getElementById("stream-typing");
                if (typingEl) typingEl.remove();
                const pi = document.getElementById("stream-processing");
                if (pi) pi.remove();
                const el = document.getElementById("stream-thinking");
                if (el) {{ el.textContent += data.delta; log.scrollTop = log.scrollHeight; }}
              }}
              if (data.type === "content" && data.delta) {{
                const typingEl = document.getElementById("stream-typing");
                if (typingEl) typingEl.remove();
                const pi = document.getElementById("stream-processing");
                if (pi) pi.remove();
                const el = document.getElementById("stream-content");
                if (el) {{ el.textContent += data.delta; log.scrollTop = log.scrollHeight; }}
              }}
              if (data.type === "tool_call") {{
                const typingEl = document.getElementById("stream-typing");
                if (typingEl) typingEl.remove();
                const toolNames = (data.tools || []).join(", ") || "…";
                const thinkEl = document.getElementById("stream-thinking");
                if (thinkEl) thinkEl.textContent += "\\n(Tool: " + toolNames + ")\\n";
                let pi = document.getElementById("stream-processing");
                if (!pi) {{
                  pi = document.createElement("div");
                  pi.id = "stream-processing";
                  pi.className = "processing-indicator";
                  const container = document.getElementById("stream-container");
                  if (container) container.querySelector(".content").appendChild(pi);
                }}
                pi.innerHTML = "⚙ " + toolNames + ' wird ausgefuehrt <span class="proc-dots"><span></span><span></span><span></span></span>';
                log.scrollTop = log.scrollHeight;
              }}
              if (data.type === "status" && data.message) {{
                const typingEl = document.getElementById("stream-typing");
                if (typingEl) typingEl.remove();
                const msg = data.message === "Connection failed, retrying…" ? "Verbindung fehlgeschlagen, versuche erneut …" : data.message;
                let pi = document.getElementById("stream-processing");
                if (!pi) {{
                  pi = document.createElement("div");
                  pi.id = "stream-processing";
                  pi.className = "processing-indicator";
                  const container = document.getElementById("stream-container");
                  if (container) container.querySelector(".content").appendChild(pi);
                }}
                pi.innerHTML = "⚙ " + msg + ' <span class="proc-dots"><span></span><span></span><span></span></span>';
                const thinkEl = document.getElementById("stream-thinking");
                if (thinkEl && !thinkEl.textContent.trimEnd().endsWith("(" + msg + ")")) thinkEl.textContent += "\\n(" + msg + ")\\n";
                log.scrollTop = log.scrollHeight;
              }}
              if (data.type === "done") {{ doneData = data; }}
            }} catch (err) {{ console.warn("Parse:", err); }}
          }}
        }}
        if (buf.trim()) try {{ const data = JSON.parse(buf); if (data.type === "done") doneData = data; }} catch (e) {{}}
        if (doneData && doneData.clear) {{
          // /new: Chat-Verlauf leeren, neue Session
          log.innerHTML = "";
          sessionStorage.removeItem("miniassistant_session");
          sessionId = doneData.session_id || "";
          if (sessionId) sessionStorage.setItem("miniassistant_session", sessionId);
          addAssistantLog(null, null, null, doneData.content || "Neue Session gestartet.");
          form.querySelector("button[type=submit]").disabled = false;
        }} else {{
          finishStreamContainer(doneData || {{}});
        }}
      }} catch (err) {{
        finishStreamContainer({{}});
        if (err.name !== "AbortError") addLog("Fehler", err.message, false);
        else addLog("System", "Abgebrochen.", false);
      }} finally {{
        btnSend.disabled = false; btnCancel.style.display = "none"; currentAbort = null;
      }}
    }});
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/api/ollama/models")
async def api_ollama_models(request: Request):
    """Liste der bei Ollama verfügbaren Modelle (für Ersteinrichtung/Config)."""
    _require_token(request)
    config = load_config()
    providers = config.get("providers") or {}
    default_prov = providers.get(next(iter(providers), "ollama")) or {}
    base_url = default_prov.get("base_url", "http://127.0.0.1:11434")
    try:
        from miniassistant.ollama_client import list_models
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(_chat_executor, lambda: list_models(base_url))
        names = [m.get("name") or m.get("model") or "" for m in (raw or []) if (m.get("name") or m.get("model"))]
        return JSONResponse({"models": names})
    except Exception as e:
        _log.warning("Ollama-Modelle konnten nicht geladen werden: %s", e)
        raise HTTPException(status_code=502, detail="Ollama-Modelle konnten nicht geladen werden")


@app.get("/api/config")
async def api_config(request: Request):
    """Config als JSON (mit Token)."""
    _require_token(request)
    return JSONResponse(load_config())


@app.post("/api/config")
async def api_config_save(request: Request):
    """Config als Roh-YAML speichern. Body = YAML-Text. Prüft vor dem Schreiben (YAML + Struktur).
    Maskierte Secrets (api_key, token, bot_token) werden durch die Originale ersetzt."""
    _require_token(request)
    body = await request.body()
    content = body.decode("utf-8", errors="replace")
    with _config_save_lock:
        # Maskierte Secrets durch Originale ersetzen
        if "****" in content:
            try:
                original = load_config_raw()
                import yaml
                orig_data = yaml.safe_load(original) or {}
                new_data = yaml.safe_load(content) or {}
                # Provider api_keys
                for prov_name, prov_cfg in (new_data.get("providers") or {}).items():
                    if isinstance(prov_cfg, dict) and prov_cfg.get("api_key") and "****" in str(prov_cfg.get("api_key", "")):
                        orig_key = ((orig_data.get("providers") or {}).get(prov_name) or {}).get("api_key")
                        if orig_key:
                            content = content.replace(str(prov_cfg["api_key"]), orig_key)
                # server.token
                new_srv = (new_data.get("server") or {}).get("token", "")
                if new_srv and "****" in str(new_srv):
                    orig_srv = (orig_data.get("server") or {}).get("token")
                    if orig_srv:
                        content = content.replace(str(new_srv), orig_srv)
                # chat_clients.matrix.token
                new_mx = ((new_data.get("chat_clients") or {}).get("matrix") or {}).get("token", "")
                if new_mx and "****" in str(new_mx):
                    orig_mx = ((orig_data.get("chat_clients") or {}).get("matrix") or {}).get("token")
                    if orig_mx:
                        content = content.replace(str(new_mx), orig_mx)
                # chat_clients.discord.bot_token
                new_dc = ((new_data.get("chat_clients") or {}).get("discord") or {}).get("bot_token", "")
                if new_dc and "****" in str(new_dc):
                    orig_dc = ((orig_data.get("chat_clients") or {}).get("discord") or {}).get("bot_token")
                    if orig_dc:
                        content = content.replace(str(new_dc), orig_dc)
                # github_token (top-level)
                new_gh = (new_data.get("github_token") or "")
                if new_gh and "****" in str(new_gh):
                    orig_gh = orig_data.get("github_token")
                    if orig_gh:
                        content = content.replace(str(new_gh), orig_gh)
                # raw_proxy.token
                new_rp = (new_data.get("raw_proxy") or {}).get("token", "")
                if new_rp and "****" in str(new_rp):
                    orig_rp = (orig_data.get("raw_proxy") or {}).get("token")
                    if orig_rp:
                        content = content.replace(str(new_rp), orig_rp)
                # email.password in accounts
                new_emails = (new_data.get("email") or {}).get("accounts") or {}
                orig_emails = (orig_data.get("email") or {}).get("accounts") or {}
                for acc_name, acc_cfg in new_emails.items():
                    if isinstance(acc_cfg, dict) and acc_cfg.get("password") and "****" in str(acc_cfg.get("password", "")):
                        orig_pass = (orig_emails.get(acc_name) or {}).get("password")
                        if orig_pass:
                            content = content.replace(str(acc_cfg["password"]), orig_pass)
            except Exception:
                pass
        ok, err = validate_config_raw(content)
        if not ok:
            raise HTTPException(status_code=400, detail=err)
        try:
            write_config_raw(content)
        except Exception as e:
            _log.error("Config konnte nicht gespeichert werden: %s", e)
            raise HTTPException(status_code=500, detail="Config konnte nicht gespeichert werden")
    return JSONResponse({"ok": True})


# ===== Form-basierte Config (/config) =================================
#
# /api/config/form GET   → liefert die Config als JSON; alle bekannten
#                          Secret-Felder werden geleert und durch ein paralleles
#                          Flag _<name>_set:bool ergänzt, damit die UI anzeigen
#                          kann ob ein Wert gesetzt ist.
# /api/config/form POST  → nimmt JSON, restauriert leere Secret-Felder aus dem
#                          aktuellen Stand, schreibt mit save_config().
#
# Secrets werden NIE als Klartext zurückgeschickt; ein leeres Feld bedeutet
# "Wert unverändert lassen". Providers sind hier readonly: was im POST-Body
# unter "providers" steht, wird verworfen — providers werden weiter via Raw-YAML
# editiert (zu viele Sonderfälle: type, options, model_options, …).

def _config_has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    return True


def _mask_config_for_form(cfg: dict[str, Any]) -> dict[str, Any]:
    """Deep-Copy + Secrets geleert. Pro Secret-Pfad ein Flag _<name>_set:bool."""
    import copy as _copy
    out = _copy.deepcopy(cfg)
    out.pop("_config_dir", None)
    # server.token
    srv = out.setdefault("server", {})
    srv["_token_set"] = _config_has_value(srv.get("token"))
    srv["token"] = ""
    # top-level github_token
    out["_github_token_set"] = _config_has_value(out.get("github_token"))
    out["github_token"] = ""
    # raw_proxy.token
    rp = out.setdefault("raw_proxy", {})
    rp["_token_set"] = _config_has_value(rp.get("token"))
    rp["token"] = ""
    # chat_clients.matrix.token / discord.bot_token
    cc = out.get("chat_clients") or {}
    if isinstance(cc, dict):
        mx = cc.get("matrix")
        if isinstance(mx, dict):
            mx["_token_set"] = _config_has_value(mx.get("token"))
            mx["token"] = ""
        dc = cc.get("discord")
        if isinstance(dc, dict):
            dc["_bot_token_set"] = _config_has_value(dc.get("bot_token"))
            dc["bot_token"] = ""
    # providers.*.api_key (read-only display, aber Klartext nie ausliefern)
    provs = out.get("providers") or {}
    if isinstance(provs, dict):
        for _pn, pv in provs.items():
            if isinstance(pv, dict):
                pv["_api_key_set"] = _config_has_value(pv.get("api_key"))
                pv["api_key"] = ""
    # email.accounts.*.password
    em = out.get("email") or {}
    if isinstance(em, dict):
        accs = em.get("accounts") or {}
        if isinstance(accs, dict):
            for _an, av in accs.items():
                if isinstance(av, dict):
                    av["_password_set"] = _config_has_value(av.get("password"))
                    av["password"] = ""
    return out


def _strip_meta_fields(obj: Any) -> Any:
    """Entfernt rekursiv alle Keys mit Unterstrich-Prefix (UI-Hilfsfelder)."""
    if isinstance(obj, dict):
        return {k: _strip_meta_fields(v) for k, v in obj.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, list):
        return [_strip_meta_fields(v) for v in obj]
    return obj


def _restore_config_secrets(new_cfg: dict[str, Any], original: dict[str, Any]) -> None:
    """Wenn ein Secret-Feld im neuen Config leer ist, Original übernehmen.
    Mutiert new_cfg in-place."""
    def _restore_at(target: dict, source: dict, path: list[str]) -> None:
        n = target
        o = source
        for p in path[:-1]:
            if not isinstance(n, dict) or p not in n or not isinstance(n[p], dict):
                return
            if not isinstance(o, dict) or p not in o or not isinstance(o[p], dict):
                return
            n = n[p]
            o = o[p]
        if not isinstance(n, dict) or not isinstance(o, dict):
            return
        last = path[-1]
        new_v = n.get(last)
        if not _config_has_value(new_v):
            orig_v = o.get(last)
            if _config_has_value(orig_v):
                n[last] = orig_v

    for path in (
        ["server", "token"],
        ["github_token"],
        ["raw_proxy", "token"],
        ["chat_clients", "matrix", "token"],
        ["chat_clients", "discord", "bot_token"],
    ):
        _restore_at(new_cfg, original, path)
    # providers.*.api_key (auch wenn UI sie nicht editiert: defensiv restaurieren)
    new_provs = new_cfg.get("providers") or {}
    orig_provs = original.get("providers") or {}
    if isinstance(new_provs, dict) and isinstance(orig_provs, dict):
        for pn, pv in new_provs.items():
            if not isinstance(pv, dict):
                continue
            if not _config_has_value(pv.get("api_key")):
                orig_pv = orig_provs.get(pn)
                if isinstance(orig_pv, dict) and _config_has_value(orig_pv.get("api_key")):
                    pv["api_key"] = orig_pv["api_key"]
    # email.accounts.*.password
    new_em = new_cfg.get("email") or {}
    orig_em = original.get("email") or {}
    if isinstance(new_em, dict) and isinstance(orig_em, dict):
        new_accs = new_em.get("accounts") or {}
        orig_accs = orig_em.get("accounts") or {}
        if isinstance(new_accs, dict) and isinstance(orig_accs, dict):
            for an, av in new_accs.items():
                if not isinstance(av, dict):
                    continue
                if not _config_has_value(av.get("password")):
                    orig_av = orig_accs.get(an)
                    if isinstance(orig_av, dict) and _config_has_value(orig_av.get("password")):
                        av["password"] = orig_av["password"]


@app.get("/api/config/form")
async def api_config_form_get(request: Request):
    """Config für Form-Editor: Secrets geleert + _<name>_set Flags."""
    _require_token(request)
    cfg = load_config()
    return JSONResponse(_mask_config_for_form(cfg))


@app.get("/api/config/reveal")
async def api_config_reveal(request: Request):
    """Auf Anfrage einen Secret-Wert zurückgeben (für Eye-Toggle). Whitelisted Pfade."""
    _require_token(request)
    path = (request.query_params.get("path") or "").strip()
    if not path or len(path) > 200:
        raise HTTPException(status_code=400, detail="path required")
    parts = path.split(".")
    allowed = False
    if path in ("server.token", "github_token", "raw_proxy.token",
                "chat_clients.matrix.token", "chat_clients.discord.bot_token"):
        allowed = True
    elif len(parts) == 3 and parts[0] == "providers" and parts[2] == "api_key":
        allowed = True
    elif len(parts) == 4 and parts[0] == "email" and parts[1] == "accounts" and parts[3] == "password":
        allowed = True
    if not allowed:
        raise HTTPException(status_code=400, detail="path not allowed")
    cfg = load_config()
    cur = cfg
    for p in parts:
        if not isinstance(cur, dict):
            return JSONResponse({"value": ""})
        cur = cur.get(p)
        if cur is None:
            return JSONResponse({"value": ""})
    return JSONResponse({"value": "" if cur is None else str(cur)})


def _deep_merge_form(original: dict, new: dict) -> dict:
    """Deep-merge `new` into `original` für Form-Save.
    Regeln:
      - dicts: rekursiv mergen (neue Keys überschreiben, fehlende behalten)
      - lists: REPLACE (sonst kann User nie etwas aus einer Liste entfernen)
      - andere Typen: REPLACE
    Damit bleibt onboarding_complete + nicht-form-erfasste Sub-Felder erhalten,
    aber Listen-Edits (tools_allow uncheck etc.) greifen normal.
    """
    out = dict(original)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_form(out[k], v)
        else:
            out[k] = v
    return out


@app.post("/api/config/form")
async def api_config_form_save(request: Request):
    """Form-Save: deep-merge in Original um nicht-form-erfasste Felder (onboarding_complete,
    room_settings sub-keys etc.) nicht zu verlieren. providers read-only. Secrets aus Original."""
    _require_token(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON-Objekt erwartet")
    new_cfg = _strip_meta_fields(body)
    if not isinstance(new_cfg, dict):
        raise HTTPException(status_code=400, detail="Ungültiger Body")
    with _config_save_lock:
        original = load_config()
        # _config_dir nicht persistieren (wird beim Laden gesetzt)
        original_copy = {k: v for k, v in original.items() if k != "_config_dir"}
        # Deep-merge: erhält Felder die Form nicht kennt (onboarding_complete, room_settings details, …)
        merged = _deep_merge_form(original_copy, new_cfg)
        # providers: read-only — Original immer behalten, egal was die UI schickt
        merged["providers"] = original_copy.get("providers") or {}
        # leere Secrets aus Original übernehmen
        _restore_config_secrets(merged, original_copy)
        try:
            save_config(merged)
        except Exception as e:
            _log.error("Config-Form konnte nicht gespeichert werden: %s", e)
            raise HTTPException(status_code=500, detail=f"Config konnte nicht gespeichert werden: {e}")
    return JSONResponse({"ok": True})


@app.get("/api/token")
async def api_show_token(request: Request):
    """Token anzeigen (nur wenn bereits gesetzt; sonst 204). Erfordert gültiges Token."""
    _require_token(request)
    cfg = load_config()
    t = (cfg.get("server") or {}).get("token")
    if not t:
        from fastapi.responses import Response
        return Response(status_code=204)
    return JSONResponse({"token": ensure_token(cfg)})


@app.post("/api/title")
async def api_generate_title(request: Request):
    """Generiert einen kurzen Chat-Titel via LLM. POST { message } -> { title }."""
    _require_token(request)
    body = await request.json()
    message = (body.get("message") or "").strip()[:300]
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    import asyncio
    from miniassistant.ollama_client import chat as _oc, get_base_url_for_model, get_api_key_for_model
    config = load_config()
    project_dir = getattr(request.app.state, "project_dir", None)
    model = resolve_model(config, None) or ""
    if not model:
        return JSONResponse({"title": message[:50]})
    try:
        base_url = get_base_url_for_model(config, model)
        api_key = get_api_key_for_model(config, model)
        prompt = (f"Create a short title (max 5 words, same language as the message, "
                  f"no quotes, no punctuation at end) for a conversation starting with:\n{message}\n"
                  f"Reply with ONLY the title, nothing else.")
        loop = asyncio.get_event_loop()
        executor = _chat_executor
        resp = await loop.run_in_executor(
            executor, lambda: _oc(base_url, [{"role": "user", "content": prompt}],
                               model=model, api_key=api_key, timeout=20.0, num_ctx=512))
        title = (resp.get("message", {}).get("content") or "").strip().strip('"').strip("'")
        if len(title) > 50:
            title = title[:50] + "…"
        return JSONResponse({"title": title or message[:50]})
    except Exception:
        return JSONResponse({"title": message[:50]})


@app.post("/api/auth/{platform}")
async def api_auth_platform(request: Request, platform: str):
    """Auth: Code einlösen und Nutzer freischalten. Plattform: matrix, discord. Erfordert gültiges Token."""
    _require_token(request)
    if platform not in ("matrix", "discord"):
        raise HTTPException(status_code=400, detail=f"Unbekannte Plattform: {platform}")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body:
        raise HTTPException(status_code=400, detail="JSON body with 'code' required")
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    try:
        from miniassistant.chat_auth import consume_code
        project_dir = getattr(request.app.state, "project_dir", None)
        cfg = load_config(project_dir)
        config_dir = (cfg.get("_config_dir") or "").strip() or None
        # Rate-Limit pro Aufrufer-IP, sonst sperrt 5 Fehlversuche alle Web-Auth-Versuche.
        _rk_ip = (request.client.host if request.client else "unknown")
        result = consume_code(code, config_dir=config_dir, rate_key=_rk_ip)
        if result:
            plat, uid = result
            return JSONResponse({"ok": True, "platform": plat, "user_id": uid})
        return JSONResponse({"ok": False, "detail": "Code nicht gefunden (bereits eingelöst oder abgelaufen?). Im Matrix-/Discord-Chat einen neuen Code anfordern."}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)


@app.patch("/api/schedule/{job_id}")
async def api_schedule_update(request: Request, job_id: str):
    """Schedule-Felder aktualisieren: prompt, model, when, room_id, channel_id, client.
    Felder die NICHT im Body sind = unverändert. Feld = null = löschen."""
    _require_token(request)
    body = await request.json()
    new_prompt = (body.get("prompt") or "").strip() or None
    new_model = body.get("model")
    new_when = (body.get("when") or "").strip() or None
    def _opt(key: str):
        if key not in body:
            return ...
        v = body[key]
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return None
    new_room = _opt("room_id")
    new_channel = _opt("channel_id")
    new_client = _opt("client")
    if (new_prompt is None and new_model is None and not new_when
            and new_room is ... and new_channel is ... and new_client is ...):
        raise HTTPException(status_code=400, detail="Mindestens ein Feld erforderlich")
    try:
        from miniassistant.scheduler import update_schedule_prompt
        ok, msg = update_schedule_prompt(
            job_id, new_prompt,
            new_model=new_model, new_when=new_when,
            new_room_id=new_room, new_channel_id=new_channel, new_client=new_client,
        )
        if ok:
            return JSONResponse({"ok": True, "message": msg})
        raise HTTPException(status_code=404, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/schedule/{job_id}")
async def api_schedule_delete(request: Request, job_id: str):
    """Schedule-Job loeschen. Token erforderlich."""
    _require_token(request)
    try:
        from miniassistant.scheduler import remove_scheduled_job
        ok, msg = remove_scheduled_job(job_id)
        if ok:
            return JSONResponse({"ok": True, "removed": msg})
        raise HTTPException(status_code=404, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/schedule/{job_id}/run")
async def api_schedule_run(request: Request, job_id: str):
    """Führt einen Schedule-Job sofort aus (fire-and-forget). Token erforderlich."""
    _require_token(request)
    try:
        import json as _json
        from miniassistant.scheduler import list_scheduled_jobs, _run_scheduled_job
        jobs = list_scheduled_jobs()
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job nicht gefunden")
        job_data: dict = {}
        for key in ("command", "prompt", "client", "model", "room_id", "channel_id"):
            if job.get(key):
                job_data[key] = job[key]
        if job.get("once"):
            job_data["once"] = True
        job_data_json = _json.dumps(job_data, ensure_ascii=False)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(_chat_executor, _run_scheduled_job, job_id, job_data_json)
        return JSONResponse({"ok": True, "message": f"Job {job_id[:8]} gestartet"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/notify")
async def api_notify(request: Request):
    """Benachrichtigung an Chat-Clients senden. Body: { message, client? }. Token erforderlich."""
    _require_token(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    client = body.get("client")  # None = alle, "matrix", "discord"
    if client and client not in ("matrix", "discord"):
        raise HTTPException(status_code=400, detail="client muss 'matrix', 'discord' oder leer sein")
    from miniassistant.notify import send_notification
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(_chat_executor, lambda: send_notification(message, client=client))
    return JSONResponse(results)


def _get_chats_dir(config: dict) -> Path:
    if config.get("chats_dir"):
        return Path(config["chats_dir"]).expanduser()
    return config_path().parent / "chats"


def _generate_title_bg(session: dict, user_msg: str) -> None:
    """Hintergrundthread: generiert per LLM einen kurzen Titel und schreibt ihn ins JSON."""
    import threading, json as _json
    def _do():
        try:
            from miniassistant.ollama_client import chat as _oc, get_base_url_for_model, get_api_key_for_model
            config = session["config"]
            model = session.get("model") or resolve_model(config, None) or ""
            if not model or not session.get("_chat_file"):
                return
            base_url = get_base_url_for_model(config, model)
            api_key = get_api_key_for_model(config, model)
            prompt = (f"Erstelle einen kurzen Titel (max 5 Wörter, keine Anführungszeichen) "
                      f"für dieses Gespräch.\nErste Nachricht: {user_msg[:300]}\n"
                      f"Antworte NUR mit dem Titel, keine Erklärung.")
            resp = _oc(base_url, [{"role": "user", "content": prompt}],
                       model=model, api_key=api_key, timeout=20.0, num_ctx=512)
            title = (resp.get("message", {}).get("content") or "").strip().strip('"').strip("'")
            if len(title) > 50:
                title = title[:50] + "…"
            if title:
                fpath = Path(session["_chat_file"])
                if fpath.exists():
                    data = _json.loads(fpath.read_text(encoding="utf-8"))
                    data["title"] = title
                    fpath.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _save_chat_to_file(session: dict, user_msg: str, assistant_msg: str) -> None:
    """Speichert Exchange als JSON. Einzige Datei pro Chat: {stem}.json"""
    import json as _json
    chats_dir = _get_chats_dir(session["config"])
    chats_dir.mkdir(parents=True, exist_ok=True)
    is_first = "_chat_file" not in session
    if is_first:
        now = datetime.datetime.now()
        stem = now.strftime("%Y-%m-%d_%H%M%S")
        fpath = chats_dir / (stem + ".json")
        data = {
            "title": user_msg[:60].replace("\n", " ").strip(),
            "model": session.get("model") or "",
            "created": now.isoformat(),
            "messages": [],
            "exchanges": [],
        }
        session["_chat_file"] = str(fpath)
        session["_chat_stem"] = stem
    else:
        fpath = Path(session["_chat_file"])
        data = _json.loads(fpath.read_text(encoding="utf-8"))
    data["messages"] = session.get("messages", [])
    data["exchanges"] = data.get("exchanges", []) + [{"user": user_msg, "assistant": assistant_msg}]
    fpath.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if is_first:
        _generate_title_bg(session, user_msg)


def _chat_stream_generator(session_id: str, session: dict, message: str, images: list | None = None):
    """Sync generator: NDJSON-Zeilen mit type thinking | content | tool_call | done; fügt session_id hinzu."""
    lock = _get_session_lock(session_id)
    if not lock.acquire(timeout=5.0):
        import json as _json
        yield _json.dumps({"type": "done", "session_id": session_id, "content": "", "error": "Modell arbeitet noch — warte bis die Antwort fertig ist, oder sende /abort zum Abbrechen."}, ensure_ascii=False) + "\n"
        return
    try:
        yield from _chat_stream_generator_locked(session_id, session, message, images=images)
    finally:
        lock.release()


def _chat_stream_generator_locked(session_id: str, session: dict, message: str, images: list | None = None):
    """Interner Generator (mit Session-Lock gehalten)."""
    import json as _json
    from miniassistant.secret_mask import mask_stream_events as _mask_stream_events
    for ev in _mask_stream_events(chat_round_stream(
        config=session["config"],
        messages=session["messages"],
        system_prompt=session["system_prompt"],
        model=session.get("model") or resolve_model(session["config"], None) or "",
        user_content=message,
        project_dir=session.get("project_dir"),
        images=images,
    ), session["config"]):
        out = dict(ev, session_id=session_id)
        if ev.get("type") == "done":
            _done_msgs = ev.get("new_messages", session["messages"])
            # Bilder aus Messages entfernen (base64-Daten verschwenden Kontext-Platz)
            from miniassistant.documents import strip_document_blocks as _strip_docs
            for _msg in _done_msgs:
                if _msg.get("images"):
                    del _msg["images"]
                    if _msg.get("role") == "user" and "[Bild]" not in (_msg.get("content") or ""):
                        _msg["content"] = "[Bild angehängt] " + (_msg.get("content") or "")
                if _msg.get("role") == "user" and "<doc " in (_msg.get("content") or ""):
                    _msg["content"] = _strip_docs(_msg["content"])
            session["messages"] = _done_msgs
            _sessions[session_id] = session
            # Memory + Chat-History: nur bei gespeicherten Chats (track=1)
            from miniassistant.scheduler import _SILENT_SENTINELS
            done_content = (ev.get("content") or "").strip()
            if done_content in _SILENT_SENTINELS:
                out["content"] = ""
                done_content = ""
            if done_content and message.strip() and session.get("_track_chat"):
                try:
                    from miniassistant.memory import append_exchange
                    append_exchange(message, done_content, project_dir=session.get("project_dir"))
                except Exception:
                    pass
                try:
                    _save_chat_to_file(session, message, done_content)
                except Exception:
                    pass
        yield _json.dumps(out, ensure_ascii=False) + "\n"


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """Chat-Stream: POST { message, optional session_id, optional track } -> NDJSON."""
    _require_token(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    _has_attachments = bool(body.get("images")) or bool(body.get("documents"))
    if not message and not _has_attachments:
        raise HTTPException(status_code=400, detail="message required")
    requested_sid = body.get("session_id") or ""
    if requested_sid and requested_sid in _sessions:
        session = _sessions[requested_sid]
        session_id = requested_sid
    else:
        restore_msgs = body.get("restore_messages")
        # Client sendet session_id + restore_messages → Session nach Server-Neustart wiederherstellen
        if requested_sid and restore_msgs and isinstance(restore_msgs, list):
            session_id = requested_sid
        else:
            session_id = str(uuid.uuid4())
        project_dir = getattr(request.app.state, "project_dir", None)
        session = create_session(None, project_dir)
        if restore_msgs and isinstance(restore_msgs, list):
            # Schema-Whitelist: nur user/assistant-Turns mit string content. Verhindert
            # injizierte tool_call/tool-Felder die falsche Tool-Outputs in die Historie schmuggeln.
            _MAX_RESTORE = 200
            _MAX_CONTENT = 50_000
            _allowed = {"user", "assistant"}
            cleaned: list[dict] = []
            for m in restore_msgs[:_MAX_RESTORE]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content")
                if role not in _allowed or not isinstance(content, str):
                    continue
                cleaned.append({"role": role, "content": content[:_MAX_CONTENT]})
            session["messages"] = cleaned
        _sessions[session_id] = session
    if body.get("track"):
        session["_track_chat"] = True

    # Modell aus Body lesen und in Session setzen
    requested_model = body.get("model")
    if requested_model:
        session["model"] = requested_model
    
    _session_last_access[session_id] = time.time()

    # Cancellation-User-ID setzen (damit chat_round_stream bei Disconnect abbrechen kann)
    # chat_context: immer Web — handle_user_input persistiert nur bei _track_chat
    session.setdefault("chat_context", {})["platform"] = "web"
    session["chat_context"]["user_id"] = f"web:{session_id}"
    session["config"].setdefault("_chat_context", {})["user_id"] = f"web:{session_id}"
    session["config"]["_chat_context"]["platform"] = "web"
    # Slot-Cache nur bei track=true (gespeicherte Chats) — sonst lohnt's nicht
    if session.get("_track_chat"):
        from miniassistant.slot_cache import derive_conv_id as _sc_derive
        _conv = _sc_derive("web", session_id=session_id)
        if _conv:
            session["config"]["_chat_context"]["conv_id"] = _conv
            session["config"]["_chat_context"]["slot_cache_endpoint"] = "web"
    else:
        session["config"]["_chat_context"].pop("conv_id", None)

    # Lokale Tools: Client übergibt Liste der Tools die er selbst ausführt
    _local_tools = body.get("local_tools")
    if _local_tools and isinstance(_local_tools, list):
        session["config"]["_client_tools"] = [str(t) for t in _local_tools]
        session["config"]["_tool_request_hook"] = _make_tool_hook(session_id)
    else:
        session["config"].pop("_client_tools", None)
        session["config"].pop("_tool_request_hook", None)

    # Bilder aus Body extrahieren und validieren
    _raw_images = body.get("images")
    _chat_images = None
    if _raw_images and isinstance(_raw_images, list):
        _chat_images = []
        for _img in _raw_images[:10]:  # Max 10 Bilder
            if isinstance(_img, dict) and _img.get("data") and _img.get("mime_type"):
                _mime = str(_img["mime_type"])
                if _mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    _chat_images.append({"data": str(_img["data"]), "mime_type": _mime})
        if not _chat_images:
            _chat_images = None

    # Dokumente aus Body extrahieren (PDF/DOCX/Text) → in message prependen + Seiten-PNGs zu Bildern
    _raw_docs = body.get("documents")
    if _raw_docs and isinstance(_raw_docs, list):
        from miniassistant.documents import is_supported as _doc_supported, extract_document as _doc_extract, format_document_block as _fmt_doc
        import base64 as _b64
        _doc_max = int((session.get("config") or {}).get("doc_max_chars") or 200000)
        _pages_max = int((session.get("config") or {}).get("doc_max_pages_render") or 10)
        _doc_blocks: list[str] = []
        for _d in _raw_docs[:5]:  # Max 5 Dokumente pro Nachricht
            if not isinstance(_d, dict) or not _d.get("data"):
                continue
            _name = str(_d.get("name") or "anhang")
            _mime = str(_d.get("mime_type") or "")
            if not _doc_supported(_mime, _name):
                continue
            try:
                _bytes = _b64.b64decode(_d["data"])
            except Exception:
                continue
            _doc = _doc_extract(_bytes, _name, _mime, max_chars=_doc_max, max_pages_render=_pages_max)
            if _doc.get("error"):
                _log.warning("Web: Dokument-Extraktion fehlgeschlagen %s: %s", _name, _doc["error"])
                continue
            _block = _fmt_doc(_doc)
            if _block:
                _doc_blocks.append(_block)
            if _doc.get("images"):
                if _chat_images is None:
                    _chat_images = []
                _chat_images.extend(_doc["images"])
        if _doc_blocks:
            message = "\n\n".join(_doc_blocks) + "\n\n" + (message or "Bitte verarbeite das Dokument.")

    # Abbruch-Befehle VOR dem Session-Lock abfangen (funktionieren auch während Model läuft)
    _cmd_lower = message.strip().lower()
    if _cmd_lower.startswith(":") and len(_cmd_lower) > 1 and not _cmd_lower[1:2].isspace():
        _cmd_lower = "/" + _cmd_lower[1:]
    if _cmd_lower in ("/abort", "/stop", "/abbruch"):
        from miniassistant.cancellation import request_cancel
        cancel_key = f"web:{session_id}"
        level = "stop" if _cmd_lower == "/stop" else "abort"
        request_cancel(cancel_key, level)
        _log.info("Web: %s (session %s) — Cancellation angefordert (%s)", message, session_id, level)
        import json as _json
        reply = "⏹ Verarbeitung wird abgebrochen…" if level == "abort" else "⏸ Verarbeitung wird nach aktuellem Schritt gestoppt…"
        one = _json.dumps({"type": "done", "session_id": session_id, "content": reply}, ensure_ascii=False) + "\n"
        return StreamingResponse(iter([one]), media_type="application/x-ndjson")

    if is_chat_command(message) or body.get("model"):
        _msg_lower = _cmd_lower
        is_new = _msg_lower == "/new"
        is_model_switch = _msg_lower.startswith("/model ") or body.get("model")
        # Run in threadpool to avoid blocking event loop during model pulls
        loop = asyncio.get_event_loop()
        executor = _chat_executor
        result = await loop.run_in_executor(executor, lambda: handle_user_input(session, message))
        session = result[1]
        _sessions[session_id] = session
        thinking = result[3] if len(result) > 3 else None
        # content (result[4]) ist bei Befehlen None – dann auf result[0] (Antworttext) zurückfallen
        content = (result[4] if len(result) > 4 else None) or result[0]
        from miniassistant.scheduler import _SILENT_SENTINELS
        if (content or "").strip() in _SILENT_SENTINELS:
            content = ""
        import json as _json
        payload: dict = {
            "type": "done",
            "thinking": thinking or "",
            "content": content or "",
            "new_messages": session["messages"],
            "session_id": session_id,
            "debug_info": result[2] if len(result) > 2 else None,
            "switch_info": result[5] if len(result) > 5 else None,
        }
        # Verlauf in der UI leeren, wenn neue Session oder Modellwechsel (Backend hat messages=[] bereits gesetzt)
        if is_new or is_model_switch:
            payload["clear"] = True
        one = _json.dumps(payload, ensure_ascii=False) + "\n"
        return StreamingResponse(iter([one]), media_type="application/x-ndjson")
    cancel_key = f"web:{session_id}"

    async def _stream_with_disconnect():
        """Wrapper: leitet sync generator weiter und signalisiert Cancellation bei Client-Disconnect."""
        gen = _chat_stream_generator(session_id, session, message, images=_chat_images)
        loop = asyncio.get_event_loop()
        _done = False

        async def _disconnect_watcher():
            """Prueft periodisch ob der Client disconnected hat."""
            while not _done:
                await asyncio.sleep(2.0)
                if await request.is_disconnected():
                    from miniassistant.cancellation import request_cancel
                    request_cancel(cancel_key, "stop")
                    _log.info("Client disconnected (session %s) — Cancellation signalisiert", session_id)
                    return

        watcher = asyncio.create_task(_disconnect_watcher())
        try:
            # Sync generator in dedicated Threadpool ausfuehren (vermeidet Blockieren bei Modell-Pulls)
            executor = _chat_executor
            while True:
                try:
                    _sentinel = object()
                    chunk = await loop.run_in_executor(executor, lambda: next(gen, _sentinel))
                    if chunk is _sentinel:
                        break
                    yield chunk
                except StopIteration:
                    break
        finally:
            _done = True
            watcher.cancel()

    return StreamingResponse(
        _stream_with_disconnect(),
        media_type="application/x-ndjson",
    )


@app.post("/api/chat/tool_result")
async def api_chat_tool_result(request: Request):
    """Client meldet Ergebnis einer lokal ausgeführten Tool-Anfrage zurück.
    Body: { tool_id: str, result: str, session_id: str }
    """
    _require_token(request)
    body = await request.json()
    tool_id = (body.get("tool_id") or "").strip()
    result = body.get("result")
    caller_sid = (body.get("session_id") or "").strip()
    if not tool_id:
        raise HTTPException(status_code=400, detail="tool_id required")
    if not caller_sid:
        raise HTTPException(status_code=400, detail="session_id required")
    with _tool_requests_lock:
        entry = _pending_tool_requests.get(tool_id)
    if not entry:
        raise HTTPException(status_code=404, detail="tool_id not found or already expired")
    # Session-Binding: nur die Session darf antworten, die den Request ausgelöst hat
    if entry.get("session_id") and caller_sid != entry["session_id"]:
        raise HTTPException(status_code=403, detail="session_id mismatch")
    result_str = str(result) if result is not None else ""
    if len(result_str.encode("utf-8", errors="replace")) > _TOOL_RESULT_MAX_BYTES:
        result_str = result_str[:_TOOL_RESULT_MAX_BYTES] + "\n… (gekürzt)"
    entry["result"] = result_str
    entry["event"].set()
    return JSONResponse({"ok": True})


@app.post("/api/chat")
async def api_chat(request: Request):
    """Chat: POST { message, optional session_id } -> { response, session_id }."""
    _require_token(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    session_id = body.get("session_id") or ""
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
    else:
        session_id = str(uuid.uuid4())
        project_dir = getattr(request.app.state, "project_dir", None)
        session = create_session(None, project_dir)
        _sessions[session_id] = session
    if body.get("track"):
        session["_track_chat"] = True
    _session_last_access[session_id] = time.time()
    # Run in threadpool to avoid blocking the event loop during Ollama model pulls
    loop = asyncio.get_event_loop()
    executor = _chat_executor
    # Wie /api/chat/stream: Web-Plattform setzen — handle_user_input persistiert nur bei _track_chat
    session.setdefault("chat_context", {})["platform"] = "web"
    session["chat_context"]["user_id"] = f"web:{session_id}"
    session["config"].setdefault("_chat_context", {})["user_id"] = f"web:{session_id}"
    session["config"]["_chat_context"]["platform"] = "web"
    if session.get("_track_chat"):
        from miniassistant.slot_cache import derive_conv_id as _sc_derive
        _conv = _sc_derive("web", session_id=session_id)
        if _conv:
            session["config"]["_chat_context"]["conv_id"] = _conv
            session["config"]["_chat_context"]["slot_cache_endpoint"] = "web"
    else:
        session["config"]["_chat_context"].pop("conv_id", None)

    result = await loop.run_in_executor(executor, lambda: handle_user_input(session, message))
    response_text = result[0]
    session = result[1]
    debug_info = result[2] if len(result) > 2 else None
    thinking = result[3] if len(result) > 3 else None
    content = result[4] if len(result) > 4 else None
    switch_info = result[5] if len(result) > 5 else None
    _sessions[session_id] = session
    out = {"response": content if content is not None else response_text, "session_id": session_id}
    if thinking:
        out["thinking"] = thinking
    if switch_info:
        out["model_switched"] = switch_info  # {"model": "...", "reason": "..."} für Anzeige z.B. "Wechsel zu X (Grund: …)"
    if debug_info is not None:
        out["_debug"] = debug_info
    return JSONResponse(out)


# --- Onboarding ---

@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    """Onboarding-Seite: geführter Dialog für SOUL/IDENTITY/TOOLS/USER. Gleiches Design wie Chat."""
    _require_token(request)  # first-run: kein Token gesetzt → durchgelassen. Sonst Token erforderlich.
    token = request.query_params.get("token", "")
    token_q = f"?token={token}" if token else ""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Onboarding – MiniAssistant</title>
    <link rel="icon" href="/favicon.ico">
    <script src="/static/marked.umd.js"></script>
    <script src="/static/purify.min.js"></script>
    <style>
    {_COMMON_CSS}
    .chat-wrap {{ display: flex; flex-direction: column; height: 100vh; max-width: min(900px, 98vw); margin: 0 auto; padding: 0.8em 1em; }}
    .chat-header {{ display: flex; align-items: center; gap: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); margin-bottom: 0.5em; flex-shrink: 0; }}
    .chat-header img {{ width: 32px; height: 32px; border-radius: 6px; }}
    .chat-header h1 {{ margin: 0; font-size: 1.2em; }}
    .chat-header .subtitle {{ font-size: 0.8em; color: var(--muted); margin-left: auto; }}
    #log {{ flex: 1; overflow-y: auto; padding: 0.3em 0; }}
    .msg {{ padding: 0.5em 0; }}
    .msg + .msg {{ margin-top: 0.1em; }}
    .msg-sep {{ border: none; border-top: 1px solid var(--border); margin: 0.5em 0; }}
    .msg-role {{ font-weight: 700; font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.25em; }}
    .msg-role.user {{ color: var(--primary); text-align: right; }}
    .msg-role.assistant {{ color: var(--success); }}
    .msg.msg-user {{ text-align: right; padding-left: 3em; border-right: 3px solid var(--primary); padding-right: 0.7em; }}
    .msg.msg-assistant {{ padding-right: 0; border-left: 3px solid var(--success); padding-left: 0.7em; }}
    .msg .content {{ line-height: 1.6; }}
    .msg .content.markdown p {{ margin: 0.3em 0; }}
    .msg .content.markdown pre {{ background: var(--bg); padding: 0.6em; border-radius: 6px; overflow-x: auto; font-size: 0.9em; }}
    .msg .content.markdown code {{ background: var(--bg); padding: 0.15em 0.35em; border-radius: 4px; font-size: 0.9em; }}
    .msg .content.markdown pre code {{ background: none; padding: 0; }}
    details.thinking {{ margin-top: 0.3em; font-size: 0.88em; color: var(--muted); }}
    details.thinking summary {{ cursor: pointer; font-weight: 500; }}
    .thinking-placeholder {{ color: var(--muted); font-style: italic; }}
    .chat-input {{ display: flex; gap: 0.5em; align-items: flex-end; padding-top: 0.6em; border-top: 1px solid var(--border); flex-shrink: 0; }}
    .chat-input textarea {{ flex: 1; padding: 0.55em 0.7em; border: 1.5px solid var(--border); border-radius: var(--radius);
                            font-family: inherit; font-size: 0.95em; resize: none; outline: none; min-height: 2.5em; max-height: 8em; transition: border-color 0.15s; }}
    .chat-input textarea:focus {{ border-color: var(--primary); }}
    .chat-input button {{ height: 2.5em; }}
    .chat-footer {{ font-size: 0.8em; color: var(--muted); padding-top: 0.3em; text-align: center; flex-shrink: 0; }}
    #saveBox {{ padding: 0.7em 1em; background: #e8f5e9; border-radius: var(--radius); border-left: 4px solid var(--success);
                display: none; flex-shrink: 0; margin-top: 0.5em; display: none; }}
    #saveBox .btn {{ margin-right: 0.5em; }}
    #saveStatus {{ font-size: 0.9em; }}
    </style>
    </head>
    <body>
    <div class="chat-wrap">
    <div class="chat-header">
      <img src="/static/miniassistant.png" alt="MiniAssistant">
      <h1>Onboarding / Setup</h1>
      <span class="subtitle">SOUL, IDENTITY, TOOLS, USER einrichten</span>
    </div>
    <div id="log"></div>
    <div id="saveBox"><button id="btnSave" class="btn btn-primary">Dateien speichern</button> <span id="saveStatus"></span></div>
    <form id="f" class="chat-input">
      <textarea id="msg" placeholder="Antwort… (Enter = Senden, Shift+Enter = Zeile)" rows="2" autocomplete="off"></textarea>
      <button type="submit" class="btn btn-primary">Senden</button>
    </form>
    <div class="chat-footer"><a href="/{token_q}">Startseite</a></div>
    </div>
    <script>
    const token = new URLSearchParams(window.location.search).get("token") || (document.cookie.match(/(?:^|;\\s*)ma_token=([^;]*)/) || [])[1] || "";
    const log = document.getElementById("log");
    const saveBox = document.getElementById("saveBox");
    const btnSave = document.getElementById("btnSave");
    const saveStatus = document.getElementById("saveStatus");
    const form = document.getElementById("f");
    const msgEl = document.getElementById("msg");
    msgEl.addEventListener("keydown", function(e) {{
      if (e.key === "Enter" && !e.shiftKey) {{ e.preventDefault(); form.requestSubmit(); }}
    }});
    msgEl.addEventListener("input", function() {{
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 200) + "px";
    }});
    let sessionId = "";
    let pendingFiles = null;

    function escapeHtml(s) {{
      const div = document.createElement("div");
      div.textContent = s;
      return div.innerHTML;
    }}
    function addLog(role, text, isMd) {{
      if (role === "Du" && log.children.length > 0) {{
        const sep = document.createElement("hr"); sep.className = "msg-sep"; log.appendChild(sep);
      }}
      const p = document.createElement("div");
      p.className = "msg " + (role === "Du" ? "msg-user" : "msg-assistant");
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role " + (role === "Du" ? "user" : "");
      roleDiv.textContent = role;
      p.appendChild(roleDiv);
      const content = document.createElement("div");
      content.className = "content" + (isMd ? " markdown" : "");
      if (isMd && typeof marked !== "undefined")
        content.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
      else
        content.textContent = text || "";
      p.appendChild(content);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}
    function addAssistantWithThinking(contentText, thinkingText, userRequest) {{
      const p = document.createElement("div");
      p.className = "msg msg-assistant";
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role assistant";
      roleDiv.textContent = "Assistant";
      p.appendChild(roleDiv);
      const wrap = document.createElement("div");
      wrap.className = "content";
      if (thinkingText) {{
        const details = document.createElement("details");
        details.className = "thinking";
        details.innerHTML = "<summary>Denkvorgang</summary><div style='white-space:pre-wrap;margin-top:0.3em;'>" + escapeHtml(thinkingText) + "</div>";
        wrap.appendChild(details);
      }}
      if (contentText) {{
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? DOMPurify.sanitize(marked.parse(contentText)) : escapeHtml(contentText);
        wrap.appendChild(md);
      }}
      p.appendChild(wrap);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}
    function showThinking() {{
      const p = document.createElement("div");
      p.className = "msg msg-assistant";
      p.id = "thinking-placeholder";
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role assistant";
      roleDiv.textContent = "Assistant";
      p.appendChild(roleDiv);
      const span = document.createElement("span");
      span.className = "thinking-placeholder";
      span.textContent = "Denkt nach …";
      p.appendChild(span);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
      form.querySelector("button[type=submit]").disabled = true;
    }}
    function hideThinking() {{
      const el = document.getElementById("thinking-placeholder");
      if (el) el.remove();
      form.querySelector("button[type=submit]").disabled = false;
    }}

    form.addEventListener("submit", async (e) => {{
      e.preventDefault();
      const content = msgEl.value.trim();
      if (!content) return;
      msgEl.value = "";
      msgEl.style.height = "auto";
      addLog("Du", content, false);
      showThinking();
      const url = "/api/onboarding" + (token ? "?token=" + encodeURIComponent(token) : "");
      try {{
        const r = await fetch(url, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify({{ message: content, session_id: sessionId || undefined }}) }});
        if (!r.ok && !r.headers.get("content-type")?.includes("application/json")) {{ throw new Error(await r.text() || r.statusText); }}
        const data = await r.json();
        sessionId = data.session_id || sessionId;
        hideThinking();
        addAssistantWithThinking(data.response || data.error || "—", data.thinking || null, content);
        if (data.saved) {{
          // Fallback-Save via Bot-Nachricht erfolgreich — nach kurzer Pause zum Chat
          setTimeout(function() {{ window.location.href = "/chat" + (token ? "?token=" + encodeURIComponent(token) : "") + (token ? "&" : "?") + "onboarding_saved=1"; }}, 2000);
        }}
        if (data.suggested_files) {{ pendingFiles = data.suggested_files; saveBox.style.display = "block"; }}
        if (data._debug) {{
          const d = document.createElement("details");
          d.className = "thinking";
          d.innerHTML = "<summary>Debug</summary><pre style='white-space:pre-wrap;font-size:11px;'>" + JSON.stringify(data._debug, null, 2).replace(/</g, "&lt;") + "</pre>";
          log.appendChild(d);
          log.scrollTop = log.scrollHeight;
        }}
      }} catch (err) {{
        hideThinking();
        addLog("Fehler", err.message, false);
      }}
    }});
    btnSave.addEventListener("click", async () => {{
      if (!pendingFiles) return;
      btnSave.disabled = true;
      saveStatus.textContent = "Speichere…";
      try {{
        const r = await fetch("/api/onboarding/save" + (token ? "?token=" + encodeURIComponent(token) : ""), {{
          method: "POST", headers: {{ "Content-Type": "application/json" }}, credentials: "same-origin", body: JSON.stringify(pendingFiles)
        }});
        const data = await r.json();
        if (data.ok) {{
          saveStatus.textContent = "Gespeichert.";
          pendingFiles = null;
          saveBox.style.display = "none";
          window.location.href = "/chat" + (token ? "?token=" + encodeURIComponent(token) : "") + (token ? "&" : "?") + "onboarding_saved=1";
          return;
        }} else {{ saveStatus.textContent = data.error || "Fehler"; }}
      }} catch (err) {{ saveStatus.textContent = err.message; }}
      btnSave.disabled = false;
    }});

    (async function() {{
      showThinking();
      const url = "/api/onboarding" + (token ? "?token=" + encodeURIComponent(token) : "");
      try {{
        const r = await fetch(url, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify({{ message: "Beginne das Onboarding." }}) }});
        if (!r.ok && !r.headers.get("content-type")?.includes("application/json")) {{ throw new Error(await r.text() || r.statusText); }}
        const data = await r.json();
        sessionId = data.session_id || sessionId;
        hideThinking();
        addAssistantWithThinking(data.response || data.error || "—", data.thinking || null, "Beginne das Onboarding.");
        if (data.suggested_files) {{ pendingFiles = data.suggested_files; saveBox.style.display = "block"; }}
        if (data._debug) {{
          const d = document.createElement("details");
          d.className = "thinking";
          d.innerHTML = "<summary>Debug</summary><pre style='white-space:pre-wrap;font-size:11px;'>" + JSON.stringify(data._debug, null, 2).replace(/</g, "&lt;") + "</pre>";
          log.appendChild(d);
          log.scrollTop = log.scrollHeight;
        }}
      }} catch (_) {{ hideThinking(); }}
    }})();
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


def _is_save_request(message: str) -> bool:
    """Erkennt ob der User im Onboarding-Chat um manuelles Speichern bittet."""
    msg = message.lower().strip()
    _SAVE_KEYWORDS = [
        "speicher", "speichere", "bitte speichern", "config speichern", "dateien speichern",
        "save", "please save", "save config", "save the config", "save files",
        "button funktioniert nicht", "button geht nicht", "knopf funktioniert nicht",
        "knopf geht nicht", "speichern geht nicht", "speichern funktioniert nicht",
        "save button", "kann nicht speichern", "can't save", "cannot save",
    ]
    return any(kw in msg for kw in _SAVE_KEYWORDS)


def _save_onboarding_files(files: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    """Speichert die vier Agent-Dateien und setzt onboarding_complete. Returns {'ok': True} oder {'error': ...}."""
    agent_dir = Path(config.get("agent_dir") or "")
    if not agent_dir:
        return {"error": "agent_dir not configured"}
    agent_dir = Path(agent_dir).expanduser().resolve()
    agent_dir.mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md"):
        content = files.get(name)
        if content is not None:
            (agent_dir / name).write_text(content if isinstance(content, str) else str(content), encoding="utf-8")
    config["onboarding_complete"] = True
    save_config(config)
    return {"ok": True}


@app.post("/api/onboarding")
async def api_onboarding(request: Request):
    """Onboarding-Chat: eine Runde mit Onboarding-System-Prompt, keine Tools. Gibt ggf. suggested_files zurück."""
    _require_token(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    config = load_config()
    session_id = body.get("session_id") or ""
    if session_id and session_id in _onboarding_sessions:
        data = _onboarding_sessions[session_id]
        messages = data.get("messages") or []
    else:
        session_id = str(uuid.uuid4())
        messages = []
        _onboarding_sessions[session_id] = {"messages": messages}

    # Fallback-Save: User bittet um manuelles Speichern weil Button nicht geht
    if _is_save_request(message):
        pending = _onboarding_sessions[session_id].get("suggested_files")
        if pending:
            result = _save_onboarding_files(pending, config)
            if result.get("ok"):
                _onboarding_sessions[session_id].pop("suggested_files", None)
                return JSONResponse({
                    "response": "✅ Dateien wurden gespeichert! Du kannst jetzt zum [Chat](/chat) wechseln.",
                    "session_id": session_id,
                    "saved": True,
                })
            else:
                return JSONResponse({
                    "response": f"❌ Fehler beim Speichern: {result.get('error', 'Unbekannter Fehler')}",
                    "session_id": session_id,
                })

    try:
        response_text, new_messages, suggested_files, debug_info, thinking, content = await asyncio.to_thread(
            run_onboarding_round, config, messages, message
        )
    except Exception as exc:
        _log.exception("Onboarding-Fehler: %s", exc)
        return JSONResponse({"error": str(exc), "session_id": session_id}, status_code=500)
    _onboarding_sessions[session_id]["messages"] = new_messages
    # suggested_files in Session merken für Fallback-Save
    if suggested_files:
        _onboarding_sessions[session_id]["suggested_files"] = suggested_files
    # Antwort nur als Content (ohne [Thinking]-Präfix); Denkvorgang getrennt
    out = {"response": content if content else response_text, "session_id": session_id}
    if thinking:
        out["thinking"] = thinking
    if suggested_files:
        out["suggested_files"] = suggested_files
    if debug_info is not None:
        out["_debug"] = debug_info
    return JSONResponse(out)


# --- Log Viewer ---

@app.get("/api/logs/list")
async def api_logs_list(request: Request):
    """Verfügbare Log-Dateien auflisten (nur die existieren)."""
    _require_token(request)
    config = load_config()
    config_dir = config.get("_config_dir") or ""
    if not config_dir:
        from miniassistant.config import get_config_dir
        config_dir = get_config_dir()
    logs = []
    # agent_actions.log
    aal_path = Path(config_dir) / "logs" / "agent_actions.log"
    if aal_path.exists():
        logs.append({"id": "agent_actions", "label": "Agent Actions", "path": str(aal_path)})
    # system log (miniassistant.log unter /var/log oder config_dir/logs)
    for sys_path in [Path("/var/log/miniassistant.log"), Path(config_dir) / "logs" / "miniassistant.log"]:
        if sys_path.exists():
            logs.append({"id": "system", "label": "System Log", "path": str(sys_path)})
            break
    # context log
    context_log = Path(config_dir) / "logs" / "context.log"
    if context_log.exists():
        logs.append({"id": "context", "label": "Context", "path": str(context_log)})
    # debug logs
    debug_chat = Path(config_dir) / "debug" / "chat.log"
    if debug_chat.exists():
        logs.append({"id": "debug_chat", "label": "Debug Chat", "path": str(debug_chat)})
    debug_serve = Path(config_dir) / "debug" / "serve.log"
    if debug_serve.exists():
        logs.append({"id": "debug_serve", "label": "Debug Serve", "path": str(debug_serve)})
    # Memory-Dateien (tägliche .md unter agent_dir/memory/)
    memory_files = []
    try:
        from miniassistant.memory import memory_dir as _memory_dir
        mem_dir = _memory_dir(getattr(app.state, "project_dir", None))
        if mem_dir.is_dir():
            for mf in sorted(mem_dir.iterdir(), reverse=True):
                if mf.is_file() and mf.suffix == ".md":
                    mid = f"memory_{mf.stem}"
                    memory_files.append({"id": mid, "label": mf.name, "path": str(mf)})
            if (mem_dir / "last_summary.json").exists():
                memory_files.append({"id": "memory_last_summary", "label": "last_summary.json", "path": str(mem_dir / "last_summary.json")})
    except Exception:
        pass
    # Per-room group logs (logs/agent_actions_groups/*.log) — nur wenn vorhanden
    groups = []
    try:
        groups_dir = Path(config_dir) / "logs" / "agent_actions_groups"
        if groups_dir.is_dir():
            # Mapping: sanitized subdir → (room_name, platform). Aus aktuell geladenen Räumen/Channels.
            from miniassistant.group_rooms import sanitize_workspace_subdir
            stem_to_name: dict[str, tuple[str, str]] = {}
            try:
                from miniassistant.matrix_bot import list_joined_rooms as _lr
                for r in _lr() or []:
                    sub = sanitize_workspace_subdir(r.get("id") or "")
                    if sub and r.get("name"):
                        stem_to_name[sub] = (str(r["name"]), "matrix")
            except Exception:
                pass
            try:
                from miniassistant.discord_bot import list_channels as _lc
                for c in _lc() or []:
                    sub = sanitize_workspace_subdir(c.get("id") or "")
                    if sub and c.get("name"):
                        guild = c.get("guild") or ""
                        label = f"{c['name']} ({guild})" if guild else c["name"]
                        stem_to_name[sub] = (label, "discord")
            except Exception:
                pass
            for gf in sorted(groups_dir.iterdir()):
                if gf.is_file() and gf.suffix == ".log":
                    stem = gf.stem  # sanitized subdir name (no traversal possible)
                    name_plat = stem_to_name.get(stem)
                    if name_plat:
                        name, plat = name_plat
                        label = f"{name} [{plat}]"
                    else:
                        # Bot ist nicht mehr im Raum oder offline — zeig stem als fallback
                        label = f"{stem} (offline/left)"
                    groups.append({
                        "id": f"group_{stem}",
                        "label": label,
                        "stem": stem,
                        "path": str(gf),
                        "size_kb": round(gf.stat().st_size / 1024, 1),
                    })
    except Exception:
        pass
    return JSONResponse({"logs": logs, "memory": memory_files, "groups": groups})


@app.get("/api/logs/{log_id}")
async def api_logs_read(request: Request, log_id: str):
    """Log-Datei lesen (tail). Query: ?lines=N (default 200), ?offset=N (byte offset für polling)."""
    _require_token(request)
    config = load_config()
    config_dir = config.get("_config_dir") or ""
    if not config_dir:
        from miniassistant.config import get_config_dir
        config_dir = get_config_dir()
    # Pfad-Mapping (sicher – keine beliebigen Pfade)
    log_map: dict[str, Path | None] = {
        "agent_actions": Path(config_dir) / "logs" / "agent_actions.log",
        "system": None,  # Wird unten bestimmt
        "context": Path(config_dir) / "logs" / "context.log",
        "debug_chat": Path(config_dir) / "debug" / "chat.log",
        "debug_serve": Path(config_dir) / "debug" / "serve.log",
    }
    # system log: erster existierender Pfad
    for sys_path in [Path("/var/log/miniassistant.log"), Path(config_dir) / "logs" / "miniassistant.log"]:
        if sys_path.exists():
            log_map["system"] = sys_path
            break
    # Per-room group logs (group_<sanitized_subdir>)
    if log_id.startswith("group_"):
        stem = log_id[6:]  # nach "group_"
        import re as _re_grp
        # Whitelist: nur sanitisierte Subdir-Namen (gleiche Regex wie sanitize_workspace_subdir).
        if _re_grp.fullmatch(r"[a-zA-Z0-9_-]{1,40}", stem):
            gp = Path(config_dir) / "logs" / "agent_actions_groups" / f"{stem}.log"
            if gp.exists():
                log_map[log_id] = gp
    # Memory-Dateien (memory_YYYY-MM-DD, memory_last_summary)
    if log_id.startswith("memory_"):
        try:
            from miniassistant.memory import memory_dir as _memory_dir
            mem_dir = _memory_dir(getattr(app.state, "project_dir", None))
            suffix = log_id[7:]  # nach "memory_"
            # Whitelist: nur Datums-Stems (YYYY-MM-DD) oder last_summary.
            # Verhindert Path-Traversal via log_id=memory_../../foo.
            import re as _re_mem
            if suffix == "last_summary":
                log_map[log_id] = mem_dir / "last_summary.json"
            elif _re_mem.fullmatch(r"\d{4}-\d{2}-\d{2}", suffix):
                log_map[log_id] = mem_dir / f"{suffix}.md"
        except Exception:
            pass
    path = log_map.get(log_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail=f"Log '{log_id}' not found")
    try:
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        offset = 0
    try:
        max_bytes = int(request.query_params.get("max_bytes", "65536"))
    except ValueError:
        max_bytes = 65536
    max_bytes = min(max_bytes, 262144)  # cap at 256KB
    file_size = path.stat().st_size
    if offset > 0 and offset < file_size:
        # Nur neue Bytes seit letztem Offset
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read(max_bytes)
        return JSONResponse({"content": content, "offset": offset + len(content.encode("utf-8")), "size": file_size})
    elif offset >= file_size:
        # Keine neuen Daten
        return JSONResponse({"content": "", "offset": file_size, "size": file_size})
    else:
        # Erste Anfrage: Tail (letzte max_bytes Bytes)
        read_start = max(0, file_size - max_bytes)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if read_start > 0:
                f.seek(read_start)
                f.readline()  # erste (evtl. angeschnittene) Zeile überspringen
            content = f.read()
        return JSONResponse({"content": content, "offset": file_size, "size": file_size})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Log-Viewer: Dropdown für Log-Auswahl, Live-Modus mit Auto-Scroll."""
    _require_token(request)
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Logs – MiniAssistant</title>
    <link rel="icon" href="/favicon.ico">
    <style>
    {_COMMON_CSS}
    .logs-wrap {{ display: flex; flex-direction: column; height: 100vh; max-width: 1100px; margin: 0 auto; padding: 0.8em 1em; }}
    .logs-header {{ display: flex; align-items: center; gap: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); margin-bottom: 0.5em; flex-shrink: 0; flex-wrap: wrap; }}
    .logs-header img {{ width: 32px; height: 32px; border-radius: 6px; }}
    .logs-header h1 {{ margin: 0; font-size: 1.2em; }}
    .logs-controls {{ display: flex; align-items: center; gap: 0.6em; margin-left: auto; flex-wrap: wrap; }}
    .logs-controls select {{ padding: 0.4em 0.6em; border: 1.5px solid var(--border); border-radius: var(--radius);
                             font-size: 0.9em; background: var(--card); color: var(--text); outline: none; }}
    .logs-controls select:focus {{ border-color: var(--primary); }}
    .logs-controls label {{ font-size: 0.85em; display: flex; align-items: center; gap: 0.3em; cursor: pointer; user-select: none; }}
    .logs-controls label input {{ accent-color: var(--primary); }}
    #log-box {{ flex: 1; overflow-y: auto; background: #1e1e1e; color: #d4d4d4; font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
                font-size: 12.5px; line-height: 1.5; padding: 0.8em; border-radius: var(--radius); white-space: pre-wrap; word-break: break-all; }}
    #log-box .sep {{ color: #555; }}
    #log-box .ts {{ color: #6a9955; }}
    #log-box .label {{ color: #569cd6; font-weight: bold; }}
    #log-box .tool {{ color: #ce9178; }}
    #log-box .user {{ color: #dcdcaa; }}
    #log-box .subagent {{ color: #c586c0; font-weight: bold; }}
    #log-box .subagent-model {{ color: #9cdcfe; }}
    .empty-hint {{ color: var(--muted); font-style: italic; text-align: center; margin-top: 3em; }}
    .log-status {{ font-size: 0.8em; color: var(--muted); padding-top: 0.3em; flex-shrink: 0; display: flex; align-items: center; gap: 1em; }}
    .live-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--success); display: inline-block; animation: pulse 1.5s infinite; }}
    .live-dot.off {{ background: var(--muted); animation: none; }}
    @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
    </style>
    </head>
    <body>
    <div class="logs-wrap">
      <div class="logs-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Logs</h1>
        <div class="logs-controls">
          <select id="log-select"><option value="">——— Log wählen ———</option></select>
          <select id="group-select" style="display:none;"><option value="">——— Raum wählen ———</option></select>
          <label><input type="checkbox" id="live-toggle" checked> Live</label>
          <label><input type="checkbox" id="scroll-toggle" checked> Auto-Scroll</label>
          <a href="/" class="btn btn-outline" style="padding:0.35em 0.8em;font-size:0.85em;">Startseite</a>
        </div>
      </div>
      <div id="log-box"><div class="empty-hint">Wähle ein Log aus dem Dropdown.</div></div>
      <div class="log-status">
        <span><span class="live-dot off" id="live-dot"></span></span>
        <span id="status-text">—</span>
      </div>
    </div>
    <script>
    (function() {{
      var select = document.getElementById("log-select");
      var groupSelect = document.getElementById("group-select");
      var box = document.getElementById("log-box");
      var liveToggle = document.getElementById("live-toggle");
      var scrollToggle = document.getElementById("scroll-toggle");
      var liveDot = document.getElementById("live-dot");
      var statusText = document.getElementById("status-text");
      var currentLog = "";
      var offset = 0;
      var pollTimer = null;
      var pollInterval = 2000;
      var groupLogs = [];

      // Cookie wird automatisch mitgeschickt (credentials: same-origin ist default bei fetch)
      // Kein manuelles Token-Handling nötig

      // Dropdown befüllen
      fetch("/api/logs/list", {{credentials: "same-origin"}}).then(function(r) {{ return r.json(); }}).then(function(data) {{
        (data.logs || []).forEach(function(l) {{
          var opt = document.createElement("option");
          opt.value = l.id;
          opt.textContent = l.label;
          select.appendChild(opt);
        }});
        // Groups-Eintrag nur wenn Per-Room-Logs existieren
        groupLogs = data.groups || [];
        if (groupLogs.length > 0) {{
          var sepG = document.createElement("option");
          sepG.disabled = true;
          sepG.textContent = "——— Groups ———";
          select.appendChild(sepG);
          var gopt = document.createElement("option");
          gopt.value = "__groups__";
          gopt.textContent = "Group-Räume (" + groupLogs.length + ")";
          select.appendChild(gopt);
          // Sub-Dropdown befüllen
          groupLogs.forEach(function(g) {{
            var o = document.createElement("option");
            o.value = g.id;
            o.textContent = g.label + " (" + g.size_kb + " KB)";
            groupSelect.appendChild(o);
          }});
        }}
        // Memory-Dateien mit Separator
        var memFiles = data.memory || [];
        if (memFiles.length > 0) {{
          var sep = document.createElement("option");
          sep.disabled = true;
          sep.textContent = "——— Memory ———";
          select.appendChild(sep);
          memFiles.forEach(function(m) {{
            var opt = document.createElement("option");
            opt.value = m.id;
            opt.textContent = m.label;
            select.appendChild(opt);
          }});
        }}
      }}).catch(function() {{}});

      function escapeHtml(t) {{
        return t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
      }}
      function colorize(text) {{
        return escapeHtml(text)
          .replace(/^(---)/gm, '<span class="sep">$1</span>')
          .replace(/\\[(\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}:\\d{{2}})\\]/g, '[<span class="ts">$1</span>]')
          .replace(/(PROMPT|THINKING|RESPONSE)/g, '<span class="label">$1</span>')
          .replace(/(TOOL\\s+\\S+)/g, '<span class="tool">$1</span>')
          .replace(/(SUBAGENT_RESULT|SUBAGENT_THINKING)(\\s+model=\\S+)/g, '<span class="subagent">$1</span><span class="subagent-model">$2</span>')
          .replace(/(SUBAGENT)(\\s+model=\\S+)/g, '<span class="subagent">$1</span><span class="subagent-model">$2</span>')
          .replace(/(User:)/g, '<span class="user">$1</span>');
      }}

      function loadLog(logId, append) {{
        if (!logId) {{
          box.innerHTML = '<div class="empty-hint">Wähle ein Log aus dem Dropdown.</div>';
          statusText.textContent = "—";
          return;
        }}
        var url = "/api/logs/" + encodeURIComponent(logId) + "?offset=" + (append ? offset : 0) + "&max_bytes=131072";
        fetch(url, {{credentials: "same-origin"}}).then(function(r) {{
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        }}).then(function(data) {{
          if (!append) {{
            box.innerHTML = colorize(data.content || "(leer)");
          }} else if (data.content) {{
            box.innerHTML += colorize(data.content);
          }}
          offset = data.offset || 0;
          var kb = (data.size / 1024).toFixed(1);
          statusText.textContent = logId + " – " + kb + " KB";
          if (scrollToggle.checked) {{
            box.scrollTop = box.scrollHeight;
          }}
        }}).catch(function(e) {{
          statusText.textContent = "Fehler: " + e.message;
        }});
      }}

      select.addEventListener("change", function() {{
        var v = this.value;
        if (v === "__groups__") {{
          // Pseudo-Auswahl: zeigt Sub-Dropdown, lädt nichts bis Raum gewählt
          groupSelect.style.display = "";
          if (!groupSelect.value) {{
            box.innerHTML = '<div class="empty-hint">Wähle einen Raum aus dem zweiten Dropdown.</div>';
            statusText.textContent = "—";
            if (pollTimer) clearInterval(pollTimer);
            return;
          }}
          currentLog = groupSelect.value;
        }} else {{
          groupSelect.style.display = "none";
          groupSelect.value = "";
          currentLog = v;
        }}
        offset = 0;
        loadLog(currentLog, false);
        restartPoll();
      }});

      groupSelect.addEventListener("change", function() {{
        if (!this.value) return;
        currentLog = this.value;
        offset = 0;
        loadLog(currentLog, false);
        restartPoll();
      }});

      function restartPoll() {{
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
        if (liveToggle.checked && currentLog) {{
          liveDot.className = "live-dot";
          pollTimer = setInterval(function() {{
            loadLog(currentLog, true);
          }}, pollInterval);
        }} else {{
          liveDot.className = "live-dot off";
        }}
      }}

      liveToggle.addEventListener("change", restartPoll);
      scrollToggle.addEventListener("change", function() {{
        if (this.checked) box.scrollTop = box.scrollHeight;
      }});

      // Manuelles Scrollen deaktiviert Auto-Scroll
      var userScrolled = false;
      box.addEventListener("scroll", function() {{
        var atBottom = (box.scrollHeight - box.scrollTop - box.clientHeight) < 30;
        if (!atBottom && scrollToggle.checked) {{
          scrollToggle.checked = false;
        }} else if (atBottom && !scrollToggle.checked) {{
          scrollToggle.checked = true;
        }}
      }});
    }})();
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/logout")
async def logout(request: Request):
    """Löscht den Auth-Cookie (auch httpOnly) und redirected zur Startseite."""
    from starlette.responses import RedirectResponse as _Redirect
    resp = _Redirect(url="/", status_code=302)
    resp.delete_cookie("ma_token", path="/", samesite="strict")
    return resp


@app.post("/api/restart")
async def api_restart(request: Request):
    """Startet den MiniAssistant-Service neu (systemd oder init.d, in Subshell)."""
    _require_token(request)
    import subprocess
    import shutil
    # Detect init system and service name
    service_name = "miniassistant"
    if shutil.which("systemctl"):
        argv: list[str] = ["systemctl", "restart", service_name]
        method = "systemd"
    elif Path(f"/etc/init.d/{service_name}").exists():
        argv = [f"/etc/init.d/{service_name}", "restart"]
        method = "init.d"
    else:
        # Fallback: kill own process group, let supervisor restart
        import os, signal
        async def _delayed_kill():
            import asyncio
            await asyncio.sleep(1)
            os.kill(os.getpid(), signal.SIGTERM)
        asyncio.create_task(_delayed_kill())
        return JSONResponse({"ok": True, "method": "sigterm", "message": "Service wird beendet (Neustart durch Supervisor)."})
    try:
        # Delay über asyncio statt shell-`sleep` — Response geht raus bevor Service stirbt.
        async def _delayed_restart(_argv: list[str]) -> None:
            await asyncio.sleep(1)
            try:
                subprocess.Popen(_argv, start_new_session=True)
            except Exception as e:
                _log.error("Delayed restart fehlgeschlagen: %s", e)
        asyncio.create_task(_delayed_restart(argv))
        return JSONResponse({"ok": True, "method": method, "message": f"Restart via {method} ausgelöst."})
    except Exception as e:
        _log.error("Restart fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail="Restart fehlgeschlagen")


@app.get("/api/slot_cache/list")
async def api_slot_cache_list(request: Request):
    """Liste aller MA-bekannten Slot-Cache-Entries + Stats."""
    _require_token(request)
    from miniassistant.config import load_config
    from miniassistant import slot_cache
    cfg = load_config()
    if not slot_cache.is_globally_enabled(cfg):
        return JSONResponse({"enabled": False, "entries": [], "stats": {}})
    entries = slot_cache.list_all(cfg)
    stats = slot_cache.get_stats(cfg)
    return JSONResponse({"enabled": True, "entries": entries, "stats": stats})


@app.delete("/api/slot_cache/{conv_id:path}")
async def api_slot_cache_delete(conv_id: str, request: Request):
    """Forget Cache-Entry. File auf LLM-Server bleibt bis Server-Cron es löscht."""
    _require_token(request)
    from miniassistant.config import load_config
    from miniassistant import slot_cache
    cfg = load_config()
    removed = slot_cache.invalidate(cfg, conv_id)
    return JSONResponse({"ok": True, "removed": removed})


@app.post("/api/slot_cache/cleanup")
async def api_slot_cache_cleanup(request: Request):
    """Manueller Trigger: TTL+LRU + unknown-models cleanup."""
    _require_token(request)
    from miniassistant.config import load_config
    from miniassistant import slot_cache
    cfg = load_config()
    r1 = slot_cache.cleanup_lru_and_ttl(cfg)
    r2 = slot_cache.cleanup_unknown_models(cfg)
    return JSONResponse({"ok": True, "removed_lru_ttl": r1, "removed_unknown_models": r2})


@app.get("/api/usage")
async def api_usage(request: Request):
    """GET /api/usage?period=hour|day|3days|week|month|year|all -> aggregierte Nutzungsdaten.

    Alternativ: ?from=YYYY-MM-DD&to=YYYY-MM-DD für benutzerdefinierten Zeitraum.
    """
    _require_token(request)
    from_str = request.query_params.get("from")
    to_str = request.query_params.get("to")
    if from_str and to_str:
        from datetime import datetime
        from miniassistant.usage import get_usage_for_range
        try:
            from_dt = datetime.strptime(from_str, "%Y-%m-%d")
            to_dt = datetime.strptime(to_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(status_code=400, detail="Datumsformat muss YYYY-MM-DD sein")
        data = get_usage_for_range(from_dt, to_dt)
    else:
        from miniassistant.usage import get_usage_for_period
        period = request.query_params.get("period", "day")
        data = get_usage_for_period(period)
    return JSONResponse(data)


@app.get("/nutzung", response_class=HTMLResponse)
async def nutzung_page(request: Request):
    """Nutzungsstatistiken: Zeitfilter, Chart, Aufschlüsselung nach Modell/Typ."""
    _require_token(request)
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Nutzung – MiniAssistant</title>
    <link rel="icon" href="/favicon.ico">
    <script src="/static/chart.umd.min.js"></script>
    <style>
    {_COMMON_CSS}
    .usage-wrap {{ max-width: 1100px; margin: 0 auto; padding: 1em; }}
    .usage-header {{ display: flex; align-items: center; gap: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); margin-bottom: 1em; flex-wrap: wrap; }}
    .usage-header img {{ width: 32px; height: 32px; border-radius: 6px; }}
    .usage-header h1 {{ margin: 0; font-size: 1.2em; }}
    .usage-nav {{ margin-left: auto; }}
    .filter-bar {{ display: flex; gap: 0.4em; flex-wrap: wrap; margin-bottom: 1em; align-items: center; }}
    .filter-bar button {{ padding: 0.4em 0.9em; border: 1.5px solid var(--border); border-radius: var(--radius);
                          background: var(--card); color: var(--text); cursor: pointer; font-size: 0.85em; transition: all 0.15s; }}
    .filter-bar button:hover {{ border-color: var(--primary); }}
    .filter-bar button.active {{ background: var(--primary); color: #fff; border-color: var(--primary); }}
    .range-sep {{ color: var(--muted); font-size: 0.85em; margin: 0 0.1em; }}
    .filter-bar input[type="date"] {{ padding: 0.35em 0.5em; border: 1.5px solid var(--border); border-radius: var(--radius);
                                      background: var(--card); color: var(--text); font-size: 0.82em; }}
    .filter-bar .range-btn {{ padding: 0.4em 0.7em; font-size: 0.82em; }}
    .summary-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.8em; margin-bottom: 1.2em; }}
    .summary-card {{ background: var(--card); border: 1.5px solid var(--border); border-radius: var(--radius); padding: 1em; text-align: center; }}
    .summary-card .label {{ font-size: 0.8em; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
    .summary-card .value {{ font-size: 1.6em; font-weight: 700; color: var(--text); margin-top: 0.2em; }}
    .chart-container {{ background: var(--card); border: 1.5px solid var(--border); border-radius: var(--radius); padding: 1em; margin-bottom: 1.2em; position: relative; height: 300px; }}
    .tables-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1em; }}
    @media (max-width: 700px) {{ .tables-row {{ grid-template-columns: 1fr; }} }}
    .usage-table {{ background: var(--card); border: 1.5px solid var(--border); border-radius: var(--radius); overflow: hidden; }}
    .usage-table h3 {{ margin: 0; padding: 0.6em 0.8em; font-size: 0.9em; border-bottom: 1px solid var(--border); background: var(--bg); }}
    .usage-table table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
    .usage-table th, .usage-table td {{ padding: 0.5em 0.8em; text-align: left; border-bottom: 1px solid var(--border); }}
    .usage-table th {{ font-weight: 600; color: var(--muted); font-size: 0.8em; text-transform: uppercase; }}
    .usage-table tr:last-child td {{ border-bottom: none; }}
    .empty-hint {{ color: var(--muted); font-style: italic; text-align: center; padding: 2em; }}
    </style>
    </head>
    <body>
    <div class="usage-wrap">
      <div class="usage-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Nutzung</h1>
        <div class="usage-nav">
          <a href="/" class="btn btn-outline" style="padding:0.35em 0.8em;font-size:0.85em;">Startseite</a>
        </div>
      </div>
      <div class="filter-bar">
        <button data-period="hour">Letzte Stunde</button>
        <button data-period="day" class="active">Heute</button>
        <button data-period="3days">3 Tage</button>
        <button data-period="week">7 Tage</button>
        <button data-period="month">30 Tage</button>
        <button data-period="year">Jahr</button>
        <button data-period="all">Gesamt</button>
        <span class="range-sep">&nbsp;|&nbsp;</span>
        <input type="date" id="range-from" title="Von">
        <span class="range-sep">&ndash;</span>
        <input type="date" id="range-to" title="Bis">
        <button class="range-btn" id="range-go">Anzeigen</button>
      </div>
      <div class="summary-cards">
        <div class="summary-card"><div class="label">Gesamtzeit</div><div class="value" id="sum-time">—</div></div>
        <div class="summary-card"><div class="label">Anfragen</div><div class="value" id="sum-requests">—</div></div>
        <div class="summary-card"><div class="label">Modelle</div><div class="value" id="sum-models">—</div></div>
        <div class="summary-card" id="sum-group-card" style="display:none;background:rgba(168,85,247,0.08);border-color:rgba(168,85,247,0.3);"><div class="label">Davon Gruppen</div><div class="value" id="sum-group-time">—</div><div style="font-size:0.75em;color:var(--muted);margin-top:0.2em;" id="sum-group-req"></div></div>
      </div>
      <div class="chart-container"><canvas id="usage-chart"></canvas></div>
      <div class="tables-row">
        <div class="usage-table">
          <h3>Nach Modell</h3>
          <div id="table-model"><div class="empty-hint">Lade…</div></div>
        </div>
        <div class="usage-table">
          <h3>Nach Typ</h3>
          <div id="table-type"><div class="empty-hint">Lade…</div></div>
        </div>
      </div>
      <div id="slot-cache-section" style="display:none;margin-top:1.5em;">
        <div class="usage-table">
          <h3>Slot Cache <span id="sc-stats-summary" style="float:right;font-weight:normal;font-size:0.85em;color:var(--muted)"></span></h3>
          <div id="sc-list-wrap" style="padding:0.6em 0.8em;">
            <div style="margin-bottom:0.8em;">
              <button id="sc-cleanup-btn" class="btn">Cleanup ausführen</button>
              <span id="sc-cleanup-msg" style="margin-left:0.6em;color:var(--muted);font-size:0.85em;"></span>
            </div>
            <div id="sc-table"><div class="empty-hint">Lade…</div></div>
          </div>
        </div>
      </div>
    </div>
    <script>
    (function() {{
      var chart = null;
      var ctx = document.getElementById("usage-chart").getContext("2d");
      var btns = document.querySelectorAll(".filter-bar button");

      function fmtSec(s) {{
        if (s < 60) return s.toFixed(1) + "s";
        var m = Math.floor(s / 60);
        var sec = Math.round(s - m * 60);
        if (m < 60) return m + "m " + sec + "s";
        var h = Math.floor(m / 60);
        m = m % 60;
        return h + "h " + m + "m";
      }}

      function buildTable(rows, cols) {{
        if (!rows || !rows.length) return '<div class="empty-hint">Keine Daten</div>';
        var h = '<table><thead><tr>';
        cols.forEach(function(c) {{ h += '<th>' + c.label + '</th>'; }});
        h += '</tr></thead><tbody>';
        rows.forEach(function(r) {{
          h += '<tr>';
          cols.forEach(function(c) {{
            var v = r[c.key];
            if (c.fmt === 'sec') v = fmtSec(v);
            h += '<td>' + v + '</td>';
          }});
          h += '</tr>';
        }});
        h += '</tbody></table>';
        return h;
      }}

      function loadData(period, fromDate, toDate) {{
        var url = fromDate && toDate
          ? "/api/usage?from=" + fromDate + "&to=" + toDate
          : "/api/usage?period=" + period;
        fetch(url, {{credentials: "same-origin"}})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            // Summary
            document.getElementById("sum-time").textContent = data.summary.formatted || "0s";
            document.getElementById("sum-requests").textContent = data.summary.total_requests;
            document.getElementById("sum-models").textContent = (data.by_model || []).length;
            // Gruppen-Card nur wenn group_seconds > 0 (nicht jeder hat Gruppen konfiguriert)
            var grpCard = document.getElementById("sum-group-card");
            var grpSec = data.summary.group_seconds || 0;
            if (grpSec > 0) {{
              grpCard.style.display = "";
              document.getElementById("sum-group-time").textContent = data.summary.group_formatted || "0s";
              var pct = data.summary.total_seconds > 0 ? Math.round(grpSec / data.summary.total_seconds * 100) : 0;
              document.getElementById("sum-group-req").textContent = (data.summary.group_requests || 0) + " Anfragen · " + pct + "% Gesamt";
            }} else {{
              grpCard.style.display = "none";
            }}

            // Chart
            var labels = (data.by_time || []).map(function(d) {{
              var l = d.label;
              if (l.length > 10) l = l.slice(5);  // kürze "2026-" weg
              return l;
            }});
            var totalsArr = (data.by_time || []).map(function(d) {{ return d.seconds; }});
            var groupArr = (data.by_time || []).map(function(d) {{ return d.group_seconds || 0; }});
            var ownerArr = (data.by_time || []).map(function(d, i) {{ return Math.max(0, totalsArr[i] - groupArr[i]); }});
            var counts = (data.by_time || []).map(function(d) {{ return d.requests; }});
            var anyGroupTime = groupArr.some(function(v) {{ return v > 0; }});

            if (chart) chart.destroy();
            var isDark = document.documentElement.getAttribute("data-theme") === "dark"
                         || (!document.documentElement.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
            var gridColor = isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.08)";
            var textColor = isDark ? "#aaa" : "#666";

            // Wenn Group-Daten vorhanden: stacked bar (Owner unten blau + Group oben lila).
            // Sonst: single blue bar wie vorher.
            var barDatasets;
            if (anyGroupTime) {{
              barDatasets = [{{
                label: "Owner",
                data: ownerArr,
                backgroundColor: "rgba(59, 130, 246, 0.6)",
                borderColor: "rgba(59, 130, 246, 1)",
                borderWidth: 1,
                borderRadius: 4,
                stack: "time",
                yAxisID: "y"
              }}, {{
                label: "Gruppe",
                data: groupArr,
                backgroundColor: "rgba(168, 85, 247, 0.7)",
                borderColor: "rgba(168, 85, 247, 1)",
                borderWidth: 1,
                borderRadius: 4,
                stack: "time",
                yAxisID: "y"
              }}];
            }} else {{
              barDatasets = [{{
                label: "Sekunden",
                data: totalsArr,
                backgroundColor: "rgba(59, 130, 246, 0.6)",
                borderColor: "rgba(59, 130, 246, 1)",
                borderWidth: 1,
                borderRadius: 4,
                yAxisID: "y"
              }}];
            }}

            chart = new Chart(ctx, {{
              type: "bar",
              data: {{
                labels: labels,
                datasets: barDatasets.concat([{{
                  label: "Anfragen",
                  data: counts,
                  type: "line",
                  borderColor: "rgba(249, 115, 22, 0.8)",
                  backgroundColor: "rgba(249, 115, 22, 0.1)",
                  borderWidth: 2,
                  pointRadius: 3,
                  fill: true,
                  yAxisID: "y1"
                }}])
              }},
              options: {{
                responsive: true,
                maintainAspectRatio: false,
                interaction: {{ mode: "index", intersect: false }},
                plugins: {{
                  legend: {{ labels: {{ color: textColor, font: {{ size: 12 }} }} }},
                  tooltip: {{
                    callbacks: {{
                      label: function(ctx) {{
                        if (ctx.dataset.label === "Anfragen") return "Anfragen: " + ctx.raw;
                        return ctx.dataset.label + ": " + fmtSec(ctx.raw);
                      }}
                    }}
                  }}
                }},
                scales: {{
                  x: {{ ticks: {{ color: textColor, font: {{ size: 11 }} }}, grid: {{ color: gridColor }}, stacked: anyGroupTime }},
                  y: {{
                    type: "linear", position: "left",
                    title: {{ display: true, text: "Sekunden", color: textColor }},
                    ticks: {{ color: textColor }}, grid: {{ color: gridColor }},
                    beginAtZero: true,
                    stacked: anyGroupTime
                  }},
                  y1: {{
                    type: "linear", position: "right",
                    title: {{ display: true, text: "Anfragen", color: textColor }},
                    ticks: {{ color: textColor, stepSize: 1 }}, grid: {{ drawOnChartArea: false }},
                    beginAtZero: true
                  }}
                }}
              }}
            }});

            // Tables — by_model: Gruppen-Spalte nur wenn irgendein Modell Group-Usage hat
            var hasGroup = (data.by_model || []).some(function(r) {{ return (r.group_seconds || 0) > 0; }});
            var modelCols = [{{key: "model", label: "Modell"}}, {{key: "seconds", label: "Zeit", fmt: "sec"}}, {{key: "requests", label: "Anfragen"}}];
            if (hasGroup) {{
              modelCols.push({{key: "group_seconds", label: "Gruppen-Zeit", fmt: "sec"}});
              modelCols.push({{key: "group_requests", label: "Gruppen-Anfragen"}});
            }}
            document.getElementById("table-model").innerHTML = buildTable(data.by_model, modelCols);
            document.getElementById("table-type").innerHTML = buildTable(
              data.by_type,
              [{{key: "type", label: "Typ"}}, {{key: "seconds", label: "Zeit", fmt: "sec"}}, {{key: "requests", label: "Anfragen"}}]
            );
          }})
          .catch(function(e) {{
            document.getElementById("sum-time").textContent = "Fehler";
            console.error("Usage fetch error:", e);
          }});
      }}

      btns.forEach(function(btn) {{
        btn.addEventListener("click", function() {{
          btns.forEach(function(b) {{ b.classList.remove("active"); }});
          btn.classList.add("active");
          document.getElementById("range-from").value = "";
          document.getElementById("range-to").value = "";
          loadData(btn.getAttribute("data-period"));
        }});
      }});

      document.getElementById("range-go").addEventListener("click", function() {{
        var f = document.getElementById("range-from").value;
        var t = document.getElementById("range-to").value;
        if (!f || !t) return;
        btns.forEach(function(b) {{ b.classList.remove("active"); }});
        loadData(null, f, t);
      }});

      loadData("day");

      // Slot-Cache Section
      function fmtTs(ts) {{
        if (!ts) return "—";
        try {{ return new Date(ts*1000).toLocaleString(); }} catch (e) {{ return ts; }}
      }}
      function loadSlotCache() {{
        fetch("/api/slot_cache/list", {{credentials: "same-origin"}})
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            var section = document.getElementById("slot-cache-section");
            if (!data.enabled) {{ section.style.display = "none"; return; }}
            section.style.display = "block";
            var s = data.stats || {{}};
            var summary = (
              "Files: " + (s.total_files || 0) +
              " · Hits 7d: " + (s.hits_7d || 0) +
              " · Misses 7d: " + (s.misses_7d || 0) +
              " · Saves 7d: " + (s.saves_7d || 0) +
              " · Instance: " + (s.instance_id || "—")
            );
            document.getElementById("sc-stats-summary").textContent = summary;
            var entries = data.entries || [];
            if (!entries.length) {{
              document.getElementById("sc-table").innerHTML = '<div class="empty-hint">Noch keine cached Conversations.</div>';
              return;
            }}
            var html = '<table style="width:100%;border-collapse:collapse;font-size:0.85em;">';
            html += '<tr><th style="text-align:left;padding:0.4em;">Conv-ID</th><th style="text-align:left;padding:0.4em;">Modell</th><th style="text-align:left;padding:0.4em;">Tokens</th><th style="text-align:left;padding:0.4em;">Erstellt</th><th style="text-align:left;padding:0.4em;">Zuletzt</th><th></th></tr>';
            entries.sort(function(a,b) {{ return (b.last_used_ts||0) - (a.last_used_ts||0); }});
            entries.forEach(function(e) {{
              html += '<tr>';
              html += '<td style="padding:0.4em;font-family:monospace;font-size:0.85em;">' + (e.conv_id||"") + '</td>';
              html += '<td style="padding:0.4em;">' + (e.model||"") + '</td>';
              html += '<td style="padding:0.4em;">' + (e.prompt_token_count||0) + '</td>';
              html += '<td style="padding:0.4em;">' + fmtTs(e.created_ts) + '</td>';
              html += '<td style="padding:0.4em;">' + fmtTs(e.last_used_ts) + '</td>';
              html += '<td style="padding:0.4em;"><button class="btn sc-forget-btn" data-conv="' + encodeURIComponent(e.conv_id||"") + '" style="font-size:0.85em;padding:0.3em 0.6em;">Forget</button></td>';
              html += '</tr>';
            }});
            html += '</table>';
            document.getElementById("sc-table").innerHTML = html;
            document.querySelectorAll(".sc-forget-btn").forEach(function(b) {{
              b.addEventListener("click", function() {{
                var conv = decodeURIComponent(b.getAttribute("data-conv"));
                if (!confirm("Cache-Entry vergessen?\\n" + conv)) return;
                fetch("/api/slot_cache/" + encodeURIComponent(conv), {{method:"DELETE", credentials:"same-origin"}})
                  .then(function() {{ loadSlotCache(); }});
              }});
            }});
          }})
          .catch(function(e) {{ console.error("slot_cache fetch:", e); }});
      }}
      var scBtn = document.getElementById("sc-cleanup-btn");
      if (scBtn) {{
        scBtn.addEventListener("click", function() {{
          var msg = document.getElementById("sc-cleanup-msg");
          msg.textContent = "Läuft...";
          fetch("/api/slot_cache/cleanup", {{method:"POST", credentials:"same-origin"}})
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
              msg.textContent = "Entfernt: " + (data.removed_lru_ttl||0) + " (LRU/TTL), " + (data.removed_unknown_models||0) + " (unknown).";
              loadSlotCache();
            }})
            .catch(function() {{ msg.textContent = "Fehler"; }});
        }});
      }}
      loadSlotCache();
    }})();
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/workspace", response_class=HTMLResponse)
async def workspace_page(request: Request):
    """Workspace Explorer für den Agent-Workspace."""
    _require_token(request)
    token = request.query_params.get("token", "") or request.cookies.get("ma_token", "")
    tq = _token_query(token)
    token_val = token.replace("?token=", "").replace("&token=", "")
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Workspace Explorer – MiniAssistant</title>
<link rel="icon" href="/favicon.ico">
<style>
{_COMMON_CSS}
.container {{ max-width: 1400px !important; }}
.ws-layout {{display:flex;gap:1rem;align-items:flex-start;height:calc(100vh - 180px)}}
.ws-tree {{width:240px;flex-shrink:0;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow-y:auto;overflow-x:hidden;height:100%}}
.ws-viewer {{flex:1;min-width:0;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.2rem;overflow:auto;height:100%}}
.ws-tree ul {{list-style:none;margin:0;padding:0}}
.ws-tree-hdr {{display:flex;background:var(--card);border:1px solid var(--border);border-bottom:none;border-radius:var(--radius) var(--radius) 0 0}}
.ws-tab {{flex:1;padding:0.4rem 0.3rem;text-align:center;cursor:pointer;background:none;border:none;border-bottom:2px solid transparent;font-size:0.8rem;color:var(--muted)}}
.ws-tab.active {{color:var(--primary);font-weight:600;border-bottom-color:var(--primary)}}
.ws-tab:hover:not(.active) {{color:var(--primary)}}
.ws-refresh-btn {{background:none;border:none;cursor:pointer;padding:0.4rem 0.55rem;color:var(--muted);font-size:1.1rem;line-height:1;transition:color 0.15s;display:inline-flex;align-items:center}}
.ws-refresh-btn:hover {{color:var(--primary)}}
.ws-refresh-btn.spinning {{animation:ws-spin 0.5s linear}}
.ws-tree li {{border-bottom:1px solid var(--border)}}
.ws-tree li:last-child {{border-bottom:none}}
.ws-tree li.ws-frow {{display:flex;align-items:stretch}}
.ws-frow > a {{flex:1;min-width:0}}
.ws-tree a {{display:flex;align-items:center;gap:0.4rem;padding:0.45rem 0.8rem;color:var(--text);text-decoration:none;font-size:0.88rem;cursor:pointer}}
.ws-tree a:hover {{background:var(--border);color:var(--primary)}}
.ws-tree a.active {{background:var(--primary);color:#fff}}
.ws-del-btn {{background:none;border:none;cursor:pointer;padding:0 0.6rem;color:var(--muted);font-size:0.88rem;flex-shrink:0;line-height:1}}
.ws-del-btn:hover {{color:#e53e3e}}
.ws-empty-trash-btn {{display:block;width:calc(100% - 1.2rem);margin:0.6rem auto;background:#e53e3e;color:#fff;border:none;border-radius:var(--radius);padding:0.45rem;cursor:pointer;font-size:0.82rem}}
.ws-empty-trash-btn:hover {{opacity:0.85}}
.ws-empty {{color:var(--muted);font-size:0.9rem;padding:1rem}}
.ws-viewer img {{max-width:100%;max-height:calc(100vh - 260px);object-fit:contain;display:block;border-radius:var(--radius);cursor:zoom-in}}
.ws-lightbox {{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;cursor:zoom-out}}
.ws-lightbox img {{max-width:95vw;max-height:95vh;object-fit:contain;border-radius:var(--radius);box-shadow:0 0 40px rgba(0,0,0,0.5)}}
.ws-viewer pre {{background:var(--bg);padding:1rem;border-radius:var(--radius);overflow:auto;font-size:0.82rem;white-space:pre-wrap;word-break:break-all}}
.ws-viewer .md-body h1,.ws-viewer .md-body h2,.ws-viewer .md-body h3 {{margin-top:1rem}}
.ws-viewer .md-body code {{background:var(--bg);padding:0.1em 0.3em;border-radius:3px;font-size:0.85em}}
.ws-viewer .md-body pre {{background:var(--bg);padding:0.8rem;border-radius:var(--radius);overflow:auto}}
.ws-viewer .md-body pre code {{background:none;padding:0}}
.ws-viewer .md-body table {{border-collapse:collapse;width:100%}}
.ws-viewer .md-body td,.ws-viewer .md-body th {{border:1px solid var(--border);padding:0.35rem 0.6rem}}
.ws-viewer .md-body th {{background:var(--bg)}}
@keyframes ws-spin {{from{{transform:rotate(0deg)}}to{{transform:rotate(360deg)}}}}
.ws-filename {{font-weight:600;margin-bottom:0.8rem;color:var(--primary);font-size:0.95rem;word-break:break-all}}
.ws-sort-bar {{display:flex;align-items:center;gap:0.3rem;padding:0.3rem 0.5rem;background:var(--card);border:1px solid var(--border);border-top:none;font-size:0.75rem}}
.ws-sort-bar select {{font-size:0.75rem;padding:0.15rem 0.3rem;border:1px solid var(--border);border-radius:3px;background:var(--bg);color:var(--text);cursor:pointer}}
.ws-sort-bar button {{font-size:0.75rem;padding:0.15rem 0.4rem;border:1px solid var(--border);border-radius:3px;background:var(--bg);color:var(--text);cursor:pointer;line-height:1;white-space:nowrap}}
.ws-sort-bar button:hover {{color:var(--primary);border-color:var(--primary)}}
.ws-modal {{position:fixed;inset:0;background:rgba(0,0,0,0.45);z-index:10000;display:flex;align-items:center;justify-content:center}}
.ws-modal-box {{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.3rem;max-width:420px;box-shadow:var(--shadow)}}
.ws-modal-msg {{margin-bottom:1rem;color:var(--text);font-size:0.95rem;word-break:break-word}}
.ws-modal-btns {{display:flex;gap:0.6rem;justify-content:flex-end}}
</style>
<script src="/static/marked.umd.js"></script>
    <script src="/static/purify.min.js"></script>
</head>
<body>
<div class="container">
<div class="card">
  <div class="usage-header" style="margin-bottom:1.2rem">
    <img src="/static/miniassistant.png" alt="Logo" style="height:2rem;width:auto">
    <h1>Workspace Explorer</h1>
    <div class="usage-nav">
      <a href="/{tq}" class="btn btn-outline">Startseite</a>
    </div>
  </div>
  <div class="ws-layout">
    <div style="width:240px;flex-shrink:0;height:100%;display:flex;flex-direction:column">
      <div class="ws-tree-hdr">
        <button class="ws-tab active" id="ws-tab-ws" onclick="wsSwitchTab('workspace')">Workspace</button>
        <button class="ws-tab" id="ws-tab-trash" onclick="wsSwitchTab('trash')">🗑️ Papierkorb</button>
        <button class="ws-refresh-btn" id="ws-refresh-btn" onclick="wsRefresh()" title="Aktualisieren">&#x21BB;</button>
      </div>
      <div class="ws-sort-bar" id="ws-sort-bar">
        <span style="color:var(--muted)">Sortierung:</span>
        <select id="ws-sort-by" onchange="wsSortChanged()">
          <option value="name">Name</option>
          <option value="date">Datum</option>
        </select>
        <button id="ws-sort-dir" onclick="wsToggleSortDir()" title="Sortierrichtung umschalten">↑ A-Z</button>
      </div>
      <div class="ws-tree" id="ws-tree" style="flex:1;border-top:none;border-radius:0 0 var(--radius) var(--radius)"><div class="ws-empty">Lade...</div></div>
    </div>
    <div class="ws-viewer" id="ws-viewer"><div class="ws-empty">Datei auswählen</div></div>
  </div>
</div>
</div>
<div id="ws-modal" class="ws-modal" style="display:none">
  <div class="ws-modal-box">
    <div id="ws-modal-msg" class="ws-modal-msg"></div>
    <div class="ws-modal-btns">
      <button id="ws-modal-no" class="btn btn-outline">Abbrechen</button>
      <button id="ws-modal-yes" class="btn btn-primary">Ja</button>
    </div>
  </div>
</div>
<script>
var WS_TOKEN = {repr(token_val)};
var WS_CURRENT_PATH = '';

function wsConfirm(msg) {{
  return new Promise(function(resolve) {{
    var m = document.getElementById('ws-modal');
    document.getElementById('ws-modal-msg').textContent = msg;
    m.style.display = 'flex';
    var yes = document.getElementById('ws-modal-yes');
    var no = document.getElementById('ws-modal-no');
    function cleanup(val) {{ m.style.display = 'none'; yes.onclick = null; no.onclick = null; resolve(val); }}
    yes.onclick = function() {{ cleanup(true); }};
    no.onclick = function() {{ cleanup(false); }};
  }});
}}

function wsGetSort() {{
  return {{
    by: localStorage.getItem('ws_sort_by') || 'name',
    dir: localStorage.getItem('ws_sort_dir') || 'asc'
  }};
}}

function wsSetSort(by, dir) {{
  localStorage.setItem('ws_sort_by', by);
  localStorage.setItem('ws_sort_dir', dir);
}}

function wsUpdateSortUI() {{
  var s = wsGetSort();
  document.getElementById('ws-sort-by').value = s.by;
  var btn = document.getElementById('ws-sort-dir');
  if (s.by === 'name') {{
    btn.textContent = s.dir === 'asc' ? '↑ A-Z' : '↓ Z-A';
  }} else {{
    btn.textContent = s.dir === 'asc' ? '↑ Älteste' : '↓ Neueste';
  }}
}}

function wsSortChanged() {{
  var by = document.getElementById('ws-sort-by').value;
  // Datum → neueste zuerst (desc), Name → A-Z (asc)
  var dir = (by === 'date') ? 'desc' : 'asc';
  wsSetSort(by, dir);
  wsUpdateSortUI();
  wsRefreshTree();
}}

function wsToggleSortDir() {{
  var s = wsGetSort();
  wsSetSort(s.by, s.dir === 'asc' ? 'desc' : 'asc');
  wsUpdateSortUI();
  wsRefreshTree();
}}

function wsRefreshTree() {{
  var isTrash = document.getElementById('ws-tab-trash').classList.contains('active');
  if (isTrash) wsLoadTrash();
  else wsLoadTree(WS_CURRENT_PATH);
}}

function wsSortItems(items) {{
  var s = wsGetSort();
  var dirs = items.filter(function(i) {{ return i.type === 'dir'; }});
  var files = items.filter(function(i) {{ return i.type !== 'dir'; }});
  function cmp(a, b) {{
    if (s.by === 'date') {{
      var at = a.mtime_ts || 0, bt = b.mtime_ts || 0;
      return s.dir === 'asc' ? at - bt : bt - at;
    }} else {{
      var an = a.name.toLowerCase(), bn = b.name.toLowerCase();
      var r = an < bn ? -1 : (an > bn ? 1 : 0);
      return s.dir === 'asc' ? r : -r;
    }}
  }}
  dirs.sort(cmp);
  files.sort(cmp);
  return dirs.concat(files);
}}

function wsApiUrl(endpoint, params) {{
  var u = new URL('/api/workspace/' + endpoint, location.origin);
  if (WS_TOKEN) u.searchParams.set('token', WS_TOKEN);
  if (params) Object.keys(params).forEach(function(k) {{ u.searchParams.set(k, params[k]); }});
  return u.toString();
}}

function wsIcon(item) {{
  if (item.type === 'dir') return '📁';
  var e = item.name.split('.').pop().toLowerCase();
  if (['png','jpg','jpeg','gif','webp','bmp'].includes(e)) return '🖼️';
  if (e === 'md') return '📝';
  if (['py','sh','js','ts','json','yaml','yml'].includes(e)) return '💻';
  return '📄';
}}

function wsEscape(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function wsSwitchTab(mode) {{
  document.getElementById('ws-tab-ws').classList.toggle('active', mode === 'workspace');
  document.getElementById('ws-tab-trash').classList.toggle('active', mode === 'trash');
  document.getElementById('ws-viewer').innerHTML = '<div class="ws-empty">Datei auswählen</div>';
  if (mode === 'workspace') {{ wsLoadTree(WS_CURRENT_PATH); }}
  else {{ wsLoadTrash(); }}
}}

function wsRefresh() {{
  var btn = document.getElementById('ws-refresh-btn');
  btn.classList.remove('spinning');
  void btn.offsetWidth;
  btn.classList.add('spinning');
  setTimeout(function() {{ btn.classList.remove('spinning'); }}, 600);
  var isTrash = document.getElementById('ws-tab-trash').classList.contains('active');
  if (isTrash) {{ wsLoadTrash(); }}
  else {{ wsLoadTree(WS_CURRENT_PATH); }}
}}

function wsLoadTree(path, keepScroll) {{
  WS_CURRENT_PATH = path || '';
  var tree = document.getElementById('ws-tree');
  var savedScroll = keepScroll ? tree.scrollTop : 0;
  tree.innerHTML = '<div class="ws-empty">Lade...</div>';
  fetch(wsApiUrl('files', {{path: WS_CURRENT_PATH}}))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ tree.innerHTML = '<div class="ws-empty">' + wsEscape(data.error) + '</div>'; return; }}
      var sortedItems = wsSortItems(data.items);
      var html = '<ul>';
      if (WS_CURRENT_PATH) {{
        var parent = WS_CURRENT_PATH.includes('/') ? WS_CURRENT_PATH.split('/').slice(0,-1).join('/') : '';
        html += '<li><a data-dir="' + wsEscape(parent) + '">⬆️ ..</a></li>';
      }}
      sortedItems.forEach(function(item) {{
        if (item.type === 'dir') {{
          html += '<li class="ws-frow"><a data-dir="' + wsEscape(item.path) + '" title="' + wsEscape(item.name) + '" style="flex:1">' + wsIcon(item) + ' ' + wsEscape(item.name) + '</a>'
                + '<button class="ws-del-btn" data-delete="' + wsEscape(item.path) + '" title="In Papierkorb verschieben">🗑️</button></li>';
        }} else {{
          var mtime = item.mtime ? '<div style="font-size:0.7rem;color:var(--muted);margin-top:1px">' + wsEscape(item.mtime) + '</div>' : '';
          html += '<li class="ws-frow"><a data-file="' + wsEscape(item.path) + '" title="' + wsEscape(item.name) + '" style="align-items:flex-start">'
                + '<span style="margin-top:2px">' + wsIcon(item) + '</span>'
                + '<span style="flex:1;min-width:0">'
                + '<div style="display:flex;align-items:baseline;gap:0.3rem"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + wsEscape(item.name) + '</span>'
                + '<span style="color:var(--muted);font-size:0.75rem;flex-shrink:0;margin-left:auto">' + wsEscape(item.size) + '</span></div>'
                + mtime
                + '</span></a>'
                + '<button class="ws-del-btn" data-delete="' + wsEscape(item.path) + '" title="In Papierkorb verschieben">🗑️</button></li>';
        }}
      }});
      if (!data.items.length) html += '<li><div class="ws-empty">Leer</div></li>';
      html += '</ul>';
      tree.innerHTML = html;
      tree.querySelectorAll('a[data-dir]').forEach(function(a) {{
        a.addEventListener('click', function(e) {{ e.preventDefault(); wsLoadTree(a.getAttribute('data-dir')); }});
      }});
      tree.querySelectorAll('a[data-file]').forEach(function(a) {{
        a.addEventListener('click', function(e) {{ e.preventDefault(); wsLoadFile(a.getAttribute('data-file')); }});
      }});
      tree.querySelectorAll('button[data-delete]').forEach(function(btn) {{
        btn.addEventListener('click', function(e) {{ e.stopPropagation(); wsDeleteFile(btn.getAttribute('data-delete')); }});
      }});
      if (savedScroll) tree.scrollTop = savedScroll;
    }})
    .catch(function(err) {{ tree.innerHTML = '<div class="ws-empty">Fehler: ' + wsEscape(String(err)) + '</div>'; }});
}}

function wsLoadTrash() {{
  var tree = document.getElementById('ws-tree');
  tree.innerHTML = '<div class="ws-empty">Lade...</div>';
  fetch(wsApiUrl('trash/files'))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ tree.innerHTML = '<div class="ws-empty">' + wsEscape(data.error) + '</div>'; return; }}
      var html = '<button class="ws-empty-trash-btn" id="empty-trash-btn">Papierkorb leeren</button><ul>';
      data.items.forEach(function(item) {{
        html += '<li><a data-trash-file="' + wsEscape(item.path) + '" title="' + wsEscape(item.name) + '">' + wsIcon(item) + ' ' + wsEscape(item.name)
              + '<span style="color:var(--muted);font-size:0.75rem;margin-left:auto;flex-shrink:0">' + wsEscape(item.size) + '</span></a></li>';
      }});
      if (!data.items.length) html += '<li><div class="ws-empty">Papierkorb ist leer</div></li>';
      html += '</ul>';
      tree.innerHTML = html;
      var emptyBtn = document.getElementById('empty-trash-btn');
      if (emptyBtn) emptyBtn.addEventListener('click', wsEmptyTrash);
      tree.querySelectorAll('a[data-trash-file]').forEach(function(a) {{
        a.addEventListener('click', function(e) {{ e.preventDefault(); wsLoadTrashFile(a.getAttribute('data-trash-file')); }});
      }});
    }})
    .catch(function(err) {{ tree.innerHTML = '<div class="ws-empty">Fehler: ' + wsEscape(String(err)) + '</div>'; }});
}}

async function wsDeleteFile(path) {{
  if (!(await wsConfirm('Datei „' + path + '“ in den Papierkorb verschieben?'))) return;
  fetch(wsApiUrl('delete'), {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{path:path}})}})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ alert(data.error); return; }}
      wsLoadTree(WS_CURRENT_PATH, true);
      document.getElementById('ws-viewer').innerHTML = '<div class="ws-empty">Datei in Papierkorb verschoben</div>';
    }})
    .catch(function(err) {{ alert('Fehler: ' + err); }});
}}

async function wsEmptyTrash() {{
  if (!(await wsConfirm('Papierkorb wirklich leeren? Alle Dateien werden endgültig gelöscht.'))) return;
  fetch(wsApiUrl('trash/empty'), {{method:'POST',headers:{{'Content-Type':'application/json'}}}})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ alert(data.error); return; }}
      wsLoadTrash();
      document.getElementById('ws-viewer').innerHTML = '<div class="ws-empty">Papierkorb geleert</div>';
    }})
    .catch(function(err) {{ alert('Fehler: ' + err); }});
}}

function wsLoadFile(path) {{
  document.querySelectorAll('#ws-tree a').forEach(function(a) {{ a.classList.remove('active'); }});
  document.querySelectorAll('#ws-tree a[data-file="' + path.replace(/"/g,'\\"') + '"]').forEach(function(a) {{ a.classList.add('active'); }});
  var viewer = document.getElementById('ws-viewer');
  viewer.innerHTML = '<div class="ws-empty">Lade...</div>';
  fetch(wsApiUrl('file', {{path: path}}))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ viewer.innerHTML = '<div class="ws-empty">' + wsEscape(data.error) + '</div>'; return; }}
      var name = path.split('/').pop();
      var html = '<div class="ws-filename">' + wsEscape(name) + '</div>';
      if (data.type === 'image') {{
        var imgUrl = wsApiUrl('raw', {{path: path}});
        html += '<img src="' + imgUrl + '" alt="' + wsEscape(name) + '" onclick="wsOpenLightbox(this.src)" title="Klicken für Originalgröße">';
      }} else if (data.type === 'markdown') {{
        html += '<div class="md-body">' + DOMPurify.sanitize(marked.parse(data.content)) + '</div>';
      }} else {{
        html += '<pre>' + wsEscape(data.content) + '</pre>';
      }}
      viewer.innerHTML = html;
    }})
    .catch(function(err) {{ viewer.innerHTML = '<div class="ws-empty">Fehler: ' + wsEscape(String(err)) + '</div>'; }});
}}

function wsLoadTrashFile(path) {{
  document.querySelectorAll('#ws-tree a').forEach(function(a) {{ a.classList.remove('active'); }});
  document.querySelectorAll('#ws-tree a[data-trash-file="' + path.replace(/"/g,'\\"') + '"]').forEach(function(a) {{ a.classList.add('active'); }});
  var viewer = document.getElementById('ws-viewer');
  viewer.innerHTML = '<div class="ws-empty">Lade...</div>';
  fetch(wsApiUrl('trash/file', {{path: path}}))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ viewer.innerHTML = '<div class="ws-empty">' + wsEscape(data.error) + '</div>'; return; }}
      var name = path.split('/').pop();
      var html = '<div class="ws-filename">' + wsEscape(name) + '</div>';
      if (data.type === 'image') {{
        var imgUrl = wsApiUrl('trash/raw', {{path: path}});
        html += '<img src="' + imgUrl + '" alt="' + wsEscape(name) + '" onclick="wsOpenLightbox(this.src)" title="Klicken für Originalgröße">';
      }} else if (data.type === 'markdown') {{
        html += '<div class="md-body">' + DOMPurify.sanitize(marked.parse(data.content)) + '</div>';
      }} else {{
        html += '<pre>' + wsEscape(data.content) + '</pre>';
      }}
      viewer.innerHTML = html;
    }})
    .catch(function(err) {{ viewer.innerHTML = '<div class="ws-empty">Fehler: ' + wsEscape(String(err)) + '</div>'; }});
}}

function wsOpenLightbox(src) {{
  var overlay = document.createElement('div');
  overlay.className = 'ws-lightbox';
  overlay.innerHTML = '<img src="' + src.replace(/"/g, '&quot;') + '">';
  overlay.addEventListener('click', function() {{ overlay.remove(); }});
  document.addEventListener('keydown', function handler(e) {{
    if (e.key === 'Escape') {{ overlay.remove(); document.removeEventListener('keydown', handler); }}
  }});
  document.body.appendChild(overlay);
}}

wsUpdateSortUI();
wsLoadTree('');
</script>
{_THEME_JS}
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/api/workspace/files")
async def api_workspace_files(request: Request):
    """Listet Dateien/Ordner im Workspace-Verzeichnis."""
    _require_token(request)
    config = load_config()
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    rel = request.query_params.get("path", "").strip().lstrip("/")
    target = (workspace / rel).resolve()
    if not _is_path_within(target, workspace):
        raise HTTPException(status_code=403, detail="Pfad außerhalb des Workspace")
    if not target.exists():
        return JSONResponse({"error": "Verzeichnis nicht gefunden", "items": []})
    if not target.is_dir():
        return JSONResponse({"error": "Kein Verzeichnis", "items": []})
    items = []
    import time as _time
    _cur_year = datetime.datetime.now().year
    for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name.startswith("."):
            continue
        rel_path = str(p.relative_to(workspace))
        st = p.stat()
        if p.is_dir():
            items.append({"name": p.name, "type": "dir", "path": rel_path, "size": "", "mtime_ts": st.st_mtime})
        else:
            size = st.st_size
            size_str = f"{size}B" if size < 1024 else (f"{size//1024}KB" if size < 1024*1024 else f"{size//1024//1024}MB")
            mt = datetime.datetime.fromtimestamp(st.st_mtime)
            if mt.year == _cur_year:
                mtime_str = mt.strftime("%d.%m. %H:%M")
            else:
                mtime_str = mt.strftime("%d.%m.%y %H:%M")
            items.append({"name": p.name, "type": "file", "path": rel_path, "size": size_str, "mtime": mtime_str, "mtime_ts": st.st_mtime})
    return JSONResponse({"items": items, "path": str(rel)})


@app.get("/api/workspace/file")
async def api_workspace_file(request: Request):
    """Liefert den Inhalt einer Datei im Workspace."""
    _require_token(request)
    config = load_config()
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    rel = request.query_params.get("path", "").strip().lstrip("/")
    target = (workspace / rel).resolve()
    if not _is_path_within(target, workspace):
        raise HTTPException(status_code=403, detail="Pfad außerhalb des Workspace")
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"})
    ext = target.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return JSONResponse({"type": "image", "name": target.name})
    if ext == ".md":
        try:
            return JSONResponse({"type": "markdown", "content": target.read_text(errors="replace")})
        except Exception as e:
            return JSONResponse({"error": str(e)})
    text_exts = {".txt", ".log", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".csv", ".toml", ".ini", ".cfg"}
    if ext in text_exts or target.stat().st_size < 500_000:
        try:
            return JSONResponse({"type": "text", "content": target.read_text(errors="replace")[:100_000]})
        except Exception as e:
            return JSONResponse({"error": str(e)})
    return JSONResponse({"error": f"Dateityp '{ext}' wird nicht unterstützt"})


@app.get("/api/img/{img_name}")
async def api_serve_generated_image(img_name: str, request: Request):
    """Liefert ein generiertes Bild aus workspace/images/."""
    _require_token(request)
    config = load_config()
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    if not workspace.is_dir():
        raise HTTPException(status_code=404)
    _mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp"}
    # img_name kann mit oder ohne Extension kommen — beide Fälle abdecken
    for ext, mime in _mime_map.items():
        if img_name.lower().endswith(ext):
            # img_name hat bereits Extension: direkt als Dateiname versuchen
            candidate = (workspace / "images" / img_name).resolve()
            if _is_path_within(candidate, workspace) and candidate.exists():
                return FileResponse(str(candidate), media_type=mime)
        candidate = (workspace / "images" / f"{img_name}{ext}").resolve()
        if _is_path_within(candidate, workspace) and candidate.exists():
            return FileResponse(str(candidate), media_type=mime)
    raise HTTPException(status_code=404)


@app.get("/api/audio/{audio_name}")
async def api_serve_audio(audio_name: str, request: Request):
    """Liefert eine generierte Audio-Datei aus workspace/audio/."""
    _require_token(request)
    config = load_config()
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    if not workspace.is_dir():
        raise HTTPException(status_code=404)
    # audio_name kann mit oder ohne .wav kommen
    for ext in (".wav", ".mp3", ".ogg"):
        if audio_name.lower().endswith(ext):
            candidate = (workspace / "audio" / audio_name).resolve()
            if _is_path_within(candidate, workspace) and candidate.exists():
                _mime = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}[ext]
                return FileResponse(str(candidate), media_type=_mime)
        candidate = (workspace / "audio" / f"{audio_name}{ext}").resolve()
        if _is_path_within(candidate, workspace) and candidate.exists():
            _mime = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}[ext]
            return FileResponse(str(candidate), media_type=_mime)
    raise HTTPException(status_code=404)


@app.get("/api/workspace/raw")
async def api_workspace_raw(request: Request):
    """Liefert eine Datei als Binary (für Bilder)."""
    _require_token(request)
    config = load_config()
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    rel = request.query_params.get("path", "").strip().lstrip("/")
    target = (workspace / rel).resolve()
    if not _is_path_within(target, workspace):
        raise HTTPException(status_code=403, detail="Pfad außerhalb des Workspace")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404)
    _mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
    media_type = _mime_map.get(target.suffix.lower())
    return FileResponse(str(target), media_type=media_type)


@app.post("/api/workspace/delete")
async def api_workspace_delete(request: Request):
    """Verschiebt eine Datei aus dem Workspace in den Papierkorb."""
    _require_token(request)
    body = await request.json()
    rel = (body.get("path") or "").strip().lstrip("/")
    if not rel:
        return JSONResponse({"error": "Kein Pfad angegeben"})
    config = load_config()
    workspace = Path(config.get("workspace") or "").expanduser().resolve()
    target = (workspace / rel).resolve()
    if not _is_path_within(target, workspace):
        raise HTTPException(status_code=403, detail="Pfad außerhalb des Workspace")
    if not target.exists():
        return JSONResponse({"error": "Nicht gefunden"})
    trash_dir = Path(config.get("trash_dir") or "~/.trash").expanduser().resolve()
    trash_dir.mkdir(parents=True, exist_ok=True)
    stem = target.name
    suf = "" if target.is_dir() else target.suffix
    base = target.stem if not target.is_dir() else target.name
    dest = trash_dir / target.name
    i = 1
    while dest.exists():
        dest = trash_dir / f"{base}_{i}{suf}"
        i += 1
    shutil.move(str(target), str(dest))
    return JSONResponse({"ok": True})


@app.get("/api/workspace/trash/files")
async def api_workspace_trash_files(request: Request):
    """Listet Dateien im Papierkorb."""
    _require_token(request)
    config = load_config()
    trash_dir = Path(config.get("trash_dir") or "~/.trash").expanduser().resolve()
    if not trash_dir.exists():
        return JSONResponse({"items": [], "path": ""})
    items = []
    for p in sorted(trash_dir.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            items.append({"name": p.name, "type": "dir", "path": p.name, "size": ""})
        else:
            size = p.stat().st_size
            size_str = f"{size}B" if size < 1024 else (f"{size//1024}KB" if size < 1024*1024 else f"{size//1024//1024}MB")
            items.append({"name": p.name, "type": "file", "path": p.name, "size": size_str})
    return JSONResponse({"items": items, "path": ""})


@app.get("/api/workspace/trash/file")
async def api_workspace_trash_file(request: Request):
    """Liefert den Inhalt einer Datei im Papierkorb."""
    _require_token(request)
    config = load_config()
    trash_dir = Path(config.get("trash_dir") or "~/.trash").expanduser().resolve()
    rel = request.query_params.get("path", "").strip().lstrip("/")
    target = (trash_dir / rel).resolve()
    if not _is_path_within(target, trash_dir):
        raise HTTPException(status_code=403, detail="Pfad außerhalb des Papierkorbs")
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "Datei nicht gefunden"})
    ext = target.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return JSONResponse({"type": "image", "name": target.name})
    if ext == ".md":
        try:
            return JSONResponse({"type": "markdown", "content": target.read_text(errors="replace")})
        except Exception as e:
            return JSONResponse({"error": str(e)})
    text_exts = {".txt", ".log", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".csv", ".toml", ".ini", ".cfg"}
    if ext in text_exts or target.stat().st_size < 500_000:
        try:
            return JSONResponse({"type": "text", "content": target.read_text(errors="replace")[:100_000]})
        except Exception as e:
            return JSONResponse({"error": str(e)})
    return JSONResponse({"error": f"Dateityp '{ext}' wird nicht unterstützt"})


@app.get("/api/workspace/trash/raw")
async def api_workspace_trash_raw(request: Request):
    """Liefert eine Datei aus dem Papierkorb als Binary (für Bilder)."""
    _require_token(request)
    config = load_config()
    trash_dir = Path(config.get("trash_dir") or "~/.trash").expanduser().resolve()
    rel = request.query_params.get("path", "").strip().lstrip("/")
    target = (trash_dir / rel).resolve()
    if not _is_path_within(target, trash_dir):
        raise HTTPException(status_code=403, detail="Pfad außerhalb des Papierkorbs")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404)
    _mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
    media_type = _mime_map.get(target.suffix.lower())
    return FileResponse(str(target), media_type=media_type)


@app.post("/api/workspace/trash/empty")
async def api_workspace_trash_empty(request: Request):
    """Leert den Papierkorb."""
    _require_token(request)
    config = load_config()
    trash_dir = Path(config.get("trash_dir") or "~/.trash").expanduser().resolve()
    if not trash_dir.exists():
        return JSONResponse({"ok": True})
    for item in trash_dir.iterdir():
        if item.is_dir():
            shutil.rmtree(str(item))
        else:
            item.unlink()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Vorgaben-Editor: SOUL/IDENTITY/TOOLS/USER, basic_rules, directions, docs, prefs
# bearbeiten (token-authed, Pfad-validiert, explizites Speichern + Bestätigung im UI).
# ---------------------------------------------------------------------------
_AGENT_EDIT_EXTS = {".md", ".txt", ".yaml", ".yml"}
# (Label, Unterverzeichnis | "" = top-level, neue Dateien erlauben, löschbar→Papierkorb)
# Reihenfolge = Anzeige-Reihenfolge. Neue Dateien + Löschen nur bei Prefs/Directions:
# Basics + Regeln sind fix, Docs werden vom agent_loader aus einer festen Liste geladen.
_AGENT_EDIT_ROOTS = [
    ("Basics", "", False, False),
    ("Docs", "docs", False, False),
    ("Regeln", "basic_rules", False, False),
    ("Prefs", "prefs", True, True),
    ("Directions", "directions", True, True),
]
_AGENT_EDIT_SUBS = {r[1] for r in _AGENT_EDIT_ROOTS}
_AGENT_DELETABLE_SUBS = {r[1] for r in _AGENT_EDIT_ROOTS if r[3]}


def _agent_dir_resolved(config: dict) -> "Path | None":
    ad = (config.get("agent_dir") or "").strip()
    return Path(ad).expanduser().resolve() if ad else None


def _agent_safe_target(config: dict, rel: str) -> "Path | None":
    """Validiert rel-Pfad: innerhalb agent_dir, erlaubte Extension, in erlaubtem Root.
    Verhindert Path-Traversal und Schreiben in memory/mempalace/binäre Dateien."""
    base = _agent_dir_resolved(config)
    if not base:
        return None
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return None
    target = (base / rel).resolve()
    if not _is_path_within(target, base):
        return None
    if target.suffix.lower() not in _AGENT_EDIT_EXTS:
        return None
    try:
        parts = target.relative_to(base).parts
    except ValueError:
        return None
    if len(parts) == 1:
        sub = ""  # top-level (SOUL.md etc.)
    elif len(parts) == 2:
        sub = parts[0]
    else:
        return None  # nur eine Verzeichnisebene tief
    if sub not in _AGENT_EDIT_SUBS:
        return None
    return target


@app.get("/api/agent/files")
async def api_agent_files(request: Request):
    """Listet die editierbaren Vorgaben-Dateien, gruppiert nach Kategorie."""
    _require_token(request)
    config = load_config()
    base = _agent_dir_resolved(config)
    if not base or not base.exists():
        return JSONResponse({"agent_dir": str(base or ""), "groups": []})
    groups = []
    for label, sub, allow_new, deletable in _AGENT_EDIT_ROOTS:
        d = base if sub == "" else (base / sub)
        items = []
        if d.exists() and d.is_dir():
            for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
                if not p.is_file() or p.name.startswith("."):
                    continue
                if p.suffix.lower() not in _AGENT_EDIT_EXTS:
                    continue
                items.append({"name": p.name, "path": str(p.relative_to(base)), "bytes": p.stat().st_size})
        groups.append({"label": label, "sub": sub, "allow_new": allow_new, "deletable": deletable, "items": items})
    return JSONResponse({"agent_dir": str(base), "groups": groups})


@app.get("/api/agent/file")
async def api_agent_file_get(request: Request):
    """Liefert den Inhalt einer Vorgaben-Datei."""
    _require_token(request)
    config = load_config()
    target = _agent_safe_target(config, request.query_params.get("path", ""))
    if not target:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"path": request.query_params.get("path", ""), "content": content, "bytes": target.stat().st_size})


@app.post("/api/agent/file")
async def api_agent_file_save(request: Request):
    """Speichert eine Vorgaben-Datei (atomar). Legt neue Dateien in erlaubten Roots an."""
    _require_token(request)
    config = load_config()
    body = await request.json()
    rel = (body.get("path") or "").strip()
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content required")
    if len(content.encode("utf-8")) > 1_000_000:
        raise HTTPException(status_code=413, detail="content too large (max 1 MB)")
    target = _agent_safe_target(config, rel)
    if not target:
        raise HTTPException(status_code=400, detail="invalid path")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(target))
    except Exception as e:
        _log.error("Vorgaben-Datei speichern fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail="save failed")
    return JSONResponse({"ok": True, "path": rel, "bytes": target.stat().st_size})


@app.post("/api/agent/delete")
async def api_agent_file_delete(request: Request):
    """Verschiebt eine Vorgaben-Datei in den Papierkorb. NUR für prefs/ und directions/ erlaubt."""
    _require_token(request)
    config = load_config()
    body = await request.json()
    rel = (body.get("path") or "").strip()
    target = _agent_safe_target(config, rel)
    if not target:
        raise HTTPException(status_code=400, detail="invalid path")
    base = _agent_dir_resolved(config)
    parts = target.relative_to(base).parts
    sub = parts[0] if len(parts) == 2 else ""
    if sub not in _AGENT_DELETABLE_SUBS:
        raise HTTPException(status_code=403, detail="deletion not allowed for this category")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    trash_dir = Path(config.get("trash_dir") or "~/.trash").expanduser().resolve()
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / target.name
    i = 1
    while dest.exists():
        dest = trash_dir / f"{target.stem}_{i}{target.suffix}"
        i += 1
    try:
        shutil.move(str(target), str(dest))
    except Exception as e:
        _log.error("Vorgaben-Datei löschen fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail="delete failed")
    return JSONResponse({"ok": True, "trashed": str(dest.name)})


_AGENT_PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Vorgaben – MiniAssistant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.ico">
<style>
__COMMON_CSS__
.container { max-width: 1200px !important; }
.ag-layout { display:flex; gap:1rem; align-items:flex-start; height:calc(100vh - 170px); }
.ag-list { width:260px; flex-shrink:0; background:var(--card); border:1px solid var(--border); border-radius:var(--radius); overflow-y:auto; height:100%; }
.ag-editor { flex:1; min-width:0; display:flex; flex-direction:column; height:100%; }
.ag-ghdr { font-size:0.72rem; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); padding:0.7rem 0.85rem 0.2rem; font-weight:700; }
.ag-list a { display:block; padding:0.4rem 0.9rem; color:var(--text); text-decoration:none; font-size:0.86rem; cursor:pointer; border-left:3px solid transparent; word-break:break-all; }
.ag-list a:hover { background:var(--border); color:var(--primary); }
.ag-list a.active { background:var(--primary); color:#fff; border-left-color:var(--primary); }
.ag-frow { display:flex; align-items:stretch; }
.ag-frow > a { flex:1; min-width:0; }
.ag-del { background:none; border:none; cursor:pointer; padding:0 0.7rem; color:var(--muted); font-size:0.82rem; flex-shrink:0; line-height:1; }
.ag-del:hover { color:var(--danger); }
.ag-newbtn { display:block; width:calc(100% - 1.7rem); margin:0.35rem 0.85rem 0.5rem; padding:0.35rem; font-size:0.78rem; background:transparent; border:1px dashed var(--border); border-radius:var(--radius); color:var(--muted); cursor:pointer; }
.ag-newbtn:hover { color:var(--primary); border-color:var(--primary); }
.ag-ta { flex:1; width:100%; font-family:'SF Mono','Fira Code','Cascadia Code',monospace; font-size:13px; padding:0.8rem; border:1.5px solid var(--border); border-radius:var(--radius); background:var(--card); color:var(--text); outline:none; resize:none; line-height:1.5; white-space:pre-wrap; overflow-wrap:break-word; overflow:auto; }
.ag-ta:focus { border-color:var(--primary); }
.ag-bar { display:flex; align-items:center; gap:0.8rem; margin-top:0.6rem; flex-shrink:0; }
.ag-fname { font-weight:600; color:var(--primary); font-size:0.92rem; word-break:break-all; flex:1; }
.ag-status { font-size:0.85rem; color:var(--muted); white-space:nowrap; }
.ag-status.dirty { color:var(--warning); }
.ag-status.ok { color:var(--success); }
.ag-empty { color:var(--muted); padding:1rem; font-size:0.9rem; }
.ag-modal { position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:10000; display:flex; align-items:center; justify-content:center; }
.ag-modal-box { background:var(--card); border:1px solid var(--border); border-radius:var(--radius); padding:1.3rem; max-width:420px; width:90%; box-shadow:var(--shadow); }
.ag-modal-msg { margin-bottom:0.9rem; color:var(--text); font-size:0.95rem; word-break:break-word; }
.ag-modal-box input { width:100%; margin-bottom:0.9rem; }
.ag-modal-btns { display:flex; gap:0.6rem; justify-content:flex-end; }
</style>
</head>
<body>
<div class="container">
<div class="card">
  <div style="display:flex; align-items:center; gap:0.6rem; margin-bottom:1rem;">
    <img src="/static/miniassistant.png" alt="Logo" style="height:2rem;width:auto">
    <h1 style="flex:1; margin:0; font-size:1.4em;">Vorgaben &amp; Dateien</h1>
    <a href="/__TQ__" class="btn btn-outline">Startseite</a>
  </div>
  <p class="text-muted" style="margin-top:-0.4rem">SOUL, IDENTITY, Regeln, Directions, Docs &amp; Prefs bearbeiten. Änderungen werden erst nach <b>Speichern</b> (mit Rückfrage) übernommen. Kein Löschen.</p>
  <div class="ag-layout">
    <div class="ag-list" id="ag-list"><div class="ag-empty">Lade…</div></div>
    <div class="ag-editor">
      <textarea class="ag-ta" id="ag-ta" placeholder="Datei links auswählen…" spellcheck="false" disabled></textarea>
      <div class="ag-bar">
        <span class="ag-fname" id="ag-fname">Keine Datei</span>
        <span class="ag-status" id="ag-status"></span>
        <span class="ag-confirm" id="ag-confirm" style="display:none; align-items:center; gap:0.5rem;">
          <span style="font-size:0.85rem; color:var(--warning);">Wirklich speichern?</span>
          <button class="btn btn-primary" id="ag-confirm-yes">Ja</button>
          <button class="btn btn-outline" id="ag-confirm-no">Abbrechen</button>
        </span>
        <button class="btn btn-primary" id="ag-save" disabled>Speichern</button>
      </div>
    </div>
  </div>
</div>
</div>
<div id="ag-modal" class="ag-modal" style="display:none">
  <div class="ag-modal-box">
    <div id="ag-modal-msg" class="ag-modal-msg"></div>
    <input type="text" id="ag-modal-input" style="display:none" spellcheck="false" autocomplete="off">
    <div class="ag-modal-btns">
      <button id="ag-modal-no" class="btn btn-outline">Abbrechen</button>
      <button id="ag-modal-yes" class="btn btn-primary">OK</button>
    </div>
  </div>
</div>
<script>
var TOKEN = __TOKEN__;
var curPath = null, origContent = "", dirty = false;
function hq(p){ return TOKEN ? (p + (p.indexOf('?')>=0?'&':'?') + 'token=' + encodeURIComponent(TOKEN)) : p; }
function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function agModal(opts){
  return new Promise(function(resolve){
    var m=document.getElementById('ag-modal');
    document.getElementById('ag-modal-msg').textContent = opts.message;
    var inp=document.getElementById('ag-modal-input');
    inp.style.display = opts.input ? 'block' : 'none';
    inp.value = opts.value || '';
    var yes=document.getElementById('ag-modal-yes'), no=document.getElementById('ag-modal-no');
    yes.textContent = opts.okLabel || 'OK';
    m.style.display='flex';
    if(opts.input) setTimeout(function(){ inp.focus(); }, 30);
    function cleanup(val){ m.style.display='none'; yes.onclick=null; no.onclick=null; inp.onkeydown=null; resolve(val); }
    yes.onclick=function(){ cleanup(opts.input ? (inp.value.trim()||null) : true); };
    no.onclick=function(){ cleanup(opts.input ? null : false); };
    if(opts.input) inp.onkeydown=function(e){ if(e.key==='Enter'){ e.preventDefault(); yes.onclick(); } else if(e.key==='Escape'){ no.onclick(); } };
  });
}
function setStatus(t, cls){ var s=document.getElementById('ag-status'); s.textContent=t; s.className='ag-status'+(cls?' '+cls:''); }
function setDirty(d){ dirty=d; document.getElementById('ag-save').disabled = !d || curPath===null; if(d) setStatus('● ungespeichert','dirty'); else setStatus(''); }
async function loadList(){
  var r, d;
  try { r = await fetch(hq('/api/agent/files')); d = await r.json(); } catch(e){ document.getElementById('ag-list').innerHTML='<div class=ag-empty>Fehler beim Laden.</div>'; return; }
  var el = document.getElementById('ag-list'); el.innerHTML='';
  if(!d.agent_dir){ el.innerHTML='<div class=ag-empty>Kein agent_dir konfiguriert.</div>'; return; }
  (d.groups||[]).forEach(function(g){
    if((!g.items || g.items.length===0) && !g.allow_new) return; // leere, nicht-erweiterbare Gruppe ausblenden
    var h=document.createElement('div'); h.className='ag-ghdr'; h.textContent=g.label; el.appendChild(h);
    (g.items||[]).forEach(function(it){
      var a=document.createElement('a'); a.textContent=it.name; a.title=it.path;
      a.onclick=function(){ openFile(it.path, a); };
      if(g.deletable){
        var row=document.createElement('div'); row.className='ag-frow';
        row.appendChild(a);
        var db=document.createElement('button'); db.className='ag-del'; db.textContent='🗑'; db.title='In Papierkorb verschieben';
        db.onclick=function(ev){ ev.stopPropagation(); deleteFile(it.path); };
        row.appendChild(db); el.appendChild(row);
      } else {
        el.appendChild(a);
      }
    });
    if(g.allow_new){
      var nb=document.createElement('button'); nb.className='ag-newbtn'; nb.textContent='+ Neue Datei';
      nb.onclick=function(){ newFile(g.sub); }; el.appendChild(nb);
    }
  });
}
async function openFile(path, aEl){
  if(dirty && !(await agModal({message:'Ungespeicherte Änderungen verwerfen?', okLabel:'Verwerfen'}))) return;
  var r;
  try { r = await fetch(hq('/api/agent/file?path='+encodeURIComponent(path))); } catch(e){ setStatus('Fehler','dirty'); return; }
  if(!r.ok){ setStatus('Fehler beim Laden','dirty'); return; }
  var d = await r.json();
  curPath = path; origContent = d.content;
  var ta=document.getElementById('ag-ta'); ta.value=d.content; ta.disabled=false;
  document.getElementById('ag-fname').textContent=path;
  document.querySelectorAll('.ag-list a').forEach(function(x){ x.classList.remove('active'); });
  if(aEl) aEl.classList.add('active');
  setDirty(false);
}
async function newFile(sub){
  if(dirty && !(await agModal({message:'Ungespeicherte Änderungen verwerfen?', okLabel:'Verwerfen'}))) return;
  var name = await agModal({message:'Dateiname für die neue Datei (z.B. notiz.md):', input:true, okLabel:'Anlegen'});
  if(!name) return; name = name.trim();
  if(!/\\.(md|txt|ya?ml)$/i.test(name)) name += '.md';
  if(!/^[A-Za-z0-9._-]+$/.test(name)){ await agModal({message:'Ungültiger Name. Nur Buchstaben, Zahlen, . _ - erlaubt.'}); return; }
  curPath = sub ? (sub+'/'+name) : name; origContent = "";
  var ta=document.getElementById('ag-ta'); ta.value=""; ta.disabled=false; ta.focus();
  document.getElementById('ag-fname').textContent=curPath+' (neu)';
  document.querySelectorAll('.ag-list a').forEach(function(x){ x.classList.remove('active'); });
  setDirty(true);
}
async function deleteFile(path){
  if(!(await agModal({message:'Datei „'+path+'“ in den Papierkorb verschieben?', okLabel:'Löschen'}))) return;
  var r;
  try { r=await fetch(hq('/api/agent/delete'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:path})}); }
  catch(e){ setStatus('Netzwerkfehler','dirty'); return; }
  if(r.ok){
    if(curPath===path){ curPath=null; origContent=''; var ta=document.getElementById('ag-ta'); ta.value=''; ta.disabled=true; document.getElementById('ag-fname').textContent='Keine Datei'; setDirty(false); }
    setStatus('✓ in Papierkorb verschoben','ok'); await loadList();
  } else { var e={}; try{ e=await r.json(); }catch(_){} setStatus('Fehler: '+(e.detail||r.status),'dirty'); }
}
document.getElementById('ag-ta').addEventListener('input', function(){ setDirty(this.value !== origContent); });
function showConfirm(on){
  document.getElementById('ag-confirm').style.display = on ? 'inline-flex' : 'none';
  document.getElementById('ag-save').style.display = on ? 'none' : 'inline-block';
}
document.getElementById('ag-save').addEventListener('click', function(){
  if(curPath===null) return;
  showConfirm(true);   // inline-Bestätigung statt Browser-Popup
});
document.getElementById('ag-confirm-no').addEventListener('click', function(){ showConfirm(false); });
document.getElementById('ag-confirm-yes').addEventListener('click', async function(){
  showConfirm(false);
  if(curPath===null) return;
  var content = document.getElementById('ag-ta').value;
  setStatus('speichere…');
  var r;
  try { r = await fetch(hq('/api/agent/file'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:curPath, content:content})}); }
  catch(e){ setStatus('Netzwerkfehler','dirty'); return; }
  if(r.ok){ origContent=content; setDirty(false); setStatus('✓ gespeichert','ok'); await loadList(); }
  else { var e={}; try{ e=await r.json(); }catch(_){} setStatus('Fehler: '+(e.detail||r.status),'dirty'); }
});
window.addEventListener('beforeunload', function(e){ if(dirty){ e.preventDefault(); e.returnValue=''; } });
loadList();
</script>
__THEME_JS__
</body>
</html>"""


@app.get("/agent", response_class=HTMLResponse)
async def agent_files_page(request: Request):
    """Vorgaben-Editor: Agent-Dateien (SOUL/IDENTITY/…, Regeln, Directions, Docs, Prefs) bearbeiten."""
    _require_token(request)
    import json as _json
    token = request.query_params.get("token", "") or request.cookies.get("ma_token", "")
    tq = _token_query(token)
    token_val = token.replace("?token=", "").replace("&token=", "")
    html = (_AGENT_PAGE_TEMPLATE
            .replace("__COMMON_CSS__", _COMMON_CSS)
            .replace("__THEME_JS__", _THEME_JS)
            .replace("__TOKEN__", _json.dumps(token_val))
            .replace("__TQ__", tq))
    return HTMLResponse(html)


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request):
    """Chatverlauf – Liste und Viewer für persistente Chat-Sessions."""
    _require_token(request)
    token = request.query_params.get("token", "") or request.cookies.get("ma_token", "")
    tq = _token_query(token)
    token_val = token.replace("?token=", "").replace("&token=", "")
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Gespeicherte Chats – MiniAssistant</title>
<link rel="icon" href="/favicon.ico">
<style>
{_COMMON_CSS}
.container {{ max-width: 1400px !important; }}
.ws-layout {{display:flex;gap:1rem;align-items:flex-start;height:calc(100vh - 180px)}}
.ch-sidebar {{width:260px;flex-shrink:0;height:100%;display:flex;flex-direction:column}}
.ch-list {{flex:1;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow-y:auto;overflow-x:hidden}}
.ch-list ul {{list-style:none;margin:0;padding:0}}
.ch-list li {{border-bottom:1px solid var(--border)}}
.ch-list li:last-child {{border-bottom:none}}
.ch-list a {{display:flex;flex-direction:column;padding:0.5rem 0.8rem;color:var(--text);text-decoration:none;cursor:pointer;gap:0.1rem}}
.ch-list a:hover {{background:var(--border)}}
.ch-list a.active {{background:var(--primary);color:#fff}}
.ch-list a.active .ch-date {{color:rgba(255,255,255,0.75)}}
.ch-title {{font-size:0.85rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.ch-date {{font-size:0.72rem;color:var(--muted)}}
.ch-viewer {{flex:1;min-width:0;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.2rem;overflow:auto;height:100%}}
.ch-viewer .md-body h1,.ch-viewer .md-body h2,.ch-viewer .md-body h3 {{margin-top:1rem}}
.ch-viewer .md-body code {{background:var(--bg);padding:0.1em 0.3em;border-radius:3px;font-size:0.85em}}
.ch-viewer .md-body pre {{background:var(--bg);padding:0.8rem;border-radius:var(--radius);overflow:auto}}
.ch-viewer .md-body pre code {{background:none;padding:0}}
.ch-viewer .md-body table {{border-collapse:collapse;width:100%}}
.ch-viewer .md-body td,.ch-viewer .md-body th {{border:1px solid var(--border);padding:0.35rem 0.6rem}}
.ch-viewer .md-body th {{background:var(--bg)}}
.ch-viewer .md-body hr {{border:none;border-top:1px solid var(--border);margin:0.8rem 0}}
.ch-viewer .msg {{padding:0.5em 0}}
.ch-viewer .msg.msg-user {{text-align:right;padding-left:3em;border-right:3px solid var(--primary);padding-right:0.7em}}
.ch-viewer .msg.msg-assistant {{border-left:3px solid var(--success);padding-left:0.7em}}
.ch-viewer .msg-role {{font-weight:700;font-size:0.78em;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.25em}}
.ch-viewer .msg-role.user {{color:var(--primary);text-align:right}}
.ch-viewer .msg-role.assistant {{color:var(--success)}}
.ch-viewer .msg .content {{line-height:1.6}}
.ch-viewer .msg .content.markdown p {{margin:0.3em 0}}
.ch-viewer .msg .content.markdown pre {{background:var(--bg);padding:0.6em;border-radius:6px;overflow-x:auto;font-size:0.9em}}
.ch-viewer .msg .content.markdown code {{background:var(--bg);padding:0.15em 0.35em;border-radius:4px;font-size:0.9em}}
.ch-viewer .msg-sep {{border:none;border-top:1px solid var(--border);margin:0.5em 0}}
.ws-empty {{color:var(--muted);font-size:0.9rem;padding:1rem}}
.ch-actions {{display:flex;gap:0.5rem;margin-bottom:0.8rem}}
.ch-resume-btn {{background:var(--primary);color:#fff;border:none;border-radius:var(--radius);padding:0.4rem 0.9rem;cursor:pointer;font-size:0.85rem}}
.ch-resume-btn:hover {{opacity:0.85}}
</style>
<script src="/static/marked.umd.js"></script>
    <script src="/static/purify.min.js"></script>
</head>
<body>
<div class="container">
<div class="card">
  <div class="usage-header" style="margin-bottom:1.2rem">
    <img src="/static/miniassistant.png" alt="Logo" style="height:2rem;width:auto">
    <h1>Gespeicherte Chats</h1>
    <div class="usage-nav">
      <a href="/chat{tq}{'&' if tq else '?'}track=1" class="btn btn-outline">Neuer Chat</a>
      <a href="/{tq}" class="btn btn-outline">Startseite</a>
    </div>
  </div>
  <div class="ws-layout">
    <div class="ch-sidebar">
      <div class="ch-list" id="ch-list"><div class="ws-empty">Lade...</div></div>
    </div>
    <div class="ch-viewer" id="ch-viewer"><div class="ws-empty">Konversation auswählen</div></div>
  </div>
</div>
</div>
<script>
var CH_TOKEN = {repr(token_val)};

function chApiUrl(endpoint, params) {{
  var u = new URL('/api/chats/' + endpoint, location.origin);
  if (CH_TOKEN) u.searchParams.set('token', CH_TOKEN);
  if (params) Object.keys(params).forEach(function(k) {{ u.searchParams.set(k, params[k]); }});
  return u.toString();
}}

function chEscape(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function chLoadList() {{
  var list = document.getElementById('ch-list');
  fetch(chApiUrl('files'))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (!data.items || !data.items.length) {{
        list.innerHTML = '<div class="ws-empty">Noch keine Konversationen</div>';
        return;
      }}
      var html = '<ul>';
      data.items.forEach(function(item) {{
        html += '<li><a data-stem="' + chEscape(item.stem) + '" title="' + chEscape(item.title) + '">'
              + '<span class="ch-title">' + chEscape(item.title) + '</span>'
              + '<span class="ch-date">' + chEscape(item.date) + '</span>'
              + '</a></li>';
      }});
      html += '</ul>';
      list.innerHTML = html;
      list.querySelectorAll('a[data-stem]').forEach(function(a) {{
        a.addEventListener('click', function(e) {{ e.preventDefault(); chLoadChat(a.getAttribute('data-stem')); }});
      }});
    }})
    .catch(function(err) {{ list.innerHTML = '<div class="ws-empty">Fehler: ' + chEscape(String(err)) + '</div>'; }});
}}

function chLoadChat(stem) {{
  document.querySelectorAll('#ch-list a').forEach(function(a) {{ a.classList.remove('active'); }});
  document.querySelectorAll('#ch-list a[data-stem="' + stem.replace(/"/g,'\\"') + '"]').forEach(function(a) {{ a.classList.add('active'); }});
  var viewer = document.getElementById('ch-viewer');
  viewer.innerHTML = '<div class="ws-empty">Lade...</div>';
  fetch(chApiUrl('file', {{stem: stem}}))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ viewer.innerHTML = '<div class="ws-empty">' + chEscape(data.error) + '</div>'; return; }}
      var exchanges = data.exchanges || [];
      var body = '';
      exchanges.forEach(function(ex, i) {{
        if (i > 0) body += '<hr class="msg-sep">';
        body += '<div class="msg msg-user"><div class="msg-role user">Du</div>'
              + '<div class="content">' + chEscape(ex.user) + '</div></div>'
              + '<div class="msg msg-assistant"><div class="msg-role assistant">Assistent</div>'
              + '<div class="content markdown">' + DOMPurify.sanitize(marked.parse(ex.assistant || '')) + '</div></div>';
      }});
      viewer.innerHTML = '<div class="ch-actions"><button class="ch-resume-btn" id="ch-resume-btn">&#9654; Fortsetzen</button></div>'
                       + '<p style="font-size:0.8rem;color:var(--muted);margin:0 0 1rem">'
                       + chEscape((data.title || '') + (data.model ? ' · ' + data.model : '')) + '</p>'
                       + (body || '<div class="ws-empty">Keine Nachrichten</div>');
      document.getElementById('ch-resume-btn').addEventListener('click', function() {{ chResume(stem); }});
      // Titel im Listeneintrag aktualisieren falls Hintergrundthread ihn inzwischen gesetzt hat
      if (data.title) {{
        var listLink = document.querySelector('#ch-list a[data-stem="' + stem.replace(/"/g,'\\"') + '"]');
        if (listLink) {{
          var titleEl = listLink.querySelector('.ch-title');
          if (titleEl && titleEl.textContent !== data.title) {{
            titleEl.textContent = data.title;
            listLink.title = data.title;
          }}
        }}
      }}
    }})
    .catch(function(err) {{ viewer.innerHTML = '<div class="ws-empty">Fehler: ' + chEscape(String(err)) + '</div>'; }});
}}

function chResume(stem) {{
  fetch(chApiUrl('resume'), {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{stem:stem}})}})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.error) {{ alert(data.error); return; }}
      window.location.href = '/chat{tq}{"&" if tq else "?"}resume_session=' + encodeURIComponent(data.session_id) + '&resume_stem=' + encodeURIComponent(stem) + '&track=1';
    }})
    .catch(function(err) {{ alert('Fehler: ' + err); }});
}}

chLoadList();
// Nochmal nach 4s laden — Hintergrundthread für Titelgenerierung hat dann Zeit fertig zu werden
setTimeout(chLoadList, 4000);
</script>
{_THEME_JS}
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/api/chats/files")
async def api_chats_files(request: Request):
    """Listet alle gespeicherten Chat-Sessions (neueste zuerst)."""
    _require_token(request)
    config = load_config()
    chats_dir = _get_chats_dir(config)
    if not chats_dir.exists():
        return JSONResponse({"items": []})
    import json as _json
    items = []
    for p in sorted(chats_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            title = data.get("title") or p.stem
        except Exception:
            title = p.stem
        try:
            dt = datetime.datetime.strptime(p.stem[:15], "%Y-%m-%d_%H%M%S")
            date_str = dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            date_str = p.stem
        items.append({"stem": p.stem, "title": title, "date": date_str})
    return JSONResponse({"items": items})


@app.get("/api/chats/file")
async def api_chats_file(request: Request):
    """Liefert den Inhalt einer Chat-JSON-Datei."""
    import json as _json
    _require_token(request)
    config = load_config()
    chats_dir = _get_chats_dir(config)
    raw_stem = request.query_params.get("stem", "").strip()
    # Strip all path separators and directory traversal components, then use Path.name as final safeguard
    stem = Path(raw_stem.replace("/", "").replace("\\", "").replace("..", "")).name
    if not stem:
        return JSONResponse({"error": "Kein stem angegeben"})
    target = (chats_dir / (stem + ".json")).resolve()
    if not _is_path_within(target, chats_dir):
        raise HTTPException(status_code=403)
    if not target.exists():
        return JSONResponse({"error": "Datei nicht gefunden"})
    try:
        return JSONResponse(_json.loads(target.read_text(encoding="utf-8")))
    except Exception as e:
        return JSONResponse({"error": "Fehler beim Lesen der Datei"})


@app.post("/api/chats/resume")
async def api_chats_resume(request: Request):
    """Lädt eine gespeicherte Chat-Session und erstellt daraus eine neue aktive Session."""
    import json as _json
    _require_token(request)
    body = await request.json()
    raw_stem = (body.get("stem") or "").strip()
    stem = Path(raw_stem.replace("/", "").replace("\\", "").replace("..", "")).name
    if not stem:
        return JSONResponse({"error": "Kein stem angegeben"})
    config = load_config()
    chats_dir = _get_chats_dir(config)
    sidecar = (chats_dir / (stem + ".json")).resolve()
    if not _is_path_within(sidecar, chats_dir):
        raise HTTPException(status_code=403)
    if not sidecar.exists():
        return JSONResponse({"error": "Keine gespeicherte Session für diesen Chat"})
    try:
        saved = _json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": "Fehler beim Laden der Session"})
    project_dir = getattr(request.app.state, "project_dir", None)
    session = create_session(None, project_dir)
    session["messages"] = saved.get("messages", [])
    if saved.get("model"):
        session["model"] = saved["model"]
    session["_chat_file"] = str(chats_dir / (stem + ".json"))
    session["_chat_stem"] = stem
    session["_track_chat"] = True
    session_id = str(uuid.uuid4())
    _sessions[session_id] = session
    return JSONResponse({"session_id": session_id})


@app.post("/api/onboarding/save")
async def api_onboarding_save(request: Request):
    """Speichert die vier Agent-Dateien (SOUL.md, IDENTITY.md, TOOLS.md, USER.md) aus dem Onboarding."""
    _require_token(request)
    body = await request.json()
    config = load_config()
    result = _save_onboarding_files(body, config)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def _wh_disabled_response():
    """Identical 404 to a missing route so unknown tokens don't leak existence."""
    raise HTTPException(status_code=404, detail="Not Found")


def _wh_extract_token(request: Request, path_token: str) -> str:
    """Use path token, but accept X-Webhook-Token header as override."""
    hdr = request.headers.get("X-Webhook-Token") or ""
    return (hdr or path_token or "").strip()


# Body keys we treat as control fields. Anything else in a JSON body counts as payload.
_WH_CONTROL_KEYS = {
    "prompt", "extra_context", "client", "room_id", "channel_id",
    "silent", "save_output", "output_name", "model",
}

# Headers we forward to the prompt — useful metadata from senders (GitHub, Slack, etc.).
_WH_FORWARD_HEADER_PREFIXES = ("x-", "user-agent")
_WH_HEADER_SKIP = {"x-webhook-token", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip"}
_WH_MAX_PAYLOAD_CHARS = 20_000


async def _build_incoming_context(request: Request) -> tuple[dict, str]:
    """Parse webhook body for control fields + payload.
    Returns (control_dict, extra_context_str).
    Body kinds handled:
      - application/json with our control keys + arbitrary other fields → controls separated, rest as payload
      - application/json with foreign schema (GitHub, Discord, ...) → whole body becomes payload
      - application/x-www-form-urlencoded → fields as JSON payload
      - text/plain or anything else → raw body bytes decoded as payload
    Forwarded HTTP headers (X-*, User-Agent) are prepended so prompts can read event type, source, etc.
    """
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    controls: dict = {}
    payload_block: str = ""

    if ctype == "application/json":
        try:
            body = await request.json()
        except Exception:
            # Declared application/json but body won't parse. Don't swallow it —
            # a non-empty broken body means the caller's fields (extra_context, prompt)
            # would vanish silently. Empty body is fine (fire default prompt).
            raw = b""
            try:
                raw = await request.body()
            except Exception:
                pass
            if raw and raw.strip():
                raise HTTPException(status_code=400, detail="invalid JSON body (Content-Type: application/json). Check shell quoting — nested single quotes break the payload.")
            body = None
        if isinstance(body, dict):
            controls = {k: body[k] for k in _WH_CONTROL_KEYS if k in body}
            rest = {k: v for k, v in body.items() if k not in _WH_CONTROL_KEYS}
            if rest:
                try:
                    payload_block = json.dumps(rest, indent=2, ensure_ascii=False)
                except Exception:
                    payload_block = repr(rest)
        elif body is not None:
            try:
                payload_block = json.dumps(body, indent=2, ensure_ascii=False)
            except Exception:
                payload_block = repr(body)
    elif ctype == "application/x-www-form-urlencoded":
        try:
            form = await request.form()
            form_dict = {k: form[k] for k in form.keys()}
            controls = {k: form_dict[k] for k in _WH_CONTROL_KEYS if k in form_dict}
            rest = {k: v for k, v in form_dict.items() if k not in _WH_CONTROL_KEYS}
            if rest:
                payload_block = json.dumps(rest, indent=2, ensure_ascii=False)
        except Exception:
            pass
    else:
        try:
            raw = await request.body()
            if raw:
                payload_block = raw.decode("utf-8", errors="replace")
        except Exception:
            pass

    if len(payload_block) > _WH_MAX_PAYLOAD_CHARS:
        payload_block = payload_block[:_WH_MAX_PAYLOAD_CHARS] + f"\n…[truncated, original {len(payload_block)} chars]"

    # Forward relevant headers (event type, signatures, user-agent — sender's clues)
    fwd_headers: list[tuple[str, str]] = []
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _WH_HEADER_SKIP:
            continue
        if any(lk.startswith(p) for p in _WH_FORWARD_HEADER_PREFIXES):
            fwd_headers.append((k, v))

    explicit_extra = str(controls.get("extra_context") or "").strip()
    if explicit_extra:
        # Caller set extra_context themselves — respect it, don't overwrite.
        return controls, explicit_extra

    if not payload_block and not fwd_headers:
        return controls, ""

    parts = ["[INCOMING WEBHOOK PAYLOAD]"]
    if fwd_headers:
        parts.append("Headers:")
        for k, v in fwd_headers:
            parts.append(f"  {k}: {v}")
    if payload_block:
        parts.append("")
        parts.append(f"Body ({ctype or 'unknown'}):")
        parts.append(payload_block)
    controls["extra_context"] = "\n".join(parts)
    return controls, controls["extra_context"]


@app.post("/webhook/{token}")
async def webhook_fire(request: Request, token: str):
    """Fire a webhook by token.
    Body handling:
      - JSON with our control keys (prompt, extra_context, client, room_id, channel_id, silent,
        save_output, output_name, model) → keys are honored, any remaining JSON fields become payload.
      - JSON from foreign senders (GitHub, Discord, ...) → whole body becomes payload.
      - form-encoded / text / raw bytes → body becomes payload.
      X-* headers and User-Agent are forwarded so prompts can read event type, signature, source.
    """
    from miniassistant import webhooks as _wh
    if not _wh.is_enabled():
        _wh_disabled_response()
    real_token = _wh_extract_token(request, token)
    item = _wh.find_by_token(real_token)
    if not item:
        _wh_disabled_response()
    if not _wh.rate_check(item["id"]):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    body, extra_context = await _build_incoming_context(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_chat_executor, lambda: _wh.fire(
        item,
        extra_context=extra_context,
        prompt_override=str(body.get("prompt") or ""),
        client_override=body.get("client"),
        room_id_override=body.get("room_id"),
        channel_id_override=body.get("channel_id"),
        silent_override=body.get("silent") if "silent" in body else None,
        save_output_override=body.get("save_output") if "save_output" in body else None,
        output_name=body.get("output_name"),
        model_override=body.get("model"),
    ))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400 if "no prompt" in (result.get("error") or "") else 500)
    return JSONResponse(result)


@app.get("/webhook/{token}/runs")
async def webhook_runs(request: Request, token: str):
    from miniassistant import webhooks as _wh
    if not _wh.is_enabled():
        _wh_disabled_response()
    real_token = _wh_extract_token(request, token)
    item = _wh.find_by_token(real_token)
    if not item:
        _wh_disabled_response()
    return JSONResponse({"runs": _wh.list_outputs(item)})


@app.get("/webhook/{token}/last")
async def webhook_last(request: Request, token: str):
    from miniassistant import webhooks as _wh
    import mimetypes
    if not _wh.is_enabled():
        _wh_disabled_response()
    real_token = _wh_extract_token(request, token)
    item = _wh.find_by_token(real_token)
    if not item:
        _wh_disabled_response()
    res = _wh.read_output(item)
    if not res:
        raise HTTPException(status_code=404, detail="no outputs")
    path, content = res
    mt = mimetypes.guess_type(path.name)[0] or "text/plain; charset=utf-8"
    from fastapi.responses import Response as _Resp
    return _Resp(content=content, media_type=mt)


@app.get("/webhook/{token}/output/{name}")
async def webhook_output(request: Request, token: str, name: str):
    from miniassistant import webhooks as _wh
    import mimetypes
    if not _wh.is_enabled():
        _wh_disabled_response()
    real_token = _wh_extract_token(request, token)
    item = _wh.find_by_token(real_token)
    if not item:
        _wh_disabled_response()
    res = _wh.read_output(item, name=name)
    if not res:
        raise HTTPException(status_code=404, detail="not found")
    path, content = res
    mt = mimetypes.guess_type(path.name)[0] or "text/plain; charset=utf-8"
    from fastapi.responses import Response as _Resp
    return _Resp(content=content, media_type=mt)


# Cookie-auth UI/API


@app.get("/api/webhook")
async def api_webhook_list(request: Request):
    _require_token(request)
    from miniassistant import webhooks as _wh
    if not _wh.is_enabled():
        return JSONResponse({"enabled": False, "items": []})
    return JSONResponse({"enabled": True, "items": _wh.list_webhooks()})


@app.post("/api/webhook")
async def api_webhook_create(request: Request):
    _require_token(request)
    from miniassistant import webhooks as _wh
    if not _wh.is_enabled():
        raise HTTPException(status_code=400, detail="webhooks disabled in config")
    body = await request.json()
    ok, res = _wh.add_webhook(
        name=str(body.get("name") or ""),
        prompt=str(body.get("prompt") or ""),
        client=body.get("client"),
        room_id=body.get("room_id"),
        channel_id=body.get("channel_id"),
        model=body.get("model"),
        silent=bool(body.get("silent", False)),
        save_output=bool(body.get("save_output", True)),
    )
    if not ok:
        raise HTTPException(status_code=400, detail=res)
    return JSONResponse({"ok": True, "item": res})


@app.patch("/api/webhook/{wid}")
async def api_webhook_update(request: Request, wid: str):
    _require_token(request)
    from miniassistant import webhooks as _wh
    body = await request.json()
    ok, msg = _wh.update_webhook(wid, body)
    if not ok:
        raise HTTPException(status_code=404 if msg == "not found" else 400, detail=msg)
    return JSONResponse({"ok": True})


@app.delete("/api/webhook/{wid}")
async def api_webhook_delete(request: Request, wid: str):
    _require_token(request)
    from miniassistant import webhooks as _wh
    purge = request.query_params.get("purge") in ("1", "true", "yes")
    ok, msg = _wh.remove_webhook(wid, purge_outputs=purge)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return JSONResponse({"ok": True, "removed": msg})


@app.get("/api/webhook/{wid}/token")
async def api_webhook_token(request: Request, wid: str):
    """Reveal plaintext token for a single webhook on demand (server.token authed).
    The /webhooks UI keeps tokens masked by default and fetches via this endpoint
    so the token is not part of the rendered HTML."""
    _require_token(request)
    from miniassistant import webhooks as _wh
    if not _wh.is_enabled():
        raise HTTPException(status_code=404, detail="webhooks disabled")
    item = _wh.find_by_id(wid)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse({"id": item.get("id", ""), "token": item.get("token", "")})


@app.post("/api/webhook/{wid}/run")
async def api_webhook_run(request: Request, wid: str):
    """Manual fire from UI. Honors body fields like POST /webhook/{token}."""
    _require_token(request)
    from miniassistant import webhooks as _wh
    item = _wh.find_by_id(wid)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_chat_executor, lambda: _wh.fire(
        item,
        extra_context=str(body.get("extra_context") or ""),
        prompt_override=str(body.get("prompt") or ""),
        silent_override=body.get("silent") if "silent" in body else None,
    ))
    return JSONResponse({"ok": True, "message": f"webhook {wid[:8]} started"})


@app.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request):
    """Webhook UI — mirrors /schedules layout."""
    _require_token(request)
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    _host_base = f"{request.url.scheme}://{request.url.netloc}"
    from miniassistant import webhooks as _wh
    enabled = _wh.is_enabled()
    items = _wh.list_webhooks() if enabled else []
    rows = ""
    if not enabled:
        rows = '<tr><td colspan="6" style="text-align:center;color:var(--muted);font-style:italic;">Webhooks deaktiviert. Aktivieren in <a href="/config' + tq + '">Konfiguration</a>: webhooks.enabled: true</td></tr>'
    elif not items:
        rows = '<tr><td colspan="6" style="text-align:center;color:var(--muted);font-style:italic;">Keine Webhooks.</td></tr>'
    else:
        import html as _html
        for w in items:
            wid = (w.get("id") or "")[:8]
            full_id = _escape(w.get("id", ""))
            handle = _escape(w.get("name") or wid)
            _raw_tok = w.get("token") or ""
            # Mask: first 4 + last 4 chars; plaintext fetched on demand via /api/webhook/{id}/token
            if len(_raw_tok) >= 12:
                tok_masked = _escape(_raw_tok[:4] + "…" + _raw_tok[-4:])
            elif _raw_tok:
                tok_masked = _escape(_raw_tok[:2] + "…")
            else:
                tok_masked = ""
            cli = _escape(w.get("client") or "default")
            if w.get("room_id"):
                _nm = _resolve_target_name(load_config(), room_id=w["room_id"]) or "?"
                cli += f' <span style="font-size:0.85em;color:var(--muted);" title="{_escape(w["room_id"])}">📍 {_escape(_nm)}</span>'
            elif w.get("channel_id"):
                _nm = _resolve_target_name(load_config(), channel_id=w["channel_id"]) or "?"
                cli += f' <span style="font-size:0.85em;color:var(--muted);" title="{_escape(w["channel_id"])}">📍 {_escape(_nm)}</span>'
            silent = ' <span style="color:var(--muted);font-size:0.8em;">silent</span>' if w.get("silent") else ""
            err = ' <span style="color:var(--danger);" title="last error">●</span>' if w.get("last_error") else ""
            last = (w.get("last_fired") or "never")[:16].replace("T", " ")
            prompt_full = _escape(w.get("prompt") or "")
            prompt_show = _escape((w.get("prompt") or "")[:80])
            if len(w.get("prompt") or "") > 80:
                task = f'<details><summary>{prompt_show}…</summary><div class="prompt-full">{prompt_full}</div></details>'
            else:
                task = prompt_show or "(empty)"
            edit_btn = (
                f'<button class="btn-edit" data-id="{full_id}"'
                f' data-name="{_html.escape(w.get("name") or "", quote=True)}"'
                f' data-prompt="{_html.escape(w.get("prompt") or "", quote=True)}"'
                f' data-model="{_html.escape(w.get("model") or "", quote=True)}"'
                f' data-client="{_html.escape(w.get("client") or "", quote=True)}"'
                f' data-room="{_html.escape(w.get("room_id") or "", quote=True)}"'
                f' data-channel="{_html.escape(w.get("channel_id") or "", quote=True)}"'
                f' data-silent="{"1" if w.get("silent") else "0"}"'
                f' title="Bearbeiten">&#9998;</button>'
            )
            _has_default = "1" if (w.get("prompt") or "").strip() else "0"
            rows += (
                f'<tr><td><strong>{handle}</strong>{silent}{err}</td>'
                f'<td>{task}</td><td>{cli}</td>'
                f'<td><code class="tok-masked">{tok_masked}</code> '
                f'<button class="btn-reveal" data-id="{full_id}" title="Token anzeigen / kopieren">&#128065;</button></td>'
                f'<td>{last}</td>'
                f'<td><code>{wid}</code> <button class="btn-run" data-id="{full_id}" data-has-default="{_has_default}" data-name="{_html.escape(w.get("name") or "", quote=True)}" title="Jetzt ausführen">&#9654;</button>{edit_btn}<button class="btn-del" data-id="{full_id}" title="Loeschen">&#10005;</button></td></tr>'
            )
    test_options = ""
    if enabled and items:
        for w in items:
            _tid = _escape(w.get("id", ""))
            _tnm = _escape(w.get("name") or (w.get("id") or "")[:8])
            test_options += f'<option value="{_tid}">{_tnm}</option>'
    test_panel = "" if not (enabled and items) else (
        '<details class="card" style="margin-bottom:1em;" open>'
        '<summary style="font-size:1.05em;font-weight:600;cursor:pointer;">🧪 Live-Test (direkt aus dem Browser feuern)</summary>'
        '<div style="padding:0.6em 0 0;">'
        '<p style="margin:0 0 0.6em;color:var(--muted);font-size:0.88em;">Feuert echt gegen <code>' + _escape(_host_base) + '/webhook/&lt;token&gt;</code> — derselbe Pfad wie ein externer curl. '
        'Prüft den JSON-Body <strong>bevor</strong> du sendest (ungültiges JSON wird serverseitig still verworfen → <code>extra_context</code> geht verloren). '
        'Achtung: hat der Webhook ein Ziel (Raum/Channel), landet die Antwort dort.</p>'
        '<div class="wh-form">'
        '<label for="testHook">Webhook</label>'
        '<select id="testHook">' + test_options + '</select>'
        '<label for="testToken">Token</label>'
        '<input type="text" id="testToken" placeholder="wird beim Auswählen geladen — oder manuell einfügen">'
        '<label for="testBody">JSON-Body</label>'
        '<textarea id="testBody" rows="4">{"prompt":"Antworte nur mit: WUFF"}</textarea>'
        '</div>'
        '<div style="margin:0.5em 0 0.3em;"><span id="testJsonState" style="font-size:0.82em;"></span></div>'
        '<label style="display:block;font-size:0.8em;color:var(--muted);margin:0.3em 0 0.2em;">Äquivalenter curl:</label>'
        '<pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.8em;margin:0;"><code id="testCurl"></code></pre>'
        '<div style="display:flex;gap:0.5em;align-items:center;margin-top:0.6em;flex-wrap:wrap;">'
        '<button class="btn btn-primary" id="testSend">▶ Senden</button>'
        '<button class="btn btn-outline" id="testCurlCopy">📋 curl kopieren</button>'
        '<span id="testStatus" style="font-size:0.85em;color:var(--muted);"></span>'
        '</div>'
        '<pre id="testResult" style="display:none;background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.82em;margin-top:0.6em;white-space:pre-wrap;"></pre>'
        '</div></details>'
    )
    test_js = "" if not (enabled and items) else ("""
    <script>
    (function(){
      var hostBase = "__HOSTBASE__";
      var hookTokens = {};
      var sel = document.getElementById("testHook");
      if (!sel) return;
      var tokIn = document.getElementById("testToken");
      var bodyIn = document.getElementById("testBody");
      var curlEl = document.getElementById("testCurl");
      var jsonState = document.getElementById("testJsonState");
      function curtok(){ return tokIn.value.trim() || "<TOKEN>"; }
      function updateCurl(){
        var b = bodyIn.value;
        if (!b.trim()){ jsonState.textContent = ""; }
        else { try { JSON.parse(b); jsonState.textContent = "\\u2713 g\\u00fcltiges JSON"; jsonState.style.color = "#2d8a4e"; }
               catch(e){ jsonState.textContent = "\\u26a0 ung\\u00fcltiges JSON \\u2014 w\\u00fcrde serverseitig still verworfen (extra_context ginge verloren)"; jsonState.style.color = "#c53030"; } }
        var bodyEsc = b.replace(/'/g, "'\\\\''");
        curlEl.textContent = "curl -X POST " + hostBase + "/webhook/" + curtok() +
          " \\\\\\n  -H \\"Content-Type: application/json\\" \\\\\\n  -d '" + bodyEsc + "'";
      }
      function loadToken(id){
        if (!id) return;
        if (hookTokens[id]){ tokIn.value = hookTokens[id]; updateCurl(); return; }
        fetch("/api/webhook/" + encodeURIComponent(id) + "/token" + tq(), {credentials:"same-origin"})
          .then(function(r){ return r.json(); })
          .then(function(d){ if (d && d.token){ hookTokens[id] = d.token; tokIn.value = d.token; updateCurl(); } })
          .catch(function(){});
      }
      sel.addEventListener("change", function(){ loadToken(sel.value); });
      bodyIn.addEventListener("input", updateCurl);
      tokIn.addEventListener("input", updateCurl);
      document.getElementById("testCurlCopy").addEventListener("click", function(){
        if (navigator.clipboard && navigator.clipboard.writeText)
          navigator.clipboard.writeText(curlEl.textContent).then(function(){ appToast("curl kopiert","success"); }, function(){ appToast("Kopieren fehlgeschlagen","error"); });
      });
      document.getElementById("testSend").addEventListener("click", function(){
        var tok = tokIn.value.trim();
        if (!tok){ appToast("Token fehlt","error"); return; }
        var b = bodyIn.value;
        var status = document.getElementById("testStatus");
        var res = document.getElementById("testResult");
        var btn = this; btn.disabled = true; status.textContent = "l\\u00e4uft\\u2026"; status.style.color = "var(--muted)";
        fetch(hostBase + "/webhook/" + encodeURIComponent(tok), {
          method:"POST", headers:{"Content-Type":"application/json"}, body: b
        }).then(function(r){
          return r.text().then(function(t){
            var pretty = t; try { pretty = JSON.stringify(JSON.parse(t), null, 2); } catch(e){}
            res.style.display = "block";
            res.textContent = "HTTP " + r.status + "\\n\\n" + pretty;
            status.textContent = r.ok ? "\\u2713 fertig" : "\\u2717 HTTP " + r.status;
            status.style.color = r.ok ? "#2d8a4e" : "#c53030";
            btn.disabled = false;
          });
        }).catch(function(e){ status.textContent = "Fehler: " + e; status.style.color = "#c53030"; btn.disabled = false; });
      });
      if (sel.value) loadToken(sel.value);
      updateCurl();
    })();
    </script>
    """.replace("__HOSTBASE__", _host_base))
    enabled_form = "" if not enabled else (
        '<details class="card wh-create" style="margin-bottom:1em;">'
        '<summary style="font-size:1.05em;font-weight:600;cursor:pointer;">+ Neuer Webhook</summary>'
        '<div class="wh-form">'
        '<label for="newName">Name <span class="hint">(slug, optional)</span></label>'
        '<input type="text" id="newName" placeholder="daily-report">'
        '<label for="newPrompt">Default Prompt <span class="hint">(optional — leer = Caller muss prompt im POST mitschicken)</span></label>'
        '<textarea id="newPrompt" rows="4" placeholder="z.B. Summarize the incoming data as a short bullet list. (Antwort wird automatisch zum Chat-Push — keine send_*-Tools nötig.)"></textarea>'
        '<label for="newTarget">Ziel (Raum / Channel) <span class="hint">(optional — leer = an alle authed User)</span></label>'
        f'<select id="newTarget">{_build_target_options(load_config())}</select>'
        '<label for="newModel">Modell <span class="hint">(optional)</span></label>'
        '<input type="text" id="newModel" placeholder="alias oder name">'
        '<label for="newSilent">Silent</label>'
        '<div><label class="cb"><input type="checkbox" id="newSilent"> kein Chat-Push, Output nur in Datei</label></div>'
        '<label for="newSaveOutput">Output</label>'
        '<div><label class="cb"><input type="checkbox" id="newSaveOutput" checked> Output zusätzlich speichern</label></div>'
        '<div></div>'
        '<div style="text-align:right;"><button class="btn btn-primary" id="btnCreate">Anlegen</button></div>'
        '</div>'
        '</details>'
    )
    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>Webhooks – MiniAssistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="/favicon.ico">
    <style>
    {_COMMON_CSS}
    .sched-wrap {{ max-width: 1000px; margin: 0 auto; padding: 1.2em 1em; }}
    .sched-header {{ display: flex; align-items: center; gap: 0.6em; margin-bottom: 1em; }}
    .sched-header img {{ width: 40px; height: 40px; border-radius: 8px; }}
    .sched-header h1 {{ margin: 0; font-size: 1.4em; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
    th {{ text-align: left; padding: 0.5em; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }}
    td {{ padding: 0.5em; border-bottom: 1px solid var(--border); vertical-align: top; }}
    code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; word-break: break-all; }}
    [data-theme="dark"] code {{ background: #2a2a4a; }}
    .btn-del {{ background: none; border: 1.5px solid var(--danger); color: var(--danger); border-radius: 4px;
      cursor: pointer; padding: 0.15em 0.4em; font-size: 0.85em; line-height: 1; transition: background 0.15s; }}
    .btn-del:hover {{ background: var(--danger); color: #fff; }}
    .btn-edit {{ background: none; border: 1.5px solid #888; color: #555; border-radius: 4px;
      cursor: pointer; padding: 0.15em 0.4em; font-size: 0.85em; line-height: 1; margin-right: 0.3em; transition: background 0.15s; }}
    .btn-edit:hover {{ background: #eee; }}
    .btn-run {{ background: none; border: 1.5px solid #2d8a4e; color: #2d8a4e; border-radius: 4px;
      cursor: pointer; padding: 0.15em 0.4em; font-size: 0.85em; line-height: 1; margin-right: 0.3em; transition: background 0.15s; }}
    .btn-run:hover {{ background: #2d8a4e; color: #fff; }}
    .btn-run:disabled {{ opacity: 0.5; cursor: default; }}
    .modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000; align-items:center; justify-content:center; }}
    .modal-overlay.open {{ display:flex; }}
    .modal-box {{ background:var(--card); border-radius:8px; padding:1.5em; max-width:600px; width:90%; box-shadow:0 4px 24px rgba(0,0,0,0.18); }}
    .modal-box h2 {{ margin:0 0 0.8em; font-size:1.1em; }}
    .modal-box textarea, .modal-box input[type=text], .modal-box select {{ width:100%; box-sizing:border-box; }}
    .modal-box label {{ display:block; font-size:0.88em; color:var(--muted); margin-bottom:0.25em; margin-top:0.7em; }}
    .modal-actions {{ display:flex; gap:0.6em; justify-content:flex-end; margin-top:0.8em; }}
    details {{ cursor: pointer; }}
    details summary {{ display: list-item; }}
    details .prompt-full {{ margin-top: 0.4em; white-space: pre-wrap; word-break: break-word; padding: 0.4em; background: var(--bg); border-radius: 4px; font-size: 0.9em; }}
    /* Form-Grid: Label links, Feld rechts, jede Zeile separat */
    .wh-form {{ display: grid; grid-template-columns: 180px 1fr; gap: 0.7em 1em; align-items: center; margin-top: 1em; }}
    .wh-form > label {{ font-size: 0.92em; color: var(--text); text-align: right; padding-right: 0.3em; margin: 0; font-weight: 500; }}
    .wh-form > label .hint {{ display: block; color: var(--muted); font-size: 0.78em; font-weight: 400; margin-top: 0.1em; }}
    .wh-form > input[type=text], .wh-form > select, .wh-form > textarea {{ width: 100%; box-sizing: border-box; }}
    .wh-form > textarea {{ resize: vertical; font-family: inherit; }}
    .wh-form > div {{ display: flex; align-items: center; }}
    .wh-form .cb {{ display: inline-flex; align-items: center; gap: 0.4em; font-size: 0.92em; color: var(--text); cursor: pointer; }}
    .wh-form .cb input {{ width: auto; }}
    .wh-create > summary {{ list-style: none; }}
    .wh-create > summary::-webkit-details-marker {{ display: none; }}
    .wh-create[open] > summary {{ border-bottom: 1px solid var(--border); padding-bottom: 0.5em; }}
    @media (max-width: 600px) {{
      .wh-form {{ grid-template-columns: 1fr; gap: 0.4em; }}
      .wh-form > label {{ text-align: left; padding-right: 0; }}
    }}
    /* Edit-Modal kriegt das gleiche Grid */
    .modal-box .wh-form {{ margin-top: 0; }}
    </style>
    </head><body>
    <div class="sched-wrap">
      <div class="sched-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Webhooks</h1>
      </div>
      {enabled_form}
      {test_panel}
      <details class="card" style="margin-bottom:1em;">
        <summary style="font-size:1.0em;font-weight:600;cursor:pointer;">📖 Beispiele</summary>
        <div style="padding:0.6em 0 0;">
          <p style="margin:0.2em 0 0.6em;color:var(--muted);font-size:0.9em;">
            Adresse unten ist deine aktuelle: <code>{_host_base}</code> (anpassen je nach Reverse-Proxy/Domain).<br>
            <code>&lt;TOKEN&gt;</code> = der Token des Webhooks (Reveal-Button 👁 in der Tabelle).
            Body-Felder sind <strong>optional</strong> ausser markiert.
          </p>

          <h3 style="margin:1em 0 0.3em;font-size:0.95em;">① Webhook <em>mit</em> Default Prompt</h3>
          <p style="margin:0 0 0.3em;font-size:0.88em;color:var(--muted);">Default Prompt im Webhook gespeichert → kein Body nötig. Einfachster Fall, z.B. cron-getriggerte Reports.</p>
          <pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.85em;"><code># Minimal — feuert mit dem gespeicherten Default-Prompt
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt;

# Mit zusätzlichem Kontext (z.B. CI-Payload, Sensorwert, …)
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/json" \\
  -d '{{"extra_context":"build #4231 failed on commit abc123"}}'

# Default-Prompt für DIESEN Lauf einmalig überschreiben
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/json" \\
  -d '{{"prompt":"Fasse das Folgende in 2 Sätzen zusammen","extra_context":"…lange Logdatei…"}}'</code></pre>

          <h3 style="margin:1.2em 0 0.3em;font-size:0.95em;">② Webhook <em>ohne</em> Default Prompt (open)</h3>
          <p style="margin:0 0 0.3em;font-size:0.88em;color:var(--muted);"><strong>Pflicht:</strong> <code>prompt</code> im Body. Sonst tut der Webhook nichts. Nutze das wenn jeder Aufruf eine andere Frage stellt.</p>
          <pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.85em;"><code># Pflicht-Body: prompt
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/json" \\
  -d '{{"prompt":"Welches Wetter ist heute in Wien?"}}'

# Mit allen optionalen Feldern
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/json" \\
  -d '{{
    "prompt":      "Analysiere die Daten und antworte als JSON",
    "extra_context": "raw_payload_or_data_here",
    "silent":       false,
    "save_output":  true,
    "output_name":  "analyse-2026-05-16.txt",
    "model":        "qwen3"
  }}'</code></pre>

          <h3 style="margin:1.2em 0 0.3em;font-size:0.95em;">Felder-Referenz</h3>
          <table style="font-size:0.85em;">
            <thead><tr><th>Feld</th><th>Pflicht</th><th>Bedeutung</th></tr></thead>
            <tbody>
              <tr><td><code>prompt</code></td><td>nur wenn kein Default</td><td>Was das Modell tun soll (Plain Language). Überschreibt Default falls beides gesetzt.</td></tr>
              <tr><td><code>extra_context</code></td><td>optional</td><td>Zusätzliche Daten (Payload, Logs, Status) — wird vor den Prompt gestellt.</td></tr>
              <tr><td><code>silent</code></td><td>optional</td><td><code>true</code> = nicht in Chat pushen, nur als Datei speichern. Default: wie Webhook-Setting.</td></tr>
              <tr><td><code>save_output</code></td><td>optional</td><td><code>false</code> = Output nicht persistieren. Default: <code>true</code>.</td></tr>
              <tr><td><code>output_name</code></td><td>optional</td><td>Dateiname für gespeicherten Output. Default: Timestamp.</td></tr>
              <tr><td><code>model</code></td><td>optional</td><td>Modell-Alias/Name nur für diesen Lauf. Default: das im Webhook gespeicherte.</td></tr>
            </tbody>
          </table>

          <h3 style="margin:1.2em 0 0.3em;font-size:0.95em;">③ Externe Services — Foreign Payloads</h3>
          <p style="margin:0 0 0.3em;font-size:0.88em;color:var(--muted);">
            Externe Services (GitHub, Slack, IoT, CI) kennen <em>unser</em> Body-Schema nicht — sie schicken ihr eigenes JSON / Form-encoded / raw Text.
            <strong>Mechanik:</strong> Setze einen Default-Prompt à la „Parse Payload und schicke Summary“. Der ganze Body + alle <code>X-*</code>/<code>User-Agent</code>-Header werden automatisch als <code>extra_context</code> reingereicht. Für externe Services ist Default-Prompt <strong>de facto Pflicht</strong>, da sie nie <code>prompt</code> mitschicken.
          </p>

          <h4 style="margin:0.8em 0 0.2em;font-size:0.92em;">GitHub Webhook → Matrix</h4>
          <p style="margin:0 0 0.3em;font-size:0.85em;color:var(--muted);">Repo Settings → Webhooks → Add Webhook. Content type <code>application/json</code>.</p>
          <pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.85em;"><code># Webhook anlegen:
# Default Prompt: "Fasse das GitHub-Event in 1 Zeile (Event-Typ aus X-GitHub-Event).
#                  Bei pull_request: '&lt;action&gt;: &lt;title&gt; von &lt;user.login&gt;'.
#                  Bei push: '&lt;n&gt; commits to &lt;ref&gt; von &lt;pusher.name&gt;'.
#                  Bei ping: still bleiben — antworte exakt [NO_MESSAGE]."
# Client: matrix, Room: !abc:server, Silent: aus
#
# Payload URL für GitHub: {_host_base}/webhook/&lt;TOKEN&gt;
#
# GitHub schickt automatisch bei jedem Event:
#   POST {_host_base}/webhook/&lt;TOKEN&gt;
#   Headers: X-GitHub-Event: pull_request, X-GitHub-Delivery: &lt;uuid&gt;, X-Hub-Signature-256: …
#   Body: {{"action":"opened","pull_request":{{"title":"Fix bug","user":{{"login":"alice"}}, …}}, …}}
# → Modell schreibt z.B. "opened: 'Fix bug' von alice" in Matrix.</code></pre>

          <h4 style="margin:0.8em 0 0.2em;font-size:0.92em;">Slack Outgoing Webhook → Matrix (form-encoded)</h4>
          <p style="margin:0 0 0.3em;font-size:0.85em;color:var(--muted);">Slack App → Outgoing Webhooks → Trigger Word + URL. Slack POSTet <code>application/x-www-form-urlencoded</code>.</p>
          <pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.85em;"><code># Default Prompt: "Slack-Nachricht. Wenn 'urgent' im text oder trigger_word=alarm
#                  → in Matrix posten als '@&lt;user_name&gt;: &lt;text&gt;'.
#                  Sonst antworte exakt [NO_MESSAGE]."
# Client: matrix, Room: !abc:server
#
# Slack schickt z.B.:
#   POST {_host_base}/webhook/&lt;TOKEN&gt;
#   Content-Type: application/x-www-form-urlencoded
#   text=urgent+server+down&user_name=alice&trigger_word=alarm&channel_name=ops
# → Modell pushed nach Matrix wenn Bedingung passt.

# Manueller Test (Slack-ähnlich simulieren):
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/x-www-form-urlencoded" \\
  -d 'text=urgent+server+down&user_name=alice&trigger_word=alarm'</code></pre>

          <h4 style="margin:0.8em 0 0.2em;font-size:0.92em;">Discord Bot Interaction Forwarding</h4>
          <p style="margin:0 0 0.3em;font-size:0.85em;color:var(--muted);">Discord hat keine klassischen „Outgoing Webhooks“. Stattdessen: dein Discord-Bot/Slash-Command-Handler POSTet zu uns weiter (z.B. via discord-interactions library mit eigenem Proxy-Bot).</p>
          <pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.85em;"><code># Mit JSON-Forwarder, der Discord-Interaction payload weitergibt:
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/json" \\
  -d '{{"type":2,"data":{{"name":"summarize","options":[]}},"member":{{"user":{{"username":"alice"}}}},"channel_id":"123"}}'
# → Modell liest type/data/options aus Payload, antwortet entsprechend.</code></pre>

          <h4 style="margin:0.8em 0 0.2em;font-size:0.92em;">IoT-Sensor mit bedingtem Alert</h4>
          <pre style="background:var(--bg);padding:0.6em;border-radius:4px;overflow-x:auto;font-size:0.85em;"><code># Default Prompt: "Wenn temp &gt; 30 → 'ALARM: &lt;sensor&gt; bei &lt;temp&gt;°C'.
#                  Sonst antworte exakt [NO_MESSAGE]."
# Client: matrix, Room: !home:server, save_output: true (Log behalten)
#
curl -X POST {_host_base}/webhook/&lt;TOKEN&gt; \\
  -H "Content-Type: application/json" \\
  -d '{{"sensor":"livingroom","temp":24.7,"ts":1715856000}}'
# → temp &lt;= 30: Modell antwortet [NO_MESSAGE] → kein Chat-Push, nur in Datei.
# → temp &gt; 30: Modell pusht Alarm nach Matrix.</code></pre>

          <p style="margin:0.8em 0 0;font-size:0.85em;color:var(--muted);">
            <strong>[NO_MESSAGE] Pattern:</strong> Wenn Modell exakt <code>[NO_MESSAGE]</code> antwortet, wird kein Chat-Push gemacht (Sentinel). Output wird trotzdem als Datei gespeichert wenn <code>save_output=true</code>. Nutze das für „nur posten bei Bedingung X“-Logik.
          </p>

          <p style="margin:1em 0 0;font-size:0.85em;color:var(--muted);">
            <strong>Antwort-Routing:</strong> Wenn Webhook ein <code>client</code>/<code>room_id</code>/<code>channel_id</code> hat → Output geht dort hin (Matrix/Discord). Sonst kommt der Output direkt im HTTP-Response-Body (gut für CI-Skripte die auf das Ergebnis warten).
          </p>
        </div>
      </details>
      <div class="card">
        <table>
          <thead><tr><th>Name</th><th>Default Prompt</th><th>Ziel</th><th>Token</th><th>Letzter Lauf</th><th>ID</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div style="margin-top:1em;">
        <a href="/{tq}" class="btn btn-outline">Startseite</a>
        <span class="text-muted" style="margin-left:1em;">▶ ausführen · ✎ bearbeiten · ✕ löschen · POST <code>/webhook/&lt;token&gt;</code> + JSON <code>{{"extra_context":"..."}}</code> · Chat: <code>/webhook &lt;text&gt;</code> für Folgefragen</span>
      </div>
    </div>
    <div class="modal-overlay" id="confirmModal">
      <div class="modal-box" style="max-width:480px;">
        <h2 id="confirmTitle" style="margin:0 0 0.6em;"></h2>
        <div id="confirmBody" style="font-size:0.92em;line-height:1.45;white-space:pre-wrap;"></div>
        <div class="modal-actions">
          <button class="btn btn-outline" id="confirmCancel">Abbrechen</button>
          <button class="btn btn-primary" id="confirmOk">OK</button>
        </div>
      </div>
    </div>
    <div id="toastWrap" style="position:fixed;bottom:1.2em;right:1.2em;display:flex;flex-direction:column;gap:0.5em;z-index:2000;pointer-events:none;"></div>
    <div class="modal-overlay" id="tokenModal">
      <div class="modal-box">
        <h2 id="tokenTitle">Token</h2>
        <p id="tokenHint" style="margin:0 0 0.5em;color:var(--muted);font-size:0.88em;"></p>
        <div style="display:flex;gap:0.4em;align-items:stretch;">
          <input type="text" id="tokenValue" readonly style="flex:1;font-family:monospace;font-size:0.92em;padding:0.5em;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);" onclick="this.select()">
          <button class="btn btn-outline" id="tokenCopy" style="white-space:nowrap;">📋 Kopieren</button>
        </div>
        <p id="tokenCopyStatus" style="margin:0.4em 0 0;font-size:0.82em;color:var(--muted);min-height:1em;"></p>
        <div class="modal-actions">
          <button class="btn btn-primary" id="tokenClose">Schließen</button>
        </div>
      </div>
    </div>
    <div class="modal-overlay" id="runModal">
      <div class="modal-box">
        <h2>Webhook ausführen — <span id="runName"></span></h2>
        <p style="margin:0 0 0.5em;color:var(--muted);font-size:0.9em;">Dieser Webhook hat keinen Default Prompt. Gib einen Prompt ein, der einmalig für diesen Lauf verwendet wird.</p>
        <div class="wh-form">
          <label for="runPrompt">Prompt <span class="hint">(Pflicht — was soll das Modell tun?)</span></label>
          <textarea id="runPrompt" rows="5" placeholder="z.B. Fasse die aktuellen Nachrichten von example.com als 3 Bullets zusammen."></textarea>
          <label for="runExtra">Extra Context <span class="hint">(optional — zusätzliche Daten z.B. Payload)</span></label>
          <textarea id="runExtra" rows="3" placeholder=""></textarea>
        </div>
        <p id="runError" style="margin:0.4em 0 0;font-size:0.85em;color:var(--danger);min-height:1em;"></p>
        <div class="modal-actions">
          <button class="btn btn-outline" id="runCancel">Abbrechen</button>
          <button class="btn btn-primary" id="runFire">Starten</button>
        </div>
      </div>
    </div>
    <div class="modal-overlay" id="editModal">
      <div class="modal-box">
        <h2>Webhook bearbeiten</h2>
        <div class="wh-form">
          <label for="editName">Name</label>
          <input type="text" id="editName">
          <label for="editPrompt">Default Prompt</label>
          <textarea id="editPrompt" rows="5"></textarea>
          <label for="editTarget">Ziel (Raum / Channel)</label>
          <select id="editTarget">{_build_target_options(load_config())}</select>
          <label for="editModel">Modell</label>
          <input type="text" id="editModel">
          <label for="editSilent">Silent</label>
          <div><label class="cb"><input type="checkbox" id="editSilent"> kein Chat-Push, Output nur in Datei</label></div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-outline" id="editCancel">Abbrechen</button>
          <button class="btn btn-primary" id="editSave">Speichern</button>
        </div>
      </div>
    </div>
    <script>
    var _editId = null;
    var token = new URLSearchParams(window.location.search).get("token") || "";
    function tq() {{ return token ? "?token=" + encodeURIComponent(token) : ""; }}
    /* === In-Page Confirm Modal (replaces window.confirm) === */
    var _confirmResolve = null;
    function appConfirm(title, body, opts) {{
      opts = opts || {{}};
      document.getElementById("confirmTitle").textContent = title || "";
      var bodyEl = document.getElementById("confirmBody");
      bodyEl.textContent = body || "";
      document.getElementById("confirmOk").textContent = opts.okLabel || "OK";
      document.getElementById("confirmCancel").textContent = opts.cancelLabel || "Abbrechen";
      document.getElementById("confirmModal").classList.add("open");
      return new Promise(function(resolve){{ _confirmResolve = resolve; }});
    }}
    function _confirmDone(result) {{
      document.getElementById("confirmModal").classList.remove("open");
      var r = _confirmResolve; _confirmResolve = null;
      if (r) r(result);
    }}
    document.getElementById("confirmOk").addEventListener("click", function(){{ _confirmDone(true); }});
    document.getElementById("confirmCancel").addEventListener("click", function(){{ _confirmDone(false); }});
    document.getElementById("confirmModal").addEventListener("click", function(e){{ if (e.target === this) _confirmDone(false); }});
    /* === Toast notifications (replaces window.alert for errors/status) === */
    function appToast(msg, kind) {{
      kind = kind || "info";
      var wrap = document.getElementById("toastWrap");
      var t = document.createElement("div");
      var bg = kind === "error" ? "#c53030" : (kind === "success" ? "#2d8a4e" : "#2a2a4a");
      t.style.cssText = "pointer-events:auto;background:" + bg + ";color:#fff;padding:0.6em 0.9em;border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,0.25);font-size:0.9em;max-width:380px;cursor:pointer;opacity:0;transition:opacity 0.2s;";
      t.textContent = msg;
      t.addEventListener("click", function(){{ t.remove(); }});
      wrap.appendChild(t);
      setTimeout(function(){{ t.style.opacity = "1"; }}, 10);
      setTimeout(function(){{ t.style.opacity = "0"; setTimeout(function(){{ if (t.parentNode) t.remove(); }}, 300); }}, kind === "error" ? 6000 : 3500);
    }}
    var _tokenReloadOnClose = false;
    function showToken(tok, opts) {{
      opts = opts || {{}};
      _tokenReloadOnClose = !!opts.reloadOnClose;
      document.getElementById("tokenTitle").textContent = opts.title || "Token";
      document.getElementById("tokenHint").textContent = opts.hint || "";
      var inp = document.getElementById("tokenValue");
      inp.value = tok;
      document.getElementById("tokenCopyStatus").textContent = "";
      document.getElementById("tokenModal").classList.add("open");
      setTimeout(function(){{ inp.focus(); inp.select(); }}, 50);
    }}
    function _closeTokenModal() {{
      document.getElementById("tokenModal").classList.remove("open");
      if (_tokenReloadOnClose) {{ _tokenReloadOnClose = false; location.reload(); }}
    }}
    document.getElementById("tokenClose").addEventListener("click", _closeTokenModal);
    document.getElementById("tokenModal").addEventListener("click", function(e){{ if (e.target === this) _closeTokenModal(); }});
    document.getElementById("tokenCopy").addEventListener("click", function(){{
      var v = document.getElementById("tokenValue").value;
      var status = document.getElementById("tokenCopyStatus");
      function ok(){{ status.textContent = "✓ Kopiert"; status.style.color = "var(--success, #2d8a4e)"; }}
      function fail(){{
        status.textContent = "Auto-Kopieren fehlgeschlagen — manuell mit Strg+C";
        status.style.color = "var(--muted)";
        document.getElementById("tokenValue").select();
      }}
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(v).then(ok, fail);
      }} else {{
        try {{ document.getElementById("tokenValue").select(); document.execCommand("copy"); ok(); }} catch (e) {{ fail(); }}
      }}
    }});
    var btnCreate = document.getElementById("btnCreate");
    function _doCreate(body) {{
      fetch("/api/webhook" + tq(), {{
        method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify(body)
      }}).then(function(r){{
        return r.json().then(function(d){{
          if (r.ok && d.item && d.item.token) {{
            showToken(d.item.token, {{
              title: "Webhook angelegt",
              hint: "Token jetzt kopieren — danach wird er im UI nur noch maskiert angezeigt. Schließen lädt die Liste neu.",
              reloadOnClose: true
            }});
          }} else if (r.ok) {{
            location.reload();
          }} else {{
            appToast(d.detail || d.error || "Fehler", "error");
          }}
        }});
      }}).catch(function(e){{ appToast("Fehler: " + e, "error"); }});
    }}
    if (btnCreate) btnCreate.addEventListener("click", function() {{
      var body = {{
        name: document.getElementById("newName").value.trim(),
        prompt: document.getElementById("newPrompt").value.trim(),
        model: document.getElementById("newModel").value.trim() || null,
        silent: document.getElementById("newSilent").checked,
        save_output: document.getElementById("newSaveOutput").checked,
      }};
      var tgt = document.getElementById("newTarget").value || "";
      if (tgt.indexOf("matrix:") === 0) {{
        body.room_id = tgt.substring(7); body.client = "matrix";
      }} else if (tgt.indexOf("discord:") === 0) {{
        body.channel_id = tgt.substring(8); body.client = "discord";
      }} else {{
        body.client = null;
      }}
      if (!body.prompt) {{
        appConfirm(
          "Kein Default Prompt gesetzt",
          "• Funktioniert nur wenn der Caller bei JEDEM POST ein 'prompt' Feld mitschickt (eigene Skripte, eigene Apps).\\n\\n" +
          "• Externe Services (GitHub, Discord, Slack, Sensoren …) schicken niemals 'prompt' — für die brauchst du einen Default Prompt.\\n\\n" +
          "Trotzdem ohne Default anlegen?",
          {{ okLabel: "Ohne Default anlegen" }}
        ).then(function(ok){{ if (ok) _doCreate(body); }});
      }} else {{
        _doCreate(body);
      }}
    }});
    document.querySelectorAll(".btn-reveal").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var id = this.getAttribute("data-id");
        fetch("/api/webhook/" + encodeURIComponent(id) + "/token" + tq(), {{credentials:"same-origin"}})
          .then(function(r){{ return r.json().then(function(d){{ return {{ok: r.ok, data: d}}; }}); }})
          .then(function(res) {{
            if (!res.ok) {{ appToast(res.data.detail || "Fehler", "error"); return; }}
            showToken(res.data.token || "", {{ title: "Token", hint: "Kopier-Button benutzen oder Textfeld auswählen und Strg+C." }});
          }})
          .catch(function(e){{ appToast("Fehler: " + e, "error"); }});
      }});
    }});
    document.querySelectorAll(".btn-edit").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        _editId = this.getAttribute("data-id");
        document.getElementById("editName").value = this.getAttribute("data-name") || "";
        document.getElementById("editPrompt").value = this.getAttribute("data-prompt") || "";
        var room = this.getAttribute("data-room") || "";
        var channel = this.getAttribute("data-channel") || "";
        var tgtSel = document.getElementById("editTarget");
        if (room) tgtSel.value = "matrix:" + room;
        else if (channel) tgtSel.value = "discord:" + channel;
        else tgtSel.value = "";
        document.getElementById("editModel").value = this.getAttribute("data-model") || "";
        document.getElementById("editSilent").checked = this.getAttribute("data-silent") === "1";
        document.getElementById("editModal").classList.add("open");
      }});
    }});
    document.getElementById("editCancel").addEventListener("click", function() {{ document.getElementById("editModal").classList.remove("open"); }});
    document.getElementById("editSave").addEventListener("click", function() {{
      var body = {{
        name: document.getElementById("editName").value.trim(),
        prompt: document.getElementById("editPrompt").value.trim(),
        model: document.getElementById("editModel").value.trim() || null,
        silent: document.getElementById("editSilent").checked,
      }};
      var tgt = document.getElementById("editTarget").value || "";
      if (tgt.indexOf("matrix:") === 0) {{
        body.room_id = tgt.substring(7); body.channel_id = null; body.client = "matrix";
      }} else if (tgt.indexOf("discord:") === 0) {{
        body.channel_id = tgt.substring(8); body.room_id = null; body.client = "discord";
      }} else {{
        body.room_id = null; body.channel_id = null; body.client = null;
      }}
      fetch("/api/webhook/" + encodeURIComponent(_editId) + tq(), {{
        method:"PATCH", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify(body)
      }}).then(function(r){{
        if (r.ok) location.reload();
        else r.json().then(function(d){{ appToast(d.detail || "Fehler", "error"); }});
      }}).catch(function(e){{ appToast("Fehler: " + e, "error"); }});
    }});
    document.getElementById("editModal").addEventListener("click", function(e){{ if (e.target === this) this.classList.remove("open"); }});
    var _runId = null;
    var _runBtn = null;
    function _doFire(id, body, btn) {{
      btn.disabled = true; btn.textContent = "…";
      var opts = {{ method: "POST" }};
      if (body) {{
        opts.headers = {{ "Content-Type": "application/json" }};
        opts.body = JSON.stringify(body);
      }}
      fetch("/api/webhook/" + encodeURIComponent(id) + "/run" + tq(), opts).then(function(r){{
        if (r.ok) {{ btn.textContent = "✓"; appToast("Webhook gestartet", "success"); setTimeout(function(){{ btn.disabled = false; btn.innerHTML = "&#9654;"; }}, 2000); }}
        else r.json().then(function(d){{ appToast(d.detail || "Fehler", "error"); btn.disabled = false; btn.innerHTML = "&#9654;"; }});
      }}).catch(function(e){{ appToast("Fehler: " + e, "error"); btn.disabled = false; btn.innerHTML = "&#9654;"; }});
    }}
    document.querySelectorAll(".btn-run").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var id = this.getAttribute("data-id");
        var hasDefault = this.getAttribute("data-has-default") === "1";
        if (hasDefault) {{
          _doFire(id, null, this);
          return;
        }}
        _runId = id;
        _runBtn = this;
        document.getElementById("runName").textContent = this.getAttribute("data-name") || id.slice(0,8);
        document.getElementById("runPrompt").value = "";
        document.getElementById("runExtra").value = "";
        document.getElementById("runError").textContent = "";
        document.getElementById("runModal").classList.add("open");
        setTimeout(function(){{ document.getElementById("runPrompt").focus(); }}, 50);
      }});
    }});
    document.getElementById("runCancel").addEventListener("click", function() {{ document.getElementById("runModal").classList.remove("open"); }});
    document.getElementById("runModal").addEventListener("click", function(e){{ if (e.target === this) this.classList.remove("open"); }});
    document.getElementById("runFire").addEventListener("click", function() {{
      var p = document.getElementById("runPrompt").value.trim();
      var err = document.getElementById("runError");
      if (!p) {{
        err.textContent = "Prompt ist Pflicht — dieser Webhook hat keinen Default.";
        document.getElementById("runPrompt").focus();
        return;
      }}
      err.textContent = "";
      var body = {{ prompt: p }};
      var ex = document.getElementById("runExtra").value.trim();
      if (ex) body.extra_context = ex;
      document.getElementById("runModal").classList.remove("open");
      if (_runId && _runBtn) _doFire(_runId, body, _runBtn);
    }});
    document.querySelectorAll(".btn-del").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var id = this.getAttribute("data-id");
        appConfirm(
          "Webhook löschen?",
          "ID: " + id.slice(0,8) + "\\n\\nOutputs (gespeicherte Dateien) bleiben erhalten.",
          {{ okLabel: "Löschen" }}
        ).then(function(ok){{
          if (!ok) return;
          fetch("/api/webhook/" + encodeURIComponent(id) + tq(), {{ method:"DELETE" }}).then(function(r){{
            if (r.ok) location.reload();
            else r.json().then(function(d){{ appToast(d.detail || "Fehler", "error"); }});
          }}).catch(function(e){{ appToast("Fehler: " + e, "error"); }});
        }});
      }});
    }});
    </script>
    {test_js}
    {_THEME_JS}
    </body></html>
    """
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Rooms / Channels page (Matrix + Discord per-room response modes)
# ---------------------------------------------------------------------------

def _build_target_options(config: dict, *, selected_matrix: str = "", selected_discord: str = "") -> str:
    """Build <option> list HTML for the room/channel select used in /schedules and /webhooks.
    Value format: '' (none), 'matrix:<room_id>' or 'discord:<channel_id>'.
    Only includes clients that are enabled. Adds 'kein expliziter Raum' as default option."""
    data = _collect_rooms_and_channels(config)
    parts = ['<option value="">— kein expliziter Raum (an alle authed User) —</option>']
    if data["matrix_enabled"]:
        if data["matrix_rooms"]:
            parts.append('<optgroup label="Matrix">')
            for r in data["matrix_rooms"]:
                if r.get("offline"):
                    continue
                rid = r["id"]
                sel = " selected" if rid == selected_matrix else ""
                label = f'{r["name"]} ({rid})'
                parts.append(f'<option value="matrix:{_escape(rid)}"{sel}>{_escape(label)}</option>')
            parts.append('</optgroup>')
        else:
            parts.append('<optgroup label="Matrix"><option disabled>(Bot in keinem Raum oder noch nicht verbunden)</option></optgroup>')
    if data["discord_enabled"]:
        if data["discord_channels"]:
            parts.append('<optgroup label="Discord">')
            for c in data["discord_channels"]:
                if c.get("offline"):
                    continue
                cid = c["id"]
                sel = " selected" if cid == selected_discord else ""
                guild = c.get("guild") or ""
                label = f'#{c["name"]} ({guild}) [{cid}]' if guild else f'#{c["name"]} [{cid}]'
                parts.append(f'<option value="discord:{_escape(cid)}"{sel}>{_escape(label)}</option>')
            parts.append('</optgroup>')
        else:
            parts.append('<optgroup label="Discord"><option disabled>(Bot in keinem Channel oder noch nicht verbunden)</option></optgroup>')
    return "\n".join(parts)


def _resolve_target_name(config: dict, *, room_id: str = "", channel_id: str = "") -> str:
    """Returns the human-friendly name for a stored room_id/channel_id, or empty string if not resolvable."""
    if not room_id and not channel_id:
        return ""
    data = _collect_rooms_and_channels(config)
    if room_id:
        for r in data["matrix_rooms"]:
            if r["id"] == room_id:
                return r["name"]
    if channel_id:
        for c in data["discord_channels"]:
            if c["id"] == channel_id:
                return f'#{c["name"]}'
    return ""


def _collect_rooms_and_channels(config: dict) -> dict:
    """Pull joined Matrix rooms + Discord channels, merge with saved modes from config."""
    matrix_cfg = (config.get("chat_clients") or {}).get("matrix") or {}
    discord_cfg = (config.get("chat_clients") or {}).get("discord") or {}
    room_modes = matrix_cfg.get("room_modes") or {}
    channel_modes = discord_cfg.get("channel_modes") or {}
    room_settings = matrix_cfg.get("room_settings") or {}
    channel_settings = discord_cfg.get("channel_settings") or {}

    def _settings_for(rs: dict, rid: str, members: int) -> dict:
        s = rs.get(rid) or {}
        ctx_mode = (s.get("context") or "").strip().lower()
        if ctx_mode not in ("agent", "group"):
            # Default: group für Räume mit >2 Mitgliedern, sonst agent (DM)
            ctx_mode = "group" if (members or 0) > 2 else "agent"
        return {
            "context": ctx_mode,
            "language": (s.get("language") or "auto").strip().lower(),
            "tools_allow": list(s.get("tools_allow") or []),
            "workspace_subdir": (s.get("workspace_subdir") or "").strip(),
            "auto_context_count": int(s.get("auto_context_count")) if s.get("auto_context_count") is not None else 3,
            "auto_context_max_chars": int(s.get("auto_context_max_chars")) if s.get("auto_context_max_chars") is not None else 200,
            "docs_in_sandbox": bool(s.get("docs_in_sandbox")),
            "search_chat_history_max": int(s.get("search_chat_history_max")) if s.get("search_chat_history_max") is not None else 200,
            "is_default_settings": rid not in rs,
        }

    matrix_rooms: list[dict] = []
    discord_channels: list[dict] = []
    try:
        from miniassistant.matrix_bot import list_joined_rooms
        for r in list_joined_rooms():
            mode = (room_modes.get(r["id"]) or "").strip().lower()
            if mode not in ("always", "mention", "off"):
                mode = "always" if r.get("members") == 2 else "mention"
            matrix_rooms.append({**r, "mode": mode, "is_default": not room_modes.get(r["id"]),
                                 "settings": _settings_for(room_settings, r["id"], r.get("members", 0))})
    except Exception as e:
        logger.warning("list_joined_rooms failed: %s", e)
    try:
        from miniassistant.discord_bot import list_channels
        for c in list_channels():
            mode = (channel_modes.get(c["id"]) or "").strip().lower()
            if mode not in ("always", "mention", "off"):
                mode = "always" if c.get("kind") == "dm" else "mention"
            members_proxy = 2 if c.get("kind") == "dm" else 3  # DM=agent default, text-channel=group default
            discord_channels.append({**c, "mode": mode, "is_default": not channel_modes.get(c["id"]),
                                     "settings": _settings_for(channel_settings, c["id"], members_proxy)})
    except Exception as e:
        logger.warning("list_channels failed: %s", e)

    seen_matrix = {r["id"] for r in matrix_rooms}
    for rid, mode in room_modes.items():
        if rid not in seen_matrix:
            matrix_rooms.append({"id": rid, "name": "(nicht verbunden)", "members": 0, "encrypted": False,
                                 "mode": mode, "is_default": False, "offline": True,
                                 "settings": _settings_for(room_settings, rid, 0)})
    seen_discord = {c["id"] for c in discord_channels}
    for cid, mode in channel_modes.items():
        if cid not in seen_discord:
            discord_channels.append({"id": cid, "name": "(nicht verbunden)", "guild": "", "kind": "text",
                                     "mode": mode, "is_default": False, "offline": True,
                                     "settings": _settings_for(channel_settings, cid, 3)})
    return {
        "matrix_enabled": bool(matrix_cfg.get("enabled")),
        "discord_enabled": bool(discord_cfg.get("enabled")),
        "matrix_rooms": matrix_rooms,
        "discord_channels": discord_channels,
    }


@app.get("/api/rooms")
async def api_rooms_get(request: Request):
    _require_token(request)
    config = load_config()
    return JSONResponse(_collect_rooms_and_channels(config))


@app.post("/api/rooms/leave")
async def api_rooms_leave(request: Request):
    """Body: {"kind": "matrix"|"discord_guild", "id": "<room_id_or_guild_id>"}.
    Matrix: bot leaves the room. Discord: bot leaves the entire guild (no per-channel leave exists)."""
    _require_token(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    kind = (body.get("kind") or "").strip().lower()
    target_id = (body.get("id") or "").strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="id required")
    loop = asyncio.get_event_loop()
    if kind == "matrix":
        try:
            from miniassistant.matrix_bot import leave_room
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"matrix_bot import failed: {e}")
        # leave_room is sync + blocks up to 30s on future.result — offload to thread pool
        ok, msg = await loop.run_in_executor(_chat_executor, leave_room, target_id)
        if ok:
            try:
                config = load_config()
                section = ((config.get("chat_clients") or {}).get("matrix") or {})
                modes = section.get("room_modes") or {}
                if target_id in modes:
                    modes.pop(target_id, None)
                    if not modes:
                        section.pop("room_modes", None)
                    save_config(config)
            except Exception as e:
                logger.warning("leave: mode cleanup failed: %s", e)
        return JSONResponse({"ok": ok, "message": msg or ("ok" if ok else "leave failed (no detail from matrix server)")})
    if kind == "discord_guild":
        try:
            from miniassistant.discord_bot import leave_guild
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"discord_bot import failed: {e}")
        ok, msg = await loop.run_in_executor(_chat_executor, leave_guild, target_id)
        return JSONResponse({"ok": ok, "message": msg or ("ok" if ok else "leave failed")})
    raise HTTPException(status_code=400, detail="kind must be 'matrix' or 'discord_guild'")


@app.patch("/api/rooms")
async def api_rooms_patch(request: Request):
    """Body: {"matrix": {"<room_id>": "always|mention|off"}, "discord": {"<channel_id>": "..."}}.
    Setting value to null/empty string removes override (falls back to default)."""
    _require_token(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    valid = {"always", "mention", "off"}
    counts = {"m": 0, "d": 0}

    def _apply_one(cc: dict, client_key: str, modes_key: str, payload_key: str) -> int:
        section = cc.get(client_key)
        if not isinstance(section, dict):
            return 0
        current = section.get(modes_key) or {}
        if not isinstance(current, dict):
            current = {}
        incoming = body.get(payload_key) or {}
        if not isinstance(incoming, dict):
            return 0
        changed = 0
        for k, v in incoming.items():
            if not isinstance(k, str) or not k:
                continue
            if v in (None, ""):
                if k in current:
                    current.pop(k, None); changed += 1
                continue
            v2 = str(v).strip().lower()
            if v2 not in valid:
                continue
            if current.get(k) != v2:
                current[k] = v2; changed += 1
        if current:
            section[modes_key] = current
        elif modes_key in section:
            section.pop(modes_key, None)
        return changed

    def _updater(config: dict) -> None:
        cc = config.setdefault("chat_clients", {})
        counts["m"] = _apply_one(cc, "matrix", "room_modes", "matrix")
        counts["d"] = _apply_one(cc, "discord", "channel_modes", "discord")

    from miniassistant.config import save_config_atomic
    save_config_atomic(_updater)
    return JSONResponse({"ok": True, "matrix_changes": counts["m"], "discord_changes": counts["d"]})


@app.patch("/api/rooms/settings")
async def api_rooms_settings_patch(request: Request):
    """Body: {"matrix": {"<room_id>": {context, language, tools_allow[], workspace_subdir} | null}, "discord": {...}}.
    null = Eintrag entfernen (fällt auf Defaults zurück). Per-Room Group-Mode-Konfiguration."""
    _require_token(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    from miniassistant.group_rooms import GROUP_ALLOWED_TOOLS, sanitize_workspace_subdir
    import re as _re
    valid_ctx = {"agent", "group"}
    valid_lang = _re.compile(r"^(auto|[a-z]{2}(-[A-Z]{2})?)$")

    def _norm_entry(entry: Any) -> dict | None:
        if not isinstance(entry, dict):
            return None
        ctx_m = str(entry.get("context") or "").strip().lower()
        if ctx_m not in valid_ctx:
            ctx_m = "agent"
        lang = str(entry.get("language") or "auto").strip().lower()
        if not valid_lang.match(lang):
            lang = "auto"
        raw_tools = entry.get("tools_allow") or []
        if not isinstance(raw_tools, list):
            raw_tools = []
        tools = sorted({str(t).strip() for t in raw_tools if isinstance(t, str)} & GROUP_ALLOWED_TOOLS)
        sub = str(entry.get("workspace_subdir") or "").strip()
        if sub:
            sub = sanitize_workspace_subdir(sub)
        out: dict[str, Any] = {"context": ctx_m, "language": lang, "tools_allow": tools}
        if sub:
            out["workspace_subdir"] = sub
        # Auto-context per-room knobs (clamped to safe ranges)
        if "auto_context_count" in entry:
            try:
                out["auto_context_count"] = max(0, min(int(entry.get("auto_context_count") or 0), 20))
            except (TypeError, ValueError):
                pass
        if "auto_context_max_chars" in entry:
            try:
                from miniassistant.group_rooms import AUTO_CONTEXT_MAX_CHARS_CAP
                out["auto_context_max_chars"] = max(20, min(int(entry.get("auto_context_max_chars") or 200), AUTO_CONTEXT_MAX_CHARS_CAP))
            except (TypeError, ValueError):
                pass
        # Docs read-only mount toggle
        if "docs_in_sandbox" in entry:
            out["docs_in_sandbox"] = bool(entry.get("docs_in_sandbox"))
        # search_chat_history max scan limit per room (10-500)
        if "search_chat_history_max" in entry:
            try:
                out["search_chat_history_max"] = max(10, min(int(entry.get("search_chat_history_max") or 200), 500))
            except (TypeError, ValueError):
                pass
        return out

    counts = {"m": 0, "d": 0}

    def _apply_one(cc: dict, client_key: str, store_key: str, payload_key: str) -> int:
        section = cc.get(client_key)
        if not isinstance(section, dict):
            section = {}
            cc[client_key] = section
        current = section.get(store_key) or {}
        if not isinstance(current, dict):
            current = {}
        incoming = body.get(payload_key) or {}
        if not isinstance(incoming, dict):
            return 0
        changed = 0
        for k, v in incoming.items():
            if not isinstance(k, str) or not k:
                continue
            if v is None:
                if k in current:
                    current.pop(k, None); changed += 1
                continue
            normed = _norm_entry(v)
            if normed is None:
                continue
            if current.get(k) != normed:
                current[k] = normed; changed += 1
        if current:
            section[store_key] = current
        elif store_key in section:
            section.pop(store_key, None)
        return changed

    def _updater(config: dict) -> None:
        cc = config.setdefault("chat_clients", {})
        counts["m"] = _apply_one(cc, "matrix", "room_settings", "matrix")
        counts["d"] = _apply_one(cc, "discord", "channel_settings", "discord")

    from miniassistant.config import save_config_atomic
    save_config_atomic(_updater)
    return JSONResponse({"ok": True, "matrix_changes": counts["m"], "discord_changes": counts["d"]})


@app.get("/rooms", response_class=HTMLResponse)
async def rooms_page(request: Request):
    _require_token(request)
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    config = load_config()
    data = _collect_rooms_and_channels(config)

    def _mode_select(prefix: str, id_: str, current: str) -> str:
        opts = []
        for v, label in [("always", "Immer antworten"), ("mention", "Nur auf Erwähnung"), ("off", "Aus (nur Tools)")]:
            sel = " selected" if v == current else ""
            opts.append(f'<option value="{v}"{sel}>{label}</option>')
        return f'<select class="mode-sel" data-kind="{prefix}" data-id="{_escape(id_)}">{"".join(opts)}</select>'

    _GROUP_TOOLS_ALL = ("web_search", "read_url", "check_url", "send_image", "send_audio", "exec", "read_recent_messages", "search_chat_history", "invoke_model")

    def _settings_row(prefix: str, target_id: str, s: dict, colspan: int) -> str:
        """Ausklappbare Detailzeile für Group-Mode-Settings (context/language/tools/workspace)."""
        if not s:
            return ""
        ctx_m = s.get("context", "agent")
        lang = s.get("language", "auto")
        allow = set(s.get("tools_allow") or [])
        sub = s.get("workspace_subdir", "")
        ac_count = int(s.get("auto_context_count", 3))
        ac_max = int(s.get("auto_context_max_chars", 200))
        sch_max = int(s.get("search_chat_history_max", 200))
        docs_mount = bool(s.get("docs_in_sandbox"))
        is_def = s.get("is_default_settings")
        ctx_opts = "".join(
            f'<option value="{v}"{" selected" if v == ctx_m else ""}>{lbl}</option>'
            for v, lbl in (("agent", "Agent (voller persönlicher Kontext)"), ("group", "Group (slim, sandboxed)"))
        )
        lang_choices = [("auto", "auto (Sprache des Inputs)"), ("de", "Deutsch"), ("en", "English"),
                        ("fr", "Français"), ("es", "Español"), ("it", "Italiano"), ("nl", "Nederlands"), ("pt", "Português")]
        lang_opts = "".join(
            f'<option value="{v}"{" selected" if v == lang else ""}>{lbl}</option>'
            for v, lbl in lang_choices
        )
        tool_checks = ""
        for t in _GROUP_TOOLS_ALL:
            checked = " checked" if t in allow else ""
            label = t + (" (sandboxed via bwrap)" if t == "exec" else "")
            tool_checks += (
                f'<label style="display:inline-flex;align-items:center;gap:0.3em;margin-right:0.9em;font-weight:normal;">'
                f'<input type="checkbox" class="grp-tool" data-tool="{t}"{checked}> <code>{t}</code><span style="font-size:0.78em;color:var(--muted);">{" sandboxed" if t == "exec" else ""}</span></label>'
            )
        hint = " <em>(Standardwerte — noch nicht explizit gespeichert)</em>" if is_def else ""
        return (
            f'<tr class="grp-settings-row" data-kind="{prefix}" data-id="{_escape(target_id)}" style="display:none;background:rgba(0,0,0,0.02);">'
            f'<td colspan="{colspan}" style="padding:0.7em 1em;">'
            f'<div style="display:grid;grid-template-columns:200px 1fr;gap:0.5em 1em;align-items:center;font-size:0.9em;">'
            f'<label>Kontext-Modus</label>'
            f'<select class="grp-ctx">{ctx_opts}</select>'
            f'<label>Sprache</label>'
            f'<select class="grp-lang">{lang_opts}</select>'
            f'<label>Tools (Whitelist)</label>'
            f'<div class="grp-tools">{tool_checks}</div>'
            f'<label>Workspace-Subdir</label>'
            f'<input type="text" class="grp-sub" value="{_escape(sub)}" placeholder="(automatisch aus Room-ID)" style="padding:0.25em 0.4em;font-size:0.9em;">'
            f'<label>Auto-Context: Nachrichten</label>'
            f'<div style="display:flex;align-items:center;gap:0.6em;"><span style="font-size:0.82em;color:var(--muted);flex:1;">vorherige Raum-Nachrichten automatisch prependen (0 = aus, default 3)</span><input type="number" class="grp-ac-count" value="{ac_count}" min="0" max="20" step="1" style="padding:0.25em 0.4em;font-size:0.9em;width:6em;" title="0 = aus, max 20"></div>'
            f'<label>Auto-Context: Zeichen/Nachricht</label>'
            f'<div style="display:flex;align-items:center;gap:0.6em;"><span style="font-size:0.82em;color:var(--muted);flex:1;">truncate jede Nachricht (default 200, max 5000)</span><input type="number" class="grp-ac-max" value="{ac_max}" min="20" max="5000" step="10" style="padding:0.25em 0.4em;font-size:0.9em;width:6em;" title="20–5000"></div>'
            f'<label>Chat-Suche: Max Scan</label>'
            f'<div style="display:flex;align-items:center;gap:0.6em;"><span style="font-size:0.82em;color:var(--muted);flex:1;">obergrenze für <code>search_chat_history</code> (default 200)</span><input type="number" class="grp-sch-max" value="{sch_max}" min="10" max="500" step="10" style="padding:0.25em 0.4em;font-size:0.9em;width:6em;" title="10–500"></div>'
            f'<label>Docs in Sandbox</label>'
            f'<label style="display:inline-flex;align-items:center;gap:0.4em;font-weight:normal;"><input type="checkbox" class="grp-docs"{" checked" if docs_mount else ""}> <span style="font-size:0.82em;color:var(--muted);">mountet <code>/docs/</code> read-only (Bot kann via <code>exec cat /docs/FILE</code> Doku lesen)</span></label>'
            f'</div>'
            f'<div style="margin-top:0.6em;font-size:0.82em;color:var(--muted);">'
            f'Group-Mode lädt slimen System-Prompt (keine SOUL/USER/Memory/Palace). '
            f'<code>exec</code> läuft in bwrap-Sandbox, sieht nur <code>&lt;workspace&gt;/groups/&lt;subdir&gt;/</code> + Systembinaries.{hint}'
            f'</div>'
            f'</td></tr>'
        )

    def _auth_badge(r: dict) -> str:
        others = r.get("others") or []
        if not others:
            return '<span style="color:var(--muted);font-size:0.85em;">—</span>'
        if r.get("room_trusted"):
            inv = _escape(r.get("inviter") or "")
            tip = f"Bot wurde von {inv} (authed) eingeladen — alle {len(others)} Mitglieder dürfen den Bot benutzen"
            return f'<span title="{tip}" style="color:#2d8a4e;">✅ Raum vertraut</span>'
        if r.get("members") == 2 and len(others) == 1:
            o = others[0]
            uid = _escape(o["user_id"])
            if o["authed"]:
                return f'<span title="{uid} hat Auth" style="color:#2d8a4e;">✅ Auth</span>'
            return f'<span title="{uid} hat KEIN Auth — User muss /auth Code einlösen, oder Bot neu vom authed User einladen lassen" style="color:#c87000;">⚠️ kein Auth</span>'
        # Group room, kein Raum-Trust
        ac = r.get("auth_count", 0)
        tot = len(others)
        color = "#2d8a4e" if ac == tot else ("#c87000" if ac == 0 else "var(--text)")
        inv = r.get("inviter") or "?"
        inv_hint = f" (Inviter: {_escape(inv)} ⚠ nicht authed)" if r.get("inviter") else " (Inviter unbekannt — Bot neu einladen für Raum-Trust)"
        return f'<span style="color:{color};font-size:0.9em;" title="{ac} von {tot} Mitgliedern haben Auth{inv_hint}">👥 {ac}/{tot} mit Auth</span>'

    matrix_rows = ""
    if not data["matrix_enabled"]:
        matrix_rows = '<tr><td colspan="6" style="text-align:center;color:var(--muted);font-style:italic;">Matrix-Client nicht aktiviert.</td></tr>'
    elif not data["matrix_rooms"]:
        matrix_rows = '<tr><td colspan="6" style="text-align:center;color:var(--muted);font-style:italic;">Keine Räume gefunden (Bot noch nicht verbunden oder in keinem Raum).</td></tr>'
    else:
        for r in data["matrix_rooms"]:
            offline = '<span style="color:var(--muted);font-size:0.8em;"> (offline)</span>' if r.get("offline") else ""
            enc = ' <span title="E2EE" style="font-size:0.8em;color:var(--muted);">🔒</span>' if r.get("encrypted") else ""
            dm = ' <span title="DM" style="font-size:0.78em;color:var(--muted);">DM</span>' if r.get("members") == 2 else ""
            members = f'<span style="color:var(--muted);font-size:0.85em;">{r.get("members", "?")} Mitg.</span>'
            default_hint = ' <span style="color:var(--muted);font-size:0.78em;" title="kein expliziter Mode — automatischer Default">(default)</span>' if r.get("is_default") else ""
            leave_btn = '' if r.get("offline") else f'<button class="btn-leave" data-kind="matrix" data-id="{_escape(r["id"])}" data-name="{_escape(r["name"])}" title="Bot aus diesem Raum entfernen">Verlassen</button>'
            # DMs (2 Mitglieder) sind immer owner-mode → keine Group-Settings nötig
            is_dm = r.get("members") == 2
            adv_btn = "" if is_dm else f'<button class="btn-adv" data-target-kind="matrix" data-target-id="{_escape(r["id"])}" title="Group-Mode-Einstellungen für diesen Raum">⚙</button>'
            matrix_rows += (
                f'<tr><td><strong>{_escape(r["name"])}</strong>{enc}{dm}{offline}</td>'
                f'<td><code style="font-size:0.78em;">{_escape(r["id"])}</code></td>'
                f'<td>{members}</td>'
                f'<td>{_auth_badge(r)}</td>'
                f'<td>{_mode_select("matrix", r["id"], r["mode"])}{default_hint}</td>'
                f'<td>{adv_btn} {leave_btn}</td></tr>'
            )
            if not is_dm:
                matrix_rows += _settings_row("matrix", r["id"], r.get("settings") or {}, colspan=6)

    # Discord: group by guild for leave-button context
    discord_rows = ""
    if not data["discord_enabled"]:
        discord_rows = '<tr><td colspan="5" style="text-align:center;color:var(--muted);font-style:italic;">Discord-Client nicht aktiviert.</td></tr>'
    elif not data["discord_channels"]:
        discord_rows = '<tr><td colspan="5" style="text-align:center;color:var(--muted);font-style:italic;">Keine Channels gefunden (Bot noch nicht verbunden oder in keinem Server/DM).</td></tr>'
    else:
        # Find unique guilds with guild_id for leave button
        seen_guilds: dict[str, str] = {}
        for c in data["discord_channels"]:
            gid = c.get("guild_id") or ""
            gname = c.get("guild") or ""
            if gid and gid not in seen_guilds:
                seen_guilds[gid] = gname
        for c in data["discord_channels"]:
            offline = '<span style="color:var(--muted);font-size:0.8em;"> (offline)</span>' if c.get("offline") else ""
            kind = ' <span title="Direct Message" style="font-size:0.78em;color:var(--muted);">DM</span>' if c.get("kind") == "dm" else ""
            gname = c.get("guild") or ""
            trust_badge = ""
            if c.get("guild_trusted"):
                inv = _escape(c.get("inviter") or "")
                trust_badge = f' <span title="Bot in Guild eingeladen von User-ID {inv} (authed) — alle Guild-Mitglieder vertraut" style="color:#2d8a4e;font-size:0.78em;">✅ vertraut</span>'
            elif c.get("inviter"):
                inv = _escape(c.get("inviter") or "")
                trust_badge = f' <span title="Inviter User-ID {inv} ist NICHT authed — Guild nicht vertraut, jeder User braucht eigenes Auth" style="color:#c87000;font-size:0.78em;">⚠ Inviter nicht authed</span>'
            elif c.get("kind") == "text":
                trust_badge = ' <span title="Kein bot_add Audit-Log gefunden — vermutlich keine VIEW_AUDIT_LOG Permission, kein Guild-Trust möglich" style="color:var(--muted);font-size:0.78em;">? kein Audit-Log</span>'
            guild = f'<span style="color:var(--muted);font-size:0.85em;">{_escape(gname)}</span>{trust_badge}'
            default_hint = ' <span style="color:var(--muted);font-size:0.78em;" title="kein expliziter Mode — automatischer Default">(default)</span>' if c.get("is_default") else ""
            # DMs sind immer owner-mode → keine Group-Settings nötig
            is_dm_c = c.get("kind") == "dm"
            adv_btn = "" if is_dm_c else f'<button class="btn-adv" data-target-kind="discord" data-target-id="{_escape(c["id"])}" title="Group-Mode-Einstellungen für diesen Channel">⚙</button>'
            discord_rows += (
                f'<tr><td><strong>{_escape(c["name"])}</strong>{kind}{offline}</td>'
                f'<td><code style="font-size:0.78em;">{_escape(c["id"])}</code></td>'
                f'<td>{guild}</td>'
                f'<td>{_mode_select("discord", c["id"], c["mode"])}{default_hint}</td>'
                f'<td>{adv_btn}</td></tr>'
            )
            if not is_dm_c:
                discord_rows += _settings_row("discord", c["id"], c.get("settings") or {}, colspan=5)
        if seen_guilds:
            for gid, gname in seen_guilds.items():
                discord_rows += (
                    f'<tr style="background:rgba(200,112,0,0.05);"><td colspan="4" style="font-size:0.88em;color:var(--muted);">'
                    f'Server <strong>{_escape(gname)}</strong> komplett verlassen (Bot wird aus allen Channels entfernt)</td>'
                    f'<td><button class="btn-leave" data-kind="discord_guild" data-id="{_escape(gid)}" data-name="{_escape(gname)}" title="Bot aus diesem Server entfernen">Server verlassen</button></td></tr>'
                )

    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>Räume – MiniAssistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="/favicon.ico">
    <style>
    {_COMMON_CSS}
    .sched-wrap {{ max-width: 1100px; margin: 0 auto; padding: 1.2em 1em; }}
    .sched-header {{ display: flex; align-items: center; gap: 0.6em; margin-bottom: 1em; }}
    .sched-header img {{ width: 40px; height: 40px; border-radius: 8px; }}
    .sched-header h1 {{ margin: 0; font-size: 1.4em; }}
    h2.sect {{ margin: 1.4em 0 0.5em; font-size: 1.1em; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92em; }}
    th {{ text-align: left; padding: 0.5em; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }}
    td {{ padding: 0.5em; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; word-break: break-all; }}
    [data-theme="dark"] code {{ background: #2a2a4a; }}
    .mode-sel {{ padding: 0.25em 0.5em; font-size: 0.9em; }}
    .modes-help {{ background: var(--card); border-radius: 6px; padding: 0.7em 0.9em; margin: 0 0 1em; font-size: 0.88em; line-height: 1.5; }}
    .modes-help b {{ color: var(--text); }}
    .btn-leave {{ background: none; border: 1.5px solid var(--danger); color: var(--danger); border-radius: 4px;
      cursor: pointer; padding: 0.2em 0.6em; font-size: 0.85em; line-height: 1.2; transition: background 0.15s; }}
    .btn-leave:hover {{ background: var(--danger); color: #fff; }}
    .btn-adv {{ background: none; border: 1px solid var(--border); color: var(--text); border-radius: 4px;
      cursor: pointer; padding: 0.2em 0.5em; font-size: 0.95em; line-height: 1.2; }}
    .btn-adv:hover {{ background: var(--border); }}
    .grp-settings-row select, .grp-settings-row input[type=text] {{ width: 100%; max-width: 360px; }}
    .modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000; align-items:center; justify-content:center; }}
    .modal-overlay.open {{ display:flex; }}
    .modal-box {{ background:var(--card); border-radius:8px; padding:1.4em; max-width:480px; width:90%; box-shadow:0 4px 24px rgba(0,0,0,0.18); }}
    .modal-box h2 {{ margin:0 0 0.6em; font-size:1.1em; }}
    .modal-actions {{ display:flex; gap:0.6em; justify-content:flex-end; margin-top:0.8em; }}
    #toastWrap {{ position:fixed;bottom:1.2em;right:1.2em;display:flex;flex-direction:column;gap:0.5em;z-index:2000;pointer-events:none; }}
    </style>
    </head><body>
    <div class="sched-wrap">
      <div class="sched-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Räume &amp; Channels</h1>
      </div>
      <div class="modes-help">
        Pro Raum/Channel festlegen, wie der Assistent reagiert:
        <ul style="margin:0.4em 0 0 1.2em;padding:0;">
          <li><b>Immer antworten</b> — auf jede Nachricht antworten (typisch für DMs / 1:1-Räume).</li>
          <li><b>Nur auf Erwähnung</b> — Bot antwortet nur wenn du ihn @-erwähnst (typisch für Gruppen).</li>
          <li><b>Aus</b> — kein automatisches Antworten. Schedules &amp; Webhooks können trotzdem in den Raum posten.</li>
        </ul>
        Änderungen werden gesammelt und mit dem <b>Speichern</b>-Button unten persistiert. <em>(default)</em> = kein expliziter Mode gesetzt, automatische Heuristik (DM=always, Gruppe=mention).
      </div>
      <div class="modes-help">
        <b>Auth &amp; Raum-Trust:</b>
        <ul style="margin:0.4em 0 0 1.2em;padding:0;">
          <li><b>✅ Raum vertraut</b> — Bot wurde von einem authentifizierten User in den Raum eingeladen → <em>alle</em> Mitglieder dürfen den Bot benutzen (gemäss Antwort-Mode).</li>
          <li><b>✅ Auth</b> (DM) — der andere User hat sich selbst per <code>/auth</code> freigeschaltet.</li>
          <li><b>⚠️ kein Auth</b> — User noch nicht freigeschaltet UND Bot nicht von authed User eingeladen. Bot antwortet nur mit Auth-Code-Hinweis.</li>
        </ul>
        Für Bestandsräume ohne erkannten Inviter: einfach <em>Verlassen</em> drücken und vom Matrix-Client neu einladen — dann wird der Inviter korrekt erfasst.
      </div>

      <h2 class="sect">Matrix</h2>
      <div class="card">
        <table>
          <thead><tr><th>Raumname</th><th>Room ID</th><th>Mitglieder</th><th>Auth</th><th>Antwort-Mode</th><th></th></tr></thead>
          <tbody>{matrix_rows}</tbody>
        </table>
      </div>

      <h2 class="sect">Discord</h2>
      <div class="card">
        <table>
          <thead><tr><th>Channel</th><th>Channel ID</th><th>Server</th><th>Antwort-Mode</th><th></th></tr></thead>
          <tbody>{discord_rows}</tbody>
        </table>
        <div style="margin-top:0.6em;padding:0.5em 0.7em;background:var(--bg);border-radius:4px;font-size:0.82em;color:var(--muted);">
          ℹ️ Discord-Bots können einzelne Channels nicht verlassen — nur ganze Server. Wenn du den Bot nur aus einem Channel haben willst, entzieh seine Permissions im Channel oder setze Antwort-Mode auf <em>Aus</em>.
        </div>
      </div>

      <div style="margin-top:1.2em;display:flex;gap:0.6em;align-items:center;flex-wrap:wrap;">
        <a href="/{tq}" class="btn btn-outline">Startseite</a>
        <button id="saveBtn" class="btn btn-primary" disabled>Speichern</button>
        <button id="discardBtn" class="btn btn-outline" disabled>Verwerfen</button>
        <span id="dirtyHint" style="color:var(--muted);font-size:0.88em;"></span>
      </div>
    </div>
    <div class="modal-overlay" id="confirmModal">
      <div class="modal-box">
        <h2 id="confirmTitle"></h2>
        <div id="confirmBody" style="font-size:0.92em;line-height:1.5;white-space:pre-wrap;"></div>
        <div class="modal-actions">
          <button class="btn btn-outline" id="confirmCancel">Abbrechen</button>
          <button class="btn btn-primary" id="confirmOk">OK</button>
        </div>
      </div>
    </div>
    <div id="toastWrap"></div>
    <script>
    var token = new URLSearchParams(window.location.search).get("token") || "";
    function tq() {{ return token ? "?token=" + encodeURIComponent(token) : ""; }}
    var _cR = null;
    function appConfirm(title, body, opts) {{
      opts = opts || {{}};
      document.getElementById("confirmTitle").textContent = title || "";
      document.getElementById("confirmBody").textContent = body || "";
      document.getElementById("confirmOk").textContent = opts.okLabel || "OK";
      document.getElementById("confirmCancel").textContent = opts.cancelLabel || "Abbrechen";
      document.getElementById("confirmModal").classList.add("open");
      return new Promise(function(resolve){{ _cR = resolve; }});
    }}
    function _cD(v) {{ document.getElementById("confirmModal").classList.remove("open"); var r = _cR; _cR = null; if (r) r(v); }}
    document.getElementById("confirmOk").addEventListener("click", function(){{ _cD(true); }});
    document.getElementById("confirmCancel").addEventListener("click", function(){{ _cD(false); }});
    document.getElementById("confirmModal").addEventListener("click", function(e){{ if (e.target === this) _cD(false); }});
    function appToast(msg, kind) {{
      kind = kind || "info";
      var wrap = document.getElementById("toastWrap");
      var t = document.createElement("div");
      var bg = kind === "error" ? "#c53030" : (kind === "success" ? "#2d8a4e" : "#2a2a4a");
      t.style.cssText = "pointer-events:auto;background:" + bg + ";color:#fff;padding:0.5em 0.8em;border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,0.25);font-size:0.88em;max-width:360px;cursor:pointer;opacity:0;transition:opacity 0.2s;";
      t.textContent = msg;
      t.addEventListener("click", function(){{ t.remove(); }});
      wrap.appendChild(t);
      setTimeout(function(){{ t.style.opacity = "1"; }}, 10);
      setTimeout(function(){{ t.style.opacity = "0"; setTimeout(function(){{ if (t.parentNode) t.remove(); }}, 300); }}, kind === "error" ? 5000 : 2500);
    }}
    // Dirty-State: zwei Buckets (modes + settings). Speichern-Button flusht beide.
    var _pending = {{matrix: {{}}, discord: {{}}}};
    var _settingsPending = {{matrix: {{}}, discord: {{}}}};  // referenced in _flushSettings/_queueSettingsFromRow below
    function _dirtyCount() {{
      return Object.keys(_pending.matrix).length + Object.keys(_pending.discord).length
           + Object.keys(_settingsPending.matrix).length + Object.keys(_settingsPending.discord).length;
    }}
    function _updateDirtyUi() {{
      var n = _dirtyCount();
      var btn = document.getElementById("saveBtn");
      var disc = document.getElementById("discardBtn");
      var hint = document.getElementById("dirtyHint");
      if (btn) btn.disabled = (n === 0);
      if (disc) disc.disabled = (n === 0);
      if (hint) hint.textContent = n === 0 ? "Keine ungespeicherten Änderungen." : ("Ungespeicherte Änderungen: " + n);
    }}
    function _flushModes() {{
      var body = {{}};
      if (Object.keys(_pending.matrix).length) body.matrix = _pending.matrix;
      if (Object.keys(_pending.discord).length) body.discord = _pending.discord;
      if (!body.matrix && !body.discord) return Promise.resolve({{ok: true, skipped: true}});
      return fetch("/api/rooms" + tq(), {{
        method: "PATCH",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body),
      }}).then(function(r){{
        if (r.ok) {{ _pending = {{matrix: {{}}, discord: {{}}}}; return {{ok: true}}; }}
        return r.json().then(function(d){{ throw new Error(d.detail || "Fehler beim Speichern (modes)"); }});
      }});
    }}
    function _flushAll() {{
      var btn = document.getElementById("saveBtn");
      if (btn) btn.disabled = true;
      Promise.all([_flushModes(), _flushSettings()]).then(function(){{
        _updateDirtyUi();
        appToast("Gespeichert", "success");
      }}).catch(function(e){{
        appToast(e.message || String(e), "error");
        _updateDirtyUi();
      }});
    }}
    function _discardAll() {{
      if (_dirtyCount() === 0) return;
      appConfirm("Änderungen verwerfen?", "Alle ungespeicherten Änderungen werden zurückgesetzt (Seite wird neu geladen).", {{okLabel: "Verwerfen"}}).then(function(ok){{
        if (ok) location.reload();
      }});
    }}
    document.querySelectorAll(".mode-sel").forEach(function(sel) {{
      sel.addEventListener("change", function() {{
        var kind = this.getAttribute("data-kind");
        var id = this.getAttribute("data-id");
        _pending[kind][id] = this.value;
        var hint = this.parentNode.querySelector('span[title*="default"]');
        if (hint) hint.remove();
        _updateDirtyUi();
      }});
    }});
    window.addEventListener("beforeunload", function(e){{
      if (_dirtyCount() > 0) {{ e.preventDefault(); e.returnValue = ""; return ""; }}
    }});
    document.getElementById("saveBtn").addEventListener("click", _flushAll);
    document.getElementById("discardBtn").addEventListener("click", _discardAll);
    _updateDirtyUi();
    // Ctrl/Cmd+S → Speichern (Browser-Speichern unterdrücken)
    window.addEventListener("keydown", function(e){{
      if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {{
        e.preventDefault();
        if (_dirtyCount() > 0) _flushAll();
      }}
    }});

    // Group-Mode Settings: Toggle + Save
    document.querySelectorAll(".btn-adv").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var kind = this.getAttribute("data-target-kind");
        var id = this.getAttribute("data-target-id");
        var rows = document.querySelectorAll('tr.grp-settings-row[data-kind="' + kind + '"][data-id="' + CSS.escape(id) + '"]');
        rows.forEach(function(r){{ r.style.display = (r.style.display === "none" ? "table-row" : "none"); }});
      }});
    }});

    function _readSettingsFromRow(row) {{
      var ctx = row.querySelector(".grp-ctx").value;
      var lang = row.querySelector(".grp-lang").value;
      var sub = (row.querySelector(".grp-sub").value || "").trim();
      var tools = [];
      row.querySelectorAll(".grp-tool:checked").forEach(function(cb){{ tools.push(cb.getAttribute("data-tool")); }});
      var ac_count = parseInt((row.querySelector(".grp-ac-count") || {{value: "3"}}).value, 10);
      var ac_max = parseInt((row.querySelector(".grp-ac-max") || {{value: "200"}}).value, 10);
      var sch_max = parseInt((row.querySelector(".grp-sch-max") || {{value: "200"}}).value, 10);
      var docs_mount = !!(row.querySelector(".grp-docs") || {{checked: false}}).checked;
      var out = {{context: ctx, language: lang, tools_allow: tools,
                  auto_context_count: isNaN(ac_count) ? 3 : ac_count,
                  auto_context_max_chars: isNaN(ac_max) ? 200 : ac_max,
                  search_chat_history_max: isNaN(sch_max) ? 200 : sch_max,
                  docs_in_sandbox: docs_mount}};
      if (sub) out.workspace_subdir = sub;
      return out;
    }}
    function _flushSettings() {{
      var body = {{}};
      if (Object.keys(_settingsPending.matrix).length) body.matrix = _settingsPending.matrix;
      if (Object.keys(_settingsPending.discord).length) body.discord = _settingsPending.discord;
      if (!body.matrix && !body.discord) return Promise.resolve({{ok: true, skipped: true}});
      return fetch("/api/rooms/settings" + tq(), {{
        method: "PATCH",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body),
      }}).then(function(r){{
        if (r.ok) {{ _settingsPending = {{matrix: {{}}, discord: {{}}}}; return {{ok: true}}; }}
        return r.json().then(function(d){{ throw new Error(d.detail || "Fehler beim Speichern (settings)"); }});
      }});
    }}
    function _queueSettingsFromRow(row) {{
      var kind = row.getAttribute("data-kind");
      var id = row.getAttribute("data-id");
      _settingsPending[kind][id] = _readSettingsFromRow(row);
      _updateDirtyUi();
    }}
    document.querySelectorAll("tr.grp-settings-row").forEach(function(row) {{
      row.querySelectorAll(".grp-ctx, .grp-lang, .grp-sub, .grp-tool, .grp-ac-count, .grp-ac-max, .grp-sch-max, .grp-docs").forEach(function(el){{
        var evt = (el.tagName === "INPUT" && (el.type === "text" || el.type === "number")) ? "input" : "change";
        el.addEventListener(evt, function(){{ _queueSettingsFromRow(row); }});
      }});
    }});
    document.querySelectorAll(".btn-leave").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var kind = this.getAttribute("data-kind");
        var id = this.getAttribute("data-id");
        var name = this.getAttribute("data-name") || id;
        console.log("btn-leave click", kind, id, name);
        var title = kind === "matrix" ? "Matrix-Raum verlassen?" : "Discord-Server verlassen?";
        var body = kind === "matrix"
          ? "Der Bot verlässt den Raum '" + name + "'. Nachrichten werden danach nicht mehr empfangen — Schedules/Webhooks für diesen Raum funktionieren auch nicht mehr (kein Mitglied → keine Sendeerlaubnis)."
          : "Der Bot verlässt den ganzen Discord-Server '" + name + "'. ALLE Channels dieses Servers werden für den Bot unerreichbar. Re-Invite nur über Server-Owner mit OAuth-Link.";
        appConfirm(title, body, {{okLabel: "Verlassen"}}).then(function(ok){{
          console.log("appConfirm result:", ok);
          if (!ok) return;
          var url = "/api/rooms/leave" + tq();
          console.log("POST", url, {{kind: kind, id: id}});
          fetch(url, {{
            method: "POST",
            credentials: "same-origin",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{kind: kind, id: id}})
          }}).then(function(r){{
            console.log("response", r.status);
            return r.json().then(function(d){{
              console.log("data", d);
              if (r.ok && d.ok) {{
                appToast("Verlassen — Liste wird neu geladen", "success");
                setTimeout(function(){{ location.reload(); }}, 1200);
              }} else {{
                appToast(d.message || d.detail || "Fehler", "error");
              }}
            }});
          }}).catch(function(e){{ console.error("fetch failed", e); appToast("Fehler: " + e.message, "error"); }});
        }});
      }});
    }});
    </script>
    {_THEME_JS}
    </body></html>
    """
    return HTMLResponse(html)
