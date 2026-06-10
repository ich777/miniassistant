# Directions — Format & Usage Guide

Directions are Markdown files with precise, self-contained instructions for recurring tasks.
They live in `{agent_dir}/directions/` and are read on-demand — they are NOT auto-loaded into context.

---

## When to check for a directions file

- The user asks you to do something that sounds like a recurring or configured task
  (e.g. "check the Sonarr queue", "fetch Cloudflare analytics", "check GitHub issues")
- A scheduled agent receives a task prompt
- A prompt explicitly says "read the instructions from directions/..." or "follow the direction"

**Always `ls directions/` first** — filenames describe the service/task.
If a matching file exists: `cat` it, find the relevant task section, execute it.
If none matches: proceed normally.

---

## Single-task file

Use when a file covers exactly one operation:

```markdown
# ServiceName - Directions

## Task
What to do — action-oriented ("Fetch X", "Check Y", "Create Z").

## Method
1. Step one
2. Step two

## Output format
No code block — output directly as Markdown.

Expected output structure here (as raw Markdown, NOT inside a code block).
```

---

## Multi-task file

Use when multiple related operations belong to the same service.
Separate tasks with `---`. When executing: run ONLY the task the user asked for.

```markdown
# ServiceName - Directions

---

## Task A: Short description

### Task
What to do.

### Method
Step 1 ...
Step 2 ...

### Output format
No code block — output directly as Markdown.

---

## Task B: Short description

### Task
...
```

**Rule:** If a directions file already exists for a service, add new tasks to it — don't create a separate file.

---

## What to include per task

- **Task:** What the agent must DO. NOT setup language ("Create a token...").
- **Method:** Exact API endpoints, commands, tool calls — no guessing needed.
- **Credentials/Tokens:** Include values directly so the agent can use them in `exec: curl` immediately.
  NEVER write "a token must be created" — only fulfilled prerequisites belong here.
- **Output format:** Exact expected output structure as raw Markdown.
  **CRITICAL:** Never show output examples inside ` ``` ` code blocks — the executing agent mirrors whatever wrapper you use.
  Always add: `No code block — output directly as Markdown.`

**What NOT to include:** Setup instructions, unmet prerequisites, references to the current session.

---

## curl/shell rules — CRITICAL

- JSON body (`-d '...'`) must be a **single line** — no literal newlines inside the value
- GraphQL queries must be a **single-line escaped string**
- Brace expansion `{0..6}` does NOT work in single exec calls — use `for i in 0 1 2 3 4 5 6`
- Only save commands that have been **verified to work** — no drafts or intermediate attempts
- Parse JSON with `jq`, not string manipulation
- Credentials in directions are meant to be used in `exec: curl` — this is NOT "echoing" them

---

## Delegating to a subagent

If a task from a directions file should be executed by a subagent via `invoke_model`:
- **Do NOT** pass the file path — the subagent should not have to read or discover the file itself
- **Do** read the file yourself, extract the relevant task section (Task + Method + Output format), and include it verbatim in the `invoke_model` message
- The subagent message must be fully self-contained — credentials, endpoints, expected output format all included

---

## When to create a directions file

- User says "save that as a direction", "create a directions file", "remember how this works", "save this instruction"
- A scheduled task prompt references a directions file
- A task is too complex to encode fully in a schedule prompt
