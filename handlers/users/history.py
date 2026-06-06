from aiogram import types
from aiogram.dispatcher.filters import Command

from filters.is_admin import is_admin_user
from loader import dp
from utils.eld import store
from utils.eld.formatting import format_history_line

HISTORY_LIMIT = 20


@dp.message_handler(Command("history"))
async def show_history(message: types.Message):
    company = await store.get_company_by_chat(message.chat.id)

    if company is not None:
        events = await store.get_recent_events(company.id, limit=HISTORY_LIMIT)
        if not events:
            await message.answer("No disconnection events recorded yet.")
            return
        lines = [f"<b>Recent disconnection events (last {len(events)}):</b>", ""]
        lines += [format_history_line(e) for e in events]
        await message.answer("\n".join(lines))
        return

    if is_admin_user(message.from_user):
        # Unbound chat (e.g. an admin DM): aggregate across all active companies.
        blocks = []
        for c in await store.active_companies():
            events = await store.get_recent_events(c.id, limit=HISTORY_LIMIT)
            if not events:
                continue
            block = [f"<b>{c.name} — recent events (last {len(events)}):</b>"]
            block += [format_history_line(e) for e in events]
            blocks.append("\n".join(block))
        if not blocks:
            await message.answer("No disconnection events recorded yet (any company).")
            return
        await message.answer("\n\n".join(blocks))
        return

    await message.answer("This chat isn't linked to a company yet.")
