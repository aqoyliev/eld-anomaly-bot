from typing import Optional

from aiogram import types
from aiogram.dispatcher.filters import BoundFilter

from data import config


def is_admin_user(user: Optional[types.User]) -> bool:
    """True if `user` is a configured admin. config.ADMINS holds Telegram user ids
    as strings (env.list) while User.id is an int, so compare via str(). `user` can
    be None (e.g. channel posts) — treat that as not-admin."""
    return user is not None and str(user.id) in config.ADMINS


def is_viewer_user(user: Optional[types.User]) -> bool:
    """True if `user` is a configured read-only viewer. config.VIEWERS holds
    Telegram user ids as strings; viewers may DM the bot for /status and /history
    only (no admin commands). `user` may be None — treat as not a viewer."""
    return user is not None and str(user.id) in config.VIEWERS


def is_admin_or_viewer(user: Optional[types.User]) -> bool:
    """Admins implicitly have the viewer's read access too."""
    return is_admin_user(user) or is_viewer_user(user)


class IsAdmin(BoundFilter):
    """Passes only for users listed in config.ADMINS."""

    async def check(self, message: types.Message) -> bool:
        return is_admin_user(message.from_user)
