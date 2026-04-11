"""
Kleines Memory-System: tägliche Dateien (memory/YYYY-MM-DD.md), nur Inhalt (kein Thinking).
Nach jeder Runde wird ein Eintrag angehängt; beim Start wird ein gekürzter Auszug (max Zeilen)
in den System-Prompt eingefügt.

Optional: mempalace-Integration — Exchanges werden parallel als Drawers in mempalace gespeichert,
und der System-Prompt nutzt L0+L1 statt raw dump.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from miniassistant.config import load_config, get_config_dir

_log = logging.getLogger("miniassistant.memory")

# --- Noise-Filter: Diese Exchanges verschwenden nur Context-Tokens ---
_NOISE_PATTERNS: list[re.Pattern[str]] = [
    # Auto-generierte Title/Tag-Requests vom System
    re.compile(r"^###\s*Task:\s*Generate\s+(a\s+)?concise", re.IGNORECASE),
    re.compile(r"^###\s*Task:\s*Generate\s+\d+-\d+\s+broad\s+tags", re.IGNORECASE),
]
_NOISE_PREFIXES: tuple[str, ...] = (
    "[SCHEDULED TASK",  # Scheduled-Task-Preamble (langer Boilerplate-Block)
)


def _is_noise(user_content: str) -> bool:
    """True wenn der User-Text ein Auto-Task oder Schedule-Preamble ist."""
    text = (user_content or "").strip()
    if not text:
        return False
    for prefix in _NOISE_PREFIXES:
        if text.startswith(prefix):
            return True
    for pat in _NOISE_PATTERNS:
        if pat.search(text):
            return True
    return False


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
    user_id: str | None = None,
) -> Path | None:
    """
    Hängt einen User/Assistant-Austausch an die Tages-Datei an.
    Nur Inhalt, kein Thinking. Eine Zeile User:, dann Assistant: (mehrzeilig möglich).
    Filtert Noise (Auto-Title/Tags, Scheduled-Task-Preambles) heraus.
    Optional: schreibt parallel in mempalace (wenn aktiviert).
    """
    if not assistant_content and not user_content:
        return None
    # Noise-Filter: Auto-Tasks und Schedule-Boilerplate nicht speichern
    if _is_noise(user_content):
        _log.debug("Memory noise filtered: %s", (user_content or "")[:80])
        return None
    d = memory_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_today_iso()}.md"
    user_line = (user_content or "").strip().replace("\n", " ")
    asst_block = (assistant_content or "").strip()
    # Format with user_id prefix if available (e.g., for Discord/Matrix tracking)
    user_prefix = f"User [{user_id}]: " if user_id else "User: "
    line = f"{user_prefix}{user_line}\nAssistant: {asst_block}\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    # mempalace Dual-Write (fire-and-forget, Fehler ignorieren)
    _mempalace_store(user_content, assistant_content, project_dir, user_id)
    return path


def _estimate_tokens(text: str) -> int:
    """Konservative Token-Schätzung (~3 Zeichen/Token)."""
    return max(1, int(len(text) / 3.0)) if text else 0


# ---------------------------------------------------------------------------
# mempalace integration (optional, graceful degradation)
# ---------------------------------------------------------------------------

_mempalace_available: bool | None = None  # lazy check


def _mempalace_palace_path(project_dir: str | None = None) -> str:
    """Palace-Pfad: innerhalb von miniassistant agent_dir (neben memory/).

    Default: ~/.config/miniassistant/agent/mempalace/palace
    Kann via mempalace.palace_path in config überschrieben werden.
    """
    mp_cfg = _get_mempalace_config(project_dir)
    custom = (mp_cfg.get("palace_path") or "").strip()
    if custom:
        return str(Path(custom).expanduser().resolve())
    config = load_config(project_dir)
    agent_dir = config.get("agent_dir") or str(Path(get_config_dir()) / "agent")
    return str(Path(agent_dir).expanduser().resolve() / "mempalace" / "palace")


def _check_mempalace(project_dir: str | None = None, auto_init: bool = True) -> bool:
    """Prüft ob mempalace installiert und ein Palace vorhanden ist.

    auto_init=True: erstellt Palace automatisch wenn enabled aber noch nicht vorhanden.
    Prüft ChromaDB-Versionskompatibilität — bei Mismatch wird der Palace
    gelöscht und mit der aktuellen Version neu aufgebaut.
    Prüft außerdem ob bestehende Memory-Dateien importiert wurden (Marker-Datei).
    """
    global _mempalace_available
    if _mempalace_available is not None:
        return _mempalace_available
    try:
        import os as _os
        _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        import chromadb  # noqa: F401
        palace = Path(_mempalace_palace_path(project_dir))
        palace_exists = palace.exists() and (palace / "chroma.sqlite3").exists()

        # ChromaDB version migration: if DB was built with incompatible version, rebuild
        if palace_exists and _chromadb_needs_migration(str(palace)):
            _log.warning("mempalace: ChromaDB version mismatch — deleting old palace and rebuilding...")
            import shutil
            shutil.rmtree(str(palace), ignore_errors=True)
            palace_exists = False  # treat as fresh init

        if palace_exists:
            _mempalace_available = True
            _log.info("mempalace: palace found and version OK — ready")
            _ensure_memory_imported(project_dir, str(palace))
        elif auto_init:
            mp_cfg = _get_mempalace_config(project_dir)
            if mp_cfg.get("enabled", False):
                _log.info("mempalace: no palace found — initializing...")
                palace_path = init_mempalace(project_dir)
                _log.info("mempalace: init complete — importing memories in background...")
                _mempalace_available = True
                _ensure_memory_imported(project_dir, palace_path)
            else:
                _mempalace_available = False
        else:
            _mempalace_available = False
    except ImportError as e:
        _log.warning("mempalace: chromadb not importable — mempalace disabled (%s)", e)
        _mempalace_available = False
    except Exception as e:
        _log.warning("mempalace check failed: %s", e, exc_info=True)
        _mempalace_available = False
    return _mempalace_available


_IMPORT_MARKER = ".memory_imported"
_CHROMADB_VERSION_MARKER = ".chromadb_version"
_import_thread_started = False


def _chromadb_needs_migration(palace_path: str) -> bool:
    """Checks if the palace was built with a different ChromaDB major.minor version.

    If the stored version doesn't match the installed version, the HNSW index
    format may be incompatible and we need to rebuild.
    """
    import chromadb
    installed = chromadb.__version__  # e.g. "0.6.3"
    installed_mm = ".".join(installed.split(".")[:2])  # "0.6"

    marker = Path(palace_path) / _CHROMADB_VERSION_MARKER
    if not marker.exists():
        return True  # no version info → assume migration needed

    stored = marker.read_text(encoding="utf-8").strip()
    stored_mm = ".".join(stored.split(".")[:2])
    return stored_mm != installed_mm


def _write_chromadb_version(palace_path: str) -> None:
    """Write current ChromaDB version to marker file."""
    import chromadb
    marker = Path(palace_path) / _CHROMADB_VERSION_MARKER
    marker.write_text(chromadb.__version__ + "\n", encoding="utf-8")


def _ensure_memory_imported(project_dir: str | None, palace_path: str) -> None:
    """Startet einmaligen Background-Import bestehender Memory-Dateien.

    Prüft Marker-Datei; wenn fehlend, läuft der Import in einem Daemon-Thread
    damit der erste Request nicht minutenlang blockiert wird.
    """
    global _import_thread_started
    marker = Path(palace_path) / _IMPORT_MARKER
    if marker.exists() or _import_thread_started:
        return
    mp_cfg = _get_mempalace_config(project_dir)
    if not mp_cfg.get("enabled", False):
        return
    _import_thread_started = True

    import threading

    def _do_import():
        _log.info("mempalace: importing existing memory files in background...")
        try:
            stats = import_existing_memories(project_dir, palace_path=palace_path)
            marker.write_text(
                f"imported={stats.get('imported', 0)} "
                f"files={stats.get('files', 0)} "
                f"noise={stats.get('skipped_noise', 0)}\n",
                encoding="utf-8",
            )
            _write_chromadb_version(palace_path)
            _log.info("mempalace: background import complete — %s", stats)
        except Exception as e:
            _log.warning("mempalace: background import failed — %s", e)

    t = threading.Thread(target=_do_import, name="mempalace-import", daemon=True)
    t.start()


def _get_mempalace_config(project_dir: str | None = None) -> dict[str, Any]:
    """Liest mempalace-Einstellungen aus miniassistant config."""
    config = load_config(project_dir)
    return config.get("mempalace") or {}


def init_mempalace(project_dir: str | None = None) -> str:
    """Initialisiert den Palace im agent_dir (erstellt Verzeichnis + Collection).

    Schnelle Operation (~1s). Der Import bestehender Memory-Dateien läuft
    asynchron im Hintergrund via _ensure_memory_imported().

    Returns: Palace-Pfad.
    """
    import os as _os
    _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    import chromadb

    palace_path = _mempalace_palace_path(project_dir)
    Path(palace_path).mkdir(parents=True, exist_ok=True)

    # Identity-Datei neben palace/ anlegen wenn nicht vorhanden
    identity_path = str(Path(palace_path).parent / "identity.txt")
    if not Path(identity_path).exists():
        Path(identity_path).write_text(
            "Ich bin MiniAssistant, ein lokaler AI-Assistent.\n"
            "Traits: hilfreich, technisch versiert, deutschsprachig.\n",
            encoding="utf-8",
        )

    client = chromadb.PersistentClient(path=palace_path)
    client.get_or_create_collection("mempalace_drawers")
    _log.info("mempalace initialized at %s", palace_path)

    # Cache invalidieren
    global _mempalace_available
    _mempalace_available = None

    return palace_path


def _parse_exchanges_from_md(text: str) -> list[tuple[str, str]]:
    """Parst User/Assistant-Paare aus einer täglichen Memory-Datei.

    Returns: Liste von (user_content, assistant_content) Tuples.
    """
    exchanges: list[tuple[str, str]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("User: "):
            user_text = line[6:].strip()
            i += 1
            asst_lines: list[str] = []
            if i < len(lines) and lines[i].startswith("Assistant: "):
                asst_lines.append(lines[i][11:])
                i += 1
                while i < len(lines) and not lines[i].startswith("User: "):
                    if lines[i].strip() == "" and i + 1 < len(lines) and lines[i + 1].startswith("User: "):
                        break
                    asst_lines.append(lines[i])
                    i += 1
            asst_text = "\n".join(asst_lines).strip()
            if user_text and asst_text:
                exchanges.append((user_text, asst_text))
        else:
            i += 1
    return exchanges


def import_existing_memories(
    project_dir: str | None = None,
    palace_path: str | None = None,
) -> dict[str, int]:
    """Importiert alle bestehenden täglichen Memory-Dateien in den Palace.

    Wird automatisch bei init_mempalace() aufgerufen, kann auch manuell
    getriggert werden. Überspringt bereits vorhandene Drawer (Deduplizierung
    via doc_id).

    Returns: Dict mit stats {imported, skipped_noise, skipped_existing, files}.
    """
    import hashlib
    import os as _os
    _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    import chromadb

    if palace_path is None:
        palace_path = _mempalace_palace_path(project_dir)
    mp_cfg = _get_mempalace_config(project_dir)
    wing = mp_cfg.get("wing", "miniassistant")
    room = mp_cfg.get("default_room", "conversations")

    mem_d = memory_dir(project_dir)
    if not mem_d.exists():
        return {"imported": 0, "skipped_noise": 0, "skipped_existing": 0, "files": 0}

    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")

    existing_ids: set[str] = set()
    try:
        all_data = col.get(limit=100_000)
        existing_ids = set(all_data.get("ids", []))
    except Exception:
        pass

    stats = {"imported": 0, "skipped_noise": 0, "skipped_existing": 0, "files": 0}
    md_files = sorted(mem_d.glob("????-??-??.md"))

    # Batch-collect all exchanges, then insert in chunks for speed
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict[str, Any]] = []
    BATCH_SIZE = 100

    for md_path in md_files:
        date_str = md_path.stem  # "2026-03-17"
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        stats["files"] += 1

        exchanges = _parse_exchanges_from_md(text)
        for user_text, asst_text in exchanges:
            if _is_noise(user_text):
                stats["skipped_noise"] += 1
                continue

            user_line = user_text.replace("\n", " ").strip()
            asst_block = asst_text.strip()
            if len(asst_block) > 800:
                asst_block = asst_block[:797] + "..."
            content = f"User: {user_line}\nAssistant: {asst_block}"

            doc_id_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            doc_id = f"ma_{date_str}_{doc_id_hash}"

            if doc_id in existing_ids:
                stats["skipped_existing"] += 1
                continue

            batch_ids.append(doc_id)
            batch_docs.append(content)
            batch_metas.append({
                "wing": wing,
                "room": room,
                "hall": "hall_events",
                "source_file": f"miniassistant_{date_str}",
                "date": date_str,
                "importance": 3,
            })
            existing_ids.add(doc_id)

            if len(batch_ids) >= BATCH_SIZE:
                try:
                    col.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
                    stats["imported"] += len(batch_ids)
                except Exception as e:
                    _log.warning("mempalace batch import failed: %s", e)
                batch_ids, batch_docs, batch_metas = [], [], []

    # Flush remaining
    if batch_ids:
        try:
            col.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
            stats["imported"] += len(batch_ids)
        except Exception as e:
            _log.warning("mempalace batch import failed: %s", e)

    _log.info(
        "mempalace import: %d imported, %d noise, %d existing, %d files",
        stats["imported"], stats["skipped_noise"], stats["skipped_existing"], stats["files"],
    )
    return stats


def _mempalace_store(
    user_content: str | None,
    assistant_content: str | None,
    project_dir: str | None = None,
    user_id: str | None = None,
) -> None:
    """Speichert einen Exchange als mempalace-Drawer (fire-and-forget)."""
    mp_cfg = _get_mempalace_config(project_dir)
    if not mp_cfg.get("enabled", False):
        return
    if not _check_mempalace(project_dir):
        return
    try:
        import os as _os
        _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        import chromadb

        palace_path = _mempalace_palace_path(project_dir)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        wing = mp_cfg.get("wing", "miniassistant")
        room = mp_cfg.get("default_room", "conversations")

        user_line = (user_content or "").strip().replace("\n", " ")
        asst_block = (assistant_content or "").strip()
        # Assistent-Antwort auf sinnvolle Länge kürzen für Drawer
        if len(asst_block) > 800:
            asst_block = asst_block[:797] + "..."
        # Add user_id prefix for tracking
        user_prefix = f"User [{user_id}]: " if user_id else "User: "
        content = f"{user_prefix}{user_line}\nAssistant: {asst_block}"

        import hashlib
        doc_id = hashlib.sha256(content.encode()).hexdigest()[:16]
        today = _today_iso()

        col.add(
            ids=[f"ma_{today}_{doc_id}"],
            documents=[content],
            metadatas=[{
                "wing": wing,
                "room": room,
                "hall": "hall_events",
                "source_file": f"miniassistant_{today}",
                "date": today,
                "importance": 3,
            }],
        )
        _log.debug("mempalace drawer stored: ma_%s_%s", today, doc_id[:8])
    except Exception as e:
        _log.debug("mempalace store failed (non-critical): %s", e)


def get_mempalace_memory(
    project_dir: str | None = None,
    max_tokens: int = 900,
    mp_cfg: dict[str, Any] | None = None,
) -> str | None:
    """
    Lädt L0 (Identity) + L1 (Essential Story) aus mempalace.
    Returns formatierter Text für den System-Prompt, oder None wenn nicht verfügbar.
    Typisch ~500-900 Tokens statt ~4500 beim raw dump.
    """
    if mp_cfg is None:
        mp_cfg = _get_mempalace_config(project_dir)
    if not mp_cfg.get("enabled", False):
        return None
    if not _check_mempalace(project_dir):
        return None
    try:
        import os as _os
        _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        from mempalace.layers import Layer0, Layer1

        palace_path = _mempalace_palace_path(project_dir)
        parts: list[str] = []

        # L0: Identity (neben palace/)
        identity_default = str(Path(palace_path).parent / "identity.txt")
        identity_path = mp_cfg.get("identity_path") or identity_default
        l0 = Layer0(identity_path=identity_path)
        l0_text = l0.render()
        if l0_text and "No identity configured" not in l0_text:
            parts.append(l0_text)

        # L1: Essential Story (top moments from palace)
        wing = mp_cfg.get("wing", "miniassistant")
        l1 = Layer1(palace_path=palace_path, wing=wing)
        l1_text = l1.generate()
        if l1_text and "No memories yet" not in l1_text and "No palace found" not in l1_text:
            parts.append(l1_text)

        if not parts:
            return None

        result = "\n\n".join(parts)
        # Token-Budget respektieren
        max_chars = int(max_tokens * 3.5)
        if len(result) > max_chars:
            result = result[:max_chars - 3] + "..."
        return result
    except Exception as e:
        _log.warning("mempalace L0+L1 load failed: %s", e)
        return None


def search_mempalace(
    query: str,
    project_dir: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    n_results: int = 5,
) -> list[dict[str, Any]]:
    """
    Semantische Suche in mempalace. Für das search_memory Tool.
    Returns Liste von Dicts mit 'content', 'similarity', 'wing', 'room', 'date'.
    """
    mp_cfg = _get_mempalace_config(project_dir)
    if not mp_cfg.get("enabled", False) or not _check_mempalace(project_dir):
        return []
    try:
        import os as _os
        _os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        from mempalace.searcher import search_memories

        palace_path = _mempalace_palace_path(project_dir)
        if wing is None:
            wing = mp_cfg.get("wing", "miniassistant")

        data = search_memories(
            query=query,
            palace_path=palace_path,
            wing=wing,
            room=room,
            n_results=n_results,
        )
        if "error" in data:
            return []

        results = []
        for hit in data.get("results", []):
            date = ""
            source = hit.get("source_file", "")
            if source.startswith("miniassistant_"):
                date = source[len("miniassistant_"):]
            results.append({
                "content": hit.get("text", ""),
                "similarity": hit.get("similarity", 0),
                "wing": hit.get("wing", ""),
                "room": hit.get("room", ""),
                "date": date,
            })
        return results
    except Exception as e:
        _log.warning("mempalace search failed: %s", e)
        return []


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
    # Älteste Tage zuerst einlesen (i=days-1 … i=0) → all_lines = [älteste…neueste]
    # Damit ist all_lines[-max_lines:] korrekt die neuesten Zeilen.
    for i in range(days - 1, -1, -1):
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
