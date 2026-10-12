"""Channel-native ranked call lists for daily, monthly and all-time views."""

import asyncio
import html
import logging
import os
from datetime import timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application

from bot import db


logger = logging.getLogger(__name__)

PERIODS = ("day", "month", "all")
CALLS_PER_PAGE = 10


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


def _period_title(period: str) -> str:
    return {
        "day": "🏆 המובילים היומיים — 24 השעות האחרונות",
        "month": "🗓 המובילים החודשיים — 30 הימים האחרונים",
        "all": "🌟 כל הזמנים — כל ההמלצות",
    }[period]


def _channel_username() -> str | None:
    explicit = os.getenv("CHANNEL_USERNAME", "").strip()
    if explicit:
        return explicit.lstrip("@").replace("https://t.me/", "").replace("t.me/", "")

    channel_id = os.getenv("CHANNEL_ID", "").strip()
    if channel_id.startswith("@"):
        return channel_id[1:]
    return None


def _message_url(message_id: int) -> str | None:
    username = _channel_username()
    if not username:
        return None
    return f"https://t.me/{username}/{message_id}"


def _bot_deep_link(bot_username: str, payload: str) -> str:
    return f"https://t.me/{bot_username}?start={payload}"


def _format_call_time(value) -> str:
    if value is None:
        return "לא זמין"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo("Asia/Jerusalem"))
    return local.strftime("%d/%m/%Y • %H:%M")


def _performance_badge(profit: float) -> str:
    if profit >= 500:
        return "🟣"
    if profit >= 300:
        return "🔥"
    if profit >= 100:
        return "🟢"
    if profit >= 50:
        return "🟡"
    return "🔵"


def _call_line(index: int, row) -> str:
    entry = float(row["entry_valuation"])
    ath = float(row["highest_valuation"] or entry)
    multiple = ath / entry if entry > 0 else 0.0
    profit = ((ath / entry) - 1.0) * 100.0 if entry > 0 else 0.0
    metric = "MC" if row["valuation_type"] == "marketCap" else "FDV"
    symbol = html.escape(str(row["symbol"]))
    token_name = html.escape(str(row["token_name"]))
    badge = _performance_badge(profit)
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"#{index}")

    original_url = _message_url(int(row["telegram_message_id"])) if row["telegram_message_id"] else None
    if original_url:
        token_header = f'{medal} <a href="{original_url}"><b>${symbol}</b></a> • {token_name}'
    else:
        token_header = f"{medal} <b>${symbol}</b> • {token_name}"

    return (
        f"{token_header}\n"
        f"💰 <b>{metric} בזמן ההמלצה:</b> {_fmt_usd(entry)}\n"
        f"🕒 <b>תאריך ושעת ההמלצה:</b> {_format_call_time(row['call_time'])}\n"
        f"{badge} <b>רווח לפי ATH:</b> {profit:+.1f}%  •  {multiple:.2f}X\n"
        f"🏆 <b>ATH:</b> {_fmt_usd(ath)}"
    )


def _all_time_summary_text(summary) -> str:
    if not summary:
        total = 0
        cumulative = 0.0
        average = 0.0
    else:
        total = int(summary["total_calls"] or 0)
        cumulative = float(summary["cumulative_ath_pnl_percent"] or 0.0)
        average = float(summary["average_ath_profit_percent"] or 0.0)

    return (
        "📊 <b>סטטיסטיקת כל ההמלצות</b>\n"
        f"🧾 <b>סה״כ המלצות:</b> {total:,}\n"
        f"💹 <b>PNL מצטבר לפי ATH:</b> {cumulative:+,.1f}%\n"
        f"📈 <b>רווח ATH ממוצע להמלצה:</b> {average:+,.1f}%\n"
        f"➗ <i>הממוצע מחושב על פני {total:,} המלצות.</i>\n\n"
        "ℹ️ <i>PNL מצטבר הוא סכום אחוזי ה־ATH של ה־Calls ולא תשואת תיק בפועל.</i>"
    )


