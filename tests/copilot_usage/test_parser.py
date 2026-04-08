"""Tests for copilot_usage.parser — session discovery, parsing, and summary."""

# pyright: reportPrivateUsage=false

import builtins
import io
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import SupportsIndex, cast, overload
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import copilot_usage.parser as _parser_module
from copilot_usage._fs_utils import safe_file_identity
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
    ToolRequest,
    ToolTelemetry,
    UserMessageData,
)
from copilot_usage.parser import (
    _DISCOVERY_CACHE,
    _EVENTS_CACHE,
    _FIRST_PASS_EVENT_TYPES,
    _MAX_CACHED_EVENTS,
    _MAX_CACHED_SESSIONS,
    _MAX_PLAN_PROBES,
    _SESSION_CACHE,
    _build_active_summary,
    _build_completed_summary,
    _CachedEvents,
    _CachedSession,
    _CopilotConfig,
    _detect_resume,
    _discover_with_identity,
    _extract_output_tokens,
    _extract_session_name,
    _first_pass,
    _FirstPassResult,
    _infer_model_from_metrics,
    _insert_session_entry,
    _read_config_model,
    _ResumeInfo,
    build_session_summary,
    discover_sessions,
    get_all_sessions,
    get_cached_events,
    parse_events,
)


def _reset_all_caches() -> None:
    """Clear all module-level caches (shared between fixture and tests)."""
    _SESSION_CACHE.clear()
    _EVENTS_CACHE.clear()
    _DISCOVERY_CACHE.clear()
    _read_config_model.cache_clear()
    _parser_module._config_file_id = None


@pytest.fixture(autouse=True)
def _clear_session_cache() -> None:
    """Isolate tests from all module-level caches."""
    _reset_all_caches()


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


def _make_completed_session(base: Path, name: str, sid: str) -> Path:
    """Create a completed session directory and return the events.jsonl path.

    Writes start, user-message, and shutdown events — the minimal set
    required to produce a valid completed ``SessionSummary``.
    """
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

        # s1 is excluded (FileNotFoundError → pruned); s2 still returned.
        assert result == [s2]

    def test_stat_race_permission_error(self, tmp_path: Path) -> None:
        """discover_sessions skips sessions whose events.jsonl is unreadable."""
        s1 = tmp_path / "sess-a" / "events.jsonl"
        _write_events(s1, _START_EVENT)

        original_stat = Path.stat

        def _flaky_stat(self: Path, **kwargs: object) -> object:
            if self.name == "events.jsonl":
                raise PermissionError("denied")
            return original_stat(self)

        with patch.object(Path, "stat", _flaky_stat):
            result = discover_sessions(tmp_path)

        # Session with unreadable events.jsonl is skipped, not crashed
        assert result == []

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
# discover_sessions — depth contract
# ---------------------------------------------------------------------------


class TestDiscoverSessionsDepth:
    """Regression: discover_sessions must only scan one directory level."""

    def test_two_level_deep_events_not_discovered(self, tmp_path: Path) -> None:
        """A nested ``deeply/nested/events.jsonl`` must NOT be discovered."""
        # Two-level deep file — should be excluded
        deep = tmp_path / "deeply" / "nested" / "events.jsonl"
        _write_events(deep, _START_EVENT)

        # One-level deep file — should be included
        valid = tmp_path / "valid-session" / "events.jsonl"
        _write_events(valid, _START_EVENT)

        result = get_all_sessions(base_path=tmp_path)
        assert len(result) == 1
        assert result[0].session_id == "test-session-001"

        # Also verify via discover_sessions directly
        paths = discover_sessions(tmp_path)
        assert paths == [valid]


# ---------------------------------------------------------------------------
# _discover_with_identity — no stat for absent plan.md (issue #763)
# ---------------------------------------------------------------------------


