"""Entrypoint for the Telegram bot."""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from bot import cache, db, notice, stats, tracker, webapp
from bot.config import Settings
from bot.handlers import (
    DB_KEY,
    REDIS_KEY,
    error_handler,
    register_handlers,
    set_bot_commands,
)


logger = logging.getLogger(__name__)
TRACKER_TASK_KEY = "tracker_task"
STATS_TASK_KEY = "stats_task"
NOTICE_TASK_KEY = "notice_task"
DASHBOARD_TASK_KEY = "dashboard_task"


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
        level=level,
    )


async def _connect_optional(name, url, connect):
    if not url:
        logger.warning("%s URL not set - running without it.", name)
        return None
    try:
        return await connect(url)
    except Exception:
        logger.exception("%s unavailable - running without it.", name)
        return None


def build_application(settings: Settings) -> Application:
    async def on_startup(application: Application) -> None:
        application.bot_data[DB_KEY] = await _connect_optional(
            "PostgreSQL", settings.database_url, db.create_pool
        )
        application.bot_data[REDIS_KEY] = await _connect_optional(
            "Redis", settings.redis_url, cache.create_client
        )

        await set_bot_commands(application)

        application.bot_data[TRACKER_TASK_KEY] = asyncio.create_task(
            tracker.tracker_loop(application),
            name="memecoin-call-tracker",
        )
        application.bot_data[STATS_TASK_KEY] = asyncio.create_task(
            stats.stats_refresh_loop(application),
            name="channel-stats-refresh",
        )
        application.bot_data[NOTICE_TASK_KEY] = asyncio.create_task(
            notice.beta_notice_loop(application),
            name="beta-channel-notice",
        )
        application.bot_data[DASHBOARD_TASK_KEY] = asyncio.create_task(
            webapp.dashboard_snapshot_loop(application),
            name="public-dashboard-snapshot",
        )

    async def on_shutdown(application: Application) -> None:
        for task_key in (TRACKER_TASK_KEY, STATS_TASK_KEY, NOTICE_TASK_KEY, DASHBOARD_TASK_KEY):
            task = application.bot_data.get(task_key)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        pool = application.bot_data.get(DB_KEY)
        if pool is not None:
            await db.close_pool(pool)

        client = application.bot_data.get(REDIS_KEY)
        if client is not None:
            await cache.close_client(client)

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    register_handlers(application)
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    # Serve the public dashboard on Railway's PORT in a daemon thread.
    webapp.start_http_server()

    application = build_application(settings)
    logger.info("Bot is running with polling.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
