"""Admin-only per-unit mute list.

Commands (restricted to config.ADMINS via the IsAdmin filter):
    /ignoreunit    stop flagging one unit (default 30 days)
    /unignoreunit  start flagging it again
    /ignorelist    what is currently ignored

Why a mute list exists at all: a carrier sometimes pulls a truck's Motive
device and runs it in a different truck. The movement provider then keeps
reporting the OLD unit number as moving while that truck's own ELD is
legitimately offline, which is a permanent false anomaly — nothing on the ELD
side can fix it and the VIN guard can't catch it (both feeds still carry the
parked truck's VIN). Muting is the honest answer until the carrier reassigns
the device in Motive, so a mute is time-limited by default: a forgotten one is
an unmonitored truck.
"""

from html import escape

from aiogram import types

from filters.is_admin import IsAdmin
from loader import dp
from utils.eld import store
from utils.eld.formatting import format_time, human_duration

_DEFAULT_DAYS = 30
_MAX_DAYS = 365

_USAGE = (
    "Usage: <code>/ignoreunit &lt;company name or id&gt; &lt;unit&gt; [days]</code>\n"
    f"Days defaults to {_DEFAULT_DAYS} (max {_MAX_DAYS}); <code>0</code> means "
    "until you /unignoreunit it.\n"
    "In a company's own group the name can be left out: "
    "<code>/ignoreunit 796801 30</code>.\n\n"
    "Example: <code>/ignoreunit MZ CARGO 796801 30</code>"
)


async def _resolve(arg: str):
    """Resolve a company by numeric id or by name. Case-insensitive on the name:
    these commands get typed on a phone while a truck is rolling, and the name
    is only a lookup key here (nothing is created under it)."""
    arg = arg.strip()
    if arg.isdigit():
        return await store.get_company(int(arg))
    company = await store.get_company_by_name(arg)
    if company is not None:
        return company
    wanted = arg.casefold()
    for candidate in await store.list_companies(active_only=False):
        if candidate.name.casefold() == wanted:
            return candidate
    return None


async def _split_company(args: str, message: types.Message):
    """Split "<company> <rest>" into (company, rest).

    Company names contain spaces ("MZ CARGO"), so the name is the LONGEST run of
    leading tokens that resolves to a company. When no prefix resolves, the
    company bound to this chat is used, which lets a dispatch group mute a unit
    without naming itself. Returns (None, args) when neither works."""
    tokens = args.split()
    # Leave at least one token for the unit number.
    for take in range(len(tokens) - 1, 0, -1):
        company = await _resolve(" ".join(tokens[:take]))
        if company is not None:
            return company, " ".join(tokens[take:])
    bound = await store.get_companies_by_chat(message.chat.id)
    if len(bound) == 1:
        return bound[0], args
    return None, args


def _split_days(rest: str):
    """Split "<unit> [days]". The trailing number is the window only when
    something precedes it, so "/ignoreunit 30" still means unit 30."""
    tokens = rest.split()
    if len(tokens) > 1 and tokens[-1].isdigit():
        return " ".join(tokens[:-1]), int(tokens[-1])
    return rest.strip(), _DEFAULT_DAYS


async def _ambiguous(message: types.Message) -> None:
    bound = await store.get_companies_by_chat(message.chat.id)
    hint = ""
    if len(bound) > 1:
        names = ", ".join(escape(c.name) for c in bound)
        hint = f"\nThis chat hosts several companies: {names}."
    await message.answer(
        f"I couldn't tell which company you mean.{hint}\n\n{_USAGE}"
    )


async def _close_open_events(company_id: int, unit_key: str) -> int:
    """Silently close the muted unit's open events — no all-clear, because
    nothing reconnected: we just stopped watching. A paused span is closed at
    its stop time so its recorded duration stays honest."""
    closed = 0
    for event in await store.get_active_events(company_id):
        if store.ignore_key(event.unit_number) == unit_key:
            await store.resolve_event(event.id, at=event.stopped_at)
            closed += 1
    return closed


