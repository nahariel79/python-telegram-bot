"""PostgreSQL access layer backed by an asyncpg connection pool."""

import logging
from datetime import datetime

import asyncpg


logger = logging.getLogger(__name__)


class DuplicateCallError(Exception):
    """Raised when a token has already been published as a call."""


POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 10
COMMAND_TIMEOUT = 10.0

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_CALLS_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
    id                      BIGSERIAL PRIMARY KEY,
    token_address           TEXT NOT NULL,
    token_name              TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    pair_address            TEXT NOT NULL,
    dexscreener_url         TEXT,
    valuation_type          TEXT NOT NULL CHECK (valuation_type IN ('marketCap', 'fdv')),
    entry_price             DOUBLE PRECISION,
    entry_valuation         DOUBLE PRECISION NOT NULL,
    current_price           DOUBLE PRECISION,
    current_valuation       DOUBLE PRECISION,
    highest_valuation       DOUBLE PRECISION NOT NULL,
    highest_profit_percent  DOUBLE PRECISION NOT NULL DEFAULT 0,
    highest_milestone       INTEGER NOT NULL DEFAULT 0,
    live_highest_valuation  DOUBLE PRECISION,
    live_highest_profit_percent DOUBLE PRECISION,
    live_ath_price          DOUBLE PRECISION,
    ath_price               DOUBLE PRECISION,
    ath_source              TEXT,
    ath_observed_at         TIMESTAMPTZ,
    history_checked_at      TIMESTAMPTZ,
    history_error           TEXT,
    current_pair_address    TEXT,
    current_dexscreener_url TEXT,
    last_pair_refresh_at    TIMESTAMPTZ,
    channel_chat_id         BIGINT NOT NULL,
    telegram_message_id     BIGINT NOT NULL,
    call_time               TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at         TIMESTAMPTZ,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_MILESTONES_TABLE = """
CREATE TABLE IF NOT EXISTS milestones (
    id                      BIGSERIAL PRIMARY KEY,
    call_id                 BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    milestone               INTEGER NOT NULL,
    observed_profit_percent DOUBLE PRECISION NOT NULL,
    observed_valuation      DOUBLE PRECISION NOT NULL,
    observed_price          DOUBLE PRECISION,
    reached_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    source                  TEXT NOT NULL DEFAULT 'live',
    published_at            TIMESTAMPTZ,
    telegram_message_id     BIGINT,
    UNIQUE (call_id, milestone)
);
"""

CREATE_SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_STATS_PAGES_TABLE = """
CREATE TABLE IF NOT EXISTS stats_pages (
    period          TEXT NOT NULL CHECK (period IN ('day', 'week', 'month', 'all')),
    page_number     INTEGER NOT NULL,
    channel_chat_id BIGINT NOT NULL,
    message_id      BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period, page_number)
);
"""

CREATE_SYSTEM_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


