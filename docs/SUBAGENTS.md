# Subagents (Worker-Modelle)

Subagents allow the main agent to delegate tasks to other models via `invoke_model(model='...', message='...')`.

```yaml
subagents:
  - chipspc/llama-chips
  - qwen3-coder
```

## Workspace

Subagents automatically receive the configured `workspace` directory as their working directory (cwd for `exec`).
**All generated files (images, reports, etc.) MUST be saved inside the workspace directory, NOT in agent_dir.**

## Subagent capabilities

Subagents have **limited tools** (injected at call time, no main-agent context cost):
- ✅ `exec` — shell commands (cwd = workspace, **runs in `sh`** — use `.` not `source`, no bash-only syntax)
- ✅ `web_search` — web search via SearXNG
- ✅ `read_url` — read web page content as text
- ✅ `check_url` — URL verification
- ❌ `save_config` — config changes (main agent only)
- ❌ `schedule` — job scheduling (main agent only)
- ❌ `invoke_model` — no cascading subagent calls

Subagent system prompt comes from `agent_dir/basic_rules/subagent.md` (user-editable).
Max 15 tool rounds per subagent call. Must use trash instead of `rm -rf`.

## User-directed delegation

If the user explicitly names a subagent (e.g. "beauftrage qwen3-coder-max"), the main agent **must** use that subagent via `invoke_model`. It must never silently fall back to doing the work itself.

## Timeout and retry behavior

If `invoke_model` returns an error or times out:
1. **Retry once** with the same model and message.
2. If it fails again: tell the user (e.g. "Subagent qwen3-coder-max ist nicht erreichbar.") and ask how to proceed.
3. **Never do the subagent's work yourself** after a failure — ask the user first.

## Message guidelines

The message to the subagent must be self-contained — the subagent has no conversation history. Include:
- The exact goal and what output is expected (report, file, list, …)
- Relevant paths, URLs, constraints
- Response language (e.g. "Antworte auf Deutsch")
- If a plan file exists: "Arbeite gemäß Plan in [PFAD]. Markiere jeden Schritt als [x] wenn erledigt, [!] wenn fehlgeschlagen."

Keep messages concise — subagents run small models with limited context.

## Debate tool

The `debate` tool uses subagents to run structured multi-round debates between two AI perspectives. Both sides are argued by subagent(s) — the main agent only kicks it off.

Key features:
- Automatic **summarization between rounds** (small models keep context)
- Full transcript saved to a **Markdown file** in the workspace
- **Cancellation** support (`/stop`, `/abort`)
- **Max 10 rounds** hard limit (prevents endless loops)

See `docs/DEBATE.md` for full documentation and examples.

## Error reporting

Subagents can report problems and suggest plan changes in their response. If a subagent includes a `## Suggested plan changes` section, the main agent reviews it and decides whether to update the plan. See `docs/PLANNING.md` for details.
