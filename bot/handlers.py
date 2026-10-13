"""Telegram update handlers for the memecoin calls bot."""

import asyncio
import csv
import io
import html
import json
import logging
import os
import re
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import cache, db, stats, webapp


logger = logging.getLogger(__name__)

DB_KEY = "db"
REDIS_KEY = "redis"

BOT_COMMANDS = (
    ("start", "Start the bot"),
    ("help", "Show help"),
    ("about", "Show bot information"),
    ("ping", "Check bot status"),
    ("myid", "Show your Telegram user ID"),
    ("channeltest", "Test publishing to the channel"),
    ("broadcast", "פרסום הודעה חופשית בערוץ"),
    ("cancelbroadcast", "ביטול טיוטת שידור"),
    ("support", "שליחת הודעה פרטית לצוות הערוץ"),
    ("bug", "דיווח על תקלה"),
    ("deletecall", "מחיקת המלצה שגויה - מנהל בלבד"),
)

MENU_HELP = "Help"
MENU_ABOUT = "About"
MENU_PING = "Ping"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_HELP, MENU_ABOUT], [MENU_PING]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Choose a menu item",
)

HELP_TEXT = """הבוט פעיל.

שלח לי כתובת CA של מטבע Solana, ואני אציג תצוגה מקדימה של ההמלצה.

פקודות:
/start - הפעלת הבוט
/channeltest - בדיקת פרסום לערוץ
/broadcast - יצירת הודעה חופשית לערוץ (מנהל בלבד)
/cancelbroadcast - ביטול טיוטת שידור
/support - פנייה פרטית לצוות הערוץ
/bug - דיווח על תקלה
/deletecall <CA/סימול> - מחיקת המלצה שגויה (מנהל בלבד)
/myid - הצגת Telegram User ID
/ping - בדיקת סטטוס
"""

SOLANA_CA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


LIST_ITEMS_PER_MESSAGE = 20

BROADCAST_STATE_KEY = "broadcast_draft"
BROADCAST_MAX_LINK_BUTTONS = 6
SUPPORT_MODE_KEY = "support_mode"
SUPPORT_REPLY_TO_KEY = "support_reply_to"


def _bot_deep_link(username: str, payload: str) -> str:
    return f"https://t.me/{username}?start={payload}"


def _lists_keyboard(bot_username: str, dex_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if dex_url:
        rows.append([InlineKeyboardButton("📈 DexScreener", url=dex_url)])

    day = webapp.dashboard_url("day") or _bot_deep_link(bot_username, "calls_day")
    month = webapp.dashboard_url("month") or _bot_deep_link(bot_username, "calls_month")
    all_time = webapp.dashboard_url("all") or _bot_deep_link(bot_username, "calls_all")
    rows.append([
        InlineKeyboardButton("🏆 מובילים יומיים", url=day),
        InlineKeyboardButton("🗓 מובילים חודשיים", url=month),
    ])
    rows.append([InlineKeyboardButton("🌟 כל הזמנים", url=all_time)])
    return InlineKeyboardMarkup(rows)


def _period_title(period: str) -> str:
    return {
        "day": "🏆 המובילים היומיים — 24 השעות האחרונות",
        "month": "🗓 המובילים החודשיים — 30 הימים האחרונים",
        "all": "🏆 כל הזמנים",
    }[period]


def _format_call_time_israel(value) -> str:
    if value is None:
        return "לא זמין"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo("Asia/Jerusalem"))
    return local.strftime("%d/%m/%Y • %H:%M")


def _call_list_item(row) -> str:
    entry = float(row["entry_valuation"])
    ath = float(row["highest_valuation"] or entry)
    multiple = ath / entry if entry > 0 else 0.0
    profit = ((ath / entry) - 1.0) * 100.0 if entry > 0 else 0.0
    metric = "MC" if row["valuation_type"] == "marketCap" else "FDV"

    return (
        f"🪙 {row['symbol']}\n"
        f"💰 {metric} בזמן ההמלצה: {_fmt_usd(entry)}\n"
        f"🕒 פורסם: {_format_call_time_israel(row['call_time'])}\n"
        f"📈 רווח לפי ATH: {profit:+.0f}% • {multiple:.2f}X\n"
        f"🏆 ATH: {_fmt_usd(ath)}"
    )


async def _send_calls_list(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
) -> None:
    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await message.reply_text("❌ מסד הנתונים אינו זמין כרגע.")
        return

    rows = await db.get_calls_for_period(pool, period)
    if not rows:
        await message.reply_text(
            f"{_period_title(period)}\n\nאין עדיין המלצות בתקופה הזאת."
        )
        return

    total = len(rows)
    chunks = [
        rows[i:i + LIST_ITEMS_PER_MESSAGE]
        for i in range(0, total, LIST_ITEMS_PER_MESSAGE)
    ]

    for index, chunk in enumerate(chunks, start=1):
        body = "\n\n".join(_call_list_item(row) for row in chunk)
        header = f"{_period_title(period)}\nסה״כ המלצות: {total}"
        if len(chunks) > 1:
            header += f"\nעמוד {index}/{len(chunks)}"

        await message.reply_text(
            f"{header}\n\n{body}",
            disable_web_page_preview=True,
        )


def _channel_target() -> str | int | None:
    value = os.getenv("CHANNEL_ID", "").strip()
    if not value:
        return None
    if value.startswith("@"):
        return value
    try:
        return int(value)
    except ValueError:
        return value


def _admin_allowed(user_id: int) -> bool:
    configured = os.getenv("ADMIN_USER_ID", "").strip()
    if not configured:
        return True
    try:
        return user_id == int(configured)
    except ValueError:
        logger.warning("ADMIN_USER_ID is not a valid integer.")
        return True