CREATE_CALLS_ACTIVE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_calls_active ON calls(active) WHERE active = TRUE;
"""

CREATE_HISTORY_DUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_calls_history_due
ON calls(history_checked_at)
WHERE active = TRUE;
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "2026_10_11_stage5_history_ath",
        """
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS ath_price DOUBLE PRECISION;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS ath_source TEXT;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS ath_observed_at TIMESTAMPTZ;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS history_checked_at TIMESTAMPTZ;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS history_error TEXT;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS current_pair_address TEXT;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS current_dexscreener_url TEXT;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS last_pair_refresh_at TIMESTAMPTZ;
        ALTER TABLE milestones ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live';

        UPDATE calls
        SET ath_price = entry_price
        WHERE ath_price IS NULL AND entry_price IS NOT NULL;

        UPDATE calls
        SET current_pair_address = pair_address
        WHERE current_pair_address IS NULL;

        UPDATE calls
        SET current_dexscreener_url = dexscreener_url
        WHERE current_dexscreener_url IS NULL;
        """,
    ),
    (
        "2026_10_11_stage6_channel_stats_and_duplicate_guard",
        """
        CREATE TABLE IF NOT EXISTS stats_pages (
            period          TEXT NOT NULL CHECK (period IN ('day', 'week', 'month', 'all')),
            page_number     INTEGER NOT NULL,
            channel_chat_id BIGINT NOT NULL,
            message_id      BIGINT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (period, page_number)
        );

        CREATE OR REPLACE FUNCTION reject_duplicate_call_token()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM calls
                WHERE lower(token_address) = lower(NEW.token_address)
            ) THEN
                RAISE EXCEPTION 'token_already_called' USING ERRCODE = '23505';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_reject_duplicate_call_token ON calls;
        CREATE TRIGGER trg_reject_duplicate_call_token
        BEFORE INSERT ON calls
        FOR EACH ROW EXECUTE FUNCTION reject_duplicate_call_token();
        """,
    ),

    (
        "2026_10_12_stage12_monthly_lists",
        """
        ALTER TABLE stats_pages DROP CONSTRAINT IF EXISTS stats_pages_period_check;
        ALTER TABLE stats_pages
        ADD CONSTRAINT stats_pages_period_check
        CHECK (period IN ('day', 'week', 'month', 'all'));
        """,
    ),
    (
        "2026_10_13_stage16_safe_historical_ath",
        """
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS live_highest_valuation DOUBLE PRECISION;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS live_highest_profit_percent DOUBLE PRECISION;
        ALTER TABLE calls ADD COLUMN IF NOT EXISTS live_ath_price DOUBLE PRECISION;

        -- Rebuild a clean live-only baseline from entry/current values and
        -- milestones that were actually observed by the live DexScreener tracker.
        UPDATE calls c
        SET live_highest_valuation = GREATEST(
                c.entry_valuation,
                COALESCE(c.current_valuation, c.entry_valuation),
                COALESCE((
                    SELECT MAX(m.observed_valuation)
                    FROM milestones m
                    WHERE m.call_id = c.id AND m.source = 'live'
                ), c.entry_valuation)
            ),
            live_ath_price = GREATEST(
                COALESCE(c.entry_price, 0),
                COALESCE(c.current_price, 0),
                COALESCE((
                    SELECT MAX(m.observed_price)
                    FROM milestones m
                    WHERE m.call_id = c.id AND m.source = 'live'
                ), 0)
            );

        UPDATE calls
        SET live_highest_profit_percent = CASE
                WHEN entry_valuation > 0 THEN
                    ((live_highest_valuation / entry_valuation) - 1.0) * 100.0
                ELSE 0
            END;

        -- Remove any previously contaminated history-derived ATH from the
        -- public ranking immediately. Historical reconciliation will rebuild it
        -- with the stricter validation introduced in Stage 16.
        UPDATE calls c
        SET highest_valuation = COALESCE(c.live_highest_valuation, c.entry_valuation),
            highest_profit_percent = COALESCE(c.live_highest_profit_percent, 0),
            ath_price = COALESCE(NULLIF(c.live_ath_price, 0), c.entry_price),
            ath_source = 'stage16_live_baseline',
            ath_observed_at = NULL,
            history_checked_at = NULL,
            history_error = NULL,
            highest_milestone = COALESCE((
                SELECT MAX(m.milestone)
                FROM milestones m
                WHERE m.call_id = c.id AND m.source = 'live'
            ), 0);
        """,
    ),
    (
        "2026_10_15_stage19_momentum_and_repeat_signals",
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id          BIGSERIAL PRIMARY KEY,
            call_id     BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            price       DOUBLE PRECISION,
            valuation   DOUBLE PRECISION NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_price_snapshots_call_time
        ON price_snapshots(call_id, observed_at DESC);

        CREATE TABLE IF NOT EXISTS momentum_alerts (
            id                      BIGSERIAL PRIMARY KEY,
            call_id                 BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            change_1m_percent       DOUBLE PRECISION,
            change_5m_percent       DOUBLE PRECISION,
            observed_price          DOUBLE PRECISION,
            observed_valuation      DOUBLE PRECISION NOT NULL,
            observed_profit_percent DOUBLE PRECISION NOT NULL,
            observed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            telegram_message_id     BIGINT
        );

        CREATE INDEX IF NOT EXISTS idx_momentum_alerts_call_time
        ON momentum_alerts(call_id, observed_at DESC);

        CREATE TABLE IF NOT EXISTS call_signals (
            id                  BIGSERIAL PRIMARY KEY,
            call_id             BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            signal_type         TEXT NOT NULL CHECK (signal_type IN ('entry', 'accumulate')),
            telegram_message_id BIGINT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    (
        "2026_10_15_stage20_volume_momentum",
        """
        ALTER TABLE price_snapshots
        ADD COLUMN IF NOT EXISTS volume_m5_usd DOUBLE PRECISION;

        ALTER TABLE momentum_alerts
        ADD COLUMN IF NOT EXISTS volume_m5_usd DOUBLE PRECISION;

        ALTER TABLE momentum_alerts
        ADD COLUMN IF NOT EXISTS volume_change_1m_percent DOUBLE PRECISION;

        ALTER TABLE momentum_alerts
        ADD COLUMN IF NOT EXISTS volume_change_5m_percent DOUBLE PRECISION;
        """,
    ),
]

UPSERT_USER = """
INSERT INTO users (telegram_id, username, first_name)
VALUES ($1, $2, $3)
ON CONFLICT (telegram_id) DO UPDATE
    SET username   = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        last_seen  = now()
