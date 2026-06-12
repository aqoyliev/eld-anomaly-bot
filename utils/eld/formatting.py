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
    # GoMotive (esp. the v1 endpoint the tracker uses) sometimes reports a null
    # speed for a reading; display that as "0 mph" rather than "unknown". This is
    # display-only — the raw None value still drives stop detection / movement.
    return f"{speed:.0f} mph" if speed is not None else "0 mph"


def _driver_suffix(driver: Optional[str]) -> str:
    return f" — {escape(driver)}" if driver else ""


def _company_line(company_name: Optional[str]) -> str:
    """Header line naming the company, so an operator watching several alert
    groups can tell them apart. Empty (no extra line) when not provided."""
    return f"<b>Company:</b> {escape(company_name)}\n" if company_name else ""


def _provider_label(event: AnomalyEvent) -> str:
    """Display name of the movement provider that flagged the event. Legacy
    rows (provider NULL) predate Samsara support, so they were GoMotive."""
    return "Samsara" if event.provider == "samsara" else "GoMotive"


def _provider_badge(event: AnomalyEvent) -> str:
    """Compact provider tag for one-line listings (/status, /history): the
    provider name with an emoji so the source device is visible at a glance."""
    return f"📡 {_provider_label(event)}"


def format_alert(event: AnomalyEvent, company_name: Optional[str] = None) -> str:
    """The 🚨 alert sent when a new anomaly is detected."""
    return (
        "🚨 <b>ELD DISCONNECTION ANOMALY</b>\n\n"
        f"{_company_line(company_name)}"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Current coordinates:</b> <code>{escape(event.last_location or 'unknown')}</code>\n"
        f"<b>Current speed:</b> {_fmt_speed(event.last_speed)}\n"
        f"<b>ELD disconnected at:</b> {_fmt_time(event.eld_disconnect_time)}\n"
        f"<b>Anomaly duration:</b> {human_duration(event.duration_seconds())}\n\n"
        f"<i>Disconnected on Quantum ELD but still moving on {_provider_label(event)}.</i>"
    )


def format_stopped(
    event: AnomalyEvent, coords: str, company_name: Optional[str] = None
) -> str:
    """Sent once when a disconnected unit pulls over / stops moving."""
    return (
        "⏸ <b>DISCONNECTED UNIT STOPPED</b>\n\n"
        f"{_company_line(company_name)}"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Stopped at:</b> <code>{escape(coords)}</code>\n"
        f"<b>Disconnected for:</b> {human_duration(event.duration_seconds())}\n\n"
        f"<i>No movement on {_provider_label(event)} since the last check, "
        "ELD still disconnected.</i>"
    )


def format_reminder(event: AnomalyEvent, company_name: Optional[str] = None) -> str:
    """Periodic reminder that a unit is still disconnected."""
    return (
        "🚨 <b>STILL DISCONNECTED</b>\n\n"
        f"{_company_line(company_name)}"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Current coordinates:</b> <code>{escape(event.last_location or 'unknown')}</code>\n"
        f"<b>Current speed:</b> {_fmt_speed(event.last_speed)}\n"
        f"<b>Anomaly duration:</b> {human_duration(event.duration_seconds())}"
    )


def format_reconnected(
    event: AnomalyEvent, company_name: Optional[str] = None
) -> str:
    """The all-clear: sent when a unit's ELD comes back online (resolved)."""
    return (
        "✅ <b>ELD RECONNECTED</b>\n\n"
        f"{_company_line(company_name)}"
        f"<b>Unit:</b> <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)}\n"
        f"<b>Was disconnected for:</b> {human_duration(event.duration_seconds())}"
    )


def format_status_line(event: AnomalyEvent) -> str:
    return (
        f"🔴 <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)} "
        f"· {_provider_badge(event)}\n"
        f"    ⚡ {_fmt_speed(event.last_speed)} · "
        f"📍 {escape(event.last_location or 'unknown')}\n"
        f"    🕑 disconnected {_fmt_time(event.eld_disconnect_time)} "
        f"· ongoing {human_duration(event.duration_seconds())}"
    )


def format_history_line(event: AnomalyEvent) -> str:
    if event.resolved:
        marker = "✅"
        tail = f"resolved, lasted {human_duration(event.duration_seconds())}"
    else:
        marker = "🔴"
        tail = f"ongoing {human_duration(event.duration_seconds())}"
    return (
        f"{marker} <code>{escape(event.unit_number)}</code>{_driver_suffix(event.driver)} "
        f"· {_provider_badge(event)}\n"
        f"    🕑 disconnected {_fmt_time(event.eld_disconnect_time)} · {tail}"
    )
