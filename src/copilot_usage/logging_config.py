"""Logging configuration — console-only for CLI tool."""

import sys

import loguru  # noqa: F401  — needed for pyright to resolve "loguru.Record" (stub-only type)
from loguru import logger

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


def _emoji_patcher(record: "loguru.Record") -> None:
    """Inject a level-specific emoji into the log record's extras."""
    record["extra"]["emoji"] = LEVEL_EMOJI.get(record["level"].name, "  ")


def setup_logging() -> None:
    """Configure loguru for CLI use: stderr only, WARNING level."""
    logger.remove()
    logger.configure(patcher=_emoji_patcher)
    logger.add(sys.stderr, format=CONSOLE_FORMAT, level="WARNING", colorize=True)