RETURNING (xmax = 0) AS is_new;
"""


async def _apply_migrations(conn: asyncpg.Connection) -> None:
    await conn.execute(CREATE_SCHEMA_MIGRATIONS_TABLE)

    for version, sql in MIGRATIONS:
        already_applied = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE version = $1;",
            version,
        )
        if already_applied:
            continue

        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES ($1);",
                version,
            )
        logger.info("Applied database migration: %s", version)


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Open the pool and initialize/upgrade the schema without deleting data."""
    pool = await asyncpg.create_pool(
        dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        command_timeout=COMMAND_TIMEOUT,
    )
    async with pool.acquire() as conn:
        await conn.execute(CREATE_USERS_TABLE)
        await conn.execute(CREATE_CALLS_TABLE)
        await conn.execute(CREATE_MILESTONES_TABLE)
        await conn.execute(CREATE_STATS_PAGES_TABLE)
        await conn.execute(CREATE_SYSTEM_STATE_TABLE)
        await _apply_migrations(conn)
        await conn.execute(CREATE_CALLS_ACTIVE_INDEX)
        await conn.execute(CREATE_HISTORY_DUE_INDEX)
    logger.info("PostgreSQL pool ready (schema initialized/migrated safely).")
    return pool


async def upsert_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> bool:
    is_new = await pool.fetchval(
        UPSERT_USER, telegram_id, username, first_name
    )
    return bool(is_new)


async def count_users(pool: asyncpg.Pool) -> int:
    return int(await pool.fetchval("SELECT count(*) FROM users;"))


async def insert_call(
    pool: asyncpg.Pool,
    *,
    token_address: str,
    token_name: str,
    symbol: str,
    pair_address: str,
    dexscreener_url: str | None,
    valuation_type: str,
    entry_price: float | None,
    entry_valuation: float,
    channel_chat_id: int,
    telegram_message_id: int,
) -> int:
    """Persist a new call. Duplicate token addresses are rejected permanently."""
    try:
        value = await pool.fetchval(
            """
            INSERT INTO calls (
                token_address, token_name, symbol, pair_address, dexscreener_url,
                valuation_type, entry_price, entry_valuation, current_price,
                current_valuation, highest_valuation, live_highest_valuation,
                live_highest_profit_percent, live_ath_price, ath_price, ath_source,
                current_pair_address, current_dexscreener_url, last_pair_refresh_at,
                channel_chat_id, telegram_message_id
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$7,$8,$8,$8,0,$7,$7,'entry',$4,$5,now(),$9,$10
            )
            RETURNING id;
            """,
            token_address, token_name, symbol, pair_address, dexscreener_url,
            valuation_type, entry_price, entry_valuation, channel_chat_id,
            telegram_message_id,
        )
        return int(value)
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateCallError(token_address) from exc


