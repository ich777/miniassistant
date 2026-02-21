"""
Scheduler: Jobs zu festen Zeiten (Cron) oder einmalig ("in N Minuten").
Nutzt APScheduler; Jobs werden in schedules.json persistiert und beim Start geladen.

Zwei Job-Typen:
  - command: Shell-Befehl ausfuehren
  - prompt: Bot mit Prompt aufwecken, Antwort an Chat-Client senden
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from miniassistant.config import get_config_dir, load_config

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     [scheduler] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_scheduler: Any = None


def _schedules_path() -> Path:
    return Path(get_config_dir()) / "schedules.json"


def _run_scheduled_job(job_id: str, job_data: str) -> None:
    """Wird vom Scheduler aufgerufen. job_data ist JSON mit command/prompt/client/once/model."""
    logger.info("Job %s gestartet", job_id[:8])
    try:
        data = json.loads(job_data)
    except Exception:
        logger.error("Job %s ungueltiges JSON", job_id[:8])
        return

    command = data.get("command", "").strip()
    prompt = data.get("prompt", "").strip()
    client = data.get("client")
    once = data.get("once", False)
    model = data.get("model")  # Optional: spezifisches Modell (Name oder Alias)
    room_id = data.get("room_id")  # Optional: direkt in diesen Matrix-Raum
    channel_id = data.get("channel_id")  # Optional: direkt in diesen Discord-Channel

    # Shell-Befehl ausfuehren (falls gesetzt)
    cmd_output = ""
    if command:
        try:
            from miniassistant.tools import run_exec
            result = run_exec(command)
            cmd_output = (result.get("stdout") or "").strip()
            if result.get("returncode") != 0:
                cmd_output += f"\n(exit {result['returncode']}): {(result.get('stderr') or '').strip()}"
        except Exception as e:
            cmd_output = f"Fehler: {e}"
            logger.exception("Job %s exec fehlgeschlagen", job_id[:8])

    # Autonomie-Präfix: Scheduled-Bot weiß dass er keine Rückfragen stellen kann
    _SCHEDULE_PREFIX = (
        "[SCHEDULED TASK — autonomous mode] "
        "You are executing a scheduled task. The user is NOT present and cannot respond. "
        "Complete the task fully on your own using your tools (exec, web_search, gh CLI, etc.). "
        "NEVER give instructions to the user, NEVER ask follow-up questions, NEVER say 'you can do X'. "
        "Just do it, deliver the result.\n\n"
    )

    # Prompt ausfuehren (Bot aufwecken)
    if prompt:
        try:
            full_prompt = _SCHEDULE_PREFIX + prompt
            if cmd_output:
                full_prompt = f"{full_prompt}\n\nAusgabe des Befehls:\n{cmd_output}"
            logger.info("Job %s prompt (model=%s): %s", job_id[:8], model or "default", prompt[:80])
            response = _run_prompt(full_prompt, model=model)
            logger.info("Job %s antwort: %d Zeichen", job_id[:8], len(response))
            _send_to_client(response, client, room_id=room_id, channel_id=channel_id)
            logger.info("Job %s -> %s gesendet", job_id[:8], client or "alle")
        except Exception as e:
            logger.exception("Job %s prompt fehlgeschlagen", job_id[:8])
            _send_to_client(f"Schedule-Fehler: {e}", client, room_id=room_id, channel_id=channel_id)
    elif command and client:
        _send_to_client(cmd_output or "(Keine Ausgabe)", client, room_id=room_id, channel_id=channel_id)

    # once=True oder date-Trigger: Job nach Ausfuehrung loeschen
    if once:
        _remove_job_by_id(job_id)


def _remove_job_by_id(job_id: str) -> None:
    """Entfernt einen Job aus schedules.json und dem Scheduler."""
    jobs = _load_jobs()
    remaining = [j for j in jobs if j.get("id") != job_id]
    if len(remaining) < len(jobs):
        _save_jobs(remaining)
        logger.info("Job %s entfernt", job_id[:8])
    sched = get_scheduler()
    if sched:
        try:
            sched.remove_job(job_id)
        except Exception:
            pass


def _run_prompt(prompt: str, model: str | None = None) -> str:
    """Fuehrt einen Prompt durch den Bot (eigene Session) und gibt die Antwort zurueck.
    model: optionaler Modellname/Alias. Wird aufgeloest; bei Fehler Fallback auf Default."""
    config = load_config()
    from miniassistant.chat_loop import create_session, handle_user_input
    from miniassistant.ollama_client import resolve_model
    session = create_session(config, None)
    if model:
        resolved = resolve_model(config, model)
        if resolved:
            session["model"] = resolved
            logger.info("Schedule: Modell '%s' -> '%s'", model, resolved)
        else:
            logger.warning("Schedule: Modell '%s' nicht auflösbar, nutze Default '%s'", model, session.get("model", ""))
    result = handle_user_input(session, prompt)
    # result[4] = content (ohne Thinking), result[0] = response_text
    content = (result[4] if len(result) > 4 else None) or result[0]
    return (content or "").strip()


def _send_to_client(message: str, client: str | None, room_id: str | None = None, channel_id: str | None = None) -> None:
    """Sendet eine Nachricht an Chat-Client(s) via notify. Optional direkt in Raum/Channel."""
    if not message:
        return
    try:
        from miniassistant.notify import send_notification
        results = send_notification(message, client=client, room_id=room_id, channel_id=channel_id)
        logger.info("Notify-Ergebnis: %s", results)
    except Exception as e:
        logger.exception("Job notify fehlgeschlagen")


def _parse_when(when: str) -> tuple[str, Any] | None:
    """
    when: Cron (5 Felder, z.B. "0 9 * * *") oder "in N minutes" / "in 1 hour".
    Returns (trigger_type, trigger_args) oder None bei Fehler.
    """
    when = (when or "").strip()
    m = re.match(r"in\s+(\d+)\s*(minute|hour)s?\s*$", when, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if "hour" in unit:
            run_date = datetime.now().astimezone() + timedelta(hours=n)
        else:
            run_date = datetime.now().astimezone() + timedelta(minutes=n)
        return ("date", {"run_date": run_date.isoformat()})
    parts = when.split()
    if len(parts) == 5:
        return ("cron", {"minute": parts[0], "hour": parts[1], "day": parts[2], "month": parts[3], "day_of_week": parts[4]})
    return None


def get_scheduler():
    """Lazy-Init des BackgroundSchedulers (UTC)."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("apscheduler nicht installiert. pip install apscheduler")
        return None
    _scheduler = BackgroundScheduler()
    return _scheduler


