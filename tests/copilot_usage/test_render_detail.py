"""Tests for copilot_usage.render_detail — private helper coverage (issue #470, #562)."""

# pyright: reportPrivateUsage=false

import io
import re
from datetime import UTC, datetime

import pytest
from rich.console import Console

from copilot_usage.models import (
    CodeChanges,
    EventType,
    SessionEvent,
    SessionSummary,
    ToolExecutionData,
    ToolTelemetry,
)
from copilot_usage.render_detail import (
    _extract_tool_name,
    _render_code_changes,
    _render_recent_events,
    _render_shutdown_cycles,
    render_session_detail,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so assertions match visible text only."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# _extract_tool_name — all branches
# ---------------------------------------------------------------------------


class TestExtractToolName:
    """Parametrized test covering every branch of _extract_tool_name."""

    @pytest.mark.parametrize(
        ("telemetry", "expected"),
        [
            pytest.param(None, "", id="telemetry-none"),
            pytest.param(ToolTelemetry(properties={}), "", id="properties-empty"),
            pytest.param(
                ToolTelemetry(properties={"outcome": "done"}), "", id="key-absent"
            ),
            pytest.param(
                ToolTelemetry(properties={"tool_name": "read_file"}),
                "read_file",
                id="key-present",
            ),
        ],
    )
    def test_extract_tool_name(
        self, telemetry: ToolTelemetry | None, expected: str
    ) -> None:
        data = ToolExecutionData(
            toolCallId="x", model="m", interactionId="i", toolTelemetry=telemetry
        )
        assert _extract_tool_name(data) == expected


# ---------------------------------------------------------------------------
# _render_code_changes — all branches
# ---------------------------------------------------------------------------


class TestRenderCodeChanges:
    """Tests for _render_code_changes covering None, all-zero, and with-data."""

    def test_none_produces_no_output(self) -> None:
        """code_changes=None → returns immediately without printing."""
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)
        _render_code_changes(None, target_console=console)
        assert buf.getvalue() == ""

    def test_all_zero_produces_no_output(self) -> None:
        """All fields zero/empty → returns without printing."""
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)
        changes = CodeChanges(linesAdded=0, linesRemoved=0, filesModified=[])
        _render_code_changes(changes, target_console=console)
        assert buf.getvalue() == ""

    def test_with_data_shows_table(self) -> None:
        """Non-zero code changes → renders a table with stats."""
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)
        changes = CodeChanges(linesAdded=10, linesRemoved=2, filesModified=["a.py"])
        _render_code_changes(changes, target_console=console)
        output = buf.getvalue()
        assert "Files modified" in output
        assert "+10" in output
        assert "-2" in output


# ---------------------------------------------------------------------------
# Helper to build a buffered console for test assertions
# ---------------------------------------------------------------------------


def _buf_console() -> tuple[io.StringIO, Console]:
    buf = io.StringIO()
    return buf, Console(file=buf, force_terminal=True, width=120)


# ---------------------------------------------------------------------------
# Gap 2 — _render_recent_events with timestamp=None (issue #562)
# ---------------------------------------------------------------------------


class TestRenderRecentEventsTimestampNone:
    """Events with timestamp=None must produce '—' in the Time column."""

    def test_event_without_timestamp_shows_dash(self) -> None:
        """Events with timestamp=None must produce '—' in the Time column."""
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={"content": "hi"})
        assert ev.timestamp is None

        buf, console = _buf_console()
        _render_recent_events(
            [ev],
            session_start=datetime(2026, 3, 7, 10, 0, 0, tzinfo=UTC),
            target_console=console,
        )
        output = buf.getvalue()
        assert "—" in output


# ---------------------------------------------------------------------------
# Gap 1 — render_session_detail fallback when both timestamps are None (#562)
# ---------------------------------------------------------------------------


