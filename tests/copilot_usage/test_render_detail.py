"""Tests for copilot_usage.render_detail — private helper coverage (issue #470, #562)."""

# pyright: reportPrivateUsage=false

import io
import re
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from copilot_usage._formatting import MAX_CONTENT_LEN
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
    UserMessageData,
)
from copilot_usage.render_detail import (
    _build_event_details,
    _event_type_label,
    _extract_tool_name,
    _format_relative_time,
    _render_active_period,
    _render_code_changes,
    _render_recent_events,
    _render_shutdown_cycles,
    _safe_event_data,
    _truncate,
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
        buf, console = _buf_console()
        _render_code_changes(None, target_console=console)
        assert buf.getvalue() == ""

    def test_all_zero_produces_no_output(self) -> None:
        """All fields zero/empty → returns without printing."""
        buf, console = _buf_console()
        changes = CodeChanges(linesAdded=0, linesRemoved=0, filesModified=[])
        _render_code_changes(changes, target_console=console)
        assert buf.getvalue() == ""

    def test_with_data_shows_table(self) -> None:
        """Non-zero code changes → renders a table with stats."""
        buf, console = _buf_console()
        changes = CodeChanges(linesAdded=10, linesRemoved=2, filesModified=["a.py"])
        _render_code_changes(changes, target_console=console)
        output = buf.getvalue()
        assert "Files modified" in output
        assert "+10" in output
        assert "-2" in output

    def test_files_present_zero_line_counts_renders_table(self) -> None:
        """filesModified non-empty with zero line deltas → table IS rendered."""
        buf, console = _buf_console()
        changes = CodeChanges(linesAdded=0, linesRemoved=0, filesModified=["a.py"])
        _render_code_changes(changes, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert output != ""
        assert "Files modified" in output
        assert re.search(r"Files modified\s+│\s+1\b", output)
        assert "+0" in output
        assert "-0" in output

    def test_zero_files_positive_line_counts_renders_table(self) -> None:
        """Empty filesModified with positive line counts → table IS rendered."""
        buf, console = _buf_console()
        changes = CodeChanges(linesAdded=5, linesRemoved=2, filesModified=[])
        _render_code_changes(changes, target_console=console)
        output = buf.getvalue()
        assert output != ""
        assert "+5" in output
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
# session_start fallback scans all events for first non-None timestamp (#1182)
# ---------------------------------------------------------------------------


class TestRenderSessionDetailFirstNoneTimestampFallback:
    """When start_time is None and events[0].timestamp is None, the renderer
    must scan forward to the first event with a non-None timestamp rather
    than falling back to datetime.now(tz=UTC), which would clamp every
    relative-time column to +0:00.
    """

    def test_relative_times_correct_when_first_event_has_no_timestamp(self) -> None:
        """events[1] anchors at +0:00, events[2] shows +5:00."""
        t = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        ev0 = SessionEvent(
            type=EventType.SESSION_START,
            timestamp=None,
            data={},
        )
        ev1 = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=t,
            data={"content": "first"},
        )
        ev2 = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=t + timedelta(minutes=5),
            data={"content": "second"},
        )
        summary = SessionSummary(session_id="null-head-test", start_time=None)

        buf, console = _buf_console()
        render_session_detail([ev0, ev1, ev2], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())

        assert "+5:00" in output
        assert "+0:00" in output


# ---------------------------------------------------------------------------
# Gap — render_session_detail start_time fallback from events (#954)
# ---------------------------------------------------------------------------


