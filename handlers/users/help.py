from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from filters.is_admin import is_admin_user, is_viewer_user
from loader import dp

USER_HELP = (
    "<b>ELD Disconnection Bot</b>\n"
    "I watch your fleet and flag any unit that's moving on GoMotive/Samsara "
    "while its Quantum ELD has gone silent (a likely disconnection). Alerts "
    "land in this group automatically.\n\n"
    "<b>Commands</b>\n"
    "/status — vehicles currently flagged for this group's company\n"
    "/history — recent disconnection events (last 20)\n"
    "/help — show this help\n\n"
    "<i>This bot is used inside your company's group chat.</i>"
)

ADMIN_HELP = (
    "<b>ELD Disconnection Bot — admin help</b>\n"
    "I watch each company's fleet and flag any unit moving on its movement "
    "provider (GoMotive or Samsara) while its Quantum ELD has gone silent. Each "
    "company has its own tokens and alert group.\n\n"
    "<b>Everyone (in a company's group)</b>\n"
    "/status — vehicles currently flagged for that group's company\n"
    "/history — recent disconnection events (last 20)\n"
    "/help — show this help\n"
    "<i>In an admin DM, /status and /history aggregate across all companies.</i>\n\n"
    "<b>Admin only</b>\n"
    "/addcompany — wizard to add a company (DM me; token messages auto-delete)\n"
    "/bindhere &lt;name|id&gt; — run in a group to make it that company's alert chat\n"
    "/companies — list companies, their state, and masked tokens\n"
    "/activate &lt;name|id&gt; — resume polling a company\n"
    "/deactivate &lt;name|id&gt; — stop polling a company (history kept)\n"
    "/cancel — abort the /addcompany wizard\n\n"
    "<b>Add a company</b>\n"
    "1. DM me <code>/addcompany</code> and follow the prompts.\n"
    "2. Add me to that company's group and send <code>/bindhere &lt;name&gt;</code> "
    "there. Polling starts on the next cycle."
)


VIEWER_HELP = (
    "<b>ELD Disconnection Bot — viewer</b>\n"
    "You have read-only access. DM me to check status across all companies.\n\n"
    "<b>Commands</b>\n"
    "/status — vehicles currently flagged (all companies)\n"
    "/history — recent disconnection events (last 20, all companies)\n"
    "/help — show this help"
)


@dp.message_handler(CommandHelp())
async def bot_help(message: types.Message):
    if is_admin_user(message.from_user):
        text = ADMIN_HELP
    elif is_viewer_user(message.from_user):
        text = VIEWER_HELP
    else:
        text = USER_HELP
    await message.answer(text)
