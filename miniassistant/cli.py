"""
CLI für MiniAssistant: config (geführte Konfiguration), chat, serve, …
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import click
from rich.console import Console
from miniassistant import __version__
from miniassistant.config import (
    get_config_dir,
    load_config,
    save_config,
    ensure_token,
    config_path,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_BIND_PORT,
    DEFAULT_MAX_CHARS_PER_FILE,
)

console = Console()


def _expand_path(s: str) -> str:
    return str(Path(s).expanduser().resolve())


@click.group()
@click.version_option(version=__version__, prog_name="MiniAssistant")
@click.option("--config-dir", envvar="MINIASSISTANT_CONFIG_DIR", default=None, help="Config-Verzeichnis (sonst ~/.config/miniassistant)")
@click.option("--project-dir", "-C", default=None, type=click.Path(exists=False, file_okay=False), help="Projektverzeichnis (dort miniassistant.yaml)")
@click.pass_context
def main(ctx: click.Context, config_dir: str | None, project_dir: str | None) -> None:
    if config_dir:
        os.environ["MINIASSISTANT_CONFIG_DIR"] = config_dir
    ctx.ensure_object(dict)
    ctx.obj["project_dir"] = project_dir


def _prompt_text(message: str, default: str = "", use_questionary: bool = True) -> str:
    """Texteingabe mit Pfeiltasten-Unterstützung (questionary), sonst click.prompt."""
    if use_questionary:
        try:
            import questionary
            out = questionary.text(message, default=default).ask()
            return (out or default).strip() if out is not None else default
        except Exception:
            pass
    return click.prompt(message, default=default, show_default=bool(default)).strip()


@main.command("config", help="Geführte Konfiguration (Config bearbeiten oder neu anlegen)")
@click.pass_context
def config_cmd(ctx: click.Context) -> None:
    """Schritt-für-Schritt Konfiguration; liest bestehende Config oder legt neue an, speichert und legt bei Bedarf Agent-Verzeichnis an."""
    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)
    path = config_path(project_dir)
    if path.exists():
        console.print(f"[bold]MiniAssistant – Konfiguration[/bold]")
        console.print(f"[yellow]Es existiert bereits eine Config unter {path}.[/yellow] Sie wird bearbeitet; bestehende Werte werden bei den Abfragen angezeigt.\n")
    else:
        console.print("[bold]MiniAssistant – Ersteinrichtung[/bold]\n")

    try:
        import questionary
        _use_q = True
    except ImportError:
        _use_q = False

    try:
        # Provider-Struktur initialisieren
        config["providers"] = config.get("providers") or {}
        config["providers"]["ollama"] = config["providers"].get("ollama") or {}
        prov = config["providers"]["ollama"]
        prov["type"] = prov.get("type", "ollama")

        # Ollama-URL
        ollama_base = prov.get("base_url") or DEFAULT_OLLAMA_BASE_URL
        base = _prompt_text("Ollama-URL (Host:Port)", default=ollama_base, use_questionary=_use_q)
        if "://" not in base:
            base = f"http://{base}"
        prov["base_url"] = base.rstrip("/")
        base_url = prov["base_url"]

        num_ctx_current = prov.get("num_ctx")
        num_ctx_default = str(num_ctx_current) if num_ctx_current is not None else "32768"
        num_ctx = _prompt_text("num_ctx (Context-Länge)", default=num_ctx_default, use_questionary=_use_q)
        prov["num_ctx"] = int(num_ctx) if num_ctx.strip() else None

        # Server (Bind + Token)
        server = config.get("server") or {}
        host = _prompt_text("Server-Bind (127.0.0.1 = nur localhost, 0.0.0.0 = alle)", default=server.get("host", "127.0.0.1"), use_questionary=_use_q)
        port = _prompt_text("Port", default=str(server.get("port", DEFAULT_BIND_PORT)), use_questionary=_use_q)
        config["server"] = config.get("server") or {}
        config["server"]["host"] = host
        config["server"]["port"] = int(port)
        # Token wird bei erstem Start generiert, wenn nicht gesetzt
        if not config["server"].get("token"):
            console.print("Token wird beim ersten Start automatisch generiert (oder in der Web-UI).")

        # Agent-Verzeichnis
        default_agent = config.get("agent_dir") or (str(Path(get_config_dir()).expanduser() / "agent") if not project_dir else str(Path(project_dir).resolve() / "agent"))
        agent_dir = _prompt_text("Agent-Verzeichnis (SOUL.md, IDENTITY.md, …)", default=default_agent, use_questionary=_use_q)
        config["agent_dir"] = _expand_path(agent_dir)

        # Modelle: von Ollama abfragen, Pfeiltasten-Auswahl, (Reasoning) anzeigen
        prov["models"] = prov.get("models") or {}
        models_cfg = prov["models"]
        models_cfg["aliases"] = models_cfg.get("aliases") or {}
        model_names: list[str] = []
        try:
            from miniassistant.ollama_client import list_models, model_supports_thinking
            raw = list_models(base_url)
            model_names = [m.get("name") or m.get("model") or "" for m in raw if (m.get("name") or m.get("model"))]
        except Exception as e:
            console.print(f"[yellow]Ollama-Modelle konnten nicht geladen werden:[/yellow] {e}")
        if model_names:
            # Anzeige mit (Reasoning) für Thinking-Modelle; Multi-Select (Checkbox) oder Pfeiltasten
            try:
                import questionary
                choices = []
                for name in model_names:
                    try:
                        is_reasoning = model_supports_thinking(base_url, name)
                    except Exception:
                        is_reasoning = False
                    label = f"{name} (Reasoning)" if is_reasoning else name
                    choices.append(label)
                # Multi-Select: Leerzeichen = an/ab, Enter = bestätigen; erstes gewähltes = Standard
                current_default = (models_cfg.get("default") or "").strip()
                default_choices = [c for c in choices if c.replace(" (Reasoning)", "").strip() == current_default] if current_default else []
                if not default_choices:
                    default_choices = choices[0:1]
                selected = questionary.checkbox(
                    "Modelle wählen (Leerzeichen an/ab, Enter = bestätigen; erstes = Standard-Modell):",
                    choices=choices,
                    default=default_choices,
                ).ask()
                if selected is None or not selected:
                    default_name = model_names[0]
                    models_cfg["default"] = default_name
                    models_cfg["list"] = None
                else:
                    chosen_names = [c.replace(" (Reasoning)", "").strip() for c in selected]
                    default_name = chosen_names[0]
                    models_cfg["default"] = default_name
                    models_cfg["list"] = chosen_names if len(chosen_names) > 1 else None
                    console.print(
                        f"Standard-Modell: {default_name}"
                        + (f" (Liste: {len(chosen_names)} Modelle)" if len(chosen_names) > 1 else "")
                    )
            except Exception:
                console.print("Verfügbare Modelle (Ollama):")
                for i, name in enumerate(model_names, 1):
                    console.print(f"  {i}. {name}")
                current_default = (models_cfg.get("default") or "").strip()
                fallback = current_default if current_default in model_names else model_names[0]
                choice = _prompt_text("Nummer(n) oder Name (mehrere mit Komma; erstes = Standard)", default=fallback, use_questionary=_use_q)
                chosen = [c.strip() for c in choice.split(",") if c.strip()]
                if not chosen:
                    chosen = [model_names[0]]
                resolved_names = []
                for c in chosen:
                    if c.isdigit() and 1 <= int(c) <= len(model_names):
                        resolved_names.append(model_names[int(c) - 1])
                    elif c in model_names:
                        resolved_names.append(c)
                    else:
                        resolved_names.append(c)
                models_cfg["default"] = resolved_names[0]
                models_cfg["list"] = resolved_names if len(resolved_names) > 1 else None
        else:
            current_model = (models_cfg.get("default") or "").strip()
            default_model = _prompt_text("Standard-Modell (Ollama-Name, z.B. llama3.2)", default=current_model or "", use_questionary=_use_q)
            models_cfg["default"] = default_model if default_model else None
            models_cfg["list"] = None

        # Auto-detect: think + model_options (num_ctx) aus Ollama-API für gewählte Modelle
        if model_names:
            try:
                from miniassistant.ollama_client import show_model, model_supports_thinking, model_supports_tools
                default_model_name = (models_cfg.get("default") or "").strip()
                chosen_list = models_cfg.get("list") or []
                all_chosen = [default_model_name] + [n for n in chosen_list if n]
                all_chosen = [n for n in dict.fromkeys(all_chosen) if n]  # deduplicate, keep order

                # think: aktivieren wenn Standard-Modell Reasoning unterstützt
                if default_model_name:
                    try:
                        if model_supports_thinking(base_url, default_model_name):
                            prov["think"] = True
                            console.print(f"  [dim]→ think: true (Standard-Modell {default_model_name} unterstützt Reasoning)[/dim]")
                        else:
                            prov["think"] = None
                    except Exception:
                        pass

                # model_options: num_ctx pro Modell aus /api/show auslesen
                model_options = dict(prov.get("model_options") or {})
                for mname in all_chosen:
                    try:
                        info = show_model(base_url, mname)
                        # num_ctx aus model_info oder parameters
                        mi = info.get("model_info") or {}
                        ctx = None
                        for key, val in mi.items():
                            if "context_length" in key and isinstance(val, (int, float)) and val > 0:
                                ctx = int(val)
                                break
                        if ctx and mname not in model_options:
                            model_options[mname] = {"num_ctx": ctx}
                            caps = []
                            if model_supports_thinking(base_url, mname):
                                caps.append("thinking")
                            if model_supports_tools(base_url, mname):
                                caps.append("tools")
                            cap_str = f" ({', '.join(caps)})" if caps else ""
                            console.print(f"  [dim]→ {mname}: num_ctx={ctx}{cap_str}[/dim]")
                    except Exception:
                        pass
                if model_options:
                    prov["model_options"] = model_options
            except Exception:
                pass

        # Optional: eine Suchmaschine (weitere z. B. VPN später per Config/save_config)
        engines = config.get("search_engines") or {}
        searxng_default = (engines.get("main") or {}).get("url") or ""
        searxng = _prompt_text("SearXNG-URL (nur Basis-URL, z.B. https://search.example.org – leer = kein Web-Search)", default=searxng_default, use_questionary=_use_q)
        if searxng.strip():
            config["search_engines"] = dict(config.get("search_engines") or {})
            config["search_engines"]["main"] = {"url": searxng.strip()}
            config["default_search_engine"] = config.get("default_search_engine") or "main"
        else:
            config["search_engines"] = {}
            config["default_search_engine"] = None

        # Vision-Modelle (optional, mehrere mit Komma)
        current_vision_list = config.get("vision") or []
        if isinstance(current_vision_list, list):
            current_vision_str = ", ".join(current_vision_list)
        elif isinstance(current_vision_list, dict):
            current_vision_str = (current_vision_list.get("model") or "")
        elif isinstance(current_vision_list, str):
            current_vision_str = current_vision_list
        else:
            current_vision_str = ""
        vision_input = _prompt_text(
            "Vision-Modelle (Komma-getrennt, z.B. google/gemini-2.5-flash, openai/gpt-4o, llava:13b – leer = keins)",
            default=current_vision_str, use_questionary=_use_q,
        )
        if vision_input.strip():
            vision_models = [m.strip() for m in vision_input.split(",") if m.strip()]
            config["vision"] = vision_models if vision_models else []
        else:
            config.pop("vision", None)

        # Image-Generation-Modelle (optional, mehrere mit Komma)
        current_img_list = config.get("image_generation") or []
        if isinstance(current_img_list, list):
            current_img_str = ", ".join(current_img_list)
        elif isinstance(current_img_list, dict):
            current_img_str = (current_img_list.get("model") or "")
        elif isinstance(current_img_list, str):
            current_img_str = current_img_list
        else:
            current_img_str = ""
        img_input = _prompt_text(
            "Bildgenerierungs-Modelle (Komma-getrennt, z.B. google/gemini-2.0-flash-exp, openai/dall-e-3 – leer = keins)",
            default=current_img_str, use_questionary=_use_q,
        )
        if img_input.strip():
            img_models = [m.strip() for m in img_input.split(",") if m.strip()]
            config["image_generation"] = img_models if img_models else []
        else:
            config.pop("image_generation", None)

        max_chars_str = _prompt_text("Max Zeichen pro Agent-Datei (SOUL.md, IDENTITY.md, … – begrenzt wie viel davon in den System-Prompt geladen wird)", default=str(config.get("max_chars_per_file", DEFAULT_MAX_CHARS_PER_FILE)), use_questionary=_use_q)
        try:
            config["max_chars_per_file"] = int(max_chars_str.strip()) if max_chars_str.strip() else DEFAULT_MAX_CHARS_PER_FILE
        except ValueError:
            config["max_chars_per_file"] = DEFAULT_MAX_CHARS_PER_FILE

        save_path = save_config(config, project_dir)
        console.print(f"\n[green]Config gespeichert:[/green] {save_path}")

        # Agent-Verzeichnis anlegen + leere Dateien
        agent_path = Path(config["agent_dir"])
        agent_path.mkdir(parents=True, exist_ok=True)
        for name in ("SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md"):
            f = agent_path / name
            if not f.exists():
                f.write_text(f"# {name}\n\n", encoding="utf-8")
                console.print(f"  Angelegt: {f}")
        console.print("\n[bold]Konfiguration gespeichert.[/bold] Nächste Schritte: [cyan]miniassistant chat[/cyan] oder [cyan]miniassistant serve[/cyan]")
    except KeyboardInterrupt:
        console.print("\n[yellow]Abgebrochen (Strg+C). Es wurde keine Config geschrieben.[/yellow]")
        return


@main.command("chat", help="Interaktiver Chat (CLI)")
@click.option("--model", "-m", default=None, help="Modell oder Alias (sonst aus Config)")
@click.option("--show-thinking/--no-show-thinking", default=True, help="Thinking-Ausgabe bei Reasoning-Modellen")
@click.pass_context
def chat(ctx: click.Context, model: str | None, show_thinking: bool) -> None:
    """Chat-Loop mit Ollama; /model MODELLNAME wechselt das Modell; exec und web_search als Tools."""
    from miniassistant.chat_loop import create_session, handle_user_input
    from miniassistant.ollama_client import resolve_model

    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)

    # Onboarding-Prüfung
    if not config.get("onboarding_complete", False):
        console.print("[bold yellow]⚠ Onboarding nicht abgeschlossen.[/bold yellow]")
        console.print("Bitte zuerst die Ersteinrichtung durchführen:")
        console.print("  • Web-UI: [cyan]miniassistant serve[/cyan] → Setup-Seite öffnen und speichern")
        console.print("  • CLI:    [cyan]miniassistant config[/cyan]")
        console.print()
        console.print("[dim]Der Chat ist erst nach dem Onboarding verfügbar.[/dim]")
        return

    session = create_session(config, project_dir)
    if model:
        session["model"] = resolve_model(config, model) or model
    console.print("[bold]MiniAssistant[/bold] – Chat. /model = aktuelles Modell, /model NAME = Wechsel, /models = Modellliste, /new = neue Session, [cyan]exit[/cyan] = Beenden.\n")
    console.print("Aktuelles Modell:", session.get("model") or "(keins)")

    while True:
        try:
            user_input = console.input("[bold blue]Du:[/bold blue] ")
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input.strip():
            continue
        if user_input.strip().lower() in ("exit", "quit", "q"):
            break
        result = handle_user_input(session, user_input)
        response, session = result[0], result[1]
        if response:
            if show_thinking and response.startswith("[Thinking]"):
                parts = response.split("\n\n", 1)
                if len(parts) >= 2:
                    console.print(parts[0], style="dim")
                    console.print(parts[1])
                else:
                    console.print(response)
            else:
                console.print("[bold green]Assistant:[/bold green]", response)


@main.command("serve", help="Web-UI und API starten")
@click.option("--host", default=None, help="Bind-Host (sonst aus Config)")
@click.option("--port", type=int, default=None, help="Port (sonst aus Config)")
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Startet FastAPI-Server (Web-UI + API); Token wird bei Bedarf generiert."""
    config = load_config(ctx.obj.get("project_dir"))
    h = host or config.get("server", {}).get("host", "127.0.0.1")
    p = port or config.get("server", {}).get("port", DEFAULT_BIND_PORT)
    token = ensure_token(config)
    # Klickbare URLs mit echten IPs erzeugen (0.0.0.0 ist nicht klickbar)
    display_hosts: list[str] = []
    if h in ("0.0.0.0", "::"):
        display_hosts.append("127.0.0.1")
        # hostname -I liefert zuverlaessig alle non-loopback IPs (Linux)
        try:
            import subprocess
            _ip_out = subprocess.run(
                ["hostname", "-I"], capture_output=True, text=True, timeout=2,
            )
            if _ip_out.returncode == 0:
                for _ip in _ip_out.stdout.strip().split():
                    if ":" not in _ip and _ip not in display_hosts:  # nur IPv4
                        display_hosts.append(_ip)
        except Exception:
            pass
        # Fallback: UDP-Connect-Trick (findet Default-Route-IP)
        if len(display_hosts) <= 1:
            try:
                import socket as _sock
                _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                _s.settimeout(0.1)
                _s.connect(("10.255.255.255", 1))
                _fallback_ip = _s.getsockname()[0]
                _s.close()
                if _fallback_ip and _fallback_ip not in display_hosts:
                    display_hosts.append(_fallback_ip)
            except Exception:
                pass
    else:
        display_hosts.append(h)

    console.print(f"Server: http://{h}:{p}")
    console.print("Token (für API/Web):", token)
    console.print("")
    console.print("[bold]Chat (klickbar):[/bold]")
    for dh in display_hosts:
        chat_url = f"http://{dh}:{p}/chat?token={token}"
        console.print(f"  {chat_url}")
    try:
        from miniassistant.debug_log import log_serve
        log_serve(f"serve started host={h} port={p}", config, ctx.obj.get("project_dir"))
    except Exception:
        pass
    try:
        from miniassistant.scheduler import start_scheduler_if_enabled
        if start_scheduler_if_enabled():
            console.print("Scheduler: [green]aktiv[/green]")
        else:
            console.print("Scheduler: [yellow]nicht verfuegbar[/yellow] (pip install apscheduler)")
    except Exception as e:
        console.print(f"Scheduler: [red]Fehler[/red] ({e})")
    # Chat-Clients Status anzeigen
    cc = config.get("chat_clients") or {}
    mc = cc.get("matrix") or config.get("matrix")
    dc = cc.get("discord")
    if mc and mc.get("enabled", True) and mc.get("token") and mc.get("user_id"):
        console.print("Matrix-Bot: [green]aktiv[/green]")
    if dc and dc.get("enabled", True) and dc.get("bot_token"):
        console.print("Discord-Bot: [green]aktiv[/green]")
    from miniassistant.web.app import app
    # Projektverzeichnis für Sessions (Config/Memory/Agent aus diesem Ordner bei -C)
    app.state.project_dir = ctx.obj.get("project_dir")
    import uvicorn
    # Log-Polling von /api/logs/ aus dem Access-Log filtern (Noise bei Live-Log-Viewer)
    class _LogPollFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "/api/logs/" in msg and "GET" in msg:
                return False
            return True
    logging.getLogger("uvicorn.access").addFilter(_LogPollFilter())
    uvicorn.run(app, host=h, port=p, log_level="info")


