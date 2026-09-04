#!/bin/sh
# MiniAssistant – Installation so schmerzlos wie möglich
# Nutzung: ./install.sh [Zielverzeichnis]
# - Prüft Python und erforderliche Tools
# - Erstellt venv (falls nicht vorhanden), installiert Abhängigkeiten
# - Installiert bei Bedarf System-Pakete (python3, venv, pip, libolm für Matrix-E2EE)
# - Optional: Init-Skript nach /etc/init.d/miniassistant installieren (mit --init)
# - Optional: launchd-Job auf macOS installieren (mit --launchd)

set -e

INSTALL_DIR="."
INIT_INSTALL=""
SYSTEMD_INSTALL=""
LAUNCHD_INSTALL=""
LAUNCHD_SYSTEM=""
MIGRATE_MEMPALACE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --init)               INIT_INSTALL=1 ;;
    --systemd)            SYSTEMD_INSTALL=1 ;;
    --launchd)            LAUNCHD_INSTALL=1 ;;
    --launchd-system)     LAUNCHD_INSTALL=1; LAUNCHD_SYSTEM=1 ;;
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
if [ "$(uname -s)" = "Darwin" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "  Homebrew nicht gefunden - liefert python3 >=3.10, ffmpeg (Voice) und libolm (Matrix-E2EE)."
    printf "  Homebrew jetzt installieren? [J/n] "
    read -r BREW_ANSWER </dev/tty 2>/dev/null || BREW_ANSWER=""
    case "$BREW_ANSWER" in
      n|N|nein|no)
        echo "  Uebersprungen. Ohne brew fehlen python3 >=3.10, ffmpeg (Voice) und libolm (Matrix-E2EE)."
        ;;
      *)
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
          || echo "  Homebrew-Installation fehlgeschlagen - weiter ohne."
        # Frisch installiertes brew in PATH holen (ARM: /opt/homebrew, Intel: /usr/local)
        for BREW_BIN in /opt/homebrew/bin/brew /usr/local/bin/brew; do
          if [ -x "$BREW_BIN" ]; then eval "$("$BREW_BIN" shellenv)"; break; fi
        done
        ;;
    esac
  fi
  if command -v brew >/dev/null 2>&1; then
    ( brew install python@3.13 libolm cmake ffmpeg ) 2>/dev/null || \
    ( brew install python3 libolm cmake ffmpeg ) 2>/dev/null || \
    ( brew install python3 ) 2>/dev/null || true
  fi
  # bubblewrap gibt es auf macOS nicht; die Group-Room-Sandbox nutzt dort sandbox-exec (Seatbelt).
  if [ -x /usr/bin/sandbox-exec ]; then
    echo "  Group-Room-Sandbox: sandbox-exec (Seatbelt) vorhanden - kein Extra-Paket noetig."
  else
    echo "  Warnung: /usr/bin/sandbox-exec fehlt - exec in Group-Rooms bleibt deaktiviert." >&2
  fi
elif command -v apt-get >/dev/null 2>&1; then
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

# Interpreter suchen statt blind `python3` zu nehmen: auf macOS ist /usr/bin/python3
# die System-3.9 (zu alt), waehrend brew python3.13 danebenliegt. Gleiches Muster
# hilft auf LTS-Distros, wo python3 alt ist und python3.11 parallel existiert.
PY_BIN=""
for PY_CAND in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$PY_CAND" >/dev/null 2>&1; then
    if "$PY_CAND" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
      PY_BIN="$(command -v "$PY_CAND")"
      break
    fi
  fi
done

if [ -z "$PY_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY_FOUND=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")
    echo "Fehler: MiniAssistant benötigt Python 3.10 oder neuer. Gefunden: $PY_FOUND." >&2
    if [ "$(uname -s)" = "Darwin" ]; then
      echo "  macOS liefert nur Python 3.9 mit. Installieren: brew install python@3.13" >&2
    else
      echo "  Bitte eine neuere Python-Version installieren." >&2
    fi
  else
    echo "Fehler: python3 nicht gefunden." >&2
    if [ "$(uname -s)" = "Darwin" ]; then
      echo "  Installieren: brew install python@3.13" >&2
    else
      echo "  Bitte Python 3.10 oder neuer installieren (z.B. ${SUDO}apt install python3 python3-venv python3-pip)." >&2
      echo "  Das Install-Skript versucht zuvor, System-Pakete zu installieren (mit sudo)." >&2
    fi
  fi
  exit 1
fi

PY_VERSION=$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || true
if [ -z "$PY_VERSION" ]; then
  echo "Fehler: python3-Version konnte nicht ermittelt werden." >&2
  exit 1
fi
echo "  Python $PY_VERSION gefunden ($PY_BIN)."

if ! "$PY_BIN" -c "import venv" 2>/dev/null; then
  echo "Fehler: Python-Modul 'venv' nicht verfügbar." >&2
  echo "  macOS: brew install python@3.13 (bringt venv mit)" >&2
  echo "  Debian/Ubuntu: ${SUDO}apt install python3-venv" >&2
  echo "  Fedora: ${SUDO}dnf install python3-virtualenv" >&2
  echo "  Alpine: ${SUDO}apk add python3 py3-pip" >&2
  exit 1
fi
echo "  Modul venv verfügbar."

