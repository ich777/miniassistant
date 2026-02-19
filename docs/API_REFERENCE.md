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

## Useful for schedules

In a schedule prompt, the assistant runs with full tool access. To trigger from bash (e.g. after a backup script):

```bash
curl -s -X POST http://localhost:8765/api/chat \
  -H "Authorization: Bearer SECRET" \
  -H "Content-Type: application/json" \
  -d '{"message": "Das Backup ist fertig. Bitte prüfe ob /backup/latest aktuell ist."}'
```
