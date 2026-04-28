"""
Tools: exec (Shell-Befehl), web_search (SearXNG), check_url (URL-Erreichbarkeit),
read_url (URL-Inhalt als Text lesen), send_email (SMTP), read_email (IMAP).
exec führt Befehle aus; Root-Info steht im System-Prompt (KI weiß ob sudo nötig).
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
import logging
import time

import threading

import httpx

try:
    from curl_cffi import requests as _curl_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _curl_requests = None  # type: ignore
    _CURL_CFFI_AVAILABLE = False

try:
    import brotli as _brotli  # noqa: F401
    _BROTLI_AVAILABLE = True
except ImportError:
    _BROTLI_AVAILABLE = False

# Playwright: lazy check — allows installation at runtime without restart
_sync_playwright = None  # type: ignore
_PLAYWRIGHT_AVAILABLE: bool | None = None  # None = not checked yet


def _check_playwright() -> bool:
    """Check (and cache) whether playwright is importable. Re-checks on first call and after failure."""
    global _sync_playwright, _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is True and _sync_playwright is not None:
        return True
    try:
        from playwright.sync_api import sync_playwright as _sp
        _sync_playwright = _sp
        _PLAYWRIGHT_AVAILABLE = True
        return True
    except ImportError:
        _PLAYWRIGHT_AVAILABLE = False
        return False

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Proxy-Auswahl für read_url
# ---------------------------------------------------------------------------

_proxy_counter: int = 0
_proxy_counter_lock = threading.Lock()


def get_read_url_proxy_names(config: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Gibt [(name, url), ...] für konfigurierte read_url-Proxies zurück.
    Leer-URL = direkte Verbindung (kein Proxy).
    Wird vom System-Prompt genutzt damit der Agent die Namen kennt.
    """
    cfg = ((config or {}).get("read_url") or {})
    proxies = cfg.get("proxies")
    if proxies and isinstance(proxies, dict):
        return [(k, (v or "").strip()) for k, v in proxies.items()]
    return []


def _get_proxy_for_request(config: dict[str, Any] | None, proxy_name: str | None = None) -> tuple[str | None, str]:
    """Gibt (proxy_url, connection_name) für read_url zurück.

    proxy_url: Proxy-URL oder None für direkte Verbindung.
    connection_name: Menschenlesbarer Name (z.B. "vpn1", "direct").

    Config-Struktur (unter read_url:):
      proxies:                       # Benannte Proxy-Einträge (empfohlen)
        direct: ""                   # Leerstring = direkt (kein Proxy)
        vpn: "socks5://10.0.0.1:1080"
        vpn2: "http://proxy2:8080"
      default_proxy: direct          # Welcher Proxy standardmäßig genutzt wird
      proxy_strategy: first          # first (default) | random | roundrobin | none

    Strategien:
      none        – immer direkt, auch wenn proxies konfiguriert sind
      first       – default_proxy nutzen (oder ersten Eintrag wenn nicht gesetzt)
      random      – zufällig aus der Liste
      roundrobin  – reihum rotieren (thread-safe)

    proxy_name: expliziter Name-Override (aus Tool-Argument), ignoriert Strategy.
    SOCKS5-Proxies erfordern: pip install httpx[socks]  (socksio)
    """
    global _proxy_counter
    cfg = ((config or {}).get("read_url") or {})
    proxies = cfg.get("proxies")

    # Benanntes Proxy-Dict
    if proxies and isinstance(proxies, dict):
        # Expliziter Name aus Tool-Call
        if proxy_name:
            if proxy_name not in proxies:
                raise ValueError(f"Proxy '{proxy_name}' nicht in der Config (verfügbar: {', '.join(proxies.keys())})")
            url = (proxies[proxy_name] or "").strip() or None
            return url, proxy_name

        strategy = (cfg.get("proxy_strategy") or "first").strip().lower()
        if strategy == "none":
            return None, "direct"

        names = list(proxies.keys())
        default = (cfg.get("default_proxy") or "").strip()

        if strategy == "random":
            import random as _random
            chosen = _random.choice(names)
        elif strategy == "roundrobin":
            with _proxy_counter_lock:
                chosen = names[_proxy_counter % len(names)]
                _proxy_counter += 1
        else:  # first
            chosen = default if default and default in proxies else names[0]

        url = (proxies.get(chosen) or "").strip() or None
        return url, chosen

    # Fallback: einzelner Proxy-String
    if proxy_name:
        raise ValueError(f"Proxy '{proxy_name}' angefordert, aber keine Proxies in der Config konfiguriert (read_url.proxies fehlt)")
    single = (cfg.get("proxy") or "").strip()
    return (single or None), ("proxy" if single else "direct")

