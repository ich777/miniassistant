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
    """Check if URL targets private/internal networks or cloud metadata services (SSRF protection).
    Strict: blocks loopback, private, link-local, reserved, unspecified, multicast, cloud metadata.
    Treats hostname "0", "0.0.0.0", "localhost" explicitly."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().strip()
        if not hostname:
            return True
        # Trivial hostnames that resolve to local/unspecified
        if hostname in ("0", "0.0.0.0", "localhost", "ip6-localhost", "ip6-loopback", "::", "::1"):
            return True
        # Block cloud metadata services
        _BLOCKED_HOSTS = {
            "169.254.169.254",  # AWS/GCP/Azure metadata
            "metadata.google.internal",
            "metadata.google.com",
            "100.100.100.200",  # Alibaba Cloud metadata
            "fd00:ec2::254",    # AWS IMDS IPv6
        }
        if hostname in _BLOCKED_HOSTS:
            return True
        # Block link-local metadata IP range (catches encoded forms before resolve)
        if hostname.startswith("169.254."):
            return True
        # Resolve hostname and check every returned IP
        import socket
        try:
            addrs = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False  # Can't resolve, let httpx handle the error
        for family, _, _, _, sockaddr in addrs:
            ip_str = sockaddr[0]
            # IPv6 zone-id strip ("fe80::1%eth0")
            if "%" in ip_str:
                ip_str = ip_str.split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_unspecified
                    or ip.is_multicast
                ):
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False


class SSRFBlocked(httpx.RequestError):
    """Raised when a request (or its redirect target) hits a blocked SSRF host."""

    def __init__(self, target: str) -> None:
        super().__init__(f"SSRF blocked (internal/private target): {target}")
        self.target = target


_MAX_REDIRECTS = 5


def _safe_redirect_loop(
    method: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> tuple[str, "httpx.Response"]:
    """Fetch with manual redirect handling — re-checks SSRF on every hop.

    Returns (final_url, final_response). Body fully buffered so callers can use
    r.text / r.content / r.json() after the underlying client is closed.
    Raises SSRFBlocked if any hop targets an internal/private/metadata host.
    """
    from urllib.parse import urljoin
    current = url
    visited: set[str] = set()
    base_kwargs: dict[str, Any] = {
        "timeout": timeout, "follow_redirects": False, "headers": headers or {},
    }
    if proxy:
        base_kwargs["proxy"] = proxy
    for _hop in range(max_redirects + 1):
        if _is_ssrf_target(current):
            raise SSRFBlocked(current)
        with httpx.Client(**base_kwargs) as client:
            r = client.request(method, current)
            _ = r.content  # force body read before client closes
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location") or ""
            if not loc:
                return current, r
            new = urljoin(current, loc)
            if new in visited:
                raise SSRFBlocked(new)  # redirect loop ≈ malicious
            visited.add(new)
            current = new
            continue
        return current, r
    raise SSRFBlocked(current)


def _resolve_url_safely(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> str:
    """Resolve redirect chain via HEAD (with GET-fallback), checking SSRF on every hop.
    Returns final URL. Raises SSRFBlocked if any hop is internal. Used by streaming
    downloads where pre-reading the body is wasteful."""
    from urllib.parse import urljoin
    current = url
    visited: set[str] = set()
    base_kwargs: dict[str, Any] = {
        "timeout": min(timeout, 30.0), "follow_redirects": False, "headers": headers or {},
    }
    if proxy:
        base_kwargs["proxy"] = proxy
    for _hop in range(max_redirects + 1):
        if _is_ssrf_target(current):
            raise SSRFBlocked(current)
        try:
            with httpx.Client(**base_kwargs) as client:
                r = client.head(current)
                if r.status_code == 405:
                    # Some servers reject HEAD; tiny ranged GET as fallback
                    r = client.get(current, headers={**(headers or {}), "Range": "bytes=0-0"})
        except httpx.HTTPError:
            # Network error during resolve — return current; final fetch will fail naturally
            return current
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location") or ""
            if not loc:
                return current
            new = urljoin(current, loc)
            if new in visited:
                raise SSRFBlocked(new)
            visited.add(new)
            current = new
            continue
        return current
    raise SSRFBlocked(current)


def _curl_cffi_safe_get(
    url: str,
    *,
    impersonate: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    max_redirects: int = _MAX_REDIRECTS,
):
    """curl_cffi.get with manual redirect handling + SSRF check on each hop.
    Returns the final curl_cffi response. Caller must check status code."""
    from urllib.parse import urljoin
    current = url
    visited: set[str] = set()
    for _hop in range(max_redirects + 1):
        if _is_ssrf_target(current):
            raise SSRFBlocked(current)
        r = _curl_requests.get(
            current, impersonate=impersonate, timeout=timeout,
            headers=headers, allow_redirects=False,
        )
        if 300 <= r.status_code < 400:
            loc = (r.headers.get("location") or r.headers.get("Location") or "")
            if not loc:
                return r
            new = urljoin(current, loc)
            if new in visited:
                raise SSRFBlocked(new)
            visited.add(new)
            current = new
            continue
        return r
    raise SSRFBlocked(current)


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


_RR_LOCK = __import__("threading").Lock()
_RR_INDEX = 0  # global round-robin counter (in-process)


def web_search_multi(
    config: dict[str, Any],
    query: str,
    categories: str | None = None,
    engine_id: str | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """Multi-Engine Web-Search mit konfigurierbarer Strategie.

    config.search_engine_strategy:
      'first'      (default) — Default-Engine, bei error/empty Fallback zu zufälliger anderer
      'specific'   — nur Default-Engine, KEIN Fallback
      'random'     — zufällige Engine
      'roundrobin' — Engines rotieren über Queries (jede sieht 1/N der Requests). Fallback bei Fehler.
      'fallback'   — alle Engines sequenziell durchgehen, erste mit Ergebnissen wins

    engine_id: expliziter Override — überspringt Strategie, nutzt nur diese Engine (kein Fallback).

    Returns: {results: [...], used_engines: [eid, ...], errors: {eid: msg}}
    """
    engines = config.get("search_engines") or {}
    if not engines:
        return {"error": "no search_engines configured", "results": [], "used_engines": [], "errors": {}}

    # Engine-Override: nutze nur diese, kein Fallback
    if engine_id:
        url = (engines.get(engine_id, {}).get("url") or "").strip()
        if not url:
            return {"error": f"engine '{engine_id}' not found or has no url", "results": [], "used_engines": [], "errors": {}}
        r = web_search(url, query, max_results=max_results, categories=categories)
        return {
            "results": r.get("results") or [],
            "used_engines": [engine_id],
            "errors": {engine_id: r["error"]} if r.get("error") else {},
        }

    strategy = (config.get("search_engine_strategy") or "first").strip().lower()
    default_eid = config.get("default_search_engine") or next(iter(engines), None)

    if strategy == "specific":
        # Nur Default-Engine, kein Fallback
        if not default_eid or default_eid not in engines:
            return {"error": "default engine not configured", "results": [], "used_engines": [], "errors": {}}
        url = (engines[default_eid].get("url") or "").strip()
        r = web_search(url, query, max_results=max_results, categories=categories)
        return {
            "results": r.get("results") or [],
            "used_engines": [default_eid],
            "errors": {default_eid: r["error"]} if r.get("error") else {},
        }

    if strategy == "random":
        import random as _r
        eid = _r.choice(list(engines.keys()))
        url = (engines[eid].get("url") or "").strip()
        r = web_search(url, query, max_results=max_results, categories=categories)
        return {
            "results": r.get("results") or [],
            "used_engines": [eid],
            "errors": {eid: r["error"]} if r.get("error") else {},
        }

    if strategy == "roundrobin":
        # Spread Load: jede Query trifft genau EINE Engine, die nächste die NÄCHSTE Engine, usw.
        # In-Process Counter (threadsafe). Bei Error/empty → sequenzieller Fallback zu anderen.
        global _RR_INDEX
        valid = [(eid, (ec.get("url") or "").strip()) for eid, ec in engines.items() if (ec.get("url") or "").strip()]
        if not valid:
            return {"error": "no valid engine urls", "results": [], "used_engines": [], "errors": {}}
        with _RR_LOCK:
            idx = _RR_INDEX % len(valid)
            _RR_INDEX = (_RR_INDEX + 1) % (len(valid) * 1000)
        # Reihenfolge: gewählte Engine first, danach restliche als Fallback-Order
        order = [valid[idx]] + valid[idx + 1:] + valid[:idx]
        used: list[str] = []
        errors: dict[str, str] = {}
        for eid, url in order:
            used.append(eid)
            r = web_search(url, query, max_results=max_results, categories=categories)
            if r.get("error"):
                errors[eid] = r["error"]
                continue
            if r.get("results"):
                return {"results": r["results"], "used_engines": used, "errors": errors}
            # leer aber kein error → nächste probieren
        return {"results": [], "used_engines": used, "errors": errors}

    if strategy == "fallback":
        # Sequenziell: Default zuerst, dann andere in Reihenfolge. Erste mit Ergebnissen wins.
        order = [default_eid] + [e for e in engines.keys() if e != default_eid]
        errors: dict[str, str] = {}
        used: list[str] = []
        for eid in order:
            if not eid or eid not in engines:
                continue
            url = (engines[eid].get("url") or "").strip()
            if not url:
                continue
            used.append(eid)
            r = web_search(url, query, max_results=max_results, categories=categories)
            if r.get("error"):
                errors[eid] = r["error"]
                continue
            if r.get("results"):
                return {"results": r["results"], "used_engines": used, "errors": errors}
        return {"results": [], "used_engines": used, "errors": errors}

    # 'first' (default behavior): Default-Engine + Fallback bei leerem Ergebnis zu zufälliger anderer
    if not default_eid or default_eid not in engines:
        return {"error": "default engine not configured", "results": [], "used_engines": [], "errors": {}}
    url = (engines[default_eid].get("url") or "").strip()
    r = web_search(url, query, max_results=max_results, categories=categories)
    if r.get("error"):
        # Bei harten Errors auf Default → fallback wie früher (alte Logik war "nur bei empty")
        errors = {default_eid: r["error"]}
        used = [default_eid]
        import random as _r
        others = [eid for eid in engines if eid != default_eid and (engines[eid].get("url") or "").strip()]
        _r.shuffle(others)
        for alt_eid in others:
            alt_url = (engines[alt_eid].get("url") or "").strip()
            used.append(alt_eid)
            alt = web_search(alt_url, query, max_results=max_results, categories=categories)
            if alt.get("error"):
                errors[alt_eid] = alt["error"]
                continue
            return {"results": alt.get("results") or [], "used_engines": used, "errors": errors}
        return {"results": [], "used_engines": used, "errors": errors}
    if r.get("results"):
        return {"results": r["results"], "used_engines": [default_eid], "errors": {}}
    # Empty: fallback zu zufälliger anderer Engine (alte Logik beibehalten)
    used = [default_eid]
    errors: dict[str, str] = {}
    import random as _r
    others = [eid for eid in engines if eid != default_eid and (engines[eid].get("url") or "").strip()]
    _r.shuffle(others)
    if others:
        alt_eid = others[0]
        alt_url = (engines[alt_eid].get("url") or "").strip()
        used.append(alt_eid)
        alt = web_search(alt_url, query, max_results=max_results, categories=categories)
        if not alt.get("error"):
            return {"results": alt.get("results") or [], "used_engines": used, "errors": errors}
        errors[alt_eid] = alt["error"]
    return {"results": [], "used_engines": used, "errors": errors}


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
            final_url, r = _safe_redirect_loop("GET", u, timeout=timeout, headers=_cu_headers)
            final = final_url
            if r.status_code in _RETRYABLE_STATUS_CODES and attempt < _RETRY_ATTEMPTS - 1:
                _log.warning("check_url attempt %d/%d got HTTP %d, retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, r.status_code, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {
                "reachable": 200 <= r.status_code < 400,
                "status_code": r.status_code,
                "final_url": final if final != u else None,
            }
        except SSRFBlocked as e:
            return {"reachable": False, "status_code": None, "final_url": None, "error": str(e)}
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


def _trunc_marker(raw: str, shown: int, offset: int = 0) -> str:
    """Offset-basierter Truncation-Marker. Weist NICHT an, das ganze Dokument zu holen
    (würde bei großen Docs den Kontext sprengen) — sondern den NÄCHSTEN Abschnitt via offset.
    `shown` = tatsächlich ausgegebene Zeichen dieses Abschnitts."""
    total = len(raw)
    end = offset + shown
    remaining = total - end
    if remaining <= 0:
        return ""
    return (f"\n\n[... {shown:,} chars shown (chars {offset:,}–{end:,} of {total:,}); {remaining:,} remain. "
            f"To read the next part, re-call read_url with the SAME url and offset={end} (keep max_chars moderate). "
            f"Read only as much as the task needs — do NOT fetch the whole document at once. ...]")


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
            # Ungekürzt zurück — read_url() macht das Windowing (offset/max_chars) zentral.
            return {"ok": True, "content": "\n".join(parts)}
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
            # Ungekürzt zurück — read_url() macht das Windowing (offset/max_chars) zentral.
            return {"ok": True, "content": r.text}
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


def read_url(url: str, max_chars: int | None = 8000, timeout: float = 15.0, config: dict[str, Any] | None = None, proxy: str | None = None, js: bool = False, use_cache: bool = True, offset: int = 0) -> dict[str, Any]:
    """
    Liest den Inhalt einer URL und gibt ihn als bereinigten Text zurück.
    Nutzt einen Browser-User-Agent um Bot-Detection/Anubis-Checks zu vermeiden.
    HTML wird automatisch in Text konvertiert.
    GitHub-URLs (Issues, PRs, Dateien) werden automatisch über die API gelesen.
    max_chars=None liefert ungekürzten Rohtext (für compress-callers).
    offset: Zeichen-Offset für Pagination großer Dokumente (Default 0).
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

    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0

    def _apply_window(raw: str) -> str:
        """Schneidet das Fenster [offset : offset+max_chars] aus und hängt bei Rest einen
        offset-basierten Truncation-Marker an. max_chars=None → ungekürzt ab offset."""
        if not raw:
            return raw
        if max_chars is None:
            return raw[offset:] if offset else raw
        seg = raw[offset:offset + max_chars]
        if len(raw) > offset + max_chars:
            seg += _trunc_marker(raw, len(seg), offset)
        return seg

    _cache = None
    if use_cache and config is not None:
        try:
            from miniassistant.url_cache import get_cache
            _cache = get_cache(config)
            _hit = _cache.get(u)
            if _hit is not None:
                _log.info("read_url: cache hit for %s (%d tokens, %d entries total)", u, _hit.tokens, _cache.stats()["entries"])
                return {"ok": True, "content": _apply_window(_hit.raw), "connection": "cache", "from_cache": True}
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
        return _apply_window(raw)

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
                return {"ok": True, "content": _apply_window(_hit2.raw), "connection": "cache", "from_cache": True, "deduped": True}
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
            if gh_result.get("ok"):
                # GitHub-Content im session-cache ablegen + windowing (offset/max_chars) anwenden
                gh_result["content"] = _cache_and_trim(gh_result.get("content") or "")
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
        last_err: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                _final_url, r = _safe_redirect_loop(
                    "GET", u, timeout=timeout, headers=headers, proxy=proxy_url,
                )
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
            except SSRFBlocked as e:
                return {"ok": False, "content": "", "error": str(e), "connection": connection_name}
            except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                # Kein Retry bei Timeouts — wenn die Seite nicht antwortet, hilft warten nicht
                return {"ok": False, "content": "", "error": str(e), "connection": connection_name}
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code in (403, 429) and _CURL_CFFI_AVAILABLE:
                    # Bot-Detection (Cloudflare etc.) → Safari-Impersonation als Fallback
                    _log.info("read_url: HTTP %d → retrying with curl_cffi Safari impersonation", e.response.status_code)
                    try:
                        cr = _curl_cffi_safe_get(u, impersonate="safari17_0", timeout=timeout)
                        if cr.status_code < 400:
                            ct = cr.headers.get("content-type", "")
                            if "html" in ct:
                                text = _html_to_text(cr.text)
                            else:
                                text = cr.text
                            return {"ok": True, "content": _cache_and_trim(text), "connection": connection_name}
                    except SSRFBlocked as ce:
                        return {"ok": False, "content": "", "error": str(ce), "connection": connection_name}
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
# download_file – Binaer-Download mit Safari-UA + Quirks. Fuer Bilder/PDFs/etc.
# ---------------------------------------------------------------------------

