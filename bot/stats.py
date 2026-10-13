"""Dashboard-backed leaderboard buttons.

Stage 18 stops creating Telegram leaderboard posts. Recommendation and milestone
buttons now open a public, responsive live dashboard served by the same Railway
service. Existing recommendation keyboards are repaired periodically.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application

from bot import db, webapp

logger = logging.getLogger(__name__)


def _bot_deep_link(bot_username: str, payload: str) -> str:
    return f"https://t.me/{bot_username}?start={payload}"


def _fallback_url(bot_username: str | None, period: str) -> str | None:
    url = webapp.dashboard_url(period)
    if url:
        return url
    if not bot_username:
        return None
    payload = {"day": "calls_day", "month": "calls_month", "all": "calls_all"}[period]
    return _bot_deep_link(bot_username, payload)


async def recommendation_keyboard(
    application: Application,
    pool,
    *,
    channel_chat_id: int,
    dex_url: str | None,
) -> InlineKeyboardMarkup:
    del pool, channel_chat_id
    rows = []
    if dex_url:
        rows.append([InlineKeyboardButton("📈 DexScreener", url=dex_url)])

    username = application.bot.username
    day = _fallback_url(username, "day")
    month = _fallback_url(username, "month")
    all_time = _fallback_url(username, "all")

    first = []
    if day:
        first.append(InlineKeyboardButton("🏆 מובילים יומיים", url=day))
    if month:
        first.append(InlineKeyboardButton("🗓 מובילים חודשיים", url=month))
    if first:
        rows.append(first)
    if all_time:
        rows.append([InlineKeyboardButton("🌟 כל הזמנים", url=all_time)])
    return InlineKeyboardMarkup(rows)


async def refresh_all_stats_pages(
    application: Application,
    pool,
    *,
    channel_chat_id: int,
) -> bool:
    """Compatibility no-op.

    Stage 18 no longer creates or edits native Telegram leaderboard posts.
    The dashboard snapshot is refreshed by bot.webapp instead.
    """
    del application, pool, channel_chat_id
    return False


async def repair_all_recommendation_keyboards(application: Application, pool) -> None:
    if not webapp.public_base_url():
        logger.warning(
            "No public dashboard domain yet; existing keyboards will keep their current links until a domain is configured."
        )
        return

    rows = await db.get_posts_for_keyboard_repair(pool)
    changed = 0
    for row in rows:
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
            changed += 1
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                continue
            logger.warning(
                "Could not repair keyboard for message %s: %s",
                row["telegram_message_id"],
                exc,
            )
        except Exception:
            logger.exception("Keyboard repair failed for message %s", row["telegram_message_id"])
    logger.info("Dashboard keyboard repair completed; touched %s messages", changed)


async def stats_refresh_loop(application: Application) -> None:
    """Repair old call/milestone buttons after deploy and occasionally thereafter."""
    await asyncio.sleep(35)
    while True:
        try:
            pool = application.bot_data.get("db")
            if pool is not None:
                await repair_all_recommendation_keyboards(application, pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic dashboard keyboard repair failed")
        await asyncio.sleep(900)
