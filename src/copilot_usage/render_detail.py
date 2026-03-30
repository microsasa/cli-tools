"""Session-detail rendering helpers for Copilot CLI.

Extracts the ``render_session_detail`` entry-point and its private
helpers from :mod:`copilot_usage.report` so that the summary/cost/live
concern and the session-detail concern live in separate modules.

All public symbols are re-exported by :mod:`copilot_usage.report` so
that external callers see no change.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from loguru import logger
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from copilot_usage._formatting import (
    MAX_CONTENT_LEN,
    format_duration,
    format_timedelta,
    format_tokens,
    hms,
)
from copilot_usage.models import (
    CodeChanges,
    EventType,
    SessionEvent,
    SessionShutdownData,
    SessionSummary,
    ToolExecutionData,
    ensure_aware,
    total_output_tokens,
)

__all__: Final[list[str]] = ["render_session_detail"]

# ---------------------------------------------------------------------------
# Session detail helpers
# ---------------------------------------------------------------------------


def _format_relative_time(delta: timedelta) -> str:
    """Format a timedelta as ``+M:SS`` or ``+H:MM:SS``."""
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, minutes, seconds = hms(total_seconds)
    if hours:
        return f"+{hours}:{minutes:02d}:{seconds:02d}"
    return f"+{minutes}:{seconds:02d}"


def _truncate(text: str, max_len: int = MAX_CONTENT_LEN) -> str:
    """Truncate *text* to *max_len* characters, appending '…' if needed."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _format_detail_duration(
    start: datetime | None,
    end: datetime | None,
) -> str:
    """Return a human-readable duration string between two timestamps."""
    if start is None or end is None:
        return "—"
    return format_timedelta(end - start)


def _event_type_label(event_type: str) -> Text:
    """Return a colour-coded :class:`Text` label for *event_type*."""
    match event_type:
        case EventType.USER_MESSAGE:
            return Text("user message", style="bold blue")
        case EventType.ASSISTANT_MESSAGE:
            return Text("assistant", style="bold green")
        case EventType.TOOL_EXECUTION_COMPLETE:
            return Text("tool", style="bold yellow")
        case EventType.TOOL_EXECUTION_START:
            return Text("tool start", style="yellow")
        case EventType.ASSISTANT_TURN_START:
            return Text("turn start", style="green")
        case EventType.ASSISTANT_TURN_END:
            return Text("turn end", style="green")
        case EventType.SESSION_START:
            return Text("session start", style="bold cyan")
        case EventType.SESSION_SHUTDOWN:
            return Text("session end", style="bold cyan")
        case _:
            return Text(event_type, style="dim")


def _safe_event_data[T](
    ev: SessionEvent,
    parser: Callable[[], T],
) -> T | None:
    """Parse event data, returning *None* on validation/type errors.

    Centralises the try/except used throughout the rendering layer so
    that every failure is observable via a ``debug``-level log line.
    """
    try:
        return parser()
    except (ValidationError, ValueError):
        logger.debug("Could not parse {} event data, skipping detail", ev.type)
        return None


def _build_event_details(ev: SessionEvent) -> str:
    """Build a one-line detail string for a timeline row."""
    match ev.type:
        case EventType.USER_MESSAGE:
            if (data := _safe_event_data(ev, ev.as_user_message)) is None:
                return ""
            if data.content:
                return _truncate(data.content)
            return ""

        case EventType.ASSISTANT_MESSAGE:
            if (data := _safe_event_data(ev, ev.as_assistant_message)) is None:
                return ""
            parts: list[str] = []
            if data.outputTokens:
                parts.append(f"tokens={data.outputTokens}")
            if data.content:
                parts.append(_truncate(data.content, 60))
            return "  ".join(parts)

        case EventType.TOOL_EXECUTION_COMPLETE:
            if (data := _safe_event_data(ev, ev.as_tool_execution)) is None:
                return ""
            parts = []
            tool_name = _extract_tool_name(data)
            if tool_name:
                parts.append(tool_name)
            parts.append("✓" if data.success else "✗")
            if data.model:
                parts.append(f"model={data.model}")
            return "  ".join(parts)

        case EventType.SESSION_SHUTDOWN:
            if (data := _safe_event_data(ev, ev.as_session_shutdown)) is None:
                return ""
            return f"type={data.shutdownType}" if data.shutdownType else ""

        case _:
            return ""


