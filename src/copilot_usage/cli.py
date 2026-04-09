"""CLI entry-point for copilot-usage.

Provides ``summary``, ``session``, ``cost``, and ``live`` commands,
plus an interactive Rich-based session when invoked without a subcommand.
"""

import select
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Final, Literal, Protocol, cast

import click
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.text import Text

from copilot_usage import __version__
from copilot_usage.logging_config import setup_logging
from copilot_usage.models import SessionSummary, ensure_aware, ensure_aware_opt
from copilot_usage.parser import (
    DEFAULT_SESSION_PATH,
    get_all_sessions,
    get_cached_events,
)
from copilot_usage.report import (
    render_cost_view,
    render_full_summary,
    render_live_sessions,
    render_session_detail,
    render_summary,
    session_display_name,
)

type _View = Literal["home", "detail", "cost"]

# (format_string, has_explicit_time) pairs — single source of truth.
_FORMAT_SPECS: Final[list[tuple[str, bool]]] = [
    ("%Y-%m-%d", False),
    ("%Y-%m-%dT%H:%M:%S", True),
]

_DATE_FORMATS: Final[list[str]] = [fmt for fmt, _ in _FORMAT_SPECS]

_WATCHDOG_DEBOUNCE_SECS: Final[float] = (
    2.0  # Prevents rapid redraws during tool-use bursts
)


@dataclass(frozen=True, slots=True)
class _ParsedDateArg:
    """Carries a parsed datetime together with whether the user supplied a time."""

    value: datetime
    has_explicit_time: bool


