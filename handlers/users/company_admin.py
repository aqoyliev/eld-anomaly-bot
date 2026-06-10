"""Admin-only company management, in-chat.

Commands (restricted to config.ADMINS via the IsAdmin filter):
    /addcompany   step-by-step wizard to create a company (tokens auto-deleted)
    /bindhere     link the current chat as a company's alert group
    /companies    list all companies (tokens masked)
    /activate     re-activate a company (start polling it)
    /deactivate   soft-delete a company (stop polling, keep history)
    /cancel       abort the /addcompany wizard
"""

from html import escape

from aiogram import types
from aiogram.dispatcher import FSMContext

from filters.is_admin import IsAdmin
from loader import dp
from states.company import AddCompany
from utils.eld import store


def _mask(token):
    if not token:
        return "(none)"
    return f"…{token[-4:]}" if len(token) > 4 else "…"


async def _resolve(arg: str):
    """Resolve a company by numeric id or by name."""
    arg = arg.strip()
    if arg.isdigit():
        return await store.get_company(int(arg))
    return await store.get_company_by_name(arg)


async def _intercept_command(message: types.Message) -> bool:
    """During the wizard, a message starting with '/' is almost certainly a
    command the admin meant to run (not a name/token — those never start with
    '/'). Don't swallow it as input; nudge them to /cancel first. Returns True
    if the message was intercepted."""
    if (message.text or "").startswith("/"):
        await message.answer(
            "You're in the middle of /addcompany. Finish this step, or send "
            "/cancel to abort."
        )
        return True
    return False


# --- /addcompany wizard ------------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["addcompany"], state="*")
async def add_start(message: types.Message, state: FSMContext):
    await state.finish()  # drop any half-finished wizard
    await state.set_state(AddCompany.name)
    await message.answer(
        "🏢 <b>New company</b>\n\nSend the company <b>name</b> (a short unique label).\n"
        "Send /cancel at any time to abort."
    )


