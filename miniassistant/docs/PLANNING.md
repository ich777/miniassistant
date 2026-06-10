# Task Planning

## When?

Create a plan when the task has **3 or more actions** (changes, installs, research steps, etc.).
Also when the user explicitly says: "make a plan", "plan this".

**No plan** for: simple questions, 1-2 steps, quick lookups.

---

## Format

**Location:** `{workspace}/TOPIC-plan.md`
**Filename:** lowercase, hyphens (e.g. `auth-refactoring-plan.md`)

```markdown
# Plan: [Short title]

**Goal:** [What should be achieved in the end?]
**Created:** [Date]

## Steps

- [ ] 1. Description
- [ ] 2. Description
- [ ] 3. Description

## Notes

[Findings, decisions made while working]
```

**Markers:** `- [ ]` open, `- [x]` done, `- [!] reason` failed.

---

## Rules

1. **Update the plan** after each completed step — not only at the end
2. **Keep steps short and concrete** — no vague descriptions
3. **Insert new steps** when needed — extend the existing plan, no new file
4. **Mark failures honestly** as `- [!]` with a reason, then add a corrected step
5. **Keep going** while the next steps are clear — only stop when user input is required
6. **Resuming:** if the user says "continue" or "look at the plan": read the plan, summarize status, work the next open step

## Completion

1. Briefly inform the user (max 5-10 sentences) what was done
2. Write a summary to `{workspace}/TOPIC-summary.md`
3. Keep the plan file as reference — only delete it if the user explicitly says so

---

## Subagents

If subagents are available, independent steps can be delegated.
**Always pass context** — the subagent doesn't know the plan. Details: see `SUBAGENTS.md`.
