"""Tests for copilot_usage.interactive module."""

import copilot_usage.interactive as _interactive_mod
from watchdog.observers import Observer


def test_watchdog_observer_imported_at_module_level() -> None:
    """Observer must be importable as a module-level attribute, not deferred."""
    assert hasattr(_interactive_mod, "Observer"), (
        "Observer should be imported at module level in interactive.py"
    )
    assert _interactive_mod.Observer is Observer
