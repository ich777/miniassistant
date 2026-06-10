#!/bin/sh
# MiniAssistant – Installation so schmerzlos wie möglich
# Nutzung: ./install.sh [Zielverzeichnis]
# - Prüft Python und erforderliche Tools
# - Erstellt venv (falls nicht vorhanden), installiert Abhängigkeiten
# - Installiert bei Bedarf System-Pakete (python3, venv, pip, libolm für Matrix-E2EE)
# - Optional: Init-Skript nach /etc/init.d/miniassistant installieren (mit --init)

set -e

INSTALL_DIR="."
INIT_INSTALL=""
SYSTEMD_INSTALL=""
MIGRATE_MEMPALACE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --init)               INIT_INSTALL=1 ;;
    --systemd)            SYSTEMD_INSTALL=1 ;;
    --migrate-mempalace)  MIGRATE_MEMPALACE=1 ;;
    *)                    INSTALL_DIR="$1" ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Root-Check: als root kein sudo in den ausgegebenen Befehlen
if [ "$(id -u)" = "0" ]; then
  SUDO=""
else
  SUDO="sudo "
fi

# --- System-Pakete installieren (vor der Python-Prüfung); bei Fehler (z. B. ohne sudo) weitermachen ---
# Für Matrix-E2EE: libolm-dev, cmake, make, python3-dev (Header für C-Erweiterungen wie python-olm)
echo "System-Pakete (python3, venv, pip; für Matrix-E2EE: libolm-dev, cmake, make, python3-dev; für Voice: ffmpeg; Emoji-Schrift: fonts-noto-color-emoji; für Group-Room-Sandbox: bubblewrap)..."
if command -v apt-get >/dev/null 2>&1; then
  ( $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-venv python3-pip python3-dev python3-yaml libolm-dev cmake build-essential ffmpeg fonts-noto-color-emoji bubblewrap ) 2>/dev/null || {
    ( $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-venv python3-pip python3-dev python3-yaml libolm-dev cmake make ffmpeg fonts-noto-color-emoji bubblewrap ) 2>/dev/null || \
    ( $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-venv python3-pip python3-yaml ffmpeg fonts-noto-color-emoji bubblewrap ) 2>/dev/null || \
    ( $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-venv python3-pip python3-yaml fonts-noto-color-emoji bubblewrap ) 2>/dev/null || \
    ( $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-venv python3-pip python3-yaml ) 2>/dev/null || true
    echo "  Hinweis: Installation fehlgeschlagen oder abgebrochen (z. B. ohne sudo). Für E2EE: libolm-dev, cmake, make, python3-dev. Für Voice: ffmpeg. Für Emoji-CLI: fonts-noto-color-emoji. Für Group-Room-exec-Sandbox: bubblewrap."
  }
elif command -v dnf >/dev/null 2>&1; then
  ( $SUDO dnf install -y python3 python3-virtualenv python3-pip python3-pyyaml olm-devel cmake make ffmpeg google-noto-emoji-fonts bubblewrap ) 2>/dev/null || \
  ( $SUDO dnf install -y python3 python3-virtualenv python3-pip python3-pyyaml ffmpeg google-noto-emoji-fonts bubblewrap ) 2>/dev/null || \
  ( $SUDO dnf install -y python3 python3-virtualenv python3-pip python3-pyyaml ) 2>/dev/null || true
elif command -v apk >/dev/null 2>&1; then
  ( $SUDO apk add python3 py3-pip py3-venv py3-yaml olm-dev cmake make ffmpeg font-noto-emoji bubblewrap ) 2>/dev/null || \
  ( $SUDO apk add python3 py3-pip py3-venv py3-yaml ffmpeg font-noto-emoji bubblewrap ) 2>/dev/null || \
  ( $SUDO apk add python3 py3-pip py3-venv py3-yaml ) 2>/dev/null || true
else
  echo "  Unbekannter Paketmanager. Bitte manuell: python3, python3-venv, python3-pip; für E2EE: libolm, cmake, make; für Voice: ffmpeg; für Emoji: fonts-noto-color-emoji; für Group-Room-Sandbox: bubblewrap."
fi
echo ""

# --- Prüfung: Python und erforderliche Tools ---
echo "Prüfe Voraussetzungen..."

if ! command -v python3 >/dev/null 2>&1; then
  echo "Fehler: python3 nicht gefunden." >&2
  echo "  Bitte Python 3.10 oder neuer installieren (z.B. ${SUDO}apt install python3 python3-venv python3-pip)." >&2
  echo "  Das Install-Skript versucht zuvor, System-Pakete zu installieren (mit sudo)." >&2
  exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || true
