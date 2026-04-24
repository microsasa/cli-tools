"""Rendering helpers for Copilot CLI session data.

Uses Rich tables and panels to display session information in
the terminal.

Session-detail rendering (``render_session_detail`` and its private
helpers) lives in :mod:`copilot_usage.render_detail`; only the public
entry-point is re-exported here so that external callers see no change.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from copilot_usage._formatting import (
    format_duration,
    format_timedelta,
    format_tokens,
)
from copilot_usage.models import (
    ModelMetrics,
    SessionSummary,
    add_to_model_metrics,
    copy_model_metrics,
    ensure_aware,
    has_active_period_stats,
    session_sort_key,
    shutdown_output_tokens,
    total_output_tokens,
)
from copilot_usage.pricing import lookup_model_pricing
from copilot_usage.render_detail import render_session_detail

__all__: Final[list[str]] = [
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

    Formats using :func:`format_timedelta` for consistent output.
    """
    delta = datetime.now(tz=UTC) - ensure_aware(start)
    return format_timedelta(delta)


@dataclass(frozen=True, slots=True)
class _EffectiveStats:
    """Active-period stats when available, otherwise session totals."""

    model_calls: int
    user_messages: int
    output_tokens: int


def _effective_stats(session: SessionSummary) -> _EffectiveStats:
    """Return active-period stats if available, otherwise session totals."""
    if has_active_period_stats(session):
        return _EffectiveStats(
            model_calls=session.active_model_calls,
            user_messages=session.active_user_messages,
            output_tokens=session.active_output_tokens,
        )
    return _EffectiveStats(
        model_calls=session.model_calls,
        user_messages=session.user_messages,
        output_tokens=total_output_tokens(session),
    )


@dataclass(frozen=True, slots=True)
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
    token_fn: Callable[[SessionSummary], int] = total_output_tokens,
    shutdown_only: bool = False,
) -> _SessionTotals:
    """Compute aggregated totals across *sessions* in a single pass.

    *token_fn* controls how output tokens are counted per session.  Defaults
    to :func:`total_output_tokens` (includes active tokens for resumed
    sessions).  Pass :func:`shutdown_output_tokens` for shutdown-only views.

    When *shutdown_only* is ``True``, model-call and user-message counts are
    reduced to shutdown-period values for resumed sessions that have both
    shutdown metrics and active-period stats.
    """
    premium = model_calls = user_messages = api_duration_ms = output_tokens = 0
    for s in sessions:
        premium += s.total_premium_requests

        if shutdown_only and s.has_shutdown_metrics and has_active_period_stats(s):
            model_calls += s.model_calls - s.active_model_calls
            user_messages += s.user_messages - s.active_user_messages
        else:
            model_calls += s.model_calls
            user_messages += s.user_messages

        api_duration_ms += s.total_api_duration_ms
        output_tokens += token_fn(s)
    return _SessionTotals(
        premium=premium,
        model_calls=model_calls,
        user_messages=user_messages,
        api_duration_ms=api_duration_ms,
        output_tokens=output_tokens,
        session_count=len(sessions),
    )


def _estimate_premium_cost(model: str | None, calls: int) -> str:
    """Return a ``~``-prefixed estimated premium cost string.

    Uses :func:`lookup_model_pricing` to look up the multiplier for *model*
    and multiplies by *calls*.  Returns ``"—"`` when *model* is ``None``.

    """
    if model is None:
        return "—"
    pricing = lookup_model_pricing(model)
    cost = round(calls * pricing.multiplier)
    return f"~{cost}"


def _format_session_running_time(session: SessionSummary) -> str:
    """Return a human-readable running time for *session*.

    Returns ``"—"`` when the session has no ``start_time``.
    """
    if session.start_time is None:
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
    """Return sessions whose start_time falls within [since, until].

    *sessions* must be sorted newest-first (descending ``start_time``).
    When *since* is provided the loop breaks on the first session older
    than the threshold, so unsorted input will produce incorrect results.
    """
    if since is not None and until is not None and since > until:
        return []

    if since is None and until is None:
        return sessions

    filtered: list[SessionSummary] = []
    for s in sessions:
        if s.start_time is None:
            continue
        aware_start = ensure_aware(s.start_time)
        # Sessions are sorted newest-first; once aware_start < since, all
        # remaining sessions are even older and can never match.
        if since is not None and aware_start < since:
            break
        if until is not None and aware_start > until:
            continue
        filtered.append(s)
    return filtered


