"""Tests for copilot_usage.report — rendering helpers."""

# pyright: reportPrivateUsage=false

import json
import re
import warnings
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from rich.console import Console

from copilot_usage._formatting import (
    format_timedelta,
    hms,
)
from copilot_usage.models import (
    CodeChanges,
    EventType,
    ModelMetrics,
    RequestMetrics,
    SessionEvent,
    SessionShutdownData,
    SessionSummary,
    TokenUsage,
    copy_model_metrics,
    ensure_aware,
    has_active_period_stats,
    merge_model_metrics,
    shutdown_output_tokens,
    total_output_tokens,
)
from copilot_usage.parser import build_session_summary, parse_events
from copilot_usage.render_detail import (
    _build_event_details,
    _event_type_label,
    _format_detail_duration,
    _format_relative_time,
    _render_active_period,
    _render_aggregate_stats,
    _render_header,
    _render_recent_events,
    _render_shutdown_cycles,
    _safe_event_data,
    _truncate,
)
from copilot_usage.report import (
    _aggregate_model_metrics,
    _compute_session_totals,
    _effective_stats,
    _EffectiveStats,
    _estimate_premium_cost,
    _filter_sessions,
    _format_elapsed_since,
    _format_session_running_time,
    _render_model_table,
    _render_session_table,
    format_duration,
    format_tokens,
    render_cost_view,
    render_full_summary,
    render_live_sessions,
    render_summary,
    session_display_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    session_id: str = "abcdef1234567890",
    name: str | None = "My Session",
    model: str | None = "claude-sonnet-4",
    start_time: datetime | None = None,
    is_active: bool = True,
    user_messages: int = 5,
    model_calls: int = 3,
    output_tokens: int = 1200,
    cwd: str | None = "/home/user/project",
) -> SessionSummary:
    metrics: dict[str, ModelMetrics] = {}
    if model and output_tokens:
        metrics[model] = ModelMetrics(
            usage=TokenUsage(outputTokens=output_tokens),
        )
    return SessionSummary(
        session_id=session_id,
        start_time=start_time,
        name=name,
        model=model,
        is_active=is_active,
        user_messages=user_messages,
        model_calls=model_calls,
        model_metrics=metrics,
        cwd=cwd,
    )


def _capture_output(sessions: list[SessionSummary]) -> str:
    """Render live sessions and capture the console output as a string."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    render_live_sessions(sessions, target_console=console)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFormatSessionRunningTime:
    """Tests for _format_session_running_time helper."""

    def test_returns_dash_when_start_time_is_none(self) -> None:
        session = _make_session(start_time=None)
        assert _format_session_running_time(session) == "—"

    def test_uses_start_time_when_no_last_resume_time(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(minutes=5))
        session.last_resume_time = None
        result = _format_session_running_time(session)
        assert "5m" in result

    def test_uses_last_resume_time_when_present(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(hours=2))
        session.last_resume_time = now - timedelta(minutes=3)
        result = _format_session_running_time(session)
        assert "3m" in result
        assert "h" not in result

    def test_delegates_to_format_elapsed_since(self) -> None:
        now = datetime.now(tz=UTC)
        start = now - timedelta(minutes=7)
        session = _make_session(start_time=start)
        session.last_resume_time = None
        sentinel = "7m 00s"
        with patch(
            "copilot_usage.report._format_elapsed_since",
            return_value=sentinel,
        ) as mock_fmt:
            result = _format_session_running_time(session)
        mock_fmt.assert_called_once_with(start)
        assert result == sentinel


class TestRenderLiveSessions:
    """Tests for render_live_sessions."""

    def test_empty_list_shows_no_active(self) -> None:
        output = _capture_output([])
        assert "No active Copilot sessions found" in output

    def test_all_completed_shows_no_active(self) -> None:
        completed = _make_session(is_active=False)
        output = _capture_output([completed])
        assert "No active Copilot sessions found" in output

    def test_active_session_shows_short_id(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(minutes=10))
        output = _capture_output([session])
        assert "abcdef12" in output

    def test_active_session_shows_name(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(
            name="Test Session", start_time=now - timedelta(minutes=5)
        )
        output = _capture_output([session])
        assert "Test Session" in output

    def test_active_session_shows_model(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(model="gpt-4", start_time=now - timedelta(minutes=5))
        output = _capture_output([session])
        assert "gpt-4" in output

    def test_active_session_shows_running_time_minutes(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(minutes=5, seconds=30))
        output = _capture_output([session])
        assert "5m" in output

    def test_active_session_shows_running_time_hours(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(hours=2, minutes=15))
        output = _capture_output([session])
        assert "2h 15m" in output

    def test_active_session_shows_message_count(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(user_messages=42, start_time=now - timedelta(minutes=5))
        output = _capture_output([session])
        assert "42" in output

    def test_active_session_shows_output_tokens(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(
            output_tokens=15000, start_time=now - timedelta(minutes=5)
        )
        output = _capture_output([session])
        assert "15.0K" in output

    def test_active_session_shows_output_tokens_m_suffix(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(
            output_tokens=1_500_000, start_time=now - timedelta(minutes=5)
        )
        output = _capture_output([session])
        assert "1.5M" in output

    def test_active_session_shows_cwd(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(
            cwd="/home/user/work", start_time=now - timedelta(minutes=5)
        )
        output = _capture_output([session])
        assert "/home/user/work" in output

    def test_missing_fields_show_dash(self) -> None:
        session = _make_session(
            name=None, model=None, cwd=None, start_time=None, output_tokens=0
        )
        output = _capture_output([session])
        # Should still render without errors
        assert "abcdef12" in output

    def test_mixed_active_and_completed(self) -> None:
        now = datetime.now(tz=UTC)
        active = _make_session(
            session_id="active__12345678",
            start_time=now - timedelta(minutes=10),
            is_active=True,
        )
        completed = _make_session(
            session_id="completed12345678",
            is_active=False,
        )
        output = _capture_output([active, completed])
        assert "active__" in output
        assert "complete" not in output

    def test_multiple_active_sessions(self) -> None:
        now = datetime.now(tz=UTC)
        s1 = _make_session(
            session_id="session_1_abcdefg",
            name="First",
            start_time=now - timedelta(minutes=5),
        )
        s2 = _make_session(
            session_id="session_2_hijklmn",
            name="Second",
            start_time=now - timedelta(hours=1),
        )
        output = _capture_output([s1, s2])
        assert "session_" in output
        assert "First" in output
        assert "Second" in output

    def test_table_title_contains_active_indicator(self) -> None:
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(minutes=1))
        output = _capture_output([session])
        assert "Active Copilot Sessions" in output

    def test_last_resume_time_used_over_start_time(self) -> None:
        """When last_resume_time is set, running time is measured from it."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="resume__12345678",
            name="Resumed",
            model="claude-sonnet-4",
            is_active=True,
            start_time=now - timedelta(days=2),
            last_resume_time=now - timedelta(minutes=3),
            user_messages=1,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                )
            },
        )
        output = _capture_output([session])
        # Should show minutes (from last_resume_time), NOT days (from start_time)
        assert "2d" not in output and "48h" not in output

    def test_resumed_session_shows_active_fields(self) -> None:
        """Resumed session should show active_user_messages and active_output_tokens."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="aabbccdd-eeee-ffff-aaaa-bbbbbbbbbbbb",
            name="Resumed Task",
            model="claude-sonnet-4",
            is_active=True,
            start_time=now - timedelta(hours=3),
            last_resume_time=now - timedelta(minutes=10),
            # Historical totals (from shutdown events)
            user_messages=263,
            model_calls=275,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=200_000),
                )
            },
            # Post-resume activity
            active_user_messages=91,
            active_output_tokens=35_000,
            active_model_calls=12,
        )
        output = _capture_output([session])
        # Should show the active-period values, not historical totals.
        # Use word-boundary regex so assertions are not fooled by
        # substring matches in session IDs, names, or other columns.
        assert re.search(r"\b91\b", output), "active_user_messages (91) not found"
        assert "35.0K" in output  # active_output_tokens
        assert not re.search(r"\b263\b", output), (
            "historical total (263) should not appear"
        )
        assert "200.0K" not in output  # historical tokens should NOT appear

    def test_active_session_without_last_resume_time_shows_active_fields(self) -> None:
        """Active session with active_* but no last_resume_time should use active fields."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="no-resume-event-1234",
            name="Active Without Explicit Resume",
            model="claude-sonnet-4",
            is_active=True,
            start_time=now - timedelta(hours=2),
            # Historical totals accumulated before the current active period
            user_messages=263,
            model_calls=275,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=200_000),
                )
            },
            # Current active-period activity, even though last_resume_time is None
            active_user_messages=91,
            active_output_tokens=35_000,
            active_model_calls=12,
        )
        output = _capture_output([session])
        # Should show the active-period values, not historical totals,
        # even when last_resume_time is None.
        assert re.search(r"\b91\b", output), "active_user_messages (91) not found"
        assert "35.0K" in output  # active_output_tokens
        assert not re.search(r"\b263\b", output), (
            "historical total (263) should not appear"
        )
        assert "200.0K" not in output  # historical tokens should NOT appear

    def test_pure_active_session_uses_totals(self) -> None:
        """Pure-active session (no prior shutdown) should still use totals."""
        now = datetime.now(tz=UTC)
        session = _make_session(
            session_id="pure_active_session",
            user_messages=12,
            output_tokens=8_000,
            start_time=now - timedelta(minutes=5),
        )
        # active_user_messages and active_output_tokens default to 0
        output = _capture_output([session])
        assert re.search(r"\b12\b", output)  # user_messages
        assert "8.0K" in output  # from model_metrics

    def test_resumed_session_zero_activity_shows_zeros(self) -> None:
        """Resumed session with zero post-resume activity shows 0, not historical totals."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="aabbccdd-eeee-ffff-aaaa-cccccccccccc",
            name="Just Resumed",
            model="claude-sonnet-4",
            is_active=True,
            start_time=now - timedelta(hours=1),
            last_resume_time=now - timedelta(seconds=30),
            user_messages=150,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=100_000),
                )
            },
            # Zero post-resume activity
            active_user_messages=0,
            active_output_tokens=0,
            active_model_calls=0,
        )
        output = _capture_output([session])
        # Should show 0 for messages (active), not 150 (historical)
        assert not re.search(r"\b150\b", output), (
            "historical total (150) should not appear"
        )
        assert "100.0K" not in output  # historical tokens should NOT appear
        # And should explicitly render zeros for the active period
        session_line = next(
            (line for line in output.splitlines() if "Just Resumed" in line),
            "",
        )
        # Expect at least two whole-word zeros on the session row (Messages and Output Tokens)
        zeros_on_row = re.findall(r"\b0\b", session_line)
        assert len(zeros_on_row) >= 2, (
            "resumed session row should show 0 for both messages and output tokens"
        )

    def test_active_model_calls_only_uses_active_path(self) -> None:
        """Edge case: active_model_calls > 0 but user_messages/output_tokens are 0.

        When last_resume_time is None and only active_model_calls is non-zero,
        the predicate must still take the active-stats path (issue #196).
        """
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="model-calls-only-1234",
            name="ModelCallsOnly",
            model="claude-sonnet-4",
            is_active=True,
            start_time=now - timedelta(minutes=15),
            last_resume_time=None,
            user_messages=50,
            model_calls=20,
            active_model_calls=5,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=80_000),
                )
            },
        )
        output = _capture_output([session])
        # Should show active_user_messages (0), NOT historical (50)
        assert not re.search(r"\b50\b", output), (
            "historical user_messages (50) should not appear"
        )
        assert "80.0K" not in output, (
            "historical output tokens (80.0K) should not appear"
        )

    def test_est_cost_column_present(self) -> None:
        """Live sessions table includes an Est. Cost column."""
        now = datetime.now(tz=UTC)
        session = _make_session(start_time=now - timedelta(minutes=5))
        output = _capture_output([session])
        assert "Est. Cost" in output

    def test_est_cost_premium_model(self) -> None:
        """Live session with a premium model shows estimated cost."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="live-premium-1234",
            name="Premium Live",
            model="claude-opus-4.6",
            is_active=True,
            start_time=now - timedelta(minutes=10),
            user_messages=5,
            model_calls=4,
            model_metrics={
                "claude-opus-4.6": ModelMetrics(
                    usage=TokenUsage(outputTokens=1000),
                )
            },
        )
        output = _capture_output([session])
        # 4 calls × 3.0 multiplier = ~12
        assert "~12" in output

    def test_est_cost_free_model(self) -> None:
        """Live session with gpt-5-mini (0× multiplier) shows ~0."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="live-free-12345678",
            name="Free Live",
            model="gpt-5-mini",
            is_active=True,
            start_time=now - timedelta(minutes=10),
            user_messages=5,
            model_calls=4,
            model_metrics={
                "gpt-5-mini": ModelMetrics(
                    usage=TokenUsage(outputTokens=500),
                )
            },
        )
        output = _capture_output([session])
        assert "~0" in output


# ---------------------------------------------------------------------------
# Helpers for session detail tests
# ---------------------------------------------------------------------------


def _capture_console(fn: object, *args: object, **kwargs: object) -> str:
    """Call *fn* with a capturing Console and return the output string."""
    from io import StringIO

    sio = StringIO()
    c = Console(file=sio, force_terminal=False, width=120)
    fn(*args, target_console=c, **kwargs)  # type: ignore[operator]
    return sio.getvalue()


def _make_event(
    event_type: str,
    *,
    data: dict[str, object] | None = None,
    timestamp: datetime | None = None,
    current_model: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        type=event_type,
        data=data or {},
        timestamp=timestamp,
        currentModel=current_model,
    )


# ---------------------------------------------------------------------------
# Tests — render_session_detail (recent events, shutdown cycles, aggregate)
# ---------------------------------------------------------------------------


class TestRenderRecentEvents:
    """Tests for recent events display (via render_session_detail)."""

    def test_empty_events_shows_message(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "No events to display" in output

    def test_user_message_shown(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "hello world"},
                timestamp=start + timedelta(seconds=30),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "user message" in output
        assert "hello world" in output

    def test_only_last_10_events_shown(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": f"msg-{i}"},
                timestamp=start + timedelta(seconds=i * 10),
            )
            for i in range(15)
        ]
        output = _capture_console(render_session_detail, events, summary)
        # First 5 should not appear; last 10 should
        assert "msg-0" not in output
        assert "msg-4" not in output
        assert "msg-5" in output
        assert "msg-14" in output

    def test_assistant_message_shows_tokens(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.ASSISTANT_MESSAGE,
                data={"content": "Sure!", "outputTokens": 42, "messageId": "m1"},
                timestamp=start + timedelta(seconds=5),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "assistant" in output
        assert "tokens=42" in output

    def test_tool_execution_shows_name_and_success(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.TOOL_EXECUTION_COMPLETE,
                data={
                    "toolCallId": "tc1",
                    "success": True,
                    "model": "gpt-4",
                    "toolTelemetry": {
                        "properties": {"tool_name": "bash"},
                    },
                },
                timestamp=start + timedelta(seconds=10),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "tool" in output
        assert "bash" in output
        assert "✓" in output
        assert "model=gpt-4" in output

    def test_event_without_timestamp_shows_dash(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=False)
        events = [
            _make_event(EventType.USER_MESSAGE, data={"content": "hi"}),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "—" in output

    def test_long_content_truncated(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        long_msg = "A" * 200
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": long_msg},
                timestamp=start,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "…" in output
        assert long_msg not in output

    def test_recent_events_custom_max_events(self) -> None:
        """With a non-default max_events=5, only the last 5 are rendered."""
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": f"evt-{i:02d}"},
                timestamp=start + timedelta(seconds=i * 10),
            )
            for i in range(8)
        ]
        output = _capture_console(_render_recent_events, events, start, max_events=5)
        # First 3 events (indices 0-2) should be omitted
        for i in range(3):
            assert f"evt-{i:02d}" not in output
        # Last 5 events (indices 3-7) must be present
        for i in range(3, 8):
            assert f"evt-{i:02d}" in output
        # Assert exactly 5 rows by counting "user message" occurrences
        assert output.count("user message") == 5


# ---------------------------------------------------------------------------
# Tests — render_session_detail
# ---------------------------------------------------------------------------


class TestRenderSessionDetail:
    """Tests for render_session_detail."""

    def test_renders_header_with_session_id(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(session_id="abc-123", is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "abc-123" in output
        assert "Session Detail" in output

    def test_renders_aggregate_stats(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(
            output_tokens=5000,
            model_calls=10,
            user_messages=7,
            is_active=False,
        )
        summary.total_api_duration_ms = 60_000
        output = _capture_console(render_session_detail, [], summary)
        assert "Aggregate Stats" in output
        assert "10" in output  # model_calls
        assert "7" in output  # user_messages
        assert "5.0K" in output  # format_tokens(5000)
        assert "1m" in output  # format_duration(60000)

    def test_renders_no_shutdown_cycles_message(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "No shutdown cycles recorded" in output

    def test_renders_shutdown_cycle_table(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        sd = SessionShutdownData(
            shutdownType="normal",
            totalPremiumRequests=5,
            totalApiDurationMs=120_000,
            modelMetrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=5),
                    usage=TokenUsage(outputTokens=800),
                )
            },
        )
        shutdown_ts = start + timedelta(hours=1)
        summary = _make_session(
            start_time=start,
            is_active=False,
            model_calls=0,
            output_tokens=0,
        )
        summary.shutdown_cycles = [(shutdown_ts, sd)]
        events = [
            _make_event(
                EventType.SESSION_SHUTDOWN,
                data={},
                timestamp=shutdown_ts,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Shutdown Cycles" in output
        # Assert against the shutdown-cycle row (contains timestamp)
        row = next(line for line in output.splitlines() if "2025-01-01 01:00" in line)
        assert re.search(r"\b5\b", row)  # premium requests
        assert re.search(r"\b3\b", row)  # API requests
        assert re.search(r"\b800\b", row)  # output tokens

    def test_renders_recent_events_title(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "hello"},
                timestamp=start + timedelta(seconds=10),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Recent Events" in output
        assert "hello" in output

    def test_renders_code_changes(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = SessionSummary(
            session_id="test-session",
            code_changes=CodeChanges(
                linesAdded=10,
                linesRemoved=3,
                filesModified=["src/main.py", "README.md"],
            ),
        )
        output = _capture_console(render_session_detail, [], summary)
        assert "Code Changes" in output
        assert "Files modified" in output
        assert "2" in output
        assert "+10" in output
        assert "-3" in output

    def test_empty_events_and_no_code_changes(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=True)
        output = _capture_console(render_session_detail, [], summary)
        assert "Session Detail" in output
        assert "No events to display" in output

    def test_active_session_shows_active_status(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=True)
        output = _capture_console(render_session_detail, [], summary)
        assert "active" in output

    def test_active_session_shows_active_period(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=True)
        summary.active_model_calls = 3
        summary.active_user_messages = 2
        summary.active_output_tokens = 500
        output = _capture_console(render_session_detail, [], summary)
        assert "Active Period" in output
        assert "3 model calls" in output
        assert "2 user messages" in output
        assert "500" in output

    def test_completed_session_no_active_period(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "Active Period" not in output

    def test_completed_session_shows_duration(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2025, 1, 1, 0, 5, 30, tzinfo=UTC)
        summary = SessionSummary(
            session_id="dur-test",
            start_time=start,
            end_time=end,
            is_active=False,
        )
        output = _capture_console(render_session_detail, [], summary)
        assert "5m 30s" in output

    def test_session_name_displayed(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(name="My Cool Session", is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "My Cool Session" in output

    def test_unnamed_session_shows_unnamed(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(name=None, is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "unnamed" in output

    def test_header_model_none_shows_dash(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(model=None, is_active=False, output_tokens=0)
        output = _capture_console(render_session_detail, [], summary)
        # Ensure the Model header shows an em dash placeholder (and not "None")
        model_lines = [line for line in output.splitlines() if "Model:" in line]
        assert model_lines, "Model header not found in output"
        assert any("—" in line for line in model_lines)
        assert all("None" not in line for line in model_lines)

    def test_header_start_time_none_shows_dash(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(start_time=None, is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "Started: —" in output
        # Ensure the dash is not coming from the model field.
        model_line = next(
            (line for line in output.splitlines() if "Model:" in line), ""
        )
        assert "claude-sonnet-4" in model_line


class TestRenderActivePeriodDirect:
    """Direct unit tests for _render_active_period values."""

    def test_active_period_renders_counts(self) -> None:
        summary = _make_session(is_active=True)
        summary.active_model_calls = 3
        summary.active_user_messages = 2
        summary.active_output_tokens = 500
        output = _capture_console(_render_active_period, summary)
        assert "3 model calls" in output
        assert "2 user messages" in output
        assert "500" in output

    def test_active_period_format_tokens_large(self) -> None:
        summary = _make_session(is_active=True)
        summary.active_model_calls = 1
        summary.active_user_messages = 1
        summary.active_output_tokens = 1500
        output = _capture_console(_render_active_period, summary)
        assert "1.5K" in output

    def test_inactive_session_no_output(self) -> None:
        summary = _make_session(is_active=False)
        summary.active_model_calls = 3
        summary.active_user_messages = 2
        summary.active_output_tokens = 500
        output = _capture_console(_render_active_period, summary)
        assert output.strip() == ""


class TestRenderHeaderDirect:
    """Direct unit tests for _render_header fallback values."""

    def test_model_none_shows_dash(self) -> None:
        summary = _make_session(model=None, is_active=False, output_tokens=0)
        output = _capture_console(_render_header, summary)
        model_line = next(
            (line for line in output.splitlines() if "Model:" in line), ""
        )
        assert "—" in model_line
        assert "None" not in model_line

    def test_start_time_none_shows_dash(self) -> None:
        summary = _make_session(start_time=None, is_active=False)
        output = _capture_console(_render_header, summary)
        assert "Started: —" in output
        # Ensure the dash is not coming from the model field.
        model_line = next(
            (line for line in output.splitlines() if "Model:" in line), ""
        )
        assert "claude-sonnet-4" in model_line

    def test_model_present_shows_model(self) -> None:
        summary = _make_session(model="gpt-4o", is_active=False)
        output = _capture_console(_render_header, summary)
        assert "gpt-4o" in output


class TestRenderAggregateStatsDirect:
    """Direct unit tests for _render_aggregate_stats data values."""

    def test_output_tokens_formatted(self) -> None:
        summary = _make_session(
            output_tokens=5000, model_calls=10, user_messages=7, is_active=False
        )
        output = _capture_console(_render_aggregate_stats, summary)
        assert "5.0K" in output

    def test_api_duration_formatted(self) -> None:
        summary = _make_session(is_active=False)
        summary.total_api_duration_ms = 60_000
        output = _capture_console(_render_aggregate_stats, summary)
        assert "1m" in output


# ---------------------------------------------------------------------------
# format_tokens tests
# ---------------------------------------------------------------------------


def test_format_tokens_millions() -> None:
    assert format_tokens(1_627_935) == "1.6M"


def test_format_tokens_thousands() -> None:
    assert format_tokens(16_655) == "16.7K"


def test_format_tokens_small() -> None:
    assert format_tokens(500) == "500"


def test_format_tokens_zero() -> None:
    assert format_tokens(0) == "0"


def test_format_tokens_exact_boundary_million() -> None:
    assert format_tokens(1_000_000) == "1.0M"


def test_format_tokens_exact_boundary_thousand() -> None:
    assert format_tokens(1_000) == "1.0K"


# ---------------------------------------------------------------------------
# format_duration tests
# ---------------------------------------------------------------------------


def test_format_duration_minutes_seconds() -> None:
    assert format_duration(389_114) == "6m 29s 114ms"


def test_format_duration_seconds_only() -> None:
    assert format_duration(5_000) == "5s"


def test_format_duration_zero() -> None:
    assert format_duration(0) == "0ms"


def test_format_duration_negative() -> None:
    assert format_duration(-100) == "0ms"


def test_format_duration_hours() -> None:
    assert format_duration(3_661_000) == "1h 1m 1s"


def test_format_duration_exact_minute() -> None:
    assert format_duration(60_000) == "1m"


def test_format_duration_exact_hour() -> None:
    assert format_duration(3_600_000) == "1h"


# ---------------------------------------------------------------------------
# render_summary helpers
# ---------------------------------------------------------------------------

_OPUS_METRICS = ModelMetrics(
    requests=RequestMetrics(count=53, cost=24),
    usage=TokenUsage(
        inputTokens=1_627_935,
        outputTokens=16_655,
        cacheReadTokens=1_424_086,
    ),
)

_SONNET_METRICS = ModelMetrics(
    requests=RequestMetrics(count=10, cost=5),
    usage=TokenUsage(
        inputTokens=200_000,
        outputTokens=5_000,
        cacheReadTokens=100_000,
    ),
)


def _make_summary_session(
    *,
    session_id: str = "abc-123",
    name: str | None = "Test Session",
    model: str | None = "claude-opus-4.6-1m",
    start_time: datetime | None = None,
    is_active: bool = False,
    premium: int = 24,
    duration_ms: int = 389_114,
    metrics: dict[str, ModelMetrics] | None = None,
    user_messages: int = 10,
    model_calls: int = 5,
) -> SessionSummary:
    if start_time is None:
        start_time = datetime(2026, 3, 7, 15, 0, tzinfo=UTC)
    return SessionSummary(
        session_id=session_id,
        start_time=start_time,
        name=name,
        model=model,
        total_premium_requests=premium,
        total_api_duration_ms=duration_ms,
        model_metrics=metrics
        if metrics is not None
        else {"claude-opus-4.6-1m": copy_model_metrics(_OPUS_METRICS)},
        user_messages=user_messages,
        model_calls=model_calls,
        is_active=is_active,
    )


def _capture_summary(
    sessions: list[SessionSummary],
    since: datetime | None = None,
    until: datetime | None = None,
) -> str:
    """Capture Rich output from render_summary to a plain string."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    render_summary(sessions, since=since, until=until, target_console=console)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_summary tests
