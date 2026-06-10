"""Maskiert bekannte Config-Secrets (Tokens, API-Keys, Passwörter) in LLM-Output und Tool-Ergebnissen.

Defense-in-Depth: fängt *wörtliche* Echos der Config-Secrets ab, bevor sie an Client/User gehen
(z. B. wenn das Modell via `exec`/`cat config.yaml` an ein Secret kommt und es ausplaudern soll).

KEIN Schutz gegen absichtliche Transformation (base64, Buchstabieren, Übersetzen). Der eigentliche
Schutz ist, Secrets nicht in den Kontext zu lassen — darum werden auch Tool-Ergebnisse maskiert,
damit das Modell den Klartext gar nicht erst sieht.

Kosten: pro Response/Stream/Tool-Call einmal die Secrets sammeln + ein Regex kompilieren (µs).
Streaming nutzt einen Sliding-Window-Buffer → O(L) gesamt, kein Re-Scan pro Chunk.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

import base64 as _b64

MASK = "[redacted]"
_MIN_SECRET_LEN = 8  # kürzere Werte nicht maskieren (sonst False-Positives auf normalem Text)


def _encoded_variants(s: str) -> list[str]:
    """Gängige Shell-Transformationen eines Secrets, damit `exec("… | base64")`-Exfiltration
    nicht durchrutscht. Deckt NICHT alles ab (rot13, reverse, gzip, awk-spacing …) — der echte
    Schutz ist, das Modell nicht an die Secrets zu lassen. Dies hebt nur die Latte gegen den
    häufigsten Fall (base64/hex)."""
    raw = s.encode("utf-8", "replace")
    variants: list[str] = []
    b64 = _b64.b64encode(raw).decode("ascii")
    variants.append(b64)                 # mit Padding
    variants.append(b64.rstrip("="))     # ohne Padding
    variants.append(_b64.b16encode(raw).decode("ascii"))         # HEX upper (xxd -p uppercase)
    variants.append(_b64.b16encode(raw).decode("ascii").lower()) # hex lower
    try:
        variants.append(_b64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
    except Exception:
        pass
    return [v for v in variants if len(v) >= _MIN_SECRET_LEN]


def collect_secrets(config: dict[str, Any]) -> list[str]:
    """Sammelt alle Secret-Werte aus der Config + gängige Encodings (base64/hex). Filtert auf
    Länge >= _MIN_SECRET_LEN, dedupliziert. Längste zuerst (greedy bei überlappenden Werten)."""
    out: set[str] = set()

    def _add(v: Any) -> None:
        if isinstance(v, str):
            s = v.strip()
            if len(s) >= _MIN_SECRET_LEN:
                out.add(s)
                for enc in _encoded_variants(s):
                    out.add(enc)

    srv = config.get("server") or {}
    _add(srv.get("token"))
    rp = config.get("raw_proxy") or {}
    _add(rp.get("token"))
    _add(config.get("github_token"))
    for prov in (config.get("providers") or {}).values():
        if isinstance(prov, dict):
            _add(prov.get("api_key"))
    cc = config.get("chat_clients") or {}
    _add((cc.get("matrix") or {}).get("token"))
    _add((cc.get("discord") or {}).get("bot_token"))
    for acc in ((config.get("email") or {}).get("accounts") or {}).values():
        if isinstance(acc, dict):
            _add(acc.get("password"))
    return sorted(out, key=len, reverse=True)


def _compile(secrets: list[str]) -> "re.Pattern[str] | None":
    if not secrets:
        return None
    return re.compile("|".join(re.escape(s) for s in secrets))


def masking_enabled(config: dict[str, Any]) -> bool:
    """server.mask_secrets_in_output (Default True)."""
    return bool((config.get("server") or {}).get("mask_secrets_in_output", True))


def mask_text(text: str, config: dict[str, Any]) -> str:
    """Maskiert alle bekannten Secrets in einem fertigen Text. No-op wenn deaktiviert/keine Secrets."""
    if not text or not masking_enabled(config):
        return text
    pat = _compile(collect_secrets(config))
    if pat is None:
        return text
    return pat.sub(MASK, text)


class StreamMasker:
    """Stateful Filter für Streaming-Output. Hält einen Tail-Puffer zurück, damit ein Secret,
    das über zwei Chunks zerteilt ankommt, nicht durchrutscht. O(L) gesamt."""

    def __init__(self, config: dict[str, Any]):
        if masking_enabled(config):
            secrets = collect_secrets(config)
            self._pat = _compile(secrets)
            self._keep = max((len(s) for s in secrets), default=0)
        else:
            self._pat = None
            self._keep = 0
        self._buf = ""

    def feed(self, delta: str) -> str:
        """Nimmt ein Chunk, gibt den sicher maskierbaren Teil zurück (ggf. leer = noch gepuffert)."""
        if self._pat is None:
            return delta
        if not delta:
            return ""
        self._buf += delta
        n = len(self._buf)
        if n <= self._keep:
            return ""
        cut = n - self._keep  # vorerst: die letzten _keep Zeichen zurückhalten
        # Falls ein Match die Grenze überspannt (beginnt vor cut, endet danach): cut auf
        # den Match-Start zurückziehen, sonst würde der Match-Anfang unmaskiert rausgehen.
        for m in self._pat.finditer(self._buf):
            if m.start() < cut < m.end():
                cut = m.start()
                break  # finditer liefert in Reihenfolge → erster Straddle ist bindend
        if cut <= 0:
            return ""
        head = self._buf[:cut]
        self._buf = self._buf[cut:]
        return self._pat.sub(MASK, head)

    def flush(self) -> str:
        """Rest am Stream-Ende ausgeben (maskiert)."""
        rest = self._buf
        self._buf = ""
        if self._pat is None or not rest:
            return rest
        return self._pat.sub(MASK, rest)


def mask_stream_events(events: Iterator[dict[str, Any]], config: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Wrappt einen chat_round_stream-Generator: maskiert Secrets in content/thinking-Deltas
    (streaming-sicher) und im finalen done-Event. Alle anderen Events bleiben unangetastet."""
    if not masking_enabled(config):
        yield from events
        return
    cmask = StreamMasker(config)
    tmask = StreamMasker(config)
    for ev in events:
        t = ev.get("type")
        if t == "content" and ev.get("delta"):
            out = cmask.feed(ev["delta"])
            if out:
                yield {**ev, "delta": out}
            continue
        if t == "thinking" and ev.get("delta"):
            out = tmask.feed(ev["delta"])
            if out:
                yield {**ev, "delta": out}
            continue
        if t == "done":
            tail_c = cmask.flush()
            tail_t = tmask.flush()
            if tail_c:
                yield {"type": "content", "delta": tail_c}
            if tail_t:
                yield {"type": "thinking", "delta": tail_t}
            new_ev = dict(ev)
            if new_ev.get("content"):
                new_ev["content"] = mask_text(new_ev["content"], config)
            if new_ev.get("thinking"):
                new_ev["thinking"] = mask_text(new_ev["thinking"], config)
            yield new_ev
            continue
        yield ev
