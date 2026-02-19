# MiniAssistant

### German only... Sorry...

Schlanker lokaler Assistent mit Multi-Provider-Support: **Ollama** (lokal/remote), **Google Gemini API**, **OpenAI API** (GPT-4o, DALL-E, o-Serie), **DeepSeek API** (V3, R1), **Anthropic API** (Claude), **Claude Code CLI**. Agent-Dateien (optional AGENTS.md, SOUL, IDENTITY, TOOLS, USER), Modellwechsel per `/model MODELLNAME` oder Alias, Token-Auth, kleines Memory, minimalistisches Web-UI und CLI.

## Installation (so schmerzlos wie möglich)

**Voraussetzungen:** Python 3.10 oder neuer, Modul `venv` (z.B. Debian: `apt install python3 python3-venv python3-pip`). Das Install-Skript prüft das vor dem Start.

```bash
cd /pfad/zum/projekt
./install.sh
source venv/bin/activate
```

**System-Pakete:** Das Install-Skript versucht zu Beginn automatisch, System-Pakete zu installieren (python3, python3-venv, python3-pip, libolm-dev für Matrix-E2EE). Dafür sind ggf. Rechte nötig (z. B. `sudo ./install.sh`). Schlägt die Installation fehl, die genannten Pakete manuell installieren und `./install.sh` erneut ausführen.

Optional: Init-Skript (sysvinit) oder systemd installieren:

```bash
# sysvinit (z. B. Debian ohne systemd)
./install.sh --init
sudo service miniassistant start

# systemd
./install.sh --systemd
sudo systemctl daemon-reload
sudo systemctl enable --now miniassistant
```

## Ersteinrichtung

Ähnlich wie bei OpenClaw – geführt über CLI oder später Web:

```bash
miniassistant config
```

Durchlauf: Ollama-URL, num_ctx, Bind (localhost oder 0.0.0.0), Port, Agent-Verzeichnis, Standard-Modell, optional SearXNG-URL. Config landet in `~/.config/miniassistant/config.yaml` oder im Projekt als `miniassistant.yaml`.

## Ollama-Optionen (temperature, top_p, …)

Unter `ollama.options` können alle von Ollama unterstützten **ModelOptions** gesetzt werden (werden bei jedem Chat-Request mitgeschickt):

- **temperature** (float): Zufälligkeit (niedriger = deterministischer, z. B. 0.2 für Fakten; höher = kreativer, z. B. 0.8–1.2). Default oft 0.8.
- **top_p** (float): Nucleus Sampling (z. B. 0.9). Nur entweder temperature oder top_p stark verändern.
- **top_k** (int): Nur die K wahrscheinlichsten nächsten Tokens (z. B. 40).
- **num_ctx** (int): Kontextlänge in Tokens (kann auch oben unter `ollama.num_ctx` stehen).
- **num_predict** (int): Maximale Anzahl zu generierender Tokens.
- **seed** (int): Fester Zufallssamen für reproduzierbare Ausgaben.
- **min_p** (float): Mindest-Wahrscheinlichkeit für Token-Auswahl.
- **stop** (string oder Liste): Stop-Sequenzen, die die Generierung beenden.

Beispiel in `config.yaml` / `miniassistant.yaml`:

```yaml
ollama:
  base_url: http://127.0.0.1:11434
  num_ctx: 8192
  think: true
  options:
    temperature: 0.7
    top_p: 0.9
    top_k: 40
```

## Nutzung

- **CLI-Chat**: `miniassistant chat` (Modellwechsel: `/model MODELLNAME` oder `/model ALIAS`)
- **Web-UI**: `miniassistant serve` – dann http://127.0.0.1:8765 (Host/Port in Config änderbar, z. B. `server.port: 8080` oder `--port 8080`)
- **Token**: Beim ersten `serve` wird ein Token generiert (CLI-Ausgabe oder `miniassistant token`). **Im Browser:** Startseite öffnen, Token eingeben und „Zum Chat“ klicken – oder direkt z. B. `http://host:8765/chat?token=DEIN_TOKEN` aufrufen. API: `?token=...` in der URL oder Header `Authorization: Bearer <token>`.

## Binden auf alle Interfaces

In der Config `server.host: "0.0.0.0"` setzen (miniassistant config oder YAML). **Wichtig**: Token setzen, damit nur Berechtigte zugreifen.

## System-Erkennung

Die LLM erfährt automatisch, auf welchem System sie läuft (OS, Distribution, Paketmanager, Init-System), damit sie die passenden Befehle nutzt (z. B. apt vs dnf, systemctl vs service). Erkannt werden u. a. Debian/Ubuntu, Fedora/RHEL, Arch, Alpine, openSUSE, macOS.

## Sprache, Merken, Scheduler

- **Sprache:** Die Antwortsprache kommt aus **IDENTITY.md** (z.B. „Response language: Deutsch“ oder „language: English“). Beim Onboarding wird sie abgefragt und dort eingetragen – es gibt keinen Config-Eintrag `language` mehr.
- **Merken:** Das Modell weiß, dass es mit **exec** Dateien lesen/schreiben kann. Wenn `workspace` oder `agent_dir` gesetzt sind, werden diese Pfade im System-Prompt genannt – der Assistent kann dort Notizen anlegen, wenn du ihn darum bittest.
- **Geplante Jobs (ohne System-Cron):** Optional `scheduler.enabled: true` in der Config und `pip install miniassistant[scheduler]`. Dann steht das Tool **schedule** zur Verfügung (Cron z. B. `0 9 * * *` oder „in 30 minutes“). Jobs werden beim `serve` ausgeführt.

## Konfiguration (Doku)

Alle Einträge sind **optional**. Fehlt etwas in der Config, werden Defaults bzw. Ollama-Standards genutzt – es bricht nichts.

Ausführlich und strukturiert: **[CONFIGURATION.md](CONFIGURATION.md)** (wo die Config liegt, alle Bereiche, Optionen, Beispiele). Die Doku wird mit dem Code mitgepflegt.

- **Smart Compacting:** Wenn der Chatverlauf den Kontext füllt, werden ältere Messages automatisch zusammengefasst. Steuerbar via `chat.context_quota` (Default: 0.85 = 85% von `num_ctx`). Skaliert automatisch mit der Modellgröße.

## Plan

Siehe [MINIASSISTANT_PLAN.md](MINIASSISTANT_PLAN.md) für Features: mehrere Modelle + Aliase, Memory bei Modellwechsel, Bootup-Assistent, SOUL selbst schreiben, Matrix optional.

## Lizenz

MIT