# ---------------------------------------------------------------------------


class TestRenderSummary:
    """Tests for render_summary."""

    def test_no_sessions(self) -> None:
        output = _capture_summary([])
        assert "No sessions found" in output

    def test_single_session(self) -> None:
        output = _capture_summary([_make_summary_session()])
        assert "Copilot Usage Summary" in output
        assert "24" in output  # premium requests
        assert "1.6M" in output  # input tokens
        assert "16.7K" in output  # output tokens
        assert "6m 29s" in output  # duration
        assert "Test Session" in output
        assert "Completed" in output

    def test_active_session(self) -> None:
        output = _capture_summary([_make_summary_session(is_active=True)])
        assert "Active" in output

    def test_session_without_name_shows_id(self) -> None:
        output = _capture_summary(
            [_make_summary_session(name=None, session_id="abcdef123456XYZ")]
        )
        assert "abcdef123456" in output

    def test_multiple_models(self) -> None:
        session = _make_summary_session(
            metrics={
                "claude-opus-4.6-1m": copy_model_metrics(_OPUS_METRICS),
                "claude-sonnet-4.5": copy_model_metrics(_SONNET_METRICS),
            }
        )
        output = _capture_summary([session])
        assert "claude-opus-4.6-1m" in output
        assert "claude-sonnet-4.5" in output

    def test_empty_model_metrics(self) -> None:
        session = _make_summary_session(metrics={})
        output = _capture_summary([session])
        assert "Copilot Usage Summary" in output
        assert "0" in output

    def test_make_summary_session_returns_isolated_metrics(self) -> None:
        """Two default sessions must not share the same ModelMetrics object."""
        s1 = _make_summary_session()
        s2 = _make_summary_session()
        opus_key = "claude-opus-4.6-1m"
        assert s1.model_metrics[opus_key] is not s2.model_metrics[opus_key]

    def test_since_filter(self) -> None:
        old = _make_summary_session(
            session_id="old",
            name="Old Session",
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
        )
        new = _make_summary_session(
            session_id="new",
            name="New Session",
            start_time=datetime(2026, 6, 1, tzinfo=UTC),
        )
        output = _capture_summary(
            [new, old],
            since=datetime(2026, 3, 1, tzinfo=UTC),
        )
        assert "New Session" in output
        assert "Old Session" not in output

    def test_until_filter(self) -> None:
        old = _make_summary_session(
            session_id="old",
            name="Old Session",
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
        )
        new = _make_summary_session(
            session_id="new",
            name="New Session",
            start_time=datetime(2026, 6, 1, tzinfo=UTC),
        )
        output = _capture_summary(
            [old, new],
            until=datetime(2026, 3, 1, tzinfo=UTC),
        )
        assert "Old Session" in output
        assert "New Session" not in output

    def test_no_start_time_excluded_by_filter(self) -> None:
        s = SessionSummary(session_id="no-time", start_time=None)
        output = _capture_summary([s], since=datetime(2026, 1, 1, tzinfo=UTC))
        assert "No sessions found" in output

    def test_preserves_descending_input_order(self) -> None:
        s1 = _make_summary_session(
            session_id="s1",
            name="Older",
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
        )
        s2 = _make_summary_session(
            session_id="s2",
            name="Newer",
            start_time=datetime(2026, 6, 1, tzinfo=UTC),
        )
        # Input is pre-sorted descending (newest first) — the contract
        # guaranteed by get_all_sessions().
        output = _capture_summary([s2, s1])
        pos_newer = output.index("Newer")
        pos_older = output.index("Older")
        assert pos_newer < pos_older

    def test_totals_aggregate_across_sessions(self) -> None:
        s1 = _make_summary_session(session_id="s1", premium=10, duration_ms=100_000)
        s2 = _make_summary_session(session_id="s2", premium=14, duration_ms=289_114)
        output = _capture_summary([s1, s2])
        assert "24" in output  # 10 + 14 premium
        assert "2" in output  # 2 sessions

    def test_zero_tokens_session(self) -> None:
        session = _make_summary_session(
            metrics={"claude-sonnet-4": ModelMetrics()},
            premium=0,
            duration_ms=0,
        )
        output = _capture_summary([session])
        assert "Copilot Usage Summary" in output
        assert "0ms" in output

    def test_summary_header_single_session_same_date_both_ends(self) -> None:
        """With a single session, earliest and latest are the same date."""
        s = _make_summary_session(start_time=datetime(2026, 3, 7, tzinfo=UTC))
        output = _capture_summary([s])
        assert output.count("2026-03-07") >= 2  # appears in both ends of range

    def test_all_sessions_have_no_start_time_shows_dates_unavailable(self) -> None:
        """Sessions with start_time=None produce 'dates unavailable', not 'no sessions'."""
        session = SessionSummary(
            session_id="abc123deadbeef",
            start_time=None,
            model="claude-sonnet-4",
            total_premium_requests=2,
        )
        output = _capture_summary([session])
        # Should NOT say "no sessions" — we have a session, just no start_time
        assert "No sessions found" not in output
        assert "no sessions" not in output.lower()
        # Should use the "dates unavailable" fallback
        assert "dates unavailable" in output
        # Body should still render the session
        assert "abc123deadbe" in output  # session_display_name truncates to 12 chars

    def test_render_summary_rejects_positional_since(self) -> None:
        """render_summary requires since/until as keyword-only arguments."""
        sessions: list[SessionSummary] = []
        with pytest.raises(TypeError):
            render_summary(sessions, datetime.now(tz=UTC))  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Coverage gap tests — report.py
# ---------------------------------------------------------------------------


