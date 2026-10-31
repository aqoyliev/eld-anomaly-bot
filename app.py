import asyncio
import logging

from aiogram import executor

from loader import dp, bot
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands
from utils.eld import store
from utils.eld.poller import run_poller
from utils.eld.tracker import run_tracker
from utils.internal_api import server as internal_api


async def on_startup(dispatcher):
    # Initialise the anomaly-events database (Postgres if DATABASE_URL is set,
    # else local SQLite) and its connection pool.
    await store.init_db()

    # Default commands (/start, /status, /history, /help)
    await set_default_commands(dispatcher)

    # Notify admin that the bot has started
    await on_startup_notify(dispatcher)

    # Launch the background ELD-anomaly poller (full sweep, every POLL_INTERVAL s)
    asyncio.create_task(run_poller(bot))

    # Launch the fine-grained tracker for flagged units (every TRACK_INTERVAL s):
    # stop detection, reminders, and reconnect-based resolution.
    asyncio.create_task(run_tracker(bot))

    # Internal HTTP API for other bots (only when INTERNAL_API_KEY is set).
    # A failure to bind must not take the alerting bot down with it.
    try:
        await internal_api.start()
    except Exception:
        logging.getLogger(__name__).exception("Internal API failed to start")


async def on_shutdown(dispatcher):
    await internal_api.stop()
    # Release the database connection pool (no-op for SQLite).
    await store.close()


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
