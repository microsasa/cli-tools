"""Tests for copilot_usage.cli — wired-up CLI commands."""

# pyright: reportPrivateUsage=false

import contextlib
import io
import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import click
import pytest
from click.testing import CliRunner
from loguru import logger
from rich.console import Console

from copilot_usage import __version__
from copilot_usage.cli import (
    _build_session_index,
    _DateTimeOrDate,
    _normalize_until,
    _ParsedDateArg,
    _print_version_header,
    _read_line_nonblocking,
    _show_session_by_index,
    _start_observer,
    _stop_observer,
    _validate_since_until,
    main,
)
from copilot_usage.interactive import (
    Stoppable,
    render_session_list as _render_session_list,
)
from copilot_usage.models import ensure_aware_opt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so assertions match visible text only."""
    return _ANSI_RE.sub("", text)


def _write_session(
    base: Path,
    session_id: str,
    *,
    name: str | None = None,
    model: str = "claude-sonnet-4",
    premium: int = 3,
    output_tokens: int = 1500,
    active: bool = False,
    use_full_uuid_dir: bool = False,
    start_time: str = "2025-01-15T10:00:00Z",
) -> Path:
    """Create a minimal events.jsonl file inside *base*/<dir>/."""
    session_dir = base / (session_id if use_full_uuid_dir else session_id[:8])
    session_dir.mkdir(parents=True, exist_ok=True)

    # Derive realistic timestamps from the base start_time.
    base_dt = datetime.fromisoformat(start_time)
    user_msg_time = (base_dt + timedelta(minutes=1)).isoformat()
    turn_start_time = (base_dt + timedelta(minutes=1, seconds=1)).isoformat()
    shutdown_time = (base_dt + timedelta(hours=1)).isoformat()

    events: list[dict[str, Any]] = [
        {
            "type": "session.start",
            "timestamp": start_time,
            "data": {
                "sessionId": session_id,
                "startTime": start_time,
                "context": {"cwd": "/home/user/project"},
            },
        },
        {
            "type": "user.message",
            "timestamp": user_msg_time,
            "data": {"content": "hello"},
        },
        {
            "type": "assistant.turn_start",
            "timestamp": turn_start_time,
            "data": {"turnId": "0", "interactionId": "int-1"},
        },
    ]

    if not active:
        events.append(
            {
                "type": "session.shutdown",
                "timestamp": shutdown_time,
                "currentModel": model,
                "data": {
                    "shutdownType": "normal",
                    "totalPremiumRequests": premium,
                    "totalApiDurationMs": 5000,
                    "modelMetrics": {
                        model: {
                            "requests": {"count": premium, "cost": premium},
                            "usage": {
                                "inputTokens": 500,
                                "outputTokens": output_tokens,
                                "cacheReadTokens": 100,
                                "cacheWriteTokens": 50,
                            },
                        }
                    },
                },
            }
        )

    events_path = session_dir / "events.jsonl"
    with events_path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")

    if name:
        (session_dir / "plan.md").write_text(f"# {name}\n")

    return session_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "usage tracker" in result.output.lower()


def test_cli_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "copilot-usage" in result.output
    assert __version__ in result.output


def test_summary_command(tmp_path: Path) -> None:
    _write_session(tmp_path, "aaaa1111-0000-0000-0000-000000000000", name="First")
    _write_session(tmp_path, "bbbb2222-0000-0000-0000-000000000000", name="Second")

    runner = CliRunner()
    result = runner.invoke(main, ["summary", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "First" in result.output or "Summary" in result.output


def test_summary_no_sessions(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["summary", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_summary_with_since(tmp_path: Path) -> None:
    _write_session(tmp_path, "cccc3333-0000-0000-0000-000000000000", name="Recent")
    runner = CliRunner()
    result = runner.invoke(
        main, ["summary", "--path", str(tmp_path), "--since", "2025-01-01"]
    )
    assert result.exit_code == 0


def test_session_command(tmp_path: Path) -> None:
    _write_session(tmp_path, "dddd4444-0000-0000-0000-000000000000", name="Detail")
    runner = CliRunner()
    result = runner.invoke(main, ["session", "dddd4444", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Session Detail" in result.output
    assert "dddd4444" in result.output


def test_session_not_found(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["session", "zzzzzzzz", "--path", str(tmp_path)])
    assert result.exit_code != 0


def test_cost_command(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "eeee5555-0000-0000-0000-000000000000",
        name="Cost Test",
        premium=5,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Cost" in result.output or "Total" in result.output


def test_cost_no_sessions(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_cost_with_date_filter(tmp_path: Path) -> None:
    _write_session(tmp_path, "ffff6666-0000-0000-0000-000000000000", name="Filtered")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "cost",
            "--path",
            str(tmp_path),
            "--since",
            "2025-01-01",
            "--until",
            "2025-12-31",
        ],
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Issue #315 — cost --since/--until actually filters output
# ---------------------------------------------------------------------------


class TestCostDateFilter:
    """Verify that cost --since/--until excludes sessions and changes Grand Total."""

    def test_cost_since_excludes_earlier_session(self, tmp_path: Path) -> None:
        """--since excludes an earlier session; only the later one appears."""
        _write_session(
            tmp_path,
            "aaaa1111-0000-0000-0000-111111111111",
            name="EarlySess",
            premium=5,
            output_tokens=1000,
            start_time="2025-01-10T08:00:00Z",
        )
        _write_session(
            tmp_path,
            "bbbb2222-0000-0000-0000-222222222222",
            name="LateSess",
            premium=3,
            output_tokens=500,
            start_time="2025-06-15T12:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["cost", "--path", str(tmp_path), "--since", "2025-03-01"],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "LateSess" in output
        assert "EarlySess" not in output
        # Grand Total premium = 3 (only the late session)
        grand_match = re.search(r"Grand Total\s*│[^│]*│\s*\d+\s*│\s*(\d+)\s*│", output)
        assert grand_match is not None, "Grand Total row not found"
        assert grand_match.group(1) == "3"

    def test_cost_until_excludes_later_session(self, tmp_path: Path) -> None:
        """--until excludes a later session; only the earlier one appears."""
        _write_session(
            tmp_path,
            "cccc3333-0000-0000-0000-333333333333",
            name="EarlyOnly",
            premium=7,
            output_tokens=2000,
            start_time="2025-02-01T09:00:00Z",
        )
        _write_session(
            tmp_path,
            "dddd4444-0000-0000-0000-444444444444",
            name="LaterExcl",
            premium=10,
            output_tokens=3000,
            start_time="2025-09-20T14:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["cost", "--path", str(tmp_path), "--until", "2025-06-01"],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "EarlyOnly" in output
        assert "LaterExcl" not in output

    def test_cost_since_iso_datetime_precision(self, tmp_path: Path) -> None:
        """--since with ISO datetime format filters with time-of-day precision."""
        _write_session(
            tmp_path,
            "eeee5555-0000-0000-0000-555555555555",
            name="Morning",
            premium=2,
            start_time="2026-03-07T08:00:00Z",
        )
        _write_session(
            tmp_path,
            "ffff6666-0000-0000-0000-666666666666",
            name="Afternoon",
            premium=4,
            start_time="2026-03-07T16:00:00Z",
        )
        runner = CliRunner()
        # Use ISO datetime to exclude the morning session
        result = runner.invoke(
            main,
            [
                "cost",
                "--path",
                str(tmp_path),
                "--since",
                "2026-03-07T12:00:00",
            ],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Afternoon" in output
        assert "Morning" not in output


def test_live_command(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "gggg7777-0000-0000-0000-000000000000",
        name="Active Session",
        active=True,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["live", "--path", str(tmp_path)])
    assert result.exit_code == 0


def test_live_no_active(tmp_path: Path) -> None:
    _write_session(tmp_path, "hhhh8888-0000-0000-0000-000000000000", name="Done")
    runner = CliRunner()
    result = runner.invoke(main, ["live", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No active" in result.output


def test_session_prefix_match(tmp_path: Path) -> None:
    """Test that session command matches by prefix when using custom path."""
    _write_session(tmp_path, "iiii9999-0000-0000-0000-000000000000", name="Prefix Test")

    runner = CliRunner()
    result = runner.invoke(main, ["session", "iiii9999", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "iiii9999" in result.output


def test_session_prefix_collision_returns_newest_by_start_time(
    tmp_path: Path,
) -> None:
    """When two sessions share the same prefix, the newest by start_time is returned.

    ``get_all_sessions`` sorts by ``start_time`` (newest first), so the first
    prefix match is the most recent session.
    """
    older_uuid = "ab111111-0000-0000-0000-000000000000"
    newer_uuid = "ab222222-0000-0000-0000-000000000000"
    _write_session(
        tmp_path, older_uuid, name="OlderSession", start_time="2025-01-14T10:00:00Z"
    )
    _write_session(
        tmp_path, newer_uuid, name="NewerSession", start_time="2025-01-16T10:00:00Z"
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["session", "ab", "--path", str(tmp_path)]
    )  # ambiguous prefix
    assert result.exit_code == 0
    # Newest match is returned — not an error, not a list of alternatives
    assert "ab222222" in result.output
    assert "ab111111" not in result.output


def test_session_shows_available_on_miss(tmp_path: Path) -> None:
    """Test that session command shows available IDs when no match found."""
    _write_session(tmp_path, "jjjj0000-0000-0000-0000-000000000000", name="Exists")

    runner = CliRunner()
    result = runner.invoke(main, ["session", "notfound", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "jjjj0000" in result.output


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


def test_summary_invalid_path() -> None:
    """--path with non-existent dir → click rejects before our code runs."""
    runner = CliRunner()
    result = runner.invoke(main, ["summary", "--path", "/nonexistent/xyz_fake_path"])
    assert result.exit_code != 0
    # Click itself produces the error; no Python traceback should appear
    assert "Traceback" not in (result.output or "")


def test_summary_error_handling(tmp_path: Path, monkeypatch: Any) -> None:
    """OSError in get_all_sessions produces a friendly error message."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "disk on fire"
        raise OSError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["summary", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "disk on fire" in result.output
    assert "Traceback" not in (result.output or "")


def test_session_no_sessions(tmp_path: Path, monkeypatch: Any) -> None:
    """session command with empty get_all_sessions → 'No sessions found.'."""

    def _empty_sessions(_base: Path | None = None) -> list[object]:
        return []

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _empty_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "anything"])
    assert result.exit_code != 0
    assert "No sessions found" in result.output