if [ -z "$PY_VERSION" ]; then
  echo "Fehler: python3-Version konnte nicht ermittelt werden." >&2
  exit 1
fi
# Mindestversion 3.10 (einfacher Check: 3.9 < 3.10)
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" = 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "Fehler: MiniAssistant benötigt Python 3.10 oder neuer. Gefunden: $PY_VERSION." >&2
  echo "  Bitte eine neuere Python-Version installieren." >&2
  exit 1
fi
echo "  Python $PY_VERSION gefunden."

if ! python3 -c "import venv" 2>/dev/null; then
  echo "Fehler: Python-Modul 'venv' nicht verfügbar." >&2
  echo "  Debian/Ubuntu: ${SUDO}apt install python3-venv" >&2
  echo "  Fedora: ${SUDO}dnf install python3-virtualenv" >&2
  echo "  Alpine: ${SUDO}apk add python3 py3-pip" >&2
  exit 1
fi
echo "  Modul venv verfügbar."

if python3 -m pip --version >/dev/null 2>&1; then
  echo "  pip verfügbar."
else
  echo "  Hinweis: System-pip nicht gefunden. Venv verwendet ggf. ensurepip."
  echo "  Falls 'pip install' später fehlschlägt: Debian/Ubuntu: ${SUDO}apt install python3-pip python3-venv"
fi
echo "  Voraussetzungen OK."
echo ""

echo "MiniAssistant – Installation in $INSTALL_DIR"

# venv: absoluten Pfad verwenden, damit activate und pip immer gefunden werden
if [ "$INSTALL_DIR" = "." ]; then
  VENV_DIR="${SCRIPT_DIR}/venv"
else
  VENV_DIR="$(cd "$SCRIPT_DIR" && cd "$INSTALL_DIR" && pwd)/venv"
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "Erstelle venv: $VENV_DIR"
  if ! python3 -m venv "$VENV_DIR"; then
    echo "Fehler: venv-Erstellung fehlgeschlagen." >&2
    echo "  Debian/Ubuntu: ${SUDO}apt install python3-venv" >&2
    exit 1
  fi
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "Fehler: venv unvollständig (bin/activate fehlt): $VENV_DIR" >&2
  echo "  Bitte venv löschen (rm -rf $VENV_DIR) und erneut ausführen." >&2
  echo "  Oder: ${SUDO}apt install python3-venv python3-pip" >&2
  exit 1
fi
. "$VENV_DIR/bin/activate"

# Pip im venv sicherstellen (falls venv ohne pip erstellt wurde, z.B. minimales python3-venv)
if ! "$VENV_DIR/bin/python3" -m pip --version >/dev/null 2>&1; then
  echo "Pip im venv aktivieren..."
  "$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null || true
fi

# Abhängigkeiten (aus pyproject.toml inkl. optionale Extras: matrix, discord, scheduler, mempalace, docs, browser)
# browser = curl_cffi (Chrome/Safari TLS-Impersonation) — nötig für read_url Anti-Bot-Fallback (Cloudflare/CDN 403)
echo "Installiere Abhängigkeiten..."
"$VENV_DIR/bin/python3" -m pip install -q --upgrade pip
"$VENV_DIR/bin/python3" -m pip install -q -e '.[matrix,discord,scheduler,mempalace,docs,browser]'

# Kurz prüfen, ob matrix-nio im selben venv importierbar ist (für Matrix-Bot)
if ! "$VENV_DIR/bin/python3" -c "import nio" 2>/dev/null; then
  echo "Hinweis: matrix-nio (Matrix-Bot) konnte nicht geladen werden. Erneut installieren mit:"
  echo "  $VENV_DIR/bin/python3 -m pip install -e '.[matrix,discord,scheduler]'"
fi
# Discord prüfen
if ! "$VENV_DIR/bin/python3" -c "import discord" 2>/dev/null; then
  echo "Hinweis: discord.py konnte nicht geladen werden. Erneut: $VENV_DIR/bin/python3 -m pip install -e '.[discord]'"
fi
# Dokument-Anhaenge (PDF/DOCX): pypdf + pypdfium2 + python-docx
if "$VENV_DIR/bin/python3" -c "import pypdf, pypdfium2, docx" 2>/dev/null; then
  echo "  Dokument-Extraktion (PDF/DOCX) verfuegbar."
else
  echo "Hinweis: Dokument-Extraktion nicht voll verfuegbar. Erneut: $VENV_DIR/bin/python3 -m pip install -e '.[docs]'"
