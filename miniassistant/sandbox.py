"""Sandbox für exec im Group-Mode. Backend: bwrap (Linux) oder sandbox-exec/Seatbelt (macOS).

Wenn der Bot in einem Group-Room exec aufruft, läuft der Befehl in einem
isolierten Mount/PID/IPC/UTS/User-Namespace. Sichtbar:
  /usr, /bin, /lib, /lib64   (read-only)
  /etc/{resolv.conf,ssl,ca-certificates,alternatives}  (read-only, minimal)
  /proc, /dev, /tmp          (frisch, tmpfs/devfs)
  /workspace                 (RW, gebunden an <host_workspace>/groups/<sub>/)

Unsichtbar: /root, /home, agent_dir, config_dir, andere Räume,
            Workspace außerhalb des Group-Subdirs.

Verfügbarkeits-Check passiert lazy beim ersten Aufruf und wird gecacht.
Wenn das Backend fehlt → exec im Group-Mode liefert Fehler, niemals ungesandboxed.

macOS-Backend (Seatbelt, `sandbox-exec`) — Unterschiede zu bwrap, bewusst in Kauf genommen:
  * Seatbelt kann nicht mounten. Der Group-Workspace liegt weiter unter seinem echten
    Pfad; `/workspace` wird an der Sandbox-Grenze in beide Richtungen umgeschrieben
    (Command rein, stdout/stderr raus), damit Prompt und Tools unverändert bleiben.
  * `deny default` verweigert den Zugriff auf alles Übrige, versteckt es aber nicht:
    `file-read-metadata` ist global erlaubt (dyld/stat brauchen es), d. h. die Existenz
    eines Pfades ist feststellbar, sein Inhalt nicht.
  * `mach-lookup` ist breit erlaubt — ohne opendirectoryd/notifyd scheitern schon `ls -l`
    und `id`.
  * `ulimit -v` gibt es auf macOS nicht (nur -t und -f werden gesetzt).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("miniassistant.sandbox")

_BWRAP_PATH: str | None = None
_BWRAP_CHECKED: bool = False
_SEATBELT_PATH: str | None = None
_SEATBELT_CHECKED: bool = False

# Sandbox-seitiger Workspace-Pfad. Unter bwrap ein echter Mountpoint, unter Seatbelt
# nur eine Fiktion, die an der Grenze auf den echten Pfad gemappt wird.
SANDBOX_WS = "/workspace"
SANDBOX_DOCS = "/docs"


def _backend() -> str:
    return "seatbelt" if sys.platform == "darwin" else "bwrap"


def bwrap_available() -> tuple[bool, str]:
    """Prüft beim ersten Aufruf: ist bwrap installiert und funktioniert userns?
    Cached das Ergebnis. Gibt (verfügbar, pfad_oder_fehlertext) zurück."""
    global _BWRAP_PATH, _BWRAP_CHECKED
    if _BWRAP_CHECKED:
        return (_BWRAP_PATH is not None, _BWRAP_PATH or "bwrap not available")
    _BWRAP_CHECKED = True
    path = shutil.which("bwrap")
    if not path:
        _log.warning("bwrap nicht im PATH — exec im Group-Mode wird deaktiviert. Install: apt install bubblewrap")
        _BWRAP_PATH = None
        return False, "bwrap not installed (apt install bubblewrap)"
    # Smoke-Test: minimaler unprivileged userns-Aufruf.
    # /usr binden + /bin /lib /lib64 /sbin als Symlink (Debian/Devuan-usrmerge) ODER direkt binden.
    # Dynamischer Linker liegt unter /lib64/ld-linux-... → ohne /lib64-Symlink scheitert execvp.
    try:
        argv = [path, "--unshare-user", "--unshare-pid", "--ro-bind", "/usr", "/usr"]
        for top, tgt in (("/bin", "usr/bin"), ("/sbin", "usr/sbin"), ("/lib", "usr/lib"), ("/lib64", "usr/lib64")):
            p = Path(top)
            if p.is_symlink():
                argv += ["--symlink", tgt, top]
            elif p.exists():
                argv += ["--ro-bind", top, top]
        argv += ["--proc", "/proc", "--dev", "/dev", "/usr/bin/true"]
        r = subprocess.run(argv, capture_output=True, timeout=5)
        if r.returncode != 0:
            _log.warning("bwrap smoke-test failed (rc=%d, stderr=%s) — Group-exec deaktiviert", r.returncode, (r.stderr or b'').decode(errors='replace')[:200])
            _BWRAP_PATH = None
            return False, f"bwrap smoke-test failed: {(r.stderr or b'').decode(errors='replace')[:200]}"
    except Exception as e:
        _log.warning("bwrap smoke-test exception: %s — Group-exec deaktiviert", e)
        _BWRAP_PATH = None
        return False, f"bwrap smoke-test exception: {e}"
    _BWRAP_PATH = path
    _log.info("bwrap verfügbar: %s — Group-exec aktiviert", path)
    return True, path


def build_bwrap_cmd(
    command: str,
    group_workspace: Path,
    allow_net: bool = True,
    cpu_seconds: int = 60,
    max_mem_kb: int = 1_048_576,
    max_file_kb: int = 102_400,
    docs_dir: Path | None = None,
) -> list[str]:
    """Erzeugt die bwrap-argv-Liste. ulimit wird vor command via bash gesetzt.
    docs_dir: optional, wird read-only nach /docs gemountet (für room_settings.docs_in_sandbox=true)."""
    ulimit_prefix = f"ulimit -t {cpu_seconds} -v {max_mem_kb} -f {max_file_kb} 2>/dev/null; "
    args: list[str] = [
        _BWRAP_PATH or "bwrap",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--new-session",
        "--hostname", "groupbox",
    ]
    if not allow_net:
        args += ["--unshare-net"]
    # Read-only Systembinaries. Auf usrmerge-Systemen (Debian/Devuan) sind /bin, /lib, /lib64
    # Symlinks nach /usr/* — diese als --symlink reinmounten statt --ro-bind (das würde scheitern).
    args += ["--ro-bind", "/usr", "/usr"]
    for top, target in (("/bin", "usr/bin"), ("/sbin", "usr/sbin"), ("/lib", "usr/lib"), ("/lib64", "usr/lib64")):
        p = Path(top)
        if p.is_symlink():
            args += ["--symlink", target, top]
        elif p.exists():
            args += ["--ro-bind", top, top]
    # Minimal /etc — nur was DNS/SSL braucht
    for ro in ("/etc/resolv.conf", "/etc/ssl", "/etc/ca-certificates", "/etc/alternatives", "/etc/nsswitch.conf", "/etc/hosts"):
        if Path(ro).exists():
            args += ["--ro-bind-try", ro, ro]
    # Virtuelle FS
    args += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
    ]
    # Group-Workspace RW als /workspace mounten
    args += ["--bind", str(group_workspace), "/workspace"]
    args += ["--chdir", "/workspace"]
    # Optional: docs read-only als /docs mounten (per-room toggle docs_in_sandbox)
    if docs_dir and docs_dir.exists():
        args += ["--ro-bind", str(docs_dir), "/docs"]
    # Env minimal
    args += [
        "--clearenv",
        "--setenv", "HOME", "/workspace",
        "--setenv", "USER", "groupbot",
        "--setenv", "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "TERM", "dumb",
    ]
    args += ["--", "/bin/bash", "-c", ulimit_prefix + command]
    return args


# ── Seatbelt-Backend (macOS) ────────────────────────────────────────────────

_SEATBELT_SMOKE_PROFILE = '(version 1)(allow default)'


def seatbelt_available() -> tuple[bool, str]:
    """Prüft beim ersten Aufruf, ob sandbox-exec existiert und startet. Cached."""
    global _SEATBELT_PATH, _SEATBELT_CHECKED
    if _SEATBELT_CHECKED:
        return (_SEATBELT_PATH is not None, _SEATBELT_PATH or "sandbox-exec not available")
    _SEATBELT_CHECKED = True
    path = shutil.which("sandbox-exec") or ("/usr/bin/sandbox-exec" if Path("/usr/bin/sandbox-exec").exists() else None)
    if not path:
        _log.warning("sandbox-exec nicht gefunden — exec im Group-Mode wird deaktiviert")
        return False, "sandbox-exec not found (macOS base system)"
    try:
        r = subprocess.run([path, "-p", _SEATBELT_SMOKE_PROFILE, "/usr/bin/true"], capture_output=True, timeout=5)
        if r.returncode != 0:
            err = (r.stderr or b"").decode(errors="replace")[:200]
            _log.warning("sandbox-exec smoke-test failed (rc=%d, stderr=%s) — Group-exec deaktiviert", r.returncode, err)
            return False, f"sandbox-exec smoke-test failed: {err}"
    except Exception as e:
        _log.warning("sandbox-exec smoke-test exception: %s — Group-exec deaktiviert", e)
        return False, f"sandbox-exec smoke-test exception: {e}"
    _SEATBELT_PATH = path
    _log.info("sandbox-exec verfügbar: %s — Group-exec aktiviert", path)
    return True, path


def _sbpl_str(value: str) -> str:
    """Escaped einen Pfad für ein SBPL-Stringliteral."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_seatbelt_profile(
    group_workspace: Path,
    allow_net: bool = True,
    docs_dir: Path | None = None,
) -> str:
    """Baut das Seatbelt-Profil (SBPL). deny-default; nur explizit Erlaubtes geht durch."""
    ws = str(group_workspace.resolve())
    # macOS spiegelt /tmp, /var und /etc via /private — beide Schreibweisen erlauben,
    # weil je nach Tool die eine oder andere Form beim Kernel ankommt.
    read_paths = [
        "/usr", "/bin", "/sbin", "/System",
        "/opt/homebrew", "/usr/local",
        "/private/var/db/timezone", "/private/var/select",
        "/private/etc/ssl", "/private/etc/openssl", "/etc/ssl",
    ]
    read_literals = [
        "/private/etc/resolv.conf", "/etc/resolv.conf",
        "/private/etc/hosts", "/etc/hosts",
        "/private/etc/services", "/private/etc/protocols",
        "/private/etc/localtime", "/etc/localtime",
        "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom",
        "/dev/dtracehelper", "/dev/tty", "/dev/stdin", "/dev/stdout", "/dev/stderr",
    ]
    write_paths = [ws, "/private/tmp", "/private/var/tmp", "/tmp"]
    write_literals = ["/dev/null", "/dev/tty", "/dev/stdout", "/dev/stderr"]

    lines = [
        "(version 1)",
        "(deny default)",
        # Kein (debug deny) — Denials sollen nicht ins Room-Output lecken.
        "(allow process-fork)",
        # Exec ist unkritisch: ausgefuehrt werden kann nur, was auch lesbar ist.
        "(allow process-exec*)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow ipc-posix-shm)",
        # Ohne opendirectoryd/notifyd scheitern schon `ls -l` und `id`.
        "(allow mach-lookup)",
        # dyld und jedes stat() brauchen das; verraet Existenz, nicht Inhalt.
        "(allow file-read-metadata)",
    ]
    lines.append("(allow file-read*")
    for sub in read_paths:
        lines.append(f"    (subpath {_sbpl_str(sub)})")
    for lit in read_literals:
        lines.append(f"    (literal {_sbpl_str(lit)})")
    lines.append(f"    (subpath {_sbpl_str(ws)})")
    if docs_dir and docs_dir.exists():
        lines.append(f"    (subpath {_sbpl_str(str(docs_dir.resolve()))})")
    lines.append(")")
    lines.append("(allow file-write*")
    for sub in write_paths:
        lines.append(f"    (subpath {_sbpl_str(sub)})")
    for lit in write_literals:
        lines.append(f"    (literal {_sbpl_str(lit)})")
    lines.append(")")
    if allow_net:
        lines.append("(allow network*)")
    return "\n".join(lines)