def test_session_skips_empty_events(tmp_path: Path) -> None:
    """session command skips sessions with no parseable events."""
    # Create a session dir with an empty events.jsonl
    empty_dir = tmp_path / "empty-sess"
    empty_dir.mkdir()
    (empty_dir / "events.jsonl").write_text("\n", encoding="utf-8")

    # Also create a valid session to generate the "Available" list
    _write_session(tmp_path, "kkkk1111-0000-0000-0000-000000000000", name="Valid")

    runner = CliRunner()
    result = runner.invoke(main, ["session", "nonexistent", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "no session matching" in result.output


def test_session_error_handling(tmp_path: Path, monkeypatch: Any) -> None:
    """OSError in get_all_sessions produces a friendly error message."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "anything"])
    assert result.exit_code != 0
    assert "permission denied" in result.output
    assert "Traceback" not in (result.output or "")


def test_session_command_skips_malformed_events(
    tmp_path: Path,
) -> None:
    """When get_all_sessions encounters a session with unparseable JSON,
    it emits a warning, skips that session from the results, and the
    command still finds a valid match."""
    _write_session(tmp_path, "target-session-aaa", name="Target")
    # Create a session dir with malformed (non-JSON) content
    failing_dir = tmp_path / "failing-"
    failing_dir.mkdir()
    events_path = failing_dir / "events.jsonl"
    events_path.write_text("invalid json that won't parse\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["session", "target", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "target" in result.output.lower()
    assert "Traceback" not in (result.output or "")


def test_session_command_get_cached_events_oserror(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When get_cached_events raises OSError for the matched session,
    the command shows a friendly error."""
    _write_session(tmp_path, "readfail-0000-0000-0000-000000000000", name="ReadFail")

    def _fail_cached_events(_path: Path) -> tuple[object, ...]:
        raise OSError("disk I/O error")

    monkeypatch.setattr("copilot_usage.cli.get_cached_events", _fail_cached_events)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "readfail", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "Error reading session" in result.output
    assert "disk I/O error" in result.output
    assert "Traceback" not in (result.output or "")


def test_session_command_logs_warning_on_malformed_events(
    tmp_path: Path,
) -> None:
    """When get_all_sessions encounters a session with malformed JSON,
    it logs a warning but the session command still works for valid sessions."""
    # Create a valid session
    _write_session(tmp_path, "valid000-0000-0000-0000-000000000000", name="Valid")

    # Create a session dir with malformed (non-JSON) content
    bad_dir = tmp_path / "bad-sess"
    bad_dir.mkdir()
    (bad_dir / "events.jsonl").write_text("not json\n", encoding="utf-8")

    sink = io.StringIO()
    handler_id: int | None = None
    _real_setup = __import__(
        "copilot_usage.logging_config", fromlist=["setup_logging"]
    ).setup_logging

    def _setup_then_add_sink() -> None:
        nonlocal handler_id
        _real_setup()
        handler_id = logger.add(sink, level="WARNING", format="{message}")

    import copilot_usage.cli as _cli_mod

    original_setup = _cli_mod.setup_logging
    _cli_mod.setup_logging = _setup_then_add_sink  # type: ignore[assignment]

    runner = CliRunner()
    try:
        result = runner.invoke(main, ["session", "valid000", "--path", str(tmp_path)])
        assert result.exit_code == 0
        warning_output = sink.getvalue()
        assert warning_output
        assert "bad-sess" in warning_output
    finally:
        _cli_mod.setup_logging = original_setup
        if handler_id is not None:
            logger.remove(handler_id)


def test_cost_no_model_metrics(tmp_path: Path) -> None:
    """Session with no model metrics → cost command doesn't crash (line 201)."""
    session_dir = tmp_path / "nomodel00"
    session_dir.mkdir(parents=True)
    events: list[dict[str, object]] = [
        {
            "type": "session.start",
            "timestamp": "2025-01-15T10:00:00Z",
            "data": {
                "sessionId": "nomodel00-0000-0000-0000-000000000000",
                "startTime": "2025-01-15T10:00:00Z",
                "context": {"cwd": "/home/user"},
            },
        },
        {
            "type": "session.shutdown",
            "timestamp": "2025-01-15T11:00:00Z",
            "data": {
                "shutdownType": "normal",
                "totalPremiumRequests": 0,
                "totalApiDurationMs": 0,
                "modelMetrics": {},
            },
        },
    ]
    events_path = session_dir / "events.jsonl"
    with events_path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")

    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Cost" in result.output or "Total" in result.output


def test_cost_zero_multiplier_model(tmp_path: Path) -> None:
    """gpt-5-mini (0× multiplier) → shows 0 for premium cost."""
    _write_session(
        tmp_path,
        "freefree-0000-0000-0000-000000000000",
        name="Free Model",
        model="gpt-5-mini",
        premium=0,
        output_tokens=500,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Free Model" in result.output
    assert "gpt-5-mini" in result.output


def test_cost_error_handling(tmp_path: Path, monkeypatch: Any) -> None:
    """OSError in get_all_sessions produces a friendly error message."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "cost explosion"
        raise OSError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "cost explosion" in result.output
    assert "Traceback" not in (result.output or "")


def test_live_error_handling(tmp_path: Path, monkeypatch: Any) -> None:
    """OSError in get_all_sessions produces a friendly error message."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "live explosion"
        raise OSError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["live", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "live explosion" in result.output
    assert "Traceback" not in (result.output or "")


# ---------------------------------------------------------------------------
# Interactive mode tests
# ---------------------------------------------------------------------------


def test_interactive_quit_immediately(tmp_path: Path) -> None:
    """Interactive loop exits cleanly on 'q' input."""
    _write_session(tmp_path, "int10000-0000-0000-0000-000000000000", name="Interactive")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="q\n")
    assert result.exit_code == 0


def test_interactive_empty_input_exits(tmp_path: Path) -> None:
    """Empty input (just Enter) exits the interactive loop."""
    _write_session(tmp_path, "int20000-0000-0000-0000-000000000000", name="EmptyExit")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="\n")
    assert result.exit_code == 0


def test_interactive_cost_view(tmp_path: Path) -> None:
    """Pressing 'c' shows cost view, then 'q' exits."""
    _write_session(tmp_path, "int30000-0000-0000-0000-000000000000", name="CostView")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="c\nq\n")
    assert result.exit_code == 0
    assert "Cost" in result.output


def test_interactive_refresh(tmp_path: Path) -> None:
    """Pressing 'r' refreshes the data, then 'q' exits."""
    _write_session(tmp_path, "int40000-0000-0000-0000-000000000000", name="Refresh")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="r\nq\n")
    assert result.exit_code == 0


def test_interactive_session_detail(tmp_path: Path) -> None:
    """Entering a session number shows session detail."""
    _write_session(tmp_path, "int50000-0000-0000-0000-000000000000", name="DetailView")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="1\nq\n")
    assert result.exit_code == 0


def test_interactive_invalid_number(tmp_path: Path) -> None:
    """Entering an out-of-range number shows error, then 'q' exits."""
    _write_session(tmp_path, "int60000-0000-0000-0000-000000000000", name="BadNum")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="99\nq\n")
    assert result.exit_code == 0
    assert "Invalid session number" in result.output


def test_interactive_unknown_command(tmp_path: Path) -> None:
    """Unknown input shows error message."""
    _write_session(tmp_path, "int70000-0000-0000-0000-000000000000", name="UnknownCmd")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="xyz\nq\n")
    assert result.exit_code == 0
    assert "Unknown command" in result.output


def test_interactive_eof_exits(tmp_path: Path) -> None:
    """EOF (no input) exits the interactive loop cleanly."""
    _write_session(tmp_path, "int80000-0000-0000-0000-000000000000", name="EOF")
    runner = CliRunner()
    # CliRunner with no input sends EOF
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0


def test_interactive_no_sessions(tmp_path: Path) -> None:
    """Interactive mode with no sessions shows 'No sessions found'."""
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="q\n")
    assert result.exit_code == 0
    assert "No sessions" in result.output


# ---------------------------------------------------------------------------
# FileNotFoundError handling in _show_session_by_index (issue #38)
# ---------------------------------------------------------------------------


def test_show_session_by_index_missing_file(tmp_path: Path) -> None:
    """_show_session_by_index prints error instead of crashing when events file is gone."""
    from copilot_usage.models import SessionSummary

    s = SessionSummary(
        session_id="dead0000-0000-0000-0000-000000000000",
        events_path=tmp_path / "nonexistent" / "events.jsonl",
    )
    console = Console(file=None, force_terminal=True)
    with console.capture() as capture:
        _show_session_by_index(console, [s], 1)
    assert "no longer available" in capture.get().lower()


# ---------------------------------------------------------------------------
# Watchdog observer tests
# ---------------------------------------------------------------------------


def test_start_observer_returns_running_observer(tmp_path: Path) -> None:
    """_start_observer returns a non-None, alive observer for an existing dir."""
    change_event = threading.Event()
    observer = _start_observer(tmp_path, change_event)
    try:
        assert observer is not None
        assert observer.is_alive()
    finally:
        _stop_observer(observer)


def test_start_observer_returns_none_on_startup_error(tmp_path: Path) -> None:
    """_start_observer returns None (does not raise) when Observer.start() fails."""
    from loguru import logger

    change_event = threading.Event()
    log_messages: list[str] = []
    handler_id = logger.add(lambda m: log_messages.append(str(m)), level="WARNING")

    try:
        with patch(
            "watchdog.observers.Observer.start",
            side_effect=RuntimeError("inotify limit exceeded"),
        ):
            result = _start_observer(tmp_path, change_event)
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any("File watcher unavailable" in m for m in log_messages)
    assert any("inotify limit exceeded" in m for m in log_messages)


def test_start_observer_returns_none_on_os_error(tmp_path: Path) -> None:
    """_start_observer handles OSError (e.g. permission denied, NFS)."""
    from loguru import logger

    change_event = threading.Event()
    log_messages: list[str] = []
    handler_id = logger.add(lambda m: log_messages.append(str(m)), level="WARNING")

    try:
        with patch(
            "watchdog.observers.Observer.start",
            side_effect=OSError("Permission denied"),
        ):
            result = _start_observer(tmp_path, change_event)
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any("File watcher unavailable" in m for m in log_messages)


def test_start_observer_cleanup_when_alive_after_failure(tmp_path: Path) -> None:
    """When Observer.start() fails but the observer is partially alive, cleanup runs."""
    from loguru import logger

    change_event = threading.Event()
    log_messages: list[str] = []
    handler_id = logger.add(lambda m: log_messages.append(str(m)), level="WARNING")

    try:
        with (
            patch(
                "watchdog.observers.Observer.start",
                side_effect=RuntimeError("partial start failure"),
            ),
            patch("watchdog.observers.Observer.is_alive", return_value=True),
            patch("watchdog.observers.Observer.stop") as mock_stop,
            patch("watchdog.observers.Observer.join") as mock_join,
        ):
            result = _start_observer(tmp_path, change_event)
    finally:
        logger.remove(handler_id)

    assert result is None
    mock_stop.assert_called_once()
    mock_join.assert_called_once_with(timeout=2)


def test_start_observer_cleanup_failure_logged_as_debug(tmp_path: Path) -> None:
    """When cleanup after a failed start also raises, a DEBUG message is logged."""
    from loguru import logger

    change_event = threading.Event()
    log_messages: list[str] = []
    handler_id = logger.add(lambda m: log_messages.append(str(m)), level="DEBUG")

    try:
        with (
            patch(
                "watchdog.observers.Observer.start",
                side_effect=RuntimeError("start failed"),
            ),
            patch("watchdog.observers.Observer.is_alive", return_value=True),
            patch(
                "watchdog.observers.Observer.stop",
                side_effect=RuntimeError("cleanup failed"),
            ),
        ):
            result = _start_observer(tmp_path, change_event)
    finally:
        logger.remove(handler_id)

    assert result is None
    assert any("Failed to clean up file watcher" in m for m in log_messages)


def test_start_observer_propagates_unexpected_exception(tmp_path: Path) -> None:
    """_start_observer does not catch non-OSError/RuntimeError exceptions."""
    change_event = threading.Event()

    with (
        patch(
            "watchdog.observers.Observer.start",
            side_effect=TypeError("unexpected"),
        ),
        pytest.raises(TypeError, match="unexpected"),
    ):
        _start_observer(tmp_path, change_event)


# ---------------------------------------------------------------------------
# _stop_observer(None) guard
# ---------------------------------------------------------------------------


def test_stop_observer_none_is_noop() -> None:
    """_stop_observer(None) returns silently without raising."""
    _stop_observer(None)  # should not raise


def test_stop_observer_calls_stop_then_join_with_timeout() -> None:
    """_stop_observer(observer) calls observer.stop() then observer.join(timeout=2)."""
    mock_obs = MagicMock(spec=Stoppable)
    _stop_observer(cast(Stoppable, mock_obs))

    mock_obs.stop.assert_called_once_with()
    mock_obs.join.assert_called_once_with(timeout=2)
    # Verify order: stop() must precede join()
    assert mock_obs.mock_calls == [call.stop(), call.join(timeout=2)]


# ---------------------------------------------------------------------------
# _FileChangeHandler tests
# ---------------------------------------------------------------------------


class TestFileChangeHandler:
    """Tests for _FileChangeHandler debounce logic."""

    def test_dispatch_sets_event_on_first_call(self) -> None:
        """First dispatch call within a cold window sets the change_event."""
        from copilot_usage.interactive import (
            FileChangeHandler as _FileChangeHandler,
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        handler.dispatch(object())
        assert event.is_set()

    def test_dispatch_suppresses_within_debounce_window(self) -> None:
        """Second dispatch call within 2 s is suppressed (debounce)."""
        import time as _time

        from copilot_usage.interactive import (
            FileChangeHandler as _FileChangeHandler,
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        handler.dispatch(object())
        assert event.is_set()

        # Clear and force _last_trigger to now so second call is within debounce
        event.clear()
        handler._last_trigger = _time.monotonic()
        handler.dispatch(object())
        assert not event.is_set()

    def test_dispatch_fires_again_after_debounce_gap(self) -> None:
        """Dispatch fires again after > 2 s gap."""
        import time as _time

        from copilot_usage.interactive import (
            FileChangeHandler as _FileChangeHandler,
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        handler.dispatch(object())
        assert event.is_set()

        event.clear()
        # Simulate passage of time by manipulating _last_trigger
        handler._last_trigger = _time.monotonic() - 3.0
        handler.dispatch(object())
        assert event.is_set()

    def test_dispatch_interface(self) -> None:
        """_FileChangeHandler provides a dispatch(event) interface compatible with watchdog handlers."""
        from copilot_usage.interactive import (
            FileChangeHandler as _FileChangeHandler,
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        # dispatch is callable and accepts an arbitrary event object
        assert callable(handler.dispatch)
        handler.dispatch(object())
        assert event.is_set()

    def test_concurrent_dispatch_sets_event_at_most_once(self) -> None:
        """Concurrent dispatch calls within the debounce window set the event at most once.

        Holds ``handler._lock`` before starting workers so both threads
        queue at the lock inside ``dispatch()``, then releases — making
        the contention deterministic rather than relying on a lucky
        interleaving.
        """
        import time as _time

        from copilot_usage.interactive import (
            FileChangeHandler as _FileChangeHandler,
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)

        # Prime the handler so _last_trigger is "now", then clear the event.
        handler.dispatch(object())
        assert event.is_set()
        event.clear()

        # Reset _last_trigger so both threads see an expired debounce window.
        handler._last_trigger = _time.monotonic() - 10.0

        set_count: list[int] = [0]
        count_lock = threading.Lock()
        original_set = event.set

        def counting_set() -> None:
            with count_lock:
                set_count[0] += 1
            original_set()

        event.set = counting_set  # type: ignore[assignment]

        # Hold handler's lock so both workers block inside dispatch().
        handler._lock.acquire()

        barrier = threading.Barrier(2, timeout=5.0)

        def worker() -> None:
            barrier.wait()
            handler.dispatch(object())

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()

        # Give threads time to pass the barrier and block at the lock.
        _time.sleep(0.1)

        # Release; the two workers now contend serially.
        handler._lock.release()

        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        assert not t1.is_alive(), "Thread 1 did not finish"
        assert not t2.is_alive(), "Thread 2 did not finish"

        assert set_count[0] == 1, (
            f"Expected change_event.set() exactly once, got {set_count[0]}"
        )


# Issue #59 — untested branches
# ---------------------------------------------------------------------------


# 1. ensure_aware_opt unit tests ----------------------------------------------


@pytest.mark.parametrize(
    ("dt_in", "expected"),
    [
        pytest.param(None, None, id="none-returns-none"),
        pytest.param(
            datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            id="aware-unchanged",
        ),
        pytest.param(
            datetime(2025, 6, 1, 12, 0, 0),
            datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            id="naive-gets-utc",
        ),
    ],
)
def test_ensure_aware_opt(dt_in: datetime | None, expected: datetime | None) -> None:
    """ensure_aware_opt handles None, aware, and naive datetimes correctly."""
    result = ensure_aware_opt(dt_in)
    assert result == expected
    if result is not None and expected is not None:
        assert result.tzinfo is not None


def test_ensure_aware_opt_preserves_non_utc_timezone() -> None:
    """An already-aware dt with a non-UTC tz is returned unchanged."""
    non_utc = timezone(offset=timedelta(hours=5))
    dt_in = datetime(2025, 1, 1, 12, 0, 0, tzinfo=non_utc)
    result = ensure_aware_opt(dt_in)
    assert result is dt_in  # exact same object


# 2. Uppercase interactive commands -------------------------------------------


def test_interactive_quit_uppercase(tmp_path: Path) -> None:
    """Uppercase 'Q' exits the interactive loop."""
    _write_session(tmp_path, "up_q0000-0000-0000-0000-000000000000", name="QuitUpper")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="Q\n")
    assert result.exit_code == 0


def test_interactive_cost_view_uppercase(tmp_path: Path) -> None:
    """Uppercase 'C' switches to cost view."""
    _write_session(tmp_path, "up_c0000-0000-0000-0000-000000000000", name="CostUpper")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="C\nq\n")
    assert result.exit_code == 0
    assert "Cost" in result.output


def test_interactive_refresh_uppercase(tmp_path: Path) -> None:
    """Uppercase 'R' refreshes data."""
    _write_session(
        tmp_path, "up_r0000-0000-0000-0000-000000000000", name="RefreshUpper"
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="R\nq\n")
    assert result.exit_code == 0


def test_interactive_session_index_zero(tmp_path: Path) -> None:
    """Session index 0 is out of range and prints 'Invalid session number: 0'."""
    _write_session(tmp_path, "idx00000-0000-0000-0000-000000000000", name="IdxZero")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="0\nq\n")
    assert result.exit_code == 0
    assert "Invalid session number: 0" in _strip_ansi(result.output)


def test_interactive_session_index_negative(tmp_path: Path) -> None:
    """Negative session index prints 'Invalid session number: -1'."""
    _write_session(tmp_path, "idx_neg0-0000-0000-0000-000000000000", name="IdxNeg")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)], input="-1\nq\n")
    assert result.exit_code == 0
    assert "Invalid session number: -1" in _strip_ansi(result.output)


