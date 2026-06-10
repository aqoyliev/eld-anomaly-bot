"""Background loop: poll both APIs, detect anomalies, alert, and track state."""

import asyncio
import logging

from aiogram import Bot

from data import config
from . import gomotive, quantumeld, store
from .detector import find_anomalies
from .formatting import format_alert

logger = logging.getLogger(__name__)


async def _send_alert(
    bot: Bot, company: store.Company, event: store.AnomalyEvent
) -> None:
    if not company.alert_chat_id:
        logger.warning("Company %s has no alert chat; cannot send alert for %s",
                       company.name, event.unit_number)
        return
    try:
        await bot.send_message(
            company.alert_chat_id, format_alert(event, company_name=company.name)
        )
    except Exception:
        logger.exception("Failed to send alert for vehicle %s", event.unit_number)


async def poll_once(bot: Bot, company: store.Company) -> None:
    if not company.quantum_token:
        logger.warning("Company %s has no Quantum token; skipping poll cycle.",
                       company.name)
        return

    quantum_base_url = company.quantum_base_url or config.QUANTUM_BASE_URL

    # 1. Ask GoMotive which vehicles are moving right now. (GoMotive's base URL is
    #    a constant, so it stays in config; only the token is per-company.)
    moving = await gomotive.fetch_moving_vehicles(
        company.gomotive_token,
        config.GOMOTIVE_BASE_URL,
        threshold_mph=config.MOVING_SPEED_THRESHOLD,
    )
    if not moving:
        logger.info("Poll[%s]: no moving vehicles on GoMotive.", company.name)
        return

    # 2. Look those vehicles up in Quantum by unit number to read their last
    #    report time (a stale report => ELD disconnected/offline).
    quantum_lookup = await quantumeld.fetch_vehicles(
        list(moving.keys()), company.quantum_token, quantum_base_url
    )

    anomalies = find_anomalies(
        moving, quantum_lookup, threshold_seconds=config.ELD_STALE_THRESHOLD
    )
    # Units GoMotive reports moving but Quantum has no record for (content:
    # null). Normally these are units outside our Quantum account and are
    # safely ignored; log them so a real fleet vehicle that ever lands here
    # (i.e. a disconnection we'd otherwise miss) is visible.
    not_in_quantum = [u for u, v in quantum_lookup.items() if v is None]
    found_in_quantum = len(moving) - len(not_in_quantum)
    logger.info(
        "Poll[%s]: %d moving, %d found in Quantum, %d anomalies",
        company.name, len(moving), found_in_quantum, len(anomalies),
    )
    if not_in_quantum:
        logger.info("Poll[%s]: %d moving units not in Quantum (skipped): %s",
                    company.name, len(not_in_quantum), not_in_quantum)

    for anomaly in anomalies:
        q, gm = anomaly.quantum, anomaly.gomotive
        # GoMotive's current coordinates = where the truck actually is right now.
        location = gm.coordinates_label
        existing = await store.get_active_event(company.id, anomaly.unit_number)
        if existing and existing.stopped_at is None:
            # Ongoing moving event — update readings, do NOT re-alert (de-dup).
            await store.touch_event(existing.id, speed=gm.speed, location=location,
                                    lat=gm.latitude, lon=gm.longitude)
            continue
        if existing:
            # The unit had stopped (anomaly paused) but is moving again: close
            # that span at the stop time and open a fresh anomaly below — a new
            # moving span is a new anomaly.
            await store.resolve_event(existing.id, at=existing.stopped_at)
            logger.info("Poll[%s]: %s rolling again — closed paused span, new anomaly.",
                        company.name, anomaly.unit_number)
        disconnect_time = (
            q.last_report_time.isoformat() if q.last_report_time else None
        )
        event = await store.open_event(
            company_id=company.id,
            unit_number=anomaly.unit_number,
            vin=q.vin,
            driver=q.driver,
            eld_disconnect_time=disconnect_time,
            speed=gm.speed,
            location=location,
            motive_vehicle_id=str(gm.vehicle_id) if gm.vehicle_id is not None else None,
            lat=gm.latitude,
            lon=gm.longitude,
        )
        await _send_alert(bot, company, event)

    # NOTE: the reconnect all-clear is NOT done here — the 2-min tracker owns it
    # (and pauses an event when the truck stops). This loop opens events: a brand
    # new anomaly, or a fresh one when a previously-stopped (paused) unit rolls
    # again — it closes the paused span at its stop time, then opens the new one.
    # A new moving span = a new anomaly.


# Re-log a still-failing identical error only once every N cycles, so a
# persistent condition (e.g. Quantum token not yet authorized) doesn't spam
# the log every poll while staying visible enough not to be forgotten.
_REPEAT_REMINDER_EVERY = 12  # at 5-min polls, ~once per hour


async def run_poller(bot: Bot) -> None:
    await store.init_db()
    logger.info("ELD anomaly poller started (interval=%ds).", config.POLL_INTERVAL)
    # Repeat-error suppression is kept per company so one fleet's persistent API
    # failure doesn't mask or reset another's.
    last_error: dict[int, str] = {}
    repeat_count: dict[int, int] = {}
    while True:
        for company in await store.active_companies():
            try:
                await poll_once(bot, company)
                if company.id in last_error:
                    logger.info(
                        "ELD poll[%s] recovered after %d failed cycle(s).",
                        company.name, repeat_count.get(company.id, 0) + 1,
                    )
                    last_error.pop(company.id, None)
                    repeat_count.pop(company.id, None)
            except Exception as err:
                signature = f"{type(err).__name__}: {err}"
                if signature != last_error.get(company.id):
                    # New or changed error — log once with full traceback.
                    logger.exception("Error during ELD poll cycle for %s",
                                     company.name)
                    last_error[company.id], repeat_count[company.id] = signature, 0
                else:
                    # Same error as last cycle — suppress, occasional reminder.
                    repeat_count[company.id] += 1
                    if repeat_count[company.id] % _REPEAT_REMINDER_EVERY == 0:
                        logger.warning(
                            "ELD poll[%s] still failing (%d consecutive cycles): %s",
                            company.name, repeat_count[company.id] + 1, signature,
                        )
        await asyncio.sleep(config.POLL_INTERVAL)
