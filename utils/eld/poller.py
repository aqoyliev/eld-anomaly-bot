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
    # Units GoMotive reports moving but GreenLight has no record for (content:
    # null). Normally these are units outside our GreenLight account and are
    # safely ignored; log them so a real fleet vehicle that ever lands here
    # (i.e. a disconnection we'd otherwise miss) is visible.
    not_in_gl = [u for u, v in gl_lookup.items() if v is None]
    found_in_gl = len(moving) - len(not_in_gl)
    logger.info(
        "Poll: %d moving, %d found in GreenLight, %d anomalies",
        len(moving), found_in_gl, len(anomalies),
    )
    if not_in_gl:
        logger.info("Poll: %d moving units not in GreenLight (skipped): %s",
                    len(not_in_gl), not_in_gl)

    for anomaly in anomalies:
        gl, gm = anomaly.greenlight, anomaly.gomotive
        # GoMotive's current coordinates = where the truck actually is right now.
        location = gm.coordinates_label
        existing = await store.get_active_event(anomaly.unit_number)
        if existing:
            # Ongoing event — update readings, do NOT re-alert (de-dup).
            await store.touch_event(existing.id, speed=gm.speed, location=location,
                                    lat=gm.latitude, lon=gm.longitude)
        else:
            disconnect_time = (
                gl.last_report_time.isoformat() if gl.last_report_time else None
            )
            event = await store.open_event(
                unit_number=anomaly.unit_number,
                vin=gl.vin,
                driver=gl.driver,
                eld_disconnect_time=disconnect_time,
                speed=gm.speed,
                location=location,
                vehicle_id=str(gm.vehicle_id) if gm.vehicle_id is not None else None,
                lat=gm.latitude,
                lon=gm.longitude,
            )
            await _send_alert(bot, event)

    # NOTE: resolution is intentionally NOT done here. A disconnected truck that
    # simply stops would drop out of the moving set and be wrongly resolved. The
    # 2-min tracker owns resolution — it resolves only when the ELD reconnects.


# Re-log a still-failing identical error only once every N cycles, so a
# persistent condition (e.g. GreenLight token not yet authorized) doesn't spam
# the log every poll while staying visible enough not to be forgotten.
_REPEAT_REMINDER_EVERY = 12  # at 5-min polls, ~once per hour


async def run_poller(bot: Bot) -> None:
    await store.init_db()
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
