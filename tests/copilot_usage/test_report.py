"""Tests for copilot_usage.report — rendering helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

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
    _event_type_label,
    _filter_sessions,
    _format_detail_duration,
    _format_relative_time,
    _format_session_running_time,
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
    buf: list[str] = []
    console = Console(file=None, force_terminal=False, width=120)

    with patch("copilot_usage.report.Console", return_value=console):

        def _capture_print(*args: object, **kwargs: object) -> None:
            from io import StringIO

            sio = StringIO()
            c = Console(file=sio, force_terminal=False, width=120)
            c.print(*args, **kwargs)  # type: ignore[arg-type]
            buf.append(sio.getvalue())

        console.print = _capture_print  # type: ignore[method-assign]
        render_live_sessions(sessions)

    return "\n".join(buf)


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
        assert "15,000" in output

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

    from copilot_usage import report as _mod

    original = _mod.Console  # type: ignore[attr-defined]
    _mod.Console = lambda **_kwargs: console  # type: ignore[assignment,misc,return-value,attr-defined]
    try:
        render_summary(sessions, since=since, until=until)
    finally:
        _mod.Console = original  # type: ignore[assignment,attr-defined]
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

    def test_summary_header_single_session_same_date_both_ends(self) -> None:
        """With a single session, earliest and latest are the same date."""
        s = _make_summary_session(start_time=datetime(2026, 3, 7, tzinfo=UTC))
        output = _capture_summary([s])
        assert output.count("2026-03-07") >= 2  # appears in both ends of range

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
        assert "N/A" in output

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
        assert "N/A" in output

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

    def test_resumed_session_grand_output_includes_historical_and_active(
        self,
    ) -> None:
        """Grand total output tokens = historical (from metrics) + active."""
        session = SessionSummary(
            session_id="resume-out-1234",
            name="Resumed",
            model="claude-opus-4.6",
            start_time=datetime(2025, 1, 15, 10, 0, tzinfo=UTC),
            is_active=True,
            model_calls=10,
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
        # 1000 historical + 200 active = 1200 → "1.2K"
        assert "1.2K" in output

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
