from aiogram.dispatcher.filters.state import State, StatesGroup


class AddCompany(StatesGroup):
    """Step-by-step wizard for /addcompany. Token steps auto-delete the message
    that carried the secret (where Telegram allows it)."""
    name = State()
    gomotive_token = State()
    quantum_token = State()
