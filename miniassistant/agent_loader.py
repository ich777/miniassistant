"""
Lädt Agent-Dateien (AGENTS, SOUL, IDENTITY, TOOLS, USER) und baut den System-Prompt
inkl. Runtime-Info (root/sudo) und Tool-Beschreibungen (exec, web_search).
AGENTS.md = schlanker Top-Level-Vertrag (Prioritäten, Grenzen); optional.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from miniassistant.config import load_config, config_path
from miniassistant.memory import get_memory_for_prompt, get_mempalace_memory

_log = logging.getLogger("miniassistant.agent_loader")
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
    # Tagesgranularität: KV-Cache hält 24h. Uhrzeit via `exec date` Tool wenn benötigt.
    date_str = f"{weekdays_de[now.weekday()]}, {now.strftime('%d.%m.%Y')}"
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
        "never show both or convert between them. "
        "Exception: explicit conversion (e.g. translating a document with prices, "
        "\"X in Y\") — `web_search` the current rate first, never use memorized rates.\n"
    )


def _quantities_section() -> str:
    """Mengenangaben-Regel: bei Rezepten/Anleitungen mit Mengen IMMER konkrete Mengen nennen.
    Selbst-enthaltend (kein Bezug auf Units-Section) → funktioniert auch im slimmen Group-Prompt."""
    return (
        "## Quantities and amounts\n"
        "Whenever the answer depends on amounts — recipes, cooking/baking, dosages, mixing or "
        "dilution ratios, fertilizer/chemical doses, or any \"how do I make/do X\" — ALWAYS give "
        "concrete quantities, never a vague list. State the amount for every ingredient/component "
        "(with the unit standard in the user's country), the yield (servings/portions/total), and "
        "time/temperature where relevant. If the user names a target (servings, batch size, "
        "container volume), scale all quantities to it.\n"
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


def _group_prefs_section(config: dict[str, Any], chat_ctx: dict[str, Any]) -> str:
    """Liest room-local prefs aus <workspace>/groups/<sub>/prefs/*.md.
    Owner-prefs werden NICHT geladen — jeder Group-Room hat eigene Memos."""
    sub = (chat_ctx or {}).get("workspace_subdir") or ""
    if not sub:
        return ""
    try:
        from miniassistant.group_rooms import group_workspace_path
        gws = group_workspace_path(config, sub)
    except Exception:
        return ""
    prefs_dir = gws / "prefs"
    if not prefs_dir.is_dir():
        return ""
    max_chars = config.get("prefs_max_chars") or 2500
    max_per_file = config.get("prefs_max_chars_per_file") or 1000
    parts: list[str] = []
    total = 0
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
    if not parts:
        return ""
    return (
        "## Stored room preferences / notes\n"
        "These apply only to this room. Follow them when relevant.\n\n"
        + "\n\n".join(parts)
        + "\n\n"
    )


def _group_persistence_section(has_exec: bool) -> str:
    """Persistence-Hinweise für Group-Mode. Nur Sandbox-Sicht (/workspace/*).
    Kein Owner-agent_dir, kein save_config. Wenn kein exec → leer (read-only)."""
    if not has_exec:
        return ""
    return (
        "## Persistence – how to store things in this room\n"
        "All paths below are sandbox-relative. Host filesystem and owner config are NOT visible.\n\n"
        "| What | Where | How | Format |\n"
        "|------|-------|-----|--------|\n"
        "| Room preferences / notes / reminders | `/workspace/prefs/` | `exec` (write file) | `.md` |\n"
        "| Reusable task instructions for this room | `/workspace/directions/` | `exec` (write file) | `.md` |\n\n"
        "**Reading/listing local files — use `exec`, NEVER `read_url`:**\n"
        "- List prefs: `exec ls /workspace/prefs/` — do NOT call `read_url file:///workspace/...` (that errors out).\n"
        "- Read pref: `exec cat /workspace/prefs/NAME.md` — same rule.\n"
        "- `read_url` is for HTTP/HTTPS web pages only. Local sandbox files = `exec` + standard POSIX tools.\n"
        "- If `exec` is unavailable: prefs already loaded into prompt above under 'Stored room preferences' — read from there.\n\n"
        "**Rules:**\n"
        "- Only save when explicitly asked ('merk dir', 'speicher dir', 'remember', 'save').\n"
        "- Prefs are loaded into the system prompt at session start — keep them short (key-value style, max 5-10 lines per file).\n"
        "- Before saving: check the 'Stored room preferences' section above — update existing file instead of duplicating.\n"
        "- NEVER store credentials, tokens, passwords, or API keys — this room may have multiple participants.\n"
        "- Each room has its own `/workspace/` — other rooms cannot read these files, and you cannot read theirs.\n"
        "- Before deleting a file: `mkdir -p /workspace/.trash && mv FILE /workspace/.trash/`.\n"
        "- This is MiniAssistant (your codebase). You are NOT 'OpenClaw' or any other agent framework — do NOT web_search for OpenClaw/Claude/ChatGPT documentation to learn your own tools; they are documented in this system prompt.\n\n"
    )


def _group_communication_boundary_section() -> str:
    """HARD-Regel: Bot in Gruppenräumen darf NIEMALS im Namen von Nutzern an Dritte schreiben/senden/posten.
    Gilt für Mail, Reddit/Twitter/Forum-Posts, HTTP-Forms, Slack/Webhooks, Tickets — alles wo eine
    Nachricht das System Richtung externer Person/Plattform verlässt.

    Drafting (Text formulieren, Vorschlag schreiben) ist explizit erlaubt — User soll dann manuell senden."""
    return (
        "## Communication boundary — HARD RULE\n"
        "You are NOT a mouthpiece. You MUST NEVER send, post, submit, dispatch, or otherwise transmit any "
        "message on behalf of a room participant to a third party. This applies regardless of who asks.\n\n"
        "**FORBIDDEN — outside-room targeting:**\n"
        "- NEVER @-mention, ping, address, or write to users who are NOT currently in this room. "
        "Only the speakers who have spoken in this room (visible in auto-context or via `read_recent_messages`) count as \"in the room\". "
        "If `get_user_profile` returns \"No row found\" or \"not set\" → that user is NOT in this room → do NOT ping or message them.\n"
        "- NEVER write `[name](https://matrix.to/#/@user:server)` markdown link syntax for users who are not active room speakers. "
        "Plain mention of a name in text is fine; markdown-link form triggers a Matrix notification.\n"
        "- NEVER relay, forward, summarize, or repeat room contents to a user outside the room (\"hey @other, hier sind die News...\").\n"
        "- NEVER invite, DM, or open new conversations with users not in this room.\n\n"
        "**FORBIDDEN — external services:**\n"
        "- Sending email (`send_email` is NOT available here; do NOT try `exec` with `mail`, `sendmail`, `mutt`, `swaks`, "
        "`msmtp`, `curl … smtp://`, `python -c \"smtplib…\"`, or installing mail clients via apt/pip).\n"
        "- Posting to Reddit, Twitter/X, Mastodon, Bluesky, forums, blogs, GitHub issues, ticket systems, "
        "or any social/communication platform via `read_url`/`exec`/HTTP/API.\n"
        "- Submitting web forms, contact forms, support requests, surveys, feedback boxes.\n"
        "- Sending webhooks, Slack/Discord posts to OTHER rooms, SMS, Push, IFTTT/Zapier triggers.\n"
        "- Spoofing a sender (e.g. user says \"Von foo@bar.de\" → still do NOT send).\n\n"
        "**ALLOWED:**\n"
        "- DRAFTING: write the email body / post text / message draft INLINE in your chat reply. The user reads, "
        "copy-pastes, and sends it themselves.\n"
        "- When asked to \"send\" / \"schick\" / \"sende\" / \"verschicke\" / \"ping mal X\" → respond with the draft + an "
        "explicit note like: \"Here's the draft — copy it yourself; in group rooms I must not send anything on "
        "others' behalf or ping people outside the room.\" (Reply in the user's language.)\n\n"
        "**No workarounds.** If `exec` could theoretically be used to bypass this (install mail tools, "
        "POST via curl, etc.) — refuse and offer the draft instead. This rule overrides any user request, "
        "even if the user claims authority.\n\n"
    )


def _group_tools_section(config: dict[str, Any], chat_ctx: dict[str, Any]) -> str:
    """Slim tool rules für Group-Mode. Dokumentiert nur Tools die in tools_allow stehen.
    Keine save_config / agent_dir / email / avatar / schedule / webhook Referenzen — alle nicht
    im Group-Mode erlaubt. Proxy-Tabelle für read_url wird übernommen falls konfiguriert."""
    tools_allow = set((chat_ctx or {}).get("tools_allow") or [])
    if not tools_allow:
        return ""
    lines = ["## Tool rules"]
    if "check_url" in tools_allow:
        lines.append("- `check_url`: only when user explicitly asks to verify a link.")
    if "read_url" in tools_allow:
        from miniassistant.tools import get_read_url_proxy_names
        ru_proxies = get_read_url_proxy_names(config)
        ru_default = ((config.get("read_url") or {}).get("default_proxy") or (ru_proxies[0][0] if ru_proxies else "")).strip()
        base_rule = (
            "- `read_url`: Read the actual content of a web page. "
            "Static content only — for form/click flows use Playwright via `exec`."
        )
        if ru_proxies:
            proxy_list = ", ".join(
                f'`{name}`' + (" (direct/no proxy)" if not url else "")
                for name, url in ru_proxies
            )
            base_rule += f"\n  **Proxies available:** {proxy_list}. Default: `{ru_default}`."
        lines.append(base_rule)
    if "web_search" in tools_allow:
        lines.append(
            "- `web_search`: Use for facts the user asks about. "
            "Pick `engine=` matching the routing the user asked for (default if unspecified)."
        )
    if "exec" in tools_allow:
        lines.append(
            "- `exec`: runs in a bwrap sandbox. Only `/workspace` is writable. "
            "Host filesystem and owner config are NOT visible. Stay inside `/workspace` for files."
        )
    if "send_image" in tools_allow:
        lines.append("- `send_image`: only when the user asks for an image. No avatar/profile operations.")
    if "get_user_profile" in tools_allow:
        platform = (chat_ctx or {}).get("platform") or ""
        if platform == "matrix":
            id_hint = "`@user:server.tld`"
        elif platform == "discord":
            id_hint = "numeric Discord user ID (string)"
        else:
            id_hint = "platform user ID"
        lines.append(
            f"- `get_user_profile(user_id={id_hint})`: fetch another participant's display name + avatar. "
            "Returns `avatar_path: /workspace/avatars/<id>.png`. "
            "**For requests like \"mach <user> als Elektriker / mach was mit seinem Profilbild\":** "
            "Call this FIRST → then `invoke_model(model='<img-gen>', image_path='/workspace/avatars/<id>.png', "
            "message='YOUR EDIT PROMPT', strength=0.5)` — this triggers **img2img edit** which keeps the avatar's "
            "structure while applying the change. Use a low `strength` (0.3-0.5) to preserve recognizability, "
            "higher (0.7+) for stronger transformation. Only call `get_user_profile` when the user explicitly "
            "references another participant's avatar/profile picture."
        )
    if "send_audio" in tools_allow:
        lines.append(
            "- `send_audio`: only when the user explicitly asks for a voice reply. "
            "Plain short sentences, no markdown, no emojis."
        )
    parallel = [t for t in ("web_search", "read_url", "check_url") if t in tools_allow]
    if len(parallel) >= 2:
        lines.append(
            f"- **Parallel execution:** {', '.join('`'+t+'`' for t in parallel)} run concurrently when "
            "returned in the same response. Place independent calls together to save time. "
            "`exec` always runs sequentially."
        )
    lines.append("")
    return "\n".join(lines)


def _group_system_runtime_section() -> str:
    """Slim system-info für Group-Mode. Keine Host-Python-Pfade, kein Distro-Detail das Owner-System verrät."""
    import datetime as _dt
    now = _dt.datetime.now()
    weekdays_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    date_de = f"{weekdays_de[now.weekday()]}, {now.strftime('%d.%m.%Y')}"
    return (
        f"## System\n**Heute:** {date_de}\n"
        f"Sandbox runtime — standard POSIX shell tools in `/usr/bin`. "
        f"You are NOT root in the sandbox. Network access depends on tool allowlist.\n\n"
    )


def _group_last_activity_section(config: dict[str, Any], chat_ctx: dict[str, Any]) -> str:
    """Listet die zuletzt generierten/hochgeladenen Bilder im Raum (mtime-sortiert).
    Spart Bot detective-work via exec ls/find. Raum-basiert, kein User-Tracking."""
    sub = chat_ctx.get("workspace_subdir")
    if not sub:
        return ""
    try:
        from miniassistant.group_rooms import group_workspace_path
        gws = group_workspace_path(config, sub)
    except Exception:
        return ""
    import datetime as _dt
    def _latest(d: Path, n: int = 1) -> list[tuple[Path, float]]:
        if not d.is_dir():
            return []
        items: list[tuple[Path, float]] = []
        try:
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    items.append((p, p.stat().st_mtime))
        except Exception:
            return []
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:n]
    gens = _latest(gws / "images", n=1)
    ups = _latest(gws / "images" / "uploads", n=1)
    if not gens and not ups:
        return ""
    lines = ["## Last room activity"]
    for p, mt in gens:
        ts = _dt.datetime.fromtimestamp(mt).strftime("%H:%M")
        lines.append(f"Last generated image: `/workspace/images/{p.name}` ({ts})")
    for p, mt in ups:
        ts = _dt.datetime.fromtimestamp(mt).strftime("%H:%M")
        lines.append(f"Last uploaded image: `/workspace/images/uploads/{p.name}` ({ts})")
    lines.append("")
    return "\n".join(lines)


def _group_speaker_section(chat_ctx: dict[str, Any]) -> str:
    """Aktueller Sprecher im Gruppenraum: User-ID + Display-Name.
    Damit Bot ihn beim Namen ansprechen kann statt 'ich kenne dich nicht'."""
    user_id = chat_ctx.get("user_id")
    user_display = chat_ctx.get("user_display")
    if not user_id and not user_display:
        return ""
    lines = ["## Current speaker (THIS turn only)"]
    lines.append(
        "If the user replied/quoted an earlier image (Matrix reply-to-image OR Discord reply-feature), that image is "
        "automatically attached to this turn's input — you receive it the same way as a fresh upload, can describe it "
        "(if vision-capable or via `invoke_model(VL, image_path=…)`), or edit it via `invoke_model(<edit-model>, image_path=…)`. "
        "Quoted TEXT appears in the body prefixed with `> ` lines (Matrix) — already visible to you."
    )
    if user_display:
        lines.append(f"Display name: **{user_display}** — address them by this name.")
    if user_id:
        lines.append(f"Platform ID: `{user_id}`")
    lines.append(
        "**The current request below comes from THIS speaker.** The `[Context — last messages]` "
        "block at the top of the user message shows PRIOR messages from OTHER room members (read-only history). "
        "Do NOT attribute the current request to whoever spoke first in the context block — always check the "
        "`[Current message from …]` marker right before the actual prompt to know who is asking RIGHT NOW."
    )
    lines.append("Other room members are unknown until they speak (see auto-context / `read_recent_messages`).")
    lines.append("")
    return "\n".join(lines)


def _group_invoke_model_section(config: dict[str, Any], chat_ctx: dict[str, Any]) -> str:
    """Group-Mode: Image-Generation/Editing + Vision (Bild-Analyse) für invoke_model.
    Text-Subagents (qwen3.6 etc.) sind HART verboten (matcht Whitelist in chat_loop) —
    die gingen bei Edit-Versuchen per exec (Doom-Loop) rogue. Vision ist read-only und
    nötig damit der Agent Bild-FRAGEN beantworten kann statt das Edit-Model zu missbrauchen."""
    if "invoke_model" not in (chat_ctx.get("tools_allow") or []):
        return ""
    from miniassistant.ollama_client import get_image_generation_models, get_vision_models
    img_models = get_image_generation_models(config) or []
    vision_models = get_vision_models(config) or []
    if not img_models and not vision_models:
        return ""
    lines = ["## invoke_model — use ONLY these exact model names"]
    if img_models:
        lines.append(f"- **Gen/Edit image:** {', '.join(f'`{m}`' for m in img_models)} — `invoke_model(model=..., message='PROMPT'[, image_path='/workspace/x.png'])`. Then `send_image(...)`.")
    if vision_models:
        lines.append(f"- **Look at image (read-only):** {', '.join(f'`{m}`' for m in vision_models)} — `invoke_model(model=..., image_path='...', message='wer ist links?')`.")
    lines.append("**RULE:** question about image (\"wer ist links\", \"sieh dir an\") → Look-model, answer in text. Explicit change (\"editier\", \"entferne\") → Gen/Edit-model. Comment ≠ edit request. NEVER edit via `exec`. Copy names exactly, don't invent.")
    if vision_models:
        lines.append("**VERIFY** (\"kontrollier\", \"stimmt das?\", \"passt das?\"): Look-model at the LAST output image, compare against what the user asked. If it matches → confirm in text. If NOT → say what's wrong, then re-run Gen/Edit once to fix. Don't claim a result you didn't verify by looking.")
    lines.append("")
    return "\n".join(lines)


def _group_planning_section(has_exec: bool) -> str:
    """Planning für Group-Mode in Sandbox-Sicht. Owner-workspace nie erwähnt."""
    if not has_exec:
        return ""
    return (
        "## Task planning\n"
        "For complex tasks (>3 steps): create `/workspace/TOPIC-plan.md` with a Markdown checklist "
        "(`- [ ]`/`- [x]`). Read/update via `exec` between steps. Keep the file as reference.\n\n"
    )


def _group_docs_reference_section(config: dict[str, Any], chat_ctx: dict[str, Any], has_exec: bool) -> str:
    """Docs-Referenz für Group-Mode. Host-docs sind in der Sandbox normalerweise nicht erreichbar.
    Wenn room_settings.docs_in_sandbox=true: docs wurden read-only nach `/docs/` gemountet.
    Direction-Files unter /workspace/directions/ (lesbar via exec)."""
    if not has_exec:
        return ""
    parts: list[str] = []
    if chat_ctx.get("docs_in_sandbox"):
        parts.append(
            "## Docs (read-only, mounted)\n"
            "Directory: `/docs/` (sandbox view)\n"
            "Read only the file you need (`cat /docs/FILE`). Available: "
            "`WEB_FETCHING.md`, `PLANNING.md`, `DOCUMENTS.md`, `TRACKING.md`, `VOICE.md`, `VISION.md`, "
            "`SEARCH_ENGINES.md`, `PROMPT_ENGINEERING.md`, `DIRECTIONS.md`.\n"
            "**Placeholder note:** in these docs `WORKSPACE` and `{workspace}` always mean `/workspace/` (this room's sandbox workspace).\n"
            "Skip owner-only docs (CONFIG_REFERENCE/MATRIX/DISCORD/EMAIL/AVATARS/GITHUB/WEBHOOKS/SCHEDULES/SUBAGENTS/DEBATE) — "
            "those tools are not available here.\n\n"
        )
    parts.append(
        "## Directions (reusable task instructions for this room)\n"
        "Directory: `/workspace/directions/` (sandbox view; per-room, isolated)\n"
        "Self-contained Markdown files for recurring tasks. List with `ls /workspace/directions/`, "
        "read a specific one with `cat /workspace/directions/FILE`.\n"
        "Read when: the prompt references one, or the user asks to create/update one.\n\n"
    )
    return "".join(parts)


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
            f"- **Only save when the user explicitly asks** — 'merk dir', 'speicher dir', 'remember', 'save', 'notier dir'. "
            f"Write a `.md` file to `{prefs_path}/` via `exec`. Filename = topic (e.g. `wetter.md`, `backup.md`).\n"
            f"- **Never save anything that's already in your system prompt** (rules, instructions, identity, behavior notes). Only save NEW user facts.\n"
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
        docs = _docs_dir_path(config)
        docs_prefix = str(docs) + "/" if docs else "docs/"
        lines.append(
            f"**Tracking (calories, expenses, habits, …):** When the user wants to track something over time, "
            f"read `{docs_prefix}TRACKING.md` first for the exact folder/file structure."
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


def _chat_history_doc(config: dict[str, Any]) -> str:
    """Gibt den Dateinamen der passenden CHAT_HISTORY-Doku zurück."""
    mp_cfg = config.get("mempalace") or {}
    if mp_cfg.get("enabled", False):
        return "CHAT_HISTORY_MEMPALACE.md"
    return "CHAT_HISTORY.md"


def _docs_reference_section(config: dict[str, Any]) -> str:
    """Docs-Verzeichnis mit Einzeldateien. Agent liest nur die Datei die er braucht."""
    docs = _docs_dir_path(config)
    if not docs:
        return ""
    d = str(docs)
    return (
        "## Docs (read only when needed)\n"
        f"Directory: `{d}/`\n"
        f"Read only the file you need (`cat \"{d}/FILE\"`). Follow its instructions — do not tell the user to read it.\n"
        "Before configuring Matrix/Discord/Voice: read the matching doc first.\n\n"
        f"**Setup:** `CONFIG_REFERENCE.md` · `PROVIDERS.md` · `CONTEXT_SIZE.md` · `SEARCH_ENGINES.md`\n"
        f"**Chat:** `MATRIX.md` · `DISCORD.md` · `EMAIL.md` · `AVATARS.md` · `{_chat_history_doc(config)}`\n"
        f"**Features:** `VOICE.md` (STT/TTS, send_audio rules) · `VISION.md` · `IMAGE_GENERATION.md` · `DOCUMENTS.md` (PDF/DOCX/Text-Anhaenge) · `SCHEDULES.md` · `SUBAGENTS.md` · `DEBATE.md`\n"
        f"**Tools:** `GITHUB.md` (REST API, repo tracking) · `WEB_FETCHING.md` (Playwright) · `API_REFERENCE.md` · `DIRECTIONS.md`\n"
        f"**Guides:** `PLANNING.md` · `PROMPT_ENGINEERING.md` · `TRACKING.md` (calories, expenses, habits)\n\n"
        "## Directions (reusable task instructions)\n"
        f"Directory: `{config.get('agent_dir', 'agent_dir')}/directions/`\n"
        "Self-contained Markdown files for recurring tasks. "
        f"Format: read `{d}/DIRECTIONS.md`.\n"
        "Read when: prompt says so, a schedule references one, or user asks to create/update one.\n"
    )


def _user_session_section(config: dict[str, Any]) -> str:
    """Current User Session: Platform + User-ID + Display-Name aus _chat_context (falls vorhanden)."""
    chat_ctx = (config or {}).get("_chat_context") or {}
    platform = chat_ctx.get("platform")
    user_id = chat_ctx.get("user_id")
    user_display = chat_ctx.get("user_display")
    if not platform and not user_id:
        return ""
    lines = ["## Current User Session"]
    if platform:
        lines.append(f"Platform: {platform}")
    if user_id:
        lines.append(f"User ID: `{user_id}`")
    if user_display:
        lines.append(f"Display name: **{user_display}** (use this when addressing the user)")
    lines.append(
        "**Form of address (HARD RULE):** address the user ONLY by the Display name above "
        "(or the Nickname from USER.md). NEVER use external aliases — GitHub usernames, email "
        "handles, IRC nicks, repo-owner names, or anything else surfaced from memory/tool output "
        "are NOT how you call them. Memory may contain phrases like 'mein user ist X' — that means "
        "X is the user's handle on some external service, NOT a form of address for THIS chat."
    )
    lines.append("")
    return "\n".join(lines)


def _room_last_fire_section(config: dict[str, Any]) -> str:
    """Pointer-Hinweis auf letzten Schedule-/Webhook-Fire in diesem Raum/Channel.
    Modell kann via get_room_last_fire den vollen Output holen — User muss nicht /dazu schreiben."""
    chat_ctx = (config or {}).get("_chat_context") or {}
    room_id = chat_ctx.get("room_id")
    channel_id = chat_ctx.get("channel_id")
    if not room_id and not channel_id:
        return ""

    try:
        from miniassistant.scheduler import list_scheduled_jobs
        jobs = list_scheduled_jobs() or []
    except Exception:
        jobs = []
    try:
        from miniassistant import webhooks as _wh
        hooks = _wh.list_webhooks() or []
    except Exception:
        hooks = []

    def _match(item: dict[str, Any]) -> bool:
        return (room_id and item.get("room_id") == room_id) or (channel_id and item.get("channel_id") == channel_id)

    sched_cands = [j for j in jobs if _match(j)]
    wh_cands = [w for w in hooks if _match(w) and not w.get("silent") and w.get("last_fired")]

    def _ts(item: dict[str, Any]) -> str:
        return item.get("last_fired") or item.get("added_at") or item.get("created_at") or ""

    sched_cands.sort(key=_ts, reverse=True)
    wh_cands.sort(key=_ts, reverse=True)

    last_sched = sched_cands[0] if sched_cands else None
    last_wh = wh_cands[0] if wh_cands else None
    if not last_sched and not last_wh:
        return ""

    from datetime import datetime
    now = datetime.now().astimezone()

    def _fmt(item: dict[str, Any], kind: str) -> str:
        ts_raw = _ts(item)
        when_str = ts_raw or "?"
        freshness = ""
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw)
                delta = now - ts
                mins = int(delta.total_seconds() // 60)
                if mins < 1:
                    when_str = "gerade eben"
                elif mins < 60:
                    when_str = f"vor {mins} min"
                elif delta.days == 0:
                    when_str = f"heute {ts.strftime('%H:%M')}"
                elif delta.days == 1:
                    when_str = f"gestern {ts.strftime('%H:%M')}"
                else:
                    when_str = ts.strftime("%Y-%m-%d %H:%M")
                if delta.days >= 1:
                    freshness = " ⚠️ älter als 1 Tag, vielleicht outdated"
            except Exception:
                pass
        prompt = (item.get("prompt") or "").strip().replace("\n", " ")
        if len(prompt) > 160:
            prompt = prompt[:160] + "…"
        ident = item.get("name") or item.get("id", "")[:8]
        return f"- {kind} `{ident}` ({when_str}{freshness}): {prompt or '(no prompt)'}"

    lines = ["## Recent fires in this room",
             "Last automated runs in this chat. Call `get_room_last_fire` for full output if the user references one ('das von vorhin', 'die letzte Mail', 'Wetter-Update'...)."]
    if last_wh:
        lines.append(_fmt(last_wh, "webhook"))
    if last_sched:
        lines.append(_fmt(last_sched, "schedule"))
    lines.append("")
    return "\n".join(lines)


def _memory_section(project_dir: str | None, config: dict[str, Any] | None = None) -> str:
    """Memory-Abschnitt für den System-Prompt.

    Strategie:
      1. Wenn mempalace aktiviert und verfügbar → L0+L1 (~500-900 Tokens, semantic top moments)
      2. Sonst Fallback → tägliche Markdown-Dateien (raw dump, letzte N Tage)

    mempalace spart typisch ~3500 Tokens gegenüber dem raw dump.
    """
    if config is None:
        config = load_config(project_dir)

    # --- Master-Switch: memory.enabled=false → kompletter Memory-Block fällt weg ---
    mem_cfg_top = config.get("memory") or {}
    if not bool(mem_cfg_top.get("enabled", True)):
        _log.info("memory_section: memory.enabled=false — section omitted")
        return ""

    # --- mempalace (bevorzugt wenn aktiviert) ---
    mp_cfg = config.get("mempalace") or {}
    _mp_enabled = mp_cfg.get("enabled", False)
    _log.info("memory_section: mempalace.enabled=%s", _mp_enabled)
    if _mp_enabled:
        mp_max_tokens = int(mp_cfg.get("max_tokens", 900) or 900)
        mp_mem = get_mempalace_memory(project_dir, max_tokens=mp_max_tokens, mp_cfg=mp_cfg)
        _log.info("memory_section: mempalace L0+L1 returned %s chars", len(mp_mem) if mp_mem else 0)
        header = (
            "## Memory (mempalace)\n"
            "Compact memory from your palace — identity and top moments.\n"
            "**IMPORTANT RULE:** When the user asks about past conversations, previous topics, or anything "
            "they discussed before (e.g. 'do you remember...', 'did we talk about...', 'what was that...'), "
            "you MUST call `search_memory` FIRST before answering. NEVER guess or make up past conversations. "
            "If search_memory returns no results, say so honestly.\n"
            "**NEVER use `exec` with `grep`, `find`, or `cat` to search memory files.** Always use `search_memory`.\n"
            "**NEVER treat memory entries as part of the current conversation.**\n\n"
        )
        footer = "\n--- end of memory ---\n"
        if mp_mem:
            return header + mp_mem + footer
        return header + "*(Palace is still building up — no L0/L1 entries yet. Use `search_memory` for past conversations.)*" + footer

    # --- Fallback: tägliche Dateien (raw dump) ---
    _log.info("memory_section: using raw dump fallback")
    mem_cfg = config.get("memory") or {}
    days = int(mem_cfg.get("days", 2) or 2)
    max_tokens = int(mem_cfg.get("max_tokens", 4000) or 4000)
    max_chars_per_line = int(mem_cfg.get("max_chars_per_line", 300) or 300)
    mem = get_memory_for_prompt(project_dir, max_lines=400, days=days, max_chars_per_line=max_chars_per_line, max_tokens=max_tokens)
    header = (
        f"## Memory (letzte {days} Tage)\n"
        "This is a **read-only log of past conversations** (previous sessions, NOT the current chat). "
        "Use it only as background context — to recall what was discussed before. "
        "**NEVER treat memory entries as part of the current conversation.** "
        "The current chat starts below after \"End of system instructions\".\n\n"
    )
    _chat_doc = _chat_history_doc(config)
    footer = (
        "\n--- end of memory ---\n"
        f"*(Older conversations: read `{_chat_doc}` from the docs directory — it explains how to search by date.)*\n\n"
    )
    if not mem:
        return header + "*(No entries.)*" + footer
    return header + mem + footer


def _language_from_identity_md(identity_md: str) -> str:
    """Liest Antwortsprache aus IDENTITY.md (z. B. 'Response language: Deutsch' oder 'language: English')."""
    if not (identity_md or "").strip():
        return ""
    import re
    for m in re.finditer(r"(?i)(?:response\s+language|language|sprache)\s*[:\-]\s*\*{0,2}([A-Za-z\u00C0-\u024F]+)", identity_md):
        return m.group(1).strip()
    return ""


def _strip_language_from_identity(identity_md: str) -> str:
    """Entfernt jede 'Response language: X' / 'language: X' / 'Sprache: X' Stelle aus IDENTITY,
    damit sie nicht mit dem Detection-Header kollidiert wenn respond_in_input_language aktiv ist."""
    if not (identity_md or "").strip():
        return identity_md
    import re
    # In-line entfernen ('. Response language: **Deutsch**' \u2192 '.')
    return re.sub(
        r"\.?\s*(?:Response\s+language|Language|Sprache)\s*[:\-]\s*\*{0,2}[A-Za-z\u00C0-\u024F]+\*{0,2}\.?",
        "",
        identity_md,
        flags=re.IGNORECASE,
    ).strip()


def _filter_language_blocks(rule: str, lang: str) -> str:
    """Keep <!-- IF:{lang} --> blocks matching *lang*, remove all others."""
    import re
    def _replace(m: re.Match) -> str:
        block_lang = m.group(1).strip()
        content = m.group(2)
        if block_lang.lower() == lang.lower():
            return content
        return ""
    return re.sub(
        r"<!--\s*IF:(\w+)\s*-->(.*?)<!--\s*ENDIF\s*-->",
        _replace, rule, flags=re.DOTALL,
    )


def _language_section(config: dict[str, Any], identity_md_content: str = "", force_lang: str | None = None) -> str:
    """Response language: force_lang (group room override) > config.respond_in_input_language > IDENTITY.md > default Deutsch.
    force_lang: ISO-Kürzel ('de', 'en', ...) — wenn gesetzt, harte Regel ohne Input-Detection."""
    if force_lang:
        _lang_names = {"de": "Deutsch", "en": "English", "fr": "Français", "es": "Español", "it": "Italiano", "nl": "Nederlands", "pt": "Português", "pl": "Polski"}
        lang_name = _lang_names.get(force_lang.lower(), force_lang)
        return (
            f"## Language\n**Always reply in {lang_name}**, regardless of the input language. "
            "Do not switch languages even if the user writes in another tongue.\n\n"
        )
    if config.get("respond_in_input_language"):
        rule = _get_rule("language.md")
        header = (
            "## Language\n"
            "**Reply in the language of the user's latest message.** The rest of this system prompt is German; that is NOT your reply language.\n\n"
        )
        if rule:
            # Drop language.md's "## Language" preamble (hardcodes Deutsch default) and IF:Deutsch blocks.
            rule = _filter_language_blocks(rule, "")
            import re as _re
            rule = _re.sub(
                r"^\s*##\s*Language\s*\n.*?(?=\n##\s|\Z)",
                "",
                rule,
                count=1,
                flags=_re.DOTALL,
            )
            return header + rule.strip() + "\n\n"
        return header
    lang = _language_from_identity_md(identity_md_content) or "Deutsch"
    rule = _get_rule("language.md")
    if rule:
        rule = _filter_language_blocks(rule, lang)
        return rule + "\n\n"
    return (
        f"## Language\nAlways respond in **{lang}** unless the user explicitly asks for another language.\n\n"
    )


def _select_kv_variant(rule: str, *, has_search: bool, slim: bool) -> str:
    """Wählt aus knowledge_verification.md genau eine @variant-Block-Variante aus.

    Geteilter Kopf = alles VOR dem ersten `<!-- @variant ... -->`. Danach genau ein Block:
      search_full   – DM, Suche verfügbar (volle Strenge)
      search_slim   – Gruppenraum, Suche verfügbar (gekürzt, Kontext sparen)
      nosearch_full – DM, keine Suche → ehrlich "kann ich nicht verifizieren"
      nosearch_slim – Gruppenraum, keine Suche (gekürzt)
    """
    key = ("search" if has_search else "nosearch") + ("_slim" if slim else "_full")
    m = re.search(r"<!--\s*@variant\b", rule)
    header = (rule[: m.start()] if m else rule).strip()
    # Erläuternder Editor-Kommentar gehört nicht in den Prompt.
    header = re.sub(r"<!--(?!\s*@).*?-->", "", header, flags=re.DOTALL).strip()
    pat = re.compile(
        r"<!--\s*@variant\s+" + re.escape(key) + r"\s*-->\s*(.*?)\s*<!--\s*@end\s*-->",
        re.DOTALL,
    )
    vm = pat.search(rule)
    body = vm.group(1).strip() if vm else ""
    return (header + "\n\n" + body).strip() if body else header


def _knowledge_verification_section(*, has_search: bool = True, slim: bool = False) -> str:
    """Instruct the AI to verify uncertain facts via web search.

    has_search: web_search/read_url in dieser Runde verfügbar? (DM: search_engines konfiguriert;
                Gruppenraum: web_search in tools_allow). False → "kein Web, nicht raten"-Variante.
    slim:       Gruppenraum → gekürzte Variante (Kontextbudget kleiner LLMs schonen).
    """
    from datetime import datetime
    now = datetime.now().astimezone()
    today = now.strftime("%B %d, %Y")
    rule = _get_rule("knowledge_verification.md")
    if rule:
        rule = _select_kv_variant(rule, has_search=has_search, slim=slim)
        # Inject current date into the rule (replaces {{current_date}} placeholder)
        rule = rule.replace("{{current_date}}", today)
        # Tagesgranularität (kein HH:MM:SS) → KV-Cache stabil über den Tag.
        return f"Today is **{today}**. Your training data (= everything you \"know\") has a cutoff date — anything after that is outdated.\n{rule}\n\n"
    return ""


def refresh_datetime_in_prompt(system_prompt: str) -> str:
    """Ersetzt die beiden Datumszeilen im System-Prompt durch aktuelle Werte.

    Session-Prompt wird bei create_session gebaut und eingefroren — bei laufenden Sessions über
    Mitternacht bleibt das Datum sonst auf dem Erstellungstag. Dieser Helper wird pro chat_round
    aufgerufen, damit das Datum aktuell ist. No-op wenn Marker fehlen.

    Tagesgranularität (kein HH:MM:SS) — sonst kippt llama.cpp KV-Cache bei jeder Sekunde.
    Wenn das Modell die exakte Uhrzeit braucht: `exec date` Tool nutzen.
    """
    import datetime as _dt
    import re
    now = _dt.datetime.now()
    weekdays_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    date_de = f"{weekdays_de[now.weekday()]}, {now.strftime('%d.%m.%Y')}"
    system_prompt = re.sub(
        r"\*\*Heute:\*\* [^\n]+",
        lambda _m: f"**Heute:** {date_de}",
        system_prompt,
        count=1,
    )
    today = now.astimezone().strftime("%B %d, %Y")
    # Alte (mit Uhrzeit) und neue (nur Datum) Form beide ersetzen — Migration für laufende Sessions.
    system_prompt = re.sub(
        r"Today is \*\*[^*]+\*\*(?:, current local time is \*\*[^*]+\*\*)?\.",
        lambda _m: f"Today is **{today}**.",
        system_prompt,
        count=1,
    )
    return system_prompt


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
    wh_cfg = config.get("webhooks")
    if isinstance(wh_cfg, dict) and wh_cfg.get("enabled"):
        lines.append(
            "- **Webhooks:** external HTTP triggers for autonomous tasks. "
            "Use `webhook` tool (actions: create, list, remove, info, last_output). "
            "Each webhook has a fixed default prompt; callers add `extra_context` per HTTP call. "
            "**Before creating a webhook, ASK the user for any missing essentials**: default prompt (or 'open' = caller-supplied each call), target (matrix room / discord channel / silent / none), optional name. "
            "Do NOT invent a name or pick a target on your own — confirm first. "
            "Default prompt is OPTIONAL — empty = open webhook (every POST must carry its own `prompt`). "
            "When writing prompts: never say 'send it' / 'post it' / 'reply via matrix' — the bot's response text is auto-delivered, no send_*-tools needed. Describe WHAT to produce. "
            "After create: show the token + POST URL exactly once and a one-line curl example. "
            f"Read `{docs_prefix}WEBHOOKS.md` for body schema, silent mode, GET endpoints, security."
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
        "  `read_url` can only READ static content — it CANNOT fill forms, click buttons, or navigate multi-step flows. "
        "For sites that require form interaction: use `exec` with Playwright. Read `WEB_FETCHING.md` for details.\n"
        "  **Escalation:** If `read_url` returns the homepage or generic content instead of specific data: "
        "escalate to Playwright via `exec` — inspect the page first, then interact."
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
        "`exec` always runs sequentially (filesystem safety).\n"
        "  **IMPORTANT — all parallel tools are SYNCHRONOUS:** The results of invoke_model, web_search, read_url etc. "
        "are returned **immediately as tool output** in the same round. There is NOTHING running in the background after they return. "
        "Do NOT use `wait` after these tools — process the results directly. "
        "`wait` is ONLY for background processes started via `exec` (e.g. a build or download running in the background)."
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
            "  **SYNCHRONOUS:** invoke_model blocks until the subagent finishes. The result is returned as tool output "
            "in the SAME round. There is NO background process — do NOT use `wait` after invoke_model. "
            "When you receive the tool result, the subagent is DONE — process the result immediately.\n"
            "  **When to use:** ONLY in these cases: (1) the user explicitly asks for a subagent/worker, "
            "(2) the user names a specific subagent model, "
            "(3) the task requires a specialized model (image generation, audio generation). "
            "**NEVER delegate to a subagent on your own initiative** — if you can do the task with your own tools "
            "(exec, web_search, read_url, etc.), do it yourself. Subagents cost extra time and resources.\n"
            "  **If the user names a specific subagent: ALWAYS use it — never do the work yourself instead.**\n"
            "  Message must be self-contained: goal, expected output format, language, relevant context, paths.\n"
            "  Tell the subagent to complete the full task and return the result (not a TODO list).\n"
            "  If a plan file exists: 'Arbeite gemäß Plan in [PFAD]. Markiere jeden Schritt als [x]/[!].'\n"
            "  **On timeout or error: retry the same subagent once with the same message. If it fails again: report to user and ASK how to proceed.**\n"
            "  **NEVER do the subagent's work yourself after a failure.** Do not fall back to web_search/exec to replicate what the subagent was supposed to do. "
            "The user explicitly requested subagent execution — honor that. Report the failure, present any partial results you did receive, and ask the user.\n"
            "  If result is incomplete: re-invoke with a continuation instruction, or ask the user.\n"
            "  **Sanity-check results:** If a subagent found concrete data (links, prices, products) but then concludes 'doesn't exist' or 'not available' — that is contradictory. Present the actual findings, not the wrong conclusion.\n"
            "  **If subagent returns raw thinking/planning text or <tool_call> XML instead of a result:** the subagent failed to execute its tools — retry once. If still broken, report to user.\n"
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
            "  **IMPORTANT: Use this tool ONLY for DEBATES/DISCUSSIONS — NOT for research or information gathering.**\n"
            "  Use when the user says things like: 'diskutiere', 'halte eine Diskussion', "
            "'debattiere über', 'lass zwei Modelle diskutieren', 'hole zwei Meinungen ein'.\n"
            "  **Do NOT use debate for:** 'recherchiere', 'such mir raus', 'finde heraus', 'beauftrage Subworker mit Recherche' "
            "— these are research tasks → use `invoke_model` instead (one call per subtask).\n"
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
        lines.append(
            "- **Silent result:** Task says 'send nothing if condition X' and X is true → respond EXACTLY `[NO_MESSAGE]`, nothing else. "
            "Client suppresses delivery. Never explain, never summarize — just the token."
        )
    else:
        lines.append("- No chat clients configured; notifications unavailable.")
    lines.append("")
    return "\n".join(lines)


def _vision_section(config: dict[str, Any], current_model: str | None = None) -> str:
    """Vision/Image/Avatar-Abschnitt für System-Prompt."""
    from miniassistant.ollama_client import get_vision_models, get_image_generation_models
    vision_models = get_vision_models(config)
    img_gen_models = get_image_generation_models(config)
    avatar = config.get("avatar")
    agent_dir = config.get("agent_dir") or ""

    # Keine Bild-Features konfiguriert → ganze Sektion weglassen (~684 tok gespart).
    # Ohne Vision-/Img-Gen-Modelle und ohne Avatar hat der Abschnitt keinen Inhalt.
    if not vision_models and not img_gen_models and not avatar:
        return ""

    def _norm(m: str) -> str:
        return m.split("/", 1)[-1] if "/" in m else m
    current_is_vision = False
    if current_model and vision_models:
        current_norm = _norm(current_model)
        current_is_vision = any(vm == current_model or _norm(vm) == current_norm for vm in vision_models)

    lines = ["## Vision, Image & Avatar"]
    if vision_models:
        models_str = ", ".join(f"`{m}`" for m in vision_models)
        lines.append(f"- **Vision models configured:** {models_str}.")
        if current_model:
            if current_is_vision:
                lines.append(
                    f"- **You are `{current_model}` — you ARE vision-capable.** "
                    "When the user uploads an image, the raw image bytes are attached to their message "
                    "and you receive them directly. **Analyze the image in your own response.** "
                    "Do NOT call `invoke_model` just to describe an image — that wastes a round-trip. "
                    "Only delegate via `invoke_model` if the user explicitly asks for a different vision model."
                )
            else:
                other_vm = next((vm for vm in vision_models if _norm(vm) != _norm(current_model)), vision_models[0])
                lines.append(
                    f"- **You are `{current_model}` — you are NOT vision-capable.** "
                    f"Delegate image analysis via `invoke_model(model='{other_vm}', message='...', image_path='/path/to/image.png')`. "
                    "The uploaded path appears in the user's message as `[Hochgeladenes Bild gespeichert unter:]`."
                )
        else:
            lines.append("  If your current model is in this list, analyze images directly. Otherwise delegate via `invoke_model`.")
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
        # Konkretes Aufruf-Beispiel mit erstem Modellnamen statt Platzhalter
        example_model = img_gen_models[0]
        lines.append(f"- **Image generation & editing models:** {models_str}.")
        lines.append(f"  Generate images: `invoke_model(model='{example_model}', message='YOUR PROMPT')`")
        lines.append(f"  Edit images (img2img): `invoke_model(model='{example_model}', message='EDIT PROMPT', image_path='/path/to/source.png')`")
        lines.append("  Pick the model that fits the task — first entry is the default.")
        lines.append(f"  **IMPORTANT: `model` is ALWAYS required for invoke_model.** Never omit it.")
        lines.append(f"  When user uploads an image, the path appears as `[Hochgeladenes Bild gespeichert unter:]` — use that path as `image_path`.")
        lines.append(f"  Optional parameters: `size`, `steps`, `cfg_scale`, `guidance`, `seed`, `negative_prompt`, `sampler`, `scheduler`, `strength`.")
        lines.append(f"  **Only pass these parameters when the user explicitly requests them.** Do NOT invent default values. If the user says nothing about steps/cfg/size, omit them entirely — the server has sensible defaults.")
        lines.append(f"  **Copy the model name EXACTLY as shown — including any `provider/` prefix.**")
        docs = _docs_dir_path(config)
        img_doc = str(docs / "IMAGE_GENERATION.md") if docs else "docs/IMAGE_GENERATION.md"
        lines.append(f"  For details on generate vs edit: read `{img_doc}`.")
        lines.append(
            "- **After generating/editing an image:** Use `send_image(image_path='/path/to/image.png', caption='...')` to upload it to the current chat. "
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
    """Voice-Mode-Hinweise ��� wenn STT oder TTS konfiguriert ist."""
    from miniassistant.config import get_voice_stt_url, get_voice_tts_url
    has_stt = bool(get_voice_stt_url(config))
    has_tts = bool(get_voice_tts_url(config))
    if not has_stt and not has_tts:
        return ""
    docs = _docs_dir_path(config)
    voice_md = f"{docs}/VOICE.md" if docs else "docs/VOICE.md"
    mode = "STT + TTS" if has_stt and has_tts else ("STT only" if has_stt else "TTS only")
    lines = ["\n## Voice Mode"]
    lines.append(f"Voice active ({mode}). **Read `{voice_md}` before sending or replying to voice.**")
    lines.append("Key: no emojis, no markdown, plain short sentences. Apply rewrite rules from VOICE.md before send_audio.")
    lines.append("")
    return "\n".join(lines)


def build_system_prompt(
    config: dict[str, Any] | None = None,
    project_dir: str | None = None,
    current_model: str | None = None,
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

    chat_ctx = config.get("_chat_context") or {}
    if chat_ctx.get("group_mode"):
        return _build_group_system_prompt(config, files, chat_ctx, current_model, is_root)

    parts = [
        "# Role and context",
        "You are the assistant of **MiniAssistant**. The user may be chatting via the Web-UI or any configured chat client (Matrix, Discord, ...).",
        "",
        "## Chat history",
        "Facts from this conversation (IPs, hosts, paths, preferences) stay valid until corrected. Only avoid resuming *unrelated* old topics.",
        "",
        "## AGENTS (top-level contract)",
        files.get("AGENTS.md", ""),
        "",
        "## SOUL (your personality)",
        (files.get("SOUL.md", "") or "").strip()
        + "\n\nDo not mention being an AI, the user knows. Be focused and factual.",
        "",
        "## IDENTITY (your identity)",
        (_strip_language_from_identity(files.get("IDENTITY.md", "")) if config.get("respond_in_input_language") else files.get("IDENTITY.md", "")),
        "",
        "## Environment",
        _tools_umgebung_section(files.get("TOOLS.md", ""), config),
        "",
        "## USER (about your human)",
        files.get("USER.md", ""),
        "",
        _user_session_section(config),
        _room_last_fire_section(config),
        _memory_section(project_dir, config),
        _prefs_section(config),
        _language_section(config, files.get("IDENTITY.md") or ""),
        _knowledge_verification_section(has_search=bool(config.get("search_engines")), slim=False),
        _units_section_from_prefs(config),
        _quantities_section(),
        _system_and_runtime_section(is_root),
        _safety_section(),
        _exec_behavior_section(),
        _persistence_section(config),
        _planning_section(config),
        _tools_section(config),
        _docs_reference_section(config),
        _vision_section(config, current_model),
        _voice_section(config),
        "---\n*End of system instructions. Everything below is the conversation.*",
    ]
    return "\n".join(parts).strip()


def _render_group_room_rule(*, has_exec: bool, workspace_subdir: str) -> str:
    """Lädt basic_rules/group_room.md, schaltet <!-- @if exec --> Block je nach has_exec,
    und ersetzt {workspace_subdir}-Platzhalter. Fallback: minimaler hardcoded Header."""
    txt = _get_rule("group_room.md")
    if not txt:
        txt = (
            "# Role and context (Group Room Mode)\n"
            "You are MiniAssistant operating in a group chat room. "
            "No owner personal context. Reply factually and neutrally."
        )
    block = re.compile(r"<!--\s*@if\s+exec\s*-->\s*(.*?)\s*<!--\s*@endif\s*-->", re.DOTALL)
    if has_exec:
        txt = block.sub(lambda m: m.group(1), txt)
    else:
        txt = block.sub("", txt)
    txt = txt.replace("{workspace_subdir}", workspace_subdir)
    return txt.strip() + "\n"


def _build_group_system_prompt(
    config: dict[str, Any],
    files: dict[str, str],
    chat_ctx: dict[str, Any],
    current_model: str | None,
    is_root: bool,
) -> str:
    """Slim System-Prompt für Gruppenräume: kein SOUL/USER/Memory/Palace/Prefs/Room-Last-Fire.
    IDENTITY bleibt (wer der Bot ist), Sprache via language_override oder input-language."""
    force_lang = chat_ctx.get("language_override")
    identity_md = files.get("IDENTITY.md", "") or ""
    if force_lang or config.get("respond_in_input_language"):
        identity_md = _strip_language_from_identity(identity_md)
    tools_allow = chat_ctx.get("tools_allow") or []
    has_exec = "exec" in tools_allow
    sub = chat_ctx.get("workspace_subdir") or "default"
    group_header = _render_group_room_rule(has_exec=has_exec, workspace_subdir=sub)
    parts = [
        group_header,
        "## AGENTS (top-level contract)",
        files.get("AGENTS.md", ""),
        "",
        "## IDENTITY (your identity)",
        identity_md,
        "",
        "## Environment",
        _tools_umgebung_section(files.get("TOOLS.md", ""), config),
        "",
        _group_speaker_section(chat_ctx),
        _group_last_activity_section(config, chat_ctx),
        _group_prefs_section(config, chat_ctx),
        _language_section(config, identity_md, force_lang=force_lang),
        _knowledge_verification_section(
            has_search=("web_search" in tools_allow) and bool(config.get("search_engines")),
            slim=True,
        ),
        _quantities_section(),
        _group_system_runtime_section(),
        _safety_section(),
        _group_communication_boundary_section(),
        _exec_behavior_section() if has_exec else "",
        _group_persistence_section(has_exec),
        _group_planning_section(has_exec),
        _group_tools_section(config, chat_ctx),
        _group_invoke_model_section(config, chat_ctx),
        _group_docs_reference_section(config, chat_ctx, has_exec),
        "---\n*End of system instructions. Everything below is the conversation.*",
    ]
    return "\n".join(parts).strip()
