"""Background loop: poll both APIs, detect anomalies, alert, and track state."""

import asyncio
import logging

from aiogram import Bot

from data import config
from . import gomotive, greenlight, store
from .detector import find_anomalies
from .formatting import format_alert

logger = logging.getLogger(__name__)


async def _send_alert(bot: Bot, event: store.AnomalyEvent) -> None:
    if not config.ALERT_CHAT_ID:
        logger.warning("ALERT_CHAT_ID not set; cannot send alert for %s", event.unit_number)
        return
    try:
        await bot.send_message(config.ALERT_CHAT_ID, format_alert(event))
    except Exception:
        logger.exception("Failed to send alert for vehicle %s", event.unit_number)


async def poll_once(bot: Bot) -> None:
    if not config.GREENLIGHT_TOKEN:
        logger.warning("GREENLIGHT_TOKEN not set; skipping poll cycle.")
        return

    # 1. Ask GoMotive which vehicles are moving right now.
    moving = await gomotive.fetch_moving_vehicles(
        threshold_mph=config.MOVING_SPEED_THRESHOLD
    )
    if not moving:
        logger.info("Poll: no moving vehicles on GoMotive.")
        return

    # 2. Look those vehicles up in GreenLight by unit number to read their last
    #    report time (a stale report => ELD disconnected/offline).
    gl_lookup = await greenlight.fetch_vehicles(list(moving.keys()))

    anomalies = find_anomalies(
        moving, gl_lookup, threshold_seconds=config.ELD_STALE_THRESHOLD
    )
    found_in_gl = sum(1 for v in gl_lookup.values() if v is not None)
    logger.info(
        "Poll: %d moving, %d found in GreenLight, %d anomalies",
        len(moving), found_in_gl, len(anomalies),
    )

    flagged_now = set()
    for anomaly in anomalies:
        flagged_now.add(anomaly.unit_number)
        gl, gm = anomaly.greenlight, anomaly.gomotive
        location = gm.location or gl.location_label
        existing = store.get_active_event(anomaly.unit_number)
        if existing:
            # Ongoing event — update readings, do NOT re-alert (de-dup).
            store.touch_event(existing.id, speed=gm.speed, location=location)
        else:
            disconnect_time = (
                gl.last_report_time.isoformat() if gl.last_report_time else None
            )
            event = store.open_event(
                unit_number=anomaly.unit_number,
                vin=gl.vin,
                driver=gl.driver,
                eld_disconnect_time=disconnect_time,
                speed=gm.speed,
                location=location,
            )
            await _send_alert(bot, event)

    # Auto-resolve events that are no longer anomalous.
    for active in store.get_active_events():
        if active.unit_number not in flagged_now:
            store.resolve_event(active.id)
            logger.info("Resolved anomaly for vehicle %s", active.unit_number)


# Re-log a still-failing identical error only once every N cycles, so a
# persistent condition (e.g. GreenLight token not yet authorized) doesn't spam
# the log every poll while staying visible enough not to be forgotten.
_REPEAT_REMINDER_EVERY = 12  # at 5-min polls, ~once per hour


async def run_poller(bot: Bot) -> None:
    store.init_db()
    logger.info("ELD anomaly poller started (interval=%ds).", config.POLL_INTERVAL)
    last_error: str | None = None
    repeat_count = 0
    while True:
        try:
            await poll_once(bot)
            if last_error is not None:
                logger.info("ELD poll cycle recovered after %d failed cycle(s).",
                            repeat_count + 1)
                last_error, repeat_count = None, 0
        except Exception as err:
            signature = f"{type(err).__name__}: {err}"
            if signature != last_error:
                # New or changed error — log once with full traceback.
                logger.exception("Error during ELD poll cycle")
                last_error, repeat_count = signature, 0
            else:
                # Same error as last cycle — suppress, with an occasional reminder.
                repeat_count += 1
                if repeat_count % _REPEAT_REMINDER_EVERY == 0:
                    logger.warning(
                        "ELD poll still failing (%d consecutive cycles): %s",
                        repeat_count + 1, signature,
                    )
        await asyncio.sleep(config.POLL_INTERVAL)
