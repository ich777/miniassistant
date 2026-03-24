"""Einfaches CSV-basiertes Usage-Tracking für LLM-Aufrufe."""

from __future__ import annotations

import csv
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

from .config import get_config_dir

_log = logging.getLogger(__name__)

_HEADER = ["ts", "model", "type", "seconds"]


def _usage_path() -> str:
    return os.path.join(get_config_dir(), "usage", "usage.csv")


def record(config: dict, model: str, call_type: str, duration_s: float) -> None:
    """Appended eine CSV-Zeile.  Noop wenn track_usage nicht aktiv."""
    if not config.get("server", {}).get("track_usage"):
        return
    path = _usage_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    try:
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(_HEADER)
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                model,
                call_type,
                f"{duration_s:.2f}",
            ])
    except OSError:
        _log.debug("usage record failed", exc_info=True)


def load(after: datetime | None = None, before: datetime | None = None) -> list[dict]:
    """Liest Usage-Einträge, optional gefiltert auf Zeitraum *after* .. *before*.

    Da die CSV chronologisch geschrieben wird, bricht die Funktion
    frühzeitig ab sobald Einträge *nach* ``before`` erscheinen.
    """
    path = _usage_path()
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    _past_end = 0
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M")
                except (KeyError, ValueError):
                    continue
                if after and ts < after:
                    continue
                if before and ts > before:
                    _past_end += 1
                    if _past_end >= 5:
                        break
                    continue
                _past_end = 0
                rows.append({
                    "ts": ts,
                    "model": row.get("model", ""),
                    "type": row.get("type", "chat"),
                    "seconds": float(row.get("seconds", 0)),
                })
    except OSError:
        _log.debug("usage load failed", exc_info=True)
    return rows


def _fmt_seconds(s: float) -> str:
    """Formatiert Sekunden als menschenlesbare Dauer."""
    if s < 60:
        return f"{s:.1f}s"
    m = int(s) // 60
    sec = s - m * 60
    if m < 60:
        return f"{m}m {sec:.0f}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m"


def aggregate(entries: list[dict], group_by: str = "day") -> dict:
    """Aggregiert Usage-Daten.

    group_by: "hour" | "day" | "week" | "month"
    """
    if not entries:
        return {
            "summary": {"total_seconds": 0, "total_requests": 0, "formatted": "0s"},
            "by_time": [],
            "by_model": [],
            "by_type": [],
        }

    total_s = sum(e["seconds"] for e in entries)
    total_n = len(entries)

    # --- by_time ---
    time_buckets: dict[str, dict] = defaultdict(lambda: {"seconds": 0.0, "requests": 0})
    for e in entries:
        ts: datetime = e["ts"]
        if group_by == "hour":
            key = ts.strftime("%Y-%m-%d %H:00")
        elif group_by == "month":
            key = ts.strftime("%Y-%m")
        else:
            key = ts.strftime("%Y-%m-%d")

        time_buckets[key]["seconds"] += e["seconds"]
        time_buckets[key]["requests"] += 1

    by_time = [{"label": k, **v} for k, v in sorted(time_buckets.items())]

    # --- by_model ---
    model_buckets: dict[str, dict] = defaultdict(lambda: {"seconds": 0.0, "requests": 0})
    for e in entries:
        model_buckets[e["model"]]["seconds"] += e["seconds"]
        model_buckets[e["model"]]["requests"] += 1
    by_model = sorted(
        [{"model": k, **v} for k, v in model_buckets.items()],
        key=lambda x: x["seconds"], reverse=True,
    )

    # --- by_type ---
    type_buckets: dict[str, dict] = defaultdict(lambda: {"seconds": 0.0, "requests": 0})
    for e in entries:
        type_buckets[e["type"]]["seconds"] += e["seconds"]
        type_buckets[e["type"]]["requests"] += 1
    by_type = sorted(
        [{"type": k, **v} for k, v in type_buckets.items()],
        key=lambda x: x["seconds"], reverse=True,
    )

    return {
        "summary": {
            "total_seconds": round(total_s, 2),
            "total_requests": total_n,
            "formatted": _fmt_seconds(total_s),
        },
        "by_time": by_time,
        "by_model": by_model,
        "by_type": by_type,
    }


def get_usage_for_period(period: str = "day") -> dict:
    """Lädt und aggregiert Usage für einen Zeitraum.

    period: "hour" | "day" | "3days" | "week" | "month" | "year" | "all"
    """
    now = datetime.now()
    if period == "hour":
        after = now - timedelta(hours=1)
        group = "hour"
    elif period == "day":
        after = now.replace(hour=0, minute=0, second=0, microsecond=0)
        group = "hour"
    elif period == "3days":
        after = now - timedelta(days=3)
        group = "hour"
    elif period == "week":
        after = now - timedelta(days=7)
        group = "day"
    elif period == "month":
        after = now - timedelta(days=30)
        group = "day"
    elif period == "year":
        after = now - timedelta(days=365)
        group = "month"
    else:  # all
        after = None
        group = "month"

    entries = load(after=after)
    return aggregate(entries, group_by=group)


def get_usage_for_range(from_dt: datetime, to_dt: datetime) -> dict:
    """Lädt und aggregiert Usage für einen benutzerdefinierten Zeitraum."""
    delta = to_dt - from_dt
    if delta.days <= 1:
        group = "hour"
    elif delta.days <= 14:
        group = "day"
    else:
        group = "month"
    entries = load(after=from_dt, before=to_dt)
    return aggregate(entries, group_by=group)
