"""
Loader für docs: Kopiert Default-Dokumentationsdateien nach agent_dir/docs/
falls nicht vorhanden. Dateien werden on-demand per cat gelesen (kein RAM-Cache).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

_log = logging.getLogger("miniassistant.docs")

# Package-Verzeichnis mit Default-Templates
_DEFAULTS_DIR = Path(__file__).resolve().parent

# Welche Dateien als Defaults ausgeliefert werden
DOC_FILES = [
    "API_REFERENCE.md",
    "AVATARS.md",
    "CONFIG_REFERENCE.md",
    "CONTEXT_SIZE.md",
    "DEBATE.md",
    "DISCORD.md",
    "GITHUB.md",
    "IMAGE_GENERATION.md",
    "MATRIX.md",
    "PLANNING.md",
    "PROVIDERS.md",
    "SCHEDULES.md",
    "SEARCH_ENGINES.md",
    "SUBAGENTS.md",
    "VISION.md",
]


def _ensure_docs_dir(agent_dir: str) -> Path | None:
    """Stellt sicher, dass agent_dir/docs/ existiert. Gibt den Pfad zurück oder None."""
    if not agent_dir:
        return None
    base = Path(agent_dir).expanduser().resolve()
    docs_dir = base / "docs"
    try:
        docs_dir.mkdir(parents=True, exist_ok=True)
        return docs_dir
    except Exception as e:
        _log.warning("Konnte docs-Verzeichnis nicht erstellen: %s", e)
        return None


def ensure_docs(config: dict[str, Any]) -> Path | None:
    """
    Haupteinstiegspunkt:
    1. Prüft ob agent_dir/docs/ existiert, erstellt es falls nötig
    2. Kopiert fehlende Default-Dateien dorthin

    Returns: Path zum docs-Verzeichnis oder None
    """
    agent_dir = (config.get("agent_dir") or "").strip()
    docs_dir = _ensure_docs_dir(agent_dir)

    if not docs_dir:
        return None

    # Fehlende Dateien aus Defaults kopieren
    for fname in DOC_FILES:
        target = docs_dir / fname
        if not target.exists():
            source = _DEFAULTS_DIR / fname
            if source.exists():
                try:
                    shutil.copy2(source, target)
                    _log.info("docs/%s erstellt (Default kopiert)", fname)
                except Exception as e:
                    _log.warning("Konnte %s nicht kopieren: %s", fname, e)

    return docs_dir


def docs_dir_path(config: dict[str, Any]) -> Path | None:
    """Gibt den Pfad zum docs-Verzeichnis zurück (agent_dir/docs/ oder Package-Fallback)."""
    agent_dir = (config.get("agent_dir") or "").strip()
    if agent_dir:
        p = Path(agent_dir).expanduser().resolve() / "docs"
        if p.is_dir():
            return p
    # Fallback: Package-Verzeichnis
    return _DEFAULTS_DIR if _DEFAULTS_DIR.is_dir() else None