class TestReportCoverageGaps:
    """Tests targeting specific uncovered lines in report.py."""

    def test_detail_duration_under_60_seconds(self) -> None:
        """_format_detail_duration with < 60s → returns '{n}s' (line 195)."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2025, 1, 1, 0, 0, 45, tzinfo=UTC)
        summary = SessionSummary(
            session_id="short-dur",
            start_time=start,
            end_time=end,
            is_active=False,
        )
        output = _capture_console(render_session_detail, [], summary)
        assert "45s" in output

    def test_event_type_label_tool_start(self) -> None:
        """EventType.TOOL_EXECUTION_START → 'tool start' via render_session_detail."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.TOOL_EXECUTION_START,
                data={"toolCallId": "tc1", "toolName": "bash"},
                timestamp=start + timedelta(seconds=5),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "tool start" in output

    def test_event_type_label_turn_start(self) -> None:
        """EventType.ASSISTANT_TURN_START → 'turn start' via render_session_detail."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.ASSISTANT_TURN_START,
                data={"turnId": "0"},
                timestamp=start + timedelta(seconds=2),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "turn start" in output

    def test_event_type_label_turn_end(self) -> None:
        """EventType.ASSISTANT_TURN_END → 'turn end' via render_session_detail."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.ASSISTANT_TURN_END,
                data={"turnId": "0"},
                timestamp=start + timedelta(seconds=3),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "turn end" in output

    def test_event_type_label_unknown(self) -> None:
        """Unknown event type → renders the raw type string."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                "session.info",
                data={"infoType": "mcp"},
                timestamp=start + timedelta(seconds=1),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "session.info" in output

    def test_user_message_empty_content(self) -> None:
        """User message with empty content renders without error."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": ""},
                timestamp=start + timedelta(seconds=1),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "user message" in output

    def test_assistant_message_no_tokens_no_content(self) -> None:
        """Assistant message with 0 tokens and empty content renders."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.ASSISTANT_MESSAGE,
                data={"messageId": "m1", "content": "", "outputTokens": 0},
                timestamp=start + timedelta(seconds=1),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "assistant" in output

    def test_shutdown_event_empty_data(self) -> None:
        """session.shutdown with empty shutdownType renders in shutdown cycles."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        sd = SessionShutdownData(
            shutdownType="",
            totalPremiumRequests=0,
            totalApiDurationMs=0,
        )
        shutdown_ts = start + timedelta(seconds=60)
        summary = _make_session(start_time=start, is_active=False)
        summary.shutdown_cycles = [(shutdown_ts, sd)]
        events = [
            _make_event(
                EventType.SESSION_SHUTDOWN,
                data={},
                timestamp=shutdown_ts,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Shutdown Cycles" in output

    def test_code_changes_all_zeros(self) -> None:
        """CodeChanges with 0 lines and no files → early return (line 413)."""
        from copilot_usage.report import render_session_detail

        summary = SessionSummary(
            session_id="zero-cc",
            code_changes=CodeChanges(
                linesAdded=0,
                linesRemoved=0,
                filesModified=[],
            ),
        )
        output = _capture_console(render_session_detail, [], summary)
        # Code Changes section should NOT appear
        assert "Code Changes" not in output

    def test_code_changes_files_only_no_line_counts(self) -> None:
        """Files modified but zero line counts → Code Changes section IS shown."""
        from copilot_usage.report import render_session_detail

        summary = SessionSummary(
            session_id="files-only",
            code_changes=CodeChanges(
                filesModified=["main.py", "utils.py"],
                linesAdded=0,
                linesRemoved=0,
            ),
        )
        output = _capture_console(render_session_detail, [], summary)
        assert "Code Changes" in output
        # Ensure the files-modified metric specifically shows a count of 2.
        assert re.search(r"Files?\s+modified[^\n]*\b2\b", output)

    def test_code_changes_additions_only_renders(self) -> None:
        """linesAdded>0, linesRemoved=0, no files → Code Changes section IS shown."""
        from copilot_usage.report import render_session_detail

        summary = SessionSummary(
            session_id="add-only",
            code_changes=CodeChanges(
                filesModified=[],
                linesAdded=5,
                linesRemoved=0,
            ),
        )
        output = _capture_console(render_session_detail, [], summary)
        assert "Code Changes" in output
        assert "+5" in output

    def test_code_changes_removals_only_renders(self) -> None:
        """linesRemoved>0, linesAdded=0, no files → Code Changes section IS shown."""
        from copilot_usage.report import render_session_detail

        summary = SessionSummary(
            session_id="rem-only",
            code_changes=CodeChanges(
                filesModified=[],
                linesAdded=0,
                linesRemoved=7,
            ),
        )
        output = _capture_console(render_session_detail, [], summary)
        assert "Code Changes" in output
        assert "-7" in output

    def test_summary_header_shows_date_range(self) -> None:
        """_render_summary_header date range: earliest date → latest date."""
        s1 = _make_summary_session(
            session_id="late",
            start_time=datetime(2025, 11, 30, tzinfo=UTC),
        )
        s2 = _make_summary_session(
            session_id="early",
            start_time=datetime(2025, 3, 1, tzinfo=UTC),
        )
        output = _capture_summary([s1, s2])
        assert "2025-03-01  →  2025-11-30" in output

    def test_summary_header_date_range_order_is_min_max(self) -> None:
        """Date range shows min date first (sessions in descending start_time order)."""
        sessions = [
            _make_summary_session(
                session_id="late",
                start_time=datetime(2025, 12, 31, tzinfo=UTC),
            ),
            _make_summary_session(
                session_id="mid",
                start_time=datetime(2025, 6, 15, tzinfo=UTC),
            ),
            _make_summary_session(
                session_id="early",
                start_time=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ]
        output = _capture_summary(sessions)
        assert "2025-01-01  →  2025-12-31" in output

    def test_summary_header_no_start_times(self) -> None:
        """Sessions with no start_time → 'dates unavailable' subtitle."""
        session = SessionSummary(session_id="no-time", start_time=None)
        output = _capture_summary([session])
        assert "dates unavailable" in output

    def test_session_table_empty_sessions(self) -> None:
        """render_summary with sessions that all lack start_time still renders."""
        s = SessionSummary(session_id="no-time", start_time=None)
        # This exercises _render_session_table with a session that has no
        # start_time (the "—" path on line 631).
        output = _capture_summary([s])
        assert "no-time" in output or "no sessions" in output

    def test_session_detail_no_start_time_uses_first_event(self) -> None:
        """render_session_detail with no start_time → uses first event timestamp."""
        from copilot_usage.report import render_session_detail

        event_time = datetime(2025, 3, 1, 10, 0, 0, tzinfo=UTC)
        summary = SessionSummary(session_id="no-start", start_time=None)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "hi"},
                timestamp=event_time,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Recent Events" in output

    def test_session_detail_naive_first_event_with_aware_subsequent(self) -> None:
        """Naive events[0].timestamp mixed with aware later timestamps must not raise TypeError."""
        from copilot_usage.report import render_session_detail

        naive_time = datetime(2025, 3, 1, 10, 0, 0)  # noqa: DTZ001
        aware_time = datetime(2025, 3, 1, 10, 0, 5, tzinfo=UTC)
        summary = SessionSummary(session_id="naive-start", start_time=None)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "hi"},
                timestamp=naive_time,
            ),
            _make_event(
                EventType.ASSISTANT_MESSAGE,
                data={"content": "ok", "outputTokens": 10, "messageId": "m1"},
                timestamp=aware_time,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Recent Events" in output


# ---------------------------------------------------------------------------
# Premium requests display (raw facts, no estimation)
# ---------------------------------------------------------------------------


class TestPremiumRequestsDisplay:
    """Tests for premium requests display in summary."""

    def test_active_session_shows_dash_for_premium(self) -> None:
        """Active session with no shutdown data shows '—' for premium."""
        session = SessionSummary(
            session_id="active-sess-1234",
            name="Active Session",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            total_premium_requests=0,
            user_messages=5,
        )
        output = _capture_summary([session])
        assert "—" in output

    def test_summary_shows_exact_for_completed(self) -> None:
        """Completed session shows exact number without '~'."""
        session = SessionSummary(
            session_id="done-sess-1234ab",
            name="Completed Session",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=42,
            user_messages=10,
        )
        output = _capture_summary([session])
        assert "42" in output
        # Should not have "~42" for completed sessions
        assert "~42" not in output


# ---------------------------------------------------------------------------
# render_full_summary capture helper
# ---------------------------------------------------------------------------


def _capture_full_summary(sessions: list[SessionSummary]) -> str:
    """Capture Rich output from render_full_summary to a plain string."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    render_full_summary(sessions, target_console=console)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_full_summary tests
# ---------------------------------------------------------------------------


class TestRenderFullSummary:
    """Tests for render_full_summary (two-section interactive view)."""

    def test_no_sessions(self) -> None:
        output = _capture_full_summary([])
        assert "No sessions found" in output

    def test_historical_section_rendered(self) -> None:
        session = SessionSummary(
            session_id="hist-1234-abcdef",
            name="HistSess",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=10,
            user_messages=3,
            model_calls=5,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(
                        inputTokens=500, outputTokens=1200, cacheReadTokens=100
                    ),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Historical Totals" in output
        assert "HistSess" in output

    def test_active_section_rendered(self) -> None:
        session = SessionSummary(
            session_id="actv-5678-abcdef",
            name="Active Session",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            user_messages=2,
            model_calls=1,
            active_model_calls=1,
            active_user_messages=2,
            active_output_tokens=500,
        )
        output = _capture_full_summary([session])
        assert "Active Sessions" in output
        assert "Active Session" in output

    def test_no_active_shows_panel(self) -> None:
        session = SessionSummary(
            session_id="done-9999-abcdef",
            name="Done",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=5,
            user_messages=1,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=1, cost=5),
                    usage=TokenUsage(outputTokens=100),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "No active sessions" in output

    def test_mixed_sessions(self) -> None:
        completed = SessionSummary(
            session_id="comp-aaaa-bbbbbb",
            name="Completed One",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=20,
            user_messages=5,
            model_calls=8,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=8, cost=20),
                    usage=TokenUsage(
                        inputTokens=1000, outputTokens=2000, cacheReadTokens=200
                    ),
                )
            },
        )
        active = SessionSummary(
            session_id="actv-cccc-dddddd",
            name="Active One",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
            is_active=True,
            user_messages=3,
            model_calls=2,
            active_model_calls=2,
            active_user_messages=3,
            active_output_tokens=800,
        )
        output = _capture_full_summary([completed, active])
        assert "Historical Totals" in output
        assert "Active Sessions" in output
        assert "Completed One" in output
        assert "Active One" in output

    def test_no_historical_data(self) -> None:
        """Session with no model_metrics and no premium reqs → no historical."""
        session = SessionSummary(
            session_id="empty-1111-aaaaaa",
            name="Empty",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=1,
            active_model_calls=1,
            active_user_messages=1,
            user_messages=1,
            active_output_tokens=100,
        )
        output = _capture_full_summary([session])
        assert "No historical shutdown data" in output

    def test_active_section_uses_last_resume_time(self) -> None:
        """Active section shows running time from last_resume_time, not start_time."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="resu-5678-abcdef",
            name="Resumed Session",
            model="claude-sonnet-4",
            start_time=now - timedelta(days=3),
            last_resume_time=now - timedelta(minutes=2),
            is_active=True,
            user_messages=1,
            model_calls=1,
            active_model_calls=1,
            active_user_messages=1,
            active_output_tokens=200,
        )
        output = _capture_full_summary([session])
        # Should show minutes (from last_resume_time), NOT days (from start_time)
        assert "3d" not in output and "72h" not in output

    def test_render_full_summary_implicit_resume_shows_active_section(self) -> None:
        """Implicit resume (is_active=True, last_resume_time=None) appears in active section."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="impl-resume-abcdef",
            name="Implicit Resume",
            model="claude-sonnet-4",
            start_time=now - timedelta(minutes=15),
            last_resume_time=None,
            is_active=True,
            user_messages=2,
            model_calls=1,
            active_user_messages=1,
            active_output_tokens=0,
            active_model_calls=0,
            total_premium_requests=5,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=5),
                    usage=TokenUsage(outputTokens=500),
                )
            },
        )
        output = _capture_full_summary([session])
        # Ensure the Active Sessions panel is present and not the "empty" variant.
        assert "Active Sessions" in output
        assert "No active sessions" not in output
        # Strip ANSI codes and isolate the Active Sessions section only.
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        parts = clean.split("Active Sessions", 1)
        active_section = parts[1] if len(parts) == 2 else ""

        assert "Implicit Resume" in active_section

    def test_active_section_shows_nonzero_activity(self) -> None:
        """Active section renders the actual active_* field values, not zero."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="pure-active-abcdef",
            name="Pure Active",
            model="claude-sonnet-4",
            start_time=now - timedelta(minutes=10),
            is_active=True,
            user_messages=4,
            model_calls=3,
            active_model_calls=3,
            active_user_messages=4,
            active_output_tokens=1500,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=1500),
                )
            },
        )
        import re

        output = _capture_full_summary([session])
        assert "Active Sessions" in output
        # Strip ANSI codes and split on │ to validate the correct columns
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        pure_active_line = next(line for line in lines if "Pure Active" in line)
        # Active Sessions columns: Name | Model | Model Calls | User Msgs | Output Tokens | Running Time
        cols = [c.strip() for c in pure_active_line.split("│")]
        assert cols[3] == "3", f"Model Calls column: expected '3', got '{cols[3]}'"
        assert cols[4] == "4", f"User Msgs column: expected '4', got '{cols[4]}'"
        assert cols[5] == "1.5K", (
            f"Output Tokens column: expected '1.5K', got '{cols[5]}'"
        )

    def test_pure_active_never_shutdown_falls_back_to_totals(self) -> None:
        """Pure-active session with active_*=0 should fall back to total fields.

        Regression test for issue #132: default view shows 0s when a
        session has never been shutdown and active_* fields are not set.
        """
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="never-shutdown-abcdef",
            name="Never Shutdown",
            model="claude-sonnet-4",
            start_time=now - timedelta(minutes=30),
            is_active=True,
            user_messages=382,
            model_calls=58,
            # active_* default to 0 — simulating old parser or direct construction
            active_model_calls=0,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=204_000),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Active Sessions" in output
        # Strip ANSI codes and locate the session row
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        row = next(line for line in lines if "Never Shutdown" in line)
        cols = [c.strip() for c in row.split("│")]
        # Columns: Name | Model | Model Calls | User Msgs | Output Tokens | Running Time
        assert cols[3] == "58", (
            f"Model Calls should fall back to total (58), got '{cols[3]}'"
        )
        assert cols[4] == "382", (
            f"User Msgs should fall back to total (382), got '{cols[4]}'"
        )
        assert cols[5] == "204.0K", (
            f"Output Tokens should fall back to total (204.0K), got '{cols[5]}'"
        )

    def test_active_model_calls_only_uses_active_path(self) -> None:
        """Full summary: active_model_calls > 0 with user_messages/output_tokens=0.

        When last_resume_time is None and only active_model_calls is non-zero,
        the predicate must take the active-stats path (issue #196).
        """
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="model-calls-only-fs",
            name="ModelCallsFS",
            model="claude-sonnet-4",
            start_time=now - timedelta(minutes=20),
            is_active=True,
            last_resume_time=None,
            user_messages=200,
            model_calls=40,
            active_model_calls=7,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=100_000),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Active Sessions" in output
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        row = next(line for line in lines if "ModelCallsFS" in line)
        cols = [c.strip() for c in row.split("│")]
        # Should use active_* (7, 0, 0), not totals (40, 200, 100.0K)
        assert cols[3] == "7", f"Model Calls should be active (7), got '{cols[3]}'"
        assert cols[4] == "0", f"User Msgs should be active (0), got '{cols[4]}'"
        assert cols[5] == "0", f"Output Tokens should be active (0), got '{cols[5]}'"
        assert "100.0K" not in row, (
            "Row should not display historical total output tokens '100.0K'"
        )

    def test_completed_zero_metrics_session_visible(self) -> None:
        """Completed session with zero metrics must appear in historical section.

        Regression test for issue #323: a completed (is_active=False) session
        with total_premium_requests=0 and model_metrics={} was silently
        excluded from both the historical and active sections in interactive
        mode, making it completely invisible.
        """
        session = SessionSummary(
            session_id="zero-met-1111-aaaaaa",
            name="Zero Metrics Done",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            end_time=datetime(2025, 1, 15, 10, 5, tzinfo=UTC),
            is_active=False,
            total_premium_requests=0,
            model_metrics={},
        )
        output = _capture_full_summary([session])
        # The session must NOT be invisible — it should appear in the
        # historical section (either by name or session ID prefix).
        assert "Zero Metrics Done" in output
        # The historical section must be rendered (not "No historical shutdown
        # data"), since there IS a completed session.
        assert "No historical shutdown data" not in output

    @pytest.mark.parametrize(
        "trigger",
        ["has_shutdown_metrics", "premium_requests"],
        ids=["via-shutdown-metrics", "via-premium-requests"],
    )
    def test_resumed_session_appears_in_both_sections(self, trigger: str) -> None:
        """Resumed session must appear in both Historical and Active sections.

        Regression guard for issue #649: the two independent ``if`` statements
        in ``render_full_summary`` intentionally place a resumed session in
        both lists.  If someone refactors ``if`` to ``elif``, this test fails.

        Parametrized over the two conditions that qualify an active session for
        the historical list: ``has_shutdown_metrics=True`` and
        ``total_premium_requests > 0``.
        """
        active_tokens = 350
        # When premium_requests triggers historical inclusion the real parser
        # produces model_metrics={} (shutdown cycles exist but no metrics
        # were recorded yet), so the shutdown-token baseline is 0.
        if trigger == "has_shutdown_metrics":
            shutdown_tokens = 2000
            model_metrics_map: dict[str, ModelMetrics] = {
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=15),
                    usage=TokenUsage(
                        inputTokens=800,
                        outputTokens=shutdown_tokens,
                    ),
                )
            }
        else:
            model_metrics_map = {}
        session = SessionSummary(
            session_id="resumed-dual-abcdef",
            name="DualSection",
            model="claude-sonnet-4",
            start_time=datetime(2025, 6, 1, 8, 0, tzinfo=UTC),
            last_resume_time=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=trigger == "has_shutdown_metrics",
            total_premium_requests=15 if trigger == "premium_requests" else 0,
            user_messages=10,
            model_calls=8,
            active_model_calls=3,
            active_user_messages=2,
            active_output_tokens=active_tokens,
            model_metrics=model_metrics_map,
        )
        output = _capture_full_summary([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        # --- Session visible in BOTH sections ---
        assert "Historical Totals" in clean
        assert "No historical shutdown data" not in clean

        assert "Active Sessions" in clean
        assert "No active sessions" not in clean

        # Split output at "Active Sessions" heading to isolate both regions.
        hist_part, active_part = clean.split("Active Sessions", 1)

        assert "DualSection" in hist_part, (
            "Resumed session must appear in the historical section"
        )
        assert "DualSection" in active_part, (
            "Resumed session must appear in the active section"
        )

    def test_resumed_session_historical_shows_shutdown_tokens(self) -> None:
        """Historical panel totals must reflect shutdown metrics only.

        The active table must show only post-shutdown ``active_*`` values.
        This ensures no double-counting across sections.
        """
        shutdown_tokens = 5000
        active_tokens = 800
        session = SessionSummary(
            session_id="resumed-tok-abcdef",
            name="TokenSplit",
            model="claude-sonnet-4",
            start_time=datetime(2025, 6, 1, 8, 0, tzinfo=UTC),
            last_resume_time=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            total_premium_requests=10,
            user_messages=12,
            model_calls=9,
            active_model_calls=4,
            active_user_messages=3,
            active_output_tokens=active_tokens,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(
                        inputTokens=1500,
                        outputTokens=shutdown_tokens,
                    ),
                )
            },
        )
        output = _capture_full_summary([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        # Split output at "Active Sessions" heading.
        hist_part, active_part = clean.split("Active Sessions", 1)

        # Historical panel should show shutdown_output_tokens (5.0K),
        # NOT the combined total (5.8K).
        assert "5.0K" in hist_part, (
            "Historical totals should use shutdown_output_tokens (5000 → 5.0K)"
        )
        assert "5.8K" not in hist_part, (
            "Historical totals must NOT include active_output_tokens"
        )

        # Active table row should show active_output_tokens (800), not
        # the shutdown total.
        active_row = next(
            (line for line in active_part.splitlines() if "TokenSplit" in line),
            "",
        )
        assert "800" in active_row, (
            "Active row should display active_output_tokens (800)"
        )
        assert "5.0K" not in active_row, (
            "Active row must NOT display shutdown output tokens"
        )

    def test_resumed_session_no_double_counting(self) -> None:
        """Rendered output must not double-count tokens across sections.

        Historical uses ``shutdown_output_tokens`` (3000 → ``3.0K``).
        Active uses ``_effective_stats`` (``active_output_tokens`` = 500).
        The rendered panels must each show only their own pool — the
        combined value (3500 → ``3.5K``) must not appear anywhere.
        """
        shutdown_tokens = 3000
        active_tokens = 500
        session = SessionSummary(
            session_id="resumed-nodup-abcdef",
            name="NoDup",
            model="claude-sonnet-4",
            start_time=datetime(2025, 6, 1, 8, 0, tzinfo=UTC),
            last_resume_time=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            total_premium_requests=7,
            user_messages=8,
            model_calls=6,
            active_model_calls=2,
            active_user_messages=1,
            active_output_tokens=active_tokens,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=4, cost=7),
                    usage=TokenUsage(
                        inputTokens=900,
                        outputTokens=shutdown_tokens,
                    ),
                )
            },
        )

        # Verify via the model functions directly: the token pools are disjoint.
        hist_tokens = shutdown_output_tokens(session)
        assert hist_tokens == shutdown_tokens

        stats = _effective_stats(session)
        assert stats.output_tokens == active_tokens

        # Verify the rendered output shows each pool separately.
        output = _capture_full_summary([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        hist_part, active_part = clean.split("Active Sessions", 1)

        # Historical panel must show shutdown tokens (3.0K),
        # NOT the combined total (3.5K).
        assert "3.0K" in hist_part, (
            "Historical totals should use shutdown_output_tokens (3000 → 3.0K)"
        )
        assert "3.5K" not in hist_part, (
            "Historical totals must NOT include active_output_tokens"
        )

        # Active table row must show active_output_tokens (500),
        # not the shutdown total.
        active_row = next(
            (line for line in active_part.splitlines() if "NoDup" in line),
            "",
        )
        assert "500" in active_row, (
            "Active row should display active_output_tokens (500)"
        )
        assert "3.0K" not in active_row, (
            "Active row must NOT display shutdown output tokens"
        )


# ---------------------------------------------------------------------------
# render_cost_view capture helper
# ---------------------------------------------------------------------------


def _capture_cost_view(
    sessions: list[SessionSummary],
    since: datetime | None = None,
    until: datetime | None = None,
) -> str:
    """Capture Rich output from render_cost_view to a plain string."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    render_cost_view(sessions, since=since, until=until, target_console=console)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_cost_view tests
# ---------------------------------------------------------------------------


class TestRenderCostView:
    """Tests for render_cost_view (per-session, per-model cost breakdown)."""

    def test_no_sessions(self) -> None:
        output = _capture_cost_view([])
        assert "No sessions found" in output

    def test_all_sessions_filtered_by_since_shows_no_sessions_found(self) -> None:
        """Non-empty session list, all fall before --since → 'No sessions found'."""
        session = SessionSummary(
            session_id="early-0000-aaaaaa",
            name="Old Session",
            start_time=datetime(2024, 1, 1, tzinfo=UTC),
            is_active=False,
            total_premium_requests=5,
        )
        output = _capture_cost_view([session], since=datetime(2026, 1, 1, tzinfo=UTC))
        assert "No sessions found" in output
        assert "Cost Breakdown" not in output

    def test_all_sessions_filtered_by_until_shows_no_sessions_found(self) -> None:
        """Non-empty session list, all fall after --until → 'No sessions found'."""
        session = SessionSummary(
            session_id="future-1111-bbbbbb",
            name="Future Session",
            start_time=datetime(2030, 6, 1, tzinfo=UTC),
            is_active=False,
            total_premium_requests=3,
        )
        output = _capture_cost_view([session], until=datetime(2025, 1, 1, tzinfo=UTC))
        assert "No sessions found" in output
        assert "Cost Breakdown" not in output

    def test_completed_session_cost(self) -> None:
        session = SessionSummary(
            session_id="cost-1111-abcdef",
            name="Cost Session",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=15,
            model_calls=10,
            user_messages=5,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=15),
                    usage=TokenUsage(
                        inputTokens=800, outputTokens=1500, cacheReadTokens=50
                    ),
                )
            },
        )
        output = _capture_cost_view([session])
        assert "Cost Breakdown" in output
        assert "Cost Session" in output
        assert "Grand Total" in output
        assert "15" in output

    def test_active_session_shows_shutdown_row(self) -> None:
        session = SessionSummary(
            session_id="actv-2222-abcdef",
            name="Active Cost",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=5,
            user_messages=3,
            active_model_calls=3,
            active_output_tokens=600,
            model_metrics={
                "claude-opus-4.6": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=1000),
                )
            },
        )
        output = _capture_cost_view([session])
        assert "Since last shutdown" in output
        # Premium Cost shows estimated cost (~9 = 3 calls × 3.0 multiplier)
        assert "~9" in output

    def test_session_without_metrics(self) -> None:
        session = SessionSummary(
            session_id="nometric-3333-ab",
            name="No Metrics",
            model="gpt-5-mini",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            model_calls=2,
            user_messages=1,
        )
        output = _capture_cost_view([session])
        assert "No Metrics" in output
        assert "—" in output

    def test_multi_model_session(self) -> None:
        session = SessionSummary(
            session_id="multi-4444-abcde",
            name="Multi Model",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            model_calls=15,
            user_messages=8,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=10),
                    usage=TokenUsage(outputTokens=2000),
                ),
                "claude-haiku-4.5": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=2),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        assert "claude-sonnet-4" in output
        assert "claude-haiku-4.5" in output
        assert "Grand Total" in output

        # Gap 1: Session name appears exactly once (cleared after first row)
        assert clean.count("Multi Model") == 1

        # Parse data rows: split on │ and keep all cells (including empty).
        # Table columns: Session | Model | Requests | Premium Cost | Model Calls | Output Tokens
        data_rows = [
            r
            for r in clean.split("\n")
            if "│" in r and "Grand Total" not in r and "Cost Breakdown" not in r
        ]
        model_rows = [r for r in data_rows if "claude-" in r]
        assert len(model_rows) == 2

        def _cells(row: str) -> list[str]:
            """Split a Rich table row on │ and strip each cell."""
            return [c.strip() for c in row.split("│")][1:-1]

        first = _cells(model_rows[0])
        second = _cells(model_rows[1])

        # Gap 2: Model-calls display (column index 4) appears on first row only.
        assert first[4] == "15"
        assert second[4] == ""

        # Gap 3: Per-model request count and cost are correct per row.
        # claude-haiku-4.5 (sorted first): count=5, cost=2
        assert first[1] == "claude-haiku-4.5"
        assert first[2] == "5"
        assert first[3] == "2"
        # claude-sonnet-4 (sorted second): count=10, cost=10
        assert second[1] == "claude-sonnet-4"
        assert second[2] == "10"
        assert second[3] == "10"

    def test_multi_model_active_session_shows_since_last_shutdown_row(self) -> None:
        """Active session with 2+ models in model_metrics renders both model rows
        and the '↳ Since last shutdown' row with correct estimated cost."""
        session = SessionSummary(
            session_id="multi-model-actv-01",
            name="Multi + Active",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 10, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=12,
            user_messages=6,
            active_model_calls=4,
            active_output_tokens=800,
            last_resume_time=datetime(2025, 1, 11, tzinfo=UTC),
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=7),
                    usage=TokenUsage(outputTokens=1400),
                ),
                "claude-opus-4.6": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=15),
                    usage=TokenUsage(outputTokens=600),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        # Both historical model rows must appear
        assert "claude-sonnet-4" in output
        assert "claude-opus-4.6" in output

        # The active row must appear
        assert "Since last shutdown" in output

        # claude-opus-4.6 multiplier = 3.0, active_model_calls = 4 → ~12
        assert "~12" in output

        # Gap 1: Session name appears exactly once (cleared after first row)
        assert clean.count("Multi + Active") == 1

        def _cells(row: str) -> list[str]:
            """Split a Rich table row on │ and strip each cell."""
            return [c.strip() for c in row.split("│")][1:-1]

        # Gap 4: shutdown_model_calls = model_calls - active_model_calls = 12 - 4 = 8
        # This value appears in the Model Calls column of the first model row only.
        data_rows = [
            r
            for r in clean.split("\n")
            if "│" in r
            and "Grand Total" not in r
            and "Cost Breakdown" not in r
            and "Since last shutdown" not in r
        ]
        model_rows = [r for r in data_rows if "claude-" in r]
        assert len(model_rows) == 2

        first = _cells(model_rows[0])
        second = _cells(model_rows[1])

        # Gap 2: Model-calls display appears on first row only (shutdown value = 8)
        assert first[4] == "8"  # shutdown_model_calls = 12 - 4
        assert second[4] == ""  # blanked after first row

        # Gap 3: Per-model request count and cost are correct.
        # claude-opus-4.6 (sorted first): count=5, cost=15
        assert first[1] == "claude-opus-4.6"
        assert first[2] == "5"
        assert first[3] == "15"
        # claude-sonnet-4 (sorted second): count=7, cost=7
        assert second[1] == "claude-sonnet-4"
        assert second[2] == "7"
        assert second[3] == "7"

        # Grand Total model calls = 12 (s.model_calls), NOT 12+4 = 16
        grand_match = re.search(
            r"Grand Total\s*│[^│]*│\s*\d+\s*│\s*\d+\s*│\s*(\d+)\s*│", clean
        )
        assert grand_match is not None, "Grand Total row not found"
        assert grand_match.group(1) == "12"

        # Grand Total output tokens = 1400 + 600 (shutdown) + 800 (active) = 2800 → "2.8K"
        assert "2.8K" in output

    def test_resumed_session_no_double_count(self) -> None:
        """Regression: active_model_calls must not be added to grand_model_calls."""
        import re

        session = SessionSummary(
            session_id="resume-5555-abcde",
            name="Resumed",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=10,
            user_messages=4,
            active_model_calls=3,
            active_output_tokens=200,
            model_metrics={
                "claude-opus-4.6": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=21),
                    usage=TokenUsage(outputTokens=1000),
                )
            },
        )
        output = _capture_cost_view([session])
        # Grand Total Model Calls should be 10, not 13
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        # Match: Grand Total │ │ Req │ Prem │ ModelCalls │
        grand_match = re.search(
            r"Grand Total\s*│[^│]*│\s*\d+\s*│\s*\d+\s*│\s*(\d+)\s*│", clean
        )
        assert grand_match is not None, "Grand Total row not found"
        assert grand_match.group(1) == "10"
        # Output Tokens must include active_output_tokens: 1000 + 200 = 1200 → "1.2K"
        assert "1.2K" in output

    def test_pure_active_session_no_metrics_shows_placeholder_row(self) -> None:
        """Active session with no model_metrics shows placeholder row but NOT
        a Since-last-shutdown row (no shutdown baseline exists)."""
        session = SessionSummary(
            session_id="pure-active-1234",
            name="Just Started",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=2,
            user_messages=1,
            active_model_calls=2,
            active_output_tokens=300,
            # model_metrics intentionally empty
        )
        output = _capture_cost_view([session])
        assert "Just Started" in output
        assert "—" in output  # placeholder row (no metrics)
        assert "Since last shutdown" not in output

    def test_pure_active_no_metrics_grand_total_includes_active_tokens(self) -> None:
        """Grand total output tokens includes active_output_tokens even when row shows '—' (issue #642)."""
        session = SessionSummary(
            session_id="pure-active-5678",
            name="Token Check",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=1,
            user_messages=1,
            active_model_calls=1,
            active_output_tokens=1500,
        )
        output = _capture_cost_view([session])
        assert "Grand Total" in output
        # No model_metrics → row shows "—" but grand total MUST include 1500
        assert "1.5K" in output

    def test_mixed_sessions_grand_total(self) -> None:
        """Grand total sums output tokens from all sessions including model-unknown (issue #642)."""
        completed = SessionSummary(
            session_id="comp-aaaa-111111",
            name="Done",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 10, tzinfo=UTC),
            is_active=False,
            model_calls=5,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=5),
                    usage=TokenUsage(outputTokens=2000),
                )
            },
        )
        active = SessionSummary(
            session_id="actv-bbbb-222222",
            name="Running",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, tzinfo=UTC),
            is_active=True,
            model_calls=3,
            active_model_calls=3,
            active_output_tokens=500,
        )
        output = _capture_cost_view([completed, active])
        # Completed session contributes 2000 tokens → "2.0K" in its row
        assert "2.0K" in output
        # Grand total includes both: 2000 + 500 = 2500 → "2.5K"
        assert "2.5K" in output

    def test_active_session_estimated_cost_known_model(self) -> None:
        """Active session shows numeric estimated cost, not 'N/A', when model is known."""
        session = SessionSummary(
            session_id="est-cost-known-mod",
            name="Known Model",
            model="claude-opus-4.5",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=5,
            active_model_calls=4,
            active_output_tokens=800,
            model_metrics={
                "claude-opus-4.5": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=15),
                    usage=TokenUsage(outputTokens=2000),
                )
            },
        )
        output = _capture_cost_view([session])
        # claude-opus-4.5 multiplier = 3.0, active_model_calls = 4 → ~12
        assert "~12" in output
        # The "Since last shutdown" row should NOT show "N/A" for Premium Cost
        lines = output.splitlines()
        shutdown_line = next(
            (line for line in lines if "Since last shutdown" in line),
            None,
        )
        assert shutdown_line is not None
        assert shutdown_line.count("N/A") == 1
        # Grand Total output tokens: 2000 (model_metrics) + 800 (active) = 2800 → "2.8K"
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        assert "2.8K" in grand_cols[6], (
            f"Grand Total output tokens should be 2.8K, got '{grand_cols[6]}'"
        )
        """gpt-5-mini has 0× multiplier → estimated cost is 0."""
        session = SessionSummary(
            session_id="est-cost-free-mod",
            name="Free Model",
            model="gpt-5-mini",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=5,
            active_model_calls=5,
            active_output_tokens=1000,
        )
        output = _capture_cost_view([session])
        assert "~0" in output

    def test_estimated_cost_premium_model_multiplier(self) -> None:
        """3 calls of claude-opus-4.6 (3× multiplier) → estimated cost ~9."""
        session = SessionSummary(
            session_id="est-cost-prem-mod",
            name="Premium Model",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=3,
            active_model_calls=3,
            active_output_tokens=500,
            model_metrics={
                "claude-opus-4.6": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=9),
                    usage=TokenUsage(outputTokens=1000),
                )
            },
        )
        output = _capture_cost_view([session])
        # 3 calls × 3.0 multiplier = ~9
        assert "~9" in output

    def test_pure_active_with_synthetic_metrics_no_double_count(self) -> None:
        """Pure-active session with synthetic model_metrics must not double-count output tokens.

        When build_session_summary creates a pure-active session, it sets both
        model_metrics.outputTokens and active_output_tokens to the same total.
        Grand Total must count them only once.
        """
        session = SessionSummary(
            session_id="pure-synth-aaaa",
            name="Pure Synth",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=5,
            user_messages=3,
            active_model_calls=5,
            active_user_messages=3,
            active_output_tokens=8000,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    # Synthetic metrics have requests at defaults (count=0)
                    usage=TokenUsage(outputTokens=8000),
                )
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        # 8000 → "8.0K", NOT 16.0K (which would indicate double-counting)
        assert "8.0K" in grand_cols[6], (
            f"Grand Total output tokens should be 8.0K, got '{grand_cols[6]}'"
        )

    def test_resumed_session_active_zero_cost_suppresses_active_row(self) -> None:
        """Cost view: resumed session with active_*=0 suppresses the active row.

        Updated for issue #775: when has_active_period_stats is False
        (all active counters are 0 and last_resume_time is None), the
        '↳ Since last shutdown' row must not appear — previously it
        fell back to session totals which was misleading.
        """
        session = SessionSummary(
            session_id="cost-never-shut",
            name="Cost No Shutdown",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=10,
            user_messages=8,
            active_model_calls=0,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=10),
                    usage=TokenUsage(outputTokens=50_000),
                )
            },
        )
        output = _capture_cost_view([session])
        # Row must not appear when there is no active-period data
        assert "Since last shutdown" not in output
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        # Per-model row shows the full model_calls (10)
        model_row = next(
            line
            for line in lines
            if "claude-sonnet-4" in line and "Cost No Shutdown" in line
        )
        model_cols = [c.strip() for c in model_row.split("│")]
        assert model_cols[5] == "10", (
            f"Model Calls in per-model row should be 10, got '{model_cols[5]}'"
        )
        # Grand Total output tokens: 50.0K (no double-counting)
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        assert "50.0K" in grand_cols[6], (
            f"Grand Total output tokens should be 50.0K, got '{grand_cols[6]}'"
        )

    def test_resumed_session_active_model_calls_only(self) -> None:
        """Cost view: active_model_calls > 0 with user_messages/output_tokens=0.

        When last_resume_time is None and only active_model_calls is non-zero,
        the predicate must take the active path (issue #196).
        """
        session = SessionSummary(
            session_id="cost-mc-only",
            name="Cost MC Only",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=10,
            user_messages=8,
            last_resume_time=None,
            active_model_calls=3,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=10),
                    usage=TokenUsage(outputTokens=50_000),
                )
            },
        )
        output = _capture_cost_view([session])
        assert "Since last shutdown" in output
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        shutdown_row = next(line for line in lines if "Since last shutdown" in line)
        cols = [c.strip() for c in shutdown_row.split("│")]
        # Should show active_model_calls (3), not model_calls (10)
        assert cols[5] == "3", f"Model Calls in active row should be 3, got '{cols[5]}'"
        # Output Tokens column should use active_output_tokens (0), not historical 50.0K
        assert cols[6] == "0", (
            f"Output Tokens in active row should be 0, got '{cols[6]}'"
        )

    def test_active_session_unknown_model_no_warning(self) -> None:
        """Active session with an unknown model must not emit UserWarning."""
        session = SessionSummary(
            session_id="unknown-model-1234",
            name="Unknown Model",
            model="experimental-model-42",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=4,
            user_messages=2,
            active_model_calls=2,
            active_output_tokens=300,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UserWarning)
            output = _capture_cost_view([session])
        assert "Since last shutdown" in output
        assert len(caught) == 0, (
            f"Expected no UserWarning, got {[str(w.message) for w in caught]}"
        )