async def get_active_calls(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return list(
        await pool.fetch(
            """
            SELECT *
            FROM calls
            WHERE active = TRUE
            ORDER BY id ASC;
            """
        )
    )


async def update_current_pair(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    pair_address: str,
    dexscreener_url: str | None,
) -> None:
    await pool.execute(
        """
        UPDATE calls
        SET current_pair_address = $2,
            current_dexscreener_url = COALESCE($3, current_dexscreener_url),
            last_pair_refresh_at = now()
        WHERE id = $1;
        """,
        call_id,
        pair_address,
        dexscreener_url,
    )


async def touch_pair_refresh(pool: asyncpg.Pool, *, call_id: int) -> None:
    await pool.execute(
        "UPDATE calls SET last_pair_refresh_at = now() WHERE id = $1;",
        call_id,
    )


async def update_call_snapshot(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    current_price: float | None,
    current_valuation: float,
    observed_high_valuation: float,
    observed_high_profit_percent: float,
) -> None:
    """Update live state without ever reducing the stored ATH."""
    await pool.execute(
        """
        UPDATE calls
        SET current_price = $2,
            current_valuation = $3,
            live_highest_valuation = GREATEST(
                COALESCE(live_highest_valuation, entry_valuation), $4
            ),
            live_highest_profit_percent = GREATEST(
                COALESCE(live_highest_profit_percent, 0), $5
            ),
            live_ath_price = GREATEST(
                COALESCE(live_ath_price, entry_price, 0), COALESCE($2, 0)
            ),
            highest_valuation = GREATEST(highest_valuation, $4),
            highest_profit_percent = GREATEST(highest_profit_percent, $5),
            last_checked_at = now()
        WHERE id = $1;
        """,
        call_id,
        current_price,
        current_valuation,
        observed_high_valuation,
        observed_high_profit_percent,
    )


async def record_price_snapshot(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    price: float | None,
    valuation: float,
    volume_m5_usd: float | None,
) -> None:
    await pool.execute(
        """
        INSERT INTO price_snapshots(call_id, price, valuation, volume_m5_usd)
        VALUES ($1, $2, $3, $4);
        """,
        call_id,
        price,
        valuation,
        volume_m5_usd,
    )


async def get_momentum_reference_values(
    pool: asyncpg.Pool,
    *,
    call_id: int,
) -> tuple[asyncpg.Record | None, asyncpg.Record | None]:
    one_minute = await pool.fetchrow(
        """
        SELECT price, valuation, volume_m5_usd, observed_at
        FROM price_snapshots
        WHERE call_id = $1
          AND observed_at BETWEEN now() - interval '100 seconds'
                              AND now() - interval '40 seconds'
        ORDER BY ABS(EXTRACT(EPOCH FROM (observed_at - (now() - interval '60 seconds'))))
        LIMIT 1;
        """,
        call_id,
    )
    five_minutes = await pool.fetchrow(
        """
        SELECT price, valuation, volume_m5_usd, observed_at
        FROM price_snapshots
        WHERE call_id = $1
          AND observed_at BETWEEN now() - interval '390 seconds'
                              AND now() - interval '240 seconds'
        ORDER BY ABS(EXTRACT(EPOCH FROM (observed_at - (now() - interval '300 seconds'))))
        LIMIT 1;
        """,
        call_id,
    )
    return one_minute, five_minutes


async def prune_price_snapshots(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM price_snapshots WHERE observed_at < now() - interval '30 minutes';"
    )


async def claim_momentum_alert(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    cooldown_minutes: int,
    volume_change_1m_percent: float | None,
    volume_change_5m_percent: float | None,
    volume_m5_usd: float,
    observed_price: float | None,
    observed_valuation: float,
    observed_profit_percent: float,
) -> int | None:
    value = await pool.fetchval(
        """
        INSERT INTO momentum_alerts(
            call_id, volume_change_1m_percent, volume_change_5m_percent,
            volume_m5_usd, observed_price, observed_valuation, observed_profit_percent
        )
        SELECT $1, $3, $4, $5, $6, $7, $8
        WHERE NOT EXISTS (
            SELECT 1
            FROM momentum_alerts
            WHERE call_id = $1
              AND observed_at > now() - ($2 * interval '1 minute')
        )
        RETURNING id;
        """,
        call_id,
        cooldown_minutes,
        volume_change_1m_percent,
        volume_change_5m_percent,
        volume_m5_usd,
        observed_price,
        observed_valuation,
        observed_profit_percent,
    )
    return int(value) if value is not None else None


async def finalize_momentum_alert(
    pool: asyncpg.Pool,
    *,
    alert_id: int,
    telegram_message_id: int,
) -> None:
    await pool.execute(
        "UPDATE momentum_alerts SET telegram_message_id = $2 WHERE id = $1;",
        alert_id,
        telegram_message_id,
    )


async def release_momentum_alert(pool: asyncpg.Pool, *, alert_id: int) -> None:
    await pool.execute("DELETE FROM momentum_alerts WHERE id = $1;", alert_id)


async def get_call_due_for_history(
    pool: asyncpg.Pool,
    *,
    recheck_seconds: int,
) -> asyncpg.Record | None:
    """Return one active call that has waited longest for historical reconciliation."""
    return await pool.fetchrow(
        """
        SELECT *
        FROM calls
        WHERE active = TRUE
          AND (
              history_checked_at IS NULL
              OR history_checked_at < now() - ($1 * interval '1 second')
          )
        ORDER BY
            CASE WHEN history_checked_at IS NULL THEN 0 ELSE 1 END ASC,
            CASE WHEN history_checked_at IS NULL THEN call_time END DESC,
            history_checked_at ASC NULLS FIRST,
            call_time DESC
        LIMIT 1;
        """,
        recheck_seconds,
    )


async def update_call_history(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    ath_price: float,
    ath_valuation: float,
    ath_profit_percent: float,
    ath_source: str,
    ath_observed_at: datetime | None,
) -> None:
    """Merge a history-derived ATH into the existing record."""
    await pool.execute(
        """
        UPDATE calls
        SET ath_price = $2,
            ath_source = $5,
            ath_observed_at = $6,
            highest_valuation = GREATEST(
                COALESCE(live_highest_valuation, entry_valuation), $3
            ),
            highest_profit_percent = GREATEST(
                COALESCE(live_highest_profit_percent, 0), $4
            ),
            history_checked_at = now(),
            history_error = NULL
        WHERE id = $1;
        """,
        call_id,
        ath_price,
        ath_valuation,
        ath_profit_percent,
        ath_source,
        ath_observed_at,
    )


async def mark_history_checked(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    error: str | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE calls
        SET history_checked_at = now(),
            history_error = $2
        WHERE id = $1;
        """,
        call_id,
        error,
    )


async def claim_milestone(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    milestone: int,
    observed_profit_percent: float,
    observed_valuation: float,
    observed_price: float | None,
    reached_at: datetime | None = None,
    source: str = "live",
) -> bool:
    """Atomically claim a new highest milestone. Lower/duplicate targets are ignored."""
    row_id = await pool.fetchval(
        """
        INSERT INTO milestones (
            call_id,
            milestone,
            observed_profit_percent,
            observed_valuation,
            observed_price,
            reached_at,
            source
        )
        SELECT $1,$2,$3,$4,$5,COALESCE($6, now()),$7
        FROM calls
        WHERE id = $1
          AND highest_milestone < $2
        ON CONFLICT (call_id, milestone) DO NOTHING
        RETURNING id;
        """,
        call_id,
        milestone,
        observed_profit_percent,
        observed_valuation,
        observed_price,
        reached_at,
        source,
    )
    return row_id is not None


async def finalize_milestone(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    milestone: int,
    telegram_message_id: int,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE milestones
                SET published_at = now(),
                    telegram_message_id = $3
                WHERE call_id = $1 AND milestone = $2;
                """,
                call_id,
                milestone,
                telegram_message_id,
            )
            await conn.execute(
                """
                UPDATE calls
                SET highest_milestone = GREATEST(highest_milestone, $2)
                WHERE id = $1;
                """,
                call_id,
                milestone,
            )


async def release_milestone_claim(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    milestone: int,
) -> None:
    await pool.execute(
        """
        DELETE FROM milestones
        WHERE call_id = $1
          AND milestone = $2
          AND published_at IS NULL;
        """,
        call_id,
        milestone,
    )


async def get_calls_for_period(
    pool: asyncpg.Pool,
    period: str,
) -> list[asyncpg.Record]:
    """Return calls for daily, monthly, or all-time leader lists."""
    if period == "day":
        where_clause = "WHERE call_time >= now() - interval '24 hours'"
    elif period == "month":
        where_clause = "WHERE call_time >= now() - interval '30 days'"
    elif period == "week":
        # Legacy compatibility only; Stage 12 no longer links to weekly lists.
        where_clause = "WHERE call_time >= now() - interval '7 days'"
    elif period == "all":
        where_clause = ""
    else:
        raise ValueError(f"Unsupported period: {period}")

    query = f"""
        SELECT
            id,
            token_address,
            token_name,
            symbol,
            valuation_type,
            entry_valuation,
            highest_valuation,
            highest_profit_percent,
            ath_price,
            ath_source,
            history_checked_at,
            dexscreener_url,
            current_dexscreener_url,
            telegram_message_id,
            call_time,
            last_checked_at
        FROM calls
        {where_clause}
        ORDER BY
            GREATEST(
                COALESCE(highest_profit_percent, 0.0),
                CASE
                    WHEN entry_valuation > 0 THEN
                        ((COALESCE(highest_valuation, entry_valuation) / entry_valuation) - 1.0) * 100.0
                    ELSE 0.0
                END
            ) DESC,
            COALESCE(highest_valuation, entry_valuation) DESC,
            call_time DESC;
    """
    return list(await pool.fetch(query))


async def get_existing_call(
    pool: asyncpg.Pool,
    token_address: str,
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT id, token_address, token_name, symbol, valuation_type,
               entry_price, entry_valuation, current_price, current_valuation,
               highest_valuation, highest_profit_percent, call_time,
               pair_address, current_pair_address, dexscreener_url,
               current_dexscreener_url, channel_chat_id, telegram_message_id
        FROM calls
        WHERE lower(token_address) = lower($1)
        ORDER BY call_time ASC
        LIMIT 1;
        """,
        token_address,
    )


async def get_call_by_id(pool: asyncpg.Pool, call_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT *
        FROM calls
        WHERE id = $1
        LIMIT 1;
        """,
        call_id,
    )


async def log_call_signal(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    signal_type: str,
    telegram_message_id: int | None,
) -> None:
    await pool.execute(
        """
        INSERT INTO call_signals(call_id, signal_type, telegram_message_id)
        VALUES ($1, $2, $3);
        """,
        call_id,
        signal_type,
        telegram_message_id,
    )


async def token_was_called(pool: asyncpg.Pool, token_address: str) -> bool:
    return bool(
        await pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM calls WHERE lower(token_address)=lower($1));",
            token_address,
        )
    )


