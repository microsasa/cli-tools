"""Logging configuration — console-only for CLI tool."""

import sys
from typing import Final, Protocol, TypedDict

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


class _LevelLike(Protocol):
    """Minimal structural type for the ``level`` field of a loguru record."""

    @property
    def name(self) -> str: ...


class _PatcherRecord(TypedDict):
    """Subset of ``loguru.Record`` describing only the keys ``_emoji_patcher`` touches.

    ``loguru.Record`` is a stub-only ``TypedDict`` (not importable at runtime).
    This local supertype lets the patcher body stay fully type-safe while
    remaining compatible with the real ``Record`` via TypedDict structural
    subtyping.
    """

    level: _LevelLike
    extra: dict[str, object]


def _emoji_patcher(record: _PatcherRecord) -> None:
    """Inject a level-specific emoji into the log record's extras."""
    record["extra"]["emoji"] = LEVEL_EMOJI.get(record["level"].name, "  ")


def setup_logging() -> None:
    """Configure loguru for CLI use: stderr only, WARNING level."""
    logger.remove()
    logger.configure(patcher=_emoji_patcher)  # type: ignore[arg-type]
    logger.add(sys.stderr, format=CONSOLE_FORMAT, level="WARNING", colorize=True)
