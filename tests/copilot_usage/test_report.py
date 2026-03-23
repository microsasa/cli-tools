"""Tests for copilot_usage.report — rendering helpers."""

# pyright: reportPrivateUsage=false

import re
import warnings
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from copilot_usage.models import (
    CodeChanges,
    EventType,
    ModelMetrics,
    RequestMetrics,
    SessionEvent,
    SessionSummary,
    TokenUsage,
)
from copilot_usage.report import (
    _aggregate_model_metrics,
    _build_event_details,
    _compute_session_totals,
    _effective_stats,
    _EffectiveStats,
    _estimate_premium_cost,
    _event_type_label,
    _filter_sessions,
    _format_detail_duration,
    _format_elapsed_since,
    _format_relative_time,
    _format_session_running_time,
    _format_timedelta,
    _has_active_period_stats,
    _render_model_table,
    _render_shutdown_cycles,
    _truncate,
    format_duration,
    format_tokens,
    render_cost_view,
    render_full_summary,
    render_live_sessions,
    render_summary,
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
        output = _capture_console(render_session_detail, [], summary)
        assert "Aggregate Stats" in output
        assert "10" in output  # model_calls
        assert "7" in output  # user_messages

    def test_renders_no_shutdown_cycles_message(self) -> None:
        from copilot_usage.report import render_session_detail

        summary = _make_session(is_active=False)
        output = _capture_console(render_session_detail, [], summary)
        assert "No shutdown cycles recorded" in output

    def test_renders_shutdown_cycle_table(self) -> None:
        from copilot_usage.report import render_session_detail

        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.SESSION_SHUTDOWN,
                data={
                    "shutdownType": "normal",
                    "totalPremiumRequests": 5,
                    "totalApiDurationMs": 120_000,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 3, "cost": 5},
                            "usage": {"outputTokens": 800},
                        }
                    },
                },
                timestamp=start + timedelta(hours=1),
            ),
        ]
        output = _capture_console(render_session_detail, events, summary)
        assert "Shutdown Cycles" in output
        assert "5" in output  # premium requests

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
    assert format_duration(389_114) == "6m 29s"


def test_format_duration_seconds_only() -> None:
    assert format_duration(5_000) == "5s"


def test_format_duration_zero() -> None:
    assert format_duration(0) == "0s"


