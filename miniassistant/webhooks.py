"""
Webhooks: external HTTP-triggered autonomous tasks.

Persistence: webhooks.json next to schedules.json in the config dir.
Outputs:     <workspace>/webhooks/<name|id>/<timestamp>.txt
Rules:       basic_rules/webhook.md is injected after the autonomy prefix.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from miniassistant.config import get_config_dir, load_config

logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     [webhooks] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_OUTPUT_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")

# Per-webhook lock for serialized mode
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# Sliding-window rate limit state: {webhook_id: [timestamps]}
_rate_state: dict[str, list[float]] = {}
_rate_guard = threading.Lock()


def _webhooks_path() -> Path:
    return Path(get_config_dir()) / "webhooks.json"


def _load() -> list[dict[str, Any]]:
    p = _webhooks_path()
    if not p.exists():
        return []
    try:
        return json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        return []


def _save(items: list[dict[str, Any]]) -> None:
    p = _webhooks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=0)


def _webhook_cfg() -> dict[str, Any]:
    cfg = load_config()
    wh = cfg.get("webhooks") or {}
    if wh is False:
        return {"enabled": False}
    if isinstance(wh, dict):
        return wh
    return {"enabled": False}


def is_enabled() -> bool:
    return bool(_webhook_cfg().get("enabled", False))


def _output_dir(item: dict[str, Any]) -> Path:
    cfg = load_config()
    workspace = (cfg.get("workspace") or "").strip()
    if not workspace:
        workspace = str(Path.home() / "workspace")
    handle = item.get("name") or (item.get("id", "") or "anon")[:8]
    return (Path(workspace) / "webhooks" / handle).resolve()


def _safe_output_name(name: str | None) -> str:
    if not name:
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".txt"
    base = Path(name).name  # strip any path components
    if not _OUTPUT_NAME_RE.match(base):
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".txt"
    return base


def list_webhooks() -> list[dict[str, Any]]:
    return _load()


def find_by_token(token: str) -> dict[str, Any] | None:
    """Constant-time token lookup."""
    if not token:
        return None
    items = _load()
    found = None
    for it in items:
        stored = it.get("token") or ""
        if stored and hmac.compare_digest(stored, token):
            found = it
    return found


def find_by_id(wid_prefix: str) -> dict[str, Any] | None:
    if not wid_prefix:
        return None
    matches = [w for w in _load() if (w.get("id") or "").startswith(wid_prefix)]
    if len(matches) == 1:
        return matches[0]
    return None


def add_webhook(
    *,
    name: str = "",
    prompt: str = "",
    client: str | None = None,
    room_id: str | None = None,
    channel_id: str | None = None,
    model: str | None = None,
    silent: bool = False,
    save_output: bool = True,
) -> tuple[bool, dict[str, Any] | str]:
    name = (name or "").strip()
    if name and not _NAME_RE.match(name):
        return False, "name must match ^[a-z0-9][a-z0-9_-]{0,63}$"
    items = _load()
    if name and any(w.get("name") == name for w in items):
        return False, f"name '{name}' already exists"
    wid = uuid.uuid4().hex
    item = {
        "id": wid,
        "name": name or None,
        "token": secrets.token_urlsafe(32),
        "prompt": (prompt or "").strip(),
        "client": client,
        "room_id": room_id,
        "channel_id": channel_id,
        "model": (model or "").strip() or None,
        "silent": bool(silent),
        "save_output": bool(save_output),
        "created_at": datetime.now().astimezone().isoformat(),
        "last_fired": None,
        "last_error": None,
    }
    items.append(item)
    _save(items)
    return True, item


def remove_webhook(wid_or_prefix: str, *, purge_outputs: bool = False) -> tuple[bool, str]:
    items = _load()
    matches = [w for w in items if (w.get("id") or "").startswith(wid_or_prefix) or w.get("name") == wid_or_prefix]
    if not matches:
        return False, "not found"
    if len(matches) > 1:
        return False, f"{len(matches)} matches — be more specific"
    target = matches[0]
    remaining = [w for w in items if w.get("id") != target.get("id")]
    _save(remaining)
    if purge_outputs:
        try:
            d = _output_dir(target)
            if d.exists():
                for f in d.iterdir():
                    if f.is_file():
                        f.unlink()
                d.rmdir()
        except Exception as e:
            logger.warning("purge outputs failed: %s", e)
    return True, target.get("id", "")[:8]


def update_webhook(wid: str, patch: dict[str, Any]) -> tuple[bool, str]:
    items = _load()
    target = next((w for w in items if w.get("id") == wid), None)
    if not target:
        return False, "not found"
    for k in ("name", "prompt", "client", "room_id", "channel_id", "model", "silent", "save_output"):
        if k in patch:
            v = patch[k]
            if k == "name":
                v = (v or "").strip() or None
                if v and not _NAME_RE.match(v):
                    return False, "invalid name"
                if v and any(w.get("name") == v and w.get("id") != wid for w in items):
                    return False, f"name '{v}' already exists"
            target[k] = v
    _save(items)
    return True, "ok"


def _rate_check(wid: str) -> bool:
    """Sliding-window rate limit. Returns True if allowed, False if over limit."""
    cfg = _webhook_cfg()
    limit = int(cfg.get("rate_limit_per_min", 10))
    now = time.time()
    with _rate_guard:
        bucket = _rate_state.setdefault(wid, [])
        cutoff = now - 60.0
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
    return True


def _get_lock(wid: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(wid)
        if not lk:
            lk = threading.Lock()
            _locks[wid] = lk
        return lk


def _sweep_outputs(item: dict[str, Any]) -> None:
    """Keep only the newest N outputs (per webhook)."""
    cfg = _webhook_cfg()
    keep = int(cfg.get("output_keep_last", 10))
    if keep <= 0:
        return
    d = _output_dir(item)
    if not d.exists():
        return
    files = sorted([f for f in d.iterdir() if f.is_file()], key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except Exception:
            pass


def sweep_all_outputs() -> None:
    """Called once on MA startup to enforce retention."""
    for item in _load():
        try:
            _sweep_outputs(item)
        except Exception as e:
            logger.warning("sweep failed for %s: %s", item.get("id", "?")[:8], e)


def _path_within(target: Path, base: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _save_output(item: dict[str, Any], content: str | bytes, output_name: str | None) -> Path:
    d = _output_dir(item)
    d.mkdir(parents=True, exist_ok=True)
    name = _safe_output_name(output_name)
    target = (d / name).resolve()
    if not _path_within(target, d):
        raise ValueError("invalid output path")
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    _sweep_outputs(item)
    return target


def list_outputs(item: dict[str, Any]) -> list[dict[str, Any]]:
    d = _output_dir(item)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        st = f.stat()
        out.append({"name": f.name, "bytes": st.st_size, "mtime": int(st.st_mtime)})
    return out


def read_output(item: dict[str, Any], name: str | None = None) -> tuple[Path, bytes] | None:
    d = _output_dir(item)
    if not d.exists():
        return None
    if name is None:
        files = sorted([f for f in d.iterdir() if f.is_file()], key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return None
        f = files[0]
    else:
        safe = _safe_output_name(name)
        f = (d / safe).resolve()
        if not _path_within(f, d):
            return None
        if not f.exists() or not f.is_file():
            return None
    return f, f.read_bytes()


def _set_last(item_id: str, *, last_fired: str | None = None, last_error: dict[str, Any] | None = "_KEEP_") -> None:
    items = _load()
    for it in items:
        if it.get("id") == item_id:
            if last_fired is not None:
                it["last_fired"] = last_fired
            if last_error != "_KEEP_":
                it["last_error"] = last_error
            break
    _save(items)


def _build_prompt(item: dict[str, Any], extra_context: str, prompt_override: str) -> str | None:
    """Assemble the final prompt: autonomy_prefix + webhook rules + extra_context + base."""
    base = (prompt_override or "").strip() or (item.get("prompt") or "").strip()
    if not base:
        return None
    from miniassistant.scheduler import autonomy_prefix
    from miniassistant.basic_rules.loader import get_rule, ensure_and_load
    ensure_and_load(load_config())  # ensure cache populated
    webhook_rules = get_rule("webhook.md") or ""
    parts = [autonomy_prefix("WEBHOOK TASK")]
    if webhook_rules:
        parts.append("WEBHOOK EXECUTION RULES (highest precedence):\n" + webhook_rules + "\n")
    if extra_context:
        parts.append((extra_context or "").strip() + "\n\n" + base)
    else:
        parts.append(base)
    return "\n".join(parts)


def fire(
    item: dict[str, Any],
    *,
    extra_context: str = "",
    prompt_override: str = "",
    client_override: str | None = None,
    room_id_override: str | None = None,
    channel_id_override: str | None = None,
    silent_override: bool | None = None,
    save_output_override: bool | None = None,
    output_name: str | None = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Execute a webhook task. Returns dict with status/response/output_path.
    Caller must enforce rate-limit + auth before invoking.
    Honors webhooks.parallel and webhooks.max_retries from config.
    """
    cfg = _webhook_cfg()
    parallel = bool(cfg.get("parallel", True))
    max_retries = max(0, int(cfg.get("max_retries", 3)))
    wid = item.get("id") or ""
    name = item.get("name") or wid[:8]

    full_prompt = _build_prompt(item, extra_context, prompt_override)
    if not full_prompt:
        return {"ok": False, "error": "no prompt available (neither webhook default nor body.prompt)"}

    silent = silent_override if silent_override is not None else bool(item.get("silent", False))
    save_output = save_output_override if save_output_override is not None else bool(item.get("save_output", True))
    client = client_override or item.get("client")
    room_id = room_id_override or item.get("room_id")
    channel_id = channel_id_override or item.get("channel_id")
    model = (model_override or item.get("model") or "").strip() or None

    def _execute() -> str:
        # Reuse scheduler._run_prompt for autonomy-prefixed execution
        from miniassistant.scheduler import _run_prompt
        return _run_prompt(full_prompt, model=model, scheduled_prompt=(prompt_override or item.get("prompt") or ""), client=client, room_id=room_id, channel_id=channel_id)

    lock = _get_lock(wid) if not parallel else None
    if lock:
        lock.acquire()
    try:
        last_exc: Exception | None = None
        response = ""
        backoff = [1, 2, 4]
        for attempt in range(max_retries + 1):
            try:
                response = _execute() or ""
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                logger.warning("webhook %s attempt %d failed: %s", name, attempt + 1, e)
                if attempt < max_retries and attempt < len(backoff):
                    time.sleep(backoff[attempt])
        fired_at = datetime.now().astimezone().isoformat()

        # Target-explicit nur wenn client/room_id/channel_id wirklich gesetzt sind.
        # HTTP-Caller ohne expliziten Target bekommt die Antwort im HTTP-Body —
        # KEIN Fanout an alle konfigurierten Chat-Clients (würde sonst leaken).
        has_target = bool(client or room_id or channel_id)

        if last_exc is not None:
            err_msg = f"{type(last_exc).__name__}: {last_exc}"
            err_trace = "".join(traceback.format_exception(type(last_exc), last_exc, last_exc.__traceback__))[-2000:]
            _set_last(wid, last_fired=fired_at, last_error={"timestamp": fired_at, "message": err_msg})
            if silent or not has_target:
                try:
                    _save_output(item, f"ERROR: {err_msg}\n\nTRACEBACK:\n{err_trace}", _safe_output_name(None).replace(".txt", "-error.txt"))
                except Exception:
                    pass
            else:
                try:
                    from miniassistant.notify import send_notification
                    send_notification(f"⚠️ Webhook '{name}' failed after {max_retries+1} attempts: {err_msg}", client=client, room_id=room_id, channel_id=channel_id)
                except Exception:
                    pass
            return {"ok": False, "error": err_msg, "fired_at": fired_at}

        # Success
        from miniassistant.scheduler import _SILENT_SENTINELS
        is_silent_token = response.strip() in _SILENT_SENTINELS
        out_path: str | None = None
        if save_output and not is_silent_token:
            try:
                p = _save_output(item, response, output_name)
                out_path = str(p)
            except Exception as e:
                logger.warning("save output failed: %s", e)

        if not silent and not is_silent_token and response.strip() and has_target:
            try:
                from miniassistant.notify import send_notification
                send_notification(response, client=client, room_id=room_id, channel_id=channel_id)
            except Exception as e:
                logger.warning("notify failed: %s", e)

        _set_last(wid, last_fired=fired_at, last_error=None)
        return {
            "ok": True,
            "id": wid,
            "fired_at": fired_at,
            "silent": silent,
            "response": "" if (silent and save_output) else response,
            "output_path": out_path,
        }
    finally:
        if lock:
            lock.release()


def rate_check(wid: str) -> bool:
    return _rate_check(wid)


def constant_time_token_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)