def _fmt_usd(value: float | int | None) -> str:
    if value is None:
        return "לא זמין"
    value = float(value)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.10f}".rstrip("0").rstrip(".")


def _age_text(pair_created_at_ms: int | None) -> str:
    if not pair_created_at_ms:
        return "לא זמין"

    created = datetime.fromtimestamp(pair_created_at_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds = max(0, int((now - created).total_seconds()))

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days:
        return f"{days} ימים ו-{hours} שעות"
    if hours:
        return f"{hours} שעות ו-{minutes} דקות"
    return f"{minutes} דקות"


def _dex_request(token_address: str) -> list[dict]:
    url = f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Telegram-Memecoin-Calls-Bot/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))

    return data if isinstance(data, list) else []


async def _fetch_best_pair(token_address: str) -> dict | None:
    pairs = await asyncio.to_thread(_dex_request, token_address)
    if not pairs:
        return None

    token_lower = token_address.lower()

    preferred = [
        p
        for p in pairs
        if str(p.get("chainId", "")).lower() == "solana"
        and str((p.get("baseToken") or {}).get("address", "")).lower() == token_lower
    ]

    candidates = preferred or [
        p for p in pairs if str(p.get("chainId", "")).lower() == "solana"
    ]
    if not candidates:
        return None

    def score(pair: dict) -> tuple[float, float]:
        liquidity = ((pair.get("liquidity") or {}).get("usd")) or 0
        volume_24h = ((pair.get("volume") or {}).get("h24")) or 0
        try:
            liquidity = float(liquidity)
        except (TypeError, ValueError):
            liquidity = 0.0
        try:
            volume_24h = float(volume_24h)
        except (TypeError, ValueError):
            volume_24h = 0.0
        return liquidity, volume_24h

    return max(candidates, key=score)


def _call_data_from_pair(token_address: str, pair: dict) -> dict:
    base = pair.get("baseToken") or {}

    def as_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    market_cap = as_float(pair.get("marketCap"))
    fdv = as_float(pair.get("fdv"))
    liquidity = as_float((pair.get("liquidity") or {}).get("usd"))
    price_usd = as_float(pair.get("priceUsd"))
    created_at = pair.get("pairCreatedAt")
    pair_address = pair.get("pairAddress")

    dex_url = pair.get("url")
    if not dex_url and pair_address:
        dex_url = f"https://dexscreener.com/solana/{pair_address}"

    if market_cap is not None and market_cap > 0:
        valuation_type = "marketCap"
        entry_valuation = market_cap
    elif fdv is not None and fdv > 0:
        valuation_type = "fdv"
        entry_valuation = fdv
    else:
        valuation_type = None
        entry_valuation = None

    return {
        "token_address": token_address,
        "name": base.get("name") or "Unknown",
        "symbol": base.get("symbol") or "UNKNOWN",
        "price_usd": price_usd,
        "market_cap": market_cap,
        "fdv": fdv,
        "liquidity": liquidity,
        "pair_created_at": created_at,
        "pair_address": pair_address,
        "dex_url": dex_url,
        "valuation_type": valuation_type,
        "entry_valuation": entry_valuation,
    }


def _preview_text(call: dict) -> str:
    if call["valuation_type"] == "marketCap":
        valuation_line = f"💰 שווי שוק: {_fmt_usd(call['entry_valuation'])}"
    elif call["valuation_type"] == "fdv":
        valuation_line = f"💰 FDV: {_fmt_usd(call['entry_valuation'])} (Market Cap לא זמין)"
    else:
        valuation_line = "💰 שווי שוק: לא זמין"

    return (
        "🔎 תצוגה מקדימה להמלצה\n\n"
        f"🪙 {call['name']} (${call['symbol']})\n"
        f"{valuation_line}\n"
        f"💵 מחיר: {_fmt_usd(call['price_usd'])}\n"
        f"💧 נזילות: {_fmt_usd(call['liquidity'])}\n"
        f"⏱ גיל ה-Pair: {_age_text(call['pair_created_at'])}\n\n"
        f"📍 CA:\n{call['token_address']}\n\n"
        "אם הנתונים נכונים, לחץ על „פרסם המלצה”."
    )



def _channel_call_text(call: dict) -> str:
    metric = "MC" if call["valuation_type"] == "marketCap" else "FDV"
    name = html.escape(str(call["name"]))
    symbol = html.escape(str(call["symbol"]))
    token_address = html.escape(str(call["token_address"]))

    return (
        "🚨🚨 <b>CALL חדש</b> 🚨🚨\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name}  •  ${symbol}</b>\n\n"
        f"💰 <b>{metric} בפרסום:</b> {_fmt_usd(call['entry_valuation'])}\n"
        f"💵 <b>מחיר:</b> {_fmt_usd(call['price_usd'])}\n"
        f"💧 <b>נזילות:</b> {_fmt_usd(call['liquidity'])}\n"
        f"⏱ <b>גיל המטבע:</b> {_age_text(call['pair_created_at'])}\n\n"
        "📍 <b>CA</b>\n"
        f"<code>{token_address}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>אזהרה: מסחר בקריפטו וב־Memecoins הוא תנודתי ומסוכן ועלול להביא "
        "לאובדן מלא של ההשקעה. אין בפרסום משום ייעוץ או המלצה אישית. "
        "כל החלטת מסחר היא באחריות המשתמש בלבד.</i>"
    )