# 3. _show_session_by_index with events_path=None ----------------------------


def test_show_session_by_index_none_events_path() -> None:
    """events_path=None produces 'No events path' error message."""
    from copilot_usage.models import SessionSummary

    s = SessionSummary(
        session_id="noneevts-0000-0000-0000-000000000000",
        events_path=None,
    )
    console = Console(file=None, force_terminal=True)
    with console.capture() as capture:
        _show_session_by_index(console, [s], 1)
    assert "no events path" in capture.get().lower()


# 4. Group-level --path propagation -------------------------------------------


def test_group_path_propagates_to_summary(tmp_path: Path) -> None:
    """Group-level --path is used by 'summary' when subcommand omits --path."""
    _write_session(tmp_path, "grp_sum00-0000-0000-0000-000000000000", name="GrpSum")
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path), "summary"])
    assert result.exit_code == 0
    assert "GrpSum" in result.output or "Summary" in result.output


def test_group_path_propagates_to_session(tmp_path: Path) -> None:
    """Group-level --path is used by 'session' when subcommand omits --path."""
    _write_session(tmp_path, "grp_ses00-0000-0000-0000-000000000000", name="GrpSes")

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path), "session", "grp_ses00"])
    assert result.exit_code == 0
    assert "grp_ses00" in result.output


def test_group_path_propagates_to_cost(tmp_path: Path) -> None:
    """Group-level --path is used by 'cost' when subcommand omits --path."""
    _write_session(
        tmp_path, "grp_cst00-0000-0000-0000-000000000000", name="GrpCost", premium=2
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path), "cost"])
    assert result.exit_code == 0
    assert "Cost" in result.output or "Total" in result.output


def test_group_path_propagates_to_live(tmp_path: Path) -> None:
    """Group-level --path is used by 'live' when subcommand omits --path."""
    _write_session(
        tmp_path,
        "grp_liv00-0000-0000-0000-000000000000",
        name="GrpLive",
        active=True,
    )
    runner = CliRunner()
    result = runner.invoke(
        main, ["--path", str(tmp_path), "live"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0
    assert "grp_liv00" in result.output or "GrpLive" in result.output


# 5. Auto-refresh branches in _interactive_loop -------------------------------


def test_auto_refresh_home_view(tmp_path: Path, monkeypatch: Any) -> None:
    """change_event triggers re-render while on home view."""
    _write_session(tmp_path, "ar_home0-0000-0000-0000-000000000000", name="AutoHome")

    draw_home_calls: list[int] = []

    import copilot_usage.cli as cli_mod

    orig_draw_home = cli_mod._draw_home

    def _patched_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_home_calls.append(1)
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _patched_draw_home)

    # Capture the change_event via _start_observer
    captured_event: list[threading.Event] = []
    orig_start_observer = cli_mod._start_observer

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:
        captured_event.append(change_event)
        return orig_start_observer(session_path, change_event)

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Set the change_event so next loop iteration triggers home refresh
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # _draw_home called at least twice: initial + auto-refresh
    assert len(draw_home_calls) >= 2


def test_auto_refresh_cost_view(tmp_path: Path, monkeypatch: Any) -> None:
    """change_event triggers re-render while on cost view."""
    _write_session(tmp_path, "ar_cost0-0000-0000-0000-000000000000", name="AutoCost")

    render_cost_calls: list[int] = []

    import copilot_usage.cli as cli_mod

    orig_render_cost = cli_mod.render_cost_view

    def _patched_render_cost(*args: Any, **kwargs: Any) -> None:
        render_cost_calls.append(1)
        orig_render_cost(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "render_cost_view", _patched_render_cost)

    # Capture the change_event via _start_observer
    captured_event: list[threading.Event] = []
    orig_start_observer = cli_mod._start_observer

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:
        captured_event.append(change_event)
        return orig_start_observer(session_path, change_event)

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "c"  # navigate to cost view
        if call_count == 2:
            # Set change_event so next loop iteration triggers cost refresh
            if captured_event:
                captured_event[0].set()
            return None
        if call_count == 3:
            return ""  # go back from cost view
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # render_cost_view called at least twice: initial 'c' + auto-refresh
    assert len(render_cost_calls) >= 2


def test_auto_refresh_detail_view(tmp_path: Path, monkeypatch: Any) -> None:
    """change_event triggers re-render while on detail view with detail_session_id set."""
    _write_session(tmp_path, "ar_det00-0000-0000-0000-000000000000", name="AutoDetail")

    show_detail_calls: list[int] = []

    import copilot_usage.cli as cli_mod

    orig_show = cli_mod._show_session_by_index

    def _patched_show(*args: Any, **kwargs: Any) -> None:
        show_detail_calls.append(1)
        orig_show(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _patched_show)

    # Capture the change_event via _start_observer
    captured_event: list[threading.Event] = []
    orig_start_observer = cli_mod._start_observer

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:
        captured_event.append(change_event)
        return orig_start_observer(session_path, change_event)

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "1"  # navigate to detail view
        if call_count == 2:
            # Set change_event so auto-refresh fires in detail view
            if captured_event:
                captured_event[0].set()
            return None
        if call_count == 3:
            return ""  # go back to home
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # _show_session_by_index called at least twice: initial '1' + auto-refresh
    assert len(show_detail_calls) >= 2


def test_auto_refresh_detail_session_id_none(tmp_path: Path, monkeypatch: Any) -> None:
    """Auto-refresh after returning from detail view does not re-render detail.

    When the user leaves detail view, detail_session_id resets to None and
    view changes to "home".  A subsequent auto-refresh must call _draw_home —
    NOT _show_session_by_index — verifying the ``detail_session_id is not None``
    guard in the auto-refresh branch.
    """
    _write_session(tmp_path, "ar_dxn0-0000-0000-0000-000000000000", name="AutoDxNone")

    import copilot_usage.cli as cli_mod

    show_detail_calls: list[int] = []
    orig_show = cli_mod._show_session_by_index

    def _tracking_show(*args: Any, **kwargs: Any) -> None:
        show_detail_calls.append(1)
        orig_show(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _tracking_show)

    draw_home_calls: list[int] = []
    orig_draw_home = cli_mod._draw_home

    def _tracking_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_home_calls.append(1)
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _tracking_draw_home)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        captured_event.append(change_event)
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "1"  # enter detail view (detail_session_id set)
        if call_count == 2:
            return ""  # go back → view="home", detail_session_id=None
        if call_count == 3:
            # Trigger auto-refresh while detail_session_id is None
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # _show_session_by_index called once (entering detail view), NOT during refresh
    assert len(show_detail_calls) == 1
    # _draw_home called for: initial render + going-back render + auto-refresh
    assert len(draw_home_calls) >= 3


# ---------------------------------------------------------------------------
# Issue #577 — auto-refresh drops prompt when view=detail & session_id=None
# ---------------------------------------------------------------------------


def test_interactive_invalid_number_then_file_change(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Prompt must be written on file-change when stuck in detail view with no session_id.

    Regression test for #577: entering an out-of-range session number sets
    view="detail" with detail_session_id=None.  A subsequent file-change event
    must reset to the home view and write a prompt instead of silently dropping it.
    """
    _write_session(tmp_path, "issue577-0000-0000-0000-000000000000", name="Issue577")

    import copilot_usage.cli as cli_mod

    draw_home_calls: list[int] = []
    orig_draw_home = cli_mod._draw_home

    def _tracking_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_home_calls.append(1)
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _tracking_draw_home)

    prompt_calls: list[str] = []
    orig_write_prompt = cli_mod._write_prompt

    def _tracking_prompt(prompt: str) -> None:
        prompt_calls.append(prompt)
        orig_write_prompt(prompt)

    monkeypatch.setattr(cli_mod, "_write_prompt", _tracking_prompt)

    captured_event: list[threading.Event] = []

    def _capturing_start(
        session_path: Path,
        change_event: threading.Event,  # noqa: ARG001
    ) -> None:
        captured_event.append(change_event)
        return

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "99"  # invalid session number → view=detail, session_id=None
        if call_count == 2:
            # Trigger file-change while in detail view with session_id=None
            if captured_event:
                captured_event[0].set()
            return None
        if call_count == 3:
            return None  # let auto-refresh fire
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0

    # _draw_home must be called after the file-change event resets to home
    # Calls: initial render + auto-refresh reset = at least 2
    assert len(draw_home_calls) >= 2

    # A prompt must have been written after the auto-refresh reset
    # (The last prompt before quit should be _HOME_PROMPT from the reset)
    home_prompts_after_back = [
        p
        for p in prompt_calls
        if "session #" in p  # _HOME_PROMPT contains "session #"
    ]
    assert len(home_prompts_after_back) >= 2, (
        "Expected _HOME_PROMPT written after auto-refresh reset; "
        f"got prompts: {prompt_calls}"
    )


# ---------------------------------------------------------------------------
# Issue #441 — detail view tracks session by ID, not positional index
# ---------------------------------------------------------------------------


def test_auto_refresh_detail_tracks_session_by_id(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Auto-refresh in detail view still shows the same session after a new one appears.

    Regression test for #441: when a new (newer) session is created while
    viewing detail for session 2, the new session is prepended to the list
    and the old index 2 now points to a different session.  The fix tracks
    detail_session_id instead of a positional index, so the correct session
    is always rendered.
    """
    # Create two initial sessions; session B is newer than session A.
    _write_session(
        tmp_path,
        "aaaa0000-0000-0000-0000-000000000000",
        name="SessionA",
        start_time="2025-01-15T08:00:00Z",
    )
    _write_session(
        tmp_path,
        "bbbb0000-0000-0000-0000-000000000000",
        name="SessionB",
        start_time="2025-01-15T09:00:00Z",
    )

    import copilot_usage.cli as cli_mod

    # Track which session_ids are rendered in _show_session_by_index
    rendered_session_ids: list[str] = []
    orig_show = cli_mod._show_session_by_index

    def _tracking_show(console: Console, sessions: list[Any], index: int) -> None:
        if 1 <= index <= len(sessions):
            rendered_session_ids.append(sessions[index - 1].session_id)
        orig_show(console, sessions, index)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _tracking_show)

    captured_event: list[threading.Event] = []

    def _capturing_start(
        session_path: Path,
        change_event: threading.Event,  # noqa: ARG001
    ) -> object:
        captured_event.append(change_event)

        class _StubObserver:
            def stop(self) -> None:
                return

            def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
                return

        return _StubObserver()

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1

        if read_call == 1:
            # Sessions are sorted newest-first: [B, A].
            # Enter detail for session #2 → SessionA.
            return "2"

        if read_call == 2:
            # Inject a new session C that is even newer than B.
            sess_c_dir = _write_session(
                tmp_path,
                "cccc0000-0000-0000-0000-000000000000",
                name="SessionC",
                start_time="2025-01-15T10:00:00Z",
            )
            # Ensure SessionC's events file has a clearly newer mtime.
            now_ts = datetime.now(UTC).timestamp()
            os.utime(sess_c_dir / "events.jsonl", (now_ts, now_ts))
            # Trigger auto-refresh — list becomes [C, B, A].
            # Old index 2 would now point to B (wrong), but the fix
            # should still render A by tracking session_id.
            if captured_event:
                captured_event[0].set()
            return None

        if read_call == 3:
            return ""  # go back to home
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0

    # Both renders (initial + auto-refresh) must show SessionA
    assert len(rendered_session_ids) >= 2
    assert all(
        sid == "aaaa0000-0000-0000-0000-000000000000" for sid in rendered_session_ids
    ), f"Expected SessionA every time, but rendered: {rendered_session_ids}"


