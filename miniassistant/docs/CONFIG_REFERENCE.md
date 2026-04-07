# Config Structure Reference (for save_config)

## Your role when changing config

1. **If something is missing** (token, homeserver, device_id for Matrix; bot_token for Discord): ask the user for it and show the relevant example below. Do not write config until you have the required values.
2. **When the user provides the data:** use the **save_config** tool with the full YAML content. It validates the config, creates up to 4 backups (`.bak`, `.bak.1`, …), then writes. Do **not** use exec to write the config file. Preserve existing keys; only add or change what the user requested.
3. **After writing config:** tell the user they must **restart the service** (e.g. `miniassistant serve` or the process that runs the bot) for changes to take effect.

---

**save_config deep-merges your YAML into the existing config.** You only need to pass the keys you want to add or change. Existing keys are preserved.

```yaml
# === COMPLETE CONFIG STRUCTURE ===
providers:
  ollama:
    type: ollama                          # protocol type (ollama, openai, ...)
    base_url: http://127.0.0.1:11434    # Ollama API URL
    num_ctx: 8192                         # global context length (tokens)
    think: true                           # global thinking/reasoning (true/false)
    options:                              # global Ollama ModelOptions
      temperature: 0.7
      top_p: 0.9
      top_k: 40
    model_options:                        # per-model overrides (override global options)
      "qwen3:14b":
        num_ctx: 32768
      "llama3.1:8b":
        num_ctx: 4096
    models:
      default: qwen3:14b                 # default model name or alias
      aliases:                            # short names → real model name
        fast: llama3.2
        reasoning: deepseek-r1:14b
        coder: qwen2.5-coder:14b
      fallbacks: [llama3.2]              # try these on error
      subagents: false                    # enable invoke_model tool
  # Additional providers (optional) – each with its own base_url and type
  ollama2:                                # second Ollama instance (e.g. GPU server)
    type: ollama
    base_url: http://192.168.1.20:11434
    think: false
    options:
      temperature: 0.5
    model_options:
      "llama3.3:70b":
        num_ctx: 65536
    models:
      aliases:
        big: llama3.3:70b

server:
  host: 0.0.0.0
  port: 8765
  token: "secret"                         # auto-generated if missing
  debug: false
  show_estimated_tokens: false            # log token estimate before each call
  log_agent_actions: false                # log prompts, thinking, tool calls to logs/agent_actions.log
  show_context: false                     # log full context (system prompt, messages, tokens) to logs/context.log

agent_dir: ~/.config/miniassistant/agent
workspace: ~/workspace                  # default: ~/workspace
trash_dir: ~/.trash                     # default: ~/.trash (separate from workspace)
max_chars_per_file: 500                   # max chars per agent file in system prompt
max_tool_rounds: 100                      # max tool call rounds per chat message (default: 100)
github_token: github_pat_xxx...           # optional: any token format (ghp_..., github_pat_...) — injected as GH_TOKEN/GITHUB_TOKEN into every exec call

search_engines:
  main:
    url: https://search.example.org
  vpn:
    url: https://search-vpn.example.org
default_search_engine: main
search_engine_strategy: first             # 'first' (default), 'random', or 'specific'
                                          # first: use default engine; on no results, suggest others to user
                                          # random: pick a random engine each request
                                          # specific: always use default engine only, no fallback suggestion

scheduler: false                          # or { enabled: true }

chat_clients:
  matrix:
    enabled: true
    homeserver: https://matrix.example.org
    user_id: "@bot:example.org"
    token: "syt_..."
    device_id: "ABCDEFGHIJ"
    bot_name: MiniAssistant
    encrypted_rooms: true
  discord:
    enabled: true
    bot_token: "discord-bot-token"

memory:
  max_chars_per_line: 300
  days: 2
  max_tokens: 4000                         # Token-Budget für Memory im System-Prompt

chat:
  context_quota: 0.85                      # Anteil von num_ctx der genutzt wird (0.5–0.95). Bei Überschreitung: Smart Compacting

# Vision & Image (optional)
vision:
  model: "llava:13b"                     # Vision model for image analysis
  num_ctx: 32768                          # optional context size
# Short form also works: vision: "llava:13b"

image_generation:
  - "llama-swap/flux.2-klein-9b"          # Image generation model (list of model names)
  - "openai/dall-e-3"
# Short form: image_generation: "stable-diffusion"
# Provider with image_api for img2img backend selection:
#   providers:
#     sd-server:
#       type: openai-compat
#       base_url: http://127.0.0.1:8080
#       image_api: ""                       # "" = OpenAI-compat /v1/images/edits (default, works with sd-server/LocalAI)
#                                           # "a1111" = /sdapi/v1/img2img (for A1111/Forge/ComfyUI backends)

voice:
  stt:
    url: tcp://localhost:10300          # Wyoming STT server (e.g. faster-whisper)
  tts:
    url: tcp://localhost:10200          # Wyoming TTS server (e.g. Piper)
    # url: http://localhost:8880        # or HTTP API (Kokoro, LocalAI/VibeVoice)
    # model: vibevoice                  # TTS model name (optional, default: kokoro)
  language: de                          # STT + TTS language (default: de)
  tts_voice: de_DE-thorsten-medium      # Voice name (optional)

avatar: "~/.config/miniassistant/agent/avatar.png"  # Bot profile picture (path or URL)

onboarding_complete: true

subagents:                                 # global subagent list (worker models)
  - ollama-online/kimi
  - ollama-online/qwen3-coder-max

fallbacks:                                 # global fallback models on error
  - qwen3:14b

email:
  default: privat                          # which account to use when not specified
  accounts:
    privat:
      imap_server: imap.gmail.com
      imap_port: 993
      smtp_server: smtp.gmail.com
      smtp_port: 587
      username: ich@gmail.com
      password: app_passwort
      ssl: true
      name: Max Mustermann              # display name (optional, defaults to username)
    arbeit:
      imap_server: imap.firma.de
      imap_port: 993
      smtp_server: smtp.firma.de
      smtp_port: 587
      username: max@firma.de
      password: passwort
      ssl: true
```

