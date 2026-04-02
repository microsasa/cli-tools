"""Pure formatting utilities shared by report and render_detail.

This module contains stateless formatting helpers that both
:mod:`copilot_usage.report` and :mod:`copilot_usage.render_detail`
need.  By living in a separate module, both can import at module scope
without creating a circular dependency.
"""

from datetime import timedelta
from typing import Final

__all__: Final[list[str]] = [
    "MAX_CONTENT_LEN",
    "format_duration",
    "format_timedelta",
    "format_tokens",
    "hms",
]

MAX_CONTENT_LEN: Final[int] = 80


def hms(total_seconds: int) -> tuple[int, int, int]:
    """Decompose *total_seconds* into ``(hours, minutes, seconds)``."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds


def format_timedelta(td: timedelta) -> str:
    """Format a timedelta to human-readable duration (e.g. '1h 5m 30s 481ms').

    Always includes milliseconds when present.
    """
    total_ms = max(int(td.total_seconds() * 1000), 0)
    remainder_ms = total_ms % 1000
    total_seconds = total_ms // 1000
    hours, minutes, seconds = hms(total_seconds)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    if remainder_ms or not parts:
        parts.append(f"{remainder_ms}ms")
    return " ".join(parts)


def format_duration(ms: int) -> str:
    """Format milliseconds to human-readable duration.

    Returns compact strings such as ``"6m 29s"``, ``"5s"``, or
    ``"1h 1m 1s"``.

    >>> format_duration(389114)
    '6m 29s 114ms'
    >>> format_duration(5000)
    '5s'
    >>> format_duration(0)
    '0ms'
    >>> format_duration(3661000)
    '1h 1m 1s'
    >>> format_duration(60000)
    '1m'
    >>> format_duration(481)
    '481ms'
    >>> format_duration(50)
    '50ms'
    >>> format_duration(1500)
    '1s 500ms'
    """
    return format_timedelta(timedelta(milliseconds=ms))


def format_tokens(n: int) -> str:
    """Format token count with K/M suffix.

    Returns ``"1.6M"`` for 1 627 935, ``"16.7K"`` for 16 655, or the
    raw integer string for values below 1 000.

    >>> format_tokens(1627935)
    '1.6M'
    >>> format_tokens(16655)
    '16.7K'
    >>> format_tokens(500)
    '500'
    >>> format_tokens(0)
    '0'
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