# ---------------------------------------------------------------------------
# Issue #419 — pure-active sessions must NOT show "↳ Since last shutdown"
# ---------------------------------------------------------------------------


class TestRenderCostViewPureActiveNoShutdownRow:
    """Fix #419: the '↳ Since last shutdown' sub-row must only appear for
    resumed sessions (is_active=True AND has_shutdown_metrics=True).
    Pure-active sessions (never shut down) must not show it."""

    def test_pure_active_no_shutdown_row(self) -> None:
        """Pure-active session (has_shutdown_metrics=False) must NOT render
        the '↳ Since last shutdown' sub-row."""
        session = SessionSummary(
            session_id="pure-active-419a",
            name="Pure Active",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=False,
            model_calls=5,
            user_messages=3,
            active_model_calls=5,
            active_output_tokens=1200,
        )
        output = _capture_cost_view([session])
        assert "Pure Active" in output
        assert "Since last shutdown" not in output

    def test_pure_active_with_known_model_shows_dash_for_requests(self) -> None:
        """Pure-active session (has_shutdown_metrics=False) with a known model
        must show '—' in Requests and Premium Cost columns, not '0'."""
        session = SessionSummary(
            session_id="pure-active-with-model",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=False,
            model_calls=5,
            active_model_calls=5,
            active_output_tokens=1200,
            # model_metrics populated synthetically (as _build_active_summary does)
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=0, cost=0),
                    usage=TokenUsage(outputTokens=1200),
                )
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        # Must not show "0" in Requests/Premium Cost columns
        lines = [ln for ln in clean.splitlines() if "claude-sonnet-4" in ln]
        assert lines, "Expected a per-model row"
        cols = [c.strip() for c in lines[0].split("│")]
        assert cols[3] == "—", f"Requests column should be '—', got '{cols[3]}'"
        assert cols[4] == "—", f"Premium Cost column should be '—', got '{cols[4]}'"

    def test_resumed_session_shows_shutdown_row(self) -> None:
        """Resumed session (has_shutdown_metrics=True) must render
        the '↳ Since last shutdown' sub-row."""
        session = SessionSummary(
            session_id="resumed-419b",
            name="Resumed Session",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=10,
            user_messages=6,
            active_model_calls=4,
            active_output_tokens=800,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=10),
                    usage=TokenUsage(outputTokens=2000),
                )
            },
        )
        output = _capture_cost_view([session])
        assert "Resumed Session" in output
        assert "Since last shutdown" in output


# ---------------------------------------------------------------------------
# _estimate_premium_cost tests
# ---------------------------------------------------------------------------


class TestEstimatePremiumCost:
    """Tests for _estimate_premium_cost helper."""

    def test_none_model_returns_dash(self) -> None:
        assert _estimate_premium_cost(None, 5) == "—"

    def test_known_model_returns_estimate(self) -> None:
        # claude-opus-4.6 has a 3× multiplier → 3 calls × 3.0 = ~9
        assert _estimate_premium_cost("claude-opus-4.6", 3) == "~9"

    def test_unknown_model_no_warning(self) -> None:
        """Unknown model degrades to 1× multiplier without emitting warnings."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UserWarning)
            result = _estimate_premium_cost("totally-unknown-model-xyz", 7)
        assert result == "~7"  # 7 calls × 1.0 = ~7
        assert len(caught) == 0, (
            f"Expected no UserWarning, got {[str(w.message) for w in caught]}"
        )

    def test_zero_calls_returns_zero(self) -> None:
        assert _estimate_premium_cost("claude-sonnet-4", 0) == "~0"

    def test_mixed_case_model_uses_correct_multiplier(self) -> None:
        """_estimate_premium_cost with mixed-case model name uses actual tier multiplier.

        claude-opus-4.6 has multiplier 3.0; mixed-case resolves correctly after
        normalization (issue #431).
        """
        result = _estimate_premium_cost("Claude-Opus-4.6", 10)
        assert result == "~30"


class TestRenderFullSummaryHelperReuse:
    """Verify _render_historical_section_from delegates to shared table helpers."""

    def test_historical_session_table_title(self) -> None:
        """Historical section must use Sessions (Shutdown Data) title."""
        session = SessionSummary(
            session_id="hist-7777-abcdef",
            name="HistReuse",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=5,
            user_messages=2,
            model_calls=3,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=5),
                    usage=TokenUsage(
                        inputTokens=300, outputTokens=600, cacheReadTokens=50
                    ),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Sessions (Shutdown Data)" in output

    def test_historical_model_table_present(self) -> None:
        """Historical section must contain per-model breakdown table."""
        session = SessionSummary(
            session_id="hist-8888-abcdef",
            name="ModelTbl",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=5,
            user_messages=2,
            model_calls=3,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=5),
                    usage=TokenUsage(
                        inputTokens=300, outputTokens=600, cacheReadTokens=50
                    ),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Per-Model Breakdown" in output
        assert "claude-sonnet-4" in output

    def test_since_until_filters_sessions(self) -> None:
        """render_cost_view since/until params exclude sessions outside range."""
        early = _make_session(
            start_time=datetime(2025, 1, 10, tzinfo=UTC), name="Early"
        )
        late = _make_session(start_time=datetime(2025, 1, 20, tzinfo=UTC), name="Late")
        since = datetime(2025, 1, 15, tzinfo=UTC)
        until = datetime(2025, 1, 25, tzinfo=UTC)
        output = _capture_cost_view([late, early], since=since, until=until)
        assert "Late" in output
        assert "Early" not in output


# ---------------------------------------------------------------------------
# Issue #18 — _build_event_details direct tests
# ---------------------------------------------------------------------------


class TestBuildEventDetails:
    """Direct tests for _build_event_details covering untested branches."""

    def test_tool_failure_shows_cross(self) -> None:
        ev = _make_event(
            EventType.TOOL_EXECUTION_COMPLETE,
            data={
                "toolCallId": "t1",
                "success": False,
                "toolTelemetry": {"properties": {"tool_name": "bash"}},
            },
        )
        details = _build_event_details(ev)
        assert "✗" in details
        assert "✓" not in details

    def test_tool_no_telemetry(self) -> None:
        ev = _make_event(
            EventType.TOOL_EXECUTION_COMPLETE,
            data={"toolCallId": "t1", "success": True},
        )
        details = _build_event_details(ev)
        assert "✓" in details

    def test_tool_no_tool_name_in_properties(self) -> None:
        ev = _make_event(
            EventType.TOOL_EXECUTION_COMPLETE,
            data={
                "toolCallId": "t1",
                "success": True,
                "toolTelemetry": {"properties": {}},
            },
        )
        details = _build_event_details(ev)
        assert "✓" in details

    def test_session_shutdown_details(self) -> None:
        ev = _make_event(
            EventType.SESSION_SHUTDOWN,
            data={
                "shutdownType": "routine",
                "totalPremiumRequests": 5,
                "totalApiDurationMs": 1000,
                "modelMetrics": {},
            },
        )
        details = _build_event_details(ev)
        assert "routine" in details

    def test_assistant_message_zero_tokens_shows_content(self) -> None:
        ev = _make_event(
            EventType.ASSISTANT_MESSAGE,
            data={"messageId": "m1", "content": "hello", "outputTokens": 0},
        )
        details = _build_event_details(ev)
        assert "hello" in details
        assert "tokens=0" not in details

    def test_user_message_malformed_data_returns_empty(self) -> None:
        ev = _make_event(EventType.USER_MESSAGE, data={"attachments": 12345})
        assert _build_event_details(ev) == ""

    def test_assistant_message_malformed_data_returns_empty(self) -> None:
        ev = _make_event(EventType.ASSISTANT_MESSAGE, data={"toolRequests": "bad"})
        assert _build_event_details(ev) == ""

    def test_tool_execution_malformed_data_returns_empty(self) -> None:
        ev = _make_event(
            EventType.TOOL_EXECUTION_COMPLETE, data={"toolTelemetry": 12345}
        )
        assert _build_event_details(ev) == ""

    def test_session_shutdown_malformed_data_returns_empty(self) -> None:
        ev = _make_event(EventType.SESSION_SHUTDOWN, data={"modelMetrics": "bad"})
        assert _build_event_details(ev) == ""

    def test_assistant_message_tokens_only_no_content(self) -> None:
        """outputTokens > 0 but content='' → shows tokens, no content."""
        ev = _make_event(
            EventType.ASSISTANT_MESSAGE,
            data={"messageId": "m1", "outputTokens": 50, "content": ""},
        )
        details = _build_event_details(ev)
        assert details == "tokens=50"

    def test_assistant_message_tokens_only_large_count(self) -> None:
        """outputTokens > 0 with large count and empty content."""
        ev = _make_event(
            EventType.ASSISTANT_MESSAGE,
            data={"messageId": "m1", "outputTokens": 150_000, "content": ""},
        )
        details = _build_event_details(ev)
        assert details == "tokens=150000"

    def test_session_shutdown_empty_shutdown_type(self) -> None:
        """shutdownType='' → returns empty string."""
        ev = _make_event(
            EventType.SESSION_SHUTDOWN,
            data={
                "shutdownType": "",
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
                "modelMetrics": {},
            },
        )
        assert _build_event_details(ev) == ""

    def test_session_shutdown_default_data(self) -> None:
        """SessionShutdownData() with all defaults → returns empty string."""
        ev = _make_event(
            EventType.SESSION_SHUTDOWN,
            data={
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
                "modelMetrics": {},
            },
        )
        assert _build_event_details(ev) == ""

    def test_shutdown_event_nonempty_shutdown_type_shown_in_detail(self) -> None:
        """SESSION_SHUTDOWN with shutdownType='normal' → _build_event_details returns 'type=normal'."""
        ev = _make_event(
            EventType.SESSION_SHUTDOWN,
            data={
                "shutdownType": "normal",
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
            },
            timestamp=None,
        )
        assert _build_event_details(ev) == "type=normal"

    def test_tool_execution_with_model_field(self) -> None:
        """TOOL_EXECUTION_COMPLETE with model → detail string includes model=<name>."""
        ev = _make_event(
            EventType.TOOL_EXECUTION_COMPLETE,
            data={
                "toolCallId": "t1",
                "success": True,
                "model": "claude-sonnet-4",
                "toolTelemetry": {"properties": {"tool_name": "bash"}},
            },
        )
        details = _build_event_details(ev)
        assert "model=claude-sonnet-4" in details
        assert "bash" in details
        assert "✓" in details

    def test_tool_execution_with_empty_model_string(self) -> None:
        """TOOL_EXECUTION_COMPLETE with model='' → model not shown in details."""
        ev = _make_event(
            EventType.TOOL_EXECUTION_COMPLETE,
            data={
                "toolCallId": "t1",
                "success": True,
                "model": "",
                "toolTelemetry": {"properties": {"tool_name": "bash"}},
            },
        )
        details = _build_event_details(ev)
        assert "model=" not in details
        assert "✓" in details


class TestRenderShutdownCyclesEdgeCases:
    """Test _render_shutdown_cycles with edge-case summaries."""

    def test_empty_shutdown_cycles_shows_no_cycles(self) -> None:
        summary = SessionSummary(session_id="empty", shutdown_cycles=[])
        output = _capture_console(_render_shutdown_cycles, summary)
        assert "No shutdown cycles recorded" in output

    def test_shutdown_with_no_timestamp_shows_dash(self) -> None:
        """Session shutdown cycle with timestamp=None → date column shows '—'."""
        sd = SessionShutdownData(
            shutdownType="routine",
            totalPremiumRequests=3,
            totalApiDurationMs=5000,
            modelMetrics={},
        )
        summary = SessionSummary(
            session_id="no-ts",
            shutdown_cycles=[(None, sd)],
        )
        output = _capture_console(_render_shutdown_cycles, summary)
        assert "Shutdown Cycles" in output
        assert "—" in output


# ---------------------------------------------------------------------------
# Issue #18 — _event_type_label tests covering all match arms
# ---------------------------------------------------------------------------


class TestEventTypeLabel:
    """Tests for _event_type_label covering all match arms."""

    @pytest.mark.parametrize(
        "event_type,expected_text",
        [
            (EventType.USER_MESSAGE, "user message"),
            (EventType.ASSISTANT_MESSAGE, "assistant"),
            (EventType.TOOL_EXECUTION_COMPLETE, "tool"),
            (EventType.ASSISTANT_TURN_START, "turn start"),
            (EventType.TOOL_EXECUTION_START, "tool start"),
            (EventType.ASSISTANT_TURN_END, "turn end"),
            (EventType.SESSION_START, "session start"),
            (EventType.SESSION_SHUTDOWN, "session end"),
            ("some.future.event", "some.future.event"),
        ],
    )
    def test_label_text(self, event_type: str, expected_text: str) -> None:
        label = _event_type_label(event_type)
        assert label.plain == expected_text


# ---------------------------------------------------------------------------
# Issue #18 — _format_relative_time hours branch
# ---------------------------------------------------------------------------


class TestFormatRelativeTime:
    def test_hours_branch(self) -> None:
        delta = timedelta(hours=2, minutes=5, seconds=30)
        assert _format_relative_time(delta) == "+2:05:30"

    def test_minutes_only(self) -> None:
        delta = timedelta(minutes=3, seconds=15)
        assert _format_relative_time(delta) == "+3:15"


# ---------------------------------------------------------------------------
# Issue #18 — _format_detail_duration hours and seconds branches
# ---------------------------------------------------------------------------


class TestFormatDetailDuration:
    def test_hours_branch(self) -> None:
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=2, minutes=30)
        assert _format_detail_duration(start, end) == "2h 30m"

    def test_seconds_branch(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = start + timedelta(seconds=45)
        assert _format_detail_duration(start, end) == "45s"


# ---------------------------------------------------------------------------
# Issue #18 — Integration: TOOL_EXECUTION_START and ASSISTANT_TURN_END
# ---------------------------------------------------------------------------


class TestRenderSessionDetailLabelIntegration:
    """Labels for tool-start and turn-end appear in rendered output."""

    def test_tool_start_and_turn_end_labels(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.TOOL_EXECUTION_START,
                data={},
                timestamp=start + timedelta(seconds=10),
            ),
            _make_event(
                EventType.ASSISTANT_TURN_END,
                data={},
                timestamp=start + timedelta(seconds=20),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "tool start" in output
        assert "turn end" in output


# ---------------------------------------------------------------------------
# Issue #19 — _aggregate_model_metrics direct tests
# ---------------------------------------------------------------------------


class TestAggregateModelMetrics:
    """Direct unit tests for _aggregate_model_metrics."""

    def test_same_model_two_sessions_sums_fields(self) -> None:
        s1 = SessionSummary(
            session_id="s1",
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=2),
                    usage=TokenUsage(
                        inputTokens=100,
                        outputTokens=50,
                        cacheReadTokens=10,
                        cacheWriteTokens=5,
                    ),
                )
            },
        )
        s2 = SessionSummary(
            session_id="s2",
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=4),
                    usage=TokenUsage(
                        inputTokens=200,
                        outputTokens=80,
                        cacheReadTokens=20,
                        cacheWriteTokens=15,
                    ),
                )
            },
        )
        merged = _aggregate_model_metrics([s1, s2])
        m = merged["claude-sonnet-4"]
        assert m.requests.count == 10
        assert m.requests.cost == 6
        assert m.usage.inputTokens == 300
        assert m.usage.outputTokens == 130
        assert m.usage.cacheReadTokens == 30
        assert m.usage.cacheWriteTokens == 20

    def test_different_models_kept_separate(self) -> None:
        s1 = SessionSummary(
            session_id="s1",
            model_metrics={"model-a": ModelMetrics(usage=TokenUsage(outputTokens=100))},
        )
        s2 = SessionSummary(
            session_id="s2",
            model_metrics={"model-b": ModelMetrics(usage=TokenUsage(outputTokens=200))},
        )
        merged = _aggregate_model_metrics([s1, s2])
        assert "model-a" in merged and "model-b" in merged
        assert merged["model-a"].usage.outputTokens == 100

    def test_empty_list_returns_empty(self) -> None:
        assert _aggregate_model_metrics([]) == {}

    def test_session_with_empty_model_metrics(self) -> None:
        s1 = SessionSummary(
            session_id="s1",
            model_metrics={
                "model-a": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=3),
                    usage=TokenUsage(outputTokens=100),
                )
            },
        )
        s2 = SessionSummary(session_id="s2", model_metrics={})
        merged = _aggregate_model_metrics([s1, s2])
        assert merged["model-a"].requests.count == 5
        assert merged["model-a"].usage.outputTokens == 100


# ---------------------------------------------------------------------------
# Issue #499 — _aggregate_model_metrics O(m) copy overhead
# ---------------------------------------------------------------------------


class TestAggregateModelMetricsPerformance:
    """Verify in-place accumulation correctness and O(m) copy overhead."""

    _MODEL_NAMES: list[str] = ["model-a", "model-b", "model-c"]

    @staticmethod
    def _make_session(sid: str, model_names: list[str]) -> SessionSummary:
        """Build a session with one ModelMetrics entry per *model_names*."""
        return SessionSummary(
            session_id=sid,
            model_metrics={
                name: ModelMetrics(
                    requests=RequestMetrics(count=2, cost=1),
                    usage=TokenUsage(
                        inputTokens=100,
                        outputTokens=50,
                        cacheReadTokens=10,
                        cacheWriteTokens=5,
                    ),
                )
                for name in model_names
            },
        )

    def test_many_sessions_correct_totals(self) -> None:
        """50+ sessions × 3 models must sum correctly."""
        n = 50
        sessions = [self._make_session(f"s{i}", self._MODEL_NAMES) for i in range(n)]
        merged = _aggregate_model_metrics(sessions)

        assert set(merged.keys()) == set(self._MODEL_NAMES)
        for name in self._MODEL_NAMES:
            m = merged[name]
            assert m.requests.count == 2 * n
            assert m.requests.cost == 1 * n
            assert m.usage.inputTokens == 100 * n
            assert m.usage.outputTokens == 50 * n
            assert m.usage.cacheReadTokens == 10 * n
            assert m.usage.cacheWriteTokens == 5 * n

    def test_copy_called_once_per_unique_model(self) -> None:
        """copy_model_metrics should be called exactly len(unique_models) times."""
        n = 60
        sessions = [self._make_session(f"s{i}", self._MODEL_NAMES) for i in range(n)]
        with patch(
            "copilot_usage.report.copy_model_metrics",
            wraps=copy_model_metrics,
        ) as mock_copy:
            _aggregate_model_metrics(sessions)
            assert mock_copy.call_count == len(self._MODEL_NAMES)


# ---------------------------------------------------------------------------
# Issue #19 — _filter_sessions with None start_time
# ---------------------------------------------------------------------------


class TestFilterSessionsNoneStartTime:
    def test_none_start_time_excluded_when_filtering(self) -> None:
        no_time = SessionSummary(session_id="no-time")
        with_time = SessionSummary(
            session_id="with-time",
            start_time=datetime(2025, 6, 1, tzinfo=UTC),
        )
        since = datetime(2025, 1, 1, tzinfo=UTC)
        result = _filter_sessions([no_time, with_time], since=since, until=None)
        ids = [s.session_id for s in result]
        assert "no-time" not in ids
        assert "with-time" in ids

    def test_none_start_time_included_when_no_bounds(self) -> None:
        session = SessionSummary(session_id="s", start_time=None)
        result = _filter_sessions([session], since=None, until=None)
        assert [s.session_id for s in result] == ["s"]

    def test_none_start_time_excluded_with_until_only(self) -> None:
        """until-only filter still excludes sessions with start_time=None."""
        no_time = SessionSummary(session_id="no-time")
        in_range = SessionSummary(
            session_id="in-range",
            start_time=datetime(2025, 3, 1, tzinfo=UTC),
        )
        until = datetime(2025, 6, 1, tzinfo=UTC)
        result = _filter_sessions([no_time, in_range], since=None, until=until)
        ids = [s.session_id for s in result]
        assert "no-time" not in ids, (
            "start_time=None session must be excluded by until-filter"
        )
        assert "in-range" in ids


# ---------------------------------------------------------------------------
# Issue #19 — _render_totals singular grammar
# ---------------------------------------------------------------------------


class TestRenderTotalsSingularLabels:
    def test_one_session_one_premium_request(self) -> None:
        """render_summary with 1 session / 1 premium request uses singular labels."""
        session = SessionSummary(
            session_id="single-sess",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            total_premium_requests=1,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=1, cost=1),
                    usage=TokenUsage(outputTokens=50),
                )
            },
        )
        output = _capture_summary([session])
        # Output contains ANSI codes around numbers, so check label forms
        assert "premium request " in output  # singular (trailing space)
        assert "premium requests" not in output
        # "session" appears without trailing 's'
        stripped = output.replace("sessions", "")
        assert "session" in stripped


# ---------------------------------------------------------------------------
# Issue #161 / #454 — _filter_sessions reversed date range (silent, no warning)
# ---------------------------------------------------------------------------


class TestFilterSessionsReversedDateRange:
    def test_reversed_since_until_returns_empty(self) -> None:
        """Passing since > until silently returns empty (no UserWarning)."""
        session = SessionSummary(
            session_id="s1",
            start_time=datetime(2026, 6, 15, tzinfo=UTC),
        )
        since = datetime(2026, 12, 31, tzinfo=UTC)
        until = datetime(2026, 1, 1, tzinfo=UTC)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _filter_sessions([session], since=since, until=until)
        assert result == []
        assert len(caught) == 0

    def test_reversed_range_returns_empty_on_empty_list(self) -> None:
        """Reversed range returns empty with no warning even for empty input."""
        since = datetime(2026, 12, 31, tzinfo=UTC)
        until = datetime(2026, 1, 1, tzinfo=UTC)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _filter_sessions([], since=since, until=until)
        assert result == []
        assert len(caught) == 0

    def test_normal_range_no_warning(self) -> None:
        """Passing since < until does NOT emit a warning."""
        session = SessionSummary(
            session_id="s1",
            start_time=datetime(2026, 6, 15, tzinfo=UTC),
        )
        since = datetime(2026, 1, 1, tzinfo=UTC)
        until = datetime(2026, 12, 31, tzinfo=UTC)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _filter_sessions([session], since=since, until=until)
        assert len(result) == 1
        assert len(caught) == 0

    def test_render_summary_reversed_range_shows_no_sessions(self) -> None:
        """render_summary with since > until prints 'No sessions found'."""
        session = SessionSummary(
            session_id="s1",
            start_time=datetime(2026, 6, 15, tzinfo=UTC),
        )
        since = datetime(2026, 12, 31, tzinfo=UTC)
        until = datetime(2026, 1, 1, tzinfo=UTC)
        output = _capture_summary([session], since=since, until=until)
        assert "No sessions found" in output

    def test_render_cost_view_reversed_range_shows_no_sessions(self) -> None:
        """render_cost_view with since > until prints 'No sessions found'."""
        session = SessionSummary(
            session_id="s1",
            start_time=datetime(2026, 6, 15, tzinfo=UTC),
        )
        since = datetime(2026, 12, 31, tzinfo=UTC)
        until = datetime(2026, 1, 1, tzinfo=UTC)
        output = _capture_cost_view([session], since=since, until=until)
        assert "No sessions found" in output


# ---------------------------------------------------------------------------
# Issue #240 — _filter_sessions naive start_time vs aware since/until
# ---------------------------------------------------------------------------


class TestFilterSessionsNaiveStartTime:
    """Regression: naive start_time must not raise TypeError against aware bounds."""

    def test_naive_start_time_with_aware_since_included(self) -> None:
        """Naive start_time after aware since should be included, not raise."""
        session = SessionSummary(
            session_id="naive",
            start_time=datetime(2026, 6, 15),  # naive
        )
        since = datetime(2026, 1, 1, tzinfo=UTC)
        result = _filter_sessions([session], since=since, until=None)
        assert len(result) == 1
        assert result[0].session_id == "naive"

    def test_naive_start_time_with_aware_until_included(self) -> None:
        """Naive start_time before aware until should be included, not raise."""
        session = SessionSummary(
            session_id="naive",
            start_time=datetime(2026, 6, 15),  # naive
        )
        until = datetime(2026, 12, 31, tzinfo=UTC)
        result = _filter_sessions([session], since=None, until=until)
        assert len(result) == 1
        assert result[0].session_id == "naive"

    def test_naive_start_time_before_since_excluded(self) -> None:
        """Naive start_time before aware since should be excluded."""
        session = SessionSummary(
            session_id="old-naive",
            start_time=datetime(2025, 1, 1),  # naive, before since
        )
        since = datetime(2026, 1, 1, tzinfo=UTC)
        result = _filter_sessions([session], since=since, until=None)
        assert result == []

    def test_naive_start_time_after_until_excluded(self) -> None:
        """Naive start_time after aware until should be excluded."""
        session = SessionSummary(
            session_id="future-naive",
            start_time=datetime(2027, 1, 1),  # naive, after until
        )
        until = datetime(2026, 12, 31, tzinfo=UTC)
        result = _filter_sessions([session], since=None, until=until)
        assert result == []


# ---------------------------------------------------------------------------
# Issue #208 — _render_model_table shows Cache Write column
# ---------------------------------------------------------------------------


class TestRenderModelTable:
    """Verify _render_model_table renders the Cache Write column."""

    def test_cache_write_column_present(self) -> None:
        """Cache Write header and formatted value appear in output."""
        session = SessionSummary(
            session_id="cw-test",
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=3),
                    usage=TokenUsage(
                        inputTokens=10_000,
                        outputTokens=2_000,
                        cacheReadTokens=8_000,
                        cacheWriteTokens=5_000,
                    ),
                )
            },
        )
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        _render_model_table(console, [session])
        output = buf.getvalue()
        assert "Cache Write" in output
        assert "5.0K" in output

    def test_cache_write_zero_renders(self) -> None:
        """Zero cacheWriteTokens still produces a Cache Write column."""
        session = SessionSummary(
            session_id="cw-zero",
            model_metrics={
                "gpt-5.1": ModelMetrics(
                    requests=RequestMetrics(count=1, cost=1),
                    usage=TokenUsage(
                        inputTokens=100,
                        outputTokens=50,
                        cacheReadTokens=0,
                        cacheWriteTokens=0,
                    ),
                )
            },
        )
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        _render_model_table(console, [session])
        output = buf.getvalue()
        assert "Cache Write" in output


# ---------------------------------------------------------------------------
# has_active_period_stats
# ---------------------------------------------------------------------------


class TestSessionDetailFallbackToNow:
    """Issue #230 — render_session_detail fallback when start_time and first event timestamp are None."""

    def test_session_detail_no_start_time_no_event_timestamp(self) -> None:
        """Both summary.start_time and events[0].timestamp are None → falls back to datetime.now(UTC)."""
        from copilot_usage.report import render_session_detail

        now = datetime.now(tz=UTC)
        summary = SessionSummary(session_id="no-anchor", start_time=None)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "first"},
                timestamp=None,
            ),
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "second"},
                timestamp=now,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Recent Events" in output
        # The second event's relative time should be approximately +0:00
        # since session_start falls back to datetime.now(UTC)
        assert "+0:00" in output