async def get_daily_leaders(
    pool: asyncpg.Pool,
    limit: int = 3,
) -> list[asyncpg.Record]:
    return list(
        await pool.fetch(
            """
            SELECT symbol, token_name, valuation_type, entry_valuation,
                   highest_valuation, highest_profit_percent
            FROM calls
            WHERE call_time >= now() - interval '24 hours'
            ORDER BY
                CASE
                    WHEN entry_valuation > 0 THEN
                        ((COALESCE(highest_valuation, entry_valuation) / entry_valuation) - 1.0) * 100.0
                    ELSE 0.0
                END DESC,
                COALESCE(highest_valuation, entry_valuation) DESC,
                call_time DESC
            LIMIT $1;
            """,
            limit,
        )
    )


async def get_weekly_leaders(
    pool: asyncpg.Pool,
    limit: int = 3,
) -> list[asyncpg.Record]:
    """Return the strongest calls from the last 7 days, ranked by ATH profit."""
    return list(
        await pool.fetch(
            """
            SELECT symbol, token_name, valuation_type, entry_valuation,
                   highest_valuation, highest_profit_percent
            FROM calls
            WHERE call_time >= now() - interval '7 days'
            ORDER BY
                CASE
                    WHEN entry_valuation > 0 THEN
                        ((COALESCE(highest_valuation, entry_valuation) / entry_valuation) - 1.0) * 100.0
                    ELSE 0.0
                END DESC,
                COALESCE(highest_valuation, entry_valuation) DESC,
                call_time DESC
            LIMIT $1;
            """,
            limit,
        )
    )


