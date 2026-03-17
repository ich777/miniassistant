# OpenAI-kompatible API

MiniAssistant stellt unter `/v1/` eine OpenAI-kompatible Schnittstelle bereit.
Damit kann jedes Tool, das die OpenAI-API unterstuetzt, direkt mit MiniAssistant kommunizieren --
inklusive Agent-Kontext (SOUL, IDENTITY, TOOLS, USER, Memory).

## Endpunkte

| Methode | Pfad | Beschreibung |
|---------|------|-------------|
| GET | `/v1/models` | Alle konfigurierten Modelle + Aliases auflisten |
| GET | `/v1/models/{id}` | Details zu einem Modell |
| POST | `/v1/chat/completions` | Chat-Completion (Streaming + Non-Streaming) |

## Authentifizierung

Gleicher Token wie fuer alle anderen API-Endpunkte (`server.token` in der Config):

```
Authorization: Bearer DEIN_TOKEN
```

## Modelle und Kurznamen

Alle in der Config unter `providers.*.models` definierten Modelle und **Aliases** sind verfuegbar.
Statt des vollen Modellnamens reicht der Alias:

```yaml
# config.yaml
providers:
  ollama:
    type: ollama
    models:
      default: qwen3:14b
      aliases:
        fast: qwen3:14b
        code: qwen2.5-coder:14b
  anthropic:
    type: anthropic
    api_key: sk-ant-...
    models:
      aliases:
        sonnet: claude-sonnet-4-20250514
        opus: claude-opus-4-20250514
```

Damit funktionieren in der API sowohl `qwen3:14b` als auch `fast` als model-Parameter.
Fuer Modelle anderer Provider: `anthropic/sonnet` oder `anthropic/claude-sonnet-4-20250514`.

## Beispiele

### Modelle auflisten

```bash
curl -s http://localhost:8765/v1/models \
  -H "Authorization: Bearer DEIN_TOKEN"
```

Antwort:
```json
{
  "object": "list",
  "data": [
    {"id": "qwen3:14b", "object": "model", "created": 1710000000, "owned_by": "ollama"},
    {"id": "fast", "object": "model", "created": 1710000000, "owned_by": "ollama"},
    {"id": "code", "object": "model", "created": 1710000000, "owned_by": "ollama"},
    {"id": "anthropic/sonnet", "object": "model", "created": 1710000000, "owned_by": "anthropic"}
  ]
}
```

### Chat (Non-Streaming)

```bash
curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer DEIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fast",
    "messages": [
      {"role": "user", "content": "Was ist Docker?"}
    ]
  }'
```

Antwort:
```json
{
  "id": "chatcmpl-abc123...",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "fast",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "Docker ist ..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 12, "completion_tokens": 85, "total_tokens": 97}
}
```

### Chat (Streaming / SSE)

```bash
curl -s -N http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer DEIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fast",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Erklaere Kubernetes kurz"}
    ]
  }'
```

Streaming-Antwort (Server-Sent Events):
```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"},"index":0,"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{"content":"Kubernetes"},"index":0,"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{"content":" ist ..."},"index":0,"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"delta":{},"index":0,"finish_reason":"stop"}]}

data: [DONE]
```

### Ohne Modell-Angabe (Default)

Wird kein `model` angegeben, wird das Default-Modell des ersten Providers verwendet:

```bash
curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer DEIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hallo!"}]}'
```

## Agent-Kontext

**Jede Anfrage** ueber `/v1/chat/completions` bekommt automatisch den vollstaendigen
Agent-Kontext als System-Prompt vorgeschaltet:

- Grundregeln (Sicherheit, Sprache, Exec-Verhalten)
- System-Informationen (OS, Datum, Uhrzeit)
- SOUL.md, IDENTITY.md, TOOLS.md, USER.md, AGENTS.md
- Memory (letzte 2 Tage)
- Gespeicherte Praeferenzen

Dadurch verhaelt sich das Modell immer wie der konfigurierte Agent -- egal welches
Tool die Anfrage schickt.

Optionaler `system`-Message in den `messages` wird als "Additional Instructions"
an den Agent-Prompt angehaengt (nicht ersetzt).

## Reasoning/Thinking

Bei Modellen mit Thinking-Unterstuetzung (z.B. DeepSeek-R1, Claude mit Extended Thinking)
wird `reasoning_content` im Response mitgeliefert:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "Die Antwort ...",
      "reasoning_content": "Lass mich ueberlegen ..."
    }
  }]
}
```

Im Streaming-Modus kommt `reasoning_content` als Delta im Chunk.

## Integration mit externen Tools

### Open WebUI

```
Einstellungen > Connections > OpenAI API
  URL:     http://DEINE_IP:8765/v1
  API Key: DEIN_TOKEN
```

### Continue.dev (VS Code)

```json
{
  "models": [{
    "title": "MiniAssistant",
    "provider": "openai",
    "model": "fast",
    "apiBase": "http://localhost:8765/v1",
    "apiKey": "DEIN_TOKEN"
  }]
}
```

### Cursor

```
Settings > Models > OpenAI API Base
  URL:     http://localhost:8765/v1
  API Key: DEIN_TOKEN
  Model:   fast
```

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8765/v1",
    api_key="DEIN_TOKEN",
)

response = client.chat.completions.create(
    model="fast",
    messages=[{"role": "user", "content": "Hallo!"}],
)
print(response.choices[0].message.content)
```

### Python (openai SDK, Streaming)

```python
stream = client.chat.completions.create(
    model="fast",
    messages=[{"role": "user", "content": "Erklaere Docker"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)
```

## Einschraenkungen

- **Kein Tool-Calling ueber die OpenAI-API**: Die internen Tools (exec, web_search, etc.)
  werden nicht ueber die OpenAI-kompatible Schnittstelle exponiert. Das ist beabsichtigt --
  Tools laufen nur ueber die native MiniAssistant-API (`/api/chat`).
- **Kein Multi-Turn State**: Jede Anfrage ist stateless. Fuer Multi-Turn-Konversationen
  muessen alle bisherigen Messages mitgeschickt werden (wie bei der echten OpenAI-API).
- **Keine Embeddings/Images/Audio**: Nur `/v1/chat/completions` und `/v1/models` sind implementiert.
- **Token-Schaetzung**: Die `usage`-Werte sind grobe Schaetzungen (kein echter Tokenizer).
