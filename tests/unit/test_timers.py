import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

import asyncio

from timers import TimerService


def run(coro):
    return asyncio.run(coro)


def test_start_and_expiry_calls_callback():
    async def scenario():
        fired = []
        service = TimerService()
        record = await service.start(0.05, lambda r: fired.append(r), name="pizza")
        assert record.name == "pizza"
        assert service.count == 1
        await asyncio.sleep(0.15)
        assert service.count == 0
        assert len(fired) == 1
        assert fired[0].name == "pizza"

    run(scenario())


def test_cancel_prevents_expiry():
    async def scenario():
        fired = []
        service = TimerService()
        await service.start(0.05, lambda r: fired.append(r), name="tea")
        assert service.cancel("TEA ") is True  # case-insensitive
        await asyncio.sleep(0.15)
        assert fired == []
        assert service.count == 0

    run(scenario())


def test_cancel_unknown_timer_returns_false():
    service = TimerService()
    assert service.cancel("nope") is False


def test_auto_names_are_unique():
    async def scenario():
        service = TimerService()
        r1 = await service.start(30, lambda r: None)
        r2 = await service.start(60, lambda r: None)
        assert (r1.name, r2.name) == ("timer 1", "timer 2")
        snapshot = service.snapshot()
        assert {t["name"] for t in snapshot} == {"timer 1", "timer 2"}
        assert all(t["remaining_seconds"] > 0 for t in snapshot)

    run(scenario())


def test_duplicate_name_rejected():
    async def scenario():
        service = TimerService()
        await service.start(30, lambda r: None, name="pizza")
        try:
            await service.start(10, lambda r: None, name="Pizza")
        except ValueError as exc:
            assert "already running" in str(exc)
        else:
            raise AssertionError("expected ValueError for duplicate name")

    run(scenario())


def test_cancel_by_numeric_id():
    async def scenario():
        fired = []
        service = TimerService()
        record = await service.start(0.05, lambda r: fired.append(r))
        assert service.cancel(record.id) is True
        await asyncio.sleep(0.12)
        assert fired == []

    run(scenario())


def test_find_returns_record():
    async def scenario():
        service = TimerService()
        record = await service.start(120, lambda r: None, name="laundry")
        assert service.find("LAUNDRY") is record
        assert service.find(record.id) is record
        assert service.find("missing") is None

    run(scenario())
