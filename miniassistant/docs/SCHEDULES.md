# Schedules

Scheduled tasks are managed by the **schedule** tool and stored in `schedules.json` (config directory).

## wait vs watch vs schedule

| Tool | When to use | Duration |
|------|-------------|----------|
| `wait` | Need result **in this conversation** — pause then continue automatically | ≤10 min |
| `watch` | Background monitoring — notify when condition met (file, PID, command) | Any |
| `schedule` | Future or recurring task — runs in its own fresh session | Any |

**`wait` example:** You ran `nohup make build &` and want to check the result in 3 minutes.
→ `wait(seconds=180, reason="Build fertig")` — the session stays open, you continue after.

**`watch` example:** You started a download and don't know when it'll finish.
→ `watch(check="file_size_stable:/tmp/file.iso", message="Download fertig!")` — background, notifies when stable.

**`schedule` example:** User wants a daily weather report at 8:00.
→ `schedule(action='create', when='0 8 * * *', prompt='...')` — separate session, recurring.

## Create
`schedule(action='create', when='30 7 * * *', prompt='...', once=false)`
- `when`: 5 cron fields in system time (e.g. `0 8 * * *` = 08:00 daily) or `'in 30 minutes'`
- `prompt`: plain language task — the bot re-executes this fresh each time
- `once`: true = run once then delete (for reminders, one-time notifications)
- `client`: `'matrix'` / `'discord'` (default: room/channel where schedule was created)
- `model`: model alias to use (only set if user explicitly requests it)

## Prompt rules — critical
- **Plain language only for simple tasks.** Write WHAT to do, not HOW: `'List open issues from GitHub repo OWNER/REPO via GitHub API'` ✅
- **For API calls or exec tasks: be explicit.** The schedule runs in a new session with no memory of the current conversation. If you discovered the right endpoint, command, or approach in this session — encode it directly into the prompt. See "Prompt engineering for schedules" below.
- **Never copy a result into prompt.** The bot fetches fresh data at execution time.
- **Never show a preview** of what the result might look like — the schedule hasn't run yet.
- **Simple message** (user says "schick mir X um Y Uhr"): `prompt='Send this exact message to the user: "X"'` — NOT just `"X"` alone or the bot will reply to it like a greeting.

## Prompt engineering for schedules

**The schedule executes in a stateless context.** The agent running the schedule has no memory of the current session — it doesn't know what you discovered, what worked, or what the correct approach is. You must encode that knowledge into the prompt.

**Rule: preserve your discoveries.** If you found a working API endpoint, the correct JSON body, or the right tool to use — write that into the prompt. Otherwise the scheduled agent will have to figure it out from scratch and may get it wrong.

**1. Execution method — be explicit:**
For API calls, always specify the exact command:
```
Call via: exec: curl -s -X POST 'https://example.com/api/action'
  -H 'Content-Type: application/json' -d '["param1","param2"]'
```
Not: `"call the API at https://example.com"` — the agent will guess the method, headers, and body.

**2. Missing/null fields — always handle explicitly:**
If a field may not always be present, say so:
```
Include delivery_times as "HH:MM – HH:MM Uhr" if present in response, otherwise omit that line.
```
Not: just listing the field in a format template — the agent will invent a plausible value if the field is missing.

**3. Self-deletion — always include the job ID:**
After creating a schedule, immediately run `schedule(action='list')` to get the job ID, then include it in the prompt:
```
If response field delivered=true: call schedule(action='remove', id='JOBID') then send "✅ Schedule entfernt."
```
Not: `"remove this schedule"` — vague, requires the agent to list and search.

**4. Response field paths — be precise:**
If the API returns nested JSON, write the exact path:
```
Check: response.data[0].state.delivered
Extract: response.data[0].lifecycle.state_info
```
Not: `"check if delivered"` — the agent may check the wrong field.

For general prompt engineering principles (trigger clarity, examples, avoiding vague wording): read `PROMPT_ENGINEERING.md` (same docs directory).

## List & Remove
- List: `schedule(action='list')` — shows all jobs with ID, time, prompt
- Remove by ID: `schedule(action='remove', id='<job_id_or_prefix>')`
- Remove by time/description: first list to find the ID, then remove

## Editing a schedule
User says "ändere", "verschiebe", "update":
1. `action='list'` → find the old job ID
2. `action='remove'` the old one
3. `action='create'` the new one
**Never leave the old job running.**

## Do it now AND schedule it
User says "schau das Wetter an und richte eine tägliche Benachrichtigung ein":
1. Do the task now immediately
2. Then create the schedule with the original task as prompt — not the result you just produced

## One-time / reminders
User says "einmalig", "einmal", "erinner mich", "remind me once": set `once=true`.
`'in N minutes'` / `'in N hours'` triggers are always once automatically.

