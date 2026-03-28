"""Temporary test file — deliberate coding guideline violations.

This file exists solely to test whether Copilot code review reads
copilot-instructions.md and flags violations. DELETE after testing.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loguru import Logger


def get_value(obj: object, key: str) -> str | None:
    """Get a value using getattr — violates guidelines."""
    return getattr(obj, key, None)


def check_type(value: object) -> bool:
    """Use hasattr — violates guidelines."""
    if hasattr(value, "name"):
        return True
    return False


def validate(x: object) -> None:
    """Use assert for type validation — violates guidelines."""
    assert isinstance(x, str), "must be a string"
