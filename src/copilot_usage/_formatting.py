"""Pure formatting utilities shared by report and render_detail.

This module contains stateless formatting helpers that both
:mod:`copilot_usage.report` and :mod:`copilot_usage.render_detail`
need.  By living in a separate module, both can import at module scope
without creating a circular dependency.
"""

from datetime import timedelta

_MAX_CONTENT_LEN = 80


def _hms(total_seconds: int) -> tuple[int, int, int]:
    """Decompose *total_seconds* into ``(hours, minutes, seconds)``."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds


def _format_timedelta(td: timedelta) -> str:
    """Format a timedelta to human-readable duration (e.g. '1h 5m 30s')."""
    total_seconds = max(int(td.total_seconds()), 0)
    hours, minutes, seconds = _hms(total_seconds)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_duration(ms: int) -> str:
    """Format milliseconds to human-readable duration.

    Returns compact strings such as ``"6m 29s"``, ``"5s"``, or
    ``"1h 1m 1s"``.
    """
    return _format_timedelta(timedelta(milliseconds=ms))


def format_tokens(n: int) -> str:
    """Format token count with K/M suffix.

    Returns ``"1.6M"`` for 1 627 935, ``"16.7K"`` for 16 655, or the
    raw integer string for values below 1 000.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
