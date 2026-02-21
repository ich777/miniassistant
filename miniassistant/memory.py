"""
Kleines Memory-System: tägliche Dateien (memory/YYYY-MM-DD.md), nur Inhalt (kein Thinking).
Nach jeder Runde wird ein Eintrag angehängt; beim Start wird ein gekürzter Auszug (max Zeilen)
in den System-Prompt eingefügt.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from miniassistant.config import load_config, get_config_dir


def memory_dir(project_dir: str | None = None) -> Path:
    """Verzeichnis für Memory-Dateien (unter Agent-Dir oder get_config_dir()/agent/memory)."""
    config = load_config(project_dir)
    agent = config.get("agent_dir") or str(Path(get_config_dir()) / "agent")
    return Path(agent).expanduser().resolve() / "memory"


def save_summary(summary: str, model_used: str | None = None, project_dir: str | None = None) -> Path:
    """Speichert eine kurze Zusammenfassung (z. B. bei Modellwechsel)."""
    d = memory_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"summary": summary}
    if model_used:
        meta["last_model"] = model_used
    path = d / "last_summary.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=0)
    return path


def load_summary(project_dir: str | None = None) -> tuple[str | None, str | None]:
    """Liest letzte Zusammenfassung und (falls gespeichert) last_model. (summary, last_model)."""
    d = memory_dir(project_dir)
    path = d / "last_summary.json"
    if not path.exists():
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("summary"), data.get("last_model")
    except Exception:
        return None, None


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def append_exchange(
    user_content: str,
    assistant_content: str,
    project_dir: str | None = None,
) -> Path | None:
    """
    Hängt einen User/Assistant-Austausch an die Tages-Datei an.
    Nur Inhalt, kein Thinking. Eine Zeile User:, dann Assistant: (mehrzeilig möglich).
    """
    if not assistant_content and not user_content:
        return None
    d = memory_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_today_iso()}.md"
    user_line = (user_content or "").strip().replace("\n", " ")
    asst_block = (assistant_content or "").strip()
    line = f"User: {user_line}\nAssistant: {asst_block}\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    return path


def _estimate_tokens(text: str) -> int:
    """Konservative Token-Schätzung (~3 Zeichen/Token)."""
    return max(1, int(len(text) / 3.0)) if text else 0


def get_memory_for_prompt(
    project_dir: str | None = None,
    max_lines: int = 400,
    days: int | None = None,
    max_chars_per_line: int | None = None,
    max_tokens: int | None = None,
) -> str | None:
    """
    Liest Memory der letzten `days` Tage; Wert aus Config (memory.days), Default 2 (heute + gestern).
    Bis zu `max_lines` Zeilen. Zeilen auf `max_chars_per_line` gekürzt (Config memory.max_chars_per_line, Default 100). 0 = keine Kürzung.
    Token-Budget: `max_tokens` (Config memory.max_tokens, Default 1500). Stoppt wenn Budget erschöpft.
    Returns None wenn kein Memory existiert.
    """
    config = load_config(project_dir) if (max_chars_per_line is None or days is None or max_tokens is None) else None
    if max_chars_per_line is None:
        max_chars_per_line = int((config.get("memory") or {}).get("max_chars_per_line", 100) or 100)
    if days is None:
        days = int((config.get("memory") or {}).get("days", 2) or 2)
    if max_tokens is None:
        max_tokens = int((config.get("memory") or {}).get("max_tokens", 1500) or 1500)
    d = memory_dir(project_dir)
    if not d.exists():
        return None
    now = datetime.now(timezone.utc)
    all_lines: list[str] = []
    for i in range(days):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        path = d / f"{day}.md"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if max_chars_per_line > 0 and len(line) > max_chars_per_line:
                    line = line[: max_chars_per_line - 1] + "…"
                all_lines.append(line)
        except Exception:
            continue
    if not all_lines:
        return None
    # Chronologisch (älteste zuerst): i=0 heute, i=1 gestern → nach reverse: [gestern…, heute…]
    all_lines.reverse()
    # Nur die neuesten max_lines Zeilen behalten (Ende der Liste = neueste)
    trimmed = all_lines[-max_lines:] if len(all_lines) > max_lines else all_lines
    # Token-Budget: von hinten (neueste) aufbauen, stoppen wenn Budget erschöpft
    if max_tokens > 0:
        budget_lines: list[str] = []
        used = 0
        for line in reversed(trimmed):
            cost = _estimate_tokens(line)
            if used + cost > max_tokens:
                break
            budget_lines.append(line)
            used += cost
        budget_lines.reverse()
        trimmed = budget_lines
    return "\n".join(trimmed).strip() or None