def test_auto_refresh_detail_session_deleted_falls_back_to_home(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When the viewed session is removed during auto-refresh, fall back to home."""
    import shutil

    sess_dir = _write_session(
        tmp_path,
        "del10000-0000-0000-0000-000000000000",
        name="WillBeDeleted",
    )

    import copilot_usage.cli as cli_mod

    draw_home_calls: list[int] = []
    orig_draw_home = cli_mod._draw_home

    def _tracking_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_home_calls.append(1)
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _tracking_draw_home)

    captured_event: list[threading.Event] = []

    def _capturing_start(
        session_path: Path,
        change_event: threading.Event,  # noqa: ARG001
    ) -> object:
        captured_event.append(change_event)

        class _StubObserver:
            def stop(self) -> None:
                return

            def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
                return

        return _StubObserver()

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            return "1"  # enter detail view
        if read_call == 2:
            # Delete the session directory, then trigger auto-refresh
            shutil.rmtree(sess_dir)
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # After deletion, the auto-refresh should have fallen back to home view.
    # draw_home calls: initial render + fallback after session deleted = at least 2
    assert len(draw_home_calls) >= 2


# ---------------------------------------------------------------------------
# Issue #407 — auto-refresh render crash must not kill interactive loop
# ---------------------------------------------------------------------------


def test_auto_refresh_render_crash_home_does_not_kill_loop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If _draw_home raises during auto-refresh, the interactive loop survives."""
    _write_session(tmp_path, "cr_home0-0000-0000-0000-000000000000", name="CrashHome")

    import copilot_usage.cli as cli_mod

    call_count_home = [0]
    orig_draw_home = cli_mod._draw_home

    def _crashing_draw_home(console: Console, sessions: list[Any]) -> None:
        call_count_home[0] += 1
        if call_count_home[0] == 2:
            raise OSError("console write failure")
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _crashing_draw_home)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        captured_event.append(change_event)
        # Do not start the real filesystem observer here; tests only need the event.
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            # Trigger first auto-refresh → crash on 2nd _draw_home call
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 2:
            # Trigger second auto-refresh → should succeed (3rd call)
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # _draw_home called at least 3 times: initial + crash + recovery
    assert call_count_home[0] >= 3


def test_auto_refresh_render_crash_cost_does_not_kill_loop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If render_cost_view raises during auto-refresh, the loop survives."""
    _write_session(tmp_path, "cr_cost0-0000-0000-0000-000000000000", name="CrashCost")

    import copilot_usage.cli as cli_mod

    cost_call_count = [0]
    orig_render_cost = cli_mod.render_cost_view

    def _crashing_cost(*args: Any, **kwargs: Any) -> None:
        cost_call_count[0] += 1
        if cost_call_count[0] == 1:
            # First call is the user 'c' command (not auto-refresh) — let it pass
            orig_render_cost(*args, **kwargs)
        elif cost_call_count[0] == 2:
            raise RuntimeError("cost render failure")
        else:
            orig_render_cost(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "render_cost_view", _crashing_cost)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        # Capture the change_event for the test without starting a real observer.
        captured_event.append(change_event)
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            return "c"  # navigate to cost view
        if read_call == 2:
            # Auto-refresh crash on cost view
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 3:
            # Another auto-refresh that should succeed
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 4:
            return ""  # go back home
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    assert cost_call_count[0] >= 3


def test_auto_refresh_render_crash_detail_does_not_kill_loop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If _show_session_by_index raises during auto-refresh, the loop survives."""
    _write_session(tmp_path, "cr_det00-0000-0000-0000-000000000000", name="CrashDetail")

    import copilot_usage.cli as cli_mod

    detail_call_count = [0]
    orig_show = cli_mod._show_session_by_index

    def _crashing_show(*args: Any, **kwargs: Any) -> None:
        detail_call_count[0] += 1
        if detail_call_count[0] == 2:
            raise PermissionError("session file locked")
        orig_show(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _crashing_show)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        # Capture the change_event for manual triggering in the test, but avoid
        # starting a real filesystem observer thread to keep the test deterministic.
        captured_event.append(change_event)

        class _StubObserver:
            def stop(self) -> None:
                """No-op stop to match the observer interface used by _stop_observer."""
                return

            def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
                """No-op join to match the observer interface used by _stop_observer."""
                return

        return _StubObserver()

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            return "1"  # navigate to detail view
        if read_call == 2:
            # Auto-refresh crash on detail view
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 3:
            # Another auto-refresh that should succeed
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 4:
            return ""  # go back home
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    assert detail_call_count[0] >= 3


def test_auto_refresh_get_all_sessions_crash_does_not_kill_loop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If get_all_sessions raises during auto-refresh, the loop survives."""
    _write_session(tmp_path, "cr_gas00-0000-0000-0000-000000000000", name="CrashGAS")

    import copilot_usage.cli as cli_mod

    gas_call_count = [0]
    orig_get_all = cli_mod.get_all_sessions

    def _crashing_get_all(*args: Any, **kwargs: Any) -> list[Any]:
        gas_call_count[0] += 1
        if gas_call_count[0] == 2:
            raise PermissionError("session dir not readable")
        return orig_get_all(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "get_all_sessions", _crashing_get_all)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        captured_event.append(change_event)
        # Do not start a real observer; test will manually trigger change_event.
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            # Trigger auto-refresh that crashes on get_all_sessions
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 2:
            # Trigger another auto-refresh that succeeds
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # get_all_sessions called at least 3 times: initial + crash + recovery
    assert gas_call_count[0] >= 3


def test_auto_refresh_keyboard_interrupt_propagates(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """KeyboardInterrupt during auto-refresh re-raises (not swallowed by except Exception)."""
    _write_session(tmp_path, "cr_kbi00-0000-0000-0000-000000000000", name="CrashKBI")

    import copilot_usage.cli as cli_mod

    call_count = [0]

    def _interrupting_draw_home(console: Console, sessions: list[Any]) -> None:  # noqa: ARG001
        call_count[0] += 1
        if call_count[0] == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_draw_home", _interrupting_draw_home)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        captured_event.append(change_event)
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    # The outer except KeyboardInterrupt in _interactive_loop catches the
    # re-raised interrupt and exits gracefully — the key assertion is that
    # _draw_home was called exactly twice (initial + interrupted auto-refresh),
    # proving the KeyboardInterrupt was NOT swallowed by except Exception.
    assert result.exit_code == 0
    assert call_count[0] == 2


def test_auto_refresh_prompt_write_also_fails(tmp_path: Path, monkeypatch: Any) -> None:
    """When render AND best-effort prompt write both fail, the loop still survives."""
    _write_session(tmp_path, "cr_pwr00-0000-0000-0000-000000000000", name="CrashPrompt")

    import copilot_usage.cli as cli_mod

    draw_call_count = [0]
    orig_draw_home = cli_mod._draw_home

    def _crashing_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_call_count[0] += 1
        if draw_call_count[0] == 2:
            raise OSError("console write failure")
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _crashing_draw_home)

    prompt_call_count = [0]
    orig_write_prompt = cli_mod._write_prompt

    def _crashing_prompt(prompt: str) -> None:
        prompt_call_count[0] += 1
        # Fail on the first best-effort prompt write (2nd call: initial + crash-handler)
        if prompt_call_count[0] == 2:
            raise OSError("prompt write failure")
        orig_write_prompt(prompt)

    monkeypatch.setattr(cli_mod, "_write_prompt", _crashing_prompt)

    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        captured_event.append(change_event)
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 2:
            if captured_event:
                captured_event[0].set()
            return None
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # Draw was called at least 3 times: initial + crash + recovery
    assert draw_call_count[0] >= 3


# ---------------------------------------------------------------------------
# Issue #684 — session command uses _SESSION_CACHE / _EVENTS_CACHE
# ---------------------------------------------------------------------------


def test_session_uses_cache_avoids_parse_events(tmp_path: Path) -> None:
    """Pre-populating _SESSION_CACHE via get_all_sessions means
    the session CLI command does NOT call parse_events directly.

    This verifies the optimised cached path introduced in issue #684.
    """
    from copilot_usage.parser import get_all_sessions

    uuids = [
        "aaaaaaaa-1111-1111-1111-111111111111",
        "bbbbbbbb-2222-2222-2222-222222222222",
        "cccccccc-3333-3333-3333-333333333333",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    # Pre-populate caches by calling get_all_sessions
    sessions = get_all_sessions(tmp_path)
    assert len(sessions) == 3

    runner = CliRunner()
    with patch("copilot_usage.parser.parse_events") as mock_parse:
        result = runner.invoke(main, ["session", "cccccccc", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "cccccccc" in result.output
    # parse_events must NOT have been called — everything from cache
    assert mock_parse.call_count == 0


def test_session_prefix_matches_with_multiple_sessions(
    tmp_path: Path,
) -> None:
    """Prefix matching still works correctly with the cached approach."""
    uuids = [
        "ab111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "ab222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "cd333333-cccc-cccc-cccc-cccccccccccc",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "cd", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "cd333333" in result.output


def test_session_exact_uuid_wins_over_partial(tmp_path: Path) -> None:
    """An exact full-UUID match is found regardless of order."""
    uuids = [
        "ab111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "ab222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    runner = CliRunner()
    # Exact UUID targets ab111111 even if ab222222 sorts first
    result = runner.invoke(
        main,
        ["session", "ab111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "ab111111" in result.output
    assert "ab222222" not in result.output


def test_session_non_uuid_dirs_found(
    tmp_path: Path,
) -> None:
    """Non-UUID directory names are found via get_all_sessions."""
    session_dir = tmp_path / "corrupt-session"
    session_dir.mkdir()
    events: list[dict[str, Any]] = [
        {
            "type": "session.start",
            "timestamp": "2025-01-15T10:00:00Z",
            "data": {
                "sessionId": "corrupt0-0000-0000-0000-000000000000",
                "startTime": "2025-01-15T10:00:00Z",
                "context": {"cwd": "/home/user"},
            },
        },
        {
            "type": "session.shutdown",
            "timestamp": "2025-01-15T11:00:00Z",
            "currentModel": "claude-sonnet-4",
            "data": {
                "shutdownType": "normal",
                "totalPremiumRequests": 1,
                "totalApiDurationMs": 100,
                "modelMetrics": {},
            },
        },
    ]
    with (session_dir / "events.jsonl").open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")

    runner = CliRunner()
    result = runner.invoke(main, ["session", "corrupt0", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "corrupt0" in result.output


def test_session_short_prefix_matches(tmp_path: Path) -> None:
    """Short prefixes (1-3 chars) still match sessions."""
    uuids = [
        "a1111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "b3333333-cccc-cccc-cccc-cccccccccccc",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "a", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "a1111111" in result.output


def test_session_no_match_shows_available(tmp_path: Path) -> None:
    """Non-matching prefix shows error and available IDs."""
    uuids = [
        "aa111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bb222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "zzz", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "no session matching" in result.output
    assert "Available" in result.output


# ---------------------------------------------------------------------------
# Gap 2 — _read_line_nonblocking unit tests (issue #258)
# ---------------------------------------------------------------------------


class TestReadLineNonblocking:
    def test_timeout_returns_none(self) -> None:
        """When stdin has no data, _read_line_nonblocking returns None."""
        r_fd, w_fd = os.pipe()
        r_file = os.fdopen(r_fd, "r")
        try:
            with patch("copilot_usage.cli.sys.stdin", r_file):
                result = _read_line_nonblocking(timeout=0.05)
            assert result is None
        finally:
            r_file.close()
            os.close(w_fd)

    def test_returns_stripped_line(self) -> None:
        """When stdin has data, _read_line_nonblocking returns stripped line."""
        r_fd, w_fd = os.pipe()
        r_file = os.fdopen(r_fd, "r")
        try:
            os.write(w_fd, b"  hello world  \n")
            with patch("copilot_usage.cli.sys.stdin", r_file):
                result = _read_line_nonblocking(timeout=1.0)
            assert result == "hello world"
        finally:
            r_file.close()
            os.close(w_fd)


# ---------------------------------------------------------------------------
# Gap 3 — _interactive_loop stdin fallback (issue #258)
# ---------------------------------------------------------------------------


def test_interactive_loop_select_value_error_falls_back_to_input(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _read_line_nonblocking raises ValueError, the loop falls back to
    blocking input(); providing 'q' via mocked input exits cleanly."""
    _write_session(tmp_path, "fb_val00-0000-0000-0000-000000000000", name="ValErr")

    import copilot_usage.cli as cli_mod

    def _raise_value_error(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        raise ValueError("underlying buffer has been detached")

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _raise_value_error)

    def _fake_input(*_args: str, **_kwargs: str) -> str:
        return "q"

    monkeypatch.setattr("builtins.input", _fake_input)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0