class TestRenderSessionDetailStartTimeFallbackFromEvents:
    """Cover the middle branch of the session_start ternary:

    ``start_time is None`` but ``events[0].timestamp`` is not None,
    so ``session_start = ensure_aware(events[0].timestamp)``.
    """

    def test_fallback_uses_first_event_timestamp(self) -> None:
        """Relative-time column uses events[0].timestamp as reference.

        With a 5-minute gap between the two events, the second event
        must show a relative offset of exactly +5:00, proving the
        renderer chose events[0].timestamp — not datetime.now().
        """
        ev1 = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
            data={"content": "hello"},
        )
        ev2 = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=datetime(2026, 1, 1, 10, 5, 0, tzinfo=UTC),
            data={"content": "world"},
        )
        summary = SessionSummary(session_id="fb-test", start_time=None)

        buf, console = _buf_console()
        render_session_detail([ev1, ev2], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())

        # The second event must appear at +5:00 relative to ev1.
        assert "+5:00" in output
        # The first event must appear at +0:00 (delta is zero).
        assert "+0:00" in output

    def test_changing_first_event_timestamp_shifts_relative_times(self) -> None:
        """Regression guard: moving events[0].timestamp changes output.

        Confirms the first event timestamp (not the last) is the
        reference for relative-time computation.
        """
        ev1_early = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
            data={"content": "a"},
        )
        ev1_late = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=datetime(2026, 1, 1, 10, 3, 0, tzinfo=UTC),
            data={"content": "a"},
        )
        ev2 = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=datetime(2026, 1, 1, 10, 5, 0, tzinfo=UTC),
            data={"content": "b"},
        )
        summary = SessionSummary(session_id="fb-reg", start_time=None)

        buf_early, con_early = _buf_console()
        render_session_detail([ev1_early, ev2], summary, target_console=con_early)
        out_early = _strip_ansi(buf_early.getvalue())

        buf_late, con_late = _buf_console()
        render_session_detail([ev1_late, ev2], summary, target_console=con_late)
        out_late = _strip_ansi(buf_late.getvalue())

        # With ev1 at 10:00 the second event is +5:00; with ev1 at 10:03 it's +2:00.
        assert "+5:00" in out_early
        assert "+2:00" in out_late
        # The two outputs must differ because the reference changed.
        assert out_early != out_late


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
# Issue #863 — "API Requests" column header in shutdown-cycles table
# ---------------------------------------------------------------------------