def build_seatbelt_cmd(
    command: str,
    group_workspace: Path,
    allow_net: bool = True,
    cpu_seconds: int = 60,
    max_file_kb: int = 102_400,
    docs_dir: Path | None = None,
) -> list[str]:
    """Erzeugt das sandbox-exec-argv. `ulimit -v` fehlt — macOS kennt es nicht."""
    profile = build_seatbelt_profile(group_workspace, allow_net=allow_net, docs_dir=docs_dir)
    ulimit_prefix = f"ulimit -t {cpu_seconds} -f {max_file_kb} 2>/dev/null; "
    ws = str(group_workspace.resolve())
    inner = _to_host_paths(command, ws, docs_dir)
    # env -i statt bwraps --clearenv: Seatbelt filtert die Umgebung nicht.
    return [
        _SEATBELT_PATH or "/usr/bin/sandbox-exec", "-p", profile,
        "/usr/bin/env", "-i",
        f"HOME={ws}",
        "USER=groupbot",
        "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "TERM=dumb",
        "/bin/bash", "-c", ulimit_prefix + inner,
    ]


def _to_host_paths(command: str, ws: str, docs_dir: Path | None) -> str:
    """/workspace/... → echter Pfad. Nur als Pfad-Token (Zeilenanfang, Whitespace,
    Quote, = oder :), damit Fliesstext im Command nicht getroffen wird."""
    out = re.sub(r'(?<![\w/])' + re.escape(SANDBOX_WS) + r'(?=/|\b)', ws, command)
    if docs_dir and docs_dir.exists():
        out = re.sub(r'(?<![\w/])' + re.escape(SANDBOX_DOCS) + r'(?=/|\b)',
                     str(docs_dir.resolve()), out)
    return out


