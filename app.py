import asyncio

from aiogram import executor

from loader import dp, bot
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands
from utils.eld import store
from utils.eld.poller import run_poller


async def on_startup(dispatcher):
    # Initialise the anomaly-events database
    store.init_db()

    # Default commands (/start, /status, /history, /help)
    await set_default_commands(dispatcher)

    # Notify admin that the bot has started
    await on_startup_notify(dispatcher)

    # Launch the background ELD-anomaly poller (polls every POLL_INTERVAL seconds)
    asyncio.create_task(run_poller(bot))


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup)
