"""
Loader für basic_rules: Kopiert Default-Regeldateien nach agent_dir/basic_rules/
falls nicht vorhanden, liest sie ein und hält sie im RAM-Cache.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

_log = logging.getLogger("miniassistant.basic_rules")

# Package-Verzeichnis mit Default-Templates
_DEFAULTS_DIR = Path(__file__).resolve().parent

# Welche Dateien als Defaults ausgeliefert werden
RULE_FILES = [
    "safety.md",
    "exec_behavior.md",
    "knowledge_verification.md",
    "language.md",
    "subagent.md",
]

# RAM-Cache: {dateiname: inhalt}
_cache: dict[str, str] = {}
_cache_loaded: bool = False


def _ensure_rules_dir(agent_dir: str) -> Path | None:
    """Stellt sicher, dass agent_dir/basic_rules/ existiert. Gibt den Pfad zurück oder None."""
    if not agent_dir:
        return None
    base = Path(agent_dir).expanduser().resolve()
    rules_dir = base / "basic_rules"
    try:
        rules_dir.mkdir(parents=True, exist_ok=True)
        return rules_dir
    except Exception as e:
        _log.warning("Konnte basic_rules-Verzeichnis nicht erstellen: %s", e)
        return None


def ensure_and_load(config: dict[str, Any]) -> dict[str, str]:
    """
    Haupteinstiegspunkt:
    1. Prüft ob agent_dir/basic_rules/ existiert, erstellt es falls nötig
    2. Kopiert fehlende Default-Dateien dorthin
    3. Liest alle .md-Dateien ein und cached sie im RAM
    4. Bei wiederholtem Aufruf wird der Cache zurückgegeben

    Returns: dict {dateiname: inhalt}
    """
    global _cache, _cache_loaded
    if _cache_loaded and _cache:
        return dict(_cache)

    agent_dir = (config.get("agent_dir") or "").strip()
    rules_dir = _ensure_rules_dir(agent_dir)

    if rules_dir:
        # Fehlende Dateien aus Defaults kopieren
        for fname in RULE_FILES:
            target = rules_dir / fname
            if not target.exists():
                source = _DEFAULTS_DIR / fname
                if source.exists():
                    try:
                        shutil.copy2(source, target)
                        _log.info("basic_rules/%s erstellt (Default kopiert)", fname)
                    except Exception as e:
                        _log.warning("Konnte %s nicht kopieren: %s", fname, e)

        # Alle .md-Dateien aus dem User-Verzeichnis lesen (auch eigene)
        for p in sorted(rules_dir.iterdir()):
            if p.is_file() and p.suffix.lower() == ".md":
                try:
                    _cache[p.name] = p.read_text(encoding="utf-8").strip()
                except Exception as e:
                    _log.warning("Konnte %s nicht lesen: %s", p.name, e)
    else:
        # Kein agent_dir → Defaults aus Package direkt laden
        for fname in RULE_FILES:
            source = _DEFAULTS_DIR / fname
            if source.exists():
                try:
                    _cache[fname] = source.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

    _cache_loaded = True
    return dict(_cache)


def get_rule(name: str) -> str:
    """Gibt den Inhalt einer gecachten Regeldatei zurück (leer wenn nicht vorhanden)."""
    return _cache.get(name, "")


def get_all_rules() -> dict[str, str]:
    """Gibt alle gecachten Regeln zurück."""
    return dict(_cache)


def reload(config: dict[str, Any]) -> dict[str, str]:
    """Cache leeren und neu laden (z.B. nach Config-Änderung)."""
    global _cache, _cache_loaded
    _cache = {}
    _cache_loaded = False
    return ensure_and_load(config)
