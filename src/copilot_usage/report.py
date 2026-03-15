"""Rendering helpers for Copilot CLI session data.

Uses Rich tables and panels to display session information in
the terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from copilot_usage.models import (
    AssistantMessageData,
    CodeChanges,
    EventType,
    ModelMetrics,
    SessionEvent,
    SessionShutdownData,
    SessionSummary,
    ToolExecutionData,
    UserMessageData,
)

__all__ = [
    "format_duration",
    "format_tokens",
    "render_cost_view",
    "render_full_summary",
    "render_live_sessions",
    "render_session_detail",
    "render_summary",
]

_MAX_CONTENT_LEN = 80


def format_tokens(n: int) -> str:
    """Format token count with K/M suffix.

    Examples:
        >>> format_tokens(1627935)
        '1.6M'
        >>> format_tokens(16655)
        '16.7K'
        >>> format_tokens(500)
        '500'
        >>> format_tokens(0)
        '0'
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_duration(ms: int) -> str:
    """Format milliseconds to human-readable duration.

    Examples:
        >>> format_duration(389114)
        '6m 29s'
        >>> format_duration(5000)
        '5s'
        >>> format_duration(0)
        '0s'
        >>> format_duration(3661000)
        '1h 1m 1s'
    """
    if ms <= 0:
        return "0s"
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _format_elapsed_since(start: datetime) -> str:
    """Return a human-readable elapsed time from *start* to now.

    Formats as ``Xh Ym`` when >= 1 hour, otherwise ``Ym Zs``.
    """
    now = datetime.now(tz=UTC)
    # Ensure start is timezone-aware for subtraction
    start_aware = start.replace(tzinfo=UTC) if start.tzinfo is None else start
    delta = now - start_aware
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def _estimated_output_tokens(session: SessionSummary) -> int:
    """Sum outputTokens across all models in *session.model_metrics*."""
    return sum(m.usage.outputTokens for m in session.model_metrics.values())


