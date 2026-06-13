"""Fine-grained tracker for already-flagged (disconnected) units.

Runs on a faster cadence than the 5-min sweep (default every 2 min). An anomaly
counts only moving-while-disconnected time, so for each active anomaly it:
  1. checks Quantum — if the ELD reports fresh again, the device reconnected,
     so the anomaly is resolved (all-clear);
  2. re-queries the unit's movement provider (GoMotive or Samsara, whichever
     flagged it) by vehicle id (cheap, just the flagged units) and, by
     comparing the position to the previous check, decides whether the truck has
     stopped/pulled over — if so it announces the stop once and PAUSES the event
     (marks ``stopped_at``), because a stop ends the moving anomaly. The event is
     left open, not resolved, so a reconnect-while-parked still raises the
     all-clear; the poller reopens a fresh anomaly if the unit rolls again;
  3. while STILL MOVING and disconnected, re-notifies the group every
     REMINDER_INTERVAL with an "Ignore" button that mutes further reminders.

The reconnect all-clear lives here (not in the 5-min poller). A paused (stopped)
unit is otherwise idle here — only its reconnect is watched.
"""

import asyncio
import logging
import math
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data import config
from . import gomotive, quantumeld, samsara, store
from .formatting import format_reconnected, format_reminder, format_stopped

logger = logging.getLogger(__name__)


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _reading_is_fresh(vehicle, provider: str) -> bool:
    """Whether a re-queried vehicle's reading is recent enough to still count as
    live movement. Dispatches to the provider's own freshness check/threshold so
    a powered-off gateway frozen at speed isn't treated as a moving truck."""
    if provider == "samsara":
        return samsara._is_fresh(vehicle, config.SAMSARA_GPS_FRESHNESS)
    return gomotive._is_fresh(vehicle, config.GOMOTIVE_GPS_FRESHNESS)


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

    quantum_base_url = config.QUANTUM_BASE_URL

    # Targeted movement re-query for just the flagged units + Quantum freshness.
    # Each event carries the provider that flagged it (NULL = gomotive on
    # legacy rows), so the ids are split and re-queried per provider.
    gm_ids = [e.motive_vehicle_id for e in events
              if e.motive_vehicle_id and (e.provider or "gomotive") == "gomotive"]
    ss_ids = [e.motive_vehicle_id for e in events
              if e.motive_vehicle_id and e.provider == "samsara"]
    gm_map = (
        await gomotive.fetch_by_ids(
            gm_ids, company.gomotive_token, config.GOMOTIVE_BASE_URL
        )
        if gm_ids else {}
    )
    ss_map = (
        await samsara.fetch_by_ids(
            ss_ids, company.samsara_token, config.SAMSARA_BASE_URL
        )
        if ss_ids else {}
    )
    quantum_map = await quantumeld.fetch_vehicles(
        [e.unit_number for e in events], company.quantum_token, quantum_base_url
    )
    now = datetime.utcnow()

    for e in events:
        # 1) Reconnected? A fresh Quantum report means the ELD is back online.
        q = quantum_map.get(e.unit_number)
        if q is not None and not quantumeld.is_disconnected(
            q, threshold_seconds=config.ELD_STALE_THRESHOLD, now=now
        ):
            await store.resolve_event(e.id)
            # All-clear goes out even if reminders were muted.
            await _notify(bot, company, format_reconnected(e, company_name=company.name))
            logger.info("Tracker[%s]: %s ELD reconnected — resolved.",
                        company.name, e.unit_number)
            continue

        # A paused (stopped) unit's anomaly is over for now: no stop re-detection,
        # no readings, no reminders. It stays open only so the reconnect check
        # above still raises the all-clear; the poller reopens a fresh anomaly if
        # it rolls again while still disconnected.
        if e.stopped_at:
            continue

        vmap = ss_map if e.provider == "samsara" else gm_map
        gm = vmap.get(e.motive_vehicle_id) if e.motive_vehicle_id else None

        # 1b) Device went dark? The movement APIs return the last-known reading
        #     even after the gateway is powered off, frozen at its final speed
        #     and position. If that final speed was highway speed the stop check
        #     below never trips (speed stays high while position is frozen), so
        #     the event would re-alert forever. Treat a stale reading as a stop:
        #     the truck is no longer CONFIRMABLY moving, which ends the anomaly.
        if gm is not None and not _reading_is_fresh(gm, e.provider):
            await store.touch_event(
                e.id, speed=gm.speed, location=gm.coordinates_label,
                lat=gm.latitude, lon=gm.longitude,
            )
            await _notify(
                bot, company,
                format_stopped(e, gm.coordinates_label, company_name=company.name),
            )
            await store.mark_stopped(e.id)
            logger.info("Tracker[%s]: %s reading went stale (device dark) — "
                        "anomaly paused.", company.name, e.unit_number)
            continue

        # 2) Stopped? An anomaly counts moving-while-disconnected time only, so a
        #    stop ENDS it: announce the stop once and PAUSE the event (freeze its
        #    duration at the stop). It is NOT resolved — that keeps the reconnect
        #    all-clear alive — and the poller reopens a fresh anomaly on re-roll.
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
            if moved_mi < config.STOP_DISPLACEMENT_MI and slow:
                # Record the final reading so the STOPPED coords/speed are current.
                await store.touch_event(
                    e.id, speed=gm.speed, location=gm.coordinates_label,
                    lat=gm.latitude, lon=gm.longitude,
                )
                await _notify(
                    bot, company,
                    format_stopped(e, gm.coordinates_label, company_name=company.name),
                )
                await store.mark_stopped(e.id)
                logger.info("Tracker[%s]: %s STOPPED — anomaly paused (moved %.3f mi).",
                            company.name, e.unit_number, moved_mi)
                continue

        # 3) Still moving + disconnected: record the latest reading for next
        #    cycle's stop comparison.
        if gm is not None:
            await store.touch_event(
                e.id, speed=gm.speed, location=gm.coordinates_label,
                lat=gm.latitude, lon=gm.longitude,
            )

        # 4) Periodic reminder while STILL MOVING and disconnected (a stopped unit
        #    has already been closed above), unless muted via the Ignore button.
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
