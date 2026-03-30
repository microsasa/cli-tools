"""Tests for copilot_usage.parser — session discovery, parsing, and summary."""

# pyright: reportPrivateUsage=false

import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from copilot_usage.models import (
    AssistantMessageData,
    EventType,
    GenericEventData,
    ModelMetrics,
    RequestMetrics,
    SessionContext,
    SessionEvent,
    SessionShutdownData,
    SessionStartData,
    SessionSummary,
    TokenUsage,
    ToolExecutionData,
    ToolTelemetry,
    UserMessageData,
)
from copilot_usage.parser import (
    _SESSION_CACHE,
    _build_active_summary,
    _CachedSession,
    _detect_resume,
    _extract_session_name,
    _first_pass,
    _infer_model_from_metrics,
    _read_config_model,
    _safe_file_identity,
    _safe_int_tokens,
    build_session_summary,
    discover_sessions,
    get_all_sessions,
    parse_events,
)


@pytest.fixture(autouse=True)
def _clear_session_cache() -> None:
    """Isolate tests from the module-level mtime cache."""
    _SESSION_CACHE.clear()


# ---------------------------------------------------------------------------
# Fixtures — synthetic events.jsonl content
# ---------------------------------------------------------------------------

_START_EVENT = json.dumps(
    {
        "type": "session.start",
        "data": {
            "sessionId": "test-session-001",
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

_USER_MSG = json.dumps(
    {
        "type": "user.message",
        "data": {
            "content": "hello",
            "transformedContent": "hello",
            "attachments": [],
            "interactionId": "int-1",
        },
        "id": "ev-user1",
        "timestamp": "2026-03-07T10:01:00.000Z",
        "parentId": "ev-start",
    }
)

_ASSISTANT_MSG = json.dumps(
    {
        "type": "assistant.message",
        "data": {
            "messageId": "msg-1",
            "content": "hi there",
            "toolRequests": [],
            "interactionId": "int-1",
            "outputTokens": 150,
        },
        "id": "ev-asst1",
        "timestamp": "2026-03-07T10:01:05.000Z",
        "parentId": "ev-user1",
    }
)

_ASSISTANT_MSG_2 = json.dumps(
    {
        "type": "assistant.message",
        "data": {
            "messageId": "msg-2",
            "content": "more content",
            "toolRequests": [],
            "interactionId": "int-1",
            "outputTokens": 200,
        },
        "id": "ev-asst2",
        "timestamp": "2026-03-07T10:01:10.000Z",
        "parentId": "ev-asst1",
    }
)

_TOOL_EXEC = json.dumps(
    {
        "type": "tool.execution_complete",
        "data": {
            "toolCallId": "tc-1",
            "model": "claude-sonnet-4",
            "interactionId": "int-1",
            "success": True,
        },
        "id": "ev-tool1",
        "timestamp": "2026-03-07T10:01:07.000Z",
        "parentId": "ev-asst1",
    }
)

_TURN_START_1 = json.dumps(
    {
        "type": "assistant.turn_start",
        "data": {"turnId": "0", "interactionId": "int-1"},
        "id": "ev-turn-start-1",
        "timestamp": "2026-03-07T10:01:01.000Z",
        "parentId": "ev-user1",
    }
)

_TURN_START_2 = json.dumps(
    {
        "type": "assistant.turn_start",
        "data": {"turnId": "1", "interactionId": "int-1"},
        "id": "ev-turn-start-2",
        "timestamp": "2026-03-07T10:01:08.000Z",
        "parentId": "ev-asst1",
    }
)

_SHUTDOWN_EVENT = json.dumps(
    {
        "type": "session.shutdown",
        "data": {
            "shutdownType": "routine",
            "totalPremiumRequests": 5,
            "totalApiDurationMs": 12000,
            "sessionStartTime": 1772895600000,
            "codeChanges": {
                "linesAdded": 50,
                "linesRemoved": 10,
                "filesModified": ["a.py", "b.py"],
            },
            "modelMetrics": {
                "claude-sonnet-4": {
                    "requests": {"count": 8, "cost": 5},
                    "usage": {
                        "inputTokens": 5000,
                        "outputTokens": 350,
                        "cacheReadTokens": 1000,
                        "cacheWriteTokens": 0,
                    },
                }
            },
            "currentModel": "claude-sonnet-4",
        },
        "id": "ev-shutdown",
        "timestamp": "2026-03-07T11:00:00.000Z",
        "parentId": "ev-asst2",
        "currentModel": "claude-sonnet-4",
    }
)

_RESUME_EVENT = json.dumps(
    {
        "type": "session.resume",
        "data": {},
        "id": "ev-resume",
        "timestamp": "2026-03-07T12:00:00.000Z",
        "parentId": "ev-shutdown",
    }
)

_POST_RESUME_USER_MSG = json.dumps(
    {
        "type": "user.message",
        "data": {
            "content": "continue working",
            "transformedContent": "continue working",
            "attachments": [],
            "interactionId": "int-2",
        },
        "id": "ev-user2",
        "timestamp": "2026-03-07T12:01:00.000Z",
        "parentId": "ev-resume",
    }
)

_POST_RESUME_ASSISTANT_MSG = json.dumps(
    {
        "type": "assistant.message",
        "data": {
            "messageId": "msg-3",
            "content": "resuming work",
            "toolRequests": [],
            "interactionId": "int-2",
            "outputTokens": 250,
        },
        "id": "ev-asst3",
        "timestamp": "2026-03-07T12:01:05.000Z",
        "parentId": "ev-user2",
    }
)

_POST_RESUME_TURN_START = json.dumps(
    {
        "type": "assistant.turn_start",
        "data": {"turnId": "2", "interactionId": "int-2"},
        "id": "ev-turn-start-post-resume",
        "timestamp": "2026-03-07T12:01:01.000Z",
        "parentId": "ev-user2",
    }
)

_SHUTDOWN_EVENT_2 = json.dumps(
    {
        "type": "session.shutdown",
        "data": {
            "shutdownType": "routine",
            "totalPremiumRequests": 10,
            "totalApiDurationMs": 20000,
            "sessionStartTime": 1772895600000,
            "codeChanges": {
                "linesAdded": 80,
                "linesRemoved": 20,
                "filesModified": ["a.py", "b.py", "c.py"],
            },
            "modelMetrics": {
                "claude-sonnet-4": {
                    "requests": {"count": 15, "cost": 10},
                    "usage": {
                        "inputTokens": 9000,
                        "outputTokens": 700,
                        "cacheReadTokens": 2000,
                        "cacheWriteTokens": 0,
                    },
                }
            },
            "currentModel": "claude-sonnet-4",
        },
        "id": "ev-shutdown-2",
        "timestamp": "2026-03-07T13:00:00.000Z",
        "parentId": "ev-asst3",
        "currentModel": "claude-sonnet-4",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_events(path: Path, *lines: str) -> Path:
    """Write event lines to an events.jsonl file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _completed_events(
    tmp_path: Path,
) -> tuple[list[SessionEvent], Path]:
    p = tmp_path / "s" / "events.jsonl"
    _write_events(
        p,
        _START_EVENT,
        _USER_MSG,
        _ASSISTANT_MSG,
        _ASSISTANT_MSG_2,
        _SHUTDOWN_EVENT,
    )
    return parse_events(p), p.parent


def _active_events(
    tmp_path: Path,
) -> tuple[list[SessionEvent], Path]:
    p = tmp_path / "s" / "events.jsonl"
    _write_events(
        p,
        _START_EVENT,
        _USER_MSG,
        _ASSISTANT_MSG,
        _ASSISTANT_MSG_2,
        _TOOL_EXEC,
    )
    return parse_events(p), p.parent


# ---------------------------------------------------------------------------
# _safe_file_identity
# ---------------------------------------------------------------------------


class TestSafeFileIdentity:
    def test_returns_mtime_ns_size_for_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("content")
        mtime_ns, size = _safe_file_identity(f)
        assert mtime_ns > 0
        assert size == len(b"content")

    def test_returns_zero_tuple_for_missing_file(self, tmp_path: Path) -> None:
        assert _safe_file_identity(tmp_path / "ghost.jsonl") == (0, 0)

    def test_returns_zero_tuple_for_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("")

        def _raise_perm(self: Path, **kwargs: object) -> object:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "stat", _raise_perm)
        assert _safe_file_identity(f) == (0, 0)

    def test_returns_zero_tuple_for_generic_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("")

        def _raise_os(self: Path, **kwargs: object) -> object:
            raise OSError("I/O error")

        monkeypatch.setattr(Path, "stat", _raise_os)
        assert _safe_file_identity(f) == (0, 0)


# ---------------------------------------------------------------------------
# discover_sessions
# ---------------------------------------------------------------------------


class TestDiscoverSessions:
    def test_finds_sessions(self, tmp_path: Path) -> None:
        s1 = tmp_path / "session-a" / "events.jsonl"
        s2 = tmp_path / "session-b" / "events.jsonl"
        _write_events(s1, _START_EVENT)
        _write_events(s2, _START_EVENT)
        result = discover_sessions(tmp_path)
        assert len(result) == 2
        assert all(p.name == "events.jsonl" for p in result)

    def test_sorted_newest_first(self, tmp_path: Path) -> None:
        older = tmp_path / "old" / "events.jsonl"
        newer = tmp_path / "new" / "events.jsonl"
        _write_events(older, _START_EVENT)
        time.sleep(0.05)
        _write_events(newer, _START_EVENT)
        result = discover_sessions(tmp_path)
        assert result[0] == newer

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert discover_sessions(tmp_path) == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        assert discover_sessions(tmp_path / "nope") == []

    def test_regular_file_returns_empty(self, tmp_path: Path) -> None:
        """Passing an existing file (not a directory) → returns []."""
        some_file = tmp_path / "some_file.txt"
        some_file.write_text("not a directory", encoding="utf-8")
        assert discover_sessions(some_file) == []

    def test_stat_race_file_deleted_between_glob_and_sort(self, tmp_path: Path) -> None:
        """TOCTOU: session dir deleted after glob but before stat()."""
        s1 = tmp_path / "session-a" / "events.jsonl"
        s2 = tmp_path / "session-b" / "events.jsonl"
        _write_events(s1, _START_EVENT)
        _write_events(s2, _START_EVENT)

        original_stat = Path.stat

        def _flaky_stat(self: Path) -> object:
            if self == s1:
                raise FileNotFoundError(f"deleted: {self}")
            return original_stat(self)

        with patch.object(Path, "stat", _flaky_stat):
            result = discover_sessions(tmp_path)

        # s2 still returned; s1 may also be present (with mtime 0)
        assert any(p == s2 for p in result)
        # The call must not raise
        assert isinstance(result, list)

    def test_stat_race_permission_error(self, tmp_path: Path) -> None:
        """discover_sessions should not crash when stat() raises PermissionError."""
        s1 = tmp_path / "sess-a" / "events.jsonl"
        _write_events(s1, _START_EVENT)

        original_stat = Path.stat

        def _flaky_stat(self: Path, **kwargs: object) -> object:
            if self.name == "events.jsonl":
                raise PermissionError("denied")
            return original_stat(self)

        with patch.object(Path, "stat", _flaky_stat):
            result = discover_sessions(tmp_path)

        # Should return the path (with mtime=0), not crash
        assert result == [s1]

    def test_get_all_sessions_skips_vanished_session(self, tmp_path: Path) -> None:
        """TOCTOU: events.jsonl deleted after discover but before parse."""
        s1 = tmp_path / "session-a" / "events.jsonl"
        s2 = tmp_path / "session-b" / "events.jsonl"
        _write_events(s1, _START_EVENT)
        _write_events(s2, _START_EVENT)

        original_open = Path.open

        def _flaky_open(self: Path, *args: object, **kwargs: object) -> object:  # type: ignore[override]
            if self == s1:
                raise FileNotFoundError(f"deleted: {self}")
            return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "open", _flaky_open):
            summaries = get_all_sessions(tmp_path)

        # Only s2 should produce a summary
        assert len(summaries) == 1


# ---------------------------------------------------------------------------
# parse_events
# ---------------------------------------------------------------------------


class TestParseEvents:
    def test_parses_valid_events(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        assert len(events) == 3
        assert events[0].type == "session.start"
        assert events[1].type == "user.message"
        assert events[2].type == "assistant.message"

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, "NOT-JSON{{{", _USER_MSG)
        events = parse_events(p)
        assert len(events) == 2

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(_START_EVENT + "\n\n\n" + _USER_MSG + "\n", encoding="utf-8")
        events = parse_events(p)
        assert len(events) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p)
        events = parse_events(p)
        assert events == []

    def test_skips_validation_errors(self, tmp_path: Path) -> None:
        bad_event = json.dumps({"no_type_field": True})
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, bad_event)
        events = parse_events(p)
        assert len(events) == 1

    def test_unicode_decode_error_returns_partial(self, tmp_path: Path) -> None:
        """events.jsonl with invalid UTF-8 bytes returns what was parsed so far.

        Due to buffered I/O, the UnicodeDecodeError may fire before any
        lines are yielded, so the result is typically an empty list.
        """
        p = tmp_path / "s" / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write a valid first line, then raw invalid UTF-8 bytes
        valid_line = _START_EVENT.encode("utf-8") + b"\n"
        bad_line = b"\xff\xfe invalid utf-8 bytes\n"
        p.write_bytes(valid_line + bad_line)
        events = parse_events(p)
        # Should return what was parsed before the error and not raise.
        assert isinstance(events, list)
        # 0 or 1 events may be returned depending on buffered I/O behavior.
        assert len(events) <= 1
        if events:
            # If any event is returned, it should be the initial session.start.
            assert len(events) == 1
            assert events[0].type == "session.start"

    def test_unicode_decode_error_full_file(self, tmp_path: Path) -> None:
        """events.jsonl that is entirely invalid UTF-8 returns empty list."""
        p = tmp_path / "s" / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xfe\x80\x81\x82")
        events = parse_events(p)
        assert events == []

    def test_unicode_decode_error_returns_partial_results(self, tmp_path: Path) -> None:
        """Valid events before an invalid UTF-8 sequence are returned.

        Python's TextIOWrapper reads in buffer-sized chunks, so the valid
        content must exceed one buffer to guarantee the first lines are
        yielded before the decode error fires on the next chunk.
        """
        p = tmp_path / "s" / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        valid_line = (
            b'{"type":"session.start","timestamp":"2026-01-01T00:00:00Z","data":{}}\n'
        )
        # First block: repeat valid lines enough to exceed the default read buffer.
        first_repeat = (io.DEFAULT_BUFFER_SIZE // len(valid_line)) + 2
        first_block = valid_line * first_repeat
        # Second block: additional valid lines that should never be returned.
        second_repeat = 5
        second_block = valid_line * second_repeat
        total_valid_lines = first_repeat + second_repeat
        # Insert invalid UTF-8 bytes between the two valid blocks so the decode
        # error occurs in the middle of the file, after some events were yielded.
        invalid_bytes = b"\xff\xfe"
        p.write_bytes(first_block + invalid_bytes + second_block)
        result = parse_events(p)
        # Partial parse: at least the first event must survive.
        assert isinstance(result, list)
        assert len(result) >= 1
        assert result[0].type == EventType.SESSION_START
        # Not everything was returned (error cut parsing short in the middle).
        assert len(result) < total_valid_lines
        # All returned events should be from the first valid block.
        assert len(result) <= first_repeat


# ---------------------------------------------------------------------------
# build_session_summary — completed session
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryCompleted:
    def test_session_id(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.session_id == "test-session-001"

    def test_not_active(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.is_active is False

    def test_uses_shutdown_data(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.total_premium_requests == 5
        assert summary.total_api_duration_ms == 12000
        assert "claude-sonnet-4" in summary.model_metrics
        assert summary.model_metrics["claude-sonnet-4"].usage.outputTokens == 350

    def test_code_changes(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.code_changes is not None
        assert summary.code_changes.linesAdded == 50

    def test_message_count(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.user_messages == 1  # 1 user message
        assert summary.model_calls == 0  # no turn_starts in _completed_events

    def test_model(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.model == "claude-sonnet-4"

    def test_timestamps(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.start_time == datetime(2026, 3, 7, 10, 0, tzinfo=UTC)
        assert summary.end_time == datetime(2026, 3, 7, 11, 0, tzinfo=UTC)

    def test_session_name_from_plan(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        plan = sdir / "plan.md"
        plan.write_text("# My Cool Project\n\nSome details.\n", encoding="utf-8")
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.name == "My Cool Project"

    def test_events_path_set_when_passed(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        ep = sdir / "events.jsonl"
        summary = build_session_summary(events, session_dir=sdir, events_path=ep)
        assert summary.events_path == ep

    def test_events_path_none_when_omitted(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.events_path is None


# ---------------------------------------------------------------------------
# build_session_summary — active session (no shutdown)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryActive:
    def test_is_active(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.is_active is True

    def test_sums_output_tokens(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        # 150 + 200 = 350 total output tokens
        assert summary.model is not None
        total = sum(m.usage.outputTokens for m in summary.model_metrics.values())
        assert total == 350

    def test_zero_premium_requests(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.total_premium_requests == 0

    def test_no_code_changes(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.code_changes is None

    def test_model_from_tool_exec(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.model == "claude-sonnet-4"

    def test_cwd(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.cwd == "/home/user/project"

    def test_last_resume_time_none_for_active(self, tmp_path: Path) -> None:
        """Active session with no resume event → last_resume_time is None."""
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.last_resume_time is None

    def test_active_fields_populated(self, tmp_path: Path) -> None:
        """Pure active session populates active_model_calls/user_messages/output_tokens."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _TURN_START_1,
            _ASSISTANT_MSG,
            _USER_MSG,  # second user message (same fixture reused)
            _TURN_START_2,
            _ASSISTANT_MSG_2,
            _TOOL_EXEC,
        )
        events = parse_events(p)
        summary = build_session_summary(events, session_dir=p.parent)
        assert summary.is_active is True
        assert summary.active_model_calls == 2
        assert summary.active_user_messages == 2
        assert summary.active_output_tokens == 350  # 150 + 200

    def test_active_session_model_from_tool_events(self, tmp_path: Path) -> None:
        """Active session with tool.execution_complete → model_metrics reflect active tokens."""
        p = tmp_path / "s" / "events.jsonl"
        # One assistant message with 150 output tokens and a tool.execution_complete
        # that provides the model name. This should result in active_output_tokens
        # being accounted for in model_metrics for the inferred model.
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events, session_dir=p.parent)

        # Session should be considered active.
        assert summary.is_active is True

        # model_metrics should exist and contain an entry for the inferred model.
        assert summary.model_metrics is not None
        assert "claude-sonnet-4" in summary.model_metrics
        sonnet_metrics = summary.model_metrics["claude-sonnet-4"]

        # The usage for the inferred model should reflect the active session's
        # output tokens (150 from the single assistant message).
        assert sonnet_metrics.usage is not None
        assert sonnet_metrics.usage.outputTokens == summary.active_output_tokens == 150

    def test_events_path_set_when_passed(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        ep = sdir / "events.jsonl"
        summary = build_session_summary(events, session_dir=sdir, events_path=ep)
        assert summary.events_path == ep

    def test_events_path_none_when_omitted(self, tmp_path: Path) -> None:
        events, sdir = _active_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.events_path is None


# ---------------------------------------------------------------------------
# build_session_summary — resumed session (shutdown followed by more events)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryResumed:
    def test_resumed_session_is_active(self, tmp_path: Path) -> None:
        """Session with shutdown followed by new messages → is_active=True."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            _RESUME_EVENT,
            _POST_RESUME_USER_MSG,
            _POST_RESUME_ASSISTANT_MSG,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True

    def test_resumed_session_sums_post_shutdown_tokens(self, tmp_path: Path) -> None:
        """Post-shutdown tokens go to active_output_tokens, not merged into metrics."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            _RESUME_EVENT,
            _POST_RESUME_USER_MSG,
            _POST_RESUME_ASSISTANT_MSG,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        # Shutdown had 350 — stays at 350 in historical metrics
        assert "claude-sonnet-4" in summary.model_metrics
        assert summary.model_metrics["claude-sonnet-4"].usage.outputTokens == 350
        # Post-resume 250 goes to active_output_tokens
        assert summary.active_output_tokens == 250

    def test_multiple_shutdowns_uses_latest(self, tmp_path: Path) -> None:
        """Session shut down and resumed multiple times → last shutdown wins."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            _RESUME_EVENT,
            _POST_RESUME_USER_MSG,
            _POST_RESUME_ASSISTANT_MSG,
            _SHUTDOWN_EVENT_2,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        # Two shutdowns: 5 + 10 = 15 total premium requests
        assert summary.is_active is False
        assert summary.total_premium_requests == 15
        # Output tokens summed: 350 + 700 = 1050
        assert summary.model_metrics["claude-sonnet-4"].usage.outputTokens == 1050

    def test_shutdown_as_last_event_is_completed(self, tmp_path: Path) -> None:
        """Normal completed session (shutdown is last) → is_active=False."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _ASSISTANT_MSG_2,
            _SHUTDOWN_EVENT,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is False
        assert summary.end_time == datetime(2026, 3, 7, 11, 0, tzinfo=UTC)

    def test_last_resume_time_none_for_completed(self, tmp_path: Path) -> None:
        """Completed session (no resume) → last_resume_time is None."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.last_resume_time is None

    def test_last_resume_time_set_for_resumed(self, tmp_path: Path) -> None:
        """Resumed session → last_resume_time equals resume event timestamp."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            _RESUME_EVENT,
            _POST_RESUME_USER_MSG,
            _POST_RESUME_ASSISTANT_MSG,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.last_resume_time == datetime(2026, 3, 7, 12, 0, tzinfo=UTC)

    def test_resume_without_timestamp(self, tmp_path: Path) -> None:
        """session.resume with no timestamp → is_active but last_resume_time is None."""
        resume_no_ts = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume-no-ts",
                "timestamp": None,
                "parentId": "ev-shutdown",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            resume_no_ts,
            _POST_RESUME_USER_MSG,
            _POST_RESUME_ASSISTANT_MSG,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        assert summary.last_resume_time is None

    def test_resume_with_missing_timestamp_field_is_active_but_last_resume_time_is_none(
        self, tmp_path: Path
    ) -> None:
        """session.resume with the timestamp key omitted entirely marks session active
        but leaves last_resume_time=None so display falls back to start_time."""
        resume_no_ts = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume",
            }
        )
        post_user = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "back", "attachments": []},
                "id": "ev-u2",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT, resume_no_ts, post_user
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        assert summary.is_active is True
        assert summary.last_resume_time is None
        assert summary.active_user_messages == 1

    def test_resumed_session_no_current_model_infers_from_metrics(
        self, tmp_path: Path
    ) -> None:
        """Shutdown with modelMetrics but no currentModel → model inferred, tokens kept."""
        shutdown_no_model = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 3,
                    "totalApiDurationMs": 5000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 3, "cost": 3},
                            "usage": {
                                "inputTokens": 2000,
                                "outputTokens": 400,
                                "cacheReadTokens": 0,
                                "cacheWriteTokens": 0,
                            },
                        }
                    },
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        resume_ev = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume",
                "timestamp": "2026-03-07T12:00:00.000Z",
            }
        )
        post_user = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "hi", "attachments": []},
                "id": "ev-u",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        post_asst = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-p",
                    "content": "ok",
                    "toolRequests": [],
                    "interactionId": "int-r",
                    "outputTokens": 100,
                },
                "id": "ev-a",
                "timestamp": "2026-03-07T12:01:05.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            shutdown_no_model,
            resume_ev,
            post_user,
            post_asst,
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        assert summary.is_active is True
        assert summary.model == "claude-sonnet-4"
        assert "claude-sonnet-4" in summary.model_metrics
        # 400 from shutdown stays at 400 in historical
        assert summary.model_metrics["claude-sonnet-4"].usage.outputTokens == 400
        # 100 post-resume goes to active
        assert summary.active_output_tokens == 100

    def test_user_message_alone_marks_resumed(self, tmp_path: Path) -> None:
        """Shutdown → USER_MESSAGE (no session.resume) → is_active=True."""
        post_user = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "continue", "attachments": []},
                "id": "ev-u-noresume",
                "timestamp": "2026-03-07T12:01:00.000Z",
                "parentId": "ev-shutdown",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            post_user,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        assert summary.last_resume_time is None
        assert summary.active_user_messages == 1
        assert summary.active_output_tokens == 0

    def test_assistant_message_alone_marks_resumed(self, tmp_path: Path) -> None:
        """Shutdown → ASSISTANT_MESSAGE (no session.resume) → is_active=True."""
        post_asst = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-noresume",
                    "content": "continuing",
                    "toolRequests": [],
                    "interactionId": "int-nr",
                    "outputTokens": 120,
                },
                "id": "ev-a-noresume",
                "timestamp": "2026-03-07T12:01:05.000Z",
                "parentId": "ev-shutdown",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            post_asst,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        assert summary.last_resume_time is None
        assert summary.active_output_tokens == 120
        assert summary.active_user_messages == 0

    def test_model_inferred_from_highest_request_count(self, tmp_path: Path) -> None:
        """Shutdown with multiple models, no currentModel → picks highest requests.count."""
        shutdown_multi = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 12,
                    "totalApiDurationMs": 8000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "gpt-4": {
                            "requests": {"count": 2, "cost": 2},
                            "usage": {
                                "inputTokens": 500,
                                "outputTokens": 100,
                                "cacheReadTokens": 0,
                                "cacheWriteTokens": 0,
                            },
                        },
                        "claude-sonnet-4": {
                            "requests": {"count": 10, "cost": 10},
                            "usage": {
                                "inputTokens": 4000,
                                "outputTokens": 800,
                                "cacheReadTokens": 0,
                                "cacheWriteTokens": 0,
                            },
                        },
                    },
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        resume_ev = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume",
                "timestamp": "2026-03-07T12:00:00.000Z",
            }
        )
        post_asst = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-p",
                    "content": "ok",
                    "toolRequests": [],
                    "interactionId": "int-r",
                    "outputTokens": 50,
                },
                "id": "ev-a",
                "timestamp": "2026-03-07T12:01:05.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, shutdown_multi, resume_ev, post_asst)
        events = parse_events(p)
        summary = build_session_summary(events)

        # claude-sonnet-4 has count=10 > gpt-4 count=2
        assert summary.model == "claude-sonnet-4"
        # Historical stays at 800 (from shutdown)
        assert summary.model_metrics["claude-sonnet-4"].usage.outputTokens == 800
        # 50 post-resume goes to active
        assert summary.active_output_tokens == 50


