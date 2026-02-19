# Schedules

Scheduled tasks (cron or "in N minutes") are managed by the **schedule** tool and stored in `schedules.json` (under the config directory). The user can list jobs with `/schedules` in chat or via `schedule(action='list')`.

- **Create:** `schedule(action='create', when='30 7 * * *', prompt='Get weather for X', client='matrix')` – `when` is 5 cron fields (system time) or e.g. `in 30 minutes`. Use **prompt** (work instruction the bot runs later), optionally **command** (shell), **client** ('matrix'/'discord'), **once**=true for one-shot.
- **model** (optional): Model name or alias to use for the prompt (e.g. `model='qwen3'`, `model='ollama-online/kimi-k2.5'`). **Only set this when the user explicitly requests a specific model.** If omitted, the current default model is used. Aliases are resolved at execution time; if the model is unavailable, it falls back to default.
- **List:** `schedule(action='list')` or user types `/schedules`. Shows model if set.
- **Remove:** `schedule(action='remove', id='<job_id_or_prefix>')`.

No separate config key is required for schedules; the scheduler is enabled by default. To disable: `scheduler: { enabled: false }` in config.