def _to_sandbox_paths(text: str, ws: str, docs_dir: Path | None) -> str:
    """Rueckrichtung fuer stdout/stderr — sonst leckt der Host-Pfad (und damit das
    Home des Owners) in den Group-Room."""
    out = text.replace(ws, SANDBOX_WS)
    if docs_dir and docs_dir.exists():
        out = out.replace(str(docs_dir.resolve()), SANDBOX_DOCS)
    return out


def sandbox_available() -> tuple[bool, str]:
    """Backend-unabhaengiger Verfuegbarkeits-Check."""
    return seatbelt_available() if _backend() == "seatbelt" else bwrap_available()


def _run_seatbelt(command: str, group_workspace: Path, timeout: int, allow_net: bool, docs_dir: Path | None) -> dict:
    ws = str(group_workspace.resolve())
    argv = build_seatbelt_cmd(command, group_workspace, allow_net=allow_net,
                              cpu_seconds=max(5, timeout), docs_dir=docs_dir)
    # Seatbelt kennt kein --die-with-parent: eigene Prozessgruppe + killpg beim Timeout,
    # sonst ueberleben Kindprozesse den Abbruch.
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=ws, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
        proc.wait(timeout=5)
        return {"stdout": "", "stderr": f"Command timed out after {timeout}s (seatbelt)", "returncode": -1}
    return {
        "stdout": _to_sandbox_paths(out or "", ws, docs_dir),
        "stderr": _to_sandbox_paths(err or "", ws, docs_dir),
        "returncode": proc.returncode,
    }


def run_sandboxed_exec(command: str, group_workspace: Path, timeout: int = 60, allow_net: bool = True, docs_dir: Path | None = None) -> dict:
    """Führt command gesandboxed aus (bwrap auf Linux, sandbox-exec auf macOS).
    Gibt dict mit stdout/stderr/returncode zurück.
    docs_dir: wenn gesetzt, read-only als /docs sichtbar."""
    ok, info = sandbox_available()
    if not ok:
        return {
            "stdout": "",
            "stderr": f"exec disabled in this group room: {info}",
            "returncode": -1,
        }
    if _backend() == "seatbelt":
        return _run_seatbelt(command, group_workspace, timeout, allow_net, docs_dir)
    argv = build_bwrap_cmd(command, group_workspace, allow_net=allow_net, cpu_seconds=max(5, timeout), docs_dir=docs_dir)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {
            "stdout": r.stdout or "",
            "stderr": r.stderr or "",
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Command timed out after {timeout}s (bwrap)", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": f"bwrap exec failed: {e}", "returncode": -1}
