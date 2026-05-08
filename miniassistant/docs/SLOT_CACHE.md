# Slot Cache — Internals

Persistiert llama.cpp KV-Cache pro Conversation. Reduziert Re-Eval-Cost bei Resume.

## Code

- `miniassistant/slot_cache.py` — Core-Modul (~370 LOC)
- `miniassistant/web/app.py` — `/api/slot_cache/*` Endpoints + Stats-Section in `/nutzung`
- `miniassistant/chat_loop.py` — Restore am Start, Save async am Ende von `chat_round` und `chat_round_stream`
- `miniassistant/web/openai_compat.py`, `web/app.py`, `matrix_bot.py`, `discord_bot.py` — setzen `conv_id` in `_chat_context`

## Datenfluss

```
   chat_round(...)
   ├─ slot_cache.restore_before_round(conv_id, model)
   │     └─ POST /upstream/{model}/slots/{free_id}?action=restore filename={instance}_{hash}.bin
   │           → 200: KV-Cache geladen → llama.cpp prüft prefix-match beim Eval
   │           → 501: Modell unsupported (multimodal o.ä.) → mark, skip future tries
   │           → 404: File weg auf LLM-Server → Entry aus state löschen
   ├─ ... LLM call ...
   └─ Thread: slot_cache.save_after_round(conv_id, model, prompt_tokens, prompt_prefix)
         └─ pick_slot via id_task heuristic (highest idle id_task = recent)
         └─ POST /upstream/{model}/slots/{slot_id}?action=save filename=...
```

## Storage

`{config_dir}/slot_cache.json` (atomic write, threading.Lock):

```json
{
  "instance_id": "a3f9c2b8",
  "entries": [
    {"conv_id":"web:abc","model":"qwen3.6-35b-a3b-uncensored","filename":"a3f9c2b8_7e2d4f6c.bin","prompt_token_count":18000,"last_used_ts":..,"created_ts":..}
  ],
  "stats": {"hits_7d":..,"misses_7d":..,"saves_7d":..,"last_reset":..}
}
```

`{config_dir}/instance_id` — 8-hex UUID, ein File, Permission 0600.

## conv_id-Schema

| Platform | Format |
|----------|--------|
| `web` | `web:{session_id}` (nur bei `track=true`) |
| `api` (/v1) | `api:{sha256_first_16(messages_concat)}` — Hash über alle History-Messages |
| `matrix` | `matrix:{room_id}:{user_id}` |
| `discord` | `discord:{channel_id}:{user_id}` |

User editiert History bei `api` → Hash ändert sich → neue conv_id → alter Eintrag wird LRU-evicted.

## Slot-Picking

llama.cpp `/slots` returnt seit ca. b8500+ keinen `prompt`-string mehr (immer leer).
Fallback: höchstes `id_task` bei idle Slot = der zuletzt verwendete = mit hoher Wahrscheinlichkeit unserer.

Race-Edge: Multi-Instance kann den Slot zwischen unserer LLM-Call und Save überschreiben.
Konsequenz: wir saven evtl. fremden KV unter unserem filename → beim Restore lädt llama.cpp
fremden KV → prefix-match scheitert → re-eval. **Korrektheit OK**, nur Cache-Miss.

## 501 / Unsupported Models

Wenn llama.cpp 501 returnt — **kein Crash, kein User-facing Error**, Chat läuft normal weiter.

**Triggern 501:**
- `--mmproj` aktiv (Vision-Modelle) → KV-Layout inkompatibel
- `--slot-save-path` fehlt → save-Endpoint deaktiviert
- `--draft-model` (speculative decoding) → oft inkompatibel
- Vulkan/OpenCL Backends → teilweise

**MA-Verhalten:**
- `_unsupported_models` (module-level in-memory set) markiert das Modell
- Erste 501 → loggt Warning einmalig, set Eintrag
- Folgende save/restore-Calls für selbes Modell: sofort skip ohne HTTP-Roundtrip
- Reset bei MA-Restart (intentional — falls llama-swap-Config geändert wurde, neue Versuche möglich)
- User merkt nichts außer ggf. Hit-Rate bleibt 0% in den Stats

## Cleanup-Strategien

| Trigger | Mechanismus |
|---------|-------------|
| TTL (14d default) | Stündlicher async Job in `app.py` ruft `cleanup_lru_and_ttl` |
| LRU (40 files default) | Im selben Job + nach jedem Save automatisch |
| Modell aus Config entfernt | `cleanup_unknown_models` im selben Job |
| `/new` / `!reset` / `/reset` | `slot_cache.invalidate(conv_id)` in `handle_user_input` |
| Restore returnt 404 | DB-Entry löschen, fall-back auf normal eval |
| Orphan-Files auf LLM-Server | Außerhalb MA — User-Cron empfohlen (`find /slots -mtime +N -delete`) |

## Multi-Instance

Filename-Prefix `{instance_id}_` → kein Konflikt zwischen MA-Instanzen.
Slot-Konkurrenz: ja möglich (beide nutzen dieselben llama.cpp Slots), aber kein Korrektheits-Schaden.

## API-Endpoints

```
GET    /api/slot_cache/list                  → {enabled, entries, stats}
DELETE /api/slot_cache/{conv_id}             → {ok, removed}
POST   /api/slot_cache/cleanup               → {ok, removed_lru_ttl, removed_unknown_models}
```

## Bekannte Limits

- **Multimodal-Modelle (`--mmproj`)**: llama.cpp 501. Workaround: separater llama-swap-Eintrag ohne mmproj.
- **Speed-Boost variabel**: Hängt von Slot-Verfügbarkeit ab. Bei vielen parallelen Conversations evtl. Cache-Miss durch Slot-Kollision.
- **Storage-Cleanup auf LLM-Server**: muss user-side via cron passieren.
- **Slot-Picking-Heuristik**: `id_task`-basiert, nicht 100% deterministisch bei Multi-Instance.

## Test-Setup

1. `slot_cache.enabled: true` in `config.yaml`
2. llama-server mit `--slot-save-path /slots`
3. Test:
   ```bash
   # Conversation 1 (cold)
   curl -X POST /v1/chat/completions -d '{"model":"qwen","messages":[{"role":"user","content":"Hallo"}]}'
   # Same conv (warm — sollte Restore triggern und schneller sein)
   curl -X POST /v1/chat/completions -d '{"model":"qwen","messages":[{"role":"user","content":"Hallo"}]}'
   ```
4. Check `{config_dir}/slot_cache.json` für Entry und Stats.
5. `/nutzung` Page öffnen → Slot-Cache-Sektion erscheint mit Tabelle.