## basic_rules (editable behavior rules)

Agent behavior rules are stored as editable Markdown files in `agent_dir/basic_rules/`:

```
agent_dir/basic_rules/
├── safety.md              # Safety rules, prompt injection defense, trash-before-delete
├── exec_behavior.md       # Exec rules (research first, one command at a time, …)
├── knowledge_verification.md  # Knowledge verification (web_search when uncertain)
├── language.md            # Response language (default: Deutsch)
└── subagent.md            # Subagent-specific rules and tool restrictions
```

- **Auto-created:** Default files are copied to `agent_dir/basic_rules/` on first start.
- **User-editable:** Files are never auto-updated. User changes are preserved.
- **Self-healing:** Deleted files are recreated from defaults on next start.
- **RAM-cached:** Files are loaded once at startup and cached in memory.
- **Extensible:** Additional `.md` files in `basic_rules/` are also loaded.

## Notes and preferences

- **User preferences (`prefs/`):** When the user says "remember..." or "save my preference for...", write a `.md` file to `agent_dir/prefs/`. These are loaded into context on every start.
- **Project notes:** When user says "make notes" / "mach dir Notizen": write summary to `agent_dir/prefs/notes-TOPIC.md`. When user says "check the notes" / "schau dir die Notizen an": read the relevant notes file.

## CRITICAL save_config rules — READ BEFORE EVERY save_config CALL

**DO NOT** rules (violations will be rejected by validation):
- **DO NOT** set `think` to a number. `think` is ONLY `true`, `false`, or `null`.
- **DO NOT** set `models.default` to a dict. It must be a string (model name or alias).
- **DO NOT** create aliases with parameters like `model,think=true`. Alias values are plain model names only.
- **DO NOT** invent option names. Only use options from the whitelist below.
- **DO NOT** add `model_options` for a model that is not in `models.aliases` (values), `models.list`, or `models.default`.
- **DO NOT** touch `models.aliases`, `models.list`, `models.default`, or `models.fallbacks` when the user only asks to change a model OPTION (like think, temperature, num_ctx). Model options go ONLY in `model_options`.

