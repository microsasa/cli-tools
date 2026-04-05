"""Filesystem helpers shared across the package."""

from collections import OrderedDict
from pathlib import Path
from typing import Final

__all__: Final[list[str]] = ["lru_insert", "safe_file_identity"]


def lru_insert[K, V](
    cache: OrderedDict[K, V],
    key: K,
    value: V,
    max_size: int,
) -> None:
    """Insert *key*→*value* into *cache* with LRU eviction at *max_size*.

    Raises :class:`ValueError` if *max_size* is less than 1.
    """
    if max_size < 1:
        msg = f"max_size must be >= 1, got {max_size}"
        raise ValueError(msg)
    if key in cache:
        del cache[key]
    elif len(cache) >= max_size:
        cache.popitem(last=False)
    cache[key] = value


def safe_file_identity(path: Path) -> tuple[int, int] | None:
    """Return ``(st_mtime_ns, st_size)`` for *path*, or ``None`` on any OS error.

    Uses nanosecond-precision mtime paired with file size for robust
    change detection — avoids the float-rounding and coarse-resolution
    issues of ``st_mtime``.  Returning ``None`` (rather than a sentinel
    tuple like ``(0, 0)``) makes it impossible for an absent-file marker
    to collide with a legitimate file identity.
    """
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
