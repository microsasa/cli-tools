"""Rendering for VS Code Copilot Chat usage data."""

from typing import Final

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from copilot_usage._formatting import format_duration
from copilot_usage.pricing import lookup_model_pricing
from copilot_usage.vscode_parser import VSCodeLogSummary

__all__: Final[list[str]] = ["render_vscode_summary"]

_DAILY_ACTIVITY_LIMIT: Final[int] = 14


def _format_log_files_line(summary: VSCodeLogSummary) -> str:
    """Build the 'Log Files' line, surfacing unreadable or inconsistent counts."""
    found = summary.log_files_found
    parsed = summary.log_files_parsed
    unreadable = found - parsed

    if unreadable > 0:
        return (
            f"[bold]Log Files:[/bold]  {parsed}"
            f" ({found} found, "
            f"[red]{unreadable} unreadable[/red])"
        )
    if unreadable < 0:
        return (
            f"[bold]Log Files:[/bold]  {parsed}"
            f" ([yellow]{found} found; inconsistent counts[/yellow])"
        )
    return f"[bold]Log Files:[/bold]  {parsed}"


def render_vscode_summary(
    summary: VSCodeLogSummary, target_console: Console | None = None
) -> None:
    """Render VS Code Copilot Chat usage summary to the console."""
    console = target_console or Console()

    # --- Totals panel ---
    date_range = "—"
    if summary.first_timestamp and summary.last_timestamp:
        first = summary.first_timestamp.strftime("%Y-%m-%d %H:%M")
        last = summary.last_timestamp.strftime("%Y-%m-%d %H:%M")
        date_range = f"{first}  →  {last}"

    log_files_line = _format_log_files_line(summary)

    lines = [
        f"[bold]Requests:[/bold]   [green]{summary.total_requests:,}[/green]",
        f"[bold]API Time:[/bold]   [green]{format_duration(summary.total_duration_ms)}[/green]",
        f"[bold]Date Range:[/bold] {date_range}",
        log_files_line,
    ]
    console.print(
        Panel("\n".join(lines), title="VS Code Copilot Chat", border_style="cyan")
    )

    # --- Per-model table ---
    if summary.requests_by_model:
        table = Table(title="Per-Model Breakdown", border_style="cyan")
        table.add_column("Model", style="bold")
        table.add_column("Tier", style="dim")
        table.add_column("Requests", justify="right")
        table.add_column("Avg Duration", justify="right")
        table.add_column("Total Duration", justify="right")

        for model in sorted(
            summary.requests_by_model,
            key=lambda m: summary.requests_by_model[m],
            reverse=True,
        ):
            count = summary.requests_by_model[model]
            total_ms = summary.duration_by_model.get(model, 0)
            avg_ms = total_ms // count if count else 0
            pricing = lookup_model_pricing(model)
            table.add_row(
                model,
                pricing.tier.value,
                f"{count:,}",
                f"{avg_ms:,}ms",
                format_duration(total_ms),
            )

        console.print(table)

    # --- By-feature table ---
    if summary.requests_by_category:
        table = Table(title="By Feature", border_style="cyan")
        table.add_column("Category", style="bold")
        table.add_column("Requests", justify="right")
        table.add_column("% of Total", justify="right")

        for category in sorted(
            summary.requests_by_category,
            key=lambda c: summary.requests_by_category[c],
            reverse=True,
        ):
            count = summary.requests_by_category[category]
            pct = count / summary.total_requests * 100 if summary.total_requests else 0
            table.add_row(category, f"{count:,}", f"{pct:.1f}%")

        console.print(table)

    # --- Daily activity (last 14 days) ---
    if summary.requests_by_date:
        table = Table(title="Daily Activity", border_style="cyan")
        table.add_column("Date", style="bold")
        table.add_column("Requests", justify="right")

        dates = sorted(summary.requests_by_date.keys(), reverse=True)[
            :_DAILY_ACTIVITY_LIMIT
        ]
        for date_str in dates:
            count = summary.requests_by_date[date_str]
            table.add_row(date_str, f"{count:,}")

        console.print(table)
