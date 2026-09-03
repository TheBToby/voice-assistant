"""Time & date helpers for the built-in clock skill (pure functions)."""

from __future__ import annotations

from datetime import datetime

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def now_text(dt: datetime | None = None) -> str:
    """Human-friendly spoken date & time, e.g.

    "It's 3:45 PM on Friday, September 3, 2026."
    """
    dt = dt or datetime.now().astimezone()
    hour12, ampm = _to_12h(dt.hour)
    minute = dt.minute
    minute_part = f"{minute:02d}" if minute else ""
    time_str = f"{hour12}:{minute_part} {ampm}" if minute_part else f"{hour12} {ampm}"
    date_str = f"{WEEKDAYS[dt.weekday()]}, {MONTHS[dt.month - 1]} {dt.day}, {dt.year}"
    return f"It's {time_str} on {date_str}."


def time_text(dt: datetime | None = None) -> str:
    """Short spoken time, e.g. '3:45 PM'."""
    dt = dt or datetime.now().astimezone()
    hour12, ampm = _to_12h(dt.hour)
    return f"{hour12}:{dt.minute:02d} {ampm}"


def date_text(dt: datetime | None = None) -> str:
    """Short spoken date, e.g. "Friday, September 3, 2026"."""
    dt = dt or datetime.now().astimezone()
    return f"{WEEKDAYS[dt.weekday()]}, {MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


def format_duration(seconds: float) -> str:
    """Spoken duration, e.g. "1 hour, 5 minutes and 30 seconds"."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _to_12h(hour: int) -> tuple[int, str]:
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return hour12, ampm
