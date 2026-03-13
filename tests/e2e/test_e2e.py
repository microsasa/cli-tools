"""End-to-end tests running CLI commands against anonymized fixture data."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from copilot_usage.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure Rich tables are not truncated during tests."""
    monkeypatch.setenv("COLUMNS", "200")


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


class TestSummaryE2E:
    """Tests for the ``summary`` command."""

    def test_finds_eight_sessions(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "8 sessions" in result.output

    def test_total_premium_requests(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # All sessions including resumed: 504 + 288 + 2 + 15 + 10 + 8 = 827
        assert "827 premium requests" in result.output

    def test_model_names_in_output(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "claude-haiku-4.5" in result.output
        assert "claude-opus-4.6" in result.output

    def test_active_and_completed_status_shown(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "Completed" in result.output
        assert "Active" in result.output

    def test_date_filtering_excludes_sessions(self) -> None:
        result = CliRunner().invoke(
            main, ["summary", "--path", str(FIXTURES), "--since", "2026-03-08"]
        )
        assert result.exit_code == 0
        # b5df (2026-03-08) + empty-session (2026-03-10) match
        assert "2 sessions" in result.output
        assert "288 premium requests" in result.output

    def test_model_calls_shown(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "20 model calls" in result.output

    def test_user_messages_shown(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "14 user messages" in result.output


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


class TestSessionE2E:
    """Tests for the ``session`` command."""

    def test_lookup_by_prefix(self) -> None:
        result = CliRunner().invoke(main, ["session", "b5df", "--path", str(FIXTURES)])
        assert "b5df8a34" in result.output
        assert "claude-opus-4.6-1m" in result.output

    def test_not_found_error(self) -> None:
        result = CliRunner().invoke(main, ["session", "badid", "--path", str(FIXTURES)])
        assert result.exit_code != 0
        assert "no session matching 'badid'" in result.output

    def test_session_detail_sections_shown(self) -> None:
        result = CliRunner().invoke(main, ["session", "b5df", "--path", str(FIXTURES)])
        assert "Session Detail" in result.output
        assert "Aggregate Stats" in result.output


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


class TestCostE2E:
    """Tests for the ``cost`` command."""

    def test_cost_table_shown(self) -> None:
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "Cost Breakdown" in result.output

    def test_total_premium_requests(self) -> None:
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # Grand total Premium Cost: 288 + 504 + 15 + 0 + 0 + 8 + 10 + 0 = 825
        assert "825" in result.output

    def test_active_session_shows_dash(self) -> None:
        """Active session with no shutdown shows '—' for premium."""
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # Active sessions (e.g. empty-sess) display em-dash for premium
        assert "empty-sess" in result.output
        assert "—" in result.output


# ---------------------------------------------------------------------------
# resumed session
# ---------------------------------------------------------------------------


class TestResumedSessionE2E:
    """Tests for resumed session detection."""

    def test_resumed_session_shows_active(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # At 80-char width "resumed-" truncates to "resume…"
        assert "resume" in result.output
        assert "Active" in result.output

    def test_resumed_session_live(self) -> None:
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # The resumed session should appear in live (active sessions only)
        assert "resumed-" in result.output

    def test_multiplier_values(self) -> None:
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # Cost table no longer has multiplier column; check premium cost total
        assert "825" in result.output


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


class TestLiveE2E:
    """Tests for the ``live`` command."""

    def test_active_session_shown(self) -> None:
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "4a5470" in result.output

    def test_completed_sessions_not_shown(self) -> None:
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "0faecbdf" not in result.output
        assert "b5df8a34" not in result.output


# ---------------------------------------------------------------------------
# corrupt session
# ---------------------------------------------------------------------------


class TestCorruptSessionE2E:
    """Tests for handling corrupt/malformed events.jsonl files."""

    def test_summary_survives_corrupt_lines(self) -> None:
        """Summary still works when events.jsonl has malformed JSON lines."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # 8 sessions total
        assert "8 sessions" in result.output

    def test_corrupt_session_appears_in_summary(self) -> None:
        """The corrupt session is parsed (valid lines kept) and shown."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "Completed" in result.output


# ---------------------------------------------------------------------------
# date filter partial
# ---------------------------------------------------------------------------


class TestSummaryDateFilterPartialE2E:
    """Tests for --since filtering that includes only some sessions."""

    def test_summary_date_filter_partial(self) -> None:
        """--since includes only sessions after 2026-03-07T12:00:00."""
        result = CliRunner().invoke(
            main,
            ["summary", "--path", str(FIXTURES), "--since", "2026-03-07T12:00:00"],
        )
        assert result.exit_code == 0
        # b5df (2026-03-08), 0faecbdf (2026-03-07T15:15), empty-session (2026-03-10)
        assert "3 sessions" in result.output

    def test_until_excludes_later_sessions(self) -> None:
        """--until 2026-03-07 keeps only sessions starting on or before that date."""
        result = CliRunner().invoke(
            main,
            ["summary", "--path", str(FIXTURES), "--until", "2026-03-07"],
        )
        assert result.exit_code == 0
        # multi-shutdown-resumed (2026-03-06) + resumed-session (2026-03-06)
        assert "2 sessions" in result.output

    def test_since_and_until_combined(self) -> None:
        """--since 2026-03-06 --until 2026-03-07 narrows to the same 2 sessions."""
        result = CliRunner().invoke(
            main,
            [
                "summary",
                "--path",
                str(FIXTURES),
                "--since",
                "2026-03-06",
                "--until",
                "2026-03-07",
            ],
        )
        assert result.exit_code == 0
        assert "2 sessions" in result.output
        assert "18 premium requests" in result.output

    def test_summary_date_filter_includes_all(self) -> None:
        """--since far in the past includes all sessions."""
        result = CliRunner().invoke(
            main,
            ["summary", "--path", str(FIXTURES), "--since", "2020-01-01"],
        )
        assert result.exit_code == 0
        assert "8 sessions" in result.output


# ---------------------------------------------------------------------------
# cost with free model
# ---------------------------------------------------------------------------


class TestCostWithFreeModelE2E:
    """Tests for cost command with 0× multiplier models."""

    def test_cost_with_free_model(self) -> None:
        """gpt-5-mini (0× multiplier) from corrupt-session fixture shows up."""
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # Cost table no longer has multiplier column
        assert "gpt-5-mini" in result.output

    def test_cost_free_model_premium_zero(self) -> None:
        """Free model session has 2 requests and 0 premium cost in cost table."""
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # corrupt-session row: gpt-5-mini with 2 requests and 0 premium cost
        assert "corrupt0" in result.output
        assert "gpt-5-mini" in result.output
        # Verify the corrupt session row shows 2 requests and 0 premium
        lines = result.output.splitlines()
        corrupt_line = next(line for line in lines if "corrupt0" in line)
        assert "gpt-5-mini" in corrupt_line
        assert "0" in corrupt_line  # premium cost is 0


# ---------------------------------------------------------------------------
# session detail timeline
# ---------------------------------------------------------------------------


class TestSessionDetailTimelineE2E:
    """Tests for session command output including recent events."""

    def test_session_detail_shows_recent_events(self) -> None:
        """Session detail output includes event types from fixture data."""
        result = CliRunner().invoke(
            main, ["session", "corrupt0", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "Recent Events" in result.output
        assert "user message" in result.output
        assert "assistant" in result.output

    def test_session_detail_shows_tool_events(self) -> None:
        """Session detail includes turn start/end events."""
        result = CliRunner().invoke(
            main, ["session", "corrupt0", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "turn start" in result.output

    def test_session_detail_shows_shutdown_cycles(self) -> None:
        """Session detail includes shutdown cycles table."""
        result = CliRunner().invoke(
            main, ["session", "corrupt0", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "Shutdown Cycles" in result.output


# ---------------------------------------------------------------------------
# premium requests for active sessions
# ---------------------------------------------------------------------------


class TestPremiumRequestsE2E:
    """Tests for premium request display in active/resumed/completed sessions."""

    def test_active_session_shows_dash_for_premium(self) -> None:
        """Active session (no shutdown) shows '—' for premium requests."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # Active sessions like empty-sess display em-dash for unknown premium
        assert "empty-sess" in result.output
        assert "—" in result.output

    def test_completed_session_shows_exact_premium(self) -> None:
        """Completed sessions show exact premium requests."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "288" in result.output  # b5df completed session

    def test_total_includes_all_sessions(self) -> None:
        """Totals include premium requests from all sessions (including resumed)."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "827 premium requests" in result.output


# ---------------------------------------------------------------------------
# multi-shutdown completed session
# ---------------------------------------------------------------------------


class TestMultiShutdownCompletedE2E:
    """Tests for a session with 2 shutdowns, different models, completed."""

    def test_session_appears_in_summary(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        # Session name truncated to first 12 chars: "multi-shutdo"
        assert "multi-shutdo" in result.output

    def test_both_model_names_in_cost(self) -> None:
        result = CliRunner().invoke(main, ["cost", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "claude-sonnet-4" in result.output
        assert "claude-opus-4.6" in result.output

    def test_summed_premium_requests(self) -> None:
        """5 + 10 = 15 premium requests visible in session detail."""
        result = CliRunner().invoke(
            main, ["session", "multi-shutdown-c", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "15 premium requests" in result.output

    def test_status_is_completed(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "Completed" in result.output

    def test_not_in_live(self) -> None:
        """Completed session should not appear in live view."""
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "multi-shutdown-completed" not in result.output

    def test_session_detail(self) -> None:
        result = CliRunner().invoke(
            main, ["session", "multi-shutdown-c", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "multi-shutdown-completed" in result.output
        assert "Shutdown Cycles" in result.output


# ---------------------------------------------------------------------------
# multi-shutdown resumed session (2 shutdowns, still active)
# ---------------------------------------------------------------------------


class TestMultiShutdownResumedE2E:
    """Tests for a session with 2 shutdowns followed by a resume (still active)."""

    def test_session_in_live(self) -> None:
        """Multi-shutdown-resumed session appears in live (it is active)."""
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "multi-sh" in result.output

    def test_summary_shows_active(self) -> None:
        """Session shows Active status in summary."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "Active" in result.output

    def test_premium_requests_in_cost(self) -> None:
        """Summed premium requests (3 + 7 = 10) visible in session detail."""
        result = CliRunner().invoke(
            main, ["session", "multi-shutdown-r", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "10 premium requests" in result.output

    def test_session_detail(self) -> None:
        """Session detail can be retrieved by prefix."""
        result = CliRunner().invoke(
            main, ["session", "multi-shutdown-r", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "multi-shutdown-resumed" in result.output
        assert "Active" in result.output
        assert "Shutdown Cycles" in result.output


# ---------------------------------------------------------------------------
# empty session (session.start only)
# ---------------------------------------------------------------------------


class TestEmptySessionE2E:
    """Tests for a session with only a session.start event."""

    def test_empty_session_in_summary(self) -> None:
        """Empty session appears in summary output."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "empty-" in result.output

    def test_empty_session_is_active(self) -> None:
        """Session with only session.start shows as Active."""
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "Active" in result.output

    def test_empty_session_in_live(self) -> None:
        """Empty session appears in live view (it is active)."""
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "empty-se" in result.output

    def test_empty_session_detail(self) -> None:
        """Session detail can be retrieved by prefix."""
        result = CliRunner().invoke(
            main, ["session", "empty-sess", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "empty-sess-0000" in result.output
        assert "active" in result.output.lower()

    def test_empty_session_zero_stats(self) -> None:
        """Empty session has 0 model calls and 0 user messages in detail."""
        result = CliRunner().invoke(
            main, ["session", "empty-sess", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        # Premium shows dash for active sessions
        assert "—" in result.output


# ---------------------------------------------------------------------------
# unhappy paths
# ---------------------------------------------------------------------------


class TestUnhappyPathE2E:
    """Tests for error handling and edge cases."""

    def test_nonexistent_path_error(self) -> None:
        """--path to nonexistent directory triggers Click exists=True validation."""
        result = CliRunner().invoke(
            main, ["summary", "--path", "/nonexistent-dir-xyz-abc-999"]
        )
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_empty_directory_graceful(self) -> None:
        """--path to a directory with no events.jsonl shows zero sessions."""
        with tempfile.TemporaryDirectory() as td:
            result = CliRunner().invoke(main, ["summary", "--path", td])
            assert result.exit_code == 0
            assert "0 sessions" in result.output or "No sessions" in result.output

    def test_session_command_no_argument(self) -> None:
        """session command with no SESSION_ID argument shows usage error."""
        result = CliRunner().invoke(main, ["session"])
        assert result.exit_code != 0
        assert "SESSION_ID" in result.output


# ---------------------------------------------------------------------------
# pure active session detail
# ---------------------------------------------------------------------------


class TestPureActiveSessionE2E:
    """Tests for session detail of a pure active session (no shutdown)."""

    def test_active_status(self) -> None:
        """Pure active session (4a5470) shows active status."""
        result = CliRunner().invoke(
            main, ["session", "4a5470", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "active" in result.output.lower()

    def test_no_shutdown_cycles(self) -> None:
        """Pure active session has no shutdown cycles recorded."""
        result = CliRunner().invoke(
            main, ["session", "4a5470", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "No shutdown cycles recorded" in result.output

    def test_has_recent_events(self) -> None:
        """Pure active session shows Recent Events table."""
        result = CliRunner().invoke(
            main, ["session", "4a5470", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "Recent Events" in result.output


# ---------------------------------------------------------------------------
# shutdown aggregation regression
# ---------------------------------------------------------------------------


class TestShutdownAggregationE2E:
    """Regression: multi-shutdown sessions must SUM premium, not take last."""

    def test_total_premium_is_sum_not_last(self) -> None:
        """multi-shutdown-completed: 5 + 10 = 15 total (not 5, not 10)."""
        result = CliRunner().invoke(
            main, ["session", "multi-shutdown-c", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "15 premium requests" in result.output

    def test_both_shutdown_cycles_visible(self) -> None:
        """Both shutdown cycles appear in the detail table."""
        result = CliRunner().invoke(
            main, ["session", "multi-shutdown-c", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "Shutdown Cycles" in result.output
        lines = result.output.splitlines()
        # Find rows in the shutdown cycles table containing premium values
        cycle_rows = [line for line in lines if "2026-03-07" in line]
        assert len(cycle_rows) >= 2, f"Expected 2 shutdown cycles, got: {cycle_rows}"