class TestShutdownCyclesColumnHeader:
    """The shutdown-cycles table must show 'API Requests' (not 'Model Calls')
    to distinguish from the summary table's turn-start-based 'Model Calls'."""

    def test_api_requests_header_present(self) -> None:
        """Shutdown-cycles table must contain 'API Requests' column header."""
        sd = SessionShutdownData(
            shutdownType="normal",
            totalPremiumRequests=1,
            totalApiDurationMs=500,
            modelMetrics={
                "test-model": ModelMetrics(
                    requests=RequestMetrics(count=2, cost=1),
                    usage=TokenUsage(outputTokens=100),
                ),
            },
        )
        summary = SessionSummary(
            session_id="header-check",
            shutdown_cycles=[(datetime(2025, 6, 1, tzinfo=UTC), sd)],
        )
        buf, console = _buf_console()
        _render_shutdown_cycles(summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "API Requests" in output

    def test_model_calls_header_absent(self) -> None:
        """Shutdown-cycles table must NOT contain 'Model Calls' header."""
        sd = SessionShutdownData(
            shutdownType="normal",
            totalPremiumRequests=1,
            totalApiDurationMs=500,
            modelMetrics={
                "test-model": ModelMetrics(
                    requests=RequestMetrics(count=2, cost=1),
                    usage=TokenUsage(outputTokens=100),
                ),
            },
        )
        summary = SessionSummary(
            session_id="header-absent",
            shutdown_cycles=[(datetime(2025, 6, 1, tzinfo=UTC), sd)],
        )
        buf, console = _buf_console()
        _render_shutdown_cycles(summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Model Calls" not in output


# ---------------------------------------------------------------------------
# Gap 1 — _render_shutdown_cycles multi-model per-cycle aggregation (#622)
# ---------------------------------------------------------------------------


class TestRenderShutdownCyclesMultiModelAggregation:
    """Multi-model modelMetrics must be summed for API Requests and
    Output Tokens columns in the Shutdown Cycles table."""

    def test_multi_model_sums_api_requests_and_output_tokens(self) -> None:
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
        assert re.search(r"\b7\b", row)  # total API requests = 3 + 4
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
        assert re.search(r"\b7\b", row)  # total API requests = 3 + 4
        assert re.search(r"\b800\b", row)  # total output tokens = 500 + 300


# ---------------------------------------------------------------------------
# _format_relative_time — direct unit tests (issue #879)
# ---------------------------------------------------------------------------


class TestFormatRelativeTime:
    """Direct unit tests covering all branches of _format_relative_time."""

    def test_sub_hour_formats_as_m_ss(self) -> None:
        """timedelta(minutes=4, seconds=7) → '+4:07'."""
        assert _format_relative_time(timedelta(minutes=4, seconds=7)) == "+4:07"

    def test_over_hour_formats_as_h_mm_ss(self) -> None:
        """timedelta(hours=1, minutes=2, seconds=3) → '+1:02:03'."""
        assert (
            _format_relative_time(timedelta(hours=1, minutes=2, seconds=3))
            == "+1:02:03"
        )

    def test_negative_delta_clamped_to_zero(self) -> None:
        """Negative timedelta must clamp to '+0:00', never a negative string."""
        assert _format_relative_time(timedelta(seconds=-10)) == "+0:00"

    def test_zero_delta(self) -> None:
        """Zero timedelta → '+0:00'."""
        assert _format_relative_time(timedelta()) == "+0:00"

    def test_exactly_one_hour(self) -> None:
        """Exactly 1h boundary triggers the hours branch."""
        assert _format_relative_time(timedelta(hours=1)) == "+1:00:00"


# ---------------------------------------------------------------------------
# _render_active_period — direct unit tests (issue #879)
# ---------------------------------------------------------------------------


class TestRenderActivePeriod:
    """Direct unit tests for _render_active_period covering active / inactive."""

    def test_active_session_renders_panel(self) -> None:
        """Active session must render an 'Active Period' panel with stats."""
        summary = SessionSummary(
            session_id="active-test",
            is_active=True,
            model_calls=3,
            user_messages=2,
            active_model_calls=3,
            active_user_messages=2,
            active_output_tokens=1000,
        )
        buf, console = _buf_console()
        _render_active_period(summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Active Period" in output
        assert "3 model calls" in output
        assert "2 user messages" in output

    def test_inactive_session_produces_no_output(self) -> None:
        """Inactive session → returns immediately, no output."""
        summary = SessionSummary(session_id="inactive-test", is_active=False)
        buf, console = _buf_console()
        _render_active_period(summary, target_console=console)
        assert buf.getvalue() == ""


class TestRenderSessionDetailActivePeriod:
    """Integration test: render_session_detail with is_active=True must
    render the Active Period panel (issue #879)."""

    def test_active_session_shows_active_period_panel(self) -> None:
        """render_session_detail with is_active=True must include the
        Active Period panel in its output."""
        summary = SessionSummary(
            session_id="active-e2e",
            start_time=datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC),
            is_active=True,
            model_calls=5,
            user_messages=3,
            active_model_calls=5,
            active_user_messages=3,
            active_output_tokens=2000,
        )
        ev = SessionEvent(
            type=EventType.USER_MESSAGE,
            timestamp=datetime(2026, 4, 1, 10, 5, 0, tzinfo=UTC),
            data={"content": "hello"},
        )
        buf, console = _buf_console()
        render_session_detail([ev], summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "Active Period" in output


# ---------------------------------------------------------------------------
# _event_type_label — parametrized unit tests (issue #879)
# ---------------------------------------------------------------------------


class TestEventTypeLabel:
    """Parametrized test for _event_type_label covering every labelled
    EventType case and the wildcard branch."""

    @pytest.mark.parametrize(
        ("event_type", "expected_text"),
        [
            pytest.param(EventType.USER_MESSAGE, "user message", id="user-message"),
            pytest.param(EventType.ASSISTANT_MESSAGE, "assistant", id="assistant"),
            pytest.param(EventType.TOOL_EXECUTION_COMPLETE, "tool", id="tool-complete"),
            pytest.param(EventType.TOOL_EXECUTION_START, "tool start", id="tool-start"),
            pytest.param(EventType.ASSISTANT_TURN_START, "turn start", id="turn-start"),
            pytest.param(EventType.ASSISTANT_TURN_END, "turn end", id="turn-end"),
            pytest.param(EventType.SESSION_START, "session start", id="session-start"),
            pytest.param(EventType.SESSION_SHUTDOWN, "session end", id="session-end"),
            pytest.param(
                "UNKNOWN_FUTURE_TYPE", "UNKNOWN_FUTURE_TYPE", id="wildcard-branch"
            ),
        ],
    )
    def test_label_text(self, event_type: str, expected_text: str) -> None:
        """Label plain text must match the expected string."""
        label = _event_type_label(event_type)
        assert label.plain == expected_text


# ---------------------------------------------------------------------------
# _build_event_details — USER_MESSAGE branch (issue #879)
# ---------------------------------------------------------------------------


class TestBuildEventDetailsUserMessage:
    """Tests for the USER_MESSAGE branch of _build_event_details."""

    def test_content_returned(self) -> None:
        """Short content is returned as-is."""
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={"content": "hello"})
        assert _build_event_details(ev) == "hello"

    def test_long_content_truncated(self) -> None:
        """Content exceeding MAX_CONTENT_LEN must be truncated with '…'."""
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={"content": "x" * 300})
        detail = _build_event_details(ev)
        assert detail.endswith("…")
        assert len(detail) <= MAX_CONTENT_LEN

    def test_empty_content_returns_empty_string(self) -> None:
        """Empty content → empty string."""
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={"content": ""})
        assert _build_event_details(ev) == ""


