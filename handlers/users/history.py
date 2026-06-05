from aiogram import types
from aiogram.dispatcher.filters import Command

from loader import dp
from utils.eld import store
from utils.eld.formatting import format_history_line

HISTORY_LIMIT = 20


@dp.message_handler(Command("history"))
async def show_history(message: types.Message):
    events = await store.get_recent_events(limit=HISTORY_LIMIT)
    if not events:
        await message.answer("No disconnection events recorded yet.")
        return

    lines = [f"<b>Recent disconnection events (last {len(events)}):</b>", ""]
    lines += [format_history_line(e) for e in events]
    await message.answer("\n".join(lines))
