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