# ---------------------------------------------------------------------------
# _safe_event_data — exception recovery paths (issue #885)
# ---------------------------------------------------------------------------


class TestSafeEventData:
    """Cover the except (ValidationError, ValueError) branch of _safe_event_data."""

    def test_returns_none_on_validation_error(self) -> None:
        """ValidationError from the parser must be caught; returns None."""
        ev = SessionEvent(
            type=EventType.USER_MESSAGE,
            data={"attachments": 123},  # int, not list[str]
        )
        result = _safe_event_data(ev, ev.as_user_message)
        assert result is None

    def test_returns_none_on_value_error(self) -> None:
        """ValueError from the parser must be caught; returns None."""
        ev = SessionEvent(type=EventType.USER_MESSAGE, data={})

        def _raise() -> UserMessageData:
            raise ValueError("synthetic mismatch")

        result = _safe_event_data(ev, _raise)
        assert result is None

    def test_returns_none_propagates_to_build_event_details(self) -> None:
        """_build_event_details returns '' when _safe_event_data returns None."""
        ev = SessionEvent(
            type=EventType.USER_MESSAGE,
            data={"attachments": 123},
        )
        assert _build_event_details(ev) == ""


# ---------------------------------------------------------------------------
# _build_event_details — wildcard case (issue #885)
# ---------------------------------------------------------------------------


def test_build_event_details_returns_empty_for_unrecognized_type() -> None:
    """Wildcard case must return '' for event types without explicit handling."""
    ev = SessionEvent(type=EventType.SESSION_RESUME, data={})
    assert _build_event_details(ev) == ""


# ---------------------------------------------------------------------------
# _build_event_details — SESSION_SHUTDOWN arm (issue #1058)
# ---------------------------------------------------------------------------


class TestBuildEventDetailsSessionShutdown:
    """Tests for the SESSION_SHUTDOWN branch of _build_event_details."""

    def test_non_empty_shutdown_type(self) -> None:
        """Non-empty shutdownType must render as 'type=<value>'."""
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            data={
                "shutdownType": "routine",
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
                "modelMetrics": {},
            },
        )
        assert _build_event_details(ev) == "type=routine"

    def test_empty_shutdown_type(self) -> None:
        """Empty shutdownType must render as ''."""
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            data={
                "shutdownType": "",
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
                "modelMetrics": {},
            },
        )
        assert _build_event_details(ev) == ""

    def test_malformed_data_returns_empty(self) -> None:
        """Malformed data (int shutdownType) triggers ValidationError → ''."""
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            data={"shutdownType": 99},  # int triggers ValidationError
        )
        assert _build_event_details(ev) == ""


# ---------------------------------------------------------------------------
# _render_shutdown_cycles — None timestamp path (issue #1058)
# ---------------------------------------------------------------------------


class TestRenderShutdownCyclesNoneTimestamp:
    """A shutdown cycle with ts=None must display '—' in the Date column."""

    def test_none_timestamp_renders_dash(self) -> None:
        """A shutdown cycle with ts=None must display '—' in the Date column."""
        sd = SessionShutdownData(
            shutdownType="routine",
            totalPremiumRequests=1,
            totalApiDurationMs=500,
            modelMetrics={},
        )
        summary = SessionSummary(
            session_id="no-ts",
            shutdown_cycles=[(None, sd)],
        )
        buf, console = _buf_console()
        _render_shutdown_cycles(summary, target_console=console)
        output = _strip_ansi(buf.getvalue())
        assert "—" in output


# ---------------------------------------------------------------------------
# _truncate — max_len ≤ 0 guard (issue #1058)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("max_len", [0, -1, -100])
def test_truncate_non_positive_max_len_returns_empty(max_len: int) -> None:
    """_truncate must return '' for any max_len ≤ 0."""
    assert _truncate("hello", max_len) == ""
