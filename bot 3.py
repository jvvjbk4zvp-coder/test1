"""
Telegram EA Control Bot — v2
=============================
Adds:
  - Admin login (password-gated, bound to YOUR Telegram user id only)
  - Client self-registration by MT5 ID -> pending admin approval
  - Auto-revoke if a client's EA reports an MT5 account id different from
    the one you approved (heartbeat mismatch detection)
  - Persistent bottom keyboard for action buttons (no scrolling needed)

SETUP
-----
1. pip install -r requirements.txt
2. Set env vars:
     TELEGRAM_BOT_TOKEN   - from @BotFather
     ADMIN_PASSWORD       - a password only you know, used once via /admin
3. Run: python bot.py
4. In Telegram, YOU send:  /admin <ADMIN_PASSWORD>
   This binds admin rights to your Telegram account permanently (until you
   restart the bot, since this demo stores it in memory — see PERSISTENCE
   note below for making it durable).
5. Clients send: /register <their MT5 account ID>
   You get a message with Approve / Reject buttons for each request.
6. On the client's MT5 terminal, install ea_bridge.mq5 and set its
   "AssignedChatID" input to that client's Telegram chat id (you can see
   it in the approval notification). The EA reports its real logged-in
   account number back to the bot every heartbeat interval. If it ever
   doesn't match what you approved, the bot revokes access automatically
   and tells both you and the client why.

PERSISTENCE NOTE
-----------------
This demo keeps admin/session/approval state in memory (dicts). Restarting
the bot clears it. For production, swap the dicts below for a real
database (SQLite is enough to start) so state survives restarts.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "PUT_A_STRONG_ADMIN_PASSWORD_HERE")

COMMANDS_DIR = Path("./commands")
COMMANDS_DIR.mkdir(exist_ok=True)

# EA writes its actual live account id here on a timer; the bot polls it.
HEARTBEAT_DIR = Path("./heartbeats")
HEARTBEAT_DIR.mkdir(exist_ok=True)

HEARTBEAT_CHECK_INTERVAL = 30      # seconds between mismatch checks
HEARTBEAT_STALE_AFTER = 300        # seconds; if no heartbeat seen, warn (not revoke)

DERISK_PARTIAL_PCT = 50
DERISK_MOVE_SL_TO_BE = True

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ea_bot")

# --------------------------------------------------------------------------
# STATE (in-memory demo store — replace with a DB for production)
# --------------------------------------------------------------------------

ADMIN_IDS: set[int] = set()  # Telegram user ids with admin rights


@dataclass
class PendingRequest:
    chat_id: int
    telegram_user: str
    mt5_id: str
    requested_at: float = field(default_factory=time.time)


@dataclass
class ApprovedClient:
    chat_id: int
    telegram_user: str
    mt5_id: str
    approved_at: float = field(default_factory=time.time)


PENDING: dict[int, PendingRequest] = {}       # chat_id -> request
APPROVED: dict[int, ApprovedClient] = {}      # chat_id -> approved client

BUTTON_LABELS = {
    "SL to BE": "SL_BE",
    "Partial 25%": "PARTIAL_25",
    "Partial 50%": "PARTIAL_50",
    "Partial 75%": "PARTIAL_75",
    "⚠️ DE-RISK": "DERISK",
    "🔴 CLOSE ALL": "CLOSE_ALL",
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_approved(chat_id: int) -> bool:
    return chat_id in APPROVED


# --------------------------------------------------------------------------
# COMMAND BRIDGE (unchanged concept — writes JSON the EA polls)
# --------------------------------------------------------------------------


def push_command(mt5_id: str, action: str, params: dict | None = None) -> str:
    queue_file = COMMANDS_DIR / f"{mt5_id}.json"
    cmd = {
        "id": f"{int(time.time() * 1000)}",
        "action": action,
        "params": params or {},
        "status": "pending",
        "created_at": time.time(),
    }
    data = []
    if queue_file.exists():
        try:
            data = json.loads(queue_file.read_text())
        except json.JSONDecodeError:
            data = []
    data.append(cmd)
    queue_file.write_text(json.dumps(data, indent=2))
    logger.info("Queued command for MT5 %s: %s", mt5_id, cmd)
    return cmd["id"]


def read_heartbeat(chat_id: int) -> dict | None:
    """EA writes {"account_id": "...", "timestamp": ...} here per client."""
    f = HEARTBEAT_DIR / f"{chat_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# KEYBOARDS
# --------------------------------------------------------------------------


def control_panel_keyboard() -> ReplyKeyboardMarkup:
    """
    Persistent bottom keyboard — stays pinned above the text input, visible
    without scrolling up through chat history, unlike inline buttons
    attached to a single message.
    """
    rows = [
        [KeyboardButton("SL to BE")],
        [
            KeyboardButton("Partial 25%"),
            KeyboardButton("Partial 50%"),
            KeyboardButton("Partial 75%"),
        ],
        [KeyboardButton("⚠️ DE-RISK")],
        [KeyboardButton("🔴 CLOSE ALL")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def approval_inline_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"APPROVE_{chat_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"REJECT_{chat_id}"),
            ]
        ]
    )


# --------------------------------------------------------------------------
# ADMIN LOGIN
# --------------------------------------------------------------------------


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if is_admin(user_id):
        await update.message.reply_text("You're already logged in as admin.")
        return

    if not args:
        await update.message.reply_text("Usage: /admin <password>")
        return

    if args[0] == ADMIN_PASSWORD:
        ADMIN_IDS.add(user_id)
        try:
            await update.message.delete()  # scrub the password from chat
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Admin access granted for this account.\n\n"
            "Commands:\n"
            "/pending — view pending registration requests\n"
            "/clients — view & revoke approved clients",
        )
    else:
        await update.message.reply_text("❌ Wrong admin password.")


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only. Use /admin <password> first.")
        return
    if not PENDING:
        await update.message.reply_text("No pending requests.")
        return
    for req in PENDING.values():
        await update.message.reply_text(
            f"Pending: @{req.telegram_user} (chat_id {req.chat_id})\n"
            f"Requested MT5 ID: {req.mt5_id}",
            reply_markup=approval_inline_keyboard(req.chat_id),
        )


async def clients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only. Use /admin <password> first.")
        return
    if not APPROVED:
        await update.message.reply_text("No approved clients yet.")
        return
    for c in APPROVED.values():
        await update.message.reply_text(
            f"@{c.telegram_user} — MT5 ID {c.mt5_id} (chat_id {c.chat_id})",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚫 Revoke", callback_data=f"REVOKE_{c.chat_id}")]]
            ),
        )


# --------------------------------------------------------------------------
# CLIENT REGISTRATION FLOW
# --------------------------------------------------------------------------


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if is_approved(chat_id):
        await update.message.reply_text(
            "Welcome back. Your control panel:", reply_markup=control_panel_keyboard()
        )
        return
    if chat_id in PENDING:
        await update.message.reply_text("Your registration is still pending admin approval.")
        return
    await update.message.reply_text(
        "Welcome. To request access, send:\n/register <your MT5 account ID>\n\n"
        "Example: /register 5012345\n\n"
        "An admin will review and approve your request."
    )


async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if is_approved(chat_id):
        await update.message.reply_text(
            "You're already approved.", reply_markup=control_panel_keyboard()
        )
        return

    if not context.args:
        await update.message.reply_text("Usage: /register <your MT5 account ID>")
        return

    mt5_id = context.args[0].strip()
    if not mt5_id.isdigit():
        await update.message.reply_text("MT5 account ID should be numbers only. Try again.")
        return

    PENDING[chat_id] = PendingRequest(
        chat_id=chat_id,
        telegram_user=user.username or user.first_name or str(user.id),
        mt5_id=mt5_id,
    )
    await update.message.reply_text(
        "Request submitted. You'll be notified once an admin approves it."
    )

    if not ADMIN_IDS:
        logger.warning("A client registered but no admin is logged in yet to approve them.")
        return

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🆕 New access request\n"
                    f"User: @{PENDING[chat_id].telegram_user}\n"
                    f"Chat ID: {chat_id}\n"
                    f"Requested MT5 ID: {mt5_id}\n\n"
                    f"Remember to set this EA's AssignedChatID input to {chat_id} "
                    f"on that client's terminal so the bot can verify it stays on "
                    f"MT5 ID {mt5_id}."
                ),
                reply_markup=approval_inline_keyboard(chat_id),
            )
        except Exception as e:
            logger.error("Could not notify admin %s: %s", admin_id, e)


# --------------------------------------------------------------------------
# APPROVE / REJECT / REVOKE CALLBACKS
# --------------------------------------------------------------------------


async def admin_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_user_id = update.effective_user.id

    if not is_admin(admin_user_id):
        await query.answer("Admins only.", show_alert=True)
        return

    data = query.data
    chat_id = int(data.rsplit("_", 1)[-1])

    if data.startswith("APPROVE_"):
        req = PENDING.pop(chat_id, None)
        if not req:
            await query.answer("Request no longer pending (already handled).")
            return
        APPROVED[chat_id] = ApprovedClient(
            chat_id=chat_id, telegram_user=req.telegram_user, mt5_id=req.mt5_id
        )
        await query.answer("Approved.")
        await query.edit_message_text(
            f"✅ Approved @{req.telegram_user} for MT5 ID {req.mt5_id}."
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ You've been approved! Here is your control panel:",
            reply_markup=control_panel_keyboard(),
        )

    elif data.startswith("REJECT_"):
        req = PENDING.pop(chat_id, None)
        await query.answer("Rejected.")
        await query.edit_message_text(
            f"❌ Rejected request from @{req.telegram_user if req else chat_id}."
        )
        if req:
            await context.bot.send_message(
                chat_id=chat_id, text="❌ Your access request was declined by the admin."
            )

    elif data.startswith("REVOKE_"):
        client = APPROVED.pop(chat_id, None)
        await query.answer("Revoked.")
        await query.edit_message_text(
            f"🚫 Revoked access for @{client.telegram_user if client else chat_id}."
        )
        if client:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🚫 Your access has been revoked by the admin.",
                reply_markup=ReplyKeyboardRemove(),
            )


# --------------------------------------------------------------------------
# ACTION BUTTON HANDLING (from the persistent bottom keyboard)
# --------------------------------------------------------------------------


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text in BUTTON_LABELS:
        if not is_approved(chat_id):
            await update.message.reply_text(
                "You're not approved yet. Use /register <MT5 ID> to request access."
            )
            return
        await handle_action(update, context, BUTTON_LABELS[text])
        return
    # Not a recognized button — ignore silently.


async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action_key: str):
    chat_id = update.effective_chat.id
    client = APPROVED[chat_id]

    actions = {
        "SL_BE": ("SL_TO_BE", "Moving Stop Loss to Breakeven..."),
        "PARTIAL_25": ("PARTIAL_CLOSE", "Closing 25% of position..."),
        "PARTIAL_50": ("PARTIAL_CLOSE", "Closing 50% of position..."),
        "PARTIAL_75": ("PARTIAL_CLOSE", "Closing 75% of position..."),
        "CLOSE_ALL": ("CLOSE_ALL", "Closing ALL open positions..."),
        "DERISK": ("DERISK", "De-risking: SL→BE + partial close..."),
    }
    ea_action, human_text = actions[action_key]

    params = {}
    if ea_action == "PARTIAL_CLOSE":
        params = {"percent": int(action_key.split("_")[1])}
    elif ea_action == "DERISK":
        params = {"percent": DERISK_PARTIAL_PCT, "move_sl_to_be": DERISK_MOVE_SL_TO_BE}

    if ea_action == "CLOSE_ALL":
        await update.message.reply_text(
            "⚠️ Are you sure you want to CLOSE ALL positions?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes, close everything", callback_data="CONFIRM_CLOSE_ALL"),
                        InlineKeyboardButton("Cancel", callback_data="CANCEL"),
                    ]
                ]
            ),
        )
        return

    cmd_id = push_command(client.mt5_id, ea_action, params)
    await update.message.reply_text(f"✅ {human_text}\n(command id: {cmd_id})")


async def confirm_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    if not is_approved(chat_id):
        await query.answer("Not approved.", show_alert=True)
        return
    client = APPROVED[chat_id]
    await query.answer("Closing all positions...")
    cmd_id = push_command(client.mt5_id, "CLOSE_ALL", {})
    await query.message.reply_text(f"🔴 CLOSE ALL sent. (command id: {cmd_id})")


async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.message.reply_text("Action cancelled.")


# --------------------------------------------------------------------------
# HEARTBEAT / MISMATCH DETECTION (background job)
# --------------------------------------------------------------------------


async def check_heartbeats(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, client in list(APPROVED.items()):
        hb = read_heartbeat(chat_id)
        if hb is None:
            continue  # EA hasn't reported yet, or not installed — don't punish for silence

        reported_id = str(hb.get("account_id", "")).strip()
        if not reported_id:
            continue

        if reported_id != client.mt5_id:
            APPROVED.pop(chat_id, None)
            logger.warning(
                "MT5 ID mismatch for chat %s: approved=%s reported=%s. Revoking.",
                chat_id, client.mt5_id, reported_id,
            )
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🚫 Access revoked: your MT5 terminal is reporting a different "
                        f"account ({reported_id}) than the one you were approved for "
                        f"({client.mt5_id}). Contact the admin if this is a mistake."
                    ),
                    reply_markup=ReplyKeyboardRemove(),
                )
            except Exception as e:
                logger.error("Could not notify client %s of revoke: %s", chat_id, e)

            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"⚠️ Auto-revoked @{client.telegram_user} (chat_id {chat_id}): "
                            f"approved MT5 {client.mt5_id}, but their EA is now reporting "
                            f"{reported_id}."
                        ),
                    )
                except Exception as e:
                    logger.error("Could not notify admin %s: %s", admin_id, e)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------


def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var before running.")
    if ADMIN_PASSWORD == "PUT_A_STRONG_ADMIN_PASSWORD_HERE":
        logger.warning(
            "ADMIN_PASSWORD is still the placeholder — set a real one via env var!"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("clients", clients_cmd))
    app.add_handler(CommandHandler("register", register_cmd))

    app.add_handler(CallbackQueryHandler(confirm_close_all, pattern="^CONFIRM_CLOSE_ALL$"))
    app.add_handler(CallbackQueryHandler(cancel_action, pattern="^CANCEL$"))
    app.add_handler(
        CallbackQueryHandler(admin_action_callback, pattern="^(APPROVE|REJECT|REVOKE)_")
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.job_queue.run_repeating(check_heartbeats, interval=HEARTBEAT_CHECK_INTERVAL, first=10)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