def test_interactive_loop_select_os_error_falls_back_to_input(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _read_line_nonblocking raises OSError, the loop falls back to
    blocking input(); providing 'q' via mocked input exits cleanly."""
    _write_session(tmp_path, "fb_oser0-0000-0000-0000-000000000000", name="OsErr")

    import copilot_usage.cli as cli_mod

    def _raise_os_error(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        raise OSError("Bad file descriptor")

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _raise_os_error)

    def _fake_input(*_args: str, **_kwargs: str) -> str:
        return "q"

    monkeypatch.setattr("builtins.input", _fake_input)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0


def test_interactive_loop_fallback_eof_exits_cleanly(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _read_line_nonblocking raises ValueError and the fallback input()
    raises EOFError, the loop terminates without exception."""
    _write_session(tmp_path, "fb_eof00-0000-0000-0000-000000000000", name="EofFb")

    import copilot_usage.cli as cli_mod

    def _raise_value_error(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        raise ValueError("stdin not selectable")

    def _raise_eof(*_args: Any, **_kwargs: Any) -> str:
        raise EOFError

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _raise_value_error)
    monkeypatch.setattr("builtins.input", _raise_eof)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0


def test_interactive_loop_fallback_unexpected_exception_exits_cleanly(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _read_line_nonblocking raises ValueError and the fallback input()
    raises an unexpected exception (e.g. UnicodeDecodeError), the loop
    terminates without propagating the exception."""
    _write_session(tmp_path, "fb_uni00-0000-0000-0000-000000000000", name="UniErr")

    import copilot_usage.cli as cli_mod

    def _raise_value_error(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        raise ValueError("stdin not selectable")

    def _raise_unicode(*_args: Any, **_kwargs: Any) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _raise_value_error)
    monkeypatch.setattr("builtins.input", _raise_unicode)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Issue #1012 — auto-refresh must fire in OSError fallback mode
# ---------------------------------------------------------------------------


def test_auto_refresh_fires_during_os_error_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _read_line_nonblocking raises OSError (simulating Windows stdin),
    the threaded input() fallback must still allow change_event auto-refresh
    to fire *while* input() is blocked.

    Regression for issue #1012: a blocking input() in the main loop would
    previously starve the change_event handler until the user pressed Enter.
    Proof: we hold input() blocked on an Event, set change_event from a
    separate thread, assert get_all_sessions is called again, THEN unblock.
    """
    _write_session(tmp_path, "fb_arfr0-0000-0000-0000-000000000000", name="FbRefresh")

    import copilot_usage.cli as cli_mod

    get_all_calls: list[int] = []
    _orig_get_all_sessions = cli_mod.get_all_sessions

    def _tracking_get_all(path: Path | None = None) -> list[Any]:
        get_all_calls.append(1)
        return _orig_get_all_sessions(path)

    monkeypatch.setattr(cli_mod, "get_all_sessions", _tracking_get_all)

    captured_event: list[threading.Event] = []
    orig_start_observer = cli_mod._start_observer

    def _capturing_start(session_path: Path, change_event: threading.Event) -> object:
        captured_event.append(change_event)
        return orig_start_observer(session_path, change_event)

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    def _raise_os_error(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        raise OSError("select not supported on Windows stdin")

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _raise_os_error)

    input_entered = threading.Event()
    input_release = threading.Event()
    input_call_count = 0

    def _fake_input(*_args: str, **_kwargs: str) -> str:
        nonlocal input_call_count
        input_call_count += 1
        if input_call_count == 1:
            input_entered.set()
            if not input_release.wait(timeout=5.0):
                raise TimeoutError("test driver did not release input()")
            return ""
        return "q"

    monkeypatch.setattr("builtins.input", _fake_input)

    def _driver() -> None:
        if not input_entered.wait(timeout=5.0):
            return
        deadline = time.monotonic() + 5.0
        while not captured_event and time.monotonic() < deadline:
            time.sleep(0.01)
        if not captured_event:
            input_release.set()
            return
        calls_before = len(get_all_calls)
        captured_event[0].set()
        refresh_deadline = time.monotonic() + 5.0
        while (
            len(get_all_calls) <= calls_before and time.monotonic() < refresh_deadline
        ):
            time.sleep(0.01)
        input_release.set()

    driver = threading.Thread(target=_driver, daemon=True)
    driver.start()

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    driver.join(timeout=5.0)

    assert result.exit_code == 0
    assert len(get_all_calls) >= 2, (
        "auto-refresh did not run while input() was blocked "
        f"(calls={len(get_all_calls)})"
    )


# ---------------------------------------------------------------------------
# Issue #329 — observer=None when session_path doesn't exist
# ---------------------------------------------------------------------------


def test_interactive_loop_nonexistent_session_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Interactive loop starts cleanly when session_path doesn't exist (observer=None).

    When the default session-state directory is absent the loop should
    still show 'No sessions found' and exit cleanly on 'q'.
    """
    import copilot_usage.cli as cli_mod
    import copilot_usage.parser as parser_mod

    # Use a non-existent path as the default session-state directory.
    # Monkeypatch Path.home so the derived session_path doesn't exist,
    # and also patch parser.DEFAULT_SESSION_PATH so get_all_sessions(None)
    # doesn't discover sessions from the real home directory.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    missing_session_state = fake_home / ".copilot" / "session-state"
    # Intentionally do NOT create .copilot/session-state inside fake_home.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(parser_mod, "DEFAULT_SESSION_PATH", missing_session_state)
    monkeypatch.setattr(cli_mod, "DEFAULT_SESSION_PATH", missing_session_state)

    # _start_observer should never be called when session_path.exists() is False.
    start_observer_calls: list[Path] = []

    def _tracking_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        start_observer_calls.append(session_path)
        raise AssertionError(
            "_start_observer should not be called when session_path does not exist"
        )

    monkeypatch.setattr(cli_mod, "_start_observer", _tracking_start)

    # Track _stop_observer calls to verify it's called with None in finally.
    stop_observer_args: list[Stoppable | None] = []

    def _tracking_stop(observer: Stoppable | None) -> None:
        stop_observer_args.append(observer)
        _stop_observer(observer)

    monkeypatch.setattr(cli_mod, "_stop_observer", _tracking_stop)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    # Invoke without --path so _interactive_loop uses Path.home() default.
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    assert "No sessions" in output
    # _start_observer was never called (session_path.exists() == False).
    assert start_observer_calls == []
    # _stop_observer was called with None in the finally block.
    assert len(stop_observer_args) == 1
    assert stop_observer_args[0] is None


def test_interactive_loop_observer_none_no_auto_refresh(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When observer=None (session_path missing), auto-refresh is skipped
    but the loop still processes user input normally."""
    import copilot_usage.cli as cli_mod
    import copilot_usage.parser as parser_mod

    fake_home = tmp_path / "fake_home2"
    fake_home.mkdir()
    missing_session_state = fake_home / ".copilot" / "session-state"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(parser_mod, "DEFAULT_SESSION_PATH", missing_session_state)
    monkeypatch.setattr(cli_mod, "DEFAULT_SESSION_PATH", missing_session_state)

    # Track _start_observer to verify it is never called.
    start_observer_calls: list[Path] = []

    def _tracking_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        start_observer_calls.append(session_path)
        raise AssertionError(
            "_start_observer should not be called when session_path does not exist"
        )

    monkeypatch.setattr(cli_mod, "_start_observer", _tracking_start)

    draw_home_calls: list[int] = []
    orig_draw = cli_mod._draw_home

    def _tracking_draw(console: Console, sessions: list[Any]) -> None:
        draw_home_calls.append(1)
        orig_draw(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _tracking_draw)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "r"  # refresh
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    # _start_observer was never called (session_path doesn't exist).
    assert start_observer_calls == []
    # _draw_home called at least twice: initial draw + manual refresh
    assert len(draw_home_calls) >= 2


# ---------------------------------------------------------------------------
# Issue #650 — redundant get_all_sessions on back-navigation
# ---------------------------------------------------------------------------


def test_back_navigation_skips_get_all_sessions_when_no_change(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Returning from detail view should NOT call get_all_sessions when
    change_event is not set (no file changes detected)."""
    _write_session(
        tmp_path, "bk_noref-0000-0000-0000-000000000000", name="BackNoRefresh"
    )

    import copilot_usage.cli as cli_mod

    # Track get_all_sessions calls after the initial load.
    gas_calls: list[int] = []
    orig_gas = cli_mod.get_all_sessions

    def _tracking_gas(*args: Any, **kwargs: Any) -> list[Any]:
        gas_calls.append(1)
        return orig_gas(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "get_all_sessions", _tracking_gas)

    # Use a no-op observer so file-system events never fire.
    def _noop_start(session_path: Path, change_event: threading.Event) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _noop_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "1"  # navigate to detail view
        if call_count == 2:
            return ""  # back-navigation (any input returns home)
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0

    # The initial load calls get_all_sessions once; the back-navigation
    # must NOT trigger an additional call because change_event was never set.
    assert len(gas_calls) == 1


def test_back_navigation_calls_get_all_sessions_when_change_event_set(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Returning from detail view SHOULD call get_all_sessions exactly once
    when change_event is set (file changes detected)."""
    _write_session(
        tmp_path, "bk_chg00-0000-0000-0000-000000000000", name="BackWithChange"
    )

    import copilot_usage.cli as cli_mod

    # Track get_all_sessions calls after the initial load.
    gas_calls: list[int] = []
    orig_gas = cli_mod.get_all_sessions

    def _tracking_gas(*args: Any, **kwargs: Any) -> list[Any]:
        gas_calls.append(1)
        return orig_gas(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "get_all_sessions", _tracking_gas)

    # Capture change_event via a no-op observer (no real file watching).
    captured_event: list[threading.Event] = []

    def _capturing_start(session_path: Path, change_event: threading.Event) -> None:  # noqa: ARG001
        captured_event.append(change_event)
        return

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "1"  # navigate to detail view
        if call_count == 2:
            # Set change_event before back-navigation
            if captured_event:
                captured_event[0].set()
            return ""  # back-navigation triggers refresh because event is set
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0

    # Initial load (1) + back-navigation with change_event set (1) = 2 calls
    assert len(gas_calls) == 2


# ---------------------------------------------------------------------------
# Issue #307 — version header on initial entry to cost / detail views
# ---------------------------------------------------------------------------


def test_interactive_cost_view_prints_version_header(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Pressing 'c' calls _print_version_header on initial entry to cost view."""
    _write_session(tmp_path, "vh_cost0-0000-0000-0000-000000000000", name="VHCost")

    import copilot_usage.cli as cli_mod

    # Disable watchdog to avoid spurious auto-refresh triggering extra header calls
    def _null_start(session_path: Path, change_event: threading.Event) -> None:  # noqa: ARG001
        """Test stub for _start_observer that disables watchdog behavior."""
        return

    monkeypatch.setattr(cli_mod, "_start_observer", _null_start)

    header_calls: list[str] = []
    orig_header = cli_mod._print_version_header

    def _patched_header(target: Console) -> None:
        header_calls.append("called")
        orig_header(target)

    monkeypatch.setattr(cli_mod, "_print_version_header", _patched_header)

    snapshots: dict[str, int] = {}
    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            snapshots["before_entry"] = len(header_calls)
            return "c"
        if call_count == 2:
            snapshots["after_entry"] = len(header_calls)
            return ""  # go back
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # Exactly one header call attributed to cost-view initial entry
    cost_entry_calls = snapshots["after_entry"] - snapshots["before_entry"]
    assert cost_entry_calls == 1, (
        f"Expected 1 header call on cost-view initial entry, got {cost_entry_calls}"
    )


def test_interactive_detail_view_prints_version_header(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Entering a session number calls _print_version_header on initial entry."""
    _write_session(tmp_path, "vh_det00-0000-0000-0000-000000000000", name="VHDetail")

    import copilot_usage.cli as cli_mod

    # Disable watchdog to avoid spurious auto-refresh triggering extra header calls
    def _null_start(session_path: Path, change_event: threading.Event) -> None:  # noqa: ARG001
        """Test stub for _start_observer that disables watchdog behavior."""
        return

    monkeypatch.setattr(cli_mod, "_start_observer", _null_start)

    header_calls: list[str] = []
    orig_header = cli_mod._print_version_header

    def _patched_header(target: Console) -> None:
        header_calls.append("called")
        orig_header(target)

    monkeypatch.setattr(cli_mod, "_print_version_header", _patched_header)

    snapshots: dict[str, int] = {}
    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            snapshots["before_entry"] = len(header_calls)
            return "1"
        if call_count == 2:
            snapshots["after_entry"] = len(header_calls)
            return ""  # go back
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    # Exactly one header call attributed to detail-view initial entry
    detail_entry_calls = snapshots["after_entry"] - snapshots["before_entry"]
    assert detail_entry_calls == 1, (
        f"Expected 1 header call on detail-view initial entry, got {detail_entry_calls}"
    )


@pytest.mark.parametrize(
    ("user_input", "view_name"),
    [("c", "cost"), ("1", "detail")],
    ids=["cost-view", "detail-view"],
)
def test_interactive_version_header_count_matches_auto_refresh(
    tmp_path: Path,
    monkeypatch: Any,
    user_input: str,
    view_name: str,
) -> None:
    """Initial entry and auto-refresh both call _print_version_header exactly once each."""
    _write_session(tmp_path, "vh_cnt00-0000-0000-0000-000000000000", name="VHCount")

    import copilot_usage.cli as cli_mod

    header_calls: list[str] = []
    orig_header = cli_mod._print_version_header

    def _patched_header(target: Console) -> None:
        header_calls.append("called")
        orig_header(target)

    monkeypatch.setattr(cli_mod, "_print_version_header", _patched_header)

    captured_event: list[threading.Event] = []

    def _null_start(session_path: Path, change_event: threading.Event) -> None:  # noqa: ARG001
        captured_event.append(change_event)
        return  # type: ignore[return-value]

    monkeypatch.setattr(cli_mod, "_start_observer", _null_start)

    snapshots: dict[str, int] = {}
    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            snapshots["before_entry"] = len(header_calls)
            return user_input  # navigate to cost or detail view
        if call_count == 2:
            snapshots["after_entry"] = len(header_calls)
            # Trigger auto-refresh
            if captured_event:
                captured_event[0].set()
            return None
        if call_count == 3:
            snapshots["after_refresh"] = len(header_calls)
            return ""  # go back
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0

    initial_entry_calls = snapshots["after_entry"] - snapshots["before_entry"]
    auto_refresh_calls = snapshots["after_refresh"] - snapshots["after_entry"]

    assert initial_entry_calls == 1, (
        f"Expected 1 header call on initial {view_name} entry, "
        f"got {initial_entry_calls}"
    )
    assert auto_refresh_calls == 1, (
        f"Expected 1 header call on auto-refresh of {view_name}, "
        f"got {auto_refresh_calls}"
    )
    assert initial_entry_calls == auto_refresh_calls, (
        f"Header call count mismatch for {view_name}: "
        f"initial={initial_entry_calls}, refresh={auto_refresh_calls}"
    )


# ---------------------------------------------------------------------------
# Issue #309 — _print_version_header direct tests
# ---------------------------------------------------------------------------


class TestPrintVersionHeader:
    """Direct tests for the _print_version_header rendering helper."""

    def test_contains_title_and_version(self) -> None:
        """Output contains 'Copilot Usage' and the current version string."""
        from copilot_usage import __version__

        c = Console(file=None, force_terminal=True, width=80)
        with c.capture() as capture:
            _print_version_header(target=c)
        output = capture.get()

        assert "Copilot Usage" in output
        assert f"v{__version__}" in output

    def test_narrow_console_no_crash(self) -> None:
        """Narrow console (width < title+version) does not crash or produce negative padding."""
        from copilot_usage import __version__

        c = Console(file=None, force_terminal=True, width=10)
        with c.capture() as capture:
            _print_version_header(target=c)
        output = capture.get()

        # Rich may wrap text across lines on narrow consoles; verify no crash
        # and both logical parts are present (possibly split by wrapping)
        assert "Copilot" in output
        assert "Usage" in output
        assert f"v{__version__}" in output

    def test_accepts_explicit_target(self) -> None:
        """Passing an explicit target console routes output there."""
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _print_version_header(target=c)
        output = capture.get()

        assert "Copilot Usage" in output
        assert len(output.strip()) > 0

    def test_padding_at_least_one_space(self) -> None:
        """The max(1, ...) guard ensures at least 1 space between title and version."""
        from copilot_usage import __version__

        title = "Copilot Usage"
        version_text = f"v{__version__}"
        # Width = title + 1 space + version so padding = max(1, 1) = 1
        # and the rendered line fits without wrapping.
        fit_width = len(title) + 1 + len(version_text)

        c = Console(file=None, force_terminal=True, width=fit_width)
        with c.capture() as capture:
            _print_version_header(target=c)
        output = _strip_ansi(capture.get())

        assert title in output
        assert version_text in output
        # Verify at least one space separates title from version
        assert f"{title} {version_text}" in output


# ---------------------------------------------------------------------------
# Issue #309 — _render_session_list direct tests
# ---------------------------------------------------------------------------


class TestRenderSessionList:
    """Direct tests for the _render_session_list rendering helper."""

    def test_one_based_numbering(self) -> None:
        """Row numbers start from 1, not 0."""
        from copilot_usage.models import SessionSummary

        sessions = [
            SessionSummary(session_id="aaaa12340000", is_active=False, name="Alpha"),
            SessionSummary(session_id="bbbb56780000", is_active=False, name="Beta"),
        ]
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = _strip_ansi(capture.get())

        # Match row numbers as first-column cell values between │ delimiters
        assert re.search(r"│\s*1\s*│", output), "Row number 1 not found in # column"
        assert re.search(r"│\s*2\s*│", output), "Row number 2 not found in # column"

    def test_active_status_label(self) -> None:
        """Active sessions display '🟢 Active'."""
        from copilot_usage.models import SessionSummary

        sessions = [
            SessionSummary(
                session_id="aaaa12340000", is_active=True, model="gpt-4", name="Alpha"
            ),
        ]
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = capture.get()

        assert "🟢 Active" in output

    def test_completed_status_label(self) -> None:
        """Completed sessions display 'Completed'."""
        from copilot_usage.models import SessionSummary

        sessions = [
            SessionSummary(
                session_id="bbbb56780000", is_active=False, model="gpt-4", name="Beta"
            ),
        ]
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = capture.get()

        assert "Completed" in output

    def test_missing_model_fallback(self) -> None:
        """Sessions with model=None show '—' in the Model column."""
        from copilot_usage.models import SessionSummary

        sessions = [
            SessionSummary(
                session_id="cccc90120000", is_active=False, model=None, name="Gamma"
            ),
        ]
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = capture.get()

        assert "—" in output

    def test_missing_name_falls_back_to_session_id(self) -> None:
        """Sessions with name=None fall back to session_id[:12]."""
        from copilot_usage.models import SessionSummary

        sessions = [
            SessionSummary(
                session_id="bbbb5678abcd9999",
                is_active=False,
                model=None,
                name=None,
            ),
        ]
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = capture.get()

        assert "bbbb5678abcd" in output

    def test_combined_active_and_completed(self) -> None:
        """Mixed active/completed sessions render all expected labels and fallbacks."""
        from copilot_usage.models import SessionSummary

        active = SessionSummary(
            session_id="aaaa12340000", is_active=True, model="gpt-4", name="Alpha"
        )
        completed = SessionSummary(
            session_id="bbbb56780000efgh", is_active=False, model=None, name=None
        )
        sessions = [active, completed]

        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = _strip_ansi(capture.get())

        # Match row numbers as first-column cell values between │ delimiters
        assert re.search(r"│\s*1\s*│", output), "Row number 1 not found in # column"
        assert re.search(r"│\s*2\s*│", output), "Row number 2 not found in # column"
        assert "🟢 Active" in output  # active label
        assert "Completed" in output  # completed label
        assert "—" in output  # missing model fallback
        assert "bbbb56780000" in output  # name=None → session_id[:12]

    def test_table_title_is_sessions(self) -> None:
        """The table has 'Sessions' as its title."""
        from copilot_usage.models import SessionSummary

        sessions = [
            SessionSummary(session_id="aaaa12340000", is_active=False, name="Test"),
        ]
        c = Console(file=None, force_terminal=True, width=120)
        with c.capture() as capture:
            _render_session_list(c, sessions)
        output = capture.get()

        assert "Sessions" in output


# ---------------------------------------------------------------------------
# Issue #345 — --until date-only normalization
# ---------------------------------------------------------------------------


class TestNormalizeUntil:
    """Verify _normalize_until extends date-only midnight to end-of-day."""

    def test_none_returns_none(self) -> None:
        assert _normalize_until(None) is None

    def test_date_only_midnight_becomes_end_of_day(self) -> None:
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=midnight, has_explicit_time=False)
        result = _normalize_until(arg)
        assert result is not None
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59
        assert result.microsecond == 999999
        assert result.date() == midnight.date()

    def test_explicit_midnight_unchanged(self) -> None:
        """Explicit T00:00:00 is NOT expanded — new behaviour from issue #870."""
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=midnight, has_explicit_time=True)
        result = _normalize_until(arg)
        assert result is not None
        assert result == midnight

    def test_non_midnight_unchanged(self) -> None:
        dt = datetime(2026, 3, 7, 10, 30, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=dt, has_explicit_time=True)
        result = _normalize_until(arg)
        assert result == dt

    def test_naive_date_only_midnight_becomes_aware_end_of_day(self) -> None:
        naive = datetime(2026, 3, 7, 0, 0, 0)
        arg = _ParsedDateArg(value=naive, has_explicit_time=False)
        result = _normalize_until(arg)
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 23

    def test_non_midnight_with_no_explicit_time_expanded(self) -> None:
        """Regression for issue #1026: expansion depends solely on has_explicit_time.

        Constructs a _ParsedDateArg with has_explicit_time=False but a
        non-midnight time component.  Before the fix, the redundant
        ``aware.time() == dt_time(0, 0, 0)`` guard prevented expansion.
        """
        arg = _ParsedDateArg(
            value=datetime(2025, 1, 15, 12, 30, 0, tzinfo=UTC),
            has_explicit_time=False,
        )
        result = _normalize_until(arg)
        assert result is not None
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59
        assert result.microsecond == 999999


class TestNormalizeUntilNonUtcTimezone:
    """_normalize_until preserves non-UTC timezone offsets when expanding date-only."""

    def test_aware_date_only_midnight_non_utc_expanded_in_same_tz(self) -> None:
        tz_plus5 = timezone(timedelta(hours=5))
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=tz_plus5)
        arg = _ParsedDateArg(value=midnight, has_explicit_time=False)
        result = _normalize_until(arg)
        assert result is not None
        assert result.tzinfo == tz_plus5
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59
        assert result.microsecond == 999999
        assert result.date() == midnight.date()

    def test_aware_non_midnight_non_utc_unchanged(self) -> None:
        tz_minus8 = timezone(timedelta(hours=-8))
        dt = datetime(2026, 3, 7, 14, 30, 0, tzinfo=tz_minus8)
        arg = _ParsedDateArg(value=dt, has_explicit_time=True)
        result = _normalize_until(arg)
        assert result == dt


# ---------------------------------------------------------------------------
# Issue #870 — _DateTimeOrDate custom param type
# ---------------------------------------------------------------------------


class TestDateTimeOrDateParamType:
    """Unit tests for the _DateTimeOrDate Click param type."""

    def test_date_only_parsed_without_explicit_time(self) -> None:
        """'2025-01-15' → has_explicit_time=False."""
        ptype = _DateTimeOrDate()
        result = ptype.convert("2025-01-15", None, None)
        assert result.value == datetime(2025, 1, 15, 0, 0, 0)
        assert result.has_explicit_time is False

    def test_full_datetime_parsed_with_explicit_time(self) -> None:
        """'2025-01-15T12:30:00' → has_explicit_time=True."""
        ptype = _DateTimeOrDate()
        result = ptype.convert("2025-01-15T12:30:00", None, None)
        assert result.value == datetime(2025, 1, 15, 12, 30, 0)
        assert result.has_explicit_time is True

    def test_explicit_midnight_parsed_with_explicit_time(self) -> None:
        """'2025-01-15T00:00:00' → has_explicit_time=True."""
        ptype = _DateTimeOrDate()
        result = ptype.convert("2025-01-15T00:00:00", None, None)
        assert result.value == datetime(2025, 1, 15, 0, 0, 0)
        assert result.has_explicit_time is True

    def test_invalid_format_raises_bad_parameter(self) -> None:
        """Unparseable input raises click.exceptions.BadParameter."""
        ptype = _DateTimeOrDate()
        with pytest.raises(click.exceptions.BadParameter):
            ptype.convert("not-a-date", None, None)

    def test_datetime_passthrough_marked_explicit(self) -> None:
        """An already-parsed datetime is wrapped with has_explicit_time=True."""
        ptype = _DateTimeOrDate()
        dt = datetime(2025, 6, 1, 0, 0, 0)
        result = ptype.convert(dt, None, None)
        assert result.value == dt
        assert result.has_explicit_time is True


# ---------------------------------------------------------------------------
# Issue #870 — parametrised _normalize_until
# ---------------------------------------------------------------------------


class TestNormalizeUntilParametrised:
    """Parametrised tests covering the three key cases from issue #870."""

    @pytest.mark.parametrize(
        ("raw_input", "expect_expanded"),
        [
            pytest.param("2025-01-15", True, id="date-only-expanded"),
            pytest.param(
                "2025-01-15T00:00:00", False, id="explicit-midnight-not-expanded"
            ),
            pytest.param("2025-01-15T12:30:00", False, id="non-midnight-unchanged"),
        ],
    )
    def test_expansion_behaviour(self, raw_input: str, expect_expanded: bool) -> None:
        """Verify end-of-day expansion depends on has_explicit_time."""
        ptype = _DateTimeOrDate()
        parsed = ptype.convert(raw_input, None, None)
        result = _normalize_until(parsed)
        assert result is not None
        if expect_expanded:
            assert result.hour == 23
            assert result.minute == 59
            assert result.second == 59
            assert result.microsecond == 999999
        else:
            assert result == ensure_aware_opt(parsed.value)


class TestSummaryUntilDateOnly:
    """CLI-level test: summary --until date-only includes sessions from that date."""

    def test_summary_until_date_only_includes_same_day(self, tmp_path: Path) -> None:
        """--until 2026-03-07 includes a session starting at 10am on 2026-03-07."""
        _write_session(
            tmp_path,
            "aaaa1111-0000-0000-0000-000000000000",
            name="MorningSess",
            start_time="2026-03-07T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["summary", "--path", str(tmp_path), "--until", "2026-03-07"]
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Morning" in output
        assert "1 session" in output

    def test_summary_until_iso_datetime_not_normalized(self, tmp_path: Path) -> None:
        """--until with explicit time is not expanded to end-of-day."""
        _write_session(
            tmp_path,
            "bbbb2222-0000-0000-0000-000000000000",
            name="AfterCutoff",
            start_time="2026-03-07T11:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "summary",
                "--path",
                str(tmp_path),
                "--until",
                "2026-03-07T10:00:00",
            ],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "No sessions" in output


# ---------------------------------------------------------------------------
# Issue #870 — CLI-level regression: explicit midnight not expanded
# ---------------------------------------------------------------------------


class TestIssue870ExplicitMidnight:
    """CLI-level regression tests for issue #870.

    Verifies that ``--until 2025-01-15T00:00:00`` (explicit midnight) excludes
    sessions on the boundary date, while ``--until 2025-01-15`` (date-only)
    includes them.  Both ``summary`` and ``cost`` commands are covered.
    """

    def test_summary_explicit_midnight_excludes_boundary(self, tmp_path: Path) -> None:
        """summary --until ...T00:00:00 excludes sessions starting on that day."""
        _write_session(
            tmp_path,
            "cc001111-0000-0000-0000-000000000000",
            name="BoundarySession",
            start_time="2025-01-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "summary",
                "--path",
                str(tmp_path),
                "--until",
                "2025-01-15T00:00:00",
            ],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "No sessions" in output

    def test_summary_date_only_includes_boundary(self, tmp_path: Path) -> None:
        """summary --until 2025-01-15 (date-only) includes sessions on that day."""
        _write_session(
            tmp_path,
            "cc002222-0000-0000-0000-000000000000",
            name="BoundarySession",
            start_time="2025-01-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["summary", "--path", str(tmp_path), "--until", "2025-01-15"],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Boundary" in output
        assert "1 session" in output

    def test_cost_explicit_midnight_excludes_boundary(self, tmp_path: Path) -> None:
        """cost --until ...T00:00:00 excludes sessions starting on that day."""
        _write_session(
            tmp_path,
            "cc003333-0000-0000-0000-000000000000",
            name="BoundarySession",
            start_time="2025-01-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "cost",
                "--path",
                str(tmp_path),
                "--until",
                "2025-01-15T00:00:00",
            ],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "No sessions" in output

    def test_cost_date_only_includes_boundary(self, tmp_path: Path) -> None:
        """cost --until 2025-01-15 (date-only) includes sessions on that day."""
        _write_session(
            tmp_path,
            "cc004444-0000-0000-0000-000000000000",
            name="BoundarySession",
            start_time="2025-01-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["cost", "--path", str(tmp_path), "--until", "2025-01-15"],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Boundary" in output

    def test_since_unaffected_by_change(self, tmp_path: Path) -> None:
        """--since 2025-01-15 is unaffected — sanity check per issue spec."""
        _write_session(
            tmp_path,
            "cc005555-0000-0000-0000-000000000000",
            name="SinceSession",
            start_time="2025-01-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["summary", "--path", str(tmp_path), "--since", "2025-01-15"],
        )
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Since" in output
        assert "1 session" in output


# ---------------------------------------------------------------------------
# Issue #454 — reversed --since/--until emits click.UsageError
# ---------------------------------------------------------------------------


class TestReversedSinceUntilCliError:
    """CLI-level test: reversed --since/--until exits non-zero with a readable error."""

    def test_summary_reversed_range_exits_nonzero(self, tmp_path: Path) -> None:
        """summary --since 2026-12-31 --until 2026-01-01 exits with non-zero code."""
        _write_session(
            tmp_path,
            "aaaa1111-0000-0000-0000-000000000000",
            name="SomeSession",
            start_time="2026-06-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "summary",
                "--path",
                str(tmp_path),
                "--since",
                "2026-12-31",
                "--until",
                "2026-01-01",
            ],
        )
        assert result.exit_code != 0
        output = _strip_ansi(result.output)
        assert "--since" in output
        assert "after" in output

    def test_cost_reversed_range_exits_nonzero(self, tmp_path: Path) -> None:
        """cost --since 2026-12-31 --until 2026-01-01 exits with non-zero code."""
        _write_session(
            tmp_path,
            "bbbb2222-0000-0000-0000-000000000000",
            name="SomeSession",
            start_time="2026-06-15T10:00:00Z",
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "cost",
                "--path",
                str(tmp_path),
                "--since",
                "2026-12-31",
                "--until",
                "2026-01-01",
            ],
        )
        assert result.exit_code != 0
        output = _strip_ansi(result.output)
        assert "--since" in output
        assert "after" in output


class TestValidateSinceUntil:
    """Direct unit tests for _validate_since_until composition logic."""

    def test_both_none(self) -> None:
        """since=None, until=None → (None, None)."""
        result = _validate_since_until(None, None)
        assert result == (None, None)

    def test_naive_since_made_aware(self) -> None:
        """Naive since is made UTC-aware; until stays None."""
        naive_since = datetime(2026, 3, 7, 10, 0, 0)
        aware_since, aware_until = _validate_since_until(naive_since, None)
        assert aware_until is None
        assert aware_since is not None
        assert aware_since.tzinfo is not None
        assert aware_since.tzinfo == UTC
        assert aware_since.replace(tzinfo=None) == naive_since

    def test_date_only_midnight_until_expanded_to_end_of_day(self) -> None:
        """Date-only midnight until is expanded to 23:59:59.999999."""
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=midnight, has_explicit_time=False)
        aware_since, aware_until = _validate_since_until(None, arg)
        assert aware_since is None
        assert aware_until is not None
        assert aware_until.hour == 23
        assert aware_until.minute == 59
        assert aware_until.second == 59
        assert aware_until.microsecond == 999999
        assert aware_until.date() == midnight.date()
        assert aware_until.tzinfo == UTC

    def test_explicit_midnight_until_not_expanded(self) -> None:
        """Explicit T00:00:00 until is NOT expanded — issue #870 fix."""
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=midnight, has_explicit_time=True)
        aware_since, aware_until = _validate_since_until(None, arg)
        assert aware_since is None
        assert aware_until == midnight

    def test_non_midnight_until_unchanged(self) -> None:
        """Non-midnight until is returned as-is (already aware)."""
        non_midnight = datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=non_midnight, has_explicit_time=True)
        aware_since, aware_until = _validate_since_until(None, arg)
        assert aware_since is None
        assert aware_until == non_midnight

    def test_valid_range_no_error(self) -> None:
        """since < until → both returned without error."""
        dt_before = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
        dt_after = datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=dt_after, has_explicit_time=True)
        aware_since, aware_until = _validate_since_until(dt_before, arg)
        assert aware_since is not None
        assert aware_until is not None
        assert aware_since <= aware_until

    def test_reversed_range_raises_usage_error(self) -> None:
        """since > until → click.UsageError with --since, after, and isoformat timestamps."""
        dt_after = datetime(2026, 12, 31, 0, 0, 0, tzinfo=UTC)
        dt_before = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        arg = _ParsedDateArg(value=dt_before, has_explicit_time=True)
        with pytest.raises(click.UsageError, match="--since") as exc_info:
            _validate_since_until(dt_after, arg)
        msg = str(exc_info.value)
        assert "after" in msg
        # Verify isoformat timestamps with sep=' ' and timespec='seconds'
        expected_since = dt_after.isoformat(sep=" ", timespec="seconds")
        expected_until = dt_before.isoformat(sep=" ", timespec="seconds")
        assert expected_since in msg
        assert expected_until in msg


