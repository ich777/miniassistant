"""
Matrix-Auth: ausstehende Codes (Nutzer → Code, Ablauf) und autorisierte Matrix-User-IDs.
Alles unter config/matrix/ (wie E2EE-Store), damit es Server-Neustarts überlebt.
"""
from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path

from miniassistant.config import get_config_dir

# Code gültig 30 Minuten
CODE_VALIDITY_SECONDS = 1800


_migration_done = False


def _auth_dir() -> Path:
    """Matrix-Datenordner (Auth + E2EE-Store von matrix-nio liegen hier)."""
    global _migration_done
    if not _migration_done:
        _migrate_from_config_root()
        _migration_done = True
    return Path(get_config_dir()).expanduser().resolve() / "matrix"


def _pending_path() -> Path:
    return _auth_dir() / "matrix_pending_codes.json"


def _authorized_path() -> Path:
    return _auth_dir() / "matrix_authorized.json"


def _migrate_from_config_root() -> None:
    """Einmalig: Dateien von config/ nach config/matrix/ verschieben, falls sie dort noch liegen."""
    root = Path(get_config_dir()).expanduser().resolve()
    dest = root / "matrix"
    for name in ("matrix_pending_codes.json", "matrix_authorized.json"):
        old_p, new_p = root / name, dest / name
        if old_p.exists() and not new_p.exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_p, new_p)
            old_p.unlink()


def _load_json(path: Path, default: dict | list) -> dict | list:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def _pending_data() -> dict:
    """Lädt pending-Codes, entfernt abgelaufene, gibt Dict zurück."""
    path = _pending_path()
    data = _load_json(path, {})
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, dict) and (v.get("expires_at") or 0) > now}


def get_or_generate_code(matrix_user_id: str) -> str:
    """Gibt einen gültigen Code für diese User-ID zurück; falls schon einer existiert, denselben (kein Spam)."""
    uid = (matrix_user_id or "").strip()
    data = _pending_data()
    for code, entry in data.items():
        if (entry.get("matrix_user_id") or "").strip() == uid:
            # Abgelaufene Einträge aus der Datei entfernen
            _save_json(_pending_path(), data)
            return code
    return generate_code(matrix_user_id)


def generate_code(matrix_user_id: str) -> str:
    """Erzeugt einen zufälligen Code, speichert ihn mit Ablaufzeit, gibt den Code zurück."""
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    path = _pending_path()
    data = _pending_data()
    data[code.upper()] = {"matrix_user_id": (matrix_user_id or "").strip(), "expires_at": time.time() + CODE_VALIDITY_SECONDS}
    _save_json(path, data)
    return code


def _normalize_code(raw: str) -> str:
    """Entfernt Präfix wie '/auth matrix ' und gibt den Code in Großbuchstaben zurück."""
    raw = (raw or "").strip()
    if "auth matrix" in raw.lower():
        raw = raw.lower().split("auth matrix", 1)[-1].strip()
    return "".join(c for c in raw.upper() if c in "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def consume_code(code: str) -> str | None:
    """
    Prüft den Code; wenn gültig, trägt die Matrix-User-ID in die autorisierte Liste ein,
    entfernt den Code und gibt die matrix_user_id zurück. Sonst None.
    Akzeptiert auch Eingaben wie "/auth matrix 5MHX456J" – nur der Code wird verwendet.
    """
    code = _normalize_code(code)
    if not code:
        return None
    path = _pending_path()
    data = _load_json(path, {})
    if not isinstance(data, dict):
        return None
    entry = data.pop(code, None)
    if not entry or not isinstance(entry, dict):
        return None
    expires = entry.get("expires_at") or 0
    if time.time() > expires:
        _save_json(path, data)
        return None
    matrix_user_id = (entry.get("matrix_user_id") or "").strip()
    if not matrix_user_id:
        _save_json(path, data)
        return None
    _save_json(path, data)
    add_authorized(matrix_user_id)
    return matrix_user_id


def add_authorized(matrix_user_id: str) -> None:
    """Fügt eine Matrix-User-ID zur autorisierten Liste hinzu."""
    path = _authorized_path()
    data = _load_json(path, [])
    if not isinstance(data, list):
        data = []
    uid = (matrix_user_id or "").strip()
    if uid and uid not in data:
        data.append(uid)
        _save_json(path, data)


def is_authorized(matrix_user_id: str) -> bool:
    """Prüft, ob die Matrix-User-ID autorisiert ist."""
    path = _authorized_path()
    data = _load_json(path, [])
    if not isinstance(data, list):
        return False
    return (matrix_user_id or "").strip() in data


def list_authorized() -> list[str]:
    """Gibt die Liste aller autorisierten Matrix-User-IDs zurück."""
    path = _authorized_path()
    data = _load_json(path, [])
    if not isinstance(data, list):
        return []
    return list(data)
