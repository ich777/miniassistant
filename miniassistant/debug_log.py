"""
Debug-Logging: Bei server.debug werden Request/Response (Chat/Ollama) und
Serve-Ereignisse in Dateien unter debug/ geschrieben (Rohformat).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _debug_dir(config: dict[str, Any], project_dir: str | None = None) -> Path | None:
    """Verzeichnis debug/ (unter Config-Dir). None wenn debug aus."""
    if not (config.get("server") or {}).get("debug"):
        return None
    from miniassistant.config import get_config_dir
    base = Path(project_dir).resolve() if project_dir else Path(get_config_dir())
    return base / "debug"


def log_chat(
    request_obj: dict[str, Any],
    response_obj: dict[str, Any],
    config: dict[str, Any],
    project_dir: str | None = None,
    *,
    label: str = "chat",
) -> None:
    """
    Schreibt einen Request- und Response-Block nach debug/chat.log (nur bei server.debug).
    request_obj/response_obj werden als JSON (indent=2) geschrieben.
    """
    d = _debug_dir(config, project_dir)
    if not d:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / "chat.log"
        ts = datetime.now(timezone.utc).isoformat()
        block = f"\n--- {label} {ts} ---\nREQUEST:\n{json.dumps(request_obj, ensure_ascii=False, indent=2)}\nRESPONSE:\n{json.dumps(response_obj, ensure_ascii=False, indent=2)}\n"
        path.write_text(path.read_text(encoding="utf-8") + block, encoding="utf-8")
    except Exception:
        pass


def log_serve(message: str, config: dict[str, Any], project_dir: str | None = None) -> None:
    """Schreibt eine Zeile nach debug/serve.log (nur bei server.debug)."""
    d = _debug_dir(config, project_dir)
    if not d:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / "serve.log"
        ts = datetime.now(timezone.utc).isoformat()
        path.write_text(path.read_text(encoding="utf-8") + f"{ts}\t{message}\n", encoding="utf-8")
    except Exception:
        pass