# ---------------------------------------------------------------------------
# vscode happy-path CLI test
# ---------------------------------------------------------------------------


def test_vscode_command(tmp_path: Path) -> None:
    """vscode command renders summary for a valid log directory."""
    log_dir = tmp_path / "session_1" / "window1" / "exthost" / "GitHub.copilot-chat"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "GitHub Copilot Chat.log"
    log_file.write_text(
        "2026-03-15 10:00:00.123 [info] ccreq:abc.copilotmd | success | "
        "claude-sonnet-4 | 500ms | [chat]\n",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])
    assert result.exit_code == 0
    output = result.output
    assert "VS Code Copilot Chat" in output
    assert "Per-Model Breakdown" in output
    assert "claude-sonnet-4" in output
    assert "By Feature" in output
    assert "Daily Activity" in output


# ---------------------------------------------------------------------------
# vscode – TestVscodeCommand
# ---------------------------------------------------------------------------

_VSCODE_LOG_LINE = (
    "2026-03-15 10:00:00.123 [info] ccreq:abc.copilotmd | success | "
    "claude-sonnet-4 | 500ms | [chat]\n"
)

_VSCODE_LOG_LINE_2 = (
    "2026-03-15 11:00:00.456 [info] ccreq:def.copilotmd | success | "
    "gpt-4o-mini | 300ms | [inline-chat]\n"
)


