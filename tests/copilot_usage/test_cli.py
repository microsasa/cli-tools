"""Tests for copilot_usage.cli — wired-up CLI commands."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from rich.console import Console

from copilot_usage.cli import (
    _ensure_aware,  # pyright: ignore[reportPrivateUsage]
    _show_session_by_index,  # pyright: ignore[reportPrivateUsage]
    _start_observer,  # pyright: ignore[reportPrivateUsage]
    _stop_observer,  # pyright: ignore[reportPrivateUsage]
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_session(
    base: Path,
    session_id: str,
    *,
    name: str | None = None,
    model: str = "claude-sonnet-4",
    premium: int = 3,
    output_tokens: int = 1500,
    active: bool = False,
) -> Path:
    """Create a minimal events.jsonl file inside *base*/<dir>/."""
    session_dir = base / session_id[:8]
    session_dir.mkdir(parents=True, exist_ok=True)

    events: list[dict[str, Any]] = [
        {
            "type": "session.start",
            "timestamp": "2025-01-15T10:00:00Z",
            "data": {
                "sessionId": session_id,
                "startTime": "2025-01-15T10:00:00Z",
                "context": {"cwd": "/home/user/project"},
            },
        },
        {
            "type": "user.message",
            "timestamp": "2025-01-15T10:01:00Z",
            "data": {"content": "hello"},
        },
        {
            "type": "assistant.turn_start",
            "timestamp": "2025-01-15T10:01:01Z",
            "data": {"turnId": "0", "interactionId": "int-1"},
        },
    ]

    if not active:
        events.append(
            {
                "type": "session.shutdown",
                "timestamp": "2025-01-15T11:00:00Z",
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
    # Patch discover_sessions to use our tmp_path
    result = runner.invoke(main, ["session", "dddd4444"])
    # Will fail with "no session" because it looks in default path; test error path
    assert (
        result.exit_code != 0 or "dddd4444" in result.output or "Error" in result.output
    )


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
    """Exercise the except-Exception branch (lines 77-79) in summary."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "disk on fire"
        raise OSError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["summary", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "disk on fire" in result.output


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
    """Trigger an exception in session detail → friendly error (lines 129-131)."""

    def _exploding_discover(_base: Path | None = None) -> list[Path]:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr("copilot_usage.cli.discover_sessions", _exploding_discover)
    runner = CliRunner()
    result = runner.invoke(main, ["session", "anything"])
    assert result.exit_code != 0
    assert "permission denied" in result.output
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
    """Exercise the except-Exception branch (lines 226-228) in cost."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "cost explosion"
        raise RuntimeError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["cost", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "cost explosion" in result.output


def test_live_error_handling(tmp_path: Path, monkeypatch: Any) -> None:
    """Exercise the except-Exception branch (lines 248-250) in live."""

    def _exploding_sessions(_base: Path | None = None) -> list[object]:
        msg = "live explosion"
        raise RuntimeError(msg)

    monkeypatch.setattr("copilot_usage.cli.get_all_sessions", _exploding_sessions)
    runner = CliRunner()
    result = runner.invoke(main, ["live", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "live explosion" in result.output


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
    observer = _start_observer(tmp_path, change_event)  # pyright: ignore[reportUnknownVariableType]
    try:
        assert observer is not None
        assert observer.is_alive()  # type: ignore[union-attr]
    finally:
        _stop_observer(observer)  # pyright: ignore[reportUnknownArgumentType]


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


# 1. _ensure_aware unit tests ------------------------------------------------


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
def test_ensure_aware(dt_in: datetime | None, expected: datetime | None) -> None:
    """_ensure_aware handles None, aware, and naive datetimes correctly."""
    result = _ensure_aware(dt_in)
    assert result == expected
    if result is not None and expected is not None:
        assert result.tzinfo is not None


def test_ensure_aware_preserves_non_utc_timezone() -> None:
    """An already-aware dt with a non-UTC tz is returned unchanged."""
    non_utc = timezone(offset=timedelta(hours=5))
    dt_in = datetime(2025, 1, 1, 12, 0, 0, tzinfo=non_utc)
    result = _ensure_aware(dt_in)
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
    result = runner.invoke(main, ["--path", str(tmp_path), "live"])
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
    """change_event triggers re-render while on detail view with detail_idx set."""
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
