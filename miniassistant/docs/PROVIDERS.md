# Providers (multiple Ollama instances, Ollama Online, etc.)

Providers are configured under `providers:` in the config. Each provider has a `type`, `base_url`, and optionally an `api_key` for authenticated endpoints (e.g. Ollama Online).

## Example: Local + Ollama Online

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    num_ctx: 32768
    models:
      default: qwen3:14b
      aliases:
        fast: qwen3:14b
  ollama-online:
    type: ollama
    base_url: https://api.ollama.com
    api_key: YOUR_OLLAMA_API_KEY
    num_ctx: 32768
    models:
      default: llama3.3:70b
      aliases:
        big: llama3.3:70b
```

Use `provider/model` syntax to target a specific provider: `/model ollama-online/big`.

## Example: Anthropic API (Claude as subagent)

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
      subagents: true
  anthropic:
    type: anthropic
    api_key: sk-ant-api03-YOUR-KEY
    think: true                    # Extended Thinking
    models:
      default: claude-sonnet-4-20250514
      aliases:
        sonnet: claude-sonnet-4-20250514
        opus: claude-opus-4-20250514
```

Supported provider types:
- **`ollama`** — Local or remote Ollama instances (default)
- **`anthropic`** — Anthropic Messages API (requires `api_key`, supports Extended Thinking, live model listing)
- **`claude-code`** — Claude Code CLI (`claude --print`, auth via `claude login`, has own tools)

## CLI: Provider management

```bash
miniassistant providers list              # Show all providers
miniassistant providers add myserver      # Add new provider (interactive)
miniassistant providers edit myserver     # Edit provider settings
miniassistant providers delete myserver   # Remove provider

# Model management per provider:
miniassistant providers models ollama --online           # List available models
miniassistant providers models ollama --add llama3.1:8b  # Add model
miniassistant providers models ollama --remove llama3.1:8b
miniassistant providers models ollama --default qwen3:14b
miniassistant providers models ollama --alias fast qwen3:14b
miniassistant providers models ollama --remove-alias fast
```

---

## Global fallback models

If the primary model fails (timeout, unreachable), fallback models are tried in order.

```yaml
fallbacks:
  - chipspc/llama-chips
  - qwen3:14b
```

Fallbacks are **not used for subagent calls** (subagents have their own error handling).
