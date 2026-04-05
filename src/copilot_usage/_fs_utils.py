"""Filesystem helpers shared across the package."""

from pathlib import Path
from typing import Final

__all__: Final[list[str]] = ["safe_file_identity"]


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


# Backward-compatible alias (not in __all__); will be removed in a future release.
_safe_file_identity = safe_file_identity
