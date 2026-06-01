# Plan: Group-Room-Mode (slim context + sandboxed exec)

**Status**: IMPLEMENTED (2026-05-16). Siehe `GROUP_ROOMS.md` für aktuelle Referenz.
**Scope**: Pro Raum/Channel umschaltbarer "Group-Context": slimer System-Prompt ohne persönliche Daten + Tool-Whitelist + sandboxed exec via bwrap. Konfigurierbar über WebUI (`/rooms`).
**Ziel**: Bot kann in Gruppenräumen sinnvoll mitreden ohne Owner-Identity/SOUL/Memory/Palace preiszugeben und ohne Zugriff auf lokales Filesystem außerhalb eines abgeschotteten Group-Workspaces.

---

## 1. Architektur-Constraint

- Der Bot-Prozess kann **NICHT komplett gesandboxed** werden — er bedient gleichzeitig Agent-Mode (Privatraum, voller Zugriff auf Config/Memory/Palace) und Group-Mode-Räume. Ein Prozess, viele Räume.
- Was sandboxed wird: **nur exec-Subprozesse die aus Group-Räumen ausgelöst werden**.
- Web-Tools (`web_search`, `read_url`, `check_url`, `send_image`) sind reines Python im Bot-Prozess, keine Shell-Injection. Brauchen keine OS-Sandbox — werden über Tool-Whitelist gefiltert.
- Pro-User-History bleibt wie heute (`room_id|user_id`). Group-Mode ändert nur System-Prompt + Tool-Set + Sandbox-Flag, nicht das Session-Routing.
- Default-Reply-Mode in Gruppenräumen ist bereits heute `mention` (matrix_bot.py:1143). Nicht ändern.

---

## 2. Config-Struktur

Neuer Block pro Plattform unter `chat_clients`:

```yaml
chat_clients:
  matrix:
    room_modes:                          # existiert bereits: always/mention/off
      "!abc:server.tld": mention
    room_settings:                       # NEU
      "!abc:server.tld":
        context: group                   # agent (default) | group
        language: auto                   # auto | de | en | fr | ...
        tools_allow:                     # Whitelist, nur wirksam wenn context=group
          - web_search
          - read_url
          - check_url
          - send_image
          - exec                         # exec wird automatisch gesandboxed
        workspace_subdir: "raumname"     # NEU, optional: Slug für <workspace>/groups/<subdir>. Default = sanitized room_id.
  discord:
    channel_modes: {...}
    channel_settings:                    # NEU, gleich strukturiert wie room_settings
      "12345": {...}
```

**Defaults bei neuem Bot-Invite in einen Raum >2 Member:**
- `context: group`
- `language: auto`
- `tools_allow: [web_search, read_url, check_url, send_image]`
- `exec` nicht im Default — Owner muss bewusst freischalten.

In DMs (=2 Member) wird `room_settings` ignoriert → bleibt Agent-Mode.

Persistenz: in normaler `config.yaml` (atomic via existierendem save_config). Kein separates File nötig.

---

## 3. WebUI-Erweiterung (`/rooms`)

`PATCH /api/rooms` (web/app.py:5281) erweitern oder zweiten Endpoint `/api/rooms/settings` einführen. Empfehlung: **zweiter Endpoint** — `room_modes` ist heute `dict[str,str]`, `room_settings` ist `dict[str,dict]`, getrennte Validierung sauberer.

Neue Endpoints:

- `PATCH /api/rooms/settings`
  - Body: `{"matrix": {"<room_id>": {context, language, tools_allow, workspace_subdir}}, "discord": {...}}`
  - Validierung: context ∈ {agent, group}, language Regex `^[a-z]{2}(-[A-Z]{2})?$|^auto$`, tools_allow ⊆ known tool names, workspace_subdir Regex `^[a-zA-Z0-9_-]{1,40}$`.
  - `null`/`{}` = Eintrag entfernen.

Auf `/rooms` Seite:
- Pro Raum-Card neuer ausklappbarer Bereich "Erweitert" (oder direkt unter mode-Dropdown):
  - Dropdown **Context**: agent / group
  - Wenn group:
    - Dropdown **Sprache**: auto / de / en / ...
    - Checkbox-Gruppe **Tools**: web_search, read_url, check_url, send_image, send_audio, exec (sandboxed)
    - Text-Input **Workspace-Name** (optional): leer = sanitized room_id