**ALWAYS quote model names containing `:` when used as YAML keys:**
```yaml
# CORRECT:
model_options:
  "qwen3:14b":
    think: true

# WRONG (will break YAML):
model_options:
  qwen3:14b:
    think: true
```

**Valid Ollama options** (for `options` and `model_options.<model>`):
`temperature`, `top_p`, `top_k`, `num_ctx`, `num_predict`, `seed`, `min_p`, `stop`, `repeat_penalty`, `repetition_penalty`, `repeat_last_n`, `tfs_z`, `mirostat`, `mirostat_eta`, `mirostat_tau`, `num_gpu`, `num_thread`, `numa`, `think`

## How to change model options (think, temperature, num_ctx, etc.)

**ALL model options go under `providers.<provider>.model_options."<model_name>"`.** This is the ONLY correct location. Never put options in aliases, default, or anywhere else.

User says: "enable thinking for qwen3-coder:30b" →
```yaml
providers:
  ollama:
    model_options:
      "qwen3-coder:30b":
        think: true
```

User says: "set temperature to 0.4 for qwen3:14b" →
```yaml
providers:
  ollama:
    model_options:
      "qwen3:14b":
        temperature: 0.4
```

User says: "set num_ctx to 32k and repeat_penalty to 1.1 for deepseek-r1:14b" →
```yaml
providers:
  ollama:
    model_options:
      "deepseek-r1:14b":
        num_ctx: 32768
        repeat_penalty: 1.1
```

User says: "disable thinking for llama3.1:8b" →
```yaml
providers:
  ollama:
    model_options:
      "llama3.1:8b":
        think: false
```

**Deep-merge**: save_config deep-merges your YAML into the existing config. If `model_options."qwen3-coder:30b"` already has `temperature: 0.7`, adding `think: true` preserves the existing temperature. You only pass what changes.

## Other save_config examples

**Add model aliases:**
```yaml
providers:
  ollama:
    models:
      aliases:
        fast: llama3.2
        coder: qwen2.5-coder:14b
```

**Change default model:**
```yaml
providers:
  ollama:
    models:
      default: qwen3:14b
```

**Enable thinking globally (all models):**
```yaml
providers:
  ollama:
    think: true
```

**Set global options:**
```yaml
providers:
  ollama:
    options:
      temperature: 0.7
      top_p: 0.9
```

**Add fallback models:**
```yaml
providers:
  ollama:
    models:
      fallbacks: [llama3.2, llama3.2:1b]
```

**Enable subagents:**
```yaml
providers:
  ollama:
    models:
      subagents: true
```

**Add a second Ollama provider:**
```yaml
providers:
  ollama2:
    type: ollama
    base_url: http://192.168.1.20:11434
    models:
      aliases:
        big: llama3.3:70b
      subagents: true
```
Models on the second provider are used with prefix: `ollama2/llama3.3:70b` or `ollama2/big`. Without prefix, the first provider (default) is used.

**Save GitHub token** (injected as `$GH_TOKEN` in every exec call — NEVER under `providers.github`):
```yaml
github_token: github_pat_xxx...
```

**Add a search engine:**
```yaml
search_engines:
  vpn:
    url: https://search-vpn.example.org
```

## Email setup (IMAP/SMTP)

Email uses the built-in `send_email` and `read_email` tools — no Python scripts needed.

**User says "richte mein Gmail ein" → ask for:** username (email address), password (App Password for Gmail), display name (optional). Then save:

```yaml
email:
  default: privat
  accounts:
    privat:
      imap_server: imap.gmail.com
      imap_port: 993
      smtp_server: smtp.gmail.com
      smtp_port: 587
      username: ich@gmail.com
      password: app_passwort_hier
      ssl: true
      name: Max Mustermann
```

