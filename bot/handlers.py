"""Telegram update handlers for the memecoin calls bot."""

import asyncio
import html
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

from bot import cache, db, stats


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
/myid - הצגת Telegram User ID
/ping - בדיקת סטטוס
"""

SOLANA_CA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


LIST_ITEMS_PER_MESSAGE = 20


def _bot_deep_link(username: str, payload: str) -> str:
    return f"https://t.me/{username}?start={payload}"


def _lists_keyboard(bot_username: str, dex_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if dex_url:
        rows.append([InlineKeyboardButton("📈 DexScreener", url=dex_url)])

    rows.append([
        InlineKeyboardButton(
            "📋 יומי",
            url=_bot_deep_link(bot_username, "calls_day"),
        ),
        InlineKeyboardButton(
            "📅 שבועי",
            url=_bot_deep_link(bot_username, "calls_week"),
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            "🏆 כל הזמנים",
            url=_bot_deep_link(bot_username, "calls_all"),
        )
    ])
    return InlineKeyboardMarkup(rows)


def _period_title(period: str) -> str:
    return {
        "day": "📋 המלצות יומיות — 24 השעות האחרונות",
        "week": "📅 המלצות שבועיות — 7 הימים האחרונים",
        "all": "🏆 כל ההמלצות — כל הזמנים",
    }[period]


def _call_list_item(row) -> str:
    entry = float(row["entry_valuation"])
    ath = float(row["highest_valuation"] or entry)
    multiple = ath / entry if entry > 0 else 0.0
    profit = ((ath / entry) - 1.0) * 100.0 if entry > 0 else 0.0
    metric = "MC" if row["valuation_type"] == "marketCap" else "FDV"

    return (
        f"🪙 {row['symbol']}\n"
        f"💰 {metric} בפרסום: {_fmt_usd(entry)}\n"
        f"🏆 ATH מאז הפרסום: {_fmt_usd(ath)} "
        f"• {multiple:.2f}X • {profit:+.0f}%"
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


def _leader_badge(profit: float) -> str:
    if profit >= 500:
        return "🟣"
    if profit >= 300:
        return "🔥"
    if profit >= 100:
        return "🟢"
    if profit >= 50:
        return "🟡"
    return "🔵"


def _daily_leaders_text(leaders) -> str:
    if not leaders:
        return (
            "🏆 <b>המובילים היומיים</b>\n"
            "<i>עדיין אין מספיק המלצות היום.</i>"
        )

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        "🏆 <b>המובילים היומיים</b>",
        "<i>לפי אחוז הרווח מה־ATH הגבוה לנמוך</i>",
        "",
    ]
    for index, row in enumerate(leaders, start=1):
        entry = float(row["entry_valuation"])
        ath = float(row["highest_valuation"] or entry)
        multiple = ath / entry if entry > 0 else 0.0
        profit = ((ath / entry) - 1.0) * 100.0 if entry > 0 else 0.0
        symbol = html.escape(str(row["symbol"]))
        medal = medals[index - 1] if index <= len(medals) else f"#{index}"
        badge = _leader_badge(profit)
        lines.extend([
            f"{medal} <b>${symbol}</b>",
            f"{badge} <b>{profit:+.0f}%  •  {multiple:.2f}X</b>",
            f"🏆 <b>ATH {_fmt_usd(ath)}</b>  •  כניסה {_fmt_usd(entry)}",
            "",
        ])
    return "\n".join(lines).rstrip()


def _channel_call_text(call: dict, leaders=None) -> str:
    metric = "MC" if call["valuation_type"] == "marketCap" else "FDV"
    name = html.escape(str(call["name"]))
    symbol = html.escape(str(call["symbol"]))
    token_address = html.escape(str(call["token_address"]))
    leaders_block = _daily_leaders_text(leaders or [])

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
        f"{leaders_block}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <i>אזהרה: מסחר בקריפטו וב־Memecoins הוא תנודתי ומסוכן ועלול להביא "
        "לאובדן מלא של ההשקעה. אין בפרסום משום ייעוץ או המלצה אישית. "
        "כל החלטת מסחר היא באחריות המשתמש בלבד.</i>"
    )


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
        "calls_week": "week",
        "calls_all": "all",
    }

    if payload in payload_to_period:
        await _send_calls_list(message, context, payload_to_period[payload])
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
            text=_channel_call_text(call, []),
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

    # IMPORTANT: the recommendation itself must always receive the daily leaders,
    # even if a Telegram statistics page is temporarily stale/deleted.  In older
    # versions these steps lived in one try block; one failed stats-page edit
    # prevented both the leaders and the buttons from being attached.
    try:
        leaders = await db.get_daily_leaders(pool, limit=3)
    except Exception:
        logger.exception("Could not load daily leaders for the new call")
        leaders = []

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
            pool,
            dex_url=call.get("dex_url"),
        )
    except Exception:
        logger.exception("Could not build stats keyboard; keeping DexScreener button")
        final_keyboard = temp_keyboard

    # Always perform this final edit.  This guarantees that daily leaders are
    # shown on every new recommendation even if list-page maintenance failed.
    try:
        await context.bot.edit_message_text(
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            text=_channel_call_text(call, leaders),
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

    application.add_handler(
        CallbackQueryHandler(publish_call_callback, pattern=r"^publish_call$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_call_callback, pattern=r"^cancel_call$")
    )

    application.add_handler(
        MessageHandler(
            filters.Regex(f"^({MENU_HELP}|{MENU_ABOUT}|{MENU_PING})$"),
            menu_button,
        )
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message)
    )
