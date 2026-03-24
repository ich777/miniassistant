# Web Fetching (read_url): JS-Rendering and Proxies

## JS-Rendering (Playwright)

`read_url` supports optional JavaScript rendering via Playwright (`js: true`).

**When to use `js: true`:**
- Page returns empty or minimal content (SPA, React/Vue/Angular app)
- Content only appears after JS execution (dynamic tables, lazy loading)
- User explicitly requests it

**When NOT to use `js: true`:**
- Normal HTML pages (Wikipedia, GitHub, docs, news, blogs)
- When a plain fetch already returns useful content
- API URLs (JSON) — no browser rendering needed

**Approach:** Try `js: false` (default) first. If content is empty or just a JS skeleton, retry with `js: true`.

**Playwright not installed:** The tool returns a warning and falls back to plain fetch. If JS rendering is needed, ask the user: "Should I install Playwright? (~300 MB)" — if they agree, install it using the Python executable from the **System** section of your prompt:
```
exec: <python> -m pip install playwright && <python> -m playwright install chromium
```
where `<python>` is the `Python:` path shown in the System section. After installing, the server must be restarted for `js=true` to work.

---

## Form interaction (when read_url is not enough)

**`read_url` (even with `js: true`) can only READ a page — it CANNOT fill forms, click buttons, or navigate multi-step flows.**
For sites that REQUIRE filling a form or clicking buttons to get results (login, multi-step wizards): write a Playwright script via `exec`.
**Note:** Many tracking/lookup sites have direct URLs (e.g. `site.com/tracking/NUMBER`) — use `read_url` for those, no Playwright needed.

### Mandatory before writing any script:

**(1) Verify the URL is actually reachable.**
Never assume a URL works or is publicly accessible. Check with a plain `read_url` first.
Does it load? Is it behind a login? Does it block bots? Is it geo-restricted?
If it fails or returns an error/redirect, a Playwright script will fail too — investigate first.

**(2) Never guess URL structure, parameters, or endpoints from memory.**
Your training data is outdated and wrong for site-specific URLs.
Always load the base URL first and read the actual page to find real form fields and flow.

**(3) Always inspect before interacting.**
Never guess CSS selectors or form structure. Run an inspection script first to discover what is on the page.
This is the same principle as research: you do not answer from memory, you look it up first.

### Step-by-step — always in this order:

**Step 1 — Check accessibility + inspect (ALWAYS first, no exceptions):**
```python
exec: python3 << 'PYEOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com", timeout=30000)
    page.wait_for_load_state("networkidle", timeout=15000)
    print("title:", page.title())
    print("url:", page.url)
    inputs = page.query_selector_all("input")
    for i in inputs:
        print("input:", i.get_attribute("id"), i.get_attribute("name"), i.get_attribute("type"))
    buttons = page.query_selector_all("button, [type=submit]")
    for b in buttons:
        print("button:", b.get_attribute("id"), b.inner_text())
    browser.close()
PYEOF
```

**Step 2 — Interact based on what you found in Step 1:**
```python
exec: python3 << 'PYEOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com", timeout=30000)
    page.wait_for_load_state("networkidle", timeout=15000)

    # Handle cookie banners first:
    try:
        page.locator("button:has-text('Accept'), button:has-text('Akzeptieren'), button:has-text('Alle')").first.click(timeout=3000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except:
        pass

    # Fill form using actual selectors from Step 1:
    page.fill("input#the-real-id", "value")
    page.click("button#the-real-submit")
    page.wait_for_load_state("networkidle", timeout=15000)

    # Multi-step: wait for next field to appear before filling:
    page.wait_for_selector("input#next-field", timeout=10000)
    page.fill("input#next-field", "value2")
    page.click("button#confirm", force=True)
    page.wait_for_load_state("networkidle", timeout=15000)

    print(page.inner_text("body"))
    browser.close()
PYEOF
```

**Rules:**
- Always `wait_for_load_state("networkidle")` after every navigation or click
- Always handle cookie banners BEFORE interacting with the real form
- If a click fails: try `force=True`
- If result is empty or wrong: print `page.content()` to debug

---

## Proxies

`read_url` supports named proxies from the config (under `read_url.proxies`).

**Select a proxy explicitly:**
```
read_url(url="https://example.com", proxy="vpn1")
```

**Available proxies:** listed in the System prompt under "Available network connections".

**When to use a proxy:**
- User asks for VPN/anonymous access
- Site is unreachable from the direct connection

**Combined (JS + proxy):** Playwright does not use proxies (browser runs locally). The proxy only applies to the normal httpx fetch.
