# Konfiguration – MiniAssistant

Alle Einstellungen sind **optional**. Was nicht gesetzt ist, nutzt sinnvolle Defaults oder wird von Ollama/System übernommen. Eine leere oder fehlende Config-Datei ist gültig.

**Hinweis:** Diese Doku wird mit dem Code mitgepflegt – neue Optionen und Verhalten werden hier ergänzt.

---

## 1. Wo liegt die Config?

| Situation | Datei |
|-----------|--------|
| Projektordner | `./miniassistant.yaml` (im Projektverzeichnis) |
| Nutzer-Config | `~/.config/miniassistant/config.yaml` |

**Reihenfolge:** Wird MiniAssistant mit `-C /pfad/zum/projekt` (bzw. Projektverzeichnis) gestartet und existiert dort `miniassistant.yaml`, wird diese genutzt. Sonst die Datei unter `~/.config/miniassistant/`.

**Umgebungsvariable:** `MINIASSISTANT_CONFIG_DIR` überschreibt das Verzeichnis für die Nutzer-Config (nicht den Dateinamen).

---

## 2. Übersicht der Bereiche

- **providers** – Anbieter-Konfiguration. Typen: `ollama` (lokal/remote), `google` (Google Gemini API), `openai` (OpenAI / kompatible APIs), `deepseek` (DeepSeek API), `anthropic` (Anthropic Messages API), `claude-code` (Claude Code CLI). Jeder Provider hat einen Namen, ein `type`-Feld und eigene Modelle/Optionen.
- **fallbacks** – (optional) Globale Fallback-Modelle bei Fehler (Timeout, HTTP-Fehler).
- **subagents** – (optional) Liste von Worker-Modellen, die der Hauptagent delegieren kann.
- **server** – Web-UI/API (Host, **Port**, Token)
- **agent_dir** – Verzeichnis mit SOUL.md, IDENTITY.md, TOOLS.md, USER.md
- **workspace** – (optional) Arbeitsverzeichnis; dem Modell bekannt für Merkdateien/Notizen
- **search_engines** – (optional) Suchmaschinen (SearXNG); pro Eintrag eine URL. Z. B. `main` und `vpn` (VPN nur bei Aufforderung nutzen).
- **default_search_engine** – (optional) Welche Engine standardmäßig genutzt wird; fehlt sie, gilt die erste.
- **max_chars_per_file** – Zeichenlimit pro Agent-Datei im System-Prompt
- **scheduler** – (optional) Geplante Jobs (Cron/„in N Minuten“) via Tool `schedule`
- **chat_clients** – (optional) Chat-Clients: Matrix und Discord
- **onboarding_complete** – (intern) Wird **nur** auf `true` gesetzt, wenn in der Web-UI beim Onboarding/Setup auf **„Speichern“** geklickt wurde. Das CLI-`config` legt nur Config und Agent-Dateien an und setzt dieses Flag **nicht** – so bleibt der Onboarding-Button sichtbar, bis der Nutzer das Setup in der Web-UI abschließt und speichert. Default: `false`.
- **memory** – (optional) Einstellungen für den Memory-Auszug im System-Prompt (z. B. `max_chars_per_line`).
- **chat** – (optional) Smart Compacting: `context_quota` (Anteil von `num_ctx` der genutzt wird, Default 0.85). Bei Überschreitung wird der Chatverlauf automatisch komprimiert.
- **vision** – (optional) Vision-Modell für Bildanalyse. Kann Modellname (String) oder Objekt mit `model` und `num_ctx` sein.
- **image_generation** – (optional) Modell für Bildgenerierung. Gleiches Format wie `vision`.
- **avatar** – (optional) Bot-Profilbild. Dateipfad (PNG empfohlen) oder URL. Wird beim Onboarding abgefragt.

---

## 3. providers

Jeder Anbieter wird unter `providers.<name>` konfiguriert. Das Feld `type` bestimmt das Protokoll. Der **erste** Provider ist der Default.