def _page_text(period: str, rows: list, page: int, pages: int, total: int, summary=None) -> str:
    title = _period_title(period)
    summary_block = _all_time_summary_text(summary) if period == "all" and page == 1 else None

    header = (
        f"<b>{title}</b>\n"
        f"📊 <b>סה״כ המלצות בתקופה:</b> {total}\n"
        "⬇️ <i>ממויין לפי אחוז הרווח מה־ATH — הגבוה ביותר ראשון</i>"
    )
    if pages > 1:
        header += f"\n📄 עמוד {page}/{pages}"
    if summary_block:
        header += f"\n\n━━━━━━━━━━━━━━━━━━\n\n{summary_block}"

    if rows:
        start_index = (page - 1) * CALLS_PER_PAGE + 1
        body = "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(
            _call_line(start_index + i, row) for i, row in enumerate(rows)
        )
    else:
        body = "אין עדיין המלצות בתקופה הזאת."

    return (
        f"{header}\n\n{body}\n\n"
        "🔄 <i>הנתונים משתנים בזמן אמת ומתעדכנים לפי נתוני שוק אמיתיים.</i>"
    )


async def _ensure_page(application: Application, pool, *, period: str, page_number: int, channel_chat_id: int) -> int:
    existing = await db.get_stats_page(pool, period, page_number)
    if existing:
        return int(existing["message_id"])

    sent = await application.bot.send_message(
        chat_id=channel_chat_id,
        text=f"<b>{_period_title(period)}</b>\n\nמכין את הרשימה...",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await db.upsert_stats_page(
        pool,
        period=period,
        page_number=page_number,
        channel_chat_id=sent.chat.id,
        message_id=sent.message_id,
    )
    return sent.message_id


async def _replace_missing_page(application: Application, pool, *, period: str, page_number: int, channel_chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None) -> int:
    sent = await application.bot.send_message(
        chat_id=channel_chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await db.upsert_stats_page(
        pool,
        period=period,
        page_number=page_number,
        channel_chat_id=sent.chat.id,
        message_id=sent.message_id,
    )
    logger.warning("Recreated missing stats page %s/%s as Telegram message %s", period, page_number, sent.message_id)
    return int(sent.message_id)


def _is_missing_or_uneditable_message(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "message to edit not found",
        "message can't be edited",
        "message can not be edited",
        "message_id_invalid",
        "message identifier is not specified",
    ))


async def _all_page1_urls(pool) -> dict[str, str]:
    urls: dict[str, str] = {}
    for period in PERIODS:
        row = await db.get_stats_page(pool, period, 1)
        if not row:
            continue
        url = _message_url(int(row["message_id"]))
        if url:
            urls[period] = url
    return urls


def _page_keyboard(*, page1_urls: dict[str, str], prev_url: str | None, next_url: str | None, bot_username: str | None, period: str) -> InlineKeyboardMarkup | None:
    rows = []

    period_row = []
    if page1_urls.get("day"):
        period_row.append(InlineKeyboardButton("🏆 יומיים", url=page1_urls["day"]))
    if page1_urls.get("month"):
        period_row.append(InlineKeyboardButton("🗓 חודשיים", url=page1_urls["month"]))
    if period_row:
        rows.append(period_row)

    if page1_urls.get("all"):
        rows.append([InlineKeyboardButton("🌟 כל הזמנים", url=page1_urls["all"])])

    if bot_username:
        rows.append([
            InlineKeyboardButton(
                "📥 הורדת הרשימה כ־CSV",
                url=_bot_deep_link(bot_username, f"export_{period}"),
            )
        ])

    nav = []
    if prev_url:
        nav.append(InlineKeyboardButton("⬅️ הקודם", url=prev_url))
    if next_url:
        nav.append(InlineKeyboardButton("הבא ➡️", url=next_url))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows) if rows else None