fi
# mempalace + ChromaDB prüfen (semantisches Gedächtnis)
if "$VENV_DIR/bin/python3" -c "import mempalace; import chromadb" 2>/dev/null; then
  MP_VER=$("$VENV_DIR/bin/python3" -c "import mempalace; print(getattr(mempalace, '__version__', mempalace.version.__version__ if hasattr(mempalace, 'version') else '?'))" 2>/dev/null || echo "?")
  echo "  mempalace $MP_VER + ChromaDB verfügbar."
else
  echo "Hinweis: mempalace oder ChromaDB konnte nicht geladen werden. Erneut:"
  echo "  $VENV_DIR/bin/python3 -m pip install -e '.[mempalace]'"
  echo "  Falls ChromaDB Probleme macht: $VENV_DIR/bin/python3 -m pip install 'chromadb>=0.5,<0.7'"
fi
# Matrix-E2EE (Entschlüsselung): pip install matrix-nio[e2e] – braucht libolm, cmake, make (s. o. System-Pakete)
E2EE_OK=0
if "$VENV_DIR/bin/python3" -c "import nio" 2>/dev/null; then
  echo "Installiere Matrix-E2EE (matrix-nio[e2e])..."
  if ! "$VENV_DIR/bin/python3" -m pip install -q matrix-nio[e2e] 2>/dev/null; then
    echo "  Fehlgeschlagen. Versuche ohne Stille, um Fehler zu sehen:"
    "$VENV_DIR/bin/python3" -m pip install matrix-nio[e2e] || true
  fi
  if "$VENV_DIR/bin/python3" -c "from nio.crypto import ENCRYPTION_ENABLED; exit(0 if ENCRYPTION_ENABLED else 1)" 2>/dev/null; then
    echo "Matrix-E2EE: aktiv (Entschlüsselung im Bot verfügbar)."
    E2EE_OK=1
  else
    echo "Matrix-E2EE: nicht verfügbar – Bot kann verschlüsselte Nachrichten nicht lesen."
    echo "  Benötigt: libolm-dev, cmake, make, python3-dev (z. B. ${SUDO}apt install libolm-dev cmake build-essential python3-dev)."
    echo "  Danach: $VENV_DIR/bin/python3 -m pip install matrix-nio[e2e]"
  fi
fi

# --- mempalace Migration: bestehende Memory-Dateien in Palace importieren ---
if [ -n "$MIGRATE_MEMPALACE" ]; then
  echo ""
  echo "mempalace Migration: Prüfe Voraussetzungen..."
  if ! "$VENV_DIR/bin/python3" -c "import chromadb; import mempalace" 2>/dev/null; then
    echo "  Fehler: mempalace oder ChromaDB nicht installiert." >&2
    echo "  Bitte zuerst ./install.sh ohne --migrate-mempalace ausführen." >&2
  else
    echo "  mempalace + ChromaDB verfügbar."
    echo "  Initialisiere Palace und importiere bestehende Memory-Dateien..."
    echo "  (Das kann bei vielen Dateien 2-5 Minuten dauern.)"
    "$VENV_DIR/bin/python3" -c "
import os, sys
os.environ['ANONYMIZED_TELEMETRY'] = 'False'
try:
    from miniassistant.config import load_config
    cfg = load_config()
    mp = cfg.get('mempalace') or {}
    if not mp.get('enabled', False):
        print('  mempalace ist in der Config nicht aktiviert.')
        print('  Bitte zuerst in config.yaml setzen:')
        print('    mempalace:')
        print('      enabled: true')
        sys.exit(0)
    from miniassistant.memory import init_mempalace, import_existing_memories
    palace_path = init_mempalace()
    print(f'  Palace erstellt: {palace_path}')
    stats = import_existing_memories(palace_path=palace_path)
    print(f'  Import abgeschlossen:')
    print(f'    Dateien:          {stats[\"files\"]}')
    print(f'    Importiert:       {stats[\"imported\"]}')
    print(f'    Noise gefiltert:  {stats[\"skipped_noise\"]}')
    print(f'    Bereits vorhanden:{stats[\"skipped_existing\"]}')
    # Marker setzen
    from pathlib import Path
    (Path(palace_path) / '.memory_imported').write_text(
        f'imported={stats[\"imported\"]} files={stats[\"files\"]} noise={stats[\"skipped_noise\"]}\n'
    )
    print('  Marker .memory_imported gesetzt — kein erneuter Import beim Start.')
except Exception as e:
    print(f'  Fehler bei Migration: {e}', file=sys.stderr)
    sys.exit(1)
