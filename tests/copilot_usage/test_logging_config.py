"""Tests for copilot_usage.logging_config — setup_logging, _emoji_patcher, LEVEL_EMOJI."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
# pyright: reportUnknownMemberType=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
import sys

import loguru
from loguru import logger

from copilot_usage.logging_config import (
    LEVEL_EMOJI,
    _emoji_patcher,
    setup_logging,
)

# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_adds_exactly_one_sink() -> None:
    """After setup_logging(), logger has exactly one handler."""
    setup_logging()
    assert len(logger._core.handlers) == 1


def test_setup_logging_idempotent() -> None:
    """Calling setup_logging() twice still results in exactly 1 sink."""
    setup_logging()
    setup_logging()
    assert len(logger._core.handlers) == 1


def test_setup_logging_targets_stderr() -> None:
    """After setup_logging(), the single sink targets sys.stderr."""
    setup_logging()
    handler = next(iter(logger._core.handlers.values()))
    assert handler._sink._stream is sys.stderr


def test_setup_logging_level_is_warning() -> None:
    """After setup_logging(), the minimum log level is WARNING."""
    setup_logging()
    handler = next(iter(logger._core.handlers.values()))
    assert handler.levelno == logger.level("WARNING").no


# ---------------------------------------------------------------------------
# _emoji_patcher
# ---------------------------------------------------------------------------


def test_emoji_patcher_known_levels() -> None:
    """_emoji_patcher sets record['extra']['emoji'] for each known level."""
    for level_name, expected_emoji in LEVEL_EMOJI.items():
        record: dict[str, object] = {
            "level": type("Level", (), {"name": level_name})(),
            "extra": {},
        }
        _emoji_patcher(record)  # type: ignore[arg-type]
        assert record["extra"]["emoji"] == expected_emoji  # type: ignore[index]


def test_emoji_patcher_unknown_level_fallback() -> None:
    """_emoji_patcher falls back to '  ' for an unknown level name."""
    record: dict[str, object] = {
        "level": type("Level", (), {"name": "NONEXISTENT"})(),
        "extra": {},
    }
    _emoji_patcher(record)  # type: ignore[arg-type]
    assert record["extra"]["emoji"] == "  "  # type: ignore[index]


# ---------------------------------------------------------------------------
# LEVEL_EMOJI
# ---------------------------------------------------------------------------

_STANDARD_LEVELS = frozenset(
    {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
)


def test_level_emoji_covers_all_standard_levels() -> None:
    """LEVEL_EMOJI contains entries for all seven standard loguru levels."""
    assert _STANDARD_LEVELS.issubset(LEVEL_EMOJI.keys())


# ---------------------------------------------------------------------------
# _emoji_patcher with real loguru record (issue #522)
# ---------------------------------------------------------------------------


def test_emoji_patcher_real_loguru_record() -> None:
    """_emoji_patcher works with an actual loguru record mapping at runtime.

    Exercises ``_emoji_patcher`` against a real ``message.record`` dict
    captured from a running loguru sink, ensuring it handles the concrete
    record structure produced at runtime.
    """
    captured: list[loguru.Record] = []

    def _sink(message: loguru.Message) -> None:
        captured.append(message.record)

    handler_id = logger.add(_sink, level="DEBUG")
    try:
        logger.info("test message for emoji patcher")
    finally:
        logger.remove(handler_id)

    assert captured, "expected at least one captured record"
    record = captured[0]
    _emoji_patcher(record)  # type: ignore[arg-type]
    assert record["extra"]["emoji"] == LEVEL_EMOJI["INFO"]
