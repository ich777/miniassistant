"""
Raw OpenAI Proxy Endpunkt – Direkte Weiterleitung an Provider ohne Agent-Context.

Stellt /raw/v1/ bereit, der Requests unverändert an die konfigurierten Provider
(llama-swap, OpenAI, DeepSeek, etc.) weiterleitet. Kein System-Prompt, kein Memory,
kein Agent-Kontext – rein Proxy-Funktionalität.

Auth: Optional über raw_proxy.token (wenn konfiguriert), sonst ohne Auth.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from miniassistant.config import load_config

_log = logging.getLogger("miniassistant.raw_proxy")

router = APIRouter(prefix="/raw/v1", tags=["Raw OpenAI Proxy"])


# ---------------------------------------------------------------------------
#  Auth helper
# ---------------------------------------------------------------------------

def _require_token(request: Request) -> None:
    """Prüft Token für Raw-Proxy."""
    import secrets as _secrets
    config = load_config()
    raw_cfg = config.get("raw_proxy") or {}
    if not raw_cfg.get("enabled", False):
        return  # Raw-Proxy deaktiviert → alles erlauben

    expected = raw_cfg.get("token")
    if not expected:
        raise HTTPException(status_code=503, detail="Raw proxy token not initialized — restart the server")

    auth = request.headers.get("Authorization")
    token = None
    if auth and auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.query_params.get("token")
    if token and _secrets.compare_digest(token, expected):
        return

    raise HTTPException(status_code=401, detail="Invalid or missing token")


def _is_model_allowed(raw_cfg: dict[str, Any], model_id: str) -> bool:
    """Prüft ob ein Modell über den Raw-Proxy erlaubt ist.
    allowed_models: Liste — wenn leer/nicht gesetzt, sind ALLE Modelle erlaubt.
    Matching: 'qwen3-35b-a3b' trifft 'llama-swap/qwen3-35b-a3b' (Suffix-Match)."""
    allowed = raw_cfg.get("allowed_models") or []
    if not allowed:
        return True
    model_lower = model_id.lower()
    model_suffix = model_id.split("/", 1)[-1].lower() if "/" in model_id else model_lower
    for entry in allowed:
        e = str(entry).lower()
        e_suffix = e.split("/", 1)[-1] if "/" in e else e
        if e == model_lower or e_suffix == model_suffix:
            return True
    return False


def _get_provider_for_model(config: dict[str, Any], model_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Findet den Provider für ein Modell. Gibt (provider_name, provider_config) zurück."""
    providers = config.get("providers") or {}
    
    # Modell-ID kann Format haben: "modelname" oder "provider/modelname"
    if "/" in model_id:
        provider_name, _ = model_id.split("/", 1)
        if provider_name in providers:
            return provider_name, providers[provider_name]
    
    # Durch alle Provider suchen
    for prov_name, prov_cfg in providers.items():
        if not isinstance(prov_cfg, dict):
            continue
        
        prov_type = str(prov_cfg.get("type", "ollama")).lower()
        
        # Nur OpenAI-kompatible Provider für diesen Proxy
        if prov_type not in ("openai", "openai-compat", "deepseek"):
            continue
        
        # Prüfen ob Modell in diesem Provider konfiguriert ist
        prov_models = prov_cfg.get("models") or {}
        default_model = prov_models.get("default")
        aliases = prov_models.get("aliases") or {}
        model_list = prov_models.get("list") or []
        
        all_models = set()
        if default_model:
            all_models.add(default_model)
        all_models.update(aliases.values())
        all_models.update(model_list)
        
        # Alias auflösen
        resolved_model = aliases.get(model_id, model_id)
        
        if resolved_model in all_models or model_id in all_models:
            return prov_name, prov_cfg
    
    return None, None


def _get_provider_url_and_key(provider_cfg: dict[str, Any]) -> tuple[str, str | None]:
    """Extrahiert base_url und api_key aus Provider-Konfiguration."""
    base_url = provider_cfg.get("base_url", "https://api.openai.com")
    # base_url kann /v1 enthalten oder nicht
    if not base_url.endswith("/"):
        base_url += "/"
    base_url = base_url.rstrip("/")  # Slash entfernen fuer saubere URL-Konstruktion
    api_key = provider_cfg.get("api_key")
    return base_url, api_key


# ---------------------------------------------------------------------------
#  GET /raw/v1/models
# ---------------------------------------------------------------------------

@router.get("/models")
async def list_models(request: Request):
    """Listet alle Modelle von allen konfigurierten OpenAI-kompatiblen Providern."""
    _require_token(request)

    config = load_config()
    raw_cfg = config.get("raw_proxy") or {}

    if not raw_cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="Raw proxy not enabled")

    def _fetch_all_models():
        all_models = []
        providers = config.get("providers") or {}

        for prov_name, prov_cfg in providers.items():
            if not isinstance(prov_cfg, dict):
                continue

            prov_type = str(prov_cfg.get("type", "ollama")).lower()
            if prov_type not in ("openai", "openai-compat", "deepseek"):
                continue

            base_url, api_key = _get_provider_url_and_key(prov_cfg)

            try:
                _b = base_url.rstrip("/")
                url = f"{_b}/models" if _b.endswith("/v1") else f"{_b}/v1/models"
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                r = httpx.get(url, headers=headers, timeout=10)
                r.raise_for_status()
                data = r.json()

                for m in data.get("data") or []:
                    model_id = m.get("id", "")
                    full_id = f"{prov_name}/{model_id}"
                    if model_id and _is_model_allowed(raw_cfg, full_id):
                        all_models.append({
                            "id": full_id,
                            "object": "model",
                            "created": m.get("created", 0),
                            "owned_by": prov_name,
                        })
            except Exception as e:
                _log.warning("Failed to fetch models from %s: %s", prov_name, e)
                continue
        return all_models

    from miniassistant.web.app import _chat_executor
    loop = asyncio.get_event_loop()
    all_models = await loop.run_in_executor(_chat_executor, _fetch_all_models)

    return JSONResponse({"object": "list", "data": all_models})


