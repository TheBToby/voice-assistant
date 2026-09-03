"""Localization for the assistant's built-in skills.

Pure data + formatting helpers (no livekit imports) so it is unit-testable:
see tests/unit/test_i18n.py.

The UI language is configured via the LANGUAGE env var (see agent/config.py);
it defaults to German. Adding a language means adding one `LanguagePack` to
`PACKS` plus a matching message dict. The same language code is also handed to
the ElevenLabs STT/TTS plugins and the system prompt, so languages without a
pack still work - only the built-in skill replies then fall back to English.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_LANGUAGE = "de"
FALLBACK_LANGUAGE = "en"

# (singular, plural) per unit: hours, minutes, seconds
DurationUnits = tuple[tuple[str, str], tuple[str, str], tuple[str, str]]


@dataclass(frozen=True)
class LanguagePack:
    """Calendar names, time format and built-in skill messages of a language."""

    code: str
    weekdays: tuple[str, ...]
    months: tuple[str, ...]
    clock_24h: bool
    time_suffix: str  # spoken after the time, e.g. " Uhr"
    now_template: str  # {time} and {date} placeholders
    date_template: str  # {weekday}, {day}, {month}, {year}
    duration_units: DurationUnits
    list_and: str  # joining word for "1 hour, 1 minute AND 1 second"
    messages: dict[str, str] = field(default_factory=dict)


_EN_MESSAGES = {
    "timer_duration_invalid": "The timer duration must be positive.",
    "timer_start_failed": "Could not start the timer: {reason}",
    "timer_started": "Timer started for {duration}.",
    "timer_started_named": "Timer called '{name}' started for {duration}.",
    "timer_cancelled": "Timer '{name}' cancelled.",
    "timer_not_found": "No timer named '{name}' is running. Running timers: {running}.",
    "no_timers_running": "No timers are running.",
    "running_timers": "Running timers:\n{list}",
    "timer_remaining": "- {name}: {duration} remaining",
    "timer_expired": "{name} timer is done!",
    "none": "none",
}

_DE_MESSAGES = {
    "timer_duration_invalid": "Die Timer-Dauer muss größer als 0 sein.",
    "timer_start_failed": "Timer konnte nicht gestartet werden: {reason}",
    "timer_started": "Timer für {duration} gestartet.",
    "timer_started_named": "Timer '{name}' für {duration} gestartet.",
    "timer_cancelled": "Timer '{name}' wurde abgebrochen.",
    "timer_not_found": "Es läuft kein Timer namens '{name}'. Laufende Timer: {running}.",
    "no_timers_running": "Es laufen keine Timer.",
    "running_timers": "Laufende Timer:\n{list}",
    "timer_remaining": "- {name}: noch {duration}",
    "timer_expired": "Der Timer '{name}' ist abgelaufen!",
    "none": "keine",
}

PACKS: dict[str, LanguagePack] = {
    "en": LanguagePack(
        code="en",
        weekdays=(
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ),
        months=(
            "January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December",
        ),
        clock_24h=False,
        time_suffix="",
        now_template="It's {time} on {date}.",
        date_template="{weekday}, {month} {day}, {year}",
        duration_units=(("hour", "hours"), ("minute", "minutes"), ("second", "seconds")),
        list_and="and",
        messages=_EN_MESSAGES,
    ),
    "de": LanguagePack(
        code="de",
        weekdays=(
            "Montag", "Dienstag", "Mittwoch", "Donnerstag",
            "Freitag", "Samstag", "Sonntag",
        ),
        months=(
            "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
            "August", "September", "Oktober", "November", "Dezember",
        ),
        clock_24h=True,
        time_suffix=" Uhr",
        now_template="Es ist {time} am {date}.",
        date_template="{weekday}, den {day}. {month} {year}",
        duration_units=(("Stunde", "Stunden"), ("Minute", "Minuten"), ("Sekunde", "Sekunden")),
        list_and="und",
        messages=_DE_MESSAGES,
    ),
}

# Human-readable names, used in the system prompt ("Always answer in German").
LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "en": "English",
}


def language_name(code: str) -> str:
    """Human-readable name for a language code ('de' -> 'German')."""
    return LANGUAGE_NAMES.get(code, code)


def normalize_language(value: str | None) -> str:
    """Map 'German', 'de', 'DE' or 'de-DE' to a supported language code.

    Only 'de' and 'en' are supported. Returns '' for anything else; callers
    decide how to handle that (keep the raw code for STT/TTS, fall back for
    the built-in strings).
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in LANGUAGE_NAMES:
        return lowered
    by_name = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
    if lowered in by_name:
        return by_name[lowered]
    base = lowered.replace("_", "-").split("-")[0]
    if base in LANGUAGE_NAMES:
        return base
    return by_name.get(base, "")


