"""
Anubis Proof-of-Work Solver (https://github.com/TecharoHQ/anubis).

Anubis schützt Seiten mit einer JS-PoW-Challenge: der Browser bekommt einen
`challenge`-String + `difficulty` und sucht eine `nonce`, sodass
SHA-256(challenge + nonce) als Hex mit `difficulty` Nullen beginnt. Das ist
reine Rechnung — kein DOM, kein Browser nötig. Python hashlib löst es schneller
als der Browser-Worker.

Verifiziert gegen Anubis-Source (lib/challenge/proofofwork/proofofwork.go,
web/js/main.ts), Stand main/v1.25:
  response = hex(sha256(challenge.randomData + str(nonce)))
  gültig wenn response.startswith("0" * rules.difficulty)
  Submit (GET): {base_prefix}/.within.website/x/cmd/anubis/api/pass-challenge
      ?id=<challenge.id>&response=<hex>&nonce=<n>&redir=<url>&elapsedTime=<ms>
  Server setzt Auth-Cookie + redirectet auf redir.

Best effort: schlägt nur die offene PoW-Wall. Cloudflare-Turnstile o. Ä. nicht.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from urllib.parse import urlencode, urljoin, urlparse

import httpx

_log = logging.getLogger("miniassistant.anubis")

# Marker im HTML, die auf eine Anubis-Challenge-Seite hindeuten.
_MARKERS = (
    'id="anubis_challenge"',
    "id='anubis_challenge'",
    'id="preact_info"',
    "id='preact_info'",
    "/.within.website/x/cmd/anubis/",
    "Protected by Anubis",
)

# templ.JSONScript rendert: <script id="..." type="application/json">{...}</script>
_CHALLENGE_RE = re.compile(
    r'<script[^>]*\bid=["\']anubis_challenge["\'][^>]*>(.*?)</script>', re.S | re.I
)
_BASEPREFIX_RE = re.compile(
    r'<script[^>]*\bid=["\']anubis_base_prefix["\'][^>]*>(.*?)</script>', re.S | re.I
)
# preact-Challenge (neuerer, leichter Modus): single-hash statt nonce-Brute-Force.
_PREACT_RE = re.compile(
    r'<script[^>]*\bid=["\']preact_info["\'][^>]*>(.*?)</script>', re.S | re.I
)

_PASS_PATH = "/.within.website/x/cmd/anubis/api/pass-challenge"

# Obergrenze gegen Endlosschleife bei absurder difficulty. 16^7 ~ 268M Hashes
# (difficulty 7 wäre schon ~Minuten); darüber lohnt sich der Versuch nicht.
_MAX_ITERS = 300_000_000


def looks_like_anubis(html: str) -> bool:
    """Heuristik: Sieht der Response-Body nach einer Anubis-Challenge-Seite aus?"""
    if not html:
        return False
    return any(m in html for m in _MARKERS)


def _parse_challenge(html: str) -> dict | None:
    """Extrahiert challenge/rules aus dem eingebetteten JSON. None wenn nicht parsebar."""
    m = _CHALLENGE_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
    except Exception as e:
        _log.debug("anubis: challenge-JSON nicht parsebar: %s", e)
        return None

    chal = data.get("challenge")
    rules = data.get("rules") or {}

    # challenge ist je nach Version Objekt {randomData,id} oder roher String
    if isinstance(chal, str):
        random_data, chal_id = chal, None
    elif isinstance(chal, dict):
        random_data = chal.get("randomData") or chal.get("random_data")
        chal_id = chal.get("id")
    else:
        return None
    if not random_data:
        return None

    try:
        difficulty = int(rules.get("difficulty", data.get("difficulty", 4)))
    except (TypeError, ValueError):
        difficulty = 4
    algorithm = rules.get("algorithm") or data.get("algorithm") or "fast"

    base_prefix = ""
    bp = _BASEPREFIX_RE.search(html)
    if bp:
        try:
            base_prefix = json.loads(bp.group(1).strip()) or ""
        except Exception:
            base_prefix = ""

    return {
        "random_data": random_data,
        "id": chal_id,
        "difficulty": max(0, difficulty),
        "algorithm": algorithm,
        "base_prefix": base_prefix,
    }


def _parse_preact(html: str) -> dict | None:
    """Extrahiert die preact-Challenge (redir/challenge/difficulty). None wenn nicht vorhanden."""
    m = _PREACT_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
    except Exception as e:
        _log.debug("anubis: preact_info-JSON nicht parsebar: %s", e)
        return None
    redir = data.get("redir")
    challenge = data.get("challenge")
    if not redir or not challenge:
        return None
    try:
        difficulty = int(data.get("difficulty", 1))
    except (TypeError, ValueError):
        difficulty = 1
    return {"redir": redir, "challenge": challenge, "difficulty": max(0, difficulty)}


def _solve_pow(random_data: str, difficulty: int) -> tuple[int | None, str | None]:
    """Brute-Force nonce, sodass hex(sha256(random_data+nonce)) mit difficulty Nullen startet."""
    prefix = "0" * difficulty
    rd = random_data.encode()
    nonce = 0
    while nonce < _MAX_ITERS:
        h = hashlib.sha256(rd + str(nonce).encode()).hexdigest()
        if h.startswith(prefix):
            return nonce, h
        nonce += 1
    return None, None


def solve_and_fetch(
    url: str,
    challenge_html: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> str | None:
    """Löst eine Anubis-Challenge und holt die geschützte Seite.

    Gibt den HTML-Body der entsperrten Seite zurück, oder None wenn nicht lösbar
    (keine bekannte Challenge, difficulty zu hoch, Solve schlug fehl, weiter geblockt).

    Wichtig: Die Challenge ist an die Issuance-Anfrage gebunden (cookie-verification +
    JWT). Darum wird die Seite hier in EINEM Client frisch geholt, gelöst und die
    pass-challenge im selben Cookie-Jar abgeschickt — `challenge_html` (vom Caller)
    dient nur als Hinweis, neu geparst wird der frische Abruf.
    Nur same-origin: pass-challenge + redir laufen gegen denselben Host wie url.
    """
    cli_kw: dict = {"timeout": timeout, "follow_redirects": True, "headers": headers or {}}
    if proxy:
        cli_kw["proxy"] = proxy
    try:
        with httpx.Client(**cli_kw) as c:
            # Frische Issuance im selben Client → cookie-verification landet im Jar.
            html = c.get(url).text
            pass_url = _build_pass_url(url, html)
            if pass_url is None:
                return None
            # Same-origin-Guard: pass-challenge muss auf denselben Host wie url zeigen.
            if urlparse(pass_url).hostname != urlparse(url).hostname:
                _log.warning("anubis: pass-challenge Host weicht ab — abbruch")
                return None
            resp = c.get(pass_url)  # validiert, setzt Auth-Cookie, redirectet auf die Seite
            if resp.status_code < 400 and not looks_like_anubis(resp.text):
                return resp.text
            _log.warning("anubis: nach Solve immer noch geblockt (HTTP %d)", resp.status_code)
    except Exception as e:
        _log.warning("anubis: solve-fetch fehlgeschlagen: %s", e)
    return None


def _build_pass_url(url: str, html: str) -> str | None:
    """Löst die Challenge im HTML und baut die pass-challenge-URL (inkl. Lösung)."""
    # Modus 1: klassische PoW-Challenge (nonce-Brute-Force über difficulty Null-Hex).
    # algorithm 'preact' nutzt denselben anubis_challenge-Block, wird aber als Modus 2
    # gelöst (single-hash) — hier nur fast/slow behandeln.
    info = _parse_challenge(html)
    if info is not None and info["algorithm"] in ("fast", "slow"):
        t0 = time.time()
        nonce, response_hash = _solve_pow(info["random_data"], info["difficulty"])
        if nonce is None:
            _log.warning("anubis: difficulty %d nicht in %d Iterationen lösbar", info["difficulty"], _MAX_ITERS)
            return None
        elapsed_ms = int((time.time() - t0) * 1000)
        _log.info("anubis: PoW gelöst (difficulty=%d, nonce=%d, %dms)", info["difficulty"], nonce, elapsed_ms)
        params = {"response": response_hash, "nonce": str(nonce), "redir": url, "elapsedTime": str(elapsed_ms)}
        if info.get("id"):
            params["id"] = info["id"]
        return urljoin(url, (info["base_prefix"] or "") + _PASS_PATH) + "?" + urlencode(params)

    # Modus 2: preact-Challenge (single SHA-256 + serverseitige Zeitsperre).
    preact = _parse_preact(html)
    if preact is not None:
        result = hashlib.sha256(preact["challenge"].encode()).hexdigest()
        # Server verlangt difficulty*80ms seit Issue-Zeitpunkt (sonst "insufficient time").
        # Browser-JS wartet difficulty*125ms; wir nehmen *90ms + Puffer, gedeckelt.
        time.sleep(min(preact["difficulty"] * 0.09 + 0.25, 10.0))
        pass_url = urljoin(url, preact["redir"])  # redir = fertige pass-challenge-URL (mit redir&id)
        sep = "&" if urlparse(pass_url).query else "?"
        _log.info("anubis: preact-Challenge gelöst (difficulty=%d)", preact["difficulty"])
        return pass_url + sep + urlencode({"result": result})

    if info is not None:
        _log.warning("anubis: Challenge-Algorithmus '%s' nicht unterstützt", info["algorithm"])
    return None  # z. B. harte Deny-Seite ohne lösbare Challenge