class TestDiscoverWithIdentityNoAbsentPlanStat:
    """os.scandir-based discovery must not stat absent plan.md files."""

    def test_no_stat_for_absent_plan_md(self, tmp_path: Path) -> None:
        """No stat() call is made for a non-existent plan.md path.

        Creates 100 sessions where only 10 have plan.md.  Patches
        ``safe_file_identity`` to count calls and asserts that the call
        count equals exactly 1 (root directory) + 10 (existing plan.md).
        ``events.jsonl`` uses direct ``Path.stat()`` rather than
        ``safe_file_identity``, and absent plan.md files are not probed
        on fresh discovery.
        """
        n_total = 100
        n_with_plan = 10
        for i in range(n_total):
            session_dir = tmp_path / f"sess-{i:04d}"
            events = session_dir / "events.jsonl"
            _write_events(events, _START_EVENT)
            if i < n_with_plan:
                (session_dir / "plan.md").write_text(
                    f"# Session {i}\n", encoding="utf-8"
                )

        with patch(
            "copilot_usage.parser.safe_file_identity", wraps=safe_file_identity
        ) as spy:
            _, result = _discover_with_identity(tmp_path)

        assert len(result) == n_total
        # 1 call for root directory + n_with_plan for existing plan.md.
        # events.jsonl uses direct stat() rather than safe_file_identity,
        # and absent plan.md files are not probed on fresh discovery.
        assert spy.call_count == 1 + n_with_plan

    def test_plan_id_populated_when_present(self, tmp_path: Path) -> None:
        """plan_id is non-None only for sessions that have plan.md on disk."""
        for i in range(5):
            session_dir = tmp_path / f"sess-{i}"
            _write_events(session_dir / "events.jsonl", _START_EVENT)
        # Add plan.md to only the first session
        (tmp_path / "sess-0" / "plan.md").write_text("# Plan\n", encoding="utf-8")

        _, result = _discover_with_identity(tmp_path)
        plans = {p.parent.name: pid for p, _eid, pid in result}

        assert plans["sess-0"] is not None
        for i in range(1, 5):
            assert plans[f"sess-{i}"] is None

    def test_include_plan_false_skips_all_plan_stat(self, tmp_path: Path) -> None:
        """When include_plan=False, plan.md stat is skipped even if file exists."""
        session_dir = tmp_path / "sess-a"
        _write_events(session_dir / "events.jsonl", _START_EVENT)
        (session_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")

        with patch(
            "copilot_usage.parser.safe_file_identity", wraps=safe_file_identity
        ) as spy:
            _, result = _discover_with_identity(tmp_path, include_plan=False)

        assert len(result) == 1
        assert result[0][2] is None  # plan_id is None
        # 1 call for root directory only; events.jsonl uses direct stat()
        # and include_plan=False skips all plan.md stat calls.
        assert spy.call_count == 1

    def test_scandir_root_oserror_returns_empty(self, tmp_path: Path) -> None:
        """Return [] when os.scandir on the root directory raises OSError."""
        session_dir = tmp_path / "sess-x"
        _write_events(session_dir / "events.jsonl", _START_EVENT)

        original_scandir = os.scandir

        def _bomb(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
            if str(path) == str(tmp_path):
                raise OSError("permission denied")
            return original_scandir(path)

        with patch("copilot_usage.parser.os.scandir", side_effect=_bomb):
            _, result = _discover_with_identity(tmp_path)

        assert result == []

    def test_scandir_session_dir_oserror_skips_entry(self, tmp_path: Path) -> None:
        """Skip a session directory when os.scandir on it raises OSError."""
        good = tmp_path / "sess-good"
        _write_events(good / "events.jsonl", _START_EVENT)
        bad = tmp_path / "sess-bad"
        _write_events(bad / "events.jsonl", _START_EVENT)

        original_scandir = os.scandir

        def _bomb(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
            if str(path) == str(bad):
                raise OSError("permission denied")
            return original_scandir(path)

        with patch("copilot_usage.parser.os.scandir", side_effect=_bomb):
            _, result = _discover_with_identity(tmp_path)

        assert len(result) == 1
        assert result[0][0].parent.name == "sess-good"

    def test_full_scandir_is_dir_oserror_skips_entry(self, tmp_path: Path) -> None:
        """Skip a root-level entry whose ``is_dir()`` raises ``OSError``.

        Simulates a broken symlink or ``EACCES`` on ``lstat`` by wrapping
        ``os.scandir`` so that one entry's ``is_dir()`` raises ``OSError``.
        The faulting entry must be silently skipped, not crash discovery.
        """
        good = tmp_path / "sess-good"
        _write_events(good / "events.jsonl", _START_EVENT)
        bad = tmp_path / "sess-bad"
        _write_events(bad / "events.jsonl", _START_EVENT)

        original_scandir = os.scandir

        def _patched_scandir(
            path: str | os.PathLike[str],
        ) -> Iterator[os.DirEntry[str]]:
            if str(path) != str(tmp_path):
                return original_scandir(path)  # type: ignore[return-value]

            class _WrappedCtx:
                """Context manager that wraps scandir entries."""

                def __enter__(self) -> Iterator[os.DirEntry[str]]:
                    with original_scandir(path) as it:
                        entries: list[os.DirEntry[str]] = list(it)
                    wrapped: list[os.DirEntry[str]] = []
                    for e in entries:
                        if e.name == "sess-bad":
                            m = MagicMock(spec=os.DirEntry)
                            m.name = e.name
                            m.path = e.path
                            m.is_dir.side_effect = OSError("lstat failed")
                            wrapped.append(cast(os.DirEntry[str], m))
                        else:
                            wrapped.append(e)
                    return iter(wrapped)

                def __exit__(self, *a: object) -> None:
                    pass

            return _WrappedCtx()  # type: ignore[return-value]

        with patch("copilot_usage.parser.os.scandir", side_effect=_patched_scandir):
            _, result = _discover_with_identity(tmp_path)

        assert len(result) == 1
        assert result[0][0].parent.name == "sess-good"


# ---------------------------------------------------------------------------
# _discover_with_identity — linear scan (issue #773)
# ---------------------------------------------------------------------------


class TestDiscoverWithIdentityLinearScan:
    """Verify linear scan returns correct tuples for many session dirs."""

    def test_120_sessions_correct_tuples(self, tmp_path: Path) -> None:
        """120 session dirs return correct (events_path, events_id, plan_id).

        Creates 120 session directories: 40 with plan.md (plus extra files),
        80 without. Asserts that every session is returned with the correct
        events_path and plan_id (non-None only when plan.md exists).
        """
        n_total = 120
        n_with_plan = 40

        for i in range(n_total):
            session_dir = tmp_path / f"sess-{i:04d}"
            _write_events(session_dir / "events.jsonl", _START_EVENT)
            # Add unrelated files to exercise the linear scan skip logic
            (session_dir / "debug.log").write_text("log\n", encoding="utf-8")
            (session_dir / "notes.txt").write_text("notes\n", encoding="utf-8")
            if i < n_with_plan:
                (session_dir / "plan.md").write_text(
                    f"# Session {i}\n", encoding="utf-8"
                )

        _, result = _discover_with_identity(tmp_path)

        assert len(result) == n_total

        lookup = {p.parent.name: (p, eid, pid) for p, eid, pid in result}
        for i in range(n_total):
            name = f"sess-{i:04d}"
            assert name in lookup
            events_path, events_id, plan_id = lookup[name]
            assert events_path == tmp_path / name / "events.jsonl"
            assert events_id is not None
            if i < n_with_plan:
                assert plan_id is not None
            else:
                assert plan_id is None

    def test_plan_id_none_without_plan_md(self, tmp_path: Path) -> None:
        """Sessions without plan.md get plan_id=None."""
        for i in range(5):
            _write_events(tmp_path / f"sess-{i}" / "events.jsonl", _START_EVENT)

        _, result = _discover_with_identity(tmp_path)

        assert len(result) == 5
        for _path, _eid, plan_id in result:
            assert plan_id is None

    def test_sessions_without_events_jsonl_skipped(self, tmp_path: Path) -> None:
        """Directories lacking events.jsonl are excluded from results."""
        good = tmp_path / "sess-good"
        _write_events(good / "events.jsonl", _START_EVENT)
        empty = tmp_path / "sess-empty"
        empty.mkdir()
        (empty / "plan.md").write_text("# Plan\n", encoding="utf-8")

        _, result = _discover_with_identity(tmp_path)

        assert len(result) == 1
        assert result[0][0].parent.name == "sess-good"


# ---------------------------------------------------------------------------
# _discover_with_identity — discovery cache (issue #809)
# ---------------------------------------------------------------------------


class TestDiscoverWithIdentityCache:
    """Root-directory identity caching skips inner os.scandir on repeat calls."""

    def test_second_call_skips_inner_scandir(self, tmp_path: Path) -> None:
        """When root mtime is unchanged, cached discovery avoids os.scandir.

        Creates 10 session subdirectories.  The first call to
        ``_discover_with_identity`` populates the discovery cache.  The
        second call — with an unchanged root directory — checks root
        identity via ``stat`` and reuses the cached entries list, so it
        must issue zero ``os.scandir`` calls, including for the root.
        """
        k = 10
        for i in range(k):
            _write_events(tmp_path / f"sess-{i:04d}" / "events.jsonl", _START_EVENT)

        # First call — full discovery.
        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == k

        original_scandir = os.scandir
        scandir_calls: list[str] = []

        def _tracking_scandir(
            path: str | os.PathLike[str],
        ) -> Iterator[os.DirEntry[str]]:
            scandir_calls.append(str(path))
            return original_scandir(path)

        # Second call — root unchanged, cache should be used.
        with patch("copilot_usage.parser.os.scandir", side_effect=_tracking_scandir):
            _, result2 = _discover_with_identity(tmp_path)

        assert len(result2) == k
        # No os.scandir calls at all — the cached entries list is reused.
        assert len(scandir_calls) == 0

    def test_changed_events_detected_despite_cached_discovery(
        self,
        tmp_path: Path,
    ) -> None:
        """Mutating events.jsonl is detected even when inner scandir is cached.

        After a cached second call, changing a session's events.jsonl
        must still produce a different ``events_file_id`` because the
        per-file ``stat`` call is always issued.
        """
        k = 5
        for i in range(k):
            _write_events(tmp_path / f"sess-{i}" / "events.jsonl", _START_EVENT)

        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == k
        ids1 = {p.parent.name: eid for p, eid, _ in result1}

        # Mutate one session's events.jsonl to change its file identity.
        target = tmp_path / "sess-2" / "events.jsonl"
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        _, result2 = _discover_with_identity(tmp_path)
        ids2 = {p.parent.name: eid for p, eid, _ in result2}

        assert len(result2) == k
        # The mutated session's identity must differ.
        assert ids2["sess-2"] != ids1["sess-2"]
        # Other sessions' identities remain unchanged.
        for name in ("sess-0", "sess-1", "sess-3", "sess-4"):
            assert ids2[name] == ids1[name]

    def test_new_session_triggers_rescan(self, tmp_path: Path) -> None:
        """Adding a session directory changes root mtime and triggers rescan."""
        for i in range(3):
            _write_events(tmp_path / f"sess-{i}" / "events.jsonl", _START_EVENT)

        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == 3

        # Add a new session — this changes the root directory mtime.
        _write_events(tmp_path / "sess-new" / "events.jsonl", _START_EVENT)

        _, result2 = _discover_with_identity(tmp_path)
        assert len(result2) == 4
        names = {p.parent.name for p, _, _ in result2}
        assert "sess-new" in names

    def test_cache_invalidated_when_root_changes(self, tmp_path: Path) -> None:
        """Full rescan issues inner os.scandir calls when root mtime changes."""
        for i in range(3):
            _write_events(tmp_path / f"sess-{i}" / "events.jsonl", _START_EVENT)

        # First call populates cache.
        _discover_with_identity(tmp_path)

        # Add a new session to change root mtime.
        _write_events(tmp_path / "sess-new" / "events.jsonl", _START_EVENT)

        original_scandir = os.scandir
        scandir_calls: list[str] = []

        def _tracking_scandir(
            path: str | os.PathLike[str],
        ) -> Iterator[os.DirEntry[str]]:
            scandir_calls.append(str(path))
            return original_scandir(path)

        with patch("copilot_usage.parser.os.scandir", side_effect=_tracking_scandir):
            _, result = _discover_with_identity(tmp_path)

        assert len(result) == 4
        # Full rescan: root + inner per-session calls.
        assert len(scandir_calls) >= 2

    def test_include_plan_false_then_true_returns_plan_paths(
        self,
        tmp_path: Path,
    ) -> None:
        """Cache populated via include_plan=False still has plan paths.

        The discovery cache always stores plan paths unconditionally.
        A call with ``include_plan=False`` followed by ``include_plan=True``
        must return non-None ``plan_id`` for sessions that have ``plan.md``.
        """
        sess = tmp_path / "sess-0"
        _write_events(sess / "events.jsonl", _START_EVENT)
        plan = sess / "plan.md"
        plan.write_text("# My Session\n", encoding="utf-8")

        # First call with include_plan=False — populates cache.
        _, result1 = _discover_with_identity(tmp_path, include_plan=False)
        assert len(result1) == 1
        assert result1[0][2] is None  # plan_id omitted when include_plan=False

        # Second call with include_plan=True — must use cached entries
        # but still produce a valid plan_id.
        _, result2 = _discover_with_identity(tmp_path, include_plan=True)
        assert len(result2) == 1
        assert result2[0][2] is not None  # plan_id must be present

    def test_deleted_events_jsonl_skipped_and_pruned_from_cache(
        self,
        tmp_path: Path,
    ) -> None:
        """Definitively-deleted events.jsonl is pruned from the cache.

        When ``events.jsonl`` is deleted (``FileNotFoundError``) from a
        session directory *without* changing the root directory mtime,
        the entry must be excluded from the result and pruned from the
        cached entries list so subsequent calls do not re-stat the
        missing file.
        """
        for i in range(3):
            _write_events(tmp_path / f"sess-{i}" / "events.jsonl", _START_EVENT)

        # Populate discovery cache.
        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == 3

        # Delete one session's events.jsonl without changing root mtime.
        target = tmp_path / "sess-1" / "events.jsonl"
        target.unlink()

        _, result2 = _discover_with_identity(tmp_path)
        names2 = {p.parent.name for p, _, _ in result2}
        assert len(result2) == 2
        assert "sess-1" not in names2

        # The stale entry must be pruned from the cache so a third call
        # also returns only 2 sessions (no repeated stat warnings).
        _, result3 = _discover_with_identity(tmp_path)
        assert len(result3) == 2
        assert _DISCOVERY_CACHE[tmp_path] is not None
        cached_paths = {ep for ep, _ in _DISCOVERY_CACHE[tmp_path].entries}
        assert target not in cached_paths

    def test_transient_permission_error_preserves_cache_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Transient PermissionError on events.jsonl keeps entry in cache.

        When ``events.jsonl`` raises a non-``FileNotFoundError`` OSError
        (e.g. ``PermissionError``), the entry is excluded from the
        current result but *not* pruned from the cache — so the session
        reappears once the file becomes readable again.
        """
        for i in range(3):
            _write_events(tmp_path / f"sess-{i}" / "events.jsonl", _START_EVENT)

        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == 3

        target = tmp_path / "sess-1" / "events.jsonl"
        original_stat = Path.stat

        def _permission_bomb(self: Path) -> os.stat_result:
            if self == target:
                raise PermissionError("transient")
            return original_stat(self)

        # Simulate transient PermissionError on one session.
        with patch.object(Path, "stat", _permission_bomb):
            _, result2 = _discover_with_identity(tmp_path)

        assert len(result2) == 2
        names2 = {p.parent.name for p, _, _ in result2}
        assert "sess-1" not in names2

        # Entry must still be in the cache.
        cached_paths = {ep for ep, _ in _DISCOVERY_CACHE[tmp_path].entries}
        assert target in cached_paths

        # Once readable again, the session reappears.
        _, result3 = _discover_with_identity(tmp_path)
        assert len(result3) == 3

    def test_deleted_plan_clears_cached_path(
        self,
        tmp_path: Path,
    ) -> None:
        """Deleted plan.md clears cached plan_path to avoid repeated stats.

        When a previously-present ``plan.md`` becomes unreadable, the
        cached ``plan_path`` is set to ``None`` so subsequent calls
        do not repeatedly stat a missing file.
        """
        sess = tmp_path / "sess-0"
        _write_events(sess / "events.jsonl", _START_EVENT)
        plan = sess / "plan.md"
        plan.write_text("# My Session\n", encoding="utf-8")

        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == 1
        assert result1[0][2] is not None  # plan_id present

        # Delete plan.md without changing root mtime.
        plan.unlink()

        _, result2 = _discover_with_identity(tmp_path)
        assert len(result2) == 1
        assert result2[0][2] is None  # plan_id gone

        # Cached plan_path must be cleared to None.
        cached_entries = _DISCOVERY_CACHE[tmp_path].entries
        assert cached_entries[0][1] is None

    def test_plan_md_created_after_cache_detected(
        self,
        tmp_path: Path,
    ) -> None:
        """Newly-created plan.md is detected on cache hit.

        When ``plan.md`` is created after the discovery cache is
        populated (without changing root mtime), the next call with
        ``include_plan=True`` must detect it and return a non-None
        ``plan_id``.
        """
        sess = tmp_path / "sess-0"
        _write_events(sess / "events.jsonl", _START_EVENT)

        # Populate cache — no plan.md exists.
        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == 1
        assert result1[0][2] is None  # no plan_id

        # Create plan.md without changing root mtime.
        plan = sess / "plan.md"
        plan.write_text("# My Session\n", encoding="utf-8")

        # Cache hit must detect the new plan.md.
        _, result2 = _discover_with_identity(tmp_path)
        assert len(result2) == 1
        assert result2[0][2] is not None  # plan_id detected

        # Cached entry must now include the plan path.
        cached_entries = _DISCOVERY_CACHE[tmp_path].entries
        assert cached_entries[0][1] == plan

    def test_plan_probe_rotates_across_cache_hits(
        self,
        tmp_path: Path,
    ) -> None:
        """plan.md created beyond initial probe budget is eventually detected.

        Creates more sessions than ``_MAX_PLAN_PROBES`` without any
        ``plan.md``.  A ``plan.md`` is then created in a session that
        sits beyond the first probe window.  After enough cache-hit
        calls for the rotating cursor to reach the target session, the
        new ``plan.md`` must be detected.
        """
        n = _MAX_PLAN_PROBES * 3
        for i in range(n):
            _write_events(tmp_path / f"sess-{i:04d}" / "events.jsonl", _START_EVENT)

        # Populate cache — no plan.md in any session.
        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == n
        for _, _, pid in result1:
            assert pid is None

        # Create plan.md in a session that is beyond the first probe
        # window.  Which index it lands on depends on entry order, so
        # target the *last* entry in the cached list to maximise the
        # distance from any starting cursor.
        cached = _DISCOVERY_CACHE[tmp_path]
        last_events_path = cached.entries[-1][0]
        target_dir = last_events_path.parent
        plan = target_dir / "plan.md"
        plan.write_text("# Late Session\n", encoding="utf-8")

        # Call repeatedly — the rotating cursor should eventually reach
        # the target entry.  ceil(n / _MAX_PLAN_PROBES) calls suffice.
        max_calls = (n + _MAX_PLAN_PROBES - 1) // _MAX_PLAN_PROBES
        detected = False
        for _ in range(max_calls):
            _, result = _discover_with_identity(tmp_path)
            for path, _, pid in result:
                if path == last_events_path and pid is not None:
                    detected = True
                    break
            if detected:
                break

        assert detected, (
            f"plan.md in {target_dir.name} not detected after "
            f"{max_calls} cache-hit calls"
        )


# ---------------------------------------------------------------------------
# _discover_with_identity — no_plan_indices probe-loop skip (issue #823)
# ---------------------------------------------------------------------------


class TestDiscoverWithIdentityNoPlanCount:
    """Probe loop is O(1) when all sessions already have plan.md."""

    def test_probe_loop_skipped_when_all_have_plan(
        self,
        tmp_path: Path,
    ) -> None:
        """When all entries have plan.md, no_plan_indices is empty and probe loop is skipped.

        Creates 200 sessions each with ``plan.md``.  After the first
        (cache-populating) call, ``no_plan_indices`` must be empty and a
        second (cache-hit) call must not enter the probe-window scan —
        verified by patching ``builtins.range`` and asserting the
        probe-loop ``range(n)`` call is never made.
        """
        n = 200
        for i in range(n):
            sess = tmp_path / f"sess-{i:04d}"
            _write_events(sess / "events.jsonl", _START_EVENT)
            (sess / "plan.md").write_text(f"# Session {i}\n", encoding="utf-8")

        # First call — populates the cache.
        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == n

        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == 0

        # Second call — cache hit.  Spy on builtins.range to verify
        # the probe-window scan is never entered (the only range(n)
        # call inside the probe block).
        original_range = builtins.range
        probe_range_calls: list[int] = []

        def _spy_range(*args: int) -> range:
            # The probe loop calls range(n) where n == len(entries).
            if len(args) == 1 and args[0] == n:
                probe_range_calls.append(args[0])
            return original_range(*args)

        with patch("builtins.range", side_effect=_spy_range):
            _, result2 = _discover_with_identity(tmp_path)

        assert len(result2) == n
        assert probe_range_calls == [], (
            f"probe-window range({n}) was called {len(probe_range_calls)} "
            f"time(s) despite no_plan_indices being empty"
        )

    def test_no_plan_indices_set_on_fresh_discovery(
        self,
        tmp_path: Path,
    ) -> None:
        """no_plan_indices length matches the number of entries without plan.md."""
        n_total = 10
        n_with_plan = 4
        for i in range(n_total):
            sess = tmp_path / f"sess-{i}"
            _write_events(sess / "events.jsonl", _START_EVENT)
            if i < n_with_plan:
                (sess / "plan.md").write_text(f"# S{i}\n", encoding="utf-8")

        _discover_with_identity(tmp_path)

        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == n_total - n_with_plan

    def test_no_plan_indices_shrinks_on_probe_success(
        self,
        tmp_path: Path,
    ) -> None:
        """no_plan_indices shrinks when a probe discovers a new plan.md."""
        sess = tmp_path / "sess-0"
        _write_events(sess / "events.jsonl", _START_EVENT)

        # Populate cache — no plan.md.
        _discover_with_identity(tmp_path)
        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == 1

        # Create plan.md, trigger cache-hit probe.
        (sess / "plan.md").write_text("# New Plan\n", encoding="utf-8")
        _discover_with_identity(tmp_path)

        assert len(cached.no_plan_indices) == 0

    def test_no_plan_indices_grows_on_plan_deletion(
        self,
        tmp_path: Path,
    ) -> None:
        """no_plan_indices grows when a previously-present plan.md is deleted."""
        sess = tmp_path / "sess-0"
        _write_events(sess / "events.jsonl", _START_EVENT)
        plan = sess / "plan.md"
        plan.write_text("# My Plan\n", encoding="utf-8")

        # Populate cache — plan.md present.
        _discover_with_identity(tmp_path)
        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == 0

        # Delete plan.md, trigger cache-hit call.
        plan.unlink()
        _discover_with_identity(tmp_path)

        assert len(cached.no_plan_indices) == 1

    def test_no_plan_indices_adjusted_on_prune(
        self,
        tmp_path: Path,
    ) -> None:
        """no_plan_indices is rebuilt when entries are pruned.

        Deleting ``events.jsonl`` for a session without ``plan.md``
        must remove its index from ``no_plan_indices`` when the entry
        is pruned.
        """
        for i in range(3):
            sess = tmp_path / f"sess-{i}"
            _write_events(sess / "events.jsonl", _START_EVENT)
        # Give only sess-0 a plan.
        (tmp_path / "sess-0" / "plan.md").write_text("# P\n", encoding="utf-8")

        _discover_with_identity(tmp_path)
        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == 2

        # Delete events.jsonl for a no-plan session.
        (tmp_path / "sess-1" / "events.jsonl").unlink()
        _discover_with_identity(tmp_path)

        assert len(cached.no_plan_indices) == 1


# ---------------------------------------------------------------------------
# _discover_with_identity — O(_MAX_PLAN_PROBES) probe scan (issue #843)
# ---------------------------------------------------------------------------


class TestDiscoverProbeWindowBounded:
    """Probe-window scan is O(_MAX_PLAN_PROBES), not O(n_sessions)."""

    def test_probe_scan_bounded_and_cursor_advances(
        self,
        tmp_path: Path,
    ) -> None:
        """Probe scan accesses ≤ _MAX_PLAN_PROBES entries per call.

        Populates ``_DISCOVERY_CACHE`` with **n = 500** fake session
        entries where **k = 8 > _MAX_PLAN_PROBES** have
        ``plan_path = None``.  A cache-hit call must access at most
        ``_MAX_PLAN_PROBES`` indices during the probe scan — verified
        by wrapping ``no_plan_indices.__getitem__`` to count accesses.
        A second call must advance the cursor past the previously-probed
        entries instead of resetting to 0.
        """
        n = 500
        k = 8  # k > _MAX_PLAN_PROBES so cursor rotation is observable

        # Build sessions on disk for the initial (cache-populating) call.
        for i in range(n):
            sess = tmp_path / f"sess-{i:04d}"
            _write_events(sess / "events.jsonl", _START_EVENT)
            if i >= k:
                (sess / "plan.md").write_text(f"# Session {i}\n", encoding="utf-8")

        # Populate cache.
        _, result1 = _discover_with_identity(tmp_path)
        assert len(result1) == n

        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == k

        # Wrap no_plan_indices with a spy to count __getitem__ accesses.
        original_no_plan_indices = list(cached.no_plan_indices)

        class _SpyList(list[int]):
            """List subclass that counts indexed access to no-plan indices."""

            def __init__(self, data: list[int]) -> None:
                super().__init__(data)
                self.no_plan_accesses: int = 0

            @overload
            def __getitem__(self, index: SupportsIndex, /) -> int: ...
            @overload
            def __getitem__(self, index: slice, /) -> list[int]: ...
            def __getitem__(
                self,
                index: SupportsIndex | slice,
                /,
            ) -> int | list[int]:
                if not isinstance(index, slice):
                    self.no_plan_accesses += 1
                return super().__getitem__(index)

        spy = _SpyList(original_no_plan_indices)
        cached.no_plan_indices = spy  # type: ignore[assignment]

        # Record the cursor before the first cache-hit call.
        cursor_before = cached.probe_cursor

        # First cache-hit call — verify bounded probe scan.
        _, result2 = _discover_with_identity(tmp_path)
        assert len(result2) == n

        # The probe scan should access exactly the bounded probe window.
        max_allowed = min(_MAX_PLAN_PROBES, k)
        assert spy.no_plan_accesses == max_allowed, (
            f"probe scan accessed {spy.no_plan_accesses} no-plan indices, "
            f"expected exactly {max_allowed} "
            f"(_MAX_PLAN_PROBES={_MAX_PLAN_PROBES}, k={k})"
        )

        # Cursor must have advanced past the probed entries, not reset to 0.
        cursor_after_first = cached.probe_cursor
        probe_count = min(_MAX_PLAN_PROBES, k)
        expected_cursor = (cursor_before + probe_count) % k
        assert cursor_after_first == expected_cursor, (
            f"probe_cursor should be {expected_cursor} after probing "
            f"{probe_count} of {k} no-plan entries, got {cursor_after_first}"
        )

        # Reset spy counter for second call.
        spy.no_plan_accesses = 0

        # Second cache-hit call — cursor must advance again, not wrap to 0.
        _, result3 = _discover_with_identity(tmp_path)
        assert len(result3) == n

        assert spy.no_plan_accesses == max_allowed

        cursor_after_second = cached.probe_cursor
        expected_cursor_2 = (cursor_after_first + probe_count) % k
        assert cursor_after_second == expected_cursor_2, (
            f"probe_cursor should be {expected_cursor_2} after second call, "
            f"got {cursor_after_second} (cursor did not advance)"
        )

        # Restore original no_plan_indices.
        cached.no_plan_indices = original_no_plan_indices

    def test_no_plan_indices_directly_indexed(
        self,
        tmp_path: Path,
    ) -> None:
        """Probe scan uses no_plan_indices for O(k) lookups, not O(n) iteration.

        Uses a spy list subclass to count ``__getitem__`` accesses on
        the *no_plan_indices* list during a cache-hit call.  With
        ``n = 500`` sessions and ``k = 2`` without ``plan.md``, the
        probe scan must access at most ``min(_MAX_PLAN_PROBES, k)``
        indices in ``no_plan_indices``.
        """
        n = 500
        k = 2

        for i in range(n):
            sess = tmp_path / f"sess-{i:04d}"
            _write_events(sess / "events.jsonl", _START_EVENT)
            if i >= k:
                (sess / "plan.md").write_text(f"# Session {i}\n", encoding="utf-8")

        # Populate cache.
        _discover_with_identity(tmp_path)
        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == k

        original_no_plan_indices = list(cached.no_plan_indices)

        class _SpyList(list[int]):
            """List subclass that counts indexed access to no-plan indices."""

            def __init__(self, data: list[int]) -> None:
                super().__init__(data)
                self.no_plan_accesses: int = 0

            @overload
            def __getitem__(self, index: SupportsIndex, /) -> int: ...
            @overload
            def __getitem__(self, index: slice, /) -> list[int]: ...
            def __getitem__(
                self,
                index: SupportsIndex | slice,
                /,
            ) -> int | list[int]:
                if not isinstance(index, slice):
                    self.no_plan_accesses += 1
                return super().__getitem__(index)

        spy = _SpyList(original_no_plan_indices)
        cached.no_plan_indices = spy  # type: ignore[assignment]

        # Cache-hit call.
        _, result = _discover_with_identity(tmp_path)
        assert len(result) == n

        # The probe scan should access exactly the bounded prefix of
        # no_plan_indices used to build probe_indices.
        max_allowed = min(_MAX_PLAN_PROBES, k)
        assert max_allowed > 0
        assert spy.no_plan_accesses == max_allowed, (
            f"probe scan accessed {spy.no_plan_accesses} no-plan indices, "
            f"expected exactly {max_allowed} (_MAX_PLAN_PROBES={_MAX_PLAN_PROBES}, k={k})"
        )

        # Restore original no_plan_indices to avoid side effects.
        cached.no_plan_indices = original_no_plan_indices

    def test_no_plan_indices_rebuilt_on_remove_invariant_violation(
        self,
        tmp_path: Path,
    ) -> None:
        """except-ValueError recovery rebuilds no_plan_indices from entries.

        Simulates an invariant violation where ``no_plan_indices.remove(idx)``
        raises ``ValueError`` (idx not in list).  The recovery path must
        rebuild ``no_plan_indices`` from ``entries`` so subsequent calls
        stay correct.
        """
        sess = tmp_path / "sess-0"
        _write_events(sess / "events.jsonl", _START_EVENT)

        # Populate cache — no plan.md.
        _discover_with_identity(tmp_path)
        cached = _DISCOVERY_CACHE[tmp_path]
        assert len(cached.no_plan_indices) == 1

        # Create plan.md so the probe will succeed.
        (sess / "plan.md").write_text("# Plan\n", encoding="utf-8")

        # Inject a list subclass whose remove() always raises ValueError,
        # simulating a stale/inconsistent no_plan_indices state.
        original_idx = cached.no_plan_indices[0]

        class _FaultyRemoveList(list[int]):
            """List that raises ValueError on remove to trigger recovery."""

            @overload
            def __getitem__(self, index: SupportsIndex, /) -> int: ...
            @overload
            def __getitem__(self, index: slice, /) -> list[int]: ...
            def __getitem__(
                self,
                index: SupportsIndex | slice,
                /,
            ) -> int | list[int]:
                return super().__getitem__(index)

            def remove(self, value: int, /) -> None:
                raise ValueError("forced invariant violation")

        faulty = _FaultyRemoveList([original_idx])
        cached.no_plan_indices = faulty  # type: ignore[assignment]

        # Cache-hit call: probe finds plan.md, remove() raises ValueError,
        # recovery path rebuilds no_plan_indices from entries.
        _, result = _discover_with_identity(tmp_path)
        assert len(result) == 1

        # After recovery, no_plan_indices must reflect actual entry state.
        # The plan was found, so entries[0] now has a plan_path and
        # no_plan_indices should be empty (all entries have plan.md).
        assert cached.no_plan_indices == []


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

    def test_validation_error_non_json_invalid_logs_warning(
        self, tmp_path: Path
    ) -> None:
        """Valid JSON failing Pydantic validation emits a validation-error warning."""
        bad_event = json.dumps({"no_type_field": True})
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, bad_event)
        try:
            SessionEvent.model_validate_json(bad_event)
        except ValidationError as exc:
            expected_error_count = exc.error_count()
        else:
            pytest.fail("Expected bad_event to raise ValidationError")
        with patch.object(_parser_module.logger, "warning") as warning_spy:
            events = parse_events(p)
        assert len(events) == 1  # bad event skipped
        warning_spy.assert_called_once()
        assert "validation error" in warning_spy.call_args.args[0].lower()
        # error_count() must be passed as positional arg after format string
        assert warning_spy.call_args.args[3] == expected_error_count

    def test_validation_error_warning_includes_file_and_lineno(
        self, tmp_path: Path
    ) -> None:
        """Validation-error warning includes the file path and line number."""
        bad_event = json.dumps({"no_type_field": True})
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, bad_event)
        with patch.object(_parser_module.logger, "warning") as warning_spy:
            parse_events(p)
        warning_spy.assert_called_once()
        args = warning_spy.call_args.args
        assert args[1] == p  # file path
        assert args[2] == 2  # line number (bad event is the second line)

    def test_multiple_validation_errors_each_warned(self, tmp_path: Path) -> None:
        """Two bad lines emit two separate warnings with correct line numbers."""
        bad1 = json.dumps({"no_type_field": True})
        bad2 = json.dumps({"also_invalid": 42})
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, bad1, bad2)
        with patch.object(_parser_module.logger, "warning") as warning_spy:
            events = parse_events(p)
        assert len(events) == 1  # only the start event survives
        assert warning_spy.call_count == 2
        # Each warning should reference its own line number
        line_numbers = [call.args[2] for call in warning_spy.call_args_list]
        assert line_numbers == [2, 3]

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


class TestParseEventsModelValidateJson:
    """Verify parse_events uses model_validate_json (fast path)."""

    def test_model_validate_json_called_per_valid_line(self, tmp_path: Path) -> None:
        """model_validate_json is called once per valid event line."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)
        with patch.object(
            SessionEvent, "model_validate_json", wraps=SessionEvent.model_validate_json
        ) as spy:
            events = parse_events(p)
        assert spy.call_count == 3
        assert len(events) == 3

    def test_model_validate_not_called(self, tmp_path: Path) -> None:
        """The old slow path (model_validate with a dict) is not used."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG)
        with patch.object(
            SessionEvent, "model_validate", wraps=SessionEvent.model_validate
        ) as spy:
            parse_events(p)
        assert spy.call_count == 0

    def test_malformed_json_skipped_with_warning(self, tmp_path: Path) -> None:
        """Malformed JSON lines are skipped; valid lines still parse."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, "NOT-JSON{{{", _USER_MSG)
        with patch.object(_parser_module.logger, "warning") as warning_spy:
            events = parse_events(p)
        assert len(events) == 2
        assert events[0].type == EventType.SESSION_START
        assert events[1].type == EventType.USER_MESSAGE
        warning_spy.assert_called_once()
        assert "json" in warning_spy.call_args.args[0].lower()


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

    def test_active_session_no_assistant_turn_empty_model_metrics(
        self, tmp_path: Path
    ) -> None:
        """Session with only session.start → model_metrics == {}, zero tokens."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT)
        events = parse_events(p)
        summary = build_session_summary(events, session_dir=p.parent)

        assert summary.model_metrics == {}
        assert summary.active_output_tokens == 0
        assert summary.total_premium_requests == 0
        assert summary.is_active is True


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
    """Verify code-change aggregation skips None cycles without losing data."""

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


class TestMultiShutdownCodeChangesAggregation:
    """Verify CodeChanges are aggregated (summed lines, union of files)
    across all shutdown cycles instead of keeping only the last."""

    def test_lines_summed_and_files_unioned_across_two_shutdowns(
        self, tmp_path: Path
    ) -> None:
        """Two shutdowns with distinct codeChanges → sum of lines, union of files."""
        shutdown_1 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 3,
                    "totalApiDurationMs": 2000,
                    "sessionStartTime": 0,
                    "codeChanges": {
                        "linesAdded": 40,
                        "linesRemoved": 5,
                        "filesModified": ["a.py", "b.py"],
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
        shutdown_2 = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "shutdownType": "routine",
                    "totalPremiumRequests": 5,
                    "totalApiDurationMs": 4000,
                    "sessionStartTime": 0,
                    "codeChanges": {
                        "linesAdded": 30,
                        "linesRemoved": 10,
                        "filesModified": ["b.py", "c.py"],
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
            shutdown_1,
            resume_ev,
            _USER_MSG,
            shutdown_2,
        )
        events = parse_events(p)
        summary = build_session_summary(events)

        assert summary.code_changes is not None
        assert summary.code_changes.linesAdded == 70  # 40 + 30
        assert summary.code_changes.linesRemoved == 15  # 5 + 10
        # Union of files, sorted alphabetically
        assert summary.code_changes.filesModified == ["a.py", "b.py", "c.py"]


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

    def test_tool_requests_default_factory_isolation(self) -> None:
        """Two instances must not share the same toolRequests list object."""
        a = AssistantMessageData()
        b = AssistantMessageData()
        assert a.toolRequests is not b.toolRequests

    def test_nonempty_tool_requests_round_trip(self) -> None:
        raw = {
            "messageId": "msg-1",
            "content": "Let me ask you something.",
            "outputTokens": 205,
            "interactionId": "int-1",
            "toolRequests": [
                {
                    "toolCallId": "toolu_vrtx_01BcqbS8Lv6dReRDKqt2SKaD",
                    "name": "ask_user",
                    "arguments": {"question": "Which framework?"},
                    "type": "function",
                },
                {
                    "toolCallId": "toolu_vrtx_01ThGGHY1fU4YkDoNkgyhxJg",
                    "name": "report_intent",
                    "arguments": {"intent": "Planning"},
                    "type": "function",
                },
            ],
        }
        d = AssistantMessageData.model_validate(raw)
        assert len(d.toolRequests) == 2
        first = d.toolRequests[0]
        assert isinstance(first, ToolRequest)
        assert first.toolCallId == "toolu_vrtx_01BcqbS8Lv6dReRDKqt2SKaD"
        assert first.name == "ask_user"
        assert first.type == "function"
        assert first.arguments == {"question": "Which framework?"}
        second = d.toolRequests[1]
        assert second.name == "report_intent"
        assert second.arguments == {"intent": "Planning"}

        # Complete the round-trip: dump back to dict and re-validate
        dumped = d.model_dump()
        d2 = AssistantMessageData.model_validate(dumped)
        assert d2 == d
        assert d2.toolRequests[0].toolCallId == first.toolCallId
        assert d2.toolRequests[1].arguments == second.arguments

    def test_tool_request_defaults(self) -> None:
        tr = ToolRequest()
        assert tr.toolCallId == ""
        assert tr.name == ""
        assert tr.arguments == {}
        assert tr.type == ""

    def test_tool_request_all_known_fields(self) -> None:
        tr = ToolRequest.model_validate(
            {
                "toolCallId": "toolu_vrtx_01CfcEZckzE3qUrR6k97KG5X",
                "name": "ask_user",
                "arguments": {
                    "question": "Pick one",
                    "choices": ["A", "B"],
                    "allow_freeform": True,
                },
                "type": "function",
            }
        )
        assert tr.toolCallId == "toolu_vrtx_01CfcEZckzE3qUrR6k97KG5X"
        assert tr.name == "ask_user"
        assert tr.type == "function"
        assert tr.arguments["choices"] == ["A", "B"]
        assert tr.arguments["allow_freeform"] is True

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
# SessionEvent.as_*() — typed accessors
# ---------------------------------------------------------------------------


class TestSessionEventTypedAccessors:
    def test_as_session_start(self) -> None:
        ev = SessionEvent(
            type="session.start",
            data={"sessionId": "s1", "version": 1, "context": {}},
        )
        result = ev.as_session_start()
        assert isinstance(result, SessionStartData)
        assert result.sessionId == "s1"

    def test_as_assistant_message(self) -> None:
        ev = SessionEvent(
            type="assistant.message",
            data={"messageId": "m1", "content": "hi", "outputTokens": 10},
        )
        result = ev.as_assistant_message()
        assert isinstance(result, AssistantMessageData)
        assert result.outputTokens == 10

    def test_as_session_shutdown(self) -> None:
        ev = SessionEvent(
            type="session.shutdown",
            data={"shutdownType": "routine", "totalPremiumRequests": 3},
        )
        result = ev.as_session_shutdown()
        assert isinstance(result, SessionShutdownData)
        assert result.totalPremiumRequests == 3

    def test_as_tool_execution(self) -> None:
        ev = SessionEvent(
            type="tool.execution_complete",
            data={"toolCallId": "tc-1", "model": "gpt-4", "success": True},
        )
        result = ev.as_tool_execution()
        assert isinstance(result, ToolExecutionData)
        assert result.model == "gpt-4"

    def test_as_user_message(self) -> None:
        ev = SessionEvent(
            type="user.message",
            data={"content": "hello"},
        )
        result = ev.as_user_message()
        assert isinstance(result, UserMessageData)
        assert result.content == "hello"

    def test_as_wrong_type_raises_value_error(self) -> None:
        ev = SessionEvent(
            type="some.unknown.event",
            data={"arbitrary": "data", "count": 42},
        )
        with pytest.raises(ValueError, match="Expected session.start"):
            ev.as_session_start()

    def test_as_abort_raises_value_error(self) -> None:
        ev = SessionEvent(type="abort", data={"reason": "user"})
        with pytest.raises(ValueError, match="Expected session.shutdown"):
            ev.as_session_shutdown()


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
        """Mix of valid (150), boolean (mapped to 0), and negative (-50) outputTokens → 150."""
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
        # 150 (valid) + 0 (bool mapped to 0) + 0 (negative rejected) = 150
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

    def test_tool_execution_complete_bad_data_silently_skipped(
        self, tmp_path: Path
    ) -> None:
        """Bad tool.execution_complete data → silently skipped (no Pydantic validation).

        The optimised path reads ``ev.data.get("model")`` directly; non-string
        or missing values are ignored without raising or logging.
        """
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
            summary = build_session_summary(
                events, config_path=tmp_path / "no-config.json"
            )
        finally:
            logger.remove(handler_id)

        # Malformed event is harmlessly ignored; summary still builds.
        assert summary is not None
        assert not any(
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
        """Confirm that only the first line of a large file is read via a single readline().

        Wraps ``Path.open`` with a spy file handle that tracks bytes returned from
        ``readline()`` and raises on ``read()`` / ``readlines()``. Also patches
        ``read_text`` as a belt-and-suspenders guard to ensure no whole-file reads.
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

    def test_extract_session_name_plan_exists_false_skips_filesystem(
        self, tmp_path: Path
    ) -> None:
        """When ``plan_exists=False``, no filesystem check is performed."""
        # plan.md exists on disk, but plan_exists=False signals it was
        # absent at discovery time → _extract_session_name must return
        # None without calling is_file().
        plan = tmp_path / "plan.md"
        plan.write_text("# Should Not Be Read\n", encoding="utf-8")

        original_is_file = Path.is_file

        def _no_is_file(self: Path) -> bool:
            if self == plan:
                raise AssertionError("is_file() must not be called on plan.md")
            return original_is_file(self)

        with patch.object(Path, "is_file", _no_is_file):
            result = _extract_session_name(tmp_path, plan_exists=False)

        assert result is None

    def test_extract_session_name_plan_exists_true_skips_is_file(
        self, tmp_path: Path
    ) -> None:
        """When ``plan_exists=True``, ``is_file()`` is skipped."""
        plan = tmp_path / "plan.md"
        plan.write_text("# My Session\n", encoding="utf-8")

        original_is_file = Path.is_file

        def _no_is_file(self: Path) -> bool:
            if self == plan:
                raise AssertionError("is_file() must not be called on plan.md")
            return original_is_file(self)

        with patch.object(Path, "is_file", _no_is_file):
            result = _extract_session_name(tmp_path, plan_exists=True)

        assert result == "My Session"

    def test_get_all_sessions_stats_plan_at_most_once(self, tmp_path: Path) -> None:
        """``plan.md`` is stat'd at most once per session on cold start."""
        session_dir = tmp_path / "sess-a"
        _write_events(session_dir / "events.jsonl", _START_EVENT, _USER_MSG)
        plan = session_dir / "plan.md"
        plan.write_text("# Test Plan\n", encoding="utf-8")

        original_stat = Path.stat
        plan_stat_count = 0

        def _counting_stat(self: Path, **kwargs: object) -> object:
            nonlocal plan_stat_count
            if self == plan:
                plan_stat_count += 1
            return original_stat(self, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "stat", _counting_stat):
            summaries = get_all_sessions(tmp_path)

        assert len(summaries) == 1
        assert summaries[0].name == "Test Plan"
        assert plan_stat_count == 1, (
            f"plan.md was stat'd {plan_stat_count} times, expected at most 1"
        )

    def test_read_config_model_empty_string_returns_none(self, tmp_path: Path) -> None:
        config = tmp_path / "config.json"
        config.write_text('{"model": ""}', encoding="utf-8")
        assert _read_config_model(config) is None

    def test_output_tokens_boolean_true_sanitized(self, tmp_path: Path) -> None:
        """Boolean True is mapped to 0 by the field validator and not counted."""
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
# _extract_output_tokens
# ---------------------------------------------------------------------------


def _make_assistant_event(output_tokens: object) -> SessionEvent:
    """Build a minimal ``assistant.message`` SessionEvent with the given outputTokens."""
    return SessionEvent(
        type=EventType.ASSISTANT_MESSAGE,
        data={
            "messageId": "m1",
            "content": "hi",
            "outputTokens": output_tokens,
        },
        id="ev-test",
        timestamp=datetime(2026, 3, 7, 10, 1, tzinfo=UTC),
    )


class TestExtractOutputTokens:
    """Unit tests for _extract_output_tokens."""

    def test_returns_int_for_genuine_int(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(42)) == 42

    def test_coerces_whole_float_to_int(self) -> None:
        """Whole-number floats like ``1234.0`` are coerced to int."""
        assert _extract_output_tokens(_make_assistant_event(1234.0)) == 1234

    def test_returns_none_for_fractional_float(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(3.14)) is None

    def test_returns_none_for_bool_true(self) -> None:
        """Boolean True is treated as invalid, not coerced to 1."""
        assert _extract_output_tokens(_make_assistant_event(True)) is None

    def test_returns_none_for_bool_false(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(False)) is None

    def test_returns_none_for_none(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(None)) is None

    def test_returns_none_for_string(self) -> None:
        """Strings are treated as invalid, even numeric ones."""
        assert _extract_output_tokens(_make_assistant_event("abc")) is None

    def test_returns_none_for_numeric_string(self) -> None:
        """Numeric strings like ``"100"`` are treated as invalid, not coerced to int."""
        assert _extract_output_tokens(_make_assistant_event("100")) is None

    def test_returns_none_for_zero(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(0)) is None

    def test_returns_none_for_negative_int(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(-1)) is None

    def test_returns_none_for_large_negative(self) -> None:
        assert _extract_output_tokens(_make_assistant_event(-100_000)) is None

    def test_returns_none_for_malformed_data(self) -> None:
        """An event with completely invalid data yields None."""
        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={"unexpected": "payload", "outputTokens": "not-a-number"},
        )
        assert _extract_output_tokens(ev) is None

    def test_returns_none_for_missing_key(self) -> None:
        """An event with no outputTokens key yields None."""
        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={"messageId": "m1", "content": "hi"},
            id="ev-test",
            timestamp=datetime(2026, 3, 7, 10, 1, tzinfo=UTC),
        )
        assert _extract_output_tokens(ev) is None


_EXTRACT_TOKENS_CASES: list[tuple[str, object, int | None]] = [
    ("valid_int", 42, 42),
    ("whole_float", 1234.0, 1234),
    ("bool_true", True, None),
    ("str_numeric", "100", None),
    ("fractional_float", 1.5, None),
    ("none_value", None, None),
]


class TestExtractOutputTokensParametrized:
    """Parametrized tests ensuring _extract_output_tokens matches field-validator behaviour."""

    @pytest.mark.parametrize(
        ("label", "raw_value", "expected"),
        _EXTRACT_TOKENS_CASES,
        ids=[c[0] for c in _EXTRACT_TOKENS_CASES],
    )
    def test_output_matches_expected(
        self, label: str, raw_value: object, expected: int | None
    ) -> None:
        """Each raw value produces the expected result without Pydantic."""
        _ = label  # used only as test-case id
        assert _extract_output_tokens(_make_assistant_event(raw_value)) == expected

    @pytest.mark.parametrize(
        ("label", "raw_value", "expected"),
        _EXTRACT_TOKENS_CASES,
        ids=[c[0] for c in _EXTRACT_TOKENS_CASES],
    )
    def test_no_pydantic_model_validate_called(
        self, label: str, raw_value: object, expected: int | None
    ) -> None:
        """AssistantMessageData.model_validate must NOT be called."""
        _ = label, expected  # used only as test-case id / reference
        with patch.object(
            AssistantMessageData, "model_validate", side_effect=AssertionError
        ):
            _extract_output_tokens(_make_assistant_event(raw_value))

    def test_missing_key_no_pydantic(self) -> None:
        """Missing outputTokens key returns None without Pydantic."""
        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={"messageId": "m1", "content": "hi"},
            id="ev-test",
            timestamp=datetime(2026, 3, 7, 10, 1, tzinfo=UTC),
        )
        with patch.object(
            AssistantMessageData, "model_validate", side_effect=AssertionError
        ):
            assert _extract_output_tokens(ev) is None


# ---------------------------------------------------------------------------
# Cross-check: _extract_output_tokens vs AssistantMessageData equivalence
# ---------------------------------------------------------------------------

_EQUIVALENCE_CASES: list[tuple[str, object]] = [
    ("bool_true", True),
    ("bool_false", False),
    ("str_numeric", "100"),
    ("str_alpha", "abc"),
    ("zero", 0),
    ("zero_float", 0.0),
    ("negative", -1),
    ("negative_float", -1.0),
    ("positive_int", 1),
    ("large_positive_int", 1234),
    ("whole_float", 1234.0),
    ("fractional_float", 3.14),
]


class TestExtractOutputTokensEquivalence:
    """Both token-extraction paths must agree on whether a value contributes positive tokens."""

    @pytest.mark.parametrize(
        ("label", "raw_value"),
        _EQUIVALENCE_CASES,
        ids=[c[0] for c in _EQUIVALENCE_CASES],
    )
    def test_positive_contribution_agreement(
        self, label: str, raw_value: object
    ) -> None:
        """For each input, both paths agree on whether the value contributes a positive token count."""
        _ = label
        fast_path_result = _extract_output_tokens(_make_assistant_event(raw_value))

        # Pydantic rejects non-whole floats with a ValidationError; the model
        # path treats those as non-contributing, same as the fast path.
        try:
            model = AssistantMessageData.model_validate({"outputTokens": raw_value})
            model_contributes = model.outputTokens > 0
            model_result = repr(model.outputTokens)
        except ValidationError as exc:
            model_contributes = False
            model_result = f"ValidationError({exc})"

        fast_path_contributes = fast_path_result is not None
        assert fast_path_contributes == model_contributes, (
            f"Divergence for {raw_value!r}: "
            f"_extract_output_tokens → {fast_path_result}, "
            f"AssistantMessageData.outputTokens → {model_result}"
        )


class TestExtractOutputTokensIntegration:
    """Integration tests exercising _extract_output_tokens through parse → build_session_summary."""

    def test_whole_float_output_tokens_counted_active_session(
        self, tmp_path: Path
    ) -> None:
        """An assistant.message with outputTokens=1234.0 must be coerced and counted."""
        float_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "hi",
                    "outputTokens": 1234.0,
                },
                "id": "ev-float",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, float_msg, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 1234

    def test_fractional_float_output_tokens_ignored_active_session(
        self, tmp_path: Path
    ) -> None:
        """An assistant.message with outputTokens=1.5 must not add to active_output_tokens."""
        float_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "hi",
                    "outputTokens": 1.5,
                },
                "id": "ev-float",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, float_msg, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 0

    def test_null_output_tokens_ignored_active_session(self, tmp_path: Path) -> None:
        """An assistant.message with outputTokens=null must not add to active_output_tokens."""
        null_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "hi",
                    "outputTokens": None,
                },
                "id": "ev-null",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, null_msg, _TOOL_EXEC)
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 0

    def test_whole_float_output_tokens_counted_post_shutdown(
        self, tmp_path: Path
    ) -> None:
        """A post-shutdown assistant.message with outputTokens=500.0 must be coerced and counted."""
        post_resume_float_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-float-post",
                    "content": "resuming with float",
                    "toolRequests": [],
                    "interactionId": "int-2",
                    "outputTokens": 500.0,
                },
                "id": "ev-asst-float-post",
                "timestamp": "2026-03-07T12:01:05.000Z",
                "parentId": "ev-user2",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            _RESUME_EVENT,
            _POST_RESUME_USER_MSG,
            post_resume_float_msg,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        # active_output_tokens only counts post-shutdown tokens
        assert summary.active_output_tokens == 500
        assert summary.is_active is True

    def test_fractional_float_output_tokens_ignored_post_shutdown(
        self, tmp_path: Path
    ) -> None:
        """A post-shutdown assistant.message with outputTokens=2.9 must not add to active_output_tokens."""
        post_resume_float_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-float-post",
                    "content": "resuming with float",
                    "toolRequests": [],
                    "interactionId": "int-2",
                    "outputTokens": 2.9,
                },
                "id": "ev-asst-float-post",
                "timestamp": "2026-03-07T12:01:05.000Z",
                "parentId": "ev-user2",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            _SHUTDOWN_EVENT,
            _RESUME_EVENT,
            _POST_RESUME_USER_MSG,
            post_resume_float_msg,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        # active_output_tokens only counts post-shutdown tokens; 2.9 is rejected
        assert summary.active_output_tokens == 0
        assert summary.is_active is True

    def test_mixed_valid_float_null_tokens(self, tmp_path: Path) -> None:
        """valid=150, float=1.5, null → active_output_tokens == 150."""
        float_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-float",
                    "content": "float tokens",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": 1.5,
                },
                "id": "ev-float-mix",
                "timestamp": "2026-03-07T10:01:06.000Z",
                "parentId": "ev-user1",
            }
        )
        null_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "msg-null",
                    "content": "null tokens",
                    "toolRequests": [],
                    "interactionId": "int-1",
                    "outputTokens": None,
                },
                "id": "ev-null-mix",
                "timestamp": "2026-03-07T10:01:07.000Z",
                "parentId": "ev-user1",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            _ASSISTANT_MSG,
            float_msg,
            null_msg,
            _TOOL_EXEC,
        )
        events = parse_events(p)
        summary = build_session_summary(events)
        assert summary.active_output_tokens == 150


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
# Issue #598 — _build_completed_summary uses O(M) copy_model_metrics calls
# ---------------------------------------------------------------------------