class _DateTimeOrDate(click.ParamType):
    """Click parameter type that distinguishes date-only from datetime inputs.

    Parses ``%Y-%m-%d`` as date-only (``has_explicit_time=False``) and
    ``%Y-%m-%dT%H:%M:%S`` as datetime (``has_explicit_time=True``).
    Returns a :class:`_ParsedDateArg`.
    """

    name: str = "datetime-or-date"

    def convert(  # noqa: RET503
        self,
        value: str | datetime,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> _ParsedDateArg:
        """Parse *value* into a ``_ParsedDateArg``."""
        if isinstance(value, datetime):
            # Already parsed (e.g. default value) — treat as explicit time.
            return _ParsedDateArg(value=value, has_explicit_time=True)

        result = self._try_parse(value)
        if result is not None:
            return result

        msg = (
            f"invalid datetime format: {value!r}. "
            "Expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
        )
        self.fail(msg, param, ctx)

    @staticmethod
    def _try_parse(value: str) -> _ParsedDateArg | None:
        """Attempt date-only then datetime parsing; return ``None`` on failure."""
        for fmt, explicit in _FORMAT_SPECS:
            try:
                return _ParsedDateArg(
                    value=datetime.strptime(value, fmt),
                    has_explicit_time=explicit,
                )
            except ValueError:
                continue
        return None


console: Final[Console] = Console()


def _normalize_until(arg: _ParsedDateArg | None) -> datetime | None:
    """Extend a date-only ``--until`` value to end-of-day (23:59:59.999999).

    Only expands to end-of-day when the user supplied a date without a time
    component (``has_explicit_time is False``).  An explicit
    ``--until 2026-03-07T00:00:00`` is left as-is, giving strict
    before-midnight semantics.
    """
    if arg is None:
        return None
    aware = ensure_aware(arg.value)
    if not arg.has_explicit_time and aware.time() == dt_time(0, 0, 0):
        return aware.replace(hour=23, minute=59, second=59, microsecond=999999)
    return aware


def _validate_since_until(
    since: datetime | None,
    until: _ParsedDateArg | None,
) -> tuple[datetime | None, datetime | None]:
    """Normalize and validate --since/--until, raising on reversed range."""
    aware_since = ensure_aware_opt(since)
    aware_until = _normalize_until(until)
    if (
        aware_since is not None
        and aware_until is not None
        and aware_since > aware_until
    ):
        raise click.UsageError(
            f"--since ({aware_since.isoformat(sep=' ', timespec='seconds')}) "
            f"is after --until ({aware_until.isoformat(sep=' ', timespec='seconds')}); "
            "no sessions will match."
        )
    return aware_since, aware_until


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

_HOME_PROMPT: Final[str] = (
    "\nEnter session # for detail, [c] cost, [r] refresh, [q] quit: "
)
_BACK_PROMPT: Final[str] = "\nPress Enter to go back... "


def _render_session_list(console: Console, sessions: list[SessionSummary]) -> None:
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
        events = get_cached_events(s.events_path)
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
    """Return a line from stdin if available within *timeout*, else None.

    Raises :class:`EOFError` when stdin is closed (``readline()`` returns
    an empty string), preventing an infinite polling loop.
    """
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        line = sys.stdin.readline()
        if not line:  # empty string means EOF
            raise EOFError("stdin closed")
        return line.strip()
    return None


class _FileChangeEventHandler(Protocol):
    """Protocol for minimal filesystem event handlers used with watchdog."""

    def dispatch(self, event: object) -> None:
        """Handle a filesystem event."""


class _FileChangeHandler:
    """Watchdog-compatible handler that triggers refresh on session-state changes.

    Implements the :class:`_FileChangeEventHandler` ``dispatch(event)``
    Protocol expected by watchdog observers, without importing the heavy
    ``watchdog`` package at module level.
    """

    def __init__(self, change_event: threading.Event) -> None:
        self._change_event = change_event
        self._last_trigger = 0.0

    def dispatch(self, event: object) -> None:
        now = time.monotonic()
        if now - self._last_trigger > _WATCHDOG_DEBOUNCE_SECS:
            self._last_trigger = now
            self._change_event.set()


class _Stoppable(Protocol):
    """Minimal interface for a watchdog-style observer."""

    def stop(self) -> None: ...
    def join(self, timeout: float | None = None) -> None: ...
    def is_alive(self) -> bool: ...


def _start_observer(
    session_path: Path, change_event: threading.Event
) -> _Stoppable | None:
    """Start a watchdog observer monitoring *session_path* for changes.

    Returns ``None`` when the observer cannot be started (e.g. inotify
    watch limit exhausted, unsupported filesystem). The caller should
    treat a ``None`` return as "auto-refresh unavailable" and continue
    without it.
    """
    from watchdog.observers import Observer  # type: ignore[import-untyped]

    handler: _FileChangeEventHandler = _FileChangeHandler(change_event)
    observer = Observer()
    observer.schedule(handler, str(session_path), recursive=True)  # type: ignore[arg-type]
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
    return cast(_Stoppable, observer)


def _stop_observer(observer: _Stoppable | None) -> None:
    """Stop a watchdog observer if running."""
    if observer is not None:
        observer.stop()
        observer.join(timeout=2)


def _build_session_index(sessions: list[SessionSummary]) -> dict[str, int]:
    """Return a mapping from session_id to list index for O(1) lookup."""
    return {s.session_id: i for i, s in enumerate(sessions)}


def _interactive_loop(path: Path | None) -> None:
    """Run the interactive Rich session loop with auto-refresh on file changes."""
    console = Console()
    session_path = path or DEFAULT_SESSION_PATH

    # File watcher for auto-refresh
    change_event = threading.Event()
    observer = (
        _start_observer(session_path, change_event) if session_path.exists() else None
    )

    view: _View = "home"
    detail_session_id: str | None = None

    sessions = get_all_sessions(path)
    session_index = _build_session_index(sessions)
    _draw_home(console, sessions)
    _write_prompt(_HOME_PROMPT)

    try:
        while True:
            # Auto-refresh on file change
            if change_event.is_set():
                change_event.clear()
                try:
                    sessions = get_all_sessions(path)
                    session_index = _build_session_index(sessions)
                    if view == "home":
                        _draw_home(console, sessions)
                        _write_prompt(_HOME_PROMPT)
                    elif view == "cost":
                        console.clear()
                        _print_version_header(console)
                        render_cost_view(sessions, target_console=console)
                        _write_prompt(_BACK_PROMPT)
                    elif view == "detail" and detail_session_id is not None:
                        detail_idx = session_index.get(detail_session_id)
                        if detail_idx is None:
                            view = "home"
                            detail_session_id = None
                            _draw_home(console, sessions)
                            _write_prompt(_HOME_PROMPT)
                        else:
                            console.clear()
                            _print_version_header(console)
                            _show_session_by_index(console, sessions, detail_idx + 1)
                            _write_prompt(_BACK_PROMPT)
                    else:
                        # detail view with no valid session — reset to home
                        view = "home"
                        detail_session_id = None
                        _draw_home(console, sessions)
                        _write_prompt(_HOME_PROMPT)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.opt(exception=True).warning(
                        "Auto-refresh render failed; will retry on next change"
                    )
                    # Best-effort prompt write so the terminal remains usable
                    try:
                        prompt = _HOME_PROMPT if view == "home" else _BACK_PROMPT
                        _write_prompt(prompt)
                    except Exception as exc:
                        logger.opt(exception=exc).debug(
                            "Best-effort prompt write also failed"
                        )

            # Non-blocking stdin read
            try:
                line = _read_line_nonblocking(timeout=0.5)
            except EOFError:
                break
            except (ValueError, OSError):
                # stdin not selectable (e.g. testing) — fall back to blocking
                try:
                    line = input().strip()
                except (EOFError, KeyboardInterrupt):
                    break
                except Exception as exc:
                    logger.warning(
                        "Unexpected stdin error; exiting interactive mode: {}", exc
                    )
                    break

            if line is None:
                continue

            # Sub-view: any input returns home
            if view in ("detail", "cost"):
                view = "home"
                detail_session_id = None
                if change_event.is_set():
                    change_event.clear()
                    sessions = get_all_sessions(path)
                    session_index = _build_session_index(sessions)
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
                session_index = _build_session_index(sessions)
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
            detail_session_id = (
                sessions[num - 1].session_id if 1 <= num <= len(sessions) else None
            )
            console.clear()
            _print_version_header(console)
            _show_session_by_index(console, sessions, num)
            _write_prompt(_BACK_PROMPT)

    except KeyboardInterrupt:
        pass  # User pressed Ctrl-C; observer cleanup runs in finally
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
    help="Show sessions starting on or after this date.",
)
@click.option(
    "--until",
    type=_DateTimeOrDate(),
    default=None,
    help="Show sessions starting before or at this timestamp cutoff (date-only values are expanded to end-of-day).",
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
    until: _ParsedDateArg | None,
    path: Path | None,
) -> None:
    """Show usage summary across all sessions."""
    path = path or ctx.obj.get("path")
    aware_since, aware_until = _validate_since_until(since, until)
    _print_version_header()
    try:
        sessions = get_all_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)
    render_summary(sessions, since=aware_since, until=aware_until)


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
        all_sessions = get_all_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)
    if not all_sessions:
        click.echo("No sessions found.", err=True)
        sys.exit(1)

    matched: SessionSummary | None = None
    for s in all_sessions:
        if s.session_id.startswith(session_id):
            matched = s
            break

    if matched is None:
        available = [s.session_id[:8] for s in all_sessions if s.session_id]
        click.echo(f"Error: no session matching '{session_id}'", err=True)
        if available:
            click.echo(f"Available: {', '.join(available)}", err=True)
        sys.exit(1)

    if matched.events_path is None:
        click.echo("Error: no events path for this session.", err=True)
        sys.exit(1)

    try:
        events = get_cached_events(matched.events_path)
    except OSError as exc:
        click.echo(f"Error reading session: {exc}", err=True)
        sys.exit(1)

    render_session_detail(events, matched)


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--since",
    type=click.DateTime(formats=_DATE_FORMATS),
    default=None,
    help="Show sessions starting on or after this date.",
)
@click.option(
    "--until",
    type=_DateTimeOrDate(),
    default=None,
    help="Show sessions starting before or at this timestamp cutoff (date-only values are expanded to end-of-day).",
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
    until: _ParsedDateArg | None,
    path: Path | None,
) -> None:
    """Show premium request costs from shutdown data."""
    path = path or ctx.obj.get("path")
    aware_since, aware_until = _validate_since_until(since, until)
    _print_version_header()
    try:
        sessions = get_all_sessions(path)
    except OSError as exc:
        click.echo(f"Error reading sessions: {exc}", err=True)
        sys.exit(1)

    render_cost_view(sessions, since=aware_since, until=aware_until)


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


# ---------------------------------------------------------------------------
# vscode
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--vscode-logs",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to VS Code 'Code/logs' directory (parent of the dated log folders).",
)
def vscode(vscode_logs: Path | None) -> None:
    """Show usage from VS Code Copilot Chat logs."""
    from copilot_usage.vscode_parser import get_vscode_summary
    from copilot_usage.vscode_report import render_vscode_summary

    _print_version_header()
    summary = get_vscode_summary(vscode_logs)
    if summary.total_requests == 0:
        if summary.log_files_found > 0 and summary.log_files_parsed == 0:
            click.echo("Error: log files were found but could not be read.", err=True)
        else:
            click.echo("No VS Code Copilot Chat requests found.", err=True)
        sys.exit(1)
    render_vscode_summary(summary)
