"""PostgreSQL access layer backed by an asyncpg connection pool."""

import logging

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
    published_at            TIMESTAMPTZ,
    telegram_message_id     BIGINT,
    UNIQUE (call_id, milestone)
);
"""

CREATE_CALLS_ACTIVE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_calls_active ON calls(active) WHERE active = TRUE;
"""

UPSERT_USER = """
INSERT INTO users (telegram_id, username, first_name)
VALUES ($1, $2, $3)
ON CONFLICT (telegram_id) DO UPDATE
    SET username   = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        last_seen  = now()
RETURNING (xmax = 0) AS is_new;
"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Open the connection pool and ensure the schema exists."""
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
        await conn.execute(CREATE_CALLS_ACTIVE_INDEX)
    logger.info("PostgreSQL pool ready (schema initialized).")
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
                channel_chat_id,
                telegram_message_id
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$7,$8,$8,$9,$10)
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


async def update_call_snapshot(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    current_price: float | None,
    current_valuation: float,
    highest_valuation: float,
    highest_profit_percent: float,
) -> None:
    await pool.execute(
        """
        UPDATE calls
        SET current_price = $2,
            current_valuation = $3,
            highest_valuation = $4,
            highest_profit_percent = $5,
            last_checked_at = now()
        WHERE id = $1;
        """,
        call_id,
        current_price,
        current_valuation,
        highest_valuation,
        highest_profit_percent,
    )


async def claim_milestone(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    milestone: int,
    observed_profit_percent: float,
    observed_valuation: float,
    observed_price: float | None,
) -> bool:
    """Atomically claim a milestone. False means it was already claimed."""
    row_id = await pool.fetchval(
        """
        INSERT INTO milestones (
            call_id,
            milestone,
            observed_profit_percent,
            observed_valuation,
            observed_price
        )
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (call_id, milestone) DO NOTHING
        RETURNING id;
        """,
        call_id,
        milestone,
        observed_profit_percent,
        observed_valuation,
        observed_price,
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
    """Allow a retry if Telegram publication failed."""
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


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("PostgreSQL pool closed.")