class TestBuildCompletedSummaryInPlaceMetrics:
    """Verify _build_completed_summary copies each model exactly once (O(M)),
    not once per shutdown cycle (O(K × M))."""

    def test_copy_model_metrics_called_exactly_m_times(self, tmp_path: Path) -> None:
        """K=5 shutdowns, M=2 models → copy_model_metrics called exactly 2 times."""
        from copilot_usage.models import copy_model_metrics

        shutdowns: list[str] = []
        resumes: list[str] = []
        for k in range(5):
            sd = json.dumps(
                {
                    "type": "session.shutdown",
                    "data": {
                        "shutdownType": "routine",
                        "totalPremiumRequests": k + 1,
                        "totalApiDurationMs": 1000 * (k + 1),
                        "sessionStartTime": 0,
                        "modelMetrics": {
                            "model-a": {
                                "requests": {"count": 1, "cost": 1},
                                "usage": {
                                    "inputTokens": 100,
                                    "outputTokens": 50,
                                    "cacheReadTokens": 10,
                                    "cacheWriteTokens": 5,
                                },
                            },
                            "model-b": {
                                "requests": {"count": 2, "cost": 2},
                                "usage": {
                                    "inputTokens": 200,
                                    "outputTokens": 100,
                                    "cacheReadTokens": 20,
                                    "cacheWriteTokens": 10,
                                },
                            },
                        },
                        "currentModel": "model-a",
                    },
                    "id": f"ev-sd-{k}",
                    "timestamp": f"2026-03-07T{10 + k}:00:00.000Z",
                    "currentModel": "model-a",
                }
            )
            shutdowns.append(sd)
            if k < 4:
                resumes.append(
                    json.dumps(
                        {
                            "type": "session.resume",
                            "data": {},
                            "id": f"ev-resume-{k}",
                            "timestamp": f"2026-03-07T{10 + k}:30:00.000Z",
                        }
                    )
                )

        # Interleave: start, user, [shutdown, resume]×4, shutdown (last)
        lines: list[str] = [_START_EVENT, _USER_MSG]
        for k in range(5):
            lines.append(shutdowns[k])
            if k < 4:
                lines.append(resumes[k])

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, *lines)
        events = parse_events(p)

        with patch(
            "copilot_usage.parser.copy_model_metrics",
            wraps=copy_model_metrics,
        ) as spy:
            summary = build_session_summary(events)

        # M = 2 distinct models → exactly 2 copy calls
        assert spy.call_count == 2

        # Verify correctness: metrics summed across all 5 shutdowns
        assert "model-a" in summary.model_metrics
        assert "model-b" in summary.model_metrics
        assert summary.model_metrics["model-a"].requests.count == 5
        assert summary.model_metrics["model-a"].usage.outputTokens == 250
        assert summary.model_metrics["model-b"].requests.count == 10
        assert summary.model_metrics["model-b"].usage.outputTokens == 500
        assert summary.total_premium_requests == 1 + 2 + 3 + 4 + 5


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

    def test_non_dict_root_list_returns_none(self, tmp_path: Path) -> None:
        """JSON root is a list → None (``isinstance(data, dict)`` guard)."""
        config = tmp_path / "config.json"
        config.write_text("[]", encoding="utf-8")
        assert _read_config_model(config) is None

    def test_non_dict_root_null_returns_none(self, tmp_path: Path) -> None:
        """JSON root is null → None (``isinstance(data, dict)`` guard)."""
        config = tmp_path / "config.json"
        config.write_text("null", encoding="utf-8")
        assert _read_config_model(config) is None

    def test_non_dict_root_int_returns_none(self, tmp_path: Path) -> None:
        """JSON root is an integer → None (``isinstance(data, dict)`` guard)."""
        config = tmp_path / "config.json"
        config.write_text("42", encoding="utf-8")
        assert _read_config_model(config) is None

    def test_non_dict_root_string_returns_none(self, tmp_path: Path) -> None:
        """JSON root is a string → None (``isinstance(data, dict)`` guard)."""
        config = tmp_path / "config.json"
        config.write_text('"a string"', encoding="utf-8")
        assert _read_config_model(config) is None


