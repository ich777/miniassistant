"""
Google Gemini API Client – Provider type: google

Unterstützt:
- Chat (generateContent) mit Streaming
- Vision (Bilder als inline_data in Parts)
- Image Generation (responseModalities: IMAGE)
- Thinking/Reasoning (Gemini 2.5 Pro/Flash thinkingConfig)
- Tool/Function Calling (functionDeclarations)
- Modell-Listing (GET /v1beta/models)

Auth: API-Key als Header (x-goog-api-key) oder Query-Parameter (?key=...).
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Generator

import httpx

_log = logging.getLogger("miniassistant.google_client")

# Google Gemini API Defaults
GOOGLE_API_URL = "https://generativelanguage.googleapis.com"
GOOGLE_API_VERSION = "v1beta"
_TIMEOUT = 120


# ═══════════════════════════════════════════════════════════════════════════
# Auth + Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _api_headers(api_key: str) -> dict[str, str]:
    """Standard-Header für Google Gemini API."""
    return {
        "x-goog-api-key": api_key,
        "content-type": "application/json",
    }


def _model_url(base_url: str, model: str, action: str, api_key: str | None = None) -> str:
    """Baut die URL für ein Modell + Action. api_key wird NICHT als Query-Param angehängt (Header stattdessen)."""
    base = base_url.rstrip("/")
    # Modellname normalisieren: wenn kein 'models/' Prefix, hinzufügen
    if not model.startswith("models/"):
        model = f"models/{model}"
    return f"{base}/{GOOGLE_API_VERSION}/{model}:{action}"


# ═══════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════

def api_list_models(
    api_key: str,
    base_url: str = GOOGLE_API_URL,
) -> list[dict[str, Any]]:
    """
    Listet verfügbare Modelle über GET /v1beta/models.
    Returns: Liste von {name, display_name, description, input_token_limit, output_token_limit} Dicts.
    """
    if not api_key:
        raise RuntimeError("Google Gemini API: api_key erforderlich")
    url = f"{base_url.rstrip('/')}/{GOOGLE_API_VERSION}/models"
    try:
        r = httpx.get(url, headers=_api_headers(api_key), timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        models = data.get("models") or []
        out: list[dict[str, Any]] = []
        for m in models:
            name = m.get("name", "")
            # 'models/gemini-2.0-flash' → 'gemini-2.0-flash'
            short_name = name.replace("models/", "") if name.startswith("models/") else name
            if not short_name:
                continue
            # Nur generative Modelle (keine Embedding-Modelle etc.)
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            out.append({
                "name": short_name,
                "display_name": m.get("displayName", short_name),
                "description": (m.get("description") or "")[:100],
                "input_token_limit": m.get("inputTokenLimit"),
                "output_token_limit": m.get("outputTokenLimit"),
            })
        return out
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            raise RuntimeError("Google Gemini API: Ungültiger API-Key (400)")
        if e.response.status_code == 403:
            raise RuntimeError("Google Gemini API: Zugriff verweigert (403) – API-Key prüfen oder API aktivieren")
        raise RuntimeError(f"Google Gemini API Fehler: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        raise RuntimeError(f"Google Gemini API nicht erreichbar: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Message Conversion
# ═══════════════════════════════════════════════════════════════════════════

def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Konvertiert interne Messages ins Google Gemini Format.
    Internes Format: {role: user/assistant/system/tool, content: str, images: [...]}
    Google Format: {role: user/model, parts: [{text: str}, {inlineData: {...}}]}

    system-role wird rausgefiltert (wird separat als systemInstruction übergeben).
    tool-role wird zu functionResponse Parts konvertiert.
    assistant → model.
    """
    api_msgs: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            continue  # System wird separat übergeben

        if role == "tool":
            # Tool-Ergebnis → functionResponse
            tool_name = msg.get("tool_name", "tool")
            content = msg.get("content", "")
            api_msgs.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": tool_name,
                        "response": {"result": content},
                    }
                }],
            })
            continue

        # user oder assistant (→ model)
        gemini_role = "model" if role == "assistant" else "user"
        parts: list[dict[str, Any]] = []

        # Text-Content
        content = msg.get("content", "")
        if content:
            parts.append({"text": content})

        # Bilder (Vision) – base64-encoded
        images = msg.get("images") or []
        for img in images:
            if isinstance(img, dict):
                # {mime_type: "image/png", data: "base64..."}
                parts.append({
                    "inlineData": {
                        "mimeType": img.get("mime_type", "image/png"),
                        "data": img.get("data", ""),
                    }
                })
            elif isinstance(img, str):
                # Reiner Base64-String → als PNG annehmen
                parts.append({
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": img,
                    }
                })

        if parts:
            api_msgs.append({"role": gemini_role, "parts": parts})

    return api_msgs


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Konvertiert Ollama-kompatibles Tool-Schema ins Google functionDeclarations Format.
    Ollama: [{type: function, function: {name, description, parameters}}]
    Google: [{functionDeclarations: [{name, description, parameters}]}]
    """
    if not tools:
        return None
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        decl: dict[str, Any] = {
            "name": name,
            "description": fn.get("description", ""),
        }
        params = fn.get("parameters")
        if params:
            decl["parameters"] = params
        declarations.append(decl)
    if not declarations:
        return None
    return [{"functionDeclarations": declarations}]


# ═══════════════════════════════════════════════════════════════════════════
# Response Parsing
# ═══════════════════════════════════════════════════════════════════════════

def _parse_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Parst Google Gemini API Response → einheitliches Format (kompatibel mit Ollama).
    Extrahiert text, thinking, tool_calls aus candidates[0].content.parts.
    """
    candidates = resp.get("candidates") or []
    if not candidates:
        # Prüfe ob promptFeedback einen Block enthält
        feedback = resp.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason", "")
        if block_reason:
            return {
                "message": {"role": "assistant", "content": f"[Blocked by Google: {block_reason}]", "thinking": ""},
                "model": "",
                "done": True,
                "provider": "google",
            }
        return {
            "message": {"role": "assistant", "content": "", "thinking": ""},
            "model": "",
            "done": True,
            "provider": "google",
        }

    candidate = candidates[0]
    content = candidate.get("content") or {}
    parts = content.get("parts") or []

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    image_data: list[dict[str, Any]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        # Thinking (Gemini 2.5 – part hat "thought": true)
        if part.get("thought"):
            thinking_parts.append(part.get("text", ""))
        elif "text" in part:
            text_parts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append({
                "function": {
                    "name": fc.get("name", ""),
                    "arguments": fc.get("args") or {},
                }
            })
        elif "inlineData" in part:
            # Generiertes Bild
            inline = part["inlineData"]
            image_data.append({
                "mime_type": inline.get("mimeType", "image/png"),
                "data": inline.get("data", ""),
            })

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts),
        "thinking": "\n".join(thinking_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if image_data:
        message["images"] = image_data

    return {
        "message": message,
        "model": resp.get("modelVersion", ""),
        "done": True,
        "provider": "google",
        "usage": resp.get("usageMetadata"),
        "finish_reason": candidate.get("finishReason"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Chat (non-streaming)
# ═══════════════════════════════════════════════════════════════════════════

def api_chat(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "gemini-2.0-flash",
    system: str | None = None,
    max_tokens: int = 8192,
    thinking: bool | str | None = None,
    thinking_budget: int = 10000,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    base_url: str = GOOGLE_API_URL,
    timeout: int = _TIMEOUT,
    image_generation: bool = False,
) -> dict[str, Any]:
    """
    Google Gemini API – POST /v1beta/models/{model}:generateContent.

    Args:
        messages: [{role: "user"/"assistant", content: "...", images: [...]}]
        api_key: Google API Key
        model: Modell-ID (z.B. gemini-2.0-flash, gemini-2.5-pro)
        system: System-Prompt (optional)
        max_tokens: Max. Output-Tokens (Default: 8192)
        thinking: Thinking aktivieren (True/False/None) – nur Gemini 2.5+
        thinking_budget: Token-Budget für Thinking (Default: 10000)
        tools: Tool-Schema (Ollama-Format, wird konvertiert)
        options: Zusätzliche Optionen (temperature, top_p, top_k, etc.)
        base_url: API Base-URL
        image_generation: Wenn True, responseModalities auf TEXT+IMAGE setzen

    Returns: Einheitliches Response-Dict (kompatibel mit Ollama-Format).
    """
    if not api_key:
        raise RuntimeError("Google Gemini API: api_key erforderlich")

    url = _model_url(base_url, model, "generateContent")

    # Messages konvertieren
    api_msgs = _convert_messages(messages)

    body: dict[str, Any] = {
        "contents": api_msgs,
    }

    # System-Prompt
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    # Generation Config
    gen_config: dict[str, Any] = {
        "maxOutputTokens": max_tokens,
    }
    # Optionen übernehmen (temperature, top_p, top_k, etc.)
    if options:
        for key in ("temperature", "topP", "topK", "top_p", "top_k"):
            val = options.get(key)
            if val is not None:
                # Ollama-Keys normalisieren → Google camelCase
                gkey = {"top_p": "topP", "top_k": "topK"}.get(key, key)
                gen_config[gkey] = val
        if options.get("seed") is not None:
            gen_config["seed"] = options["seed"]
        if options.get("stop"):
            stop = options["stop"]
            if isinstance(stop, str):
                stop = [stop]
            gen_config["stopSequences"] = stop

    # Thinking (Gemini 2.5+) – bei Bildern im Request deaktivieren (Kompatibilitätsproblem)
    _has_images = any(
        "inlineData" in part
        for _m in api_msgs for part in (_m.get("parts") or [])
    )
    if thinking and not _has_images:
        gen_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    elif thinking and _has_images:
        _log.info("Google Gemini: Thinking deaktiviert wegen Bildern im Request")

    # Image Generation
    if image_generation:
        gen_config["responseModalities"] = ["TEXT", "IMAGE"]

    body["generationConfig"] = gen_config

    # Tools – bei Bildern im Request nicht mitschicken (Gemini Kompatibilitätsproblem)
    if _has_images:
        _log.info("Google Gemini: Tools deaktiviert wegen Bildern im Request")
    else:
        google_tools = _convert_tools(tools or [])
        if google_tools:
            body["tools"] = google_tools

    headers = _api_headers(api_key)
    # Debug: Bild-Infos loggen wenn vorhanden
    for _amsg in api_msgs:
        for _part in (_amsg.get("parts") or []):
            if "inlineData" in _part:
                _idata = _part["inlineData"]
                _b64len = len(_idata.get("data", ""))
                _log.info("Google Gemini API: Bild in Request – mime=%s, base64_len=%d (~%d KB raw)",
                          _idata.get("mimeType", "?"), _b64len, _b64len * 3 // 4 // 1024)
    _log.debug("Google Gemini API: model=%s, msgs=%d, thinking=%s", model, len(api_msgs), thinking)

    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        resp = r.json()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = e.response.text[:300]
        if status == 400:
            raise RuntimeError(f"Google Gemini API: Bad Request (400). {detail}")
        if status == 403:
            raise RuntimeError(f"Google Gemini API: Zugriff verweigert (403). {detail}")
        if status == 429:
            raise RuntimeError(f"Google Gemini API: Rate Limit erreicht (429). {detail}")
        if status == 500:
            raise RuntimeError(f"Google Gemini API: Interner Fehler (500). {detail}")
        raise RuntimeError(f"Google Gemini API {status}: {detail}")
    except Exception as e:
        raise RuntimeError(f"Google Gemini API nicht erreichbar: {e}")

    return _parse_response(resp)


# ═══════════════════════════════════════════════════════════════════════════
# Chat (Streaming)
# ═══════════════════════════════════════════════════════════════════════════

def api_chat_stream(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "gemini-2.0-flash",
    system: str | None = None,
    max_tokens: int = 8192,
    thinking: bool | str | None = None,
    thinking_budget: int = 10000,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    base_url: str = GOOGLE_API_URL,
    timeout: int = _TIMEOUT,
) -> Generator[dict[str, Any], None, None]:
    """
    Google Gemini API mit Streaming (SSE).
    POST /v1beta/models/{model}:streamGenerateContent?alt=sse
    Yields Chunks im einheitlichen Format.
    """
    if not api_key:
        raise RuntimeError("Google Gemini API: api_key erforderlich")

    url = _model_url(base_url, model, "streamGenerateContent") + "?alt=sse"
    api_msgs = _convert_messages(messages)

    body: dict[str, Any] = {"contents": api_msgs}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    gen_config: dict[str, Any] = {"maxOutputTokens": max_tokens}
    if options:
        for key in ("temperature", "topP", "topK", "top_p", "top_k"):
            val = options.get(key)
            if val is not None:
                gkey = {"top_p": "topP", "top_k": "topK"}.get(key, key)
                gen_config[gkey] = val
    # Bilder erkennen → Thinking + Tools deaktivieren (Gemini Kompatibilitätsproblem)
    _has_images_stream = any(
        "inlineData" in part
        for _m in api_msgs for part in (_m.get("parts") or [])
    )
    if thinking and not _has_images_stream:
        gen_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    elif thinking and _has_images_stream:
        _log.info("Google Gemini Stream: Thinking deaktiviert wegen Bildern")
    body["generationConfig"] = gen_config

    if _has_images_stream:
        _log.info("Google Gemini Stream: Tools deaktiviert wegen Bildern")
    else:
        google_tools = _convert_tools(tools or [])
        if google_tools:
            body["tools"] = google_tools

    headers = _api_headers(api_key)

    with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            candidates = event.get("candidates") or []
            if not candidates:
                continue
            parts = (candidates[0].get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("thought"):
                    text = part.get("text", "")
                    if text:
                        yield {"message": {"thinking": text}, "done": False}
                elif "text" in part:
                    yield {"message": {"content": part["text"]}, "done": False}
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    yield {
                        "message": {
                            "tool_calls": [{
                                "function": {
                                    "name": fc.get("name", ""),
                                    "arguments": fc.get("args") or {},
                                }
                            }]
                        },
                        "done": False,
                    }

            finish = candidates[0].get("finishReason")
            if finish and finish == "STOP":
                yield {"done": True}


# ═══════════════════════════════════════════════════════════════════════════
# Capability Checks
# ═══════════════════════════════════════════════════════════════════════════

def model_supports_vision(model: str) -> bool:
    """Google Gemini Modelle unterstützen nativ Vision (alle Gemini-Modelle sind multimodal)."""
    n = (model or "").lower()
    # Embedding-Modelle und text-only Modelle filtern
    if "embedding" in n or "aqa" in n:
        return False
    return True


def model_supports_tools(model: str) -> bool:
    """Die meisten Gemini-Modelle unterstützen Function Calling."""
    n = (model or "").lower()
    if "embedding" in n or "aqa" in n:
        return False
    return True


def model_supports_thinking(model: str) -> bool:
    """Gemini 2.5 Pro und Flash unterstützen Thinking."""
    n = (model or "").lower()
    return "2.5" in n or "2-5" in n


def model_supports_image_generation(model: str) -> bool:
    """Gemini 2.0 Flash, Imagen und Modelle mit 'image' im Namen unterstützen Image Generation."""
    n = (model or "").lower()
    if "imagen" in n:
        return True
    if "2.0-flash" in n or "2-0-flash" in n:
        return True
    if "image" in n and "gemini" in n:
        return True
    return False
