"""Wyoming-Protokoll-Client für STT (faster-whisper) und TTS (Piper).

Wyoming: simples TCP-Protokoll mit newline-terminierten JSON-Events + optionalen Binär-Payloads.
STT: transcribe → audio-start → audio-chunk(s) → audio-stop → transcript
TTS: synthesize → audio-start + audio-chunk(s) + audio-stop
"""
from __future__ import annotations

import io
import json
import logging
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_CHUNK = 4096


def _parse_url(url: str) -> tuple[str, int]:
    """'tcp://host:port' oder 'host:port' → (host, port)."""
    url = url.strip()
    for prefix in ("wyoming+tcp://", "tcp://", "wyoming://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    host, _, port_s = url.rpartition(":")
    return (host or "localhost"), int(port_s)


def _send_event(sock: socket.socket, etype: str, data: dict[str, Any] | None = None, payload: bytes | None = None) -> None:
    header: dict[str, Any] = {"type": etype, "data": data or {}}
    if payload:
        header["payload_length"] = len(payload)
    sock.sendall((json.dumps(header) + "\n").encode())
    if payload:
        sock.sendall(payload)


def _recv_event(sock: socket.socket) -> tuple[str, dict[str, Any], bytes | None]:
    # Read header line (newline-terminated JSON)
    buf = b""
    while True:
        ch = sock.recv(1)
        if not ch:
            raise ConnectionError("Wyoming: Verbindung geschlossen")
        buf += ch
        if ch == b"\n":
            break
    header = json.loads(buf)

    # Wyoming v1.8+: event data comes as a separate blob of data_length bytes
    # (NOT newline-terminated, immediately following the header line)
    data: dict[str, Any] = header.get("data") or {}
    dlen = header.get("data_length")
    if dlen:
        parts: list[bytes] = []
        rem = int(dlen)
        while rem > 0:
            c = sock.recv(min(_CHUNK, rem))
            if not c:
                break
            parts.append(c)
            rem -= len(c)
        try:
            data = json.loads(b"".join(parts))
        except Exception:
            pass

    # Binary payload (payload_length bytes of raw audio data)
    payload: bytes | None = None
    plen = header.get("payload_length")
    if plen:
        parts = []
        rem = int(plen)
        while rem > 0:
            c = sock.recv(min(_CHUNK, rem))
            if not c:
                break
            parts.append(c)
            rem -= len(c)
        payload = b"".join(parts)
    return header.get("type", ""), data, payload


def _to_pcm16(audio_bytes: bytes) -> bytes:
    """Konvertiert beliebiges Audioformat zu 16kHz 16bit Mono PCM via ffmpeg."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".audio") as f:
        f.write(audio_bytes)
        tmp = f.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", tmp, "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg Fehler: {r.stderr.decode()}")
        return r.stdout
    finally:
        Path(tmp).unlink(missing_ok=True)


def _pcm_to_wav(pcm: bytes, rate: int = 22050, channels: int = 1) -> bytes:
    """Verpackt raw PCM 16bit in WAV-Container."""
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(audio_bytes: bytes, url: str, language: str = "de") -> str:
    """Transkribiert Audio zu Text via Wyoming STT (faster-whisper)."""
    _log.info("Wyoming STT: %d bytes audio → %s (lang=%s)", len(audio_bytes), url, language)
    pcm = _to_pcm16(audio_bytes)
    host, port = _parse_url(url)
    with socket.create_connection((host, port), timeout=30) as sock:
        _send_event(sock, "transcribe", {"language": language})
        _send_event(sock, "audio-start", {"rate": 16000, "width": 2, "channels": 1})
        for i in range(0, len(pcm), _CHUNK):
            _send_event(sock, "audio-chunk", {"rate": 16000, "width": 2, "channels": 1}, payload=pcm[i:i + _CHUNK])
        _send_event(sock, "audio-stop", {})
        while True:
            etype, edata, _ = _recv_event(sock)
            if etype == "transcript":
                text = (edata.get("text") or "").strip()
                _log.info("Wyoming STT: Transkript: %s", text[:80])
                return text
            if etype in ("error", "run-finished"):
                _log.warning("Wyoming STT: unerwartetes Event '%s': %s", etype, edata)
                break
    return ""


def synthesize(
    text: str,
    url: str,
    voice: str | None = None,
    noise_scale: float | None = None,
    noise_w: float | None = None,
    length_scale: float | None = None,
    sentence_silence: float | None = None,
) -> bytes:
    """Synthetisiert Text zu WAV-Audio via Wyoming TTS (Piper).

    Synthesis-Optionen (Piper-spezifisch, alle optional):
      noise_scale:       Variabilität/Ausdrucksstärke der Stimme    (Default: 0.667)
      noise_w:           Dauer-Variabilität der Phoneme              (Default: 0.8)
      length_scale:      Sprechgeschwindigkeit (>1 = langsamer)      (Default: 1.0)
      sentence_silence:  Pause nach Sätzen in Sekunden               (Default: 0.2)
    """
    _log.info("Wyoming TTS: %d Zeichen → %s", len(text), url)
    host, port = _parse_url(url)
    voice_data: dict[str, Any] = {}
    if voice:
        voice_data["name"] = voice
    synth_config: dict[str, float] = {}
    if noise_scale is not None:
        synth_config["noise_scale"] = float(noise_scale)
    if noise_w is not None:
        synth_config["noise_w"] = float(noise_w)
    if length_scale is not None:
        synth_config["length_scale"] = float(length_scale)
    if sentence_silence is not None:
        synth_config["sentence_silence"] = float(sentence_silence)
    synth_data: dict[str, Any] = {"text": text, "voice": voice_data}
    if synth_config:
        synth_data["config"] = synth_config
    chunks: list[bytes] = []
    info: dict[str, Any] = {"rate": 22050, "width": 2, "channels": 1}
    with socket.create_connection((host, port), timeout=60) as sock:
        _send_event(sock, "synthesize", synth_data)
        while True:
            etype, edata, payload = _recv_event(sock)
            if etype == "audio-start":
                info = edata
            elif etype == "audio-chunk" and payload:
                chunks.append(payload)
            elif etype in ("audio-stop", "run-finished"):
                break
            elif etype == "error":
                raise RuntimeError(f"Wyoming TTS Fehler: {edata}")
    wav = _pcm_to_wav(b"".join(chunks), rate=info.get("rate", 22050), channels=info.get("channels", 1))
    _log.info("Wyoming TTS: %d bytes WAV generiert", len(wav))
    return wav