## Workspace cleanup protection

Before deleting or cleaning up files in the workspace:
1. Run `schedule(action='list')` to see all active schedules
2. Check if any schedule prompt references a file in the workspace (e.g. `github-track-*.md`, `*-plan.md`)
3. **Never delete files that are referenced by an active schedule**
4. Tell the user which files you skipped and why

Files that are typically protected:
- `github-track-*.md` — GitHub repo tracking (see `GITHUB.md`)
- `email-track-*.md` — Email monitoring (see `EMAIL.md`)
- `*-plan.md` — active task plans

## Email schedule examples

User says "prüf meine Mails alle 30 Minuten":
```
schedule(action='create', when='*/30 * * * *',
  prompt='Check email account "privat" for new messages. Read EMAIL.md for IMAP instructions. Track reported messages in WORKSPACE/email-track-privat.md. Send a summary for each new message (sender, subject, 2-sentence preview). If no new messages: do nothing, send nothing.')
```

User says "richte einen Auto-Responder ein für Mails von chef@firma.de":
```
schedule(action='create', when='*/15 * * * *',
  prompt='Check email account "arbeit" for new messages. Read EMAIL.md for auto-reply instructions. Auto-reply to emails from chef@firma.de: "Nachricht erhalten, ich melde mich in 24h." Track replied UIDs in WORKSPACE/email-track-arbeit.md. Never reply twice to the same message.')
```

## Schedules that need web scraping or Playwright

**When this applies:** Package tracking, price monitoring, form-based portals, any site that requires form interaction or dynamic content.

**The rule: scripts come before schedules.**
A schedule prompt that says "track my package on website X" will fail — the schedule runs independently, and the bot will hallucinate URLs, guess form fields, and invent results it doesn't have. Always write and test a working Python script first, then have the schedule run that script.

---

### Step 1 — Write the script in WORKSPACE

**Mandatory design rules for the script:**
- Print **only** what the API or page actually returns — never add estimates, predictions, or context
- Use **structured output** so the schedule can parse it unambiguously:
  ```
  STATUS: unterwegs
  LAST_SCAN: 2026-03-17 02:03 Uhr, Ricany u Prahy (CZ)
  DELIVERED: false
  ```
- Handle all error cases explicitly:
  - Site unreachable → `ERROR: Timeout`
  - Data not found → `NOT_FOUND`
  - Never let the script silently succeed with empty/wrong data
- Always discover the API/page structure first (read WEB_FETCHING.md) — never guess endpoints

**Test the script before creating any schedule:**
```
exec: python3 WORKSPACE/track.py
```
If the output is wrong, fix the script first. Never create a schedule for an untested script.

---

### Step 2 — Create the schedule referencing the script

```
schedule(action='create', when='0 8,12,16,20 * * *',
  prompt='Run: exec python3 WORKSPACE/track.py — then send the complete output verbatim as a message. Do NOT summarize, interpret, or add any information not in the script output.')
```

**Key: `verbatim`.** The schedule must send exactly what the script printed — no embellishment.

---

### Step 3 — Self-deleting schedules (e.g. "notify when delivered")

The script must signal the condition clearly in its output:
```python
if data["delivered"]:
    print("DELIVERED: true")
    print(f"STATUS: Zugestellt um {data['time']}")
else:
    print("DELIVERED: false")
    print(f"STATUS: {data['status']}")
```

Then:
1. Create the schedule and note its ID (from `schedule(action='list')` right after creation)
2. The schedule prompt includes the ID for self-deletion:

```
schedule(action='create', when='0 8,12,16,20 * * *',
  prompt='Run: exec python3 WORKSPACE/track.py — then:
  1. Send the output verbatim as a message.
  2. If the output contains "DELIVERED: true": immediately call schedule(action="remove", id="JOBID_PLACEHOLDER") and send "✅ Schedule entfernt."')
```

After creation, `schedule(action='list')` → get the real job ID → edit the prompt to replace `JOBID_PLACEHOLDER`.

---

### What NOT to write in a Playwright schedule prompt

❌ `'Track package 12345 on website.com — use Playwright if needed'`
→ The bot will guess URLs, hallucinate form fields, and invent statuses.

❌ `'exec: python3 << PYEOF ... PYEOF'` (inline script in prompt)
→ Inline scripts break easily, are hard to maintain, and can't be tested separately.

✅ `'Run: exec python3 WORKSPACE/track.py — send output verbatim'`
→ Tested script, factual output, no interpretation.

## German synonyms the user might say
"geplanter Job", "Benachrichtigung", "Erinnerung", "Aufgabe", "Automatisierung", "täglich", "wöchentlich", "morgens"
