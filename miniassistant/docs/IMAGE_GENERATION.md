# Image Generation & Editing

When the user asks to generate/create an image, use the configured image generation model.
When the user uploads an image and asks to **edit/change/modify** it, use Image Editing (img2img).

## Config

```yaml
image_generation:
  - "google/gemini-2.5-flash-image"       # Google Gemini (native image generation)
  - "openai/dall-e-3"                      # OpenAI DALL-E 3
  - "openai/chatgpt-image-latest"          # OpenAI ChatGPT Image
  # - "ollama-online/flux"                 # Can also use Ollama models with provider prefix
```

Image generation is a **list** of models. Use `invoke_model(model='EXACT_NAME_FROM_LIST', message='PROMPT')` to generate.
If the list is empty: tell the user "Kein Bildgenerierungs-Modell konfiguriert. Bitte `image_generation` in der Config setzen."

## Workflow — Image Generation (new image from text)

1. **Generate the image** using `invoke_model` with one of the configured models (use the **exact name** including provider prefix).
   The system automatically saves generated images to `WORKSPACE/images/` (the configured workspace directory).
2. **Send the image** using the `send_image` tool:
   ```
   send_image(image_path="/absolute/path/to/image.png", caption="Beschreibung")
   ```
   The tool automatically detects the current platform (Matrix/Discord/Web-UI) and uploads the image:
   - **Matrix:** Uploads to media repo via bot client (E2EE-capable), sends as `m.image`.
   - **Discord:** Uploads as file attachment via bot API.
   - **Web-UI:** Returns the file path (inline display not supported).

**No curl commands needed.** The `send_image` tool handles credentials, room/channel detection, and upload automatically.

## Workflow — Image Editing (modify an existing image)

When the user uploads an image and asks to edit/modify/transform it, the image is automatically saved to disk.
You will see a `[Hochgeladenes Bild gespeichert unter:]` block with the file path(s).

**Use that path with `image_path`:**
```
invoke_model(model='EXACT_NAME_FROM_LIST', message='make the sky sunset orange', image_path='/path/to/uploaded/image.png')
```

**Optional `strength` parameter** (0.0–1.0): Controls how much the image changes.
- `0.1–0.3` = subtle changes (color correction, minor touch-ups)
- `0.4–0.6` = moderate changes (style transfer, object modifications)
- `0.7–1.0` = major changes (heavy transformation, almost new image)
- If omitted: the backend uses its default (typically ~0.5–0.75).

**Only pass `strength` when the user explicitly asks for subtle/strong changes.** Do NOT invent default values.

### How to decide: Generate vs Edit

| User says | Action |
|-----------|--------|
| "erstelle ein Bild von..." / "generate..." | **Generate** — no `image_path` |
| "ändere das Bild..." / "mach den Himmel rot" (+ uploaded image) | **Edit** — use `image_path` |
| "mach das Bild schärfer" / "entferne den Hintergrund" (+ uploaded image) | **Edit** — use `image_path` |
| "erstelle ein ähnliches Bild wie dieses" (+ uploaded image) | **Edit** with high strength (~0.8) |
| Bild hochgeladen ohne Text / "was ist das?" | **Vision/Analyse** — do NOT edit, just describe |

## CRITICAL — NEVER generate fake images

**NEVER** output `![...](data:image/png;base64,...)` or any base64-encoded image data in your response text. You CANNOT generate images by writing base64 — that produces garbage data, not a real image. **ALWAYS** use `invoke_model` with a configured image generation model, then `send_image` to deliver it. There is no shortcut.

## Always available

Image generation is **always available** when configured — no per-provider activation needed.

**Google Gemini:** Gemini 2.0/2.5 Flash and Imagen support native image generation and editing via the API. For editing, the source image is automatically included in the API request as inlineData.

**OpenAI DALL-E / ChatGPT Image:** DALL-E and chatgpt-image-latest support image generation via `POST /v1/images/generations` and editing via `POST /v1/images/edits`. Supports sizes: 1024x1024, 1024x1792, 1792x1024.

**Local backends (sd-server, LocalAI, Flux):** Image editing uses `/v1/images/edits` with the source image as base64. If the backend doesn't support `/edits`, it falls back to `/generations` with the image parameter. Parameters like `steps`, `cfg_scale`, `strength` are passed via `<sd_cpp_extra_args>` tags.