_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB Hard-Cap


def _safari_image_headers(url: str, referer: str | None = None) -> dict[str, str]:
    """Erzeugt Safari-typische Headers fuer Binary-Download.

    Quirks die Safari real schickt (vs naivem curl):
    - Accept: bildtyp-spezifisch fuer image/*-URLs, sonst */*
    - Accept-Language: nutzersystem-aehnlich
    - Sec-Fetch-Dest/Mode/Site: ab Safari 17 immer mitgeschickt
    - Referer: viele CDNs (Wikimedia, getty, instagram) blocken ohne
    - DNT: optional, nicht alle Safari-Setups senden
    """
    is_image = bool(re.search(r'\.(jpe?g|png|gif|webp|avif|svg|bmp|tiff?)(\?|$)', url, re.IGNORECASE))
    h: dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "Accept": (
            "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5"
            if is_image else "*/*"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br" if _BROTLI_AVAILABLE else "gzip, deflate",
        "Sec-Fetch-Dest": "image" if is_image else "document",
        "Sec-Fetch-Mode": "no-cors" if is_image else "navigate",
        "Sec-Fetch-Site": "cross-site",
    }
    if referer:
        h["Referer"] = referer
    else:
        # Auto-Referer fuer bekannte CDNs (sonst 403)
        if "upload.wikimedia.org" in url:
            h["Referer"] = "https://commons.wikimedia.org/"
        elif "imgur.com" in url:
            h["Referer"] = "https://imgur.com/"
        elif "redditmedia.com" in url or "i.redd.it" in url:
            h["Referer"] = "https://www.reddit.com/"
    return h


