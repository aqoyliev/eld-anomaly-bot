"""Human-readable rendering of anomaly events for Telegram (HTML parse mode)."""

from datetime import datetime
from html import escape
from typing import Optional

from .store import AnomalyEvent


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
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return value


def _fmt_speed(speed: Optional[float]) -> str:
    return f"{speed:.0f} mph" if speed is not None else "unknown"


def format_alert(event: AnomalyEvent) -> str:
    """The 🚨 alert sent when a new anomaly is detected."""
    driver = f" — driver {escape(event.driver)}" if event.driver else ""
    return (
        "🚨 <b>ELD DISCONNECTION ANOMALY</b>\n\n"
        f"<b>Vehicle:</b> {escape(event.unit_number)}{driver}\n"
        f"<b>Last known location:</b> {escape(event.last_location or 'unknown')}\n"
        f"<b>Current speed:</b> {_fmt_speed(event.last_speed)}\n"
        f"<b>ELD disconnected at:</b> {_fmt_time(event.eld_disconnect_time)}\n"
        f"<b>Anomaly duration:</b> {human_duration(event.duration_seconds())}\n\n"
        "<i>Disconnected on GreenLight ELD but still moving on GoMotive.</i>"
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
