# Document Attachments

Users can attach documents in addition to images. Supported in **Discord, Matrix, and Web-UI**.

## Supported types

- **PDF** (text + scanned). Text-PDFs: extracted via `pypdf`. Scanned PDFs: pages rendered to PNG (via `pypdfium2`) and routed through the Vision pipeline.
- **DOCX** (Microsoft Word, OpenXML). Paragraphs + tables, via `python-docx`.
- **Plain text**: `.txt`, `.md`, `.csv`, `.json`, `.xml`, `.log`, `.rst`.

## How they appear in your context

Document text is wrapped in `<doc name="filename">…</doc>` blocks at the start of the user message. After your response, these blocks are stripped from history and replaced with `[Anhang: filename — N Zeichen]` markers (so subsequent turns don't carry the full text).

For scanned PDFs (pages with little extractable text), each page comes through as a PNG image in the standard image pipeline — read them like any other image.

## Limits (config)

- `doc_max_chars` — extraction-time safety ceiling, default **200000**. Real fit happens at runtime.
- `doc_max_pages_render` — max pages rendered for scanned PDFs, default **10**.
- `doc_response_reserve` — tokens kept free for your response, default **2000**.

**Adaptive fit:** Before the LLM call, doc blocks are scaled to fit the current model's `num_ctx` (after history compacting). When trimmed, the `<doc>` block is marked `truncated="true"` and ends with `[...gekuerzt um in Kontext zu passen...]`. Don't worry about overflow — small models get less, large models get more, automatically.

## Optional dependency

Install with `pip install -e '.[docs]'` (adds `pypdf`, `pypdfium2`, `python-docx`).

## What to do with documents

Treat the `<doc>` content as user-provided source material — translate, summarize, extract data, answer questions about it. **Do not** echo the full document back unless asked. For currency/price conversions inside translated documents, see the Units and Currency rule.