| Schlüssel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `type` | string | nein | `ollama` | Protokoll/API-Typ: `ollama` (lokal/remote), `google` (Google Gemini API), `openai` (OpenAI / kompatible APIs), `deepseek` (DeepSeek API), `anthropic` (Anthropic Messages API), `claude-code` (Claude Code CLI). |
| `api_key` | string | nein | (keins) | API-Key für authentifizierte Endpoints (Ollama Online, Google Gemini, OpenAI, DeepSeek, Anthropic API). Wird in der WebUI maskiert. |
| `base_url` | string | nein | `http://127.0.0.1:11434` | API-Endpoint (Host + Port). Bei `google`: Default `https://generativelanguage.googleapis.com`. Bei `openai`: Default `https://api.openai.com`. Bei `deepseek`: Default `https://api.deepseek.com`. Bei `anthropic`: Default `https://api.anthropic.com`. Bei `claude-code`: nicht nötig. |
| `num_ctx` | integer | nein | (Ollama-Default) | Kontextlänge in Tokens (global). Pro Modell siehe `model_options`. |
| `think` | boolean/string | nein | (nicht gesetzt) | **Reasoning/Thinking:** `true` aktiviert, **`false` deaktiviert** (von Ollama unterstützt). Manche Modelle unterstützen `"low"`/`"medium"`/`"high"`. Ohne Angabe entscheidet das Modell. |
| `options` | Objekt | nein | `{}` | Globale [Ollama ModelOptions](https://docs.ollama.com/api/chat#modeloptions); werden mit `model_options[Modell]` überschrieben. |
| `model_options` | Objekt | nein | `{}` | **Optionen pro Modell** (Modellname → Optionen). Überschreibt die globalen `options` bzw. `num_ctx` für dieses Modell. Z. B. `qwen3:14b: { num_ctx: 32768 }`. |

### options (alle optional)

Alles unter `providers.<name>.options` wird 1:1 an die API übergeben. Fehlt etwas, nutzt der Provider seine Defaults – es muss nichts gesetzt werden.

| Option | Typ | Kurzbeschreibung |
|--------|-----|------------------|
| `temperature` | float | Zufälligkeit (niedriger = deterministischer, höher = kreativer). |
| `top_p` | float | Nucleus Sampling. |
| `top_k` | integer | Nur die K wahrscheinlichsten nächsten Tokens. |
| `num_ctx` | integer | Kontextlänge (alternativ als Top-Level-Key im Provider). |
| `num_predict` | integer | Maximale Anzahl zu generierender Tokens. |
| `seed` | integer | Fester Seed für reproduzierbare Ausgaben. |
| `min_p` | float | Mindest-Wahrscheinlichkeit für Token. |
| `repeat_penalty` | float | Wiederholungsstrafe (>1.0 = weniger Wiederholungen, z. B. 1.1). Alias: `repetition_penalty`. |
| `repeat_last_n` | integer | Anzahl Tokens die für repeat_penalty berücksichtigt werden. |
| `stop` | string/array | Stop-Sequenzen. |

**Beispiel (inkl. Optionen pro Modell):**

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://192.168.1.10:11434
    num_ctx: 8192
    think: true              # Global: Thinking/Reasoning an (für alle Modelle)
    options:
      temperature: 0.7       # Globale Optionen (gelten als Default)
      top_p: 0.9
      top_k: 40
    model_options:            # Pro Modell: überschreibt globale options + think
      "qwen3:14b":
        num_ctx: 32768
        temperature: 0.4      # Qwen3 soll deterministischer antworten
        think: true            # Thinking explizit an (überschreibt global)
      "llama3.2:1b":
        num_ctx: 4096
        temperature: 0.9      # Llama soll kreativer sein
        think: false           # Kein Thinking für kleines Modell
      "deepseek-r1:14b":
        num_ctx: 65536
        # think wird von global geerbt (true)
        # temperature wird von global geerbt (0.7)
```

> **Wichtig:** Einträge in `model_options` **überschreiben** die globalen `options` pro Modell – nicht andersherum. Nicht gesetzte Werte werden von `options` geerbt. Das gilt für **alle** Optionen: `temperature`, `top_p`, `top_k`, `num_ctx`, `think`, `num_predict`, `seed`, `min_p`, `stop` usw.

### Mehrere Provider (Multi-Ollama)

Du kannst **mehrere Ollama-Instanzen** auf verschiedenen Servern/Ports nutzen. Jeder zusätzliche Provider bekommt einen eigenen Namen unter `providers` und eine eigene `base_url`. Modelle werden mit **Provider-Präfix** angesprochen (z. B. `ollama2/llama3.3:70b`).

```yaml
providers:
  ollama:                              # Default-Provider (kein Präfix nötig)
    type: ollama
    base_url: http://127.0.0.1:11434
    think: true
    models:
      default: qwen3:14b
      aliases:
        quick: llama3.2:1b
  ollama2:                             # Zweiter Provider (z.B. GPU-Server)
    type: ollama
    base_url: http://192.168.1.20:11434
    think: false
    options:
      temperature: 0.5
    model_options:
      "llama3.3:70b":
        num_ctx: 65536
    models:
      aliases:
        big: llama3.3:70b
```

**Nutzung:**
- `qwen3:14b` → Default-Provider (`ollama`, `http://127.0.0.1:11434`)
- `ollama2/llama3.3:70b` → Zweiter Provider (`http://192.168.1.20:11434`)
- `ollama2/big` → Alias → `ollama2/llama3.3:70b`
- `/model ollama2/big` → Wechselt zum zweiten Provider
- `/models ollama2` → Listet alle Modelle auf dem zweiten Provider

Jeder Provider hat eigene `options`, `model_options`, `think`, `num_ctx` und `models.aliases`. Ohne Präfix wird immer der Default-Provider (`ollama`) verwendet.

---

## 4. server

Web-UI und API (Bind-Adresse, **Port**, Token). Port und Host sind einstellbar (Config oder `miniassistant serve --port 8080 --host 0.0.0.0`).

| Schlüssel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `host` | string | nein | `127.0.0.1` | Bind-Adresse (`0.0.0.0` = alle Interfaces). |
| `port` | integer | nein | `8765` | Port für HTTP. |
| `token` | string | nein | (wird bei erstem `serve` erzeugt) | API/Web-Zugriff nur mit diesem Token (Header `Authorization: Bearer …` oder `?token=…`). |
| `debug` | boolean | nein | `false` | Wenn `true`: API-Antworten enthalten `_debug` (Request/Response-JSON); zusätzlich wird ins Verzeichnis **debug/** geschrieben: **chat.log** (jeder Ollama-Request und -Response im Rohformat, inkl. erster Prompts) und **serve.log** (Serve-Start, jeder eingehende Request). Beide Dateien liegen unter dem Config-Verzeichnis (z. B. `~/.config/miniassistant/debug/`). |
| `show_estimated_tokens` | boolean | nein | `false` | Wenn `true`: Vor jedem Ollama-Call wird die geschätzte Token-Anzahl (System-Prompt, Messages, Tools, Gesamt) ins Server-Log geschrieben. Format: `INFO:     Estimated tokens – system: X, messages: Y, tools: Z, total: N`. Nützlich zur Kontrolle des Kontextverbrauchs bei lokalen LLMs. |
| `log_agent_actions` | boolean | nein | `false` | Wenn `true`: Jeder Prompt, Thinking, Antwort und Tool-Call wird in `$config_dir/logs/agent_actions.log` protokolliert. Einträge werden durch `---` getrennt. Kann in der Web-UI unter **Logs** live eingesehen werden. |
| `show_context` | boolean | nein | `false` | Wenn `true`: Vor jedem Ollama-Call wird der **vollständige Kontext** (System-Prompt, Messages, Token-Schätzung, verbleibende Tokens für Response/Thinking) in `$config_dir/logs/context.log` geschrieben. Format analog zu `agent_actions.log` mit Zeitstempel. Nützlich zum Debugging des Kontexts und Token-Budgets. |

Wenn `token` nicht gesetzt ist, wird beim ersten Start von `miniassistant serve` eines generiert und in der Config gespeichert.

---

## 5. models (pro Provider: providers.\<name\>.models)

Standard-Modell und Aliase gelten **pro Provider**. Sie stehen unter **`providers.<name>.models`**. Aliase werden immer aufgelöst (Chat, `/model`, invoke_model).

Für **Cross-Provider-Zugriff** (z.B. Subagents) wird das Modell mit **Provider-Präfix** angegeben: `ollama2/llama3.3:70b`.

| Schlüssel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `default` | string | nein | (keins) | Modellname für die Standard-Session (z. B. `llama3.2`). |
| `aliases` | Objekt | nein | `{}` | Kurznamen → Modellname, z. B. `compiler: qwen2.5-coder:14b`. |
| `list` | array | nein | (nicht gesetzt) | Optional: Liste erlaubter Modelle (Einschränkung). |
| `fallbacks` | array | nein | `[]` | **Optional.** Bei Fehler (z. B. HTTP 400, Timeout) nacheinander versuchen; der User sieht „Wechsel zu Modell X (Grund: …)“. |
| `subagents` | boolean | nein | `false` | **Optional.** Wenn `true`: Tool **invoke_model** (Subagenten) aktivieren – Haupt-KI kann ein anderes Modell (Name/Alias) für eine Aufgabe aufrufen. |

**Beispiel (unter Anbieter):**

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: llama3.2
      aliases:
        small: llama3.2
        compiler: qwen2.5-coder:14b
      fallbacks: [llama3.2]   # optional
      subagents: true         # optional, dann invoke_model verfügbar
```

**Subagenten (optional):** Wenn mindestens ein Provider `subagents: true` hat, kann die Haupt-KI mit **invoke_model** ein Modell aufrufen. Für Cross-Provider: `invoke_model(model="ollama2/big", message="...")`. Die Antwort des Subagenten wird der Haupt-KI zurückgegeben.

---

## 6. agent_dir

| Schlüssel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `agent_dir` | string | nein | `~/.config/miniassistant/agent` (bzw. `get_config_dir()/agent`) | Verzeichnis mit AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, USER.md. |

Absoluter oder relativer Pfad; Tilde wird auf das Home-Verzeichnis aufgelöst.

**Rollen der Dateien (OpenClaw-inspiriert, schlank):** AGENTS.md = optionaler Top-Level-Vertrag (Prioritäten, Grenzen, Qualitätsbarriere; wenige Zeilen). SOUL = Persönlichkeit, Ton, Werte. IDENTITY = Name, Rolle, Ziele, **Antwortsprache** (z. B. „Response language: Deutsch“ – die Sprache kommt nur aus IDENTITY.md, nicht aus der Config). TOOLS = Umgebung, Pfade, Hinweise. USER = Nutzer-Präferenzen, Format. AGENTS.md wird zuerst ins Prompt geladen; fehlt sie, erscheint ein Hinweis. Merken über workspace/exec bzw. Memory bei Modellwechsel.

---

## 6b. memory

Steuert den **Memory-Auszug** (tägliche Logs unter `agent_dir/memory/YYYY-MM-DD.md`), der in den System-Prompt übernommen wird.

| Schlüssel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `max_chars_per_line` | integer | nein | `300` | Maximale Zeichen pro Zeile beim Lesen des Memory. Längere Zeilen werden mit „…“ gekürzt. `0` = keine Kürzung. |
| `days` | integer | nein | `2` | Anzahl Tage Memory für den Auszug (heute + vergangene Tage). Z. B. `2` = heute und gestern. |

**Beispiel:**

```yaml
memory:
  max_chars_per_line: 400
  days: 2
```

---

## 6b2. chat (Smart Compacting)

Steuert das **automatische Komprimieren** des Chatverlaufs, wenn der Kontext voll wird.

| Schlüssel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `context_quota` | float | nein | `0.85` | Anteil von `num_ctx`, der für System-Prompt + Tools + Messages genutzt wird (Bereich: 0.5–0.95). Bei Überschreitung wird Smart Compacting ausgelöst. |

**Funktionsweise:** Wenn `system_prompt + tools + messages` den Wert `num_ctx × context_quota` überschreitet:
1. Ältere Messages werden vom Modell zu einer kurzen Zusammenfassung komprimiert
2. Neueste Messages (≈15% von `num_ctx`) bleiben unverändert
3. Ergebnis im Kontext: `[Zusammenfassung] + [neueste Messages]`

**Skaliert automatisch:** Bei einem 32K-Modell wird ab ~27K komprimiert, bei 128K ab ~111K. Keine festen Token-Limits nötig.

Jeder Exchange wird nach jeder Runde in `memory/YYYY-MM-DD.md` gespeichert — nichts geht verloren wenn Messages aus dem aktiven Kontext entfernt werden.

**Beispiel:**

```yaml
chat:
  context_quota: 0.85
```

---

## 6c. search_engines

Mehrere Suchmaschinen (SearXNG) mit fester ID. **default_search_engine** legt fest, welche standardmäßig genutzt wird; fehlt der Eintrag oder ist nur eine Engine konfiguriert, wird diese verwendet.

| Schlüssel | Typ | Beschreibung |
|-----------|-----|--------------|
| `search_engines` | Objekt | `id → { url: "https://..." }`. Z. B. `main`, `vpn`. |
| `default_search_engine` | string | ID der Standard-Engine (optional). |

**VPN-Suche:** Eine Engine mit ID, die „vpn“ enthält (z. B. `vpn`, `searxng_vpn`), wird vom Assistenten **nur** genutzt, wenn der Nutzer ausdrücklich VPN/gesicherte Suche wünscht oder es in Prefs (z. B. Suchgewohnheiten) steht. Die Default-Engine ist für die normale Suche.

**Beispiel:**

```yaml
search_engines:
  main:
    url: https://search.example.org
  vpn:
    url: https://search-vpn.example.org
default_search_engine: main
```

---

## 7. Weitere Optionen

| Schluessel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `workspace` | string | nein | `~/.config/miniassistant/workspace` | Arbeitsverzeichnis fuer Kompilate, Notizen, Merkdateien. |
| `search_engines` | Objekt | nein | `{}` | Suchmaschinen: `id → { url: "https://..." }`. Z. B. `main`, `vpn`. Wenn nicht leer: Tool `web_search` mit optionalem Parameter `engine`. |
| `default_search_engine` | string | nein | (erste Engine) | ID der Standard-Suchmaschine. Fehlt sie oder ist nur eine Engine konfiguriert, wird diese genutzt. |
| `max_chars_per_file` | integer | nein | `500` | Maximale Zeichen pro Agent-Datei (SOUL.md, IDENTITY.md, TOOLS.md, USER.md, AGENTS.md) die in den System-Prompt geladen werden. Längere Dateien werden gekürzt. Kleiner = weniger Kontextverbrauch. |
| `prefs_max_chars` | integer | nein | `2500` | Maximale Zeichen gesamt fuer Stored preferences (prefs/*, *.md im agent_dir). |
| `prefs_max_chars_per_file` | integer | nein | `1000` | Maximale Zeichen pro Pref-Datei; jede Datei wird vor dem Einbau auf dieses Limit gekuerzt. |
| `scheduler` | Objekt | nein | (nicht gesetzt) | Wenn `enabled: true`: Tool **schedule** verfuegbar. |
| `chat_clients` | Objekt | nein | (nicht gesetzt) | Chat-Client-Anbindungen (Matrix, Discord). Siehe unten. |

**Abwaertskompatibilitaet:** Top-level `matrix:` wird automatisch nach `chat_clients.matrix` migriert. Alte Configs funktionieren weiterhin.

---

## 7a. chat_clients

Chat-Clients werden unter `chat_clients:` konfiguriert. Momentan unterstuetzt: **Matrix** und **Discord**.

### Matrix

**Abhaengigkeit:** `pip install miniassistant[matrix]` (matrix-nio + mistune fuer HTML-Formatierung). Fuer E2EE: `pip install miniassistant[matrix-e2e]` + `apt install libolm-dev`.

**Config:**

| Schluessel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `enabled` | boolean | nein | `true` | `false` = Matrix-Bot nicht starten. |
| `homeserver` | string | ja | - | Serveradresse (z. B. `https://matrix.example.org`). |
| `bot_name` | string | nein | `MiniAssistant` | Anzeigename des Bots. |
| `user_id` | string | ja | - | Matrix-Benutzer-ID (z. B. `"@bot:example.org"` - wegen @ in Anfuehrungszeichen). |
| `token` | string | ja | - | Access Token des Bot-Accounts. |
| `device_id` | string | nein | `miniassistant` | Device-ID aus der Login-Antwort (wichtig fuer E2EE). |
| `encrypted_rooms` | boolean | nein | `true` | E2EE aktivieren/deaktivieren. |

**Nachrichten-Formatierung:** Der Bot sendet Nachrichten mit HTML-Formatierung (`org.matrix.custom.html`). Markdown wird automatisch via `mistune` zu HTML konvertiert, sodass **fett**, *kursiv*, `code` und Listen in Matrix-Clients korrekt dargestellt werden.

### Discord

**Abhaengigkeit:** `pip install miniassistant[discord]` (discord.py).

**Config:**

| Schluessel | Typ | Pflicht? | Default | Beschreibung |
|-----------|-----|----------|---------|--------------|
| `enabled` | boolean | nein | `true` | `false` = Discord-Bot nicht starten. |
| `bot_token` | string | ja | - | Bot-Token aus dem Discord Developer Portal. |
| `command_prefix` | string | nein | `!` | Befehlspraefix (momentan nicht genutzt, fuer spaeter). |

**Discord-Bot einrichten:**

1. Im [Discord Developer Portal](https://discord.com/developers/applications) eine neue Application erstellen.
2. Unter **Bot**: Token generieren und in die Config eintragen.
3. Unter **Bot > Privileged Gateway Intents**: **Message Content Intent** aktivieren.
4. Bot einladen: OAuth2 > URL Generator > Scopes: `bot`, Permissions: `Send Messages`, `Read Message History`.
5. Der Bot reagiert auf **DMs** und **@-Mentions** in Channels.

**Nachrichten-Formatierung:** Discord unterstuetzt nativ Markdown - keine Konvertierung noetig.

**Befehle:** `/model MODELLNAME` und `/models` funktionieren in Matrix und Discord. **`/new`** (neue Session, Verlauf leeren, Memory bleibt im Prompt) wirkt nur in **Web-UI und CLI** – in Matrix und Discord wird `/new` ignoriert (keine Antwort), da dort pro Nutzer ohnehin eine Session läuft.

### Auth-Flow (Matrix + Discord)

1. **Nutzer schreibt dem Bot** (Matrix-DM, Discord-DM oder @-Mention).
2. **Der Bot** erzeugt einen Code und sendet ihn direkt im Chat: "Dein Auth-Code: **ABC123**. Antworte mit: `/auth ABC123`"
3. **Nutzer antwortet direkt im Chat** mit `/auth ABC123` (oder nur dem Code).
4. **Die App** prueft den Code (gueltig 30 Minuten), speichert den Nutzer als autorisiert.
5. Alternativ in der Web-UI: `/auth matrix ABC123` bzw. `/auth discord ABC123`.

**Speicherort:** Auth-Daten liegen unter `config/auth/` (`pending_codes.json`, `authorized.json`). Alte Daten aus `config/matrix/` werden automatisch migriert.

### Token und device_id fuer Matrix

`token` und `device_id` kommen aus der Login-Antwort des Matrix-Servers:

```bash
curl --request POST \
  --url "https://DEIN_HOMESERVER/_matrix/client/v3/login" \
  --header "Content-Type: application/json" \
  --data '{
  "type": "m.login.password",
  "identifier": { "type": "m.id.user", "user": "BOT_BENUTZERNAME" },
  "password": "BOT_PASSWORT"
}'
```

`token` = `access_token`, `user_id` in Anfuehrungszeichen, `device_id` exakt aus der Antwort.

---

## 8. Vollstaendiges Beispiel (minimal)

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: llama3.2

server:
  host: 127.0.0.1
  port: 8765

agent_dir: ~/.config/miniassistant/agent
```

---

## 9. Vollstaendiges Beispiel (mit Chat-Clients)

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    num_ctx: 8192
    think: true
    options:
      temperature: 0.7
    model_options:
      "qwen3:14b":
        num_ctx: 32768
      "llama3.1:8b":
        num_ctx: 4096
    models:
      default: deepseek-r1
      aliases:
        fast: llama3.2
        reasoning: deepseek-r1
      fallbacks: [llama3.2]
      subagents: false

server:
  host: 0.0.0.0
  port: 8765
  token: "dein-geheimes-token"
  show_estimated_tokens: false

agent_dir: /opt/miniassistant/agent
workspace: /home/user/projekte

search_engines:
  main:
    url: https://search.example.org
  vpn:
    url: https://search-vpn.example.org
default_search_engine: main

chat_clients:
  matrix:
    enabled: true
    homeserver: https://matrix.example.org
    bot_name: MiniAssistant
    user_id: "@miniassistant:example.org"
    token: "syt_..."
    device_id: miniassistant
    encrypted_rooms: true
  discord:
    enabled: true
    bot_token: "dein-discord-bot-token"

memory:
  max_chars_per_line: 400
  days: 2

chat:
  context_quota: 0.85
```

---

## 10. Kurz: Was passiert, wenn etwas fehlt?

- **providers** fehlt komplett: Default-Provider `ollama` mit `http://127.0.0.1:11434`.
- **server.token** fehlt: wird beim ersten `serve` erzeugt und gespeichert.
- **providers.\<name\>.models.default** fehlt: Nutzer muss mit `/model MODELLNAME` waehlen.
- **agent_dir** fehlt: Default-Pfad unter `~/.config/miniassistant/agent`.
- **chat_clients** fehlt: Kein Matrix/Discord, nur CLI und Web-UI.

Alles ist optional; eine leere oder minimale Config ist ausreichend.

---

## 11. Web-UI und Zugriff

- **Token:** Chat, API und Konfiguration erfordern ein Token.
- **Favicon/Logo:** Die Web-UI zeigt das MiniAssistant-Logo (`miniassistant.png`) als Favicon und im Header.
- **Chat:** Nachrichten sind durch Trennlinien getrennt. Thinking wird als aufklappbarer Spoiler angezeigt.
- **Auth:** Freischaltung fuer Matrix/Discord erfolgt direkt im jeweiligen Chat-Client oder per `/auth <platform> <CODE>` in der Web-UI. Es gibt keine separate Auth-Seite mehr.

---

## 12. Schlankheit

| Konfiguration | Wirkung | Default |
|---------------|---------|---------|
| `max_chars_per_file` | Max. Zeichen pro Agent-Datei im System-Prompt | 500 |
| `memory.days` | Tage Memory im Prompt | 2 |
| `memory.max_chars_per_line` | Max. Zeichen pro Memory-Zeile | 300 |
| `prefs_max_chars` (Merkdateien) | Max. Zeichen gesamt im Prompt | 2500 |
| `prefs_max_chars_per_file` | Max. Zeichen pro Pref-Datei | 1000 |

---

## 13. Zweite Instanz (gleiche Maschine, anderes Config-Verzeichnis)

Du kannst mehrere MiniAssistant-Instanzen auf derselben Maschine betreiben, jede mit eigenem Config-Verzeichnis und eigenem Port. **Dasselbe venv** reicht.

### Schritte

1. **Config-Verzeichnis anlegen** (z. B. für Instanz 2):
   ```bash
   sudo mkdir -p /etc/miniassistant-instance2
   sudo chown DEIN_USER:DEIN_GROUP /etc/miniassistant-instance2
   ```
   Oder im Home: `mkdir -p ~/.config/miniassistant2`

2. **Config-Datei** in diesem Verzeichnis: `config.yaml` (nicht `miniassistant.yaml`). Port **muss** sich von Instanz 1 unterscheiden, z. B.:
   ```yaml
   server:
     port: 8766
   # Rest wie gewohnt (providers, agent_dir, …)
   ```
   Erste Instanz nutzt z. B. Port 8765, zweite 8766.

3. **Starten mit Umgebungsvariable:**
   ```bash
   export MINIASSISTANT_CONFIG_DIR=/etc/miniassistant-instance2
   miniassistant serve
   ```
   Oder in einer Zeile: `MINIASSISTANT_CONFIG_DIR=/etc/miniassistant-instance2 miniassistant serve`

4. **Gleiches venv:** Ein Aufruf aus demselben venv mit gesetztem `MINIASSISTANT_CONFIG_DIR` lädt Config, Agent, Auth und Debug-Logs aus dem angegebenen Verzeichnis. Es gibt keine Konflikte mit der anderen Instanz (anderer Port, andere Dateien).

### systemd (Beispiel zweite Instanz)

Unit-Datei z. B. `/etc/systemd/system/miniassistant-instance2.service`:

```ini
[Unit]
Description=MiniAssistant instance 2
After=network.target

[Service]
Type=simple
User=DEIN_USER
Group=DEIN_GROUP
Environment="MINIASSISTANT_CONFIG_DIR=/etc/miniassistant-instance2"
WorkingDirectory=/pfad/zu/miniassistant
ExecStart=/pfad/zu/miniassistant/venv/bin/miniassistant serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- `WorkingDirectory`: Projekt-Root (dort, wo das venv liegt).
- `ExecStart`: absoluter Pfad zur `miniassistant`-Executable im **gleichen** venv wie Instanz 1 (z. B. `/opt/miniassistant/venv/bin/miniassistant serve`).
- Port steht in der Config unter `MINIASSISTANT_CONFIG_DIR` (`server.port`, z. B. 8766).

Aktivieren und starten:
```bash
sudo systemctl daemon-reload
sudo systemctl enable miniassistant-instance2
sudo systemctl start miniassistant-instance2
```

### Klassisches init.d (SysV)

Script z. B. `/etc/init.d/miniassistant-instance2` (ausführbar, `chmod +x`): Umgebung setzen, dann dasselbe venv aufrufen, z. B.:

```bash
#!/bin/bash
export MINIASSISTANT_CONFIG_DIR=/etc/miniassistant-instance2
/pfad/zu/venv/bin/miniassistant serve
```

Für Dienste mit Start/Stop nutzt du üblicherweise einen Wrapper (z. B. `start-stop-daemon` oder ein kleines Script, das die PID speichert und beim Stop killt). Oder du betreibst beide Instanzen per systemd (empfohlen).

---

## 14. Ollama Online (Cloud-Provider mit API-Key)

Neben lokalen Ollama-Instanzen kannst du auch **Cloud-basierte Ollama-Endpunkte** nutzen (z. B. Ollama Online, oder eigene GPU-Server mit Auth). Dafür gibt es das Feld `api_key` im Provider.

**Wichtig:** `api_key` ist **nur für Remote-/Online-Provider** gedacht. Lokale Ollama-Instanzen brauchen keinen API-Key — lass das Feld einfach weg.

```yaml
providers:
  ollama:                          # Lokal – KEIN api_key
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
  ollama-online:                   # Cloud – MIT api_key
    type: ollama
    base_url: https://api.ollama.com
    api_key: dein-geheimer-api-key
    num_ctx: 32768
    models:
      default: llama3.3:70b
      aliases:
        big: llama3.3:70b
```

Der `api_key` wird als `Authorization: Bearer <key>` Header bei allen API-Calls mitgeschickt. In der Web-UI wird er maskiert angezeigt (erste 4 Zeichen + `****`).

### CLI-Verwaltung

```bash
miniassistant providers list              # Alle Provider anzeigen
miniassistant providers add ollama-online  # Neuen Provider hinzufügen (interaktiv)
miniassistant providers edit ollama-online # Provider bearbeiten
miniassistant providers delete ollama-online
miniassistant providers models ollama-online --online  # Verfügbare Modelle abrufen
```

---

## 15. Claude Code als Subagent

Du kannst **Claude Code** (Anthropic CLI) als Subagent-Provider nutzen. Claude Code hat eigene Tools (Dateizugriff, Shell, etc.) und wird über die `claude` CLI angesprochen — kein Ollama nötig.

### Voraussetzungen

1. Claude Code installieren: `npm install -g @anthropic-ai/claude-code`
2. Authentifizieren: `claude login` (OAuth) oder `ANTHROPIC_API_KEY` als Umgebungsvariable setzen

### Konfiguration

```yaml
providers:
  ollama:                          # Lokaler Provider (Standard)
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
      subagents: true
  claude:                          # Claude Code als Subagent
    type: claude-code              # ← Spezieller Typ!
    models:
      aliases:
        claude: claude-sonnet-4-20250514
```

### Nutzung

Der Hauptagent kann Claude über `invoke_model(model='claude/claude', message='...')` aufrufen. Claude Code:
- Handhabt **eigene Authentifizierung** (`claude login`)
- Hat **eigene Tools** (Shell, Dateisystem, Web) — braucht keine von uns
- Wird im **nicht-interaktiven Modus** aufgerufen (`claude --print`)
- Bekommt den `subagent.md` System-Prompt
- Ist auf **max. 3 Agentic-Turns** begrenzt
- Timeout: 5 Minuten pro Aufruf

### Vorteile

- **Kein extra Context** im Hauptagent (Persönlichkeit wird nur beim Aufruf injiziert)
- Claude Code managed seine eigene Auth — wir speichern **keinen API-Key**
- Lokales Modell (z.B. qwen3) bleibt Hauptagent, Claude wird nur bei Bedarf gerufen

---

## 15b. Anthropic Messages API (type: anthropic)

Alternativ zur Claude Code CLI kannst du die **Anthropic Messages API** direkt nutzen. Das ist schneller (kein CLI-Overhead) und unterstützt **Extended Thinking**.

### Voraussetzungen

- Anthropic API Key (https://console.anthropic.com/settings/keys)

### Konfiguration

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
      subagents: true
  anthropic:
    type: anthropic
    api_key: sk-ant-api03-DEIN-KEY-HIER
    think: true                    # Extended Thinking aktivieren
    models:
      default: claude-sonnet-4-20250514
      aliases:
        sonnet: claude-sonnet-4-20250514
        opus: claude-opus-4-20250514
        haiku: claude-haiku-35-20241022
```

### Verfügbare Modelle

```bash
miniassistant providers models anthropic --online
```

Listet alle verfügbaren Modelle über `GET /v1/models` auf.

### Extended Thinking

Wenn `think: true` im Provider oder `model_options` gesetzt ist, wird **Extended Thinking** aktiviert:
- Anthropic nennt das "extended thinking" (nicht "reasoning" wie bei Ollama)
- Budget: 10.000 Tokens für den Thinking-Prozess (konfigurierbar)
- Die Thinking-Ausgabe wird im Agent-Log protokolliert
- Kompatibel mit `claude-sonnet-4-20250514` und `claude-opus-4-20250514`

### Unterschied zu Claude Code (type: claude-code)

| Feature | `type: claude-code` | `type: anthropic` |
|---------|--------------------|--------------------|
| Auth | `claude login` (OAuth) | `api_key` in Config |
| Verbindung | CLI subprocess | HTTP API |
| Eigene Tools | Ja (Shell, Dateien) | Nein (nur Text) |
| Extended Thinking | Nein | Ja |
| Modelle abrufen | Statische Liste | Live via API |
| Geschwindigkeit | Langsamer (CLI-Start) | Schneller (direkt) |
| Kosten | Claude Code Abo | Pay-per-Token |

### API-Details

- **Base URL:** `https://api.anthropic.com` (konfigurierbar via `base_url`)
- **Auth:** `x-api-key` Header (nicht Bearer!)
- **API-Version:** `2023-06-01`
- **Streaming:** Unterstützt (SSE)
- **Maskierung:** `api_key` wird in der WebUI maskiert (erste 4 Zeichen + `****`)

---

## 15c. Google Gemini API (type: google)

Du kannst die **Google Gemini API** als Provider nutzen. Gemini-Modelle sind nativ multimodal (Vision + Text + Image Generation) und unterstützen Function Calling.

### Voraussetzungen

- Google API Key (https://aistudio.google.com/apikey)

### Konfiguration

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
      subagents: true
  google:
    type: google
    api_key: AIzaSy...DEIN-KEY-HIER
    think: true                    # Thinking aktivieren (nur Gemini 2.5+)
    models:
      default: gemini-2.5-flash
      aliases:
        flash: gemini-2.0-flash
        pro: gemini-2.5-pro
```

### Verfügbare Modelle

```bash
miniassistant providers models google --online
```

Listet alle verfügbaren Modelle über `GET /v1beta/models` auf.

### Thinking (Gemini 2.5+)

Wenn `think: true` im Provider oder `model_options` gesetzt ist, wird **Thinking** aktiviert:
- Nur bei Gemini 2.5 Pro und Gemini 2.5 Flash unterstützt
- Budget: 10.000 Tokens für den Thinking-Prozess
- Die Thinking-Ausgabe wird im Agent-Log protokolliert

### Vision (nativ)

Gemini-Modelle sind **nativ multimodal** — kein separates Vision-Modell nötig. Bilder werden direkt als `inlineData` (base64) in der Anfrage mitgeschickt. Wenn `vision.model` auf ein Google-Modell zeigt (z.B. `google/gemini-2.0-flash`), wird es automatisch für Bildanalyse genutzt.

### Image Generation

Gemini 2.0 Flash und Imagen unterstützen Bildgenerierung direkt über die API (`responseModalities: ["TEXT", "IMAGE"]`). Generierte Bilder werden als base64 zurückgegeben.

### Function Calling (Tools)

Google Gemini unterstützt **Function Calling** — alle MiniAssistant-Tools (exec, web_search, schedule, etc.) funktionieren. Das Tool-Schema wird automatisch ins Google-Format (`functionDeclarations`) konvertiert.

### Unterschied zu anderen Providern

| Feature | `type: google` | `type: anthropic` | `type: ollama` |
|---------|---------------|--------------------|--------------------|
| Auth | `api_key` (AIza...) | `api_key` (sk-ant...) | Optional (Bearer) |
| Vision | Nativ (alle Modelle) | Nein | Modell-abhängig |
| Image Gen | Ja (Gemini 2.0 Flash) | Nein | Modell-abhängig |
| Thinking | Gemini 2.5+ | Claude Sonnet/Opus | Modell-abhängig |
| Tools | Ja (functionDeclarations) | Nein (nur Text) | Modell-abhängig |
| Streaming | Ja (SSE) | Ja (SSE) | Ja (NDJSON) |
| Kosten | Pay-per-Token (Free Tier verfügbar) | Pay-per-Token | Lokal kostenlos |

### API-Details

- **Base URL:** `https://generativelanguage.googleapis.com` (konfigurierbar via `base_url`)
- **Auth:** `x-goog-api-key` Header
- **API-Version:** `v1beta`
- **Streaming:** Unterstützt (SSE via `streamGenerateContent?alt=sse`)
- **Maskierung:** `api_key` wird in der WebUI maskiert (erste 4 Zeichen + `****`)
- **Free Tier:** Gemini 2.0 Flash bietet ein kostenloses Kontingent (15 RPM, 1M TPM)

### CLI-Verwaltung

```bash
miniassistant providers add google           # Neuen Google-Provider hinzufügen (interaktiv)
miniassistant providers models google --online  # Verfügbare Modelle abrufen
miniassistant providers edit google           # Provider bearbeiten
miniassistant providers delete google
```

---

## 15d. OpenAI API (type: openai)

Du kannst die **OpenAI API** als Provider nutzen. GPT-4o-Modelle unterstützen Vision, Function Calling und Reasoning (o-Serie). DALL-E kann Bilder generieren.

**Auch kompatibel** mit OpenAI-kompatiblen APIs wie **Together AI**, **Groq**, **Perplexity**, **OpenRouter**, **vLLM**, etc. – einfach `base_url` anpassen.

### Voraussetzungen

- OpenAI API Key (https://platform.openai.com/api-keys)

### Konfiguration

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
      subagents: true
  openai:
    type: openai
    api_key: sk-...DEIN-KEY-HIER
    think: true                    # Reasoning aktivieren (nur o1/o3/o4-mini)
    num_ctx: 128000
    models:
      default: gpt-4o
      aliases:
        mini: gpt-4o-mini
        o4: o4-mini
```

### Verfügbare Modelle

```bash
miniassistant providers models openai --online
```

Listet alle verfügbaren Modelle über `GET /v1/models` auf.

### Reasoning (o-Serie)

Wenn `think: true` im Provider oder `model_options` gesetzt ist, wird **Reasoning** aktiviert:
- Nur bei o1, o3, o4-mini unterstützt
- `reasoning_effort`: `low`/`medium`/`high` (Default: `medium`)
- Die Reasoning-Ausgabe wird im Agent-Log protokolliert (als `reasoning_content`)

### Vision

GPT-4o-Modelle sind **multimodal** — sie unterstützen Vision. Bilder werden als `data:image/png;base64,...` URLs in der Anfrage mitgeschickt. Wenn `vision.model` auf ein OpenAI-Modell zeigt (z.B. `openai/gpt-4o`), wird es automatisch für Bildanalyse genutzt.

### Image Generation (DALL-E)

DALL-E 3 kann Bilder generieren über `POST /v1/images/generations`. Generierte Bilder werden als base64 zurückgegeben.

### Function Calling (Tools)

OpenAI unterstützt **Function Calling** — alle MiniAssistant-Tools (exec, web_search, schedule, etc.) funktionieren. Das Tool-Schema ist direkt kompatibel (identisches Format wie Ollama).

### OpenAI-kompatible APIs

Viele Cloud-Provider nutzen das OpenAI-Format. Einfach `base_url` anpassen:

```yaml
providers:
  groq:
    type: openai
    base_url: https://api.groq.com/openai
    api_key: gsk_...
    models:
      default: llama-3.3-70b-versatile
  together:
    type: openai
    base_url: https://api.together.xyz
    api_key: ...
    models:
      default: meta-llama/Llama-3.3-70B-Instruct-Turbo
```

### Unterschied zu anderen Providern

| Feature | `type: openai` | `type: google` | `type: anthropic` | `type: ollama` |
|---------|---------------|---------------|--------------------|--------------------|
| Auth | `api_key` (sk-...) | `api_key` (AIza...) | `api_key` (sk-ant...) | Optional (Bearer) |
| Vision | GPT-4o, o-Serie | Nativ (alle) | Nein | Modell-abhängig |
| Image Gen | DALL-E 3 | Gemini 2.0 Flash | Nein | Modell-abhängig |
| Reasoning | o1/o3/o4-mini | Gemini 2.5+ | Claude Sonnet/Opus | Modell-abhängig |
| Tools | Ja (functions) | Ja (functionDeclarations) | Nein (nur Text) | Modell-abhängig |
| Streaming | Ja (SSE) | Ja (SSE) | Ja (SSE) | Ja (NDJSON) |
| Kompatible APIs | Groq, Together, OpenRouter, vLLM, ... | — | — | — |

### API-Details

- **Base URL:** `https://api.openai.com` (konfigurierbar via `base_url`)
- **Auth:** `Authorization: Bearer <api_key>` Header
- **Chat:** `POST /v1/chat/completions`
- **Models:** `GET /v1/models`
- **Image Gen:** `POST /v1/images/generations`
- **Streaming:** SSE mit `stream: true`
- **Maskierung:** `api_key` wird in der WebUI maskiert (erste 4 Zeichen + `****`)

### CLI-Verwaltung

```bash
miniassistant providers add openai           # Neuen OpenAI-Provider hinzufügen (interaktiv)
miniassistant providers models openai --online  # Verfügbare Modelle abrufen
miniassistant providers edit openai           # Provider bearbeiten
miniassistant providers delete openai
```

---

## 15e. DeepSeek API (type: deepseek)

Du kannst die **DeepSeek API** als Provider nutzen. DeepSeek-V3 unterstützt Function Calling, DeepSeek-R1 bietet natives Reasoning (Thinking). Die API ist **OpenAI-kompatibel** – intern wird der `openai_client` verwendet.

### Voraussetzungen

- DeepSeek API Key (https://platform.deepseek.com/api_keys)

### Konfiguration

```yaml
providers:
  ollama:
    type: ollama
    base_url: http://127.0.0.1:11434
    models:
      default: qwen3:14b
      subagents: true
  deepseek:
    type: deepseek
    api_key: sk-...DEIN-KEY-HIER
    think: true                    # Reasoning aktivieren (nur DeepSeek-R1)
    num_ctx: 65536
    models:
      default: deepseek-chat
      aliases:
        r1: deepseek-reasoner
```

### Verfügbare Modelle

```bash
miniassistant providers models deepseek --online
```

Aktuelle Modelle:
- **deepseek-chat** – DeepSeek-V3 (Chat, Function Calling, 64k Context)
- **deepseek-reasoner** – DeepSeek-R1 (Reasoning/Thinking, 64k Context)

### Thinking (DeepSeek-R1)

Wenn `think: true` gesetzt ist und das Modell `deepseek-reasoner` genutzt wird:
- R1 liefert `reasoning_content` in der Antwort
- Die Thinking-Ausgabe wird im Agent-Log protokolliert
- Bei `deepseek-chat` (V3) hat `think` keinen Effekt

### Function Calling (Tools)

DeepSeek-V3 (`deepseek-chat`) unterstützt **Function Calling** – alle MiniAssistant-Tools (exec, web_search, schedule, etc.) funktionieren. DeepSeek-R1 (`deepseek-reasoner`) unterstützt **keine** Tools.

### Einschränkungen

- **Kein Vision** – DeepSeek-Modelle unterstützen keine Bildanalyse über die API
- **Keine Image Generation** – Keine Bildgenerierung verfügbar
- **Context:** 64k Tokens (V3 und R1)

### API-Details

- **Base URL:** `https://api.deepseek.com` (konfigurierbar via `base_url`)
- **Auth:** `Authorization: Bearer <api_key>` Header (OpenAI-kompatibel)
- **Chat:** `POST /v1/chat/completions`
- **Models:** `GET /v1/models`
- **Streaming:** SSE mit `stream: true`
- **Protokoll:** OpenAI-kompatibel (nutzt intern `openai_client.py`)
- **Preis:** Pay-per-Token, sehr günstig im Vergleich zu OpenAI/Anthropic

### CLI-Verwaltung

```bash
miniassistant providers add deepseek           # Neuen DeepSeek-Provider hinzufügen (interaktiv)
miniassistant providers models deepseek --online  # Verfügbare Modelle abrufen
miniassistant providers edit deepseek           # Provider bearbeiten
miniassistant providers delete deepseek
```

---

## 16. Globale Fallback-Modelle

Wenn das primäre Modell fehlschlägt (Timeout, HTTP-Fehler), werden Fallback-Modelle nacheinander versucht.

```yaml
fallbacks:
  - chipspc/llama-chips
  - qwen3:14b
```

Fallbacks gelten **nur für den Hauptagent** — Subagent-Aufrufe haben ihre eigene Fehlerbehandlung. Zusätzlich kann jeder Provider eigene Fallbacks unter `providers.<name>.models.fallbacks` haben. Die werden vor den globalen Fallbacks versucht.

---

## 17. Subagents (Worker-Modelle)

Subagents erlauben dem Hauptagent, Aufgaben an andere Modelle zu delegieren.

```yaml
subagents:
  - chipspc/llama-chips
  - qwen3-coder
```

### Fähigkeiten der Subagents

Subagents haben **eingeschränkte Tools**:
- ✅ `exec` — Shell-Befehle ausführen (mit Sicherheitsregeln)
- ✅ `web_search` — Websuche
- ✅ `check_url` — URLs prüfen
- ❌ `save_config` — Konfiguration ändern (nur Hauptagent)
- ❌ `schedule` — Jobs planen (nur Hauptagent)
- ❌ `invoke_model` — Weitere Subagents aufrufen (keine Kaskade)

### Sicherheit

Subagents haben eigene Sicherheitsregeln (aus `basic_rules/subagent.md`):
- Kein `rm -rf` — immer Trash verwenden
- Keine System-Pakete installieren ohne explizite Anweisung
- Keine Services starten/stoppen
- Max. 3 Tool-Runden pro Aufruf

### Kontext

Die Subagent-Persönlichkeit wird **nur beim Aufruf** injiziert und verbraucht **keinen Kontext** im Hauptagent. Das bedeutet: mehr Platz für Konversation und Memory im Hauptagent.

---

## 18. basic_rules (editierbare Verhaltensregeln)

Die Verhaltensregeln des Assistenten liegen als **editierbare Markdown-Dateien** im Agent-Verzeichnis:

```
agent_dir/basic_rules/
├── safety.md              # Sicherheitsregeln, Prompt Injection Defense
├── exec_behavior.md       # Exec-Verhaltensregeln (Research first, one command at a time, …)
├── knowledge_verification.md  # Wissensverifikation (web_search bei Unsicherheit)
├── language.md            # Antwortsprache (Default: Deutsch)
└── subagent.md            # Regeln für Subagent-Aufrufe
```

### Verhalten

- **Automatisch erstellt:** Beim ersten Start werden die Default-Dateien nach `agent_dir/basic_rules/` kopiert.
- **User-editierbar:** Du kannst die Dateien frei anpassen — sie werden **nie** automatisch überschrieben.
- **Wiederherstellung:** Wenn eine Datei gelöscht wird, wird sie beim nächsten Start aus den Defaults neu erstellt.
- **RAM-Cache:** Die Dateien werden beim Start eingelesen und im Speicher gehalten. Änderungen werden erst nach einem Neustart wirksam.
- **Eigene Regeln:** Du kannst zusätzliche `.md`-Dateien in `basic_rules/` anlegen — sie werden ebenfalls geladen.

---

## 19. Notizen und Merkdateien

Der Assistent kann sich Dinge merken — Präferenzen, Projektnotizen, etc.

### Präferenzen (`prefs/`)

Wenn der User sagt "merke dir…" oder "speichere meine Präferenz für…", schreibt der Assistent eine `.md`-Datei nach `agent_dir/prefs/`. Diese werden beim Start in den Kontext geladen.

```
agent_dir/prefs/
├── wetter.md          # Wetteranzeige-Präferenzen
├── backup.md          # Backup-Einstellungen
└── display.md         # Darstellungs-Präferenzen
```

### Projektnotizen

Bei "mach dir Notizen" schreibt der Assistent eine Zusammenfassung nach `agent_dir/prefs/notes-TOPIC.md`. Bei "schau dir die Notizen an" liest er sie als Kontext.

### Memory

Automatische Gesprächs-Zusammenfassungen (letzte N Tage):

```yaml
memory:
  days: 2                # Tage im Prompt (Default: 2)
  max_chars_per_line: 300 # Zeilenlänge kürzen (Default: 300, 0 = kein Limit)
  max_tokens: 6000        # Token-Budget für Memory (Default: 6000)
```

`max_tokens` verhindert, dass Memory zu viel Kontext verbraucht. Bei 32k num_ctx: ~8k System + ~6k Memory + ~1.3k Tools = ~15k, Rest für Konversation.

---

## 20. Vision & Bildgenerierung

### Vision (Bildanalyse)

Wenn ein Vision-Modell konfiguriert ist, kann der Assistent Bilder analysieren (z. B. Bild-Uploads in Discord/Matrix).

```yaml
vision:
  model: "llava:13b"       # Modellname (Ollama oder provider/model)
  num_ctx: 32768            # optional: Context-Größe für das Vision-Modell
```

**Kurzform** (nur Modellname):
```yaml
vision: "llava:13b"
```

Wenn das aktuelle Chat-Modell selbst Vision unterstützt (z. B. `gemma3`, `llava`, `minicpm-v`), wird direkt analysiert — kein Umweg über ein separates Modell.

Ohne `vision`-Config: Der Assistent weist den User darauf hin, ein Vision-Modell zu konfigurieren.

### Bildgenerierung

```yaml
image_generation:
  model: "stable-diffusion"  # Modellname
  num_ctx: 32768              # optional
```

**Kurzform:**
```yaml
image_generation: "stable-diffusion"
```

### Bekannte Vision-Modelle

| Modell | Beschreibung |
|--------|-------------|
| `llava:13b` / `llava:7b` | LLaVA (gute allgemeine Vision) |
| `gemma3` | Google Gemma 3 (eingebaute Vision) |
| `minicpm-v` | MiniCPM-V (leichtgewichtig) |
| `llama3.2-vision` | Llama 3.2 Vision |

### Verhalten

- **Immer verfügbar** wenn konfiguriert — kein `enabled: true` nötig, nicht an Provider gebunden wie Subagents.
- **Avatar ändern:** Der Assistent kann sein eigenes Profilbild auf Matrix/Discord setzen. Details in `docs/AVATARS.md`.
