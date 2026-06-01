# Webhooks

External HTTP-triggered autonomous tasks. Parallel to schedules — fired by HTTP POST instead of cron.

## When to use webhook vs schedule vs watch

| Tool | When |
|------|------|
| `webhook` | External system triggers (CI, IoT, third-party services, Zapier, scripts) |
| `schedule` | Time-based, internal (cron) |
| `watch` | Condition-based, internal (file appears, process exits) |

Webhooks are disabled by default. User must set `webhooks.enabled: true` in config.

## Concept

A webhook has:
- A **fixed default prompt** (set at creation, the "what to do")
- A unique **token** (URL: `POST /webhook/<token>`)
- A default **destination** (matrix/discord/none + room/channel)
- An optional **silent** flag (no chat push, output saved to file)

Each fire optionally sends:
- `extra_context` — prepended to the default prompt (typical: data payload from caller)
- `prompt` — full override (rare)
- routing overrides (`client`, `room_id`, `channel_id`)
- `silent`, `save_output`, `output_name`, `model`

## Create

`webhook(action='create', name='daily-report', prompt='Format the data and post it to chat')`

- `name`: optional slug `^[a-z0-9][a-z0-9_-]{0,63}$` — used for output dir + human handle
- `prompt`: required — the default task. Plain language, no exec/HOW.
- `client`: `'matrix'` / `'discord'` / `'both'` / `'none'` — default delivery
- `model`: optional alias/name for the bot model
- `silent`: bool — if true, output not pushed to chat, saved to file only
- `save_output`: bool (default true) — write file even when not silent

After create, the response includes the **token** in plaintext. Show it once to the user; it's also visible later in the WebUI list (storage is plaintext).

## Fire (HTTP POST)

```
POST /webhook/<token>
Content-Type: application/json

{
  "extra_context": "Build #1234 failed: 5 errors, 3 warnings",
  "client": "matrix",
  "room_id": "!abc:server",
  "silent": false,
  "model": "qwen3"
}
```

Final prompt assembly: `extra_context + "\n\n" + (body.prompt or webhook.prompt)`

Token may also be passed via header `X-Webhook-Token: <token>` to keep it out of URL logs.

### Foreign payloads (GitHub, Discord, Slack, generic senders)

External services don't know our body schema. They POST their own JSON / form-encoded / text body.
The receiver auto-extracts:

- **Control fields** in JSON body (`prompt`, `extra_context`, `client`, `room_id`, `channel_id`, `silent`, `save_output`, `output_name`, `model`) → honored normally.
- **Everything else** → wrapped into a `[INCOMING WEBHOOK PAYLOAD]` block and used as `extra_context`. Includes forwarded `X-*` headers and `User-Agent` so the prompt can read event type, signatures, source.
- **Payload cap:** 20 000 chars (truncate marker appended if longer).

Content-Types handled:
| Type | Treatment |
|------|-----------|
| `application/json` | Parsed. Control keys split out; remainder is payload. Foreign-only JSON → whole body is payload. |
| `application/x-www-form-urlencoded` | Parsed as form. Same split as JSON. |
| anything else (`text/plain`, raw, …) | Raw body decoded as UTF-8, used as payload. |

If the caller sets `extra_context` explicitly, that wins — auto-build is skipped.