- Save-Button schickt PATCH `/api/rooms/settings`.

UI-Hinweis bei exec-Checkbox: "Läuft in bwrap-Sandbox, sieht nur den Group-Workspace, keine Config, kein Home."

---

## 4. System-Prompt im Group-Mode

In `agent_loader.build_system_prompt(config, project_dir, current_model=None)` neuen Pfad einbauen. Trigger: `config["_chat_context"].get("group_mode")` — Flag wird in Bot-Routing gesetzt (Section 6).

**Drin (group_mode):**
- `# Role and context` (kurz: "Du bist in einem Gruppenraum mit mehreren Teilnehmern.")
- `## AGENTS`
- `## IDENTITY` — Sprachzeile entfernt/überschrieben (siehe `_strip_language_from_identity`, agent_loader.py:1006)
- `## Environment` (Tools-Umgebung) — gefilterte Tools-Liste
- `_language_section` mit Override (siehe unten)
- `_units_section_from_prefs` — bleibt (allgemein, kein Personenbezug)
- `_system_and_runtime_section`
- `_safety_section`
- `_exec_behavior_section` — Spezial-Variante: Hinweis auf Sandbox + Workspace-Pfad
- `_persistence_section`
- `_planning_section`
- `_tools_section` — gefiltert
- `_docs_reference_section` — bleibt
- `_vision_section`, `_voice_section`

**Raus (group_mode):**
- `## SOUL` — komplett weg
- `## USER` (USER.md) — komplett weg
- `_user_session_section` — komplett weg (kein User-ID-Block über aktuellen Sprecher hinaus — Sprecher kommt als Message-Prefix, siehe 4b)
- `_room_last_fire_section` — weg
- `_memory_section` — weg (kein Palace-Inhalt, kein Raw-Memory)
- `_prefs_section` — weg
- `_knowledge_verification_section` — bleibt (Anti-Hallucinate gilt überall)

**Kein Owner-Text** — ack, nichts wie "der Eigentümer heißt X" oder "deine letzten Memories sagen". Group-Mode = unwissend über Owner.

### 4a. Language-Override

Heute: `respond_in_input_language` ist global; `_language_section` (agent_loader.py) liest Identity + Flag.

Neu:
- `language: auto` → wie `respond_in_input_language: true` für diese Session.
- `language: de` (o.ä.) → `_language_section` baut feste Sprachregel ("Antworte ausschließlich auf Deutsch …"), Identity-Sprachzeile via `_strip_language_from_identity` entfernt, danach harte Regel angehängt.
- Override wird über `chat_context["language_override"]` reingegeben → von `build_system_prompt` ausgewertet.

### 4b. Sprecher-Attribution

Da Session per `room_id|user_id` getrennt ist, sieht der Bot die History nur eines Users → keine Verwechslung. Aber im Group-Mode könnte die Antwort an mehrere gehen. Empfehlung: **keine Änderung am Message-Format** für jetzt. User sieht sowieso wer schreibt (Matrix-UI). Falls später Cross-User-Awareness gewünscht: per-room shared session — explizit nicht Scope dieses Plans.

---

## 5. Tools-Whitelist

`ollama_client.get_tools_schema(config)` (ollama_client.py:304) bekommt zweiten Parameter:

```python
def get_tools_schema(config, *, allow: set[str] | None = None) -> list[dict]:
    schema = _tools_schema(config, ...)
    if allow is not None:
        schema = [t for t in schema if t["function"]["name"] in allow]
    return schema
```

Aufrufer in `chat_loop.py:4653` + `:5083` ziehen `allow` aus `chat_context.tools_allow`.

**Tools die im Group-Mode standardmäßig blockiert sein sollten** (nicht in default tools_allow):
- `schedule` / `add_scheduled_job` — würde Owner-Scheduler verschmutzen
- `add_webhook` — gleich
- `invoke_model` (Subagents) — Tokenkosten ausufern
- `search_memory` / `save_memory` — Palace-Zugriff/Pollution
- `send_audio` — eher persönlich; Owner kann optional erlauben
- `get_room_last_fire` — Owner-Fires
- `wait` — generell raus (nur nach exec/watch sinnvoll, Group-exec eh sandboxed)

**Default-erlaubt:** `web_search`, `read_url`, `check_url`, `send_image`.

**Opt-in via UI:** `exec` (sandboxed), `send_audio`.