# ---------------------------------------------------------------------------
# build_session_summary — implicit resume (post-shutdown activity, no session.resume)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryImplicitResume:
    """Post-shutdown user/assistant messages without explicit session.resume event."""

    def test_implicit_resume_premium_requests_still_aggregated(
        self, tmp_path: Path
    ) -> None:
        """Shutdown-cycle premium requests are included even with implicit resume."""
        post_user = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "more work", "attachments": []},
                "id": "ev-u-prem",
                "timestamp": "2026-03-07T12:01:00.000Z",
                "parentId": "ev-shutdown",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            post_user,
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        # _SHUTDOWN_EVENT has totalPremiumRequests=5
        assert summary.total_premium_requests == 5
        assert summary.is_active is True
        assert summary.last_resume_time is None


# ---------------------------------------------------------------------------
# build_session_summary — multi-shutdown resumed (2+ shutdowns then still active)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryMultiShutdownResumed:
    """Two shutdowns followed by a resume with post-resume activity (still active)."""

    @staticmethod
    def _build(tmp_path: Path) -> SessionSummary:
        shutdown_1 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 3,
                    "totalApiDurationMs": 4000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 3, "cost": 3},
                            "usage": {
                                "inputTokens": 800,
                                "outputTokens": 150,
                                "cacheReadTokens": 200,
                                "cacheWriteTokens": 0,
                            },
                        }
                    },
                    "currentModel": "claude-sonnet-4",
                },
                "id": "ev-sd1",
                "timestamp": "2026-03-07T09:00:00.000Z",
                "currentModel": "claude-sonnet-4",
            }
        )
        resume_1 = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume1",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        mid_user = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "mid", "attachments": []},
                "id": "ev-mid-u",
                "timestamp": "2026-03-07T11:01:00.000Z",
            }
        )
        mid_turn_start = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "1", "interactionId": "int-mid"},
                "id": "ev-mid-ts",
                "timestamp": "2026-03-07T11:01:01.000Z",
            }
        )
        mid_asst = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-mid",
                    "content": "ok",
                    "toolRequests": [],
                    "interactionId": "int-mid",
                    "outputTokens": 250,
                },
                "id": "ev-mid-a",
                "timestamp": "2026-03-07T11:01:05.000Z",
            }
        )
        shutdown_2 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 7,
                    "totalApiDurationMs": 9000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-opus-4.6": {
                            "requests": {"count": 5, "cost": 7},
                            "usage": {
                                "inputTokens": 1500,
                                "outputTokens": 350,
                                "cacheReadTokens": 400,
                                "cacheWriteTokens": 0,
                            },
                        }
                    },
                    "currentModel": "claude-opus-4.6",
                },
                "id": "ev-sd2",
                "timestamp": "2026-03-07T12:00:00.000Z",
                "currentModel": "claude-opus-4.6",
            }
        )
        resume_2 = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume2",
                "timestamp": "2026-03-07T14:00:00.000Z",
            }
        )
        post_user_1 = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "post1", "attachments": []},
                "id": "ev-post-u1",
                "timestamp": "2026-03-07T14:01:00.000Z",
            }
        )
        post_turn_start_1 = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "2", "interactionId": "int-post1"},
                "id": "ev-post-ts1",
                "timestamp": "2026-03-07T14:01:01.000Z",
            }
        )
        post_asst_1 = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-p1",
                    "content": "reply1",
                    "toolRequests": [],
                    "interactionId": "int-post1",
                    "outputTokens": 100,
                },
                "id": "ev-post-a1",
                "timestamp": "2026-03-07T14:01:05.000Z",
            }
        )
        post_user_2 = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "post2", "attachments": []},
                "id": "ev-post-u2",
                "timestamp": "2026-03-07T14:05:00.000Z",
            }
        )
        post_turn_start_2 = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "3", "interactionId": "int-post2"},
                "id": "ev-post-ts2",
                "timestamp": "2026-03-07T14:05:01.000Z",
            }
        )
        post_asst_2 = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-p2",
                    "content": "reply2",
                    "toolRequests": [],
                    "interactionId": "int-post2",
                    "outputTokens": 125,
                },
                "id": "ev-post-a2",
                "timestamp": "2026-03-07T14:05:05.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _TURN_START_1,
            _ASSISTANT_MSG,
            shutdown_1,
            resume_1,
            mid_user,
            mid_turn_start,
            mid_asst,
            shutdown_2,
            resume_2,
            post_user_1,
            post_turn_start_1,
            post_asst_1,
            post_user_2,
            post_turn_start_2,
            post_asst_2,
        )
        events = parse_events(p)
        return build_session_summary(events)

    def test_is_active(self, tmp_path: Path) -> None:
        """2 shutdowns + resume with post-resume activity → is_active=True."""
        summary = self._build(tmp_path)
        assert summary.is_active is True

    def test_total_premium_requests_summed(self, tmp_path: Path) -> None:
        """Premium requests aggregated from both shutdowns: 3 + 7 = 10."""
        summary = self._build(tmp_path)
        assert summary.total_premium_requests == 10

    def test_merged_model_metrics_has_both_models(self, tmp_path: Path) -> None:
        """Merged metrics contain keys for both sonnet and opus."""
        summary = self._build(tmp_path)
        assert "claude-sonnet-4" in summary.model_metrics
        assert "claude-opus-4.6" in summary.model_metrics

    def test_merged_model_metrics_values(self, tmp_path: Path) -> None:
        """Each model has correct metrics from its respective shutdown."""
        summary = self._build(tmp_path)
        sonnet = summary.model_metrics["claude-sonnet-4"]
        assert sonnet.requests.count == 3
        assert sonnet.usage.inputTokens == 800
        assert sonnet.usage.outputTokens == 150

        opus = summary.model_metrics["claude-opus-4.6"]
        assert opus.requests.count == 5
        assert opus.usage.inputTokens == 1500
        assert opus.usage.outputTokens == 350

    def test_active_turn_starts_only_after_last_shutdown(self, tmp_path: Path) -> None:
        """active_model_calls counts only turn_starts after last shutdown."""
        summary = self._build(tmp_path)
        assert summary.active_model_calls == 2

    def test_active_user_messages_only_after_last_shutdown(
        self, tmp_path: Path
    ) -> None:
        """active_user_messages counts only user.messages after last shutdown."""
        summary = self._build(tmp_path)
        assert summary.active_user_messages == 2

    def test_active_output_tokens_only_after_last_shutdown(
        self, tmp_path: Path
    ) -> None:
        """active_output_tokens sums only outputTokens after last shutdown: 100+125."""
        summary = self._build(tmp_path)
        assert summary.active_output_tokens == 225

    def test_last_resume_time_is_latest_resume(self, tmp_path: Path) -> None:
        """last_resume_time is set to the timestamp of the last session.resume."""
        summary = self._build(tmp_path)
        # resume_2 timestamp is 2026-03-07T14:00:00.000Z
        assert summary.last_resume_time == datetime(2026, 3, 7, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# build_session_summary — multi-shutdown code_changes preservation
# ---------------------------------------------------------------------------


class TestMultiShutdownCodeChangesPreservation:
    """Verify the ``if sd.codeChanges is not None`` guard in build_session_summary
    preserves earlier code-change data when a later shutdown omits it."""

    def test_first_shutdown_code_changes_preserved_when_last_has_none(
        self, tmp_path: Path
    ) -> None:
        """When shutdown_1 has codeChanges and shutdown_2 does not, summary.code_changes
        must equal shutdown_1's data (not None). Verifies the `if sd.codeChanges is not None`
        guard in build_session_summary."""
        shutdown_with_cc = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 3,
                    "totalApiDurationMs": 2000,
                    "sessionStartTime": 0,
                    "codeChanges": {
                        "linesAdded": 42,
                        "linesRemoved": 7,
                        "filesModified": ["main.py"],
                    },
                    "modelMetrics": {},
                },
                "id": "ev-sd1",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        resume_ev = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-r",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        shutdown_no_cc = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 2,
                    "totalApiDurationMs": 1000,
                    "sessionStartTime": 0,
                    # codeChanges intentionally absent
                    "modelMetrics": {},
                },
                "id": "ev-sd2",
                "timestamp": "2026-03-07T12:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            shutdown_with_cc,
            resume_ev,
            _USER_MSG,
            shutdown_no_cc,
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        assert summary.is_active is False
        assert summary.code_changes is not None, (
            "code_changes from first shutdown must not be reset to None"
        )
        assert summary.code_changes.linesAdded == 42
        assert summary.code_changes.linesRemoved == 7
        assert summary.code_changes.filesModified == ["main.py"]
        assert summary.total_premium_requests == 5  # sum of both shutdowns

    def test_last_shutdown_code_changes_wins_when_set(self, tmp_path: Path) -> None:
        """When shutdown_1 has no codeChanges but shutdown_2 does, summary.code_changes
        must equal shutdown_2's data."""
        shutdown_no_cc = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 1,
                    "totalApiDurationMs": 500,
                    "sessionStartTime": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd1",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        resume_ev = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-r",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        shutdown_with_cc = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 4,
                    "totalApiDurationMs": 3000,
                    "sessionStartTime": 0,
                    "codeChanges": {
                        "linesAdded": 20,
                        "linesRemoved": 3,
                        "filesModified": ["b.py"],
                    },
                    "modelMetrics": {},
                },
                "id": "ev-sd2",
                "timestamp": "2026-03-07T12:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            shutdown_no_cc,
            resume_ev,
            _USER_MSG,
            shutdown_with_cc,
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        assert summary.is_active is False
        assert summary.code_changes is not None
        assert summary.code_changes.linesAdded == 20
        assert summary.code_changes.filesModified == ["b.py"]


