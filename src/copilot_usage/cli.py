"""CLI entry-point for copilot-usage.

Provides ``summary``, ``session``, ``cost``, and ``live`` commands,
plus an interactive Rich-based session when invoked without a subcommand.
"""

import select
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text
from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
from watchdog.observers import Observer  # type: ignore[import-untyped]

from copilot_usage import __version__
from copilot_usage.models import SessionSummary, ensure_aware_opt
from copilot_usage.parser import (
    build_session_summary,
    discover_sessions,
    get_all_sessions,
    parse_events,
)
from copilot_usage.report import (
    render_cost_view,
    render_full_summary,
    render_live_sessions,
    render_session_detail,
    render_summary,
)

_DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]

console = Console()


def _print_version_header(target: Console | None = None) -> None:
    """Print 'Copilot Usage' left-aligned with version right-aligned."""
    c = target or console
    title = "Copilot Usage"
    version_text = f"v{__version__}"
    header = Text()
    header.append(title, style="bold")
    header.append(" " * max(1, c.width - len(title) - len(version_text)))
    header.append(version_text, style="dim")
    c.print(header)


# ---------------------------------------------------------------------------
# Interactive mode helpers
# ---------------------------------------------------------------------------

_HOME_PROMPT = "\nEnter session # for detail, [c] cost, [r] refresh, [q] quit: "
_BACK_PROMPT = "\nPress Enter to go back... "


def _render_session_list(console: Console, sessions: list[SessionSummary]) -> None:
    """Print a numbered list of sessions for interactive selection."""
    table = Table(title="Sessions", border_style="cyan")
    table.add_column("#", style="bold cyan", justify="right", width=4)
    table.add_column("Name", style="bold", max_width=40)
    table.add_column("Model")
    table.add_column("Status")

    for idx, s in enumerate(sessions, start=1):
        name = s.name or s.session_id[:12]
        model = s.model or "—"
        status = "🟢 Active" if s.is_active else "Completed"
        table.add_row(str(idx), name, model, status)

    console.print(table)


def _show_session_by_index(
    console: Console,
    sessions: list[SessionSummary],
    index: int,
) -> None:
    """Render session detail for the session at *index* (1-based)."""
    if index < 1 or index > len(sessions):
        console.print(f"[red]Invalid session number: {index}[/red]")
        return

    s = sessions[index - 1]
    if s.events_path is None:
        console.print("[red]No events path for this session.[/red]")
        return

    try:
        events = parse_events(s.events_path)
    except (FileNotFoundError, OSError) as exc:
        console.print(f"[red]Session file no longer available: {exc}[/red]")
        return

    render_session_detail(events, s, target_console=console)


def _draw_home(console: Console, sessions: list[SessionSummary]) -> None:
    """Clear screen and render the home view."""
    console.clear()
    _print_version_header(console)
    render_full_summary(sessions, target_console=console)
    console.print()
    _render_session_list(console, sessions)


def _write_prompt(prompt: str) -> None:
    """Write prompt to stdout without a newline wait."""
    sys.stdout.write(prompt)
    sys.stdout.flush()


def _read_line_nonblocking(timeout: float = 0.5) -> str | None:
    """Return a line from stdin if available within *timeout*, else None."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    return None


class _FileChangeHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Watchdog handler that triggers refresh on any session-state change."""

    def __init__(self, change_event: threading.Event) -> None:
        super().__init__()
        self._change_event = change_event
        self._last_trigger = 0.0

    def dispatch(self, event: object) -> None:
        now = time.monotonic()
        if now - self._last_trigger > 2.0:  # debounce 2s
            self._last_trigger = now
            self._change_event.set()


class _Stoppable(Protocol):
    """Minimal interface for a watchdog-style observer."""

    def stop(self) -> None: ...
    def join(self, timeout: float) -> None: ...


def _start_observer(session_path: Path, change_event: threading.Event) -> _Stoppable:
    """Start a watchdog observer monitoring *session_path* for changes."""
    handler = _FileChangeHandler(change_event)
    observer = Observer()
    observer.schedule(handler, str(session_path), recursive=True)
    observer.daemon = True
    observer.start()
    return observer  # type: ignore[return-value]


def _stop_observer(observer: _Stoppable | None) -> None:
    """Stop a watchdog observer if running."""
    if observer is not None:
        observer.stop()
        observer.join(timeout=2)


