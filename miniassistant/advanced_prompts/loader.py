"""
Loader für advanced/AIO-Prompts: Kopiert die ausgelieferten Default-Prompt-Dateien
nach agent_dir/advanced_prompts/ falls nicht vorhanden (persistent + user-editierbar,
gleiches Muster wie basic_rules/docs). Group-Räume nutzen den Advanced-Prompt NICHT,
außer ein Raum setzt advanced_prompt explizit.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

_log = logging.getLogger("miniassistant.advanced_prompts")

_DEFAULTS_DIR = Path(__file__).resolve().parent

# Mit dem Package ausgelieferte Default-Prompt-Dateien
PROMPT_FILES = ["aio.md", "aio_caveman.md"]

# Default-Datei wenn advanced_prompt aktiviert aber keine Datei gesetzt ist
DEFAULT_PROMPT_FILE = "aio_caveman.md"


def advanced_prompts_dir(config: dict[str, Any]) -> Path | None:
    """agent_dir/advanced_prompts/ — None wenn kein agent_dir."""
    agent_dir = (config.get("agent_dir") or "").strip()
    if not agent_dir:
        return None
    return Path(agent_dir).expanduser().resolve() / "advanced_prompts"


def ensure_advanced_prompts(config: dict[str, Any]) -> Path | None:
    """Stellt agent_dir/advanced_prompts/ sicher und kopiert fehlende Default-Dateien dorthin.
    Gibt das Verzeichnis zurück (oder None). Überschreibt vorhandene (vom User editierte) Dateien NICHT."""
    target_dir = advanced_prompts_dir(config)
    if not target_dir:
        return None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _log.warning("Konnte advanced_prompts-Verzeichnis nicht erstellen: %s", e)
        return None
    for fname in PROMPT_FILES:
        target = target_dir / fname
        if not target.exists():
            source = _DEFAULTS_DIR / fname
            if source.exists():
                try:
                    shutil.copy2(source, target)
                    _log.info("advanced_prompts/%s erstellt (Default kopiert)", fname)
                except Exception as e:
                    _log.warning("Konnte %s nicht kopieren: %s", fname, e)
    return target_dir


def resolve_prompt_path(config: dict[str, Any], spec: str) -> str | None:
    """Löst eine Prompt-Datei-Angabe auf:
    - absoluter Pfad / enthält '/' → wie angegeben (nach expanduser)
    - reiner Dateiname → agent_dir/advanced_prompts/<name>, sonst Package-Default
    Gibt den Pfad zurück wenn die Datei existiert, sonst None."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if "/" in spec or spec.startswith("~"):
        p = Path(spec).expanduser()
        return str(p) if p.is_file() else None
    # Reiner Dateiname → erst agent_dir, dann Package-Default
    d = advanced_prompts_dir(config)
    if d:
        cand = d / spec
        if cand.is_file():
            return str(cand)
    pkg = _DEFAULTS_DIR / spec
    return str(pkg) if pkg.is_file() else None