def test_format_duration_negative() -> None:
    assert format_duration(-100) == "0s"


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
        else {"claude-opus-4.6-1m": _OPUS_METRICS},
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
                "claude-opus-4.6-1m": _OPUS_METRICS,
                "claude-sonnet-4.5": _SONNET_METRICS,
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
            [old, new],
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

    def test_sorts_newest_first(self) -> None:
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
        output = _capture_summary([s1, s2])
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
        assert "0s" in output

    def test_summary_header_single_session_same_date_both_ends(self) -> None:
        """With a single session, earliest and latest are the same date."""
        s = _make_summary_session(start_time=datetime(2026, 3, 7, tzinfo=UTC))
        output = _capture_summary([s])
        assert output.count("2026-03-07") >= 2  # appears in both ends of range


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
        summary = _make_session(start_time=start, is_active=False)
        events = [
            _make_event(
                EventType.SESSION_SHUTDOWN,
                data={
                    "shutdownType": "",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                },
                timestamp=start + timedelta(seconds=60),
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

    def test_summary_header_shows_date_range(self) -> None:
        """_render_summary_header date range: earliest date → latest date."""
        s1 = _make_summary_session(
            session_id="early",
            start_time=datetime(2025, 3, 1, tzinfo=UTC),
        )
        s2 = _make_summary_session(
            session_id="late",
            start_time=datetime(2025, 11, 30, tzinfo=UTC),
        )
        output = _capture_summary([s1, s2])
        assert "2025-03-01  →  2025-11-30" in output

    def test_summary_header_date_range_order_is_min_max(self) -> None:
        """Date range shows min date first even when sessions are not in order."""
        sessions = [
            _make_summary_session(
                session_id="mid",
                start_time=datetime(2025, 6, 15, tzinfo=UTC),
            ),
            _make_summary_session(
                session_id="early",
                start_time=datetime(2025, 1, 1, tzinfo=UTC),
            ),
            _make_summary_session(
                session_id="late",
                start_time=datetime(2025, 12, 31, tzinfo=UTC),
            ),
        ]
        output = _capture_summary(sessions)
        assert "2025-01-01  →  2025-12-31" in output

    def test_summary_header_no_start_times(self) -> None:
        """Sessions with no start_time → 'no sessions' subtitle (line 533)."""
        session = SessionSummary(session_id="no-time", start_time=None)
        output = _capture_summary([session])
        assert "no sessions" in output

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
            active_model_calls=1,
            active_user_messages=1,
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
        assert "claude-sonnet-4" in output
        assert "claude-haiku-4.5" in output
        assert "Grand Total" in output

    def test_resumed_session_no_double_count(self) -> None:
        """Regression: active_model_calls must not be added to grand_model_calls."""
        import re

        session = SessionSummary(
            session_id="resume-5555-abcde",
            name="Resumed",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
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

    def test_pure_active_session_no_metrics_shows_both_rows(self) -> None:
        """Active session with no model_metrics shows placeholder row AND Since-last-shutdown row."""
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
        assert "Since last shutdown" in output  # active row
        # Premium Cost shows estimated cost (~2 = 2 calls × 1.0 multiplier)
        assert "~2" in output

    def test_pure_active_no_metrics_grand_total_includes_active_tokens(self) -> None:
        """Grand total output tokens includes active_output_tokens for no-metrics active session."""
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
        # 1500 output tokens → formatted as "1.5K"
        assert "1.5K" in output

    def test_mixed_sessions_grand_total(self) -> None:
        """Grand total sums metrics-output from completed + active_output from active-no-metrics."""
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
        # 2000 + 500 = 2500 → "2.5K"
        assert "2.5K" in output

    def test_active_session_estimated_cost_known_model(self) -> None:
        """Active session shows numeric estimated cost, not 'N/A', when model is known."""
        session = SessionSummary(
            session_id="est-cost-known-mod",
            name="Known Model",
            model="claude-opus-4.5",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
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

    def test_pure_active_never_shutdown_cost_falls_back(self) -> None:
        """Cost view: pure-active session with active_*=0 uses totals for the active row.

        Regression test for issue #132.
        """
        session = SessionSummary(
            session_id="cost-never-shut",
            name="Cost No Shutdown",
            model="claude-sonnet-4",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
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
        assert "Since last shutdown" in output
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        lines = clean.splitlines()
        shutdown_row = next(line for line in lines if "Since last shutdown" in line)
        cols = [c.strip() for c in shutdown_row.split("│")]
        # Should show model_calls (10) and model_metrics tokens (50.0K), not 0
        assert "10" in cols[5], (
            f"Model Calls in active row should be 10, got '{cols[5]}'"
        )
        assert "50.0K" in cols[6], (
            f"Output Tokens in active row should be 50.0K, got '{cols[6]}'"
        )
        # Grand Total output tokens must NOT double-count: should be 50.0K, not 100.0K
        grand_row = next(line for line in lines if "Grand Total" in line)
        grand_cols = [c.strip() for c in grand_row.split("│")]
        assert "50.0K" in grand_cols[6], (
            f"Grand Total output tokens should be 50.0K, got '{grand_cols[6]}'"
        )
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


class TestRenderFullSummaryHelperReuse:
    """Verify _render_historical_section delegates to shared table helpers."""

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
        output = _capture_cost_view([early, late], since=since, until=until)
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


class TestRenderShutdownCyclesMalformed:
    """Test _render_shutdown_cycles skips events with malformed data."""

    def test_malformed_shutdown_event_skipped(self) -> None:
        events = [
            _make_event(
                EventType.SESSION_SHUTDOWN,
                data={"modelMetrics": "invalid"},
                timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ]
        output = _capture_console(_render_shutdown_cycles, events)
        assert "No shutdown cycles recorded" in output


# ---------------------------------------------------------------------------
# Issue #18 — _event_type_label tests covering all match arms
# ---------------------------------------------------------------------------


class TestEventTypeLabel:
    """Tests for _event_type_label covering all match arms."""

    @pytest.mark.parametrize(
        "event_type,expected_text",
        [
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
# Issue #161 — _filter_sessions reversed date range warning
# ---------------------------------------------------------------------------


class TestFilterSessionsReversedDateRange:
    def test_reversed_since_until_warns(self) -> None:
        """Passing since > until emits a UserWarning."""
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
        assert len(caught) == 1
        assert "--since" in str(caught[0].message)
        assert "after" in str(caught[0].message)

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
# _has_active_period_stats
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


class TestBuildEventDetailsCatchAll:
    """Issue #230 — _build_event_details catch-all branch for event types without explicit details."""

    @pytest.mark.parametrize(
        "event_type",
        [
            EventType.SESSION_START,
            EventType.SESSION_RESUME,
            EventType.ABORT,
        ],
    )
    def test_catch_all_returns_empty_string(self, event_type: str) -> None:
        ev = _make_event(event_type, data={"sessionId": "s1"})
        assert _build_event_details(ev) == ""


class TestHasActivePeriodStats:
    """Tests for the _has_active_period_stats helper."""

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
        assert _has_active_period_stats(session) is True

    def test_returns_true_with_active_user_messages(self) -> None:
        """Session with positive active_user_messages returns True."""
        session = SessionSummary(
            session_id="active-msgs-1234",
            is_active=True,
            active_user_messages=5,
            active_output_tokens=0,
            active_model_calls=0,
        )
        assert _has_active_period_stats(session) is True

    def test_returns_true_with_active_output_tokens(self) -> None:
        """Session with positive active_output_tokens returns True."""
        session = SessionSummary(
            session_id="active-tokens-1234",
            is_active=True,
            active_user_messages=0,
            active_output_tokens=1000,
            active_model_calls=0,
        )
        assert _has_active_period_stats(session) is True

    def test_returns_true_with_active_model_calls(self) -> None:
        """Session with positive active_model_calls returns True."""
        session = SessionSummary(
            session_id="active-calls-1234",
            is_active=True,
            active_user_messages=0,
            active_output_tokens=0,
            active_model_calls=3,
        )
        assert _has_active_period_stats(session) is True

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
        assert _has_active_period_stats(session) is False


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
        # Falls back to _estimated_output_tokens which sums model_metrics
        assert stats.output_tokens == 4200

    def test_frozen_dataclass(self) -> None:
        """_EffectiveStats instances are immutable."""
        session = SessionSummary(
            session_id="frozen-test-1234",
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
        """When start == now, format is '0s'."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        with patch("copilot_usage.report.datetime", wraps=datetime) as mock_dt:
            mock_dt.now.return_value = now
            result = _format_elapsed_since(now)
        assert result == "0s"


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
# Issue #243 — Unit tests for _format_timedelta core helper
# ---------------------------------------------------------------------------


class TestFormatTimedelta:
    def test_zero(self) -> None:
        assert _format_timedelta(timedelta(0)) == "0s"

    def test_seconds_only(self) -> None:
        assert _format_timedelta(timedelta(seconds=5)) == "5s"

    def test_minutes_and_seconds(self) -> None:
        assert _format_timedelta(timedelta(minutes=6, seconds=29)) == "6m 29s"

    def test_exact_minute(self) -> None:
        assert _format_timedelta(timedelta(minutes=1)) == "1m"

    def test_exact_hour(self) -> None:
        assert _format_timedelta(timedelta(hours=1)) == "1h"

    def test_hours_minutes_seconds(self) -> None:
        assert _format_timedelta(timedelta(hours=1, minutes=1, seconds=1)) == "1h 1m 1s"

    def test_hours_and_minutes_no_seconds(self) -> None:
        assert _format_timedelta(timedelta(hours=2, minutes=30)) == "2h 30m"

    def test_negative_clamped_to_zero(self) -> None:
        assert _format_timedelta(timedelta(seconds=-10)) == "0s"

    def test_large_duration(self) -> None:
        assert (
            _format_timedelta(timedelta(hours=100, minutes=5, seconds=3))
            == "100h 5m 3s"
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
