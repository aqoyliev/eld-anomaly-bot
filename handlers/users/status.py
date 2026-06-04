from aiogram import types
from aiogram.dispatcher.filters import Command

from loader import dp
from utils.eld import store
from utils.eld.formatting import format_status_line


@dp.message_handler(Command("status"))
async def show_status(message: types.Message):
    events = store.get_active_events()
    if not events:
        await message.answer("✅ No vehicles currently flagged.")
        return

    lines = [f"<b>Currently flagged vehicles ({len(events)}):</b>", ""]
    lines += [format_status_line(e) for e in events]
    await message.answer("\n".join(lines))