# ---------------------------------------------------------------------------
# get_all_sessions
# ---------------------------------------------------------------------------


class TestGetAllSessions:
    def test_returns_summaries(self, tmp_path: Path) -> None:
        s1 = tmp_path / "a" / "events.jsonl"
        s2 = tmp_path / "b" / "events.jsonl"
        _write_events(s1, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        _write_events(s2, _START_EVENT, _SHUTDOWN_EVENT)
        result = get_all_sessions(tmp_path)
        assert len(result) == 2

    def test_sorted_newest_first(self, tmp_path: Path) -> None:
        older_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "old",
                    "version": 1,
                    "startTime": "2026-01-01T00:00:00.000Z",
                    "context": {},
                },
                "id": "e1",
                "timestamp": "2026-01-01T00:00:00.000Z",
            }
        )
        newer_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "new",
                    "version": 1,
                    "startTime": "2026-06-01T00:00:00.000Z",
                    "context": {},
                },
                "id": "e2",
                "timestamp": "2026-06-01T00:00:00.000Z",
            }
        )
        _write_events(tmp_path / "a" / "events.jsonl", older_start)
        _write_events(tmp_path / "b" / "events.jsonl", newer_start)
        result = get_all_sessions(tmp_path)
        assert result[0].session_id == "new"
        assert result[1].session_id == "old"

    def test_empty_base(self, tmp_path: Path) -> None:
        assert get_all_sessions(tmp_path) == []

    def test_events_path_set_for_all_sessions(self, tmp_path: Path) -> None:
        """Regression: get_all_sessions returns summaries with non-None events_path."""
        s1 = tmp_path / "a" / "events.jsonl"
        s2 = tmp_path / "b" / "events.jsonl"
        _write_events(s1, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        _write_events(s2, _START_EVENT, _SHUTDOWN_EVENT)
        result = get_all_sessions(tmp_path)
        assert len(result) == 2
        returned_paths = {s.events_path for s in result}
        assert returned_paths == {s1, s2}


# ---------------------------------------------------------------------------
# Real data smoke test (against ~/.copilot/session-state/)
# ---------------------------------------------------------------------------

_REAL_BASE = Path.home() / ".copilot" / "session-state"


class TestRealData:
    """Smoke tests against actual session data — skipped if not present."""

    @pytest.mark.skipif(
        not _REAL_BASE.is_dir(),
        reason="No real session data available",
    )
    def test_discover_finds_sessions(self) -> None:
        paths = discover_sessions(_REAL_BASE)
        assert len(paths) >= 1

    @pytest.mark.skipif(
        not _REAL_BASE.is_dir(),
        reason="No real session data available",
    )
    def test_get_all_sessions_returns_summaries(self) -> None:
        summaries = get_all_sessions(_REAL_BASE)
        assert len(summaries) >= 1
        for s in summaries:
            assert s.session_id != ""


# ---------------------------------------------------------------------------
# Pydantic model unit tests
# ---------------------------------------------------------------------------


class TestSessionContextModel:
    def test_defaults(self) -> None:
        ctx = SessionContext()
        assert ctx.cwd is None

    def test_with_cwd(self) -> None:
        ctx = SessionContext(cwd="/home/user")
        assert ctx.cwd == "/home/user"


class TestSessionStartDataModel:
    def test_required_session_id(self) -> None:
        d = SessionStartData(sessionId="abc")
        assert d.sessionId == "abc"
        assert d.version == 1
        assert d.producer == ""
        assert d.startTime is None
        assert d.context.cwd is None

    def test_missing_session_id_raises(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            SessionStartData.model_validate({})


class TestAssistantMessageDataModel:
    def test_defaults(self) -> None:
        d = AssistantMessageData()
        assert d.messageId == ""
        assert d.content == ""
        assert d.outputTokens == 0
        assert d.reasoningText is None
        assert d.toolRequests == []

    def test_optional_reasoning(self) -> None:
        d = AssistantMessageData(reasoningText="thinking...", reasoningOpaque="x")
        assert d.reasoningText == "thinking..."
        assert d.reasoningOpaque == "x"


class TestSessionShutdownDataModel:
    def test_defaults(self) -> None:
        d = SessionShutdownData()
        assert d.shutdownType == ""
        assert d.totalPremiumRequests == 0
        assert d.codeChanges is None
        assert d.modelMetrics == {}
        assert d.currentModel is None

    def test_with_model_metrics(self) -> None:
        d = SessionShutdownData(
            modelMetrics={
                "gpt-4": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                )
            }
        )
        assert d.modelMetrics["gpt-4"].usage.outputTokens == 100

    def test_empty_model_metrics(self) -> None:
        d = SessionShutdownData(modelMetrics={})
        assert d.modelMetrics == {}


class TestToolExecutionDataModel:
    def test_defaults(self) -> None:
        d = ToolExecutionData()
        assert d.toolCallId == ""
        assert d.model is None
        assert d.success is False
        assert d.toolTelemetry is None

    def test_with_telemetry(self) -> None:
        d = ToolExecutionData(
            toolCallId="tc-1",
            success=True,
            toolTelemetry=ToolTelemetry(properties={"key": "value"}),
        )
        assert d.toolTelemetry is not None
        assert d.toolTelemetry.properties["key"] == "value"


class TestUserMessageDataModel:
    def test_defaults(self) -> None:
        d = UserMessageData()
        assert d.content == ""
        assert d.transformedContent is None
        assert d.attachments == []
        assert d.interactionId is None


class TestGenericEventDataModel:
    def test_allows_extra_fields(self) -> None:
        d = GenericEventData.model_validate({"foo": "bar", "num": 42})
        assert d.model_extra is not None
        assert d.model_extra["foo"] == "bar"


# ---------------------------------------------------------------------------
# SessionEvent.parse_data() — all branches
# ---------------------------------------------------------------------------


class TestSessionEventParseData:
    def test_parse_session_start(self) -> None:
        ev = SessionEvent(
            type="session.start",
            data={"sessionId": "s1", "version": 1, "context": {}},
        )
        result = ev.parse_data()
        assert isinstance(result, SessionStartData)
        assert result.sessionId == "s1"

    def test_parse_assistant_message(self) -> None:
        ev = SessionEvent(
            type="assistant.message",
            data={"messageId": "m1", "content": "hi", "outputTokens": 10},
        )
        result = ev.parse_data()
        assert isinstance(result, AssistantMessageData)
        assert result.outputTokens == 10

    def test_parse_session_shutdown(self) -> None:
        ev = SessionEvent(
            type="session.shutdown",
            data={"shutdownType": "routine", "totalPremiumRequests": 3},
        )
        result = ev.parse_data()
        assert isinstance(result, SessionShutdownData)
        assert result.totalPremiumRequests == 3

    def test_parse_tool_execution_complete(self) -> None:
        ev = SessionEvent(
            type="tool.execution_complete",
            data={"toolCallId": "tc-1", "model": "gpt-4", "success": True},
        )
        result = ev.parse_data()
        assert isinstance(result, ToolExecutionData)
        assert result.model == "gpt-4"

    def test_parse_user_message(self) -> None:
        ev = SessionEvent(
            type="user.message",
            data={"content": "hello"},
        )
        result = ev.parse_data()
        assert isinstance(result, UserMessageData)
        assert result.content == "hello"

    def test_parse_unknown_event_type(self) -> None:
        ev = SessionEvent(
            type="some.unknown.event",
            data={"arbitrary": "data", "count": 42},
        )
        result = ev.parse_data()
        assert isinstance(result, GenericEventData)

    def test_parse_abort_event(self) -> None:
        ev = SessionEvent(type="abort", data={"reason": "user"})
        result = ev.parse_data()
        assert isinstance(result, GenericEventData)


# ---------------------------------------------------------------------------
# Edge cases — build_session_summary
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryEdgeCases:
    def test_empty_events(self) -> None:
        """Empty events → active SessionSummary with empty session_id (documented edge case)."""
        summary = build_session_summary([])
        assert summary.is_active is True
        assert summary.session_id == ""
        assert summary.model_calls == 0
        assert summary.user_messages == 0

    def test_no_session_dir(self, tmp_path: Path) -> None:
        events, _ = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=None)
        assert summary.name is None

    def test_plan_md_without_heading(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        plan = sdir / "plan.md"
        plan.write_text("Just some text without heading\n", encoding="utf-8")
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.name is None

    def test_no_plan_md(self, tmp_path: Path) -> None:
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.name is None

    def test_shutdown_without_code_changes(self, tmp_path: Path) -> None:
        shutdown_no_cc = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 2,
                    "totalApiDurationMs": 500,
                    "sessionStartTime": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, shutdown_no_cc)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.code_changes is None
        assert summary.model_metrics == {}

    def test_active_session_no_tool_exec_no_model(self, tmp_path: Path) -> None:
        """Active session with assistant messages but no tool.execution_complete."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.is_active is True
        assert summary.model is None
        # No model so tokens can't be attributed
        assert summary.model_metrics == {}

    def test_active_session_with_model_tokens(self, tmp_path: Path) -> None:
        """Active session where model is found via tool exec → tokens attributed."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        assert summary.model == "claude-sonnet-4"
        assert summary.model_metrics["claude-sonnet-4"].usage.outputTokens == 150

    def test_unexpected_event_types_ignored(self, tmp_path: Path) -> None:
        weird = json.dumps(
            {
                "type": "some.custom.event",
                "data": {"x": 1},
                "id": "ev-weird",
                "timestamp": "2026-03-07T10:05:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, weird, _USER_MSG)
        events = parse_events(p)
        assert len(events) == 3
        summary = build_session_summary(events)
        assert summary.user_messages == 1

    def test_shutdown_model_from_data_currentModel(self, tmp_path: Path) -> None:
        """When top-level currentModel is absent, use data.currentModel."""
        shutdown_ev = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 1,
                    "totalApiDurationMs": 100,
                    "sessionStartTime": 0,
                    "currentModel": "gpt-4",
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, shutdown_ev)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.model == "gpt-4"

    def test_ev_currentModel_takes_priority_over_data_currentModel(
        self, tmp_path: Path
    ) -> None:
        """Top-level ev.currentModel wins over data.currentModel."""
        shutdown_ev = json.dumps(
            {
                "type": "session.shutdown",
                "currentModel": "claude-opus-4.6",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 1,
                    "totalApiDurationMs": 100,
                    "sessionStartTime": 0,
                    "currentModel": "claude-sonnet-4",
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, shutdown_ev)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.model == "claude-opus-4.6"

    def test_assistant_message_without_output_tokens(self, tmp_path: Path) -> None:
        msg_no_tokens = json.dumps(
            {
                "type": "assistant.message",
                "data": {"messageId": "m1", "content": "hi"},
                "id": "ev-m",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, msg_no_tokens, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        # No outputTokens → model_metrics should be empty even though model is known
        assert summary.model_metrics == {}

    def test_negative_output_tokens_not_accumulated(self, tmp_path: Path) -> None:
        """assistant.message with negative outputTokens → active_output_tokens == 0."""
        neg_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-neg",
                    "content": "bad tokens",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": -50,
                },
                "id": "ev-neg",
                "timestamp": "2026-03-07T10:01:05.000Z",
                "parentId": "ev-user1",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, neg_msg, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 0

    def test_mixed_valid_bool_negative_tokens(self, tmp_path: Path) -> None:
        """Mix of valid (150), boolean (True), and negative (-50) outputTokens → 150."""
        bool_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-bool",
                    "content": "bool tokens",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": True,
                },
                "id": "ev-bool",
                "timestamp": "2026-03-07T10:01:06.000Z",
                "parentId": "ev-user1",
            }
        )
        neg_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-neg2",
                    "content": "neg tokens",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": -50,
                },
                "id": "ev-neg2",
                "timestamp": "2026-03-07T10:01:07.000Z",
                "parentId": "ev-user1",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, bool_msg, neg_msg, _TOOL_EXEC
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 150

    def test_multiple_session_start_uses_first(self, tmp_path: Path) -> None:
        """Two session.start events; session_id, start_time, cwd match FIRST event."""
        second_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "second-session-999",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.0",
                    "startTime": "2026-03-07T10:30:00.000Z",
                    "context": {"cwd": "/home/user/other-project"},
                },
                "id": "ev-start-2",
                "timestamp": "2026-03-07T10:30:00.000Z",
                "parentId": None,
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, second_start, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.session_id == "test-session-001"
        assert summary.start_time is not None
        assert summary.start_time.hour == 10
        assert summary.start_time.minute == 0
        assert summary.cwd == "/home/user/project"

    def test_empty_session_id_blocks_subsequent_start(self, tmp_path: Path) -> None:
        """First session.start with empty sessionId still blocks later starts."""
        empty_id_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.0",
                    "startTime": "2026-03-07T09:00:00.000Z",
                    "context": {"cwd": "/home/user/first"},
                },
                "id": "ev-empty-start",
                "timestamp": "2026-03-07T09:00:00.000Z",
                "parentId": None,
            }
        )
        second_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "real-session-id",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.0",
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {"cwd": "/home/user/second"},
                },
                "id": "ev-start-2",
                "timestamp": "2026-03-07T10:00:00.000Z",
                "parentId": None,
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, empty_id_start, second_start, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events)
        # First start wins even with empty sessionId
        assert summary.session_id == ""
        assert summary.cwd == "/home/user/first"
        assert summary.start_time is not None
        assert summary.start_time.hour == 9

    def test_skips_empty_events_files(self, tmp_path: Path) -> None:
        _write_events(tmp_path / "empty" / "events.jsonl")
        _write_events(tmp_path / "valid" / "events.jsonl", _START_EVENT)
        result = get_all_sessions(tmp_path)
        assert len(result) == 1

    def test_sessions_without_start_time_sort_last(self, tmp_path: Path) -> None:
        no_time_start = json.dumps(
            {
                "type": "session.start",
                "data": {"sessionId": "no-time", "version": 1, "context": {}},
                "id": "e1",
            }
        )
        with_time_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "has-time",
                    "version": 1,
                    "startTime": "2026-06-01T00:00:00.000Z",
                    "context": {},
                },
                "id": "e2",
                "timestamp": "2026-06-01T00:00:00.000Z",
            }
        )
        _write_events(tmp_path / "a" / "events.jsonl", no_time_start)
        _write_events(tmp_path / "b" / "events.jsonl", with_time_start)
        result = get_all_sessions(tmp_path)
        assert len(result) == 2
        assert result[0].session_id == "has-time"
        assert result[1].session_id == "no-time"

    def test_nonexistent_base(self, tmp_path: Path) -> None:
        result = get_all_sessions(tmp_path / "does_not_exist")
        assert result == []

    def test_naive_and_none_start_times_do_not_raise(self, tmp_path: Path) -> None:
        """Regression: mixing naive start_time with None (→ aware EPOCH) must not raise."""
        naive_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "naive-session",
                    "version": 1,
                    "startTime": "2026-03-08T01:11:20.932",
                    "context": {},
                },
                "id": "e1",
            }
        )
        no_time_start = json.dumps(
            {
                "type": "session.start",
                "data": {"sessionId": "no-time", "version": 1, "context": {}},
                "id": "e2",
            }
        )
        _write_events(tmp_path / "a" / "events.jsonl", naive_start)
        _write_events(tmp_path / "b" / "events.jsonl", no_time_start)
        result = get_all_sessions(tmp_path)
        assert len(result) == 2
        assert result[0].session_id == "naive-session"
        assert result[1].session_id == "no-time"

    def test_shutdown_with_empty_model_metrics_has_shutdown_metrics_false(
        self, tmp_path: Path
    ) -> None:
        """Shutdown with modelMetrics: {} → has_shutdown_metrics is False."""
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 1000,
                    "sessionStartTime": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, shutdown)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.has_shutdown_metrics is False
        assert summary.model_metrics == {}
        assert summary.is_active is False