_RETRYABLE_STATUS_CODES = (500, 502, 503, 504)
_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 3  # seconds


def _is_ssrf_target(url: str) -> bool:
    """Check if URL targets private/internal networks or cloud metadata services (SSRF protection)."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().strip()
        if not hostname:
            return True
        # Block cloud metadata services
        _BLOCKED_HOSTS = {
            "169.254.169.254",  # AWS/GCP/Azure metadata
            "metadata.google.internal",
            "metadata.google.com",
            "100.100.100.200",  # Alibaba Cloud metadata
        }
        if hostname in _BLOCKED_HOSTS:
            return True
        # Block link-local metadata IP range
        if hostname.startswith("169.254."):
            return True
        # Resolve hostname and check if IP is private/reserved
        import socket
        try:
            addrs = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False  # Can't resolve, let httpx handle the error
        for family, _, _, _, sockaddr in addrs:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False


# Browser User-Agent: Safari 18.4.1 auf macOS Sequoia (Apple Silicon meldet
# ebenfalls "Intel" im UA — das ist Apples bewusstes Privacy-Verhalten)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.4.1 Safari/605.1.15"
)

# Fixes unmatched quotes in heredoc delimiters that LLMs commonly produce,
# e.g.  << 'EOF  (missing closing ')  →  << 'EOF'
_HEREDOC_BAD_QUOTE = re.compile(r"(<<-?\s*)(['\"])(\w+)\s*$", re.MULTILINE)


def _fix_heredoc_quotes(cmd: str) -> str:
    """Close unmatched quotes on heredoc delimiters (e.g. << 'EOF → << 'EOF')."""
    def _repl(m: re.Match) -> str:
        prefix, quote, tag = m.group(1), m.group(2), m.group(3)
        return f"{prefix}{quote}{tag}{quote}"
    return _HEREDOC_BAD_QUOTE.sub(_repl, cmd)


def run_exec(command: str, timeout: int = 60, cwd: str | None = None, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Führt einen Shell-Befehl aus. Gibt stdout, stderr und returncode zurück.
    cwd: Working directory (default: Workspace aus Config, sonst Process-CWD).
    extra_env: Zusätzliche Umgebungsvariablen (z. B. GH_TOKEN) — werden zu os.environ gemergt.
    Kein sudo-Handling hier – die KI weiß aus dem System-Prompt ob sie root ist.
    """
    import os
    try:
        command = _fix_heredoc_quotes(command)
        if cwd:
            from pathlib import Path
            Path(cwd).mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **(extra_env or {})}
        # Ensure HOME is set so ~ expands correctly (may be missing in service environments)
        if "HOME" not in env:
            import pwd
            try:
                env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
            except Exception:
                env["HOME"] = "/root"
        result = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
            env=env,
        )
        return {
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "Command timed out",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        }


def web_search(searxng_url: str, query: str, max_results: int = 5, categories: str | None = None) -> dict[str, Any]:
    """SearXNG JSON-API: ?q=...&format=json. Gibt Titel, URL, Snippet zurück.
    categories: Optional SearXNG category (e.g. 'images', 'videos', 'news')."""
    url = searxng_url.rstrip("/")
    if "/search" not in url and not url.endswith("/search"):
        url = f"{url}/search"
    params: dict[str, str] = {"q": query, "format": "json"}
    if categories:
        params["categories"] = categories
    last_err: Exception | None = None
    _ws_headers = {"User-Agent": _USER_AGENT}
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with httpx.Client(timeout=15.0, headers=_ws_headers) as client:
                r = client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
            break
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1:
                _log.warning("web_search attempt %d/%d failed (%s) [engine: %s], retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e, searxng_url, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {"error": str(e), "results": []}
        except httpx.HTTPStatusError as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1 and e.response.status_code in _RETRYABLE_STATUS_CODES:
                _log.warning("web_search attempt %d/%d failed (HTTP %d) [engine: %s], retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e.response.status_code, searxng_url, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {"error": str(e), "results": []}
        except Exception as e:
            return {"error": str(e), "results": []}
    else:
        return {"error": str(last_err), "results": []}
    results = data.get("results") or []
    out = []
    for hit in results[:max_results]:
        entry: dict[str, str] = {
            "title": hit.get("title", ""),
            "url": hit.get("url", ""),
            "snippet": hit.get("content", "")[:300],
        }
        if hit.get("img_src"):
            entry["img_src"] = hit["img_src"]
        out.append(entry)
    return {"results": out}


def check_url(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """
    Prüft, ob eine URL erreichbar ist (HTTP/HTTPS, folgt Redirects).
    Gibt reachable (bool), status_code, final_url (nach Redirects) und ggf. error zurück.
    Kein automatisches Anhängen von www – es wird genau die übergebene URL geprüft.
    """
    u = (url or "").strip()
    if not u:
        return {"reachable": False, "status_code": None, "final_url": None, "error": "URL is empty"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    if _is_ssrf_target(u):
        return {"reachable": False, "status_code": None, "final_url": None, "error": "Access to internal/private networks is blocked"}
    last_err: Exception | None = None
    _cu_headers = {"User-Agent": _USER_AGENT}
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=_cu_headers) as client:
                r = client.get(u)
                final = str(r.url)
                if r.status_code in _RETRYABLE_STATUS_CODES and attempt < _RETRY_ATTEMPTS - 1:
                    _log.warning("check_url attempt %d/%d got HTTP %d, retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, r.status_code, _RETRY_DELAY)
                    time.sleep(_RETRY_DELAY)
                    continue
                return {
                    "reachable": 200 <= r.status_code < 400,
                    "status_code": r.status_code,
                    "final_url": final if final != u else None,
                }
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1:
                _log.warning("check_url attempt %d/%d failed (%s), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {"reachable": False, "status_code": None, "final_url": None, "error": str(e)}
        except httpx.HTTPStatusError as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1 and e.response.status_code in _RETRYABLE_STATUS_CODES:
                _log.warning("check_url attempt %d/%d failed (HTTP %d), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e.response.status_code, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {
                "reachable": False,
                "status_code": e.response.status_code if e.response else None,
                "final_url": str(e.response.url) if e.response else None,
                "error": str(e),
            }
        except Exception as e:
            return {"reachable": False, "status_code": None, "final_url": None, "error": str(e)}
    return {"reachable": False, "status_code": None, "final_url": None, "error": str(last_err)}


# ---------------------------------------------------------------------------
# read_url – URL-Inhalt als bereinigten Text lesen
# ---------------------------------------------------------------------------

class _HTMLToText(HTMLParser):
    """Minimaler HTML-zu-Text-Konverter ohne externe Abhängigkeiten."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Mehrfach-Leerzeilen zusammenfassen, Zeilen trimmen
        lines = [line.strip() for line in raw.splitlines()]
        collapsed: list[str] = []
        prev_empty = False
        for line in lines:
            if not line:
                if not prev_empty:
                    collapsed.append("")
                prev_empty = True
            else:
                collapsed.append(line)
                prev_empty = False
        return "\n".join(collapsed).strip()


def _html_to_text(html: str) -> str:
    """Konvertiert HTML zu lesbarem Text (ohne externe Libs)."""
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()


_GITHUB_ISSUE_PR_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/(issues|pull)/(\d+)(?:[#?].*)?$"
)
_GITHUB_RAW_FILE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+?)(?:[#?].*)?$"
)


def _trunc_marker(raw: str, shown: int) -> str:
    """Informativer Truncation-Marker mit Rest-Länge + Handlungsanweisung."""
    total = len(raw)
    remaining = total - shown
    return (f"\n\n[... truncated: {shown:,} of {total:,} chars shown, {remaining:,} chars cut. "
            f"Re-call read_url with max_chars={total} to get full content. ...]")


def _try_github_api(url: str, gh_token: str | None, max_chars: int, timeout: float) -> dict[str, Any] | None:
    """Versucht GitHub-URLs über die API zu lesen. Gibt None zurück wenn keine GitHub-URL."""
    # Issues / Pull Requests
    m = _GITHUB_ISSUE_PR_RE.match(url)
    if m:
        owner, repo, kind, number = m.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
        headers: dict[str, str] = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        try:
            r = httpx.get(api_url, headers=headers, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            data = r.json()
            parts = [
                f"# {data.get('title', '')} (#{number})",
                f"**State:** {data.get('state', '')}",
                f"**Author:** {(data.get('user') or {}).get('login', '')}",
                f"**Created:** {data.get('created_at', '')}",
                f"**Labels:** {', '.join(l.get('name', '') for l in (data.get('labels') or []))}",
                "",
                data.get("body") or "(no description)",
            ]
            # Kommentare laden wenn wenige
            comments_count = data.get("comments", 0)
            if comments_count > 0 and comments_count <= 20:
                try:
                    cr = httpx.get(api_url + "/comments", headers=headers, timeout=timeout)
                    cr.raise_for_status()
                    for c in cr.json():
                        parts.append(f"\n---\n**{(c.get('user') or {}).get('login', '')}** ({c.get('created_at', '')}):\n{c.get('body', '')}")
                except Exception:
                    pass
            elif comments_count > 20:
                parts.append(f"\n({comments_count} comments — showing first page only)")
            text = "\n".join(parts)
            if len(text) > max_chars:
                text = text[:max_chars] + _trunc_marker(text, max_chars)
            return {"ok": True, "content": text}
        except Exception as e:
            _log.warning("GitHub API fallback failed for %s: %s", url, e)
            return None  # Fallback auf normalen HTTP-Abruf

    # Dateien in Repos (blob → raw)
    m = _GITHUB_RAW_FILE_RE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        headers = {"User-Agent": _USER_AGENT}
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        try:
            r = httpx.get(raw_url, headers=headers, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            text = r.text
            if len(text) > max_chars:
                text = text[:max_chars] + _trunc_marker(text, max_chars)
            return {"ok": True, "content": text}
        except Exception as e:
            _log.warning("GitHub raw fallback failed for %s: %s", url, e)
            return None

    return None  # Keine GitHub-URL


def _playwright_read_url(url: str, timeout: float = 30.0) -> str:
    """Lädt eine URL über einen headless Chromium-Browser (JS-Rendering).
    Erfordert: pip install miniassistant[js] && playwright install chromium
    """
    with _sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
            html = page.content()
        finally:
            browser.close()
    return _html_to_text(html)


def read_url(url: str, max_chars: int | None = 8000, timeout: float = 15.0, config: dict[str, Any] | None = None, proxy: str | None = None, js: bool = False, use_cache: bool = True) -> dict[str, Any]:
    """
    Liest den Inhalt einer URL und gibt ihn als bereinigten Text zurück.
    Nutzt einen Browser-User-Agent um Bot-Detection/Anubis-Checks zu vermeiden.
    HTML wird automatisch in Text konvertiert.
    GitHub-URLs (Issues, PRs, Dateien) werden automatisch über die API gelesen.
    max_chars=None liefert ungekürzten Rohtext (für compress-callers).
    use_cache=True nutzt session-cache (miniassistant.url_cache) — hits liefern ungekürzt.
    """
    u = (url or "").strip()
    if not u:
        return {"ok": False, "content": "", "error": "URL is empty"}
    if u.startswith("file://"):
        return {"ok": False, "content": "", "error": "file:// URLs are not supported — use the read_file tool for local files"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    if _is_ssrf_target(u):
        return {"ok": False, "content": "", "error": "Access to internal/private networks is blocked"}

    _cache = None
    if use_cache and config is not None:
        try:
            from miniassistant.url_cache import get_cache
            _cache = get_cache(config)
            _hit = _cache.get(u)
            if _hit is not None:
                _log.info("read_url: cache hit for %s (%d tokens, %d entries total)", u, _hit.tokens, _cache.stats()["entries"])
                _txt = _hit.raw
                if max_chars is not None and len(_txt) > max_chars:
                    _txt = _txt[:max_chars] + _trunc_marker(_hit.raw, max_chars)
                return {"ok": True, "content": _txt, "connection": "cache", "from_cache": True}
        except Exception as e:
            _log.debug("read_url: cache lookup failed for %s: %s", u, e)
            _cache = None

    def _cache_and_trim(raw: str) -> str:
        """Store uncapped raw in session cache, return trimmed-to-max_chars copy."""
        if _cache is not None and raw:
            try:
                from miniassistant.chat_loop import _estimate_tokens
                _tok = _estimate_tokens(raw)
            except Exception:
                _tok = max(1, len(raw) // 4)
            try:
                _cache.put(u, raw, _tok)
            except Exception as _e:
                _log.debug("read_url: cache put failed: %s", _e)
        if max_chars is not None and len(raw) > max_chars:
            return raw[:max_chars] + _trunc_marker(raw, max_chars)
        return raw

    # In-flight dedup: if another thread already fetches this URL, wait for its cache put instead of re-fetching
    _is_leader = True
    if _cache is not None:
        _is_leader, _evt = _cache.begin_fetch(u)
        if not _is_leader:
            _log.info("read_url: in-flight wait for %s (another thread is fetching)", u)
            _got = _evt.wait(timeout=60)
            _hit2 = _cache.get(u) if _got else None
            if _hit2 is not None:
                _log.info("read_url: in-flight dedup hit for %s (%d tokens)", u, _hit2.tokens)
                _txt = _hit2.raw
                if max_chars is not None and len(_txt) > max_chars:
                    _txt = _txt[:max_chars] + _trunc_marker(_hit2.raw, max_chars)
                return {"ok": True, "content": _txt, "connection": "cache", "from_cache": True, "deduped": True}
            # Waiter timed out OR leader finished without caching (fetch failed) → become leader ourselves
            _is_leader, _evt = _cache.begin_fetch(u)

    try:
        # JS-Rendering via Playwright (optional, nur wenn js=True)
        if js:
            if not _check_playwright():
                _log.warning("read_url: js=True but Playwright not installed — falling back to plain fetch")
                fallback_note = "[Hinweis: js=True angefordert, aber Playwright ist nicht installiert. " \
                                "Zum Installieren: exec: pip install miniassistant[js] && playwright install chromium]\n\n"
            else:
                try:
                    text = _playwright_read_url(u, timeout=max(timeout, 30.0))
                    return {"ok": True, "content": _cache_and_trim(text)}
                except Exception as e:
                    _log.warning("read_url: Playwright fehlgeschlagen (%s) — Fallback auf normalen Fetch", e)
                    # Fallback auf normalen httpx-Abruf
                fallback_note = ""
            # Playwright nicht verfügbar oder fehlgeschlagen → weiter mit normalem Fetch
            # fallback_note wird unten ggf. dem Content vorangestellt
        else:
            fallback_note = ""

        # GitHub-URLs über die API lesen (besserer Content, kein HTML-Navigation-Müll)
        gh_token = ((config or {}).get("github_token") or "").strip() or None
        gh_result = _try_github_api(u, gh_token, max_chars, timeout)
        if gh_result is not None:
            return gh_result
        try:
            proxy_url, connection_name = _get_proxy_for_request(config, proxy_name=proxy)
        except ValueError as e:
            return {"ok": False, "content": "", "error": str(e)}
        if proxy_url:
            _log.info("read_url: using connection '%s' → %s", connection_name, proxy_url)
        else:
            _log.info("read_url: using connection '%s' (direct)", connection_name)
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br" if _BROTLI_AVAILABLE else "gzip, deflate",
        }
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "headers": headers,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        last_err: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                with httpx.Client(**client_kwargs) as client:
                    r = client.get(u)
                    r.raise_for_status()
                    content_type = r.headers.get("content-type", "")
                    if "html" in content_type:
                        text = _html_to_text(r.text)
                    elif "json" in content_type:
                        text = r.text
                    elif content_type.startswith("text/"):
                        text = r.text
                    else:
                        return {
                            "ok": False,
                            "content": "",
                            "error": f"Unsupported content-type: {content_type}",
                            "connection": connection_name,
                        }
                return {"ok": True, "content": fallback_note + _cache_and_trim(text), "connection": connection_name}
            except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                # Kein Retry bei Timeouts — wenn die Seite nicht antwortet, hilft warten nicht
                return {"ok": False, "content": "", "error": str(e), "connection": connection_name}
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code in (403, 429) and _CURL_CFFI_AVAILABLE:
                    # Bot-Detection (Cloudflare etc.) → Safari-Impersonation als Fallback
                    _log.info("read_url: HTTP %d → retrying with curl_cffi Safari impersonation", e.response.status_code)
                    try:
                        cr = _curl_requests.get(u, impersonate="safari17_0", timeout=timeout)
                        if cr.status_code < 400:
                            ct = cr.headers.get("content-type", "")
                            if "html" in ct:
                                text = _html_to_text(cr.text)
                            else:
                                text = cr.text
                            return {"ok": True, "content": _cache_and_trim(text), "connection": connection_name}
                    except Exception as ce:
                        _log.debug("read_url curl_cffi fallback failed: %s", ce)
                if attempt < _RETRY_ATTEMPTS - 1 and e.response.status_code in _RETRYABLE_STATUS_CODES:
                    _log.warning("read_url attempt %d/%d failed (HTTP %d), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e.response.status_code, _RETRY_DELAY)
                    time.sleep(_RETRY_DELAY)
                    continue
                return {
                    "ok": False,
                    "content": "",
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "connection": connection_name,
                }
            except Exception as e:
                return {"ok": False, "content": "", "error": str(e), "connection": connection_name}
        return {"ok": False, "content": "", "error": str(last_err), "connection": connection_name}
    finally:
        if _is_leader and _cache is not None:
            try:
                _cache.finish_fetch(u)
            except Exception as _fe:
                _log.debug("read_url: finish_fetch failed for %s: %s", u, _fe)


# ---------------------------------------------------------------------------
# Email – send_email (SMTP) und read_email (IMAP)
# ---------------------------------------------------------------------------

def _get_email_account(config: dict[str, Any], account: str | None = None) -> dict[str, Any] | None:
    """Resolve email account from config. Supports three formats:
    Format 1: email.accounts.{name}  (documented, multi-account)
    Format 2: email.{name}.{...}     (nested by account name, e.g. email.main.email)
    Format 3: email.address/email    (flat, single account — treated as 'main')
    Also normalizes 'address' key to 'email' for consistency.
    """
    email_cfg = config.get("email") or {}
    if not email_cfg:
        return None

    # Format 1: email.accounts.{name}
    accounts = email_cfg.get("accounts")
    if accounts and isinstance(accounts, dict):
        default = email_cfg.get("default", next(iter(accounts), None))
        name = account or default
        acc = accounts.get(name) if name else None
        return _normalize_email_acc(acc) if acc else None

    # Format 2: email.{name}.{...} (skip meta keys, look for dict values)
    meta_keys = {"default", "accounts"}
    account_keys = [k for k in email_cfg if k not in meta_keys and isinstance(email_cfg[k], dict)]
    if account_keys:
        default = email_cfg.get("default", account_keys[0])
        name = account or default
        acc = email_cfg.get(name)
        return _normalize_email_acc(acc) if acc else None

    # Format 3: flat — email.address or email.email directly (single account)
    if email_cfg.get("address") or email_cfg.get("email") or email_cfg.get("username"):
        return _normalize_email_acc(email_cfg)

    return None


def _normalize_email_acc(acc: dict[str, Any]) -> dict[str, Any]:
    """Ensure the account dict has an 'email' key (normalize from 'address'/'username' if needed)."""
    if "email" not in acc:
        addr = acc.get("address") or acc.get("username") or ""
        if addr:
            acc = dict(acc)
            acc["email"] = addr
    return acc


def _get_email_account_names(config: dict[str, Any]) -> list[str]:
    """Return list of configured email account names."""
    email_cfg = config.get("email") or {}
    if not email_cfg:
        return []
    # Format 1
    accounts = email_cfg.get("accounts")
    if accounts and isinstance(accounts, dict):
        return list(accounts.keys())
    # Format 2
    meta_keys = {"default", "accounts"}
    account_keys = [k for k in email_cfg if k not in meta_keys and isinstance(email_cfg[k], dict)]
    if account_keys:
        return account_keys
    # Format 3: flat single account
    if email_cfg.get("address") or email_cfg.get("email") or email_cfg.get("username"):
        return ["main"]
    return []


def send_email(config: dict[str, Any], to: str, subject: str, body: str, account: str | None = None) -> dict[str, Any]:
    """Send an email via SMTP using credentials from config."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    acc = _get_email_account(config, account)
    if not acc:
        return {"ok": False, "error": "No email account configured. Set up email in config.yaml."}

    sender = acc.get("email") or acc.get("username", "")
    password = acc.get("password", "")
    if not sender or not password:
        return {"ok": False, "error": "Email account missing 'email'/'username' or 'password'."}

    # Sanitize headers against injection (strip newlines/carriage returns)
    to = to.replace("\n", "").replace("\r", "")
    subject = subject.replace("\n", "").replace("\r", "")

    try:
        msg = MIMEMultipart()
        msg["From"] = acc.get("name", sender) + f" <{sender}>" if acc.get("name") else sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        smtp_server = acc.get("smtp_server", "smtp.gmail.com")
        smtp_port = int(acc.get("smtp_port", 587))

        # Port 465 = implicit SSL (SMTP_SSL), Port 587 = STARTTLS
        # ssl/use_ssl only forces SMTP_SSL for non-standard ports
        use_ssl = acc.get("ssl") or acc.get("use_ssl")
        if smtp_port == 465 or (use_ssl and smtp_port != 587):
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(sender, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender, password)
                server.send_message(msg)

        return {"ok": True, "message": f"Email sent to {to}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_email(
    config: dict[str, Any],
    folder: str = "INBOX",
    count: int = 5,
    filter_criteria: str = "UNSEEN",
    account: str | None = None,
    mark_read: bool = True,
) -> dict[str, Any]:
    """Read emails via IMAP using credentials from config.
    mark_read=True (default): marks fetched emails as SEEN so they don't appear again with UNSEEN filter.
    mark_read=False: uses BODY.PEEK[] so emails stay unread on the server.
    """
    import imaplib
    import email as email_mod
    from email.header import decode_header

    acc = _get_email_account(config, account)
    if not acc:
        return {"ok": False, "error": "No email account configured.", "emails": []}

    sender = acc.get("email") or acc.get("username", "")
    password = acc.get("password", "")
    if not sender or not password:
        return {"ok": False, "error": "Email account missing credentials.", "emails": []}

    try:
        imap_server = acc.get("imap_server", "imap.gmail.com")
        imap_port = int(acc.get("imap_port", 993))

        mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        mail.login(sender, password)
        mail.select(folder)

        _, data = mail.search(None, filter_criteria)
        ids = data[0].split()

        # BODY.PEEK[] = fetch without marking as read; RFC822 = fetch and mark as read
        fetch_cmd = "(RFC822)" if mark_read else "(BODY.PEEK[])"

        emails: list[dict[str, str]] = []
        for uid in ids[-count:]:
            _, msg_data = mail.fetch(uid, fetch_cmd)
            raw = msg_data[0][1]
            msg = email_mod.message_from_bytes(raw)

            # Decode subject
            subj = ""
            if msg["Subject"]:
                parts = decode_header(msg["Subject"])
                subj = "".join(
                    p.decode(enc or "utf-8") if isinstance(p, bytes) else p
                    for p, enc in parts
                )

            # Get plain-text body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(errors="replace")
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="replace")

            emails.append({
                "from": msg.get("From", ""),
                "to": msg.get("To", ""),
                "subject": subj,
                "date": msg.get("Date", ""),
                "body": body[:2000],
            })

        mail.logout()
        return {"ok": True, "count": len(emails), "emails": emails}
    except Exception as e:
        return {"ok": False, "error": str(e), "emails": []}
