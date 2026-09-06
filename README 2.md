# Telegram EA Control Bot (v2 — Admin Approval + Auto-Revoke)

Your clients request access with their MT5 account ID, **you approve or
reject each one**, and the bot automatically revokes access if a client's
EA ever reports a different MT5 account than the one you approved. Action
buttons sit in a **persistent bottom keyboard** — always visible, no
scrolling required.

## Files

- `bot.py` — the Telegram bot: admin login, client registration/approval,
  action buttons, heartbeat mismatch detection
- `ea_bridge.mq5` — MT5 Expert Advisor: executes trade commands AND reports
  its real logged-in account id back to the bot every 30s
- `requirements.txt` — Python deps
- `.gitignore` — keeps secrets and generated files out of git

## How the pieces talk to each other

```
Telegram Bot (Python)
   ├─ writes  commands/<mt5_id>.json     -> read by the EA, executed as trades
   └─ reads   heartbeats/<chat_id>.json  <- written by the EA every 30s
```

Both folders need to be visible to **both** the bot process and the
client's MT5 terminal. The simplest setup: run the bot on the same
machine/VPS as that client's MT5 terminal, and point `COMMANDS_DIR` /
`HEARTBEAT_DIR` in `bot.py` at that terminal's `MQL5/Files/` folder (or use
`FILE_COMMON` in the EA to use the shared `Terminal/Common/Files/` folder,
which is easier when multiple terminals need to reach the same location).
For clients on separate machines, swap the file-drop for a small HTTPS
endpoint instead (the EA already supports `WebRequest()` calls with minor
edits — ask if you want that version).

## One-time setup

```bash
pip install -r requirements.txt
```

Set two environment variables before running:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AA...fromBotFather"
export ADMIN_PASSWORD="choose-a-strong-password"
```

Run it:

```bash
python bot.py
```

## Becoming admin (you, only)

In Telegram, message your bot:

```
/admin your-strong-password
```

This binds admin rights to **your Telegram account specifically** — no one
else can become admin without that password, and even if someone learns
your bot's username, they can't self-promote. The bot deletes your
password message right after reading it.

Admin commands:
- `/pending` — see and approve/reject outstanding requests
- `/clients` — see approved clients, with a Revoke button for each

## Client flow

1. Client messages the bot, sends `/start`
2. Bot tells them to send `/register <their MT5 account ID>`
3. **You** get a message with the client's Telegram handle, their claimed
   MT5 ID, and **✅ Approve / ❌ Reject** buttons
4. If you approve, the client immediately gets their control panel — a
   keyboard pinned at the bottom of the chat:

   ```
   [        SL to BE        ]
   [ Partial 25% ][ Partial 50% ][ Partial 75% ]
   [       ⚠️ DE-RISK        ]
   [      🔴 CLOSE ALL       ]
   ```

   This is a native Telegram "reply keyboard" — it stays visible above
   their message box permanently, so they never have to scroll up to find
   it, unlike buttons attached to one message.

## Setting up identity verification (the auto-revoke part)

For the mismatch detection to work, each client's **EA** needs to know
which Telegram chat it's reporting for:

1. When a client registers, the notification you get includes their
   `chat_id` (e.g. `123456789`)
2. On that specific client's MT5 terminal, open the EA's inputs and set:
   ```
   AssignedChatID = 123456789
   ```
3. The EA now writes `heartbeats/123456789.json` every 30 seconds
   containing the account number it's *actually* logged into
   (`AccountInfoInteger(ACCOUNT_LOGIN)`), regardless of what you approved
4. The bot polls this file every 30 seconds. If the reported account ever
   stops matching the MT5 ID you approved for that chat, the bot:
   - immediately revokes their access (removes the control panel)
   - tells the client why
   - tells you which client and what mismatch triggered it

This catches a client switching the EA to a different MT5 login on the
same terminal, or copying the EA to an unapproved account.

**Limitation to know:** this only detects account swaps on terminals
running the EA with a correctly-set `AssignedChatID`. If a client never
installs the EA, or installs it without setting that input, the bot has no
way to check their real account — commands they trigger will just silently
go into a command queue no EA is polling. Consider making EA installation
with a correct `AssignedChatID` a hard requirement before you approve
someone.

## Security recommendations before going live

- Replace the in-memory `PENDING`/`APPROVED`/`ADMIN_IDS` dicts with a real
  database (SQLite is enough) — right now all of this resets if the bot
  restarts, meaning every client would need to be re-approved
- Log every approval, revoke, and executed command with timestamp for audit
- Rotate your bot token immediately if it's ever been pasted anywhere
  public (chat screenshots, GitHub, etc.) — regenerate via @BotFather →
  `/mybots` → your bot → API Token → Revoke current token
- Consider adding a confirmation step to **De-risk**, not just Close All
- If you move to the HTTP/WebRequest bridge instead of files, put a shared
  secret header on requests so the EA only accepts commands that actually
  came from your bot