Hard-Reject Liste: selbst wenn jemand via Config `schedule` o.ä. einträgt → Group-Sessions führen diese Tools nicht aus. Doppelte Schutzschicht in `tools.py`-Handlern: Early-Return wenn `chat_ctx.group_mode=True` und Tool nicht in safe-Liste.

---

## 6. exec-Sandbox via bwrap

### 6a. Strategie

**bwrap (bubblewrap)** als primäre Lösung — User-Namespace-basierte Sandbox, kein setuid root nötig (sofern `kernel.unprivileged_userns_clone=1`, Standard auf Devuan/Debian).

**Fallback-Kette** (in dieser Reihenfolge prüfen, einmal beim Startup cachen):
1. `bwrap` verfügbar + userns funktioniert → bwrap.
2. `firejail` installiert → firejail mit eigenem Profil.
3. Keins von beiden → **exec im Group-Mode komplett deaktivieren** (Fehler an Modell zurück: "exec im Group-Mode nicht verfügbar — Sandbox fehlt"). Niemals ungesandboxed ausführen.

Sandbox-Verfügbarkeit + Modus loggen beim Bot-Start.

### 6b. bwrap-Wrapper

Neue Funktion in `tools.py` (oder neuer Modul `miniassistant/sandbox.py`):

```python
def build_bwrap_cmd(command: str, group_workspace: Path, allow_net: bool = True) -> list[str]:
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        # net optional — siehe 6d
        *(["--unshare-net"] if not allow_net else []),
        # Read-only Systembinaries
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        # Minimal /etc (nur was Tools brauchen)
        "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind", "/etc/ssl", "/etc/ssl",
        "--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates",
        "--ro-bind", "/etc/alternatives", "/etc/alternatives",
        # Virtuelle FS
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        # Group-Workspace RW gemounted als /workspace
        "--bind", str(group_workspace), "/workspace",
        "--chdir", "/workspace",
        # Env minimal
        "--clearenv",
        "--setenv", "HOME", "/workspace",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        # Optional: --cap-drop ALL (default in bwrap mit --unshare-user)
        "--",
        "/bin/bash", "-c", command,
    ]
```

**Wichtig:** kein `--ro-bind /home`, kein `--bind /root`, kein Config-Dir, kein agent_dir — alles unsichtbar.

### 6c. Group-Workspace-Auflösung

In `run_exec` (tools.py:349) am Anfang:

```python
chat_ctx = config.get("_chat_context") or {}
if chat_ctx.get("group_mode"):
    workspace_root = Path(config["workspace"]).resolve() / "groups"
    subdir = chat_ctx.get("workspace_subdir") or _sanitize_room_id(chat_ctx.get("room_id") or chat_ctx.get("channel_id") or "default")
    group_ws = (workspace_root / subdir).resolve()
    # Path-Traversal-Schutz
    if not str(group_ws).startswith(str(workspace_root) + "/"):
        return {"ok": False, "error": "invalid group workspace path"}
    group_ws.mkdir(parents=True, exist_ok=True)
    bwrap_cmd = build_bwrap_cmd(command, group_ws, allow_net=True)
    # subprocess.run(bwrap_cmd, ...) statt /bin/bash -c command
```

`_sanitize_room_id`: `re.sub(r"[^a-zA-Z0-9_-]", "_", room_id)[:40]`.

### 6d. Netzwerk

Default: **Netzwerk an** (`allow_net=True`). Sinnvoll für `curl`, `wget`, `apt list` o.ä. — Bot kann sonst nichts Nützliches.

Falls Owner Netz raushaben will: zusätzliche Config `room_settings.<id>.exec_no_net: true` → `--unshare-net`. Nicht im V1 nötig, kann später.

### 6e. Resource-Limits

Optional aber sinnvoll, billig zu addieren:
- `--rlimit-as` gibt's nicht in bwrap. Via `prlimit` davor oder `ulimit` im `bash -c`:
  - `ulimit -t 60 -v 1048576 -f 102400; <command>` (CPU 60s, RAM 1GB, max file size 100MB)
- Bestehender `timeout`-Parameter (tools.py:349) bleibt — `subprocess.run(..., timeout=N)` gilt für den ganzen bwrap-Aufruf.

### 6f. firejail-Fallback

