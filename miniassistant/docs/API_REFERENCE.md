# API Reference (for scripts, schedules, and automation)

All endpoints require `Authorization: Bearer <TOKEN>` header (same token as in `server.token`).
Base URL: `http://<HOST>:<PORT>` (from `server.host` and `server.port` in config).

## Send a prompt (non-streaming)

```bash
curl -s -X POST http://localhost:8765/api/chat \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "Wie wird das Wetter morgen?"}'
```

Response: `{ "response": "...", "session_id": "...", "thinking": "..." }`

Use `session_id` in subsequent requests to continue the conversation.

## Send a prompt (streaming)

```bash
curl -s -N -X POST http://localhost:8765/api/chat/stream \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "Erkläre Docker", "session_id": "optional-id"}'
```

Returns NDJSON lines: `{"type":"thinking","content":"..."}`, `{"type":"content","content":"..."}`, `{"type":"done",...}`.

## Switch model

```bash
curl -s -X POST http://localhost:8765/api/chat \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "/model deepseek"}'
```

## List models

```bash
curl -s http://localhost:8765/api/ollama/models \
  -H "Authorization: Bearer SECRET"
```

## OpenAI-kompatible API

MiniAssistant bietet unter `/v1/` eine OpenAI-kompatible Schnittstelle.
Damit funktionieren Tools wie Open WebUI, Continue.dev, Cursor und das openai Python SDK direkt.

```bash
# Modelle auflisten (inkl. Aliases/Kurznamen)
curl -s http://localhost:8765/v1/models \
  -H "Authorization: Bearer SECRET"

# Chat Completion (model kann ein Alias wie "fast" sein)
curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "Hallo!"}]}'

# Streaming
curl -s -N http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "stream": true, "messages": [{"role": "user", "content": "Hallo!"}]}'
```

Der Agent-Kontext (SOUL, IDENTITY, TOOLS, USER, Memory) wird automatisch als System-Prompt vorgeschaltet.
Details: [OPENAI_API.md](../../OPENAI_API.md)

## Client-side tool execution

When using `local_tools` in `/api/chat/stream`, the server may send `tool_request` events. The client executes the tool locally and returns the result:

```bash
curl -s -X POST http://localhost:8765/api/chat/tool_result \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"tool_id": "uuid-from-tool-request", "result": "command output here", "session_id": "your-session-id"}'
```

- `tool_id` (required): The ID from the `tool_request` event
- `result` (required): Tool execution output (max 100 KB, truncated if larger)
- `session_id` (recommended): Must match the session that triggered the request

Timeout: 60 seconds. If no result is received, the server executes the tool itself as fallback.

## Useful for schedules

In a schedule prompt, the assistant runs with full tool access. To trigger from bash (e.g. after a backup script):

```bash
curl -s -X POST http://localhost:8765/api/chat \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "Das Backup ist fertig. Bitte prüfe ob /backup/latest aktuell ist."}'
```
