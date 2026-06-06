from aiogram import types

from data import config

# Shown to everyone in group chats (regular users use the bot only in groups).
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
    bot = dp.bot
    # The user menu lives in group chats only — regular users can't use the bot
    # in a DM (see PrivateAdminOnlyMiddleware), so we don't advertise commands
    # there. Private chats fall through to: per-admin menus below, or nothing.
    await bot.set_my_commands(
        DEFAULT_COMMANDS, scope=types.BotCommandScopeAllGroupChats()
    )
    # Clear any previously-set global default so it doesn't linger in non-admin
    # DMs (scopes set on earlier runs persist server-side). Empty list = remove.
    try:
        await bot.set_my_commands([], scope=types.BotCommandScopeDefault())
    except Exception:
        pass
    # Per-admin command menus, scoped to each admin's private chat. Wrapped in
    # try/except since an admin who has never opened the bot has no chat yet.
    for admin in config.ADMINS:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=types.BotCommandScopeChat(chat_id=int(admin)),
            )
        except Exception:
            pass