async def _send_calls_csv(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    period: str,
) -> None:
    """Send a follower a CSV snapshot of the selected ranked list."""
    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await message.reply_text("❌ מסד הנתונים אינו זמין כרגע.")
        return

    rows = await db.get_calls_for_period(pool, period)
    if not rows:
        await message.reply_text("אין עדיין המלצות לייצוא בתקופה הזאת.")
        return

    period_name = {
        "day": "daily",
        "month": "monthly",
        "all": "all_time",
    }[period]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Rank",
        "Symbol",
        "Token Name",
        "CA",
        "Recommendation Date (Israel)",
        "Recommendation Time (Israel)",
        "Entry Metric",
        "Entry MC/FDV USD",
        "ATH MC/FDV USD",
        "ATH Profit %",
        "ATH Multiple X",
        "DexScreener",
    ])

    israel = ZoneInfo("Asia/Jerusalem")
    for index, row in enumerate(rows, start=1):
        entry = float(row["entry_valuation"])
        ath = float(row["highest_valuation"] or entry)
        profit = ((ath / entry) - 1.0) * 100.0 if entry > 0 else 0.0
        multiple = ath / entry if entry > 0 else 0.0
        call_time = row["call_time"]
        if call_time.tzinfo is None:
            call_time = call_time.replace(tzinfo=timezone.utc)
        call_time = call_time.astimezone(israel)
        metric = "MC" if row["valuation_type"] == "marketCap" else "FDV"

        writer.writerow([
            index,
            row["symbol"],
            row["token_name"],
            row["token_address"],
            call_time.strftime("%d/%m/%Y"),
            call_time.strftime("%H:%M"),
            metric,
            f"{entry:.8f}",
            f"{ath:.8f}",
            f"{profit:.2f}",
            f"{multiple:.4f}",
            row["dexscreener_url"] or "",
        ])

    # UTF-8 BOM keeps Hebrew readable when the CSV is opened in Excel.
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    filename = f"memecoin_calls_{period_name}.csv"
    await message.reply_document(
        document=InputFile(data, filename=filename),
        caption=(
            "📥 קובץ ההמלצות להורדה.\n"
            "הנתונים משקפים את ה־ATH שנשמר במערכת בזמן יצירת הקובץ."
        ),
    )



def _support_admin_id() -> int | None:
    configured = os.getenv("ADMIN_USER_ID", "").strip()
    if not configured:
        return None
    try:
        return int(configured)
    except ValueError:
        logger.warning("ADMIN_USER_ID is not a valid integer.")
        return None


def _support_kind_label(mode: str) -> str:
    return "🐞 דיווח על תקלה" if mode == "bug" else "💬 שאלה / פנייה פרטית"


async def _begin_support(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
) -> None:
    if _support_admin_id() is None:
        await message.reply_text(
            "❌ מערכת הפניות עדיין לא הוגדרה. נסו שוב מאוחר יותר."
        )
        return

    context.user_data[SUPPORT_MODE_KEY] = mode
    if mode == "bug":
        text = (
            "🐞 <b>דיווח על תקלה</b>\n\n"
            "שלחו עכשיו הודעה עם תיאור התקלה. אפשר לצרף גם צילום מסך.\n"
            "כדאי לציין מה לחצתם ומה ציפיתם לראות."
        )
    else:
        text = (
            "💬 <b>פנייה פרטית לצוות הערוץ</b>\n\n"
            "שלחו עכשיו את השאלה או ההודעה שלכם. אפשר לצרף גם צילום מסך.\n"
            "הפנייה תועבר למנהל הערוץ באופן פרטי."
        )
    await message.reply_text(text, parse_mode="HTML")


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.chat.type != "private":
        return
    await _begin_support(message, context, mode="support")


async def bug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.chat.type != "private":
        return
    await _begin_support(message, context, mode="bug")