# ---------------------------------------------------------------------------
# Coverage gap tests — parser.py
# ---------------------------------------------------------------------------


class TestParserCoverageGaps:
    """Tests targeting specific uncovered lines in parser.py."""

    def test_extract_session_name_os_error(self, tmp_path: Path) -> None:
        """plan.md exists but is unreadable → name is None (lines 103-104)."""
        events, sdir = _completed_events(tmp_path)
        plan = sdir / "plan.md"
        plan.write_text("# Title\n", encoding="utf-8")
        plan.chmod(0o000)
        try:
            summary = build_session_summary(events, session_dir=sdir)
            assert summary.name is None
        finally:
            plan.chmod(0o644)

    def test_session_start_validation_error(self, tmp_path: Path) -> None:
        """Malformed session.start data → skipped (lines 155-156)."""
        bad_start = json.dumps(
            {
                "type": "session.start",
                "data": {"sessionId": 12345},  # sessionId should be str
                "id": "ev-bad",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, bad_start, _USER_MSG, _ASSISTANT_MSG, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        # session.start was skipped, so no session_id extracted
        assert summary.session_id == ""
        assert summary.is_active is True

    def test_session_shutdown_validation_error(self, tmp_path: Path) -> None:
        """Malformed session.shutdown data → skipped (lines 166-167)."""
        bad_shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": "not-a-number",
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
                "currentModel": "gpt-4",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, bad_shutdown)
        events = parse_events(p)
        summary = build_session_summary(events)
        # Shutdown was skipped → session is active
        assert summary.is_active is True
        assert summary.session_id == "test-session-001"

    def test_resumed_session_new_model_not_in_metrics(self, tmp_path: Path) -> None:
        """Resumed session uses model not in shutdown metrics → new entry (line 226)."""
        # Shutdown with model A, resume uses model B
        shutdown_model_a = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 5,
                    "totalApiDurationMs": 1000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 5, "cost": 5},
                            "usage": {
                                "inputTokens": 1000,
                                "outputTokens": 200,
                                "cacheReadTokens": 0,
                                "cacheWriteTokens": 0,
                            },
                        }
                    },
                    "currentModel": "claude-sonnet-4",
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
                "currentModel": "gpt-5.1",
            }
        )
        resume_ev = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume",
                "timestamp": "2026-03-07T12:00:00.000Z",
            }
        )
        post_resume_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m-post",
                    "content": "resumed",
                    "toolRequests": [],
                    "interactionId": "int-r",
                    "outputTokens": 300,
                },
                "id": "ev-post",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p, _START_EVENT, _USER_MSG, shutdown_model_a, resume_ev, post_resume_msg
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        # Post-resume tokens go to active, not merged into model_metrics
        assert summary.active_output_tokens == 300
        # gpt-5.1 should NOT be in historical model_metrics (it's post-resume activity)
        assert "gpt-5.1" not in summary.model_metrics
        # Original model metrics preserved
        assert "claude-sonnet-4" in summary.model_metrics

    def test_active_session_tool_exec_validation_error(self, tmp_path: Path) -> None:
        """Bad tool.execution_complete in active session → skipped (lines 252-253)."""
        bad_tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": 999, "success": "not-bool"},
                "id": "ev-bad-tool",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, bad_tool)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.is_active is True
        # No model could be extracted from the bad tool event
        assert summary.model is None


# ---------------------------------------------------------------------------
# Issue #259 — debug logging on ValidationError in build_session_summary
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryDebugLogging:
    """Verify build_session_summary emits debug logs on malformed events."""

    def test_session_start_validation_error_logs_debug(self, tmp_path: Path) -> None:
        from loguru import logger

        bad_start = json.dumps(
            {
                "type": "session.start",
                "data": {"sessionId": 12345},  # sessionId should be str
                "id": "ev-bad",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, bad_start, _USER_MSG, _ASSISTANT_MSG, _TOOL_EXEC)
        events = parse_events(p)

        log_messages: list[str] = []
        handler_id = logger.add(lambda m: log_messages.append(str(m)), level="DEBUG")
        try:
            summary = build_session_summary(events)
        finally:
            logger.remove(handler_id)

        assert summary.session_id == ""
        assert any(
            "could not parse" in msg and "session.start" in msg for msg in log_messages
        )

    def test_session_shutdown_validation_error_logs_debug(self, tmp_path: Path) -> None:
        from loguru import logger

        bad_shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": "not-a-number",
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
                "currentModel": "gpt-4",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, bad_shutdown)
        events = parse_events(p)

        log_messages: list[str] = []
        handler_id = logger.add(lambda m: log_messages.append(str(m)), level="DEBUG")
        try:
            summary = build_session_summary(events)
        finally:
            logger.remove(handler_id)

        assert summary.is_active is True
        assert any(
            "could not parse" in msg and "session.shutdown" in msg
            for msg in log_messages
        )

    def test_tool_execution_complete_validation_error_logs_debug(
        self, tmp_path: Path
    ) -> None:
        """Bad tool.execution_complete data → debug log emitted with event type."""
        from loguru import logger

        bad_tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": 999, "success": "not-bool"},
                "id": "ev-bad-tool",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, bad_tool)
        events = parse_events(p)

        log_messages: list[str] = []
        handler_id = logger.add(lambda m: log_messages.append(str(m)), level="DEBUG")
        try:
            build_session_summary(events, config_path=tmp_path / "no-config.json")
        finally:
            logger.remove(handler_id)

        assert any(
            "could not parse" in msg and "tool.execution_complete" in msg
            for msg in log_messages
        )