def _make_vscode_log(base: Path, session_name: str, content: str) -> Path:
    """Create a VS Code log file inside *base* with the correct directory structure."""
    log_dir = base / session_name / "window1" / "exthost" / "GitHub.copilot-chat"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "GitHub Copilot Chat.log"
    log_file.write_text(content)
    return log_file


class TestVscodeCommand:
    """Tests for ``vscode`` command edge cases and error paths."""

    def test_vscode_command_no_requests_exits_1(self, tmp_path: Path) -> None:
        """Log directory with files that contain zero parsable requests → exit 1."""
        _make_vscode_log(tmp_path, "session_1", "some irrelevant log line\n")
        runner = CliRunner()
        result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])
        assert result.exit_code == 1
        assert "No VS Code Copilot Chat requests found" in result.output

    def test_vscode_command_nonexistent_logs_exits_2(self, tmp_path: Path) -> None:
        """Nonexistent ``--vscode-logs`` path → Click usage error with exit code 2."""
        bad_path = tmp_path / "does-not-exist"
        runner = CliRunner()
        result = runner.invoke(main, ["vscode", "--vscode-logs", str(bad_path)])
        assert result.exit_code == 2

    def test_vscode_command_multi_file_aggregation(self, tmp_path: Path) -> None:
        """Two log files discovered → request counts are summed correctly."""
        _make_vscode_log(tmp_path, "session_1", _VSCODE_LOG_LINE * 3)
        _make_vscode_log(tmp_path, "session_2", _VSCODE_LOG_LINE_2 * 2)
        runner = CliRunner()
        result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])
        assert result.exit_code == 0
        clean = _strip_ansi(result.output)
        assert re.search(r"Requests:\s*5\b", clean), f"Expected 5 requests in: {clean}"
        # Both models should appear
        assert "claude-sonnet-4" in result.output
        assert "gpt-4o-mini" in result.output

    def test_vscode_command_one_file_oserror_skipped(self, tmp_path: Path) -> None:
        """One unreadable file in a multi-file directory → only the readable file counts."""
        _make_vscode_log(tmp_path, "session_1", _VSCODE_LOG_LINE * 2)
        bad_file = _make_vscode_log(tmp_path, "session_2", _VSCODE_LOG_LINE_2 * 3)

        original_open = Path.open

        def _open_with_oserror_for_bad_file(
            self: Path, *args: Any, **kwargs: Any
        ) -> Any:
            if self == bad_file:
                raise OSError("simulated read failure")
            return original_open(self, *args, **kwargs)  # pyright: ignore[reportUnknownVariableType]

        runner = CliRunner()
        with patch.object(
            Path,
            "open",
            autospec=True,
            side_effect=_open_with_oserror_for_bad_file,
        ):
            result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])

        assert result.exit_code == 0
        clean = _strip_ansi(result.output)
        # Only the 2 requests from the good file should count.
        assert re.search(r"Requests:\s*2\b", clean), f"Expected 2 requests in: {clean}"
        assert "claude-sonnet-4" in result.output

    def test_vscode_command_all_files_unreadable_prints_specific_error(
        self, tmp_path: Path
    ) -> None:
        """All discovered log files raise OSError → specific 'could not be read' error."""
        _make_vscode_log(tmp_path, "session_1", _VSCODE_LOG_LINE)

        original_open = Path.open

        def _open_always_fails(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.name == "GitHub Copilot Chat.log":
                raise OSError("simulated read failure")
            return original_open(self, *args, **kwargs)  # pyright: ignore[reportUnknownVariableType]

        runner = CliRunner()
        with patch.object(
            Path,
            "open",
            autospec=True,
            side_effect=_open_always_fails,
        ):
            result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])

        assert result.exit_code == 1
        assert "log files were found but could not be read" in result.output
        assert "No VS Code Copilot Chat requests found" not in result.output


# ---------------------------------------------------------------------------
# Lazy watchdog import
# ---------------------------------------------------------------------------


def test_watchdog_not_imported_at_module_level() -> None:
    """Importing copilot_usage.cli must NOT pull in watchdog eagerly."""
    import sys

    # Snapshot full module state so we can restore everything after the test,
    # preventing leaked interpreter state from making later tests flaky.
    saved_modules = sys.modules.copy()

    # Save the parent-package attribute because Python's import machinery
    # sets ``copilot_usage.cli`` on the parent when the submodule is imported,
    # and ``sys.modules.update`` alone does not undo that side-effect.
    import copilot_usage as _pkg

    try:
        saved_cli_attr = _pkg.cli  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType,reportAttributeAccessIssue]
        _had_cli_attr = True
    except AttributeError:
        saved_cli_attr = None
        _had_cli_attr = False

    # Only remove the specific modules we need to re-import: the CLI module
    # (and its submodules) plus watchdog, so we can observe a fresh import.
    mods_to_remove = [
        name
        for name in list(sys.modules)
        if name == "copilot_usage.cli"
        or name.startswith("copilot_usage.cli.")
        or name == "watchdog"
        or name.startswith("watchdog.")
    ]
    for name in mods_to_remove:
        del sys.modules[name]

    try:
        import copilot_usage.cli  # noqa: F401  # pyright: ignore[reportUnusedImport]

        assert "watchdog.observers" not in sys.modules
        assert "watchdog.events" not in sys.modules
    finally:
        # Restore original module state so subsequent tests are unaffected.
        for key in list(sys.modules):
            if key not in saved_modules:
                del sys.modules[key]
        sys.modules.update(saved_modules)

        # Restore the parent-package attribute to the original module object.
        if _had_cli_attr:
            _pkg.cli = saved_cli_attr  # pyright: ignore[reportAttributeAccessIssue]
        else:
            # If the attribute did not exist before, remove any attribute that
            # was added as a side-effect of importing copilot_usage.cli.
            with contextlib.suppress(AttributeError):
                del _pkg.cli  # pyright: ignore[reportAttributeAccessIssue]