def _aggregate_model_metrics(
    sessions: list[SessionSummary],
) -> dict[str, ModelMetrics]:
    """Merge model metrics across all sessions into a single dict.

    Accumulates in-place so each unique model name is copied at most once,
    reducing copy overhead from O(n × m) to O(m).
    """
    result: dict[str, ModelMetrics] = {}
    for s in sessions:
        for model_name, mm in s.model_metrics.items():
            if model_name in result:
                add_to_model_metrics(result[model_name], mm)
            else:
                result[model_name] = copy_model_metrics(mm)
    return result


def _render_summary_header(
    console: Console,
    sessions: list[SessionSummary],
) -> None:
    """Print the report header with date range.

    *sessions* must be non-empty — callers are responsible for the
    empty-list check.

    Exploits the pre-sorted order (newest-first, ``None``-start-time
    entries last) guaranteed by :func:`~copilot_usage.parser.get_all_sessions`
    to find the date range while doing only O(1) ``ensure_aware`` conversions
    and, in the worst case, scanning over at most the trailing ``None``
    ``start_time`` entries instead of all sessions.
    """
    latest: datetime | None = None
    earliest: datetime | None = None

    # Find the first non-None start_time (latest session, newest-first order).
    first_idx: int | None = None
    for idx, session in enumerate(sessions):
        if session.start_time is not None:
            latest = ensure_aware(session.start_time)
            first_idx = idx
            break

    # If no session has a start_time, skip the reverse scan entirely.
    if latest is not None and first_idx is not None:
        # Find the last non-None start_time (earliest session, scanning from end).
        for rev_idx, session in enumerate(reversed(sessions)):
            if session.start_time is not None:
                last_idx = len(sessions) - 1 - rev_idx
                if last_idx == first_idx:
                    # Single-session range: reuse the already-converted datetime.
                    earliest = latest
                else:
                    # start_time proven non-None by the guard above.
                    earliest = ensure_aware(session.start_time)
                break

    if earliest is not None and latest is not None:
        subtitle = f"{earliest.strftime('%Y-%m-%d')}  →  {latest.strftime('%Y-%m-%d')}"
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
    token_fn: Callable[[SessionSummary], int] = total_output_tokens,
    pre_sorted: bool = True,
    shutdown_only: bool = False,
) -> None:
    """Render the per-session table ordered by start time (newest first).

    *token_fn* controls how output tokens are counted per session.  Defaults
    to :func:`total_output_tokens` (includes active tokens for resumed
    sessions).  Pass :func:`shutdown_output_tokens` for shutdown-only views.

    When *shutdown_only* is ``True``, model-call and user-message counts are
    reduced to shutdown-period values for resumed sessions (mirroring the
    logic in :func:`render_cost_view`).

    When *pre_sorted* is ``True`` (the default), the input is assumed to
    already be in descending ``start_time`` order — the contract guaranteed
    by :func:`~copilot_usage.parser.get_all_sessions` — and no sort is
    performed.  Set to ``False`` to sort explicitly when calling with
    unsorted data.
    """
    if not sessions:
        return

    ordered: list[SessionSummary] = (
        sessions if pre_sorted else sorted(sessions, key=session_sort_key, reverse=True)
    )

    table = Table(title=title, border_style="cyan")
    table.add_column("Name", style="bold", max_width=40)
    table.add_column("Model")
    table.add_column("Premium", justify="right")
    table.add_column("Model Calls", justify="right")
    table.add_column("User Msgs", justify="right")
    table.add_column("Output Tokens", justify="right")
    table.add_column("Status")

    for s in ordered:
        name = session_display_name(s)
        model = s.model or "—"

        output_tokens = token_fn(s)

        if shutdown_only and s.has_shutdown_metrics and has_active_period_stats(s):
            displayed_calls = s.model_calls - s.active_model_calls
            displayed_msgs = s.user_messages - s.active_user_messages
        else:
            displayed_calls = s.model_calls
            displayed_msgs = s.user_messages

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
            str(displayed_calls),
            str(displayed_msgs),
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

    *sessions* must be in descending ``start_time`` order — the contract
    guaranteed by :func:`~copilot_usage.parser.get_all_sessions`.  No
    re-sorting is performed.
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


