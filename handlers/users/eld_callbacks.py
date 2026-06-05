"""Inline-button callbacks for ELD anomaly notifications."""

from aiogram import types

from loader import dp
from utils.eld import store


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("eld_ignore:"))
async def ignore_reminders(call: types.CallbackQuery):
    """The 'Ignore' button on a reminder: mute further reminders for that unit."""
    event_id = int(call.data.split(":", 1)[1])
    await store.mute_reminders(event_id)
    await call.answer("Muted — no more reminders for this unit.", show_alert=False)
    try:
        # Drop the button so it can't be tapped again.
        await call.message.edit_reply_markup()
    except Exception:
        pass
