# Discord Bot Setup

**Required in config:** `chat_clients.discord.bot_token`. Include `enabled: true` (optional, default true; use `false` to disable the bot).

**If user has no bot_token yet**, tell them:

1. Open [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. Go to **Bot**, create a bot and **copy the token**.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
4. Invite the bot: OAuth2 > URL Generator > Scopes: `bot`; Permissions: Send Messages, Read Message History. Open the generated URL to invite.
5. The bot responds in DMs and when @mentioned in channels.

**Example config block:**

```yaml
chat_clients:
  discord:
    enabled: true
    bot_token: "THEIR_BOT_TOKEN"
```

**After you write config:** remind the user to restart the service.

---

## Adding the bot to a Discord server

If the user asks how to add/invite the bot to their server:

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → select the application.
2. Navigate to **OAuth2 → URL Generator**.
3. Under **Scopes**, select: `bot`.
4. Under **Bot Permissions**, select:
   - Send Messages
   - Read Message History
   - Embed Links (optional, for rich formatting)
   - Attach Files (optional, for image uploads)
   - Use Slash Commands (optional)
5. Copy the generated URL at the bottom and open it in a browser.
6. Select the target server from the dropdown and click **Authorize**.
7. The bot should now appear in the server's member list.

**Troubleshooting:**
- Bot doesn't respond in channels? It only responds when **@mentioned** by name. In DMs it responds to every message.
- "Missing Permissions" error? Make sure the bot role has Send Messages permission in the channel.
- Bot is offline? Check that `miniassistant` service is running and the token in config is correct.
