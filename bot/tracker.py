"""Live + historical tracker for published memecoin calls."""

import asyncio
import html
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from bot import db, stats


logger = logging.getLogger(__name__)

WARNING_TEXT = (
    "⚠️ אזהרה: מסחר בקריפטו וב־Memecoins הוא תנודתי ומסוכן ועלול "
    "להביא לאובדן מלא של ההשקעה. אין בפרסום משום ייעוץ או המלצה אישית. "
    "כל החלטת מסחר היא באחריות המשתמש בלבד."
)


def _bot_deep_link(username: str, payload: str) -> str:
    return f"https://t.me/{username}?start={payload}"


def _milestone_keyboard(
    bot_username: str,
    dex_url: str | None,
) -> InlineKeyboardMarkup:
    rows = []
    if dex_url:
        rows.append([InlineKeyboardButton("📈 DexScreener", url=dex_url)])

    rows.append([
        InlineKeyboardButton(
            "📋 יומי",
            url=_bot_deep_link(bot_username, "calls_day"),
        ),
        InlineKeyboardButton(
            "🗓 חודשי",
            url=_bot_deep_link(bot_username, "calls_month"),
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            "🏆 כל הזמנים",
            url=_bot_deep_link(bot_username, "calls_all"),
        )
    ])
    return InlineKeyboardMarkup(rows)


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


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _http_json(url: str, headers: dict[str, str] | None = None) -> dict | list:
    request_headers = {
        "User-Agent": "Telegram-Memecoin-Calls-Bot/2.0",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


# ---------- DexScreener live data ----------


def _pair_request(pair_address: str) -> dict | None:
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    data = _http_json(url)
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not pairs:
        return None

    for pair in pairs:
        if str(pair.get("pairAddress", "")).lower() == pair_address.lower():
            return pair
    return pairs[0]


async def _fetch_pair(pair_address: str) -> dict | None:
    return await asyncio.to_thread(_pair_request, pair_address)


def _token_pairs_request(token_address: str) -> list[dict]:
    url = f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}"
    data = _http_json(url)
    return data if isinstance(data, list) else []


def _pair_score(pair: dict) -> tuple[float, float]:
    liquidity = _to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
    volume_24h = _to_float((pair.get("volume") or {}).get("h24")) or 0.0
    return liquidity, volume_24h


async def _fetch_best_pair(token_address: str) -> dict | None:
    pairs = await asyncio.to_thread(_token_pairs_request, token_address)
    token_lower = token_address.lower()
    candidates = [
        p for p in pairs
        if str(p.get("chainId", "")).lower() == "solana"
        and str((p.get("baseToken") or {}).get("address", "")).lower() == token_lower
    ]
    if not candidates:
        candidates = [
            p for p in pairs
            if str(p.get("chainId", "")).lower() == "solana"
        ]
    return max(candidates, key=_pair_score) if candidates else None


def _pair_refresh_due(call, refresh_seconds: int) -> bool:
    last = call["last_pair_refresh_at"]
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() >= refresh_seconds


async def _resolve_live_pair(pool, call, refresh_seconds: int) -> tuple[str, str | None]:
    pair_address = call["current_pair_address"] or call["pair_address"]
    dex_url = call["current_dexscreener_url"] or call["dexscreener_url"]

    if not _pair_refresh_due(call, refresh_seconds):
        return pair_address, dex_url

    try:
        best = await _fetch_best_pair(call["token_address"])
        if best and best.get("pairAddress"):
            pair_address = best["pairAddress"]
            dex_url = best.get("url") or f"https://dexscreener.com/solana/{pair_address}"
            await db.update_current_pair(
                pool,
                call_id=call["id"],
                pair_address=pair_address,
                dexscreener_url=dex_url,
            )
        else:
            await db.touch_pair_refresh(pool, call_id=call["id"])
    except Exception:
        logger.exception("Could not refresh best pair for call %s", call["id"])
        await db.touch_pair_refresh(pool, call_id=call["id"])

    return pair_address, dex_url


