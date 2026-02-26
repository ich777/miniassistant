"""
Tools: exec (Shell-Befehl), web_search (SearXNG), check_url (URL-Erreichbarkeit),
read_url (URL-Inhalt als Text lesen), send_email (SMTP), read_email (IMAP).
exec führt Befehle aus; Root-Info steht im System-Prompt (KI weiß ob sudo nötig).
"""
from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from typing import Any
import logging
import time

import httpx

_log = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = (500, 502, 503, 504)
_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 3  # seconds


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


def web_search(searxng_url: str, query: str, max_results: int = 5) -> dict[str, Any]:
    """SearXNG JSON-API: ?q=...&format=json. Gibt Titel, URL, Snippet zurück."""
    url = searxng_url.rstrip("/")
    if "/search" not in url and not url.endswith("/search"):
        url = f"{url}/search"
    params = {"q": query, "format": "json"}
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with httpx.Client(timeout=15.0) as client:
                r = client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
            break
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1:
                _log.warning("web_search attempt %d/%d failed (%s), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {"error": str(e), "results": []}
        except httpx.HTTPStatusError as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1 and e.response.status_code in _RETRYABLE_STATUS_CODES:
                _log.warning("web_search attempt %d/%d failed (HTTP %d), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e.response.status_code, _RETRY_DELAY)
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
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
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
                text = text[:max_chars] + "\n\n[... truncated ...]"
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
                text = text[:max_chars] + "\n\n[... truncated ...]"
            return {"ok": True, "content": text}
        except Exception as e:
            _log.warning("GitHub raw fallback failed for %s: %s", url, e)
            return None

    return None  # Keine GitHub-URL


def read_url(url: str, max_chars: int = 8000, timeout: float = 15.0, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Liest den Inhalt einer URL und gibt ihn als bereinigten Text zurück.
    Nutzt einen Browser-User-Agent um Bot-Detection/Anubis-Checks zu vermeiden.
    HTML wird automatisch in Text konvertiert.
    GitHub-URLs (Issues, PRs, Dateien) werden automatisch über die API gelesen.
    max_chars begrenzt die Ausgabe (für kleine Modell-Kontexte).
    """
    u = (url or "").strip()
    if not u:
        return {"ok": False, "content": "", "error": "URL is empty"}
    if not u.startswith(("http://", "https://")):
        u = "https://" + u

    # GitHub-URLs über die API lesen (besserer Content, kein HTML-Navigation-Müll)
    gh_token = ((config or {}).get("github_token") or "").strip() or None
    gh_result = _try_github_api(u, gh_token, max_chars, timeout)
    if gh_result is not None:
        return gh_result
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
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
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1:
                _log.warning("read_url attempt %d/%d failed (%s), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {"ok": False, "content": "", "error": str(e)}
        except httpx.HTTPStatusError as e:
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1 and e.response.status_code in _RETRYABLE_STATUS_CODES:
                _log.warning("read_url attempt %d/%d failed (HTTP %d), retrying in %ds …", attempt + 1, _RETRY_ATTEMPTS, e.response.status_code, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
                continue
            return {
                "ok": False,
                "content": "",
                "error": f"HTTP {e.response.status_code}: {e}",
            }
        except Exception as e:
            return {"ok": False, "content": "", "error": str(e)}
    return {"ok": False, "content": "", "error": str(last_err)}


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