# ---------------------------------------------------------------------------
# model_calls and user_messages
# ---------------------------------------------------------------------------


class TestModelCallsAndUserMessages:
    """Tests for model_calls and user_messages fields."""

    def test_active_session_counts_turn_starts(self, tmp_path: Path) -> None:
        """Active session with 5 turn_starts → model_calls = 5."""
        opus_tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-1",
                    "model": "claude-opus-4.6",
                    "interactionId": "int-1",
                    "success": True,
                },
                "id": "ev-tool-opus",
                "timestamp": "2026-03-07T10:01:07.000Z",
            }
        )
        turns = [
            json.dumps(
                {
                    "type": "assistant.turn_start",
                    "data": {"turnId": str(i), "interactionId": "int-1"},
                    "id": f"ev-turn-{i}",
                    "timestamp": f"2026-03-07T10:{i + 2:02d}:00.000Z",
                }
            )
            for i in range(5)
        ]
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, opus_tool, *turns, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        assert summary.model_calls == 5
        assert summary.user_messages == 1
        assert summary.total_premium_requests == 0

    def test_completed_session_counts_turn_starts(self, tmp_path: Path) -> None:
        """Completed session records model_calls from turn_start events."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _TURN_START_1,
            _ASSISTANT_MSG,
            _TURN_START_2,
            _ASSISTANT_MSG_2,
            _SHUTDOWN_EVENT,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is False
        assert summary.model_calls == 2
        assert summary.user_messages == 1
        assert summary.total_premium_requests == 5

    def test_completed_session_uses_exact_premium_requests(
        self, tmp_path: Path
    ) -> None:
        """Shutdown as last event → uses shutdown's totalPremiumRequests."""
        events, sdir = _completed_events(tmp_path)
        summary = build_session_summary(events, session_dir=sdir)
        assert summary.is_active is False
        assert summary.total_premium_requests == 5

    def test_active_session_zero_multiplier_model(self, tmp_path: Path) -> None:
        """Active session using gpt-5-mini (0×) → no estimation, just raw counts."""
        free_tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-free",
                    "model": "gpt-5-mini",
                    "interactionId": "int-1",
                    "success": True,
                },
                "id": "ev-tool-free",
                "timestamp": "2026-03-07T10:01:07.000Z",
            }
        )
        turns = [
            json.dumps(
                {
                    "type": "assistant.turn_start",
                    "data": {"turnId": str(i), "interactionId": "int-1"},
                    "id": f"ev-turn-{i}",
                    "timestamp": f"2026-03-07T10:{i + 2:02d}:00.000Z",
                }
            )
            for i in range(3)
        ]
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, free_tool, *turns, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.is_active is True
        assert summary.model_calls == 3
        assert summary.total_premium_requests == 0


# ---------------------------------------------------------------------------
# config.json model reading
# ---------------------------------------------------------------------------


class TestConfigModelReading:
    """Tests for reading model from config.json for active sessions."""

    def test_active_session_reads_config_model(self, tmp_path: Path) -> None:
        """Active session with no tool exec reads model from config.json."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "claude-opus-4.6-1m"}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model == "claude-opus-4.6-1m"

    def test_tool_exec_model_takes_precedence(self, tmp_path: Path) -> None:
        """Model from tool.execution_complete overrides config.json."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model == "claude-sonnet-4"

    def test_missing_config_returns_none(self, tmp_path: Path) -> None:
        """No config.json → model stays None."""
        config = tmp_path / "nonexistent" / "config.json"
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None

    def test_active_session_model_none_with_output_tokens_has_empty_metrics(
        self, tmp_path: Path
    ) -> None:
        """model=None + output tokens → model_metrics empty, active_output_tokens set."""
        config = tmp_path / "nonexistent" / "config.json"
        asst_250 = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-250",
                    "content": "hello",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": 250,
                },
                "id": "ev-asst-250",
                "timestamp": "2026-03-07T10:01:05.000Z",
                "parentId": "ev-user1",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, asst_250)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None
        assert summary.model_metrics == {}
        assert summary.active_output_tokens == 250

    def test_active_session_model_known_zero_tokens_has_empty_metrics(
        self, tmp_path: Path
    ) -> None:
        """model resolved from tool event + zero assistant output → model_metrics empty, model field set."""
        tool_exec_only = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-z",
                    "model": "claude-sonnet-4",
                    "interactionId": "int-1",
                    "success": True,
                },
                "id": "ev-tool-z",
                "timestamp": "2026-03-07T10:01:07.000Z",
                "parentId": "ev-user1",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_exec_only)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.is_active is True
        assert summary.model == "claude-sonnet-4"
        assert summary.model_metrics == {}
        assert summary.active_output_tokens == 0

    def test_active_session_config_model_zero_tokens_empty_metrics(
        self, tmp_path: Path
    ) -> None:
        """model from config.json + no assistant messages → model_metrics empty, model field set."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"model": "claude-haiku-4.5"}), encoding="utf-8")
        p = tmp_path / "s" / "events.jsonl"
        # Session with only session.start + user.message (no assistant.message)
        _write_events(p, _START_EVENT, _USER_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.is_active is True
        assert summary.model == "claude-haiku-4.5"
        assert summary.model_metrics == {}
        assert summary.active_output_tokens == 0

    def test_invalid_config_json(self, tmp_path: Path) -> None:
        """Malformed config.json → model stays None."""
        config = tmp_path / "config.json"
        config.write_text("NOT JSON{{{", encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None

    def test_invalid_config_json_emits_warning(self, tmp_path: Path) -> None:
        """Malformed config.json → returns None AND emits a WARNING log."""
        from loguru import logger

        config = tmp_path / "config.json"
        config.write_text("NOT JSON{{{", encoding="utf-8")

        warnings: list[str] = []
        handler_id = logger.add(
            lambda msg: warnings.append(str(msg)),
            level="WARNING",
            format="{message}",
        )
        try:
            result = _read_config_model(config)
        finally:
            logger.remove(handler_id)

        assert result is None
        assert len(warnings) == 1
        assert "malformed JSON" in warnings[0]
        assert str(config) in warnings[0]

    def test_config_without_model_key(self, tmp_path: Path) -> None:
        """config.json without 'model' key → model stays None."""
        config = tmp_path / "config.json"
        config.write_text('{"reasoning_effort": "high"}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None

    def test_config_model_integer_returns_none(self, tmp_path: Path) -> None:
        """config.json with {"model": 42} → model is None."""
        config = tmp_path / "config.json"
        config.write_text('{"model": 42}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None

    def test_config_model_null_returns_none(self, tmp_path: Path) -> None:
        """config.json with {"model": null} → model is None."""
        config = tmp_path / "config.json"
        config.write_text('{"model": null}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None

    def test_config_model_list_returns_none(self, tmp_path: Path) -> None:
        """config.json with {"model": []} → model is None."""
        config = tmp_path / "config.json"
        config.write_text('{"model": []}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=config)
        assert summary.model is None

    @staticmethod
    def _patch_unreadable_config(config: Path):
        """Context manager that makes ``Path.read_text`` raise for *config*."""
        original_read_text = Path.read_text

        def _raise_on_config(self_path: Path, *args: object, **kwargs: object) -> str:
            if self_path == config:
                raise OSError("Permission denied")
            return original_read_text(self_path, *args, **kwargs)  # type: ignore[arg-type]

        return patch.object(Path, "read_text", new=_raise_on_config)

    def test_unreadable_config_returns_none(self, tmp_path: Path) -> None:
        """config.json exists but is unreadable (OSError) → model stays None."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "claude-sonnet-4"}', encoding="utf-8")

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)

        with self._patch_unreadable_config(config):
            summary = build_session_summary(events, config_path=config)

        assert summary.model is None

    def test_unreadable_config_logs_debug(self, tmp_path: Path) -> None:
        """OSError reading config.json → returns None AND emits a DEBUG log."""
        from loguru import logger

        config = tmp_path / "config.json"
        config.write_text('{"model": "claude-sonnet-4"}', encoding="utf-8")

        log_messages: list[str] = []
        handler_id = logger.add(
            lambda msg: log_messages.append(str(msg)),
            level="DEBUG",
            format="{message}",
        )
        try:
            with self._patch_unreadable_config(config):
                result = _read_config_model(config)
        finally:
            logger.remove(handler_id)

        assert result is None
        assert any(
            "Could not read config file" in msg and str(config) in msg
            for msg in log_messages
        )

    def test_unicode_decode_error_returns_none(self, tmp_path: Path) -> None:
        """config.json with non-UTF-8 bytes → returns None without raising."""
        config = tmp_path / "config.json"
        config.write_bytes(b'\xff\xfe{"model": "gpt-5.1"}')
        result = _read_config_model(config)
        assert result is None


