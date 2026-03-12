"""Formatiert Antworttext für TTS.

Entfernt Markdown, extrahiert Tabellen und Codeblöcke als visuelle Inhalte
die separat als Text gesendet werden.
"""
from __future__ import annotations

import re


def format_for_voice(text: str) -> tuple[str, str]:
    """Verarbeitet Antworttext für Sprachausgabe.

    Returns:
        voice_text: bereinigter Text für TTS (kein Markdown)
        visual_content: extrahierte Tabellen/Codeblöcke zum separaten Senden als Text
    """
    visual: list[str] = []

    # Codeblöcke extrahieren
    def _extract_code(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        visual.append(m.group(0).strip())
        label = f"{lang}-" if lang else ""
        return f"[{label}Codeblock im Chat]"

    text = re.sub(r"```(\w*)\n?(.*?)```", _extract_code, text, flags=re.DOTALL)

    # Tabellen extrahieren
    def _extract_table(m: re.Match) -> str:
        table = m.group(0).strip()
        visual.append(table)
        data_rows = [l for l in table.splitlines()
                     if "|" in l and not re.match(r"^\|[-| :]+\|$", l.strip())]
        count = max(0, len(data_rows) - 1)  # minus Header
        return f"[Tabelle mit {count} Zeilen im Chat]"

    text = re.sub(r"(\|.+\|[ \t]*\n)+", _extract_table, text)

    # Inline-Code → plain text
    text = re.sub(r"`([^`\n]+)`", r"\1", text)

    # Markdown-Formatierung entfernen
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)   # Links → Label
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text, "\n\n".join(visual)
