# Email (IMAP/SMTP)

`email:` is a **top-level key** in `config.yaml` — NOT under `providers:`, NOT under `chat_clients:`. Use `save_config` to set it.

## Config structure

**Required:** `email.accounts.<name>` with `imap_server`, `smtp_server`, `username`, `password`. Optional: `imap_port` (default 993), `smtp_port` (default 587), `ssl` (default true), `name` (display name). `email.default` sets the default account.

```yaml
email:
  default: privat
  accounts:
    privat:
      imap_server: imap.gmail.com
      imap_port: 993
      smtp_server: smtp.gmail.com
      smtp_port: 587
      username: ich@gmail.com
      password: app_passwort
      ssl: true
      name: Max Mustermann
    arbeit:
      imap_server: imap.firma.de
      imap_port: 993
      smtp_server: smtp.firma.de
      smtp_port: 587
      username: max@firma.de
      password: passwort
      ssl: true
```

## Sending an email

Use the **`send_email` tool** — do NOT write Python scripts, do NOT use `exec` for email.

**When the user asks to write/send an email:**

1. **Pick the account** — use the account the user named, or omit `account` for default
2. **Generate a subject** — short keywords only (3–6 words max), never the full message text. e.g. "Kurze Begrüßung" or "Frage wegen Projekt X"
3. **Write the email** — compose the full text based on the user's request; make it natural and appropriate (formal/casual depending on context)
4. **Send it** via `send_email(to, subject, body, account?)` — credentials are loaded automatically
5. **Confirm** — tell the user: sent from which account, to whom, subject

**Examples:**
```
send_email(to="xyz@example.com", subject="Kurze Begrüßung", body="Hallo Christoph, ...")
send_email(to="chef@firma.de", subject="Update Projekt X", body="...", account="arbeit")
```

**User says → what to do:**
- "Schreib eine Mail an xyz@xyz.xyz und bedanke dich für..." → compose thank-you email, generate subject, send from default account
- "Schreib von meinem Account 'arbeit' an ..." → use `account="arbeit"`
- "Schreib eine formelle/informelle Mail an ..." → adjust tone accordingly
- Subject not mentioned by user → generate one from context — always short keywords, never a full sentence

## Reading email

Use the **`read_email` tool** — do NOT write Python scripts.

**Parameters:**
- `filter` — IMAP search criteria (default: `UNSEEN`)
- `count` — number of emails to fetch (default: 5)
- `folder` — IMAP folder (default: `INBOX`)
- `account` — account name (omit for default)
- `mark_read` — mark fetched emails as read on server (default: true)

**IMAP search criteria — use the right one:**
- User says "ungelesene Mails" / "neue Mails" → `filter="UNSEEN"`
- User says "gelesene Mails" → `filter="SEEN"`
- User says "alle Mails" / "Posteingang" → `filter="ALL"`
- User says "Mails von xyz@..." → `filter="FROM \"xyz@...\""`
- User says "Mails mit Betreff ..." → `filter="SUBJECT \"...\""`

**Examples:**
```
read_email()                                          # unread, default account
read_email(filter="ALL", count=10)                    # last 10, all
read_email(filter="FROM \"chef@firma.de\"", account="arbeit")  # from boss, work account
read_email(filter="UNSEEN", mark_read=false)          # peek without marking as read
```

## Scheduled email monitoring

For recurring email checks (e.g. "prüf meine Mails alle 30 Minuten"), use `schedule` with a prompt that calls `read_email`.

**How tracking works:** `read_email` with `filter="UNSEEN"` + `mark_read=true` (default) automatically tracks which emails are new — fetched emails are marked as SEEN on the server and won't appear again on the next check.

**Schedule examples:**

User says "prüf meine Mails alle 30 Minuten":
```
schedule(
  action='create',
  when='*/30 * * * *',
  prompt='Use read_email(filter="UNSEEN") to check for new emails. For each new message: send a summary (sender, subject, 2-sentence preview). If no new messages: respond with EXACTLY [NO_MESSAGE] and nothing else — the scheduler suppresses that token so the user gets no notification.'
)
```

User says "prüf mein Arbeit-Konto alle 15 Minuten":
```
schedule(
  action='create',
  when='*/15 * * * *',
  prompt='Use read_email(filter="UNSEEN", account="arbeit") to check for new emails on the "arbeit" account. Summarize each new message. If none: respond with EXACTLY [NO_MESSAGE] (scheduler suppresses it).'
)
```

## Auto-reply (via schedule)

User says "richte einen Auto-Responder für Mails von chef@firma.de ein":
```
schedule(
  action='create',
  when='*/15 * * * *',
  prompt='Use read_email(filter="UNSEEN FROM \"chef@firma.de\"", account="arbeit") to check for new emails from chef@firma.de. For each new email: send an auto-reply using send_email(to=sender_address, subject="Re: " + original_subject, body="Nachricht erhalten, ich melde mich innerhalb von 24 Stunden.", account="arbeit"). mark_read=true ensures no duplicate replies.'
)
```

## Rules

- **Never output passwords, tokens, or credentials** in your response text.
- **Always use `send_email` and `read_email` tools** — never write Python scripts for email.
- Credentials are loaded automatically from config — never ask the user for login data.
- For Gmail: use an **App Password** (not the main password) — 2FA must be enabled.
- For Outlook/Office365: SMTP server is `smtp.office365.com`, port `587`.
- If IMAP/SMTP fails: check ssl setting, try port 465 for SSL or 587 for STARTTLS.
