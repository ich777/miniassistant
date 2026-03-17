# miniclient — MiniAssistant Go-Client

Portabler Terminal-Client für MiniAssistant. Einzelnes Binary, keine Abhängigkeiten.

## Build

```bash
go mod tidy   # lädt Abhängigkeit (readline)
go build -o miniclient .
```

Cross-Compile (Beispiele):
```bash
GOOS=linux  GOARCH=amd64   go build -o miniclient-linux-amd64 .
GOOS=linux  GOARCH=arm64   go build -o miniclient-linux-arm64 .
GOOS=darwin GOARCH=arm64   go build -o miniclient-macos-arm64 .
GOOS=windows GOARCH=amd64  go build -o miniclient.exe .
```

## Einzel-Frage (Scripting)

```bash
# Frage als Argument
miniclient --question "Was ist die Hauptstadt von Frankreich?"
miniclient -q "Erkläre diesen Fehler: segfault at 0x0"

# Pipe (für Scripting)
echo "Fasse diese Ausgabe zusammen:" | cat - build.log | miniclient -q
cat error.log | miniclient -q "Was bedeutet dieser Fehler?"
git diff | miniclient -q "Schreib eine Commit-Message für diesen Diff"
```

Die Antwort geht auf **stdout**, Fehler auf stderr — gut für Shell-Pipelines.
Kein Session-Speicher, kein interaktiver Modus, kein Spinner.

## Quickstart

```bash
# Ersteinrichtung (Server-URL, Token, Modell, Tool-Ausführung)
./miniclient config

# Chat starten
./miniclient
```

## Befehle

| Befehl | Beschreibung |
|--------|-------------|
| `miniclient` | Chat starten |
| `miniclient config` | Konfiguration erstellen/bearbeiten |
| `miniclient config --show` | Aktuelle Config anzeigen |
| `miniclient --sessions` | Alle Sessions auflisten |
| `miniclient --continue` | Session auswählen und fortsetzen |
| `miniclient --continue 2` | Session Nr. 2 direkt fortsetzen |
| `miniclient --continue abc123` | Session per ID-Prefix fortsetzen |
| `miniclient --question TEXT` | Einzel-Frage, Antwort auf stdout, beenden |
| `miniclient -q TEXT` | Kurzform von `--question` |
| `miniclient -h` | Hilfe |

## Konfiguration

Gespeichert unter `~/.config/miniassistant/config.json`:

```json
{
  "server": "http://192.168.1.100:8765",
  "token": "dein-token",
  "model": "",
  "local_tools": [],
  "proxy": ""
}
```

Das `proxy`-Feld gilt nur für lokale `read_url`-Aufrufe. Unterstützte Formate:

```
http://host:port
https://host:port
socks5://host:port
socks5://user:pass@host:port
```

Konfigurierbar über `miniclient config` (Schritt 5) oder manuell in der JSON-Datei.
Leer lassen oder weglassen = kein Proxy.

Umgebungsvariablen (überschreiben Config-Datei):

```bash
MINIASSISTANT_URL=http://host:8765
MINIASSISTANT_TOKEN=dein-token
MINIASSISTANT_MODEL=qwen3:8b
```

## Session-Persistenz

Sessions werden unter `~/.config/miniassistant/sessions/<id>.json` gespeichert.
Beim nächsten Start wird die letzte Session angezeigt und angeboten fortzusetzen.
Der Client generiert automatisch einen kurzen Titel (via LLM) für jede neue Session.

```bash
# Alle Sessions anzeigen
miniclient --sessions

# Bestimmte Session fortsetzen
miniclient --continue 2
miniclient --continue abc12345
```

Funktioniert auch nach einem Server-Neustart: die gespeicherten Messages werden beim
ersten Request an den Server gesendet, sodass der Kontext vollständig wiederhergestellt wird.

Kompaktierungen (automatische Zusammenfassungen langer Verläufe) werden transparent
übernommen — die kompaktierte Version wird lokal gespeichert.

## Lokale Tool-Ausführung

Standardmäßig werden alle Tools serverseitig ausgeführt. Optional können `exec` und
`read_url` auf dem **Client-System** ausgeführt werden (konfigurierbar über `miniclient config`):

| Tool | Lokal | Serverseitig |
|------|-------|-------------|
| `exec` | Befehle auf diesem Rechner | Befehle auf dem Server |
| `read_url` | URLs von diesem Rechner | URLs vom Server |
| `web_search` | — | immer serverseitig |
| `send_mail` | — | immer serverseitig |

**Sicherheit:** Lokales `exec` führt Befehle direkt auf dem Client-System aus.
Nur aktivieren, wenn du dem MiniAssistant-Server vertraust.

## Tastaturkürzel

Die readline-Bibliothek (`ergochat/readline`) bietet volle Terminal-Navigation:

| Kürzel | Funktion |
|--------|---------|
| `↑` / `↓` | Verlauf durchblättern |
| `Ctrl+←` / `Ctrl+→` | Wortweise navigieren |
| `Ctrl+A` / `Ctrl+E` | Zeilenanfang / -ende |
| `Ctrl+K` | Rest der Zeile löschen |
| `Ctrl+W` | Letztes Wort löschen |
| `Ctrl+C` | Session beenden (wird gespeichert) |

## Markdown-Rendering

Der Client rendert Markdown-Antworten im Terminal: Tabellen mit Rahmen,
Code-Blöcke, Überschriften, fetter Text, Listen und mehr — ohne externe Abhängigkeiten.

## Anforderungen

- Go 1.21 oder neuer (nur für den Build)
- Fertige Binaries laufen ohne Go-Installation
- `curl` und `jq` werden **nicht** benötigt (anders als `macli.sh`)
- Einzige Abhängigkeit: `github.com/ergochat/readline` (wird von `go mod tidy` geladen)
