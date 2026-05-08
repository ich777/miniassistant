# Plan: Slot Cache (KV-Cache Persistenz für llama.cpp Backend)

**Status**: PLAN — nicht implementiert.
**Scope**: MiniAssistant Feature für persistierten llama.cpp KV-Cache pro Conversation.
**Ziel**: Lange Conversations / MA-Restart sparen prompt-eval (5-30s pro Resume).

---

## 1. Architektur-Constraint (wichtig!)

LLM-Server (mit llama-swap + llama.cpp) und MA-Server sind **getrennte Maschinen**.
- Slot-Files liegen physisch auf LLM-Server unter `--slot-save-path` (z.B. `/slots`).
- MA-Server hat KEINEN direkten Filesystem-Zugriff.
- Alle Operationen via HTTP API: `POST /upstream/{model}/slots/{id}?action=save|restore&filename=X`.
- Konsequenz: MA kann Slot-Files nicht direkt löschen, nicht Größe prüfen, nicht discovern.

**Was MA macht:**
- Tracked Mapping `conv_id → filename` in eigener SQLite-DB.
- Sendet Save/Restore via API.
- Schätzt File-Size anhand `prompt_token_count` (für LRU-Eviction in DB).
- Wenn DB-Eviction passiert: API-File bleibt evtl. orphan auf LLM-Server. TTL-basierte Cleanup-Strategie auf LLM-Server-Seite empfohlen (optional cron).

**Slot-Save-Path Config-Eintrag**: nur informativ + für User-Doku. MA sendet nur `filename`, llama.cpp prefixed auf seinen Path.

---

## 2. Config-Struktur

### a) Globaler Slot-Cache-Block (`config.yaml`)

```yaml
slot_cache:
  enabled: false                    # Default OFF — opt-in
  ttl_days: 14                      # Auto-Invalidierung nach N Tagen Inaktivität
  max_files: 40                     # LRU-Cap
  min_tokens_to_cache: 10000        # Erst ab N Tokens lohnt's sich
  llama_swap_url: ""                # Optional, sonst aus providers[].base_url ableiten
  remote_slot_path: "/slots"        # NUR informativ (User-Hinweis wo's auf LLM-Server liegt)
```

**Nicht enthalten** (bewusst):
- `max_storage_gb` — File-Size auf LLM-Server nicht via API abfragbar. Cleanup auf LLM-Server-Seite via cron `find /slots -mtime +N -delete` regeln.
- `bytes_per_token` Schätzung — nutzt nichts ohne echte Messung.

### b) Per-Provider/Per-Modell Override (in providers-Block, wie `num_ctx`)

```yaml
providers:
  llama-swap:
    type: openai-compat
    base_url: http://10.0.0.2:8080
    num_ctx: 256000
    slot_cache: true                # Provider-default für alle Modelle
    model_options:
      qwen3.6-35b-a3b-uncensored:
        num_ctx: 128000
        slot_cache: true            # explizit aktiv
      qwen3.5-9b:
        num_ctx: 128000
        slot_cache: false           # zu klein, lohnt nicht
      deepseek-r1-70b:
        slot_cache: false           # zu groß, Storage zu teuer
```

**Resolution-Logik**:
1. `slot_cache.enabled=false` global → IMMER aus, alle Overrides ignoriert
2. `slot_cache.enabled=true` global → check `model_options.{model}.slot_cache` zuerst
3. Fallback: `providers.{provider}.slot_cache`
4. Default wenn nichts: `true` (alle Modelle, wenn global an)

### c) Per-Endpoint (vereinfacht — nur `/raw/v1`)

```yaml
raw_proxy:
  enabled: true
  slot_cache: false                 # Default OFF für raw (User-controlled prompts = wenig stabiler Prefix)
```

**Web-Chat & /v1**: kein Endpoint-Toggle nötig.
- `/api/chat/stream` ohne `track`: schon kein History-Persist → nicht cachen (ohnehin kurze Sessions).
- `/api/chat/stream` mit `track=true`: cachen wenn slot_cache.enabled.
- `/v1/chat/completions`: cachen wenn slot_cache.enabled.
- Matrix/Discord: cachen wenn slot_cache.enabled (immer mit conv_id).

→ Doku stellt das klar; kein zusätzlicher Config-Key nötig.

---

## 3. Instance-ID (Multi-MA-Support)

Damit zwei MA-Instances die selben llama-swap-Slots nicht überschreiben:

