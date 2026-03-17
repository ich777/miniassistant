"""
Plattform-übergreifendes Auth-System für Chat-Clients (Matrix, Discord, …).
Speichert ausstehende Codes und autorisierte Nutzer unter config/auth/.
Migriert automatisch alte Daten aus config/matrix/.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from miniassistant.config import get_config_dir

# Code gültig 30 Minuten
CODE_VALIDITY_SECONDS = 1800

# Rate limiting: max failed attempts per time window
_MAX_FAILED_ATTEMPTS = 5
_RATE_LIMIT_WINDOW = 60  # seconds
_failed_attempts: dict[str, list[float]] = {}  # key → list of timestamps

_migration_done = False
_auth_file_lock = threading.Lock()  # Schützt Read-Modify-Write auf JSON-Dateien


def _auth_dir(config_dir: str | None = None) -> Path:
    """Auth-Verzeichnis: config_dir/auth wenn angegeben (gleiche Config wie die App), sonst get_config_dir()/auth."""
    global _migration_done
    if not _migration_done:
        _migrate_from_matrix()
        _migration_done = True
    if config_dir and str(config_dir).strip():
        d = Path(config_dir).expanduser().resolve() / "auth"
    else:
        d = Path(get_config_dir()).expanduser().resolve() / "auth"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pending_path(config_dir: str | None = None) -> Path:
    return _auth_dir(config_dir) / "pending_codes.json"


def _authorized_path(config_dir: str | None = None) -> Path:
    return _auth_dir(config_dir) / "authorized.json"


def _migrate_from_matrix() -> None:
    """Einmalig: Daten von config/matrix/ nach config/auth/ migrieren."""
    root = Path(get_config_dir()).expanduser().resolve()
    old_dir = root / "matrix"
    new_dir = root / "auth"
    # Alte pending codes migrieren
    old_pending = old_dir / "matrix_pending_codes.json"
    new_pending = new_dir / "pending_codes.json"
    if old_pending.exists() and not new_pending.exists():
        new_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(old_pending, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            # Altes Format: {code: {matrix_user_id, expires_at}} → Neu: {code: {platform, user_id, expires_at}}
            new_data = {}
            if isinstance(old_data, dict):
                for code, entry in old_data.items():
                    if isinstance(entry, dict):
                        new_data[code] = {
                            "platform": "matrix",
                            "user_id": entry.get("matrix_user_id", ""),
                            "expires_at": entry.get("expires_at", 0),
                        }
            with open(new_pending, "w", encoding="utf-8") as f:
                json.dump(new_data, f, ensure_ascii=False, indent=0)
        except Exception:
            pass
    # Alte authorized migrieren
    old_auth = old_dir / "matrix_authorized.json"
    new_auth = new_dir / "authorized.json"
    if old_auth.exists() and not new_auth.exists():
        new_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(old_auth, "r", encoding="utf-8") as f:
                old_list = json.load(f)
            # Altes Format: ["@user:server", ...] → Neu: [{"platform": "matrix", "user_id": "@user:server"}, ...]
            new_list = []
            if isinstance(old_list, list):
                for uid in old_list:
                    if isinstance(uid, str) and uid.strip():
                        new_list.append({"platform": "matrix", "user_id": uid.strip()})
            with open(new_auth, "w", encoding="utf-8") as f:
                json.dump(new_list, f, ensure_ascii=False, indent=0)
        except Exception:
            pass
    # Auch alte Dateien im config-root migrieren (ganz alter Pfad)
    for name in ("matrix_pending_codes.json", "matrix_authorized.json"):
        old_p = root / name
        target = old_dir / name
        if old_p.exists() and not target.exists():
            old_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_p, target)
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
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, prefix=".auth_tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
        os.replace(tmp_str, path)
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def _pending_data(config_dir: str | None = None) -> dict:
    """Lädt pending-Codes, entfernt abgelaufene, gibt Dict zurück."""
    path = _pending_path(config_dir)
    data = _load_json(path, {})
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, dict) and (v.get("expires_at") or 0) > now}


def get_or_generate_code(platform: str, user_id: str, config_dir: str | None = None) -> str:
    """Gibt einen gültigen Code für diese Platform+User-ID zurück; falls schon einer existiert, denselben."""
    uid = (user_id or "").strip()
    plat = (platform or "").strip().lower()
    data = _pending_data(config_dir)
    for code, entry in data.items():
        if (entry.get("platform") or "").strip().lower() == plat and (entry.get("user_id") or "").strip() == uid:
            _save_json(_pending_path(config_dir), data)
            return code
    return generate_code(platform, user_id, config_dir)


def generate_code(platform: str, user_id: str, config_dir: str | None = None) -> str:
    """Erzeugt einen zufälligen Code, speichert ihn mit Ablaufzeit, gibt den Code zurück."""
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    with _auth_file_lock:
        path = _pending_path(config_dir)
        data = _pending_data(config_dir)
        data[code.upper()] = {
            "platform": (platform or "").strip().lower(),
            "user_id": (user_id or "").strip(),
            "expires_at": time.time() + CODE_VALIDITY_SECONDS,
        }
        _save_json(path, data)
    return code


def _normalize_code(raw: str) -> str:
    """Entfernt Präfixe wie '/auth matrix ' oder '/auth discord ' und gibt den Code zurück."""
    raw = (raw or "").strip()
    low = raw.lower()
    for prefix in ("auth matrix", "auth discord", "auth", "/auth matrix", "/auth discord", "/auth", "matrix", "discord"):
        if low.startswith(prefix):
            raw = raw[len(prefix):].strip()
            break
    return "".join(c for c in raw.upper() if c in "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def _check_rate_limit(key: str) -> bool:
    """Returns True if the key is rate-limited (too many failed attempts)."""
    now = time.time()
    attempts = _failed_attempts.get(key, [])
    # Remove expired entries
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    _failed_attempts[key] = attempts
    return len(attempts) >= _MAX_FAILED_ATTEMPTS


def _record_failed_attempt(key: str) -> None:
    """Record a failed auth attempt for rate limiting."""
    now = time.time()
    attempts = _failed_attempts.get(key, [])
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    attempts.append(now)
    _failed_attempts[key] = attempts


def consume_code(code: str, config_dir: str | None = None) -> tuple[str, str] | None:
    """
    Prüft den Code; wenn gültig, trägt den Nutzer in die autorisierte Liste ein,
    entfernt den Code und gibt (platform, user_id) zurück. Sonst None.
    Akzeptiert auch Eingaben wie "/auth matrix 5MHX456J" oder "/auth 5MHX456J".
    config_dir: dasselbe Verzeichnis wie die geladene Config (z. B. config.get("_config_dir")), damit Bot und Web-UI dieselbe Auth-Datei nutzen.
    """
    code = _normalize_code(code)
    if not code:
        return None
    # Rate limit check based on normalized code prefix (first 4 chars as key to group attempts)
    rate_key = f"auth:{config_dir or 'default'}"
    if _check_rate_limit(rate_key):
        return None
    with _auth_file_lock:
        path = _pending_path(config_dir)
        data = _load_json(path, {})
        if not isinstance(data, dict):
            _record_failed_attempt(rate_key)
            return None
        entry = data.pop(code, None)
        if not entry or not isinstance(entry, dict):
            _record_failed_attempt(rate_key)
            return None
        expires = entry.get("expires_at") or 0
        if time.time() > expires:
            _save_json(path, data)
            _record_failed_attempt(rate_key)
            return None
        platform = (entry.get("platform") or "").strip()
        user_id = (entry.get("user_id") or "").strip()
        if not platform or not user_id:
            _save_json(path, data)
            return None
        _save_json(path, data)
    add_authorized(platform, user_id, config_dir)
    return (platform, user_id)


def add_authorized(platform: str, user_id: str, config_dir: str | None = None) -> None:
    """Fügt einen Nutzer zur autorisierten Liste hinzu."""
    plat = (platform or "").strip().lower()
    uid = (user_id or "").strip()
    if not plat or not uid:
        return
    with _auth_file_lock:
        path = _authorized_path(config_dir)
        data = _load_json(path, [])
        if not isinstance(data, list):
            data = []
        # Duplikat-Check
        for entry in data:
            if isinstance(entry, dict) and entry.get("platform") == plat and entry.get("user_id") == uid:
                return
        data.append({"platform": plat, "user_id": uid})
        _save_json(path, data)


def is_authorized(platform: str, user_id: str, config_dir: str | None = None) -> bool:
    """Prüft, ob der Nutzer autorisiert ist."""
    path = _authorized_path(config_dir)
    data = _load_json(path, [])
    if not isinstance(data, list):
        return False
    plat = (platform or "").strip().lower()
    uid = (user_id or "").strip()
    for entry in data:
        if isinstance(entry, dict) and entry.get("platform") == plat and entry.get("user_id") == uid:
            return True
    return False


def list_authorized(platform: str | None = None, config_dir: str | None = None) -> list[dict[str, str]]:
    """Gibt die Liste aller autorisierten Nutzer zurück, optional gefiltert nach Platform."""
    path = _authorized_path(config_dir)
    data = _load_json(path, [])
    if not isinstance(data, list):
        return []
    if platform:
        plat = platform.strip().lower()
        return [e for e in data if isinstance(e, dict) and e.get("platform") == plat]
    return [e for e in data if isinstance(e, dict)]


# ---- Abwärtskompatibilität: alte matrix_auth Funktionen ----
# Diese werden von bestehenden Importen genutzt und leiten auf das neue System weiter.

def get_or_generate_code_matrix(matrix_user_id: str) -> str:
    """Kompatibilitäts-Wrapper für Matrix."""
    return get_or_generate_code("matrix", matrix_user_id)


def is_authorized_matrix(matrix_user_id: str) -> bool:
    """Kompatibilitäts-Wrapper für Matrix."""
    return is_authorized("matrix", matrix_user_id)
