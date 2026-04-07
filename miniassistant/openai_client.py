"""
OpenAI API Client – Provider type: openai

Unterstützt:
- Chat Completions (/v1/chat/completions) mit Streaming
- Vision (Bilder als base64 data-URL in content-Array)
- Image Generation (/v1/images/generations, DALL-E 3)
- Tool/Function Calling (tools-Array)
- Modell-Listing (GET /v1/models)
- Reasoning (o1/o3/o4-mini via reasoning_effort)

Auth: API-Key als Bearer Token (Authorization: Bearer sk-...).
Kompatibel mit OpenAI-kompatiblen APIs (Together, Groq, Perplexity, etc.) via base_url.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Generator

import httpx

_log = logging.getLogger("miniassistant.openai_client")

# OpenAI API Defaults
OPENAI_API_URL = "https://api.openai.com"
_TIMEOUT = 120


# ═══════════════════════════════════════════════════════════════════════════
# Auth + Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _api_headers(api_key: str | None) -> dict[str, str]:
    """Standard-Header für OpenAI-kompatible APIs. Ohne api_key wird kein Auth-Header gesendet."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _api_url(base_url: str, path: str) -> str:
    """Baut API-URL auf und verhindert /v1/v1-Dopplung.
    path muss mit / beginnen (z.B. '/models', '/chat/completions').
    Wenn base_url bereits auf /v1 endet, wird kein weiteres /v1 eingefügt."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{path}"
    return f"{base}/v1{path}"


def _uses_max_completion_tokens(model: str) -> bool:
    """Erkennt Modelle die max_completion_tokens statt max_tokens benötigen.
    Betrifft: o-Serie (o1, o3, o4-mini, ...), GPT-5.x+, und alle neueren Modelle.
    Bei unbekannten Modellen: lieber max_completion_tokens (ist der neuere Standard)."""
    m = model.lower().strip()
    # o-Serie: o1, o3, o3-mini, o4-mini, ...
    if m.startswith(("o1", "o3", "o4")):
        return True
    # GPT-5.x und neuer
    if m.startswith("gpt-5") or m.startswith("gpt-6"):
        return True
    # chatgpt-4o-latest und ähnliche wrapper-Modelle
    if "chatgpt" in m:
        return True
    # GPT-4o bleibt bei max_tokens (Abwärtskompatibilität)
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════

def api_list_models(
    api_key: str,
    base_url: str = OPENAI_API_URL,
) -> list[dict[str, Any]]:
    """
    Listet verfügbare Modelle über GET /v1/models.
    Returns: Liste von {name, owned_by} Dicts.
    """
    # api_key ist optional für OpenAI-kompatible APIs (z.B. vLLM, llama.cpp)
    url = _api_url(base_url, "/models")
    try:
        r = httpx.get(url, headers=_api_headers(api_key), timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        models = data.get("data") or []
        out: list[dict[str, Any]] = []
        for m in models:
            model_id = m.get("id", "")
            if not model_id:
                continue
            out.append({
                "name": model_id,
                "owned_by": m.get("owned_by", ""),
            })
        # Alphabetisch sortieren
        out.sort(key=lambda x: x["name"])
        return out
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise RuntimeError("OpenAI API: Ungültiger API-Key (401)")
        if e.response.status_code == 403:
            raise RuntimeError("OpenAI API: Zugriff verweigert (403)")
        raise RuntimeError(f"OpenAI API Fehler: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        raise RuntimeError(f"OpenAI API nicht erreichbar: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Message Conversion
# ═══════════════════════════════════════════════════════════════════════════

def _convert_messages(
    messages: list[dict[str, Any]],
    system: str | None = None,
) -> list[dict[str, Any]]:
    """Konvertiert interne Messages ins OpenAI Chat Completions Format.
    Internes Format: {role: user/assistant/system/tool, content: str, images: [...], tool_calls: [...]}
    OpenAI Format: {role: system/user/assistant/tool, content: str|[{type:text,...},{type:image_url,...}]}

    tool-role bekommt tool_call_id (generiert falls nicht vorhanden).
    """
    api_msgs: list[dict[str, Any]] = []

    # System-Prompt als erste Message
    if system:
        api_msgs.append({"role": "system", "content": system})

    _tool_call_counter = 0

    for msg in messages:
        role = msg.get("role", "user")

        if role == "system":
            # Zusätzliche System-Messages durchreichen
            api_msgs.append({"role": "system", "content": msg.get("content", "")})
            continue

        if role == "tool":
            # Prüfen ob die vorherige assistant-Nachricht tool_calls hat.
            # Bei no_api_tools (XML-Tool-Calls) hat sie keines → tool-Result als user senden,
            # da vLLM ohne passendes tool_calls-Array im assistant die role:tool-Message ablehnt.
            prev_assistant = next((m for m in reversed(api_msgs) if m.get("role") == "assistant"), None)
            if not (prev_assistant and prev_assistant.get("tool_calls")):
                tool_name = msg.get("tool_name", "tool")
                api_msgs.append({
                    "role": "user",
                    "content": f"<tool_response>\n<tool_name>{tool_name}</tool_name>\n<content>{msg.get('content', '')}</content>\n</tool_response>",
                })
            else:
                # Standard OpenAI tool_call_id matching
                tool_call_id = msg.get("tool_call_id", "")
                if not tool_call_id:
                    _tool_call_counter += 1
                    tool_call_id = f"call_{_tool_call_counter}"
                api_msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": msg.get("content", ""),
                })
            continue

        if role == "assistant":
            out_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.get("content") or "",
            }
            # Tool-Calls durchreichen wenn vorhanden
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                openai_tcs = []
                for i, tc in enumerate(tool_calls):
                    fn = tc.get("function") or {}
                    tc_id = tc.get("id", "")
                    if not tc_id:
                        _tool_call_counter += 1
                        tc_id = f"call_{_tool_call_counter}"
                    args = fn.get("arguments")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    openai_tcs.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args or "{}",
                        },
                    })
                out_msg["tool_calls"] = openai_tcs
                # OpenAI: wenn tool_calls vorhanden, content kann null sein
                if not out_msg["content"]:
                    out_msg["content"] = None
            api_msgs.append(out_msg)
            continue

        # User-Message
        images = msg.get("images") or []
        content_text = msg.get("content", "")

        if images:
            # Multi-Part Content (Text + Bilder)
            parts: list[dict[str, Any]] = []
            if content_text:
                parts.append({"type": "text", "text": content_text})
            for img in images:
                if isinstance(img, dict):
                    mime = img.get("mime_type", "image/png")
                    data = img.get("data", "")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    })
                elif isinstance(img, str):
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img}"},
                    })
            api_msgs.append({"role": "user", "content": parts})
        else:
            api_msgs.append({"role": "user", "content": content_text})

    return api_msgs


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Konvertiert Ollama-kompatibles Tool-Schema ins OpenAI Format.
    Ollama: [{type: function, function: {name, description, parameters}}]
    OpenAI: identisches Format – nur sicherstellen dass type: function gesetzt ist.
    """
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out if out else None


