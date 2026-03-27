"""Rendering helpers for Copilot CLI session data.

Uses Rich tables and panels to display session information in
the terminal.

Session-detail rendering (``render_session_detail`` and its private
helpers) lives in :mod:`copilot_usage.render_detail` and is re-exported
here so that external callers see no change.
"""

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from copilot_usage._formatting import (
    _format_timedelta,  # pyright: ignore[reportPrivateUsage]
    format_duration,
    format_tokens,
)
from copilot_usage.models import (
    EPOCH,
    ModelMetrics,
    SessionSummary,
    ensure_aware,
    has_active_period_stats,
    merge_model_metrics,
    shutdown_output_tokens,
    total_output_tokens,
)
from copilot_usage.pricing import lookup_model_pricing
from copilot_usage.render_detail import (
    _build_event_details as _build_event_details,  # pyright: ignore[reportPrivateUsage]
    _event_type_label as _event_type_label,  # pyright: ignore[reportPrivateUsage]
    _extract_tool_name as _extract_tool_name,  # pyright: ignore[reportPrivateUsage]
    _format_detail_duration as _format_detail_duration,  # pyright: ignore[reportPrivateUsage]
    _format_relative_time as _format_relative_time,  # pyright: ignore[reportPrivateUsage]
    _render_aggregate_stats as _render_aggregate_stats,  # pyright: ignore[reportPrivateUsage]
    _render_code_changes as _render_code_changes,  # pyright: ignore[reportPrivateUsage]
    _render_header as _render_header,  # pyright: ignore[reportPrivateUsage]
    _render_recent_events as _render_recent_events,  # pyright: ignore[reportPrivateUsage]
    _render_shutdown_cycles as _render_shutdown_cycles,  # pyright: ignore[reportPrivateUsage]
    _safe_event_data as _safe_event_data,  # pyright: ignore[reportPrivateUsage]
    _truncate as _truncate,  # pyright: ignore[reportPrivateUsage]
    render_session_detail as render_session_detail,
)

# Backward-compatible private aliases so existing internal call-sites and
# tests that import the underscore-prefixed names keep working.
_shutdown_output_tokens = shutdown_output_tokens
_total_output_tokens = total_output_tokens
_has_active_period_stats = has_active_period_stats

__all__ = [
    "format_duration",
    "format_tokens",
    "render_cost_view",
    "render_full_summary",
    "render_live_sessions",
    "render_session_detail",
    "render_summary",
    "session_display_name",
]


def session_display_name(session: SessionSummary) -> str:
    """Return session name, falling back to first 12 chars of ID, then "(no id)"."""
    return session.name or session.session_id[:12] or "(no id)"


def _format_elapsed_since(start: datetime) -> str:
    """Return a human-readable elapsed time from *start* to now.

    Formats using :func:`_format_timedelta` for consistent output.
    """
    delta = datetime.now(tz=UTC) - ensure_aware(start)
    return _format_timedelta(delta)


@dataclass(frozen=True)
class _EffectiveStats:
    """Active-period stats when available, otherwise session totals."""

    model_calls: int
    user_messages: int
    output_tokens: int


def _effective_stats(session: SessionSummary) -> _EffectiveStats:
    """Return active-period stats if available, otherwise session totals."""
    if _has_active_period_stats(session):
        return _EffectiveStats(
            model_calls=session.active_model_calls,
            user_messages=session.active_user_messages,
            output_tokens=session.active_output_tokens,
        )
    return _EffectiveStats(
        model_calls=session.model_calls,
        user_messages=session.user_messages,
        output_tokens=_total_output_tokens(session),
    )


@dataclass(frozen=True)
class _SessionTotals:
    """Aggregated totals across a list of sessions."""

    premium: int
    model_calls: int
    user_messages: int
    api_duration_ms: int
    output_tokens: int
    session_count: int


def _compute_session_totals(
    sessions: list[SessionSummary],
    *,
    token_fn: Callable[[SessionSummary], int] = _total_output_tokens,
) -> _SessionTotals:
    """Compute aggregated totals across *sessions*.

    *token_fn* controls how output tokens are counted per session.  Defaults
    to :func:`_total_output_tokens` (includes active tokens for resumed
    sessions).  Pass :func:`_shutdown_output_tokens` for shutdown-only views.
    """
    return _SessionTotals(
        premium=sum(s.total_premium_requests for s in sessions),
        model_calls=sum(s.model_calls for s in sessions),
        user_messages=sum(s.user_messages for s in sessions),
        api_duration_ms=sum(s.total_api_duration_ms for s in sessions),
        output_tokens=sum(token_fn(s) for s in sessions),
        session_count=len(sessions),
    )


