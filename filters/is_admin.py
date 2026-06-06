from aiogram import types
from aiogram.dispatcher.filters import BoundFilter

from data import config


class IsAdmin(BoundFilter):
    """Passes only for users listed in config.ADMINS. ADMINS holds ids as strings
    (env.list), while from_user.id is an int, so compare via str()."""

    async def check(self, message: types.Message) -> bool:
        # from_user can be None (e.g. channel posts); treat that as "not admin".
        return bool(message.from_user) and str(message.from_user.id) in config.ADMINS
