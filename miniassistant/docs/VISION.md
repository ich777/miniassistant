# Vision (Image Analysis)

When a user sends an image (via Discord, Matrix, or Web-UI), the assistant can analyze it using a **vision-capable model**.

## Config

```yaml
vision:
  model: "llava:13b"                    # Vision model for image analysis (Ollama model name)
  # model: "ollama-online/llava:13b"    # Can also use provider prefix
  # model: "google/gemini-2.0-flash"     # Google Gemini (nativ multimodal)
  # model: "openai/gpt-4o"                # OpenAI GPT-4o (multimodal)
  num_ctx: 32768                         # Context size for vision model
```

If `vision.model` is not set: tell the user "Kein Vision-Modell konfiguriert. Bitte `vision.model` in der Config setzen (z.B. `llava:13b`, `gemma3`, `minicpm-v`)."

## Behavior

1. **Image received** → check if the current chat model itself supports vision (e.g. `gemma3`, `llava`, `minicpm-v`). If yes: analyze directly, no extra model needed.
2. **Current model has no vision** → use `vision.model` from config. The image is sent to the vision model with the user's question (or "Describe this image" if no question).
3. **No vision model configured** → inform the user and suggest adding one.

## Vision-capable models (examples)

- **llava:13b** / **llava:7b** — LLaVA (good general vision)
- **gemma3** — Google Gemma 3 (built-in vision)
- **minicpm-v** — MiniCPM-V (lightweight vision)
- **llama3.2-vision** — Llama 3.2 Vision
- **google/gemini-2.0-flash** — Google Gemini (nativ multimodal, kein separates Vision-Modell nötig)
- **google/gemini-2.5-pro** — Google Gemini 2.5 Pro (nativ multimodal + Thinking)
- **openai/gpt-4o** — OpenAI GPT-4o (multimodal, Vision + Text)
- **openai/gpt-4o-mini** — OpenAI GPT-4o Mini (multimodal, günstiger)

## How vision works internally

The vision model receives:
- The image (base64-encoded or as URL, depending on API)
- The user's message/question about the image
- A short system prompt: "Describe or analyze the image as requested. Be precise and concise."

The response is returned to the user in the original chat. The vision model does **not** have tool access — it only returns text.

## Always available

Vision is **always available** when configured — it does not need to be enabled per-provider like subagents. Any provider's model can be used as vision model (local Ollama, Ollama Online, Google Gemini, OpenAI, Anthropic).

**Google Gemini:** All Gemini models are natively multimodal — they support vision out of the box. Images are sent as `inlineData` (base64) in the request parts. No separate vision model needed when using Gemini as chat model.

**OpenAI:** GPT-4o models support vision. Images are sent as `data:image/png;base64,...` URLs in the content array. GPT-4o-mini also supports vision at lower cost.