**Generation**:
- Beim ersten Start: UUID4 wird in `{config_dir}/instance_id` geschrieben (8 Zeichen Hex reichen, z.B. `a3f9c2b8`).
- Idempotent: existiert das File, wird's gelesen.
- Permission 0600.

**Verwendung**:
- Slot-File-Namensschema: `{instance_id}_{conv_hash}.bin`, z.B. `a3f9c2b8_7e2d4f6c.bin`
- DB-Spalte enthält vollen Filename
- Auf LLM-Server unterschiedliche Instances → unterschiedliche Files → keine Kollision

**Hinweis**: Conv-Cache ist NICHT shared zwischen Instances (ist für jede Instance eigene KV-State). Akzeptiert.

---

## 4. Modell-Alias-Auflösung

Cache-Key normalisieren BEVOR conv_id gehasht wird:

```python
def cache_key_for_model(config: dict, requested: str) -> str:
    real = resolve_model(config, requested) or requested
    # entferne provider-prefix
    return real.split("/", 1)[-1] if "/" in real else real
```

→ `"qwen"`, `"llama-swap/qwen"`, `"qwen3.6-35b-a3b-uncensored"` mappen alle auf `qwen3.6-35b-a3b-uncensored`.

---

## 5. Storage: JSON-File

Datei: `{config_dir}/slot_cache.json`

```json
{
  "instance_id": "a3f9c2b8",
  "entries": [
    {
      "conv_id": "web:abc123",
      "model": "qwen3.6-35b-a3b-uncensored",
      "filename": "a3f9c2b8_7e2d4f6c.bin",
      "prompt_token_count": 18000,
      "last_used_ts": 1715000000,
      "created_ts": 1714900000
    }
  ]
}
```

**Patterns** (aus MA bereits etabliert, vgl. `schedules.json`, `chats/*.json`):
- Atomic-Write: schreibe zu `slot_cache.json.tmp`, dann `os.replace()` → `slot_cache.json`
- Threading-Lock global: `_slot_cache_lock = threading.Lock()` für alle Reads+Writes
- Lookup: linear scan über `entries` (40 Einträge → <1ms)
- Mutation: lade ganzes JSON, mutiere Liste, schreibe atomic zurück
- Wenn File fehlt/korrupt: leere Liste, neue instance_id generieren

**Kein DB-Schema, keine Migrations, keine Indexes nötig** — File ist klein, alle Ops O(N) mit N≤40.

**Helper-Funktionen** in `slot_cache.py`:
```python
def _load() -> dict:
    with _lock:
        if not path.exists():
            return {"instance_id": _ensure_instance_id(), "entries": []}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"instance_id": _ensure_instance_id(), "entries": []}

def _save(data: dict) -> None:
    with _lock:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, path)

def upsert(conv_id, model, filename, prompt_tokens):
    data = _load()
    entries = [e for e in data["entries"] if not (e["conv_id"]==conv_id and e["model"]==model)]
    entries.append({
        "conv_id": conv_id, "model": model, "filename": filename,
        "prompt_token_count": prompt_tokens,
        "last_used_ts": int(time.time()),
        "created_ts": int(time.time()),
    })
    data["entries"] = entries
    _save(data)
```

---

## 6. conv_id-Generation pro Platform

```python
def derive_conv_id(session, platform, request_data) -> str:
    if platform == "web":
        return f"web:{session['session_id']}"
    if platform == "matrix":
        return f"matrix:{room_id}:{user_id}"
    if platform == "discord":
        return f"discord:{channel_id}:{user_id}"
    if platform == "api":
        # Hash aller bisherigen Messages (deterministisch)
        # → Wenn User Message editiert, ändert sich Hash → neue conv_id
        h = hashlib.sha256()
        for m in messages:
            h.update(f"{m['role']}:{m['content']}".encode())
        return f"api:{h.hexdigest()[:16]}"
    return None  # → kein Caching
```

---

## 7. Save-Flow

```python
def save_after_round(conv_id, model_normalized, prompt_token_count, llama_swap_url):
    if not slot_cache_enabled_for(model_normalized, endpoint):
        return
    if prompt_token_count < min_tokens_to_cache:
        return

    # Slot-ID finden: querye /slots, finde Slot mit längstem Match auf aktuellen Prompt
    slots = httpx.get(f"{llama_swap_url}/upstream/{model_normalized}/slots", timeout=5).json()
    slot_id = pick_slot_by_prompt_match(slots, current_prompt_first_2k_chars)
    if slot_id is None:
        log.debug("slot_cache: no matching slot found for save")
        return

    filename = f"{instance_id}_{conv_hash(conv_id)}.bin"
    try:
        httpx.post(f"{llama_swap_url}/upstream/{model_normalized}/slots/{slot_id}",
                   params={"action": "save", "filename": filename}, timeout=30)
    except Exception as e:
        log.warning("slot_cache save failed: %s", e)
        return

    db.upsert(conv_id, model_normalized, filename, prompt_token_count, now)
    cleanup_lru()
```