@main.command("token", help="Token anzeigen oder neu generieren")
@click.option("--regenerate", is_flag=True, help="Neuen Token generieren und speichern")
@click.pass_context
def token_cmd(ctx: click.Context, regenerate: bool) -> None:
    config = load_config(ctx.obj.get("project_dir"))
    if regenerate:
        import secrets
        config.setdefault("server", {})["token"] = secrets.token_urlsafe(32)
        save_config(config, ctx.obj.get("project_dir"))
        console.print("Neuer Token generiert und gespeichert.")
    t = ensure_token(config)
    console.print("Token:", t)


@main.command("matrix-e2ee-check", help="Prüfen, ob Matrix-E2EE (Entschlüsselung) in dieser Umgebung verfügbar ist")
def matrix_e2ee_check() -> None:
    """Zeigt an, ob der Bot verschlüsselte Matrix-Nachrichten entschlüsseln kann."""
    try:
        from nio.crypto import ENCRYPTION_ENABLED
    except ImportError:
        console.print("[red]Matrix-E2EE: nicht verfügbar[/red] (matrix-nio oder python-olm nicht installiert)")
        console.print("  → pip install matrix-nio[e2e] (benötigt libolm, z. B. apt install libolm-dev)")
        return
    if ENCRYPTION_ENABLED:
        console.print("[green]Matrix-E2EE: aktiv[/green] – Bot kann verschlüsselte Nachrichten entschlüsseln.")
    else:
        console.print("[red]Matrix-E2EE: nicht verfügbar[/red] (python-olm/libolm fehlt)")
        console.print("  → libolm installieren: apt install libolm-dev")
        console.print("  → danach: pip install matrix-nio[e2e]")