def download_file(
    url: str,
    path: str,
    *,
    referer: str | None = None,
    max_bytes: int = _DOWNLOAD_MAX_BYTES,
    timeout: float = 60.0,
    config: dict[str, Any] | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """
    Laedt eine Datei binaer von URL und speichert sie unter `path` (relativ → workspace/).

    Headers Safari-konform (UA, Accept-image, Sec-Fetch-*, Auto-Referer fuer Wikimedia/Imgur/Reddit).
    Bei 403/429 Fallback via curl_cffi mit Safari-TLS-Impersonation.

    Returns: {ok, path?, bytes?, content_type?, error?}
    """
    u = (url or "").strip()
    if not u:
        return {"ok": False, "error": "URL is empty"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    if _is_ssrf_target(u):
        return {"ok": False, "error": "Access to internal/private networks is blocked"}

    # Pfad: relativ → workspace/, absolut nur erlaubt wenn unter workspace
    workspace = ""
    if config:
        workspace = (config.get("workspace") or "").strip()
    from pathlib import Path as _P
    p = _P(path).expanduser()
    if not p.is_absolute():
        if not workspace:
            return {"ok": False, "error": "no workspace configured — provide an absolute path or set workspace in config"}
        p = (_P(workspace).resolve() / p).resolve()
    else:
        p = p.resolve()
    # Path-Traversal-Schutz: muss innerhalb workspace liegen
    if workspace:
        ws = _P(workspace).resolve()
        try:
            p.relative_to(ws)
        except ValueError:
            return {"ok": False, "error": f"path must be inside workspace ({ws})"}
    p.parent.mkdir(parents=True, exist_ok=True)

    headers = _safari_image_headers(u, referer=referer)
    proxy_url = None
    connection_name = "direct"
    if config is not None:
        try:
            proxy_url, connection_name = _get_proxy_for_request(config, proxy_name=proxy)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def _save_stream(resp_iter, ctype: str) -> dict[str, Any]:
        total = 0
        with open(p, "wb") as f:
            for chunk in resp_iter:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    f.close()
                    p.unlink(missing_ok=True)
                    return {"ok": False, "error": f"file exceeds max_bytes ({max_bytes})"}
                f.write(chunk)
        return {"ok": True, "path": str(p), "bytes": total, "content_type": ctype, "connection": connection_name}

    last_err: Exception | None = None
    # Resolve redirects manually with SSRF check on every hop, then stream final URL.
    try:
        final_url = _resolve_url_safely(u, timeout=timeout, headers=headers, proxy=proxy_url)
    except SSRFBlocked as e:
        return {"ok": False, "error": str(e), "connection": connection_name}
    client_kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False, "headers": headers}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with httpx.Client(**client_kwargs) as client:
                with client.stream("GET", final_url) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        # _resolve_url_safely should have eliminated these; if not, refuse.
                        return {"ok": False, "error": "unresolved redirect after safe resolve", "connection": connection_name}
                    if r.status_code in (403, 429) and _CURL_CFFI_AVAILABLE:
                        _log.info("download_file: HTTP %d → curl_cffi Safari impersonation", r.status_code)
                        break  # break to curl_cffi fallback below
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "application/octet-stream")
                    return _save_stream(r.iter_bytes(64 * 1024), ctype)
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            return {"ok": False, "error": str(e), "connection": connection_name}
        except httpx.HTTPStatusError as e:
            last_err = e
            sc = e.response.status_code
            if sc in (403, 429) and _CURL_CFFI_AVAILABLE:
                break  # fallback below
            if attempt < _RETRY_ATTEMPTS - 1 and sc in _RETRYABLE_STATUS_CODES:
                _log.warning("download_file attempt %d/%d failed (HTTP %d), retry in %ds", attempt + 1, _RETRY_ATTEMPTS, sc, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {"ok": False, "error": f"HTTP {sc}: {e}", "connection": connection_name}
        except Exception as e:
            return {"ok": False, "error": str(e), "connection": connection_name}

    # curl_cffi Fallback (TLS-fingerprint = Safari) — manual redirect with SSRF check
    if _CURL_CFFI_AVAILABLE:
        try:
            cr = _curl_cffi_safe_get(final_url, impersonate="safari17_0", timeout=timeout, headers=headers)
            if cr.status_code < 400:
                ctype = cr.headers.get("content-type", "application/octet-stream")
                content = cr.content or b""
                if len(content) > max_bytes:
                    return {"ok": False, "error": f"file exceeds max_bytes ({max_bytes})", "connection": connection_name}
                with open(p, "wb") as f:
                    f.write(content)
                return {"ok": True, "path": str(p), "bytes": len(content), "content_type": ctype, "connection": f"{connection_name}+curl_cffi"}
            return {"ok": False, "error": f"HTTP {cr.status_code} (curl_cffi)", "connection": connection_name}
        except SSRFBlocked as ce:
            return {"ok": False, "error": str(ce), "connection": connection_name}
        except Exception as ce:
            return {"ok": False, "error": f"curl_cffi failed: {ce}", "connection": connection_name}
    return {"ok": False, "error": str(last_err) if last_err else "unknown error", "connection": connection_name}


# ---------------------------------------------------------------------------
# Email – send_email (SMTP) und read_email (IMAP)
# ---------------------------------------------------------------------------

def _get_email_account(config: dict[str, Any], account: str | None = None) -> dict[str, Any] | None:
    """Resolve email account from config. Canonical schema: email.accounts.{name} + email.default."""
    email_cfg = config.get("email") or {}
    accounts = email_cfg.get("accounts")
    if not accounts or not isinstance(accounts, dict):
        return None
    default = email_cfg.get("default") or next(iter(accounts), None)
    name = account or default
    acc = accounts.get(name) if name else None
    if not acc or not isinstance(acc, dict):
        return None
    # Ensure 'email' key (config.py normalizes to 'username' — copy for SMTP/IMAP compat)
    if "email" not in acc and acc.get("username"):
        acc = {**acc, "email": acc["username"]}
    return acc


def _get_email_account_names(config: dict[str, Any]) -> list[str]:
    """Return list of configured email account names."""
    email_cfg = config.get("email") or {}
    accounts = email_cfg.get("accounts")
    if not accounts or not isinstance(accounts, dict):
        return []
    return list(accounts.keys())


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
    def _strip_crlf(s: str) -> str:
        return (s or "").replace("\n", "").replace("\r", "")
    to = _strip_crlf(to)
    subject = _strip_crlf(subject)
    sender_clean = _strip_crlf(sender)
    name_raw = _strip_crlf(str(acc.get("name") or ""))

    try:
        msg = MIMEMultipart()
        msg["From"] = f"{name_raw} <{sender_clean}>" if name_raw else sender_clean
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

    # IMAP-Argumente defensiv prüfen (auch wenn Agent-controlled — verhindert Halluzinations-Schaden).
    # Folder: nur Buchstaben/Zahlen/Punkt/Slash/Bindestrich/Unterstrich/Leerzeichen, max 64 Zeichen.
    if not isinstance(folder, str) or not re.match(r"^[A-Za-z0-9._/ \-]{1,64}$", folder):
        return {"ok": False, "error": f"invalid folder name: {folder!r}", "emails": []}
    # Filter: nur IMAP-Standard-Schlüsselwörter + simple Argumente. Keine CRLF/Quotes.
    if not isinstance(filter_criteria, str) or "\r" in filter_criteria or "\n" in filter_criteria:
        return {"ok": False, "error": "invalid filter_criteria (CRLF)", "emails": []}
    if len(filter_criteria) > 256 or not re.match(r"^[A-Za-z0-9 _.@\-:/+()<>=*]+$", filter_criteria):
        return {"ok": False, "error": f"invalid filter_criteria: {filter_criteria!r}", "emails": []}

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
