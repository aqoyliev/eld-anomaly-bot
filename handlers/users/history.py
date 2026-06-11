from aiogram import types
from aiogram.dispatcher.filters import Command

from filters.is_admin import is_admin_or_viewer
from loader import dp
from utils.eld import store
from utils.eld.formatting import format_history_line

HISTORY_LIMIT = 20


async def _company_blocks(companies):
    """One block per company that has recorded events (empty ones skipped)."""
    blocks = []
    for c in companies:
        events = await store.get_recent_events(c.id, limit=HISTORY_LIMIT)
        if not events:
            continue
        block = [f"<b>{c.name} — recent events (last {len(events)}):</b>"]
        block += [format_history_line(e) for e in events]
        blocks.append("\n".join(block))
    return blocks


@dp.message_handler(Command("history"))
async def show_history(message: types.Message):
    companies = await store.get_companies_by_chat(message.chat.id)

    if len(companies) == 1:
        events = await store.get_recent_events(companies[0].id, limit=HISTORY_LIMIT)
        if not events:
            await message.answer("No disconnection events recorded yet.")
            return
        lines = [f"<b>Recent disconnection events (last {len(events)}):</b>", ""]
        lines += [format_history_line(e) for e in events]
        await message.answer("\n".join(lines))
        return

    if companies:
        # Several companies share this chat: one block per company.
        blocks = await _company_blocks(companies)
        if not blocks:
            await message.answer("No disconnection events recorded yet.")
            return
        await message.answer("\n\n".join(blocks))
        return

    if is_admin_or_viewer(message.from_user):
        # Unbound chat (e.g. an admin/viewer DM): aggregate across all companies.
        blocks = await _company_blocks(await store.active_companies())
        if not blocks:
            await message.answer("No disconnection events recorded yet (any company).")
            return
        await message.answer("\n\n".join(blocks))
        return

    await message.answer("This chat isn't linked to a company yet.")
