# Avatar / Profile Picture

## Where the avatar is stored

The bot's avatar image should be stored at:
- **Primary:** `agent_dir/avatar.png` (e.g. `~/.config/miniassistant/agent/avatar.png`)
- **Fallback:** `miniassistant/web/static/miniassistant.png` (the built-in default logo)

**Config key** (optional):
```yaml
avatar: "/path/to/avatar.png"          # local file path
# or
avatar: "https://example.org/bot.png"  # URL (will be downloaded)
```

If `avatar` is not set in config, check `agent_dir/avatar.png`. If that doesn't exist, the default logo is used.

**Format:** PNG recommended (works everywhere: Matrix, Discord, Web-UI). Square aspect ratio (e.g. 256x256 or 512x512). Max ~2 MB.

## URL validation

If the avatar is a URL:
1. **Validate the URL** with `check_url` — must return HTTP 200.
2. **Download** to `agent_dir/avatar.png`:
   ```bash
   curl -sL "URL" -o "AGENT_DIR/avatar.png"
   ```
3. **Check download success** — verify exit code and file size:
   ```bash
   ls -la "AGENT_DIR/avatar.png"
   ```
   If the file is 0 bytes or missing → download failed. Tell the user.
4. **Verify it's actually an image:**
   ```bash
   file "AGENT_DIR/avatar.png"
   ```
   Expected output must contain `PNG image data`, `JPEG image data`, or similar image type.
   If it says `HTML document`, `ASCII text`, or anything non-image → the URL did not point to an image. Delete the file and tell the user.
5. **Optional size check:** `identify "AGENT_DIR/avatar.png"` (if ImageMagick is installed) to confirm dimensions. Recommend square 256x256+.

## Setting avatar on Matrix — step by step

**Do these steps in order. Do NOT use placeholder values — extract real values from config first.**

### Step 1: Check if avatar image exists
```bash
ls -la "AGENT_DIR/avatar.png"
```
If missing or 0 bytes → tell user "Kein Avatar-Bild gefunden" and stop.

### Step 2: Read Matrix credentials from config
Only read the relevant section — **not** the whole config:
```bash
grep -A20 'matrix:' ~/.config/miniassistant/config.yaml
```
Extract these **actual values** from the output:
- `homeserver` → e.g. `https://matrix.minenet.at`
- `token` → the actual access_token string (e.g. `syt_...`)
- `user_id` → e.g. `@clawi:matrix.minenet.at`

### Step 3: Upload image to Matrix media repo
Use the **real values** from Step 2 (not placeholders like `HOMESERVER` or `TOKEN`!):
```bash
curl -s -X POST "REAL_HOMESERVER/_matrix/media/v3/upload?filename=avatar.png" \
  -H "Authorization: Bearer REAL_TOKEN" \
  -H "Content-Type: image/png" \
  --data-binary @"AGENT_DIR/avatar.png"
```
The response contains `content_uri` (e.g. `"mxc://matrix.minenet.at/MEDIA_ID"`). Extract this value.

### Step 4: Set as profile picture
Use the **real** `content_uri` from Step 3 and the **real** user_id from Step 2:
```bash
curl -s -X PUT "REAL_HOMESERVER/_matrix/client/v3/profile/REAL_USER_ID/avatar_url" \
  -H "Authorization: Bearer REAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"avatar_url": "REAL_MXC_URI_FROM_STEP_3"}'
```

**CRITICAL:** Every value in the curl commands above MUST be a real value read from the config or from a previous step's output. NEVER use `matrix.example.org`, `TOKEN`, `HOMESERVER`, or any other placeholder string.

### Step 5: Confirm
Tell the user the avatar was set. Save path in config: `save_config({avatar: "agent_dir/avatar.png"})`.

## Setting avatar on Discord — step by step

**Do these steps in order. Do NOT use placeholder values — extract real values from config first.**

### Step 1: Check avatar image (same as Matrix Step 1)

### Step 2: Read Discord credentials from config
```bash
grep -A10 'discord:' ~/.config/miniassistant/config.yaml
```
Extract: the **actual** `bot_token` value from the output.

### Step 3: Upload avatar
Use the **real bot_token** from Step 2 (not a placeholder like `BOT_TOKEN`!):
```bash
BASE64=$(base64 -w0 "AGENT_DIR/avatar.png")
curl -s -X PATCH "https://discord.com/api/v10/users/@me" \
  -H "Authorization: Bot REAL_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"avatar\": \"data:image/png;base64,$BASE64\"}"
```

**CRITICAL:** `REAL_BOT_TOKEN` must be the actual token string from the config, not a placeholder. NEVER use `BOT_TOKEN`, `$BOT_TOKEN`, or any other placeholder.

### Step 4: Confirm (same as Matrix Step 5)

## Behavior — when user asks to set/change avatar

**Act immediately. Do NOT tell the user to do it — do it yourself.**

1. **Check if avatar image exists:** `exec: ls -la agent_dir/avatar.png`
   - If missing and user provided a file path → copy it: `exec: cp PATH agent_dir/avatar.png`
   - If missing and user provided a URL → download it (see URL validation above)
   - If missing and no image given → tell user to provide one, stop.
2. **Read config** (`exec: grep -A20 'matrix:' CONFIG_PATH` or `discord:`) to get the credentials for the platform the user is chatting on.
3. **Execute the curl commands** from the relevant section above, substituting the real values.
4. **Save** avatar path: `save_config({avatar: "agent_dir/avatar.png"})`
5. **Confirm** to the user.

## Onboarding

During onboarding, ask the user if they want to set an avatar:
- "Hast du ein Profilbild/Avatar für den Bot? (PNG-Datei oder URL, optional)"
- If yes: download/copy to `agent_dir/avatar.png`, save path in config.
- If no: the default logo will be used.

## If avatar not found

If the configured avatar path doesn't exist or the URL is invalid:
1. Log a warning.
2. Fall back to the default logo (`miniassistant/web/static/miniassistant.png`).
3. Tell the user: "Avatar nicht gefunden. Bitte einen neuen setzen oder den Pfad in der Config prüfen."
