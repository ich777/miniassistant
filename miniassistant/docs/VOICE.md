# Voice (STT + TTS)

MiniAssistant supports voice messages on Matrix and Discord.

- **STT** (speech-to-text): transcribes incoming audio — recommended: [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) or [wyoming-faster-whisper](https://github.com/rhasspy/wyoming-faster-whisper)
- **TTS** (text-to-speech): two backends supported:
  - **Piper** via Wyoming protocol (`tcp://`) — classic, low resource, many voices
  - **Kokoro** via HTTP API (`http://`) — high quality neural TTS, OpenAI-compat API

Both STT and TTS are optional independently: STT-only → voice input with text reply. TTS-only is not useful.

## Requirements

- `ffmpeg` installed on the system (added automatically by `install.sh`)
- Running Wyoming STT and/or TTS server(s)

## Quick start with Docker

**STT (faster-whisper):**
```bash
docker run -it -p 10300:10300 \
  -v ~/.cache/faster-whisper:/root/.cache/huggingface \
  rhasspy/wyoming-faster-whisper \
  --model tiny-int8 --language de
```

**TTS Option A — Piper (Wyoming):**
```bash
docker run -it -p 10200:10200 \
  -v ~/.local/share/voices:/voices \
  rhasspy/wyoming-piper \
  --voice de_DE-thorsten-medium
```
Download Piper voices from: https://github.com/rhasspy/piper/blob/master/VOICES.md

**TTS Option B — Kokoro (HTTP API):**
```bash
docker run -it -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:v0.2.1
# GPU variant:
docker run -it -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-gpu:v0.2.1
```
Kokoro voices: `af_bella`, `af_sarah`, `af_sky`, `af_nicole`, `am_adam`, `am_michael`, `bf_emma`, `bm_george`, `bm_lewis`

## Quick start without Docker

**wyoming-faster-whisper:**
```bash
pip install wyoming-faster-whisper
wyoming-faster-whisper --uri tcp://0.0.0.0:10300 --model tiny-int8 --language de
```

**wyoming-piper:**
```bash
pip install wyoming-piper
# Download a voice first (see VOICES.md link above)
wyoming-piper --uri tcp://0.0.0.0:10200 --piper /path/to/piper --voice de_DE-thorsten-medium
```

**Kokoro-FastAPI (pip):**
```bash
pip install kokoro-fastapi
python -m kokoro_fastapi --port 8880
```

## Config

Add to your `config.yaml` (or tell the agent: "richte Voice ein"):

**With Piper (Wyoming):**
```yaml
voice:
  stt:
    url: tcp://localhost:10300      # Wyoming STT server
  tts:
    url: tcp://localhost:10200      # Wyoming TTS (Piper)
  language: de                      # STT language hint (ISO 639-1)
  tts_voice: de_DE-thorsten-medium  # Piper voice name
```

**With Kokoro:**
```yaml
voice:
  stt:
    url: tcp://localhost:10300      # Wyoming STT server (unchanged)
  tts:
    url: http://localhost:8880      # Kokoro-FastAPI HTTP server
  language: de
  tts_voice: af_bella              # Kokoro voice name (see voices below)
```

The TTS backend is auto-detected from the URL scheme: `tcp://` → Piper/Wyoming, `http://` → Kokoro/HTTP-API.

Restart the service after saving. The agent can configure this via `save_config`.

## How it works

1. **Incoming audio** (Matrix `m.audio` or Discord audio attachment)
2. `ffmpeg` converts to 16 kHz 16-bit mono PCM
3. Sent to Wyoming STT → transcript
4. Transcript sent to agent with `[Voice]` prefix → agent responds in spoken style
5. Response sent to Wyoming TTS → WAV audio
6. WAV sent back as audio message/attachment
7. Tables and code blocks are always sent as separate text

## Text formatting for send_audio

When calling `send_audio(text="...")`, the text must be **plain spoken language** — no emojis, no markdown, no symbols, no coordinates, no URLs, no technical IDs or long numbers (they are read aloud literally and are meaningless as speech). Only include what a human would naturally say out loud.

Dense data (coordinates, specs, addresses) goes in a **follow-up text message**, not in the audio.

**Rewrite rules for spoken German — apply BEFORE calling send_audio:**

| Written form | Spoken form |
|-------------|-------------|
| Hyphens between numbers: `10-20` | `10 bis 20` (compound words like `E-Mail` stay) |
| Times: `14:30` | `14 Uhr 30` |
| Slashes: `km/h` | `Kilometer pro Stunde` |
| `und/oder` | `und oder` |
| `z.B.` | `zum Beispiel` |
| `d.h.` | `das heißt` |
| `ca.` | `circa` |
| `usw.` | `und so weiter` |
| Numbered lists: `1. … 2. …` | `Erstens … Zweitens …` |

**After a successful `send_audio`: no text reply, no confirmation, no "Technischer Hinweis".**

## Supported audio formats (incoming)

Anything `ffmpeg` can decode: ogg, mp3, wav, m4a, webm, flac, aac, …

Matrix voice messages are typically OGG Opus — supported natively.
Discord voice messages are typically OGG Opus as well.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Sprachfunktion nicht konfiguriert" | `voice.stt.url` missing in config |
| "Spracherkennung fehlgeschlagen" | STT server not reachable or ffmpeg not installed |
| "Konnte Sprachnachricht nicht erkennen" | Empty transcript — try a different model or check language setting |
| TTS fails, text reply sent instead | TTS server not reachable — check `voice.tts.url` |
| Audio not playing in Matrix | Verify `m.audio` support in your Matrix client |
