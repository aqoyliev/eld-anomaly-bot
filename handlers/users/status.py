from aiogram import types
from aiogram.dispatcher.filters import Command

from data import config
from loader import dp
from utils.eld import store
from utils.eld.formatting import format_status_line


def _is_admin(message: types.Message) -> bool:
    # config.ADMINS holds Telegram user ids as strings (env.list); from_user.id
    # is an int, so compare via str(). from_user can be None (channel posts).
    return bool(message.from_user) and str(message.from_user.id) in config.ADMINS


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

    if _is_admin(message):
        # Unbound chat (e.g. an admin DM): aggregate across all active companies.
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