def _extract_tool_name(data: ToolExecutionData) -> str:
    """Try to extract a human-readable tool name from telemetry."""
    if data.toolTelemetry and data.toolTelemetry.properties:
        return data.toolTelemetry.properties.get("tool_name", "")
    return ""


# ---------------------------------------------------------------------------
# Header / aggregate / shutdown-cycle helpers for session detail
# ---------------------------------------------------------------------------


def _render_header(
    summary: SessionSummary,
    *,
    target_console: Console | None = None,
) -> None:
    """Print a Rich panel with session metadata."""
    out = target_console or Console()

    status = "[green]active[/green]" if summary.is_active else "[dim]completed[/dim]"
    start_str = (
        summary.start_time.strftime("%Y-%m-%d %H:%M:%S") if summary.start_time else "—"
    )
    duration = _format_detail_duration(summary.start_time, summary.end_time)
    name = summary.name or "unnamed"

    content = (
        f"[bold]Session:[/bold] {summary.session_id}\n"
        f"[bold]Name:[/bold]    {name}\n"
        f"[bold]Model:[/bold]   {summary.model or '—'}\n"
        f"[bold]Status:[/bold]  {status}\n"
        f"[bold]Started:[/bold] {start_str}\n"
        f"[bold]Duration:[/bold] {duration}"
    )
    out.print(Panel(content, title="Session Detail", border_style="blue"))


def _render_aggregate_stats(
    summary: SessionSummary,
    *,
    target_console: Console | None = None,
) -> None:
    """Print aggregate stats panel (model calls, user msgs, tokens, premium)."""
    out = target_console or Console()

    total_output = total_output_tokens(summary)

    lines = [
        f"[green]{summary.model_calls}[/green] model calls   "
        f"[green]{summary.user_messages}[/green] user messages   "
        f"[green]{format_tokens(total_output)}[/green] output tokens",
        f"[green]{summary.total_premium_requests}[/green] premium requests   "
        f"[green]{format_duration(summary.total_api_duration_ms)}[/green] API duration",
    ]
    out.print(Panel("\n".join(lines), title="Aggregate Stats", border_style="cyan"))


def _render_shutdown_cycles(
    events: list[SessionEvent],
    *,
    target_console: Console | None = None,
) -> None:
    """Render per-shutdown-cycle table from session events."""
    out = target_console or Console()

    shutdown_events: list[SessionShutdownData] = []
    shutdown_timestamps: list[datetime | None] = []
    for ev in events:
        if ev.type == EventType.SESSION_SHUTDOWN:
            if (data := _safe_event_data(ev, ev.as_session_shutdown)) is None:
                continue
            shutdown_events.append(data)
            shutdown_timestamps.append(ev.timestamp)

    if not shutdown_events:
        out.print("[dim]No shutdown cycles recorded.[/dim]")
        return

    table = Table(title="Shutdown Cycles", border_style="cyan")
    table.add_column("Date", style="cyan")
    table.add_column("Premium Req", justify="right", style="green")
    table.add_column("Model Calls", justify="right")
    table.add_column("Output Tokens", justify="right")
    table.add_column("API Duration", justify="right")

    for sd, ts in zip(shutdown_events, shutdown_timestamps, strict=True):
        date_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "—"
        total_requests = sum(mm.requests.count for mm in sd.modelMetrics.values())
        total_output = sum(mm.usage.outputTokens for mm in sd.modelMetrics.values())
        table.add_row(
            date_str,
            str(sd.totalPremiumRequests),
            str(total_requests),
            format_tokens(total_output),
            format_duration(sd.totalApiDurationMs),
        )

    out.print(table)


