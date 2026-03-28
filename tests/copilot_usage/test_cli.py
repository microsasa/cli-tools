"""Tests for copilot_usage.cli — wired-up CLI commands."""

import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from copilot_usage import __version__
from copilot_usage.cli import (
    _normalize_until,  # pyright: ignore[reportPrivateUsage]
    _print_version_header,  # pyright: ignore[reportPrivateUsage]
    _read_line_nonblocking,  # pyright: ignore[reportPrivateUsage]
    _render_session_list,  # pyright: ignore[reportPrivateUsage]
    _show_session_by_index,  # pyright: ignore[reportPrivateUsage]
    _start_observer,  # pyright: ignore[reportPrivateUsage]
    _stop_observer,  # pyright: ignore[reportPrivateUsage]
    main,
)
from copilot_usage.models import ensure_aware_opt
from copilot_usage.parser import parse_events

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


def test_session_prefix_match(tmp_path: Path, monkeypatch: Any) -> None:
    """Test that session command matches by prefix when using custom path."""
    _write_session(tmp_path, "iiii9999-0000-0000-0000-000000000000", name="Prefix Test")

    def _fake_discover(_base_path: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "iiii9999"])
    assert result.exit_code == 0
    assert "iiii9999" in result.output


def test_session_prefix_collision_returns_newest_by_mtime(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When two sessions share the same prefix, the newest by mtime is returned.

    This documents the intentional 'first discovered wins' contract.
    If ambiguity-detection logic is ever added, this test must be updated.
    """
    older_uuid = "ab111111-0000-0000-0000-000000000000"
    newer_uuid = "ab222222-0000-0000-0000-000000000000"
    older_dir = _write_session(tmp_path, older_uuid, name="OlderSession")
    newer_dir = _write_session(tmp_path, newer_uuid, name="NewerSession")

    # Set explicit mtimes so the newer session sorts first even on filesystems
    # with coarse mtime resolution (avoids CI flakiness).
    os.utime(older_dir / "events.jsonl", (1_000_000, 1_000_000))
    os.utime(newer_dir / "events.jsonl", (2_000_000, 2_000_000))

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        paths = list(tmp_path.glob("*/events.jsonl"))
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "ab"])  # ambiguous prefix
    assert result.exit_code == 0
    # Newest match is returned — not an error, not a list of alternatives
    assert "ab222222" in result.output
    assert "ab111111" not in result.output


def test_session_shows_available_on_miss(tmp_path: Path, monkeypatch: Any) -> None:
    """Test that session command shows available IDs when no match found."""
    _write_session(tmp_path, "jjjj0000-0000-0000-0000-000000000000", name="Exists")

    def _fake_discover(_base_path: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "notfound"])
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
    """session command with empty discover → 'No sessions found.' (lines 99-101)."""

    def _empty_discover(_base: Path | None = None) -> list[Path]:
        return []

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _empty_discover)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "anything"])
    assert result.exit_code != 0
    assert "No sessions found" in result.output


def test_session_skips_empty_events(tmp_path: Path, monkeypatch: Any) -> None:
    """session command skips files with no parseable events (line 107, 118)."""
    # Create a session dir with an empty events.jsonl
    empty_dir = tmp_path / "empty-sess"
    empty_dir.mkdir()
    (empty_dir / "events.jsonl").write_text("\n", encoding="utf-8")

    # Also create a valid session to generate the "Available" list
    _write_session(tmp_path, "kkkk1111-0000-0000-0000-000000000000", name="Valid")

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "nonexistent"])
    assert result.exit_code != 0
    assert "no session matching" in result.output


def test_session_error_handling(tmp_path: Path, monkeypatch: Any) -> None:
    """PermissionError in discover_sessions produces a friendly error message."""

    def _exploding_discover(_base: Path | None = None) -> list[Path]:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _exploding_discover)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "anything"])
    assert result.exit_code != 0
    assert "permission denied" in result.output
    assert "Traceback" not in (result.output or "")


def test_session_command_continues_after_parse_oserror(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """OSError from parse_events for one session is silently skipped;
    the command still finds a matching session in another directory."""
    target_session_dir = _write_session(tmp_path, "target-session-aaa", name="Target")
    failing_session_dir = _write_session(
        tmp_path, "failing-session-bbb", name="Failing"
    )

    # Set explicit mtimes so the failing session is visited first (higher mtime
    # appears first in the reverse-sorted list), avoiding nondeterminism on
    # filesystems with coarse mtime resolution.
    target_events = target_session_dir / "events.jsonl"
    failing_events = failing_session_dir / "events.jsonl"
    os.utime(target_events, (1_000_000, 1_000_000))
    os.utime(failing_events, (2_000_000, 2_000_000))

    original_parse = parse_events

    def _flaky_parse(path: Path) -> list[Any]:
        if "failing" in str(path):
            raise OSError("permission denied")
        return original_parse(path)

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)
    monkeypatch.setattr("copilot_usage.cli.parse_events", _flaky_parse)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "target"])
    assert result.exit_code == 0
    assert "target" in result.output.lower()
    assert "Traceback" not in (result.output or "")


def test_session_command_all_parse_oserror(tmp_path: Path, monkeypatch: Any) -> None:
    """When all sessions fail to parse with OSError, the command shows
    'no session matching' rather than crashing."""
    _write_session(tmp_path, "sess-aaa-fail-0000", name="Fail1")
    _write_session(tmp_path, "sess-bbb-fail-0000", name="Fail2")

    def _always_fail(path: Path) -> list[Any]:
        raise OSError("disk I/O error")

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)
    monkeypatch.setattr("copilot_usage.cli.parse_events", _always_fail)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "sess"])
    assert result.exit_code == 1
    assert "no session matching" in result.output.lower()
    assert "Traceback" not in (result.output or "")


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
            "copilot_usage.cli.Observer.start",
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
            "copilot_usage.cli.Observer.start",
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
                "copilot_usage.cli.Observer.start",
                side_effect=RuntimeError("partial start failure"),
            ),
            patch("copilot_usage.cli.Observer.is_alive", return_value=True),
            patch("copilot_usage.cli.Observer.stop") as mock_stop,
            patch("copilot_usage.cli.Observer.join") as mock_join,
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
                "copilot_usage.cli.Observer.start",
                side_effect=RuntimeError("start failed"),
            ),
            patch("copilot_usage.cli.Observer.is_alive", return_value=True),
            patch(
                "copilot_usage.cli.Observer.stop",
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
            "copilot_usage.cli.Observer.start",
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


# ---------------------------------------------------------------------------
# _FileChangeHandler tests
# ---------------------------------------------------------------------------


class TestFileChangeHandler:
    """Tests for _FileChangeHandler debounce logic."""

    def test_dispatch_sets_event_on_first_call(self) -> None:
        """First dispatch call within a cold window sets the change_event."""
        from copilot_usage.cli import (
            _FileChangeHandler,  # pyright: ignore[reportPrivateUsage]
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        handler.dispatch(object())
        assert event.is_set()

    def test_dispatch_suppresses_within_debounce_window(self) -> None:
        """Second dispatch call within 2 s is suppressed (debounce)."""
        import time as _time

        from copilot_usage.cli import (
            _FileChangeHandler,  # pyright: ignore[reportPrivateUsage]
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        handler.dispatch(object())
        assert event.is_set()

        # Clear and force _last_trigger to now so second call is within debounce
        event.clear()
        handler._last_trigger = _time.monotonic()  # pyright: ignore[reportPrivateUsage]
        handler.dispatch(object())
        assert not event.is_set()

    def test_dispatch_fires_again_after_debounce_gap(self) -> None:
        """Dispatch fires again after > 2 s gap."""
        import time as _time

        from copilot_usage.cli import (
            _FileChangeHandler,  # pyright: ignore[reportPrivateUsage]
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        handler.dispatch(object())
        assert event.is_set()

        event.clear()
        # Simulate passage of time by manipulating _last_trigger
        handler._last_trigger = _time.monotonic() - 3.0  # pyright: ignore[reportPrivateUsage]
        handler.dispatch(object())
        assert event.is_set()

    def test_inherits_from_filesystemeventhandler(self) -> None:
        """_FileChangeHandler inherits from watchdog FileSystemEventHandler."""
        from watchdog.events import (
            FileSystemEventHandler,  # type: ignore[import-untyped]
        )

        from copilot_usage.cli import (
            _FileChangeHandler,  # pyright: ignore[reportPrivateUsage]
        )

        event = threading.Event()
        handler = _FileChangeHandler(event)
        assert isinstance(handler, FileSystemEventHandler)
        handler.dispatch(object())
        assert event.is_set()


# ---------------------------------------------------------------------------
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


def test_group_path_propagates_to_session(tmp_path: Path, monkeypatch: Any) -> None:
    """Group-level --path is used by 'session' when subcommand omits --path."""
    _write_session(tmp_path, "grp_ses00-0000-0000-0000-000000000000", name="GrpSes")

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)
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

    orig_draw_home = cli_mod._draw_home  # pyright: ignore[reportPrivateUsage]

    def _patched_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_home_calls.append(1)
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _patched_draw_home)

    # Capture the change_event via _start_observer
    captured_event: list[threading.Event] = []
    orig_start_observer = cli_mod._start_observer  # pyright: ignore[reportPrivateUsage]

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
    orig_start_observer = cli_mod._start_observer  # pyright: ignore[reportPrivateUsage]

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

    orig_show = cli_mod._show_session_by_index  # pyright: ignore[reportPrivateUsage]

    def _patched_show(*args: Any, **kwargs: Any) -> None:
        show_detail_calls.append(1)
        orig_show(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _patched_show)

    # Capture the change_event via _start_observer
    captured_event: list[threading.Event] = []
    orig_start_observer = cli_mod._start_observer  # pyright: ignore[reportPrivateUsage]

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
    orig_show = cli_mod._show_session_by_index  # pyright: ignore[reportPrivateUsage]

    def _tracking_show(*args: Any, **kwargs: Any) -> None:
        show_detail_calls.append(1)
        orig_show(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "_show_session_by_index", _tracking_show)

    draw_home_calls: list[int] = []
    orig_draw_home = cli_mod._draw_home  # pyright: ignore[reportPrivateUsage]

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
    orig_show = cli_mod._show_session_by_index  # pyright: ignore[reportPrivateUsage]

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
    orig_draw_home = cli_mod._draw_home  # pyright: ignore[reportPrivateUsage]

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
    orig_draw_home = cli_mod._draw_home  # pyright: ignore[reportPrivateUsage]

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
    orig_show = cli_mod._show_session_by_index  # pyright: ignore[reportPrivateUsage]

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
    orig_draw_home = cli_mod._draw_home  # pyright: ignore[reportPrivateUsage]

    def _crashing_draw_home(console: Console, sessions: list[Any]) -> None:
        draw_call_count[0] += 1
        if draw_call_count[0] == 2:
            raise OSError("console write failure")
        orig_draw_home(console, sessions)

    monkeypatch.setattr(cli_mod, "_draw_home", _crashing_draw_home)

    prompt_call_count = [0]
    orig_write_prompt = cli_mod._write_prompt  # pyright: ignore[reportPrivateUsage]

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
# Issue #138 — fast pre-filter on directory name
# ---------------------------------------------------------------------------


def test_session_prefilter_skips_non_matching_dirs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """parse_events is only called for the matching directory when prefix ≥ 4 chars.

    Creates ≥ 5 UUID-named sessions and verifies the pre-filter skips parsing
    directories whose names don't start with the requested prefix.
    """
    uuids = [
        "aaaaaaaa-1111-1111-1111-111111111111",
        "bbbbbbbb-2222-2222-2222-222222222222",
        "cccccccc-3333-3333-3333-333333333333",
        "dddddddd-4444-4444-4444-444444444444",
        "eeeeeeee-5555-5555-5555-555555555555",
    ]
    target = uuids[2]  # cccccccc-...

    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    parse_calls: list[Path] = []
    original_parse = __import__(
        "copilot_usage.parser", fromlist=["parse_events"]
    ).parse_events

    def _tracking_parse(events_path: Path) -> list[Any]:
        parse_calls.append(events_path)
        return original_parse(events_path)

    monkeypatch.setattr("copilot_usage.cli.parse_events", _tracking_parse)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "cccccccc"])
    assert result.exit_code == 0
    assert "cccccccc" in result.output

    # Only the matching directory should have been parsed
    assert len(parse_calls) == 1
    assert parse_calls[0].parent.name == target


def test_session_prefilter_short_prefix_parses_all(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Short prefixes (< 4 chars) bypass the pre-filter and parse all sessions."""
    uuids = [
        "ab111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "ab222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "cd333333-cccc-cccc-cccc-cccccccccccc",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        # Sort reverse-alphabetically so cd333… (non-matching) is visited
        # before ab… dirs, proving the pre-filter didn't skip it.
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.parent.name,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    parse_calls: list[Path] = []
    original_parse = __import__(
        "copilot_usage.parser", fromlist=["parse_events"]
    ).parse_events

    def _tracking_parse(events_path: Path) -> list[Any]:
        parse_calls.append(events_path)
        return original_parse(events_path)

    monkeypatch.setattr("copilot_usage.cli.parse_events", _tracking_parse)

    runner = CliRunner()
    # "ab" is only 2 chars — pre-filter should NOT skip anything
    result = runner.invoke(main, ["session", "ab"])
    assert result.exit_code == 0

    # The non-matching cd333… dir must have been parsed (no pre-filter applied).
    assert len(parse_calls) >= 2
    parsed_dirs = {p.parent.name for p in parse_calls}
    assert "cd333333-cccc-cccc-cccc-cccccccccccc" in parsed_dirs

    # First match (reverse-alpha discovery: ab222222 before ab111111) is shown.
    assert "ab222222" in result.output
    assert "ab111111" not in result.output


def test_session_exact_uuid_wins_over_partial(tmp_path: Path, monkeypatch: Any) -> None:
    """An exact full-UUID match is found regardless of discovery order."""
    uuids = [
        "ab111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "ab222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        # ab222222 is discovered before ab111111
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.parent.name,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    runner = CliRunner()
    # Exact UUID targets ab111111 even though ab222222 is discovered first
    result = runner.invoke(main, ["session", "ab111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])
    assert result.exit_code == 0
    assert "ab111111" in result.output
    assert "ab222222" not in result.output


def test_session_prefilter_non_uuid_dirs_always_parsed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Non-UUID directory names are always parsed even with long prefix."""
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

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return list(tmp_path.glob("*/events.jsonl"))

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    runner = CliRunner()
    result = runner.invoke(main, ["session", "corrupt0"])
    assert result.exit_code == 0
    assert "corrupt0" in result.output


def test_session_command_one_char_prefix_skips_prefilter(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A 1-character prefix bypasses the pre-filter and parses all sessions."""
    uuids = [
        "a1111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "a2222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "b3333333-cccc-cccc-cccc-cccccccccccc",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        # Non-matching dir first to prove prefilter was skipped
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.parent.name,
            reverse=True,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    parse_calls: list[Path] = []
    original_parse = __import__(
        "copilot_usage.parser", fromlist=["parse_events"]
    ).parse_events

    def _tracking_parse(events_path: Path) -> list[Any]:
        parse_calls.append(events_path)
        return original_parse(events_path)

    monkeypatch.setattr("copilot_usage.cli.parse_events", _tracking_parse)

    runner = CliRunner()
    # "a" is only 1 char — pre-filter must NOT skip anything
    result = runner.invoke(main, ["session", "a"])
    assert result.exit_code == 0

    # Non-matching b3333… was parsed (no pre-filter applied)
    parsed_dirs = {p.parent.name for p in parse_calls}
    assert "b3333333-cccc-cccc-cccc-cccccccccccc" in parsed_dirs

    # First match in discovery order (reverse-alpha: a2222222 before a1111111)
    assert "a2222222" in result.output


def test_session_command_three_char_prefix_skips_prefilter(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A 3-character prefix that matches nothing shows 'no session matching' + Available."""
    uuids = [
        "aa111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bb222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    for uid in uuids:
        _write_session(tmp_path, uid, use_full_uuid_dir=True)

    def _fake_discover(_base: Path | None = None) -> list[Path]:
        return sorted(
            tmp_path.glob("*/events.jsonl"),
            key=lambda p: p.parent.name,
        )

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _fake_discover)

    parse_calls: list[Path] = []
    original_parse = __import__(
        "copilot_usage.parser", fromlist=["parse_events"]
    ).parse_events

    def _tracking_parse(events_path: Path) -> list[Any]:
        parse_calls.append(events_path)
        return original_parse(events_path)

    monkeypatch.setattr("copilot_usage.cli.parse_events", _tracking_parse)

    runner = CliRunner()
    # "iii" is 3 chars and matches nothing — all sessions parsed, error shown
    result = runner.invoke(main, ["session", "iii"])
    assert result.exit_code == 1

    # Pre-filter was skipped: both UUID dirs were fully parsed
    assert len(parse_calls) == 2
    parsed_dirs = {p.parent.name for p in parse_calls}
    assert "aa111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in parsed_dirs
    assert "bb222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in parsed_dirs

    # Error + Available list
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
    # and also patch parser._DEFAULT_BASE so get_all_sessions(None)
    # doesn't discover sessions from the real home directory.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    missing_session_state = fake_home / ".copilot" / "session-state"
    # Intentionally do NOT create .copilot/session-state inside fake_home.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(parser_mod, "_DEFAULT_BASE", missing_session_state)

    # _start_observer should never be called when session_path.exists() is False.
    start_observer_calls: list[Path] = []

    def _tracking_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        start_observer_calls.append(session_path)
        raise AssertionError(
            "_start_observer should not be called when session_path does not exist"
        )

    monkeypatch.setattr(cli_mod, "_start_observer", _tracking_start)

    # Track _stop_observer calls to verify it's called with None in finally.
    stop_observer_args: list[object] = []

    def _tracking_stop(observer: object) -> None:
        stop_observer_args.append(observer)
        _stop_observer(observer)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]

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
    monkeypatch.setattr(parser_mod, "_DEFAULT_BASE", missing_session_state)

    # Track _start_observer to verify it is never called.
    start_observer_calls: list[Path] = []

    def _tracking_start(session_path: Path, change_event: threading.Event) -> object:  # noqa: ARG001
        start_observer_calls.append(session_path)
        raise AssertionError(
            "_start_observer should not be called when session_path does not exist"
        )

    monkeypatch.setattr(cli_mod, "_start_observer", _tracking_start)

    draw_home_calls: list[int] = []
    orig_draw = cli_mod._draw_home  # pyright: ignore[reportPrivateUsage]

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
    orig_header = cli_mod._print_version_header  # pyright: ignore[reportPrivateUsage]

    def _patched_header(target: Console | None = None) -> None:
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
    orig_header = cli_mod._print_version_header  # pyright: ignore[reportPrivateUsage]

    def _patched_header(target: Console | None = None) -> None:
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
    orig_header = cli_mod._print_version_header  # pyright: ignore[reportPrivateUsage]

    def _patched_header(target: Console | None = None) -> None:
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
    """Verify _normalize_until extends midnight to end-of-day."""

    def test_none_returns_none(self) -> None:
        assert _normalize_until(None) is None

    def test_midnight_becomes_end_of_day(self) -> None:
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=UTC)
        result = _normalize_until(midnight)
        assert result is not None
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59
        assert result.microsecond == 999999
        assert result.date() == midnight.date()

    def test_non_midnight_unchanged(self) -> None:
        dt = datetime(2026, 3, 7, 10, 30, 0, tzinfo=UTC)
        result = _normalize_until(dt)
        assert result == dt

    def test_naive_midnight_becomes_aware_end_of_day(self) -> None:
        naive = datetime(2026, 3, 7, 0, 0, 0)
        result = _normalize_until(naive)
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 23


class TestNormalizeUntilNonUtcTimezone:
    """_normalize_until preserves non-UTC timezone offsets when expanding midnight."""

    def test_aware_midnight_non_utc_expanded_in_same_tz(self) -> None:
        tz_plus5 = timezone(timedelta(hours=5))
        midnight = datetime(2026, 3, 7, 0, 0, 0, tzinfo=tz_plus5)
        result = _normalize_until(midnight)
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
        result = _normalize_until(dt)
        assert result == dt


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
# Issue #454 — reversed --since/--until emits click.UsageError
# ---------------------------------------------------------------------------


class TestReversedSinceUntilCliError:
    """CLI-level test: reversed --since/--until exits non-zero with a readable error."""

    def test_summary_reversed_range_exits_nonzero(self, tmp_path: Path) -> None:
        """summary --since 2026-12-31 --until 2026-01-01 exits with code 2."""
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
        """cost --since 2026-12-31 --until 2026-01-01 exits with code 2."""
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
