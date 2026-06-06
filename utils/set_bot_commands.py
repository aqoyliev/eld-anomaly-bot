from aiogram import types

from data import config

# Shown to everyone.
DEFAULT_COMMANDS = [
    types.BotCommand("start", "Start the bot"),
    types.BotCommand("status", "List currently flagged vehicles"),
    types.BotCommand("history", "Recent disconnection events"),
    types.BotCommand("help", "Help"),
]

# Shown only to admins (their menu also includes the defaults).
ADMIN_COMMANDS = DEFAULT_COMMANDS + [
    types.BotCommand("addcompany", "Add a company (wizard)"),
    types.BotCommand("bindhere", "Link this chat to a company"),
    types.BotCommand("companies", "List companies"),
    types.BotCommand("activate", "Activate a company"),
    types.BotCommand("deactivate", "Deactivate a company"),
]


async def set_default_commands(dp):
    await dp.bot.set_my_commands(DEFAULT_COMMANDS)
    # Per-admin command menus, scoped to each admin's private chat. Wrapped in
    # try/except since an admin who has never opened the bot has no chat yet.
    for admin in config.ADMINS:
        try:
            await dp.bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=types.BotCommandScopeChat(chat_id=int(admin)),
            )
        except Exception:
            pass
