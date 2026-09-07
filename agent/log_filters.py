"""Logging filters: downgrade expected, self-healed provider noise.

The voice pipeline retries transient provider hiccups automatically, but
the LiveKit frameworks log the benign cases loudly. The clearest example
is the ElevenLabs STT websocket: when a session ends (or the provider
idles out a quiet connection) the server closes the socket with close
code 1000 ("normal closure"). The plugin's recv_task treats any close it
did not initiate itself as an error and logs a full ERROR with traceback,
the framework then logs its automatic retry, and during session teardown
a best-effort binary send hits the already-closed room engine. None of
that affects the conversation.

This filter matches exactly those known-benign patterns. With the
default LOG_LEVEL=info the matched records are dropped entirely; with
LOG_LEVEL=debug they are downgraded to DEBUG and kept, so the full
sequence stays inspectable. Everything else - unexpected close codes
(1006/1008/1011, ...), non-retryable API errors, auth failures - keeps
its original level and is always logged.

Why not just downgrade? The root logger level only gates record
*creation*; once a filter mutates a record's level, emission still
depends on handler levels (which default to NOTSET = pass everything),
so a downgraded ERROR would still print on the console.
"""

from __future__ import annotations

import logging

# (logger name prefix, needle A, needle B): a record is matched when the
# record's logger name starts with the prefix and both needles appear in the
# message or the attached exception chain.
_DOWNGRADE_RULES: tuple[tuple[str, str, str], ...] = (
    # recv_task ERROR on the provider closing the socket with code 1000
    (
        "livekit.plugins.elevenlabs",
        "ElevenLabs STT connection closed unexpectedly",
        "status_code=1000",
    ),
    # the framework's automatic retry for that same event
    ("livekit.agents", "failed to recognize speech", "status_code=1000"),
    # best-effort send after the room engine already closed (teardown)
    ("livekit.agents", "failed to send binary stream message", "engine is closed"),
)


def _record_text(record: logging.LogRecord) -> str:
    """Formatted message plus any attached exception-chain text.

    The needles can live in either place: e.g. the recv_task ERROR's
    message is just "Error in recv_task" while the close details are on
    the APIStatusError, and the reverse is true for the retry warning.
    """
    parts = [record.getMessage()]
    if record.exc_text:
        parts.append(record.exc_text)
    exc = record.exc_info[1] if record.exc_info else None
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    return "\n".join(parts)


class TeardownNoiseFilter(logging.Filter):
    """Drop known-benign provider websocket-close noise (or debug it).

    When the root log level is DEBUG ("LOG_LEVEL=debug") the matched
    records are kept, downgraded to DEBUG. At any higher level they are
    dropped, because a downgraded ERROR would still pass every handler
    whose level defaults to NOTSET.
    """

    def __init__(self, verbose_level: int = logging.DEBUG) -> None:
        super().__init__()
        self.verbose_level = verbose_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            for name_prefix, needle_a, needle_b in _DOWNGRADE_RULES:
                if not record.name.startswith(name_prefix):
                    continue
                text = _record_text(record)
                if needle_a in text and needle_b in text:
                    if logging.getLogger().getEffectiveLevel() <= self.verbose_level:
                        record.levelno = logging.DEBUG
                        record.levelname = "DEBUG"
                        break
                    return False  # drop: not visible at this log level
        return True


def install() -> TeardownNoiseFilter:
    """Attach the filter to the relevant loggers and the root handlers.

    Logger-level filters keep working for handlers that are added later
    (e.g. by the worker CLI), but only for records logged on that exact
    logger; the handler-level attachment also covers child loggers of
    livekit.agents. Applying both makes the downgrade idempotent.
    """
    noise_filter = TeardownNoiseFilter()
    for name in ("livekit.plugins.elevenlabs", "livekit.agents"):
        logging.getLogger(name).addFilter(noise_filter)
    for handler in logging.getLogger().handlers:
        handler.addFilter(noise_filter)
    return noise_filter