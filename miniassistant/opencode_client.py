"""OpenCode-Connector — MiniAssistant orchestriert, OpenCode codet.

MiniAssistant (35B) ist NUR Orchestrator. Coding-Tasks gehen an OpenCode, das
eigenen Kontext (Sessions in opencode.db), eigene Provider und eigene Auth hat.
MiniAssistant hält KEINE Model-Keys — die liegen in OpenCodes Config
(~/.config/opencode/config.json + ~/.local/share/opencode/auth.json).

Async Job-Registry (Coding dauert Minuten, blockiert nicht den Chat-Turn):

    start_job(prompt, repo, model=, agent=)  → job_id, sofort return
    job_status(job_id)                        → running|done|failed|timeout|crashed (+ diff, session_id)
    continue_job(job_id, prompt)              → Follow-up in DERSELBEN opencode-Session (Kontext bleibt)
    job_cancel(job_id) / job_list()

Model wird pro Job als `provider/model` übergeben (z. B. ollama/qwen3-coder-next-80b);
None → OpenCodes Default. Session-ID wird aus dem NDJSON-Output extrahiert, damit
der Orchestrator nachfragen kann.

Restart-fest: kein Popen-Handle über Neustarts. Jeder Job schreibt Exit-Code in
<id>.rc; Status = rc-Datei + PID-Liveness (Prozessgruppe). Parallele Jobs bekommen
je einen git-worktree, damit Edits sich nicht überschreiben.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

_log = logging.getLogger("miniassistant.opencode_client")

_OPENCODE_SEARCH = ["opencode", "~/.opencode/bin/opencode", "~/.local/bin/opencode"]
# OpenCode/Claude setzen diese Vars um nested Sessions zu verhindern → entfernen.
_NESTED_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "OPENCODE")

_DEFAULT_MAX_RUNTIME = 1200      # 20 min pro Job
_DEFAULT_MAX_CONCURRENT = 3

_TERMINAL_STATES = ("done", "failed", "timeout", "crashed", "cancelled")


# ── binary / env ────────────────────────────────────────────────────────────

def opencode_bin() -> str | None:
    for c in _OPENCODE_SEARCH:
        p = os.path.expanduser(c)
        if os.path.sep in p:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for v in _NESTED_VARS:
        env.pop(v, None)
    return env


# ── config / registry paths ─────────────────────────────────────────────────

def _oc_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("opencode") or {}


def _jobs_dir(config: dict[str, Any]) -> Path:
    d = _oc_cfg(config).get("jobs_dir") or "~/.miniassistant/opencode_jobs"
    p = Path(os.path.expanduser(d))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _job_paths(config: dict[str, Any], job_id: str) -> tuple[Path, Path, Path]:
    base = _jobs_dir(config)
    return base / f"{job_id}.json", base / f"{job_id}.log", base / f"{job_id}.rc"


def _read_entry(config: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    meta, _, _ = _job_paths(config, job_id)
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text())
    except Exception:
        return None


def _write_entry(config: dict[str, Any], entry: dict[str, Any]) -> None:
    meta, _, _ = _job_paths(config, entry["id"])
    tmp = meta.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, indent=2))
    tmp.replace(meta)


# ── process-group liveness ──────────────────────────────────────────────────

def _pgid_alive(pgid: int) -> bool:
    if not pgid or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pgid_safe_to_kill(pgid: int, job_id: str) -> bool:
    """Guard against recycled PIDs after a MiniAssistant restart: the spawned sh
    wrapper's cmdline contains the job's log path (job_id). If the group leader is
    gone but the group still exists, the kernel won't recycle that PID while it remains
    a live pgid → orphaned children, safe to kill."""
    cmd = ""
    try:
        with open(f"/proc/{pgid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        # macOS/BSD haben kein /proc — ps liefert dieselbe Info.
        try:
            r = subprocess.run(["ps", "-o", "command=", "-p", str(pgid)],
                               capture_output=True, text=True, timeout=5)
            cmd = r.stdout or ""
        except Exception:
            return True
    if not cmd.strip():
        return True
    return job_id in cmd or "opencode" in cmd


def _kill_pgid(pgid: int, job_id: str | None = None) -> None:
    if not pgid or pgid <= 1:
        return
    if job_id and not _pgid_safe_to_kill(pgid, job_id):
        _log.warning("opencode: pgid %s no longer matches job %s (recycled pid?) — not killing", pgid, job_id)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except Exception:
            pass
        time.sleep(0.5)
        if not _pgid_alive(pgid):
            return


# ── git worktree isolation ──────────────────────────────────────────────────

def _is_git_repo(path: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def _make_worktree(repo: str, job_id: str, config: dict[str, Any]) -> str | None:
    wt = _jobs_dir(config) / f"wt-{job_id}"
    try:
        r = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "--detach", str(wt), "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            _log.warning("worktree add failed: %s", r.stderr.strip())
            return None
        return str(wt)
    except Exception as e:
        _log.warning("worktree add error: %s", e)
        return None


def remove_worktree(config: dict[str, Any], entry: dict[str, Any]) -> None:
    wt, repo = entry.get("worktree"), entry.get("repo")
    if wt and repo and os.path.isdir(wt):
        try:
            subprocess.run(
                ["git", "-C", repo, "worktree", "remove", "--force", wt],
                capture_output=True, text=True, timeout=60,
            )
            _log.info("opencode: removed worktree of finished job %s", entry.get("id"))
        except Exception as e:
            _log.warning("worktree remove error: %s", e)


# ── NDJSON output parsing (session id, final text, cost) ─────────────────────

def _parse_log(config: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Extract session_id, final assistant text and cost/tokens from opencode NDJSON log."""
    _, log_p, _ = _job_paths(config, job_id)
    out: dict[str, Any] = {"session_id": None, "text": "", "cost": None, "tokens": None}
    if not log_p.exists():
        return out
    texts: list[str] = []
    try:
        for line in log_p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("sessionID"):
                out["session_id"] = ev["sessionID"]
            part = ev.get("part") or {}
            if ev.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
            if ev.get("type") == "step_finish":
                if part.get("cost") is not None:
                    out["cost"] = part["cost"]
                if part.get("tokens"):
                    out["tokens"] = part["tokens"]
    except Exception:
        pass
    out["text"] = "".join(texts).strip()
    return out


