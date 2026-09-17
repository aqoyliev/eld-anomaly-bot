from aiogram.dispatcher.filters.state import State, StatesGroup


class AddCompany(StatesGroup):
    """Step-by-step wizard for /addcompany. Token steps auto-delete the message
    that carried the secret (where Telegram allows it). The two movement-provider
    steps (GoMotive, Samsara) are individually skippable — a company needs at
    least one of them. The ELD side works the same way: Quantum, EVO and Vitality
    are each skippable, but at least one ELD system is required (the Vitality
    step becomes mandatory when the other two were skipped). EVO needs three values (api key, provider
    token, USDOT number), hence its three states; Vitality needs one (the
    carrier's company key)."""
    name = State()
    gomotive_token = State()
    samsara_token = State()
    quantum_token = State()
    evo_api_key = State()
    evo_provider_token = State()
    evo_usdot = State()
    vitality_company_key = State()


class SetEld(StatesGroup):
    """/seteld: waits for the new credential (auto-deleted) or "remove"."""
    value = State()