**Add a second account** (keep existing, just add the new one):
```yaml
email:
  accounts:
    arbeit:
      imap_server: imap.firma.de
      imap_port: 993
      smtp_server: smtp.firma.de
      smtp_port: 587
      username: max@firma.de
      password: firmen_passwort
      ssl: true
```

**Change default account:**
```yaml
email:
  default: arbeit
```

**Provider quick reference:**
- Gmail: `imap.gmail.com` / `smtp.gmail.com`, ports 993/587, use App Password (not main password)
- Outlook/Office365: `outlook.office365.com` / `smtp.office365.com`, ports 993/587
- Port 465 (SSL-only): set `ssl: true` and `smtp_port: 465` — uses `SMTP_SSL` instead of STARTTLS

**CRITICAL:** Always use `send_email` and `read_email` tools — never write Python scripts for email. Credentials are loaded automatically from config. Read EMAIL.md for tool usage details.

## Voice setup (Wyoming STT + TTS)

Voice enables speech-to-text (STT) and text-to-speech (TTS) for Matrix and Discord.
Requires: `ffmpeg` system package + running Wyoming STT/TTS servers.

Three TTS backends supported:
- **Piper** via Wyoming protocol (`tcp://`) — classic, low resource, many voices
- **Kokoro** via HTTP API (`http://`) — high quality neural TTS, OpenAI-compat API
- **VibeVoice** via LocalAI (`http://`) — multi-speaker, expressive TTS (model: vibevoice)

**User says "richte Voice ein" or "konfiguriere Sprachausgabe" → ask for:**
- STT server URL (e.g. `tcp://localhost:10300` for faster-whisper)
- TTS server URL (e.g. `tcp://localhost:10200` for Piper, `http://localhost:8880` for Kokoro, `http://localhost:8080` for LocalAI) — optional, text-only reply if missing
- Language (default: `de`) — used for STT and TTS
- Voice name — optional (e.g. `de_DE-thorsten-medium` for Piper, `af_bella` for Kokoro, `Emma` for VibeVoice)
- TTS model — optional (only needed for HTTP backends, default: `kokoro`)

**Minimal voice setup (STT only):**
```yaml
voice:
  stt:
    url: tcp://localhost:10300
  language: de
```

**Full voice setup (STT + TTS with Piper):**
```yaml
voice:
  stt:
    url: tcp://localhost:10300
  tts:
    url: tcp://localhost:10200
  language: de
  tts_voice: de_DE-thorsten-medium
```

**Full voice setup (STT + TTS with VibeVoice via LocalAI):**
```yaml
voice:
  stt:
    url: tcp://localhost:10300
  tts:
    url: http://localhost:8080
    model: vibevoice
  language: de
  tts_voice: Emma
```

**Config keys:**
- `voice.tts.url` — TTS server URL (tcp:// for Wyoming, http:// for HTTP API)
- `voice.tts.model` — TTS model name (optional, default: `kokoro`, set to `vibevoice` for VibeVoice)
- `voice.language` — language for STT + TTS (default: `de`)
- `voice.tts_voice` — voice name (or `voice.tts.voice` as alternative)

**Wyoming server addresses:**
- `tcp://host:port` — standard form
- `wyoming://host:port` and `wyoming+tcp://host:port` are also accepted
- Default ports: faster-whisper = 10300, Piper = 10200 (configurable in the Wyoming server)

**How voice works:**
- Matrix: incoming `m.audio` messages → STT → agent → TTS → audio reply
- Discord: audio attachments (ogg, mp3, wav, m4a, webm) → STT → agent → TTS → WAV attachment
- Messages transcribed with `[Voice]` prefix so agent responds in spoken language (concise, no markdown)
- If TTS is not configured, agent replies in text
- Tables and code blocks are always sent as separate text messages

**Read VOICE.md for server setup instructions.**