# ---------------------------------------------------------------------------
#  POST /raw/v1/chat/completions
# ---------------------------------------------------------------------------

@router.post("/chat/completions")
async def chat_completions(request: Request):
    """Leitet Chat-Completion Request direkt an den Provider weiter."""
    _require_token(request)
    
    config = load_config()
    raw_cfg = config.get("raw_proxy") or {}
    
    if not raw_cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="Raw proxy not enabled")
    
    body = await request.json()
    model = body.get("model", "")
    
    if not model:
        raise HTTPException(status_code=400, detail="model parameter required")

    if not _is_model_allowed(raw_cfg, model):
        raise HTTPException(status_code=403, detail=f"Model not allowed via raw proxy: {model}")

    # Provider für Modell finden
    prov_name, prov_cfg = _get_provider_for_model(config, model)
    if not prov_cfg:
        raise HTTPException(status_code=400, detail=f"No provider found for model: {model}")

    # Modell-Name auflösen (Alias → echter Modellname)
    prov_models = prov_cfg.get("models") or {}
    aliases = prov_models.get("aliases") or {}
    resolved_model = aliases.get(model, model)
    if "/" in resolved_model:
        resolved_model = resolved_model.split("/", 1)[-1]
    
    base_url, api_key = _get_provider_url_and_key(prov_cfg)
    
    # Body mit aufgelöstem Modellnamen kopieren
    body = {**body, "model": resolved_model}
    
    # Request an Provider weiterleiten
    _b = base_url.rstrip("/")
    url = f"{_b}/chat/completions" if _b.endswith("/v1") else f"{_b}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    stream = body.get("stream", False)
    
    # Timeout: kurze connect, lange read (Model-Laden bis 5 Min)
    _timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=300.0)

    from miniassistant.web.app import _chat_executor

    try:
        if stream:
            from miniassistant.chat_loop import _iter_with_keepalive
            _KEEPALIVE_SSE = 'data: {"choices":[{"delta":{},"index":0,"finish_reason":null}]}\n\n'

            def _upstream():
                """Sync-Generator: streamt vom Provider (Context bleibt offen)."""
                with httpx.Client(timeout=_timeout) as client:
                    with client.stream("POST", url, headers=headers, json=body) as r:
                        r.raise_for_status()
                        for chunk in r.iter_text():
                            yield chunk

            def _stream_with_keepalive():
                for item in _iter_with_keepalive(_upstream):
                    if item is None:
                        yield _KEEPALIVE_SSE
                    else:
                        yield item

            async def _async_stream():
                """Sync-Generator im dedizierten Chat-Threadpool iterieren."""
                _loop = asyncio.get_event_loop()
                gen = _stream_with_keepalive()
                _sentinel = object()
                while True:
                    chunk = await _loop.run_in_executor(_chat_executor, lambda: next(gen, _sentinel))
                    if chunk is _sentinel:
                        break
                    yield chunk

            return StreamingResponse(
                _async_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Non-Streaming Response — im Threadpool ausführen, damit Event-Loop frei bleibt
            loop = asyncio.get_event_loop()
            r = await loop.run_in_executor(
                _chat_executor,
                lambda: httpx.post(url, headers=headers, json=body, timeout=_timeout),
            )
            r.raise_for_status()
            return JSONResponse(r.json())

    except httpx.HTTPStatusError as e:
        _log.error("Provider error: %s %s", e.response.status_code, e.response.text[:200])
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        _log.error("Proxy error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
#  POST /raw/v1/completions (Text Completion, optional)
# ---------------------------------------------------------------------------

@router.post("/completions")
async def completions(request: Request):
    """Leidet Text-Completion Request direkt an den Provider weiter."""
    _require_token(request)
    
    config = load_config()
    raw_cfg = config.get("raw_proxy") or {}
    
    if not raw_cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="Raw proxy not enabled")
    
    body = await request.json()
    model = body.get("model", "")
    
    if not model:
        raise HTTPException(status_code=400, detail="model parameter required")
    
    prov_name, prov_cfg = _get_provider_for_model(config, model)
    if not prov_cfg:
        raise HTTPException(status_code=400, detail=f"No provider found for model: {model}")
    
    base_url, api_key = _get_provider_url_and_key(prov_cfg)
    _b = base_url.rstrip("/")
    url = f"{_b}/completions" if _b.endswith("/v1") else f"{_b}/v1/completions"
    
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    from miniassistant.web.app import _chat_executor
    try:
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(
            _chat_executor,
            lambda: httpx.post(url, headers=headers, json=body, timeout=120),
        )
        r.raise_for_status()
        return JSONResponse(r.json())
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
