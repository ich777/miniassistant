# Schedules

Scheduled tasks are managed by the **schedule** tool and stored in `schedules.json` (config directory).

## Create
`schedule(action='create', when='30 7 * * *', prompt='...', once=false)`
- `when`: 5 cron fields in system time (e.g. `0 8 * * *` = 08:00 daily) or `'in 30 minutes'`
- `prompt`: plain language task — the bot re-executes this fresh each time
- `once`: true = run once then delete (for reminders, one-time notifications)
- `client`: `'matrix'` / `'discord'` (default: room/channel where schedule was created)
- `model`: model alias to use (only set if user explicitly requests it)

## Prompt rules — critical
- **Plain language only.** Write WHAT to do, not HOW: `'List open issues from GitHub repo OWNER/REPO via GitHub API'` ✅ — NOT `exec: curl ...` ❌
- **Never copy a result into prompt.** The bot fetches fresh data at execution time.
- **Never show a preview** of what the result might look like — the schedule hasn't run yet.
- **Simple message** (user says "schick mir X um Y Uhr"): `prompt='Send this exact message to the user: "X"'` — NOT just `"X"` alone or the bot will reply to it like a greeting.

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
- `*-plan.md` — active task plans

## German synonyms the user might say
"geplanter Job", "Benachrichtigung", "Erinnerung", "Aufgabe", "Automatisierung", "täglich", "wöchentlich", "morgens"
