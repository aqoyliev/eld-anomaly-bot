from aiogram.dispatcher.filters.state import State, StatesGroup


class AddCompany(StatesGroup):
    """Step-by-step wizard for /addcompany. Token steps auto-delete the message
    that carried the secret (where Telegram allows it). The two movement-provider
    steps (GoMotive, Samsara) are individually skippable — a company needs at
    least one of them. The ELD side works the same way: Quantum and EVO are each
    skippable, but at least one ELD system is required (skipping Quantum makes
    the EVO steps mandatory). EVO needs three values (api key, provider token,
    USDOT number), hence its three states."""
    name = State()
    gomotive_token = State()
    samsara_token = State()
    quantum_token = State()
    evo_api_key = State()
    evo_provider_token = State()
    evo_usdot = State()
