from aiogram import types
from aiogram.dispatcher.filters import Command

from filters.is_admin import is_admin_or_viewer
from loader import dp
from utils.eld import store
from utils.eld.formatting import format_status_line


def _moving(events):
    """Keep only events whose unit is still moving. A disconnected unit that
    pulls over gets ``stopped_at`` set (the event stays active to watch for a
    reconnect); /status excludes those so it shows only moving-while-disconnected
    units."""
    return [e for e in events if e.stopped_at is None]


async def _company_blocks(companies):
    """One block per company that has moving flagged units (empty ones skipped)."""
    blocks = []
    for c in companies:
        events = _moving(await store.get_active_events(c.id))
        if not events:
            continue
        block = [f"<b>{c.name} — moving & disconnected ({len(events)}):</b>"]
        block += [format_status_line(e) for e in events]
        blocks.append("\n".join(block))
    return blocks


@dp.message_handler(Command("status"))
async def show_status(message: types.Message):
    companies = await store.get_companies_by_chat(message.chat.id)

    if len(companies) == 1:
        events = _moving(await store.get_active_events(companies[0].id))
        if not events:
            await message.answer("✅ No moving units currently disconnected.")
            return
        lines = [f"<b>Moving units disconnected ({len(events)}):</b>", ""]
        lines += [format_status_line(e) for e in events]
        await message.answer("\n".join(lines))
        return

    if companies:
        # Several companies share this chat: one block per company.
        blocks = await _company_blocks(companies)
        if not blocks:
            await message.answer("✅ No moving units currently disconnected.")
            return
        await message.answer("\n\n".join(blocks))
        return

    if is_admin_or_viewer(message.from_user):
        # Unbound chat (e.g. an admin/viewer DM): aggregate across all companies.
        blocks = await _company_blocks(await store.active_companies())
        if not blocks:
            await message.answer(
                "✅ No moving units currently disconnected (any company)."
            )
            return
        await message.answer("\n\n".join(blocks))
        return

    await message.answer("This chat isn't linked to a company yet.")
