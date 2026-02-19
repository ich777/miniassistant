# Image Generation

When the user asks to generate/create an image, use the configured image generation model.

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

## Workflow — step by step

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

## Always available

Image generation is **always available** when configured — no per-provider activation needed.

**Google Gemini:** Gemini 2.0/2.5 Flash and Imagen support native image generation via the API. Generated images are returned as base64 `inlineData` in the response and automatically saved to disk.

**OpenAI DALL-E / ChatGPT Image:** DALL-E 3 and chatgpt-image-latest support image generation via `POST /v1/images/generations`. Generated images are returned as base64 and automatically saved to disk. Supports sizes: 1024x1024, 1024x1792, 1792x1024.
