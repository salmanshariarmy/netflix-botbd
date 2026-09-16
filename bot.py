# bot.py
import os
import logging
import json
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

from netflix_session import NetflixSessionEngine

# ── CONFIG ────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ALLOWED_USERS = os.getenv("ALLOWED_USERS", "")  # comma-separated Telegram user IDs, empty = open

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS.split(",")


# ── HANDLERS ──────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 Netflix Session Checker\n\n"
        "Send me your Netflix cookies and I'll:\n"
        "• Check if the session is valid\n"
        "• Show account details (profile, plan, country)\n"
        "• Generate a login bookmarklet\n"
        "• Generate NFToken login link (for TV/laptop/phone)\n\n"
        "Cookie format: Netscape, JSON (Cookie-Editor), or header-style"
    )


async def handle_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("❌ Unauthorized")
        logger.warning(f"Unauthorized attempt by user {user.id}")
        return

    await update.message.reply_text("⏳ Checking session...")

    engine = NetflixSessionEngine()
    try:
        result = engine.check_session(update.message.text.strip())
    except Exception as e:
        logger.exception("Error processing cookies")
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return

    if result["status"] == "valid":
        acct = result["account"]
        auth = engine.extract_auth_cookies(update.message.text.strip())
        bookmarklet = engine.generate_bookmarklet(auth) if auth else ""

        lines = ["✅ **Session Valid**\n"]
        if acct.get("profile_name"):
            lines.append(f"👤 Profile: `{acct['profile_name']}`")
        if acct.get("email"):
            lines.append(f"📧 Email: `{acct['email']}`")
        if acct.get("plan"):
            lines.append(f"📺 Plan: `{acct['plan']}`")
        if acct.get("country"):
            lines.append(f"🌍 Country: `{acct['country']}`")

        if bookmarklet:
            lines.append("\n🔗 **Login Bookmarklet:**")
            lines.append(f"`{bookmarklet[:80]}...`")
            lines.append("_Paste it in the browser URL bar on netflix.com (logged out)_")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

        # Store cookies for callback buttons
        context.user_data["last_cookies"] = update.message.text

        keyboard = [
            [InlineKeyboardButton("📋 Full Bookmarklet", callback_data="bookmarklet")],
            [InlineKeyboardButton("🔗 Generate NFToken Link", callback_data="nftoken")],
        ]
        await update.message.reply_text(
            "Tap below:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif result["status"] == "expired":
        await update.message.reply_text(
            "❌ **Session Expired**\n\nThe cookies are no longer valid.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ **{result['status']}**\n\n{result.get('message', 'Unknown')}",
            parse_mode="Markdown",
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cookies = context.user_data.get("last_cookies", "")
    engine = NetflixSessionEngine()

    if query.data == "bookmarklet":
        auth = engine.extract_auth_cookies(cookies)
        if auth:
            bm = engine.generate_bookmarklet(auth)
            await query.message.reply_text(
                f"📋 **Full Bookmarklet**\n\nCopy this entire line:\n\n`{bm}`\n\n"
                "1. Copy it\n2. Open a new tab\n3. Go to netflix.com (must be logged OUT)\n"
                "4. Paste in the URL bar and press Enter",
                parse_mode="Markdown",
            )

    elif query.data == "nftoken":
        await query.message.reply_text("⏳ Generating nftoken link...")
        try:
            result = engine.generate_nftoken(cookies)
            if result.get("success"):
                lines = [
                    "✅ **NFToken Generated!**\n",
                    "🔗 **Login Link:**",
                    f"`{result['url']}`\n",
                    "**How to use:**",
                    "1. Copy the full URL above",
                    "2. Open it in any browser (TV, laptop, phone)",
                    "3. You'll be logged in automatically\n",
                ]
                if result.get("expires"):
                    try:
                        exp_str = datetime.fromtimestamp(result["expires"]).strftime("%Y-%m-%d %H:%M:%S")
                        lines.append(f"⏰ Expires: `{exp_str}`")
                    except Exception:
                        pass
                await query.message.reply_text("\n".join(lines), parse_mode="Markdown")
            else:
                await query.message.reply_text(
                    f"❌ **NFToken failed**\n\n`{result.get('error', 'Unknown error')[:400]}`",
                    parse_mode="Markdown",
                )
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {str(e)}")


# ── MAIN ──────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookies))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot started polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
