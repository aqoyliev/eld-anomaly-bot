from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp


@dp.message_handler(CommandHelp())
async def bot_help(message: types.Message):
    text = ("Commands: ",
            "/start - Start the bot",
            "/status - List currently flagged vehicles",
            "/history - Recent disconnection events",
            "/help - Help")

    await message.answer("\n".join(text))