# ---------------------------------------------------------------------------
# build_session_summary — empty session (only session.start)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryEmptySession:
    """Session with only a session.start event and nothing else."""

    def test_is_active(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.is_active is True

    def test_session_id(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.session_id == "test-session-001"

    def test_zero_premium_requests(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.total_premium_requests == 0

    def test_zero_output_tokens(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.active_output_tokens == 0

    def test_zero_model_calls(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.model_calls == 0

    def test_zero_user_messages(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.user_messages == 0

    def test_name_from_plan_md(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        (tmp_path / "s" / "plan.md").write_text(
            "# Empty Session\n\nNothing here.", encoding="utf-8"
        )
        events = parse_events(p)
        summary = build_session_summary(
            events, session_dir=tmp_path / "s", config_path=Path("/dev/null")
        )
        assert summary.name == "Empty Session"

    def test_no_plan_md_name_is_none(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(
            events, session_dir=tmp_path / "s", config_path=Path("/dev/null")
        )
        assert summary.name is None

    def test_model_is_none_without_config(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.model is None

    def test_empty_model_metrics(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.model_metrics == {}

    def test_end_time_is_none(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.end_time is None

    def test_no_code_changes(self, tmp_path: Path) -> None:
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=Path("/dev/null"))
        assert summary.code_changes is None


# ---------------------------------------------------------------------------
# Issue #19 — _extract_session_name edge cases
# ---------------------------------------------------------------------------


class TestExtractSessionName:
    """Tests for _extract_session_name covering untested branches."""

    def test_plain_text_line_returns_none(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("Just plain text\n", encoding="utf-8")
        assert _extract_session_name(tmp_path) is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("", encoding="utf-8")
        assert _extract_session_name(tmp_path) is None

    def test_oserror_returns_none_and_logs(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("# Title\n", encoding="utf-8")

        original_open = Path.open

        def _raise_os_error(  # type: ignore[override]
            self: Path, *args: object, **kwargs: object
        ) -> object:
            if self == plan:
                raise OSError("denied")
            return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

        from loguru import logger

        log_messages: list[str] = []
        handler_id = logger.add(lambda m: log_messages.append(str(m)), level="DEBUG")
        try:
            with patch.object(Path, "open", _raise_os_error):
                assert _extract_session_name(tmp_path) is None
        finally:
            logger.remove(handler_id)
        assert any("Could not read session name" in msg for msg in log_messages)

    def test_extract_session_name_unicode_decode_error(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_bytes(b"\xff\xfe not valid utf-8")
        assert _extract_session_name(tmp_path) is None  # was crashing before fix

    def test_extract_session_name_no_h1_header(self, tmp_path: Path) -> None:
        (tmp_path / "plan.md").write_text(
            "## Subheading\nsome text\n", encoding="utf-8"
        )
        assert _extract_session_name(tmp_path) is None

    def test_extract_session_name_empty_h1(self, tmp_path: Path) -> None:
        (tmp_path / "plan.md").write_text("# \n", encoding="utf-8")
        assert _extract_session_name(tmp_path) is None

    def test_extract_session_name_ignores_subsequent_headings(
        self, tmp_path: Path
    ) -> None:
        """Only the first ``# `` heading is used; later headings are ignored."""
        plan = tmp_path / "plan.md"
        plan.write_text(
            "# First Heading\n# Second Heading\nsome body text\n", encoding="utf-8"
        )
        assert _extract_session_name(tmp_path) == "First Heading"

    def test_large_plan_reads_only_first_line(self, tmp_path: Path) -> None:
        """Confirm that only a single readline() reads ≤ 1 KB of a 100 KB+ file.

        Wraps ``Path.open`` with a spy file handle that tracks bytes read
        through ``readline()`` and raises on ``read()`` / ``readlines()``.
        Also patches ``read_text`` as a belt-and-suspenders guard.
        """
        title = "My Session Title"
        filler = "x" * 100 * 1024  # 100 KB of filler
        plan = tmp_path / "plan.md"
        plan.write_text(f"# {title}\n{filler}\n", encoding="utf-8")

        # Sanity: the function returns the expected title.
        assert _extract_session_name(tmp_path) == title

        original_open = Path.open
        bytes_read: list[int] = [0]
        readline_calls: list[int] = [0]

        class _SpyFile:
            """Context-manager spy that records readline bytes and forbids whole-file reads."""

            def __init__(self, fh: io.TextIOWrapper) -> None:
                self._fh = fh

            def readline(self, limit: int = -1) -> str:
                readline_calls[0] += 1
                line = self._fh.readline(limit)
                bytes_read[0] += len(line.encode("utf-8"))
                return line

            def read(self, size: int = -1) -> str:  # noqa: ARG002
                raise AssertionError("read() must not be called on plan.md")

            def readlines(self, hint: int = -1) -> list[str]:  # noqa: ARG002
                raise AssertionError("readlines() must not be called on plan.md")

            def __enter__(self) -> "_SpyFile":
                self._fh.__enter__()
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_val: BaseException | None,
                exc_tb: object,
            ) -> None:
                self._fh.__exit__(exc_type, exc_val, exc_tb)  # type: ignore[arg-type]

        def _spy_open(self_: Path, *args: object, **kwargs: object) -> object:
            fh = original_open(self_, *args, **kwargs)  # type: ignore[arg-type]
            return _SpyFile(fh) if self_ == plan else fh  # type: ignore[arg-type]

        original_read_text = Path.read_text

        def _no_read_text(self_: Path, *_a: object, **_kw: object) -> str:
            if self_ == plan:
                raise AssertionError("read_text must not be called")
            return original_read_text(self_, *_a, **_kw)  # type: ignore[arg-type]

        with (
            patch.object(Path, "open", _spy_open),
            patch.object(Path, "read_text", _no_read_text),
        ):
            result = _extract_session_name(tmp_path)

        assert result == title
        # Title line is ~20 bytes; the full file is 100+ KB.
        # A 1 KB threshold leaves ample headroom while catching whole-file reads.
        assert bytes_read[0] < 1024, (
            f"Expected < 1 KB from readline(), got {bytes_read[0]} bytes"
        )
        # Exactly one readline() call proves we don't iterate or re-read.
        assert readline_calls[0] == 1, (
            f"Expected exactly 1 readline() call, got {readline_calls[0]}"
        )


# ---------------------------------------------------------------------------
# Issue #19 — get_all_sessions OSError recovery
# ---------------------------------------------------------------------------


class TestGetAllSessionsOsError:
    """Tests that get_all_sessions gracefully skips sessions with OSError."""

    def test_oserror_session_is_skipped(self, tmp_path: Path) -> None:
        """A session that raises OSError on parse is silently skipped."""
        for sid in ["sess-a", "sess-b"]:
            d = tmp_path / sid
            d.mkdir()
            (d / "events.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session.start",
                        "data": {
                            "sessionId": sid,
                            "startTime": "2025-01-15T10:00:00Z",
                            "context": {"cwd": "/"},
                        },
                        "timestamp": "2025-01-15T10:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        target = tmp_path / "sess-a" / "events.jsonl"
        original_open = Path.open

        def _flaky_open(self: Path, *args: object, **kwargs: object) -> object:  # type: ignore[override]
            if self == target:
                raise OSError("permission denied")
            return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "open", _flaky_open):
            results = get_all_sessions(tmp_path)

        assert len(results) == 1
        assert results[0].session_id == "sess-b"

    def test_unicode_decode_error_session_is_skipped(self, tmp_path: Path) -> None:
        """A session with non-UTF-8 events.jsonl is gracefully skipped."""
        # Create a valid session
        d_good = tmp_path / "sess-good"
        d_good.mkdir()
        (d_good / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "session.start",
                    "data": {
                        "sessionId": "sess-good",
                        "startTime": "2025-01-15T10:00:00Z",
                        "context": {"cwd": "/"},
                    },
                    "timestamp": "2025-01-15T10:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # Create a session with invalid UTF-8 bytes
        d_bad = tmp_path / "sess-bad"
        d_bad.mkdir()
        (d_bad / "events.jsonl").write_bytes(b"\xff\xfe\x80\x81\n")

        results = get_all_sessions(tmp_path)
        # The good session should be present; the bad one skipped
        session_ids = [r.session_id for r in results]
        assert "sess-good" in session_ids
        assert "sess-bad" not in session_ids
        assert len(session_ids) == 1


# ---------------------------------------------------------------------------
# _infer_model_from_metrics — direct unit tests
# ---------------------------------------------------------------------------


class TestInferModelFromMetrics:
    """Direct unit tests for every branch of _infer_model_from_metrics."""

    def test_empty_returns_none(self) -> None:
        assert _infer_model_from_metrics({}) is None

    def test_single_key_returns_it(self) -> None:
        metrics = {"claude-sonnet-4": ModelMetrics(requests=RequestMetrics(count=5))}
        assert _infer_model_from_metrics(metrics) == "claude-sonnet-4"

    def test_multi_key_returns_highest_count(self) -> None:
        metrics = {
            "claude-sonnet-4": ModelMetrics(requests=RequestMetrics(count=3)),
            "claude-opus-4.6": ModelMetrics(requests=RequestMetrics(count=10)),
        }
        assert _infer_model_from_metrics(metrics) == "claude-opus-4.6"

    def test_tie_returns_a_model_deterministically(self) -> None:
        """When counts are equal, tie-breaking must be stable and by insertion order.

        This test documents (and locks in) the current tie-breaking behaviour:
        the first key by insertion order wins when counts are equal.
        If the behaviour changes, the test should be updated intentionally.
        """
        # First insertion order: model-a, then model-b
        metrics = {
            "model-a": ModelMetrics(requests=RequestMetrics(count=5)),
            "model-b": ModelMetrics(requests=RequestMetrics(count=5)),
        }
        assert _infer_model_from_metrics(metrics) == "model-a"

        # Reversed insertion order: model-b, then model-a
        metrics_reversed = {
            "model-b": ModelMetrics(requests=RequestMetrics(count=5)),
            "model-a": ModelMetrics(requests=RequestMetrics(count=5)),
        }
        assert _infer_model_from_metrics(metrics_reversed) == "model-b"

    def test_single_model_with_zero_count(self) -> None:
        """Single model with count=0 is still returned (single-key fast path)."""
        metrics = {"gpt-4o": ModelMetrics(requests=RequestMetrics(count=0))}
        assert _infer_model_from_metrics(metrics) == "gpt-4o"


# ---------------------------------------------------------------------------
# build_session_summary — completed session without currentModel (integration)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryInfersModelWhenCurrentModelAbsent:
    """Shutdown event with no currentModel → _infer_model_from_metrics is used."""

    def test_completed_session_infers_model_from_metrics(self, tmp_path: Path) -> None:
        shutdown_no_model = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 8,
                    "totalApiDurationMs": 5000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 3, "cost": 3},
                            "usage": {
                                "inputTokens": 1000,
                                "outputTokens": 200,
                                "cacheReadTokens": 0,
                                "cacheWriteTokens": 0,
                            },
                        },
                        "claude-opus-4.6": {
                            "requests": {"count": 8, "cost": 8},
                            "usage": {
                                "inputTokens": 3000,
                                "outputTokens": 600,
                                "cacheReadTokens": 0,
                                "cacheWriteTokens": 0,
                            },
                        },
                    },
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, shutdown_no_model)
        events = parse_events(p)
        summary = build_session_summary(events)

        # Highest count wins
        assert summary.model == "claude-opus-4.6"
        assert summary.is_active is False
        assert summary.total_premium_requests == 8


# ---------------------------------------------------------------------------
# get_all_sessions / session CLI with no session.start (Gap 3 — issue #275)
# ---------------------------------------------------------------------------


class TestGetAllSessionsNoStartEvent:
    """Events file with no ``session.start`` → summary has session_id='' and
    start_time=None but is still included in results and does not cause errors.
    """

    def test_summary_included_with_empty_session_id(self, tmp_path: Path) -> None:
        """get_all_sessions returns a summary for the file, not silently dropped."""
        only_user_msg = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "hello", "attachments": []},
                "id": "ev-u1",
                "timestamp": "2026-03-08T12:00:00.000Z",
            }
        )
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "sessionStartTime": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-08T12:10:00.000Z",
            }
        )
        _write_events(
            tmp_path / "no-start" / "events.jsonl",
            only_user_msg,
            shutdown,
        )
        results = get_all_sessions(tmp_path)
        assert len(results) == 1
        assert results[0].session_id == ""
        assert results[0].start_time is None

    def test_session_cli_does_not_match_empty_id(self, tmp_path: Path) -> None:
        """The ``session`` CLI command must not match the empty-ID session for
        any 4+ char query, and must not include a blank entry in 'Available:'.
        """
        from click.testing import CliRunner

        from copilot_usage.cli import main

        # Session without session.start (empty session_id)
        only_user_msg = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "hello", "attachments": []},
                "id": "ev-u1",
                "timestamp": "2026-03-08T12:00:00.000Z",
            }
        )
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "sessionStartTime": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-08T12:10:00.000Z",
            }
        )
        _write_events(
            tmp_path / "no-start-sess" / "events.jsonl",
            only_user_msg,
            shutdown,
        )

        # Also add a valid session so "Available:" list is populated
        _write_events(
            tmp_path / "valid-sess" / "events.jsonl",
            _START_EVENT,
            _SHUTDOWN_EVENT,
        )

        runner = CliRunner()
        result = runner.invoke(
            main, ["session", "nonexistent", "--path", str(tmp_path)]
        )
        assert result.exit_code != 0

        # "Available:" should contain the valid session but not a blank entry
        assert "test-ses" in result.output  # from _START_EVENT's sessionId
        # Blank entries would appear as ", ," or leading/trailing ", "
        if "Available:" in result.output:
            available_line = [
                line for line in result.output.splitlines() if "Available:" in line
            ][0]
            entries = [
                e.strip() for e in available_line.split("Available:")[1].split(",")
            ]
            assert "" not in entries, (
                f"Blank entry in Available list: {available_line!r}"
            )

    def test_no_crash_with_only_non_start_events(self, tmp_path: Path) -> None:
        """Corrupt session (shutdown without start) must not cause a traceback."""
        shutdown_only = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "crash",
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "sessionStartTime": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-08T12:10:00.000Z",
            }
        )
        _write_events(
            tmp_path / "corrupt" / "events.jsonl",
            shutdown_only,
        )
        # Must not raise
        results = get_all_sessions(tmp_path)
        assert len(results) == 1
        assert results[0].session_id == ""
        assert results[0].start_time is None
        # Completed (has shutdown), not active
        assert results[0].is_active is False


# ---------------------------------------------------------------------------
# Falsy-string edge cases (issue #321)
# ---------------------------------------------------------------------------


class TestParserFalsyStringEdgeCases:
    """Edge cases where parser helpers return '' instead of None."""

    def test_extract_session_name_whitespace_heading_returns_none(
        self, tmp_path: Path
    ) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("# \n", encoding="utf-8")
        assert _extract_session_name(tmp_path) is None

    def test_extract_session_name_hash_only_returns_none(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("#\n", encoding="utf-8")
        assert _extract_session_name(tmp_path) is None

    def test_read_config_model_empty_string_returns_none(self, tmp_path: Path) -> None:
        config = tmp_path / "config.json"
        config.write_text('{"model": ""}', encoding="utf-8")
        assert _read_config_model(config) is None

    def test_output_tokens_boolean_true_excluded(self, tmp_path: Path) -> None:
        """Boolean True must not be counted as outputTokens."""
        msg_bool_tokens = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "hi",
                    "outputTokens": True,
                },
                "id": "ev-m",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, msg_bool_tokens)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 0

    def test_output_tokens_boolean_false_excluded(self, tmp_path: Path) -> None:
        """Boolean False must not be counted as outputTokens."""
        msg_bool_tokens = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "hi",
                    "outputTokens": False,
                },
                "id": "ev-m",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, msg_bool_tokens)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 0


# ---------------------------------------------------------------------------
# _safe_int_tokens
# ---------------------------------------------------------------------------


class TestSafeIntTokens:
    def test_returns_int_for_genuine_int(self) -> None:
        assert _safe_int_tokens(42) == 42

    def test_returns_none_for_bool_true(self) -> None:
        assert _safe_int_tokens(True) is None

    def test_returns_none_for_bool_false(self) -> None:
        assert _safe_int_tokens(False) is None

    def test_returns_none_for_string(self) -> None:
        assert _safe_int_tokens("100") is None

    def test_returns_none_for_none(self) -> None:
        assert _safe_int_tokens(None) is None

    def test_returns_none_for_float(self) -> None:
        assert _safe_int_tokens(3.14) is None

    def test_returns_zero_for_zero(self) -> None:
        assert _safe_int_tokens(0) == 0

    def test_returns_none_for_negative_int(self) -> None:
        assert _safe_int_tokens(-1) is None

    def test_returns_none_for_large_negative(self) -> None:
        assert _safe_int_tokens(-100_000) is None


# ---------------------------------------------------------------------------
# Three shutdown cycles with mixed models
# ---------------------------------------------------------------------------


class TestThreeShutdownCyclesMergeModelMetrics:
    def test_three_shutdown_cycles_merge_model_metrics(self, tmp_path: Path) -> None:
        """Metrics accumulate correctly across 3 shutdown cycles with mixed models."""
        shutdown_cycle_1 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 500,
                    "totalApiDurationMs": 10000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 10, "cost": 500},
                            "usage": {
                                "inputTokens": 2000,
                                "outputTokens": 1000,
                                "cacheReadTokens": 100,
                                "cacheWriteTokens": 50,
                            },
                        }
                    },
                    "currentModel": "claude-sonnet-4",
                },
                "id": "ev-sd1",
                "timestamp": "2026-03-07T10:30:00.000Z",
                "currentModel": "claude-sonnet-4",
            }
        )
        resume_1 = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-r1",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        user_msg_2 = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "cycle 2"},
                "id": "ev-u2",
                "timestamp": "2026-03-07T11:01:00.000Z",
            }
        )
        shutdown_cycle_2 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 100,
                    "totalApiDurationMs": 5000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-opus-4.6": {
                            "requests": {"count": 5, "cost": 100},
                            "usage": {
                                "inputTokens": 3000,
                                "outputTokens": 800,
                                "cacheReadTokens": 200,
                                "cacheWriteTokens": 60,
                            },
                        }
                    },
                    "currentModel": "claude-opus-4.6",
                },
                "id": "ev-sd2",
                "timestamp": "2026-03-07T12:00:00.000Z",
                "currentModel": "claude-opus-4.6",
            }
        )
        resume_2 = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-r2",
                "timestamp": "2026-03-07T13:00:00.000Z",
            }
        )
        user_msg_3 = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "cycle 3"},
                "id": "ev-u3",
                "timestamp": "2026-03-07T13:01:00.000Z",
            }
        )
        shutdown_cycle_3 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 200,
                    "totalApiDurationMs": 8000,
                    "sessionStartTime": 0,
                    "modelMetrics": {
                        "claude-sonnet-4": {
                            "requests": {"count": 7, "cost": 200},
                            "usage": {
                                "inputTokens": 4000,
                                "outputTokens": 600,
                                "cacheReadTokens": 150,
                                "cacheWriteTokens": 30,
                            },
                        },
                        "claude-opus-4.6": {
                            "requests": {"count": 3, "cost": 150},
                            "usage": {
                                "inputTokens": 1500,
                                "outputTokens": 400,
                                "cacheReadTokens": 80,
                                "cacheWriteTokens": 20,
                            },
                        },
                    },
                    "currentModel": "claude-sonnet-4",
                },
                "id": "ev-sd3",
                "timestamp": "2026-03-07T14:00:00.000Z",
                "currentModel": "claude-sonnet-4",
            }
        )

        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            shutdown_cycle_1,
            resume_1,
            user_msg_2,
            shutdown_cycle_2,
            resume_2,
            user_msg_3,
            shutdown_cycle_3,
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        # Total premium requests: 500 + 100 + 200 = 800
        assert summary.total_premium_requests == 800
        # Total API duration: 10000 + 5000 + 8000 = 23000
        assert summary.total_api_duration_ms == 23000
        # Completed (shutdown is last event)
        assert summary.is_active is False

        # Both models present
        assert "claude-sonnet-4" in summary.model_metrics
        assert "claude-opus-4.6" in summary.model_metrics

        # claude-sonnet-4: cycle 1 + cycle 3 merged
        sonnet = summary.model_metrics["claude-sonnet-4"]
        assert sonnet.requests.count == 10 + 7  # 17
        assert sonnet.requests.cost == 500 + 200  # 700
        assert sonnet.usage.inputTokens == 2000 + 4000  # 6000
        assert sonnet.usage.outputTokens == 1000 + 600  # 1600
        assert sonnet.usage.cacheReadTokens == 100 + 150  # 250
        assert sonnet.usage.cacheWriteTokens == 50 + 30  # 80

        # claude-opus-4.6: cycle 2 + cycle 3 merged
        opus = summary.model_metrics["claude-opus-4.6"]
        assert opus.requests.count == 5 + 3  # 8
        assert opus.requests.cost == 100 + 150  # 250
        assert opus.usage.inputTokens == 3000 + 1500  # 4500
        assert opus.usage.outputTokens == 800 + 400  # 1200
        assert opus.usage.cacheReadTokens == 200 + 80  # 280
        assert opus.usage.cacheWriteTokens == 60 + 20  # 80