# ---------------------------------------------------------------------------
# Issue #585 — O(1) session-ID lookup via _build_session_index
# ---------------------------------------------------------------------------


def test_build_session_index_returns_correct_mapping() -> None:
    """_build_session_index maps each session_id to its list position."""
    from copilot_usage.models import SessionSummary

    sessions = [
        SessionSummary(session_id=f"sess-{i:04d}", is_active=False) for i in range(5)
    ]
    index = _build_session_index(sessions)
    assert index == {
        "sess-0000": 0,
        "sess-0001": 1,
        "sess-0002": 2,
        "sess-0003": 3,
        "sess-0004": 4,
    }


def test_build_session_index_empty_list() -> None:
    """_build_session_index returns empty dict for no sessions."""
    assert _build_session_index([]) == {}


def test_auto_refresh_detail_uses_session_index_for_200_sessions(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Auto-refresh detail lookup uses _build_session_index with 200+ sessions.

    Constructs 200 SessionSummary objects, simulates a file-change event while
    detail_session_id is set to the *last* session ID (worst case for a linear
    scan), asserts the correct detail_idx is resolved, and verifies that
    _build_session_index is actually invoked during the refresh path.
    """
    from copilot_usage.models import SessionSummary
    from copilot_usage.parser import get_all_sessions

    num_sessions = 200
    target_session_id = f"sess-{num_sessions - 1:04d}-0000-0000-0000-000000000000"

    # Write a real session for the target so _show_session_by_index can render it
    _write_session(
        tmp_path,
        target_session_id,
        name="TargetSession",
        start_time="2025-01-15T10:00:00Z",
    )

    import copilot_usage.cli as cli_mod

    # Build a large sessions list where the target is last
    fake_sessions: list[SessionSummary] = [
        SessionSummary(
            session_id=f"sess-{i:04d}-0000-0000-0000-000000000000",
            is_active=False,
            name=f"Session{i}",
        )
        for i in range(num_sessions - 1)
    ]
    # Target is the real session so it has events_path for rendering
    real_sessions = get_all_sessions(tmp_path)
    target = next(s for s in real_sessions if s.session_id == target_session_id)
    fake_sessions.append(target)

    # Track which index is rendered
    rendered_indices: list[int] = []
    orig_show = cli_mod._show_session_by_index

    def _tracking_show(console: Console, sessions: list[Any], index: int) -> None:
        rendered_indices.append(index)
        orig_show(console, sessions, index)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _tracking_show)

    # Make get_all_sessions return our large fake list
    def _fake_get_all(_path: Path | None) -> list[SessionSummary]:
        return fake_sessions

    monkeypatch.setattr(cli_mod, "get_all_sessions", _fake_get_all)

    # Spy on _build_session_index to verify it is called during refresh
    build_index_calls: list[int] = []
    orig_build = cli_mod._build_session_index

    def _spy_build_session_index(
        sessions: list[SessionSummary],
    ) -> dict[str, int]:
        build_index_calls.append(len(sessions))
        return orig_build(sessions)

    monkeypatch.setattr(cli_mod, "_build_session_index", _spy_build_session_index)

    captured_event: list[threading.Event] = []

    def _capturing_start(
        session_path: Path,  # noqa: ARG001
        change_event: threading.Event,
    ) -> object:
        captured_event.append(change_event)

        class _StubObserver:
            def stop(self) -> None:
                return

            def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
                return

        return _StubObserver()

    monkeypatch.setattr(cli_mod, "_start_observer", _capturing_start)

    read_call = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal read_call
        read_call += 1
        if read_call == 1:
            # Enter detail for the last session (1-based index)
            return str(num_sessions)
        if read_call == 2:
            # Trigger auto-refresh while in detail view
            if captured_event:
                captured_event[0].set()
            return None
        if read_call == 3:
            return ""  # go back to home
        return "q"

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0

    # Both initial selection and auto-refresh must render the last session
    assert len(rendered_indices) >= 2
    assert all(idx == num_sessions for idx in rendered_indices), (
        f"Expected index {num_sessions} every time, got: {rendered_indices}"
    )

    # _build_session_index must be called at least twice: once on startup,
    # once on auto-refresh.  This would fail if the code regressed to a
    # linear next(enumerate(…)) scan.
    assert len(build_index_calls) >= 2, (
        f"Expected _build_session_index to be called ≥2 times, got {len(build_index_calls)}"
    )


def test_session_index_lookup_performance() -> None:
    """Dict-based lookup for 200 sessions completes well under 50 ms.

    Uses a generous wall-clock budget to avoid flakiness on shared CI runners
    with CPU throttling or debug builds.
    """
    import time

    from copilot_usage.models import SessionSummary

    sessions = [
        SessionSummary(
            session_id=f"perf-{i:04d}-0000-0000-0000-000000000000",
            is_active=False,
        )
        for i in range(200)
    ]
    target_id = sessions[-1].session_id

    start = time.perf_counter_ns()
    index = _build_session_index(sessions)
    result = index.get(target_id)
    elapsed_ns = time.perf_counter_ns() - start

    assert result == 199
    assert elapsed_ns < 50_000_000, (
        f"Lookup took {elapsed_ns / 1_000_000:.3f} ms (> 50 ms)"
    )


# ---------------------------------------------------------------------------
# EOF handling in _read_line_nonblocking (issue #746)
# ---------------------------------------------------------------------------


def test_read_line_nonblocking_raises_eoferror_on_closed_pipe(
    monkeypatch: Any,
) -> None:
    """_read_line_nonblocking raises EOFError when stdin is a closed pipe."""
    r_fd, w_fd = os.pipe()
    os.close(w_fd)  # close write end → read end reaches EOF immediately
    read_file = os.fdopen(r_fd, "r")
    try:
        monkeypatch.setattr("sys.stdin", read_file)
        with pytest.raises(EOFError):
            _read_line_nonblocking(timeout=1.0)
    finally:
        read_file.close()


def test_interactive_loop_exits_on_selectable_eof_stdin(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """_interactive_loop exits cleanly (exit code 0) when _read_line_nonblocking raises EOFError."""
    import copilot_usage.cli as cli_mod

    _write_session(tmp_path, "eof70000-0000-0000-0000-000000000000", name="EOF-select")

    call_count = 0

    def _fake_read(timeout: float = 0.5) -> str | None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise EOFError("stdin closed")
        return "q"  # safety fallback — should never be reached

    monkeypatch.setattr(cli_mod, "_read_line_nonblocking", _fake_read)

    def _noop_start(session_path: Path, change_event: threading.Event) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(cli_mod, "_start_observer", _noop_start)

    runner = CliRunner()
    result = runner.invoke(main, ["--path", str(tmp_path)])
    assert result.exit_code == 0
    assert call_count == 1, "Loop should have exited on the first EOFError"


# ---------------------------------------------------------------------------
# Issue #808 — session command edge cases
# ---------------------------------------------------------------------------


def test_session_command_empty_id_exits_with_error(tmp_path: Path) -> None:
    """session '' should exit 1 with a useful error, not silently show a session."""
    _write_session(tmp_path, "aaaa0000-0000-0000-0000-000000000000", name="First")
    runner = CliRunner()
    result = runner.invoke(main, ["session", "", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "session ID cannot be empty" in result.output


def test_show_session_by_index_generic_oserror() -> None:
    """OSError (not just FileNotFoundError) from get_cached_events is caught."""
    from copilot_usage.models import SessionSummary

    s = SessionSummary(
        session_id="oserr000-0000-0000-0000-000000000000",
        events_path=Path("/fake/path/events.jsonl"),
    )
    console = Console(file=None, force_terminal=True)

    with (
        patch(
            "copilot_usage.cli.get_cached_events",
            side_effect=OSError("permission denied"),
        ),
        console.capture() as capture,
    ):
        _show_session_by_index(console, [s], 1)

    output = capture.get().lower()
    assert "no longer available" in output
    assert "permission denied" in output


def test_build_session_index_duplicate_ids() -> None:
    """Duplicate session_id: last occurrence wins in the index."""
    from copilot_usage.models import SessionSummary

    sessions = [
        SessionSummary(session_id="dup-id", is_active=False),
        SessionSummary(session_id="unique-id", is_active=False),
        SessionSummary(session_id="dup-id", is_active=True),
    ]
    index = _build_session_index(sessions)
    assert index["dup-id"] == 2
    assert index["unique-id"] == 1


# ---------------------------------------------------------------------------
# Issue #1045 — non-interactive commands use a single per-call Console
# ---------------------------------------------------------------------------


class TestSingleConsolePerCommand:
    """Each non-interactive command should construct exactly one Console.

    Verifies the fix for issue #1045 by patching ``Console`` in the CLI
    module **and** every renderer module (``report``, ``render_detail``,
    ``interactive``, ``vscode_report``).  If any renderer falls back to
    creating its own ``Console()`` (because ``target_console`` was not
    forwarded), the spy will record a second construction and the
    ``len(created_consoles) == 1`` assertion will fail.
    """

    def _invoke_and_capture_consoles(
        self,
        monkeypatch: pytest.MonkeyPatch,
        args: list[str],
    ) -> tuple[Any, list[Console]]:
        """Invoke a CLI command and record every ``Console`` created.

        Patches ``Console`` in the CLI module and all renderer modules so
        that a fallback ``target_console or Console()`` in any renderer is
        caught as a second construction.
        """
        import copilot_usage.cli as cli_module
        import copilot_usage.interactive as interactive_module
        import copilot_usage.render_detail as render_detail_module
        import copilot_usage.report as report_module
        import copilot_usage.vscode_report as vscode_report_module

        created_consoles: list[Console] = []
        original_console = cli_module.Console

        def recording_console(*a: Any, **kw: Any) -> Console:
            console = original_console(*a, **kw)
            created_consoles.append(console)
            return console

        monkeypatch.setattr(cli_module, "Console", recording_console)
        monkeypatch.setattr(interactive_module, "Console", recording_console)
        monkeypatch.setattr(report_module, "Console", recording_console)
        monkeypatch.setattr(render_detail_module, "Console", recording_console)
        monkeypatch.setattr(vscode_report_module, "Console", recording_console)
        runner = CliRunner()
        result = runner.invoke(main, args)
        assert result.exit_code == 0
        assert len(created_consoles) == 1
        return result, created_consoles

    def test_summary_header_and_body_same_console(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """summary command: one Console renders both header and body."""
        _write_session(tmp_path, "aaaa1111-0000-0000-0000-000000000000", name="First")
        result, created_consoles = self._invoke_and_capture_consoles(
            monkeypatch,
            ["summary", "--path", str(tmp_path)],
        )
        assert len(created_consoles) == 1
        output = _strip_ansi(result.output)
        assert "Copilot Usage" in output
        assert f"v{__version__}" in output
        # Report body content from render_summary
        assert "First" in output or "Summary" in output

    def test_session_header_and_body_same_console(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session command: one Console renders both header and body."""
        _write_session(tmp_path, "bbbb2222-0000-0000-0000-000000000000", name="Detail")
        result, created_consoles = self._invoke_and_capture_consoles(
            monkeypatch,
            ["session", "bbbb2222", "--path", str(tmp_path)],
        )
        assert len(created_consoles) == 1
        output = _strip_ansi(result.output)
        assert "Copilot Usage" in output
        assert f"v{__version__}" in output
        assert "Session Detail" in output

    def test_cost_header_and_body_same_console(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cost command: one Console renders both header and body."""
        _write_session(
            tmp_path,
            "cccc3333-0000-0000-0000-000000000000",
            name="Cost Test",
            premium=5,
        )
        result, created_consoles = self._invoke_and_capture_consoles(
            monkeypatch,
            ["cost", "--path", str(tmp_path)],
        )
        assert len(created_consoles) == 1
        output = _strip_ansi(result.output)
        assert "Copilot Usage" in output
        assert f"v{__version__}" in output
        assert "Cost" in output or "Total" in output

    def test_live_header_and_body_same_console(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """live command: one Console renders both header and body."""
        _write_session(
            tmp_path,
            "dddd4444-0000-0000-0000-000000000000",
            name="Active",
            active=True,
        )
        result, created_consoles = self._invoke_and_capture_consoles(
            monkeypatch,
            ["live", "--path", str(tmp_path)],
        )
        assert len(created_consoles) == 1
        output = _strip_ansi(result.output)
        assert "Copilot Usage" in output
        assert f"v{__version__}" in output
        assert "Active Copilot Sessions" in output or "Active" in output

    def test_vscode_header_and_body_same_console(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """vscode command: one Console renders both header and body."""
        log_dir = tmp_path / "session_1" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(
            "2026-03-15 10:00:00.123 [info] ccreq:abc.copilotmd | success | "
            "claude-sonnet-4 | 500ms | [chat]\n",
        )
        result, created_consoles = self._invoke_and_capture_consoles(
            monkeypatch,
            ["vscode", "--vscode-logs", str(tmp_path)],
        )
        assert len(created_consoles) == 1
        output = _strip_ansi(result.output)
        assert "Copilot Usage" in output
        assert f"v{__version__}" in output
        assert "VS Code Copilot Chat" in output