class TestSessionDetailNaiveEventFallback:
    """Issue #370 — render_session_detail with naive timestamp fallback."""

    def test_single_naive_event_no_start_time(self) -> None:
        """A single event with a naive timestamp and start_time=None must not raise TypeError."""
        from copilot_usage.report import render_session_detail

        naive_ts = datetime(2025, 6, 1, 12, 0, 0)  # noqa: DTZ001
        summary = SessionSummary(session_id="naive-only", start_time=None)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "hello"},
                timestamp=naive_ts,
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Recent Events" in output


class TestHistoricalSectionZeroPremiumWithMetrics:
    """Issue #230 — completed session with 0 premium requests but non-empty model_metrics."""

    def test_zero_premium_with_model_metrics_appears_in_historical(self) -> None:
        """Completed session using only free/low-multiplier models should still appear in historical."""
        session = SessionSummary(
            session_id="free-model-sess-01",
            name="FreeModelSession",
            model="gpt-5-mini",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=False,
            total_premium_requests=0,
            user_messages=3,
            model_calls=4,
            model_metrics={
                "gpt-5-mini": ModelMetrics(
                    requests=RequestMetrics(count=4, cost=0),
                    usage=TokenUsage(outputTokens=800),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Historical Totals" in output
        assert "FreeModelSession" in output


class TestHistoricalSectionResumedFreeSessions:
    """Issue #377 — resumed sessions with 0 premium requests and has_shutdown_metrics."""

    def test_resumed_free_session_appears_in_historical(self) -> None:
        """Resumed session (is_active=True, has_shutdown_metrics=True, total_premium_requests=0)
        must appear in the historical section."""
        session = SessionSummary(
            session_id="resumed-free-model-1234",
            name="ResumedFreeSession",
            model="gpt-5-mini",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            total_premium_requests=0,
            user_messages=10,
            model_calls=20,
            active_model_calls=3,
            active_output_tokens=500,
            model_metrics={
                "gpt-5-mini": ModelMetrics(
                    requests=RequestMetrics(count=17, cost=0),
                    usage=TokenUsage(outputTokens=8000),
                )
            },
        )
        output = _capture_full_summary([session])
        assert "Historical Totals" in output
        assert "ResumedFreeSession" in output

    def test_resumed_free_session_appears_in_active_section(self) -> None:
        """Same resumed session must also appear in the active section."""
        session = SessionSummary(
            session_id="resumed-free-model-1234",
            name="ResumedFreeSession",
            model="gpt-5-mini",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            total_premium_requests=0,
            user_messages=10,
            model_calls=20,
            active_model_calls=3,
            active_output_tokens=500,
            model_metrics={
                "gpt-5-mini": ModelMetrics(
                    requests=RequestMetrics(count=17, cost=0),
                    usage=TokenUsage(outputTokens=8000),
                )
            },
        )
        output = _capture_full_summary([session])
        # Active section table title should indicate it is scoped to the period
        # since the last shutdown, which distinguishes it from the historical table.
        assert "Since Last Shutdown" in output
        # The same resumed session should appear in both the historical and active sections.
        assert output.count("ResumedFreeSession") == 2
        # And the generic empty-active message should not be shown.
        assert "No active sessions" not in output

    def test_pure_active_session_not_in_historical(self) -> None:
        """Pure active session (is_active=True, has_shutdown_metrics=False,
        total_premium_requests=0) must NOT appear in the historical section."""
        session = SessionSummary(
            session_id="pure-active-1234",
            name="PureActiveSession",
            model="gpt-5-mini",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=False,
            total_premium_requests=0,
            user_messages=2,
            model_calls=1,
            active_model_calls=1,
            active_output_tokens=100,
        )
        output = _capture_full_summary([session])
        assert "No historical shutdown data" in output


class TestBuildEventDetailsCatchAll:
    """Issue #230 — _build_event_details catch-all branch for event types without explicit details."""

    @pytest.mark.parametrize(
        "event_type",
        [
            EventType.SESSION_START,
            EventType.SESSION_RESUME,
            EventType.ABORT,
            EventType.SESSION_ERROR,
            EventType.SESSION_PLAN_CHANGED,
            EventType.SESSION_WORKSPACE_FILE_CHANGED,
            EventType.TOOL_EXECUTION_START,
            EventType.ASSISTANT_TURN_START,
            EventType.ASSISTANT_TURN_END,
        ],
    )
    def test_catch_all_returns_empty_string(self, event_type: str) -> None:
        ev = _make_event(event_type, data={"sessionId": "s1"})
        assert _build_event_details(ev) == ""


class TestHasActivePeriodStats:
    """Tests for the has_active_period_stats helper."""

    def test_returns_true_with_last_resume_time(self) -> None:
        """Resumed session with last_resume_time set returns True."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="resumed-session-1234",
            is_active=True,
            start_time=now - timedelta(hours=2),
            last_resume_time=now - timedelta(minutes=5),
            user_messages=50,
            active_user_messages=0,
            active_output_tokens=0,
            active_model_calls=0,
        )
        assert has_active_period_stats(session) is True

    def test_returns_true_with_active_user_messages(self) -> None:
        """Session with positive active_user_messages returns True."""
        session = SessionSummary(
            session_id="active-msgs-1234",
            is_active=True,
            active_user_messages=5,
            user_messages=5,
            active_output_tokens=0,
            active_model_calls=0,
        )
        assert has_active_period_stats(session) is True

    def test_returns_true_with_active_output_tokens(self) -> None:
        """Session with positive active_output_tokens returns True."""
        session = SessionSummary(
            session_id="active-tokens-1234",
            is_active=True,
            active_user_messages=0,
            active_output_tokens=1000,
            active_model_calls=0,
        )
        assert has_active_period_stats(session) is True

    def test_returns_true_with_active_model_calls(self) -> None:
        """Session with positive active_model_calls returns True."""
        session = SessionSummary(
            session_id="active-calls-1234",
            is_active=True,
            model_calls=3,
            active_user_messages=0,
            active_output_tokens=0,
            active_model_calls=3,
        )
        assert has_active_period_stats(session) is True

    def test_returns_false_for_pure_active_never_shutdown(self) -> None:
        """Pure-active session with no shutdown and all active_* counters zero returns False."""
        session = SessionSummary(
            session_id="pure-active-1234",
            is_active=True,
            start_time=datetime.now(tz=UTC) - timedelta(minutes=10),
            user_messages=8,
            model_calls=5,
            active_user_messages=0,
            active_output_tokens=0,
            active_model_calls=0,
        )
        assert has_active_period_stats(session) is False


class TestEffectiveStats:
    """Tests for the _effective_stats helper."""

    def test_returns_active_stats_when_active_period_present(self) -> None:
        """Session with active-period stats returns active_* field values."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="resumed-eff-1234",
            is_active=True,
            start_time=now - timedelta(hours=1),
            last_resume_time=now - timedelta(minutes=5),
            model_calls=100,
            user_messages=50,
            active_model_calls=7,
            active_user_messages=3,
            active_output_tokens=2500,
            model_metrics={
                "gpt-4": ModelMetrics(usage=TokenUsage(outputTokens=9000)),
            },
        )
        stats = _effective_stats(session)
        assert isinstance(stats, _EffectiveStats)
        assert stats.model_calls == 7
        assert stats.user_messages == 3
        assert stats.output_tokens == 2500

    def test_returns_session_totals_when_no_active_period(self) -> None:
        """Pure-active session without active-period stats falls back to totals."""
        session = SessionSummary(
            session_id="pure-active-eff-1234",
            is_active=True,
            start_time=datetime.now(tz=UTC) - timedelta(minutes=10),
            model_calls=12,
            user_messages=8,
            active_model_calls=0,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "gpt-4": ModelMetrics(usage=TokenUsage(outputTokens=4200)),
            },
        )
        stats = _effective_stats(session)
        assert isinstance(stats, _EffectiveStats)
        assert stats.model_calls == 12
        assert stats.user_messages == 8
        # Falls back to total_output_tokens which sums model_metrics
        assert stats.output_tokens == 4200

    def test_frozen_dataclass(self) -> None:
        """_EffectiveStats instances are immutable."""
        session = SessionSummary(
            session_id="frozen-test-1234",
            model_calls=1,
            active_model_calls=1,
        )
        stats = _effective_stats(session)
        with pytest.raises(AttributeError):
            stats.model_calls = 99  # type: ignore[misc]


class TestComputeSessionTotals:
    """Tests for _compute_session_totals helper."""

    def test_empty_list(self) -> None:
        """An empty session list yields all-zero totals."""
        totals = _compute_session_totals([])
        assert totals.premium == 0
        assert totals.model_calls == 0
        assert totals.user_messages == 0
        assert totals.api_duration_ms == 0
        assert totals.output_tokens == 0
        assert totals.session_count == 0

    def test_single_session(self) -> None:
        """A single session's values are reflected exactly."""
        session = SessionSummary(
            session_id="single-session",
            total_premium_requests=10,
            model_calls=5,
            user_messages=3,
            total_api_duration_ms=2000,
            model_metrics={
                "gpt-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )
        totals = _compute_session_totals([session])
        assert totals.premium == 10
        assert totals.model_calls == 5
        assert totals.user_messages == 3
        assert totals.api_duration_ms == 2000
        assert totals.output_tokens == 500
        assert totals.session_count == 1

    def test_multiple_sessions(self) -> None:
        """Totals are summed across multiple sessions."""
        s1 = SessionSummary(
            session_id="s1",
            total_premium_requests=10,
            model_calls=5,
            user_messages=3,
            total_api_duration_ms=2000,
            model_metrics={
                "gpt-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )
        s2 = SessionSummary(
            session_id="s2",
            total_premium_requests=20,
            model_calls=15,
            user_messages=7,
            total_api_duration_ms=3000,
            model_metrics={
                "gpt-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=1000),
                ),
            },
        )
        totals = _compute_session_totals([s1, s2])
        assert totals.premium == 30
        assert totals.model_calls == 20
        assert totals.user_messages == 10
        assert totals.api_duration_ms == 5000
        assert totals.output_tokens == 1500
        assert totals.session_count == 2

    def test_sessions_with_multiple_models(self) -> None:
        """Output tokens are summed across all models in all sessions."""
        session = SessionSummary(
            session_id="multi-model",
            total_premium_requests=5,
            model_calls=4,
            user_messages=2,
            total_api_duration_ms=1000,
            model_metrics={
                "gpt-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=300),
                ),
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=700),
                ),
            },
        )
        totals = _compute_session_totals([session])
        assert totals.output_tokens == 1000
        assert totals.session_count == 1

    def test_shutdown_only_subtracts_active_counts_for_resumed_session(self) -> None:
        """shutdown_only=True: resumed session contributes only shutdown-period calls."""
        session = SessionSummary(
            session_id="resumed-so",
            total_premium_requests=10,
            model_calls=500,
            active_model_calls=200,
            user_messages=100,
            active_user_messages=40,
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=1_000_000),
                ),
            },
        )
        totals = _compute_session_totals([session], shutdown_only=True)
        assert totals.model_calls == 300  # 500 - 200
        assert totals.user_messages == 60  # 100 - 40

    def test_shutdown_only_no_effect_for_completed_session(self) -> None:
        """shutdown_only=True: completed session (no active stats) counts unchanged."""
        session = SessionSummary(
            session_id="completed-so",
            total_premium_requests=5,
            model_calls=80,
            user_messages=30,
            is_active=False,
            has_shutdown_metrics=True,
        )
        totals = _compute_session_totals([session], shutdown_only=True)
        assert totals.model_calls == 80
        assert totals.user_messages == 30

    def test_frozen_dataclass(self) -> None:
        """_SessionTotals instances are immutable."""
        totals = _compute_session_totals([])
        with pytest.raises(AttributeError):
            totals.premium = 42  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Issue #237 — Direct unit tests for _truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_exact_boundary_no_truncation(self) -> None:
        """len(text) == max_len should return the original string unchanged."""
        assert _truncate("hello", max_len=5) == "hello"

    def test_shorter_than_max_no_truncation(self) -> None:
        """len(text) < max_len should return the original string unchanged."""
        assert _truncate("hello", max_len=6) == "hello"

    def test_truncation_appends_ellipsis(self) -> None:
        """When text exceeds max_len, result is max_len chars ending with '…'."""
        result = _truncate("hello world", max_len=8)
        assert result == "hello w…"
        assert len(result) == 8

    def test_unicode_slice_by_codepoint(self) -> None:
        """Truncation slices by codepoint index, not byte offset."""
        text = "👋" * 10  # 10 codepoints
        result = _truncate(text, max_len=5)
        assert len(result) == 5
        assert result.endswith("…")

    def test_max_len_zero_returns_empty(self) -> None:
        """max_len=0 must return an empty string."""
        assert _truncate("hello", max_len=0) == ""

    def test_max_len_one_returns_ellipsis(self) -> None:
        """max_len=1 with text longer than 1 returns just the ellipsis."""
        assert _truncate("hello", max_len=1) == "…"

    def test_max_len_one_single_char_no_truncation(self) -> None:
        """max_len=1 with a single-char string returns it unchanged."""
        assert _truncate("x", max_len=1) == "x"


# ---------------------------------------------------------------------------
# Issue #237 — Direct unit tests for _format_elapsed_since
# ---------------------------------------------------------------------------