async def _relay_support_message(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = message.from_user
    if user is None:
        return
    admin_id = _support_admin_id()
    if admin_id is None:
        await message.reply_text("❌ מערכת הפניות אינה זמינה כרגע.")
        return

    mode = context.user_data.pop(SUPPORT_MODE_KEY, "support")
    username = f"@{user.username}" if user.username else "ללא username"
    name = html.escape(user.full_name or user.first_name or "משתמש")
    label = _support_kind_label(mode)
    info = await context.bot.send_message(
        chat_id=admin_id,
        text=(
            f"{label}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>{name}</b> • {html.escape(username)}\n"
            f"🆔 <code>{user.id}</code>\n\n"
            "ההודעה עצמה מופיעה מיד מתחת."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "✉️ השב בפרטי",
            callback_data=f"support_reply:{user.id}",
        )]]),
    )
    try:
        await context.bot.copy_message(
            chat_id=admin_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
    except Exception:
        logger.exception("Could not copy support message to admin")
        try:
            await context.bot.edit_message_text(
                chat_id=admin_id,
                message_id=info.message_id,
                text=f"{label}\n\n⚠️ לא הצלחתי להעתיק את תוכן הפנייה.",
            )
        except Exception:
            pass
        await message.reply_text("❌ הייתה תקלה בשליחת הפנייה. נסו שוב.")
        return

    await message.reply_text(
        "✅ ההודעה נשלחה לצוות הערוץ.\n"
        "אם תתקבל תשובה, היא תגיע אליכם כאן בשיחה הפרטית עם הבוט."
    )


async def support_reply_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()

    admin_id = _support_admin_id()
    if admin_id is None or user.id != admin_id:
        await query.answer("אין הרשאה", show_alert=True)
        return

    try:
        target_user_id = int((query.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        return

    context.user_data[SUPPORT_REPLY_TO_KEY] = target_user_id
    await context.bot.send_message(
        chat_id=admin_id,
        text=(
            f"✉️ שלח עכשיו תשובה למשתמש <code>{target_user_id}</code>.\n"
            "ההודעה הבאה שתשלח לבוט תועבר אליו בפרטי."
        ),
        parse_mode="HTML",
    )


async def _send_support_reply(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_user_id = context.user_data.pop(SUPPORT_REPLY_TO_KEY, None)
    if not target_user_id:
        return

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text="💬 <b>תשובה מצוות הערוץ:</b>",
            parse_mode="HTML",
        )
        await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        await message.reply_text("✅ התשובה נשלחה בפרטי.")
    except Exception as exc:
        logger.exception("Could not send support reply")
        await message.reply_text(f"❌ לא הצלחתי לשלוח את התשובה: {exc}")


async def private_photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.chat.type != "private":
        return

    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if draft and draft.get("mode") == "awaiting_content":
        if not _broadcast_admin_allowed(user.id):
            context.user_data.pop(BROADCAST_STATE_KEY, None)
            await message.reply_text("⛔ אין לך הרשאה לשדר הודעות לערוץ.")
            return
        await _capture_broadcast_content(message, context)
        return

    admin_id = _support_admin_id()
    if admin_id is not None and user.id == admin_id and context.user_data.get(SUPPORT_REPLY_TO_KEY):
        await _send_support_reply(message, context)
        return

    if context.user_data.get(SUPPORT_MODE_KEY):
        await _relay_support_message(message, context)
        return

def _broadcast_admin_allowed(user_id: int) -> bool:
    """Broadcasts are intentionally stricter than call publishing.

    ADMIN_USER_ID must be configured so no follower can ever publish arbitrary
    channel content if the bot is exposed publicly.
    """
    configured = os.getenv("ADMIN_USER_ID", "").strip()
    if not configured:
        return False
    try:
        return user_id == int(configured)
    except ValueError:
        logger.warning("ADMIN_USER_ID is not a valid integer.")
        return False


def _valid_broadcast_url(value: str) -> bool:
    value = value.strip()
    if value.startswith("tg://"):
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _broadcast_public_keyboard(draft: dict) -> InlineKeyboardMarkup | None:
    buttons = draft.get("buttons") or []
    if not buttons:
        return None

    rows = []
    for index in range(0, len(buttons), 2):
        rows.append([
            InlineKeyboardButton(item["label"], url=item["url"])
            for item in buttons[index:index + 2]
        ])
    return InlineKeyboardMarkup(rows)


def _broadcast_preview_keyboard(draft: dict) -> InlineKeyboardMarkup:
    rows = []
    buttons = draft.get("buttons") or []
    for index in range(0, len(buttons), 2):
        rows.append([
            InlineKeyboardButton(item["label"], url=item["url"])
            for item in buttons[index:index + 2]
        ])

    if len(buttons) < BROADCAST_MAX_LINK_BUTTONS:
        rows.append([
            InlineKeyboardButton("➕ הוסף כפתור קישור", callback_data="broadcast_add_button")
        ])

    rows.append([
        InlineKeyboardButton("✅ פרסם בערוץ", callback_data="broadcast_publish"),
        InlineKeyboardButton("📌 פרסם והצמד", callback_data="broadcast_publish_pin"),
    ])
    rows.append([
        InlineKeyboardButton("✏️ החלף תוכן", callback_data="broadcast_replace"),
        InlineKeyboardButton("❌ ביטול", callback_data="broadcast_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


async def _capture_broadcast_content(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get(BROADCAST_STATE_KEY) or {}
    old_preview_id = draft.get("preview_message_id")
    buttons = list(draft.get("buttons") or [])

    if old_preview_id:
        try:
            await context.bot.delete_message(
                chat_id=message.chat_id,
                message_id=old_preview_id,
            )
        except Exception:
            logger.debug("Could not remove old broadcast preview", exc_info=True)

    draft = {
        "mode": "preview",
        "source_chat_id": message.chat_id,
        "source_message_id": message.message_id,
        "buttons": buttons,
    }

    preview = await context.bot.copy_message(
        chat_id=message.chat_id,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
        reply_markup=_broadcast_preview_keyboard(draft),
    )
    draft["preview_message_id"] = preview.message_id
    context.user_data[BROADCAST_STATE_KEY] = draft

    await message.reply_text(
        "👆 זו התצוגה המקדימה.\n"
        "העיצוב, הקישורים וההדגשות שהוספת בטלגרם נשמרים.\n"
        "אפשר להוסיף כפתור קישור, לפרסם, להצמיד, להחליף תוכן או לבטל."
    )


async def _capture_broadcast_button_spec(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if not draft:
        return

    if "|" not in text:
        await message.reply_text(
            "שלח כך בדיוק:\n"
            "שם הכפתור | https://example.com\n\n"
            "לדוגמה:\n📈 לאתר | https://example.com"
        )
        return

    label, url = (part.strip() for part in text.split("|", 1))
    if not label or len(label) > 64:
        await message.reply_text("❌ שם הכפתור חייב להכיל 1–64 תווים.")
        return
    if not _valid_broadcast_url(url):
        await message.reply_text("❌ הקישור אינו תקין. השתמש ב-http://, https:// או tg://")
        return

    buttons = list(draft.get("buttons") or [])
    if len(buttons) >= BROADCAST_MAX_LINK_BUTTONS:
        draft["mode"] = "preview"
        await message.reply_text("הגעת למספר הכפתורים המרבי להודעה.")
        return

    buttons.append({"label": label, "url": url})
    draft["buttons"] = buttons
    draft["mode"] = "preview"
    context.user_data[BROADCAST_STATE_KEY] = draft

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=message.chat_id,
            message_id=draft["preview_message_id"],
            reply_markup=_broadcast_preview_keyboard(draft),
        )
    except Exception:
        logger.exception("Could not refresh broadcast preview keyboard")

    await message.reply_text("✅ הכפתור נוסף לתצוגה המקדימה.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if message.chat.type != "private":
        await message.reply_text("את פקודת השידור מפעילים בשיחה פרטית עם הבוט.")
        return

    if not _broadcast_admin_allowed(user.id):
        configured = os.getenv("ADMIN_USER_ID", "").strip()
        if not configured:
            await message.reply_text(
                "🔒 שידור חופשי נעול עד להגדרת ADMIN_USER_ID ב-Railway.\n\n"
                "שלח /myid, העתק את המספר, והוסף אותו ב-Railway כמשתנה ADMIN_USER_ID."
            )
        else:
            await message.reply_text("⛔ אין לך הרשאה לשדר הודעות לערוץ.")
        return

    context.user_data[BROADCAST_STATE_KEY] = {
        "mode": "awaiting_content",
        "buttons": [],
    }
    await message.reply_text(
        "📣 מצב שידור הופעל.\n\n"
        "שלח עכשיו את ההודעה בדיוק כפי שתרצה שתופיע בערוץ.\n"
        "אפשר לשלוח:\n"
        "• טקסט חופשי עם Bold / Italic / קישורים / אימוג׳ים\n"
        "• תמונה עם כיתוב\n\n"
        "אחרי השליחה אראה לך תצוגה מקדימה לפני פרסום."
    )


async def cancel_broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not _broadcast_admin_allowed(user.id):
        await message.reply_text("⛔ אין לך הרשאה.")
        return

    draft = context.user_data.pop(BROADCAST_STATE_KEY, None)
    if draft and draft.get("preview_message_id"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=message.chat_id,
                message_id=draft["preview_message_id"],
                reply_markup=None,
            )
        except Exception:
            pass
    await message.reply_text("❌ טיוטת השידור בוטלה.")


async def broadcast_media_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if not draft or draft.get("mode") != "awaiting_content":
        return
    if not _broadcast_admin_allowed(user.id):
        context.user_data.pop(BROADCAST_STATE_KEY, None)
        await message.reply_text("⛔ אין לך הרשאה לשדר הודעות לערוץ.")
        return

    await _capture_broadcast_content(message, context)


async def broadcast_add_button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()

    if not _broadcast_admin_allowed(user.id):
        await context.bot.send_message(chat_id=user.id, text="⛔ אין לך הרשאה.")
        return

    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if not draft:
        await context.bot.send_message(chat_id=user.id, text="❌ הטיוטה כבר לא זמינה.")
        return

    if len(draft.get("buttons") or []) >= BROADCAST_MAX_LINK_BUTTONS:
        await context.bot.send_message(chat_id=user.id, text="הגעת למספר הכפתורים המרבי.")
        return

    draft["mode"] = "awaiting_button"
    context.user_data[BROADCAST_STATE_KEY] = draft
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "שלח את הכפתור בפורמט:\n"
            "שם הכפתור | https://example.com\n\n"
            "לדוגמה:\n📈 כניסה לאתר | https://example.com"
        ),
    )


async def _publish_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    pin: bool,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()

    if not _broadcast_admin_allowed(user.id):
        await context.bot.send_message(chat_id=user.id, text="⛔ אין לך הרשאה.")
        return

    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if not draft or not draft.get("source_message_id"):
        await context.bot.send_message(chat_id=user.id, text="❌ הטיוטה כבר לא זמינה.")
        return

    target = _channel_target()
    if target is None:
        await context.bot.send_message(
            chat_id=user.id,
            text="❌ CHANNEL_ID לא מוגדר ב-Railway.",
        )
        return

    try:
        sent = await context.bot.copy_message(
            chat_id=target,
            from_chat_id=draft["source_chat_id"],
            message_id=draft["source_message_id"],
            reply_markup=_broadcast_public_keyboard(draft),
        )
    except Exception as exc:
        logger.exception("Broadcast publication failed")
        await context.bot.send_message(
            chat_id=user.id,
            text=f"❌ לא הצלחתי לפרסם את ההודעה בערוץ.\nשגיאה: {exc}",
        )
        return

    pin_warning = None
    if pin:
        try:
            await context.bot.pin_chat_message(
                chat_id=target,
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception as exc:
            logger.exception("Broadcast published but pinning failed")
            pin_warning = str(exc)

    context.user_data.pop(BROADCAST_STATE_KEY, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    result = "✅ ההודעה פורסמה בערוץ."
    if pin and not pin_warning:
        result += "\n📌 ההודעה גם הוצמדה."
    elif pin_warning:
        result += f"\n⚠️ הפרסום הצליח, אבל ההצמדה נכשלה: {pin_warning}"

    await context.bot.send_message(chat_id=user.id, text=result)


async def broadcast_publish_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _publish_broadcast(update, context, pin=False)


async def broadcast_publish_pin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _publish_broadcast(update, context, pin=True)


async def broadcast_replace_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()

    if not _broadcast_admin_allowed(user.id):
        await context.bot.send_message(chat_id=user.id, text="⛔ אין לך הרשאה.")
        return

    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if not draft:
        return
    draft["mode"] = "awaiting_content"
    context.user_data[BROADCAST_STATE_KEY] = draft

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=user.id,
        text="✏️ שלח עכשיו את התוכן החדש. הכפתורים שכבר הוספת יישמרו.",
    )


async def broadcast_cancel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()

    if not _broadcast_admin_allowed(user.id):
        return

    context.user_data.pop(BROADCAST_STATE_KEY, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await context.bot.send_message(chat_id=user.id, text="❌ טיוטת השידור בוטלה.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    pool = context.bot_data.get(DB_KEY)
    is_new = True
    if pool is not None:
        is_new = await db.upsert_user(pool, user.id, user.username, user.first_name)

    payload = context.args[0] if context.args else None
    payload_to_period = {
        "calls_day": "day",
        "calls_month": "month",
        "calls_all": "all",
    }
    export_to_period = {
        "export_day": "day",
        "export_month": "month",
        "export_all": "all",
    }

    if payload in {"support", "bug"}:
        await _begin_support(message, context, mode=payload)
        return

    if payload in payload_to_period:
        await _send_calls_list(message, context, payload_to_period[payload])
        return

    if payload in export_to_period:
        await _send_calls_csv(message, context, export_to_period[payload])
        return

    name = user.first_name if user.first_name else "friend"
    greeting = "ברוך הבא" if is_new else "ברוך שובך"

    await message.reply_text(
        f"{greeting}, {name}!\n\n"
        "שלח לי CA של מטבע Solana כדי להכין תצוגה מקדימה להמלצה.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message is not None:
        await update.effective_message.reply_text(HELP_TEXT)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message is not None:
        await update.effective_message.reply_text("בוט המלצות Memecoins בזמן אמת.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        await message.reply_text("pong")
        return

    cached = await cache.get_or_set_ping(client)
    source = "cached" if cached else "fresh"
    await message.reply_text(f"pong ({source})")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    user = update.effective_user
    if message is not None and user is not None:
        await message.reply_text(f"Telegram User ID שלך:\n{user.id}")


async def channel_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    target = _channel_target()
    if target is None:
        await message.reply_text("❌ CHANNEL_ID לא מוגדר ב-Railway.")
        return

    try:
        sent = await context.bot.send_message(
            chat_id=target,
            text="בדיקת מערכת ✅\nהבוט מחובר לערוץ ומסוגל לפרסם הודעות.",
        )
        await message.reply_text(
            f"✅ ההודעה פורסמה בערוץ.\nChat ID בפועל: {sent.chat.id}"
        )
    except Exception as exc:
        logger.exception("Channel test failed")
        await message.reply_text(
            "❌ לא הצלחתי לפרסם בערוץ.\n\n"
            f"השגיאה שהתקבלה: {exc}"
        )


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
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or user is None:
        return

    text = message.text.strip()

    draft = context.user_data.get(BROADCAST_STATE_KEY)
    if draft and draft.get("mode") == "awaiting_content":
        if not _broadcast_admin_allowed(user.id):
            context.user_data.pop(BROADCAST_STATE_KEY, None)
            await message.reply_text("⛔ אין לך הרשאה לשדר הודעות לערוץ.")
            return
        await _capture_broadcast_content(message, context)
        return

    if draft and draft.get("mode") == "awaiting_button":
        if not _broadcast_admin_allowed(user.id):
            context.user_data.pop(BROADCAST_STATE_KEY, None)
            await message.reply_text("⛔ אין לך הרשאה לשדר הודעות לערוץ.")
            return
        await _capture_broadcast_button_spec(message, context, text)
        return

    admin_id = _support_admin_id()
    if admin_id is not None and user.id == admin_id and context.user_data.get(SUPPORT_REPLY_TO_KEY):
        await _send_support_reply(message, context)
        return

    if context.user_data.get(SUPPORT_MODE_KEY):
        await _relay_support_message(message, context)
        return

    if text.lower() in {"test", "בדיקה"}:
        await channel_test(update, context)
        return

    if not _admin_allowed(user.id):
        await message.reply_text("⛔ אין לך הרשאה לפרסם המלצות דרך הבוט.")
        return

    if not SOLANA_CA_RE.fullmatch(text):
        await message.reply_text("שלח כתובת CA תקינה של Solana.")
        return

    pool = context.bot_data.get(DB_KEY)
    if pool is not None:
        existing = await db.get_existing_call(pool, text)
        if existing is not None:
            await message.reply_text(
                "⛔ המטבע הזה כבר פורסם בעבר ולכן לא ניתן לפרסם אותו שוב.\n\n"
                f"${existing['symbol']} | Call ID: {existing['id']}\n"
                f"MC/FDV בפרסום: {_fmt_usd(existing['entry_valuation'])}\n"
                f"ATH מאז הפרסום: {_fmt_usd(existing['highest_valuation'])}\n"
                f"שיא: {float(existing['highest_profit_percent'] or 0):+.0f}%"
            )
            return

    status = await message.reply_text("🔍 בודק את המטבע ב-DexScreener...")

    try:
        pair = await _fetch_best_pair(text)
    except Exception as exc:
        logger.exception("DexScreener request failed")
        await status.edit_text(
            "❌ לא הצלחתי לקבל כרגע נתונים מ-DexScreener.\n"
            f"שגיאה: {exc}"
        )
        return

    if not pair:
        await status.edit_text("❌ לא מצאתי Pair של המטבע הזה ב-DexScreener.")
        return

    call = _call_data_from_pair(text, pair)

    if not call["pair_address"] or call["entry_valuation"] is None:
        await status.edit_text(
            "❌ לא ניתן לעקוב אחרי המטבע כי DexScreener לא החזיר Pair ושווי שוק/FDV תקינים."
        )
        return

    context.user_data["pending_call"] = call

    buttons = [[
        InlineKeyboardButton("✅ פרסם המלצה", callback_data="publish_call"),
        InlineKeyboardButton("❌ ביטול", callback_data="cancel_call"),
    ]]

    if call.get("dex_url"):
        buttons.append([InlineKeyboardButton("📈 DexScreener", url=call["dex_url"])])

    await status.edit_text(
        _preview_text(call),
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def publish_call_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    await query.answer()

    if not _admin_allowed(user.id):
        await query.edit_message_text("⛔ אין לך הרשאה לפרסם המלצות.")
        return

    call = context.user_data.get("pending_call")
    if not call:
        await query.edit_message_text(
            "❌ התצוגה המקדימה כבר אינה זמינה. שלח שוב את ה-CA."
        )
        return

    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await query.edit_message_text(
            "❌ מסד הנתונים אינו זמין כרגע, ולכן ההמלצה לא תפורסם ללא מעקב."
        )
        return

    # Final duplicate guard immediately before publication.
    existing = await db.get_existing_call(pool, call["token_address"])
    if existing is not None:
        context.user_data.pop("pending_call", None)
        await query.edit_message_text(
            "⛔ המטבע הזה כבר פורסם בעבר ולא ניתן לפרסם אותו שוב.\n\n"
            f"${existing['symbol']} | Call ID: {existing['id']}\n"
            f"MC/FDV בפרסום: {_fmt_usd(existing['entry_valuation'])}\n"
            f"ATH מאז הפרסום: {_fmt_usd(existing['highest_valuation'])}"
        )
        return

    target = _channel_target()
    if target is None:
        await query.edit_message_text("❌ CHANNEL_ID לא מוגדר ב-Railway.")
        return

    # Publish first with a temporary DexScreener-only button. After the call is
    # saved, channel-native daily/weekly/all-time list buttons are attached.
    temp_rows = []
    if call.get("dex_url"):
        temp_rows.append([InlineKeyboardButton("📈 DexScreener", url=call["dex_url"])])
    temp_keyboard = InlineKeyboardMarkup(temp_rows) if temp_rows else None

    try:
        sent = await context.bot.send_message(
            chat_id=target,
            text=_channel_call_text(call),
            parse_mode="HTML",
            reply_markup=temp_keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("Publishing call failed")
        await query.edit_message_text(
            "❌ לא הצלחתי לפרסם את ההמלצה בערוץ.\n\n"
            f"שגיאה: {exc}"
        )
        return

    try:
        call_id = await db.insert_call(
            pool,
            token_address=call["token_address"],
            token_name=call["name"],
            symbol=call["symbol"],
            pair_address=call["pair_address"],
            dexscreener_url=call.get("dex_url"),
            valuation_type=call["valuation_type"],
            entry_price=call["price_usd"],
            entry_valuation=call["entry_valuation"],
            channel_chat_id=sent.chat.id,
            telegram_message_id=sent.message_id,
        )
    except db.DuplicateCallError:
        try:
            await context.bot.delete_message(
                chat_id=sent.chat.id,
                message_id=sent.message_id,
            )
        except Exception:
            logger.exception("Could not delete duplicate channel message")
        context.user_data.pop("pending_call", None)
        await query.edit_message_text(
            "⛔ המטבע כבר פורסם בעבר. הפרסום החדש בוטל אוטומטית."
        )
        return
    except Exception as exc:
        logger.exception("Saving call failed")
        try:
            await context.bot.delete_message(
                chat_id=sent.chat.id,
                message_id=sent.message_id,
            )
        except Exception:
            logger.exception("Could not delete untracked channel message")

        await query.edit_message_text(
            "❌ ההמלצה לא נשמרה למסד הנתונים ולכן ביטלתי את הפרסום.\n\n"
            f"שגיאה: {exc}"
        )
        return

    # Repair/create the channel-native list posts.  A failure here no longer
    # prevents the recommendation text itself from being finalized.
    try:
        await stats.refresh_all_stats_pages(
            context.application,
            pool,
            channel_chat_id=sent.chat.id,
        )
    except Exception:
        logger.exception("Could not refresh channel stats pages; continuing with call")

    try:
        final_keyboard = await stats.recommendation_keyboard(
            context.application,
            pool,
            channel_chat_id=sent.chat.id,
            dex_url=call.get("dex_url"),
        )
    except Exception:
        logger.exception("Could not build stats keyboard; keeping DexScreener button")
        final_keyboard = temp_keyboard

    # Finalize the recommendation with the persistent list buttons.
    try:
        await context.bot.edit_message_text(
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            text=_channel_call_text(call),
            parse_mode="HTML",
            reply_markup=final_keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Could not finalize new call text/keyboard")

    context.user_data.pop("pending_call", None)

    await query.edit_message_text(
        "✅ ההמלצה פורסמה ונכנסה למעקב אוטומטי.\n\n"
        f"{call['name']} (${call['symbol']})\n"
        f"Call ID: {call_id}\n"
        "הכפתורים ברשומה מפנים לרשימות בתוך הערוץ עצמו."
    )


async def cancel_call_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    context.user_data.pop("pending_call", None)
    await query.edit_message_text("❌ פרסום ההמלצה בוטל.")


async def delete_call_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not _broadcast_admin_allowed(user.id):
        await message.reply_text("⛔ הפקודה זמינה למנהל בלבד. ודא ש-ADMIN_USER_ID מוגדר ב-Railway.")
        return
    if not context.args:
        await message.reply_text(
            "🗑 למחיקת המלצה שגויה שלח:\n"
            "/deletecall CA\n\n"
            "אפשר גם Call ID או סימול מדויק, למשל: /deletecall PENIS"
        )
        return
    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await message.reply_text("❌ מסד הנתונים אינו זמין כרגע.")
        return

    term = " ".join(context.args).strip()
    matches = await db.find_calls_for_admin_delete(pool, term)
    if not matches:
        await message.reply_text("לא נמצאה המלצה תואמת למחיקה.")
        return
    if len(matches) > 1:
        lines = ["נמצאו כמה התאמות. השתמש ב-CA המלא או ב-Call ID:"]
        for row in matches:
            ca = str(row["token_address"])
            lines.append(
                f"• ID {row['id']} | ${row['symbol']} | {row['token_name']} | "
                f"{ca[:6]}…{ca[-6:]}"
            )
        await message.reply_text("\n".join(lines))
        return

    row = matches[0]
    entry = float(row["entry_valuation"] or 0.0)
    ath = float(row["highest_valuation"] or entry)
    profit = ((ath / entry) - 1.0) * 100.0 if entry > 0 else 0.0
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ כן, מחק הכל", callback_data=f"delete_call_confirm:{row['id']}"),
        InlineKeyboardButton("❌ ביטול", callback_data="delete_call_cancel"),
    ]])
    await message.reply_text(
        "🗑 <b>מחיקת המלצה</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{html.escape(str(row['token_name']))} (${html.escape(str(row['symbol']))})</b>\n"
        f"🆔 Call ID: <b>{row['id']}</b>\n"
        f"🎯 MC/FDV בפרסום: <b>{_fmt_usd(entry)}</b>\n"
        f"🏆 ATH שמור: <b>{_fmt_usd(ath)}</b>\n"
        f"📈 רווח ATH: <b>{profit:+.1f}%</b>\n"
        f"🕒 פורסם: {_format_call_time_israel(row['call_time'])}\n\n"
        "המחיקה תסיר את ה-Call ואת יעדי הרווח שלו מה-DB ומהסטטיסטיקות. "
        "הבוט גם ינסה למחוק את ההודעות המשויכות מהערוץ.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def delete_call_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    if not _broadcast_admin_allowed(user.id):
        await query.edit_message_text("⛔ אין הרשאה למחיקה.")
        return
    try:
        call_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError, AttributeError):
        await query.edit_message_text("❌ מזהה Call לא תקין.")
        return
    pool = context.bot_data.get(DB_KEY)
    if pool is None:
        await query.edit_message_text("❌ מסד הנתונים אינו זמין כרגע.")
        return

    call, milestones = await db.get_call_delete_bundle(pool, call_id)
    if call is None:
        await query.edit_message_text("ה-Call כבר אינו קיים במסד הנתונים.")
        return

    chat_id = int(call["channel_chat_id"])
    message_ids = []
    if call["telegram_message_id"]:
        message_ids.append(int(call["telegram_message_id"]))
    message_ids.extend(
        int(row["telegram_message_id"])
        for row in milestones
        if row["telegram_message_id"]
    )

    deleted = await db.delete_call_admin(pool, call_id)
    removed_messages = 0
    for message_id in sorted(set(message_ids)):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            removed_messages += 1
        except Exception:
            logger.warning("Could not delete Telegram message %s for removed call %s", message_id, call_id)

    if deleted:
        await query.edit_message_text(
            "✅ ההמלצה נמחקה מהמערכת.\n"
            f"Call ID: {call_id}\n"
            f"הודעות שנמחקו מהערוץ: {removed_messages}\n\n"
            "ה-Dashboard יתעדכן אוטומטית בתוך כמה שניות."
        )
    else:
        await query.edit_message_text("❌ לא הצלחתי למחוק את ה-Call.")


async def delete_call_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text("❌ המחיקה בוטלה.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message is not None:
        await update.effective_message.reply_text("פקודה לא מוכרת. שלח /help לעזרה.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    if isinstance(error, (Conflict, NetworkError, TimedOut)):
        logger.warning("Transient Telegram error: %s", error)
        return

    logger.exception("Error while processing update: %s", update, exc_info=error)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "מצטער, אירעה שגיאה בזמן עיבוד ההודעה."
        )


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("channeltest", channel_test))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("cancelbroadcast", cancel_broadcast_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("bug", bug_command))
    application.add_handler(CommandHandler("deletecall", delete_call_command))

    application.add_handler(
        CallbackQueryHandler(publish_call_callback, pattern=r"^publish_call$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_call_callback, pattern=r"^cancel_call$")
    )
    application.add_handler(
        CallbackQueryHandler(broadcast_add_button_callback, pattern=r"^broadcast_add_button$")
    )
    application.add_handler(
        CallbackQueryHandler(broadcast_publish_callback, pattern=r"^broadcast_publish$")
    )
    application.add_handler(
        CallbackQueryHandler(
            broadcast_publish_pin_callback, pattern=r"^broadcast_publish_pin$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(broadcast_replace_callback, pattern=r"^broadcast_replace$")
    )
    application.add_handler(
        CallbackQueryHandler(broadcast_cancel_callback, pattern=r"^broadcast_cancel$")
    )
    application.add_handler(
        CallbackQueryHandler(support_reply_callback, pattern=r"^support_reply:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(delete_call_confirm_callback, pattern=r"^delete_call_confirm:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(delete_call_cancel_callback, pattern=r"^delete_call_cancel$")
    )

    application.add_handler(
        MessageHandler(
            filters.Regex(f"^({MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"),
            menu_button,
        )
    )
    application.add_handler(
        MessageHandler(filters.PHOTO, private_photo_message)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message)
    )