Profile-Datei mitliefern oder inline `--private=<group_ws> --net=none/--noroot --whitelist=/usr --whitelist=/bin ...`. Profile in `miniassistant/sandbox_profiles/group_exec.profile`. Wenn firejail genutzt wird → Doc-Note dass es weniger streng ist als bwrap (firejail nutzt setuid).

### 6g. Doc-Note für User

In CONFIGURATION.md kurzer Abschnitt: "Group-Mode exec braucht `bwrap` oder `firejail`. Auf Devuan: `apt install bubblewrap`. Sonst ist exec im Group-Mode deaktiviert."

---

## 7. Write-Gating (Memory/Palace/Logs)

Wenn `chat_ctx.group_mode=True`:

| Schreibvorgang | Verhalten group_mode |
|---|---|
| `memory.append_exchange` | **skip** |
| `memory.save_summary` | **skip** |
| `mempalace.save_moment` | **skip** |
| `context_log` (Conv-Tracking) | **skip** ODER eigene Datei `<state_dir>/context_log_groups/<sanitized_room>.jsonl` (für Debug). Empfehlung: **skip** für V1, einfacher. |
| `agent_actions_log` (Tool-Calls + Outputs) | **schreiben**, aber in eigene Datei `agent_actions_groups/<sanitized_room>.jsonl`. Tool-Aufrufe sollen forensisch nachvollziehbar bleiben. |
| `debug_log` | wie heute (rein Server-Side) |
| `chat_history.json` (UI-Chat) | nicht betroffen — UI-Chat ist `web`-Platform, group_mode nur matrix/discord |

Implementierung: jede Schreib-Funktion bekommt frühen Check:
```python
def append_exchange(...):
    if (config.get("_chat_context") or {}).get("group_mode"):
        return  # group-mode: no personal history
    ...
```

agent_actions_log bekommt Branch auf Pfad:
```python
log_path = base if not group_mode else groups_dir / f"{sanitized}.jsonl"
```

---

## 8. Routing in Bot-Clients

### 8a. matrix_bot._get_chat_response (matrix_bot.py:508)

Vor `create_session`:

```python
room_settings = ((config.get("chat_clients") or {}).get("matrix") or {}).get("room_settings") or {}
rs = room_settings.get(room_id) or {}
ctx_mode = rs.get("context", "agent") if room_id else "agent"
group_mode = (ctx_mode == "group")

ctx = {"platform": "matrix", "room_id": room_id, "user_id": matrix_user_id}
if group_mode:
    ctx["group_mode"] = True
    ctx["tools_allow"] = set(rs.get("tools_allow") or [])
    ctx["language_override"] = rs.get("language") or "auto"
    ctx["workspace_subdir"] = rs.get("workspace_subdir") or None
if _sc_conv_id:
    ctx["conv_id"] = _sc_conv_id
    ctx["slot_cache_endpoint"] = "matrix"
config["_chat_context"] = ctx
```

Session-Key bleibt `f"{room_id or ''}|{matrix_user_id}"` — **wichtig**: wenn Owner zwischen agent/group umschaltet, **muss die Session invalidiert werden** (anderer System-Prompt!). Lösung: Session-Key um Mode erweitern: `f"{room_id}|{user_id}|{ctx_mode}"`. Alte Personal-Session bleibt im Speicher liegen falls Owner zurückschaltet, History kommt zurück.

### 8b. discord_bot._get_chat_response (discord_bot.py:163)

Spiegelbildlich mit `channel_settings`.

### 8c. create_session / handle_user_input

`chat_loop.create_session` liest bereits `config["_chat_context"]` → `build_system_prompt` greift auf `chat_ctx` zu. Nur sicherstellen:
- `build_system_prompt` checkt `chat_ctx.group_mode` und `chat_ctx.language_override`
- `get_tools_schema`-Calls in chat_loop bekommen `allow=chat_ctx.tools_allow if group_mode else None`

---

## 9. Default beim Bot-Invite

In matrix_bot beim ersten Sync nach Invite (oder Lazy beim ersten Message-Empfang in unbekanntem Raum):

```python
def _ensure_default_room_settings(config, room_id, member_count):
    if member_count <= 2:
        return  # DM — kein group-Default
    cc = config.get("chat_clients", {}).get("matrix", {})
    rs = cc.setdefault("room_settings", {})
    if room_id in rs:
        return
    rs[room_id] = {
        "context": "group",
        "language": "auto",
        "tools_allow": ["web_search", "read_url", "check_url", "send_image"],
    }
    save_config(config)
    logger.info("Group-Defaults für Raum %s gesetzt (group/auto/web-tools)", room_id)
```