class TestFormatElapsedSince:
    def test_hours_branch(self) -> None:
        """When elapsed >= 1 hour, format is 'Xh Ym'."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        start = now - timedelta(hours=2, minutes=15)
        with patch("copilot_usage.report.datetime", wraps=datetime) as mock_dt:
            mock_dt.now.return_value = now
            result = _format_elapsed_since(start)
        assert result == "2h 15m"

    def test_minutes_seconds_branch(self) -> None:
        """When elapsed < 1 hour, format is 'Ym Zs'."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        start = now - timedelta(minutes=5, seconds=30)
        with patch("copilot_usage.report.datetime", wraps=datetime) as mock_dt:
            mock_dt.now.return_value = now
            result = _format_elapsed_since(start)
        assert result == "5m 30s"

    def test_zero_elapsed(self) -> None:
        """When start == now, format is '0ms'."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        with patch("copilot_usage.report.datetime", wraps=datetime) as mock_dt:
            mock_dt.now.return_value = now
            result = _format_elapsed_since(now)
        assert result == "0ms"


# ---------------------------------------------------------------------------
# Issue #237 — Boundary tests for _format_detail_duration
# ---------------------------------------------------------------------------


class TestFormatDetailDurationBoundaries:
    def test_exactly_60_seconds(self) -> None:
        """60s sits on the < 60 boundary — should produce '1m'."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        assert _format_detail_duration(start, start + timedelta(seconds=60)) == "1m"

    def test_exactly_3600_seconds(self) -> None:
        """3600s sits on the minutes < 60 boundary — should produce '1h'."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        assert _format_detail_duration(start, start + timedelta(seconds=3600)) == "1h"

    def test_start_none(self) -> None:
        """None start should return em-dash."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        assert _format_detail_duration(None, start) == "—"

    def test_end_none(self) -> None:
        """None end should return em-dash."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        assert _format_detail_duration(start, None) == "—"


# ---------------------------------------------------------------------------
# Issue #243 — Unit tests for format_timedelta core helper
# ---------------------------------------------------------------------------


class TestFormatTimedelta:
    def test_zero(self) -> None:
        assert format_timedelta(timedelta(0)) == "0ms"

    def test_seconds_only(self) -> None:
        assert format_timedelta(timedelta(seconds=5)) == "5s"

    def test_minutes_and_seconds(self) -> None:
        assert format_timedelta(timedelta(minutes=6, seconds=29)) == "6m 29s"

    def test_exact_minute(self) -> None:
        assert format_timedelta(timedelta(minutes=1)) == "1m"

    def test_exact_hour(self) -> None:
        assert format_timedelta(timedelta(hours=1)) == "1h"

    def test_hours_minutes_seconds(self) -> None:
        assert format_timedelta(timedelta(hours=1, minutes=1, seconds=1)) == "1h 1m 1s"

    def test_hours_and_minutes_no_seconds(self) -> None:
        assert format_timedelta(timedelta(hours=2, minutes=30)) == "2h 30m"

    def test_negative_clamped_to_zero(self) -> None:
        assert format_timedelta(timedelta(seconds=-10)) == "0ms"

    def test_large_duration(self) -> None:
        assert (
            format_timedelta(timedelta(hours=100, minutes=5, seconds=3)) == "100h 5m 3s"
        )


# ---------------------------------------------------------------------------
# Issue #250 — naive/aware datetime mixing regression tests
# ---------------------------------------------------------------------------


class TestNaiveDatetimeMixing:
    """Regression: naive start_time must not raise TypeError in any path."""

    def test_render_summary_with_naive_start_times(self) -> None:
        """render_summary with naive start_time sessions does not raise."""
        s1 = _make_summary_session(
            session_id="naive-1",
            name="Naive Session 1",
            start_time=datetime(2026, 3, 1),  # naive
        )
        s2 = _make_summary_session(
            session_id="naive-2",
            name="Naive Session 2",
            start_time=datetime(2026, 6, 1),  # naive
        )
        output = _capture_summary([s1, s2])
        assert "Copilot Usage Summary" in output
        assert "2026-03-01" in output
        assert "2026-06-01" in output

    def test_render_summary_mixed_naive_and_aware(self) -> None:
        """render_summary with a mix of naive and aware start_time does not raise."""
        naive = _make_summary_session(
            session_id="naive",
            name="Naive",
            start_time=datetime(2026, 3, 1),
        )
        aware = _make_summary_session(
            session_id="aware",
            name="Aware",
            start_time=datetime(2026, 6, 1, tzinfo=UTC),
        )
        output = _capture_summary([naive, aware])
        assert "Copilot Usage Summary" in output

    def test_render_session_detail_naive_start_with_aware_events(self) -> None:
        """render_session_detail with naive start_time and aware event timestamps."""
        from copilot_usage.report import render_session_detail

        naive_start = datetime(2026, 3, 8, 1, 11, 20)
        summary = _make_session(start_time=naive_start, is_active=False)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "hello"},
                timestamp=datetime(2026, 3, 8, 1, 12, 0, tzinfo=UTC),
            ),
            _make_event(
                EventType.ASSISTANT_MESSAGE,
                data={"content": "hi", "outputTokens": 10, "messageId": "m1"},
                timestamp=datetime(2026, 3, 8, 1, 12, 30, tzinfo=UTC),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Recent Events" in output


# ---------------------------------------------------------------------------
# Issue #259 — _safe_event_data helper tests
# ---------------------------------------------------------------------------


class TestSafeEventData:
    """Tests for _safe_event_data: returns parsed data or None with debug log."""

    def test_returns_parsed_data_on_success(self) -> None:
        ev = _make_event(
            EventType.USER_MESSAGE,
            data={"content": "hello"},
        )
        result = _safe_event_data(ev, ev.as_user_message)
        assert result is not None
        assert result.content == "hello"

    def test_returns_none_on_validation_error(self) -> None:
        ev = _make_event(EventType.USER_MESSAGE, data={"attachments": 12345})
        result = _safe_event_data(ev, ev.as_user_message)
        assert result is None

    def test_returns_none_on_value_error(self) -> None:
        # Parser for wrong event type raises ValueError
        ev = _make_event(EventType.USER_MESSAGE, data={"content": "hi"})
        result = _safe_event_data(ev, ev.as_session_start)
        assert result is None

    def test_logs_debug_on_failure(self) -> None:
        from loguru import logger

        log_messages: list[str] = []
        handler_id = logger.add(lambda m: log_messages.append(str(m)), level="DEBUG")
        try:
            ev = _make_event(EventType.USER_MESSAGE, data={"attachments": 12345})
            _safe_event_data(ev, ev.as_user_message)
        finally:
            logger.remove(handler_id)
        assert any(
            "Could not parse" in msg and "user.message" in msg for msg in log_messages
        )


# ---------------------------------------------------------------------------
# render_cost_view — active session with model=None (Gap 2 — issue #275)
# ---------------------------------------------------------------------------


class TestRenderCostViewActiveModelNone:
    """Verify the '↳ Since last shutdown' row renders cleanly when model is None."""

    def test_since_last_shutdown_row_with_model_none(self) -> None:
        session = SessionSummary(
            session_id="no-model-active-1234",
            model=None,
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=2,
            active_model_calls=2,
            active_output_tokens=300,
            model_metrics={
                "unknown": ModelMetrics(
                    requests=RequestMetrics(count=0, cost=0),
                    usage=TokenUsage(outputTokens=0),
                ),
            },
        )
        output = _capture_cost_view([session])
        assert "Since last shutdown" in output
        # model column and premium cost column both show "—"
        assert output.count("—") >= 2
        # must not render Python's None literal
        assert "None" not in output

    def test_no_crash_with_model_none_active_session(self) -> None:
        """render_cost_view must not raise for an active session with model=None."""
        session = SessionSummary(
            session_id="safe-active-5678",
            model=None,
            is_active=True,
            model_calls=0,
            active_model_calls=0,
            active_output_tokens=0,
        )
        # Should complete without any exception
        output = _capture_cost_view([session])
        assert "None" not in output


# ---------------------------------------------------------------------------
# Issue #276 — total_output_tokens and resumed-session token accounting
# ---------------------------------------------------------------------------


class TestTotalOutputTokens:
    """Tests for the total_output_tokens helper (issue #276)."""

    def test_non_resumed_session(self) -> None:
        """A normal session returns model_metrics output tokens only."""
        session = SessionSummary(
            session_id="normal-1234",
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )
        assert total_output_tokens(session) == 500

    def test_resumed_session_includes_active_tokens(self) -> None:
        """Resumed session with shutdown data adds active_output_tokens."""
        session = SessionSummary(
            session_id="resumed-1234",
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=250,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        assert total_output_tokens(session) == 600  # 350 + 250

    def test_pure_active_session_no_double_count(self) -> None:
        """Pure-active session (no shutdown data) does not double-count."""
        session = SessionSummary(
            session_id="pure-active-1234",
            is_active=True,
            has_shutdown_metrics=False,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=400,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=0, cost=0),
                    usage=TokenUsage(outputTokens=400),
                ),
            },
        )
        # has_shutdown_metrics=False → should NOT add active tokens
        assert total_output_tokens(session) == 400

    def test_empty_model_metrics(self) -> None:
        """Session with no model_metrics and no active tokens returns 0."""
        session = SessionSummary(
            session_id="empty-1234",
            model_metrics={},
        )
        assert total_output_tokens(session) == 0

    def test_empty_model_metrics_with_active_tokens(self) -> None:
        """Active session with no model_metrics uses active_output_tokens."""
        session = SessionSummary(
            session_id="no-metrics-active",
            is_active=True,
            model_calls=3,
            active_model_calls=3,
            active_output_tokens=500,
            model_metrics={},
        )
        assert total_output_tokens(session) == 500

    def test_multiple_models_resumed(self) -> None:
        """Resumed session sums across models and adds active tokens."""
        session = SessionSummary(
            session_id="multi-model-resumed",
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=100,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=6),
                    usage=TokenUsage(outputTokens=200),
                ),
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=2, cost=4),
                    usage=TokenUsage(outputTokens=300),
                ),
            },
        )
        assert total_output_tokens(session) == 600  # 200 + 300 + 100

    # -- Issue #351 regression tests --

    def test_active_session_with_nonzero_request_count_no_double_count(self) -> None:
        """Active session with requests.count > 0 but has_shutdown_metrics=False.

        Simulates a future scenario where in-flight requests bump count
        without real shutdown data.  Must not double-count.
        """
        session = SessionSummary(
            session_id="active-nonzero-req",
            is_active=True,
            has_shutdown_metrics=False,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=400,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=0),
                    usage=TokenUsage(outputTokens=400),
                ),
            },
        )
        assert total_output_tokens(session) == 400

    def test_resumed_session_with_shutdown_adds_active_tokens(self) -> None:
        """Resumed session with has_shutdown_metrics=True adds active tokens.

        Ensures shutdown_tokens + active_output_tokens, not
        shutdown_tokens + 2 × active_output_tokens.
        """
        shutdown_tokens = 500
        active_tokens = 200
        session = SessionSummary(
            session_id="resumed-explicit-flag",
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=active_tokens,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=20),
                    usage=TokenUsage(outputTokens=shutdown_tokens),
                ),
            },
        )
        assert total_output_tokens(session) == shutdown_tokens + active_tokens

    def test_completed_session_empty_metrics_via_parser(self, tmp_path: Path) -> None:
        """Parser→report integration: shutdown with modelMetrics={} → 0 tokens."""
        p = tmp_path / "s" / "events.jsonl"
        p.parent.mkdir(parents=True)
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "empty-metrics-session",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.0",
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {"cwd": "/home/user/project"},
                },
                "id": "ev-start",
                "timestamp": "2026-03-07T10:00:00.000Z",
                "parentId": None,
            }
        )
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "sessionStartTime": 1772895600000,
                    "modelMetrics": {},
                },
                "id": "ev-shutdown",
                "timestamp": "2026-03-07T11:00:00.000Z",
                "parentId": "ev-start",
            }
        )
        p.write_text(start + "\n" + shutdown + "\n", encoding="utf-8")
        events = parse_events(p)
        summary = build_session_summary(events, session_dir=p.parent)
        assert summary.model_metrics == {}
        assert total_output_tokens(summary) == 0

    def test_resumed_session_empty_shutdown_metrics_via_parser(
        self, tmp_path: Path
    ) -> None:
        """Parser→report integration: shutdown with modelMetrics={} then resume → only active tokens."""
        p = tmp_path / "s" / "events.jsonl"
        p.parent.mkdir(parents=True)
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "empty-metrics-resumed",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.0",
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {"cwd": "/home/user/project"},
                },
                "id": "ev-start",
                "timestamp": "2026-03-07T10:00:00.000Z",
                "parentId": None,
            }
        )
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "sessionStartTime": 1772895600000,
                    "modelMetrics": {},
                },
                "id": "ev-shutdown",
                "timestamp": "2026-03-07T11:00:00.000Z",
                "parentId": "ev-start",
            }
        )
        resume = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume",
                "timestamp": "2026-03-07T12:00:00.000Z",
                "parentId": "ev-shutdown",
            }
        )
        user_msg = json.dumps(
            {
                "type": "user.message",
                "data": {
                    "content": "hello",
                    "transformedContent": "hello",
                    "attachments": [],
                    "interactionId": "int-1",
                },
                "id": "ev-user1",
                "timestamp": "2026-03-07T12:01:00.000Z",
                "parentId": "ev-resume",
            }
        )
        assistant_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-1",
                    "content": "hi there",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": 400,
                },
                "id": "ev-asst1",
                "timestamp": "2026-03-07T12:01:05.000Z",
                "parentId": "ev-user1",
            }
        )
        lines = [start, shutdown, resume, user_msg, assistant_msg]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        events = parse_events(p)
        summary = build_session_summary(events, session_dir=p.parent)
        assert summary.is_active is True
        assert summary.model_metrics == {}
        assert summary.active_output_tokens == 400
        assert total_output_tokens(summary) == 400


class TestRenderAggregateStatsResumedTokens:
    """Issue #290 — _render_aggregate_stats includes active tokens for resumed sessions."""

    def test_aggregate_stats_shows_total_output_tokens_for_resumed_session(
        self,
    ) -> None:
        """Output tokens in Aggregate Stats panel include active_output_tokens for resumed sessions."""
        from copilot_usage.render_detail import _render_aggregate_stats

        session = SessionSummary(
            session_id="agg-resumed-1234",
            model_calls=5,
            user_messages=3,
            total_premium_requests=2,
            total_api_duration_ms=1500,
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=250,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )

        expected_total = total_output_tokens(session)  # 350 + 250 = 600
        assert expected_total == 600

        output = _capture_console(_render_aggregate_stats, session)
        assert format_tokens(expected_total) in output
        # Ensure the shutdown-only baseline is NOT shown
        shutdown_only = format_tokens(350)
        if shutdown_only != format_tokens(expected_total):
            assert shutdown_only not in output


class TestComputeSessionTotalsResumed:
    """Issue #276 — _compute_session_totals includes active tokens for resumed sessions."""

    def test_resumed_session_totals_include_active_tokens(self) -> None:
        """Totals for a resumed session include both historical and active output tokens."""
        session = SessionSummary(
            session_id="resumed-totals",
            total_premium_requests=10,
            model_calls=8,
            user_messages=4,
            total_api_duration_ms=3000,
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=250,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        totals = _compute_session_totals([session])
        assert totals.output_tokens == 600  # 350 + 250

    def test_mixed_sessions_totals(self) -> None:
        """Totals across normal and resumed sessions are correct."""
        normal = SessionSummary(
            session_id="normal-mix",
            total_premium_requests=5,
            model_calls=3,
            user_messages=2,
            total_api_duration_ms=1000,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=6),
                    usage=TokenUsage(outputTokens=400),
                ),
            },
        )
        resumed = SessionSummary(
            session_id="resumed-mix",
            total_premium_requests=8,
            model_calls=6,
            user_messages=3,
            total_api_duration_ms=2000,
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=200,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=4, cost=8),
                    usage=TokenUsage(outputTokens=300),
                ),
            },
        )
        totals = _compute_session_totals([normal, resumed])
        # normal: 400, resumed: 300 + 200 = 500, total: 900
        assert totals.output_tokens == 900


class TestRenderSessionTableResumed:
    """Issue #276 — per-row Output Tokens in render_summary includes post-resume tokens."""

    def test_session_table_includes_active_tokens(self) -> None:
        """render_summary per-row Output Tokens includes post-resume active tokens."""
        session = SessionSummary(
            session_id="resumed-table-1234",
            name="Resumed Session",
            model="gpt-4",
            start_time=datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            model_calls=10,
            user_messages=5,
            active_output_tokens=250,
            active_model_calls=3,
            active_user_messages=2,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=14),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        output = _capture_summary([session])
        # Total should be 600 (350 + 250), displayed as "600"
        assert "600" in output


class TestRenderSessionTableTokenFn:
    """Issue #459 — _render_session_table uses token_fn Callable instead of bool flag."""

    def _build_resumed_session(self) -> SessionSummary:
        """Return a resumed session with distinct shutdown vs total token counts."""
        return SessionSummary(
            session_id="token-fn-test-1234",
            name="TokenFn Session",
            model="gpt-4",
            start_time=datetime(2025, 7, 1, 12, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            model_calls=8,
            user_messages=4,
            active_output_tokens=200,
            active_model_calls=2,
            active_user_messages=1,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=6, cost=12),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )

    def test_shutdown_output_tokens_via_token_fn(self) -> None:
        """token_fn=shutdown_output_tokens uses shutdown-only tokens (500)."""
        session = self._build_resumed_session()
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=120)
        _render_session_table(
            console,
            [session],
            title="Shutdown Only",
            token_fn=shutdown_output_tokens,
        )
        output = buf.getvalue()
        assert format_tokens(500) in output
        # Must NOT contain the total (700 = 500 + 200)
        assert format_tokens(700) not in output

    def test_total_output_tokens_via_token_fn(self) -> None:
        """token_fn=total_output_tokens uses total tokens (500 + 200 = 700)."""
        session = self._build_resumed_session()
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=120)
        _render_session_table(
            console,
            [session],
            title="Total Tokens",
            token_fn=total_output_tokens,
        )
        output = buf.getvalue()
        assert format_tokens(700) in output


class TestRenderCostViewResumed:
    """Issue #276 — render_cost_view grand total matches after refactor."""

    def test_cost_view_resumed_session_grand_total(self) -> None:
        """Grand total output tokens in cost view includes active tokens for resumed sessions."""
        session = SessionSummary(
            session_id="cost-resumed-1234",
            name="Cost Resumed",
            model="gpt-4",
            start_time=datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            model_calls=10,
            user_messages=5,
            active_output_tokens=250,
            active_model_calls=3,
            active_user_messages=2,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=14),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        output = _capture_cost_view([session])
        # Grand total should include 350 (historical) + 250 (active) = 600
        assert "600" in output

    def test_cost_view_pure_active_no_double_count(self) -> None:
        """Pure-active session does not double-count output tokens in cost view."""
        session = SessionSummary(
            session_id="cost-pure-active",
            name="Pure Active Cost",
            model="gpt-4",
            start_time=datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
            is_active=True,
            last_resume_time=datetime.now(tz=UTC),
            model_calls=5,
            user_messages=3,
            active_output_tokens=400,
            active_model_calls=5,
            active_user_messages=3,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=0, cost=0),
                    usage=TokenUsage(outputTokens=400),
                ),
            },
        )
        output = _capture_cost_view([session])
        # Should show 400, NOT 800 (no double-counting)
        assert "800" not in output
        assert "400" in output


# ---------------------------------------------------------------------------
# Issue #409 — per-model row shows shutdown-only model calls
# ---------------------------------------------------------------------------


class TestRenderCostViewModelCallsConsistency:
    """Issue #409 — per-model row must show shutdown-only model calls."""

    def test_resumed_session_per_model_row_shows_shutdown_calls(self) -> None:
        """Per-model row shows shutdown-only model calls (7), not total (10)."""
        session = SessionSummary(
            session_id="calls-resumed-409",
            name="Resumed 409",
            model="gpt-4",
            start_time=datetime(2025, 7, 1, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            model_calls=10,
            user_messages=5,
            active_model_calls=3,
            active_user_messages=2,
            active_output_tokens=250,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=14),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()

        # Find the per-model row (contains "gpt-4" and the session name)
        model_row = [ln for ln in lines if "gpt-4" in ln and "Resumed 409" in ln]
        assert model_row, "Expected a per-model row with session name"
        # Shutdown-only model calls = 10 - 3 = 7; must appear in the Model Calls column
        model_cols = [col.strip() for col in model_row[0].split("│")]
        assert model_cols[5] == "7"

        # The ↳ row shows active-period model calls (3)
        since_row = [ln for ln in lines if "Since last shutdown" in ln]
        assert since_row, "Expected a ↳ Since last shutdown row"
        since_cols = [col.strip() for col in since_row[0].split("│")]
        assert since_cols[5] == "3"

        # Grand total shows the full session total (10)
        grand_row = [ln for ln in lines if "Grand Total" in ln]
        assert grand_row, "Expected a Grand Total row"
        grand_cols = [col.strip() for col in grand_row[0].split("│")]
        assert grand_cols[5] == "10"

    def test_completed_session_no_regression(self) -> None:
        """Completed session (active_model_calls=0) still shows full model_calls."""
        session = SessionSummary(
            session_id="calls-complete-409",
            name="Completed 409",
            model="gpt-4",
            start_time=datetime(2025, 7, 1, 10, 0, tzinfo=UTC),
            is_active=False,
            has_shutdown_metrics=True,
            model_calls=10,
            user_messages=5,
            active_model_calls=0,
            active_user_messages=0,
            active_output_tokens=0,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=10, cost=20),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()

        # Locate the Model Calls column index from a data row (split on │).
        # Rich tables use │ for data rows; Model Calls is at column index 5.
        model_calls_idx = 5

        # Per-model row shows full model calls (10 - 0 = 10)
        model_row = [ln for ln in lines if "gpt-4" in ln and "Completed 409" in ln]
        assert model_row, "Expected a per-model row with session name"
        model_cols = [col.strip() for col in model_row[0].split("│")]
        assert model_cols[model_calls_idx] == "10"

        # No ↳ row for completed sessions
        since_row = [ln for ln in lines if "Since last shutdown" in ln]
        assert not since_row, "Completed session should not have a ↳ row"

        # Grand total also shows 10 in the Model Calls column
        grand_row = [ln for ln in lines if "Grand Total" in ln]
        assert grand_row
        grand_cols = [col.strip() for col in grand_row[0].split("│")]
        assert grand_cols[model_calls_idx] == "10"

    def test_resumed_session_visual_sum_matches_grand_total(self) -> None:
        """Summing per-model row + ↳ row model calls equals grand total."""
        session = SessionSummary(
            session_id="calls-sum-409",
            name="Sum Check",
            model="gpt-4",
            start_time=datetime(2025, 7, 1, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            model_calls=10,
            user_messages=5,
            active_model_calls=3,
            active_user_messages=2,
            active_output_tokens=100,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=14),
                    usage=TokenUsage(outputTokens=200),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()

        # Extract model calls from the per-model, ↳, and grand total rows
        model_row = [ln for ln in lines if "gpt-4" in ln and "Sum Check" in ln]
        since_row = [ln for ln in lines if "Since last shutdown" in ln]
        grand_row = [ln for ln in lines if "Grand Total" in ln]
        assert model_row and since_row and grand_row

        # Rich data rows use │ separators; Model Calls is at column index 5.
        model_calls_idx = 5

        def get_model_calls(line: str) -> int:
            cols = [col.strip() for col in line.split("│")]
            cell = cols[model_calls_idx]
            match = re.search(r"\d+", cell)
            assert match is not None, f"No integer model calls found in cell: {cell!r}"
            return int(match.group(0))

        shutdown_calls = get_model_calls(model_row[0])
        active_calls = get_model_calls(since_row[0])
        total_calls = get_model_calls(grand_row[0])

        # Check individual expected values and the visual sum invariant
        assert shutdown_calls == 7, f"Expected 7 shutdown calls, got {shutdown_calls}"
        assert active_calls == 3, f"Expected 3 active calls, got {active_calls}"
        assert total_calls == 10, f"Expected 10 total calls, got {total_calls}"
        assert shutdown_calls + active_calls == total_calls, (
            f"Expected shutdown ({shutdown_calls}) + active ({active_calls}) "
            f"to equal total ({total_calls})"
        )

    def test_negative_shutdown_model_calls_rejected_by_validator(self) -> None:
        """active_model_calls > model_calls is rejected by the model validator."""
        with pytest.raises(ValidationError):
            SessionSummary(
                session_id="calls-negative-409",
                name="Negative Guard",
                model="gpt-4",
                start_time=datetime(2025, 7, 1, 10, 0, tzinfo=UTC),
                is_active=True,
                has_shutdown_metrics=True,
                last_resume_time=datetime.now(tz=UTC),
                model_calls=3,
                user_messages=5,
                active_model_calls=5,
                active_user_messages=2,
                active_output_tokens=250,
                model_metrics={
                    "gpt-4": ModelMetrics(
                        requests=RequestMetrics(count=2, cost=4),
                        usage=TokenUsage(outputTokens=100),
                    ),
                },
            )


# ---------------------------------------------------------------------------
# Issue #276 — shutdown_output_tokens and render_full_summary split-view
# ---------------------------------------------------------------------------


class TestShutdownOutputTokens:
    """Tests for the shutdown_output_tokens helper."""

    def test_returns_baseline_only(self) -> None:
        """Shutdown helper returns model_metrics total without active tokens."""
        session = SessionSummary(
            session_id="shutdown-only-1234",
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            active_output_tokens=250,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        assert shutdown_output_tokens(session) == 350
        # Contrast with total_output_tokens which includes active
        assert total_output_tokens(session) == 600

    def test_empty_metrics(self) -> None:
        """Empty model_metrics returns 0."""
        session = SessionSummary(session_id="empty-shut", model_metrics={})
        assert shutdown_output_tokens(session) == 0


class TestRenderFullSummaryResumedSplitView:
    """Regression: render_full_summary historical section excludes active tokens."""

    def test_historical_section_excludes_active_tokens(self) -> None:
        """Historical Totals / Sessions (Shutdown Data) must use shutdown-only tokens."""
        resumed = SessionSummary(
            session_id="resumed-split-1234",
            name="Resumed Split",
            model="gpt-4",
            start_time=datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime.now(tz=UTC),
            total_premium_requests=10,
            model_calls=8,
            user_messages=4,
            total_api_duration_ms=3000,
            active_output_tokens=250,
            active_model_calls=3,
            active_user_messages=2,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=350),
                ),
            },
        )
        output = _capture_full_summary([resumed])

        # The historical section should show 350 (shutdown-only),
        # NOT 600 (350 + 250 active).
        # "Historical Totals" panel should contain shutdown-only tokens.
        assert "Historical Totals" in output
        assert "Sessions (Shutdown Data)" in output

        # Active section should show the active-period tokens (250).
        assert "Active Sessions" in output
        assert "250" in output

        # The shutdown-only baseline (350) should appear in the historical table.
        assert "350" in output


