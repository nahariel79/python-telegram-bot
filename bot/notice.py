"""Recurring beta/testing notice for the Telegram channel.

The last successful publication time is stored in PostgreSQL so Railway
restarts/deploys do not create duplicate notices.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot import db

logger = logging.getLogger(__name__)

NOTICE_STATE_KEY = "beta_notice_last_sent_at"
NOTICE_CHECK_SECONDS = 300


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


def _enabled() -> bool:
    value = os.getenv("BETA_NOTICE_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _interval_hours() -> float:
    raw = os.getenv("BETA_NOTICE_HOURS", "12").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        logger.warning("BETA_NOTICE_HOURS is invalid; using 12 hours")
        return 12.0


def _notice_text() -> str:
    return (
        "🧪 <b>הערוץ נמצא בשלבי הרצה ושיפור</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "אנחנו ממשיכים לשפר את המערכת, את מעקב ה־ATH ואת נתוני ההמלצות בזמן אמת.\n\n"
        "🐞 <b>ייתכנו מדי פעם באגים או נתונים שדורשים תיקון.</b>\n"
        "אם מצאתם משהו שלא נראה נכון — נשמח מאוד שתדווחו לנו בהודעה פרטית.\n\n"
        "💬 <b>יש לכם שאלה, הבהרה או רוצים להתייעץ לגבי הנתונים והשימוש בערוץ?</b>\n"
        "אפשר לפנות אלינו גם בהודעה פרטית ונשתדל לעזור.\n\n"
        "🙏 כל דיווח עוזר לנו לשפר את המערכת עבור כולם.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>הערוץ עוסק במידע ומעקב אחר Calls. אין במענה פרטי משום ייעוץ השקעות אישי.</i>"
    )


def _notice_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    base = f"https://t.me/{bot_username}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐞 דווחו על תקלה", url=f"{base}?start=bug"),
            InlineKeyboardButton("💬 פנייה פרטית", url=f"{base}?start=support"),
        ]
    ])


async def publish_beta_notice(application, pool) -> bool:
    target = _channel_target()
    if target is None:
        logger.warning("CHANNEL_ID is not configured; beta notice skipped")
        return False

    me = await application.bot.get_me()
    if not me.username:
        logger.warning("Bot has no username; beta notice skipped")
        return False

    await application.bot.send_message(
        chat_id=target,
        text=_notice_text(),
        parse_mode="HTML",
        reply_markup=_notice_keyboard(me.username),
        disable_web_page_preview=True,
    )
    await db.set_system_state(
        pool,
        NOTICE_STATE_KEY,
        datetime.now(timezone.utc).isoformat(),
    )
    logger.info("Published recurring beta/testing notice")
    return True


async def beta_notice_loop(application) -> None:
    """Publish the beta notice on a persistent schedule.

    The loop checks every five minutes, but the actual publication cadence is
    controlled by BETA_NOTICE_HOURS (default 12 hours). The last sent time is
    persisted in PostgreSQL, so a deploy/restart does not reset the schedule.
    """
    # Give startup migrations and the other background jobs time to settle.
    await asyncio.sleep(30)

    while True:
        try:
            if not _enabled():
                await asyncio.sleep(NOTICE_CHECK_SECONDS)
                continue

            pool = application.bot_data.get("db")
            if pool is None:
                await asyncio.sleep(NOTICE_CHECK_SECONDS)
                continue

            now = datetime.now(timezone.utc)
            last_raw = await db.get_system_state(pool, NOTICE_STATE_KEY)
            last_sent = None
            if last_raw:
                try:
                    last_sent = datetime.fromisoformat(last_raw)
                    if last_sent.tzinfo is None:
                        last_sent = last_sent.replace(tzinfo=timezone.utc)
                except ValueError:
                    logger.warning("Invalid stored beta notice timestamp: %s", last_raw)

            due = last_sent is None or now >= last_sent + timedelta(hours=_interval_hours())
            if due:
                await publish_beta_notice(application, pool)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Beta notice loop failed; will retry")

        await asyncio.sleep(NOTICE_CHECK_SECONDS)
