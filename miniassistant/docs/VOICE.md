# Voice (STT + TTS)

Voice input and output on all platforms (Matrix, Discord, Web UI, OpenAI-compatible API).
STT transcribes incoming audio. TTS generates spoken replies via `send_audio`. Both are optional and independent.
**`send_audio` works on every platform** — including Web and API clients. Always use the tool, never output `<audio>` HTML tags directly.

## Rules for voice replies

Read this section before using `send_audio` or replying to `[Voice]` messages.

**send_audio(text="...")** sends a voice reply. The text MUST be plain spoken language:
- No emojis. No markdown. No symbols. No URLs. No coordinates. No technical IDs.
- Short: 1-3 sentences. Only say what a human would say out loud.
- Dense data (coordinates, specs, addresses) goes in a follow-up text message, not in audio.
- After successful send_audio: **no text reply.** No confirmation. No "Technischer Hinweis".

**Incoming voice** (message starts with `[Voice]`): same rules. Plain, short, no formatting.

### Text rewrite rules — apply BEFORE calling send_audio

| Written form | Spoken form |
|---|---|
| Decimal comma: `3,5` / `0,25` | `3 Komma 5` / `0 Komma 25` (German decimal separator — never leave bare `,` in numbers) |
| Number ranges: `10-20` / `Mo-Fr` / `8-17 Uhr` | `10 bis 20` / `Montag bis Freitag` / `8 bis 17 Uhr` (any `-` between two values = `bis`. Compounds like `E-Mail`, `WLAN-Passwort` stay) |
| Times: `14:30` | `14 Uhr 30` |
| Ordinals (dates): `12. Mai` / `am 1. Januar` / `3. Stock` | `zwölfter Mai` / `am ersten Januar` / `dritter Stock` (digit + `.` after number = ordinal — spell out: 1.→erster, 2.→zweiter, 3.→dritter, 4.→vierter, 5.→fünfter, 6.→sechster, 7.→siebter, 8.→achter, 9.→neunter, 10.→zehnter, 11.→elfter, 12.→zwölfter, 13.→dreizehnter, 20.→zwanzigster, 21.→einundzwanzigster, 30.→dreißigster, 31.→einunddreißigster. Adjust ending by case: "am 12." → "am zwölften") |
| Temperature: `20°C` / `-5°C` / `68°F` | `20 Grad Celsius` / `minus 5 Grad Celsius` / `68 Grad Fahrenheit` |
| Speed: `km/h` / `m/s` / `mph` | `Kilometer pro Stunde` / `Meter pro Sekunde` / `Meilen pro Stunde` |
| Length: `km` / `m` / `cm` / `mm` | `Kilometer` / `Meter` / `Zentimeter` / `Millimeter` |
| Weight: `kg` / `g` / `mg` / `t` | `Kilogramm` / `Gramm` / `Milligramm` / `Tonnen` |
| Volume: `l` / `ml` / `hl` | `Liter` / `Milliliter` / `Hektoliter` |
| Area: `m²` / `km²` / `ha` | `Quadratmeter` / `Quadratkilometer` / `Hektar` |
| Volume³: `m³` / `cm³` | `Kubikmeter` / `Kubikzentimeter` |
| Power/Energy: `W` / `kW` / `kWh` / `PS` | `Watt` / `Kilowatt` / `Kilowattstunden` / `PS` |
| Data: `MB` / `GB` / `TB` / `Mbit/s` | `Megabyte` / `Gigabyte` / `Terabyte` / `Megabit pro Sekunde` |
| Currency: `€` / `$` / `CHF` / `12,50 €` | `Euro` / `Dollar` / `Schweizer Franken` / `12 Euro 50` |
| Percent: `25%` / `0,5%` | `25 Prozent` / `0 Komma 5 Prozent` |
| Slashes (general): `und/oder` | `und oder` |
| `z.B.` | `zum Beispiel` |
| `d.h.` | `das heisst` |
| `ca.` | `circa` |
| `usw.` | `und so weiter` |
| `bzw.` | `beziehungsweise` |
| `etc.` | `et cetera` |
| Numbered lists: `1. ... 2. ...` | `Erstens ... Zweitens ...` |