@dp.message_handler(IsAdmin(), commands=["cancel"], state="*")
async def cancel(message: types.Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Nothing to cancel.")
        return
    await state.finish()
    await message.answer("Cancelled.")


@dp.message_handler(state=AddCompany.name)
async def add_name(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Please send a non-empty name, or /cancel.")
        return
    if await store.get_company_by_name(name) is not None:
        await message.answer(f"A company named <b>{escape(name)}</b> already exists. "
                             "Send a different name, or /cancel.")
        return
    await state.update_data(name=name)
    await state.set_state(AddCompany.gomotive_token)
    await message.answer(
        f"Name: <b>{escape(name)}</b> ✅\n\nNow send the <b>GoMotive API token</b>.\n"
        "<i>I'll delete your message right after reading it.</i>"
    )


async def _consume_secret(message: types.Message) -> str:
    """Read a token from the message and delete the message so the secret doesn't
    linger in chat history. Returns the token text."""
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        await message.answer(
            "⚠️ I couldn't delete that message (I may lack delete rights here) — "
            "please remove it manually so the token isn't left in the chat."
        )
    return token


@dp.message_handler(state=AddCompany.gomotive_token)
async def add_gomotive(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty token — send the GoMotive token, or /cancel.")
        return
    await state.update_data(gomotive_token=token)
    await state.set_state(AddCompany.quantum_token)
    await message.answer(
        "GoMotive token received ✅\n\nNow send the <b>Quantum ELD API token</b>.\n"
        "<i>I'll delete that message too.</i>"
    )


@dp.message_handler(state=AddCompany.quantum_token)
async def add_quantum(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty token — send the Quantum token, or /cancel.")
        return
    await state.update_data(quantum_token=token)
    await state.set_state(AddCompany.quantum_base_url)
    await message.answer(
        "Quantum token received ✅\n\nSend a custom <b>Quantum base URL</b>, "
        "or send <b>skip</b> to use the default."
    )


@dp.message_handler(state=AddCompany.quantum_base_url)
async def add_base_url(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    raw = (message.text or "").strip()
    base_url = None if raw.lower() in ("skip", "-", "") else raw
    data = await state.get_data()
    await state.finish()

    company = await store.add_company(
        name=data["name"],
        gomotive_token=data["gomotive_token"],
        quantum_token=data["quantum_token"],
        quantum_base_url=base_url,
    )
    await message.answer(
        f"✅ Created company <b>{escape(company.name)}</b> (id {company.id}).\n"
        f"  GoMotive: <code>{_mask(company.gomotive_token)}</code>\n"
        f"  Quantum: <code>{_mask(company.quantum_token)}</code>\n"
        f"  Base URL: {escape(company.quantum_base_url or '(default)')}\n\n"
        "It won't be polled until an alert chat is linked. Go to its alert group "
        f"and send:\n<code>/bindhere {escape(company.name)}</code>"
    )


# --- /bindhere ---------------------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["bindhere"], state="*")
async def bind_here(message: types.Message):
    arg = message.get_args().strip()
    if not arg:
        companies = await store.list_companies(active_only=False)
        names = ", ".join(escape(c.name) for c in companies) or "(none yet — /addcompany)"
        await message.answer(
            "Usage: <code>/bindhere &lt;company name or id&gt;</code>\n"
            "Run this in the group that should receive that company's alerts.\n\n"
            f"Companies: {names}"
        )
        return

    company = await _resolve(arg)
    if company is None:
        await message.answer(f"No company found for <b>{escape(arg)}</b>.")
        return

    existing = await store.get_company_by_chat(message.chat.id)
    if existing is not None and existing.id != company.id:
        # A chat maps to exactly one company — unbind the previous one so it
        # isn't left sharing this chat (and silently double-alerting).
        await store.bind_company_chat(existing.id, None)
        await message.answer(
            f"⚠️ This chat was linked to <b>{escape(existing.name)}</b> — unlinking it "
            f"and re-linking to <b>{escape(company.name)}</b>. "
            f"(<b>{escape(existing.name)}</b> now has "
            "no alert chat and won't be polled until you /bindhere it elsewhere.)"
        )

    await store.bind_company_chat(company.id, message.chat.id)
    state_note = "" if company.active else " (note: it's deactivated — /activate it to poll)"
    await message.answer(
        f"✅ Linked this chat (<code>{message.chat.id}</code>) to <b>{escape(company.name)}</b>. "
        f"Alerts will arrive here from the next poll cycle.{state_note}"
    )


# --- /companies --------------------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["companies"], state="*")
async def list_companies(message: types.Message):
    companies = await store.list_companies(active_only=False)
    if not companies:
        await message.answer("No companies yet. Add one with /addcompany.")
        return
    lines = [f"<b>Companies ({len(companies)}):</b>", ""]
    for c in companies:
        polled = "polled" if (c.active and c.alert_chat_id) else "NOT polled"
        chat = f"<code>{c.alert_chat_id}</code>" if c.alert_chat_id else "(unbound)"
        lines.append(
            f"<b>[{c.id}] {escape(c.name)}</b> — {'active' if c.active else 'inactive'}, {polled}\n"
            f"   GoMotive <code>{_mask(c.gomotive_token)}</code> · "
            f"Quantum <code>{_mask(c.quantum_token)}</code> · chat {chat}"
        )
    await message.answer("\n".join(lines))


# --- /activate, /deactivate --------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["deactivate"], state="*")
async def deactivate(message: types.Message):
    await _set_active(message, 0)


@dp.message_handler(IsAdmin(), commands=["activate"], state="*")
async def activate(message: types.Message):
    await _set_active(message, 1)


async def _set_active(message: types.Message, value: int):
    arg = message.get_args().strip()
    verb = "activate" if value else "deactivate"
    if not arg:
        await message.answer(f"Usage: <code>/{verb} &lt;company name or id&gt;</code>")
        return
    company = await _resolve(arg)
    if company is None:
        await message.answer(f"No company found for <b>{escape(arg)}</b>.")
        return
    await store.set_company_active(company.id, value)
    await message.answer(
        f"✅ {'Activated' if value else 'Deactivated'} <b>{escape(company.name)}</b>."
        + ("" if value else " It will no longer be polled (history kept).")
    )