async def get_all_time_performance_summary(
    pool: asyncpg.Pool,
) -> asyncpg.Record:
    """Return all-time call count plus cumulative/average ATH profit percentages.

    cumulative_ath_pnl_percent is the arithmetic sum of each call's ATH profit
    percentage. It is a channel statistic, not a portfolio return calculation.
    average_ath_profit_percent is that cumulative figure divided by call count.
    """
    return await pool.fetchrow(
        """
        WITH per_call AS (
            SELECT
                CASE
                    WHEN entry_valuation > 0 THEN
                        ((COALESCE(highest_valuation, entry_valuation) / entry_valuation) - 1.0) * 100.0
                    ELSE 0.0
                END AS ath_profit_percent
            FROM calls
        )
        SELECT
            COUNT(*)::BIGINT AS total_calls,
            COALESCE(SUM(ath_profit_percent), 0.0)::DOUBLE PRECISION
                AS cumulative_ath_pnl_percent,
            CASE
                WHEN COUNT(*) > 0 THEN
                    (COALESCE(SUM(ath_profit_percent), 0.0) / COUNT(*))::DOUBLE PRECISION
                ELSE 0.0::DOUBLE PRECISION
            END AS average_ath_profit_percent
        FROM per_call;
        """
    )


async def get_stats_page(
    pool: asyncpg.Pool,
    period: str,
    page_number: int,
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM stats_pages WHERE period=$1 AND page_number=$2;",
        period, page_number,
    )