def render_live_sessions(sessions: list[SessionSummary]) -> None:
    """Render overview of active sessions only.

    Filters to ``is_active=True`` sessions.
    Shows running time as ``Xh Ym`` or ``Ym Zs``.
    """
    console = Console()

    active = [s for s in sessions if s.is_active]

    if not active:
        console.print(
            Panel(
                "No active Copilot sessions found",
                title="Live Sessions",
                border_style="dim",
            )
        )
        return

    table = Table(title="🟢 Active Copilot Sessions")
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="green")
    table.add_column("Model", style="magenta")
    table.add_column("Running", style="yellow", justify="right")
    table.add_column("Messages", style="blue", justify="right")
    table.add_column("Output Tokens", style="red", justify="right")
    table.add_column("CWD", style="dim")

    for s in active:
        short_id = s.session_id[:8] if s.session_id else "—"
        name = s.name or "—"
        model = s.model or "—"
        running = (
            _format_elapsed_since(s.last_resume_time or s.start_time)
            if s.start_time
            else "—"
        )
        messages = str(s.user_messages)
        tokens = f"{_estimated_output_tokens(s):,}"
        cwd = s.cwd or "—"

        table.add_row(
            f"🟢 {short_id}",
            name,
            model,
            running,
            messages,
            tokens,
            cwd,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Session detail helpers
# ---------------------------------------------------------------------------


def _format_relative_time(delta: timedelta) -> str:
    """Format a timedelta as ``+M:SS`` or ``+H:MM:SS``."""
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"+{hours}:{minutes:02d}:{seconds:02d}"
    return f"+{minutes}:{seconds:02d}"


def _truncate(text: str, max_len: int = _MAX_CONTENT_LEN) -> str:
    """Truncate *text* to *max_len* characters, appending '…' if needed."""
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
    delta = end - start
    total_seconds = max(int(delta.total_seconds()), 0)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


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


def _build_event_details(ev: SessionEvent) -> str:
    """Build a one-line detail string for a timeline row."""
    match ev.type:
        case EventType.USER_MESSAGE:
            data = ev.parse_data()
            if isinstance(data, UserMessageData) and data.content:
                return _truncate(data.content)
            return ""

        case EventType.ASSISTANT_MESSAGE:
            data = ev.parse_data()
            if isinstance(data, AssistantMessageData):
                parts: list[str] = []
                if data.outputTokens:
                    parts.append(f"tokens={data.outputTokens}")
                if data.content:
                    parts.append(_truncate(data.content, 60))
                return "  ".join(parts)
            return ""

        case EventType.TOOL_EXECUTION_COMPLETE:
            data = ev.parse_data()
            if isinstance(data, ToolExecutionData):
                parts_t: list[str] = []
                tool_name = _extract_tool_name(data)
                if tool_name:
                    parts_t.append(tool_name)
                parts_t.append("✓" if data.success else "✗")
                if data.model:
                    parts_t.append(f"model={data.model}")
                return "  ".join(parts_t)
            return ""

        case EventType.SESSION_SHUTDOWN:
            data = ev.parse_data()
            if isinstance(data, SessionShutdownData):
                return f"type={data.shutdownType}" if data.shutdownType else ""
            return ""

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

    total_output = sum(mm.usage.outputTokens for mm in summary.model_metrics.values())

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
            data = ev.parse_data()
            if isinstance(data, SessionShutdownData):
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
    """Show the most recent *max_events* events with timestamp, type, brief info."""
    out = target_console or Console()

    if not events:
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
            delta = ev.timestamp - session_start
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

    session_start = summary.start_time or (
        events[0].timestamp if events and events[0].timestamp else datetime.now(tz=UTC)
    )
    _render_recent_events(events, session_start, target_console=out)
    out.print()

    _render_code_changes(summary.code_changes, target_console=out)


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------


def _filter_sessions(
    sessions: list[SessionSummary],
    since: datetime | None,
    until: datetime | None,
) -> list[SessionSummary]:
    """Return sessions whose start_time falls within [since, until]."""
    if since is None and until is None:
        return sessions

    filtered: list[SessionSummary] = []
    for s in sessions:
        if s.start_time is None:
            continue
        if since is not None and s.start_time < since:
            continue
        if until is not None and s.start_time > until:
            continue
        filtered.append(s)
    return filtered


def _aggregate_model_metrics(
    sessions: list[SessionSummary],
) -> dict[str, ModelMetrics]:
    """Merge model metrics across all sessions into a single dict."""
    merged: dict[str, ModelMetrics] = {}
    for s in sessions:
        for model_name, mm in s.model_metrics.items():
            if model_name not in merged:
                merged[model_name] = ModelMetrics(
                    requests=mm.requests.model_copy(),
                    usage=mm.usage.model_copy(),
                )
            else:
                existing = merged[model_name]
                existing.requests.count += mm.requests.count
                existing.requests.cost += mm.requests.cost
                existing.usage.inputTokens += mm.usage.inputTokens
                existing.usage.outputTokens += mm.usage.outputTokens
                existing.usage.cacheReadTokens += mm.usage.cacheReadTokens
                existing.usage.cacheWriteTokens += mm.usage.cacheWriteTokens
    return merged


def _render_summary_header(
    console: Console,
    sessions: list[SessionSummary],
) -> None:
    """Print the report header with date range."""
    start_times = [s.start_time for s in sessions if s.start_time is not None]
    if start_times:
        earliest = min(start_times).strftime("%Y-%m-%d")
        latest = max(start_times).strftime("%Y-%m-%d")
        subtitle = f"{earliest}  →  {latest}"
    else:
        subtitle = "no sessions"
    console.print()
    console.print(
        Text("Copilot Usage Summary", style="bold cyan"),
        Text(f"  ({subtitle})", style="dim"),
    )
    console.print()


def _render_totals(console: Console, sessions: list[SessionSummary]) -> None:
    """Render the totals panel."""
    total_premium = sum(s.total_premium_requests for s in sessions)
    total_model_calls = sum(s.model_calls for s in sessions)
    total_user_messages = sum(s.user_messages for s in sessions)
    total_duration = sum(s.total_api_duration_ms for s in sessions)
    total_sessions = len(sessions)

    total_output = 0
    for s in sessions:
        for mm in s.model_metrics.values():
            total_output += mm.usage.outputTokens

    pr_label = "premium request" if total_premium == 1 else "premium requests"
    session_label = "session" if total_sessions == 1 else "sessions"
    lines = [
        f"[green]{total_premium}[/green] {pr_label}   "
        f"[green]{total_model_calls}[/green] model calls   "
        f"[green]{total_user_messages}[/green] user messages   "
        f"[green]{format_tokens(total_output)}[/green] output tokens",
        f"[green]{format_duration(total_duration)}[/green] API duration   "
        f"[green]{total_sessions}[/green] {session_label}",
    ]

    console.print(Panel("\n".join(lines), title="Totals", border_style="cyan"))


def _render_model_table(
    console: Console,
    sessions: list[SessionSummary],
    *,
    title: str = "Per-Model Breakdown",
) -> None:
    """Render the per-model breakdown table."""
    merged = _aggregate_model_metrics(sessions)
    if not merged:
        return

    table = Table(title=title, border_style="cyan")
    table.add_column("Model", style="bold")
    table.add_column("Requests", justify="right")
    table.add_column("Premium Cost", justify="right")
    table.add_column("Input Tokens", justify="right")
    table.add_column("Output Tokens", justify="right")
    table.add_column("Cache Read", justify="right")

    for model_name in sorted(merged):
        mm = merged[model_name]
        table.add_row(
            model_name,
            str(mm.requests.count),
            str(mm.requests.cost),
            format_tokens(mm.usage.inputTokens),
            format_tokens(mm.usage.outputTokens),
            format_tokens(mm.usage.cacheReadTokens),
        )

    console.print(table)


def _render_session_table(
    console: Console,
    sessions: list[SessionSummary],
    *,
    title: str = "Sessions",
) -> None:
    """Render the per-session table sorted by start time (newest first)."""
    if not sessions:
        return

    sorted_sessions = sorted(
        sessions,
        key=lambda s: s.start_time.isoformat() if s.start_time else "",
        reverse=True,
    )

    table = Table(title=title, border_style="cyan")
    table.add_column("Name", style="bold", max_width=40)
    table.add_column("Model")
    table.add_column("Premium", justify="right")
    table.add_column("Model Calls", justify="right")
    table.add_column("User Msgs", justify="right")
    table.add_column("Output Tokens", justify="right")
    table.add_column("Status")

    for s in sorted_sessions:
        name = s.name or s.session_id[:12]
        model = s.model or "—"

        output_tokens = sum(mm.usage.outputTokens for mm in s.model_metrics.values())

        if s.is_active:
            status = Text("Active 🟢", style="yellow")
        else:
            status = Text("Completed", style="dim")

        # Show premium requests from shutdown data if > 0, otherwise "—"
        if s.total_premium_requests > 0:
            pr_display = str(s.total_premium_requests)
        else:
            pr_display = "—"

        table.add_row(
            name,
            model,
            pr_display,
            str(s.model_calls),
            str(s.user_messages),
            format_tokens(output_tokens),
            status,
        )

    console.print(table)


def render_summary(
    sessions: list[SessionSummary],
    since: datetime | None = None,
    until: datetime | None = None,
) -> None:
    """Render the full summary report to the terminal using Rich.

    Filters sessions by date range when *since* and/or *until* are given.
    """
    console = Console()
    filtered = _filter_sessions(sessions, since, until)

    if not filtered:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    _render_summary_header(console, filtered)
    _render_totals(console, filtered)
    console.print()
    _render_model_table(console, filtered)
    console.print()
    _render_session_table(console, filtered)
    console.print()


# ---------------------------------------------------------------------------
# Two-section full summary (for interactive mode)
# ---------------------------------------------------------------------------


def _render_historical_section(
    console: Console,
    sessions: list[SessionSummary],
) -> None:
    """Render Section 1: Historical Data from shutdown cycles."""
    # Filter to sessions that have shutdown data
    historical = [
        s
        for s in sessions
        if s.total_premium_requests > 0 or (s.model_metrics and not s.is_active)
    ]

    if not historical:
        console.print("[dim]No historical shutdown data.[/dim]")
        return

    # Totals panel
    total_premium = sum(s.total_premium_requests for s in historical)
    total_model_calls = sum(s.model_calls for s in historical)
    total_user_messages = sum(s.user_messages for s in historical)
    total_duration = sum(s.total_api_duration_ms for s in historical)
    total_output = sum(
        mm.usage.outputTokens for s in historical for mm in s.model_metrics.values()
    )

    lines = [
        f"[green]{total_premium}[/green] premium requests   "
        f"[green]{total_model_calls}[/green] model calls   "
        f"[green]{total_user_messages}[/green] user messages   "
        f"[green]{format_tokens(total_output)}[/green] output tokens",
        f"[green]{format_duration(total_duration)}[/green] API duration",
    ]
    console.print(
        Panel("\n".join(lines), title="📊 Historical Totals", border_style="cyan")
    )

    # Per-model table
    _render_model_table(console, historical)

    # Per-session table
    _render_session_table(console, historical, title="Sessions (Shutdown Data)")


def _render_active_section(
    console: Console,
    sessions: list[SessionSummary],
) -> None:
    """Render Section 2: Active Sessions since last shutdown."""
    active = [s for s in sessions if s.is_active]

    if not active:
        console.print(
            Panel(
                "No active sessions", title="🟢 Active Sessions", border_style="green"
            )
        )
        return

    table = Table(
        title="🟢 Active Sessions (Since Last Shutdown)", border_style="green"
    )
    table.add_column("Name", style="bold", max_width=40)
    table.add_column("Model")
    table.add_column("Model Calls", justify="right")
    table.add_column("User Msgs", justify="right")
    table.add_column("Output Tokens", justify="right")
    table.add_column("Running Time", justify="right")

    for s in active:
        name = s.name or s.session_id[:12]
        model = s.model or "—"
        running = (
            _format_elapsed_since(s.last_resume_time or s.start_time)
            if s.start_time
            else "—"
        )

        table.add_row(
            name,
            model,
            str(s.active_model_calls),
            str(s.active_user_messages),
            format_tokens(s.active_output_tokens),
            running,
        )

    console.print(table)


def render_full_summary(
    sessions: list[SessionSummary],
    *,
    target_console: Console | None = None,
) -> None:
    """Render the two-section summary for interactive mode.

    Section 1: Historical shutdown data (totals, per-model, per-session).
    Section 2: Active sessions since last shutdown.
    """
    console = target_console or Console()

    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    _render_summary_header(console, sessions)
    _render_historical_section(console, sessions)
    console.print()
    _render_active_section(console, sessions)


# ---------------------------------------------------------------------------
# Cost view (for interactive mode)
# ---------------------------------------------------------------------------


def render_cost_view(
    sessions: list[SessionSummary],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    target_console: Console | None = None,
) -> None:
    """Render per-session, per-model cost breakdown.

    Filters sessions by date range when *since* and/or *until* are given.
    For active sessions, appends a "↳ Since last shutdown" row with N/A
    for premium and the active model calls / output tokens.
    """
    console = target_console or Console()
    sessions = _filter_sessions(sessions, since, until)

    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    table = Table(title="💰 Cost Breakdown", border_style="cyan")
    table.add_column("Session", style="bold", max_width=35)
    table.add_column("Model")
    table.add_column("Requests", justify="right")
    table.add_column("Premium Cost", justify="right", style="green")
    table.add_column("Model Calls", justify="right")
    table.add_column("Output Tokens", justify="right")

    grand_premium = 0
    grand_requests = 0
    grand_model_calls = 0
    grand_output = 0

    for s in sessions:
        name = s.name or s.session_id[:12]
        model_calls_display = str(s.model_calls)

        if s.model_metrics:
            for model_name in sorted(s.model_metrics):
                mm = s.model_metrics[model_name]
                table.add_row(
                    name,
                    model_name,
                    str(mm.requests.count),
                    str(mm.requests.cost),
                    model_calls_display,
                    format_tokens(mm.usage.outputTokens),
                )
                grand_requests += mm.requests.count
                grand_premium += mm.requests.cost
                grand_output += mm.usage.outputTokens
                # Only show session-level info once
                name = ""
                model_calls_display = ""
        else:
            table.add_row(
                name,
                s.model or "—",
                "—",
                "—",
                str(s.model_calls),
                "—",
            )

        grand_model_calls += s.model_calls

        if s.is_active:
            table.add_row(
                "  ↳ Since last shutdown",
                s.model or "—",
                "N/A",
                "N/A",
                str(s.active_model_calls),
                format_tokens(s.active_output_tokens),
            )
            grand_output += s.active_output_tokens

    table.add_section()
    table.add_row(
        "[bold]Grand Total[/bold]",
        "",
        f"[bold]{grand_requests}[/bold]",
        f"[bold]{grand_premium}[/bold]",
        f"[bold]{grand_model_calls}[/bold]",
        f"[bold]{format_tokens(grand_output)}[/bold]",
    )

    console.print(table)
