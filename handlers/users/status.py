from aiogram import types
from aiogram.dispatcher.filters import Command

from filters.is_admin import is_admin_or_viewer
from loader import dp
from utils.eld import store
from utils.eld.formatting import format_status_line


@dp.message_handler(Command("status"))
async def show_status(message: types.Message):
    company = await store.get_company_by_chat(message.chat.id)

    if company is not None:
        events = await store.get_active_events(company.id)
        if not events:
            await message.answer("✅ No vehicles currently flagged.")
            return
        lines = [f"<b>Currently flagged vehicles ({len(events)}):</b>", ""]
        lines += [format_status_line(e) for e in events]
        await message.answer("\n".join(lines))
        return

    if is_admin_or_viewer(message.from_user):
        # Unbound chat (e.g. an admin/viewer DM): aggregate across all companies.
        blocks = []
        for c in await store.active_companies():
            events = await store.get_active_events(c.id)
            if not events:
                continue
            block = [f"<b>{c.name} — flagged ({len(events)}):</b>"]
            block += [format_status_line(e) for e in events]
            blocks.append("\n".join(block))
        if not blocks:
            await message.answer("✅ No vehicles currently flagged (any company).")
            return
        await message.answer("\n\n".join(blocks))
        return

    await message.answer("This chat isn't linked to a company yet.")
