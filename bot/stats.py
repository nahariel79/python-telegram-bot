"""Live, channel-native ranked call lists.

Stage 14 keeps one stable Telegram post per leaderboard page, refreshes it from
PostgreSQL frequently, and repairs old recommendation/milestone keyboards when
leaderboard message IDs ever change.
"""

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application

from bot import db


logger = logging.getLogger(__name__)

PERIODS = ("day", "month", "all")
CALLS_PER_PAGE = 6
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


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
        "day": "🏆 המובילים היומיים",
        "month": "🗓 המובילים החודשיים",
        "all": "🌟 כל הזמנים",
    }[period]


def _period_subtitle(period: str) -> str:
    return {
        "day": "24 השעות האחרונות",
        "month": "30 הימים האחרונים",
        "all": "כל ההמלצות מאז תחילת המעקב",
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
    return value.astimezone(ISRAEL_TZ).strftime("%d/%m/%Y • %H:%M")


def _render_time() -> str:
    return datetime.now(timezone.utc).astimezone(ISRAEL_TZ).strftime("%d/%m/%Y • %H:%M")


def _performance_badge(profit: float) -> str:
    if profit >= 1000:
        return "💎"
    if profit >= 500:
        return "🟣"
    if profit >= 300:
        return "🔥"
    if profit >= 100:
        return "🟢"
    if profit >= 50:
        return "🟡"
    return "🔵"


def _row_profit(row) -> tuple[float, float, float]:
    entry = float(row["entry_valuation"] or 0.0)
    ath = float(row["highest_valuation"] or entry or 0.0)
    if entry <= 0:
        return entry, ath, 0.0
    computed = ((ath / entry) - 1.0) * 100.0
    stored = float(row["highest_profit_percent"] or 0.0)
    return entry, ath, max(computed, stored)


def _call_card(index: int, row) -> str:
    entry, ath, profit = _row_profit(row)
    multiple = ath / entry if entry > 0 else 0.0
    metric = "MC" if row["valuation_type"] == "marketCap" else "FDV"
    symbol = html.escape(str(row["symbol"]))
    token_name = html.escape(str(row["token_name"]))
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"#{index}")
    badge = _performance_badge(profit)

    original_url = _message_url(int(row["telegram_message_id"])) if row["telegram_message_id"] else None
    if original_url:
        title = f'{medal} <a href="{original_url}"><b>${symbol}</b></a> — {token_name}'
    else:
        title = f"{medal} <b>${symbol}</b> — {token_name}"

    return (
        f"{title}\n"
        f"{badge} <b>{profit:+.1f}%</b>  •  <b>{multiple:.2f}X</b>\n"
        f"🏆 <b>ATH:</b> <b>{_fmt_usd(ath)}</b>\n"
        f"🎯 <b>{metric} בזמן ההמלצה:</b> {_fmt_usd(entry)}\n"
        f"🕒 <b>פורסם:</b> {_format_call_time(row['call_time'])}"
    )


def _all_time_summary_text(summary) -> str:
    total = int(summary["total_calls"] or 0) if summary else 0
    cumulative = float(summary["cumulative_ath_pnl_percent"] or 0.0) if summary else 0.0
    average = float(summary["average_ath_profit_percent"] or 0.0) if summary else 0.0
    return (
        "📊 <b>סטטיסטיקה כוללת</b>\n"
        f"🧾 סה״כ המלצות: <b>{total:,}</b>\n"
        f"💹 PNL מצטבר לפי ATH: <b>{cumulative:+,.1f}%</b>\n"
        f"📈 רווח ATH ממוצע: <b>{average:+,.1f}%</b>\n"
        "ℹ️ <i>PNL מצטבר הוא סכום אחוזי ה־ATH של ה־Calls, לא תשואת תיק בפועל.</i>"
    )