"
    if [ $? -eq 0 ]; then
      echo "  mempalace Migration erfolgreich abgeschlossen."
    fi
  fi
fi

echo ""
echo "Installation abgeschlossen. Aktivieren: source ${VENV_DIR}/bin/activate"
echo "Dann: miniassistant config   (Konfiguration / Ersteinrichtung)"
echo "      miniassistant serve     (Web-UI starten)"
echo "      miniassistant chat      (CLI-Chat)"
echo "      miniassistant matrix-e2ee-check   (prüfen, ob Entschlüsselung aktiv)"
echo ""
echo ""
echo "Optional: JS-Rendering (Playwright) für JavaScript-lastige Seiten (SPAs, React/Vue/Angular):"
echo "  $VENV_DIR/bin/python3 -m pip install 'miniassistant[js]'"
echo "  $VENV_DIR/bin/playwright install chromium   (~300 MB)"
echo ""
echo "mempalace (AI Memory mit semantischer Suche) wurde mitinstalliert."
echo "  Aktivieren in config.yaml:"
echo "    mempalace:"
echo "      enabled: true"
echo "  Palace wird automatisch beim Service-Start erstellt."
echo "  Bestehende Memory-Dateien importieren: ./install.sh --migrate-mempalace"
echo "  Spart ~3500 Tokens im System-Prompt (L0+L1 statt raw dump)."
echo ""
echo "Hinweis Emoji im CLI-Chat: fonts-noto-color-emoji wurde installiert."
echo "  Für korrekte Darstellung wird ein modernes Terminal empfohlen"
echo "  (GNOME Terminal, kitty, WezTerm, Windows Terminal)."
echo "  Bei Bedarf: fc-cache -f  (Font-Cache aktualisieren)"
if [ "$E2EE_OK" = "0" ] && "$VENV_DIR/bin/python3" -c "import nio" 2>/dev/null; then
  echo ""
  echo "Hinweis: Matrix-E2EE ist nicht aktiv. Für verschlüsselte Räume zuerst Build-Pakete installieren (s. o.), dann install.sh erneut ausführen oder: pip install matrix-nio[e2e]"
fi

if [ -n "$INIT_INSTALL" ]; then
  INIT_DEST="/etc/init.d/miniassistant"
  if [ -w /etc/init.d ] 2>/dev/null || [ "$(id -u)" = "0" ]; then
    sed -e "s|%INSTALL_DIR%|$SCRIPT_DIR|g" \
        "$SCRIPT_DIR/init.d/miniassistant" > "$INIT_DEST"
    chmod 755 "$INIT_DEST"
    echo "Init-Skript installiert: $INIT_DEST"
    echo "  ${SUDO}update-rc.d miniassistant defaults  # Debian/Ubuntu: Autostart"
    echo "  ${SUDO}service miniassistant start"
  else
    echo "Hinweis: Init-Skript manuell installieren (als root oder mit sudo):"
    echo "  ${SUDO}sed 's|%INSTALL_DIR%|$SCRIPT_DIR|g' init.d/miniassistant > /etc/init.d/miniassistant"
    echo "  ${SUDO}chmod 755 /etc/init.d/miniassistant"
    echo "  ${SUDO}update-rc.d miniassistant defaults  # Debian/Ubuntu"
  fi
fi

if [ -n "$SYSTEMD_INSTALL" ]; then
  SVC="miniassistant.service"
  SVC_SRC="$SCRIPT_DIR/systemd/$SVC"
  if [ ! -f "$SVC_SRC" ]; then
    echo "Systemd-Vorlage nicht gefunden: $SVC_SRC" >&2
  elif [ -w /etc/systemd/system ] 2>/dev/null || [ "$(id -u)" = "0" ]; then
    sed -e "s|%INSTALL_DIR%|$SCRIPT_DIR|g" "$SVC_SRC" > "/etc/systemd/system/$SVC"
    echo "Systemd-Unit installiert: /etc/systemd/system/$SVC"
    echo "  ${SUDO}systemctl daemon-reload"
    echo "  ${SUDO}systemctl enable --now miniassistant"
  else
    echo "Systemd-Unit manuell installieren (als root oder mit sudo):"
    echo "  ${SUDO}sed 's|%INSTALL_DIR%|$SCRIPT_DIR|g' $SVC_SRC > /etc/systemd/system/$SVC"
    echo "  ${SUDO}systemctl daemon-reload"
    echo "  ${SUDO}systemctl enable --now miniassistant"
  fi
fi