class Localizer:
    """Format dates, times, durations and skill messages in one language."""

    def __init__(self, language: str | None = None) -> None:
        self.code = (
            normalize_language(language)
            or (language or "").strip().lower()
            or DEFAULT_LANGUAGE
        )
        # languages without a pack keep their code (STT/TTS/prompt may still
        # support them) while the built-in strings fall back to English
        self.pack = PACKS.get(self.code, PACKS[FALLBACK_LANGUAGE])

    @property
    def language_name(self) -> str:
        return language_name(self.code)

    # ------------------------------------------------------------------
    # date & time
    # ------------------------------------------------------------------
    def now_text(self, dt: datetime | None = None) -> str:
        """Human-friendly spoken date & time."""
        dt = dt or datetime.now().astimezone()
        return self.pack.now_template.format(
            time=self._now_time_text(dt), date=self.date_text(dt)
        )

    def time_text(self, dt: datetime | None = None) -> str:
        """Short spoken time, e.g. '3:45 PM' or '15:45 Uhr'."""
        dt = dt or datetime.now().astimezone()
        if self.pack.clock_24h:
            return f"{dt.hour:02d}:{dt.minute:02d}{self.pack.time_suffix}"
        hour12, ampm = _to_12h(dt.hour)
        return f"{hour12}:{dt.minute:02d} {ampm}{self.pack.time_suffix}"

    def _now_time_text(self, dt: datetime) -> str:
        """Time as used inside now_text ('3 PM' instead of '3:00 PM')."""
        if self.pack.clock_24h:
            return self.time_text(dt)
        hour12, ampm = _to_12h(dt.hour)
        minute_part = f"{dt.minute:02d}" if dt.minute else ""
        time_str = f"{hour12}:{minute_part} {ampm}" if minute_part else f"{hour12} {ampm}"
        return time_str + self.pack.time_suffix

    def date_text(self, dt: datetime | None = None) -> str:
        """Short spoken date, e.g. 'Friday, September 3, 2026'."""
        dt = dt or datetime.now().astimezone()
        return self.pack.date_template.format(
            weekday=self.pack.weekdays[dt.weekday()],
            day=dt.day,
            month=self.pack.months[dt.month - 1],
            year=dt.year,
        )

    # ------------------------------------------------------------------
    # durations
    # ------------------------------------------------------------------
    def format_duration(self, seconds: float) -> str:
        """Spoken duration, e.g. '1 hour, 5 minutes and 30 seconds'."""
        seconds = max(0, int(round(seconds)))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        units = self.pack.duration_units
        parts: list[str] = []
        if hours:
            parts.append(_plural(units[0], hours))
        if minutes:
            parts.append(_plural(units[1], minutes))
        if secs or not parts:
            parts.append(_plural(units[2], secs))
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + f" {self.pack.list_and} " + parts[-1]

    # ------------------------------------------------------------------
    # skill messages
    # ------------------------------------------------------------------
    def message(self, key: str, **kwargs: object) -> str:
        """Look up a message template and fill in the placeholders."""
        template = self.pack.messages.get(key) or PACKS[FALLBACK_LANGUAGE].messages.get(key)
        if template is None:
            return key
        return template.format(**kwargs)


def _plural(unit: tuple[str, str], n: int) -> str:
    return f"{n} {unit[0] if n == 1 else unit[1]}"


def _to_12h(hour: int) -> tuple[int, str]:
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return hour12, ampm