def _render_active_period(
    summary: SessionSummary,
    *,
    target_console: Console | None = None,
) -> None:
    """Show model calls / messages / tokens since last shutdown (if active)."""
    out = target_console or Console()

    if not summary.is_active:
        return

    content = (
        f"[green]{summary.active_model_calls}[/green] model calls   "
        f"[green]{summary.active_user_messages}[/green] user messages   "
        f"[green]{format_tokens(summary.active_output_tokens)}[/green] output tokens"
    )
    out.print(
        Panel(
            content,
            title="🟢 Active Period (since last shutdown)",
            border_style="green",
        )
    )


def _render_recent_events(
    events: list[SessionEvent],
    session_start: datetime,
    *,
    target_console: Console | None = None,
    max_events: int = 10,
) -> None:
    """Show the most recent *max_events* events with timestamp, type, brief info.

    *max_events* must be positive.  Passing ``0`` (or a negative value) is
    treated as "show nothing" so callers never accidentally render the full
    list via the ``events[-0:]`` Python-slice quirk.
    """
    out = target_console or Console()

    if not events or max_events <= 0:
        out.print("[dim]No events to display.[/dim]")
        return

    recent = events[-max_events:]

    table = Table(
        title="Recent Events", show_lines=False, expand=True, title_style="bold"
    )
    table.add_column("Time", style="cyan", width=12, no_wrap=True)
    table.add_column("Event", width=16)
    table.add_column("Details", ratio=1)

    for ev in recent:
        if ev.timestamp is not None:
            delta = ensure_aware(ev.timestamp) - session_start
            rel = _format_relative_time(delta)
        else:
            rel = "—"

        label = _event_type_label(ev.type)
        details = _build_event_details(ev)
        table.add_row(rel, label, details)

    out.print(table)


def _render_code_changes(
    code_changes: CodeChanges | None,
    *,
    target_console: Console | None = None,
) -> None:
    """Print code-change stats if present."""
    out = target_console or Console()

    if code_changes is None:
        return

    if (
        not code_changes.filesModified
        and not code_changes.linesAdded
        and not code_changes.linesRemoved
    ):
        return

    table = Table(title="Code Changes", title_style="bold", expand=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Files modified", str(len(code_changes.filesModified)))
    table.add_row("Lines added", f"[green]+{code_changes.linesAdded}[/green]")
    table.add_row("Lines removed", f"[red]-{code_changes.linesRemoved}[/red]")
    out.print(table)


# ---------------------------------------------------------------------------
# Main session detail entry point
# ---------------------------------------------------------------------------


def render_session_detail(
    events: list[SessionEvent],
    summary: SessionSummary,
    *,
    target_console: Console | None = None,
) -> None:
    """Render a useful summary view of a single session.

    Displays:
    - Header panel (name, ID, model, status, start time)
    - Aggregate stats (model calls, user messages, output tokens, premium)
    - Per-shutdown-cycle table
    - Active period (if session is active)
    - Last 10 events (recent activity, not a full timeline)
    - Code changes (if any)

    Parameters
    ----------
    events:
        The full list of parsed :class:`SessionEvent` objects for this
        session.
    summary:
        Pre-computed :class:`SessionSummary` for the session.
    target_console:
        Optional :class:`Console` to print to (defaults to a fresh
        console).
    """
    out = target_console or Console()

    _render_header(summary, target_console=out)
    out.print()

    _render_aggregate_stats(summary, target_console=out)
    out.print()

    _render_shutdown_cycles(events, target_console=out)
    out.print()

    _render_active_period(summary, target_console=out)

    session_start = (
        ensure_aware(summary.start_time)
        if summary.start_time
        else (
            ensure_aware(events[0].timestamp)
            if events and events[0].timestamp
            else datetime.now(tz=UTC)
        )
    )
    _render_recent_events(events, session_start, target_console=out)
    out.print()

    _render_code_changes(summary.code_changes, target_console=out)
