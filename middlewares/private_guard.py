from aiogram import types
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware

from filters.is_admin import is_admin_or_viewer


class PrivateAdminOnlyMiddleware(BaseMiddleware):
    """Restrict private chats to admins and read-only viewers.

    Regular users may only use the bot inside a company's group/supergroup alert
    chat. A direct message from a non-admin/non-viewer is dropped before any
    handler runs (with a one-line explanation), so /start, /status, /history, etc.
    all stay silent in such a DM. Group messages and anything from an admin or
    viewer are unaffected — this keeps the /addcompany wizard and other admin DM
    commands working, and lets viewers DM /status and /history. (Viewers can
    reach no admin command: those are gated by the IsAdmin filter and simply
    no-op for them.)"""

    NOTICE = (
        "This bot only works inside your company's group chat — "
        "ask an admin to add you there."
    )

    async def on_pre_process_message(self, message: types.Message, data: dict):
        # Runs before handler lookup, so a blocked DM skips all filters/handlers.
        if message.chat.type == types.ChatType.PRIVATE and not is_admin_or_viewer(
            message.from_user
        ):
            await message.answer(self.NOTICE)
            raise CancelHandler()

    async def on_pre_process_callback_query(
        self, call: types.CallbackQuery, data: dict
    ):
        # Inline-button presses (e.g. the reminder "ignore" button) live in the
        # alert group, but guard private callbacks too for completeness.
        if (
            call.message
            and call.message.chat.type == types.ChatType.PRIVATE
            and not is_admin_or_viewer(call.from_user)
        ):
            await call.answer(self.NOTICE, show_alert=True)
            raise CancelHandler()