async def refresh_stats_period(application: Application, pool, *, period: str, channel_chat_id: int) -> None:
    calls = await db.get_calls_for_period(pool, period)
    performance_summary = await db.get_all_time_performance_summary(pool) if period == "all" else None
    chunks = [calls[i:i + CALLS_PER_PAGE] for i in range(0, len(calls), CALLS_PER_PAGE)] or [[]]

    page_ids: list[int] = []
    for page_number in range(1, len(chunks) + 1):
        page_ids.append(await _ensure_page(
            application, pool, period=period, page_number=page_number, channel_chat_id=channel_chat_id
        ))

    for other_period in PERIODS:
        await _ensure_page(
            application, pool, period=other_period, page_number=1, channel_chat_id=channel_chat_id
        )

    async def render_once() -> bool:
        recreated = False
        page1_urls = await _all_page1_urls(pool)
        bot_username = application.bot.username

        for idx, rows in enumerate(chunks):
            page_number = idx + 1
            message_id = page_ids[idx]
            prev_url = _message_url(page_ids[idx - 1]) if idx > 0 else None
            next_url = _message_url(page_ids[idx + 1]) if idx + 1 < len(page_ids) else None
            keyboard = _page_keyboard(
                page1_urls=page1_urls,
                prev_url=prev_url,
                next_url=next_url,
                bot_username=bot_username,
                period=period,
            )
            text = _page_text(period, rows, page_number, len(chunks), len(calls), summary=performance_summary)

            try:
                await application.bot.edit_message_text(
                    chat_id=channel_chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except BadRequest as exc:
                error_text = str(exc).lower()
                if "message is not modified" in error_text:
                    continue
                if _is_missing_or_uneditable_message(exc):
                    page_ids[idx] = await _replace_missing_page(
                        application, pool,
                        period=period,
                        page_number=page_number,
                        channel_chat_id=channel_chat_id,
                        text=text,
                        keyboard=keyboard,
                    )
                    recreated = True
                    continue
                raise
        return recreated

    recreated = await render_once()
    if recreated:
        await render_once()

    existing_pages = await db.list_stats_pages(pool, period)
    needed = len(chunks)
    for row in existing_pages:
        page_number = int(row["page_number"])
        if page_number <= needed:
            continue
        try:
            await application.bot.delete_message(
                chat_id=int(row["channel_chat_id"]),
                message_id=int(row["message_id"]),
            )
        except Exception:
            logger.warning("Could not delete obsolete stats page %s/%s", period, page_number, exc_info=True)
        await db.delete_stats_page_record(pool, period=period, page_number=page_number)


async def _remove_legacy_weekly_pages(application: Application, pool) -> None:
    """Remove the old weekly stats posts once Stage 12 replaces them with monthly."""
    try:
        legacy = await db.list_stats_pages(pool, "week")
    except Exception:
        logger.exception("Could not inspect legacy weekly stats pages")
        return

    for row in legacy:
        try:
            await application.bot.delete_message(
                chat_id=int(row["channel_chat_id"]),
                message_id=int(row["message_id"]),
            )
        except Exception:
            # It may already have been deleted manually; the DB record can still be retired.
            logger.info("Legacy weekly stats message already unavailable or not deletable")
        await db.delete_stats_page_record(pool, period="week", page_number=int(row["page_number"]))


async def refresh_all_stats_pages(application: Application, pool, *, channel_chat_id: int) -> None:
    await _remove_legacy_weekly_pages(application, pool)

    for period in PERIODS:
        await _ensure_page(
            application, pool, period=period, page_number=1, channel_chat_id=channel_chat_id
        )

    for period in PERIODS:
        await refresh_stats_period(
            application, pool, period=period, channel_chat_id=channel_chat_id
        )

    for period in PERIODS:
        await refresh_stats_period(
            application, pool, period=period, channel_chat_id=channel_chat_id
        )


async def recommendation_keyboard(pool, *, dex_url: str | None) -> InlineKeyboardMarkup:
    page1_urls = await _all_page1_urls(pool)
    rows = []
    if dex_url:
        rows.append([InlineKeyboardButton("📈 DexScreener", url=dex_url)])

    row = []
    if page1_urls.get("day"):
        row.append(InlineKeyboardButton("🏆 מובילים יומיים", url=page1_urls["day"]))
    if page1_urls.get("month"):
        row.append(InlineKeyboardButton("🗓 מובילים חודשיים", url=page1_urls["month"]))
    if row:
        rows.append(row)
    if page1_urls.get("all"):
        rows.append([InlineKeyboardButton("🌟 כל הזמנים", url=page1_urls["all"])])

    return InlineKeyboardMarkup(rows)


async def stats_refresh_loop(application: Application) -> None:
    try:
        interval = max(30, int(os.getenv("STATS_REFRESH_SECONDS", "60")))
    except ValueError:
        interval = 60

    logger.info("Channel stats refresh loop started; interval=%ss", interval)

    while True:
        try:
            pool = application.bot_data.get("db")
            if pool is not None:
                row = await pool.fetchrow("SELECT channel_chat_id FROM calls ORDER BY id DESC LIMIT 1")
                if row is None:
                    row = await pool.fetchrow("SELECT channel_chat_id FROM stats_pages LIMIT 1")
                if row is not None:
                    await refresh_all_stats_pages(
                        application, pool, channel_chat_id=int(row["channel_chat_id"])
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed refreshing channel stats pages")

        await asyncio.sleep(interval)