def _fetch_models_for_provider(prov_name: str, prov_cfg: dict, timeout: int = 5) -> list[str]:
    """Holt Modelle von einem Provider (Ollama, Google, OpenAI, Anthropic, Claude Code). Timeout in Sekunden."""
    import httpx as _httpx
    prov_type = str(prov_cfg.get("type", "ollama")).lower().strip()
    if prov_type == "google":
        from miniassistant.google_client import api_list_models as google_list
        api_key = prov_cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError("api_key fehlt")
        base_url = prov_cfg.get("base_url", "https://generativelanguage.googleapis.com")
        models = google_list(api_key, base_url=base_url)
        return [m.get("name", "") for m in models if m.get("name")]
    elif prov_type in ("openai", "deepseek"):
        from miniassistant.openai_client import api_list_models as openai_list
        api_key = prov_cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError("api_key fehlt")
        _default_url = "https://api.deepseek.com" if prov_type == "deepseek" else "https://api.openai.com"
        base_url = prov_cfg.get("base_url", _default_url)
        models = openai_list(api_key, base_url=base_url)
        return [m.get("name", "") for m in models if m.get("name")]
    elif prov_type == "anthropic":
        from miniassistant.claude_client import api_list_models, ANTHROPIC_API_URL, _api_headers
        api_key = prov_cfg.get("api_key", "")
        if not api_key:
            raise RuntimeError("api_key fehlt")
        base_url = prov_cfg.get("base_url", ANTHROPIC_API_URL)
        url = f"{base_url.rstrip('/')}/v1/models"
        r = _httpx.get(url, headers=_api_headers(api_key), timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
    elif prov_type == "claude-code":
        from miniassistant.claude_client import cli_list_models
        return cli_list_models()
    else:
        # Ollama
        base_url = prov_cfg.get("base_url", "http://127.0.0.1:11434")
        api_key = prov_cfg.get("api_key")
        url = f"{base_url.rstrip('/')}/api/tags"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        r = _httpx.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        raw = r.json().get("models") or []
        return [m.get("name") or m.get("model") or "" for m in raw if (m.get("name") or m.get("model"))]


@main.command("models", help="Modelle anzeigen und verwalten (alle Provider)")
@click.option("--provider", "-p", default=None, help="Nur Modelle eines bestimmten Providers")
@click.option("--online", is_flag=True, help="Verfügbare Modelle vom Provider abrufen")
@click.option("--set-default", default=None, help="Standard-Modell setzen")
@click.pass_context
def models_cmd(ctx: click.Context, provider: str | None, online: bool, set_default: str | None) -> None:
    """Modelle anzeigen, verwalten und wechseln — Provider-übergreifend."""
    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)
    providers = config.get("providers") or {}

    if not providers:
        console.print("[yellow]Keine Provider konfiguriert. Zuerst:[/yellow] [cyan]miniassistant config[/cyan]")
        return

    if set_default:
        # Standard-Modell global setzen (im ersten Provider)
        from miniassistant.ollama_client import _find_provider
        if "/" in set_default:
            prov_prefix, model_name = set_default.split("/", 1)
            real_key = _find_provider(providers, prov_prefix)
            if real_key:
                providers[real_key].setdefault("models", {})["default"] = model_name
                save_config(config, project_dir)
                console.print(f"[green]Standard-Modell für {real_key}: {model_name}[/green]")
                return
        # Kein Prefix → Default-Provider
        default_prov = next(iter(providers), "ollama")
        providers[default_prov].setdefault("models", {})["default"] = set_default
        save_config(config, project_dir)
        console.print(f"[green]Standard-Modell: {set_default}[/green]")
        return

    # Modelle anzeigen
    _valid_provs = [k for k in providers if k and isinstance(k, str) and isinstance(providers[k], dict)]
    default_prov = _valid_provs[0] if _valid_provs else "ollama"
    for prov_name, prov_cfg in providers.items():
        if not prov_name or not isinstance(prov_name, str) or not isinstance(prov_cfg, dict):
            continue
        if provider:
            from miniassistant.ollama_client import _find_provider
            if _find_provider(providers, provider) != prov_name:
                continue
        prov_type = str(prov_cfg.get("type", "ollama")).lower()
        models_cfg = prov_cfg.get("models") or {}
        default_model = models_cfg.get("default") or "(keins)"
        aliases = models_cfg.get("aliases") or {}
        model_list = models_cfg.get("list") or []

        is_default = " [dim](Standard-Provider)[/dim]" if prov_name == default_prov else ""
        console.print(f"\n[bold]{prov_name}[/bold] ({prov_type}){is_default}")
        console.print(f"  Standard: [cyan]{default_model}[/cyan]")
        if aliases:
            for a, t in aliases.items():
                prefix = f"{prov_name}/" if prov_name != default_prov else ""
                console.print(f"  Alias: [green]{prefix}{a}[/green] → {t}")
        if model_list:
            for m in model_list:
                console.print(f"  Modell: {m}")

    # Online-Modelle wenn gewünscht
    if online:
        console.print()
        for prov_name, prov_cfg in providers.items():
            if not prov_name or not isinstance(prov_name, str) or not isinstance(prov_cfg, dict):
                continue
            if provider:
                from miniassistant.ollama_client import _find_provider
                if _find_provider(providers, provider) != prov_name:
                    continue
            prov_type = str(prov_cfg.get("type", "ollama")).lower()
            try:
                names = _fetch_models_for_provider(prov_name, prov_cfg)
                if names:
                    console.print(f"[green]{prov_name} ({prov_type}): {len(names)} online verfügbar[/green]")
                    for n in names:
                        console.print(f"  {n}")
                else:
                    console.print(f"[yellow]{prov_name}: Keine Modelle gefunden[/yellow]")
            except Exception as e:
                console.print(f"[red]{prov_name}: Fehler – {e}[/red]")
        return

    # Interaktiver Modus wenn keine Flags und questionary vorhanden
    if not provider and not set_default:
        try:
            import questionary
        except ImportError:
            console.print("\n[dim]Tipp: pip install questionary für interaktiven Modus[/dim]")
            console.print("Optionen: --online, --set-default MODELL, --provider NAME")
            return

        while True:
            console.print()
            action = questionary.select(
                "Was möchtest du tun?",
                choices=[
                    "Provider hinzufügen",
                    "Provider-Modelle verwalten",
                    "Standard-Modell wechseln",
                    "Modell hinzufügen",
                    "Modell entfernen",
                    "Fertig",
                ],
            ).ask()
            if not action or action == "Fertig":
                return

            if action == "Standard-Modell wechseln":
                choices: list[str] = []
                for pn, pc in providers.items():
                    if not isinstance(pc, dict):
                        continue
                    mc = pc.get("models") or {}
                    prefix = f"{pn}/" if pn != default_prov else ""
                    for a in (mc.get("aliases") or {}):
                        choices.append(f"{prefix}{a}")
                    for m in (mc.get("list") or []):
                        choices.append(f"{prefix}{m}")
                    if mc.get("default"):
                        entry = f"{prefix}{mc['default']}"
                        if entry not in choices:
                            choices.insert(0, entry)
                if not choices:
                    m_name = questionary.text("Modellname eingeben:").ask()
                else:
                    m_name = questionary.select("Standard-Modell wählen:", choices=choices).ask()
                if m_name and m_name.strip():
                    m = m_name.strip()
                    if "/" in m:
                        prov_prefix, model_part = m.split("/", 1)
                        from miniassistant.ollama_client import _find_provider
                        rk = _find_provider(providers, prov_prefix)
                        if rk:
                            providers[rk].setdefault("models", {})["default"] = model_part
                    else:
                        providers[default_prov].setdefault("models", {})["default"] = m
                    save_config(config, project_dir)
                    console.print(f"[green]Standard-Modell: {m}[/green]")

            elif action == "Modell hinzufügen":
                prov_choices = [pn for pn in providers if pn and isinstance(pn, str) and isinstance(providers[pn], dict)]
                if not prov_choices:
                    console.print("[yellow]Keine gültigen Provider.[/yellow]")
                    continue
                if len(prov_choices) == 1:
                    target_prov = prov_choices[0]
                else:
                    target_prov = questionary.select("Zu welchem Provider?", choices=prov_choices).ask()
                if not target_prov:
                    continue
                how = questionary.select(
                    "Wie?",
                    choices=["Manuell eingeben", "Online abrufen und auswählen"],
                ).ask()
                if not how:
                    continue
                target_cfg = providers[target_prov]
                target_models = target_cfg.setdefault("models", {"default": None, "aliases": {}, "list": None})
                if how.startswith("Online"):
                    try:
                        names = _fetch_models_for_provider(target_prov, target_cfg)
                    except Exception as e:
                        console.print(f"[red]{target_prov}: Fehler – {e}[/red]")
                        continue
                    if not names:
                        console.print("[yellow]Keine Modelle gefunden.[/yellow]")
                        continue
                    selected = questionary.checkbox(
                        f"{len(names)} Modelle. Auswählen (Leerzeichen = an/ab, Enter = bestätigen):",
                        choices=names,
                    ).ask()
                    if selected:
                        current_list = list(target_models.get("list") or [])
                        for s in selected:
                            if s not in current_list:
                                current_list.append(s)
                        target_models["list"] = current_list if current_list else None
                        if not target_models.get("default") and selected:
                            target_models["default"] = selected[0]
                        save_config(config, project_dir)
                        console.print(f"[green]{len(selected)} Modell(e) zu {target_prov} hinzugefügt.[/green]")
                else:
                    m_name = questionary.text("Modellname:").ask()
                    if m_name and m_name.strip():
                        current_list = list(target_models.get("list") or [])
                        m_name = m_name.strip()
                        if m_name not in current_list:
                            current_list.append(m_name)
                            target_models["list"] = current_list
                            if not target_models.get("default"):
                                target_models["default"] = m_name
                            save_config(config, project_dir)
                            console.print(f"[green]Modell '{m_name}' zu {target_prov} hinzugefügt.[/green]")
                        else:
                            console.print(f"[yellow]'{m_name}' ist bereits konfiguriert.[/yellow]")

            elif action == "Modell entfernen":
                # Alle Modelle sammeln
                all_models: list[tuple[str, str]] = []  # (display, prov_name)
                for pn, pc in providers.items():
                    if not isinstance(pc, dict):
                        continue
                    prefix = f"{pn}/" if pn != default_prov else ""
                    for m in (pc.get("models") or {}).get("list") or []:
                        all_models.append((f"{prefix}{m}", pn))
                if not all_models:
                    console.print("[yellow]Keine Modelle zum Entfernen.[/yellow]")
                    continue
                to_remove = questionary.checkbox(
                    "Modelle zum Entfernen:", choices=[d for d, _ in all_models],
                ).ask()
                if to_remove:
                    for display in to_remove:
                        for d, pn in all_models:
                            if d == display:
                                mc = providers[pn].get("models") or {}
                                ml = list(mc.get("list") or [])
                                # Modellname ohne Prefix extrahieren
                                raw_name = display.split("/", 1)[-1] if "/" in display else display
                                if raw_name in ml:
                                    ml.remove(raw_name)
                                    mc["list"] = ml or None
                                    if mc.get("default") == raw_name:
                                        mc["default"] = ml[0] if ml else None
                                break
                    save_config(config, project_dir)
                    console.print(f"[green]{len(to_remove)} Modell(e) entfernt.[/green]")

            elif action == "Provider hinzufügen":
                console.print(f"[dim]→ miniassistant providers add[/dim]")
                ctx.invoke(providers_add)
                # Config neu laden nach Provider-Änderung
                config = load_config(project_dir)
                providers = config.get("providers") or {}
                default_prov = next(iter(providers), "ollama")

            elif action == "Provider-Modelle verwalten":
                prov_choices = [pn for pn in providers if pn and isinstance(pn, str) and isinstance(providers[pn], dict)]
                if len(prov_choices) == 1:
                    chosen = prov_choices[0]
                else:
                    chosen = questionary.select("Provider wählen:", choices=prov_choices).ask()
                if chosen:
                    console.print(f"[dim]→ miniassistant providers models {chosen}[/dim]")
                    ctx.invoke(providers_models, provider_name=chosen)
                    # Config neu laden
                    config = load_config(project_dir)
                    providers = config.get("providers") or {}
                    default_prov = next(iter(providers), "ollama")