async def list_stats_pages(
    pool: asyncpg.Pool,
    period: str,
) -> list[asyncpg.Record]:
    return list(
        await pool.fetch(
            "SELECT * FROM stats_pages WHERE period=$1 ORDER BY page_number;",
            period,
        )
    )


async def upsert_stats_page(
    pool: asyncpg.Pool,
    *,
    period: str,
    page_number: int,
    channel_chat_id: int,
    message_id: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO stats_pages(period,page_number,channel_chat_id,message_id)
        VALUES($1,$2,$3,$4)
        ON CONFLICT(period,page_number) DO UPDATE
        SET channel_chat_id=EXCLUDED.channel_chat_id,
            message_id=EXCLUDED.message_id,
            updated_at=now();
        """,
        period, page_number, channel_chat_id, message_id,
    )


async def delete_stats_page_record(
    pool: asyncpg.Pool,
    *,
    period: str,
    page_number: int,
) -> None:
    await pool.execute(
        "DELETE FROM stats_pages WHERE period=$1 AND page_number=$2;",
        period, page_number,
    )


async def get_posts_for_keyboard_repair(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Return every published call/milestone post that should carry live list buttons."""
    return list(
        await pool.fetch(
            """
            SELECT DISTINCT ON (channel_chat_id, telegram_message_id)
                channel_chat_id,
                telegram_message_id,
                dexscreener_url
            FROM (
                SELECT
                    c.channel_chat_id,
                    c.telegram_message_id,
                    COALESCE(c.current_dexscreener_url, c.dexscreener_url) AS dexscreener_url
                FROM calls c
                WHERE c.telegram_message_id IS NOT NULL

                UNION ALL

                SELECT
                    c.channel_chat_id,
                    m.telegram_message_id,
                    COALESCE(c.current_dexscreener_url, c.dexscreener_url) AS dexscreener_url
                FROM milestones m
                JOIN calls c ON c.id = m.call_id
                WHERE m.telegram_message_id IS NOT NULL
                  AND m.published_at IS NOT NULL
            ) AS posts
            ORDER BY channel_chat_id, telegram_message_id;
            """
        )
    )



async def get_system_state(pool: asyncpg.Pool, key: str) -> str | None:
    return await pool.fetchval(
        "SELECT value_text FROM system_state WHERE key = $1;",
        key,
    )


async def set_system_state(pool: asyncpg.Pool, key: str, value: str) -> None:
    await pool.execute(
        """
        INSERT INTO system_state(key, value_text, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
        SET value_text = EXCLUDED.value_text, updated_at = now();
        """,
        key, value,
    )


async def find_calls_for_admin_delete(pool: asyncpg.Pool, term: str) -> list[asyncpg.Record]:
    """Find a call by exact CA, exact symbol, call ID, or exact token name."""
    clean = term.strip()
    symbol = clean.lstrip("$")
    return list(
        await pool.fetch(
            """
            SELECT id, token_address, token_name, symbol, valuation_type,
                   entry_valuation, highest_valuation, highest_profit_percent,
                   call_time, channel_chat_id, telegram_message_id,
                   COALESCE(current_dexscreener_url, dexscreener_url) AS dexscreener_url
            FROM calls
            WHERE lower(token_address) = lower($1)
               OR lower(symbol) = lower($2)
               OR lower(token_name) = lower($1)
               OR id::text = $1
            ORDER BY call_time DESC
            LIMIT 10;
            """,
            clean,
            symbol,
        )
    )


async def get_call_delete_bundle(pool: asyncpg.Pool, call_id: int) -> tuple[asyncpg.Record | None, list[asyncpg.Record]]:
    call = await pool.fetchrow(
        """
        SELECT id, token_address, token_name, symbol, valuation_type,
               entry_valuation, highest_valuation, highest_profit_percent,
               call_time, channel_chat_id, telegram_message_id
        FROM calls WHERE id=$1;
        """,
        call_id,
    )
    if call is None:
        return None, []
    milestones = list(
        await pool.fetch(
            """
            SELECT milestone, telegram_message_id
            FROM milestones
            WHERE call_id=$1 AND telegram_message_id IS NOT NULL
            ORDER BY milestone;
            """,
            call_id,
        )
    )
    return call, milestones


async def delete_call_admin(pool: asyncpg.Pool, call_id: int) -> bool:
    """Delete one call. Milestones are removed automatically by ON DELETE CASCADE."""
    result = await pool.execute("DELETE FROM calls WHERE id=$1;", call_id)
    return result.endswith("1")


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("PostgreSQL pool closed.")