class TestCopilotConfigRejectsNonObject:
    """Verify that ``_CopilotConfig`` rejects a non-object JSON root."""

    def test_model_validate_json_rejects_list(self) -> None:
        """``_CopilotConfig.model_validate_json('[]')`` raises ``ValidationError``."""
        with pytest.raises(ValidationError):
            _CopilotConfig.model_validate_json("[]")


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

    def test_stale_lru_cache_not_visible_after_fixture_cleanup(
        self, tmp_path: Path
    ) -> None:
        """Autouse fixture clears lru_cache so stale entries don't leak."""
        # 1. Populate the lru_cache via get_all_sessions with a patched config
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        session_start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "stale-test",
                    "version": 1,
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {},
                },
                "id": "ev-stale",
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
                "id": "ev-u-stale",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        events_path = tmp_path / "sessions" / "stale-test" / "events.jsonl"
        _write_events(events_path, session_start, user_msg)

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            summaries = get_all_sessions(tmp_path / "sessions")
        assert summaries[0].model == "gpt-5.1"

        # 2. Simulate fixture cleanup (as if a new test started)
        _reset_all_caches()

        # 3. Directly call build_session_summary (bypassing get_all_sessions)
        #    on an active session with no model info — model must be None.
        events = parse_events(events_path)
        summary = build_session_summary(events, events_path=events_path)
        assert summary.model is None

    def test_unchanged_config_skips_cache_clear(self, tmp_path: Path) -> None:
        """get_all_sessions called twice with unchanged config reads it once.

        When the config file has not changed between invocations, the
        ``_read_config_model`` lru_cache is not cleared and the second
        call is served entirely from cache — ``Path.read_text`` is
        called at most once for the config file.
        """
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
            # First call — should read config once (cache miss)
            summaries = get_all_sessions(tmp_path / "sessions")
            assert summaries[0].model == "gpt-5.1"
            assert read_count == 1

            # Second call — config file unchanged, cache NOT cleared
            summaries = get_all_sessions(tmp_path / "sessions")
            assert summaries[0].model == "gpt-5.1"
            # read_count must still be 1: no extra read on the second call
            assert read_count == 1


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
        assert result.all_shutdowns == ()
        assert result.session_id == "test-session-001"

    def test_all_shutdowns_is_tuple(self, tmp_path: Path) -> None:
        """_first_pass returns all_shutdowns as an immutable tuple."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT)
        events = parse_events(p)
        fp = _first_pass(events)
        assert isinstance(fp.all_shutdowns, tuple)
        assert len(fp.all_shutdowns) == 1


# ---------------------------------------------------------------------------
# Issue #470 — _detect_resume direct unit tests
# ---------------------------------------------------------------------------


class TestDetectResumeDirect:
    """Direct unit tests for _detect_resume covering untested branches."""

    def test_empty_shutdowns_returns_zeroed(self) -> None:
        """No shutdowns → zeroed _ResumeInfo."""
        result = _detect_resume(events=[], all_shutdowns=())
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
# Issue #553 / #640 — _detect_resume must not allocate an O(n) list slice
# ---------------------------------------------------------------------------


class TestDetectResumeNoListSlice:
    """Verify _detect_resume uses index loop (zero-copy) instead of a list slice."""

    def test_no_intermediate_list_allocation(self, tmp_path: Path) -> None:
        """Build a session with an early shutdown and 1 000+ post-shutdown events.

        Uses tracemalloc to assert that _detect_resume does NOT allocate a
        list of length >= 1 000 for the post-shutdown tail.
        """
        import tracemalloc

        # Build a synthetic events.jsonl: start → user → shutdown → 1200 user messages
        post_events: list[str] = [
            json.dumps(
                {
                    "type": "user.message",
                    "data": {
                        "content": f"msg-{i}",
                        "transformedContent": f"msg-{i}",
                        "attachments": [],
                        "interactionId": f"int-post-{i}",
                    },
                    "id": f"ev-post-user-{i}",
                    "timestamp": "2026-03-07T12:01:00.000Z",
                    "parentId": "ev-shutdown",
                }
            )
            for i in range(1_200)
        ]

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT, *post_events)
        events = parse_events(p)
        fp = _first_pass(events)

        # Measure peak memory during _detect_resume.
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            result = _detect_resume(events, fp.all_shutdowns)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # The call should have counted all post-shutdown user messages
        assert result.post_shutdown_user_messages == 1_200

        # Assert that peak memory usage stays below what we'd expect from
        # allocating a list slice of 1000+ elements. On 64-bit, a list of
        # 1000 pointers is ~8 KB, so a 1200-element slice would be >9.6 KB
        # plus list overhead. Keeping the threshold at 8 KB ensures we would
        # catch such a large temporary list allocation.
        assert peak < 8_000, (
            f"Unexpected high peak memory in _detect_resume: {peak} bytes"
        )


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

    def test_tool_model_skips_integer_model(self, tmp_path: Path) -> None:
        """tool.execution_complete with model=42 (integer) is skipped."""
        tool_int = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-1", "model": 42, "success": True},
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_int)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model is None

    def test_tool_model_skips_boolean_model(self, tmp_path: Path) -> None:
        """tool.execution_complete with model=true (bool) is skipped; bool is not str."""
        tool_bool = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-1", "model": True, "success": True},
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_bool)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model is None

    def test_tool_model_skips_dict_model(self, tmp_path: Path) -> None:
        """tool.execution_complete with model={...} (dict) is skipped."""
        tool_dict = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-1",
                    "model": {"id": "gpt-5"},
                    "success": True,
                },
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_dict)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model is None

    def test_tool_model_falls_through_to_valid_string_after_bad_type(
        self, tmp_path: Path
    ) -> None:
        """Non-string model in first tool event skipped; second valid string wins."""
        tool_bad = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-1", "model": 99, "success": True},
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        tool_good = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-2",
                    "model": "claude-sonnet-4",
                    "success": True,
                },
                "id": "ev-t2",
                "timestamp": "2026-03-07T10:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_bad, tool_good)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.tool_model == "claude-sonnet-4"

    def test_build_active_summary_non_string_tool_model_falls_back_to_config(
        self, tmp_path: Path
    ) -> None:
        """Non-string tool model → tool_model=None → config fallback used."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        tool_int = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "tc-1", "model": 999, "success": True},
                "id": "ev-t1",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, tool_int)
        events = parse_events(p)

        fp = _first_pass(events)
        assert fp.tool_model is None

        summary = build_session_summary(events, config_path=config)
        assert summary.model == "gpt-5.1"

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

    def test_no_pydantic_validation_for_tool_model(self) -> None:
        """Optimised path reads model via dict lookup, not Pydantic validation.

        Builds 1 000 TOOL_EXECUTION_COMPLETE events without a ``model`` key
        (worst-case) and asserts that ``ToolExecutionData.model_validate`` is
        never called — proving the hot loop avoids the Pydantic round-trip.
        """
        events: list[SessionEvent] = [
            SessionEvent(
                type=EventType.TOOL_EXECUTION_COMPLETE,
                data={"toolCallId": f"tc-{i}", "success": True},
                id=f"ev-tool-{i}",
                timestamp=None,
                parentId=None,
            )
            for i in range(1_000)
        ]
        with patch.object(
            ToolExecutionData, "model_validate", wraps=ToolExecutionData.model_validate
        ) as mock_validate:
            fp = _first_pass(events)
            assert mock_validate.call_count == 0
        assert fp.tool_model is None