@main.group("providers", help="Provider verwalten (hinzufügen, bearbeiten, löschen, Modelle konfigurieren)")
@click.pass_context
def providers_cmd(ctx: click.Context) -> None:
    pass


@providers_cmd.command("list", help="Alle konfigurierten Provider anzeigen")
@click.pass_context
def providers_list(ctx: click.Context) -> None:
    config = load_config(ctx.obj.get("project_dir"))
    providers = config.get("providers") or {}
    if not providers:
        console.print("[yellow]Keine Provider konfiguriert.[/yellow]")
        return
    default_name = next(iter(providers), "")
    for name, prov in providers.items():
        if not isinstance(prov, dict):
            continue
        is_default = " [green](default)[/green]" if name == default_name else ""
        prov_type = prov.get("type", "ollama")
        base_url = prov.get("base_url", "")
        has_key = " 🔑" if prov.get("api_key") else ""
        console.print(f"  [bold]{name}[/bold]{is_default} — type={prov_type} url={base_url}{has_key}")
        models = prov.get("models") or {}
        default_model = models.get("default") or ""
        aliases = models.get("aliases") or {}
        model_list = models.get("list") or []
        if default_model:
            console.print(f"    Standard: {default_model}")
        if aliases:
            for alias, target in aliases.items():
                console.print(f"    Alias: {alias} → {target}")
        if model_list:
            console.print(f"    Modelle: {', '.join(model_list)}")