# ── job launch ──────────────────────────────────────────────────────────────

def _preset(config: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Named preset (config: opencode.presets) → {agent, model, max_runtime}. Optional."""
    if not name:
        return {}
    return (_oc_cfg(config).get("presets") or {}).get(name) or {}


def _build_argv(exe: str, prompt: str, *, model: str | None, agent: str | None,
                session: str | None, attach_url: str | None = None,
                remote_dir: str | None = None) -> list[str]:
    argv = [exe, "run", "--format", "json"]
    if attach_url:
        # Remote opencode server (opencode serve on another host). --dir is the
        # path ON THE REMOTE. Worktree/diff isolation is the remote's concern.
        argv += ["--attach", attach_url]
        if remote_dir:
            argv += ["--dir", remote_dir]
    if session:
        argv += ["-s", session]
    if agent:
        argv += ["--agent", agent]
    if model:
        argv += ["-m", model]
    argv.append(prompt)  # prompt = trailing positional
    return argv


def _spawn(config: dict[str, Any], job_id: str, argv: list[str], rundir: str) -> int:
    """Detached run: tool → log, exit-code → rc file. Returns pgid."""
    _, log_p, rc_p = _job_paths(config, job_id)
    if rc_p.exists():
        rc_p.unlink()
    # "$@" keeps every arg (incl. prompt) unquoted-safe as positionals.
    wrapper = f'"$@" > {shlex.quote(str(log_p))} 2>&1; echo $? > {shlex.quote(str(rc_p))}'
    full = ["sh", "-c", wrapper, "_"] + argv
    proc = subprocess.Popen(
        full, env=_child_env(), cwd=rundir,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group → cancel kills the whole tree
    )
    return proc.pid


def start_job(
    config: dict[str, Any],
    prompt: str,
    repo: str,
    *,
    model: str | None = None,
    agent: str | None = None,
    preset: str | None = None,
    use_worktree: bool = True,
    parent_id: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Dispatch a coding task to OpenCode as a detached job. Returns entry (status=running)."""
    exe = opencode_bin()
    if not exe:
        return {"status": "rejected", "error": "opencode binary not found (~/.opencode/bin/opencode)"}
    if not os.path.isdir(repo):
        return {"status": "rejected", "error": f"repo not found: {repo}"}

    running = [e for e in job_list(config) if e.get("status") == "running"]
    max_conc = int(_oc_cfg(config).get("max_concurrent", _DEFAULT_MAX_CONCURRENT))
    if len(running) >= max_conc:
        return {"status": "rejected", "error": f"max_concurrent={max_conc} reached ({len(running)} running)"}

    pre = _preset(config, preset)
    model = model or pre.get("model")
    agent = agent or pre.get("agent")
    attach_url = (_oc_cfg(config).get("attach_url") or "").strip() or None

    job_id = uuid.uuid4().hex[:12]
    if attach_url:
        # Remote server: repo path is remote, worktree/diff handled there.
        rundir, worktree = repo, None
        spawn_cwd = os.path.expanduser("~")
        argv = _build_argv(exe, prompt, model=model, agent=agent, session=None,
                           attach_url=attach_url, remote_dir=repo)
    else:
        rundir, worktree = repo, None
        if use_worktree and _is_git_repo(repo):
            worktree = _make_worktree(repo, job_id, config)
            if worktree:
                rundir = worktree
        spawn_cwd = rundir
        argv = _build_argv(exe, prompt, model=model, agent=agent, session=None)
    pgid = _spawn(config, job_id, argv, spawn_cwd)

    entry = {
        "id": job_id, "status": "running", "kind": "start",
        "prompt": prompt, "repo": repo, "rundir": rundir, "worktree": worktree,
        "remote": bool(attach_url), "attach_url": attach_url,
        "model": model, "agent": agent, "preset": preset,
        "session_id": None, "pgid": pgid, "started": time.time(),
        "attempt": attempt, "parent_id": parent_id,
        "max_runtime": int(pre.get("max_runtime", _oc_cfg(config).get("max_runtime", _DEFAULT_MAX_RUNTIME))),
    }
    _write_entry(config, entry)
    _log.info("opencode job %s started (model=%s agent=%s wt=%s)", job_id, model, agent, bool(worktree))
    return entry


def continue_job(config: dict[str, Any], job_id: str, prompt: str) -> dict[str, Any]:
    """Follow-up in the SAME opencode session (context preserved). Spawns a new job entry."""
    parent = _read_entry(config, job_id)
    if not parent:
        return {"status": "not_found", "id": job_id}
    session = parent.get("session_id")
    if not session:
        # session id is written when the parent finished — resolve lazily
        session = _parse_log(config, job_id).get("session_id")
    if not session:
        return {"status": "rejected", "error": "parent has no session_id yet (still running / no output)"}

    exe = opencode_bin()
    if not exe:
        return {"status": "rejected", "error": "opencode binary not found"}

    new_id = uuid.uuid4().hex[:12]
    rundir = parent.get("rundir") or parent.get("repo")
    if parent.get("remote"):
        argv = _build_argv(exe, prompt, model=parent.get("model"), agent=parent.get("agent"),
                           session=session, attach_url=parent.get("attach_url"), remote_dir=parent.get("repo"))
        spawn_cwd = os.path.expanduser("~")
    else:
        argv = _build_argv(exe, prompt, model=parent.get("model"), agent=parent.get("agent"), session=session)
        spawn_cwd = rundir
    pgid = _spawn(config, new_id, argv, spawn_cwd)

    entry = {
        "id": new_id, "status": "running", "kind": "followup",
        "prompt": prompt, "repo": parent.get("repo"), "rundir": rundir,
        "remote": bool(parent.get("remote")), "attach_url": parent.get("attach_url"),
        "worktree": parent.get("worktree"), "model": parent.get("model"),
        "agent": parent.get("agent"), "preset": parent.get("preset"),
        "session_id": session, "pgid": pgid, "started": time.time(),
        "attempt": 1, "parent_id": job_id,
        "max_runtime": int(parent.get("max_runtime", _DEFAULT_MAX_RUNTIME)),
    }
    _write_entry(config, entry)
    _log.info("opencode followup %s on session %s (parent %s)", new_id, session, job_id)
    return entry


# ── status / diff ───────────────────────────────────────────────────────────

def _git_diff_stat(entry: dict[str, Any]) -> str:
    rundir = entry.get("rundir")
    if not rundir or not _is_git_repo(rundir):
        return ""
    try:
        subprocess.run(["git", "-C", rundir, "add", "-A", "--intent-to-add"],
                       capture_output=True, text=True, timeout=20)
        r = subprocess.run(["git", "-C", rundir, "diff", "--stat"],
                           capture_output=True, text=True, timeout=20)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _refresh_entry(config: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Transition running → done/failed/timeout/crashed from rc-file + PID liveness,
    persist changes, and prune the worktree of terminal jobs whose result was read.
    Applied by job_status AND job_list, so never-polled jobs don't stay 'running'
    forever (occupying max_concurrent slots)."""
    job_id = entry.get("id", "")
    if entry.get("status") == "running":
        _, _, rc_p = _job_paths(config, job_id)
        if rc_p.exists():
            try:
                code = int(rc_p.read_text().strip() or "1")
            except Exception:
                code = 1
            entry["status"] = "done" if code == 0 else "failed"
            entry["returncode"] = code
            entry["ended"] = time.time()
        else:
            elapsed = time.time() - float(entry.get("started", 0))
            if elapsed > float(entry.get("max_runtime", _DEFAULT_MAX_RUNTIME)):
                _kill_pgid(int(entry.get("pgid", 0)), job_id)
                entry["status"] = "timeout"
                entry["ended"] = time.time()
            elif not _pgid_alive(int(entry.get("pgid", 0))):
                entry["status"] = "crashed"
                entry["ended"] = time.time()
        # extract session id / result once output exists
        parsed = _parse_log(config, job_id)
        if parsed.get("session_id"):
            entry["session_id"] = parsed["session_id"]
        if parsed.get("cost") is not None:
            entry["cost"] = parsed["cost"]
        _write_entry(config, entry)
    # Worktree cleanup: only for terminal jobs whose result was returned at least
    # once (job_status sets result_read) — never delete unseen work.
    if entry.get("status") in _TERMINAL_STATES and entry.get("result_read"):
        remove_worktree(config, entry)
    return entry


def job_status(config: dict[str, Any], job_id: str, *, with_diff: bool = True) -> dict[str, Any]:
    """Status from rc-file + process-group liveness. Enforces runtime cap, extracts session/result."""
    entry = _read_entry(config, job_id)
    if not entry:
        return {"status": "not_found", "id": job_id}
    entry = _refresh_entry(config, entry)

    out = dict(entry)
    out["elapsed"] = round(time.time() - float(entry.get("started", 0)), 1)
    if with_diff:
        out["diff_stat"] = _git_diff_stat(entry)
        if entry.get("status") in _TERMINAL_STATES:
            out["result"] = _parse_log(config, job_id).get("text", "")
            if not entry.get("result_read"):
                entry["result_read"] = True
                _write_entry(config, entry)
    return out


def job_cancel(config: dict[str, Any], job_id: str) -> dict[str, Any]:
    entry = _read_entry(config, job_id)
    if not entry:
        return {"status": "not_found", "id": job_id}
    if entry.get("status") == "running":
        _kill_pgid(int(entry.get("pgid", 0)), job_id)
        entry["status"] = "cancelled"
        entry["ended"] = time.time()
        _write_entry(config, entry)
    return {"status": entry["status"], "id": job_id}


def job_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for meta in sorted(_jobs_dir(config).glob("*.json")):
        try:
            entry = json.loads(meta.read_text())
        except Exception:
            continue
        out.append(_refresh_entry(config, entry))
    return out