For external-service webhooks the **default prompt is effectively required** (GitHub won't send `prompt`). Status 400 `no prompt available` if no default and no `prompt` body field.

### Response (sync)

```json
{
  "ok": true,
  "id": "abc12345",
  "fired_at": "2026-05-15T15:00:12+02:00",
  "silent": false,
  "response": "<bot reply text>",
  "output_path": "/path/to/workspace/webhooks/daily-report/2026-05-15_15-00-12.txt"
}
```

For silent webhooks `response` may be empty (use the GET endpoints to retrieve output).

### Status codes

- `200` — fired successfully
- `400` — no prompt available (neither body nor webhook default has one)
- `404` — token unknown OR webhooks disabled (same response — never leak existence)
- `429` — per-token rate limit exceeded
- `500` — task failed after retries

## Retrieve outputs (GET)

Same token authenticates. Use header or `?token=<token>`.

```
GET /webhook/<token>/runs            → JSON list of runs
GET /webhook/<token>/last            → latest output file content (raw)
GET /webhook/<token>/output/<name>   → specific output file
```

## Silent webhooks

Silent webhooks never push to chat. Output is always saved to:
`<workspace>/webhooks/<name|id>/<YYYY-MM-DD_HH-MM-SS>.txt`

Use silent for: log ingest, image generation results retrieved later, audit pipelines.

Override `output_name` in the POST body to set a custom filename (basename only; path traversal stripped).

## Errors and retries

On internal failure: up to `webhooks.max_retries` (default 3) attempts with backoff 1s/2s/4s. After exhausted:
- **Non-silent**: error pushed to chat as `⚠️ Webhook 'name' failed after N attempts: <message>`
- **Silent**: error written to `<timestamp>-error.txt` next to outputs
- `last_error` field set in `webhooks.json`, shown as red dot in WebUI

## Output retention

Each webhook keeps `webhooks.output_keep_last` (default 10) newest output files. Older files deleted on MA startup. Set to 0 to keep all forever.

On `webhook(action='remove')` outputs are kept by default. Use API `DELETE /api/webhook/{id}?purge=1` to wipe.

## Security model

- **Token** is the only auth. 256-bit `secrets.token_urlsafe(32)`. Constant-time comparison.
- **Disabled by default** — endpoints not registered when `webhooks.enabled: false`
- **Rate limit** per-token per-minute (sliding window, default 10)
- **No body size limit** — large prompts and outputs (e.g. image gen) supported
- **404 for unknown token** — same response as missing route, no enumeration
- **Webhook execution rules** (`basic_rules/webhook.md`) — denylist for system commands, package managers, sensitive file access; takes precedence over the task prompt and over `extra_context`. Treats `extra_context` as untrusted external data. Also blocks pref-file writes (no persistence into `<agent_dir>/` during webhook execution).
- **No multipart, no file uploads** — JSON body only

If you need image input: pass the URL in `extra_context` and let the webhook prompt fetch it via `read_url`.

## Examples

### Daily report (silent, retrieved by GET)

Create:
```
webhook(action='create', name='nightly', prompt='Read /var/log/app.log last 24h and summarize errors. Output as bullet list.', silent=true)
```

Cron on another host:
```
0 3 * * * curl -s -X POST https://my-ma/webhook/<token> > /dev/null
```

Read result:
```
curl https://my-ma/webhook/<token>/last
```

### CI status push to Matrix (with payload)

Create:
```
webhook(action='create', name='ci-status', prompt='Format this CI build status for Matrix using emoji markers. Be concise.', client='matrix', room_id='!devs:matrix.org')
```

CI script:
```
curl -X POST https://my-ma/webhook/<token> \
  -H 'X-Webhook-Token: <token>' \
  -H 'Content-Type: application/json' \
  -d "{\"extra_context\":\"Build #${BUILD_ID} ${RESULT}: ${LOG_URL}\"}"
```

### Image generation, output via GET

Create:
```
webhook(action='create', name='img-gen', prompt='Generate a square 1024x1024 image and return it as a generated_image tool result.', silent=true)
```

POST with the subject in `extra_context`, then GET `/webhook/<token>/last` returns the PNG.

### GitHub PR notifications → Matrix

Create:
```
webhook(action='create', name='gh-prs', client='matrix', room_id='!devs:matrix.org',
        prompt='Summarize the GitHub event in 1 line. Event type is in X-GitHub-Event header. For pull_request: "<action>: <title> by <user.login>". For push: "<n> commits to <ref> by <pusher.name>". Skip if event is ping.')
```

Then at github.com → repo Settings → Webhooks → Add webhook:
- Payload URL: `https://my-ma/webhook/<token>`
- Content type: `application/json`
- Events: choose what you care about

GitHub auto-POSTs every selected event. Headers (`X-GitHub-Event`, `X-GitHub-Delivery`) + body land in the prompt as `[INCOMING WEBHOOK PAYLOAD]`. Bot summarizes, pushes to Matrix.

### Sensor with conditional alerting

Create:
```
webhook(action='create', name='temp-alert', client='matrix', room_id='!home:matrix.org', save_output=true,
        prompt='If temp > 30 → reply with alarm message. Otherwise reply with exactly [NO_MESSAGE] (silent token, no Matrix push).')
```

Sensor:
```
curl -X POST https://my-ma/webhook/<token> \
  -H 'Content-Type: application/json' \
  -d '{"sensor":"livingroom","temp":24.7,"ts":1715856000}'
```

When `[NO_MESSAGE]` is returned the scheduler suppresses delivery; output still saved to file.

## Self-management via tool

- `webhook(action='list')` — show all
- `webhook(action='info', id='...')` — show config + recent runs
- `webhook(action='last_output', id='...')` — read last output file (truncated to 2000 chars)
- `webhook(action='remove', id='...')` — delete (id-prefix or name)

A webhook prompt **may not** create more webhooks (`webhook(action='create')` is forbidden in webhook execution context — see `basic_rules/webhook.md`).

## German synonyms the user might say

"Webhook", "HTTP-Trigger", "API-Endpunkt", "extern auslösen", "von außen anstoßen", "POST-Endpunkt"