@providers_cmd.command("add", help="Neuen Provider hinzufügen")
@click.argument("name", required=False, default="")
@click.pass_context
def providers_add(ctx: click.Context, name: str) -> None:
    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)
    providers = config.setdefault("providers", {})
    # name kann None sein wenn via ctx.invoke aufgerufen
    if name is None:
        name = ""

    try:
        import questionary
        _use_q = True
    except ImportError:
        _use_q = False

    # 1) Provider-Typ auswählen (mit Presets)
    type_choices = [
        "Ollama – lokal (http://127.0.0.1:11434)",
        "Ollama – Online (https://ollama.com, API-Key nötig)",
        "Ollama – eigener Server (URL eingeben)",
        "Google Gemini API (Gemini, API-Key nötig)",
        "OpenAI API (GPT-4o, DALL-E, API-Key nötig)",
        "DeepSeek API (DeepSeek-V3/R1, API-Key nötig)",
        "Anthropic API (Claude, API-Key nötig)",
    ]
    # Claude Code nur anzeigen wenn installiert
    try:
        from miniassistant.claude_client import cli_find_binary
        _claude_bin = cli_find_binary()
        if _claude_bin:
            import shutil as _shutil
            _in_path = bool(_shutil.which("claude"))
            _path_info = " (installiert)" if _in_path else f" ({_claude_bin})"
            type_choices.append(f"Claude Code CLI{_path_info}")
        else:
            type_choices.append("Claude Code CLI (nicht installiert – überspringen)")
    except Exception:
        pass

    if _use_q:
        type_choice = questionary.select("Provider-Typ:", choices=type_choices).ask()
    else:
        console.print("Provider-Typen: 1=Ollama lokal, 2=Ollama Online, 3=Ollama Server, 4=Google Gemini, 5=OpenAI, 6=DeepSeek, 7=Anthropic, 8=Claude Code")
        tc = _prompt_text("Typ (1-8)", default="1", use_questionary=False)
        idx = int(tc) - 1 if tc.strip().isdigit() else 0
        type_choice = type_choices[idx] if 0 <= idx < len(type_choices) else type_choices[0]

    if not type_choice or "nicht installiert" in type_choice:
        if "nicht installiert" in (type_choice or ""):
            console.print("[yellow]Claude Code CLI nicht gefunden.[/yellow] Installieren: npm install -g @anthropic-ai/claude-code && claude login")
        return

    # Preset bestimmen
    if "lokal" in type_choice:
        prov_type, preset = "ollama", "local"
    elif "Online" in type_choice:
        prov_type, preset = "ollama", "online"
    elif "eigener" in type_choice or "Server" in type_choice:
        prov_type, preset = "ollama", "custom"
    elif "Google" in type_choice or "Gemini" in type_choice:
        prov_type, preset = "google", "google"
    elif "OpenAI" in type_choice or "GPT" in type_choice:
        prov_type, preset = "openai", "openai"
    elif "DeepSeek" in type_choice:
        prov_type, preset = "deepseek", "deepseek"
    elif "Anthropic" in type_choice:
        prov_type, preset = "anthropic", "anthropic"
    elif "Claude Code" in type_choice:
        prov_type, preset = "claude-code", "claude-code"
    else:
        prov_type, preset = "ollama", "local"

    # 2) Name
    if not name:
        default_names = {
            "local": "ollama", "online": "ollama-online", "custom": "ollama2",
            "google": "google", "openai": "openai", "deepseek": "deepseek", "anthropic": "anthropic", "claude-code": "claude",
        }
        default_name = default_names.get(preset, "provider")
        while default_name in providers:
            default_name += "2"
        name = _prompt_text("Provider-Name", default=default_name, use_questionary=_use_q)
        if not name or not name.strip():
            return
        name = name.strip()

    if name in providers:
        console.print(f"[red]Provider '{name}' existiert bereits.[/red] Nutze 'providers edit {name}'.")
        return

    # 3) Typ-spezifische Felder mit Presets
    prov_cfg: dict = {"type": prov_type, "models": {"default": None, "aliases": {}, "list": None}}

    if preset == "local":
        # Ollama lokal — sinnvolle Defaults
        base_url = _prompt_text("Base-URL", default="http://127.0.0.1:11434", use_questionary=_use_q)
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        prov_cfg["base_url"] = base_url.rstrip("/")
        num_ctx = _prompt_text("num_ctx (Context-Länge, 0=Server-Default)", default="32768", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)

    elif preset == "online":
        # Ollama Online — HTTPS, API-Key Pflicht
        prov_cfg["base_url"] = "https://ollama.com"
        console.print(f"[dim]Base-URL: https://ollama.com[/dim]")
        api_key = _prompt_text("Ollama Online API-Key", default="", use_questionary=_use_q)
        if not api_key.strip():
            console.print("[red]API-Key ist erforderlich für Ollama Online.[/red]")
            return
        prov_cfg["api_key"] = api_key.strip()
        num_ctx = _prompt_text("num_ctx (Context-Länge)", default="131072", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)
        else:
            prov_cfg["num_ctx"] = 131072

    elif preset == "custom":
        # Ollama eigener Server — alles konfigurierbar
        base_url = _prompt_text("Base-URL (z.B. http://192.168.1.100:11434)", default="http://", use_questionary=_use_q)
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        prov_cfg["base_url"] = base_url.rstrip("/")
        api_key = _prompt_text("API-Key (leer = keiner)", default="", use_questionary=_use_q)
        if api_key.strip():
            prov_cfg["api_key"] = api_key.strip()
        num_ctx = _prompt_text("num_ctx (Context-Länge, 0=Server-Default)", default="32768", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)

    elif preset == "google":
        # Google Gemini API — URL fest, API-Key Pflicht
        prov_cfg["base_url"] = "https://generativelanguage.googleapis.com"
        console.print(f"[dim]Base-URL: https://generativelanguage.googleapis.com[/dim]")
        api_key = _prompt_text("Google API-Key (AIza...)", default="", use_questionary=_use_q)
        if not api_key.strip():
            console.print("[red]API-Key ist erforderlich für Google Gemini.[/red]")
            console.print("[dim]API-Key erstellen: https://aistudio.google.com/apikey[/dim]")
            return
        prov_cfg["api_key"] = api_key.strip()
        num_ctx = _prompt_text("num_ctx (Context-Länge, Gemini max ~1M)", default="1000000", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)
        else:
            prov_cfg["num_ctx"] = 1000000
        if _use_q:
            think = questionary.confirm("Thinking aktivieren? (nur Gemini 2.5+)", default=False).ask()
        else:
            think = _prompt_text("Thinking aktivieren? (j/n, nur Gemini 2.5+)", default="n", use_questionary=False).lower().startswith("j")
        if think:
            prov_cfg["think"] = True

    elif preset == "openai":
        # OpenAI API — URL fest, API-Key Pflicht
        prov_cfg["base_url"] = "https://api.openai.com"
        console.print(f"[dim]Base-URL: https://api.openai.com[/dim]")
        console.print("[dim]Auch kompatibel mit OpenAI-kompatiblen APIs (Together, Groq, etc.) – base_url nachträglich ändern.[/dim]")
        api_key = _prompt_text("OpenAI API-Key (sk-...)", default="", use_questionary=_use_q)
        if not api_key.strip():
            console.print("[red]API-Key ist erforderlich für OpenAI.[/red]")
            console.print("[dim]API-Key erstellen: https://platform.openai.com/api-keys[/dim]")
            return
        prov_cfg["api_key"] = api_key.strip()
        num_ctx = _prompt_text("num_ctx (Context-Länge, GPT-4o max ~128k)", default="128000", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)
        else:
            prov_cfg["num_ctx"] = 128000
        if _use_q:
            think = questionary.confirm("Reasoning aktivieren? (nur o1/o3/o4-mini)", default=False).ask()
        else:
            think = _prompt_text("Reasoning aktivieren? (j/n, nur o1/o3/o4-mini)", default="n", use_questionary=False).lower().startswith("j")
        if think:
            prov_cfg["think"] = True

    elif preset == "deepseek":
        # DeepSeek API — OpenAI-kompatibel, eigene base_url
        prov_cfg["base_url"] = "https://api.deepseek.com"
        console.print(f"[dim]Base-URL: https://api.deepseek.com[/dim]")
        api_key = _prompt_text("DeepSeek API-Key (sk-...)", default="", use_questionary=_use_q)
        if not api_key.strip():
            console.print("[red]API-Key ist erforderlich für DeepSeek.[/red]")
            console.print("[dim]API-Key erstellen: https://platform.deepseek.com/api_keys[/dim]")
            return
        prov_cfg["api_key"] = api_key.strip()
        num_ctx = _prompt_text("num_ctx (Context-Länge, DeepSeek-V3/R1 max ~64k)", default="65536", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)
        else:
            prov_cfg["num_ctx"] = 65536
        if _use_q:
            think = questionary.confirm("Thinking aktivieren? (nur DeepSeek-R1)", default=False).ask()
        else:
            think = _prompt_text("Thinking aktivieren? (j/n, nur DeepSeek-R1)", default="n", use_questionary=False).lower().startswith("j")
        if think:
            prov_cfg["think"] = True

    elif preset == "anthropic":
        # Anthropic API — URL fest, num_ctx=200000 (Claude Context-Window)
        prov_cfg["base_url"] = "https://api.anthropic.com"
        console.print(f"[dim]Base-URL: https://api.anthropic.com[/dim]")
        api_key = _prompt_text("Anthropic API-Key (sk-ant-...)", default="", use_questionary=_use_q)
        if not api_key.strip():
            console.print("[red]API-Key ist erforderlich für Anthropic.[/red]")
            return
        prov_cfg["api_key"] = api_key.strip()
        num_ctx = _prompt_text("num_ctx (Context-Länge, Claude max ~200k)", default="200000", use_questionary=_use_q)
        if num_ctx.strip() and num_ctx.strip() != "0":
            prov_cfg["num_ctx"] = int(num_ctx)
        else:
            prov_cfg["num_ctx"] = 200000
        if _use_q:
            think = questionary.confirm("Extended Thinking aktivieren?", default=True).ask()
        else:
            think = _prompt_text("Extended Thinking? (j/n)", default="j", use_questionary=False).lower().startswith("j")
        if think:
            prov_cfg["think"] = True

    elif preset == "claude-code":
        console.print("[dim]Claude Code nutzt eigene Auth (claude login). Kein API-Key nötig.[/dim]")

    providers[name] = prov_cfg

    # 4) Modelle laden / konfigurieren
    try:
        model_names = _fetch_models_for_provider(name, prov_cfg)
        if model_names:
            console.print(f"[green]{len(model_names)} Modelle verfügbar.[/green]")
            if _use_q:
                selected = questionary.checkbox(
                    "Modelle wählen (Leerzeichen = an/ab, Enter = bestätigen):",
                    choices=model_names,
                ).ask()
                custom = questionary.text(
                    "Weiteres Modell manuell eingeben (leer = überspringen):",
                    default="",
                ).ask()
                if custom and custom.strip():
                    selected = (selected or []) + [custom.strip()]
                if selected:
                    prov_cfg["models"]["default"] = selected[0]
                    prov_cfg["models"]["list"] = selected if len(selected) > 1 else None
            else:
                for i, m in enumerate(model_names[:10], 1):
                    console.print(f"  {i}. {m}")
                choice = _prompt_text("Nummer(n) oder Name (Komma-getrennt)", default=model_names[0], use_questionary=False)
                chosen = [c.strip() for c in choice.split(",") if c.strip()]
                if chosen:
                    resolved = []
                    for c in chosen:
                        if c.isdigit() and 1 <= int(c) <= len(model_names):
                            resolved.append(model_names[int(c) - 1])
                        else:
                            resolved.append(c)
                    prov_cfg["models"]["default"] = resolved[0]
                    prov_cfg["models"]["list"] = resolved if len(resolved) > 1 else None
        else:
            console.print("[yellow]Keine Modelle gefunden.[/yellow]")
            default_model = _prompt_text("Standard-Modell (manuell)", default="", use_questionary=_use_q)
            if default_model.strip():
                prov_cfg["models"]["default"] = default_model.strip()
    except Exception as e:
        console.print(f"[yellow]Modelle nicht abrufbar:[/yellow] {e}")
        default_model = _prompt_text("Standard-Modell (manuell)", default="", use_questionary=_use_q)
        if default_model.strip():
            prov_cfg["models"]["default"] = default_model.strip()

    if not prov_cfg.get("models", {}).get("default"):
        if _use_q:
            import questionary as _q
            confirmed = _q.confirm(
                f"Kein Standard-Modell gesetzt. Provider '{name}' trotzdem speichern?",
                default=False,
            ).ask()
        else:
            confirmed = _prompt_text(
                "Kein Standard-Modell gesetzt. Trotzdem speichern? (j/n)",
                default="n",
                use_questionary=False,
            ).lower().startswith("j")
        if not confirmed:
            console.print("[yellow]Abgebrochen. Provider nicht gespeichert.[/yellow]")
            return

    save_config(config, project_dir)
    console.print(f"[green]Provider '{name}' ({prov_type}) hinzugefügt.[/green]")


