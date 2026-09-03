import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

import asyncio
from datetime import datetime

from clock import _to_12h, date_text, format_duration, now_text, time_text


def test_now_text_contains_weekday_and_time():
    dt = datetime(2026, 9, 3, 15, 45)
    text = now_text(dt)
    assert "3:45 PM" in text
    assert "Thursday" in text
    assert "September 3, 2026" in text


def test_time_and_date_text():
    dt = datetime(2026, 1, 1, 0, 5)
    assert time_text(dt) == "12:05 AM"
    assert date_text(dt) == "Thursday, January 1, 2026"


def test_to_12h_edges():
    assert _to_12h(0) == (12, "AM")
    assert _to_12h(12) == (12, "PM")
    assert _to_12h(13) == (1, "PM")
    assert _to_12h(23) == (11, "PM")


def test_format_duration():
    assert format_duration(0) == "0 seconds"
    assert format_duration(45) == "45 seconds"
    assert format_duration(60) == "1 minute"
    assert format_duration(90) == "1 minute and 30 seconds"
    assert format_duration(3600) == "1 hour"
    assert format_duration(3661) == "1 hour, 1 minute and 1 second"
