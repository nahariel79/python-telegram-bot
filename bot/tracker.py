"""Background tracker for published memecoin calls."""

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from bot import db


logger = logging.getLogger(__name__)


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


def _duration_text(started_at: datetime) -> str:
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    total = max(0, int((now - started_at).total_seconds()))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        return f"{days} ימים, {hours} שעות"
    if hours:
        return f"{hours} שעות ו-{minutes} דקות"
    return f"{minutes} דקות"


def _pair_request(pair_address: str) -> dict | None:
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Telegram-Memecoin-Calls-Bot/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))

    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not pairs:
        return None

    for pair in pairs:
        if str(pair.get("pairAddress", "")).lower() == pair_address.lower():
            return pair

    return pairs[0]


async def _fetch_pair(pair_address: str) -> dict | None:
    return await asyncio.to_thread(_pair_request, pair_address)


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _milestone_message(call, milestone: int, profit: float, valuation: float) -> str:
    multiple = valuation / float(call["entry_valuation"])
    metric = "שווי שוק" if call["valuation_type"] == "marketCap" else "FDV"

    return (
        f"🔥 {call['symbol']} הגיע ל-{milestone}% רווח!\n\n"
        f"🚀 {multiple:.2f}X מאז ההמלצה\n\n"
        f"💰 {metric} בהמלצה: {_fmt_usd(call['entry_valuation'])}\n"
        f"📈 {metric} נוכחי: {_fmt_usd(valuation)}\n"
        f"🟢 רווח נוכחי: +{profit:.1f}%\n"
        f"⏱ זמן מאז ההמלצה: {_duration_text(call['call_time'])}"
        "\n\n⚠️ אזהרה: מסחר בקריפטו וב־Memecoins הוא תנודתי ומסוכן ועלול להביא לאובדן מלא של ההשקעה. אין בפרסום משום ייעוץ או המלצה אישית. כל החלטת מסחר היא באחריות המשתמש בלבד."
    )


async def check_calls_once(application: Application) -> None:
    pool = application.bot_data.get("db")
    if pool is None:
        logger.warning("Tracker skipped: PostgreSQL is unavailable.")
        return

    calls = await db.get_active_calls(pool)

    for call in calls:
        try:
            pair = await _fetch_pair(call["pair_address"])
            if not pair:
                logger.warning(
                    "No DexScreener pair data for call %s (%s)",
                    call["id"],
                    call["symbol"],
                )
                continue

            current_price = _to_float(pair.get("priceUsd"))
            current_valuation = _to_float(pair.get(call["valuation_type"]))

            if current_valuation is None or current_valuation <= 0:
                logger.warning(
                    "No usable %s for call %s",
                    call["valuation_type"],
                    call["id"],
                )
                continue

            entry = float(call["entry_valuation"])
            profit = ((current_valuation / entry) - 1.0) * 100.0

            previous_high = float(call["highest_valuation"] or entry)
            highest_valuation = max(previous_high, current_valuation)
            highest_profit = max(
                float(call["highest_profit_percent"] or 0),
                ((highest_valuation / entry) - 1.0) * 100.0,
            )

            await db.update_call_snapshot(
                pool,
                call_id=call["id"],
                current_price=current_price,
                current_valuation=current_valuation,
                highest_valuation=highest_valuation,
                highest_profit_percent=highest_profit,
            )

            if profit < 100:
                continue

            # 100%, 200%, 300%... If several were crossed between checks,
            # publish only the highest currently reached target.
            milestone = int(profit // 100) * 100

            claimed = await db.claim_milestone(
                pool,
                call_id=call["id"],
                milestone=milestone,
                observed_profit_percent=profit,
                observed_valuation=current_valuation,
                observed_price=current_price,
            )
            if not claimed:
                continue

            keyboard = None
            if call["dexscreener_url"]:
                keyboard = InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "📈 DexScreener",
                            url=call["dexscreener_url"],
                        )
                    ]]
                )

            try:
                sent = await application.bot.send_message(
                    chat_id=call["channel_chat_id"],
                    text=_milestone_message(
                        call,
                        milestone,
                        profit,
                        current_valuation,
                    ),
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception(
                    "Failed publishing milestone %s for call %s",
                    milestone,
                    call["id"],
                )
                await db.release_milestone_claim(
                    pool,
                    call_id=call["id"],
                    milestone=milestone,
                )
                continue

            await db.finalize_milestone(
                pool,
                call_id=call["id"],
                milestone=milestone,
                telegram_message_id=sent.message_id,
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Tracker failed on call %s", call["id"])


async def tracker_loop(application: Application) -> None:
    try:
        interval = int(os.getenv("TRACK_INTERVAL_SECONDS", "30"))
    except ValueError:
        interval = 30

    interval = max(15, interval)
    logger.info("Call tracker started; interval=%ss", interval)

    while True:
        try:
            await check_calls_once(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected tracker cycle error")

        await asyncio.sleep(interval)
