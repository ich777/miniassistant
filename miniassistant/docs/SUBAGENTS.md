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

## When to use subagents

**ONLY use `invoke_model` in these cases:**
1. The user **explicitly asks** for a subagent/worker (e.g. "beauftrage einen Subworker", "lass das den qwen machen")
2. The task **requires a specialized model** (image generation, audio generation)

**NEVER delegate on your own initiative.** If you can do the task with your own tools (exec, web_search, read_url, etc.), do it yourself. Subagents cost extra time and resources — don't use them "just because they're available".

## User-directed delegation

If the user explicitly names a subagent (e.g. "beauftrage qwen3-coder-max"), the main agent **must** use that subagent via `invoke_model`. It must never silently fall back to doing the work itself.

## Timeout and retry behavior

The system automatically retries each `invoke_model` call once (with 5s delay) before returning an error.
If you still receive an error after the automatic retry:

1. **Retry once more** yourself — call `invoke_model` again with the same model and message. Do NOT skip this step.
2. If it fails again: tell the user (e.g. "Subagent qwen3-coder-max ist nicht erreichbar.") and ask how to proceed.
3. **Never do the subagent's work yourself** after a failure — ask the user first.
4. **Never substitute subagent results with your own knowledge** or with data from other tools (e.g. web_search). The user explicitly requested subagent execution — honor that.

**You are the orchestrator.** Your job is to delegate, collect results, and synthesize. If a subagent fails, your first action is ALWAYS to retry it — not to work around it with data you already have.

## Parallel execution

Multiple tool calls returned in a **single response** are executed concurrently when safe (invoke_model, web_search, read_url, check_url, read_email). This means:

- If you need to delegate **multiple independent tasks**, call `invoke_model` for each one **in the same response** — they will run in parallel, not one after another.
- Same for research: call multiple `web_search` or `read_url` in a single response to search/read in parallel.
- **Do this whenever tasks are independent.** Example: "search 4 sources" → 4× `web_search` in one response, not 4 sequential rounds.
- Sequential tools (exec, save_config, schedule) still run one at a time.

**When NOT to parallelize** — run sequentially instead:
- Tasks write to the **same file** → parallel writes cause data loss/corruption
- Task B **depends on the result** of task A → ordering required
- Tasks modify **shared state** (same config section, same API resource, same schedule)
- The target API has **rate limits** that concurrent requests would exceed
- **If unsure** whether tasks are truly independent: default to sequential.

## Message guidelines

The message to the subagent must be self-contained — the subagent has no conversation history. Include:
- The exact goal and what output is expected (report, file, list, …)
- Relevant paths, URLs, constraints
- Response language (e.g. "Antworte auf Deutsch")
- If a plan file exists: "Arbeite gemäß Plan in [PFAD]. Markiere jeden Schritt als [x] wenn erledigt, [!] wenn fehlgeschlagen."

**Transfer your knowledge.** If you already know the correct approach — the right API endpoint, the exec command that works, the tool to use — include it in the message. The subagent has no session context and will have to discover everything from scratch otherwise, likely getting it wrong.

**Directions → subagent:** If the task is covered by a directions file, do NOT pass the file path — the subagent should not have to discover or read it. Instead: read the file yourself, extract the relevant task section (Aufgabe + Methode + Ausgabe-Format), and paste it directly into the invoke_model message. The subagent gets exactly what it needs, nothing more.

Example — wrong:
> "Track my package on example-shop.com"

Example — correct:
> "Track my package on example-shop.com. Use exec with: `curl -s -X POST 'https://example-shop.com/api/track' -H 'Content-Type: application/json' -d '[\"TRACKINGNR\",\"PLZ\"]'`. Parse the JSON response and return status, last scan location, and delivered flag."

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
