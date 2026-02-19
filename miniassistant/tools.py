"""
Tools: exec (Shell-Befehl), web_search (SearXNG), check_url (URL-Erreichbarkeit).
exec führt Befehle aus; Root-Info steht im System-Prompt (KI weiß ob sudo nötig).
"""
from __future__ import annotations

import re
import subprocess
from typing import Any
import httpx

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