**Async**: `threading.Thread(target=save_after_round, daemon=True).start()` → User wartet nicht.

---

## 8. Restore-Flow

```python
def restore_before_round(conv_id, model_normalized, llama_swap_url) -> bool:
    if not slot_cache_enabled_for(model_normalized, endpoint):
        return False

    row = db.get(conv_id, model_normalized)
    if not row:
        return False

    # Find freien Slot
    slots = httpx.get(f"{llama_swap_url}/upstream/{model_normalized}/slots", timeout=5).json()
    free = next((s for s in slots if not s["is_processing"]), None)
    if not free:
        log.debug("slot_cache: no free slot for restore")
        return False

    try:
        r = httpx.post(f"{llama_swap_url}/upstream/{model_normalized}/slots/{free['id']}",
                       params={"action": "restore", "filename": row["filename"]},
                       timeout=30)
        if r.status_code != 200:
            # File evtl. weg auf LLM-Server (TTL/manueller Cleanup), Eintrag löschen
            db.delete(conv_id, model_normalized)
            return False
    except Exception as e:
        log.warning("slot_cache restore failed: %s", e)
        return False

    db.touch(conv_id, model_normalized, now)
    return True
```

---

## 9. Invalidierung

| Trigger | Aktion |
|---------|--------|
| `/new` Web-Chat | `db.delete(conv_id, *)` für aktuelle session |
| Matrix `!reset` | dito |
| Discord `/reset` | dito |
| User editiert History | conv_id ändert sich (api-platform via hash) → alter Eintrag wird LRU-evicted |
| Modell aus Config entfernt | Periodischer Sweep: alle DB-Rows mit `model NOT IN known_models` → delete |
| Restore returnt 404/500 | DB-Row löschen (File auf LLM-Server weg/korrupt) |
| TTL abgelaufen | Periodischer Sweep: `last_used_ts < now - ttl_days*86400` → delete |
| LRU-Cap überschritten | Älteste Entries löschen bis `count <= max_files` |

**Scheduler-Job**: `slot_cache_cleanup` läuft alle 1h, ruft `cleanup_lru()`, `cleanup_unknown_models()`, `cleanup_ttl()`.

⚠️ **Orphan-Files auf LLM-Server**: Wenn DB-Row gelöscht aber File noch da → MA referenziert es nicht mehr, Storage auf LLM-Server wird nicht freigegeben durch MA. Lösung: User-Doku empfiehlt cron auf LLM-Server:

```bash
# /etc/cron.daily/cleanup-llama-slots
find /slots -type f -name "*.bin" -mtime +30 -delete
```

→ Doku-Sektion "Slot-Cache Cleanup auf LLM-Server-Seite".

---

## 10. Stats-Page Integration

Nicht eigene Web-UI, sondern Erweiterung der **bestehenden Usage-Page** (`/usage` + `/api/usage`,
gerendert in `web/app.py` ab Zeile ~2820).

### a) Neue Sektion in der Usage-Page (nur wenn `slot_cache.enabled=true`)

Tabelle "Slot Cache" mit:
- **Header-Stats** (oben): Total Files, Hit-Rate (letzte 7 Tage), Top-Modelle
- **Cache-Liste** (Tabelle): `conv_id | model | created | last_used | prompt_tokens | actions`
- **Action-Buttons** pro Row: "Forget" → entfernt Entry aus JSON (orphan-File bleibt bis Server-Cron)
- **Bulk-Actions** oben: "Forget all > N days", "Forget all for model X"

### b) Neuer API-Endpoint

```python
@app.get("/api/slot_cache/list")
async def api_slot_cache_list(request: Request):
    """Liste aller MA-bekannten Cached Conversations."""
    _require_token(request)
    if not config.slot_cache_enabled:
        return JSONResponse({"enabled": False, "entries": []})
    return JSONResponse({"enabled": True, "entries": slot_cache.list_all(), "stats": slot_cache.stats()})

@app.delete("/api/slot_cache/{conv_id}")
async def api_slot_cache_delete(conv_id: str, request: Request):
    """Forget einen Cache-Entry (entfernt aus JSON, File bleibt bis Server-Cron)."""
    _require_token(request)
    slot_cache.invalidate_by_conv_id(conv_id)
    return JSONResponse({"ok": True})
```

