"""
Minimalistisches Web-UI und API für MiniAssistant.
- Config anzeigen/bearbeiten (Ollama-URL, Modelle, num_ctx, Bind, Token)
- Chat (mit /model MODELLNAME), exec und web_search als Tools
- Token-Auth: Header Authorization: Bearer <token> oder ?token=...
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

_log = logging.getLogger("uvicorn.error")

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from fastapi.staticfiles import StaticFiles

from miniassistant.config import load_config, save_config, ensure_token, config_path, load_config_raw, write_config_raw, validate_config_raw
from miniassistant.chat_loop import create_session, handle_user_input, run_onboarding_round, chat_round_stream, is_chat_command
from miniassistant.ollama_client import resolve_model

# Projekt-Root für Templates (ein Verzeichnis über miniassistant/)
ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# In-Memory-Sessions (session_id -> session dict)
_sessions: dict[str, dict] = {}
_onboarding_sessions: dict[str, dict] = {}  # session_id -> { "messages": [...] }

app = FastAPI(title="MiniAssistant", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_matrix_bot_task: asyncio.Task | None = None
_discord_bot_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    """Bei server.debug: Startup in debug/serve.log schreiben. Chat-Bots starten falls konfiguriert."""
    global _matrix_bot_task, _discord_bot_task
    try:
        project_dir = getattr(app.state, "project_dir", None)
        config = load_config(project_dir)
        if (config.get("server") or {}).get("debug"):
            from miniassistant.debug_log import log_serve
            log_serve("Application startup", config)
        cc = config.get("chat_clients") or {}
        # Matrix-Bot
        mc = cc.get("matrix") or config.get("matrix")
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


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Bot-Tasks abbrechen und auf Beendigung warten."""
    global _matrix_bot_task, _discord_bot_task
    for task in (_matrix_bot_task, _discord_bot_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _matrix_bot_task = None
    _discord_bot_task = None


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
    config = load_config()
    expected = (config.get("server") or {}).get("token")
    if not expected:
        return ""  # no token configured yet -> allow (e.g. first run)
    token = _get_token_from_request(request)
    if token and token == expected:
        return token
    raise HTTPException(status_code=401, detail="Invalid or missing token")


_TOKEN_COOKIE_PAGES = {"/", "/chat", "/config", "/schedules", "/onboarding", "/logs"}


@app.middleware("http")
async def _token_cookie_middleware(request: Request, call_next):
    """Wenn Token als URL-Param kommt und Seite ein HTML-Page ist: Cookie setzen, redirect ohne Token in URL."""
    if request.method == "GET" and request.url.path in _TOKEN_COOKIE_PAGES:
        url_token = request.query_params.get("token", "").strip()
        cookie_token = request.cookies.get("ma_token", "").strip()
        if url_token and url_token != cookie_token:
            # Token in Cookie speichern und redirect ohne ?token= in der URL
            from starlette.responses import RedirectResponse as _Redirect
            clean_url = str(request.url).split("?")[0]
            # Andere Query-Params beibehalten (falls vorhanden)
            from urllib.parse import urlencode, parse_qs
            params = dict(request.query_params)
            params.pop("token", None)
            if params:
                clean_url += "?" + urlencode(params)
            resp = _Redirect(url=clean_url, status_code=302)
            resp.set_cookie("ma_token", url_token, httponly=False, samesite="strict", max_age=365 * 24 * 3600)
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
        config_links = '<li><a href="/config' + tq + '">Konfiguration</a></li><li><a href="/schedules' + tq + '">Geplante Jobs</a></li><li><a href="/logs' + tq + '">Logs</a></li><li><a href="/api/config' + tq + '">API: Config (JSON)</a></li>'
    logout_btn = '<button type="button" class="btn btn-outline" id="logout-btn" style="margin-left:0.5em;">Logout</button>' if is_authed else ""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>MiniAssistant</title>
    <link rel="icon" type="image/png" href="/static/miniassistant.png">
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


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Config-Seite: YAML in Textarea anzeigen, editierbar. Nur zugänglich mit gültigem Token (oder wenn noch keiner gesetzt)."""
    _require_token(request)  # 401 wenn Token in Config gesetzt, aber fehlt oder falsch
    import base64
    token = request.query_params.get("token", "")
    tq = _token_query(token)
    raw = load_config_raw()
    # Sensitive Werte maskieren (nur Anzeige, beim Speichern bleibt Original wenn nicht geändert)
    import re
    def _mask_secrets(text: str) -> str:
        pattern = r'((?:api_key|token|bot_token):\s*)(\S+)'
        return re.sub(pattern, lambda m: m.group(1) + m.group(2)[:4] + '****' if len(m.group(2)) > 4 else m.group(0), text)
    display_raw = _mask_secrets(raw)
    raw_b64 = base64.b64encode(display_raw.encode("utf-8")).decode("ascii")
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Konfiguration – MiniAssistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" type="image/png" href="/static/miniassistant.png">
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
    .form-actions {{ display: flex; align-items: center; gap: 0.8em; margin-top: 0.8em; flex-wrap: wrap; }}
    #msg {{ font-size: 0.9em; font-weight: 500; }}
    #msg.success {{ color: var(--success); }}
    #msg.error {{ color: var(--danger); }}
    .nav-bottom {{ margin-top: 1.2em; padding-top: 0.8em; border-top: 1px solid var(--border); }}
    </style>
    </head>
    <body>
    <div class="config-wrap">
      <div class="config-header">
        <img src="/static/miniassistant.png" alt="Logo">
        <h1>Konfiguration</h1>
      </div>
      <p class="config-desc">YAML bearbeiten und speichern. Die Datei wird auf gueltiges YAML und erwartete Struktur geprueft.</p>
      <form id="f">
        <textarea id="config-yaml" name="yaml" spellcheck="false"></textarea>
        <div class="form-actions">
          <button type="submit" class="btn btn-primary">Speichern</button>
          <span id="msg"></span>
        </div>
      </form>
      <div class="nav-bottom">
        <a href="/{tq}" class="btn btn-outline">Startseite</a>
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
    document.getElementById("f").addEventListener("submit", function(e) {{
      e.preventDefault();
      var msg = document.getElementById("msg");
      msg.textContent = "";
      msg.className = "";
      var yaml = document.getElementById("config-yaml").value;
      var token = new URLSearchParams(window.location.search).get("token") || "";
      fetch("/api/config" + (token ? "?token=" + encodeURIComponent(token) : ""), {{
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
    </script>
    {_THEME_JS}
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    """Zeigt geplante Jobs an."""
    _require_token(request)
    token = request.query_params.get("token", "")
    tq = _token_query(token)
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
            if j.get("prompt"):
                task_parts.append(_escape(j["prompt"][:80]))
            if j.get("command"):
                task_parts.append(f'<code>{_escape(j["command"][:60])}</code>')
            task = "<br>".join(task_parts) if task_parts else "?"
            client = _escape(j.get("client") or "alle")
            model = _escape(j.get("model") or "default")
            once_tag = ' <span style="color:var(--muted);font-size:0.8em;">einmalig</span>' if j.get("once") else ""
            full_id = _escape(j.get("id", ""))
            rows += (
                f'<tr><td><code>{_escape(when)}</code>{once_tag}</td><td>{task}</td><td>{client}</td><td>{model}</td><td>{added}</td>'
                f'<td><code>{jid}</code> <button class="btn-del" data-id="{full_id}" title="Loeschen">&#10005;</button></td></tr>'
            )
    html = f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"><title>Schedules – MiniAssistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" type="image/png" href="/static/miniassistant.png">
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
    <script>
    document.querySelectorAll(".btn-del").forEach(function(btn) {{
      btn.addEventListener("click", function() {{
        var id = this.getAttribute("data-id");
        if (!confirm("Job " + id.slice(0,8) + " loeschen?")) return;
        var token = new URLSearchParams(window.location.search).get("token") || "";
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
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _js_escape(s: str) -> str:
    """Für sichere Nutzung in JavaScript-Strings (z. B. in HTML)."""
    import json
    return json.dumps(s)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Chat-Seite: Markdown, Thinking optional in Spoiler, aufgehuebschtes Design."""
    from fastapi.responses import RedirectResponse
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
    <link rel="icon" type="image/png" href="/static/miniassistant.png">
    <script src="/static/marked.umd.js"></script>
    <style>
    {_COMMON_CSS}
    .chat-wrap {{ display: flex; flex-direction: column; height: 100vh; max-width: min(900px, 98vw); margin: 0 auto; padding: 0.8em 1em; }}
    .chat-header {{ display: flex; align-items: center; gap: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); margin-bottom: 0.5em; flex-shrink: 0; }}
    .chat-header img {{ width: 32px; height: 32px; border-radius: 6px; }}
    .chat-header h1 {{ margin: 0; font-size: 1.2em; }}
    .chat-header .cmds {{ font-size: 0.75em; color: var(--muted); margin-left: auto; max-width: 50%; text-align: right; }}
    #log {{ flex: 1; overflow-y: auto; padding: 0.3em 0; }}
    .msg {{ padding: 0.7em 0; }}
    .msg + .msg {{ border-top: 1px solid #eee; }}
    .msg-role {{ font-weight: 600; font-size: 0.85em; color: var(--muted); margin-bottom: 0.25em; }}
    .msg-role.user {{ color: var(--primary); }}
    .msg-role.assistant {{ color: var(--success); }}
    .msg .content {{ line-height: 1.55; }}
    .msg .content.markdown p {{ margin: 0.3em 0; }}
    .msg .content.markdown pre {{ background: #f0f2f5; padding: 0.6em; border-radius: 6px; overflow-x: auto; font-size: 0.9em; }}
    .msg .content.markdown code {{ background: #f0f2f5; padding: 0.15em 0.35em; border-radius: 4px; font-size: 0.9em; }}
    .msg .content.markdown pre code {{ background: none; padding: 0; }}
    details.thinking {{ margin-top: 0.3em; font-size: 0.88em; color: var(--muted); }}
    details.thinking summary {{ cursor: pointer; font-weight: 500; }}
    .thinking-placeholder {{ color: var(--muted); font-style: italic; }}
    .onboarding-notice {{ background: #fff9e6; padding: 0.6em 0.8em; border-radius: var(--radius); border-left: 4px solid var(--warning); font-size: 0.9em; margin-bottom: 0.5em; }}
    .chat-input {{ display: flex; gap: 0.5em; align-items: flex-end; padding-top: 0.6em; border-top: 1px solid var(--border); flex-shrink: 0; }}
    .chat-input textarea {{ flex: 1; padding: 0.55em 0.7em; border: 1.5px solid var(--border); border-radius: var(--radius);
                            font-family: inherit; font-size: 0.95em; resize: none; outline: none; min-height: 2.5em; max-height: 8em; transition: border-color 0.15s; }}
    .chat-input textarea:focus {{ border-color: var(--primary); }}
    .chat-input button {{ height: 2.5em; }}
    .chat-footer {{ font-size: 0.8em; color: var(--muted); padding-top: 0.3em; text-align: center; flex-shrink: 0; }}
    .thinking-live {{ white-space: pre-wrap; font-size: 0.88em; color: var(--muted); margin: 0.3em 0; }}
    .typing-dots {{ display: inline-flex; align-items: center; gap: 0.2em; margin: 0.3em 0; color: var(--muted); font-size: 1.2em; }}
    .typing-dots span {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: typing-bounce 0.6s ease-in-out infinite both; }}
    .typing-dots span:nth-child(2) {{ animation-delay: 0.15s; }}
    .typing-dots span:nth-child(3) {{ animation-delay: 0.3s; }}
    @keyframes typing-bounce {{ 0%, 80%, 100% {{ transform: scale(0.6); opacity: 0.5; }} 40% {{ transform: scale(1); opacity: 1; }} }}
    .processing-indicator {{ display: inline-flex; align-items: center; gap: 0.4em; margin: 0.3em 0; color: var(--muted); font-size: 0.88em; animation: processing-pulse 1.5s ease-in-out infinite; }}
    @keyframes processing-pulse {{ 0%, 100% {{ opacity: 0.5; }} 50% {{ opacity: 1; }} }}
    </style>
    </head>
    <body>
    <div class="chat-wrap">
    <div class="chat-header">
      <img src="/static/miniassistant.png" alt="MiniAssistant">
      <h1>Chat</h1>
      <span class="cmds">/model, /models, /new, /schedules, /schedule remove &lt;ID&gt;, /auth</span>
    </div>
    {"<div class=\"onboarding-notice\">Setup noch nicht abgeschlossen. <a href=\"/onboarding" + token_q + "\">Onboarding / Setup</a></div>" if show_onboarding else ""}
    <div id="log"></div>
    <form id="f" class="chat-input">
      <textarea id="msg" placeholder="Nachricht… (Enter = Senden, Shift+Enter = Zeile)" rows="2" autocomplete="off"></textarea>
      <button type="submit" class="btn btn-primary" id="btn-send">Senden</button>
      <button type="button" class="btn btn-outline" id="btn-cancel" style="display:none;">Abbrechen</button>
    </form>
    <div class="chat-footer"><span style="opacity:0.7;">Konversationen werden nicht gespeichert (Seite neu laden = neuer Chat).</span> {onboarding_link}<a href="/{token_q}" class="btn btn-outline" style="padding:0.3em 0.7em;font-size:0.85em;">Startseite</a></div>
    </div>
    <script>
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
    if (params.get("onboarding_saved") === "1") {{
      sessionStorage.removeItem("miniassistant_session");
      sessionId = "";
      const notice = document.createElement("div");
      notice.className = "onboarding-notice";
      notice.textContent = "Setup gespeichert. Neue Session gestartet.";
      log.appendChild(notice);
      if (history.replaceState) history.replaceState({{}}, "", "/chat" + (token ? "?token=" + encodeURIComponent(token) : ""));
    }}

    function escapeHtml(s) {{
      const div = document.createElement("div");
      div.textContent = s;
      return div.innerHTML;
    }}
    function addLog(role, text, isMarkdown) {{
      const p = document.createElement("div");
      p.className = "msg";
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role " + (role === "Du" ? "user" : "");
      roleDiv.textContent = role;
      p.appendChild(roleDiv);
      const content = document.createElement("div");
      content.className = "content" + (isMarkdown ? " markdown" : "");
      if (isMarkdown && typeof marked !== "undefined")
        content.innerHTML = marked.parse(text || "");
      else
        content.textContent = text || "";
      p.appendChild(content);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}
    function addAssistantLog(fullText, userRequest, thinkingText, contentText) {{
      const p = document.createElement("div");
      p.className = "msg";
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
        md.innerHTML = typeof marked !== "undefined" ? marked.parse(answer) : escapeHtml(answer);
        wrap.appendChild(md);
      }}
      p.appendChild(wrap);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}

    function showStreamContainer(userRequest) {{
      const p = document.createElement("div");
      p.className = "msg";
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
      var thinkingText = thinkingEl ? thinkingEl.textContent.trim() : "";
      var contentText  = contentEl  ? contentEl.textContent.trim()  : "";
      if (!contentText && doneData && doneData.content) contentText = doneData.content.trim();
      if (thinkingEl) thinkingEl.remove();
      if (contentEl)  contentEl.remove();
      if (thinkingText && contentText) {{
        const details = document.createElement("details");
        details.className = "thinking";
        details.innerHTML = "<summary>Denkvorgang</summary><div style='white-space:pre-wrap;margin-top:0.3em;'>" + escapeHtml(thinkingText) + "</div>";
        wrap.appendChild(details);
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? marked.parse(contentText) : escapeHtml(contentText);
        wrap.appendChild(md);
      }} else if (contentText) {{
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? marked.parse(contentText) : escapeHtml(contentText);
        wrap.appendChild(md);
      }} else if (thinkingText) {{
        const note = document.createElement("p");
        note.style.cssText = "color:var(--muted);font-size:0.85em;font-style:italic;margin:0.2em 0 0.4em;";
        note.textContent = "(Kein separater Antworttext)";
        wrap.appendChild(note);
        const md = document.createElement("div");
        md.className = "markdown";
        md.innerHTML = typeof marked !== "undefined" ? marked.parse(thinkingText) : escapeHtml(thinkingText);
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
      container.id = "";
      log.scrollTop = log.scrollHeight;
      form.querySelector("button[type=submit]").disabled = false;
    }}
    form.addEventListener("submit", async (e) => {{
      e.preventDefault();
      const content = msgEl.value.trim();
      if (!content) return;
      msgEl.value = "";
      msgEl.style.height = "auto";
      addLog("Du", content, false);
      showStreamContainer(content);
      btnSend.disabled = true; btnCancel.style.display = "";
      currentAbort = new AbortController();
      const url = "/api/chat/stream" + (token ? "?token=" + encodeURIComponent(token) : "");
      const body = {{ message: content }};
      if (sessionId) body.session_id = sessionId;
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
                pi.textContent = "⚙ " + toolNames + " wird ausgefuehrt …";
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
                pi.textContent = "⚙ " + msg;
                const thinkEl = document.getElementById("stream-thinking");
                if (thinkEl) thinkEl.textContent += "\\n(" + msg + ")\\n";
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
        raw = list_models(base_url)
        names = [m.get("name") or m.get("model") or "" for m in (raw or []) if (m.get("name") or m.get("model"))]
        return JSONResponse({"models": names})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama-Modelle konnten nicht geladen werden: {e}")


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
        except Exception:
            pass
    ok, err = validate_config_raw(content)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    try:
        write_config_raw(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"ok": True})


@app.get("/api/token")
async def api_show_token(request: Request):
    """Token anzeigen (nur wenn bereits gesetzt; sonst 204)."""
    cfg = load_config()
    t = (cfg.get("server") or {}).get("token")
    if not t:
        from fastapi.responses import Response
        return Response(status_code=204)
    return JSONResponse({"token": ensure_token(cfg)})


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
        result = consume_code(code, config_dir)
        if result:
            plat, uid = result
            return JSONResponse({"ok": True, "platform": plat, "user_id": uid})
        return JSONResponse({"ok": False, "detail": "Code nicht gefunden (bereits eingelöst oder abgelaufen?). Im Matrix-/Discord-Chat einen neuen Code anfordern."}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)


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
    results = send_notification(message, client=client)
    return JSONResponse(results)


def _chat_stream_generator(session_id: str, session: dict, message: str):
    """Sync generator: NDJSON-Zeilen mit type thinking | content | tool_call | done; fügt session_id hinzu."""
    import json as _json
    for ev in chat_round_stream(
        config=session["config"],
        messages=session["messages"],
        system_prompt=session["system_prompt"],
        model=session.get("model") or resolve_model(session["config"], None) or "",
        user_content=message,
        project_dir=session.get("project_dir"),
    ):
        out = dict(ev, session_id=session_id)
        if ev.get("type") == "done":
            _done_msgs = ev.get("new_messages", session["messages"])
            # Bilder aus Messages entfernen (base64-Daten verschwenden Kontext-Platz)
            for _msg in _done_msgs:
                if _msg.get("images"):
                    del _msg["images"]
                    if _msg.get("role") == "user" and "[Bild]" not in (_msg.get("content") or ""):
                        _msg["content"] = "[Bild angehängt] " + (_msg.get("content") or "")
            session["messages"] = _done_msgs
            _sessions[session_id] = session
            # Memory: Exchange speichern (wie handle_user_input für Matrix/Discord)
            done_content = (ev.get("content") or "").strip()
            if done_content and message.strip():
                try:
                    from miniassistant.memory import append_exchange
                    append_exchange(message, done_content, project_dir=session.get("project_dir"))
                except Exception:
                    pass
        yield _json.dumps(out, ensure_ascii=False) + "\n"


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """Chat-Stream: POST { message, optional session_id } -> NDJSON (thinking/content live, am Ende done)."""
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
    if is_chat_command(message):
        is_new = message.strip().lower() == "/new"
        is_model_switch = message.strip().lower().startswith("/model ")
        result = handle_user_input(session, message)
        session = result[1]
        _sessions[session_id] = session
        thinking = result[3] if len(result) > 3 else None
        # content (result[4]) ist bei Befehlen None – dann auf result[0] (Antworttext) zurückfallen
        content = (result[4] if len(result) > 4 else None) or result[0]
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
    return StreamingResponse(
        _chat_stream_generator(session_id, session, message),
        media_type="application/x-ndjson",
    )


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
    result = handle_user_input(session, message)
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
    token = request.query_params.get("token", "")
    token_q = f"?token={token}" if token else ""
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Onboarding – MiniAssistant</title>
    <link rel="icon" type="image/png" href="/static/miniassistant.png">
    <script src="/static/marked.umd.js"></script>
    <style>
    {_COMMON_CSS}
    .chat-wrap {{ display: flex; flex-direction: column; height: 100vh; max-width: min(900px, 98vw); margin: 0 auto; padding: 0.8em 1em; }}
    .chat-header {{ display: flex; align-items: center; gap: 0.6em; padding-bottom: 0.5em; border-bottom: 1px solid var(--border); margin-bottom: 0.5em; flex-shrink: 0; }}
    .chat-header img {{ width: 32px; height: 32px; border-radius: 6px; }}
    .chat-header h1 {{ margin: 0; font-size: 1.2em; }}
    .chat-header .subtitle {{ font-size: 0.8em; color: var(--muted); margin-left: auto; }}
    #log {{ flex: 1; overflow-y: auto; padding: 0.3em 0; }}
    .msg {{ padding: 0.7em 0; }}
    .msg + .msg {{ border-top: 1px solid #eee; }}
    .msg-role {{ font-weight: 600; font-size: 0.85em; color: var(--muted); margin-bottom: 0.25em; }}
    .msg-role.user {{ color: var(--primary); }}
    .msg-role.assistant {{ color: var(--success); }}
    .msg .content {{ line-height: 1.55; }}
    .msg .content.markdown p {{ margin: 0.3em 0; }}
    .msg .content.markdown pre {{ background: #f0f2f5; padding: 0.6em; border-radius: 6px; overflow-x: auto; font-size: 0.9em; }}
    .msg .content.markdown code {{ background: #f0f2f5; padding: 0.15em 0.35em; border-radius: 4px; font-size: 0.9em; }}
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
      const p = document.createElement("div");
      p.className = "msg";
      const roleDiv = document.createElement("div");
      roleDiv.className = "msg-role " + (role === "Du" ? "user" : "");
      roleDiv.textContent = role;
      p.appendChild(roleDiv);
      const content = document.createElement("div");
      content.className = "content" + (isMd ? " markdown" : "");
      if (isMd && typeof marked !== "undefined")
        content.innerHTML = marked.parse(text || "");
      else
        content.textContent = text || "";
      p.appendChild(content);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}
    function addAssistantWithThinking(contentText, thinkingText, userRequest) {{
      const p = document.createElement("div");
      p.className = "msg";
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
        md.innerHTML = typeof marked !== "undefined" ? marked.parse(contentText) : escapeHtml(contentText);
        wrap.appendChild(md);
      }}
      p.appendChild(wrap);
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }}
    function showThinking() {{
      const p = document.createElement("div");
      p.className = "msg";
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
    return JSONResponse({"logs": logs, "memory": memory_files})


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
    # Memory-Dateien (memory_YYYY-MM-DD, memory_last_summary)
    if log_id.startswith("memory_"):
        try:
            from miniassistant.memory import memory_dir as _memory_dir
            mem_dir = _memory_dir(getattr(app.state, "project_dir", None))
            suffix = log_id[7:]  # nach "memory_"
            if suffix == "last_summary":
                log_map[log_id] = mem_dir / "last_summary.json"
            else:
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
    <link rel="icon" type="image/png" href="/static/miniassistant.png">
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
      var box = document.getElementById("log-box");
      var liveToggle = document.getElementById("live-toggle");
      var scrollToggle = document.getElementById("scroll-toggle");
      var liveDot = document.getElementById("live-dot");
      var statusText = document.getElementById("status-text");
      var currentLog = "";
      var offset = 0;
      var pollTimer = null;
      var pollInterval = 2000;

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
        # systemd: restart in background subshell (so response can be sent before process dies)
        cmd = f"(sleep 1 && systemctl restart {service_name}) &"
    elif Path(f"/etc/init.d/{service_name}").exists():
        cmd = f"(sleep 1 && /etc/init.d/{service_name} restart) &"
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
        subprocess.Popen(cmd, shell=True, start_new_session=True)
        method = "systemd" if "systemctl" in cmd else "init.d"
        return JSONResponse({"ok": True, "method": method, "message": f"Restart via {method} ausgelöst."})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restart fehlgeschlagen: {e}")


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
