# Debate — Structured AI Debate

The `debate` tool runs a multi-round, structured discussion between two AI perspectives on a topic. Both sides are argued by subagent models.

## Flow

1. **Main agent** calls `debate(topic, perspective_a, perspective_b, model, ...)`
2. For each round:
   - **Side A** argues (gets a summary of prior rounds + B's last argument)
   - **Side B** replies (gets the summary + A's current argument)
   - The round is **summarized automatically** → compact context for the next round
3. After all rounds: a **neutral conclusion** is generated
4. Full transcript → Markdown file in the workspace

## Why summaries?

Small models have limited context. Instead of sending the entire debate history (which blows the context after 2-3 rounds), each side gets:
- A **compact summary** of all prior rounds (~150 words)
- The **last argument** of the other side (in full)

This keeps the context manageable even across 5-10 rounds.

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `topic` | ✅ | The debate topic or question |
| `perspective_a` | ✅ | Position of side A (e.g. "Pro nuclear power") |
| `perspective_b` | ✅ | Position of side B (e.g. "Against nuclear power") |
| `model` | ✅ | Subagent model for side A (and B if `model_b` is unset) |
| `model_b` | ❌ | Optional different model for side B |
| `rounds` | ❌ | Number of back-and-forth rounds (1-10, default: 3) |
| `language` | ❌ | Response language (default: German) |

## Examples

### Same model, different perspectives
```
debate(
  topic="Should Germany switch its nuclear plants back on?",
  perspective_a="Pro nuclear: climate protection, supply security",
  perspective_b="Against nuclear: safety risks, waste storage",
  model="qwen3",
  rounds=3
)
```

### Different models
```
debate(
  topic="Is open source better than proprietary software?",
  perspective_a="Open source: transparency, freedom, community",
  perspective_b="Proprietary: support, integration, stability",
  model="qwen3",
  model_b="ollama-online/gemma3",
  rounds=5,
  language="Deutsch"
)
```

## Output

- **Markdown file** in the workspace: `debate-{topic}-{timestamp}.md`
  - Header with metadata (models, perspectives, rounds)
  - Each round: argument A + argument B
  - Conclusion at the end
- **Tool return** to the main agent: summary + file path

## Safeguards

- **Max 10 rounds** — hard limit prevents infinite loops
- **Cancellation** — `/stop` or `/abort` ends the debate cleanly (the transcript so far is kept)
- **Status updates** — on Matrix/Discord: progress messages between rounds
- **Fault tolerance** — if a model call fails, "(error: ...)" is recorded instead of aborting

## Tool access

Debaters have the **same tools as normal subagents**:
- ✅ `web_search` — for current info (weather, news, prices, facts)
- ✅ `exec` — shell commands (e.g. inspect files in code debates)
- ✅ `check_url` — URL checking

This means debates about **current topics** work — debaters can run a web search before arguing to back their position with up-to-date data.

## Context management (for small models)

Each side gets per round:
```
System: role + position + rules (~200 tokens)
User:   summary of prior rounds (~150 words)
        + last counter-argument (full, max ~300 words)
```
Total per call: ~400-600 tokens — fits even 2K-4K context models.

## Logging

With `server.log_agent_actions` enabled:
- `DEBATE_START` — topic, perspectives, models, rounds
- `DEBATE_ROUND` — each individual argument (A and B)
- `DEBATE_END` — completion with round count and file path