# ---------------------------------------------------------------------------
# Issue #308 — hms decomposition helper
# ---------------------------------------------------------------------------


class TestHms:
    def test_zero(self) -> None:
        assert hms(0) == (0, 0, 0)

    def test_seconds_only(self) -> None:
        assert hms(45) == (0, 0, 45)

    def test_minutes_and_seconds(self) -> None:
        assert hms(125) == (0, 2, 5)

    def test_exact_hour(self) -> None:
        assert hms(3600) == (1, 0, 0)

    def test_hours_minutes_seconds(self) -> None:
        assert hms(3661) == (1, 1, 1)

    def test_large_value(self) -> None:
        assert hms(360303) == (100, 5, 3)


# ---------------------------------------------------------------------------
# Issue #308 / #454 — reversed range no longer emits a warning
# ---------------------------------------------------------------------------


class TestReversedRangeNoWarningFromRenderSummary:
    def test_reversed_range_no_warning_from_render_summary(self) -> None:
        """After issue #454, reversed range no longer emits a warning."""
        session = SessionSummary(
            session_id="s1",
            start_time=datetime(2026, 6, 15, tzinfo=UTC),
        )
        since = datetime(2026, 12, 31, tzinfo=UTC)
        until = datetime(2026, 1, 1, tzinfo=UTC)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            render_summary([session], since=since, until=until)

        assert len(caught) == 0


# ---------------------------------------------------------------------------
# Issue #320 — render_cost_view and _render_model_table sort order assertions
# ---------------------------------------------------------------------------


class TestCostViewModelSortOrder:
    """Assert render_cost_view lists models in alphabetical order."""

    def test_cost_view_models_sorted_alphabetically(self) -> None:
        session = SessionSummary(
            session_id="sort-test-1234",
            name="Sort Test",
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            model_metrics={
                "z-model": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=1),
                    usage=TokenUsage(outputTokens=100),
                ),
                "a-model": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=2),
                    usage=TokenUsage(outputTokens=200),
                ),
            },
        )
        output = _capture_cost_view([session])
        a_pos = output.index("a-model")
        z_pos = output.index("z-model")
        assert a_pos < z_pos, "a-model should appear before z-model in sorted output"

    def test_cost_view_single_model(self) -> None:
        """Edge case — single model in metrics dict appears once and correct."""
        session = SessionSummary(
            session_id="single-model-cost",
            name="Single Model",
            start_time=datetime(2025, 2, 1, tzinfo=UTC),
            model_metrics={
                "only-model": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=4),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )
        output = _capture_cost_view([session])
        assert output.count("only-model") == 1


class TestRenderModelTableSortOrder:
    """Assert _render_model_table lists models in alphabetical order."""

    def test_model_table_sorted_alphabetically(self) -> None:
        """Two models via _render_model_table — rows appear in alphabetical order."""
        session = SessionSummary(
            session_id="sort-mt-1234",
            name="Model Table Sort",
            start_time=datetime(2025, 3, 1, tzinfo=UTC),
            model_metrics={
                "z-model": ModelMetrics(
                    requests=RequestMetrics(count=2, cost=1),
                    usage=TokenUsage(outputTokens=50),
                ),
                "a-model": ModelMetrics(
                    requests=RequestMetrics(count=4, cost=3),
                    usage=TokenUsage(outputTokens=150),
                ),
            },
        )
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        _render_model_table(console, [session])
        output = buf.getvalue()
        a_pos = output.index("a-model")
        z_pos = output.index("z-model")
        assert a_pos < z_pos, "a-model should appear before z-model in sorted output"

    def test_model_table_single_model(self) -> None:
        """Edge case — single model appears exactly once."""
        session = SessionSummary(
            session_id="single-mt",
            model_metrics={
                "only-model": ModelMetrics(
                    requests=RequestMetrics(count=1, cost=1),
                    usage=TokenUsage(outputTokens=10),
                ),
            },
        )
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=200)
        _render_model_table(console, [session])
        output = buf.getvalue()
        assert output.count("only-model") == 1


# ---------------------------------------------------------------------------
# session_display_name
# ---------------------------------------------------------------------------


class TestSessionDisplayName:
    def test_returns_name_when_set(self) -> None:
        s = SessionSummary(session_id="abcdef123456789x", name="My Session")
        assert session_display_name(s) == "My Session"

    def test_returns_truncated_id_when_name_is_none(self) -> None:
        s = SessionSummary(session_id="abcdef123456789x", name=None)
        assert session_display_name(s) == "abcdef123456"

    def test_returns_truncated_id_when_name_is_empty(self) -> None:
        s = SessionSummary(session_id="abcdef123456789x", name="")
        assert session_display_name(s) == "abcdef123456"

    def test_short_session_id(self) -> None:
        """When session_id is shorter than 12 chars, return whatever slice gives."""
        s = SessionSummary(session_id="abc", name=None)
        assert session_display_name(s) == "abc"

    def test_empty_session_id_returns_fallback(self) -> None:
        s = SessionSummary(session_id="", name=None)
        assert session_display_name(s) == "(no id)"


# ---------------------------------------------------------------------------
# Issue #355 — _filter_sessions exact timestamp boundary semantics
# ---------------------------------------------------------------------------


class TestFilterSessionsExactBoundary:
    def test_session_at_exact_since_is_included(self) -> None:
        t = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        s = SessionSummary(session_id="s", start_time=t)
        result = _filter_sessions([s], since=t, until=None)
        assert len(result) == 1

    def test_session_at_exact_until_is_included(self) -> None:
        t = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        s = SessionSummary(session_id="s", start_time=t)
        result = _filter_sessions([s], since=None, until=t)
        assert len(result) == 1

    def test_session_at_exact_point_range_is_included(self) -> None:
        """since == until == session.start_time → included."""
        t = datetime(2025, 6, 15, tzinfo=UTC)
        s = SessionSummary(session_id="s", start_time=t)
        result = _filter_sessions([s], since=t, until=t)
        assert len(result) == 1

    def test_session_one_microsecond_before_since_excluded(self) -> None:
        """Session starting 1µs before since is excluded."""
        since = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        s = SessionSummary(
            session_id="s",
            start_time=since - timedelta(microseconds=1),
        )
        result = _filter_sessions([s], since=since, until=None)
        assert result == []

    def test_session_one_microsecond_after_until_excluded(self) -> None:
        """Session starting 1µs after until is excluded."""
        until = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        s = SessionSummary(
            session_id="s",
            start_time=until + timedelta(microseconds=1),
        )
        result = _filter_sessions([s], since=None, until=until)
        assert result == []


# ---------------------------------------------------------------------------
# Issue #345 — _filter_sessions until date-only boundary
# ---------------------------------------------------------------------------


class TestFilterSessionsUntilBoundary:
    def test_until_date_only_includes_sessions_from_that_date(self) -> None:
        """Date-only --until 2026-03-07 (normalized to end-of-day) should include a 10am session."""
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=UTC)
        end_of_day = midnight.replace(hour=23, minute=59, second=59, microsecond=999999)
        session = SessionSummary(
            session_id="test",
            start_time=datetime(2026, 3, 7, 10, 0, 0, tzinfo=UTC),
        )
        # After normalization until = end-of-day
        result = _filter_sessions([session], since=None, until=end_of_day)
        assert len(result) == 1

    def test_until_exact_timestamp_excludes_session_after(self) -> None:
        """--until 2026-03-07T10:00:00 should exclude a session starting at 11am."""
        until = datetime(2026, 3, 7, 10, 0, 0, tzinfo=UTC)
        session = SessionSummary(
            session_id="test",
            start_time=datetime(2026, 3, 7, 11, 0, 0, tzinfo=UTC),
        )
        result = _filter_sessions([session], since=None, until=until)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Issue #755 — _filter_sessions early termination with sorted input
# ---------------------------------------------------------------------------


class TestFilterSessionsEarlyTermination:
    """Verify that _filter_sessions breaks early on sorted newest-first input."""

    def test_ensure_aware_called_at_most_k_plus_one_times(self) -> None:
        """Given 1000 sessions sorted newest-first with since=yesterday,
        ensure_aware must be called at most k+1 times (k matching + 1 that
        triggers the break), not 1000 times."""
        now = datetime(2026, 4, 5, tzinfo=UTC)
        since = now - timedelta(days=1)
        k = 10
        # k recent sessions that pass the filter
        recent = [
            SessionSummary(
                session_id=f"recent-{i}",
                start_time=now - timedelta(hours=i),
            )
            for i in range(k)
        ]
        # 990 old sessions that should be skipped via early break
        old = [
            SessionSummary(
                session_id=f"old-{i}",
                start_time=now - timedelta(days=30 + i),
            )
            for i in range(990)
        ]
        sessions = recent + old

        with patch(
            "copilot_usage.report.ensure_aware",
            wraps=ensure_aware,
        ) as spy:
            result = _filter_sessions(sessions, since=since, until=None)

        assert len(result) == k
        # k calls for matching sessions + 1 call that triggered the break
        assert spy.call_count <= k + 1, (
            f"ensure_aware called {spy.call_count} times; expected <= {k + 1}"
        )

    def test_none_start_time_at_end_excluded_with_break(self) -> None:
        """Sessions with start_time=None must not be included even when
        early termination on ``since`` breaks the loop before reaching them."""
        now = datetime(2026, 4, 5, tzinfo=UTC)
        since = now - timedelta(days=1)
        recent = SessionSummary(
            session_id="recent",
            start_time=now - timedelta(hours=1),
        )
        old = SessionSummary(
            session_id="old",
            start_time=now - timedelta(days=30),
        )
        none_session = SessionSummary(session_id="no-time", start_time=None)
        # newest-first ordering; None-start entry at the very end
        sessions = [recent, old, none_session]
        result = _filter_sessions(sessions, since=since, until=None)
        ids = [s.session_id for s in result]
        assert "recent" in ids
        assert "no-time" not in ids, "start_time=None session must not be included"


# ---------------------------------------------------------------------------
# Issue #391 — smoke test: render_session_detail re-export from report
# ---------------------------------------------------------------------------


class TestRenderSessionDetailReExport:
    """Guard against regressions in the public import path after the
    session-detail extraction into render_detail.py (issue #391)."""

    def test_render_session_detail_importable_from_report(self) -> None:
        """``from copilot_usage.report import render_session_detail`` must resolve."""
        from copilot_usage.report import render_session_detail

        assert callable(render_session_detail)

    def test_render_session_detail_importable_from_render_detail(self) -> None:
        """``from copilot_usage.render_detail import render_session_detail`` must resolve."""
        from copilot_usage.render_detail import render_session_detail

        assert callable(render_session_detail)

    def test_both_imports_are_same_function(self) -> None:
        """The re-exported symbol is the exact same object."""
        from copilot_usage.render_detail import (
            render_session_detail as detail_fn,
        )
        from copilot_usage.report import (
            render_session_detail as report_fn,
        )

        assert report_fn is detail_fn

    def test_render_detail_importable_first_in_fresh_process(self) -> None:
        """Importing render_detail first in a clean interpreter must not fail.

        This catches circular-import regressions that in-process imports
        miss because ``copilot_usage.report`` is already cached in
        ``sys.modules`` by earlier tests.
        """
        import subprocess
        import sys

        try:
            result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-c",
                    "from copilot_usage.render_detail import render_session_detail; "
                    "assert callable(render_session_detail)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "Subprocess import of 'copilot_usage.render_detail' timed out; "
                "possible circular import regression causing the child interpreter "
                "to hang."
            )
        assert result.returncode == 0, (
            f"Importing render_detail first failed:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Issue #418 — Gap 4: _format_relative_time negative delta clamped to zero
# ---------------------------------------------------------------------------


class TestFormatRelativeTimeNegativeDelta:
    """Gap 4: negative timedelta → '+0:00' (clock skew)."""

    def test_negative_delta_clamped_to_zero(self) -> None:
        """Negative timedelta → '+0:00'."""
        assert _format_relative_time(timedelta(seconds=-90)) == "+0:00"

    def test_negative_fractional_clamped_to_zero(self) -> None:
        """Negative fractional seconds → '+0:00'."""
        assert _format_relative_time(timedelta(seconds=-0.5)) == "+0:00"

    def test_zero_delta(self) -> None:
        """Zero timedelta → '+0:00'."""
        assert _format_relative_time(timedelta(seconds=0)) == "+0:00"


class TestRenderSessionDetailEventBeforeSessionStart:
    """Gap 4 integration: event timestamped before session_start shows '+0:00'."""

    def test_event_before_session_start_renders_zero(self) -> None:
        """Event with timestamp before session_start shows '+0:00', not an error."""
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 1, 0, tzinfo=UTC)
        # Event is 5 seconds BEFORE session_start
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "early msg"},
                timestamp=start - timedelta(seconds=5),
            ),
        ]
        summary = _make_session(start_time=start, is_active=False)
        output = _capture_console(render_session_detail, events, summary)
        assert "+0:00" in output
        assert "early msg" in output


# ---------------------------------------------------------------------------
# Issue #418 — Gap 5: _render_recent_events max_events=0 / max_events=1
# ---------------------------------------------------------------------------


class TestRenderRecentEventsMaxEventsBoundary:
    """Gap 5: max_events=0 shows no events; max_events=1 shows only the last."""

    def test_max_events_zero_shows_none(self) -> None:
        """max_events=0 renders zero events (guard against events[-0:] quirk)."""
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": f"z-{i}"},
                timestamp=start + timedelta(seconds=i * 10),
            )
            for i in range(5)
        ]
        output = _capture_console(_render_recent_events, events, start, max_events=0)
        assert "No events to display" in output
        for i in range(5):
            assert f"z-{i}" not in output

    def test_max_events_one_shows_last(self) -> None:
        """max_events=1 renders only the last event."""
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": f"m-{i}"},
                timestamp=start + timedelta(seconds=i * 10),
            )
            for i in range(4)
        ]
        output = _capture_console(_render_recent_events, events, start, max_events=1)
        # Only the last event should appear
        assert "m-3" in output
        # Earlier events should not appear
        for i in range(3):
            assert f"m-{i}" not in output
        assert output.count("user message") == 1

    def test_max_events_negative_shows_none(self) -> None:
        """max_events=-1 produces 'No events to display' — covers the negative branch of the <= 0 guard."""
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        events = [
            _make_event(
                EventType.USER_MESSAGE,
                data={"content": "visible?"},
                timestamp=start + timedelta(seconds=1),
            )
        ]
        output = _capture_console(_render_recent_events, events, start, max_events=-1)
        assert "No events to display" in output
        assert "visible?" not in output


# ---------------------------------------------------------------------------
# Issue #472 — Cleanup: callers guard against empty sessions
# ---------------------------------------------------------------------------


class TestRenderSummaryHeaderEmptyGuard:
    """Callers of _render_summary_header guard against empty sessions."""

    def test_render_summary_guards_empty_sessions(self) -> None:
        """render_summary returns early for empty list."""
        output = _capture_summary([])
        assert "No sessions found" in output

    def test_render_full_summary_guards_empty_sessions(self) -> None:
        """render_full_summary returns early for empty list."""
        output = _capture_full_summary([])
        assert "No sessions found" in output


# ---------------------------------------------------------------------------
# Issue #642 — render_cost_view grand total must *include* output tokens from
# sessions with no model_metrics (per-session row still shows "—")
# ---------------------------------------------------------------------------