def _interactive_loop(path: Path | None) -> None:
    """Run the interactive Rich session loop with auto-refresh on file changes."""
    console = Console()
    session_path = path or Path.home() / ".copilot" / "session-state"

    # File watcher for auto-refresh
    change_event = threading.Event()
    observer = (
        _start_observer(session_path, change_event) if session_path.exists() else None
    )

    view: str = "home"  # "home" | "detail" | "cost"
    detail_idx: int | None = None

    sessions = get_all_sessions(path)
    _draw_home(console, sessions)
    _write_prompt(_HOME_PROMPT)

    try:
        while True:
            # Auto-refresh on file change
            if change_event.is_set():
                change_event.clear()
                sessions = get_all_sessions(path)
                if view == "home":
                    _draw_home(console, sessions)
                    _write_prompt(_HOME_PROMPT)
                elif view == "cost":
                    console.clear()
                    _print_version_header(console)
                    render_cost_view(sessions, target_console=console)
                    _write_prompt(_BACK_PROMPT)
                elif view == "detail" and detail_idx is not None:
                    console.clear()
                    _print_version_header(console)
                    _show_session_by_index(console, sessions, detail_idx)
                    _write_prompt(_BACK_PROMPT)

            # Non-blocking stdin read
            try:
                line = _read_line_nonblocking(timeout=0.5)
            except (ValueError, OSError):
                # stdin not selectable (e.g. testing) — fall back to blocking
                try:
                    line = input().strip()
                except (EOFError, KeyboardInterrupt):
                    break

            if line is None:
                continue

            # Sub-view: any input returns home
            if view in ("detail", "cost"):
                view = "home"
                detail_idx = None
                sessions = get_all_sessions(path)
                _draw_home(console, sessions)
                _write_prompt(_HOME_PROMPT)
                continue

            # Home view commands
            if line in ("q", "Q"):
                break

            if line == "":
                _write_prompt(_HOME_PROMPT)
                continue

            if line in ("c", "C"):
                view = "cost"
                console.clear()
                _print_version_header(console)
                render_cost_view(sessions, target_console=console)
                _write_prompt(_BACK_PROMPT)
                continue

            if line in ("r", "R"):
                sessions = get_all_sessions(path)
                _draw_home(console, sessions)
                _write_prompt(_HOME_PROMPT)
                continue

            try:
                num = int(line)
            except ValueError:
                console.print(f"[red]Unknown command: {line}[/red]")
                _write_prompt(_HOME_PROMPT)
                continue

            view = "detail"
            detail_idx = num
            console.clear()
            _print_version_header(console)
            _show_session_by_index(console, sessions, num)
            _write_prompt(_BACK_PROMPT)

    except KeyboardInterrupt:
        pass
    finally:
        _stop_observer(observer)


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="copilot-usage")
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Custom session-state directory.",
)
@click.pass_context
def main(ctx: click.Context, path: Path | None) -> None:
    """Copilot CLI usage tracker — parse local session data for token metrics."""
    from copilot_usage.logging_config import setup_logging

    setup_logging()

    ctx.ensure_object(dict)
    ctx.obj["path"] = path

    if ctx.invoked_subcommand is None:
        _interactive_loop(path)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--since",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="Show sessions starting after this date.",
)
@click.option(
    "--until",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="Show sessions starting before this date.",
)
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Custom session-state directory.",
)
@click.pass_context
def summary(
    ctx: click.Context,
    since: datetime | None,
    until: datetime | None,
    path: Path | None,
) -> None:
    """Show usage summary across all sessions."""
    _print_version_header()
    path = path or ctx.obj.get("path")
    try:
        sessions = get_all_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)
    render_summary(
        sessions, since=ensure_aware_opt(since), until=ensure_aware_opt(until)
    )


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


@main.command()
@click.argument("session_id")
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Custom session-state directory.",
)
@click.pass_context
def session(ctx: click.Context, session_id: str, path: Path | None) -> None:
    """Show detailed usage for a specific session."""
    _print_version_header()
    path = path or ctx.obj.get("path")
    try:
        event_paths = discover_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)
    if not event_paths:
        click.echo("No sessions found.", err=True)
        sys.exit(1)

    # Fast path: skip directories that clearly cannot match the prefix.
    # Only apply the pre-filter on UUID-shaped directory names (36 chars
    # with 4 dashes), where the directory name IS the session ID.
    # Non-UUID dirs (e.g. test fixtures) always need a full parse.
    available: list[str] = []
    for events_path in event_paths:
        dir_name = events_path.parent.name
        is_uuid_dir = len(dir_name) == 36 and dir_name.count("-") == 4
        if len(session_id) >= 4 and is_uuid_dir and not dir_name.startswith(session_id):
            available.append(dir_name[:8])
            continue
        try:
            events = parse_events(events_path)
        except OSError:
            continue
        if not events:
            continue
        s = build_session_summary(events, session_dir=events_path.parent)
        if s.session_id.startswith(session_id):
            render_session_detail(events, s)
            return
        if s.session_id:
            available.append(s.session_id[:8])

    click.echo(f"Error: no session matching '{session_id}'", err=True)
    if available:
        click.echo(f"Available: {', '.join(available)}", err=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--since",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="Show sessions starting after this date.",
)
@click.option(
    "--until",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="Show sessions starting before this date.",
)
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Custom session-state directory.",
)
@click.pass_context
def cost(
    ctx: click.Context,
    since: datetime | None,
    until: datetime | None,
    path: Path | None,
) -> None:
    """Show premium request costs from shutdown data."""
    _print_version_header()
    path = path or ctx.obj.get("path")
    try:
        sessions = get_all_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)

    render_cost_view(
        sessions,
        since=ensure_aware_opt(since),
        until=ensure_aware_opt(until),
    )


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Custom session-state directory.",
)
@click.pass_context
def live(ctx: click.Context, path: Path | None) -> None:
    """Show usage for active sessions."""
    _print_version_header()
    path = path or ctx.obj.get("path")
    try:
        sessions = get_all_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)
    render_live_sessions(sessions)