def _page_text(period: str, rows: list, page: int, pages: int, total: int, summary=None) -> str:
    header = (
        f"<b>{_period_title(period)}</b>\n"
        f"<i>{_period_subtitle(period)}</i>\n"
        f"🟢 <b>LIVE</b>  •  🔄 עודכן: {_render_time()}\n"
        f"📊 מספר המלצות: <b>{total}</b>\n"
        "⬇️ <i>ממויין לפי אחוז הרווח מה־ATH — הגבוה ביותר ראשון</i>"
    )
    if pages > 1:
        header += f"\n📄 עמוד <b>{page}/{pages}</b>"

    if period == "all" and page == 1:
        header += f"\n\n━━━━━━━━━━━━━━━━━━\n{_all_time_summary_text(summary)}"

    if rows:
        start_index = (page - 1) * CALLS_PER_PAGE + 1
        body = "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(
            _call_card(start_index + offset, row)
            for offset, row in enumerate(rows)
        )
    else:
        body = "אין עדיין המלצות בתקופה הזאת."

    return (
        f"{header}\n\n━━━━━━━━━━━━━━━━━━\n\n{body}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔄 <i>הנתונים משתנים בזמן אמת ומתעדכנים לפי נתוני שוק אמיתיים.</i>"
    )


async def _ensure_page(application: Application, pool, *, period: str, page_number: int, channel_chat_id: int) -> int:
    existing = await db.get_stats_page(pool, period, page_number)
    if existing:
        return int(existing["message_id"])

    sent = await application.bot.send_message(
        chat_id=channel_chat_id,
        text=f"<b>{_period_title(period)}</b>\n\nמכין נתונים חיים...",
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
    return int(sent.message_id)


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
    logger.warning(
        "Recreated missing stats page %s/%s as Telegram message %s",
        period,
        page_number,
        sent.message_id,
    )
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

    top = []
    if page1_urls.get("day"):
        top.append(InlineKeyboardButton("🏆 יומיים", url=page1_urls["day"]))
    if page1_urls.get("month"):
        top.append(InlineKeyboardButton("🗓 חודשיים", url=page1_urls["month"]))
    if top:
        rows.append(top)

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


async def refresh_stats_period(application: Application, pool, *, period: str, channel_chat_id: int) -> bool:
    """Refresh one period. Returns True if a page message had to be recreated."""
    calls = await db.get_calls_for_period(pool, period)
    summary = await db.get_all_time_performance_summary(pool) if period == "all" else None
    chunks = [calls[i:i + CALLS_PER_PAGE] for i in range(0, len(calls), CALLS_PER_PAGE)] or [[]]

    page_ids: list[int] = []
    for page_number in range(1, len(chunks) + 1):
        page_ids.append(await _ensure_page(
            application,
            pool,
            period=period,
            page_number=page_number,
            channel_chat_id=channel_chat_id,
        ))

    for other_period in PERIODS:
        await _ensure_page(
            application,
            pool,
            period=other_period,
            page_number=1,
            channel_chat_id=channel_chat_id,
        )

    recreated_any = False

    async def render_once() -> bool:
        nonlocal recreated_any
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
            text = _page_text(period, rows, page_number, len(chunks), len(calls), summary=summary)

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
                        application,
                        pool,
                        period=period,
                        page_number=page_number,
                        channel_chat_id=channel_chat_id,
                        text=text,
                        keyboard=keyboard,
                    )
                    recreated = True
                    recreated_any = True
                    continue
                raise
        return recreated

    if await render_once():
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
            logger.warning(
                "Could not delete obsolete stats page %s/%s",
                period,
                page_number,
                exc_info=True,
            )
        await db.delete_stats_page_record(pool, period=period, page_number=page_number)

    return recreated_any


async def _remove_legacy_weekly_pages(application: Application, pool) -> None:
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
            logger.info("Legacy weekly stats message already unavailable or not deletable")
        await db.delete_stats_page_record(
            pool,
            period="week",
            page_number=int(row["page_number"]),
        )


