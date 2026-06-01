"""bwrap-Sandbox für exec im Group-Mode.

Wenn der Bot in einem Group-Room exec aufruft, läuft der Befehl in einem
isolierten Mount/PID/IPC/UTS/User-Namespace. Sichtbar:
  /usr, /bin, /lib, /lib64   (read-only)
  /etc/{resolv.conf,ssl,ca-certificates,alternatives}  (read-only, minimal)
  /proc, /dev, /tmp          (frisch, tmpfs/devfs)
  /workspace                 (RW, gebunden an <host_workspace>/groups/<sub>/)

Unsichtbar: /root, /home, agent_dir, config_dir, andere Räume,
            Workspace außerhalb des Group-Subdirs.

Verfügbarkeits-Check passiert lazy beim ersten Aufruf und wird gecacht.
Wenn bwrap fehlt → exec im Group-Mode liefert Fehler, niemals ungesandboxed.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

_log = logging.getLogger("miniassistant.sandbox")

_BWRAP_PATH: str | None = None
_BWRAP_CHECKED: bool = False


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


def run_sandboxed_exec(command: str, group_workspace: Path, timeout: int = 60, allow_net: bool = True, docs_dir: Path | None = None) -> dict:
    """Führt command in bwrap aus. Gibt dict mit stdout/stderr/returncode zurück.
    docs_dir: wenn gesetzt, wird das Verzeichnis read-only nach /docs gemountet."""
    ok, info = bwrap_available()
    if not ok:
        return {
            "stdout": "",
            "stderr": f"exec disabled in this group room: {info}",
            "returncode": -1,
        }
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
