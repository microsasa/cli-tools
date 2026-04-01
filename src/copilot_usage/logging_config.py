"""Logging configuration — console-only for CLI tool."""

import sys
from typing import Final

from loguru import logger

LEVEL_EMOJI: Final[dict[str, str]] = {
    "TRACE": "🔍",
    "DEBUG": "🐛",
    "INFO": "ℹ️ ",
    "SUCCESS": "✅",
    "WARNING": "⚠️ ",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}

CONSOLE_FORMAT: Final[str] = (
    "<dim>{time:HH:mm:ss}</dim> "
    "{extra[emoji]} "
    "<level>{level:<7}</level> "
    "<cyan>{message}</cyan>"
)


def _emoji_patcher(record: dict[str, object]) -> None:
    """Inject a level-specific emoji into the log record's extras."""
    # record is structurally a loguru.Record (TypedDict) at runtime;
    # typed as plain dict because loguru.Record is stub-only, not importable.
    record["extra"]["emoji"] = LEVEL_EMOJI.get(record["level"].name, "  ")  # type: ignore[index,union-attr]


def setup_logging() -> None:
    """Configure loguru for CLI use: stderr only, WARNING level."""
    logger.remove()
    logger.configure(patcher=_emoji_patcher)  # type: ignore[arg-type]
    logger.add(sys.stderr, format=CONSOLE_FORMAT, level="WARNING", colorize=True)
