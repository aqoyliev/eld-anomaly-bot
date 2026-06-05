"""Human-readable rendering of anomaly events for Telegram (HTML parse mode)."""

from datetime import datetime, timezone
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from .store import AnomalyEvent

# All stored/computed times are naive UTC; alerts display them in US Eastern
# (handles EST/EDT automatically via the IANA tz database).
_EASTERN = ZoneInfo("America/New_York")


def human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _fmt_time(value: Optional[str]) -> str:
    if not value:
        return "unknown"
    try:
        dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return dt.astimezone(_EASTERN).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except ValueError:
        return value


def _fmt_speed(speed: Optional[float]) -> str:
    return f"{speed:.0f} mph" if speed is not None else "unknown"


def _driver_suffix(driver: Optional[str]) -> str:
    return f" — {escape(driver)}" if driver else ""


def format_alert(event: AnomalyEvent) -> str:
    """The 🚨 alert sent when a new anomaly is detected."""
    return (
        "🚨 <b>ELD DISCONNECTION ANOMALY</b>\n\n"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Current coordinates:</b> <code>{escape(event.last_location or 'unknown')}</code>\n"
        f"<b>Current speed:</b> {_fmt_speed(event.last_speed)}\n"
        f"<b>ELD disconnected at:</b> {_fmt_time(event.eld_disconnect_time)}\n"
        f"<b>Anomaly duration:</b> {human_duration(event.duration_seconds())}\n\n"
        "<i>Disconnected on GreenLight ELD but still moving on GoMotive.</i>"
    )


def format_stopped(event: AnomalyEvent, coords: str) -> str:
    """Sent once when a disconnected unit pulls over / stops moving."""
    return (
        "⏸ <b>DISCONNECTED UNIT STOPPED</b>\n\n"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Stopped at:</b> <code>{escape(coords)}</code>\n"
        f"<b>Disconnected for:</b> {human_duration(event.duration_seconds())}\n\n"
        "<i>No movement on GoMotive since the last check, ELD still disconnected.</i>"
    )


def format_reminder(event: AnomalyEvent) -> str:
    """Periodic reminder that a unit is still disconnected."""
    return (
        "🚨 <b>STILL DISCONNECTED</b>\n\n"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Current coordinates:</b> <code>{escape(event.last_location or 'unknown')}</code>\n"
        f"<b>Current speed:</b> {_fmt_speed(event.last_speed)}\n"
        f"<b>Anomaly duration:</b> {human_duration(event.duration_seconds())}"
    )


def format_reconnected(event: AnomalyEvent) -> str:
    """The all-clear: sent when a unit's ELD comes back online (resolved)."""
    return (
        "✅ <b>ELD RECONNECTED</b>\n\n"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Was disconnected for:</b> {human_duration(event.duration_seconds())}"
    )


def format_status_line(event: AnomalyEvent) -> str:
    return (
        f"🔴 <b>{escape(event.unit_number)}</b> — "
        f"{_fmt_speed(event.last_speed)} @ {escape(event.last_location or 'unknown')}\n"
        f"    disconnected {_fmt_time(event.eld_disconnect_time)} "
        f"(ongoing {human_duration(event.duration_seconds())})"
    )


def format_history_line(event: AnomalyEvent) -> str:
    if event.resolved:
        marker = "✅"
        tail = f"resolved, lasted {human_duration(event.duration_seconds())}"
    else:
        marker = "🔴"
        tail = f"ongoing {human_duration(event.duration_seconds())}"
    return (
        f"{marker} <b>{escape(event.unit_number)}</b> — "
        f"disconnected {_fmt_time(event.eld_disconnect_time)} ({tail})"
    )