# ---------------------------------------------------------------------------
# Issue #509 — mtime-based session cache
# ---------------------------------------------------------------------------


class TestSessionCacheMtime:
    """get_all_sessions skips parse_events for files whose mtime is unchanged."""

    @staticmethod
    def _make_session(base: Path, name: str, sid: str) -> Path:
        """Create a completed session and return the events.jsonl path."""
        return _make_completed_session(base, name, sid)

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
        """events.jsonl stat'd once (discovery); absent plan.md incurs no stat."""
        self._make_session(tmp_path, "sess-a", "a")

        with patch(
            "copilot_usage.parser.safe_file_identity", wraps=safe_file_identity
        ) as spy:
            get_all_sessions(tmp_path)
            # safe_file_identity called once for _CONFIG_PATH and once for
            # the root directory (discovery cache check).  events.jsonl is
            # stat'd directly (not via safe_file_identity) and plan.md does
            # not exist so its absence is detected via os.scandir dir
            # listing — no stat call is issued.
            assert spy.call_count == 2

    def test_resumed_session_is_cached(self, tmp_path: Path) -> None:
        """A session that resumed after shutdown IS cached with config_model=None (model from shutdown)."""
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

        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            results = get_all_sessions(tmp_path)
        assert len(results) == 1
        assert results[0].is_active is True

        # Resumed session IS cached; model comes from the shutdown event,
        # NOT from config, so config_model should be None.
        assert events_path in _SESSION_CACHE
        assert _SESSION_CACHE[events_path].config_model is None

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
        assert entry.plan_id == safe_file_identity(plan)
        assert entry.summary.name == "Test"

    def test_cached_sessions_no_redundant_plan_stat(self, tmp_path: Path) -> None:
        """Cached refresh uses at most N+1 stat calls for N sessions (no plan.md stat).

        After the initial call populates the cache, a second call must
        not issue separate stat calls for plan.md — absent ``plan.md``
        files are detected via ``os.scandir`` directory listing with zero
        ``stat()`` overhead.  Total stat calls for the cached path are
        1 per session (events.jsonl in discovery) plus 1 for config,
        with zero additional calls during the cached refresh.
        """
        n = 5
        for i in range(n):
            self._make_session(tmp_path, f"sess-{i}", str(i))

        # Populate cache
        get_all_sessions(tmp_path)

        # Second call — all sessions are cached
        with patch(
            "copilot_usage.parser.safe_file_identity", wraps=safe_file_identity
        ) as spy:
            results = get_all_sessions(tmp_path)
            assert len(results) == n
            # 1 for _CONFIG_PATH + 1 for root directory (discovery cache) +
            # 1 per session from _discover_with_identity
            # (events.jsonl only — plan.md absent, detected by scandir).
            assert spy.call_count == 2 + n

    def test_stale_cache_entries_evicted_on_session_delete(
        self, tmp_path: Path
    ) -> None:
        """Cache entries for deleted session directories are pruned."""
        import shutil

        p1 = self._make_session(tmp_path, "sess-a", "a")
        p2 = self._make_session(tmp_path, "sess-b", "b")
        p3 = self._make_session(tmp_path, "sess-c", "c")

        # First call populates cache for all three sessions.
        result1 = get_all_sessions(tmp_path)
        assert len(result1) == 3
        assert p1 in _SESSION_CACHE
        assert p2 in _SESSION_CACHE
        assert p3 in _SESSION_CACHE

        # Delete sess-b from disk.
        shutil.rmtree(p2.parent)

        # Second call should discover only sess-a and sess-c; sess-b
        # must be evicted from the cache.
        result2 = get_all_sessions(tmp_path)
        assert len(result2) == 2
        assert p1 in _SESSION_CACHE
        assert p2 not in _SESSION_CACHE
        assert p3 in _SESSION_CACHE


# ---------------------------------------------------------------------------
# Issue #827 — stale-entry pruning scoped to current base_path
# ---------------------------------------------------------------------------


class TestStalePruningScopedToBasePath:
    """Stale-entry pruning must not evict entries from other base paths."""

    @staticmethod
    def _make_session(base: Path, name: str, sid: str) -> Path:
        """Create a completed session and return the events.jsonl path."""
        return _make_completed_session(base, name, sid)

    def test_different_base_paths_preserve_each_others_cache(
        self, tmp_path: Path
    ) -> None:
        """Calling get_all_sessions with path_b must not evict path_a entries."""
        path_a = tmp_path / "root_a"
        path_b = tmp_path / "root_b"
        pa = self._make_session(path_a, "sess-a", "a")
        pb = self._make_session(path_b, "sess-b", "b")

        get_all_sessions(path_a)
        assert pa in _SESSION_CACHE

        # Calling with a different root must not evict path_a's entries.
        get_all_sessions(path_b)
        assert pa in _SESSION_CACHE, "path_a entry evicted by path_b call"
        assert pb in _SESSION_CACHE

    def test_no_reparse_after_switching_base_paths(self, tmp_path: Path) -> None:
        """Returning to path_a after path_b must not re-parse (cache hit)."""
        path_a = tmp_path / "root_a"
        path_b = tmp_path / "root_b"
        self._make_session(path_a, "sess-a", "a")
        self._make_session(path_b, "sess-b", "b")

        get_all_sessions(path_a)
        get_all_sessions(path_b)

        # Third call back to path_a — parse_events should not be called.
        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            result = get_all_sessions(path_a)
            assert len(result) == 1
            assert spy.call_count == 0

    def test_stale_pruning_still_works_within_same_root(self, tmp_path: Path) -> None:
        """Deleted sessions under the *same* root are still evicted."""
        import shutil

        path_a = tmp_path / "root_a"
        p1 = self._make_session(path_a, "sess-1", "s1")
        p2 = self._make_session(path_a, "sess-2", "s2")

        get_all_sessions(path_a)
        assert p1 in _SESSION_CACHE
        assert p2 in _SESSION_CACHE

        shutil.rmtree(p2.parent)

        get_all_sessions(path_a)
        assert p1 in _SESSION_CACHE
        assert p2 not in _SESSION_CACHE, "Deleted session not evicted"


# ---------------------------------------------------------------------------
# Issue #836 — stale-prune scan skipped on discovery cache hits
# ---------------------------------------------------------------------------


class TestStalePruneScanSkippedOnCacheHit:
    """Stale-prune scan in get_all_sessions is skipped on pure cache hits.

    When ``_discover_with_identity`` returns ``is_cache_hit=True`` the
    root directory is unchanged *and* no cached ``events.jsonl`` was
    definitively deleted, so no sessions can have been added or removed.
    The O(cache_size) stale-prune scan is therefore unnecessary and must
    be skipped.  If deletions *are* detected, ``is_cache_hit`` is
    flipped to ``False`` and the scan must run.
    """

    @staticmethod
    def _make_session(base: Path, name: str, sid: str) -> Path:
        """Create a completed session and return the events.jsonl path."""
        return _make_completed_session(base, name, sid)

    def test_stale_prune_scan_skipped_on_cache_hit(self, tmp_path: Path) -> None:
        """On a discovery cache hit, is_relative_to is never called."""
        p1 = self._make_session(tmp_path, "sess-a", "a")
        p2 = self._make_session(tmp_path, "sess-b", "b")

        # First call populates caches (discovery miss).
        result1 = get_all_sessions(tmp_path)
        assert len(result1) == 2
        assert p1 in _SESSION_CACHE
        assert p2 in _SESSION_CACHE

        # Second call without filesystem changes → discovery cache hit.
        # The stale-prune scan must be skipped entirely — no
        # Path.is_relative_to calls should occur.
        orig_is_relative_to = Path.is_relative_to
        call_count = 0

        def counting_is_relative_to(self: Path, other: str | os.PathLike[str]) -> bool:
            nonlocal call_count
            call_count += 1
            return orig_is_relative_to(self, other)

        with patch.object(Path, "is_relative_to", counting_is_relative_to):
            result2 = get_all_sessions(tmp_path)

        assert len(result2) == 2
        assert call_count == 0, (
            f"is_relative_to called {call_count} time(s) on cache hit; "
            "stale-prune scan should have been skipped"
        )

    def test_stale_prune_scan_runs_on_cache_miss(self, tmp_path: Path) -> None:
        """On a discovery cache miss, stale entries are still pruned."""
        import shutil

        p1 = self._make_session(tmp_path, "sess-a", "a")
        p2 = self._make_session(tmp_path, "sess-b", "b")

        # First call populates caches.
        get_all_sessions(tmp_path)
        assert p1 in _SESSION_CACHE
        assert p2 in _SESSION_CACHE

        # Delete sess-b — this changes the root directory identity,
        # forcing a discovery cache miss.
        shutil.rmtree(p2.parent)

        result = get_all_sessions(tmp_path)
        assert len(result) == 1
        assert p1 in _SESSION_CACHE
        assert p2 not in _SESSION_CACHE, "Deleted session not evicted on cache miss"

    def test_stale_prune_runs_on_events_jsonl_deletion(self, tmp_path: Path) -> None:
        """Deleting events.jsonl without changing root dir mtime still prunes.

        When ``events.jsonl`` is removed inside a session directory the
        root directory's identity may remain unchanged (the session
        *subdirectory* inode changes, but many filesystems only bump the
        parent's mtime when a direct child is added/removed).
        ``_discover_with_identity`` detects the ``FileNotFoundError``
        and flips ``is_cache_hit`` to ``False`` so that
        ``get_all_sessions`` still runs its stale-prune scan and evicts
        the orphaned ``_SESSION_CACHE`` / ``_EVENTS_CACHE`` entries.
        """
        p1 = self._make_session(tmp_path, "sess-a", "a")
        p2 = self._make_session(tmp_path, "sess-b", "b")

        # First call populates all caches (discovery miss).
        result1 = get_all_sessions(tmp_path)
        assert len(result1) == 2
        assert p1 in _SESSION_CACHE
        assert p2 in _SESSION_CACHE

        # Save root dir identity so we can restore it after deletion.
        root = tmp_path.resolve()
        orig_stat = root.stat()

        # Delete only events.jsonl — the session dir still exists so
        # many filesystems won't update the root dir mtime.
        p2.unlink()

        # Restore root mtime/atime to guarantee root identity is unchanged.
        os.utime(root, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))

        # Verify root identity is truly unchanged — without this the test
        # could silently pass via the "root cache miss" path.
        assert safe_file_identity(root) == (
            orig_stat.st_mtime_ns,
            orig_stat.st_size,
        ), "Root identity changed unexpectedly; edge-case test is invalid"

        result2 = get_all_sessions(tmp_path)
        assert len(result2) == 1
        assert p1 in _SESSION_CACHE
        assert p2 not in _SESSION_CACHE, (
            "Stale _SESSION_CACHE entry not pruned after events.jsonl deletion"
        )
        assert p2 not in _EVENTS_CACHE, (
            "Stale _EVENTS_CACHE entry not pruned after events.jsonl deletion"
        )


# ---------------------------------------------------------------------------
# Issue #552 — active sessions cached in _SESSION_CACHE
# ---------------------------------------------------------------------------


