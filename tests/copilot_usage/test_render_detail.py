"""Tests for copilot_usage.render_detail — private helper coverage (issue #470, #562)."""

# pyright: reportPrivateUsage=false

import io
import re
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from copilot_usage.models import (
    CodeChanges,
    EventType,
    ModelMetrics,
    RequestMetrics,
    SessionEvent,
    SessionShutdownData,
    SessionSummary,
    TokenUsage,
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

    def test_aggregated_code_changes_rendered_correctly(self) -> None:
        """Code Changes panel reflects aggregated totals from multiple cycles.

        Simulates two shutdown cycles whose CodeChanges have been aggregated
        by ``_build_completed_summary`` (sum of lines, union of files) and
        verifies that ``render_session_detail`` renders those aggregated totals.
        """
        aggregated = CodeChanges(
            linesAdded=70,  # e.g. 40 + 30
            linesRemoved=15,  # e.g. 5 + 10
            filesModified=["a.py", "b.py", "c.py"],
        )
        sd1 = SessionShutdownData(
            shutdownType="routine",
            totalPremiumRequests=3,
            totalApiDurationMs=2000,
            modelMetrics={},
        )
        sd2 = SessionShutdownData(
            shutdownType="routine",
            totalPremiumRequests=5,
            totalApiDurationMs=4000,
            modelMetrics={},
        )
        ts1 = datetime(2026, 3, 7, 10, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
        summary = SessionSummary(
            session_id="agg-cc",
            start_time=datetime(2026, 3, 7, 9, 0, 0, tzinfo=UTC),
            code_changes=aggregated,
            shutdown_cycles=[(ts1, sd1), (ts2, sd2)],
        )
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            timestamp=ts2,
            data={},
        )
        buf, console = _buf_console()
        render_session_detail([ev], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Code Changes" in output
        assert "+70" in output
        assert "-15" in output
        assert "Files modified" in output
        assert re.search(r"Files modified\s+│\s+3\b", output)


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
        sd = SessionShutdownData(
            shutdownType="normal",
            totalPremiumRequests=0,
            totalApiDurationMs=0,
            modelMetrics={},
        )
        summary = SessionSummary(
            session_id="empty-metrics",
            shutdown_cycles=[(datetime(2026, 3, 7, 11, 0, 0, tzinfo=UTC), sd)],
        )
        buf, console = _buf_console()
        _render_shutdown_cycles(summary, target_console=console)
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
        sd = SessionShutdownData(
            shutdownType="normal",
            totalPremiumRequests=10,
            totalApiDurationMs=60_000,
            modelMetrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=7),
                    usage=TokenUsage(outputTokens=500),
                ),
                "claude-haiku-4.5": ModelMetrics(
                    requests=RequestMetrics(count=4, cost=3),
                    usage=TokenUsage(outputTokens=300),
                ),
            },
        )
        summary = SessionSummary(
            session_id="multi-model",
            shutdown_cycles=[(datetime(2025, 1, 1, tzinfo=UTC), sd)],
        )
        buf, console = _buf_console()
        _render_shutdown_cycles(summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Shutdown Cycles" in output
        # Assert against the shutdown-cycle row (contains timestamp)
        row = next(line for line in output.splitlines() if "2025-01-01 00:00" in line)
        assert re.search(r"\b7\b", row)  # total model calls = 3 + 4
        assert re.search(r"\b800\b", row)  # total output tokens = 500 + 300


# ---------------------------------------------------------------------------
# Issue #635 — shutdown_cycles populated at build time, O(k) render
# ---------------------------------------------------------------------------


def _make_shutdown_data(premium: int = 1) -> SessionShutdownData:
    """Build a minimal SessionShutdownData for testing."""
    return SessionShutdownData(
        shutdownType="normal",
        totalPremiumRequests=premium,
        totalApiDurationMs=1000,
        modelMetrics={
            "test-model": ModelMetrics(
                requests=RequestMetrics(count=2, cost=1),
                usage=TokenUsage(outputTokens=100),
            ),
        },
    )


class TestShutdownCyclesPopulated:
    """Verify that shutdown_cycles is populated at build time and used
    by _render_shutdown_cycles without scanning the event list."""

    def test_build_session_summary_populates_shutdown_cycles(self) -> None:
        """build_session_summary produces a SessionSummary whose
        shutdown_cycles list matches the number of shutdown events."""
        from copilot_usage.parser import build_session_summary

        start = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
        n_filler = 5_000
        events: list[SessionEvent] = [
            SessionEvent(
                type=EventType.SESSION_START,
                data={
                    "sessionId": "perf-test",
                    "startTime": start.isoformat(),
                },
                timestamp=start,
            ),
        ]
        # Filler events (user messages) — should be ignored by shutdown_cycles
        events.extend(
            SessionEvent(
                type=EventType.USER_MESSAGE,
                data={"content": f"msg-{i}"},
                timestamp=start + timedelta(seconds=i + 1),
            )
            for i in range(n_filler)
        )
        # Three shutdown cycles
        for c in range(3):
            ts = start + timedelta(hours=c + 1)
            events.append(
                SessionEvent(
                    type=EventType.SESSION_SHUTDOWN,
                    timestamp=ts,
                    data={
                        "shutdownType": "normal",
                        "totalPremiumRequests": c + 1,
                        "totalApiDurationMs": 500 * (c + 1),
                        "modelMetrics": {
                            "test-model": {
                                "requests": {"count": c + 1, "cost": 1},
                                "usage": {"outputTokens": (c + 1) * 100},
                            },
                        },
                    },
                )
            )
        summary = build_session_summary(events)
        assert len(summary.shutdown_cycles) == 3
        # Timestamps must match the shutdown events
        for idx, (ts, sd) in enumerate(summary.shutdown_cycles):
            assert ts == start + timedelta(hours=idx + 1)
            assert sd.totalPremiumRequests == idx + 1

    def test_render_shutdown_cycles_uses_summary_not_events(self) -> None:
        """_render_shutdown_cycles reads summary.shutdown_cycles (O(k))
        and never iterates an event list."""
        cycles: list[tuple[datetime | None, SessionShutdownData]] = [
            (datetime(2025, 1, 1, h, 0, 0, tzinfo=UTC), _make_shutdown_data(h))
            for h in range(1, 4)
        ]
        summary = SessionSummary(
            session_id="direct-cycles",
            shutdown_cycles=cycles,
        )
        buf, console = _buf_console()
        _render_shutdown_cycles(summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Shutdown Cycles" in output
        # All three cycles should appear
        assert len(summary.shutdown_cycles) == 3
        for h in range(1, 4):
            assert f"2025-01-01 0{h}:00" in output

    def test_render_session_detail_shows_precomputed_cycles(self) -> None:
        """render_session_detail renders shutdown cycles from the summary
        even when the events list is empty."""
        sd = _make_shutdown_data(5)
        ts = datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)
        summary = SessionSummary(
            session_id="precomputed",
            start_time=datetime(2025, 3, 1, 11, 0, 0, tzinfo=UTC),
            is_active=False,
            shutdown_cycles=[(ts, sd)],
        )
        buf, console = _buf_console()
        render_session_detail([], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Shutdown Cycles" in output
        row = next(line for line in output.splitlines() if "2025-03-01 12:00" in line)
        assert re.search(r"\b5\b", row)  # premium requests

    def test_render_cycles_deterministic_no_event_scan(self) -> None:
        """Rendering pre-built shutdown_cycles produces valid output
        without requiring an event list, proving O(k) behaviour."""
        cycles: list[tuple[datetime | None, SessionShutdownData]] = [
            (datetime(2025, 1, 1, h, 0, 0, tzinfo=UTC), _make_shutdown_data(h))
            for h in range(1, 4)
        ]
        summary = SessionSummary(
            session_id="perf-bench",
            shutdown_cycles=cycles,
        )
        buf, console = _buf_console()

        _render_shutdown_cycles(summary, target_console=console)

        output = buf.getvalue()
        # Each cycle should appear in the rendered table
        for h in range(1, 4):
            assert f"2025-01-01 0{h}:00" in output


# ---------------------------------------------------------------------------
# Gap 2 — Multi-model aggregation via render_session_detail end-to-end (#622)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gap 3 — _render_recent_events with max_events <= 0 (issue #686)
# ---------------------------------------------------------------------------


class TestRenderRecentEventsNonPositiveMax:
    """max_events=0 or negative must print 'No events to display' and not
    render any event content — guarding against the ``events[-0:]`` quirk."""

    @pytest.mark.parametrize("max_events", [0, -1, -100])
    def test_render_recent_events_non_positive_max_shows_no_events(
        self, max_events: int
    ) -> None:
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={"content": "hi"})
        buf, console = _buf_console()
        _render_recent_events(
            [ev],
            session_start=datetime(2026, 1, 1, tzinfo=UTC),
            target_console=console,
            max_events=max_events,
        )
        assert "No events to display" in buf.getvalue()
        assert "hi" not in buf.getvalue()  # content must NOT appear


# ---------------------------------------------------------------------------
# Issue #849 — tool-calling assistant messages show tool names
# ---------------------------------------------------------------------------


class TestBuildEventDetailsToolRequests:
    """_build_event_details must surface tool names from toolRequests."""

    def test_tool_only_turn_shows_tool_names(self) -> None:
        """ASSISTANT_MESSAGE with content='', outputTokens=0, and two
        toolRequests must render both tool names."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={
                "content": "",
                "outputTokens": 0,
                "toolRequests": [
                    {"name": "bash", "toolCallId": "t1"},
                    {"name": "view", "toolCallId": "t2"},
                ],
            },
        )
        detail = _build_event_details(ev)
        assert "bash" in detail
        assert "view" in detail
        assert detail.startswith("tools:")

    def test_mixed_turn_shows_tokens_and_tool(self) -> None:
        """ASSISTANT_MESSAGE with content, outputTokens, and one
        toolRequest must render token info and the tool name."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={
                "content": "ok",
                "outputTokens": 100,
                "toolRequests": [
                    {"name": "edit", "toolCallId": "t3"},
                ],
            },
        )
        detail = _build_event_details(ev)
        assert "tokens=100" in detail
        assert "ok" in detail
        assert "tool: edit" in detail

    def test_no_tools_unchanged(self) -> None:
        """ASSISTANT_MESSAGE without toolRequests must behave as before."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={
                "content": "hello",
                "outputTokens": 50,
            },
        )
        detail = _build_event_details(ev)
        assert "tokens=50" in detail
        assert "hello" in detail
        assert "tool" not in detail

    def test_truncation_applied_to_long_tool_list(self) -> None:
        """When the joined tool names exceed 60 chars, truncation applies."""
        from copilot_usage.render_detail import _build_event_details

        long_names = [
            {"name": f"very_long_tool_name_{i}", "toolCallId": f"t{i}"}
            for i in range(10)
        ]
        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={
                "content": "",
                "outputTokens": 0,
                "toolRequests": long_names,
            },
        )
        detail = _build_event_details(ev)
        assert len(detail) <= 60
        assert detail.endswith("…")

    def test_empty_names_show_unknown(self) -> None:
        """toolRequests present but all names empty must show '(unknown)'."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={
                "content": "",
                "outputTokens": 0,
                "toolRequests": [
                    {"name": "", "toolCallId": "t1"},
                    {"name": "", "toolCallId": "t2"},
                ],
            },
        )
        detail = _build_event_details(ev)
        assert "tools: (unknown)" in detail

    def test_singular_label_based_on_displayed_names(self) -> None:
        """When two toolRequests exist but only one has a name, use 'tool'."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={
                "content": "",
                "outputTokens": 0,
                "toolRequests": [
                    {"name": "bash", "toolCallId": "t1"},
                    {"name": "", "toolCallId": "t2"},
                ],
            },
        )
        detail = _build_event_details(ev)
        assert "tool: bash" in detail
        assert not detail.startswith("tools:")


class TestRenderSessionDetailMultiModelShutdown:
    """Multi-model shutdown totals must propagate through
    render_session_detail end-to-end."""

    def test_multi_model_shutdown_via_full_render(self) -> None:
        """render_session_detail must show summed model calls and
        output tokens for a multi-model shutdown event."""
        sd = SessionShutdownData(
            shutdownType="normal",
            totalPremiumRequests=10,
            totalApiDurationMs=60_000,
            modelMetrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=7),
                    usage=TokenUsage(outputTokens=500),
                ),
                "claude-haiku-4.5": ModelMetrics(
                    requests=RequestMetrics(count=4, cost=3),
                    usage=TokenUsage(outputTokens=300),
                ),
            },
        )
        ts = datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC)
        summary = SessionSummary(
            session_id="multi-model-e2e",
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            is_active=False,
            shutdown_cycles=[(ts, sd)],
        )
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            timestamp=ts,
            data={},
        )
        buf, console = _buf_console()
        render_session_detail([ev], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Shutdown Cycles" in output
        # Assert against the shutdown-cycle row (contains timestamp)
        row = next(line for line in output.splitlines() if "2025-01-01 01:00" in line)
        assert re.search(r"\b7\b", row)  # total model calls = 3 + 4
        assert re.search(r"\b800\b", row)  # total output tokens = 500 + 300


# ---------------------------------------------------------------------------
# Issue #860 — untested branches in _build_event_details
# ---------------------------------------------------------------------------


class TestBuildEventDetailsUntestedBranches:
    """Cover the two branches in _build_event_details that had zero test
    coverage: SESSION_SHUTDOWN with falsy shutdownType and
    TOOL_EXECUTION_COMPLETE with a non-None model field."""

    def test_session_shutdown_none_shutdown_type_returns_empty(self) -> None:
        """SESSION_SHUTDOWN with shutdownType=None must return ''."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            data={"totalPremiumRequests": 0, "totalApiDurationMs": 0},
        )
        detail = _build_event_details(ev)
        assert detail == ""

    def test_tool_execution_complete_with_model(self) -> None:
        """TOOL_EXECUTION_COMPLETE with model set must include 'model=...'."""
        from copilot_usage.render_detail import _build_event_details

        ev = SessionEvent(
            type=EventType.TOOL_EXECUTION_COMPLETE,
            data={
                "toolCallId": "tc1",
                "model": "claude-sonnet-4",
                "success": True,
            },
        )
        detail = _build_event_details(ev)
        assert "model=claude-sonnet-4" in detail
