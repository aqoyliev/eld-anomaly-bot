"""/id — show the sender's Telegram user id and the chat id.

Open to everyone: admin commands silently ignore non-admins (IsAdmin filter),
so this is the quick way to find the user id that must be added to the ADMINS
env var when an admin command appears to "not work".
"""

from aiogram import types
from aiogram.dispatcher.filters import Command

from loader import dp


@dp.message_handler(Command("id"), state="*")
async def show_id(message: types.Message):
    await message.answer(
        f"Your user id: <code>{message.from_user.id}</code>\n"
        f"This chat id: <code>{message.chat.id}</code>"
    )
