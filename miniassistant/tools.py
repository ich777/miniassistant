"""
Tools: exec (Shell-Befehl), web_search (SearXNG), check_url (URL-Erreichbarkeit),
read_url (URL-Inhalt als Text lesen).
exec führt Befehle aus; Root-Info steht im System-Prompt (KI weiß ob sudo nötig).
"""
from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from typing import Any
import httpx

# Browser User-Agent um Bot-Detection und Anubis-Checks zu vermeiden
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
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


def run_exec(command: str, timeout: int = 60, cwd: str | None = None) -> dict[str, Any]:
    """
    Führt einen Shell-Befehl aus. Gibt stdout, stderr und returncode zurück.
    cwd: Working directory (default: Workspace aus Config, sonst Process-CWD).
    Kein sudo-Handling hier – die KI weiß aus dem System-Prompt ob sie root ist.
    """
    try:
        command = _fix_heredoc_quotes(command)
        if cwd:
            from pathlib import Path
            Path(cwd).mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
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


def web_search(searxng_url: str, query: str, max_results: int = 5) -> dict[str, Any]:
    """SearXNG JSON-API: ?q=...&format=json. Gibt Titel, URL, Snippet zurück."""
    url = searxng_url.rstrip("/")
    if "/search" not in url and not url.endswith("/search"):
        url = f"{url}/search"
    params = {"q": query, "format": "json"}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"error": str(e), "results": []}
    results = data.get("results") or []
    out = []
    for hit in results[:max_results]:
        out.append({
            "title": hit.get("title", ""),
            "url": hit.get("url", ""),
            "snippet": hit.get("content", "")[:300],
        })
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
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(u)
            final = str(r.url)
            return {
                "reachable": 200 <= r.status_code < 400,
                "status_code": r.status_code,
                "final_url": final if final != u else None,
            }
    except httpx.HTTPStatusError as e:
        return {
            "reachable": False,
            "status_code": e.response.status_code if e.response else None,
            "final_url": str(e.response.url) if e.response else None,
            "error": str(e),
        }
    except Exception as e:
        return {"reachable": False, "status_code": None, "final_url": None, "error": str(e)}


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


def read_url(url: str, max_chars: int = 8000, timeout: float = 15.0) -> dict[str, Any]:
    """
    Liest den Inhalt einer URL und gibt ihn als bereinigten Text zurück.
    Nutzt einen Browser-User-Agent um Bot-Detection/Anubis-Checks zu vermeiden.
    HTML wird automatisch in Text konvertiert.
    max_chars begrenzt die Ausgabe (für kleine Modell-Kontexte).
    """
    u = (url or "").strip()
    if not u:
        return {"ok": False, "content": "", "error": "URL is empty"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        }
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
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
                }
        # Auf max_chars begrenzen
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... truncated ...]"
        return {"ok": True, "content": text}
    except httpx.HTTPStatusError as e:
        return {
            "ok": False,
            "content": "",
            "error": f"HTTP {e.response.status_code}: {e}",
        }
    except Exception as e:
        return {"ok": False, "content": "", "error": str(e)}