### c) Hit-Rate Tracking

In der JSON-File ein extra Feld:
```json
{
  "instance_id": "...",
  "entries": [...],
  "stats": {
    "hits_7d": 42,
    "misses_7d": 8,
    "saves_7d": 50,
    "last_reset": 1715000000
  }
}
```

Increment bei jedem Save/Restore-Versuch. Reset alle 7 Tage automatisch.

### d) Settings-Toggle

Slot-Cache-Config wird via existing `/settings` Page bearbeitet (raw-yaml-Edit oder geführter
Form). Neue dedizierte Settings-Page nicht nötig.

→ Aufwand: ~1.5h statt vorher 2-3h. Konsistente UX mit existing Stats.

---

## 11. Integration-Punkte im Code

### a) `chat_loop.py` — `chat_round` und `chat_round_stream`
```python
# Anfang chat_round:
conv_id = derive_conv_id(session, platform, ...)
if conv_id and slot_cache.enabled:
    slot_cache.restore_before_round(conv_id, model_norm)

# nach erfolgreicher Antwort:
if conv_id and slot_cache.enabled and prompt_token_count >= min_tokens_to_cache:
    threading.Thread(target=slot_cache.save_after_round,
                     args=(conv_id, model_norm, prompt_token_count),
                     daemon=True).start()
```

### b) `chat_loop.py` — `/new` handling
```python
if user_input.strip().lower() == "/new":
    if conv_id and slot_cache.enabled:
        slot_cache.invalidate(conv_id)
    session["messages"] = []
    ...
```

### c) `web/app.py` — `/api/chat/stream` track-flag check
- Slot-Cache nur wenn `session.get("_track_chat")` = True. Sonst conv_id wird `None`.

### d) `web/raw_proxy.py` — config-Toggle
```python
if raw_cfg.get("slot_cache", False) and conv_id_can_be_derived(...):
    # cache, sonst skip
```

### e) `scheduler.py` — Cleanup-Job
- Add new periodic job `slot_cache_cleanup` (alle 1h).

---

## 12. Was NICHT cachen

| Pfad | Grund |
|------|-------|
| Subagent-Calls (`debate`, `invoke_model` text) | Kurzlebig, kein User-facing Resume |
| Image Generation (`invoke_model` Bild) | Andere Modell-Klasse, keine KV |
| STT (Whisper/Wyoming) | Architektur ohne KV-Cache-Konzept |
| TTS | dito |
| Compaction-Calls | Internal step, nicht User-facing |
| Scheduler one-shot Jobs | conv_id wechselt, keine Re-Use |
| Vision-Conversations (Phase 1) | mmproj-Slots eigene KV-Logik, später evtl. |

→ Filter: in chat_round prüfen `_chat_context.platform in ("web","api","matrix","discord")` UND nicht `_subagent_call` flag.

---

## 13. CONFIGURATION.md Update

Neue Sektion `## Slot Cache (Performance)`:

````markdown
## Slot Cache (Performance)

Persistiert llama.cpp KV-Cache pro Conversation auf disk (am LLM-Server). Spart bei Resume
einer alten Conversation 5-30s prompt-eval. Funktioniert nur mit llama.cpp / llama-swap Backend
(provider type `openai-compat`).

```yaml
slot_cache:
  enabled: false              # default OFF — opt-in
  ttl_days: 14
  max_files: 40
  min_tokens_to_cache: 10000
  llama_swap_url: ""          # leer = automatisch aus providers ableiten
  remote_slot_path: "/slots"  # informativ — wo's auf LLM-Server liegt
```

### Per-Modell aktivieren/deaktivieren

In `providers.{provider}.model_options`:

```yaml
providers:
  llama-swap:
    type: openai-compat
    base_url: http://10.0.0.2:8080
    slot_cache: true                  # default für alle Modelle hier
    model_options:
      qwen3.6-35b-a3b-uncensored:
        slot_cache: true
      qwen3.5-9b:
        slot_cache: false             # klein, lohnt nicht
```

### Per-Endpoint

`/api/chat/stream` (Web): aktiv wenn track=true.
`/v1/chat/completions`: immer aktiv wenn `slot_cache.enabled`.
`/raw/v1/chat/completions`: nur wenn `raw_proxy.slot_cache: true`.

### Voraussetzungen llama-server

```
--slot-save-path /slots
```