Trigger: nach erfolgreichem `room_join` mit `member_count > 2` und keinem bestehenden Eintrag.

Discord: gleicher Mechanismus beim ersten Message-Empfang in einem Channel — Discord hat kein Invite-Event-Analog für DM-Erkennung; stattdessen `isinstance(message.channel, discord.DMChannel)` checken.

---

## 10. Edge Cases & Notes

- **Owner postet in Group-Raum**: gleich wie jeder andere User — sieht Group-Bot, kein Personal-Context. Wenn Owner Personal-Bot will → Mode auf `agent` im UI umstellen oder DM nutzen.
- **Voice in Group-Mode**: Transcript-Input geht durch gleiche Pipeline. Voice-Output (`send_audio`) per Tool-Whitelist erlaubt/nicht. Wenn deaktiviert: Bot antwortet nur Text.
- **send_image im Group-Mode**: braucht Pfad-Zugriff auf das Image-File. Wenn Image vom Bot generiert (DALL-E etc.) → liegt im Image-Cache des Bot-Prozesses, hat nichts mit Group-Workspace zu tun. OK.
- **Image-Upload in Group-Raum** (User schickt Bild): wird per `images=[...]` an `handle_user_input` weitergereicht, im Bot-Prozess geöffnet (nicht in exec-Sandbox). Funktioniert weiter.
- **Multi-Tenancy**: bei vielen Group-Räumen + viel exec → bwrap-Spawn-Overhead (~50–100ms). Akzeptabel.
- **Cleanup**: alte Group-Workspaces löschen wenn Bot aus Raum geleavt? Optional, nicht V1. Doc-Hinweis: "Räume bleibende Workspaces unter `<workspace>/groups/` manuell ggf. löschen."
- **Symlink-Attacks** im Group-Workspace: bwrap-Mount-Namespace verhindert Escape via Symlink — Bind-Mounts respektieren Mount-Boundaries.

---

## 11. Implementierung — Reihenfolge

1. **Plumbing**: `room_settings`-Lookup in matrix_bot + discord_bot; `chat_context.group_mode` setzen. Session-Key um Mode erweitern.
2. **System-Prompt-Branch**: `build_system_prompt` mit group_mode-Pfad; Sektionen skippen; Language-Override.
3. **Tools-Whitelist**: `get_tools_schema(..., allow=)` + Hard-Reject in Tool-Handlern.
4. **Write-Gating**: memory.py, mempalace, context_log, agent_actions_log.
5. **WebUI**: `PATCH /api/rooms/settings` + `/rooms`-Seite erweitern.
6. **bwrap-Sandbox**: neuer `sandbox.py`-Modul; Integration in `run_exec`; Verfügbarkeits-Check beim Startup.
7. **Default-Setter** beim Raum-Join.
8. **firejail-Fallback** (optional, falls bwrap nicht vorhanden).
9. **Doc** in `CONFIGURATION.md` + neue `miniassistant/docs/GROUP_ROOMS.md` (LLM-Referenz).

## 12. Aufwand-Schätzung

- 1–4: ~3h
- 5 (UI): ~1.5h
- 6 (Sandbox): ~2h (testen + Edge Cases)
- 7–9: ~1h

**Gesamt: ~7–8h.** Sandbox + UI sind die Hauptbrocken.

---

## 13. Offene Fragen für vor Implementierung

1. **firejail-Fallback bauen oder bwrap-only?** Wenn bwrap auf der Zielmaschine garantiert da ist → bwrap-only spart Code.
2. **Netz default im exec-Sandbox an oder aus?** Vorschlag: **an**, Owner kann pro Raum abdrehen (Phase 2).
3. **agent_actions_log: in eigene Datei pro Raum oder eine globale group-Datei?** Vorschlag: pro Raum (`agent_actions_groups/<sanitized>.jsonl`), forensik-freundlich.
4. **Group-Workspace-Cleanup**: V1 manuell oder gleich Cleanup-Befehl im UI?
5. **Session-Cache-Eviction bei Mode-Switch**: Session-Key um Mode erweitern (zwei parallele Sessions) oder bei Switch alte Session droppen? Vorschlag: erweitern — Wechsel zurück bringt History wieder.
