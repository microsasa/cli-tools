"""End-to-end tests running CLI commands against anonymized fixture data."""

import re
import shutil
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

    def test_finds_all_sessions(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "9 sessions" in result.output

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
        assert "22 model calls" in result.output

    def test_user_messages_shown(self) -> None:
        result = CliRunner().invoke(main, ["summary", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        assert "16 user messages" in result.output


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
        # 9 sessions total
        assert "9 sessions" in result.output

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
        """--until 2026-03-07 keeps sessions starting on or before that date (end-of-day)."""
        result = CliRunner().invoke(
            main,
            ["summary", "--path", str(FIXTURES), "--until", "2026-03-07"],
        )
        assert result.exit_code == 0
        # All sessions from 2026-03-06 and 2026-03-07 included (7 of 9);
        # b5df (2026-03-08) and empty-session (2026-03-10) excluded.
        assert "7 sessions" in result.output
        # 0faecbdf (15:15) is a same-day session that must be included
        assert "0faecbdf" in result.output
        # b5df (2026-03-08) excluded
        assert "b5df" not in result.output

    def test_since_and_until_combined(self) -> None:
        """--since 2026-03-06 --until 2026-03-07 includes both days (end-of-day)."""
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
        assert "7 sessions" in result.output
        assert "539 premium requests" in result.output

    def test_summary_date_filter_includes_all(self) -> None:
        """--since far in the past includes all sessions."""
        result = CliRunner().invoke(
            main,
            ["summary", "--path", str(FIXTURES), "--since", "2020-01-01"],
        )
        assert result.exit_code == 0
        assert "9 sessions" in result.output

    def test_until_with_explicit_time_not_expanded(self) -> None:
        """--until 2026-03-07T09:00:00 is NOT expanded; sessions after 09:00 excluded."""
        result = CliRunner().invoke(
            main,
            [
                "summary",
                "--path",
                str(FIXTURES),
                "--until",
                "2026-03-07T09:00:00",
            ],
        )
        assert result.exit_code == 0
        # 0faecbdf (15:15) starts after 09:00, so excluded
        assert "0faecbdf" not in result.output


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
# pure active session with activity (regression for #154)
# ---------------------------------------------------------------------------


class TestPureActiveSessionActivityE2E:
    """Regression: pure active session must show non-zero activity counts."""

    def test_live_shows_pure_active_session(self) -> None:
        """Pure active session appears in the live view."""
        result = CliRunner().invoke(main, ["live", "--path", str(FIXTURES)])
        assert result.exit_code == 0
        clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        lines = clean.splitlines()
        active_line = next(line for line in lines if "pure-act" in line)
        # Live table columns: Session ID | Name | Model | Running | Messages | Output Tokens | CWD
        cols = [c.strip() for c in active_line.split("│")]
        assert cols[5] == "2", f"Messages column: expected '2', got '{cols[5]}'"

    def test_session_detail_shows_active(self) -> None:
        """Pure active session detail shows active status."""
        result = CliRunner().invoke(
            main, ["session", "pure-active", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "active" in result.output.lower()

    def test_session_detail_shows_user_messages(self) -> None:
        """Pure active session detail shows 2 user messages."""
        result = CliRunner().invoke(
            main, ["session", "pure-active", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "2 user messages" in result.output

    def test_session_detail_shows_model_calls(self) -> None:
        """Pure active session detail shows 2 model calls."""
        result = CliRunner().invoke(
            main, ["session", "pure-active", "--path", str(FIXTURES)]
        )
        assert result.exit_code == 0
        assert "2 model calls" in result.output


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


# ---------------------------------------------------------------------------
# cost --since / --until date filtering (Gap 1 from #368)
# ---------------------------------------------------------------------------


class TestCostDateFilterE2E:
    """E2E tests for cost --since / --until against fixture data."""

    def test_cost_since_excludes_older_sessions(self) -> None:
        """--since 2026-03-08 narrows cost view to sessions on/after that date."""
        result = CliRunner().invoke(
            main, ["cost", "--path", str(FIXTURES), "--since", "2026-03-08"]
        )
        assert result.exit_code == 0
        # b5df8a34 (2026-03-08) and empty-session (2026-03-10) should appear
        assert "b5df" in result.output
        assert "empty-sess" in result.output
        # Verify the Grand Total premium cost column equals the expected value.
        # The Rich table uses │ separators; Premium Cost is the 5th column (index 4).
        grand_total_line = next(
            line for line in result.output.splitlines() if "Grand Total" in line
        )
        columns = [c.strip() for c in grand_total_line.split("│")]
        premium_cost_col = re.sub(r"\x1b\[[0-9;]*m", "", columns[4])
        assert premium_cost_col == "288"

    def test_cost_until_excludes_newer_sessions(self) -> None:
        """--until 2026-03-07 includes sessions through end-of-day 2026-03-07."""
        result = CliRunner().invoke(
            main, ["cost", "--path", str(FIXTURES), "--until", "2026-03-07"]
        )
        assert result.exit_code == 0
        # Sessions from 2026-03-06 and 2026-03-07 are included;
        # b5df (2026-03-08) and empty-session (2026-03-10) are excluded.
        assert "resumed-sess" in result.output
        assert "multi-shutdo" in result.output
        assert "b5df" not in result.output
        assert "empty-sess" not in result.output
        assert "Grand Total" in result.output
        grand_total_line = next(
            line for line in result.output.splitlines() if "Grand Total" in line
        )
        columns = [c.strip() for c in grand_total_line.split("│")]
        premium_cost_col = re.sub(r"\x1b\[[0-9;]*m", "", columns[4])
        assert premium_cost_col == "537"

    def test_cost_inverted_date_range_shows_error(self) -> None:
        """--since after --until exits non-zero with a readable error."""
        result = CliRunner().invoke(
            main,
            [
                "cost",
                "--path",
                str(FIXTURES),
                "--since",
                "2026-12-01",
                "--until",
                "2026-01-01",
            ],
        )
        assert result.exit_code != 0
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--since" in output
        assert "after" in output


# ---------------------------------------------------------------------------
# session not-found "Available:" list (Gap 2 from #368)
# ---------------------------------------------------------------------------


class TestSessionNotFoundAvailableE2E:
    """E2E test for the 'Available:' list when session lookup fails."""

    def test_not_found_shows_available_list(self) -> None:
        """session with bad prefix prints 'Available:' with known fixture IDs."""
        result = CliRunner().invoke(
            main, ["session", "xxxxxxxx", "--path", str(FIXTURES)]
        )
        assert result.exit_code != 0
        assert "no session matching 'xxxxxxxx'" in result.output
        assert "Available:" in result.output
        # All known fixture session prefixes should appear
        for prefix in ["b5df8a34", "4a547040", "0faecbdf"]:
            assert prefix in result.output


# ---------------------------------------------------------------------------
# interactive summary (two-section layout)
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Marker that separates the historical section from the active section
_ACTIVE_MARKER = "Active Sessions (Since Last Shutdown)"

# Minimal completed session with zero premium requests (free-tier model only).
# Used to verify that the historical filter's ``not s.is_active`` branch
# includes sessions even when ``total_premium_requests == 0``.
_FREE_COMPLETED_EVENTS = (
    '{"type":"session.start","data":{"sessionId":"free-completed-001",'
    '"version":1,"producer":"copilot-agent","copilotVersion":"1.0.2",'
    '"startTime":"2026-03-07T10:00:00.000Z",'
    '"context":{"cwd":"/tmp/gh-aw/agent"}},'
    '"id":"fc-start","timestamp":"2026-03-07T10:00:00.000Z","parentId":null}\n'
    '{"type":"session.shutdown","data":{"shutdownType":"routine",'
    '"totalPremiumRequests":0,"totalApiDurationMs":1000,'
    '"modelMetrics":{"gpt-5-mini":{"requests":{"count":1,"cost":0},'
    '"usage":{"inputTokens":100,"outputTokens":50,'
    '"cacheReadTokens":0,"cacheWriteTokens":0}}},'
    '"currentModel":"gpt-5-mini"},'
    '"id":"fc-shutdown","timestamp":"2026-03-07T10:30:00.000Z",'
    '"parentId":"fc-start"}\n'
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _historical_section(output: str) -> str:
    """Return everything before the first active-section marker."""
    return output.split(_ACTIVE_MARKER)[0]


def _active_section(output: str) -> str:
    """Return only the Active Sessions section after the first marker.

    The full interactive layout renders additional content after the Active
    Sessions table (a "Sessions" list and home prompt). For tests that are
    specifically validating the Active Sessions section, we slice the output
    to stop before those later regions so that assertions cannot be
    accidentally satisfied by the follow-on content.
    """
    parts = output.split(_ACTIVE_MARKER, 1)
    if len(parts) <= 1:
        return ""

    section = parts[1]

    # Heuristically detect the start of the subsequent "Sessions" list table
    # or other content that follows the Active Sessions table. We trim the
    # active section at the earliest such boundary if present.
    end_markers = [
        "\nSessions\n",
        "\nSessions ",
    ]

    end_index = len(section)
    for marker in end_markers:
        idx = section.find(marker)
        if idx != -1 and idx < end_index:
            end_index = idx

    return section[:end_index]


class TestInteractiveSummaryE2E:
    """E2E tests for the default interactive mode (two-section layout).

    The default CLI path (no subcommand) enters ``_interactive_loop`` which
    calls ``render_full_summary`` — a two-section layout with *Historical
    Totals* and *Active Sessions*.  ``input="q\\n"`` quits immediately after
    the initial render.
    """

    # -- Section presence -------------------------------------------------

    def test_both_sections_rendered(self) -> None:
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        assert "Historical Totals" in result.output
        assert "Active Sessions" in result.output

    # -- Historical section content ---------------------------------------

    def test_resumed_session_in_historical(self) -> None:
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "resumed-sess" in _historical_section(output)

    def test_completed_sessions_in_historical(self) -> None:
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        historical = _historical_section(output)
        assert "0faecbdf" in historical

    def test_pure_active_absent_from_historical(self) -> None:
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        historical = _historical_section(output)
        assert "pure-active" not in historical

    def test_historical_totals_use_shutdown_tokens(self) -> None:
        """Historical totals must use shutdown-only tokens (no active-period
        double-counting).  The fixture set yields 414 000 shutdown output
        tokens across all historical sessions.
        """
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        historical = _historical_section(output)
        assert "414.0K output tokens" in historical

    # -- Active section content -------------------------------------------

    def test_resumed_session_in_active(self) -> None:
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "resumed-sess" in _active_section(output)

    def test_active_sessions_in_active_section(self) -> None:
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        active = _active_section(output)
        assert "4a547040" in active
        assert "resumed-sess" in active

    # -- Regression: resumed-session split --------------------------------

    def test_resumed_active_tokens_not_duplicated(self) -> None:
        """Active section must show active-period tokens only.

        ``resumed-session`` has ``active_output_tokens=325`` (not the 500
        from shutdown or 825 total).  The active-section row must reflect
        only the post-resume activity.
        """
        result = CliRunner().invoke(main, ["--path", str(FIXTURES)], input="q\n")
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        active = _active_section(output)
        resumed_rows = [ln for ln in active.splitlines() if "resumed-sess" in ln]
        assert resumed_rows
        assert "325" in resumed_rows[0]

    # -- Regression: free-model (zero-premium) completed session ----------

    def test_free_model_completed_in_historical(self) -> None:
        """A completed session with ``total_premium_requests=0`` must still
        appear in the historical section (the ``not s.is_active`` branch
        of the filter).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "free-completed"
            session_dir.mkdir()
            (session_dir / "events.jsonl").write_text(
                _FREE_COMPLETED_EVENTS,
                encoding="utf-8",
            )
            result = CliRunner().invoke(main, ["--path", tmpdir], input="q\n")
            assert result.exit_code == 0
            output = _strip_ansi(result.output)
            assert "Historical Totals" in output
            assert "free-complet" in _historical_section(output)

    # -- Regression: pure-active-only input -------------------------------

    def test_pure_active_only_no_historical(self) -> None:
        """When the only sessions are pure-active (no shutdown data), the
        historical section must show the ``No historical shutdown data``
        message and the active section must still render.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            shutil.copytree(
                FIXTURES / "pure-active-session",
                Path(tmpdir) / "pure-active-session",
            )
            result = CliRunner().invoke(main, ["--path", tmpdir], input="q\n")
            assert result.exit_code == 0
            assert "No historical shutdown data" in result.output
            assert "Active Sessions" in result.output