class TestActiveSessionCaching:
    """Active sessions are cached and not re-parsed when files are unchanged."""

    def test_active_session_parse_events_called_once(self, tmp_path: Path) -> None:
        """parse_events is called exactly once across two get_all_sessions calls
        for an active session whose events.jsonl has not changed."""
        # Create an active session (no shutdown) with 200 events worth of data
        events_lines: list[str] = [_START_EVENT, _USER_MSG]
        events_lines.extend(
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {
                        "messageId": f"msg-{i}",
                        "content": f"response {i}",
                        "toolRequests": [],
                        "interactionId": "int-1",
                        "outputTokens": 10,
                    },
                    "id": f"ev-asst-{i}",
                    "timestamp": f"2026-03-07T10:{i % 60:02d}:{i % 60:02d}.000Z",
                }
            )
            for i in range(200)
        )
        events_lines.append(_TOOL_EXEC)

        p = tmp_path / "active-sess" / "events.jsonl"
        _write_events(p, *events_lines)

        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        # First call — must parse
        with (
            patch("copilot_usage.parser._CONFIG_PATH", config),
            patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy,
        ):
            result1 = get_all_sessions(tmp_path)
            assert len(result1) == 1
            assert result1[0].is_active is True
            assert spy.call_count == 1

        # Second call — no file changes, should use cache
        with (
            patch("copilot_usage.parser._CONFIG_PATH", config),
            patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy,
        ):
            result2 = get_all_sessions(tmp_path)
            assert len(result2) == 1
            assert result2[0].is_active is True
            assert spy.call_count == 0

    def test_active_session_cache_invalidated_on_config_change(
        self,
        tmp_path: Path,
    ) -> None:
        """Active session cache is invalidated when config model changes."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            result1 = get_all_sessions(tmp_path / "sessions")
            assert len(result1) == 1
            assert result1[0].model == "gpt-5.1"

            # Change config model
            config.write_text('{"model": "claude-sonnet-4"}', encoding="utf-8")

            # Second call should re-parse because config model changed
            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result2 = get_all_sessions(tmp_path / "sessions")
                assert len(result2) == 1
                assert result2[0].model == "claude-sonnet-4"
                assert spy.call_count == 1

    def test_active_session_cache_hit_on_unchanged_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Active session uses cache when config model is unchanged."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            get_all_sessions(tmp_path / "sessions")

            # Second call — same config, same file → cache hit
            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result = get_all_sessions(tmp_path / "sessions")
                assert len(result) == 1
                assert result[0].model == "gpt-5.1"
                assert spy.call_count == 0

    def test_completed_session_config_model_none_in_cache(
        self,
        tmp_path: Path,
    ) -> None:
        """Completed sessions store config_model=None in cache."""
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, _SHUTDOWN_EVENT)

        get_all_sessions(tmp_path)

        entry = _SESSION_CACHE[p]
        assert entry.config_model is None
        assert entry.depends_on_config is False
        assert entry.summary.is_active is False

    def test_active_session_config_none_to_real_invalidates(
        self,
        tmp_path: Path,
    ) -> None:
        """Cache invalidates when config transitions from None to a real model."""
        config = tmp_path / "config.json"
        # Start with no config file → config model is None
        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            result1 = get_all_sessions(tmp_path / "sessions")
            assert len(result1) == 1
            assert result1[0].model is None

            entry = _SESSION_CACHE[p]
            assert entry.depends_on_config is True
            assert entry.config_model is None

            # Now create a config with a model
            config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result2 = get_all_sessions(tmp_path / "sessions")
                assert len(result2) == 1
                assert result2[0].model == "gpt-5.1"
                assert spy.call_count == 1

    def test_active_session_with_event_model_not_invalidated_by_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Active sessions whose model comes from events ignore config changes."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        # _TOOL_EXEC has model "claude-sonnet-4" in its data
        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG, _TOOL_EXEC)

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            result1 = get_all_sessions(tmp_path / "sessions")
            assert len(result1) == 1
            assert result1[0].model == "claude-sonnet-4"

            entry = _SESSION_CACHE[p]
            assert entry.depends_on_config is False

            # Change config — should NOT trigger re-parse
            config.write_text('{"model": "gpt-5.2"}', encoding="utf-8")

            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result2 = get_all_sessions(tmp_path / "sessions")
                assert len(result2) == 1
                assert result2[0].model == "claude-sonnet-4"
                assert spy.call_count == 0

    def test_active_session_real_model_to_none_invalidates(
        self,
        tmp_path: Path,
    ) -> None:
        """Cache invalidates when config model transitions from a real value to None (file deleted)."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            result1 = get_all_sessions(tmp_path / "sessions")
            assert len(result1) == 1
            assert result1[0].model == "gpt-5.1"

            entry = _SESSION_CACHE[p]
            assert entry.depends_on_config is True
            assert entry.config_model == "gpt-5.1"

            # Delete the config file — config_model should now be None
            config.unlink()

            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result2 = get_all_sessions(tmp_path / "sessions")
                assert len(result2) == 1
                assert result2[0].model is None  # config-sourced model gone
                assert spy.call_count == 1  # re-parsed due to staleness

    def test_config_staleness_takes_priority_over_plan_update(
        self,
        tmp_path: Path,
    ) -> None:
        """When both plan.md and config model change, config staleness triggers
        a full re-parse — the plan-only fast path is NOT taken."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        # Active session without tool execution → depends_on_config=True
        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        plan = p.parent / "plan.md"
        plan.write_text("# Original\n", encoding="utf-8")

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            result1 = get_all_sessions(tmp_path / "sessions")
            assert len(result1) == 1
            assert result1[0].model == "gpt-5.1"

            entry = _SESSION_CACHE[p]
            assert entry.depends_on_config is True
            assert entry.config_model == "gpt-5.1"

            # Change BOTH plan.md and config model between calls
            plan.write_text("# Renamed\n", encoding="utf-8")
            config.write_text('{"model": "claude-sonnet-4"}', encoding="utf-8")

            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result2 = get_all_sessions(tmp_path / "sessions")
                assert len(result2) == 1
                # Full re-parse must have occurred (config staleness wins)
                assert spy.call_count == 1
                assert result2[0].model == "claude-sonnet-4"

            # Cache entry must reflect the new config model
            entry2 = _SESSION_CACHE[p]
            assert entry2.config_model == "claude-sonnet-4"
            assert entry2.depends_on_config is True

    def test_plan_update_when_config_unchanged_preserves_depends_on_config(
        self,
        tmp_path: Path,
    ) -> None:
        """When only plan.md changes (config unchanged), the plan-only fast
        path is taken and depends_on_config / config_model are preserved."""
        config = tmp_path / "config.json"
        config.write_text('{"model": "gpt-5.1"}', encoding="utf-8")

        # Active session without tool execution → depends_on_config=True
        p = tmp_path / "sessions" / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        plan = p.parent / "plan.md"
        plan.write_text("# Original\n", encoding="utf-8")

        with patch("copilot_usage.parser._CONFIG_PATH", config):
            result1 = get_all_sessions(tmp_path / "sessions")
            assert len(result1) == 1
            assert result1[0].model == "gpt-5.1"
            assert result1[0].name == "Original"

            entry = _SESSION_CACHE[p]
            assert entry.depends_on_config is True
            assert entry.config_model == "gpt-5.1"

            # Change ONLY plan.md — config stays the same
            plan.write_text("# Renamed\n", encoding="utf-8")

            with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
                result2 = get_all_sessions(tmp_path / "sessions")
                assert len(result2) == 1
                # Plan-only fast path — no re-parse
                assert spy.call_count == 0
                assert result2[0].model == "gpt-5.1"
                assert result2[0].name == "Renamed"

            # Cache entry must preserve config tracking fields
            entry2 = _SESSION_CACHE[p]
            assert entry2.depends_on_config is True
            assert entry2.config_model == "gpt-5.1"


# ---------------------------------------------------------------------------
# get_cached_events — parsed-events cache
# ---------------------------------------------------------------------------


def _make_user_event(index: int) -> str:
    """Return a unique user.message JSON line for synthetic events files."""
    return json.dumps(
        {
            "type": "user.message",
            "data": {
                "content": f"message-{index}",
                "transformedContent": f"message-{index}",
                "attachments": [],
                "interactionId": f"int-{index}",
            },
            "id": f"ev-user-{index}",
            "timestamp": "2026-03-07T10:01:00.000Z",
            "parentId": "ev-start",
        }
    )


def _write_large_events_file(path: Path, count: int) -> Path:
    """Write a synthetic events.jsonl with *count* user message events."""
    lines = [_START_EVENT] + [_make_user_event(i) for i in range(count)]
    return _write_events(path, *lines)


class TestGetCachedEvents:
    """Tests for the get_cached_events parsed-events cache."""

    def test_cache_hit_returns_same_object(self, tmp_path: Path) -> None:
        """Second call with unchanged file returns the same tuple object."""
        p = tmp_path / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        first = get_cached_events(p)
        second = get_cached_events(p)

        assert first is second
        assert len(first) == 3

    def test_cache_miss_on_file_change(self, tmp_path: Path) -> None:
        """Modifying the file invalidates the cache."""
        p = tmp_path / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG)

        first = get_cached_events(p)
        assert len(first) == 2

        # Append another event to change file identity
        with p.open("a", encoding="utf-8") as fh:
            fh.write(_ASSISTANT_MSG + "\n")

        second = get_cached_events(p)
        assert second is not first
        assert len(second) == 3

    def test_large_file_cache_hit_no_reparse(self, tmp_path: Path) -> None:
        """Cache hit on a ≥ 1000-event file does not call parse_events."""
        p = tmp_path / "s1" / "events.jsonl"
        _write_large_events_file(p, 1_000)

        first = get_cached_events(p)
        assert len(first) == 1_001  # 1 start + 1000 user messages

        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            second = get_cached_events(p)
            assert spy.call_count == 0

        assert second is first

    def test_cache_eviction_bounds_memory(self, tmp_path: Path) -> None:
        """Cache evicts LRU entry when _MAX_CACHED_EVENTS is exceeded."""
        paths: list[Path] = []
        for i in range(_MAX_CACHED_EVENTS + 1):
            p = tmp_path / f"s{i}" / "events.jsonl"
            _write_events(p, _START_EVENT, _USER_MSG)
            get_cached_events(p)
            paths.append(p)

        assert len(_EVENTS_CACHE) == _MAX_CACHED_EVENTS
        # The first path should have been evicted (LRU)
        assert paths[0] not in _EVENTS_CACHE
        # The last path should still be cached
        assert paths[-1] in _EVENTS_CACHE

    def test_lru_eviction_protects_recently_accessed(self, tmp_path: Path) -> None:
        """Recently accessed entry survives eviction; true LRU is evicted."""
        paths: list[Path] = []
        for i in range(_MAX_CACHED_EVENTS):
            p = tmp_path / f"s{i}" / "events.jsonl"
            _write_events(p, _START_EVENT, _USER_MSG)
            get_cached_events(p)
            paths.append(p)

        assert len(_EVENTS_CACHE) == _MAX_CACHED_EVENTS

        # Re-access session 0 to promote it to most-recently-used
        get_cached_events(paths[0])

        # Insert a 9th session — should evict session 1 (now LRU), not 0
        p_new = tmp_path / "s_new" / "events.jsonl"
        _write_events(p_new, _START_EVENT, _USER_MSG)
        get_cached_events(p_new)

        assert len(_EVENTS_CACHE) == _MAX_CACHED_EVENTS
        assert paths[0] in _EVENTS_CACHE, "Recently accessed entry was evicted"
        assert paths[1] not in _EVENTS_CACHE, "LRU entry should have been evicted"
        assert p_new in _EVENTS_CACHE

    def test_cache_populates_entry(self, tmp_path: Path) -> None:
        """After a call, _EVENTS_CACHE contains a _CachedEvents entry."""
        p = tmp_path / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG)

        get_cached_events(p)

        assert p in _EVENTS_CACHE
        entry = _EVENTS_CACHE[p]
        assert isinstance(entry, _CachedEvents)
        assert entry.file_id == safe_file_identity(p)
        assert len(entry.events) == 2

    def test_cached_reads_skip_parsing(self, tmp_path: Path) -> None:
        """Repeated cached reads never re-invoke parse_events."""
        p = tmp_path / "s1" / "events.jsonl"
        _write_large_events_file(p, 1_000)

        # Prime the cache with a cold read
        first = get_cached_events(p)

        # Subsequent reads must not call parse_events at all
        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            second = get_cached_events(p)
            third = get_cached_events(p)
            assert spy.call_count == 0

        # All reads return the exact same cached tuple object
        assert second is first
        assert third is first

    def test_returns_immutable_tuple(self, tmp_path: Path) -> None:
        """get_cached_events returns an immutable tuple, not a mutable list."""
        p = tmp_path / "s1" / "events.jsonl"
        _write_events(p, _START_EVENT, _USER_MSG, _ASSISTANT_MSG)

        first = get_cached_events(p)

        # Return type is tuple, not list.
        assert isinstance(first, tuple)

        # Attempting item assignment raises TypeError (tuple is immutable).
        with pytest.raises(TypeError):
            first[0] = first[-1]  # type: ignore[index]

        # A second call returns the exact same cached object.
        second = get_cached_events(p)
        assert second is first

    def test_oserror_propagated_on_missing_file(self, tmp_path: Path) -> None:
        """get_cached_events raises OSError when the file does not exist."""
        missing = tmp_path / "ghost" / "events.jsonl"
        with pytest.raises(OSError):
            get_cached_events(missing)


# ---------------------------------------------------------------------------
# Issue #668 — get_all_sessions populates _EVENTS_CACHE
# ---------------------------------------------------------------------------


class TestGetAllSessionsPopulatesEventsCache:
    """get_all_sessions stores parsed events in _EVENTS_CACHE so that
    subsequent get_cached_events calls avoid a redundant file re-read.
    """

    @staticmethod
    def _make_session(base: Path, name: str, sid: str) -> Path:
        """Create a completed session and return the events.jsonl path."""
        return _make_completed_session(base, name, sid)

    def test_cold_cache_populates_events_cache(self, tmp_path: Path) -> None:
        """After a cold get_all_sessions call, _EVENTS_CACHE contains entries."""
        p1 = self._make_session(tmp_path, "sess-a", "a")
        p2 = self._make_session(tmp_path, "sess-b", "b")

        assert len(_EVENTS_CACHE) == 0  # cold cache

        get_all_sessions(tmp_path)

        assert p1 in _EVENTS_CACHE
        assert p2 in _EVENTS_CACHE
        assert isinstance(_EVENTS_CACHE[p1], _CachedEvents)
        assert isinstance(_EVENTS_CACHE[p2], _CachedEvents)

    def test_get_cached_events_after_get_all_sessions_no_reparse(
        self, tmp_path: Path
    ) -> None:
        """get_cached_events reuses _EVENTS_CACHE populated by get_all_sessions
        — parse_events is called only once per session, not twice."""
        p = self._make_session(tmp_path, "sess-a", "a")

        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            get_all_sessions(tmp_path)
            assert spy.call_count == 1  # single parse during get_all_sessions

            events = get_cached_events(p)
            # Still 1 — no additional parse_events call
            assert spy.call_count == 1

        assert len(events) == 3  # start + user + shutdown


# ---------------------------------------------------------------------------
# Issue #676 — deferred-events cache overflow with >8 sessions
# ---------------------------------------------------------------------------


class TestGetAllSessionsEventsCacheOverflow:
    """Verify deferred-events overflow logic in get_all_sessions.

    When more than ``_MAX_CACHED_EVENTS`` sessions are discovered, only
    the newest ``_MAX_CACHED_EVENTS`` are retained in ``_EVENTS_CACHE``
    and the oldest are excluded.  The insertion order ensures newest
    entries sit at the MRU (back) of the ``OrderedDict``.
    """

    @staticmethod
    def _make_session(base: Path, name: str, sid: str) -> Path:
        """Create a completed session and return the events.jsonl path."""
        return _make_completed_session(base, name, sid)

    def _make_sessions_with_distinct_mtimes(self, base: Path, count: int) -> list[Path]:
        """Create *count* sessions with ascending mtimes (oldest first).

        Returns a list of ``events.jsonl`` paths ordered oldest → newest
        (i.e. ``paths[0]`` is the oldest, ``paths[-1]`` is the newest).
        Explicit ``os.utime`` calls guarantee distinct nanosecond mtimes
        regardless of filesystem timer resolution.
        """
        paths: list[Path] = []
        for i in range(count):
            p = self._make_session(base, f"sess-{i}", str(i))
            # Assign monotonically increasing mtime so discovery ordering
            # is deterministic: session 0 is oldest, session count-1 is newest.
            mtime_ns = (1_000_000_000 + i) * 1_000_000_000  # distinct seconds
            atime_ns = mtime_ns
            os.utime(p, ns=(atime_ns, mtime_ns))
            paths.append(p)
        return paths

    def test_only_newest_max_sessions_cached(self, tmp_path: Path) -> None:
        """With _MAX_CACHED_EVENTS + 1 sessions, only the newest
        _MAX_CACHED_EVENTS have events cached."""
        total = _MAX_CACHED_EVENTS + 1
        paths = self._make_sessions_with_distinct_mtimes(tmp_path, total)

        get_all_sessions(tmp_path)

        # The newest _MAX_CACHED_EVENTS sessions should be in _EVENTS_CACHE.
        for p in paths[-_MAX_CACHED_EVENTS:]:
            assert p in _EVENTS_CACHE, f"expected {p.parent.name} in cache"
        # The oldest session should NOT be in _EVENTS_CACHE.
        assert paths[0] not in _EVENTS_CACHE, "oldest session should be excluded"

    def test_excluded_session_reparses_on_get_cached_events(
        self, tmp_path: Path
    ) -> None:
        """Session excluded from deferred_events requires a re-parse via
        get_cached_events."""
        total = _MAX_CACHED_EVENTS + 1
        paths = self._make_sessions_with_distinct_mtimes(tmp_path, total)

        get_all_sessions(tmp_path)

        excluded_path = paths[0]
        assert excluded_path not in _EVENTS_CACHE

        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            events = get_cached_events(excluded_path)
            spy.assert_called_once()  # cache miss → re-parse

        assert len(events) == 3  # start + user + shutdown

    def test_newest_session_survives_subsequent_eviction(self, tmp_path: Path) -> None:
        """After get_all_sessions with _MAX_CACHED_EVENTS sessions, adding
        one more via get_cached_events evicts the oldest (LRU), not the
        newest (MRU)."""
        paths = self._make_sessions_with_distinct_mtimes(tmp_path, _MAX_CACHED_EVENTS)
        oldest_path = paths[0]
        newest_path = paths[-1]

        get_all_sessions(tmp_path)

        # Confirm all _MAX_CACHED_EVENTS entries are cached.
        assert len(_EVENTS_CACHE) == _MAX_CACHED_EVENTS
        # Newest is at the back of _EVENTS_CACHE (MRU position).
        assert list(_EVENTS_CACHE.keys())[-1] == newest_path

        # Create a 9th session and load it via get_cached_events.
        extra = self._make_session(tmp_path, "sess-extra", "extra")
        get_cached_events(extra)

        # The oldest session should have been evicted (LRU at front).
        assert oldest_path not in _EVENTS_CACHE
        # The newest session should still be cached (MRU at back).
        assert newest_path in _EVENTS_CACHE
        # The newly-loaded session should be cached.
        assert extra in _EVENTS_CACHE


# ---------------------------------------------------------------------------
# Issue #640 — _detect_resume: replace islice with range-index loop
# ---------------------------------------------------------------------------


class TestDetectResumeRangeIndex:
    """Verify _detect_resume correctness and O(n_remaining) index access.

    These tests validate correctness for a large event list and ensure the
    implementation only accesses post-shutdown indices, preventing both
    iterator-based scanning and redundant pre-shutdown index access.
    """

    def test_correctness_with_large_event_list(self, tmp_path: Path) -> None:
        """5 000-event session with shutdown at index 4 990.

        Builds a synthetic events.jsonl and asserts _detect_resume produces
        the correct counts for the 10 post-shutdown events (a mix of
        user messages, assistant turns, and assistant messages with tokens).
        """
        # Pre-shutdown padding: 4 988 user messages (indices 2..4989 after
        # start + first user msg).
        pre_events: list[str] = [
            json.dumps(
                {
                    "type": "user.message",
                    "data": {
                        "content": f"pre-{i}",
                        "transformedContent": f"pre-{i}",
                        "attachments": [],
                        "interactionId": f"int-pre-{i}",
                    },
                    "id": f"ev-pre-{i}",
                    "timestamp": "2026-03-07T10:05:00.000Z",
                    "parentId": "ev-start",
                }
            )
            for i in range(4_988)
        ]

        # Post-shutdown: 5 user messages, 3 assistant turn starts,
        # 2 assistant messages with tokens.
        post_user: list[str] = [
            json.dumps(
                {
                    "type": "user.message",
                    "data": {
                        "content": f"post-u-{i}",
                        "transformedContent": f"post-u-{i}",
                        "attachments": [],
                        "interactionId": f"int-post-u-{i}",
                    },
                    "id": f"ev-post-u-{i}",
                    "timestamp": "2026-03-07T12:01:00.000Z",
                    "parentId": "ev-shutdown",
                }
            )
            for i in range(5)
        ]
        post_turns: list[str] = [
            json.dumps(
                {
                    "type": "assistant.turn_start",
                    "data": {},
                    "id": f"ev-post-turn-{i}",
                    "timestamp": "2026-03-07T12:02:00.000Z",
                    "parentId": "ev-shutdown",
                }
            )
            for i in range(3)
        ]
        post_asst: list[str] = [
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {
                        "messageId": f"pm-{i}",
                        "content": f"resp-{i}",
                        "toolRequests": [],
                        "interactionId": f"int-post-a-{i}",
                        "outputTokens": 50,
                    },
                    "id": f"ev-post-asst-{i}",
                    "timestamp": "2026-03-07T12:03:00.000Z",
                    "parentId": "ev-shutdown",
                }
            )
            for i in range(2)
        ]

        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            _START_EVENT,
            _USER_MSG,
            *pre_events,
            _SHUTDOWN_EVENT,
            *post_user,
            *post_turns,
            *post_asst,
        )
        events = parse_events(p)
        fp = _first_pass(events)

        # Sanity: total events should be exactly 5 001.
        # start(1) + user(1) + pre(4988) + shutdown(1) + post(5+3+2) = 5001
        assert len(events) == 5_001

        result = _detect_resume(events, fp.all_shutdowns)

        assert result.session_resumed is True
        assert result.post_shutdown_user_messages == 5
        assert result.post_shutdown_turn_starts == 3
        assert result.post_shutdown_output_tokens == 100  # 2 × 50
        assert result.last_resume_time is None  # no session.resume event

    def test_only_iterates_remaining_events(self) -> None:
        """Verify _detect_resume only accesses post-shutdown indices.

        Uses a list subclass that raises on ``__iter__`` and on
        ``__getitem__`` for indices ≤ ``shutdown_idx``.  This proves the
        implementation neither uses the iterator protocol nor scans
        pre-shutdown elements via index, and would fail if the code
        regressed to ``itertools.islice`` or a naïve index-from-zero loop.
        """
        from copilot_usage.models import SessionEvent

        class _NoPreScanList(list[SessionEvent]):
            """list subclass that forbids iteration and pre-shutdown indexing."""

            def __init__(
                self,
                items: list[SessionEvent],
                *,
                forbidden_up_to: int,
            ) -> None:
                super().__init__(items)
                self._forbidden_up_to = forbidden_up_to

            def __iter__(self) -> Iterator[SessionEvent]:
                raise AssertionError(
                    "_detect_resume must use index-based access, not __iter__"
                )

            @overload
            def __getitem__(self, index: SupportsIndex, /) -> SessionEvent: ...

            @overload
            def __getitem__(self, index: slice, /) -> list[SessionEvent]: ...

            def __getitem__(
                self, index: SupportsIndex | slice, /
            ) -> SessionEvent | list[SessionEvent]:
                if isinstance(index, slice):
                    return super().__getitem__(index)
                int_idx = index.__index__()
                if int_idx <= self._forbidden_up_to:
                    raise AssertionError(
                        f"_detect_resume must not access index {int_idx} "
                        f"(<= shutdown_idx {self._forbidden_up_to})"
                    )
                return super().__getitem__(index)

        n_total = 5_000
        shutdown_idx = 4_990

        events: list[SessionEvent] = []
        for i in range(n_total):
            if i < shutdown_idx:
                events.append(
                    SessionEvent(
                        type=EventType.USER_MESSAGE,
                        data={
                            "content": f"m-{i}",
                            "transformedContent": f"m-{i}",
                            "attachments": [],
                            "interactionId": f"int-{i}",
                        },
                        id=f"ev-{i}",
                        timestamp=None,
                        parentId=None,
                    )
                )
            elif i == shutdown_idx:
                events.append(
                    SessionEvent(
                        type=EventType.SESSION_SHUTDOWN,
                        data={
                            "shutdownType": "routine",
                            "totalPremiumRequests": 0,
                            "totalApiDurationMs": 0,
                            "sessionStartTime": 0,
                        },
                        id=f"ev-shutdown-{i}",
                        timestamp=None,
                        parentId=None,
                    )
                )
            else:
                events.append(
                    SessionEvent(
                        type=EventType.USER_MESSAGE,
                        data={
                            "content": f"post-{i}",
                            "transformedContent": f"post-{i}",
                            "attachments": [],
                            "interactionId": f"int-post-{i}",
                        },
                        id=f"ev-post-{i}",
                        timestamp=None,
                        parentId=None,
                    )
                )

        shutdowns: tuple[tuple[int, SessionShutdownData], ...] = (
            (
                shutdown_idx,
                SessionShutdownData(
                    shutdownType="routine",
                    totalPremiumRequests=0,
                    totalApiDurationMs=0,
                ),
            ),
        )

        no_iter_events = _NoPreScanList(events, forbidden_up_to=shutdown_idx)
        result = _detect_resume(no_iter_events, shutdowns)

        # Only the 9 post-shutdown user messages should be counted
        expected_remaining = n_total - shutdown_idx - 1  # 9
        assert result.post_shutdown_user_messages == expected_remaining
        assert result.session_resumed is True


# ---------------------------------------------------------------------------
# Issue #685 — _detect_resume: non-indicator post-shutdown events must NOT
#              set session_resumed
# ---------------------------------------------------------------------------


class TestDetectResumeNonIndicatorEvents:
    """Non-indicator events after shutdown must not trigger resume."""

    def test_post_shutdown_non_indicator_events_do_not_resume(
        self, tmp_path: Path
    ) -> None:
        """TOOL_EXECUTION_COMPLETE + SESSION_ERROR after shutdown → session_resumed=False."""
        tool_exec = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-post",
                    "model": "claude-sonnet-4",
                    "interactionId": "int-1",
                    "success": True,
                },
                "id": "ev-tool-post",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        session_error = json.dumps(
            {
                "type": "session.error",
                "data": {"message": "something went wrong"},
                "id": "ev-error-post",
                "timestamp": "2026-03-07T12:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT, tool_exec, session_error
        )
        events = parse_events(p)
        fp = _first_pass(events)

        result = _detect_resume(events, fp.all_shutdowns)

        assert result.session_resumed is False
        assert result.post_shutdown_user_messages == 0
        assert result.post_shutdown_turn_starts == 0
        assert result.last_resume_time is None

    def test_build_session_summary_not_active_with_non_indicator_events(
        self, tmp_path: Path
    ) -> None:
        """Full build_session_summary produces is_active=False and end_time set."""
        tool_exec = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc-post",
                    "model": "claude-sonnet-4",
                    "interactionId": "int-1",
                    "success": True,
                },
                "id": "ev-tool-post",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        session_error = json.dumps(
            {
                "type": "session.error",
                "data": {"message": "something went wrong"},
                "id": "ev-error-post",
                "timestamp": "2026-03-07T12:02:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p, _START_EVENT, _USER_MSG, _SHUTDOWN_EVENT, tool_exec, session_error
        )
        events = parse_events(p)

        summary = build_session_summary(events)

        assert summary.is_active is False
        assert summary.end_time is not None


# ---------------------------------------------------------------------------
# Issue #685 — _build_completed_summary: defensive shutdown-index guard
# ---------------------------------------------------------------------------


class TestBuildCompletedSummaryShutdownIndexGuard:
    """Verify the defensive idx < len(events) guard in _build_completed_summary."""

    def test_shutdown_idx_out_of_bounds_yields_none_timestamp(self) -> None:
        """When shutdown event index equals len(events), timestamp is None."""
        sd = SessionShutdownData(
            shutdownType="routine",
            totalPremiumRequests=3,
            totalApiDurationMs=5000,
        )
        # Hand-craft a _FirstPassResult with shutdown index == 2, but we will
        # only supply 2 events (indices 0 and 1), so idx 2 is out of bounds.
        fp = _FirstPassResult(
            session_id="oob-session",
            start_time=datetime(2026, 3, 7, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 3, 7, 11, 0, tzinfo=UTC),
            cwd="/home/user/project",
            model="claude-sonnet-4",
            all_shutdowns=((2, sd),),
            user_message_count=1,
            total_output_tokens=0,
            total_turn_starts=0,
            tool_model=None,
        )
        resume = _ResumeInfo(
            session_resumed=False,
            post_shutdown_output_tokens=0,
            post_shutdown_turn_starts=0,
            post_shutdown_user_messages=0,
            last_resume_time=None,
        )
        # Only 2 events → index 2 is out of bounds
        events: list[SessionEvent] = [
            SessionEvent(
                type=EventType.SESSION_START,
                data={
                    "sessionId": "oob-session",
                    "version": 1,
                    "startTime": "2026-03-07T10:00:00.000Z",
                    "context": {"cwd": "/home/user/project"},
                },
                id="ev-start",
                timestamp=datetime(2026, 3, 7, 10, 0, tzinfo=UTC),
            ),
            SessionEvent(
                type=EventType.USER_MESSAGE,
                data={"content": "hello"},
                id="ev-user",
                timestamp=datetime(2026, 3, 7, 10, 1, tzinfo=UTC),
            ),
        ]

        summary = _build_completed_summary(fp, name=None, resume=resume, events=events)

        # The guard should produce None instead of raising IndexError
        assert len(summary.shutdown_cycles) == 1
        assert summary.shutdown_cycles[0][0] is None
        assert summary.shutdown_cycles[0][1] is sd


# -------------------------------------------------------------------
# _MAX_CACHED_EVENTS raised to 32 — regression test
# -------------------------------------------------------------------


class TestEventsCacheLimitCoversTypicalSessions:
    """Verify that _EVENTS_CACHE holds >8 sessions after get_all_sessions.

    Before the fix, _MAX_CACHED_EVENTS was 8, so any session ranked 9th
    or beyond would miss the events cache and trigger a redundant
    parse_events call on every detail-view navigation.  With the limit
    raised to 32, all sessions in a typical installation are cached.
    """

    def test_no_redundant_parse_after_warmup(self, tmp_path: Path) -> None:
        """Create 12 sessions, warm caches, then assert no re-parses."""
        num_sessions = 12
        paths: list[Path] = []
        for i in range(num_sessions):
            ep = _make_completed_session(tmp_path, f"session-{i:02d}", f"sid-{i:04d}")
            paths.append(ep)

        # Warm both _SESSION_CACHE and _EVENTS_CACHE via get_all_sessions.
        summaries = get_all_sessions(tmp_path)
        assert len(summaries) == num_sessions

        # All 12 sessions should fit in a 32-entry events cache.
        assert len(_EVENTS_CACHE) == num_sessions

        # Now call get_cached_events for every session and assert that
        # parse_events is *never* invoked (all hits come from cache).
        with patch("copilot_usage.parser.parse_events", wraps=parse_events) as spy:
            for ep in paths:
                get_cached_events(ep)
            assert spy.call_count == 0


# ---------------------------------------------------------------------------
# Issue #722 — _SESSION_CACHE bounded by _MAX_CACHED_SESSIONS
# ---------------------------------------------------------------------------


class TestSessionCacheLRUEviction:
    """_SESSION_CACHE is bounded by _MAX_CACHED_SESSIONS and uses LRU eviction."""

    def test_cache_bounded_after_many_sessions(self, tmp_path: Path) -> None:
        """get_all_sessions with > _MAX_CACHED_SESSIONS dirs caps the cache."""
        total = _MAX_CACHED_SESSIONS + 10
        created_paths: list[Path] = []
        base_mtime = time.time()

        for i in range(total):
            session_path = _make_completed_session(
                tmp_path, f"sess-{i:04d}", f"sid-{i:04d}"
            )
            created_paths.append(session_path)

            # Make recency deterministic so the newest sessions are unambiguous.
            mtime = base_mtime + i
            os.utime(session_path, (mtime, mtime))
            os.utime(session_path.parent, (mtime, mtime))

        summaries = get_all_sessions(tmp_path)
        assert len(summaries) == total
        assert len(_SESSION_CACHE) == _MAX_CACHED_SESSIONS

        oldest_paths = created_paths[: total - _MAX_CACHED_SESSIONS]
        newest_paths = created_paths[total - _MAX_CACHED_SESSIONS :]

        for path in oldest_paths:
            assert path not in _SESSION_CACHE

        for path in newest_paths:
            assert path in _SESSION_CACHE

    def test_insert_session_entry_evicts_lru(self) -> None:
        """_insert_session_entry evicts the oldest entry at capacity."""
        _SESSION_CACHE.clear()
        dummy = _CachedSession(
            file_id=(1, 1),
            plan_id=None,
            config_model=None,
            depends_on_config=False,
            summary=SessionSummary(session_id="s"),
        )
        # Fill to capacity.
        for i in range(_MAX_CACHED_SESSIONS):
            _insert_session_entry(Path(f"/fake/{i}/events.jsonl"), dummy)
        assert len(_SESSION_CACHE) == _MAX_CACHED_SESSIONS

        first_key = next(iter(_SESSION_CACHE))

        # Insert one more — should evict the first.
        _insert_session_entry(Path("/fake/new/events.jsonl"), dummy)
        assert len(_SESSION_CACHE) == _MAX_CACHED_SESSIONS
        assert first_key not in _SESSION_CACHE
        assert Path("/fake/new/events.jsonl") in _SESSION_CACHE

    def test_cache_hit_promotes_to_mru(self, tmp_path: Path) -> None:
        """Accessing a cached session promotes it in correct LRU order."""
        # Create 3 sessions and force a deterministic discovery order:
        # newest-first by mtime => p3, p2, p1.
        p1 = _make_completed_session(tmp_path, "sess-a", "sid-a")
        p2 = _make_completed_session(tmp_path, "sess-b", "sid-b")
        p3 = _make_completed_session(tmp_path, "sess-c", "sid-c")
        base_time = time.time()
        os.utime(p1, (base_time - 30, base_time - 30))
        os.utime(p2, (base_time - 20, base_time - 20))
        os.utime(p3, (base_time - 10, base_time - 10))

        get_all_sessions(tmp_path)
        assert p1 in _SESSION_CACHE
        assert p2 in _SESSION_CACHE
        assert p3 in _SESSION_CACHE

        # After first call, oldest (p1) should be at front (LRU) and
        # newest (p3) at back (MRU).
        assert list(_SESSION_CACHE) == [p1, p2, p3]

        # Scramble the cache order so the next call must actively
        # restore the correct LRU ordering.
        _SESSION_CACHE.move_to_end(p3, last=False)
        assert list(_SESSION_CACHE) == [p3, p1, p2]

        # Second call hits the cache; all sessions should be promoted in
        # oldest→newest order, restoring newest-at-MRU.
        get_all_sessions(tmp_path)

        # Cache should still contain all three sessions, and the newest
        # entry (p3) should be last (MRU).
        assert len(_SESSION_CACHE) == 3
        assert list(_SESSION_CACHE) == [p1, p2, p3]

    def test_stale_pruning_bounded(self, tmp_path: Path) -> None:
        """Stale entries for deleted session directories are removed from the cache."""
        import shutil

        total = 5
        paths: list[Path] = []
        for i in range(total):
            ep = _make_completed_session(tmp_path, f"sess-{i}", f"sid-{i}")
            paths.append(ep)

        get_all_sessions(tmp_path)
        assert len(_SESSION_CACHE) == total

        # Delete 2 sessions from disk.
        shutil.rmtree(paths[1].parent)
        shutil.rmtree(paths[3].parent)

        summaries = get_all_sessions(tmp_path)
        assert len(summaries) == total - 2
        assert paths[1] not in _SESSION_CACHE
        assert paths[3] not in _SESSION_CACHE
        assert len(_SESSION_CACHE) == total - 2

    def test_stale_events_cache_pruned_on_session_delete(self, tmp_path: Path) -> None:
        """_EVENTS_CACHE entries for deleted sessions are pruned alongside _SESSION_CACHE."""
        import shutil

        total = 3
        paths: list[Path] = []
        for i in range(total):
            ep = _make_completed_session(tmp_path, f"sess-{i}", f"sid-{i}")
            paths.append(ep)

        get_all_sessions(tmp_path)
        assert len(_SESSION_CACHE) == total
        # _EVENTS_CACHE is populated for at least the newest sessions.
        cached_events_before = [p for p in paths if p in _EVENTS_CACHE]
        assert len(cached_events_before) > 0

        # Delete the middle session from disk.
        shutil.rmtree(paths[1].parent)

        summaries = get_all_sessions(tmp_path)
        assert len(summaries) == total - 1
        assert paths[1] not in _SESSION_CACHE
        assert paths[1] not in _EVENTS_CACHE


# ---------------------------------------------------------------------------
# Issue #723 — _first_pass frozenset pre-filter performance
# ---------------------------------------------------------------------------


class TestFirstPassPreFilter:
    """Verify the _FIRST_PASS_EVENT_TYPES frozenset guard works correctly."""

    def test_frozenset_contains_all_handled_types(self) -> None:
        """_FIRST_PASS_EVENT_TYPES covers exactly the five elif-chain types.

        TOOL_EXECUTION_COMPLETE is handled separately before the frozenset
        filter (issue #772) and is intentionally excluded.
        """
        expected = frozenset(
            {
                EventType.SESSION_START,
                EventType.SESSION_SHUTDOWN,
                EventType.USER_MESSAGE,
                EventType.ASSISTANT_TURN_START,
                EventType.ASSISTANT_MESSAGE,
            }
        )
        assert expected == _FIRST_PASS_EVENT_TYPES

    def test_unhandled_types_not_in_frozenset(self) -> None:
        """Unhandled event types are not in _FIRST_PASS_EVENT_TYPES."""
        unhandled = [
            EventType.TOOL_EXECUTION_START,
            EventType.ASSISTANT_TURN_END,
            EventType.SESSION_RESUME,
            EventType.SESSION_ERROR,
            EventType.SESSION_PLAN_CHANGED,
            EventType.SESSION_WORKSPACE_FILE_CHANGED,
            EventType.ABORT,
        ]
        for et in unhandled:
            assert et not in _FIRST_PASS_EVENT_TYPES

    def test_first_pass_skips_unhandled_events(self, tmp_path: Path) -> None:
        """_first_pass produces correct results when unhandled events are mixed in."""
        ts = "2026-03-07T10:00:00.000Z"
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "perf-test",
                    "startTime": ts,
                    "context": {"cwd": "/test"},
                },
                "id": "ev-start",
                "timestamp": ts,
            }
        )
        user_msg = json.dumps(
            {
                "type": "user.message",
                "data": {},
                "id": "ev-user",
                "timestamp": ts,
            }
        )
        # Unhandled events that should be skipped
        unhandled_events = [
            json.dumps(
                {
                    "type": "tool.execution_start",
                    "data": {},
                    "id": f"ev-unhandled-{i}",
                    "timestamp": ts,
                }
            )
            for i in range(20)
        ]
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, start, *unhandled_events, user_msg)
        events = parse_events(p)
        fp = _first_pass(events)
        assert fp.session_id == "perf-test"
        assert fp.user_message_count == 1

    def test_first_pass_10k_events_prefilter_consulted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The frozenset pre-filter is consulted exactly once per non-tool event.

        Instead of a wall-clock ``timeit`` assertion (which is flaky on
        shared/loaded CI runners), we monkeypatch the module-level frozenset
        with a counting wrapper and verify it is checked once per non-tool
        event.  TOOL_EXECUTION_COMPLETE events are short-circuited before the
        frozenset filter and therefore never consult it.
        """
        ts = "2026-03-07T10:00:00.000Z"
        start_line = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "bench",
                    "startTime": ts,
                    "context": {"cwd": "/bench"},
                },
                "id": "ev-start",
                "timestamp": ts,
            }
        )
        # Build a realistic mix: ~40% handled, ~60% unhandled
        handled_types: list[tuple[str, dict[str, str | int]]] = [
            ("user.message", {}),
            ("assistant.turn_start", {}),
            ("assistant.message", {"outputTokens": 10}),
            ("tool.execution_complete", {"model": "gpt-4"}),
        ]
        unhandled_types = [
            "tool.execution_start",
            "assistant.turn_end",
            "session.resume",
            "session.error",
            "session.plan_changed",
            "session.workspace_file_changed",
        ]
        lines = [start_line]
        for i in range(9999):
            if i % 10 < 4:
                etype, data = handled_types[i % len(handled_types)]
                lines.append(
                    json.dumps(
                        {
                            "type": etype,
                            "data": data,
                            "id": f"ev-{i}",
                            "timestamp": ts,
                        }
                    )
                )
            else:
                etype_str = unhandled_types[i % len(unhandled_types)]
                lines.append(
                    json.dumps(
                        {
                            "type": etype_str,
                            "data": {},
                            "id": f"ev-{i}",
                            "timestamp": ts,
                        }
                    )
                )

        p = tmp_path / "bench" / "events.jsonl"
        _write_events(p, *lines)
        events = parse_events(p)
        assert len(events) == 10000

        # TOOL_EXECUTION_COMPLETE events are handled *before* the frozenset
        # filter, so they never consult _FIRST_PASS_EVENT_TYPES.  Count only
        # the non-tool events that reach the frozenset.
        tool_event_count = sum(
            1 for e in events if e.type == EventType.TOOL_EXECUTION_COMPLETE
        )
        expected_checks = len(events) - tool_event_count

        check_count = 0
        original = _FIRST_PASS_EVENT_TYPES

        class _CountingFrozenset(frozenset):  # type: ignore[type-arg]
            def __contains__(self, item: object) -> bool:
                nonlocal check_count
                check_count += 1
                return item in original

        monkeypatch.setattr(
            "copilot_usage.parser._FIRST_PASS_EVENT_TYPES",
            _CountingFrozenset(original),
        )

        fp = _first_pass(events)

        # Guard consulted exactly once per non-tool event
        assert check_count == expected_checks, (
            f"Expected {expected_checks} frozenset membership checks, got {check_count}"
        )
        assert fp.session_id == "bench"