def _load_jobs() -> list[dict[str, Any]]:
    path = _schedules_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_jobs(jobs: list[dict[str, Any]]) -> None:
    path = _schedules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=0)


def add_scheduled_job(
    when: str,
    *,
    command: str = "",
    prompt: str = "",
    client: str | None = None,
    once: bool = False,
    model: str | None = None,
    room_id: str | None = None,
    channel_id: str | None = None,
) -> tuple[bool, str]:
    """
    Fuegt einen geplanten Job hinzu.
    - command: Shell-Befehl (optional)
    - prompt: Bot-Prompt – wird ausgefuehrt und Antwort an client gesendet (optional)
    - client: 'matrix', 'discord' oder None (alle)
    - when: Cron (5 Felder) oder 'in N minutes' / 'in 1 hour'
    - once: True = nach erster Ausfuehrung loeschen (auch bei Cron)
    - model: optionaler Modellname/Alias fuer den Prompt (Default: aktuelles Default-Modell)
    - room_id: Matrix-Raum-ID – Ergebnis direkt dorthin senden
    - channel_id: Discord-Channel-ID – Ergebnis direkt dorthin senden
    Date-Trigger ("in N minutes") werden immer als once behandelt.
    """
    if not command and not prompt:
        return False, "Mindestens 'command' oder 'prompt' noetig."
    parsed = _parse_when(when)
    if not parsed:
        return False, "Ungueltiges 'when': Cron (5 Felder) oder 'in 30 minutes' / 'in 1 hour'."
    trigger_type, trigger_args = parsed
    # Date-Trigger sind immer einmalig
    if trigger_type == "date":
        once = True
    sched = get_scheduler()
    if not sched:
        return False, "Scheduler nicht verfuegbar (pip install apscheduler)."

    job_id = str(uuid.uuid4())
    job_data_dict: dict[str, Any] = {}
    if command:
        job_data_dict["command"] = command
    if prompt:
        job_data_dict["prompt"] = prompt
    if client:
        job_data_dict["client"] = client
    if once:
        job_data_dict["once"] = True
    if model:
        job_data_dict["model"] = model
    if room_id:
        job_data_dict["room_id"] = room_id
    if channel_id:
        job_data_dict["channel_id"] = channel_id
    job_data_json = json.dumps(job_data_dict, ensure_ascii=False)

    jobs = _load_jobs()
    job_entry: dict[str, Any] = {
        "id": job_id,
        "trigger": trigger_type,
        "trigger_args": trigger_args,
        "command": command,
        "prompt": prompt,
        "client": client,
        "once": once,
        "model": model,
        "added_at": datetime.now().astimezone().isoformat(),
    }
    if room_id:
        job_entry["room_id"] = room_id
    if channel_id:
        job_entry["channel_id"] = channel_id
    jobs.append(job_entry)
    _save_jobs(jobs)

    try:
        _add_to_scheduler(sched, job_id, trigger_type, trigger_args, job_data_json)
    except Exception as e:
        _save_jobs([j for j in jobs if j["id"] != job_id])
        return False, str(e)

    desc = []
    if prompt:
        desc.append(f"Prompt: {prompt[:60]}")
    if command:
        desc.append(f"Command: {command[:60]}")
    if client:
        desc.append(f"-> {client}")
    if model:
        desc.append(f"model={model}")
    if once:
        desc.append("einmalig")
    return True, f"{job_id[:8]} ({', '.join(desc)})"