# ---------------------------------------------------------------------------
# _read_config_model — direct unit tests for every branch
# ---------------------------------------------------------------------------


class TestReadConfigModel:
    """Direct unit tests for ``_read_config_model`` covering all branches."""

    def test_valid_config_returns_model_string(self, tmp_path: Path) -> None:
        """Happy path: {"model": "claude-opus-4"} → "claude-opus-4"."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "claude-opus-4"}', encoding="utf-8")
        assert _read_config_model(config) == "claude-opus-4"

    def test_config_path_does_not_exist(self, tmp_path: Path) -> None:
        """Non-existent config_path → None (``not path.is_file()`` branch)."""
        config = tmp_path / "no-such-file.json"
        assert _read_config_model(config) is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        """File with invalid JSON → None (``json.JSONDecodeError`` branch)."""
        config = tmp_path / "config.json"
        config.write_text("NOT JSON{{{", encoding="utf-8")
        assert _read_config_model(config) is None

    def test_malformed_json_emits_warning(self, tmp_path: Path) -> None:
        """Malformed JSON → ``logger.warning`` is called."""
        from loguru import logger

        config = tmp_path / "config.json"
        config.write_text("{broken", encoding="utf-8")

        warnings: list[str] = []
        handler_id = logger.add(
            lambda msg: warnings.append(str(msg)),
            level="WARNING",
            format="{message}",
        )
        try:
            _read_config_model(config)
        finally:
            logger.remove(handler_id)

        assert len(warnings) == 1
        assert "malformed JSON" in warnings[0]
        assert str(config) in warnings[0]

    def test_model_integer_returns_none(self, tmp_path: Path) -> None:
        """{"model": 123} → None (``isinstance(model, str)`` guard)."""
        config = tmp_path / "config.json"
        config.write_text('{"model": 123}', encoding="utf-8")
        assert _read_config_model(config) is None

    def test_model_null_returns_none(self, tmp_path: Path) -> None:
        """{"model": null} → None (``data.get("model")`` returns None)."""
        config = tmp_path / "config.json"
        config.write_text('{"model": null}', encoding="utf-8")
        assert _read_config_model(config) is None

    def test_model_key_absent_returns_none(self, tmp_path: Path) -> None:
        """Key missing entirely → None."""
        config = tmp_path / "config.json"
        config.write_text('{"reasoning_effort": "high"}', encoding="utf-8")
        assert _read_config_model(config) is None

    def test_model_boolean_returns_none(self, tmp_path: Path) -> None:
        """{"model": true} → None (bool is not str)."""
        config = tmp_path / "config.json"
        config.write_text('{"model": true}', encoding="utf-8")
        assert _read_config_model(config) is None

    def test_oserror_returns_none(self, tmp_path: Path) -> None:
        """OSError on read → None."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        original_read_text = Path.read_text

        def _raise(self_path: Path, *a: object, **kw: object) -> str:
            if self_path == config:
                raise OSError("Permission denied")
            return original_read_text(self_path, *a, **kw)  # type: ignore[arg-type]

        with patch.object(Path, "read_text", new=_raise):
            assert _read_config_model(config) is None

    def test_oserror_emits_debug_log(self, tmp_path: Path) -> None:
        """OSError → ``logger.debug`` is called."""
        from loguru import logger

        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        original_read_text = Path.read_text

        def _raise(self_path: Path, *a: object, **kw: object) -> str:
            if self_path == config:
                raise OSError("Permission denied")
            return original_read_text(self_path, *a, **kw)  # type: ignore[arg-type]

        messages: list[str] = []
        handler_id = logger.add(
            lambda msg: messages.append(str(msg)),
            level="DEBUG",
            format="{message}",
        )
        try:
            with patch.object(Path, "read_text", new=_raise):
                _read_config_model(config)
        finally:
            logger.remove(handler_id)

        assert any(
            "Could not read config file" in m and str(config) in m for m in messages
        )

    def test_unicode_decode_error_returns_none(self, tmp_path: Path) -> None:
        """Non-UTF-8 bytes → None (``UnicodeDecodeError`` branch)."""
        config = tmp_path / "config.json"
        config.write_bytes(b'\xff\xfe{"model": "gpt-5.1"}')
        assert _read_config_model(config) is None


# ---------------------------------------------------------------------------
# Issue #508 — _read_config_model caching
# ---------------------------------------------------------------------------


class TestReadConfigModelCaching:
    """Verify ``@lru_cache`` prevents redundant disk reads across calls."""

    def test_repeated_calls_same_path_hit_cache(self, tmp_path: Path) -> None:
        """Multiple calls with the same config_path only read disk once."""
        _read_config_model.cache_clear()
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        assert _read_config_model(config) == "gpt-5.1"
        assert _read_config_model(config) == "gpt-5.1"
        assert _read_config_model(config) == "gpt-5.1"

        info = _read_config_model.cache_info()
        assert info.misses == 1
        assert info.hits == 2

    def test_different_paths_are_cached_independently(self, tmp_path: Path) -> None:
        """Each unique path is a separate cache entry."""
        _read_config_model.cache_clear()
        c1 = tmp_path / "a" / "config.json"
        c2 = tmp_path / "b" / "config.json"
        c1.parent.mkdir()
        c2.parent.mkdir()
        c1.write_text('{"model": "gpt-5.1"}', encoding="utf-8")
        c2.write_text('{"model": "claude-sonnet-4"}', encoding="utf-8")

        assert _read_config_model(c1) == "gpt-5.1"
        assert _read_config_model(c2) == "claude-sonnet-4"
        assert _read_config_model(c1) == "gpt-5.1"

        info = _read_config_model.cache_info()
        assert info.misses == 2
        assert info.hits == 1

    def test_get_all_sessions_reads_config_at_most_once(self, tmp_path: Path) -> None:
        """N active-modelless sessions → config file read at most once."""
        _read_config_model.cache_clear()
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        # Create 3 active sessions with no model info in events
        for name in ("s1", "s2", "s3"):
            session_start = json.dumps(
                {
                    "type": "session.start",
                    "data": {
                        "sessionId": name,
                        "version": 1,
                        "startTime": "2026-03-07T10:00:00.000Z",
                        "context": {},
                    },
                    "id": f"ev-{name}",
                    "timestamp": "2026-03-07T10:00:00.000Z",
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
                    "id": f"ev-u-{name}",
                    "timestamp": "2026-03-07T10:01:00.000Z",
                }
            )
            _write_events(
                tmp_path / "sessions" / name / "events.jsonl",
                session_start,
                user_msg,
            )

        read_count = 0
        original_read = Path.read_text

        def counting_read(self: Path, *a: object, **kw: object) -> str:
            nonlocal read_count
            if self == config:
                read_count += 1
            return original_read(self, *a, **kw)  # type: ignore[arg-type]

        with (
            patch.object(Path, "read_text", new=counting_read),
            patch("copilot_usage.parser._CONFIG_PATH", config),
        ):
            summaries = get_all_sessions(tmp_path / "sessions")

        active = [s for s in summaries if s.is_active]
        assert len(active) == 3
        assert all(s.model == "gpt-5.1" for s in active)
        # Config file read at most once thanks to lru_cache
        assert read_count <= 1

    def test_get_all_sessions_clears_cache_between_calls(self, tmp_path: Path) -> None:
        """Successive get_all_sessions calls pick up config edits."""
        _read_config_model.cache_clear()
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        session_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "s1",
                    "version": 1,
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {},
                },
                "id": "ev-s1",
                "timestamp": "2026-03-07T10:00:00.000Z",
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
                "id": "ev-u-s1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        _write_events(
            tmp_path / "sessions" / "s1" / "events.jsonl",
            session_start,
            user_msg,
        )

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            summaries = get_all_sessions(tmp_path / "sessions")
            assert summaries[0].model == "gpt-5.1"

            # Edit the config file between calls
            config.write_text('{"model": "claude-sonnet-4"}', encoding="utf-8")

            summaries = get_all_sessions(tmp_path / "sessions")
            assert summaries[0].model == "claude-sonnet-4"


# ---------------------------------------------------------------------------
# Issue #418 — Gap 1: malformed session.start (ValidationError)
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryMalformedSessionStart:
    """Gap 1: malformed session.start → session_id='', start_time=None."""

    def test_malformed_session_start_skipped(self, tmp_path: Path) -> None:
        """Malformed session.start → session_id='', start_time=None, is_active=True."""
        bad_start = json.dumps({"type": "session.start", "data": {}, "id": "ev-bad"})
        assistant = json.dumps(
            {
                "type": "assistant.message",
                "data": {"messageId": "m1", "content": "hi", "outputTokens": 100},
                "id": "ev-a1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, bad_start, assistant)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.session_id == ""
        assert summary.start_time is None
        assert summary.is_active is True

    def test_second_session_start_ignored_after_valid(self, tmp_path: Path) -> None:
        """Second valid session.start after the first is silently ignored."""
        second_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "second-id",
                    "startTime": "2026-03-07T11:00:00.000Z",
                    "context": {"cwd": "/other"},
                },
                "id": "ev-start-2",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, second_start, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        # First session.start wins
        assert summary.session_id == "test-session-001"
        assert summary.cwd == "/home/user/project"


# ---------------------------------------------------------------------------
# Issue #418 — Gap 2: active session, no model, has output tokens
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryActiveNoModelOutputTokens:
    """Gap 2: output tokens accumulated but model is None."""

    def test_active_no_model_output_tokens_preserved(self, tmp_path: Path) -> None:
        """Active session: model is None → model_metrics={}, active_output_tokens set."""
        assistant = json.dumps(
            {
                "type": "assistant.message",
                "data": {"messageId": "m1", "content": "hi", "outputTokens": 250},
                "id": "ev-a1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, assistant)
        events = parse_events(p)
        # No tool events and no config file → model stays None
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.model is None
        assert summary.model_metrics == {}
        assert summary.active_output_tokens == 250
        assert summary.active_model_calls == 0


# ---------------------------------------------------------------------------
# Issue #418 — Gap 3: multiple tool.execution_complete — first model wins
# ---------------------------------------------------------------------------


class TestBuildSessionSummaryToolModelSelection:
    """Gap 3: first non-None tool model wins; None falls through."""

    def test_first_tool_model_wins(self, tmp_path: Path) -> None:
        """When multiple tool events have models, first non-None model is used."""
        tool1 = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-1",
                    "model": "claude-sonnet-4",
                    "success": True,
                },
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        tool2 = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-2",
                    "model": "gpt-5.1",
                    "success": True,
                },
                "id": "ev-t2",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, tool1, tool2)
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.model == "claude-sonnet-4"

    def test_tool_model_none_falls_through_to_second(self, tmp_path: Path) -> None:
        """First tool event has model=None → loop continues to second event."""
        tool_no_model = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-1",
                    "model": None,
                    "success": True,
                },
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        tool_with_model = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-2",
                    "model": "gpt-5.1",
                    "success": True,
                },
                "id": "ev-t2",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, tool_no_model, tool_with_model
        )
        events = parse_events(p)
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.model == "gpt-5.1"


# ---------------------------------------------------------------------------
# Issue #470 — _first_pass direct unit tests
# ---------------------------------------------------------------------------


class TestFirstPassDirect:
    """Direct unit tests for _first_pass covering untested branches."""

    def test_second_session_start_ignored(self, tmp_path: Path) -> None:
        """Only the first SESSION_START's identity is used."""
        t1 = "2026-03-07T10:00:00.000Z"
        t2 = "2026-03-07T11:00:00.000Z"
        start1 = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "first",
                    "startTime": t1,
                    "context": {"cwd": "/a"},
                },
                "id": "ev-s1",
                "timestamp": t1,
            }
        )
        start2 = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "second",
                    "startTime": t2,
                    "context": {"cwd": "/b"},
                },
                "id": "ev-s2",
                "timestamp": t2,
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, start1, start2, _USER_MSG)
        events = parse_events(p)
        result = _first_pass(events)
        assert result.session_id == "first"
        assert result.start_time == datetime(2026, 3, 7, 10, 0, tzinfo=UTC)

    def test_invalid_session_start_skipped(self, tmp_path: Path) -> None:
        """A malformed SESSION_START (ValidationError) is skipped without crash."""
        bad_start = json.dumps(
            {
                "type": "session.start",
                "data": {"bad": "data"},
                "id": "ev-bad",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, bad_start, _USER_MSG)
        events = parse_events(p)
        result = _first_pass(events)
        assert result.session_id == ""
        assert result.start_time is None

    def test_invalid_shutdown_skipped(self, tmp_path: Path) -> None:
        """A malformed SESSION_SHUTDOWN is skipped; session proceeds without it."""
        bad_shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {"totalPremiumRequests": "not-a-number"},
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, bad_shutdown)
        events = parse_events(p)
        result = _first_pass(events)
        assert result.all_shutdowns == []
        assert result.session_id == "test-session-001"