async def refresh_all_stats_pages(application: Application, pool, *, channel_chat_id: int) -> bool:
    """Refresh every leaderboard. Returns True if a primary/list page changed ID."""
    await _remove_legacy_weekly_pages(application, pool)

    before = {}
    for period in PERIODS:
        row = await db.get_stats_page(pool, period, 1)
        before[period] = int(row["message_id"]) if row else None

    for period in PERIODS:
        try:
            await _ensure_page(
                application,
                pool,
                period=period,
                page_number=1,
                channel_chat_id=channel_chat_id,
            )
        except Exception:
            logger.exception("Could not ensure stats page %s/1", period)

    recreated = False
    for period in PERIODS:
        try:
            recreated = (
                await refresh_stats_period(
                    application,
                    pool,
                    period=period,
                    channel_chat_id=channel_chat_id,
                )
                or recreated
            )
        except Exception:
            logger.exception("Could not refresh stats period %s", period)

    after = {}
    for period in PERIODS:
        row = await db.get_stats_page(pool, period, 1)
        after[period] = int(row["message_id"]) if row else None

    return recreated or before != after


async def recommendation_keyboard(application: Application, pool, *, channel_chat_id: int, dex_url: str | None) -> InlineKeyboardMarkup:
    """Build buttons that always point to the current channel-native leaderboards."""
    page1_urls: dict[str, str] = {}

    for period in PERIODS:
        try:
            message_id = await _ensure_page(
                application,
                pool,
                period=period,
                page_number=1,
                channel_chat_id=channel_chat_id,
            )
            url = _message_url(int(message_id))
            if url:
                page1_urls[period] = url
        except Exception:
            logger.exception("Could not prepare recommendation button for %s", period)

    rows = []
    if dex_url:
        rows.append([InlineKeyboardButton("📈 DexScreener", url=dex_url)])

    ranking_row = []
    if page1_urls.get("day"):
        ranking_row.append(InlineKeyboardButton("🏆 מובילים יומיים", url=page1_urls["day"]))
    if page1_urls.get("month"):
        ranking_row.append(InlineKeyboardButton("🗓 מובילים חודשיים", url=page1_urls["month"]))
    if ranking_row:
        rows.append(ranking_row)

    if page1_urls.get("all"):
        rows.append([InlineKeyboardButton("🌟 כל הזמנים", url=page1_urls["all"])])

    return InlineKeyboardMarkup(rows)


async def repair_all_recommendation_keyboards(application: Application, pool) -> None:
    """Point older call/milestone posts at the current leaderboard messages.

    This is intentionally run only once at startup, or after list page IDs are
    recreated, so it does not create continuous Telegram edit traffic.
    """
    posts = await db.get_posts_for_keyboard_repair(pool)
    if not posts:
        return

    logger.info("Repairing leaderboard buttons on %s existing channel posts", len(posts))
    for row in posts:
        try:
            keyboard = await recommendation_keyboard(
                application,
                pool,
                channel_chat_id=int(row["channel_chat_id"]),
                dex_url=row["dexscreener_url"],
            )
            await application.bot.edit_message_reply_markup(
                chat_id=int(row["channel_chat_id"]),
                message_id=int(row["telegram_message_id"]),
                reply_markup=keyboard,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning(
                    "Could not repair buttons on message %s: %s",
                    row["telegram_message_id"],
                    exc,
                )
        except Exception:
            logger.exception(
                "Could not repair buttons on message %s",
                row["telegram_message_id"],
            )
        await asyncio.sleep(0.12)


async def stats_refresh_loop(application: Application) -> None:
    try:
        interval = max(20, int(os.getenv("STATS_REFRESH_SECONDS", "30")))
    except ValueError:
        interval = 30

    logger.info("Live leaderboard refresh loop started; interval=%ss", interval)
    first_cycle = True

    while True:
        try:
            pool = application.bot_data.get("db")
            if pool is not None:
                row = await pool.fetchrow(
                    "SELECT channel_chat_id FROM calls ORDER BY id DESC LIMIT 1"
                )
                if row is None:
                    row = await pool.fetchrow(
                        "SELECT channel_chat_id FROM stats_pages LIMIT 1"
                    )
                if row is not None:
                    ids_changed = await refresh_all_stats_pages(
                        application,
                        pool,
                        channel_chat_id=int(row["channel_chat_id"]),
                    )
                    if first_cycle or ids_changed:
                        await repair_all_recommendation_keyboards(application, pool)
                        first_cycle = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed refreshing live leaderboard pages")

        await asyncio.sleep(interval)