# --- /ignoreunit -------------------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["ignoreunit"], state="*")
async def ignore_unit(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.answer(_USAGE)
        return

    company, rest = await _split_company(args, message)
    if company is None:
        await _ambiguous(message)
        return

    unit, days = _split_days(rest)
    if not unit:
        await message.answer(_USAGE)
        return
    if days and not 1 <= days <= _MAX_DAYS:
        await message.answer(
            f"Days must be 1–{_MAX_DAYS}, or <code>0</code> to ignore the unit "
            "until you /unignoreunit it."
        )
        return

    entry = await store.add_unit_ignore(
        company.id, unit, days=days or None,
        created_by=str(message.from_user.id),
    )
    closed = await _close_open_events(company.id, entry.unit_key)

    plural = "s" if days != 1 else ""
    window = (
        f"until <b>{format_time(entry.until)}</b> ({days} day{plural})"
        if entry.until else "until you remove it (<b>no expiry</b>)"
    )
    closed_note = (
        f"\nClosed {closed} open event{'s' if closed != 1 else ''} for it "
        "(no all-clear was sent)."
        if closed else ""
    )
    await message.answer(
        f"🚫 Ignoring unit <b>{escape(entry.label or entry.unit_key)}</b> for "
        f"<b>{escape(company.name)}</b> {window}.\n"
        f"No alerts or reminders for it from the next poll cycle.{closed_note}\n\n"
        f"Undo: <code>/unignoreunit {escape(company.name)} "
        f"{escape(entry.unit_key)}</code>"
    )


# --- /unignoreunit -----------------------------------------------------------

@dp.message_handler(IsAdmin(), commands=["unignoreunit"], state="*")
async def unignore_unit(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.answer(
            "Usage: <code>/unignoreunit &lt;company name or id&gt; &lt;unit&gt;</code>\n"
            "See /ignorelist for what's currently ignored."
        )
        return

    company, rest = await _split_company(args, message)
    if company is None:
        await _ambiguous(message)
        return
    unit = rest.strip()
    if not unit:
        await message.answer(
            "Usage: <code>/unignoreunit &lt;company name or id&gt; &lt;unit&gt;</code>"
        )
        return

    entry = await store.remove_unit_ignore(company.id, unit)
    if entry is None:
        await message.answer(
            f"Unit <b>{escape(unit)}</b> isn't on <b>{escape(company.name)}</b>'s "
            "ignore list. See /ignorelist."
        )
        return
    await message.answer(
        f"✅ Watching unit <b>{escape(entry.label or entry.unit_key)}</b> again for "
        f"<b>{escape(company.name)}</b>. It can be flagged from the next poll cycle."
    )


# --- /ignorelist -------------------------------------------------------------

def _entry_line(entry: store.UnitIgnore) -> str:
    left = entry.expires_in_seconds
    window = (
        f"expires {format_time(entry.until)} (in {human_duration(left)})"
        if left is not None else "no expiry"
    )
    return f"• <b>{escape(entry.label or entry.unit_key)}</b> — {window}"


@dp.message_handler(IsAdmin(), commands=["ignorelist"], state="*")
async def ignore_list(message: types.Message):
    bound = await store.get_companies_by_chat(message.chat.id)
    # An admin DM (nothing bound here) shows every company's list.
    companies = bound or await store.list_companies(active_only=False)

    blocks = []
    for company in companies:
        entries = await store.list_unit_ignores(company.id)
        if not entries:
            continue
        lines = [f"<b>{escape(company.name)} — ignored units ({len(entries)}):</b>"]
        lines += [_entry_line(e) for e in entries]
        blocks.append("\n".join(lines))

    if not blocks:
        await message.answer(
            "No units are being ignored. Mute one with "
            "<code>/ignoreunit &lt;company&gt; &lt;unit&gt; [days]</code>."
        )
        return
    await message.answer("\n\n".join(blocks))