**Rule of thumb:** if a symbol (`,` `-` `°` `/` `%` `€`) sits between or after digits, expand it to a German word. Never let the TTS see a bare symbol next to a number.

Tables and code blocks are automatically extracted and sent as separate text message.

## Setup

**Requirements:** `ffmpeg` (added by `install.sh`). A running STT and/or TTS server.

### STT — faster-whisper (recommended)
```bash
# Docker
docker run -it -p 10300:10300 rhasspy/wyoming-faster-whisper --model tiny-int8 --language de
# Or pip
pip install wyoming-faster-whisper
wyoming-faster-whisper --uri tcp://0.0.0.0:10300 --model tiny-int8 --language de
```

### TTS option A — Piper (lightweight, many voices)
```bash
docker run -it -p 10200:10200 rhasspy/wyoming-piper --voice de_DE-thorsten-medium
```
Voices: https://github.com/rhasspy/piper/blob/master/VOICES.md

### TTS option B — Kokoro (high quality neural)
```bash
docker run -it -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:v0.2.1
```
Voices: `af_bella`, `af_sarah`, `af_sky`, `af_nicole`, `am_adam`, `am_michael`, `bf_emma`, `bm_george`, `bm_lewis`

## Config

```yaml
# Piper (Wyoming protocol):
voice:
  stt:
    url: tcp://localhost:10300
  tts:
    url: tcp://localhost:10200
  language: de
  tts_voice: de_DE-thorsten-medium

# Kokoro (HTTP API):
voice:
  stt:
    url: tcp://localhost:10300
  tts:
    url: http://localhost:8880
  tts_voice: af_bella

# LocalAI / VibeVoice:
voice:
  tts:
    url: http://localhost:8080/tts
    model: vibevoice
    voice: Emma

# Chatterbox-TTS-Server (native /tts endpoint):
# llama-swap healthcheck: use `checkEndpoint: /v1/audio/voices` (tagged
# "llama-swap Compatible" by chatterbox itself). NOT `/health` — doesn't exist.
# `/` works but renders HTML every probe.
voice:
  tts:
    url: http://localhost:8004/tts
    # Behind llama-swap: use /upstream/<model-name>/tts so swap can route by path:
    # url: http://swap-host:8080/upstream/chatterbox-multilingual/tts
    voice: siri.wav
    voice_mode: clone           # or "predefined"
    language: de
    cfg_weight: 0.6
    exaggeration: 1.4
    temperature: 1.0
    chunk_size: 240
    split_text: true
    seed: 42                    # set >0 for stable cloned voice
    response_format: wav        # → output_format

# Chatterbox via OpenAI-compat endpoint (limited params):
voice:
  tts:
    url: http://localhost:8004/v1/audio/speech
    model: chatterbox-multilingual
    voice: siri.wav
    seed: 42                    # only: model, voice, response_format, speed, seed accepted
```

Backend auto-detected from URL scheme + path:
- `tcp://` / `wyoming://` → Wyoming/Piper
- HTTP path ends `/tts` → Chatterbox-native (`CustomTTSRequest`), forwards `voice_mode`, `cfg_weight`, `exaggeration`, `temperature`, `chunk_size`, `split_text`, `seed`, `speed_factor`, `language`
- HTTP path ends `/audio/speech` or no path → OpenAI-compat. Only `model`, `voice`, `response_format`, `speed`, `seed`, `language` forwarded — extra keys are silently dropped by the server.

Restart after config changes.

## How it works

1. Incoming audio → ffmpeg → 16kHz mono PCM → STT → transcript
2. Transcript sent to agent with `[Voice]` prefix
3. Agent responds → TTS → WAV → audio message back
4. Tables and code blocks extracted and sent as separate text

Supported input: anything ffmpeg decodes (ogg, mp3, wav, m4a, webm, flac, aac, ...).

## Troubleshooting

| Problem | Fix |
|---|---|
| "Sprachfunktion nicht konfiguriert" | `voice.stt.url` missing in config |
| "Spracherkennung fehlgeschlagen" | STT server unreachable or ffmpeg missing |
| "Konnte Sprachnachricht nicht erkennen" | Empty transcript — try different model or check language |
| TTS fails, text sent instead | TTS server unreachable — check `voice.tts.url` |