@providers_cmd.command("edit", help="Provider bearbeiten")
@click.argument("name")
@click.pass_context
def providers_edit(ctx: click.Context, name: str) -> None:
    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)
    providers = config.get("providers") or {}
    # Case-insensitive lookup
    from miniassistant.ollama_client import _find_provider
    real_name = _find_provider(providers, name)
    if not real_name:
        console.print(f"[red]Provider '{name}' nicht gefunden.[/red] Verfügbar: {', '.join(providers.keys())}")
        return
    prov = providers[real_name]
    try:
        import questionary
        _use_q = True
    except ImportError:
        _use_q = False
    console.print(f"[bold]Provider '{real_name}' bearbeiten[/bold] (Enter = Wert behalten)")
    prov["type"] = _prompt_text("Typ", default=prov.get("type", "ollama"), use_questionary=_use_q)
    new_url = _prompt_text("Base-URL", default=prov.get("base_url", ""), use_questionary=_use_q)
    if new_url and "://" not in new_url:
        new_url = f"http://{new_url}"
    prov["base_url"] = new_url.rstrip("/") if new_url else prov.get("base_url")
    current_key = prov.get("api_key") or ""
    key_display = f"{current_key[:8]}..." if current_key else ""
    new_key = _prompt_text(f"API-Key (aktuell: {key_display or 'keiner'}, leer = behalten)", default="", use_questionary=_use_q)
    if new_key.strip():
        prov["api_key"] = new_key.strip()
    num_ctx = _prompt_text("num_ctx", default=str(prov.get("num_ctx") or ""), use_questionary=_use_q)
    prov["num_ctx"] = int(num_ctx) if num_ctx.strip() else prov.get("num_ctx")
    save_config(config, project_dir)
    console.print(f"[green]Provider '{real_name}' gespeichert.[/green]")


