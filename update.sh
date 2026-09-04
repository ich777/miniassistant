#!/bin/bash
# Frische Kopie aus dem Upstream-Repo ziehen und neu installieren.
# Achtung: loescht INSTALL_DIR komplett. Config (~/.config/miniassistant) bleibt.
#
# Der ganze Ablauf steht in main(), weil bash Funktionen vollstaendig parst, bevor
# sie laufen — sonst wuerde das rm -rf dieses Script mitten im Lesen abschneiden.

set -e

REPO="https://github.com/ich777/miniassistant"
INSTALL_DIR="${MINIASSISTANT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
FORCE=""
if [ "$1" = "-y" ] || [ "$1" = "--force" ]; then FORCE=1; fi

LAUNCHD_TARGET=""
LAUNCHD_PLIST=""

# Gleiche Label-Konvention wie /api/restart in der Web-UI.
detect_launchd() {
    for L in com.miniassistant local.miniassistant miniassistant; do
        if [ -f "/Library/LaunchDaemons/$L.plist" ]; then
            LAUNCHD_TARGET="system/$L"
            LAUNCHD_PLIST="/Library/LaunchDaemons/$L.plist"
            return 0
        fi
    done
    for L in com.miniassistant local.miniassistant miniassistant; do
        for D in "$HOME/Library/LaunchAgents" /Library/LaunchAgents; do
            if [ -f "$D/$L.plist" ]; then
                LAUNCHD_TARGET="gui/$(id -u)/$L"
                LAUNCHD_PLIST="$D/$L.plist"
                return 0
            fi
        done
    done
    return 1
}

svc_stop() {
    if [ "$(uname -s)" = "Darwin" ]; then
        if detect_launchd; then
            launchctl bootout "$LAUNCHD_TARGET" 2>/dev/null || true
        else
            echo "Kein launchd-Job gefunden - ueberspringe Stop." >&2
        fi
    elif command -v systemctl >/dev/null 2>&1 && systemctl cat miniassistant >/dev/null 2>&1; then
        systemctl stop miniassistant || true
    elif [ -x /etc/init.d/miniassistant ]; then
        /etc/init.d/miniassistant stop || true
    else
        echo "Kein Init-Eintrag gefunden - ueberspringe Stop." >&2
    fi
}

svc_start() {
    if [ "$(uname -s)" = "Darwin" ]; then
        if [ -n "$LAUNCHD_TARGET" ]; then
            # bootout hat den Job entladen -> bootstrap statt kickstart.
            launchctl bootstrap "${LAUNCHD_TARGET%/*}" "$LAUNCHD_PLIST" || true
        fi
    elif command -v systemctl >/dev/null 2>&1 && systemctl cat miniassistant >/dev/null 2>&1; then
        systemctl start miniassistant || true
    elif [ -x /etc/init.d/miniassistant ]; then
        /etc/init.d/miniassistant start || true
    fi
}

# Der Reclone loescht alles Lokale. Nicht gepushte Aenderungen waeren weg -
# deshalb erst fragen, statt sie stillschweigend zu ueberschreiben.
check_local_changes() {
    if [ -n "$FORCE" ]; then return 0; fi
    if ! command -v git >/dev/null 2>&1; then return 0; fi
    if [ ! -d "$INSTALL_DIR/.git" ]; then return 0; fi
    DIRTY="$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$DIRTY" = "0" ]; then return 0; fi
    echo "WARNUNG: $DIRTY lokal geaenderte/unversionierte Dateien in $INSTALL_DIR." >&2
    echo "Das Update loescht das Verzeichnis und klont $REPO neu - diese Aenderungen sind dann weg." >&2
    if [ ! -t 0 ]; then
        echo "Kein Terminal fuer die Rueckfrage. Abbruch. Mit -y erzwingen." >&2
        exit 1
    fi
    printf "Trotzdem fortfahren und lokale Aenderungen verwerfen? [j/N] "
    read -r ANSWER
    case "$ANSWER" in
        j|J|y|Y) ;;
        *) echo "Abgebrochen."; exit 1 ;;
    esac
}

main() {
    PARENT_DIR="$(dirname "$INSTALL_DIR")"
    NAME="$(basename "$INSTALL_DIR")"

    echo "Update: $INSTALL_DIR aus $REPO"
    check_local_changes
    svc_stop

    cd "$PARENT_DIR"
    rm -rf "$INSTALL_DIR"
    git clone --depth 1 "$REPO" "$NAME"
    cd "$INSTALL_DIR"
    if ! bash install.sh; then
        echo "install.sh fehlgeschlagen - Dienst bleibt gestoppt." >&2
        exit 1
    fi

    svc_start
    echo "Update fertig."
}

main "$@"
