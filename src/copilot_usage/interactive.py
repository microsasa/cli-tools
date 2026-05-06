"""Interactive-mode UI helpers for copilot-usage.

Contains rendering, file-watching, and input helpers used by the
interactive Rich-based session loop in :mod:`copilot_usage.cli`.
"""

import sys
import threading
import time
from pathlib import Path
from typing import Final, Protocol, cast

from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.text import Text
from watchdog.observers import Observer

from copilot_usage import __version__
from copilot_usage.models import SessionSummary
from copilot_usage.report import (
    render_full_summary,
    session_display_name,
)

__all__: Final[list[str]] = [
    "WATCHDOG_DEBOUNCE_SECS",
    "print_version_header",
    "render_session_list",
    "draw_home",
    "write_prompt",
    "FileChangeEventHandler",
    "FileChangeHandler",
    "Stoppable",
    "start_observer",
    "stop_observer",
    "build_session_index",
]

WATCHDOG_DEBOUNCE_SECS: Final[float] = (
    2.0  # Prevents rapid redraws during tool-use bursts
)


def print_version_header(target: Console | None = None) -> None:
    """Print 'Copilot Usage' left-aligned with version right-aligned."""
    console = target or Console()
    title = "Copilot Usage"
    version_text = f"v{__version__}"
    header = Text()
    header.append(title, style="bold")
    header.append(" " * max(1, console.width - len(title) - len(version_text)))
    header.append(version_text, style="dim")
    console.print(header)


def render_session_list(console: Console, sessions: list[SessionSummary]) -> None:
    """Print a numbered list of sessions for interactive selection."""
    table = Table(title="Sessions", border_style="cyan")
    table.add_column("#", style="bold cyan", justify="right", width=4)
    table.add_column("Name", style="bold", max_width=40)
    table.add_column("Model")
    table.add_column("Status")

    for idx, s in enumerate(sessions, start=1):
        name = session_display_name(s)
        model = s.model or "—"
        status = "🟢 Active" if s.is_active else "Completed"
        table.add_row(str(idx), name, model, status)

    console.print(table)


def draw_home(console: Console, sessions: list[SessionSummary]) -> None:
    """Clear screen and render the home view."""
    console.clear()
    print_version_header(console)
    render_full_summary(sessions, target_console=console)
    console.print()
    render_session_list(console, sessions)


def write_prompt(prompt: str) -> None:
    """Write the prompt to stdout without a trailing newline and flush immediately."""
    sys.stdout.write(prompt)
    sys.stdout.flush()


class FileChangeEventHandler(Protocol):
    """Protocol for minimal filesystem event handlers used with watchdog."""

    def dispatch(self, event: object) -> None:
        """Handle a filesystem event."""


class FileChangeHandler:
    """Watchdog-compatible handler that triggers refresh on session-state changes.

    Implements the :class:`FileChangeEventHandler` ``dispatch(event)``
    Protocol expected by watchdog observers, without importing the heavy
    ``watchdog`` package at module level.
    """

    def __init__(self, change_event: threading.Event) -> None:
        self._change_event = change_event
        self._last_trigger = 0.0
        self._lock = threading.Lock()

    def dispatch(self, event: object) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_trigger <= WATCHDOG_DEBOUNCE_SECS:
                return
            self._last_trigger = now
        self._change_event.set()


class Stoppable(Protocol):
    """Minimal interface for a watchdog-style observer."""

    def stop(self) -> None: ...
    def join(self, timeout: float | None = None) -> None: ...
    def is_alive(self) -> bool: ...


def start_observer(
    session_path: Path, change_event: threading.Event
) -> Stoppable | None:
    """Start a watchdog observer monitoring *session_path* for changes.

    Returns ``None`` when the observer cannot be started (e.g. inotify
    watch limit exhausted, unsupported filesystem). The caller should
    treat a ``None`` return as "auto-refresh unavailable" and continue
    without it.
    """
    handler: FileChangeEventHandler = FileChangeHandler(change_event)
    observer = Observer()
    observer.schedule(handler, str(session_path), recursive=True)  # pyright: ignore[reportArgumentType]
    observer.daemon = True
    try:
        observer.start()
    except (OSError, RuntimeError) as exc:
        logger.warning("File watcher unavailable (auto-refresh disabled): {}", exc)
        # Best-effort cleanup in case the observer partially started
        try:
            if observer.is_alive():
                observer.stop()
                observer.join(timeout=2)
        except (OSError, RuntimeError) as cleanup_exc:
            logger.opt(exception=cleanup_exc).debug(
                "Failed to clean up file watcher after start failure"
            )
        return None
    return cast(Stoppable, observer)


def stop_observer(observer: Stoppable | None) -> None:
    """Stop a watchdog observer if running."""
    if observer is not None:
        observer.stop()
        observer.join(timeout=2)


def build_session_index(sessions: list[SessionSummary]) -> dict[str, int]:
    """Return a mapping from session_id to list index for O(1) lookup."""
    return {s.session_id: i for i, s in enumerate(sessions)}