@providers_cmd.command("delete", help="Provider löschen")
@click.argument("name")
@click.pass_context
def providers_delete(ctx: click.Context, name: str) -> None:
    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)
    providers = config.get("providers") or {}
    from miniassistant.ollama_client import _find_provider
    real_name = _find_provider(providers, name)
    if not real_name:
        console.print(f"[red]Provider '{name}' nicht gefunden.[/red]")
        return
    if len(providers) <= 1:
        console.print("[red]Kann den letzten Provider nicht löschen.[/red]")
        return
    if not click.confirm(f"Provider '{real_name}' wirklich löschen?"):
        return
    del providers[real_name]
    save_config(config, project_dir)
    console.print(f"[green]Provider '{real_name}' gelöscht.[/green]")


@providers_cmd.command("models", help="Modelle eines Providers verwalten")
@click.argument("provider_name")
@click.option("--add", "add_model", default=None, help="Modell hinzufügen")
@click.option("--remove", "remove_model", default=None, help="Modell entfernen")
@click.option("--default", "set_default", default=None, help="Standard-Modell setzen")
@click.option("--alias", nargs=2, type=str, default=None, help="Alias setzen: --alias ALIAS MODELL")
@click.option("--remove-alias", default=None, help="Alias entfernen")
@click.option("--online", is_flag=True, help="Verfügbare Modelle vom Provider abrufen")
@click.pass_context
def providers_models(ctx: click.Context, provider_name: str, add_model: str | None,
                     remove_model: str | None, set_default: str | None,
                     alias: tuple[str, str] | None, remove_alias: str | None,
                     online: bool) -> None:
    project_dir = ctx.obj.get("project_dir")
    config = load_config(project_dir)
    providers = config.get("providers") or {}
    from miniassistant.ollama_client import _find_provider
    real_name = _find_provider(providers, provider_name)
    if not real_name:
        console.print(f"[red]Provider '{provider_name}' nicht gefunden.[/red]")
        return
    prov = providers[real_name]
    models = prov.setdefault("models", {"default": None, "aliases": {}, "list": None})
    changed = False

    if online:
        prov_type = str(prov.get("type", "ollama")).lower().strip()
        try:
            names = _fetch_models_for_provider(real_name, prov)
            if names:
                console.print(f"[green]{len(names)} Modelle verfügbar bei {real_name} ({prov_type}):[/green]")
                for n in names:
                    console.print(f"  {n}")
            else:
                console.print("[yellow]Keine Modelle gefunden.[/yellow]")
        except Exception as e:
            console.print(f"[red]{real_name}: Fehler – {e}[/red]")
        return

    if add_model:
        current_list = list(models.get("list") or [])
        if add_model not in current_list:
            current_list.append(add_model)
            models["list"] = current_list
            if not models.get("default"):
                models["default"] = add_model
            changed = True
            console.print(f"[green]Modell '{add_model}' hinzugefügt.[/green]")
        else:
            console.print(f"[yellow]Modell '{add_model}' ist bereits konfiguriert.[/yellow]")

    if remove_model:
        current_list = list(models.get("list") or [])
        if remove_model in current_list:
            current_list.remove(remove_model)
            models["list"] = current_list or None
            if models.get("default") == remove_model:
                models["default"] = current_list[0] if current_list else None
            changed = True
            console.print(f"[green]Modell '{remove_model}' entfernt.[/green]")
        else:
            console.print(f"[yellow]Modell '{remove_model}' nicht in der Liste.[/yellow]")

    if set_default:
        models["default"] = set_default
        changed = True
        console.print(f"[green]Standard-Modell: {set_default}[/green]")

    if alias:
        alias_name, alias_target = alias
        models.setdefault("aliases", {})[alias_name] = alias_target
        changed = True
        console.print(f"[green]Alias: {alias_name} → {alias_target}[/green]")

    if remove_alias:
        aliases = models.get("aliases") or {}
        if remove_alias in aliases:
            del aliases[remove_alias]
            changed = True
            console.print(f"[green]Alias '{remove_alias}' entfernt.[/green]")
        else:
            console.print(f"[yellow]Alias '{remove_alias}' nicht gefunden.[/yellow]")

    if changed:
        save_config(config, project_dir)
        console.print("[green]Gespeichert.[/green]")
    elif not online:
        # Keine Option angegeben → geführter interaktiver Modus oder Anzeige
        console.print(f"[bold]Modelle für Provider '{real_name}':[/bold]")
        console.print(f"  Standard: {models.get('default') or '(keins)'}")
        for a, t in (models.get("aliases") or {}).items():
            console.print(f"  Alias: {a} → {t}")
        for m in (models.get("list") or []):
            console.print(f"  Modell: {m}")
        # Geführter Flow wenn questionary vorhanden
        try:
            import questionary
            action = questionary.select(
                "Was möchtest du tun?",
                choices=[
                    "Online-Modelle abrufen und auswählen",
                    "Modell manuell hinzufügen",
                    "Standard-Modell setzen",
                    "Alias erstellen",
                    "Modell entfernen",
                    "Alias entfernen",
                    "Nichts (Abbrechen)",
                ],
            ).ask()
            if not action or action.startswith("Nichts"):
                return
            if action.startswith("Online"):
                try:
                    names = _fetch_models_for_provider(real_name, prov)
                    if not names:
                        console.print("[yellow]Keine Modelle gefunden.[/yellow]")
                        return
                    selected = questionary.checkbox(
                        f"{len(names)} Modelle verfügbar. Auswählen (Leerzeichen = an/ab, Enter = bestätigen):",
                        choices=names,
                    ).ask()
                    if selected:
                        current_list = list(models.get("list") or [])
                        for s in selected:
                            if s not in current_list:
                                current_list.append(s)
                        models["list"] = current_list if current_list else None
                        if not models.get("default") and selected:
                            models["default"] = selected[0]
                        save_config(config, project_dir)
                        console.print(f"[green]{len(selected)} Modell(e) hinzugefügt. Gespeichert.[/green]")
                except Exception as e:
                    console.print(f"[red]Fehler:[/red] {e}")
            elif action.startswith("Modell manuell"):
                m_name = questionary.text("Modellname:").ask()
                if m_name and m_name.strip():
                    current_list = list(models.get("list") or [])
                    if m_name.strip() not in current_list:
                        current_list.append(m_name.strip())
                        models["list"] = current_list
                        if not models.get("default"):
                            models["default"] = m_name.strip()
                        save_config(config, project_dir)
                        console.print(f"[green]Modell '{m_name.strip()}' hinzugefügt.[/green]")
            elif action.startswith("Standard"):
                all_models = list(models.get("list") or [])
                aliases = list((models.get("aliases") or {}).keys())
                choices = all_models + aliases
                if not choices:
                    m_name = questionary.text("Modellname für Standard:").ask()
                else:
                    m_name = questionary.select("Standard-Modell wählen:", choices=choices).ask()
                if m_name and m_name.strip():
                    models["default"] = m_name.strip()
                    save_config(config, project_dir)
                    console.print(f"[green]Standard: {m_name.strip()}[/green]")
            elif action.startswith("Alias erstellen"):
                a_name = questionary.text("Alias-Name (Kurzname):").ask()
                a_target = questionary.text("Ziel-Modell:").ask()
                if a_name and a_target and a_name.strip() and a_target.strip():
                    models.setdefault("aliases", {})[a_name.strip()] = a_target.strip()
                    save_config(config, project_dir)
                    console.print(f"[green]Alias: {a_name.strip()} → {a_target.strip()}[/green]")
            elif action.startswith("Modell entfernen"):
                current_list = list(models.get("list") or [])
                if not current_list:
                    console.print("[yellow]Keine Modelle zum Entfernen.[/yellow]")
                    return
                to_remove = questionary.checkbox("Modelle zum Entfernen:", choices=current_list).ask()
                if to_remove:
                    for r in to_remove:
                        current_list.remove(r)
                    models["list"] = current_list or None
                    if models.get("default") in (to_remove or []):
                        models["default"] = current_list[0] if current_list else None
                    save_config(config, project_dir)
                    console.print(f"[green]{len(to_remove)} Modell(e) entfernt.[/green]")
            elif action.startswith("Alias entfernen"):
                aliases = models.get("aliases") or {}
                if not aliases:
                    console.print("[yellow]Keine Aliase vorhanden.[/yellow]")
                    return
                to_remove = questionary.checkbox("Aliase zum Entfernen:", choices=list(aliases.keys())).ask()
                if to_remove:
                    for r in to_remove:
                        aliases.pop(r, None)
                    save_config(config, project_dir)
                    console.print(f"[green]{len(to_remove)} Alias(e) entfernt.[/green]")
        except ImportError:
            console.print(f"\nOptionen: --add, --remove, --default, --alias, --remove-alias, --online")


if __name__ == "__main__":
    main()