if "$PY_BIN" -m pip --version >/dev/null 2>&1; then
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
  if ! "$PY_BIN" -m venv "$VENV_DIR"; then
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
# macOS: python-olm findet brew-libolm nicht von allein — Header/Lib-Pfade explizit setzen.
if [ "$(uname -s)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
  OLM_PREFIX="$(brew --prefix libolm 2>/dev/null || true)"
  if [ -n "$OLM_PREFIX" ] && [ -d "$OLM_PREFIX" ]; then
    CFLAGS="-I$OLM_PREFIX/include ${CFLAGS:-}"
    LDFLAGS="-L$OLM_PREFIX/lib ${LDFLAGS:-}"
    export CFLAGS LDFLAGS
  fi
fi
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
if [ "$(uname -s)" = "Darwin" ]; then
  echo "Hinweis Emoji im CLI-Chat: macOS bringt Apple Color Emoji mit — nichts zu tun."
else
  echo "Hinweis Emoji im CLI-Chat: fonts-noto-color-emoji wurde installiert."
  echo "  Für korrekte Darstellung wird ein modernes Terminal empfohlen"
  echo "  (GNOME Terminal, kitty, WezTerm, Windows Terminal)."
  echo "  Bei Bedarf: fc-cache -f  (Font-Cache aktualisieren)"
fi
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

# macOS-Autostart. --launchd = LaunchAgent (startet beim Login, kein root),
# --launchd-system = LaunchDaemon (startet beim Boot, braucht root).
if [ -n "$LAUNCHD_INSTALL" ]; then
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "Hinweis: --launchd gilt nur fuer macOS. Auf diesem System: --init (sysvinit) oder --systemd." >&2
  else
    PLIST_LABEL="com.miniassistant"
    PLIST_SRC="$SCRIPT_DIR/launchd/$PLIST_LABEL.plist"
    if [ ! -f "$PLIST_SRC" ]; then
      echo "launchd-Vorlage nicht gefunden: $PLIST_SRC" >&2
    else
      if [ -n "$LAUNCHD_SYSTEM" ]; then
        PLIST_DEST="/Library/LaunchDaemons/$PLIST_LABEL.plist"
        PLIST_DOMAIN="system"
      else
        if [ "$(id -u)" = "0" ]; then
          echo "  Warnung: --launchd als root legt den LaunchAgent in $HOME an (root, nicht dein User)." >&2
          echo "  Ohne sudo ausfuehren, oder --launchd-system fuer einen systemweiten Daemon nutzen." >&2
        fi
        PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
        PLIST_DOMAIN="gui/$(id -u)"
      fi
      # Logverzeichnis muss existieren — launchd legt es nicht an und der Job startet sonst nicht.
      mkdir -p "$HOME/.config/miniassistant/logs" 2>/dev/null || true
      mkdir -p "$(dirname "$PLIST_DEST")" 2>/dev/null || true
      if sed -e "s|%INSTALL_DIR%|$SCRIPT_DIR|g" -e "s|%HOME%|$HOME|g" "$PLIST_SRC" > "$PLIST_DEST" 2>/dev/null; then
        if [ -n "$LAUNCHD_SYSTEM" ]; then
          chown root:wheel "$PLIST_DEST" 2>/dev/null || true
          chmod 644 "$PLIST_DEST" 2>/dev/null || true
        fi
        echo "launchd-Job installiert: $PLIST_DEST"
        # Bestehenden Job vorher rauswerfen, sonst schlaegt bootstrap mit 'service already loaded' fehl.
        launchctl bootout "$PLIST_DOMAIN/$PLIST_LABEL" 2>/dev/null || true
        if launchctl bootstrap "$PLIST_DOMAIN" "$PLIST_DEST" 2>/dev/null; then
          echo "  Gestartet und beim ${LAUNCHD_SYSTEM:+Boot}${LAUNCHD_SYSTEM:-Login} aktiv."
          echo "  Status:   launchctl print $PLIST_DOMAIN/$PLIST_LABEL"
          echo "  Neustart: launchctl kickstart -k $PLIST_DOMAIN/$PLIST_LABEL"
          echo "  Stoppen:  launchctl bootout $PLIST_DOMAIN/$PLIST_LABEL"
        else
          echo "  bootstrap fehlgeschlagen. Manuell:"
          echo "  ${SUDO}launchctl bootstrap $PLIST_DOMAIN $PLIST_DEST"
        fi
      else
        echo "launchd-Job manuell installieren:" >&2
        echo "  ${SUDO}sed -e 's|%INSTALL_DIR%|$SCRIPT_DIR|g' -e \"s|%HOME%|\$HOME|g\" $PLIST_SRC > $PLIST_DEST" >&2
        echo "  ${SUDO}launchctl bootstrap $PLIST_DOMAIN $PLIST_DEST" >&2
      fi
    fi
  fi
elif [ "$(uname -s)" = "Darwin" ] && [ -z "$INIT_INSTALL" ] && [ -z "$SYSTEMD_INSTALL" ]; then
  echo ""
  echo "Autostart auf macOS: ./install.sh --launchd          (LaunchAgent, startet beim Login)"
  echo "                     ./install.sh --launchd-system   (LaunchDaemon, startet beim Boot, als root)"
fi

# --init/--systemd auf macOS: gibt es dort nicht.
if [ "$(uname -s)" = "Darwin" ] && { [ -n "$INIT_INSTALL" ] || [ -n "$SYSTEMD_INSTALL" ]; }; then
  echo "Hinweis: --init/--systemd gibt es auf macOS nicht. Autostart dort mit --launchd." >&2
fi
