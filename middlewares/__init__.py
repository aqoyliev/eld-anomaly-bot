from aiogram import Dispatcher

from loader import dp
from .throttling import ThrottlingMiddleware
from .private_guard import PrivateAdminOnlyMiddleware


if __name__ == "middlewares":
    dp.middleware.setup(ThrottlingMiddleware())
    # Block non-admins from using the bot in private DMs (group chats unaffected).
    dp.middleware.setup(PrivateAdminOnlyMiddleware())