def _render_historical_section_from(
    console: Console,
    historical: list[SessionSummary],
) -> None:
    """Render Section 1: Historical Data from a pre-partitioned list.

    *historical* must already contain the relevant sessions (those with
    ``total_premium_requests > 0``, or not active, or with shutdown
    metrics).  No filtering is performed here.
    """
    if not historical:
        console.print("[dim]No historical shutdown data.[/dim]")
        return

    # Totals panel — shutdown-only tokens and counts for the historical view
    totals = _compute_session_totals(
        historical, token_fn=shutdown_output_tokens, shutdown_only=True
    )

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

    # Per-session table — shutdown-only tokens and counts
    _render_session_table(
        console,
        historical,
        title="Sessions (Shutdown Data)",
        token_fn=shutdown_output_tokens,
        shutdown_only=True,
    )


def _render_active_section_from(
    console: Console,
    active: list[SessionSummary],
) -> None:
    """Render Section 2: Active Sessions from a pre-partitioned list.

    *active* must already contain only sessions where ``is_active`` is
    ``True``.  No filtering is performed here.

    The table title includes "Since Last Shutdown" only when at least one
    session has prior shutdown data (``has_shutdown_metrics=True``).
    """
    if not active:
        console.print(
            Panel(
                "No active sessions", title="🟢 Active Sessions", border_style="green"
            )
        )
        return

    has_resumed = any(s.has_shutdown_metrics for s in active)
    title = (
        "🟢 Active Sessions (Since Last Shutdown)"
        if has_resumed
        else "🟢 Active Sessions"
    )
    table = Table(title=title, border_style="green")
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

    *sessions* must be in descending ``start_time`` order — the contract
    guaranteed by :func:`~copilot_usage.parser.get_all_sessions`.  No
    re-sorting is performed.
    """
    console = target_console or Console()

    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    # Single pass: partition into historical and active sub-lists.
    historical: list[SessionSummary] = []
    active: list[SessionSummary] = []
    for s in sessions:
        if s.total_premium_requests > 0 or not s.is_active or s.has_shutdown_metrics:
            historical.append(s)
        if s.is_active:
            active.append(s)

    _render_summary_header(console, sessions)
    _render_historical_section_from(console, historical)
    console.print()
    _render_active_section_from(console, active)


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
    For active sessions with shutdown metrics **and** meaningful
    active-period stats (``has_active_period_stats``), appends a
    "↳ Since last shutdown" row with an estimated premium cost and the
    active model calls / output tokens.  When ``has_active_period_stats``
    is ``False`` (all active counters are 0 and ``last_resume_time`` is
    ``None``), the row is suppressed to avoid misleadingly attributing
    session totals to the post-shutdown period.

    *sessions* must be in descending ``start_time`` order — the contract
    guaranteed by :func:`~copilot_usage.parser.get_all_sessions`.  No
    re-sorting is performed.
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
        # For sessions with shutdown metrics and active-period stats,
        # show shutdown-only model calls so this column aligns with the
        # shutdown-only model_metrics data. Otherwise, display total calls.
        if s.has_shutdown_metrics and has_active_period_stats(s):
            shutdown_model_calls = s.model_calls - s.active_model_calls
            model_calls_display = str(shutdown_model_calls)
        else:
            model_calls_display = str(s.model_calls)

        session_output = 0
        if s.model_metrics:
            # Pure-active sessions (still running, no prior shutdown) have
            # synthetic zeros in requests/cost — display "—" instead.
            show_requests = s.has_shutdown_metrics or not s.is_active
            for model_name in sorted(s.model_metrics):
                mm = s.model_metrics[model_name]
                session_output += mm.usage.outputTokens
                requests_display = str(mm.requests.count) if show_requests else "—"
                premium_display = str(mm.requests.cost) if show_requests else "—"
                table.add_row(
                    name,
                    model_name,
                    requests_display,
                    premium_display,
                    model_calls_display,
                    format_tokens(mm.usage.outputTokens),
                )
                if show_requests:
                    grand_requests += mm.requests.count
                    grand_premium += mm.requests.cost
                # Only show session-level info once
                name = ""
                model_calls_display = ""
            # For resumed sessions add post-shutdown active tokens
            if has_active_period_stats(s) and s.has_shutdown_metrics:
                session_output += s.active_output_tokens
        else:
            session_output = total_output_tokens(s)
            table.add_row(
                name,
                s.model or "—",
                "—",
                "—",
                str(s.model_calls),
                format_tokens(session_output) if session_output else "—",
            )

        grand_output += session_output
        grand_model_calls += s.model_calls

        # For active sessions, append a shutdown-relative summary row only
        # when shutdown metrics are available and active-period stats exist.
        if s.is_active and s.has_shutdown_metrics and has_active_period_stats(s):
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
