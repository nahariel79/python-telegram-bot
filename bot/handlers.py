"""Telegram update handlers for the memecoin calls bot."""

import logging
import os

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import cache, db


logger = logging.getLogger(__name__)

# Keys used to read shared connections from Application.bot_data.
DB_KEY = "db"
REDIS_KEY = "redis"

# In-memory fallback for the per-user message counter when Redis is unavailable.
_LOCAL_MESSAGE_COUNTS: dict[int, int] = {}

BOT_COMMANDS = (
    ("start", "פתיחת הבוט"),
    ("help", "עזרה"),
    ("about", "מידע על הבוט"),
    ("ping", "בדיקת מצב הבוט"),
    ("channeltest", "בדיקת פרסום לערוץ"),
)

MENU_HELP = "עזרה"
MENU_ABOUT = "אודות"
MENU_PING = "בדיקת בוט"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_HELP, MENU_ABOUT], [MENU_PING]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="בחר פעולה",
)

HELP_TEXT = """פקודות זמינות:
/start - פתיחת הבוט
/help - עזרה
/about - מידע על הבוט
/ping - בדיקת מצב הבוט
/channeltest - בדיקת פרסום לערוץ

לבדיקת פרסום לערוץ אפשר גם לשלוח בפרטי את המילה: test
"""


def _channel_id_from_env() -> int | str | None:
    """Return CHANNEL_ID as int for numeric IDs or str for @usernames."""
    raw_value = os.getenv("CHANNEL_ID", "").strip()
    if not raw_value:
        return None

    try:
        return int(raw_value)
    except ValueError:
        # Allows using a public channel username such as @my_channel.
        return raw_value


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    # Persist the user in PostgreSQL when available.
    pool = context.bot_data.get(DB_KEY)
    is_new = True
    if pool is not None:
        is_new = await db.upsert_user(pool, user.id, user.username, user.first_name)

    name = user.first_name if user.first_name else ""
    greeting = "ברוך הבא" if is_new else "ברוך שובך"
    await message.reply_text(
        f"{greeting} {name}! הבוט פעיל.\n\n"
        "לבדיקת פרסום לערוץ שלח: test",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(HELP_TEXT)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        "בוט לניהול המלצות Memecoins בזמן אמת.\n"
        "בשלב זה אנחנו בודקים את החיבור בין הבוט לערוץ."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        await message.reply_text("✅ הבוט פעיל")
        return

    cached = await cache.get_or_set_ping(client)
    source = "cache" if cached else "fresh"
    await message.reply_text(f"✅ הבוט פעיל ({source})")


async def publish_channel_test(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Publish a test message to the configured Telegram channel."""
    message = update.effective_message
    if message is None:
        return

    channel_id = _channel_id_from_env()
    if channel_id is None:
        await message.reply_text(
            "❌ CHANNEL_ID לא מוגדר ב-Railway.\n"
            "הוסף משתנה בשם CHANNEL_ID והכנס בו את מזהה הערוץ."
        )
        return

    try:
        # get_chat validates that the bot can resolve the configured channel.
        chat = await context.bot.get_chat(chat_id=channel_id)

        await context.bot.send_message(
            chat_id=channel_id,
            text="בדיקת מערכת ✅\n\nהבוט מחובר לערוץ ומסוגל לפרסם הודעות.",
        )

        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "הערוץ"
        await message.reply_text(
            "✅ ההודעה פורסמה בערוץ בהצלחה.\n\n"
            f"ערוץ: {chat_name}\n"
            f"Chat ID: {chat.id}"
        )

    except Forbidden as exc:
        logger.warning("Telegram channel permission error: %s", exc)
        await message.reply_text(
            "❌ הבוט מצא את הערוץ אבל אין לו הרשאה לפרסם בו.\n\n"
            "בדוק שהבוט מוגדר כמנהל בערוץ ושיש לו הרשאת Post Messages."
        )
    except BadRequest as exc:
        logger.warning("Telegram channel bad request: %s", exc)
        await message.reply_text(
            "❌ Telegram לא הצליח למצוא או לפתוח את הערוץ לפי CHANNEL_ID.\n\n"
            f"השגיאה שהתקבלה: {exc}\n\n"
            "בדרך כלל זה אומר שה-CHANNEL_ID אינו נכון."
        )
    except Exception as exc:  # Keep the exact Telegram error visible during setup.
        logger.exception("Unexpected channel test error")
        await message.reply_text(
            "❌ הפרסום לערוץ נכשל.\n\n"
            f"שגיאה: {type(exc).__name__}: {exc}"
        )


async def channel_test_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await publish_channel_test(update, context)


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text == MENU_HELP:
        await help_command(update, context)
    elif text == MENU_ABOUT:
        await about(update, context)
    elif text == MENU_PING:
        await ping(update, context)


async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ordinary private messages sent to the bot."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message is None or not message.text or user is None or chat is None:
        return

    # Ignore channel/group posts. We currently accept input only in a private chat.
    if chat.type != "private":
        return

    text = message.text.strip()

    # First integration test: sending "test" privately publishes to the channel.
    if text.lower() in {"test", "בדיקה"}:
        await publish_channel_test(update, context)
        return

    # Keep the template's original message counter until CA handling is added.
    client = context.bot_data.get(REDIS_KEY)
    if client is not None:
        count = await cache.increment_message_count(client, user.id)
    else:
        count = _LOCAL_MESSAGE_COUNTS[user.id] = (
            _LOCAL_MESSAGE_COUNTS.get(user.id, 0) + 1
        )

    await message.reply_text(
        f"קיבלתי את ההודעה (#{count}).\n\n"
        "כרגע לצורך הבדיקה שלח: test"
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return

    await message.reply_text("פקודה לא מוכרת. שלח /help לקבלת עזרה.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    # Transient polling/network errors can occur briefly during Railway redeploys.
    if isinstance(error, (Conflict, NetworkError, TimedOut)):
        logger.warning("Transient Telegram error: %s", error)
        return

    logger.exception("Error while processing update: %s", update, exc_info=error)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "❌ אירעה שגיאה בזמן עיבוד ההודעה."
        )


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("channeltest", channel_test_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(
        MessageHandler(
            filters.Regex(f"^({MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"),
            menu_button,
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message))
