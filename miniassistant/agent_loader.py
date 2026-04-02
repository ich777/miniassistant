"""
Lädt Agent-Dateien (AGENTS, SOUL, IDENTITY, TOOLS, USER) und baut den System-Prompt
inkl. Runtime-Info (root/sudo) und Tool-Beschreibungen (exec, web_search).
AGENTS.md = schlanker Top-Level-Vertrag (Prioritäten, Grenzen); optional.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from miniassistant.config import load_config, config_path
from miniassistant.memory import get_memory_for_prompt
from miniassistant.basic_rules.loader import ensure_and_load as _load_basic_rules, get_rule as _get_rule
from miniassistant.docs.loader import ensure_docs as _ensure_docs, docs_dir_path as _docs_dir_path_from_config


def _is_root() -> bool:
    """True wenn der Prozess als root (euid 0) läuft."""
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False  # Windows o.ä.


def _detect_system() -> dict[str, str]:
    """
    Erkennt OS, Distribution, Paketmanager und Init-System,
    damit die LLM die richtigen Befehle nutzt (apt vs dnf, systemctl vs service, …).
    """
    import platform
    import subprocess
    out: dict[str, str] = {
        "os": platform.system(),
        "machine": platform.machine(),
        "release": platform.release() or "",
        "distro": "",
        "package_manager": "",
        "init_system": "",
    }
    # Init: systemd vs sysvinit
    if Path("/run/systemd/system").exists():
        out["init_system"] = "systemd"
    else:
        out["init_system"] = "sysvinit"
    # Distro + Paketmanager (Linux)
    if out["os"] == "Linux":
        for etc in ("/etc/os-release", "/usr/lib/os-release"):
            p = Path(etc)
            if p.exists():
                try:
                    for line in p.read_text(encoding="utf-8").splitlines():
                        if line.startswith("ID="):
                            out["distro"] = line.split("=", 1)[1].strip().strip('"')
                            break
                        if line.startswith("ID_LIKE="):
                            if not out["distro"]:
                                out["distro"] = line.split("=", 1)[1].strip().strip('"').split()[0]
                            break
                except Exception:
                    pass
                break
        # Paketmanager anhand Distro / vorhandener Befehle
        if out["distro"] in ("debian", "ubuntu", "raspbian"):
            out["package_manager"] = "apt"
        elif out["distro"] in ("fedora", "rhel", "centos", "rocky", "alma"):
            out["package_manager"] = "dnf"
        elif out["distro"] in ("arch", "manjaro"):
            out["package_manager"] = "pacman"
        elif out["distro"] in ("alpine",):
            out["package_manager"] = "apk"
        elif out["distro"] in ("opensuse-leap", "opensuse-tumbleweed", "suse"):
            out["package_manager"] = "zypper"
        else:
            for cmd in ("apt", "dnf", "yum", "pacman", "apk", "zypper"):
                try:
                    subprocess.run([cmd, "--version"], capture_output=True, timeout=2)
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
                out["package_manager"] = cmd
                break
    elif out["os"] == "Darwin":
        out["distro"] = "macos"
        out["package_manager"] = "brew"
    return out


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text.strip()
    # Kürzen auf max_chars, möglichst am Satzende
    cut = text[: max_chars]
    last_period = cut.rfind(".")
    if last_period > max_chars // 2:
        return cut[: last_period + 1].strip()
    return cut.strip() + "…"


def load_agent_files(agent_dir: str, max_chars_per_file: int = 500) -> dict[str, str]:
    """Liest AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, USER.md und kürzt auf max_chars."""
    result: dict[str, str] = {}
    base = Path(agent_dir)
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md"):
        path = base / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            result[name] = _trim(text, max_chars_per_file)
        elif name == "AGENTS.md":
            result[name] = "(AGENTS.md not found – optional: priorities, limits, quality bar in a few lines.)"
        else:
            result[name] = f"(File {name} not found)"
    return result


def _system_and_runtime_section(is_root: bool) -> str:
    """Host system + runtime info (OS, distro, package manager, init, root status) for the LLM."""
    import datetime as _dt
    now = _dt.datetime.now()
    weekdays_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    date_str = f"{weekdays_de[now.weekday()]}, {now.strftime('%d.%m.%Y')} – {now.strftime('%H:%M')} Uhr"
    s = _detect_system()
    parts = [f"**{s['os']}** (Kernel: {s['release']}, {s['machine']})"]
    if s["distro"]:
        parts.append(f"Distro: **{s['distro']}**")
    if s["package_manager"]:
        parts.append(f"Pkg: **{s['package_manager']}**")
    if s["init_system"]:
        if s["init_system"] == "systemd":
            parts.append("Init: **systemd** (systemctl)")
        else:
            parts.append("Init: **sysvinit** (service NAME start/stop)")
    if is_root:
        parts.append("Running as **root** – no sudo needed")
    else:
        parts.append("Not root – use **sudo** when needed")
    import sys as _sys
    parts.append(f"Python: **{_sys.executable}**")
    return f"## System\n**Heute:** {date_str}\n\n" + ". ".join(parts) + ".\n\n"


def _safety_section() -> str:
    """Liest basic_rules/safety.md und injiziert den Inhalt."""
    rule = _get_rule("safety.md")
    return (rule + "\n\n") if rule else ""


def _units_section_from_prefs(config: dict[str, Any]) -> str:
    """Units-Regel: LLM leitet Einheiten aus dem Land des Users (USER.md) ab."""
    return (
        "## Units and Currency\n"
        "Use the measurement system, temperature unit, and currency that are standard "
        "in the user's country (see USER section above). Show only one unit system — "
        "never show both or convert between them.\n"
    )


def _prefs_section(config: dict[str, Any]) -> str:
    """
    Liest Merkdateien/Präferenzen aus agent_dir: Unterordner prefs/ und *.md/*.txt im Agent-Verzeichnis.
    Diese werden beim Start in den Kontext geladen. Limit: prefs_max_chars (default 2500) damit z.B. wetter.md vollständig reinkommt.
    """
    agent_dir = (config.get("agent_dir") or "").strip()
    if not agent_dir:
        return ""
    base = Path(agent_dir).expanduser().resolve()
    if not base.exists():
        return ""
    max_chars = config.get("prefs_max_chars") or 2500
    max_per_file = config.get("prefs_max_chars_per_file") or 1000
    parts: list[str] = []
    total = 0
    # prefs/ Unterordner
    prefs_dir = base / "prefs"
    if prefs_dir.is_dir():
        for p in sorted(prefs_dir.iterdir()):
            if p.is_file() and total < max_chars:
                try:
                    t = p.read_text(encoding="utf-8", errors="replace").strip()
                    if t:
                        chunk = t[: min(max_per_file, max_chars - total)]
                        parts.append(f"### {p.name}\n{chunk}")
                        total += len(chunk)
                except Exception:
                    pass
    # *.md und *.txt direkt im agent_dir (Kompatibilitaet mit alten .txt-Dateien)
    for p in sorted(base.iterdir()):
        if p.is_file() and p.suffix.lower() in (".md", ".txt") and p.name not in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md") and total < max_chars:
            try:
                t = p.read_text(encoding="utf-8", errors="replace").strip()
                if t:
                    chunk = t[: min(max_per_file, max_chars - total)]
                    parts.append(f"### {p.name}\n{chunk}")
                    total += len(chunk)
            except Exception:
                pass
    if not parts:
        return ""
    return "## Stored preferences / notes\nFollow these when relevant.\n\n" + "\n\n".join(parts) + "\n\n"


def _persistence_section(config: dict[str, Any]) -> str:
    """Hinweis auf Verzeichnisse zum Merken/Schreiben."""
    agent_dir = (config.get("agent_dir") or "").strip()
    workspace = (config.get("workspace") or "").strip()
    if not agent_dir and not workspace:
        return ""
    agent_dir_resolved = str(Path(agent_dir).expanduser().resolve()) if agent_dir else ""
    workspace_resolved = str(Path(workspace).expanduser().resolve()) if workspace else ""
    prefs_path = f"{agent_dir_resolved}/prefs" if agent_dir_resolved else ""
    lines = ["## Persistence – how to store things"]
    if prefs_path:
        lines.append(
            f"There are exactly **two storage mechanisms** — choose the right one:\n\n"
            f"| What | Where | How | Format |\n"
            f"|------|-------|-----|--------|\n"
            f"| User preferences, notes, reminders | `{prefs_path}/` | `exec` (write file) | `.md` (Markdown) |\n"
            f"| System config (providers, models, server, scheduler, ...) | `config.yaml` | `save_config` tool | YAML (merged) |\n\n"
            f"**Top-level config keys — each is independent:**\n"
            f"- `providers` / `server` / `scheduler` — AI and server settings\n"
            f"- `chat_clients.matrix` / `chat_clients.discord` — chat bot connections (Matrix, Discord) — **NOT email**\n"
            f"- `email` — email accounts (IMAP/SMTP) — **completely separate from chat_clients**\n\n"
            f"**Rules:**\n"
            f"- **Trigger phrases:** When the user says 'merk dir', 'speicher dir', 'remember', 'save', 'notier dir' → "
            f"write a `.md` file to `{prefs_path}/` via `exec`. Filename = topic (e.g. `wetter.md`, `backup.md`).\n"
            f"- `save_config` is **ONLY** for system config. **NEVER** use it for user preferences.\n"
            f"- Prefs are plain Markdown. They are loaded into your system prompt at session start (see \"Stored preferences\" above) — every line costs context tokens.\n"
            f"- **Keep prefs short:** Only key facts, key-value style (e.g. `Ort: Lunz am See`). No long explanations, no full instructions. Max 5-10 lines per file.\n"
            f"- **Before saving:** Look at your \"Stored preferences\" section above — if a file for that topic already exists, update it instead of creating a duplicate.\n"
            f"- **NEVER store credentials, tokens, passwords, or API keys in prefs files** — they get loaded into the system prompt every session. "
            f"For config credentials (e.g. Matrix token), use `save_config`. For other secrets, warn the user that prefs/ is not secure."
        )
    if prefs_path:
        lines.append(
            f"**Project notes:** When the user says 'mach dir Notizen' or asks you to study a project/repo, "
            f"write a concise summary to `{prefs_path}/notes-TOPIC.md` (key facts, architecture, tech stack — no full code). "
            f"When asked 'schau dir die Notizen an', read the relevant notes file and use it as context."
        )
    trash_dir = (config.get("trash_dir") or "").strip()
    _trash_path = str(Path(trash_dir).expanduser().resolve()) if trash_dir else (f"{workspace_resolved}/.trash" if workspace_resolved else ".trash")
    lines.append(f"Before deleting any file, move it to the app trash: `mv FILE {_trash_path}/` (auto-created, separate from workspace). If user asks to empty the trash: `rm -rf {_trash_path}/*`.")
    if workspace_resolved:
        lines.append(
            f"**Working directory for all file operations: `{workspace_resolved}`**\n"
            f"Use this for ALL clones, downloads, and generated files. "
            f"Before downloading or cloning anything, check here first: `ls {workspace_resolved}/`"
        )
    lines.append("")
    return "\n".join(lines)


def _planning_section(config: dict[str, Any]) -> str:
    """Kompakte Anweisung für Task-Planning (Plan-Dateien im Workspace)."""
    workspace = (config.get("workspace") or "").strip()
    if not workspace:
        return ""
    ws = str(Path(workspace).expanduser().resolve())
    # Pfad zu PLANNING.md (aus agent_dir/docs/ oder Package-Fallback)
    planning_md = None
    docs = _docs_dir_path_from_config(config)
    if docs:
        p = docs / "PLANNING.md"
        if p.exists():
            planning_md = str(p)
    lines = [
        "## Task planning",
        f"For complex tasks (>3 steps or multiple components): create `{ws}/TOPIC-plan.md` with a Markdown checklist (`- [ ]`/`- [x]`).",
        f"Read and update the plan via `exec` (cat/write) between steps. Mark steps done as you go.",
        "With subagents: include relevant plan context in invoke_model message.",
        "Keep the plan file as reference — only delete when the user explicitly asks.",
        f"When the user says **'schau dir den Plan an'**, **'mach weiter'**, or **references a plan by name**: "
        f"read the plan file from `{ws}/`, summarize the status, and continue with the next open step.",
    ]
    if planning_md:
        lines.append(f"For format details and examples: `cat \"{planning_md}\"`")
    lines.append("")
    return "\n".join(lines)


def _docs_dir_path(config: dict[str, Any]) -> Path | None:
    """Path to docs/ directory. Uses agent_dir/docs/ (synced by docs loader) or package fallback.
    Caches result on config dict to avoid redundant path lookups within a single build."""
    cached = config.get("_docs_dir_cache")
    if cached is not None:
        return cached if cached else None
    result = _docs_dir_path_from_config(config)
    config["_docs_dir_cache"] = result or ""
    return result


def _docs_reference_section(config: dict[str, Any]) -> str:
    """Docs-Verzeichnis mit Einzeldateien. Agent liest nur die Datei die er braucht."""
    docs = _docs_dir_path(config)
    if not docs:
        return ""
    d = str(docs)
    return (
        "## Docs reference (read only when needed)\n"
        f"Documentation directory: `{d}/`\n"
        f"Each topic is a separate file. **Read only the file you need** (`cat \"{d}/FILE\"`), never all of them.\n"
        "When a topic is relevant, **read the file yourself and follow the instructions** — do not tell the user to read it.\n\n"
        "**Read-first rules:** Before configuring Matrix/Discord/Voice (installation, server setup, save_config), read the matching doc file first. Do NOT read docs just to use a tool — `send_audio`, `send_image`, `web_search` etc. can be called directly.\n\n"
        "| File | Topic |\n"
        "|------|-------|\n"
        f"| `MATRIX.md` | Configuring YOUR bot connection (chat_clients.matrix) |\n"
        f"| `DISCORD.md` | Configuring YOUR bot connection (chat_clients.discord) |\n"
        f"| `CONFIG_REFERENCE.md` | Config structure, save_config rules |\n"
        "| `PROVIDERS.md` | Multiple Ollama instances, Ollama Online, Anthropic |\n"
        "| `CONTEXT_SIZE.md` | num_ctx, per-model context |\n"
        "| `SCHEDULES.md` | Schedule tool, cron jobs, workspace cleanup protection |\n"
        "| `GITHUB.md` | GitHub REST API, token usage, repo tracking |\n"
        "| `SEARCH_ENGINES.md` | SearXNG setup |\n"
        "| `SUBAGENTS.md` | Worker models, invoke_model |\n"
        "| `VISION.md` | Image analysis |\n"
        "| `IMAGE_GENERATION.md` | Image generation |\n"
        "| `DEBATE.md` | Multi-round AI debate |\n"
        "| `AVATARS.md` | Bot profile picture |\n"
        "| `EMAIL.md` | IMAP/SMTP, multi-account, tracking, auto-reply |\n"
        "| `VOICE.md` | Wyoming STT/TTS, voice setup, send_audio text formatting |\n"
        "| `CHAT_HISTORY.md` | How to find past conversations (date → memory file → summarize) |\n"
        "| `PROMPT_ENGINEERING.md` | Writing prompts/rules/instructions for LLMs |\n"
        "| `WEB_FETCHING.md` | read_url JS rendering (Playwright) and proxies |\n"
        "| `API_REFERENCE.md` | REST API endpoints, OpenAI-compatible API |\n"
        "| `DIRECTIONS.md` | Directions format, multi-task layout, curl rules, when to create |\n\n"
        "## Directions (reusable task instructions)\n"
        f"Directory: `{config.get('agent_dir', 'agent_dir')}/directions/`\n"
        "Directions are self-contained Markdown files with exact instructions for recurring tasks (API calls, scheduled jobs, etc.). "
        "A file can cover ONE or MULTIPLE tasks (sections separated by `---`). "
        f"Full format guide: read `{d}/DIRECTIONS.md`.\n\n"
        "**Read a directions file when:**\n"
        "- The prompt explicitly says 'lies directions/...', 'folge der Direktive', 'nutze die ... Direktive'\n"
        "- A scheduled task references a directions file — read it and execute the specified task\n"
        "- The user asks you to create or update a directions file → "
        f"read `{d}/DIRECTIONS.md` for format first\n"
        "Do NOT speculatively check directions/ on every request.\n"
    )


def _memory_section(project_dir: str | None, config: dict[str, Any] | None = None) -> str:
    """Kurzer Memory-Auszug (letzte Tage, max Zeilen) für den System-Prompt. Uses memory config values."""
    if config is None:
        config = load_config(project_dir)
    mem_cfg = config.get("memory") or {}
    days = int(mem_cfg.get("days", 2) or 2)
    max_tokens = int(mem_cfg.get("max_tokens", 4000) or 4000)
    max_chars_per_line = int(mem_cfg.get("max_chars_per_line", 300) or 300)
    mem = get_memory_for_prompt(project_dir, max_lines=400, days=days, max_chars_per_line=max_chars_per_line, max_tokens=max_tokens)
    header = f"## Memory (letzte {days} Tage)\n"
    footer = "\n*(Ältere Gespräche: lies `CHAT_HISTORY.md` aus dem Docs-Verzeichnis — dort steht, wie du nach Datum suchst.)*\n\n"
    if not mem:
        return header + "*(Keine Einträge.)*" + footer
    return header + mem + footer


def _language_from_identity_md(identity_md: str) -> str:
    """Liest Antwortsprache aus IDENTITY.md (z. B. 'Response language: Deutsch' oder 'language: English')."""
    if not (identity_md or "").strip():
        return ""
    import re
    for m in re.finditer(r"(?i)(?:response\s+language|language|sprache)\s*[:\-]\s*([A-Za-z\u00C0-\u024F]+)", identity_md):
        return m.group(1).strip()
    return ""


def _language_section(config: dict[str, Any], identity_md_content: str = "") -> str:
    """Response language from IDENTITY.md only; default Deutsch."""
    lang = _language_from_identity_md(identity_md_content) or "Deutsch"
    rule = _get_rule("language.md")
    if rule:
        # Sprache aus IDENTITY.md in die Regeldatei injizieren (ersetzt 'Deutsch' Platzhalter)
        # Wenn IDENTITY.md keine Sprache hat → Deutsch bleibt Default
        if not _language_from_identity_md(identity_md_content):
            # Keine Sprache in IDENTITY.md → Deutsch als Default
            return rule.replace("**Deutsch**", "**Deutsch**") + "\n\n"
        return rule.replace("**Deutsch**", f"**{lang}**") + "\n\n"
    return (
        f"## Language\nAlways respond in **{lang}** unless the user explicitly asks for another language.\n\n"
    )


def _knowledge_verification_section() -> str:
    """Instruct the AI to verify uncertain facts via web search."""
    from datetime import datetime
    now = datetime.now().astimezone()
    today = now.strftime("%B %d, %Y")
    current_time = now.strftime("%H:%M:%S")
    tz_name = now.strftime("%Z") or now.strftime("%z")
    rule = _get_rule("knowledge_verification.md")
    if rule:
        # Inject current date into the rule (replaces {{current_date}} placeholder)
        rule = rule.replace("{{current_date}}", today)
        return f"Today is **{today}**, current local time is **{current_time} {tz_name}**. Your training data (= everything you \"know\") has a cutoff date — anything after that is outdated.\n{rule}\n\n"
    return ""


def _tools_umgebung_section(tools_md: str, config: dict[str, Any]) -> str:
    """TOOLS.md-Inhalt + Verbindungsübersicht (Proxies / Search Engines) falls konfiguriert."""
    lines = []
    if tools_md:
        lines.append(tools_md.strip())

    # Verfügbare Verbindungen: Proxies für read_url + passende Search Engines
    from miniassistant.tools import get_read_url_proxy_names
    ru_proxies = get_read_url_proxy_names(config)
    search_engines = config.get("search_engines") or {}
    if ru_proxies:
        lines.append("\n## Available network connections")
        lines.append(
            "These are the named outbound connections on this system. "
            "The user calls them 'VPN' or 'connection' — there are NO tunnel interfaces (tun0/wg0). "
            "**For HTTP requests and IP checks: use `read_url` with `proxy=`. "
            "For web searches: use `web_search` with `engine=`. "
            "NEVER use `exec`/`curl`/`ip`/`ifconfig` to test connections or get public IPs.**\n"
        )
        lines.append("| Name | read_url proxy | web_search engine | Notes |")
        lines.append("|------|---------------|-------------------|-------|")
        # Baue Mapping: proxy-name → search-engine-name (nach Namensähnlichkeit)
        engine_ids = list(search_engines.keys())
        default_engine = (config.get("default_search_engine") or (engine_ids[0] if engine_ids else "")).strip()
        ru_default = ((config.get("read_url") or {}).get("default_proxy") or (ru_proxies[0][0] if ru_proxies else "")).strip()
        for pname, purl in ru_proxies:
            # Suche passende Search Engine: gleicher Name oder ähnlich (vpn1↔vpn, direct↔main)
            matched_engine = ""
            if pname in engine_ids:
                matched_engine = pname
            else:
                for eid in engine_ids:
                    if eid in pname or pname in eid:
                        matched_engine = eid
                        break
                if not matched_engine:
                    # direct/no-proxy → default engine
                    if not purl and default_engine:
                        matched_engine = default_engine
            proxy_cell = f"`{pname}`" + (" ← default" if pname == ru_default else "")
            engine_cell = f"`{matched_engine}`" + (" ← default" if matched_engine == default_engine else "") if matched_engine else "–"
            url_note = purl if purl else "no proxy / direct"
            lines.append(f"| **{pname}** | {proxy_cell} | {engine_cell} | {url_note} |")
        lines.append(
            f"\nExample: `read_url(url=\"https://ifconfig.me/ip\", proxy=\"vpn1\")` → exit IP of vpn1. "
            f"Return all at once in parallel: one `read_url` per connection in a single response."
        )
    return "\n".join(lines).strip()


def _exec_behavior_section() -> str:
    """Kompakte Verhaltensregeln für exec-Aufrufe: Schritt für Schritt, nicht aufgeben, Research first."""
    rule = _get_rule("exec_behavior.md")
    return (rule + "\n\n") if rule else ""


def _tools_section(config: dict[str, Any]) -> str:
    """Nur Verhaltensregeln fuer Tools – Details stehen bereits im Tool-Schema."""
    docs = _docs_dir_path(config)
    docs_prefix = str(docs) + "/" if docs else "docs/"
    lines = ["## Tool rules"]
    sched_cfg = config.get("scheduler")
    if sched_cfg in (None, False) or sched_cfg is True or (isinstance(sched_cfg, dict) and sched_cfg.get("enabled", True)):
        lines.append(
            "- **Always use `schedule` instead of cron/crontab.** "
            "prompt = plain language task (e.g. `'List open issues from GitHub repo OWNER/REPO'`) — "
            "NO shell commands, NO exec:/tool syntax, NO pre-written answers, NO result previews. "
            "After creating: confirm what was scheduled, when, and what it will do. "
            f"Read `{docs_prefix}SCHEDULES.md` for edge cases (once, simple messages, editing, now+schedule, prompt engineering for API/exec schedules). "
            f"For complex schedule prompts (API calls, exec, self-deletion): also read `{docs_prefix}PROMPT_ENGINEERING.md`."
        )
        lines.append(
            "- **Waiting:** need result in this session ≤10 min → `wait`. "
            "Background task, notify when done → `watch`. Future or recurring → `schedule`."
        )
    lines.append(
        "- `save_config`: **only for system config** (see Persistence section). Pass only keys to change (deep-merged). After saving, tell the user to restart **miniassistant**.\n"
        "  Per-model options → `providers.<name>.model_options.\"model:tag\"`. Quote `:` in YAML keys.\n"
        "  Valid options: temperature, top_p, top_k, num_ctx, num_predict, seed, min_p, stop, repeat_penalty, repetition_penalty, repeat_last_n, think.\n"
        f"  If unsure about the config structure: read `{docs_prefix}CONFIG_REFERENCE.md`."
    )
    lines.append(
        "- **GitHub:** Use REST API via `curl` — NEVER `gh` CLI, NEVER `gh auth`, NEVER tell the user to set up auth. "
        "`$GH_TOKEN` is already injected in every exec call — no setup needed. "
        f"Read `{docs_prefix}GITHUB.md` for curl examples and **repo tracking** setup."
    )
    # Email: nur anzeigen wenn konfiguriert
    from miniassistant.tools import _get_email_account_names
    email_accounts = _get_email_account_names(config)
    if email_accounts:
        accounts_str = ", ".join(f'"{a}"' for a in email_accounts)
        lines.append(
            f"- **Email:** Configured accounts: {accounts_str}. "
            f"Use the `send_email` tool to send emails and `read_email` to read emails. "
            f"Credentials are loaded automatically — NEVER ask the user for login data, NEVER hardcode credentials."
        )
    lines.append("- `check_url`: only when user explicitly asks to verify/check links.")
    lines.append(
        "- **URL / web fetching rules:**\n"
        "  (a) Never construct, guess, or assume URLs, API endpoints, or query parameters from memory — "
        "training data is outdated. Always verify a URL is real and accessible before using it.\n"
        "  (b) `read_url` can only READ static content — it CANNOT fill forms, click buttons, or navigate multi-step flows. "
        "For any site that requires form interaction (package tracking, login, search forms): use `exec` with a Playwright script. "
        "Read `WEB_FETCHING.md` for the exact inspect-first pattern.\n"
        "  (c) **Escalation rule:** If `read_url` returns the homepage, an error, or generic content instead of the specific data you need: "
        "the site requires form interaction. Do NOT give up. Do NOT tell the user the data is unavailable. "
        "Immediately escalate to a Playwright script via `exec` — inspect the page first, then interact."
    )
    # read_url: Basis-Regel + Proxy-Info falls konfiguriert
    from miniassistant.tools import get_read_url_proxy_names
    ru_proxies = get_read_url_proxy_names(config)
    ru_default = ((config.get("read_url") or {}).get("default_proxy") or (ru_proxies[0][0] if ru_proxies else "")).strip()
    if ru_proxies:
        proxy_list = ", ".join(
            f'`{name}`' + (" (direct/no proxy)" if not url else f" ({url})")
            for name, url in ru_proxies
        )
        non_direct = [name for name, url in ru_proxies if url]
        direct_names = [name for name, url in ru_proxies if not url]
        exit_ip_example = (
            f'`read_url(url="https://ifconfig.me/ip", proxy="{non_direct[0]}")` '
            f'gives {non_direct[0]}\'s exit IP'
            if non_direct else
            f'`read_url(url="https://ifconfig.me/ip", proxy="{ru_proxies[0][0]}")` gives exit IP'
        )
        parallel_example = ""
        if non_direct:
            all_names = ([direct_names[0]] if direct_names else []) + non_direct
            if len(all_names) >= 2:
                calls = ", ".join(
                    f'read_url(url="https://ifconfig.me/ip", proxy="{n}")'
                    for n in all_names[:3]
                )
                parallel_example = (
                    f"\n  To get ALL exit IPs at once, return them **in parallel** (one response): {calls}."
                )
        lines.append(
            "- `read_url`: Read the actual content of a web page. Use this to read URLs found during research, "
            "or URLs the user sends you. **When the user sends a link and says 'schau dir das an' or 'lies das': "
            "use `read_url` to read the content — do NOT guess what the page says.**\n"
            f"  **Proxies / VPN exits available:** {proxy_list}. Default: `{ru_default}`.\n"
            "  These proxy entries are the VPN/proxy exit points — when the user asks about 'VPN IPs', 'proxy IPs', "
            "or 'exit IPs', they mean these entries. Use `read_url` with the `proxy` parameter — "
            "**NEVER use `exec`/`curl`/`ip`/`ifconfig` for checking exit IPs or proxy connectivity**, "
            "as those only show local network interfaces, not proxy exits.\n"
            "  **Session routing preference:** When the user says to use a specific connection or VPN"
            " (e.g. 'use vpn1', 'route everything via VPN'), apply it to ALL subsequent `read_url` calls (proxy=)"
            " AND all `web_search` calls (engine=) for the rest of the conversation. Proxy and engine names correspond:"
            " e.g. proxy `vpn1` ↔ engine `vpn`, proxy `vpn2` ↔ engine `vpn2`, proxy `direct` ↔ engine `main`.\n"
            f"  {exit_ip_example}.{parallel_example}"
        )
    else:
        lines.append(
            "- `read_url`: Read the actual content of a web page. Use this to read URLs found during research, "
            "or URLs the user sends you. **When the user sends a link and says 'schau dir das an' or 'lies das': "
            "use `read_url` to read the content — do NOT guess what the page says.**"
        )
    lines.append(
        "- **Parallel execution:** When you return multiple tool calls in a single response, "
        "these tools run **concurrently** (in parallel): `web_search`, `read_url`, `check_url`, `read_email`, `invoke_model`. "
        "When you have multiple **independent** calls of these tools, return them ALL in one response to save time. "
        "Example: user asks 'search 3 sources for X' → return 3× `web_search` in one response (not 3 separate rounds). "
        "**Ordering is preserved:** if you return [web_search, web_search, exec, read_url], the searches run first in parallel, "
        "then exec runs after they finish, then read_url. So place dependent calls AFTER the calls they depend on. "
        "`exec` always runs sequentially (filesystem safety)."
    )
    # Subagents: global config (subagents: [list]) oder per-provider subagents: true
    subagent_list = config.get("subagents") or []
    providers = config.get("providers") or {}
    any_per_provider = any(
        (p.get("models") or {}).get("subagents")
        for p in providers.values() if isinstance(p, dict)
    )
    if subagent_list or any_per_provider:
        # Global subagents list: show full name + any aliases from the matching provider
        sub_display = []
        if subagent_list:
            default_prov = next(iter(providers), "")
            for m in subagent_list:
                # Determine provider from "provider/model" or default
                if "/" in m:
                    prov_name, model_name = m.split("/", 1)
                else:
                    prov_name, model_name = default_prov, m
                prov_cfg = providers.get(prov_name) or {}
                aliases = (prov_cfg.get("models") or {}).get("aliases") or {}
                matching_aliases = [alias for alias, target in aliases.items() if target == model_name]
                if matching_aliases:
                    alias_str = ", ".join(f"`{a}`" for a in matching_aliases)
                    sub_display.append(f"`{m}` (aliases: {alias_str})")
                else:
                    sub_display.append(f"`{m}`")
        if not sub_display and any_per_provider:
            # Fallback: per-provider Modelle sammeln
            default_prov = next(iter(providers), "ollama")
            for prov_name, prov_cfg in providers.items():
                if not isinstance(prov_cfg, dict) or not (prov_cfg.get("models") or {}).get("subagents"):
                    continue
                prefix = f"{prov_name}/" if prov_name != default_prov else ""
                for alias in ((prov_cfg.get("models") or {}).get("aliases") or {}):
                    sub_display.append(f"`{prefix}{alias}`")
        lines.append(
            "- `invoke_model`: Delegate tasks to subagents via `invoke_model(model='...', message='...')`.\n"
            "  **If the user names a specific subagent: ALWAYS use it — never do the work yourself instead.**\n"
            "  Message must be self-contained: goal, expected output format, language, relevant context, paths.\n"
            "  Tell the subagent to complete the full task and return the result (not a TODO list).\n"
            "  If a plan file exists: 'Arbeite gemäß Plan in [PFAD]. Markiere jeden Schritt als [x]/[!].'\n"
            "  **On timeout or error: retry the same subagent once with the same message. Then report to user.**\n"
            "  Do NOT do the work yourself after a subagent failure — ask the user how to proceed.\n"
            "  If result is incomplete: re-invoke with a continuation instruction, or ask the user.\n"
            "  **Sanity-check results:** If a subagent found concrete data (links, prices, products) but then concludes 'doesn't exist' or 'not available' — that is contradictory. Present the actual findings, not the wrong conclusion. Subagents may have outdated knowledge (= outdated training data).\n"
            "  **If subagent returns raw JSON instead of a result:** the subagent failed to execute — do NOT pretend it succeeded. Either retry the subagent or do the task yourself with your own tools.\n"
            "  Present the subagent's result directly — never redo it yourself.\n"
            "  **Parallel execution:** Multiple tool calls in a single response run concurrently for: invoke_model, web_search, read_url, check_url, read_email.\n"
            "  When you have multiple independent tasks (e.g. delegate to 3 subagents, or search 4 sources), call ALL of them in ONE response — they execute in parallel, saving time.\n"
            "  Do NOT call them one by one in separate rounds when they are independent.\n"
            "  **CRITICAL — user requests 'N parallel workers':** When the user explicitly asks for N parallel subagents/workers, you MUST output ALL N `invoke_model` calls in your very first response — not one per round. Generating them one at a time defeats the purpose and ignores the user's explicit instruction. Split the task into N independent sub-tasks and emit all N calls simultaneously.\n"
            "  **When NOT to parallelize:** Run subagents sequentially (one per round) when: (1) tasks write to the same file — parallel writes cause data loss; (2) task B needs the result of task A — dependency requires order; (3) tasks modify shared state (same config, same API resource, same schedule); (4) the API being called has rate limits that concurrent requests would exceed. If unsure whether tasks are truly independent: run sequentially."
        )
        if sub_display:
            lines.append(f"  **Available subagents:** {', '.join(sub_display)}.\n"
                         f"  Use the full name (e.g. `llama-swap/qwen3-35b-a3b`) or any listed alias (e.g. `qwen`). Do NOT invent names not listed here.")
        lines.append(
            "- `debate`: Start a **structured multi-round debate/discussion** between two AI perspectives.\n"
            "  **IMPORTANT: Use this tool when the user says things like:** 'diskutiere mit einem Subworker', 'halte eine Diskussion', "
            "'debattiere über', 'lass zwei Modelle diskutieren', 'hole zwei Meinungen ein', 'Diskussion mit Subagent'.\n"
            "  Do NOT just do a web_search and answer yourself — use the `debate` tool to let subagents argue both sides.\n"
            "  Both sides are argued by subagent(s) — the full transcript is saved to a Markdown file in the workspace.\n"
            "  Between rounds, previous arguments are automatically summarized so small models keep context.\n"
            "  Parameters: `topic`, `perspective_a`, `perspective_b`, `model` (required), `model_b` (optional), `rounds` (1-10, default 3), `language`.\n"
            "  You choose the perspectives — e.g. for weather: 'Wetter wird besser' vs. 'Wetter bleibt schlecht'.\n"
            f"  Read `{docs_prefix}DEBATE.md` for details."
        )
    cc = config.get("chat_clients") or {}
    clients = []
    for k in ("matrix", "discord"):
        cfg = (cc.get(k) or config.get(k) or {}) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        if cfg.get("enabled", True) and (cfg.get("token") or cfg.get("bot_token")):
            clients.append(k)
    if clients:
        lines.append(f"- Notifications only via {', '.join(clients)}. No notify-send.")
        lines.append(
            "- `status_update`: Send an **intermediate message** to the user during multi-step tasks.\n"
            "  Use it to report progress (e.g. 'Schritt 3/7 erledigt'), share interim findings, or ask for input when you are stuck.\n"
            "  Keep updates short (1-3 sentences). Do NOT use for the final answer — that goes in your normal response."
        )
    else:
        lines.append("- No chat clients configured; notifications unavailable.")
    lines.append("")
    return "\n".join(lines)


def _vision_section(config: dict[str, Any]) -> str:
    """Vision/Image/Avatar-Abschnitt für System-Prompt."""
    from miniassistant.ollama_client import get_vision_models, get_image_generation_models
    vision_models = get_vision_models(config)
    img_gen_models = get_image_generation_models(config)
    avatar = config.get("avatar")
    agent_dir = config.get("agent_dir") or ""
    # Immer anzeigen — Avatar-Info ist auch ohne Vision relevant
    lines = ["## Vision, Image & Avatar"]
    if vision_models:
        models_str = ", ".join(f"`{m}`" for m in vision_models)
        lines.append(f"- **Vision models:** {models_str} (image analysis). The user can request a specific model.")
        lines.append("  If the current chat model itself supports vision (llava, gemma3, minicpm-v, etc.), analyze directly without switching.")
        docs_v = _docs_dir_path(config)
        docs_v_prefix = str(docs_v) + "/" if docs_v else "docs/"
        lines.append(f"  Read `{docs_v_prefix}VISION.md` for details on how to handle image uploads.")
    else:
        lines.append("- **No vision model configured.** If the user sends an image, tell them to set `vision` in the config.")
    # Config-Pfad für Image-Upload und Avatar-Anweisungen
    from miniassistant.config import config_path as _config_path
    cfg_path = str(_config_path(None))
    if img_gen_models:
        models_str = ", ".join(f"`{m}`" for m in img_gen_models)
        lines.append(f"- **Image generation models:** {models_str}. Use `invoke_model(model='MODEL_NAME', message='PROMPT')` to generate images.")
        lines.append("  Use the **exact model name** as listed (including `provider/` prefix).")
        lines.append(
            "- **After generating an image:** Use `send_image(image_path='/path/to/image.png', caption='...')` to upload it to the current chat. "
            "The tool handles Matrix upload (via bot client, E2EE-capable), Discord upload, and Web-UI automatically. No curl needed.\n"
            "  **After a successful `send_image`: do NOT reply with text.** The user already sees the image — a confirmation message would be redundant. Only reply if the tool fails."
        )
    avatar_file = f"{agent_dir}/avatar.png" if agent_dir else "agent_dir/avatar.png"
    if avatar:
        lines.append(f"- **Avatar:** `{avatar}` (bot profile picture). Image file: `{avatar_file}`.")
    else:
        lines.append(f"- **Avatar:** not set. Default location: `{avatar_file}`.")
    docs = _docs_dir_path(config)
    avatars_md = str(docs / "AVATARS.md") if docs else "docs/AVATARS.md"
    lines.append(
        f"- **When asked to set/change avatar:** First `ls -la \"{avatar_file}\"`. "
        f"Then `cat \"{avatars_md}\"` for the steps. "
        f"Get credentials with `grep -A20 'matrix:' \"{cfg_path}\"` (or `discord:`, or any other chat client section). "
        "Use the real values in curl — never placeholders. Execute step by step."
    )
    lines.append("")
    return "\n".join(lines)


def _voice_section(config: dict[str, Any]) -> str:
    """Voice-Mode-Hinweise — wenn STT oder TTS konfiguriert ist."""
    from miniassistant.config import get_voice_stt_url, get_voice_tts_url
    has_stt = bool(get_voice_stt_url(config))
    has_tts = bool(get_voice_tts_url(config))
    if not has_stt and not has_tts:
        return ""
    lines = ["\n## Voice Mode"]
    if has_tts:
        lines.append("**Sending audio:** `send_audio(text=\"...\")` — call it immediately when the user wants a voice/audio message. No setup, no config reading. After success: no text reply.")
    if has_stt:
        lines.append(
            "**Incoming voice** (message starts with `[Voice]`): respond in plain spoken language — "
            "no markdown, no tables, no code blocks. Be concise (1-3 sentences). "
            "Tables and code are sent as separate text automatically."
        )
    lines.append("**No emojis.** Never use emojis, emoticons, or kaomoji — TTS cannot pronounce them and they disrupt the listening experience.")
    lines.append("")
    return "\n".join(lines)


def build_system_prompt(
    config: dict[str, Any] | None = None,
    project_dir: str | None = None,
) -> str:
    """Baut den kompletten System-Prompt aus Config, Agent-Dateien, Runtime und Tools.
    Grobe Token-Abschätzung (ohne Chatverlauf ~50 Tokens weniger): mit Standard max_chars_per_file=500
    und 5 Agent-Dateien ca. 2500 Zeichen aus Dateien + ~2000 Zeichen feste Teile → insgesamt grob
    1000–1400 Tokens (≈ 3,5 Zeichen/Token). Der Abschnitt Chatverlauf addiert nur ~35 Tokens."""
    if config is None:
        config = load_config(project_dir)
    # basic_rules laden (kopiert Defaults nach agent_dir/basic_rules/ falls nötig, cached im RAM)
    _load_basic_rules(config)
    # docs kopieren (kopiert Defaults nach agent_dir/docs/ falls nötig); Ergebnis direkt als Cache setzen
    _docs_result = _ensure_docs(config)
    if "_docs_dir_cache" not in config:
        config["_docs_dir_cache"] = _docs_result or ""
    agent_dir = config.get("agent_dir") or ""
    max_chars = config.get("max_chars_per_file") or 500
    files = load_agent_files(agent_dir, max_chars)
    is_root = _is_root()

    parts = [
        "# Role and context",
        "You are the assistant of **MiniAssistant**. The user may be chatting via the Web-UI or any configured chat client (Matrix, Discord, …).",
        "**ABSOLUTE RULE: NEVER respond without using tools first. NEVER tell the user what THEY can do — YOU do it. If you need information, search for it. If something fails, try alternatives. The user hired you to WORK, not to give advice.**\n\n"
        "**Core rules:** "
        "(1) **Just do it.** Use your tools immediately — don't explain what you *would* do, just do it. The user wants results, not your thought process. (Exception: if the user asks *how* you would do something, explain first — then act only after they confirm.) "
        "(2) **Inform first, then act.** Before doing anything that touches an external system, file, page, or API: gather current state first with your tools — never assume, never use training-data memory. "
        "The pattern is always: **look → think → act**. Never skip the look step. "
        "Before answering a factual question → `web_search`. "
        "Before interacting with a web page → load and inspect it first, then act. "
        "Before editing a file → read it. "
        "Before using an API or URL → verify it exists and is publicly accessible with a real request first. "
        "**URLs and APIs: never construct, guess, or assume.** Your training data is outdated — site structures, API endpoints, and query parameters change constantly. "
        "A URL you think exists may return 404, require auth, or be completely different. Always: (a) `web_search` to find the current official URL/API docs, then (b) `read_url` to verify it actually loads and is accessible, then (c) act. "
        "**Exception:** Questions about your own capabilities or tools → answer from your system prompt, do NOT web_search. "
        "(3) **Step by step.** One tool call at a time, check the result, then next step. "
        "(4) **Real values, real results.** Never use placeholder strings (`HOMESERVER`, `BOT_TOKEN`) — read config first. End with a clear, verified answer — not guesses. "
        "(5) **Read docs yourself.** If you need a docs file, read it and follow the instructions — don't tell the user to read it. "
        "(6) **Don't over-ask.** If you have enough information to proceed, just do it. Only ask when essential info is truly missing (e.g. credentials the config doesn't have).",
        "",
        "## Chat history",
        "Only reference prior messages when the user explicitly refers to them. Do not proactively resume older topics.",
        "",
        "## AGENTS (top-level contract)",
        files.get("AGENTS.md", ""),
        "",
        "## SOUL (your personality)",
        (files.get("SOUL.md", "") or "").strip()
        + "\n\nDo not mention being an AI, the user knows. Be focused and factual.",
        "",
        "## IDENTITY (your identity)",
        files.get("IDENTITY.md", ""),
        "",
        "## Environment",
        _tools_umgebung_section(files.get("TOOLS.md", ""), config),
        "",
        "## USER (about your human)",
        files.get("USER.md", ""),
        "",
        _memory_section(project_dir, config),
        _prefs_section(config),
        _language_section(config, files.get("IDENTITY.md") or ""),
        _knowledge_verification_section(),
        _units_section_from_prefs(config),
        _system_and_runtime_section(is_root),
        _safety_section(),
        _exec_behavior_section(),
        _persistence_section(config),
        _planning_section(config),
        _tools_section(config),
        _docs_reference_section(config),
        _vision_section(config),
        _voice_section(config),
        "---\n*End of system instructions. Everything below is the conversation.*",
    ]
    return "\n".join(parts).strip()