# ---------- Historical OHLCV ----------


def _extract_ohlcv(data: dict | list) -> list[list[float]]:
    if not isinstance(data, dict):
        return []
    try:
        rows = data["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


def _ohlcv_high(
    rows: list[list[float]],
    min_timestamp: int,
    *,
    expected_start_price: float | None = None,
) -> tuple[float | None, int | None]:
    """Return a validated OHLCV high and reject obvious bad-candle scaling.

    Pool OHLCV can occasionally contain a wrong token side or a malformed wick.
    A single such candle used to turn a ~$1M ATH into ~$1B.  We validate the
    first available candle against the known call price and suppress only
    extreme wick outliers; normal memecoin volatility is intentionally allowed.
    """
    parsed: list[tuple[int, float | None, float, float | None, float | None]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        try:
            ts = int(row[0])
            open_price = float(row[1]) if len(row) > 1 and row[1] is not None else None
            high = float(row[2])
            low = float(row[3]) if len(row) > 3 and row[3] is not None else None
            close = float(row[4]) if len(row) > 4 and row[4] is not None else None
        except (TypeError, ValueError):
            continue
        if ts < min_timestamp or high <= 0:
            continue
        parsed.append((ts, open_price, high, low, close))

    if not parsed:
        return None, None

    parsed.sort(key=lambda item: item[0])

    # The candle nearest the call should be on roughly the same price scale as
    # the price saved at publication.  A 1000x mismatch is a data-side error,
    # not a believable move at the exact moment of the call.
    if expected_start_price is not None and expected_start_price > 0:
        anchor = parsed[0]
        anchor_values = [
            value for value in (anchor[1], anchor[4], anchor[3])
            if value is not None and value > 0
        ]
        if anchor_values:
            anchor_price = sorted(anchor_values)[len(anchor_values) // 2]
            scale_ratio = max(
                anchor_price / expected_start_price,
                expected_start_price / anchor_price,
            )
            try:
                max_anchor_factor = max(10.0, float(os.getenv(
                    "HISTORY_MAX_ANCHOR_FACTOR", "500"
                )))
            except ValueError:
                max_anchor_factor = 500.0
            if scale_ratio > max_anchor_factor:
                logger.warning(
                    "Rejecting OHLCV series: start-price scale mismatch %.1fx "
                    "(history=%s entry=%s)",
                    scale_ratio, anchor_price, expected_start_price,
                )
                return None, None

    try:
        max_wick_factor = max(10.0, float(os.getenv(
            "HISTORY_MAX_WICK_FACTOR", "100"
        )))
    except ValueError:
        max_wick_factor = 100.0

    best_price = None
    best_ts = None
    for ts, open_price, high, low, close in parsed:
        body_values = [
            value for value in (open_price, close, low)
            if value is not None and value > 0
        ]
        candidate = high
        if body_values:
            body_reference = max(body_values)
            # Keep legitimate wicks, but never let an isolated 100x+ malformed
            # wick define the public ATH.  Use the candle body instead.
            if high / body_reference > max_wick_factor:
                logger.warning(
                    "Ignoring extreme OHLCV wick %.1fx at %s",
                    high / body_reference, ts,
                )
                candidate = max(
                    value for value in (open_price, close)
                    if value is not None and value > 0
                )

        if candidate > 0 and (best_price is None or candidate > best_price):
            best_price = candidate
            best_ts = ts

    return best_price, best_ts


def _geckoterminal_pool_url(
    pool_address: str,
    token_address: str,
    timeframe: str,
    *,
    aggregate: int,
    before_timestamp: int,
    limit: int,
) -> str:
    params = urllib.parse.urlencode({
        "aggregate": aggregate,
        "before_timestamp": before_timestamp,
        "limit": limit,
        "currency": "usd",
        "token": token_address,
    })
    return (
        "https://api.geckoterminal.com/api/v2/networks/solana/pools/"
        f"{pool_address}/ohlcv/{timeframe}?{params}"
    )


def _coingecko_token_url(
    token_address: str,
    timeframe: str,
    *,
    aggregate: int,
    before_timestamp: int,
    limit: int,
) -> str:
    params = urllib.parse.urlencode({
        "aggregate": aggregate,
        "before_timestamp": before_timestamp,
        "limit": limit,
        "currency": "usd",
    })
    return (
        "https://pro-api.coingecko.com/api/v3/onchain/networks/solana/tokens/"
        f"{token_address}/ohlcv/{timeframe}?{params}"
    )


def _history_request(
    *,
    token_address: str,
    pool_address: str | None,
    timeframe: str,
    aggregate: int,
    before_timestamp: int,
    limit: int,
    pro_api_key: str | None,
) -> list[list[float]]:
    if pro_api_key:
        url = _coingecko_token_url(
            token_address,
            timeframe,
            aggregate=aggregate,
            before_timestamp=before_timestamp,
            limit=limit,
        )
        data = _http_json(url, {"x-cg-pro-api-key": pro_api_key})
        return _extract_ohlcv(data)

    if not pool_address:
        return []

    url = _geckoterminal_pool_url(
        pool_address,
        token_address,
        timeframe,
        aggregate=aggregate,
        before_timestamp=before_timestamp,
        limit=limit,
    )
    data = _http_json(url)
    return _extract_ohlcv(data)


async def _fetch_history_window(
    *,
    token_address: str,
    pool_address: str | None,
    start: datetime,
    end: datetime,
    pro_api_key: str | None,
    expected_start_price: float | None = None,
) -> tuple[float | None, datetime | None]:
    """Get the maximum OHLCV high from start to end.

    The first ~16 hours use 1-minute candles. Older history uses 1-hour
    candles, paginated. OHLC high preserves short spikes inside each candle.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    if end_ts <= start_ts:
        return None, None

    best_price = None
    best_ts = None

    def consider(price: float | None, ts: int | None) -> None:
        nonlocal best_price, best_ts
        if price is not None and (best_price is None or price > best_price):
            best_price = price
            best_ts = ts

    # High precision around the call itself, where the first spike matters most.
    minute_end = min(end_ts, start_ts + 16 * 3600)
    minute_rows = await asyncio.to_thread(
        _history_request,
        token_address=token_address,
        pool_address=pool_address,
        timeframe="minute",
        aggregate=1,
        before_timestamp=minute_end + 60,
        limit=1000,
        pro_api_key=pro_api_key,
    )
    price, ts = _ohlcv_high(
        minute_rows,
        start_ts,
        expected_start_price=expected_start_price,
    )
    consider(price, ts)

    # For older history, hourly OHLCV keeps the true intra-hour HIGH while
    # requiring only a few pages even for months of calls.
    hourly_cutoff = start_ts + 15 * 3600
    if end_ts > hourly_cutoff:
        before = end_ts + 3600
        max_pages = max(1, int(os.getenv("HISTORY_MAX_HOURLY_PAGES", "4")))

        for _ in range(max_pages):
            rows = await asyncio.to_thread(
                _history_request,
                token_address=token_address,
                pool_address=pool_address,
                timeframe="hour",
                aggregate=1,
                before_timestamp=before,
                limit=1000,
                pro_api_key=pro_api_key,
            )
            if not rows:
                break

            price, ts = _ohlcv_high(rows, hourly_cutoff)
            consider(price, ts)

            timestamps = []
            for row in rows:
                try:
                    timestamps.append(int(row[0]))
                except (TypeError, ValueError, IndexError):
                    pass
            if not timestamps:
                break

            oldest = min(timestamps)
            if oldest <= hourly_cutoff:
                break
            before = oldest - 1

    observed_at = (
        datetime.fromtimestamp(best_ts, tz=timezone.utc)
        if best_ts is not None
        else None
    )
    return best_price, observed_at


async def _historical_ath(call) -> tuple[float | None, datetime | None, str | None]:
    entry_price = _to_float(call["entry_price"])
    if entry_price is None or entry_price <= 0:
        return None, None, "Entry price is missing"

    now = datetime.now(timezone.utc)
    call_time = call["call_time"]
    if call_time.tzinfo is None:
        call_time = call_time.replace(tzinfo=timezone.utc)

    pro_key = os.getenv("COINGECKO_PRO_API_KEY", "").strip() or None

    if pro_key:
        try:
            price, observed_at = await _fetch_history_window(
                token_address=call["token_address"],
                pool_address=None,
                start=call_time,
                end=now,
                pro_api_key=pro_key,
                expected_start_price=entry_price,
            )
            if price is not None:
                return price, observed_at, "coingecko_token_ohlcv"
        except Exception as exc:
            logger.warning(
                "CoinGecko token OHLCV failed for call %s; falling back to pools: %s",
                call["id"],
                exc,
            )

    # Keyless fallback: query both the original pool and the latest best pool.
    # This helps across migrations while keeping the original call pool history.
    pools: list[str] = []
    for value in (call["pair_address"], call["current_pair_address"]):
        if value and value not in pools:
            pools.append(value)

    best_price = None
    best_time = None
    successful_pool = False
    last_error = None

    for pool_address in pools:
        try:
            price, observed_at = await _fetch_history_window(
                token_address=call["token_address"],
                pool_address=pool_address,
                start=call_time,
                end=now,
                pro_api_key=None,
                expected_start_price=entry_price,
            )
            successful_pool = True
            if price is not None and (best_price is None or price > best_price):
                best_price = price
                best_time = observed_at
        except urllib.error.HTTPError as exc:
            last_error = f"GeckoTerminal HTTP {exc.code}"
            logger.warning(
                "GeckoTerminal OHLCV HTTP error for call %s pool %s: %s",
                call["id"],
                pool_address,
                exc,
            )
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "GeckoTerminal OHLCV error for call %s pool %s: %s",
                call["id"],
                pool_address,
                exc,
            )

    if best_price is not None:
        return best_price, best_time, "geckoterminal_pool_ohlcv"
    if successful_pool:
        return None, None, "No OHLCV candles returned since call"
    return None, None, last_error or "Historical OHLCV unavailable"


# ---------- Milestones ----------


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



def _milestone_message(
    call,
    milestone: int,
    profit: float,
    valuation: float,
    *,
    source: str,
) -> str:
    multiple = valuation / float(call["entry_valuation"])
    metric = "MC" if call["valuation_type"] == "marketCap" else "FDV"
    token_name = html.escape(str(call["token_name"]))
    symbol = html.escape(str(call["symbol"]))

    if source == "history":
        status_title = "🏆 <b>שיא מאומת מההיסטוריה</b>"
        value_label = "ATH מאז ההמלצה"
        detail_line = "🕒 <i>היעד אומת מהיסטוריית OHLCV מאז פרסום ה־Call.</i>"
    else:
        status_title = "⚡ <b>היעד הושג עכשיו</b>"
        value_label = "שווי נוכחי"
        detail_line = None

    parts = [
        "💥💥💥 <b>יעד רווח הושג!</b> 💥💥💥",
        "━━━━━━━━━━━━━━━━━━",
        f"🪙 <b>{token_name} (${symbol})</b>",
        f"🚀 <b>יעד: +{milestone}%</b>",
        f"🔥 <b>{multiple:.2f}X מאז ההמלצה</b>",
        "━━━━━━━━━━━━━━━━━━",
        status_title,
        "",
        f"💰 <b>{metric} בפרסום:</b> {_fmt_usd(call['entry_valuation'])}",
        f"🏆 <b>{value_label}:</b> {_fmt_usd(valuation)}",
        f"📈 <b>רווח:</b> +{profit:.1f}%",
        f"⏱ <b>זמן מההמלצה:</b> {_duration_text(call['call_time'])}",
    ]

    if detail_line:
        parts.extend(["", detail_line])

    parts.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ <i>אזהרה: מסחר בקריפטו וב־Memecoins הוא תנודתי ומסוכן ועלול "
        "להביא לאובדן מלא של ההשקעה. אין בפרסום משום ייעוץ או המלצה אישית. "
        "כל החלטת מסחר היא באחריות המשתמש בלבד.</i>",
    ])
    return "\n".join(parts)


async def _publish_milestone(
    application: Application,
    pool,
    call,
    *,
    milestone: int,
    profit: float,
    valuation: float,
    price: float | None,
    reached_at: datetime | None,
    source: str,
    dex_url: str | None,
) -> bool:
    claimed = await db.claim_milestone(
        pool,
        call_id=call["id"],
        milestone=milestone,
        observed_profit_percent=profit,
        observed_valuation=valuation,
        observed_price=price,
        reached_at=reached_at,
        source=source,
    )
    if not claimed:
        return False

    # Refreshing the list contents and building the buttons are intentionally
    # independent. A temporary failure while editing one list must NEVER strip
    # the daily/monthly/all-time buttons from a milestone post.
    try:
        await stats.refresh_all_stats_pages(
            application,
            pool,
            channel_chat_id=int(call["channel_chat_id"]),
        )
    except Exception:
        logger.exception("Could not refresh channel list pages before milestone; keeping list buttons")

    try:
        keyboard = await stats.recommendation_keyboard(
            application,
            pool,
            channel_chat_id=int(call["channel_chat_id"]),
            dex_url=dex_url or call["dexscreener_url"],
        )
    except Exception:
        logger.exception("Could not build channel list buttons for milestone")
        # Last-resort fallback keeps DexScreener visible. The stats refresh loop
        # will repair the channel-native list pages for the next post.
        fallback_rows = []
        final_dex_url = dex_url or call["dexscreener_url"]
        if final_dex_url:
            fallback_rows.append([InlineKeyboardButton("📈 DexScreener", url=final_dex_url)])
        keyboard = InlineKeyboardMarkup(fallback_rows) if fallback_rows else None


    try:
        sent = await application.bot.send_message(
            chat_id=call["channel_chat_id"],
            text=_milestone_message(
                call,
                milestone,
                profit,
                valuation,
                source=source,
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception(
            "Failed publishing %s milestone %s for call %s",
            source,
            milestone,
            call["id"],
        )
        await db.release_milestone_claim(
            pool,
            call_id=call["id"],
            milestone=milestone,
        )
        return False

    await db.finalize_milestone(
        pool,
        call_id=call["id"],
        milestone=milestone,
        telegram_message_id=sent.message_id,
    )
    return True


def _format_call_time_israel(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y • %H:%M")


def _original_call_url(call) -> str | None:
    channel = os.getenv("CHANNEL_ID", "").strip()
    if channel.startswith("@") and call["telegram_message_id"]:
        return f"https://t.me/{channel[1:]}/{int(call['telegram_message_id'])}"
    return None


def _percent_change(current: float, previous: float | None) -> float | None:
    if previous is None or previous <= 0:
        return None
    return ((current / previous) - 1.0) * 100.0


def _momentum_message(
    call,
    *,
    current_valuation: float,
    current_profit: float,
    volume_m5_usd: float,
    volume_change_1m: float | None,
    volume_change_5m: float | None,
) -> str:
    metric = "MC" if call["valuation_type"] == "marketCap" else "FDV"
    name = html.escape(str(call["token_name"]))
    symbol = html.escape(str(call["symbol"]))
    ath = max(float(call["highest_valuation"] or call["entry_valuation"]), current_valuation)

    one = f"{volume_change_1m:+.1f}%" if volume_change_1m is not None else "לא זמין"
    five = f"{volume_change_5m:+.1f}%" if volume_change_5m is not None else "לא זמין"

    return (
        "⚡📊 <b>זינוק חריג בווליום</b> 📊⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{name} (${symbol})</b>\n"
        "🔥 <b>המטבע מראה סימנים לקפיצה</b>\n\n"
        f"💥 <b>ווליום 5 דקות:</b> {_fmt_usd(volume_m5_usd)}\n"
        f"⏱ <b>שינוי בווליום לעומת לפני ~1 דקה:</b> {one}\n"
        f"⏱ <b>שינוי בווליום לעומת לפני ~5 דקות:</b> {five}\n\n"
        f"🎯 <b>{metric} בזמן ההמלצה:</b> {_fmt_usd(call['entry_valuation'])}\n"
        f"💰 <b>{metric} נוכחי:</b> {_fmt_usd(current_valuation)}\n"
        f"📈 <b>רווח/הפסד נוכחי:</b> {current_profit:+.1f}%\n"
        f"🏆 <b>ATH מאז ההמלצה:</b> {_fmt_usd(ath)}\n"
        f"🕒 <b>הומלץ:</b> {_format_call_time_israel(call['call_time'])}\n\n"
        "ℹ️ <i>הטריגר להתראה הוא זינוק בווליום בלבד; המחיר לא חייב להיות בעלייה בזמן הזיהוי.</i>\n"
        "⚠️ <i>זינוק בווליום עשוי להקדים תנועה חדה, אך אינו מבטיח עליית מחיר.</i>"
    )


async def _publish_momentum_alert(
    application: Application,
    pool,
    call,
    *,
    current_price: float | None,
    current_valuation: float,
    current_profit: float,
    volume_m5_usd: float,
    volume_change_1m: float | None,
    volume_change_5m: float | None,
    dex_url: str | None,
) -> bool:
    try:
        cooldown = max(5, int(os.getenv("MOMENTUM_COOLDOWN_MINUTES", "30")))
    except ValueError:
        cooldown = 30

    alert_id = await db.claim_momentum_alert(
        pool,
        call_id=int(call["id"]),
        cooldown_minutes=cooldown,
        volume_change_1m_percent=volume_change_1m,
        volume_change_5m_percent=volume_change_5m,
        volume_m5_usd=volume_m5_usd,
        observed_price=current_price,
        observed_valuation=current_valuation,
        observed_profit_percent=current_profit,
    )
    if alert_id is None:
        return False

    try:
        keyboard = await stats.recommendation_keyboard(
            application,
            pool,
            channel_chat_id=int(call["channel_chat_id"]),
            dex_url=dex_url or call["current_dexscreener_url"] or call["dexscreener_url"],
        )
        rows = [list(row) for row in keyboard.inline_keyboard]
        original_url = _original_call_url(call)
        if original_url:
            rows.insert(1, [InlineKeyboardButton("🧾 להמלצה המקורית", url=original_url)])
        keyboard = InlineKeyboardMarkup(rows)

        sent = await application.bot.send_message(
            chat_id=int(call["channel_chat_id"]),
            text=_momentum_message(
                call,
                current_valuation=current_valuation,
                current_profit=current_profit,
                volume_m5_usd=volume_m5_usd,
                volume_change_1m=volume_change_1m,
                volume_change_5m=volume_change_5m,
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed publishing momentum alert for call %s", call["id"])
        await db.release_momentum_alert(pool, alert_id=alert_id)
        return False

    await db.finalize_momentum_alert(
        pool,
        alert_id=alert_id,
        telegram_message_id=sent.message_id,
    )
    return True


# ---------- Live loop ----------


async def check_calls_once(application: Application) -> None:
    pool = application.bot_data.get("db")
    if pool is None:
        logger.warning("Live tracker skipped: PostgreSQL is unavailable.")
        return

    try:
        pair_refresh_seconds = max(60, int(os.getenv("PAIR_REFRESH_SECONDS", "300")))
    except ValueError:
        pair_refresh_seconds = 300

    try:
        concurrency = max(1, min(10, int(os.getenv("TRACK_CONCURRENCY", "6"))))
    except ValueError:
        concurrency = 6

    calls = await db.get_active_calls(pool)
    semaphore = asyncio.Semaphore(concurrency)

    async def check_one(call) -> None:
        async with semaphore:
            try:
                pair_address, dex_url = await _resolve_live_pair(
                    pool,
                    call,
                    pair_refresh_seconds,
                )

                pair = await _fetch_pair(pair_address)
                if not pair and pair_address != call["pair_address"]:
                    pair = await _fetch_pair(call["pair_address"])
                    dex_url = call["dexscreener_url"]

                if not pair:
                    logger.warning(
                        "No DexScreener pair data for call %s (%s)",
                        call["id"],
                        call["symbol"],
                    )
                    return

                current_price = _to_float(pair.get("priceUsd"))
                current_valuation = _to_float(pair.get(call["valuation_type"]))
                current_volume_m5 = _to_float((pair.get("volume") or {}).get("m5")) or 0.0

                if current_valuation is None or current_valuation <= 0:
                    logger.warning(
                        "No usable %s for call %s",
                        call["valuation_type"],
                        call["id"],
                    )
                    return

                entry = float(call["entry_valuation"])
                profit = ((current_valuation / entry) - 1.0) * 100.0
                observed_high = max(entry, current_valuation)
                observed_high_profit = max(0.0, ((observed_high / entry) - 1.0) * 100.0)

                await db.update_call_snapshot(
                    pool,
                    call_id=call["id"],
                    current_price=current_price,
                    current_valuation=current_valuation,
                    observed_high_valuation=observed_high,
                    observed_high_profit_percent=observed_high_profit,
                )

                await db.record_price_snapshot(
                    pool,
                    call_id=int(call["id"]),
                    price=current_price,
                    valuation=current_valuation,
                    volume_m5_usd=current_volume_m5,
                )

                one_ref, five_ref = await db.get_momentum_reference_values(
                    pool,
                    call_id=int(call["id"]),
                )

                def ref_volume(ref):
                    if ref is None:
                        return None
                    return _to_float(ref["volume_m5_usd"])

                volume_change_1m = _percent_change(current_volume_m5, ref_volume(one_ref))
                volume_change_5m = _percent_change(current_volume_m5, ref_volume(five_ref))

                try:
                    threshold_1m = float(os.getenv("VOLUME_SPIKE_1M_PERCENT", "50"))
                except ValueError:
                    threshold_1m = 50.0
                try:
                    threshold_5m = float(os.getenv("VOLUME_SPIKE_5M_PERCENT", "100"))
                except ValueError:
                    threshold_5m = 100.0
                try:
                    min_volume_m5 = float(os.getenv("VOLUME_SPIKE_MIN_5M_USD", "1000"))
                except ValueError:
                    min_volume_m5 = 1000.0

                milestone = int(profit // 100) * 100 if profit >= 100 else 0
                has_new_milestone = milestone > int(call["highest_milestone"] or 0)

                momentum_triggered = current_volume_m5 >= min_volume_m5 and (
                    (volume_change_1m is not None and volume_change_1m >= threshold_1m)
                    or (volume_change_5m is not None and volume_change_5m >= threshold_5m)
                )

                # Avoid two near-identical posts in the same cycle: if a new
                # 100% milestone was just reached, the milestone post wins.
                if momentum_triggered and not has_new_milestone:
                    await _publish_momentum_alert(
                        application,
                        pool,
                        call,
                        current_price=current_price,
                        current_valuation=current_valuation,
                        current_profit=profit,
                        volume_m5_usd=current_volume_m5,
                        volume_change_1m=volume_change_1m,
                        volume_change_5m=volume_change_5m,
                        dex_url=dex_url,
                    )

                if not has_new_milestone:
                    return

                await _publish_milestone(
                    application,
                    pool,
                    call,
                    milestone=milestone,
                    profit=profit,
                    valuation=current_valuation,
                    price=current_price,
                    reached_at=datetime.now(timezone.utc),
                    source="live",
                    dex_url=dex_url,
                )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Live tracker failed on call %s", call["id"])

    await asyncio.gather(*(check_one(call) for call in calls))
    try:
        await db.prune_price_snapshots(pool)
    except Exception:
        logger.exception("Could not prune old momentum snapshots")


async def live_tracker_loop(application: Application) -> None:
    try:
        interval = int(os.getenv("TRACK_INTERVAL_SECONDS", "30"))
    except ValueError:
        interval = 30

    interval = max(15, interval)
    logger.info("Live tracker started; interval=%ss", interval)

    while True:
        try:
            await check_calls_once(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected live tracker cycle error")
        await asyncio.sleep(interval)


# ---------- Historical reconciliation loop ----------


async def reconcile_one_history(application: Application) -> None:
    pool = application.bot_data.get("db")
    if pool is None:
        logger.warning("History reconciliation skipped: PostgreSQL is unavailable.")
        return

    try:
        recheck_seconds = max(
            300,
            int(os.getenv("HISTORY_RECHECK_SECONDS", "600")),
        )
    except ValueError:
        recheck_seconds = 600

    call = await db.get_call_due_for_history(
        pool,
        recheck_seconds=recheck_seconds,
    )
    if call is None:
        return

    try:
        historical_price, observed_at, source_or_error = await _historical_ath(call)
        if historical_price is None:
            await db.mark_history_checked(
                pool,
                call_id=call["id"],
                error=source_or_error,
            )
            return

        entry_price = float(call["entry_price"])
        entry_valuation = float(call["entry_valuation"])
        historical_price = max(entry_price, historical_price)
        multiple = historical_price / entry_price
        ath_valuation = entry_valuation * multiple
        ath_profit = (multiple - 1.0) * 100.0

        await db.update_call_history(
            pool,
            call_id=call["id"],
            ath_price=historical_price,
            ath_valuation=ath_valuation,
            ath_profit_percent=ath_profit,
            ath_source=source_or_error or "ohlcv",
            ath_observed_at=observed_at,
        )

        if ath_profit < 100:
            return

        milestone = int(ath_profit // 100) * 100
        if milestone <= int(call["highest_milestone"] or 0):
            return

        await _publish_milestone(
            application,
            pool,
            call,
            milestone=milestone,
            profit=ath_profit,
            valuation=ath_valuation,
            price=historical_price,
            reached_at=observed_at,
            source="history",
            dex_url=call["current_dexscreener_url"] or call["dexscreener_url"],
        )

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("History reconciliation failed on call %s", call["id"])
        await db.mark_history_checked(
            pool,
            call_id=call["id"],
            error=str(exc)[:500],
        )


async def history_reconciliation_loop(application: Application) -> None:
    try:
        loop_seconds = int(os.getenv("HISTORY_LOOP_SECONDS", "60"))
    except ValueError:
        loop_seconds = 60
    loop_seconds = max(60, loop_seconds)

    try:
        batch_size = max(1, min(3, int(os.getenv("HISTORY_BATCH_SIZE", "2"))))
    except ValueError:
        batch_size = 2

    logger.info(
        "Historical ATH reconciliation started; up to %s due calls every %ss",
        batch_size,
        loop_seconds,
    )

    await asyncio.sleep(15)

    while True:
        try:
            for _ in range(batch_size):
                await reconcile_one_history(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected historical reconciliation error")
        await asyncio.sleep(loop_seconds)


async def tracker_loop(application: Application) -> None:
    """Run live tracking and history reconciliation together."""
    await asyncio.gather(
        live_tracker_loop(application),
        history_reconciliation_loop(application),
    )
