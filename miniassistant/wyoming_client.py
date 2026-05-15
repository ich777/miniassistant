"""Wyoming-Protokoll-Client für STT (faster-whisper) und TTS (Piper/Kokoro).

Wyoming: simples TCP-Protokoll mit newline-terminierten JSON-Events + optionalen Binär-Payloads.
STT: transcribe → audio-start → audio-chunk(s) → audio-stop → transcript
TTS Wyoming: synthesize → audio-start + audio-chunk(s) + audio-stop
TTS HTTP:   POST /v1/audio/speech (OpenAI-compat, z.B. Kokoro-FastAPI)
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


def _synthesize_http(
    text: str,
    url: str,
    voice: str | None = None,
    model: str | None = None,
    language: str | None = None,
    **opts: Any,
) -> bytes:
    """TTS via HTTP API.

    Zwei Body-Schemas, automatisch nach URL-Pfad gewählt:

      Pfad endet auf `/tts`          → Chatterbox-native (CustomTTSRequest)
                                       Felder: text, voice_mode, predefined_voice_id /
                                       reference_audio_filename, output_format, split_text,
                                       chunk_size, temperature, exaggeration, cfg_weight,
                                       seed, speed_factor, language
      Pfad endet auf `/audio/speech` → OpenAI-compat (Kokoro-FastAPI, Chatterbox-OpenAI,
                                       LocalAI/VibeVoice). Felder: model, input, voice,
                                       response_format, speed, seed, language
      kein Pfad                      → /v1/audio/speech anhängen, OpenAI-compat

    Unbekannte Keys in `opts` werden je Pfad gefiltert und weitergegeben.
    Antwort: rohe Audio-Bytes (WAV by default).
    """
    import httpx as _httpx
    from urllib.parse import urlparse as _urlparse
    _path = _urlparse(url).path.rstrip("/")
    if _path and _path != "/":
        endpoint = url.rstrip("/")
    else:
        endpoint = url.rstrip("/") + "/v1/audio/speech"

    is_chatterbox_native = endpoint.endswith("/tts")

    if is_chatterbox_native:
        # Chatterbox CustomTTSRequest
        voice_mode = str(opts.get("voice_mode") or "predefined")
        payload: dict[str, Any] = {
            "text": text,
            "voice_mode": voice_mode,
            "output_format": str(opts.get("response_format") or "wav"),
        }
        if voice:
            if voice_mode == "clone":
                payload["reference_audio_filename"] = voice
            else:
                payload["predefined_voice_id"] = voice
        if language:
            payload["language"] = language
        # Float/Int/Bool-Forwards (nur wenn gesetzt)
        for key in ("temperature", "exaggeration", "cfg_weight", "speed_factor"):
            if opts.get(key) is not None:
                payload[key] = float(opts[key])
        # speed → speed_factor Alias (OpenAI-Stil → native)
        if "speed_factor" not in payload and opts.get("speed") is not None:
            payload["speed_factor"] = float(opts["speed"])
        for key in ("seed", "chunk_size"):
            if opts.get(key) is not None:
                payload[key] = int(opts[key])
        if opts.get("split_text") is not None:
            payload["split_text"] = bool(opts["split_text"])
        _log.info(
            "HTTP TTS (chatterbox-native): %d Zeichen → %s (mode=%s, voice=%s, lang=%s)",
            len(text), endpoint, voice_mode, voice or "default", language or "-",
        )
    else:
        # OpenAI-compat (Kokoro, Chatterbox-OpenAI, LocalAI)
        payload = {
            "model": model or "kokoro",
            "input": text,
            "response_format": str(opts.get("response_format") or "wav"),
        }
        if voice:
            payload["voice"] = voice
        if language:
            payload["language"] = language
        if opts.get("speed") is not None:
            payload["speed"] = float(opts["speed"])
        if opts.get("seed") is not None:
            payload["seed"] = int(opts["seed"])
        _log.info(
            "HTTP TTS (openai-compat): %d Zeichen → %s (model=%s, voice=%s, lang=%s)",
            len(text), endpoint, payload["model"], voice or "default", language or "-",
        )

    resp = _httpx.post(endpoint, json=payload, timeout=120.0)
    resp.raise_for_status()
    wav = resp.content
    _log.info("HTTP TTS: %d bytes Audio empfangen", len(wav))
    return wav


def synthesize(
    text: str,
    url: str,
    voice: str | None = None,
    noise_scale: float | None = None,
    noise_w: float | None = None,
    length_scale: float | None = None,
    sentence_silence: float | None = None,
    model: str | None = None,
    language: str | None = None,
    **opts: Any,
) -> bytes:
    """Synthetisiert Text zu WAV-Audio.

    URL-Schema bestimmt das Backend:
      http:// / https://  → HTTP API (siehe _synthesize_http für die zwei Body-Schemas)
      tcp:// / wyoming:// → Wyoming TCP-Protokoll (Piper)

    HTTP-spezifische Optionen kommen via **opts durch (z.B. seed, speed,
    voice_mode, cfg_weight, exaggeration, temperature, chunk_size, split_text,
    response_format) — werden je nach Endpoint gefiltert.
    Piper-spezifische Optionen (Wyoming only):
      noise_scale, noise_w, length_scale, sentence_silence
    """
    if url.startswith("http://") or url.startswith("https://"):
        return _synthesize_http(text, url, voice=voice, model=model, language=language, **opts)

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
