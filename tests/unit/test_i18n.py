"""Unit tests for the localization module (agent/i18n.py).

Built-in languages: German (default) and English.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from i18n import DEFAULT_LANGUAGE, LANGUAGE_NAMES, Localizer, normalize_language  # noqa: E402


def test_default_language_is_german():
    assert DEFAULT_LANGUAGE == "de"
    assert set(LANGUAGE_NAMES) == {"de", "en"}
    t = Localizer()
    assert t.code == "de"
    assert t.language_name == "German"


def test_german_datetime_is_24h():
    t = Localizer("de")
    dt = datetime(2026, 9, 3, 15, 45)
    assert t.time_text(dt) == "15:45 Uhr"
    assert t.date_text(dt) == "Donnerstag, den 3. September 2026"
    assert t.now_text(dt) == "Es ist 15:45 Uhr am Donnerstag, den 3. September 2026."


def test_german_leading_zero_hours():
    t = Localizer("de")
    assert t.time_text(datetime(2026, 1, 1, 0, 5)) == "00:05 Uhr"
    assert t.time_text(datetime(2026, 1, 1, 8, 0)) == "08:00 Uhr"


def test_english_output_unchanged():
    t = Localizer("en")
    dt = datetime(2026, 9, 3, 15, 45)
    assert t.now_text(dt) == "It's 3:45 PM on Thursday, September 3, 2026."
    assert t.time_text(datetime(2026, 1, 1, 0, 5)) == "12:05 AM"
    assert t.date_text(dt) == "Thursday, September 3, 2026"


def test_clock_wrappers_default_to_english():
    from clock import date_text, format_duration, now_text, time_text

    dt = datetime(2026, 9, 3, 15, 45)
    assert now_text(dt) == "It's 3:45 PM on Thursday, September 3, 2026."
    assert time_text(dt) == "3:45 PM"
    assert date_text(dt) == "Thursday, September 3, 2026"
    assert format_duration(3661) == "1 hour, 1 minute and 1 second"


def test_clock_wrappers_accept_localizer():
    from clock import format_duration, now_text

    t = Localizer("de")
    dt = datetime(2026, 9, 3, 15, 45)
    assert (
        now_text(dt, localizer=t)
        == "Es ist 15:45 Uhr am Donnerstag, den 3. September 2026."
    )
    assert format_duration(3661, localizer=t) == "1 Stunde, 1 Minute und 1 Sekunde"


def test_format_duration_german():
    t = Localizer("de")
    assert t.format_duration(0) == "0 Sekunden"
    assert t.format_duration(45) == "45 Sekunden"
    assert t.format_duration(60) == "1 Minute"
    assert t.format_duration(90) == "1 Minute und 30 Sekunden"
    assert t.format_duration(3600) == "1 Stunde"
    assert t.format_duration(3661) == "1 Stunde, 1 Minute und 1 Sekunde"


def test_timer_messages_in_both_languages():
    de, en = Localizer("de"), Localizer("en")
    assert de.message("timer_started", duration="7 Minuten") == (
        "Timer für 7 Minuten gestartet."
    )
    assert de.message(
        "timer_started_named", name="pizza", duration="7 Minuten"
    ) == ("Timer 'pizza' für 7 Minuten gestartet.")
    assert de.message("timer_cancelled", name="pizza") == (
        "Timer 'pizza' wurde abgebrochen."
    )
    assert de.message("timer_not_found", name="pizza", running="keine") == (
        "Es läuft kein Timer namens 'pizza'. Laufende Timer: keine."
    )
    assert de.message("no_timers_running") == "Es laufen keine Timer."
    assert de.message("running_timers", list="- pizza: noch 7 Minuten") == (
        "Laufende Timer:\n- pizza: noch 7 Minuten"
    )
    assert de.message("timer_expired", name="Pizza") == (
        "Der Timer 'Pizza' ist abgelaufen!"
    )
    assert de.message("timer_duration_invalid") == (
        "Die Timer-Dauer muss größer als 0 sein."
    )
    assert en.message("timer_expired", name="Pizza") == "Pizza timer is done!"
    assert en.message("timer_duration_invalid") == (
        "The timer duration must be positive."
    )


def test_unknown_language_falls_back_to_english_strings():
    t = Localizer("xx")
    assert t.code == "xx"  # kept for STT/TTS and the system prompt
    assert t.now_text(datetime(2026, 9, 3, 15, 45)).startswith("It's ")


def test_normalize_language():
    assert normalize_language("DE") == "de"
    assert normalize_language("de-DE") == "de"
    assert normalize_language("de_DE") == "de"
    assert normalize_language("German") == "de"
    assert normalize_language("english") == "en"
    assert normalize_language("en") == "en"
    assert normalize_language("fr") == ""  # not built in
    assert normalize_language("") == ""
    assert normalize_language(None) == ""
