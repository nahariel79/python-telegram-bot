"""PostgreSQL access layer backed by an asyncpg connection pool."""

import logging
from datetime import datetime

import asyncpg


logger = logging.getLogger(__name__)

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
    """Persist a newly published call and return its database ID."""
    return int(
        await pool.fetchval(
            """
            INSERT INTO calls (
                token_address,
                token_name,
                symbol,
                pair_address,
                dexscreener_url,
                valuation_type,
                entry_price,
                entry_valuation,
                current_price,
                current_valuation,
                highest_valuation,
                ath_price,
                ath_source,
                current_pair_address,
                current_dexscreener_url,
                last_pair_refresh_at,
                channel_chat_id,
                telegram_message_id
            )
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$7,$8,$8,$7,'entry',$4,$5,now(),$9,$10
            )
            RETURNING id;
            """,
            token_address,
            token_name,
            symbol,
            pair_address,
            dexscreener_url,
            valuation_type,
            entry_price,
            entry_valuation,
            channel_chat_id,
            telegram_message_id,
        )
    )


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
        ORDER BY history_checked_at ASC NULLS FIRST, call_time ASC
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
        SET ath_price = CASE
                WHEN ath_price IS NULL OR $2 > ath_price THEN $2
                ELSE ath_price
            END,
            ath_source = CASE
                WHEN ath_price IS NULL OR $2 > ath_price THEN $5
                ELSE ath_source
            END,
            ath_observed_at = CASE
                WHEN ath_price IS NULL OR $2 > ath_price THEN $6
                ELSE ath_observed_at
            END,
            highest_valuation = GREATEST(highest_valuation, $3),
            highest_profit_percent = GREATEST(highest_profit_percent, $4),
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
    """Return calls for daily, weekly, or all-time lists."""
    if period == "day":
        where_clause = "WHERE call_time >= now() - interval '24 hours'"
    elif period == "week":
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
            call_time
        FROM calls
        {where_clause}
        ORDER BY call_time DESC;
    """
    return list(await pool.fetch(query))


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("PostgreSQL pool closed.")
