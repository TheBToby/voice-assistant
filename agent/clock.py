"""Time & date helpers for the built-in clock skill (pure functions).

All formatting lives in i18n.Localizer; these wrappers keep the historical
English defaults (see tests/unit/test_clock.py) while the agent passes the
language configured via LANGUAGE (default: German).
"""

from __future__ import annotations

from datetime import datetime

from i18n import FALLBACK_LANGUAGE, Localizer, _to_12h

__all__ = ["_to_12h", "date_text", "format_duration", "now_text", "time_text"]


def now_text(dt: datetime | None = None, localizer: Localizer | None = None) -> str:
    """Human-friendly spoken date & time, e.g.

    "It's 3:45 PM on Friday, September 3, 2026."          (en)
    "Es ist 15:45 Uhr am Freitag, den 3. September 2026." (de)
    """
    return (localizer or Localizer(FALLBACK_LANGUAGE)).now_text(dt)


def time_text(dt: datetime | None = None, localizer: Localizer | None = None) -> str:
    """Short spoken time, e.g. '3:45 PM' or '15:45 Uhr'."""
    return (localizer or Localizer(FALLBACK_LANGUAGE)).time_text(dt)


def date_text(dt: datetime | None = None, localizer: Localizer | None = None) -> str:
    """Short spoken date, e.g. "Friday, September 3, 2026"."""
    return (localizer or Localizer(FALLBACK_LANGUAGE)).date_text(dt)


def format_duration(
    seconds: float, localizer: Localizer | None = None
) -> str:
    """Spoken duration, e.g. "1 hour, 5 minutes and 30 seconds"."""
    return (localizer or Localizer(FALLBACK_LANGUAGE)).format_duration(seconds)