def _estimate_premium_cost(model: str | None, calls: int) -> str:
    """Return a ``~``-prefixed estimated premium cost string.

    Uses :func:`lookup_model_pricing` to look up the multiplier for *model*
    and multiplies by *calls*.  Returns ``"—"`` when *model* is ``None``.

    Warnings from :func:`lookup_model_pricing` (e.g. unknown models) are
    suppressed so that normal CLI rendering never emits noise on stderr.
    """
    if model is None:
        return "—"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        pricing = lookup_model_pricing(model)
    cost = round(calls * pricing.multiplier)
    return f"~{cost}"


def _format_session_running_time(session: SessionSummary) -> str:
    """Return a human-readable running time for *session*.

    Returns ``"—"`` when the session has no ``start_time``.
    """
    if not session.start_time:
        return "—"
    return _format_elapsed_since(session.last_resume_time or session.start_time)


def render_live_sessions(
    sessions: list[SessionSummary],
    *,
    target_console: Console | None = None,
) -> None:
    """Render overview of active sessions only.

    Filters to ``is_active=True`` sessions.
    Shows running time as ``Xh Ym`` or ``Ym Zs``.
    """
    console = target_console or Console()

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
    table.add_column("Est. Cost", style="green", justify="right")
    table.add_column("Output Tokens", style="red", justify="right")
    table.add_column("CWD", style="dim")

    for s in active:
        short_id = s.session_id[:8] if s.session_id else "—"
        name = s.name or "—"
        model = s.model or "—"
        running = _format_session_running_time(s)

        stats = _effective_stats(s)
        messages = str(stats.user_messages)
        est_cost = _estimate_premium_cost(s.model, stats.model_calls)
        tokens = format_tokens(stats.output_tokens)
        cwd = s.cwd or "—"

        table.add_row(
            f"🟢 {short_id}",
            name,
            model,
            running,
            messages,
            est_cost,
            tokens,
            cwd,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------


def _filter_sessions(
    sessions: list[SessionSummary],
    since: datetime | None,
    until: datetime | None,
) -> list[SessionSummary]:
    """Return sessions whose start_time falls within [since, until]."""
    if since is not None and until is not None and since > until:
        warnings.warn(
            f"--since ({since.date()}) is after --until ({until.date()}); "
            "no sessions will match.",
            UserWarning,
            stacklevel=2,
        )
        return []

    if since is None and until is None:
        return sessions

    filtered: list[SessionSummary] = []
    for s in sessions:
        if s.start_time is None:
            continue
        aware_start = ensure_aware(s.start_time)
        if since is not None and aware_start < since:
            continue
        if until is not None and aware_start > until:
            continue
        filtered.append(s)
    return filtered


def _aggregate_model_metrics(
    sessions: list[SessionSummary],
) -> dict[str, ModelMetrics]:
    """Merge model metrics across all sessions into a single dict."""
    merged: dict[str, ModelMetrics] = {}
    for s in sessions:
        merged = merge_model_metrics(merged, s.model_metrics)
    return merged


def _render_summary_header(
    console: Console,
    sessions: list[SessionSummary],
) -> None:
    """Print the report header with date range.

    Must only be called with a non-empty *sessions* list — both
    call-sites (``render_summary`` and ``render_full_summary``) guard
    against the empty case before reaching here.
    """
    assert sessions, "_render_summary_header requires non-empty sessions"  # noqa: S101
    start_times = [
        ensure_aware(s.start_time) for s in sessions if s.start_time is not None
    ]
    if start_times:
        earliest = min(start_times).strftime("%Y-%m-%d")
        latest = max(start_times).strftime("%Y-%m-%d")
        subtitle = f"{earliest}  →  {latest}"
    else:
        subtitle = "dates unavailable"
    console.print()
    console.print(
        Text("Copilot Usage Summary", style="bold cyan"),
        Text(f"  ({subtitle})", style="dim"),
    )
    console.print()


def _render_totals(console: Console, sessions: list[SessionSummary]) -> None:
    """Render the totals panel."""
    totals = _compute_session_totals(sessions)

    pr_label = "premium request" if totals.premium == 1 else "premium requests"
    session_label = "session" if totals.session_count == 1 else "sessions"
    lines = [
        f"[green]{totals.premium}[/green] {pr_label}   "
        f"[green]{totals.model_calls}[/green] model calls   "
        f"[green]{totals.user_messages}[/green] user messages   "
        f"[green]{format_tokens(totals.output_tokens)}[/green] output tokens",
        f"[green]{format_duration(totals.api_duration_ms)}[/green] API duration   "
        f"[green]{totals.session_count}[/green] {session_label}",
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
    table.add_column("Cache Write", justify="right")

    for model_name in sorted(merged):
        mm = merged[model_name]
        table.add_row(
            model_name,
            str(mm.requests.count),
            str(mm.requests.cost),
            format_tokens(mm.usage.inputTokens),
            format_tokens(mm.usage.outputTokens),
            format_tokens(mm.usage.cacheReadTokens),
            format_tokens(mm.usage.cacheWriteTokens),
        )

    console.print(table)


def _render_session_table(
    console: Console,
    sessions: list[SessionSummary],
    *,
    title: str = "Sessions",
    include_active_tokens: bool = True,
) -> None:
    """Render the per-session table sorted by start time (newest first).

    When *include_active_tokens* is ``False`` the table uses
    :func:`_shutdown_output_tokens` so that only shutdown-derived metrics
    appear (appropriate for historical / "Shutdown Data" views).
    """
    if not sessions:
        return

    sorted_sessions = sorted(
        sessions,
        key=lambda s: ensure_aware(s.start_time) if s.start_time is not None else EPOCH,
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
        name = session_display_name(s)
        model = s.model or "—"

        token_fn = (
            _total_output_tokens if include_active_tokens else _shutdown_output_tokens
        )
        output_tokens = token_fn(s)

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
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    target_console: Console | None = None,
) -> None:
    """Render the full summary report to the terminal using Rich.

    Filters sessions by date range when *since* and/or *until* are given.
    """
    console = target_console or Console()
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
    # Include all completed (non-active) sessions so they are never silently
    # invisible.  Previously, zero-metrics completed sessions were excluded,
    # causing a count mismatch with ``render_summary()``.
    # Also include any session with shutdown model_metrics
    # (has_shutdown_metrics), which covers resumed sessions that used only
    # free-tier models (total_premium_requests == 0).
    historical = [
        s
        for s in sessions
        if s.total_premium_requests > 0 or not s.is_active or s.has_shutdown_metrics
    ]

    if not historical:
        console.print("[dim]No historical shutdown data.[/dim]")
        return

    # Totals panel — shutdown-only tokens for the historical view
    totals = _compute_session_totals(historical, token_fn=_shutdown_output_tokens)

    lines = [
        f"[green]{totals.premium}[/green] premium requests   "
        f"[green]{totals.model_calls}[/green] model calls   "
        f"[green]{totals.user_messages}[/green] user messages   "
        f"[green]{format_tokens(totals.output_tokens)}[/green] output tokens",
        f"[green]{format_duration(totals.api_duration_ms)}[/green] API duration",
    ]
    console.print(
        Panel("\n".join(lines), title="📊 Historical Totals", border_style="cyan")
    )

    # Per-model table
    _render_model_table(console, historical)

    # Per-session table — shutdown-only tokens
    _render_session_table(
        console,
        historical,
        title="Sessions (Shutdown Data)",
        include_active_tokens=False,
    )


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
        name = session_display_name(s)
        model = s.model or "—"
        running = _format_session_running_time(s)

        stats = _effective_stats(s)
        model_calls = str(stats.model_calls)
        user_msgs = str(stats.user_messages)
        output_tokens = format_tokens(stats.output_tokens)

        table.add_row(
            name,
            model,
            model_calls,
            user_msgs,
            output_tokens,
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
    For active sessions with shutdown metrics, appends a
    "↳ Since last shutdown" row with an estimated premium cost and the
    active model calls / output tokens.
    """
    console = target_console or Console()
    filtered = _filter_sessions(sessions, since, until)

    if not filtered:
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

    for s in filtered:
        name = session_display_name(s)
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
        grand_output += _total_output_tokens(s)

        if s.is_active and s.has_shutdown_metrics:
            cost_stats = _effective_stats(s)
            cost_calls = cost_stats.model_calls
            cost_tokens = cost_stats.output_tokens
            est = _estimate_premium_cost(s.model, cost_calls)
            table.add_row(
                "  ↳ Since last shutdown",
                s.model or "—",
                "N/A",
                est,
                str(cost_calls),
                format_tokens(cost_tokens),
            )

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
