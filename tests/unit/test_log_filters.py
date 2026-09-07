"""Tests for the agent's log noise filter (agent/log_filters.py).

The ElevenLabs STT websocket is closed by the server with code 1000
(normal closure) at session end; the plugin logs a full ERROR traceback,
the framework logs its automatic retry, and teardown adds a best-effort
send against the already-closed room engine. The filter downgrades that
self-healed sequence to DEBUG - these tests pin down which messages are
affected and, more importantly, which stay loud.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from log_filters import TeardownNoiseFilter, install  # noqa: E402

FILTER = TeardownNoiseFilter()


@pytest.fixture(autouse=True)
def root_log_level():
    """Tests set the root level explicitly; always restore it afterwards."""
    root = logging.getLogger()
    old_level = root.level
    yield root
    root.setLevel(old_level)


class FakeAPIStatusError(Exception):
    """Mirror livekit.agents.APIStatusError's str() for filter matching."""

    def __init__(self, message: str, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = True
        self.body = body

    def __str__(self) -> str:  # matches the real exception's shape
        return (
            f"message={self.message!r}, status_code={self.status_code}, "
            f"retryable={self.retryable}, body={self.body}"
        )


def make_record(
    name: str,
    level: int,
    msg: str,
    exc: Exception | None = None,
) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc is not None else None
    return logging.LogRecord(name, level, "somewhere.py", 1, msg, (), exc_info)


def test_elevenlabs_close_1000_error_is_downgraded(root_log_level):
    root_log_level.setLevel(logging.DEBUG)  # LOG_LEVEL=debug
    record = make_record(
        "livekit.plugins.elevenlabs",
        logging.ERROR,
        "Error in recv_task",  # needle lives on the exception, not the message
        exc=FakeAPIStatusError(
            "ElevenLabs STT connection closed unexpectedly",
            status_code=1000,
            body="msg.data=1000 msg.extra=''",
        ),
    )
    assert FILTER.filter(record) is True  # record is kept, not dropped
    assert record.levelno == logging.DEBUG
    assert record.levelname == "DEBUG"


def test_recognize_speech_retry_warning_is_downgraded(root_log_level):
    root_log_level.setLevel(logging.DEBUG)  # LOG_LEVEL=debug
    # here the needles are part of the formatted message itself
    record = make_record(
        "livekit.agents",
        logging.WARNING,
        "failed to recognize speech: message='ElevenLabs STT connection "
        "closed unexpectedly', status_code=1000, retryable=True, "
        "retrying in 0.1s",
    )
    assert FILTER.filter(record) is True
    assert record.levelno == logging.DEBUG


def test_engine_closed_send_warning_is_downgraded(root_log_level):
    root_log_level.setLevel(logging.DEBUG)  # LOG_LEVEL=debug
    record = make_record(
        "livekit.agents",
        logging.WARNING,
        "failed to send binary stream message",
        exc=ConnectionError("engine: connection error: engine is closed"),
    )
    assert FILTER.filter(record) is True
    assert record.levelno == logging.DEBUG


def test_other_close_codes_stay_loud():
    for status_code in (-1, 1006, 1008, 1011):
        record = make_record(
            "livekit.plugins.elevenlabs",
            logging.ERROR,
            "Error in recv_task",
            exc=FakeAPIStatusError(
                "ElevenLabs STT connection closed unexpectedly",
                status_code=status_code,
            ),
        )
        assert FILTER.filter(record) is True
        assert record.levelno == logging.ERROR, status_code
        assert record.levelname == "ERROR"


def test_other_loggers_and_messages_stay_loud():
    # same text from an unrelated logger is not touched
    record = make_record(
        "my.own.app",
        logging.WARNING,
        "failed to recognize speech: status_code=1000",
    )
    FILTER.filter(record)
    assert record.levelno == logging.WARNING
    # elevenlabs messages without the close-1000 pattern are not touched
    record = make_record(
        "livekit.plugins.elevenlabs",
        logging.ERROR,
        "failed to process ElevenLabs STT message",
    )
    FILTER.filter(record)
    assert record.levelno == logging.ERROR


def test_filter_is_idempotent(root_log_level):
    root_log_level.setLevel(logging.DEBUG)
    record = make_record(
        "livekit.agents",
        logging.WARNING,
        "failed to send binary stream message",
        exc=ConnectionError("engine: connection error: engine is closed"),
    )
    FILTER.filter(record)
    FILTER.filter(record)  # e.g. logger-level and handler-level attachment
    assert record.levelno == logging.DEBUG


def test_matching_records_are_dropped_at_default_log_level(root_log_level):
    root_log_level.setLevel(logging.INFO)  # LOG_LEVEL=info (the default)
    # a downgraded ERROR would still pass handlers whose level is NOTSET,
    # so at info+ the record is dropped outright
    record = make_record(
        "livekit.plugins.elevenlabs",
        logging.ERROR,
        "Error in recv_task",
        exc=FakeAPIStatusError(
            "ElevenLabs STT connection closed unexpectedly",
            status_code=1000,
        ),
    )
    assert FILTER.filter(record) is False
    record = make_record(
        "livekit.agents",
        logging.WARNING,
        "failed to send binary stream message",
        exc=ConnectionError("engine: connection error: engine is closed"),
    )
    assert FILTER.filter(record) is False
    # anything not matching the rules is kept as usual
    keep = make_record(
        "livekit.plugins.elevenlabs",
        logging.ERROR,
        "Error in recv_task",
        exc=FakeAPIStatusError(
            "ElevenLabs STT connection closed unexpectedly", status_code=1006
        ),
    )
    assert FILTER.filter(keep) is True


def test_downgraded_records_skip_warning_level_handlers(root_log_level):
    logger = logging.getLogger("livekit.plugins.elevenlabs")
    old_level, old_filters = logger.level, list(logger.filters)
    emitted: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record)

    capture = Capture()
    capture.setLevel(logging.WARNING)  # after __init__ (which resets level)
    try:
        logger.setLevel(logging.DEBUG)
        logger.addFilter(FILTER)
        logger.addHandler(capture)
        logger.error(
            "Error in recv_task",
            exc_info=(
                FakeAPIStatusError,
                FakeAPIStatusError(
                    "ElevenLabs STT connection closed unexpectedly",
                    status_code=1000,
                ),
                None,
            ),
        )
        logger.warning("real problem happened")  # unchanged level -> emitted
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
        logger.filters = old_filters

    assert [r.getMessage() for r in emitted] == ["real problem happened"]


def test_install_attaches_to_expected_loggers_and_handlers():
    noise_filter = install()
    for name in ("livekit.plugins.elevenlabs", "livekit.agents"):
        assert any(f is noise_filter for f in logging.getLogger(name).filters)
    for handler in logging.getLogger().handlers:
        assert any(f is noise_filter for f in handler.filters)