def _add_to_scheduler(sched: Any, job_id: str, trigger_type: str, trigger_args: dict, job_data_json: str) -> None:
    job_args = [job_id, job_data_json]
    if trigger_type == "date":
        from apscheduler.triggers.date import DateTrigger
        run_date = datetime.fromisoformat(trigger_args["run_date"].replace("Z", "+00:00"))
        now = datetime.now().astimezone()
        if run_date <= now:
            raise ValueError("Zeitpunkt liegt in der Vergangenheit.")
        sched.add_job(
            _run_scheduled_job, DateTrigger(run_date=run_date),
            id=job_id, args=job_args, replace_existing=True,
        )
    else:
        from apscheduler.triggers.cron import CronTrigger
        sched.add_job(
            _run_scheduled_job,
            CronTrigger(
                minute=trigger_args.get("minute", "*"),
                hour=trigger_args.get("hour", "*"),
                day=trigger_args.get("day", "*"),
                month=trigger_args.get("month", "*"),
                day_of_week=trigger_args.get("day_of_week", "*"),
            ),
            id=job_id, args=job_args, replace_existing=True,
        )


def start_scheduler_if_enabled() -> bool:
    """Laedt gespeicherte Jobs und startet den Scheduler. Returns True wenn Scheduler laeuft."""
    sched = get_scheduler()
    if not sched:
        return False
    now = datetime.now().astimezone()
    jobs = _load_jobs()
    expired_ids: list[str] = []
    for job in jobs:
        jid = job.get("id")
        if not jid:
            continue
        trigger = job.get("trigger")
        args = job.get("trigger_args") or {}
        # Abgelaufene date-Jobs aufraeumen
        if trigger == "date":
            try:
                run_date = datetime.fromisoformat(args.get("run_date", "").replace("Z", "+00:00"))
                if run_date <= now:
                    expired_ids.append(jid)
                    continue
            except Exception:
                expired_ids.append(jid)
                continue
        # Job-Daten rekonstruieren
        job_data_dict: dict[str, Any] = {}
        if job.get("command"):
            job_data_dict["command"] = job["command"]
        if job.get("prompt"):
            job_data_dict["prompt"] = job["prompt"]
        if job.get("client"):
            job_data_dict["client"] = job["client"]
        if job.get("once"):
            job_data_dict["once"] = True
        if job.get("model"):
            job_data_dict["model"] = job["model"]
        if job.get("room_id"):
            job_data_dict["room_id"] = job["room_id"]
        if job.get("channel_id"):
            job_data_dict["channel_id"] = job["channel_id"]
        if not job_data_dict:
            continue
        job_data_json = json.dumps(job_data_dict, ensure_ascii=False)
        try:
            _add_to_scheduler(sched, jid, trigger or "cron", args, job_data_json)
        except Exception:
            continue
    # Abgelaufene Jobs aus schedules.json entfernen
    if expired_ids:
        remaining = [j for j in jobs if j.get("id") not in expired_ids]
        _save_jobs(remaining)
        logger.info("%d abgelaufene Jobs entfernt", len(expired_ids))
    active_count = len(sched.get_jobs()) if sched.get_jobs else 0
    if not sched.running:
        sched.start()
    active_count = len(sched.get_jobs())
    logger.info("Gestartet mit %d aktiven Jobs", active_count)
    return True


def remove_scheduled_job(job_id_prefix: str) -> tuple[bool, str]:
    """Entfernt einen Job anhand der (Teil-)ID."""
    jobs = _load_jobs()
    matches = [j for j in jobs if j.get("id", "").startswith(job_id_prefix)]
    if not matches:
        return False, "Job nicht gefunden."
    if len(matches) > 1:
        return False, f"{len(matches)} Jobs gefunden – ID genauer angeben."
    job = matches[0]
    jid = job["id"]
    remaining = [j for j in jobs if j["id"] != jid]
    _save_jobs(remaining)
    sched = get_scheduler()
    if sched:
        try:
            sched.remove_job(jid)
        except Exception:
            pass
    return True, jid[:8]


def list_scheduled_jobs() -> list[dict[str, Any]]:
    return _load_jobs()