class TestRenderCostViewNoModelMetricsGrandTotal:
    """Grand Total Output Tokens must include model-unknown active sessions."""

    def test_grand_total_includes_no_model_metrics_session(self) -> None:
        """Session with model_metrics={} and active_output_tokens>0 must
        contribute to Grand Total output tokens (issue #642)."""
        session = SessionSummary(
            session_id="no-metrics-active-506",
            name="No Metrics Active",
            model=None,
            is_active=True,
            model_calls=3,
            active_model_calls=3,
            active_output_tokens=500,
            model_metrics={},
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        expected = format_tokens(total_output_tokens(session))
        lines = clean.splitlines()
        # Per-session row must show actual token count (issue #734 fix)
        session_row = next(line for line in lines if "No Metrics Active" in line)
        session_cols = [c.strip() for c in session_row.split("│")]
        assert session_cols[6] == expected, (
            f"Per-session output tokens should be {expected}, got '{session_cols[6]}'"
        )
        assert "Grand Total" in clean
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        assert grand_cols[6] == expected, (
            f"Grand Total output tokens should be {expected}, got '{grand_cols[6]}'"
        )


# ---------------------------------------------------------------------------
# Issue #734 — render_cost_view per-session row must show output tokens for
# no-model sessions with active_output_tokens > 0
# ---------------------------------------------------------------------------


class TestRenderCostViewNoModelOutputTokensRow:
    """Per-session row must show actual tokens, not '—', for no-model sessions."""

    def test_no_model_active_output_tokens_shown_in_row(self) -> None:
        """Active session with model_metrics={} and active_output_tokens=1500
        must display the formatted token count in its row and match the
        Grand Total (issue #734)."""
        session = SessionSummary(
            session_id="no-model-active-734",
            name="No Model Active 734",
            model=None,
            is_active=True,
            model_calls=5,
            active_model_calls=5,
            active_output_tokens=1500,
            model_metrics={},
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()

        expected = format_tokens(total_output_tokens(session))

        # 1. Per-session row shows the formatted token count (not "—")
        session_row = next(line for line in lines if "No Model Active 734" in line)
        session_cols = [c.strip() for c in session_row.split("│")]
        assert session_cols[6] == expected, (
            f"Per-session output tokens should be {expected}, got '{session_cols[6]}'"
        )

        # 2. Grand Total row output-token value equals the per-session value
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        assert grand_cols[6] == expected, (
            f"Grand Total output tokens should be {expected}, got '{grand_cols[6]}'"
        )

    def test_no_model_zero_output_tokens_shows_dash(self) -> None:
        """No-model session with zero active_output_tokens still shows '—'."""
        session = SessionSummary(
            session_id="no-model-zero-734",
            name="No Model Zero",
            model=None,
            is_active=True,
            model_calls=2,
            active_model_calls=2,
            active_output_tokens=0,
            model_metrics={},
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        session_row = next(line for line in lines if "No Model Zero" in line)
        session_cols = [c.strip() for c in session_row.split("│")]
        assert session_cols[6] == "—"


class TestRenderSessionTablePreSorted:
    """Issue #541 — _render_session_table skips redundant sort for pre-sorted input."""

    @staticmethod
    def _build_sessions(count: int = 50) -> list[SessionSummary]:
        """Build *count* sessions in descending start_time order (pre-sorted)."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            SessionSummary(
                session_id=f"sess-{i:04d}",
                name=f"Session {i}",
                model="gpt-4",
                start_time=base - timedelta(hours=i),
                is_active=False,
                model_calls=i,
                user_messages=i,
                model_metrics={
                    "gpt-4": ModelMetrics(
                        usage=TokenUsage(outputTokens=100 * (i + 1)),
                    ),
                },
            )
            for i in range(count)
        ]

    def test_pre_sorted_output_preserves_descending_order(self) -> None:
        """Rows appear in the same descending start_time order as the input."""
        sessions = self._build_sessions(50)
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=160)
        _render_session_table(console, sessions, pre_sorted=True)
        output = buf.getvalue()
        # Single pass: capture the match and build indices in one iteration
        found_indices: list[int] = []
        for line in output.splitlines():
            m = re.search(r"Session (\d+)", line)
            if m:
                found_indices.append(int(m.group(1)))
        assert len(found_indices) == 50
        # Must be in ascending index order (0, 1, 2, …) which corresponds
        # to descending start_time (newest first), matching input order.
        assert found_indices == list(range(50))

    def test_pre_sorted_false_sorts_explicitly(self) -> None:
        """When pre_sorted=False the function sorts the input itself."""
        sessions = list(reversed(self._build_sessions(10)))  # ascending = wrong order
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=160)
        _render_session_table(console, sessions, pre_sorted=False)
        output = buf.getvalue()
        # Single pass: capture the match and build indices in one iteration
        found_indices: list[int] = []
        for line in output.splitlines():
            m = re.search(r"Session (\d+)", line)
            if m:
                found_indices.append(int(m.group(1)))
        # Should be re-sorted into descending start_time (index 0 first)
        assert found_indices == list(range(10))


class TestMergeAndAggregateConsistency:
    """Regression: merge_model_metrics and _aggregate_model_metrics must agree."""

    _METRICS: dict[str, ModelMetrics] = {
        "claude-sonnet-4": ModelMetrics(
            requests=RequestMetrics(count=5, cost=3),
            usage=TokenUsage(
                inputTokens=1000,
                outputTokens=500,
                cacheReadTokens=200,
                cacheWriteTokens=100,
            ),
        ),
        "gpt-5.1": ModelMetrics(
            requests=RequestMetrics(count=2, cost=1),
            usage=TokenUsage(
                inputTokens=400,
                outputTokens=150,
                cacheReadTokens=80,
                cacheWriteTokens=40,
            ),
        ),
    }

    def test_identical_field_values(self) -> None:
        """merge_model_metrics({}, data) and _aggregate_model_metrics([session])
        must produce identical field values for the same input.

        This catches future field additions that update only one call-site.
        """
        merged = merge_model_metrics({}, self._METRICS)

        session = SessionSummary(
            session_id="consistency",
            model_metrics=self._METRICS,
        )
        aggregated = _aggregate_model_metrics([session])

        assert set(merged.keys()) == set(aggregated.keys())
        for model_name in merged:
            m = merged[model_name]
            a = aggregated[model_name]
            # Compare full ModelMetrics contents to catch any future field additions
            assert m.model_dump() == a.model_dump()


# ---------------------------------------------------------------------------
# _render_summary_header date range on pre-sorted sessions
# ---------------------------------------------------------------------------


class TestRenderSummaryHeaderDateRange:
    """_render_summary_header finds correct earliest/latest dates from pre-sorted sessions."""

    def test_large_sorted_sessions(self) -> None:
        """≥100 sessions in descending start_time order produce the correct date range."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
        # Build 120 sessions in descending start_time order (newest first)
        # using a deterministic set of day offsets.
        import random

        rng = random.Random(42)  # noqa: S311
        days = list(range(365))
        rng.shuffle(days)
        selected = sorted(days[:120], reverse=True)

        sessions = [
            _make_summary_session(
                session_id=f"s-{i}",
                start_time=base + timedelta(days=d),
            )
            for i, d in enumerate(selected)
        ]

        expected_earliest = (base + timedelta(days=min(selected))).strftime("%Y-%m-%d")
        expected_latest = (base + timedelta(days=max(selected))).strftime("%Y-%m-%d")

        output = _capture_summary(sessions)
        assert f"{expected_earliest}  →  {expected_latest}" in output

    def test_none_start_times_at_end(self) -> None:
        """None start_time entries at the end still yield correct range."""
        sessions = [
            _make_summary_session(
                session_id="late",
                start_time=datetime(2025, 12, 25, tzinfo=UTC),
            ),
            _make_summary_session(
                session_id="mid",
                start_time=datetime(2025, 6, 15, tzinfo=UTC),
            ),
            _make_summary_session(
                session_id="early",
                start_time=datetime(2025, 1, 10, tzinfo=UTC),
            ),
            SessionSummary(session_id="none-1", start_time=None),
            SessionSummary(session_id="none-2", start_time=None),
            SessionSummary(session_id="none-3", start_time=None),
        ]
        output = _capture_summary(sessions)
        assert "2025-01-10  →  2025-12-25" in output

    def test_all_none_start_times(self) -> None:
        """All-None start_time produces 'dates unavailable'."""
        sessions = [
            SessionSummary(session_id=f"none-{i}", start_time=None) for i in range(5)
        ]
        output = _capture_summary(sessions)
        assert "dates unavailable" in output

    def test_single_session_same_earliest_latest(self) -> None:
        """Single session shows same date for both endpoints."""
        sessions = [
            _make_summary_session(
                session_id="only",
                start_time=datetime(2025, 7, 4, tzinfo=UTC),
            ),
        ]
        output = _capture_summary(sessions)
        assert "2025-07-04  →  2025-07-04" in output


# ---------------------------------------------------------------------------
# Issue #597 — _render_summary_header O(1) date range on pre-sorted sessions
# ---------------------------------------------------------------------------


class TestRenderSummaryHeaderO1DateRange:
    """_render_summary_header uses O(1) lookups on pre-sorted sessions."""

    def test_ensure_aware_called_at_most_twice(self) -> None:
        """With 200+ sessions, ensure_aware is called at most twice."""
        base = datetime(2024, 1, 1, tzinfo=UTC)
        # Build 250 sessions in descending start_time order (newest first),
        # with None-start-time entries at the end — the contract from
        # get_all_sessions.
        sessions = [
            _make_summary_session(
                session_id=f"s-{i}",
                start_time=base + timedelta(days=249 - i),
            )
            for i in range(250)
        ] + [SessionSummary(session_id=f"none-{i}", start_time=None) for i in range(10)]

        expected_earliest = base.strftime("%Y-%m-%d")
        expected_latest = (base + timedelta(days=249)).strftime("%Y-%m-%d")

        call_count = 0
        original_ensure_aware = ensure_aware

        def counting_ensure_aware(dt: datetime) -> datetime:
            nonlocal call_count
            call_count += 1
            return original_ensure_aware(dt)

        with patch(
            "copilot_usage.report.ensure_aware",
            side_effect=counting_ensure_aware,
        ):
            output = _capture_full_summary(sessions)

        assert f"{expected_earliest}  →  {expected_latest}" in output
        assert call_count <= 2, (
            f"ensure_aware called {call_count} times, expected at most 2"
        )


# ---------------------------------------------------------------------------
# Issue #615 — _compute_session_totals single-pass optimisation
# ---------------------------------------------------------------------------


class TestComputeSessionTotalsSinglePass:
    """Issue #615 — verify single-pass accumulator correctness at scale."""

    @staticmethod
    def _make_stubs(n: int) -> list[SessionSummary]:
        """Build *n* SessionSummary stubs with deterministic field values."""
        return [
            SessionSummary(
                session_id=f"perf-{i}",
                total_premium_requests=i,
                model_calls=i * 2,
                user_messages=i * 3,
                total_api_duration_ms=i * 10,
                model_metrics={
                    "gpt-4": ModelMetrics(
                        usage=TokenUsage(outputTokens=i * 5),
                    ),
                },
            )
            for i in range(n)
        ]

    def test_large_batch_correctness(self) -> None:
        """1 000-session batch returns the same totals a naive sum would."""
        n = 1_000
        sessions = self._make_stubs(n)
        totals = _compute_session_totals(sessions)

        # Expected values using the triangular-number formula: sum(0..n-1)
        tri = n * (n - 1) // 2
        assert totals.premium == tri
        assert totals.model_calls == tri * 2
        assert totals.user_messages == tri * 3
        assert totals.api_duration_ms == tri * 10
        assert totals.output_tokens == tri * 5
        assert totals.session_count == n

    def test_large_batch_custom_token_fn(self) -> None:
        """Custom *token_fn* is respected for a large batch."""
        sessions = self._make_stubs(500)
        totals = _compute_session_totals(sessions, token_fn=shutdown_output_tokens)
        tri = 500 * 499 // 2
        assert totals.output_tokens == tri * 5
        assert totals.session_count == 500

    class _SinglePassIterable:
        """Iterable wrapper that enforces a single pass over the data."""

        def __init__(self, items: list[SessionSummary]) -> None:
            self._items = items
            self.iter_count = 0

        def __iter__(self):
            if self.iter_count >= 1:
                raise AssertionError("sessions iterable was iterated more than once")
            self.iter_count += 1
            return iter(self._items)

        def __len__(self) -> int:
            return len(self._items)

    def test_sessions_iterated_only_once(self) -> None:
        """_compute_session_totals performs a single pass over the iterable."""
        sessions = self._make_stubs(500)
        wrapped = self._SinglePassIterable(sessions)
        totals = _compute_session_totals(wrapped)  # type: ignore[arg-type]

        # Verify we only iterated once over the sessions iterable.
        assert wrapped.iter_count == 1

        # Sanity-check that totals are still correct for the wrapped iterable.
        tri = 500 * 499 // 2
        assert totals.premium == tri
        assert totals.model_calls == tri * 2
        assert totals.user_messages == tri * 3
        assert totals.api_duration_ms == tri * 10
        assert totals.output_tokens == tri * 5
        assert totals.session_count == 500


# ---------------------------------------------------------------------------
# Issue #625 — render_full_summary single-pass partition
# ---------------------------------------------------------------------------


class TestRenderFullSummaryIterationCount:
    """Verify render_full_summary iterates the input list at most twice.

    Uses a custom list subclass that counts forward ``__iter__`` and
    ``__reversed__`` traversals to assert that the session list is not
    traversed redundantly.
    """

    @staticmethod
    def _make_sessions(n: int) -> list[SessionSummary]:
        """Build *n* sessions — mix of active and completed."""
        sessions: list[SessionSummary] = []
        for i in range(n):
            is_active = i % 5 == 0  # 20% active
            sessions.append(
                SessionSummary(
                    session_id=f"iter-{i:04d}-abcdef",
                    name=f"S{i}",
                    model="claude-sonnet-4",
                    start_time=datetime(2025, 3, 1, tzinfo=UTC) - timedelta(hours=i),
                    is_active=is_active,
                    total_premium_requests=i,
                    user_messages=i,
                    model_calls=i,
                    model_metrics={
                        "claude-sonnet-4": ModelMetrics(
                            requests=RequestMetrics(count=i, cost=i),
                            usage=TokenUsage(outputTokens=i * 100),
                        )
                    }
                    if i > 0
                    else {},
                )
            )
        return sessions

    class _CountingList(list[SessionSummary]):
        """A ``list`` subclass that counts forward and reverse traversals."""

        def __init__(self, items: list[SessionSummary]) -> None:
            super().__init__(items)
            self.iter_count: int = 0
            self.reversed_count: int = 0

        def __iter__(self):  # type: ignore[override]
            self.iter_count += 1
            return super().__iter__()

        def __reversed__(self):  # type: ignore[override]
            self.reversed_count += 1
            return super().__reversed__()

    def test_input_list_iterated_at_most_twice(self) -> None:
        """render_full_summary must iterate the sessions list ≤ 2 times."""
        raw = self._make_sessions(500)
        counted = self._CountingList(raw)

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        render_full_summary(counted, target_console=console)

        # Forward __iter__ passes: one for the partition loop in
        # render_full_summary + one in _render_summary_header's
        # forward scan for the latest start_time.
        assert counted.iter_count <= 2

        # _render_summary_header also does a reversed() scan to find the
        # earliest start_time — track that separately.
        assert counted.reversed_count <= 1

        # Sanity-check that both sections actually rendered.
        output = buf.getvalue()
        assert "Historical Totals" in output
        assert "Active Sessions" in output


# ---------------------------------------------------------------------------
# Issue #686 — cost view: empty model_metrics + is_active + has_shutdown_metrics
# ---------------------------------------------------------------------------


class TestCostViewEmptyMetricsWithActivePeriod:
    """model_metrics={} + is_active + has_shutdown_metrics → fallback row + active row."""

    def test_cost_view_empty_metrics_with_active_period_shows_both_rows(self) -> None:
        """Fallback summary row and '↳ Since last shutdown' row both render."""
        session = SessionSummary(
            session_id="empty-resumed",
            model="claude-sonnet-4",
            model_metrics={},
            is_active=True,
            has_shutdown_metrics=True,
            active_model_calls=3,
            active_output_tokens=150,
            model_calls=3,
            user_messages=2,
        )
        output = _capture_cost_view([session])
        assert session_display_name(session) in output
        assert "↳ Since last shutdown" in output


# ---------------------------------------------------------------------------
# Issue #775 — render_cost_view "Since last shutdown" row must not appear
# when has_active_period_stats is False
# ---------------------------------------------------------------------------


class TestCostViewActiveNoActivePeriodStats:
    """Fix #775: when is_active=True, has_shutdown_metrics=True but
    has_active_period_stats=False, the '↳ Since last shutdown' row must
    not appear (it previously fell back to session totals)."""

    def test_since_last_shutdown_row_suppressed_when_no_active_stats(self) -> None:
        """When is_active=True and has_shutdown_metrics=True but
        has_active_period_stats=False, the '↳ Since last shutdown' row
        must not appear — showing session totals is misleading."""
        session = SessionSummary(
            session_id="resume-no-activity",
            model="claude-sonnet-4",
            start_time=datetime(2025, 3, 1, 12, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=5,
            user_messages=3,
            active_model_calls=0,
            active_user_messages=0,
            active_output_tokens=0,
            last_resume_time=None,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=5),
                    usage=TokenUsage(outputTokens=1000),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        # The ↳ row must NOT appear
        assert "Since last shutdown" not in clean
        # Per-model row should still render with full session totals
        lines = clean.splitlines()
        expected_session_label = session_display_name(session)
        model_row = next(
            (
                ln
                for ln in lines
                if "claude-sonnet-4" in ln and expected_session_label in ln
            ),
            None,
        )
        assert model_row is not None, "Expected a per-model row"
        cols = [c.strip() for c in model_row.split("│")]
        assert cols[5] == "5", f"Model Calls should be 5, got '{cols[5]}'"

    def test_grand_total_not_double_counted_when_no_active_stats(self) -> None:
        """Grand total must not double-count when has_active_period_stats=False.

        Companion to the suppression test: for this fixture, the grand-total
        row should show the session's expected totals without adding any
        fallback active-period values a second time.
        """
        session = SessionSummary(
            session_id="resume-no-activity-grand",
            model="claude-sonnet-4",
            start_time=datetime(2025, 3, 1, 12, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            model_calls=5,
            user_messages=3,
            active_model_calls=0,
            active_user_messages=0,
            active_output_tokens=0,
            last_resume_time=None,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=5),
                    usage=TokenUsage(outputTokens=1000),
                ),
            },
        )
        output = _capture_cost_view([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        # model_calls = 5 (no deduction since has_active_period_stats is False)
        assert grand_cols[5] == "5", (
            f"Grand Total model calls should be 5, got '{grand_cols[5]}'"
        )
        # output tokens = 1.0K (shutdown baseline only, no active tokens to add)
        assert grand_cols[6] == "1.0K", (
            f"Grand Total output tokens should be 1.0K, got '{grand_cols[6]}'"
        )


# ---------------------------------------------------------------------------
# Issue #703 — historical session table shows total model_calls/user_messages
# ---------------------------------------------------------------------------


class TestHistoricalSessionTableShutdownOnlyCounts:
    """Regression: historical section must show shutdown-only model_calls / user_messages."""

    def test_historical_rows_use_shutdown_only_counts(self) -> None:
        """Historical section subtracts active counts; active section shows active counts."""
        now = datetime.now(tz=UTC)
        session = SessionSummary(
            session_id="resumed-counts-703",
            name="Resumed Counts",
            model="claude-sonnet-4",
            start_time=now - timedelta(hours=2),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=now - timedelta(minutes=5),
            model_calls=500,
            active_model_calls=200,
            user_messages=100,
            active_user_messages=40,
            total_premium_requests=10,
            model_metrics={
                "claude-sonnet-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=1_000_000),
                ),
            },
        )
        output = _capture_full_summary([session])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        # --- Historical Totals panel (must use shutdown-only counts) ---
        assert "Historical Totals" in clean
        totals_start = clean.index("Historical Totals")
        sessions_table_start = clean.index("Sessions (Shutdown Data)")
        totals_panel = clean[totals_start:sessions_table_start]
        assert "300 model calls" in totals_panel, (
            f"Historical Totals panel should show '300 model calls', got: {totals_panel}"
        )
        assert "60 user messages" in totals_panel, (
            f"Historical Totals panel should show '60 user messages', got: {totals_panel}"
        )

        # --- Historical section ---
        assert "Sessions (Shutdown Data)" in clean
        hist_start = sessions_table_start
        # Active Sessions heading comes after the historical table.
        active_start = clean.index("Active Sessions")
        hist_section = clean[hist_start:active_start]

        hist_row = next(
            line for line in hist_section.splitlines() if "Resumed Counts" in line
        )
        hist_cols = [c.strip() for c in hist_row.split("│")]
        # Columns: (border) | Name | Model | Premium | Model Calls | User Msgs | Output Tokens | Status | (border)
        assert hist_cols[4] == "300", (
            f"Historical Model Calls: expected '300', got '{hist_cols[4]}'"
        )
        assert hist_cols[5] == "60", (
            f"Historical User Msgs: expected '60', got '{hist_cols[5]}'"
        )

        # --- Active section ---
        active_section = clean[active_start:]
        active_row = next(
            line for line in active_section.splitlines() if "Resumed Counts" in line
        )
        active_cols = [c.strip() for c in active_row.split("│")]
        # Active Sessions columns: Name | Model | Model Calls | User Msgs | Output Tokens | Running Time
        assert active_cols[3] == "200", (
            f"Active Model Calls: expected '200', got '{active_cols[3]}'"
        )
        assert active_cols[4] == "40", (
            f"Active User Msgs: expected '40', got '{active_cols[4]}'"
        )


# ---------------------------------------------------------------------------
# Issue #948 — render_cost_view must not call total_output_tokens redundantly
# ---------------------------------------------------------------------------


class TestRenderCostViewNoRedundantTotalOutputTokens:
    """Issue #948 — eliminate redundant total_output_tokens calls."""

    def test_grand_total_matches_expected_for_mixed_sessions(self) -> None:
        """Grand-total output tokens equals sum(total_output_tokens(s))
        for a mix of sessions with/without model_metrics, including
        resumed sessions where has_active_period_stats is True."""
        # Session with model_metrics (Case 2 in the issue)
        with_metrics = SessionSummary(
            session_id="mix-with-metrics-948",
            name="With Metrics",
            model="gpt-4",
            start_time=datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
            is_active=False,
            model_calls=8,
            user_messages=4,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=1000),
                ),
                "gpt-4o": ModelMetrics(
                    requests=RequestMetrics(count=3, cost=6),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )

        # Session without model_metrics (Case 1 in the issue)
        without_metrics = SessionSummary(
            session_id="mix-no-metrics-948",
            name="No Metrics",
            model=None,
            start_time=datetime(2025, 6, 2, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=3,
            active_model_calls=3,
            active_output_tokens=700,
            model_metrics={},
        )

        # Resumed session with model_metrics and active period stats
        resumed = SessionSummary(
            session_id="mix-resumed-948",
            name="Resumed",
            model="gpt-4",
            start_time=datetime(2025, 6, 3, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime(2025, 6, 3, 12, 0, tzinfo=UTC),
            model_calls=12,
            user_messages=6,
            active_output_tokens=300,
            active_model_calls=4,
            active_user_messages=2,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=8, cost=16),
                    usage=TokenUsage(outputTokens=800),
                ),
            },
        )

        sessions = [resumed, without_metrics, with_metrics]
        expected_total = sum(total_output_tokens(s) for s in sessions)

        output = _capture_cost_view(sessions)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        actual = grand_cols[6]
        assert actual == format_tokens(expected_total), (
            f"Grand Total output tokens: expected {format_tokens(expected_total)}, "
            f"got '{actual}'"
        )

    def test_total_output_tokens_not_called_for_model_metrics_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """total_output_tokens must not be called for sessions where the
        per-model loop already accumulates the total (model_metrics present)."""
        # Session with model_metrics — total_output_tokens should NOT be called
        with_metrics = SessionSummary(
            session_id="spy-with-metrics-948",
            name="Spy With Metrics",
            model="gpt-4",
            start_time=datetime(2025, 7, 1, 10, 0, tzinfo=UTC),
            is_active=False,
            model_calls=5,
            user_messages=2,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=5, cost=10),
                    usage=TokenUsage(outputTokens=800),
                ),
            },
        )

        # Session without model_metrics — called at most once
        without_metrics = SessionSummary(
            session_id="spy-no-metrics-948",
            name="Spy No Metrics",
            model=None,
            start_time=datetime(2025, 7, 2, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=2,
            active_model_calls=2,
            active_output_tokens=300,
            model_metrics={},
        )

        call_log: list[str] = []
        original_fn = total_output_tokens

        def spy(session: SessionSummary) -> int:
            call_log.append(session.session_id)
            return original_fn(session)

        monkeypatch.setattr("copilot_usage.report.total_output_tokens", spy)

        _capture_cost_view([without_metrics, with_metrics])

        # total_output_tokens must NOT be called for the model_metrics session
        assert "spy-with-metrics-948" not in call_log, (
            "total_output_tokens should not be called for sessions with model_metrics"
        )

        # total_output_tokens called at most once for the no-metrics session
        no_metrics_calls = call_log.count("spy-no-metrics-948")
        assert no_metrics_calls == 1, (
            f"total_output_tokens called {no_metrics_calls} times for "
            f"no-model-metrics session; expected exactly 1"
        )

    def test_resumed_session_with_metrics_no_redundant_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """For a resumed session with model_metrics, total_output_tokens
        must not be called — the per-model loop plus active_output_tokens
        is used instead."""
        resumed = SessionSummary(
            session_id="spy-resumed-948",
            name="Spy Resumed",
            model="gpt-4",
            start_time=datetime(2025, 7, 3, 10, 0, tzinfo=UTC),
            is_active=True,
            has_shutdown_metrics=True,
            last_resume_time=datetime(2025, 7, 3, 12, 0, tzinfo=UTC),
            model_calls=10,
            user_messages=5,
            active_output_tokens=250,
            active_model_calls=3,
            active_user_messages=2,
            model_metrics={
                "gpt-4": ModelMetrics(
                    requests=RequestMetrics(count=7, cost=14),
                    usage=TokenUsage(outputTokens=500),
                ),
            },
        )

        call_log: list[str] = []
        original_fn = total_output_tokens

        def spy(session: SessionSummary) -> int:
            call_log.append(session.session_id)
            return original_fn(session)

        monkeypatch.setattr("copilot_usage.report.total_output_tokens", spy)

        output = _capture_cost_view([resumed])
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)

        # Verify correctness: 500 (shutdown) + 250 (active) = 750
        expected = format_tokens(750)
        lines = clean.splitlines()
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        assert grand_cols[6] == expected, (
            f"Grand Total output tokens: expected {expected}, got '{grand_cols[6]}'"
        )

        # Verify no redundant call
        assert "spy-resumed-948" not in call_log, (
            "total_output_tokens should not be called for resumed sessions "
            "with model_metrics"
        )