# ═══════════════════════════════════════════════════════════════════════════
# Response Parsing
# ═══════════════════════════════════════════════════════════════════════════

def _parse_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Parst OpenAI Chat Completions Response → einheitliches Format (kompatibel mit Ollama).
    Extrahiert content, tool_calls, reasoning aus choices[0].message.
    """
    choices = resp.get("choices") or []
    if not choices:
        error = resp.get("error") or {}
        if error:
            err_msg = error.get("message", str(error))
            return {
                "message": {"role": "assistant", "content": f"[OpenAI Error: {err_msg}]", "thinking": ""},
                "model": resp.get("model", ""),
                "done": True,
                "provider": "openai",
            }
        return {
            "message": {"role": "assistant", "content": "", "thinking": ""},
            "model": resp.get("model", ""),
            "done": True,
            "provider": "openai",
        }

    choice = choices[0]
    msg = choice.get("message") or {}

    content = msg.get("content") or ""
    thinking = ""

    # Reasoning-Modelle (o1, o3, o4-mini) können reasoning_content haben
    if msg.get("reasoning_content"):
        thinking = msg["reasoning_content"]

    # Tool-Calls konvertieren
    tool_calls: list[dict[str, Any]] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        tool_calls.append({
            "id": tc.get("id", ""),
            "function": {
                "name": fn.get("name", ""),
                "arguments": args,
            },
        })

    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "thinking": thinking,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "message": message,
        "model": resp.get("model", ""),
        "done": True,
        "provider": "openai",
        "usage": resp.get("usage"),
        "timings": resp.get("timings"),
        "finish_reason": choice.get("finish_reason"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Chat (non-streaming)
# ═══════════════════════════════════════════════════════════════════════════

def api_chat(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    system: str | None = None,
    max_tokens: int = 4096,
    thinking: bool | str | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    base_url: str = OPENAI_API_URL,
    timeout: int = _TIMEOUT,
) -> dict[str, Any]:
    """
    OpenAI Chat Completions API – POST /v1/chat/completions.

    Args:
        messages: [{role, content, images, tool_calls}]
        api_key: OpenAI API Key (sk-...)
        model: Modell-ID (z.B. gpt-4o, gpt-4o-mini, o4-mini)
        system: System-Prompt
        max_tokens: Max. Output-Tokens
        thinking: Reasoning aktivieren (für o1/o3/o4-mini Modelle)
        tools: Tool-Schema (Ollama-Format, wird konvertiert)
        options: Zusätzliche Optionen (temperature, top_p, etc.)
        base_url: API Base-URL (für OpenAI-kompatible APIs)

    Returns: Einheitliches Response-Dict.
    """
    # api_key ist optional für OpenAI-kompatible APIs (z.B. vLLM, llama.cpp)

    url = _api_url(base_url, "/chat/completions")

    # Messages konvertieren (System-Prompt wird in Messages eingebaut)
    api_msgs = _convert_messages(messages, system=system)

    # Neuere Modelle (o-Serie, GPT-5.x) nutzen max_completion_tokens statt max_tokens
    _use_mct = _uses_max_completion_tokens(model)
    body: dict[str, Any] = {
        "model": model,
        "messages": api_msgs,
        "stream": False,
    }
    if _use_mct:
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens

    # Optionen übernehmen
    if options:
        for key in ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty"):
            val = options.get(key)
            if val is not None:
                body[key] = val
        if options.get("seed") is not None:
            body["seed"] = options["seed"]
        if options.get("stop"):
            body["stop"] = options["stop"]

    # Reasoning (o1/o3/o4-mini)
    if thinking:
        # reasoning_effort: low/medium/high
        effort = thinking if isinstance(thinking, str) else "medium"
        body["reasoning_effort"] = effort
        # Sicherstellen dass max_completion_tokens gesetzt ist (auch für ältere Reasoning-Modelle)
        if not _use_mct:
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = max_tokens

    # Tools
    openai_tools = _convert_tools(tools or [])
    if openai_tools:
        body["tools"] = openai_tools
        body["parallel_tool_calls"] = True  # llama.cpp/llama-swap benötigt dies explizit für mehrere Tool-Calls

    headers = _api_headers(api_key)
    _log.debug("OpenAI API: model=%s, msgs=%d, thinking=%s", model, len(api_msgs), thinking)

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
            raise RuntimeError(f"OpenAI API: Bad Request (400). {detail}")
        if status == 401:
            raise RuntimeError(f"OpenAI API: Ungültiger API-Key (401). {detail}")
        if status == 403:
            raise RuntimeError(f"OpenAI API: Zugriff verweigert (403). {detail}")
        if status == 429:
            raise RuntimeError(f"OpenAI API: Rate Limit erreicht (429). {detail}")
        if status == 500:
            raise RuntimeError(f"OpenAI API: Interner Fehler (500). {detail}")
        raise RuntimeError(f"OpenAI API {status}: {detail}")
    except Exception as e:
        raise RuntimeError(f"OpenAI API nicht erreichbar: {e}")

    return _parse_response(resp)


# ═══════════════════════════════════════════════════════════════════════════
# Chat (Streaming)
# ═══════════════════════════════════════════════════════════════════════════

def api_chat_stream(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    system: str | None = None,
    max_tokens: int = 4096,
    thinking: bool | str | None = None,
    tools: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    base_url: str = OPENAI_API_URL,
    timeout: int = _TIMEOUT,
) -> Generator[dict[str, Any], None, None]:
    """
    OpenAI Chat Completions API mit Streaming (SSE).
    Yields Chunks im einheitlichen Format.
    """
    # api_key ist optional für OpenAI-kompatible APIs (z.B. vLLM, llama.cpp)

    url = _api_url(base_url, "/chat/completions")
    api_msgs = _convert_messages(messages, system=system)

    _use_mct = _uses_max_completion_tokens(model)
    body: dict[str, Any] = {
        "model": model,
        "messages": api_msgs,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if _use_mct:
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens

    if options:
        for key in ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty"):
            val = options.get(key)
            if val is not None:
                body[key] = val
    if thinking:
        effort = thinking if isinstance(thinking, str) else "medium"
        body["reasoning_effort"] = effort
        if not _use_mct:
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = max_tokens

    openai_tools = _convert_tools(tools or [])
    if openai_tools:
        body["tools"] = openai_tools
        body["parallel_tool_calls"] = True  # llama.cpp/llama-swap benötigt dies explizit für mehrere Tool-Calls

    headers = _api_headers(api_key)

    # Akkumulierte Tool-Calls für Streaming (OpenAI streamt sie in Teilen)
    _tool_calls_acc: dict[int, dict[str, Any]] = {}
    # Usage/Timings aus Stream-Chunks sammeln (llama.cpp, OpenAI, vLLM)
    _usage: dict[str, Any] | None = None
    _timings: dict[str, Any] | None = None

    with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                # Akkumulierte Tool-Calls als finalen Chunk senden
                if _tool_calls_acc:
                    tcs = []
                    for idx in sorted(_tool_calls_acc.keys()):
                        tc = _tool_calls_acc[idx]
                        args_str = tc.get("arguments", "")
                        try:
                            args = json.loads(args_str) if args_str else {}
                        except json.JSONDecodeError:
                            args = {}
                        tcs.append({
                            "id": tc.get("id", ""),
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": args,
                            },
                        })
                    yield {"message": {"tool_calls": tcs}, "done": False}
                _done: dict[str, Any] = {"done": True}
                if _usage:
                    _done["usage"] = _usage
                if _timings:
                    _done["timings"] = _timings
                yield _done
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # Usage/Timings aus jedem Chunk sammeln — OpenAI/vLLM senden usage
            # im finalen Chunk (choices=[]), llama.cpp sendet zusätzlich timings
            if event.get("usage"):
                _usage = event["usage"]
            if event.get("timings"):
                _timings = event["timings"]

            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            # Content
            if delta.get("content"):
                yield {"message": {"content": delta["content"]}, "done": False}

            # Reasoning Content (o1/o3/o4-mini)
            if delta.get("reasoning_content"):
                yield {"message": {"thinking": delta["reasoning_content"]}, "done": False}

            # Tool-Calls (gestreamt in Teilen)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                if idx not in _tool_calls_acc:
                    _tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                if tc.get("id"):
                    _tool_calls_acc[idx]["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    _tool_calls_acc[idx]["name"] = fn["name"]
                if fn.get("arguments"):
                    _tool_calls_acc[idx]["arguments"] += fn["arguments"]

            # Kein frühes done bei finish_reason — immer auf [DONE] warten.
            # Grund: Tool-Calls werden in _tool_calls_acc akkumuliert und erst
            # beim [DONE]-Sentinel geliefert. Ein früher Ausstieg bei finish_reason
            # würde sie verlieren (race condition bei vLLM mit tool-call-parser).


# ═══════════════════════════════════════════════════════════════════════════
# Image Generation (DALL-E)
# ═══════════════════════════════════════════════════════════════════════════

def api_generate_image(
    prompt: str,
    *,
    api_key: str,
    model: str = "dall-e-3",
    size: str = "1024x1024",
    quality: str = "standard",
    base_url: str = OPENAI_API_URL,
    timeout: int = 600,
    steps: int | None = None,
    cfg_scale: float | None = None,
    guidance: float | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    seed: int | None = None,
    negative_prompt: str | None = None,
) -> dict[str, Any]:
    """
    OpenAI Image Generation – POST /v1/images/generations.
    Returns: {url: str, revised_prompt: str} oder {b64_json: str, revised_prompt: str}.
    Für lokale Backends (sd-server / stable-diffusion.cpp): Generation-Parameter werden
    als <sd_cpp_extra_args>-Tag im Prompt eingebettet, da sd-server sie im JSON-Body ignoriert.
    Für DALL-E: nur size/quality/response_format (OpenAI-Standard).
    """
    # api_key ist optional für OpenAI-kompatible APIs (z.B. LocalAI, llama.cpp)
    url = _api_url(base_url, "/images/generations")
    is_openai = OPENAI_API_URL in base_url

    # Für lokale Backends: Parameter als <sd_cpp_extra_args> in Prompt einbetten,
    # da sd-server (stable-diffusion.cpp) steps/cfg_scale/etc. im JSON-Body ignoriert.
    api_prompt = prompt
    if not is_openai:
        extra: dict[str, Any] = {}
        if steps is not None:
            extra["steps"] = steps
        if cfg_scale is not None:
            extra["cfg_scale"] = cfg_scale
        if guidance is not None:
            extra["guidance"] = guidance
        if sampler is not None:
            extra["sample_method"] = sampler
        if scheduler is not None:
            extra["scheduler"] = scheduler
        if seed is not None:
            extra["seed"] = seed
        if negative_prompt is not None:
            extra["negative_prompt"] = negative_prompt
        if extra:
            api_prompt = f"{prompt}<sd_cpp_extra_args>{json.dumps(extra)}</sd_cpp_extra_args>"

    body: dict[str, Any] = {"model": model, "prompt": api_prompt, "n": 1}
    if is_openai:
        # DALL-E braucht size/quality/response_format
        body["size"] = size
        body["quality"] = quality
        body["response_format"] = "b64_json"
    else:
        # Lokale Backends: size wird vom JSON-Body gelesen, rest via sd_cpp_extra_args
        body["response_format"] = "b64_json"
        body["size"] = size

    def _parse(resp) -> dict:
        import base64 as _b64
        from urllib.parse import urljoin
        # Normalize response — some backends return list or nested structures
        if isinstance(resp, list):
            data = resp[0] if resp else {}
        elif isinstance(resp, dict):
            if isinstance(resp.get("data"), list):
                data = resp["data"][0] if resp["data"] else {}
            else:
                data = resp
        else:
            data = {}

        b64 = ""
        img_url = ""

        if isinstance(data, dict):
            img_url = data.get("url", "")
            b64 = data.get("b64_json", "")

            # Handle data URI (some backends return data-URI instead of b64_json)
            if not b64 and isinstance(img_url, str) and img_url.startswith("data:"):
                b64 = img_url.split(",", 1)[-1]

            # Fetch from HTTP URL (e.g. OpenWebUI file endpoint)
            if not b64 and img_url and not img_url.startswith("data:"):
                full_url = urljoin(base_url, img_url) if not img_url.startswith(("http://", "https://")) else img_url
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                try:
                    _r = httpx.get(full_url, headers=headers, timeout=120)
                    _r.raise_for_status()
                    content_type = _r.headers.get("content-type", "")
                    if "image" in content_type:
                        b64 = _b64.b64encode(_r.content).decode("utf-8")
                except Exception:
                    pass  # Caller has its own URL fallback

        return {
            "b64_json": b64,
            "url": img_url,
            "revised_prompt": data.get("revised_prompt", "") if isinstance(data, dict) else "",
            "mime_type": "image/png",
        }

    try:
        r = httpx.post(url, headers=_api_headers(api_key), json=body, timeout=timeout)
        if r.status_code == 422 and not is_openai:
            # Backend rejected response_format — retry without it
            body.pop("response_format", None)
            r = httpx.post(url, headers=_api_headers(api_key), json=body, timeout=timeout)
        r.raise_for_status()
        return _parse(r.json())
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"OpenAI Image Generation Fehler ({e.response.status_code}): {detail}")
    except Exception as e:
        raise RuntimeError(f"OpenAI Image Generation nicht erreichbar: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Image Editing (img2img) — POST /v1/images/edits
# ═══════════════════════════════════════════════════════════════════════════

def api_edit_image(
    prompt: str,
    image_path: str,
    *,
    api_key: str,
    model: str = "dall-e-2",
    size: str = "1024x1024",
    base_url: str = OPENAI_API_URL,
    timeout: int = 600,
    mask_path: str | None = None,
    steps: int | None = None,
    cfg_scale: float | None = None,
    guidance: float | None = None,
    strength: float | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    seed: int | None = None,
    negative_prompt: str | None = None,
    image_api: str = "",
) -> dict[str, Any]:
    """
    Image Editing (img2img).
    image_api steuert den Endpunkt:
      - "" (leer/default): OpenAI-kompatibel — POST /v1/images/edits multipart/form-data.
        Für echtes OpenAI und sd-server/LocalAI (die /edits unterstützen).
      - "a1111": POST /sdapi/v1/img2img mit JSON body (A1111/Forge/ComfyUI-kompatibel).
    Fallback: wenn /v1/images/edits fehlschlägt → automatisch /sdapi/v1/img2img probieren.
    Returns: {b64_json: str, url: str, revised_prompt: str, mime_type: str}.
    """
    import base64 as _b64
    from pathlib import Path as _Path

    url = _api_url(base_url, "/images/edits")
    is_openai = OPENAI_API_URL in base_url
    img_p = _Path(image_path)
    if not img_p.exists():
        raise RuntimeError(f"Quellbild nicht gefunden: {image_path}")

    img_bytes = img_p.read_bytes()
    img_mime = "image/png"
    _suffix = img_p.suffix.lower()
    if _suffix in (".jpg", ".jpeg"):
        img_mime = "image/jpeg"
    elif _suffix == ".webp":
        img_mime = "image/webp"

    def _parse(resp: dict) -> dict:
        data = (resp.get("data") or [{}])[0]
        b64 = data.get("b64_json", "")
        img_url = data.get("url", "")
        if not b64 and img_url and img_url.startswith("data:"):
            b64 = img_url.split(",", 1)[-1]
        if not b64 and img_url and not img_url.startswith("data:"):
            full_url = urljoin(base_url, img_url) if not img_url.startswith(("http://", "https://")) else img_url
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                _r = httpx.get(full_url, headers=headers, timeout=120)
                _r.raise_for_status()
                if "image" in _r.headers.get("content-type", ""):
                    b64 = _b64.b64encode(_r.content).decode("utf-8")
            except Exception:
                pass
        return {
            "b64_json": b64,
            "url": img_url,
            "revised_prompt": data.get("revised_prompt", "") if isinstance(data, dict) else "",
            "mime_type": "image/png",
        }

    img_b64 = _b64.b64encode(img_bytes).decode("utf-8")
    _api = (image_api or "").strip().lower()

    # --- Hilfsfunktionen für die beiden Protokolle ---

    def _try_openai_edits() -> dict[str, Any]:
        """OpenAI-kompatibel: POST /v1/images/edits mit multipart/form-data.
        Funktioniert mit echtem OpenAI, sd-server und LocalAI."""
        # Extra-Parameter als <sd_cpp_extra_args> im Prompt einbetten (sd-server)
        _prompt = prompt
        _extra: dict[str, Any] = {}
        if not is_openai:
            if steps is not None: _extra["steps"] = steps
            if cfg_scale is not None: _extra["cfg_scale"] = cfg_scale
            if guidance is not None: _extra["guidance"] = guidance
            if strength is not None: _extra["strength"] = strength
            if sampler is not None: _extra["sample_method"] = sampler
            if scheduler is not None: _extra["scheduler"] = scheduler
            if seed is not None: _extra["seed"] = seed
            if negative_prompt is not None: _extra["negative_prompt"] = negative_prompt
            if _extra:
                _prompt = f"{prompt}<sd_cpp_extra_args>{json.dumps(_extra)}</sd_cpp_extra_args>"
        _files: dict[str, Any] = {"image": (img_p.name, img_bytes, img_mime)}
        _form: dict[str, Any] = {"prompt": _prompt, "model": model, "size": size}
        if is_openai:
            _form["response_format"] = "b64_json"
        if mask_path:
            _mp = _Path(mask_path)
            if _mp.exists():
                _files["mask"] = (_mp.name, _mp.read_bytes(), "image/png")
        _hdrs = {}
        if api_key:
            _hdrs["Authorization"] = f"Bearer {api_key}"
        r = httpx.post(url, headers=_hdrs, data=_form, files=_files, timeout=timeout)
        r.raise_for_status()
        return _parse(r.json())

    def _try_a1111_img2img() -> dict[str, Any]:
        """A1111-kompatibel: POST /sdapi/v1/img2img mit JSON body.
        Funktioniert mit A1111, Forge, ComfyUI und sd-server."""
        _w, _h = 1024, 1024
        _sp = size.split("x")
        if len(_sp) == 2:
            try: _w, _h = int(_sp[0]), int(_sp[1])
            except ValueError: pass
        _url = base_url.rstrip("/") + "/sdapi/v1/img2img"
        _body: dict[str, Any] = {
            "prompt": prompt,
            "init_images": [img_b64],
            "denoising_strength": strength if strength is not None else 0.75,
            "width": _w, "height": _h,
        }
        if negative_prompt is not None: _body["negative_prompt"] = negative_prompt
        if steps is not None: _body["steps"] = steps
        if cfg_scale is not None: _body["cfg_scale"] = cfg_scale
        if seed is not None: _body["seed"] = seed
        if sampler is not None: _body["sampler_name"] = sampler
        if scheduler is not None: _body["scheduler"] = scheduler
        if mask_path:
            _mp = _Path(mask_path)
            if _mp.exists():
                _body["mask"] = _b64.b64encode(_mp.read_bytes()).decode("utf-8")
        r = httpx.post(_url, headers=_api_headers(api_key), json=_body, timeout=timeout)
        r.raise_for_status()
        _resp = r.json()
        _imgs = _resp.get("images") or []
        return {"b64_json": _imgs[0] if _imgs else "", "url": "", "revised_prompt": "", "mime_type": "image/png"}

    # --- Routing basierend auf image_api Config ---

    if _api == "a1111":
        # Explizit A1111/Forge konfiguriert → direkt /sdapi/v1/img2img
        try:
            return _try_a1111_img2img()
        except Exception as e:
            raise RuntimeError(f"Image Edit Fehler (/sdapi/v1/img2img): {e}")

    # Default: OpenAI-kompatibel (/v1/images/edits multipart)
    # Bei lokalen Backends: Fallback auf A1111 wenn /edits fehlschlägt
    try:
        return _try_openai_edits()
    except Exception as _edit_err:
        if is_openai:
            raise RuntimeError(f"OpenAI Image Edit Fehler: {_edit_err}")
        _log.info("/v1/images/edits failed (%s), trying /sdapi/v1/img2img fallback", _edit_err)

    try:
        return _try_a1111_img2img()
    except Exception as _a1111_err:
        raise RuntimeError(f"Image Edit fehlgeschlagen — /v1/images/edits: {_edit_err} | /sdapi/v1/img2img: {_a1111_err}")


# ═══════════════════════════════════════════════════════════════════════════
# Capability Checks
# ═══════════════════════════════════════════════════════════════════════════

def model_supports_vision(model: str) -> bool:
    """Die meisten GPT-4-Modelle unterstützen Vision."""
    n = (model or "").lower()
    if "gpt-4o" in n or "gpt-4-turbo" in n or "gpt-4-vision" in n:
        return True
    if "o1" in n or "o3" in n or "o4" in n:
        return True
    return False


def model_supports_tools(model: str) -> bool:
    """Die meisten OpenAI-Modelle unterstützen Function Calling."""
    n = (model or "").lower()
    # Embedding/TTS/Whisper/DALL-E unterstützen keine Tools
    if any(x in n for x in ("embedding", "tts", "whisper", "dall-e", "davinci", "babbage")):
        return False
    return True


def model_supports_thinking(model: str) -> bool:
    """o1, o3, o4-mini unterstützen Reasoning."""
    n = (model or "").lower()
    return any(x in n for x in ("o1", "o3", "o4"))


def model_supports_image_generation(model: str) -> bool:
    """DALL-E und chatgpt-image Modelle unterstützen Image Generation."""
    n = (model or "").lower()
    return "dall-e" in n or "dalle" in n or "chatgpt-image" in n