class TestRenderSessionDetailBothTimestampsNone:
    """render_session_detail must not raise when start_time and all
    event timestamps are None (falls back to datetime.now)."""

    def test_no_start_time_no_event_timestamps_does_not_raise(self) -> None:
        """render_session_detail must not raise when start_time and all
        event timestamps are None (falls back to datetime.now).
        """
        summary = SessionSummary(session_id="fallback-test", start_time=None)
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={"content": "hi"})
        assert ev.timestamp is None

        buf, console = _buf_console()
        render_session_detail([ev], summary, target_console=console)
        # Rendered without error; at minimum the header must appear
        assert "Session Detail" in buf.getvalue()


# ---------------------------------------------------------------------------
# Gap 3 — _render_shutdown_cycles with empty modelMetrics (issue #562)
# ---------------------------------------------------------------------------


class TestRenderShutdownCyclesEmptyModelMetrics:
    """A SESSION_SHUTDOWN event with empty modelMetrics must not crash
    and must produce a table row with zero counts."""

    def test_shutdown_with_empty_metrics_renders_row(self) -> None:
        """A SESSION_SHUTDOWN event with empty modelMetrics must not crash
        and must produce a table row with zero counts.
        """
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            timestamp=datetime(2026, 3, 7, 11, 0, 0, tzinfo=UTC),
            data={
                "shutdownType": "normal",
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
                "modelMetrics": {},
            },
        )
        buf, console = _buf_console()
        _render_shutdown_cycles([ev], target_console=console)
        output = buf.getvalue()
        assert "Shutdown Cycles" in output
        # totals from empty modelMetrics → 0
        assert "0" in output


# ---------------------------------------------------------------------------
# Gap 1 — _render_shutdown_cycles multi-model per-cycle aggregation (#622)
# ---------------------------------------------------------------------------


class TestRenderShutdownCyclesMultiModelAggregation:
    """Multi-model modelMetrics must be summed for Model Calls and
    Output Tokens columns in the Shutdown Cycles table."""

    def test_multi_model_sums_model_calls_and_output_tokens(self) -> None:
        """Two models in one shutdown event → totals are summed."""
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            data={
                "shutdownType": "normal",
                "totalPremiumRequests": 10,
                "totalApiDurationMs": 60_000,
                "modelMetrics": {
                    "claude-sonnet-4": {
                        "requests": {"count": 3, "cost": 7},
                        "usage": {"outputTokens": 500},
                    },
                    "claude-haiku-4.5": {
                        "requests": {"count": 4, "cost": 3},
                        "usage": {"outputTokens": 300},
                    },
                },
            },
        )
        buf, console = _buf_console()
        _render_shutdown_cycles([ev], target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Shutdown Cycles" in output
        # Assert against the shutdown-cycle row (contains timestamp)
        row = next(line for line in output.splitlines() if "2025-01-01 00:00" in line)
        assert "7" in row  # total model calls = 3 + 4
        assert "800" in row  # total output tokens = 500 + 300


# ---------------------------------------------------------------------------
# Gap 2 — Multi-model aggregation via render_session_detail end-to-end (#622)
# ---------------------------------------------------------------------------


class TestRenderSessionDetailMultiModelShutdown:
    """Multi-model shutdown totals must propagate through
    render_session_detail end-to-end."""

    def test_multi_model_shutdown_via_full_render(self) -> None:
        """render_session_detail must show summed model calls and
        output tokens for a multi-model shutdown event."""
        summary = SessionSummary(
            session_id="multi-model-e2e",
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            is_active=False,
        )
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            timestamp=datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC),
            data={
                "shutdownType": "normal",
                "totalPremiumRequests": 10,
                "totalApiDurationMs": 60_000,
                "modelMetrics": {
                    "claude-sonnet-4": {
                        "requests": {"count": 3, "cost": 7},
                        "usage": {"outputTokens": 500},
                    },
                    "claude-haiku-4.5": {
                        "requests": {"count": 4, "cost": 3},
                        "usage": {"outputTokens": 300},
                    },
                },
            },
        )
        buf, console = _buf_console()
        render_session_detail([ev], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Shutdown Cycles" in output
        # Assert against the shutdown-cycle row (contains timestamp)
        row = next(line for line in output.splitlines() if "2025-01-01 01:00" in line)
        assert "7" in row  # total model calls = 3 + 4
        assert "800" in row  # total output tokens = 500 + 300