# ---------------------------------------------------------------------------
# Issue #470 — _detect_resume direct unit tests
# ---------------------------------------------------------------------------


class TestDetectResumeDirect:
    """Direct unit tests for _detect_resume covering untested branches."""

    def test_empty_shutdowns_returns_zeroed(self) -> None:
        """No shutdowns → zeroed _ResumeInfo."""
        result = _detect_resume(events=[], all_shutdowns=[])
        assert result.session_resumed is False
        assert result.post_shutdown_output_tokens == 0
        assert result.post_shutdown_turn_starts == 0
        assert result.post_shutdown_user_messages == 0
        assert result.last_resume_time is None

    def test_captures_resume_timestamp(self, tmp_path: Path) -> None:
        """SESSION_RESUME timestamp is captured into last_resume_time."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT, _RESUME_EVENT)
        events = parse_events(p)
        fp = _first_pass(events)
        result = _detect_resume(events, fp.all_shutdowns)
        assert result.session_resumed is True
        assert result.last_resume_time == datetime(2026, 3, 7, 12, 0, tzinfo=UTC)

    def test_accumulates_post_shutdown_tokens(self, tmp_path: Path) -> None:
        """Post-shutdown assistant messages accumulate output tokens."""
        asst1 = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "a",
                    "toolRequests": [],
                    "interactionId": "i1",
                    "outputTokens": 100,
                },
                "id": "ev-a1",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        asst2 = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m2",
                    "content": "b",
                    "toolRequests": [],
                    "interactionId": "i2",
                    "outputTokens": 50,
                },
                "id": "ev-a2",
                "timestamp": "2026-03-07T12:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT, asst1, asst2)
        events = parse_events(p)
        fp = _first_pass(events)
        result = _detect_resume(events, fp.all_shutdowns)
        assert result.post_shutdown_output_tokens == 150

    def test_counts_user_messages_and_turn_starts(self, tmp_path: Path) -> None:
        """Post-shutdown user messages and turn starts are counted separately."""
        user1 = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "u1"},
                "id": "ev-u1",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        user2 = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "u2"},
                "id": "ev-u2",
                "timestamp": "2026-03-07T12:02:00.000Z",
            }
        )
        turn1 = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "t1"},
                "id": "ev-ts1",
                "timestamp": "2026-03-07T12:01:30.000Z",
            }
        )
        turn2 = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "t2"},
                "id": "ev-ts2",
                "timestamp": "2026-03-07T12:02:30.000Z",
            }
        )
        turn3 = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "t3"},
                "id": "ev-ts3",
                "timestamp": "2026-03-07T12:03:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _SHUTDOWN_EVENT,
            user1,
            user2,
            turn1,
            turn2,
            turn3,
        )
        events = parse_events(p)
        fp = _first_pass(events)
        result = _detect_resume(events, fp.all_shutdowns)
        assert result.post_shutdown_user_messages == 2
        assert result.post_shutdown_turn_starts == 3


# ---------------------------------------------------------------------------
# Issue #470 — _build_active_summary model from config.json fallback
# ---------------------------------------------------------------------------


class TestBuildActiveSummaryConfigFallback:
    """Direct test for _build_active_summary config.json model fallback."""

    def test_model_from_config_fallback(self, tmp_path: Path) -> None:
        """When no tool events exist, model is read from config.json."""
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"model": "gpt-4o"}), encoding="utf-8")
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        fp = _first_pass(events)
        result = _build_active_summary(fp, name=None, config_path=config)
        assert result.model == "gpt-4o"


# ---------------------------------------------------------------------------
# Issue #498 — _first_pass captures tool_model, eliminating second pass
# ---------------------------------------------------------------------------


class TestFirstPassToolModel:
    """Verify _first_pass populates tool_model from tool.execution_complete events."""

    def test_tool_model_captured_in_first_pass(self, tmp_path: Path) -> None:
        """tool_model is set from the first tool.execution_complete with a model."""
        turn_start = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "t1"},
                "id": "ev-ts1",
                "timestamp": "2026-03-07T10:00:30.000Z",
            }
        )
        assistant = json.dumps(
            {
                "type": "assistant.message",
                "data": {"messageId": "m1", "content": "hi", "outputTokens": 100},
                "id": "ev-a1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-1",
                    "model": "claude-sonnet-4",
                    "success": True,
                },
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, turn_start, assistant, tool)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model == "claude-sonnet-4"

    def test_tool_model_none_when_no_tool_events(self, tmp_path: Path) -> None:
        """tool_model is None when no tool.execution_complete events exist."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model is None

    def test_tool_model_skips_none_model(self, tmp_path: Path) -> None:
        """tool.execution_complete with model=None is skipped; next one wins."""
        tool_no_model = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-1", "model": None, "success": True},
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        tool_with_model = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-2", "model": "gpt-5.1", "success": True},
                "id": "ev-t2",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_no_model, tool_with_model)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model == "gpt-5.1"

    def test_active_session_resolves_model_via_first_pass(self, tmp_path: Path) -> None:
        """Active session (no shutdown) resolves model from _first_pass.tool_model."""
        turn_start = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "t1"},
                "id": "ev-ts1",
                "timestamp": "2026-03-07T10:00:30.000Z",
            }
        )
        assistant = json.dumps(
            {
                "type": "assistant.message",
                "data": {"messageId": "m1", "content": "hi", "outputTokens": 200},
                "id": "ev-a1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-1",
                    "model": "claude-sonnet-4",
                    "success": True,
                },
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, turn_start, assistant, tool)
        events = parse_events(p)

        # Verify _first_pass captures tool_model
        fp = _first_pass(events)
        assert fp.tool_model == "claude-sonnet-4"

        # Verify build_session_summary uses tool_model correctly
        summary = build_session_summary(events, config_path=tmp_path / "no-config.json")
        assert summary.model == "claude-sonnet-4"
        assert summary.is_active is True

    def test_malformed_tool_event_skipped(self, tmp_path: Path) -> None:
        """A malformed tool.execution_complete is skipped; next valid one wins."""
        bad_tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-1", "toolTelemetry": "invalid"},
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        good_tool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-2",
                    "model": "gpt-5.1",
                    "success": True,
                },
                "id": "ev-t2",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, bad_tool, good_tool)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model == "gpt-5.1"


# ---------------------------------------------------------------------------
# Issue #509 — mtime-based session cache
# ---------------------------------------------------------------------------


class TestSessionCacheMtime:
    """get_all_sessions skips parse_events for files whose mtime is unchanged."""

    def _make_session(self, base: Path, name: str, sid: str) -> Path:
        """Create a completed session (with shutdown) and return events_path."""
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": sid,
                    "version": 1,
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {"cwd": "/"},
                },
                "id": f"ev-{sid}",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        user = json.dumps(
            {
                "type": "user.message",
                "data": {
                    "content": "hi",
                    "transformedContent": "hi",
                    "attachments": [],
                    "interactionId": "int-1",
                },
                "id": f"ev-u-{sid}",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 1,
                    "totalApiDurationMs": 500,
                    "sessionStartTime": 1772895600000,
                    "modelMetrics": {
                        "gpt-5.1": {
                            "requests": {"count": 1, "cost": 1},
                            "usage": {"outputTokens": 50},
                        }
                    },
                },
                "id": f"ev-sd-{sid}",
                "timestamp": "2026-03-07T10:05:00.000Z",
            }
        )
        return _write_events(base / name / "events.jsonl", start, user, shutdown)

    def test_unchanged_file_not_reparsed(self, tmp_path: Path) -> None:
        """Only the file with a bumped mtime is re-parsed on the second call."""
        p1 = self._make_session(tmp_path, "sess-a", "a")
        self._make_session(tmp_path, "sess-b", "b")

        # First call — populates cache; both files must be parsed.
        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            result1 = get_all_sessions(tmp_path)
            assert len(result1) == 2
            assert spy.call_count == 2

        # Modify only sess-a (append an extra event and bump mtime).
        extra = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-extra",
                    "content": "extra",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": 42,
                },
                "id": "ev-extra",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        with p1.open("a", encoding="utf-8") as fh:
            fh.write(extra + "\n")

        # Ensure the mtime actually differs (size already changed via
        # append, but bump mtime_ns too for robustness).
        import os

        stat = p1.stat()
        os.utime(p1, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

        # Second call — only the modified file should be re-parsed.
        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            result2 = get_all_sessions(tmp_path)
            assert len(result2) == 2
            assert spy.call_count == 1
            spy.assert_called_once_with(p1)

    def test_cache_returns_correct_summaries(self, tmp_path: Path) -> None:
        """Cached entries produce the same summaries as a fresh parse."""
        self._make_session(tmp_path, "sess-a", "a")
        self._make_session(tmp_path, "sess-b", "b")

        first = get_all_sessions(tmp_path)
        second = get_all_sessions(tmp_path)

        # Ensure that all fields of the summaries are identical between
        # the initial parse and the cached results.
        assert [s.model_dump() for s in first] == [s.model_dump() for s in second]

    def test_cache_refreshes_session_name_on_plan_rename(self, tmp_path: Path) -> None:
        """Cached summaries pick up plan.md edits without re-parsing events."""
        p = self._make_session(tmp_path, "sess-a", "a")
        plan = p.parent / "plan.md"
        plan.write_text("# Original Name\n", encoding="utf-8")

        first = get_all_sessions(tmp_path)
        assert len(first) == 1
        assert first[0].name == "Original Name"

        # Edit plan.md without touching events.jsonl
        plan.write_text("# Renamed Session\n", encoding="utf-8")

        second = get_all_sessions(tmp_path)
        assert len(second) == 1
        assert second[0].name == "Renamed Session"
        # The cache should have been updated in-place
        cached_summary = _SESSION_CACHE[p].summary
        assert cached_summary.name == "Renamed Session"

    def test_single_stat_per_file(self, tmp_path: Path) -> None:
        """events.jsonl stat'd once (discovery), plan.md stat'd once (cache store)."""
        self._make_session(tmp_path, "sess-a", "a")

        with patch(
            "copilot_usage.parser._safe_file_identity", wraps=_safe_file_identity
        ) as spy:
            get_all_sessions(tmp_path)
            # _safe_file_identity called once by _discover_with_identity for
            # events.jsonl, and once for plan.md when storing the cache entry.
            assert spy.call_count == 2

    def test_resumed_session_not_cached(self, tmp_path: Path) -> None:
        """A session that resumed after shutdown is NOT cached (model may change)."""
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "resumed-1",
                    "version": 1,
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {"cwd": "/"},
                },
                "id": "ev-start",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        shutdown = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 1,
                    "totalApiDurationMs": 500,
                    "sessionStartTime": 1772895600000,
                    "modelMetrics": {
                        "gpt-5.1": {
                            "requests": {"count": 1, "cost": 1},
                            "usage": {"outputTokens": 50},
                        }
                    },
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T10:05:00.000Z",
            }
        )
        resume = json.dumps(
            {
                "type": "session.resume",
                "data": {},
                "id": "ev-resume",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        user_after = json.dumps(
            {
                "type": "user.message",
                "data": {
                    "content": "hi again",
                    "transformedContent": "hi again",
                    "attachments": [],
                    "interactionId": "int-2",
                },
                "id": "ev-u2",
                "timestamp": "2026-03-07T11:01:00.000Z",
            }
        )
        events_path = _write_events(
            tmp_path / "sess-resumed" / "events.jsonl",
            start,
            shutdown,
            resume,
            user_after,
        )

        results = get_all_sessions(tmp_path)
        assert len(results) == 1
        assert results[0].is_active is True

        # Resumed session should NOT be in the cache
        assert events_path not in _SESSION_CACHE

    def test_plan_md_not_reread_when_unchanged(self, tmp_path: Path) -> None:
        """plan.md is not re-read for cached sessions when its identity is unchanged."""
        p = self._make_session(tmp_path, "sess-a", "a")
        plan = p.parent / "plan.md"
        plan.write_text("# My Session\n", encoding="utf-8")

        # First call populates cache
        first = get_all_sessions(tmp_path)
        assert len(first) == 1
        assert first[0].name == "My Session"

        # Second call with plan.md unchanged — _extract_session_name must NOT be called
        with patch(
            "copilot_usage.parser._extract_session_name",
            wraps=_extract_session_name,
        ) as spy:
            second = get_all_sessions(tmp_path)
            assert len(second) == 1
            assert second[0].name == "My Session"
            spy.assert_not_called()

    def test_plan_md_reread_when_changed(self, tmp_path: Path) -> None:
        """plan.md IS re-read when its file identity changes between calls."""
        p = self._make_session(tmp_path, "sess-a", "a")
        plan = p.parent / "plan.md"
        plan.write_text("# Original\n", encoding="utf-8")

        first = get_all_sessions(tmp_path)
        assert first[0].name == "Original"

        # Modify plan.md
        plan.write_text("# Updated\n", encoding="utf-8")

        with patch(
            "copilot_usage.parser._extract_session_name",
            wraps=_extract_session_name,
        ) as spy:
            second = get_all_sessions(tmp_path)
            assert second[0].name == "Updated"
            spy.assert_called_once()

    def test_cached_session_stores_plan_id(self, tmp_path: Path) -> None:
        """Cache entries use _CachedSession with plan_id field."""
        p = self._make_session(tmp_path, "sess-a", "a")
        plan = p.parent / "plan.md"
        plan.write_text("# Test\n", encoding="utf-8")

        get_all_sessions(tmp_path)

        entry = _SESSION_CACHE[p]
        assert isinstance(entry, _CachedSession)
        assert entry.plan_id == _safe_file_identity(plan)
        assert entry.summary.name == "Test"
