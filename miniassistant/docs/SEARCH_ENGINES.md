# Search Engines (SearXNG) and VPN Search

**Config:** Search engines are under **`search_engines`** (not a single URL). Each entry has an id and a **url**. **`default_search_engine`** is optional; if missing, the first engine is used. On first-time setup (onboarding) only one engine is configured (e.g. `main`). A second engine (e.g. for VPN/secure search) is **not** in the initial assistant – the user adds it later.

**When the user asks for a second SearXNG (e.g. VPN):**

1. Read the current config to see existing `search_engines` and `default_search_engine`.
2. Add a new entry with an id that contains **`vpn`** (e.g. `vpn` or `searxng_vpn`) so the assistant knows to use it only when explicitly asked or when prefs say so. Example:

```yaml
search_engines:
  main:
    url: https://search.example.org
  vpn:
    url: https://search-vpn.example.org
default_search_engine: main
```

3. Use **save_config** with the full YAML (merge the new engine into existing config). Tell the user to restart.

**Behaviour:** The assistant has a **web_search** tool with an optional **engine** parameter. It uses the default engine for normal searches. If an engine id contains **vpn** (e.g. `vpn`, `searxng_vpn`), the assistant uses it **only** when the user explicitly asks for VPN/secure search or when prefs (e.g. search habits in prefs) specify it. You know this from the system prompt (TOOLS section and search engines list).
