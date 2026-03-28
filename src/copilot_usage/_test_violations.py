"""Temporary test file — deliberate coding guideline violations.

DELETE after testing.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loguru import Logger


def get_value(obj: object, key: str) -> str | None:
    """Violates guidelines."""
    return getattr(obj, key, None)