Cleanup-Cronjob auf LLM-Server (orphan-Files):
```
0 4 * * * find /slots -name '*.bin' -mtime +30 -delete
```

### Web-UI

Settings → Slot Cache. Live-View aller gecacheten Conversations + Delete-Buttons.
````

→ README.md kriegt nur 5-Zeiler-Pointer auf diese Sektion.

---

## 14. Tests

### Unit
- `derive_conv_id` deterministisch
- `cache_key_for_model` resolved Aliases korrekt
- LRU-Eviction-Logik
- TTL-Cleanup
- SQLite-Schema-Migration

### Integration (Mock-llama-swap)
- Save → Restore Round-Trip
- Restore-404 → DB-Row gelöscht
- Slot-busy → Restore skipped
- Multiple-Instance-IDs → no collision

### Real (manual)
1. `enabled: true` in config, restart MA
2. Conversation mit ~30k tokens History
3. Andere Conversation, dann zurück zur ersten
4. Erste Anfrage zur ersten conv: Latenz vor/nach Vergleich
5. MA Restart, dieselbe conv: Latenz prüfen
6. Web-UI: Files erscheinen mit Sizes

---

## 15. Implementierungs-Reihenfolge

| Phase | Inhalt | Aufwand |
|-------|--------|---------|
| **1 — Core MVP** | SQLite, instance-id, save/restore, config-resolution, integration in chat_round (web+api) | 4h |
| **2 — Production** | Per-Modell-Toggle, /raw-Toggle, Cleanup-Job (TTL+LRU), unknown-model-sweep | 2h |
| **3 — Multi-Platform** | Matrix conv_id, Discord conv_id, /new invalidation überall | 1h |
| **4 — Stats-Integration** | Erweiterung der existing Usage-Page um Slot-Cache-Sektion (Liste, Hit-Rate, Forget-Buttons) | 1.5h |
| **5 — Doku** | CONFIGURATION.md, README.md Pointer, miniassistant/docs/SLOT_CACHE.md | 1h |
| **Total** | | **~9-10h** |

---

## 16. Offene Fragen vor Start

Keine kritischen mehr. Klar:
- ✅ Modell-Resolve via `cache_key_for_model`
- ✅ Default OFF
- ✅ Per-Modell in `model_options`
- ✅ /raw eigener Toggle, Web/v1/Matrix/Discord automatisch
- ✅ Defaults: 10k min, 40 files (Storage-Cap entfällt — kein API-Zugriff auf File-Sizes)
- ✅ MA hat keinen FS-Zugriff auf Slot-Files → API-only
- ✅ Multi-Instance via instance_id-Prefix in Filename
- ✅ Web-UI statt CLI für Cache-Management
- ✅ Korruption / fehlende File → DB-Row löschen, fall-back auf normal eval
- ✅ Orphan-Files auf LLM-Server → User-Cron empfohlen, dokumentiert

**Einzig nice-to-have offen**:
- SSH-basiertes File-Cleanup von MA aus → Phase 99 / unwahrscheinlich, weil cron sauberer.

---

## 17. Risiken & Mitigations

| Risiko | Mitigation |
|--------|------------|
| Slot-File-Format-Wechsel zwischen llama.cpp Versionen | Restore catch → DB-Row löschen → fallback. Logging. |
| LLM-Server Storage voll | User-Cron + Doku. MA loggt Save-Failures. |
| MA-DB inkonsistent mit LLM-Server | Periodischer Reconcile-Sweep im Web-UI. |
| Falsches Restore (Slot-File für andere conv) | hash-Verifikation: nach Restore checke generation prefix-match (optional Phase 2). |
| Performance-Hit durch /slots-Polling | Cache `/slots`-Response 5s lang in MA. |
| Sensible Daten in Slot-Files (KV enthält System-Prompt + History) | File-Permissions 0600 (llama.cpp default), Verzeichnis 0700, Doku-Hinweis. |

---

## 18. Erfolgs-Kriterien

Nach 1 Woche Production mit `enabled: true`:
- Hit-Rate ≥ 30% bei aktiven Usern
- Average Save < 200ms (async, User merkt nichts)
- Average Restore + Latenz-Ersparnis > 5s bei großen Conversations
- Keine Crashes durch Slot-Cache-Errors (Catch-All-Pfad)
- File-Count bleibt unter `max_files` (LRU greift)
- Storage auf LLM-Server unter Kontrolle (User-Cron räumt Orphans + alte Files)

Wenn nicht erreicht: Feature-Disable und Re-Evaluation.
