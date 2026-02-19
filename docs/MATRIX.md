# Matrix Bot Setup

**Required in config:** `chat_clients.matrix` with at least `homeserver` (not `homeserver_url`), `user_id`, `token` (not `access_token`; the value comes from the login response's `access_token`). Include `enabled: true` (optional, default true). Optionally `bot_name` (display name, default "MiniAssistant") and `encrypted_rooms: true` (default; set to false for unencrypted-only). For E2EE, `device_id` from the login response is required (not the default "miniassistant" if the server issued another one).

**If user has no token or device_id yet**, show them how to get both in one request:

```bash
curl --request POST \
  --url "https://THEIR_HOMESERVER/_matrix/client/v3/login" \
  --header "Content-Type: application/json" \
  --data '{
  "type": "m.login.password",
  "identifier": { "type": "m.id.user", "user": "BOT_USERNAME" },
  "password": "BOT_PASSWORD"
}'
```

From the JSON response: put `token` into the config key **`token`** (config key is `token`, not `access_token`). Use `user_id` in quotes as `user_id`, and `device_id` exactly as returned (important for E2EE).

**Example config block** (user fills in values):

```yaml
chat_clients:
  matrix:
    enabled: true
    bot_name: "MiniAssistant"
    homeserver: https://matrix.example.org
    user_id: "@bot:example.org"
    token: "syt_..."
    device_id: "ABCDEFGHIJ"   # from login response
    encrypted_rooms: true
```

**User side:** Create a Matrix account for the bot on their homeserver; use the curl above (or a client) to get token and device_id. No extra steps in the Matrix client besides having the account.
