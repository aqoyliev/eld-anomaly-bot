"""Fine-grained tracker for already-flagged (disconnected) units.

Runs on a faster cadence than the 5-min sweep (default every 2 min). For each
active anomaly it:
  1. checks GreenLight — if the ELD reports fresh again, the device reconnected,
     so the anomaly is resolved;
  2. re-queries GoMotive by vehicle id (cheap, just the flagged units) and, by
     comparing the position to the previous check, decides whether the truck has
     stopped/pulled over — notifying the group once per stop;
  3. re-notifies the group every REMINDER_INTERVAL while still disconnected,
     attaching an "Ignore" button that mutes further reminders for that unit.

Resolution lives here (not in the 5-min poller) so a disconnected truck that
merely stops isn't wrongly resolved.
"""

import asyncio
import logging
import math
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data import config
from . import gomotive, greenlight, store
from .formatting import format_reconnected, format_reminder, format_stopped

logger = logging.getLogger(__name__)


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ignore_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton(
            "🔕 Mute reminders", callback_data=f"eld_ignore:{event_id}"
        )
    )


async def _notify(
    bot: Bot, company: store.Company, text: str, reply_markup=None
) -> None:
    if not company.alert_chat_id:
        logger.warning("Company %s has no alert chat; cannot send tracker "
                       "notification.", company.name)
        return
    try:
        await bot.send_message(
            company.alert_chat_id, text, reply_markup=reply_markup
        )
    except Exception:
        logger.exception("Failed to send tracker notification")


async def track_once(bot: Bot, company: store.Company) -> None:
    events = await store.get_active_events(company.id)
    if not events:
        return

    gl_base_url = company.greenlight_base_url or config.GREENLIGHT_BASE_URL

    # Targeted GoMotive movement for just the flagged units + GreenLight freshness.
    ids = [e.motive_vehicle_id for e in events if e.motive_vehicle_id]
    gm_map = (
        await gomotive.fetch_by_ids(
            ids, company.gomotive_token, config.GOMOTIVE_BASE_URL
        )
        if ids else {}
    )
    gl_map = await greenlight.fetch_vehicles(
        [e.unit_number for e in events], company.greenlight_token, gl_base_url
    )
    now = datetime.utcnow()

    for e in events:
        # 1) Reconnected? A fresh GreenLight report means the ELD is back online.
        gl = gl_map.get(e.unit_number)
        if gl is not None and not greenlight.is_disconnected(
            gl, threshold_seconds=config.ELD_STALE_THRESHOLD, now=now
        ):
            await store.resolve_event(e.id)
            # All-clear goes out even if reminders were muted / the unit stopped.
            await _notify(bot, company, format_reconnected(e, company_name=company.name))
            logger.info("Tracker[%s]: %s ELD reconnected — resolved.",
                        company.name, e.unit_number)
            continue

        gm = gm_map.get(e.motive_vehicle_id) if e.motive_vehicle_id else None

        # 2) Stop detection: compare this position to the previous check's.
        if (
            gm is not None
            and gm.latitude is not None
            and gm.longitude is not None
            and e.last_lat is not None
            and e.last_lon is not None
        ):
            moved_mi = _haversine_mi(e.last_lat, e.last_lon, gm.latitude, gm.longitude)
            # Motive reports speed: null for a parked vehicle, so null counts as
            # stopped (the displacement check guards against a null-but-moving read).
            slow = gm.speed is None or gm.speed <= config.STOP_SPEED_MPH
            stopped = moved_mi < config.STOP_DISPLACEMENT_MI and slow
            if stopped and not e.stop_notified:
                await _notify(
                    bot, company,
                    format_stopped(e, gm.coordinates_label, company_name=company.name),
                )
                await store.set_stop_notified(e.id, 1)
                logger.info("Tracker[%s]: %s STOPPED (moved %.3f mi).",
                            company.name, e.unit_number, moved_mi)
            elif not stopped and e.stop_notified:
                await store.set_stop_notified(e.id, 0)  # moving again

        # 3) Record the latest reading for next cycle's comparison.
        if gm is not None:
            await store.touch_event(
                e.id, speed=gm.speed, location=gm.coordinates_label,
                lat=gm.latitude, lon=gm.longitude,
            )

        # 4) Periodic "still disconnected" reminder, unless muted via Ignore.
        if not e.reminders_muted and e.last_alert_at:
            since = (now - datetime.fromisoformat(e.last_alert_at)).total_seconds()
            if since >= config.REMINDER_INTERVAL:
                latest = await store.get_active_event(company.id, e.unit_number) or e
                await _notify(
                    bot, company,
                    format_reminder(latest, company_name=company.name),
                    reply_markup=ignore_keyboard(e.id),
                )
                await store.mark_alerted(e.id)


async def run_tracker(bot: Bot) -> None:
    logger.info("ELD tracker started (interval=%ds).", config.TRACK_INTERVAL)
    while True:
        for company in await store.active_companies():
            try:
                await track_once(bot, company)
            except Exception:
                logger.exception("Error during ELD tracker cycle for %s",
                                 company.name)
        await asyncio.sleep(config.TRACK_INTERVAL)
