"""Logging configuration — console-only for CLI tool."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

LEVEL_EMOJI: dict[str, str] = {
    "TRACE": "🔍",
    "DEBUG": "🐛",
    "INFO": "ℹ️ ",
    "SUCCESS": "✅",
    "WARNING": "⚠️ ",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}

CONSOLE_FORMAT = (
    "<dim>{time:HH:mm:ss}</dim> "
    "{extra[emoji]} "
    "<level>{level:<7}</level> "
    "<cyan>{message}</cyan>"
)


def _emoji_patcher(record: Record) -> None:
    record["extra"]["emoji"] = LEVEL_EMOJI.get(record["level"].name, "  ")


def setup_logging() -> None:
    """Configure loguru for CLI use: stderr only, WARNING level."""
    logger.remove()
    logger.configure(patcher=_emoji_patcher)
    logger.add(sys.stderr, format=CONSOLE_FORMAT, level="WARNING", colorize=True)
