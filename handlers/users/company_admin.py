"""Admin-only company management, in-chat.

Commands (restricted to config.ADMINS via the IsAdmin filter):
    /addcompany   step-by-step wizard to create a company (tokens auto-deleted)
    /bindhere     link the current chat as a company's alert group (a chat may
                  host several companies)
    /unbindhere   unlink a company from the current chat
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
        f"Name: <b>{escape(name)}</b> ✅\n\nNow send the <b>GoMotive API token</b>, "
        "or <code>skip</code> if this company's trucks use Samsara instead.\n"
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


def _is_skip(message: types.Message) -> bool:
    return (message.text or "").strip().lower() in {"skip", "-"}


_SAMSARA_PROMPT = (
    "Now send the <b>Samsara API token</b>{or_skip}.\n"
    "<i>I'll delete that message too.</i>"
)


@dp.message_handler(state=AddCompany.gomotive_token)
async def add_gomotive(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    if _is_skip(message):
        await state.update_data(gomotive_token=None)
        await state.set_state(AddCompany.samsara_token)
        await message.answer(
            "GoMotive skipped.\n\n"
            + _SAMSARA_PROMPT.format(or_skip=" (required — GoMotive was skipped)")
        )
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty token — send the GoMotive token, "
                             "<code>skip</code>, or /cancel.")
        return
    await state.update_data(gomotive_token=token)
    await state.set_state(AddCompany.samsara_token)
    await message.answer(
        "GoMotive token received ✅\n\n"
        + _SAMSARA_PROMPT.format(
            or_skip=", or <code>skip</code> if this company has no Samsara devices"
        )
    )


_QUANTUM_PROMPT = (
    "Now send the <b>Quantum ELD API token</b>, or <code>skip</code> if this "
    "company's ELDs report to EVO instead.\n"
    "<i>I'll delete that message too.</i>"
)


@dp.message_handler(state=AddCompany.samsara_token)
async def add_samsara(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    if _is_skip(message):
        data = await state.get_data()
        if not data.get("gomotive_token"):
            # No movement provider at all — the company could never be polled.
            await message.answer(
                "GoMotive was skipped, so a <b>Samsara token is required</b> "
                "(a company needs at least one movement provider). Send the "
                "Samsara token, or /cancel."
            )
            return
        await state.update_data(samsara_token=None)
        await state.set_state(AddCompany.quantum_token)
        await message.answer("Samsara skipped.\n\n" + _QUANTUM_PROMPT)
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty token — send the Samsara token, "
                             "<code>skip</code>, or /cancel.")
        return
    await state.update_data(samsara_token=token)
    await state.set_state(AddCompany.quantum_token)
    await message.answer("Samsara token received ✅\n\n" + _QUANTUM_PROMPT)


@dp.message_handler(state=AddCompany.quantum_token)
async def add_quantum(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    if _is_skip(message):
        await state.update_data(quantum_token=None)
        await state.set_state(AddCompany.evo_api_key)
        await message.answer(
            "Quantum skipped.\n\nNow send the <b>EVO ELD api key</b> "
            "(required — Quantum was skipped; a company needs at least one "
            "ELD system).\n<i>I'll delete that message too.</i>"
        )
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty token — send the Quantum token, "
                             "<code>skip</code>, or /cancel.")
        return
    await state.update_data(quantum_token=token)
    await state.set_state(AddCompany.evo_api_key)
    await message.answer(
        "Quantum token received ✅\n\nNow send the <b>EVO ELD api key</b>, or "
        "<code>skip</code> if this company doesn't use EVO.\n"
        "<i>I'll delete that message too.</i>"
    )


@dp.message_handler(state=AddCompany.evo_api_key)
async def add_evo_api_key(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    if _is_skip(message):
        data = await state.get_data()
        if not data.get("quantum_token"):
            # No ELD system at all — the company could never be polled.
            await message.answer(
                "Quantum was skipped, so the <b>EVO credentials are required</b> "
                "(a company needs at least one ELD system). Send the EVO api "
                "key, or /cancel."
            )
            return
        await _create_company(message, state, evo_api_key=None,
                              evo_provider_token=None, evo_usdot=None)
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty value — send the EVO api key, "
                             "<code>skip</code>, or /cancel.")
        return
    await state.update_data(evo_api_key=token)
    await state.set_state(AddCompany.evo_provider_token)
    await message.answer(
        "EVO api key received ✅\n\nNow send the <b>EVO provider token</b>.\n"
        "<i>I'll delete that message too.</i>"
    )


@dp.message_handler(state=AddCompany.evo_provider_token)
async def add_evo_provider_token(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    token = await _consume_secret(message)
    if not token:
        await message.answer("Empty value — send the EVO provider token, or /cancel.")
        return
    await state.update_data(evo_provider_token=token)
    await state.set_state(AddCompany.evo_usdot)
    await message.answer(
        "EVO provider token received ✅\n\nFinally, send the company's "
        "<b>USDOT number</b> (digits only — it goes in the EVO API URL, "
        "so it isn't a secret)."
    )


@dp.message_handler(state=AddCompany.evo_usdot)
async def add_evo_usdot(message: types.Message, state: FSMContext):
    if await _intercept_command(message):
        return
    usdot = (message.text or "").strip()
    if not usdot.isdigit():
        await message.answer("A USDOT number is digits only — try again, or /cancel.")
        return
    data = await state.get_data()
    await _create_company(message, state, evo_api_key=data["evo_api_key"],
                          evo_provider_token=data["evo_provider_token"],
                          evo_usdot=usdot)


async def _create_company(message: types.Message, state: FSMContext, *,
                          evo_api_key, evo_provider_token, evo_usdot):
    data = await state.get_data()
    await state.finish()

    company = await store.add_company(
        name=data["name"],
        gomotive_token=data["gomotive_token"],
        samsara_token=data.get("samsara_token"),
        quantum_token=data.get("quantum_token"),
        evo_api_key=evo_api_key,
        evo_provider_token=evo_provider_token,
        evo_usdot=evo_usdot,
    )
    evo_label = (
        f"{_mask(company.evo_api_key)} (USDOT {escape(company.evo_usdot)})"
        if company.evo_configured else "(none)"
    )
    await message.answer(
        f"✅ Created company <b>{escape(company.name)}</b> (id {company.id}).\n"
        f"  GoMotive: <code>{_mask(company.gomotive_token)}</code>\n"
        f"  Samsara: <code>{_mask(company.samsara_token)}</code>\n"
        f"  Quantum: <code>{_mask(company.quantum_token)}</code>\n"
        f"  EVO: <code>{evo_label}</code>\n\n"
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

    bound = await store.get_companies_by_chat(message.chat.id)
    if any(c.id == company.id for c in bound):
        await message.answer(
            f"<b>{escape(company.name)}</b> is already linked to this chat."
        )
        return

    await store.bind_company_chat(company.id, message.chat.id)
    state_note = "" if company.active else " (note: it's deactivated — /activate it to poll)"
    shared_note = ""
    if bound:
        names = ", ".join(f"<b>{escape(c.name)}</b>" for c in bound)
        shared_note = f"\nThis chat also receives alerts for {names}."
    await message.answer(
        f"✅ Linked this chat (<code>{message.chat.id}</code>) to <b>{escape(company.name)}</b>. "
        f"Alerts will arrive here from the next poll cycle.{state_note}{shared_note}"
    )


# --- /unbindhere ---------------------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["unbindhere"], state="*")
async def unbind_here(message: types.Message):
    bound = await store.get_companies_by_chat(message.chat.id)
    if not bound:
        await message.answer("No company is linked to this chat.")
        return

    arg = message.get_args().strip()
    if arg:
        company = await _resolve(arg)
        if company is None or all(c.id != company.id for c in bound):
            names = ", ".join(escape(c.name) for c in bound)
            await message.answer(
                f"<b>{escape(arg)}</b> isn't linked to this chat. Linked here: {names}."
            )
            return
    elif len(bound) == 1:
        company = bound[0]
    else:
        names = ", ".join(escape(c.name) for c in bound)
        await message.answer(
            "Several companies are linked to this chat — say which one:\n"
            f"<code>/unbindhere &lt;company name or id&gt;</code>\n\nLinked here: {names}."
        )
        return

    await store.bind_company_chat(company.id, None)
    await message.answer(
        f"✅ Unlinked <b>{escape(company.name)}</b> from this chat. It now has no "
        "alert chat and won't be polled until you /bindhere it somewhere."
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
        # USDOT is public (it's in the EVO URL), so it identifies the EVO
        # setup better than another masked token would.
        evo = f"USDOT {escape(c.evo_usdot)}" if c.evo_configured else "(none)"
        lines.append(
            f"<b>[{c.id}] {escape(c.name)}</b> — {'active' if c.active else 'inactive'}, {polled}\n"
            f"   GoMotive <code>{_mask(c.gomotive_token)}</code> · "
            f"Samsara <code>{_mask(c.samsara_token)}</code> · "
            f"Quantum <code>{_mask(c.quantum_token)}</code> · "
            f"EVO <code>{evo}</code> · chat {chat}"
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