# ---------------------------------------------------------------------------
# Issue #772 — TOOL_EXECUTION_COMPLETE short-circuits after tool_model found
# ---------------------------------------------------------------------------


class TestToolCompleteShortCircuit:
    """TOOL_EXECUTION_COMPLETE events bypass the frozenset filter entirely.

    Once ``tool_model`` is resolved, each subsequent TOOL_EXECUTION_COMPLETE
    event should cost only one string comparison + one None-check + continue,
    instead of traversing the full elif chain.
    """

    def test_1000_tool_events_resolves_model_at_index_3(self, tmp_path: Path) -> None:
        """1 000 tool.execution_complete events; only index 3 carries a model.

        Asserts ``_first_pass().tool_model == 'gpt-4o'`` when the first model
        appears on the fourth tool event.
        """
        ts = "2026-03-07T10:00:00.000Z"
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "tool-sc",
                    "startTime": ts,
                    "context": {"cwd": "/t"},
                },
                "id": "ev-start",
                "timestamp": ts,
            }
        )
        user_msg = json.dumps(
            {
                "type": "user.message",
                "data": {},
                "id": "ev-user",
                "timestamp": ts,
            }
        )
        tool_events: list[str] = []
        for i in range(1_000):
            data: dict[str, str | bool] = {
                "toolCallId": f"tc-{i}",
                "success": True,
            }
            if i == 3:
                data["model"] = "gpt-4o"
            tool_events.append(
                json.dumps(
                    {
                        "type": "tool.execution_complete",
                        "data": data,
                        "id": f"ev-tool-{i}",
                        "timestamp": ts,
                    }
                )
            )

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, start, user_msg, *tool_events)
        events = parse_events(p)
        fp = _first_pass(events)

        assert fp.tool_model == "gpt-4o"
        assert fp.session_id == "tool-sc"
        assert fp.user_message_count == 1

    def test_1000_tool_events_bypass_frozenset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Frozenset pre-filter is never consulted for TOOL_EXECUTION_COMPLETE.

        Uses a counting wrapper around the module-level frozenset to verify
        that tool events are handled before the frozenset membership check.
        """
        ts = "2026-03-07T10:00:00.000Z"
        start = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "tool-bypass",
                    "startTime": ts,
                    "context": {"cwd": "/t"},
                },
                "id": "ev-start",
                "timestamp": ts,
            }
        )
        user_msg = json.dumps(
            {
                "type": "user.message",
                "data": {},
                "id": "ev-user",
                "timestamp": ts,
            }
        )
        tool_events = [
            json.dumps(
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "toolCallId": f"tc-{i}",
                        "success": True,
                        **({"model": "gpt-4o"} if i == 3 else {}),
                    },
                    "id": f"ev-tool-{i}",
                    "timestamp": ts,
                }
            )
            for i in range(1_000)
        ]

        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, start, user_msg, *tool_events)
        events = parse_events(p)

        check_count = 0
        original = _FIRST_PASS_EVENT_TYPES

        class _CountingFrozenset(frozenset):  # type: ignore[type-arg]
            def __contains__(self, item: object) -> bool:
                nonlocal check_count
                check_count += 1
                return item in original

        monkeypatch.setattr(
            "copilot_usage.parser._FIRST_PASS_EVENT_TYPES",
            _CountingFrozenset(original),
        )

        fp = _first_pass(events)

        # Only start + user_msg should consult the frozenset (2 events)
        assert check_count == 2, (
            f"Expected 2 frozenset checks (start + user_msg), got {check_count}"
        )
        assert fp.tool_model == "gpt-4o"
        assert fp.session_id == "tool-bypass"


# ---------------------------------------------------------------------------
# Issue #756 — _detect_resume: if/elif chain short-circuits after first match
# ---------------------------------------------------------------------------


class TestDetectResumeElifShortCircuit:
    """Verify _detect_resume uses a short-circuiting if/elif chain.

    With the old implementation, every event triggered 5 unconditional ``if``
    checks.  The new ``if/elif`` chain should evaluate at most 4 comparisons
    per event and exactly 1 for the most common ``ASSISTANT_MESSAGE`` case.
    """

    def test_comparison_count_500_assistant_messages(self, tmp_path: Path) -> None:
        """500 post-shutdown ASSISTANT_MESSAGE events → ≤ 500 type comparisons.

        Uses a spy wrapper around ``ev.type`` access to count the total number
        of equality comparisons performed inside the _detect_resume loop.
        """
        # Build a minimal session: start → user → shutdown → 500 assistant msgs
        start_raw = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "cmp-bench",
                    "machineId": "m1",
                    "parentSessionId": None,
                    "repoName": "test-repo",
                    "repoUrl": "https://example.com/repo",
                    "clientVersion": "1.0.0",
                    "extensionVersion": "1.0.0",
                    "userAgent": "test",
                    "repoDevcontainerConfig": None,
                },
                "id": "ev-start",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        user_raw = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "hello"},
                "id": "ev-u0",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        shutdown_raw = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "modelMetrics": {},
                },
                "id": "ev-sd",
                "timestamp": "2026-03-07T11:00:00.000Z",
            }
        )
        asst_events: list[str] = [
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {
                        "messageId": f"m{n}",
                        "content": f"reply{n}",
                        "toolRequests": [],
                        "interactionId": f"i{n}",
                        "outputTokens": 10,
                    },
                    "id": f"ev-a{n}",
                    "timestamp": "2026-03-07T12:00:00.000Z",
                }
            )
            for n in range(500)
        ]
        p = tmp_path / "s" / "events.jsonl"
        _write_events(p, start_raw, user_raw, shutdown_raw, *asst_events)
        events = parse_events(p)
        fp = _first_pass(events)

        # Spy: wrap each post-shutdown event's type to count == comparisons
        eq_count = 0
        last_sd_idx = fp.all_shutdowns[-1][0]

        class _SpyType:
            """Proxy around EventType that counts __eq__ calls."""

            __slots__ = ("_real",)

            def __init__(self, real: str) -> None:
                object.__setattr__(self, "_real", real)

            def __eq__(self, other: object) -> bool:
                nonlocal eq_count
                eq_count += 1
                return self._real == other

            def __hash__(self) -> int:
                return hash(self._real)

        for idx in range(last_sd_idx + 1, len(events)):
            object.__setattr__(events[idx], "type", _SpyType(events[idx].type))

        result = _detect_resume(events, fp.all_shutdowns)

        # Correctness: all 500 assistant messages recognised
        assert result.session_resumed is True
        assert result.post_shutdown_output_tokens == 5000
        assert result.post_shutdown_turn_starts == 0
        assert result.post_shutdown_user_messages == 0

        # Performance: with an elif chain, ASSISTANT_MESSAGE is the first
        # branch so each event needs exactly 1 comparison → 500 total.
        # Allow a small margin for any non-assistant events (there are none
        # here, but be robust).
        assert eq_count <= 500, (
            f"Expected ≤ 500 ev.type comparisons for 500 ASSISTANT_MESSAGE "
            f"events with if/elif chain, got {eq_count}"
        )

    def test_correctness_mixed_post_shutdown_events(self, tmp_path: Path) -> None:
        """Mixed post-shutdown events produce correct counters with elif chain."""
        start_raw = json.dumps(
            {
                "type": "session.start",
                "data": {
                    "sessionId": "mix-bench",
                    "machineId": "m1",
                    "parentSessionId": None,
                    "repoName": "test-repo",
                    "repoUrl": "https://example.com/repo",
                    "clientVersion": "1.0.0",
                    "extensionVersion": "1.0.0",
                    "userAgent": "test",
                    "repoDevcontainerConfig": None,
                },
                "id": "ev-start",
                "timestamp": "2026-03-07T10:00:00.000Z",
            }
        )
        user_pre = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "pre"},
                "id": "ev-u-pre",
                "timestamp": "2026-03-07T10:01:00.000Z",
            }
        )
        shutdown_raw = json.dumps(
            {
                "type": "session.shutdown",
                "data": {
                    "totalPremiumRequests": 0,
                    "totalApiDurationMs": 0,
                    "modelMetrics": {},
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
        user_post = json.dumps(
            {
                "type": "user.message",
                "data": {"content": "post"},
                "id": "ev-u-post",
                "timestamp": "2026-03-07T12:01:00.000Z",
            }
        )
        turn_start = json.dumps(
            {
                "type": "assistant.turn_start",
                "data": {"turnId": "t1"},
                "id": "ev-ts",
                "timestamp": "2026-03-07T12:01:30.000Z",
            }
        )
        asst_msg = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "messageId": "m1",
                    "content": "hi",
                    "toolRequests": [],
                    "interactionId": "i1",
                    "outputTokens": 42,
                },
                "id": "ev-a1",
                "timestamp": "2026-03-07T12:02:00.000Z",
            }
        )
        tool_exec = json.dumps(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "tc1",
                    "model": "claude-sonnet-4",
                    "interactionId": "int-1",
                    "success": True,
                },
                "id": "ev-tool",
                "timestamp": "2026-03-07T12:03:00.000Z",
            }
        )
        p = tmp_path / "s" / "events.jsonl"
        _write_events(
            p,
            start_raw,
            user_pre,
            shutdown_raw,
            resume_ev,
            user_post,
            turn_start,
            asst_msg,
            tool_exec,
        )
        events = parse_events(p)
        fp = _first_pass(events)
        result = _detect_resume(events, fp.all_shutdowns)

        assert result.session_resumed is True
        assert result.last_resume_time == datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
        assert result.post_shutdown_user_messages == 1
        assert result.post_shutdown_turn_starts == 1
        assert result.post_shutdown_output_tokens == 42
