"""Tests for copilot_usage.vscode_parser and the vscode CLI subcommand."""

import os
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from loguru import logger

from copilot_usage._fs_utils import safe_file_identity
from copilot_usage.cli import main
from copilot_usage.vscode_parser import (
    _CCREQ_RE,  # pyright: ignore[reportPrivateUsage]
    _MAX_CACHED_VSCODE_LOGS,  # pyright: ignore[reportPrivateUsage]
    _PER_FILE_SUMMARY_CACHE,  # pyright: ignore[reportPrivateUsage]
    _VSCODE_DISCOVERY_CACHE,  # pyright: ignore[reportPrivateUsage]
    _VSCODE_LOG_CACHE,  # pyright: ignore[reportPrivateUsage]
    VSCodeLogSummary,
    VSCodeRequest,
    _cached_discover_vscode_logs,  # pyright: ignore[reportPrivateUsage]
    _default_log_candidates,  # pyright: ignore[reportPrivateUsage]
    _get_cached_vscode_requests,  # pyright: ignore[reportPrivateUsage]
    _merge_partial,  # pyright: ignore[reportPrivateUsage]
    _scan_child_ids,  # pyright: ignore[reportPrivateUsage]
    _SummaryAccumulator,  # pyright: ignore[reportPrivateUsage]
    _update_vscode_summary,  # pyright: ignore[reportPrivateUsage]
    _VSCodeDiscoveryCache,  # pyright: ignore[reportPrivateUsage]
    build_vscode_summary,
    discover_vscode_logs,
    get_vscode_summary,
    parse_vscode_log,
)

# ---------------------------------------------------------------------------
# Sample log lines
# ---------------------------------------------------------------------------

_LOG_OPUS = (
    "2026-03-13 22:10:24.523 [info] ccreq:c0c8885e.copilotmd"
    " | success | claude-opus-4.6 | 8003ms | [panel/editAgent]"
)
_LOG_REDIRECT = (
    "2026-03-13 22:10:48.752 [info] ccreq:e120f69a.copilotmd"
    " | success | gpt-4o-mini -> gpt-4o-mini-2024-07-18 | 481ms"
    " | [copilotLanguageModelWrapper]"
)
_LOG_GPT4O = (
    "2026-03-13 22:10:16.597 [info] ccreq:2fad3591.copilotmd"
    " | success | gpt-4o-mini-2024-07-18 | 432ms | [title]"
)
_LOG_NOISE = (
    "2026-03-13 21:48:39.404 [info] [GitExtensionServiceImpl]"
    " Initializing Git extension service."
)


@pytest.fixture(autouse=True)
def _clear_vscode_caches() -> None:  # pyright: ignore[reportUnusedFunction]
    """Ensure every test starts with empty VS Code caches."""
    _VSCODE_LOG_CACHE.clear()
    _PER_FILE_SUMMARY_CACHE.clear()
    _VSCODE_DISCOVERY_CACHE.clear()
    import copilot_usage.vscode_parser as _mod

    _mod._vscode_summary_cache = None  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# _CCREQ_RE regex
# ---------------------------------------------------------------------------


class TestCcreqRegex:
    def test_normal_line(self) -> None:
        m = _CCREQ_RE.match(_LOG_OPUS)
        assert m is not None
        ts, req_id, model, dur, cat = m.groups()
        assert ts == "2026-03-13 22:10:24.523"
        assert req_id == "c0c8885e"
        assert model == "claude-opus-4.6"
        assert dur == "8003"
        assert cat == "panel/editAgent"

    def test_redirect_line(self) -> None:
        m = _CCREQ_RE.match(_LOG_REDIRECT)
        assert m is not None
        _, _, model, dur, cat = m.groups()
        assert model == "gpt-4o-mini"
        assert dur == "481"
        assert cat == "copilotLanguageModelWrapper"

    def test_plain_model_line(self) -> None:
        m = _CCREQ_RE.match(_LOG_GPT4O)
        assert m is not None
        _, _, model, dur, cat = m.groups()
        assert model == "gpt-4o-mini-2024-07-18"
        assert dur == "432"
        assert cat == "title"

    def test_noise_line_does_not_match(self) -> None:
        assert _CCREQ_RE.match(_LOG_NOISE) is None

    def test_empty_line_does_not_match(self) -> None:
        assert _CCREQ_RE.match("") is None


# ---------------------------------------------------------------------------
# parse_vscode_log
# ---------------------------------------------------------------------------


class TestParseVscodeLog:
    def test_parses_real_lines(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        log_file.write_text(
            "\n".join([_LOG_OPUS, _LOG_NOISE, _LOG_REDIRECT, _LOG_GPT4O]),
            encoding="utf-8",
        )
        requests = parse_vscode_log(log_file)
        assert len(requests) == 3
        assert requests[0].model == "claude-opus-4.6"
        assert requests[0].duration_ms == 8003
        assert requests[1].model == "gpt-4o-mini"
        assert requests[2].model == "gpt-4o-mini-2024-07-18"

    def test_empty_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "empty.log"
        log_file.write_text("", encoding="utf-8")
        assert parse_vscode_log(log_file) == []

    def test_missing_file_raises_oserror(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_such.log"
        with pytest.raises(OSError):
            parse_vscode_log(missing)

    def test_invalid_timestamp_line_is_skipped(self, tmp_path: Path) -> None:
        """A regex-matching line with an unparseable timestamp is skipped."""
        bad_ts = "9999-99-99 99:99:99.000"  # impossible date triggers ValueError
        bad_line = (
            f"{bad_ts} [info] ccreq:abc123.copilotmd"
            " | success | claude-sonnet-4 | 100ms | [panel]"
        )
        # Ensure the constructed line still matches the _CCREQ_RE regex; otherwise
        # this test would no longer exercise the ValueError timestamp branch.
        assert _CCREQ_RE.match(bad_line) is not None
        good_line = _LOG_OPUS  # a valid known-good line
        log_file = tmp_path / "test.log"
        log_file.write_text(f"{bad_line}\n{good_line}", encoding="utf-8")
        result = parse_vscode_log(log_file)
        assert len(result) == 1  # bad line skipped
        assert result[0].model == "claude-opus-4.6"

    def test_all_lines_invalid_timestamp_returns_empty_list(
        self, tmp_path: Path
    ) -> None:
        """All lines match _CCREQ_RE but have invalid timestamps → returns [], not None."""
        bad_ts = "9999-99-99 99:99:99.000"
        bad_line = (
            f"{bad_ts} [info] ccreq:abc123.copilotmd"
            " | success | claude-sonnet-4 | 100ms | [panel]"
        )
        assert _CCREQ_RE.match(bad_line) is not None  # regex matches
        log_file = tmp_path / "all_bad.log"
        log_file.write_text(f"{bad_line}\n{bad_line}\n", encoding="utf-8")
        result = parse_vscode_log(log_file)
        assert result == []

    def test_non_utf8_only_file_returns_empty(self, tmp_path: Path) -> None:
        """A file containing only non-UTF-8 bytes returns [] without raising."""
        log_path = tmp_path / "test.log"
        log_path.write_bytes(b"\xff\xfe\x80\x81\x82")
        result = parse_vscode_log(log_path)
        assert result == []

    def test_valid_lines_around_non_utf8_bytes_are_parsed(self, tmp_path: Path) -> None:
        """Valid ccreq lines survive surrounding non-UTF-8 garbage."""
        valid_line = (
            b"2026-01-15 10:00:00.000 [info] ccreq:abc123.copilotmd"
            b" | success | gpt-4o | 500ms | [chat]\n"
        )
        log_path = tmp_path / "test.log"
        log_path.write_bytes(
            b"\xff\xfe garbage\n" + valid_line + b"\x80\x81 more garbage\n"
        )
        result = parse_vscode_log(log_path)
        assert len(result) == 1
        assert result[0].request_id == "abc123"

    def test_ccreq_line_with_replacement_char_is_skipped(self, tmp_path: Path) -> None:
        """A ccreq line whose timestamp contains a non-UTF-8 byte is skipped."""
        # b"\\xff" in the middle of the timestamp → replaced with U+FFFD →
        # regex fails to match the corrupted timestamp field.
        corrupted_line = (
            b"2026-01\xff15 10:00:00.000 [info] ccreq:xyz.copilotmd"
            b" | success | gpt-4o | 200ms | [chat]\n"
        )
        log_path = tmp_path / "test.log"
        log_path.write_bytes(corrupted_line)
        result = parse_vscode_log(log_path)
        assert result == []


# ---------------------------------------------------------------------------
# build_vscode_summary
# ---------------------------------------------------------------------------


class TestBuildVscodeSummary:
    def _make_requests(self) -> list[VSCodeRequest]:
        from datetime import datetime

        return [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 13, 22, 10, 24),
                request_id="aaa",
                model="claude-opus-4.6",
                duration_ms=8003,
                category="panel/editAgent",
            ),
            VSCodeRequest(
                timestamp=datetime(2026, 3, 13, 22, 10, 48),
                request_id="bbb",
                model="gpt-4o-mini",
                duration_ms=481,
                category="copilotLanguageModelWrapper",
            ),
            VSCodeRequest(
                timestamp=datetime(2026, 3, 14, 10, 0, 0),
                request_id="ccc",
                model="claude-opus-4.6",
                duration_ms=1200,
                category="panel/editAgent",
            ),
        ]

    def test_total_counts(self) -> None:
        summary = build_vscode_summary(self._make_requests())
        assert summary.total_requests == 3
        assert summary.total_duration_ms == 8003 + 481 + 1200

    def test_requests_by_model(self) -> None:
        summary = build_vscode_summary(self._make_requests())
        assert summary.requests_by_model["claude-opus-4.6"] == 2
        assert summary.requests_by_model["gpt-4o-mini"] == 1

    def test_duration_by_model(self) -> None:
        summary = build_vscode_summary(self._make_requests())
        assert summary.duration_by_model["claude-opus-4.6"] == 8003 + 1200
        assert summary.duration_by_model["gpt-4o-mini"] == 481

    def test_requests_by_date(self) -> None:
        summary = build_vscode_summary(self._make_requests())
        assert summary.requests_by_date["2026-03-13"] == 2
        assert summary.requests_by_date["2026-03-14"] == 1

    def test_first_last_timestamps(self) -> None:
        from datetime import datetime

        summary = build_vscode_summary(self._make_requests())
        assert summary.first_timestamp == datetime(2026, 3, 13, 22, 10, 24)
        assert summary.last_timestamp == datetime(2026, 3, 14, 10, 0, 0)

    def test_empty_requests(self) -> None:
        summary = build_vscode_summary([])
        assert summary.total_requests == 0
        assert summary.first_timestamp is None
        assert summary.last_timestamp is None

    def test_log_files_parsed_default_is_zero(self) -> None:
        summary = build_vscode_summary(self._make_requests())
        assert summary.log_files_parsed == 0

    def test_log_files_parsed_keyword(self) -> None:
        summary = build_vscode_summary(self._make_requests(), log_files_parsed=3)
        assert summary.log_files_parsed == 3

    def test_log_files_found_param(self) -> None:
        summary = build_vscode_summary(
            self._make_requests(), log_files_parsed=2, log_files_found=5
        )
        assert summary.log_files_found == 5
        assert summary.log_files_parsed == 2

    def test_first_last_timestamps_unsorted_input(self) -> None:
        """build_vscode_summary must derive correct bounds even when input is not chronological."""
        from datetime import datetime

        requests = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 14, 14, 0),
                request_id="a",
                model="gpt-4o",
                duration_ms=100,
                category="cat",
            ),  # middle
            VSCodeRequest(
                timestamp=datetime(2026, 3, 14, 10, 0),
                request_id="b",
                model="gpt-4o",
                duration_ms=200,
                category="cat",
            ),  # earliest
            VSCodeRequest(
                timestamp=datetime(2026, 3, 14, 18, 0),
                request_id="c",
                model="gpt-4o",
                duration_ms=150,
                category="cat",
            ),  # latest
        ]
        summary = build_vscode_summary(requests)
        assert summary.first_timestamp == datetime(2026, 3, 14, 10, 0)
        assert summary.last_timestamp == datetime(2026, 3, 14, 18, 0)


# ---------------------------------------------------------------------------
# discover_vscode_logs — platform defaults
# ---------------------------------------------------------------------------


class TestDiscoverVscodeLogs:
    def test_custom_base_path(self, tmp_path: Path) -> None:
        """Custom base_path with no matching files returns empty list."""
        assert discover_vscode_logs(tmp_path) == []

    def test_finds_log_files(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS, encoding="utf-8")
        logs = discover_vscode_logs(tmp_path)
        assert len(logs) == 1
        assert logs[0] == log_file

    def test_default_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
        with patch.object(Path, "is_dir", return_value=False):
            result = discover_vscode_logs()
        assert result == []

    def test_default_windows_no_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows without APPDATA uses the home-relative fallback path."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "win32")
        monkeypatch.setenv("APPDATA", "")  # empty → falsy
        with patch.object(
            Path, "is_dir", autospec=True, return_value=False
        ) as mock_is_dir:
            result = discover_vscode_logs()
        mock_is_dir.assert_any_call(
            Path.home() / "AppData" / "Roaming" / "Code" / "logs"
        )
        assert result == []

    def test_default_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "darwin")
        monkeypatch.delenv("APPDATA", raising=False)
        with patch.object(Path, "is_dir", return_value=False):
            result = discover_vscode_logs()
        assert result == []

    def test_default_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "linux")
        monkeypatch.delenv("APPDATA", raising=False)
        with patch.object(Path, "is_dir", return_value=False):
            result = discover_vscode_logs()
        assert result == []

    # -- Insiders default discovery -----------------------------------------

    def test_default_linux_insiders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Linux Insiders log directory is discovered by default."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "linux")
        monkeypatch.delenv("APPDATA", raising=False)
        fake_home = tmp_path / "fakehome"
        insiders = fake_home / ".config" / "Code - Insiders" / "logs"
        log_dir = insiders / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS, encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        result = discover_vscode_logs()
        assert len(result) == 1
        assert result[0] == log_file

    def test_default_macos_insiders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS Insiders log directory is discovered by default."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "darwin")
        monkeypatch.delenv("APPDATA", raising=False)
        fake_home = tmp_path / "fakehome"
        insiders = (
            fake_home / "Library" / "Application Support" / "Code - Insiders" / "logs"
        )
        log_dir = insiders / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS, encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        result = discover_vscode_logs()
        assert len(result) == 1
        assert result[0] == log_file

    def test_default_windows_insiders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows Insiders log directory is discovered by default."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "win32")
        appdata = tmp_path / "AppData"
        monkeypatch.setenv("APPDATA", str(appdata))
        insiders = appdata / "Code - Insiders" / "logs"
        log_dir = insiders / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS, encoding="utf-8")
        result = discover_vscode_logs()
        assert len(result) == 1
        assert result[0] == log_file

    def test_default_windows_insiders_no_appdata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows Insiders without APPDATA uses home-relative fallback."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "win32")
        monkeypatch.setenv("APPDATA", "")
        fake_home = tmp_path / "fakehome"
        insiders = fake_home / "AppData" / "Roaming" / "Code - Insiders" / "logs"
        log_dir = insiders / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS, encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        result = discover_vscode_logs()
        assert len(result) == 1
        assert result[0] == log_file

    def test_both_stable_and_insiders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both stable and Insiders logs are returned, sorted together."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "linux")
        monkeypatch.delenv("APPDATA", raising=False)

        fake_home = tmp_path / "fakehome"
        config = fake_home / ".config"

        stable = config / "Code" / "logs"
        insiders = config / "Code - Insiders" / "logs"

        # Create a log in stable
        stable_dir = stable / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        stable_dir.mkdir(parents=True)
        stable_log = stable_dir / "GitHub Copilot Chat.log"
        stable_log.write_text(_LOG_OPUS, encoding="utf-8")

        # Create a log in Insiders
        insiders_dir = (
            insiders / "20260314" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        insiders_dir.mkdir(parents=True)
        insiders_log = insiders_dir / "GitHub Copilot Chat.log"
        insiders_log.write_text(_LOG_GPT4O, encoding="utf-8")

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        result = discover_vscode_logs()
        assert len(result) == 2
        assert result == sorted(result), "results must be sorted"
        assert stable_log in result
        assert insiders_log in result


# ---------------------------------------------------------------------------
# _default_log_candidates (direct unit tests)
# ---------------------------------------------------------------------------


class TestDefaultLogCandidates:
    """Direct tests for the platform-specific candidate directory logic."""

    def test_linux_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "linux")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        candidates = _default_log_candidates()
        assert candidates == [
            tmp_path / ".config" / "Code" / "logs",
            tmp_path / ".config" / "Code - Insiders" / "logs",
        ]

    def test_darwin_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "darwin")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        candidates = _default_log_candidates()
        assert candidates == [
            tmp_path / "Library" / "Application Support" / "Code" / "logs",
            tmp_path / "Library" / "Application Support" / "Code - Insiders" / "logs",
        ]

    def test_win32_with_appdata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        appdata = tmp_path / "CustomAppData"
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "win32")
        monkeypatch.setenv("APPDATA", str(appdata))
        candidates = _default_log_candidates()
        assert candidates == [
            appdata / "Code" / "logs",
            appdata / "Code - Insiders" / "logs",
        ]

    def test_win32_no_appdata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", "win32")
        monkeypatch.setenv("APPDATA", "")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        candidates = _default_log_candidates()
        assert candidates == [
            tmp_path / "AppData" / "Roaming" / "Code" / "logs",
            tmp_path / "AppData" / "Roaming" / "Code - Insiders" / "logs",
        ]

    @pytest.mark.parametrize("platform", ["freebsd", "openbsd", "sunos", "haiku"])
    def test_unknown_platform_falls_back_to_linux_layout(
        self,
        platform: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any unrecognised platform uses the ~/.config/Code/logs layout."""
        monkeypatch.setattr("copilot_usage.vscode_parser.sys.platform", platform)
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        candidates = _default_log_candidates()
        assert candidates == [
            tmp_path / ".config" / "Code" / "logs",
            tmp_path / ".config" / "Code - Insiders" / "logs",
        ], f"Expected ~/.config layout for platform={platform!r}"


# ---------------------------------------------------------------------------
# get_vscode_summary (end-to-end)
# ---------------------------------------------------------------------------


class TestGetVscodeSummary:
    def test_end_to_end(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        (log_dir / "GitHub Copilot Chat.log").write_text(
            "\n".join([_LOG_OPUS, _LOG_REDIRECT, _LOG_NOISE, _LOG_GPT4O]),
            encoding="utf-8",
        )
        summary = get_vscode_summary(tmp_path)
        assert summary.total_requests == 3
        assert summary.log_files_parsed == 1
        assert summary.log_files_found == 1
        assert "claude-opus-4.6" in summary.requests_by_model

    def test_no_logs(self, tmp_path: Path) -> None:
        summary = get_vscode_summary(tmp_path)
        assert summary.total_requests == 0
        assert summary.log_files_parsed == 0
        assert summary.log_files_found == 0

    def test_all_invalid_timestamps_still_counted_in_log_files_parsed(
        self, tmp_path: Path
    ) -> None:
        """File with all-invalid-timestamp lines is counted in log_files_parsed."""
        bad_ts = "9999-99-99 99:99:99.000"
        bad_line = (
            f"{bad_ts} [info] ccreq:abc123.copilotmd"
            " | success | claude-sonnet-4 | 100ms | [panel]"
        )
        log_dir = (
            tmp_path / "20260313T120000" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(f"{bad_line}\n", encoding="utf-8")
        summary = get_vscode_summary(tmp_path)
        assert summary.log_files_parsed == 1  # file read successfully, even if empty
        assert summary.total_requests == 0

    def test_incremental_aggregation(self) -> None:
        """Per-file incremental processing: requests are aggregated per file.

        Also verifies that multi-file aggregation sets first/last timestamp
        from the earliest and latest batches respectively.
        """
        from datetime import datetime
        from unittest.mock import call

        file_a = Path("/fake/log_a.log")
        file_b = Path("/fake/log_b.log")
        requests_a = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 13, 10, 0, 0),
                request_id="a1",
                model="gpt-4o",
                duration_ms=100,
                category="panel",
            ),
            VSCodeRequest(
                timestamp=datetime(2026, 3, 13, 10, 1, 0),
                request_id="a2",
                model="gpt-4o",
                duration_ms=200,
                category="panel",
            ),
        ]
        requests_b = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 14, 12, 0, 0),
                request_id="b1",
                model="claude-sonnet-4",
                duration_ms=300,
                category="inline",
            ),
        ]

        def _fake_parse(path: Path) -> list[VSCodeRequest]:
            if path == file_a:
                return list(requests_a)
            return list(requests_b)

        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=[file_a, file_b],
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                side_effect=_fake_parse,
            ) as mock_parse,
            patch(
                "copilot_usage.vscode_parser.build_vscode_summary",
            ) as mock_build,
        ):
            summary = get_vscode_summary()

        assert summary.total_requests == 3
        assert summary.log_files_parsed == 2
        assert mock_parse.call_count == 2
        mock_parse.assert_has_calls([call(file_a), call(file_b)])
        # Verify the incremental path is used: build_vscode_summary must NOT
        # be called because get_vscode_summary now aggregates per-file via
        # _update_vscode_summary instead of collecting all requests first.
        mock_build.assert_not_called()
        # Timestamp bounds span the earliest and latest batches.
        assert summary.first_timestamp == requests_a[0].timestamp
        assert summary.last_timestamp == requests_b[-1].timestamp

    def test_oserror_skips_file_and_continues(self) -> None:
        """When one log file raises OSError, the other is still processed."""
        from datetime import datetime

        file_a = Path("/fake/log_a.log")
        file_b = Path("/fake/log_b.log")
        requests_b = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 14, 12, 0, 0),
                request_id="b1",
                model="claude-sonnet-4",
                duration_ms=300,
                category="inline",
            ),
        ]

        def _fake_parse(path: Path) -> list[VSCodeRequest]:
            if path == file_a:
                raise OSError("Permission denied")
            return list(requests_b)

        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=[file_a, file_b],
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                side_effect=_fake_parse,
            ),
        ):
            summary = get_vscode_summary()

        assert summary.log_files_parsed == 1
        assert summary.total_requests == 1
        assert summary.requests_by_model["claude-sonnet-4"] == 1

    def test_log_files_found_equals_discovered(self) -> None:
        """log_files_found equals the number of paths from discover_vscode_logs."""
        from datetime import datetime

        paths = [Path(f"/fake/log_{i}.log") for i in range(3)]
        req = VSCodeRequest(
            timestamp=datetime(2026, 3, 13, 10, 0, 0),
            request_id="a1",
            model="gpt-4o",
            duration_ms=100,
            category="panel",
        )

        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=paths,
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                return_value=[req],
            ),
        ):
            summary = get_vscode_summary()

        assert summary.log_files_found == 3
        assert summary.log_files_parsed == 3

    def test_log_files_found_vs_parsed_on_oserror(self) -> None:
        """log_files_found counts all discovered; log_files_parsed only successes."""
        from datetime import datetime

        path1 = Path("/fake/log_1.log")
        path2 = Path("/fake/log_2.log")
        req = VSCodeRequest(
            timestamp=datetime(2026, 3, 14, 12, 0, 0),
            request_id="b1",
            model="claude-sonnet-4",
            duration_ms=300,
            category="inline",
        )

        def _fake_parse(path: Path) -> list[VSCodeRequest]:
            if path == path1:
                raise OSError("Permission denied")
            return [req]

        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=[path1, path2],
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                side_effect=_fake_parse,
            ),
        ):
            summary = get_vscode_summary()

        assert summary.log_files_found == 2
        assert summary.log_files_parsed == 1


# ---------------------------------------------------------------------------
# CLI: vscode subcommand
# ---------------------------------------------------------------------------


class TestVscodeCliCommand:
    def test_vscode_registered(self) -> None:
        assert "vscode" in [c.name for c in main.commands.values()]

    def test_no_logs_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])
        assert result.exit_code == 1
        assert "No VS Code Copilot Chat requests found" in result.output

    def test_vscode_single_file_oserror_logs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError on one file logs a warning; remaining files still parsed."""
        # Create two valid log files.
        for session in ("s1", "s2"):
            log_dir = tmp_path / session / "window1" / "exthost" / "GitHub.copilot-chat"
            log_dir.mkdir(parents=True)
            (log_dir / "GitHub Copilot Chat.log").write_text(
                _LOG_OPUS + "\n", encoding="utf-8"
            )

        # Make parse_vscode_log raise OSError only on the first call.
        call_count = 0
        _real_parse = parse_vscode_log

        def _failing_once(path: Path) -> list[VSCodeRequest]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "Permission denied"
                raise OSError(msg)
            return _real_parse(path)

        monkeypatch.setattr(
            "copilot_usage.vscode_parser.parse_vscode_log", _failing_once
        )

        warnings: list[str] = []

        def _sink(message: object) -> None:
            warnings.append(str(message))

        handler_id = logger.add(_sink, level="WARNING")
        try:
            summary = get_vscode_summary(tmp_path)
        finally:
            logger.remove(handler_id)

        assert summary.log_files_parsed == 1
        assert summary.total_requests == 1
        assert any("Could not read log file" in w for w in warnings)

    def test_vscode_all_files_oserror_shows_io_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When all discovered files fail, the CLI reports an I/O failure."""
        log_dir = tmp_path / "s1" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        (log_dir / "GitHub Copilot Chat.log").write_text(
            _LOG_OPUS + "\n", encoding="utf-8"
        )

        def _always_raise(*_a: object, **_kw: object) -> object:
            msg = "Permission denied"
            raise OSError(msg)

        monkeypatch.setattr(
            "copilot_usage.vscode_parser.parse_vscode_log", _always_raise
        )
        runner = CliRunner()
        result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])
        assert result.exit_code == 1
        assert "log files were found but could not be read" in result.output

    def test_vscode_logs_option_passed(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "20260313" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        (log_dir / "GitHub Copilot Chat.log").write_text(
            "\n".join([_LOG_OPUS, _LOG_GPT4O]),
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(main, ["vscode", "--vscode-logs", str(tmp_path)])
        assert result.exit_code == 0
        assert "VS Code Copilot Chat" in result.output

    def test_all_files_error_shows_correct_message(self) -> None:
        """Mock-only: log_files_found>0, log_files_parsed==0 → I/O error."""
        summary = VSCodeLogSummary(
            log_files_found=2, log_files_parsed=0, total_requests=0
        )
        with patch(
            "copilot_usage.vscode_parser.get_vscode_summary",
            return_value=summary,
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["vscode"])
        assert result.exit_code == 1
        assert "could not be read" in result.output

    def test_no_files_shows_no_requests_message(self) -> None:
        """Mock-only: log_files_found==0 → no-requests message."""
        summary = VSCodeLogSummary(
            log_files_found=0, log_files_parsed=0, total_requests=0
        )
        with patch(
            "copilot_usage.vscode_parser.get_vscode_summary",
            return_value=summary,
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["vscode"])
        assert result.exit_code == 1
        assert "No VS Code" in result.output


# ---------------------------------------------------------------------------
# Benchmark / correctness: large batch of requests
# ---------------------------------------------------------------------------

_NUM_BENCHMARK_REQUESTS = 10_000
_MODELS = ["claude-opus-4.6", "gpt-4o-mini", "claude-sonnet-4"]
_CATEGORIES = ["panel/editAgent", "inline", "copilotLanguageModelWrapper"]


def _make_bulk_requests(n: int = _NUM_BENCHMARK_REQUESTS) -> list[VSCodeRequest]:
    """Build *n* requests spread across a few models/categories on one date."""
    from datetime import datetime

    base = datetime(2026, 3, 13, 10, 0, 0)
    return [
        VSCodeRequest(
            timestamp=base,
            request_id=f"r{i}",
            model=_MODELS[i % len(_MODELS)],
            duration_ms=100 + (i % 7),
            category=_CATEGORIES[i % len(_CATEGORIES)],
        )
        for i in range(n)
    ]


class TestBuildVscodeSummaryBulk:
    """Correctness check using a large batch of requests."""

    def test_bulk_aggregation_correctness(self) -> None:
        requests = _make_bulk_requests()
        summary = build_vscode_summary(requests)

        assert summary.total_requests == _NUM_BENCHMARK_REQUESTS

        # Model distribution: requests are round-robined across 3 models
        for model in _MODELS:
            expected = sum(1 for r in requests if r.model == model)
            assert summary.requests_by_model[model] == expected

        # Duration by model
        for model in _MODELS:
            expected_dur = sum(r.duration_ms for r in requests if r.model == model)
            assert summary.duration_by_model[model] == expected_dur

        # Category distribution
        for cat in _CATEGORIES:
            expected_cat = sum(1 for r in requests if r.category == cat)
            assert summary.requests_by_category[cat] == expected_cat

        # All requests share the same date
        assert summary.requests_by_date == {"2026-03-13": _NUM_BENCHMARK_REQUESTS}

        # Total duration
        expected_total = sum(r.duration_ms for r in requests)
        assert summary.total_duration_ms == expected_total

    def test_bulk_multi_date(self) -> None:
        """Requests spanning multiple dates are aggregated correctly."""
        from datetime import datetime, timedelta

        base = datetime(2026, 3, 10, 8, 0, 0)
        requests = [
            VSCodeRequest(
                timestamp=base + timedelta(days=i // 100),
                request_id=f"m{i}",
                model="gpt-4o",
                duration_ms=50,
                category="panel",
            )
            for i in range(1000)
        ]
        summary = build_vscode_summary(requests)
        assert summary.total_requests == 1000
        assert sum(summary.requests_by_date.values()) == 1000
        # 10 distinct dates (0..999 // 100 → 0..9)
        assert len(summary.requests_by_date) == 10

    def test_finalized_summary_uses_mapping_proxy(self) -> None:
        """_finalize_summary wraps dicts in MappingProxyType for immutability."""
        import types

        summary = build_vscode_summary(_make_bulk_requests())
        assert type(summary.requests_by_model) is types.MappingProxyType
        assert type(summary.duration_by_model) is types.MappingProxyType
        assert type(summary.requests_by_category) is types.MappingProxyType
        assert type(summary.requests_by_date) is types.MappingProxyType


# ---------------------------------------------------------------------------
# Non-chronological request ordering (date accumulation only)
# ---------------------------------------------------------------------------


class TestNonChronologicalRequests:
    """Date accumulation handles out-of-order timestamps correctly."""

    def test_non_chronological_date_accumulation(self) -> None:
        """Requests arriving Mar 5 → Mar 3 → Mar 5 are aggregated by date value.

        Timestamp bounds (first/last) use a per-request min/max scan, so
        they reflect the global min/max regardless of input ordering.
        Date-bucketing is still correct regardless of ordering.
        """
        mar5_a = VSCodeRequest(
            timestamp=datetime(2026, 3, 5, 10, 0, 0),
            request_id="r1",
            model="gpt-4o",
            duration_ms=100,
            category="panel",
        )
        mar3 = VSCodeRequest(
            timestamp=datetime(2026, 3, 3, 14, 0, 0),
            request_id="r2",
            model="gpt-4o",
            duration_ms=200,
            category="panel",
        )
        mar5_b = VSCodeRequest(
            timestamp=datetime(2026, 3, 5, 18, 0, 0),
            request_id="r3",
            model="gpt-4o",
            duration_ms=150,
            category="panel",
        )

        summary = build_vscode_summary([mar5_a, mar3, mar5_b])

        assert summary.requests_by_date["2026-03-05"] == 2
        assert summary.requests_by_date["2026-03-03"] == 1
        # Timestamp bounds reflect the global min/max across all requests,
        # regardless of input order.
        assert summary.first_timestamp == mar3.timestamp
        assert summary.last_timestamp == mar5_b.timestamp
        assert summary.total_requests == 3


# ---------------------------------------------------------------------------
# Timestamp-bounds correctness
# ---------------------------------------------------------------------------


class TestTimestampBoundsCorrectness:
    """Timestamp bounds are derived from per-request min/max scan."""

    def test_single_batch_chronological(self) -> None:
        """first/last timestamps equal the head/tail of a chronological batch."""
        reqs = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 10, 8, 0, i),
                request_id=f"r{i}",
                model="gpt-4o",
                duration_ms=100 + i,
                category="panel",
            )
            for i in range(5)
        ]

        summary = build_vscode_summary(reqs)

        assert summary.first_timestamp == reqs[0].timestamp
        assert summary.last_timestamp == reqs[-1].timestamp

    def test_two_batches_advances_last_only(self) -> None:
        """Second (later) batch advances last_timestamp but not first_timestamp."""
        batch1 = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 10, 8, 0, i),
                request_id=f"b1r{i}",
                model="gpt-4o",
                duration_ms=100,
                category="panel",
            )
            for i in range(3)
        ]
        batch2 = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 11, 9, 0, i),
                request_id=f"b2r{i}",
                model="gpt-4o",
                duration_ms=200,
                category="panel",
            )
            for i in range(4)
        ]

        acc = _SummaryAccumulator()
        _update_vscode_summary(acc, batch1)

        first_after_batch1 = acc.first_timestamp
        last_after_batch1 = acc.last_timestamp

        _update_vscode_summary(acc, batch2)

        # first_timestamp unchanged; last_timestamp advances.
        assert acc.first_timestamp == first_after_batch1
        assert acc.first_timestamp == batch1[0].timestamp
        assert acc.last_timestamp == batch2[-1].timestamp
        assert acc.last_timestamp != last_after_batch1

    def test_first_timestamp_updated_when_second_batch_is_earlier(self) -> None:
        """Second (earlier) batch moves first_timestamp backward."""
        batch1 = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 11, 9, 0, i),
                request_id=f"b1r{i}",
                model="gpt-4o",
                duration_ms=100,
                category="panel",
            )
            for i in range(3)
        ]
        batch2 = [
            VSCodeRequest(
                timestamp=datetime(2026, 3, 10, 8, 0, i),
                request_id=f"b2r{i}",
                model="gpt-4o",
                duration_ms=200,
                category="panel",
            )
            for i in range(4)
        ]

        acc = _SummaryAccumulator()
        _update_vscode_summary(acc, batch1)

        first_after_batch1 = acc.first_timestamp
        last_after_batch1 = acc.last_timestamp

        _update_vscode_summary(acc, batch2)

        # first_timestamp moves backward to batch2; last_timestamp unchanged.
        assert acc.first_timestamp == batch2[0].timestamp
        assert acc.first_timestamp != first_after_batch1
        assert acc.last_timestamp == batch1[-1].timestamp
        assert acc.last_timestamp == last_after_batch1


# ---------------------------------------------------------------------------
# Benchmark: parse_vscode_log pre-filter on large synthetic log files
# ---------------------------------------------------------------------------

_NOISE_LINES: list[str] = [
    "2026-03-13 21:48:39.404 [info] [GitExtensionServiceImpl]"
    " Initializing Git extension service.",
    "2026-03-13 21:48:40.100 [debug] [ExtHost] resolving workspace folder...",
    "2026-03-13 21:48:41.555 [warning] Slow network detected for telemetry.",
    "2026-03-13 21:49:00.000 [info] [typescript-language-features] TSServer started.",
    "2026-03-13 21:49:02.123 [error] ENOENT: no such file or directory,"
    " open '/tmp/missing.ts'",
]


def _build_synthetic_log(
    tmp_path: Path, *, total_lines: int, matching_lines: int
) -> Path:
    """Create a synthetic VS Code log file with *matching_lines* ccreq lines
    scattered among *total_lines* of noise.
    """
    noise_count = total_lines - matching_lines
    lines: list[str] = []
    # Distribute matching lines evenly across the file
    interval = max(noise_count // max(matching_lines, 1), 1)
    match_idx = 0
    for i in range(total_lines):
        if match_idx < matching_lines and i > 0 and i % interval == 0:
            ms = 100 + match_idx
            lines.append(
                f"2026-03-13 22:10:{match_idx % 60:02d}.{match_idx:03d}"
                f" [info] ccreq:req{match_idx:05d}.copilotmd"
                f" | success | gpt-4o-mini | {ms}ms | [panel]"
            )
            match_idx += 1
        else:
            lines.append(_NOISE_LINES[i % len(_NOISE_LINES)])
    log_file = tmp_path / "synthetic.log"
    log_file.write_text("\n".join(lines), encoding="utf-8")
    return log_file


class TestParseVscodeLogPreFilter:
    """Correctness and performance of the ccreq: pre-filter."""

    def test_synthetic_log_correctness(self, tmp_path: Path) -> None:
        """parse_vscode_log returns exactly the expected matching requests
        from a large synthetic log file dominated by noise lines.
        """
        total = 50_000
        expected_matches = 50
        log_file = _build_synthetic_log(
            tmp_path, total_lines=total, matching_lines=expected_matches
        )
        requests = parse_vscode_log(log_file)
        assert len(requests) == expected_matches
        # Verify each returned request has the expected model
        for req in requests:
            assert req.model == "gpt-4o-mini"
            assert req.category == "panel"
            assert req.duration_ms >= 100

    def test_synthetic_log_prefilter_uses_regex_only_for_matching_lines(
        self, tmp_path: Path
    ) -> None:
        """parse_vscode_log only applies _CCREQ_RE to lines containing 'ccreq:'."""

        class _SpyRegex:
            """Spy wrapper around the real _CCREQ_RE to verify pre-filtering."""

            def __init__(self, real_re: re.Pattern[str]) -> None:
                self._real_re = real_re
                self.calls: list[str] = []

            def match(self, text: str) -> re.Match[str] | None:
                # Enforce that regex is only invoked for ccreq lines
                assert "ccreq:" in text
                self.calls.append(text)
                return self._real_re.match(text)

        total = 5_000
        expected_matches = 50
        log_file = _build_synthetic_log(
            tmp_path, total_lines=total, matching_lines=expected_matches
        )

        spy = _SpyRegex(_CCREQ_RE)
        with patch("copilot_usage.vscode_parser._CCREQ_RE", spy):
            requests = parse_vscode_log(log_file)

        # We still parse the expected number of requests.
        assert len(requests) == expected_matches
        # The regex should be invoked exactly once per matching ccreq line.
        assert len(spy.calls) == expected_matches
        # And every call should indeed be on a ccreq line.
        assert all("ccreq:" in line for line in spy.calls)

    def test_no_matching_lines(self, tmp_path: Path) -> None:
        """A log file with zero ccreq lines returns an empty list."""
        log_file = _build_synthetic_log(tmp_path, total_lines=10_000, matching_lines=0)
        assert parse_vscode_log(log_file) == []

    def test_all_lines_match(self, tmp_path: Path) -> None:
        """A log file where every line is a ccreq line parses all correctly."""
        n = 100
        lines = [
            f"2026-03-13 22:10:{i % 60:02d}.{i:03d}"
            f" [info] ccreq:req{i:05d}.copilotmd"
            f" | success | claude-opus-4.6 | {100 + i}ms | [inline]"
            for i in range(n)
        ]
        log_file = tmp_path / "all_match.log"
        log_file.write_text("\n".join(lines), encoding="utf-8")
        requests = parse_vscode_log(log_file)
        assert len(requests) == n
        for i, req in enumerate(requests):
            assert req.request_id == f"req{i:05d}"
            assert req.model == "claude-opus-4.6"
            assert req.duration_ms == 100 + i
            assert req.category == "inline"


class TestParseVscodeLogFromisoformat:
    """Verify that fromisoformat correctly parses all timestamps at scale."""

    def test_1000_matching_lines_parsed(self, tmp_path: Path) -> None:
        """A log file with 1 000 ccreq lines is fully parsed without ValueError."""
        n = 1_000
        lines = [
            f"2026-03-13 {(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.{i % 1_000_000:06d}"
            f" [info] ccreq:req{i:05d}.copilotmd"
            f" | success | gpt-4o-mini | {50 + i}ms | [panel/editAgent]"
            for i in range(n)
        ]
        log_file = tmp_path / "fromisoformat_1000.log"
        log_file.write_text("\n".join(lines), encoding="utf-8")
        requests = parse_vscode_log(log_file)
        assert len(requests) == n
        for i, req in enumerate(requests):
            assert req.request_id == f"req{i:05d}"
            assert req.duration_ms == 50 + i


# ---------------------------------------------------------------------------
# _VSCODE_LOG_CACHE / _get_cached_vscode_requests
# ---------------------------------------------------------------------------


def _make_log_line(*, req_idx: int = 0, model: str = "gpt-4o-mini") -> str:
    return (
        f"2026-03-13 22:10:{req_idx % 60:02d}.{req_idx:03d}"
        f" [info] ccreq:req{req_idx:05d}.copilotmd"
        f" | success | {model} | {100 + req_idx}ms | [inline]"
    )


class TestVscodeLogCache:
    """Tests for the module-level _VSCODE_LOG_CACHE and _get_cached_vscode_requests."""

    def test_first_call_populates_cache(self, tmp_path: Path) -> None:
        log_file = tmp_path / "chat.log"
        log_file.write_text(_make_log_line(req_idx=0))
        requests = _get_cached_vscode_requests(log_file)
        assert len(requests) == 1
        assert log_file in _VSCODE_LOG_CACHE

    def test_second_call_returns_cached_without_reparse(self, tmp_path: Path) -> None:
        """parse_vscode_log is only called once when file is unchanged."""
        log_file = tmp_path / "chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        with patch(
            "copilot_usage.vscode_parser.parse_vscode_log",
            wraps=parse_vscode_log,
        ) as spy:
            first = _get_cached_vscode_requests(log_file)
            second = _get_cached_vscode_requests(log_file)
            assert spy.call_count == 1
        assert first == second
        assert log_file in _VSCODE_LOG_CACHE
        assert _VSCODE_LOG_CACHE[log_file].requests == second

    def test_cache_invalidated_on_file_change(self, tmp_path: Path) -> None:
        """Changing the file causes a re-parse on the next call."""
        log_file = tmp_path / "chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        first = _get_cached_vscode_requests(log_file)
        assert len(first) == 1

        # Mutate the file by adding a second request line. The rewritten file
        # has different contents and size, so its identity changes and the
        # next cache lookup should re-parse it.
        log_file.write_text(
            _make_log_line(req_idx=0) + "\n" + _make_log_line(req_idx=1)
        )

        second = _get_cached_vscode_requests(log_file)
        assert len(second) == 2

    def test_lru_eviction(self, tmp_path: Path) -> None:
        """When the cache exceeds _MAX_CACHED_VSCODE_LOGS, the oldest entry is evicted."""
        paths: list[Path] = []
        for i in range(_MAX_CACHED_VSCODE_LOGS + 1):
            p = tmp_path / f"log_{i}.log"
            p.write_text(_make_log_line(req_idx=i))
            paths.append(p)

        for p in paths:
            _get_cached_vscode_requests(p)

        # The first entry should have been evicted.
        assert paths[0] not in _VSCODE_LOG_CACHE
        assert len(_VSCODE_LOG_CACHE) == _MAX_CACHED_VSCODE_LOGS

    def test_lru_promotion_on_access(self, tmp_path: Path) -> None:
        """Accessing a cached entry moves it to the back (most-recently used)."""
        p1 = tmp_path / "a.log"
        p2 = tmp_path / "b.log"
        p1.write_text(_make_log_line(req_idx=0))
        p2.write_text(_make_log_line(req_idx=1))

        _get_cached_vscode_requests(p1)
        _get_cached_vscode_requests(p2)
        # p1 is currently the LRU entry; access it to promote.
        _get_cached_vscode_requests(p1)

        keys = list(_VSCODE_LOG_CACHE.keys())
        assert keys[-1] == p1  # p1 is now at the back (most recently used)

    def test_get_vscode_summary_uses_cache(self, tmp_path: Path) -> None:
        """get_vscode_summary leverages the cache on repeated calls."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        with patch(
            "copilot_usage.vscode_parser.parse_vscode_log",
            wraps=parse_vscode_log,
        ) as spy:
            s1 = get_vscode_summary(tmp_path)
            s2 = get_vscode_summary(tmp_path)
            assert spy.call_count == 1
        assert s1.total_requests == 1
        assert s2.total_requests == 1

    def test_explicit_file_id_skips_safe_file_identity(self, tmp_path: Path) -> None:
        """Passing an explicit file_id bypasses safe_file_identity entirely."""
        log_file = tmp_path / "chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        # Warm the cache with the default sentinel (safe_file_identity is used).
        first = _get_cached_vscode_requests(log_file)
        assert len(first) == 1

        # Retrieve the file_id that was cached so we can pass it explicitly.
        cached_id = _VSCODE_LOG_CACHE[log_file].file_id

        # Patch safe_file_identity to raise — it must never be called.
        with patch(
            "copilot_usage.vscode_parser.safe_file_identity",
            side_effect=AssertionError("safe_file_identity should not be called"),
        ):
            second = _get_cached_vscode_requests(log_file, file_id=cached_id)

        assert second == first

    def test_explicit_file_id_none_cache_hit(self, tmp_path: Path) -> None:
        """Two calls with file_id=None parse only once (None == None cache hit)."""
        log_file = tmp_path / "chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        with patch(
            "copilot_usage.vscode_parser.parse_vscode_log",
            wraps=parse_vscode_log,
        ) as spy:
            first = _get_cached_vscode_requests(log_file, file_id=None)
            second = _get_cached_vscode_requests(log_file, file_id=None)
            assert spy.call_count == 1

        assert first == second
        assert len(first) == 1

    def test_explicit_file_id_mismatch_triggers_reparse(self, tmp_path: Path) -> None:
        """A different explicit file_id invalidates the cache and re-parses."""
        log_file = tmp_path / "chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        with patch(
            "copilot_usage.vscode_parser.parse_vscode_log",
            wraps=parse_vscode_log,
        ) as spy:
            _get_cached_vscode_requests(log_file, file_id=(0, 0))
            assert spy.call_count == 1

            _get_cached_vscode_requests(log_file, file_id=(1, 1))
            assert spy.call_count == 2


# ---------------------------------------------------------------------------
# _vscode_summary_cache – summary-level caching
# ---------------------------------------------------------------------------


class TestVscodeSummaryCacheSkipsReaggregation:
    """Verify that get_vscode_summary() skips _update_vscode_summary on a warm cache.

    Uses monkeypatching to count calls to _update_vscode_summary, matching
    the project's deterministic perf-test convention (no wall-clock timing).
    """

    def test_second_call_skips_update(self, tmp_path: Path) -> None:
        """_update_vscode_summary is not called on the second invocation."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"

        # Write 500 synthetic request lines.
        lines = [_make_log_line(req_idx=i) for i in range(500)]
        log_file.write_text("\n".join(lines))

        with patch(
            "copilot_usage.vscode_parser._update_vscode_summary",
            wraps=_update_vscode_summary,
        ) as spy:
            s1 = get_vscode_summary(tmp_path)
            assert spy.call_count == 1  # called once during first aggregation

            s2 = get_vscode_summary(tmp_path)
            assert spy.call_count == 1  # NOT called again — cached summary returned

        assert s1.total_requests == 500
        assert s2.total_requests == 500

    def test_cache_invalidated_on_file_change(self, tmp_path: Path) -> None:
        """Changing a log file invalidates the summary cache."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        s1 = get_vscode_summary(tmp_path)
        assert s1.total_requests == 1

        # Mutate the file — summary cache should be invalidated.
        log_file.write_text(
            _make_log_line(req_idx=0) + "\n" + _make_log_line(req_idx=1)
        )

        with patch(
            "copilot_usage.vscode_parser._update_vscode_summary",
            wraps=_update_vscode_summary,
        ) as spy:
            s2 = get_vscode_summary(tmp_path)
            assert spy.call_count == 1  # re-aggregated because file changed

        assert s2.total_requests == 2

    def test_cache_invalidated_on_new_file(self, tmp_path: Path) -> None:
        """Adding a new log file invalidates the summary cache."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        s1 = get_vscode_summary(tmp_path)
        assert s1.total_requests == 1
        assert s1.log_files_found == 1

        # Add a second log directory with a new log file.
        log_dir2 = (
            tmp_path / "20260314T100000" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir2.mkdir(parents=True)
        log_file2 = log_dir2 / "GitHub Copilot Chat.log"
        log_file2.write_text(_make_log_line(req_idx=10))

        with patch(
            "copilot_usage.vscode_parser._update_vscode_summary",
            wraps=_update_vscode_summary,
        ) as spy:
            s2 = get_vscode_summary(tmp_path)
            assert spy.call_count == 1  # only the new file re-aggregated

        assert s2.total_requests == 2
        assert s2.log_files_found == 2

    def test_cache_not_populated_on_partial_failure(self, tmp_path: Path) -> None:
        """Summary is NOT cached when a log file fails to read."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        # Add a second log file that will fail to read.
        log_dir2 = (
            tmp_path / "20260314T100000" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir2.mkdir(parents=True)
        log_file2 = log_dir2 / "GitHub Copilot Chat.log"
        log_file2.write_text(_make_log_line(req_idx=1))

        # Make the second file's parse raise OSError after stat succeeds.
        # Capture the real function before patching to avoid recursion.
        real_fn = _get_cached_vscode_requests

        def _fail_second(
            log_path: Path,
            file_id: tuple[int, int] | None = None,
        ) -> tuple[VSCodeRequest, ...]:
            if log_path == log_file2:
                raise OSError("transient read failure")
            return real_fn(log_path, file_id)

        with patch(
            "copilot_usage.vscode_parser._get_cached_vscode_requests",
            side_effect=_fail_second,
        ):
            s1 = get_vscode_summary(tmp_path)

        assert s1.total_requests == 1
        assert s1.log_files_found == 2
        assert s1.log_files_parsed == 1

        # Second call should re-aggregate because the cache was not populated.
        with patch(
            "copilot_usage.vscode_parser._update_vscode_summary",
            wraps=_update_vscode_summary,
        ) as spy:
            get_vscode_summary(tmp_path)
            # Only file2 is re-aggregated; file1's per-file summary is cached.
            assert spy.call_count == 1

    def test_cached_return_is_mutation_safe(self, tmp_path: Path) -> None:
        """Dict fields on a cached return are immutable (MappingProxyType)."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0, model="gpt-4o-mini"))

        s1 = get_vscode_summary(tmp_path)
        assert s1.requests_by_model == {"gpt-4o-mini": 1}

        # Attempting to mutate the immutable dict field raises TypeError.
        with pytest.raises(TypeError):
            s1.requests_by_model["gpt-4o-mini"] = 9999  # type: ignore[index]

        # Second call returns the exact same cached object.
        s2 = get_vscode_summary(tmp_path)
        assert s1 is s2


# ---------------------------------------------------------------------------
# Per-file summary cache — only changed files trigger _update_vscode_summary
# ---------------------------------------------------------------------------


class TestPerFileSummaryCacheSkipsUnchangedFiles:
    """Verify that get_vscode_summary re-aggregates only changed files.

    When one of several log files changes, the per-file summary cache
    provides the unchanged files' contribution via _merge_partial (which
    is O(num_models + num_categories + num_dates)), so
    _update_vscode_summary is only called for the file(s) that actually
    changed.

    Uses monkeypatching to count calls to _update_vscode_summary,
    following the project's deterministic perf-test convention (no
    wall-clock timing).
    """

    def test_only_changed_file_reaggregated(self, tmp_path: Path) -> None:
        """_update_vscode_summary is called only for the changed file."""
        n = 50  # requests per file
        m = 10  # new lines appended to one file

        log_dir1 = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir1.mkdir(parents=True)
        log_file1 = log_dir1 / "GitHub Copilot Chat.log"

        log_dir2 = (
            tmp_path / "20260314T100000" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir2.mkdir(parents=True)
        log_file2 = log_dir2 / "GitHub Copilot Chat.log"

        # Write N lines to each file.
        log_file1.write_text("\n".join(_make_log_line(req_idx=i) for i in range(n)))
        log_file2.write_text("\n".join(_make_log_line(req_idx=i + n) for i in range(n)))

        # Warm both caches (summary + per-file).
        s1 = get_vscode_summary(tmp_path)
        assert s1.total_requests == n * 2

        # Append M new lines to file2 only; file1 is unchanged.
        existing = log_file2.read_text()
        extra = "\n".join(_make_log_line(req_idx=i + n * 2) for i in range(m))
        log_file2.write_text(existing + "\n" + extra)

        # Spy on _update_vscode_summary and call again.
        with patch(
            "copilot_usage.vscode_parser._update_vscode_summary",
            wraps=_update_vscode_summary,
        ) as spy:
            s2 = get_vscode_summary(tmp_path)
            # Only the changed file's requests are aggregated; the
            # unchanged file's contribution comes from the per-file
            # summary cache without iterating its requests.
            assert spy.call_count == 1

        assert s2.total_requests == n * 2 + m


# ---------------------------------------------------------------------------
# stat() syscall budget — safe_file_identity called at most once per file
# ---------------------------------------------------------------------------


class TestSafeFileIdentityCalledOncePerFile:
    """Assert safe_file_identity is called at most once per log file per call.

    Before the optimisation, ``get_vscode_summary`` stat'd every file
    twice on a cold summary cache: once to build ``current_ids`` and
    again inside ``_get_cached_vscode_requests``.  After the fix the
    pre-computed identity is threaded through, so only one stat per file
    should occur.
    """

    @staticmethod
    def _make_log_tree(tmp_path: Path, n_files: int) -> list[Path]:
        """Create *n_files* log files under a VS Code log directory layout."""
        paths: list[Path] = []
        for i in range(n_files):
            log_dir = (
                tmp_path
                / f"2026031{i}T211400"
                / "window1"
                / "exthost"
                / "GitHub.copilot-chat"
            )
            log_dir.mkdir(parents=True)
            log_file = log_dir / "GitHub Copilot Chat.log"
            log_file.write_text(_make_log_line(req_idx=i))
            paths.append(log_file)
        return paths

    def test_cold_cache_stats_each_file_once(self, tmp_path: Path) -> None:
        """On a cold summary + per-file cache, each file is stat'd once."""
        log_files = self._make_log_tree(tmp_path, n_files=5)
        call_counts: dict[Path, int] = {}
        real_fn = safe_file_identity

        def _counting_spy(path: Path) -> tuple[int, int] | None:
            call_counts[path] = call_counts.get(path, 0) + 1
            return real_fn(path)

        with patch(
            "copilot_usage.vscode_parser.safe_file_identity",
            side_effect=_counting_spy,
        ):
            summary = get_vscode_summary(tmp_path)

        assert summary.total_requests == len(log_files)
        for log_file in log_files:
            assert call_counts.get(log_file, 0) <= 1, (
                f"safe_file_identity called {call_counts[log_file]} times "
                f"for {log_file.name}, expected at most 1"
            )

    def test_warm_per_file_cache_stats_each_file_once(self, tmp_path: Path) -> None:
        """With a warm per-file cache but cold summary cache, still one stat."""
        log_files = self._make_log_tree(tmp_path, n_files=3)
        # Warm the per-file cache.
        for lf in log_files:
            _get_cached_vscode_requests(lf)

        # Clear summary cache only (per-file cache stays warm).
        import copilot_usage.vscode_parser as _mod

        _mod._vscode_summary_cache = None  # pyright: ignore[reportPrivateUsage]

        call_counts: dict[Path, int] = {}
        real_fn = safe_file_identity

        def _counting_spy(path: Path) -> tuple[int, int] | None:
            call_counts[path] = call_counts.get(path, 0) + 1
            return real_fn(path)

        with patch(
            "copilot_usage.vscode_parser.safe_file_identity",
            side_effect=_counting_spy,
        ):
            summary = get_vscode_summary(tmp_path)

        assert summary.total_requests == len(log_files)
        for log_file in log_files:
            assert call_counts.get(log_file, 0) <= 1, (
                f"safe_file_identity called {call_counts[log_file]} times "
                f"for {log_file.name}, expected at most 1"
            )


# ---------------------------------------------------------------------------
# _PER_FILE_SUMMARY_CACHE LRU eviction
# ---------------------------------------------------------------------------


class TestPerFileSummaryCacheLRUEviction:
    """Verify that _PER_FILE_SUMMARY_CACHE evicts the oldest entry when full.

    After removing _BoundedFileSummaryCache, eviction is handled by the
    shared ``lru_insert()`` helper.  This test exercises the integration:
    writing more than ``_MAX_CACHED_VSCODE_LOGS`` distinct files must
    evict the oldest entry while the newest survives.
    """

    def test_oldest_evicted_newest_survives(self, tmp_path: Path) -> None:
        """Inserting _MAX_CACHED_VSCODE_LOGS + 1 files evicts the first."""
        limit = _MAX_CACHED_VSCODE_LOGS

        # Create limit + 1 log files inside a VS Code log directory layout.
        log_files: list[Path] = []
        for i in range(limit + 1):
            log_dir = (
                tmp_path
                / f"session{i:04d}"
                / "window1"
                / "exthost"
                / "GitHub.copilot-chat"
            )
            log_dir.mkdir(parents=True)
            log_file = log_dir / "GitHub Copilot Chat.log"
            log_file.write_text(_make_log_line(req_idx=i))
            log_files.append(log_file)

        # Process all files through get_vscode_summary so the per-file
        # summary cache is populated via lru_insert.
        summary = get_vscode_summary(tmp_path)
        assert summary.total_requests == limit + 1

        # The cache is bounded: the oldest (first) file should be evicted.
        assert log_files[0] not in _PER_FILE_SUMMARY_CACHE, (
            "oldest entry should have been evicted from _PER_FILE_SUMMARY_CACHE"
        )
        # The newest (last) file should survive.
        assert log_files[-1] in _PER_FILE_SUMMARY_CACHE, (
            "newest entry should be present in _PER_FILE_SUMMARY_CACHE"
        )
        # Cache size must match the configured limit after one eviction.
        assert len(_PER_FILE_SUMMARY_CACHE) == limit


# ---------------------------------------------------------------------------
# Immutable dict fields — MappingProxyType protection
# ---------------------------------------------------------------------------


class TestImmutableSummaryFields:
    """Verify that VSCodeLogSummary dict fields are always immutable
    MappingProxyType instances — whether produced by _finalize_summary,
    build_vscode_summary, or direct construction with plain dicts.
    """

    def test_cache_hit_returns_same_object(self, tmp_path: Path) -> None:
        """Two get_vscode_summary() calls with no changes return the same object."""
        log_dir = tmp_path / "session" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS + "\n")

        first = get_vscode_summary(tmp_path)
        second = get_vscode_summary(tmp_path)
        assert first is second

    def test_mutation_raises_requests_by_model(self, tmp_path: Path) -> None:
        """Attempting to mutate requests_by_model raises TypeError."""
        log_dir = tmp_path / "session" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS + "\n")

        summary = get_vscode_summary(tmp_path)
        with pytest.raises(TypeError):
            summary.requests_by_model["x"] = 1  # type: ignore[index]

    def test_mutation_raises_duration_by_model(self, tmp_path: Path) -> None:
        """Attempting to mutate duration_by_model raises TypeError."""
        log_dir = tmp_path / "session" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS + "\n")

        summary = get_vscode_summary(tmp_path)
        with pytest.raises(TypeError):
            summary.duration_by_model["x"] = 1  # type: ignore[index]

    def test_mutation_raises_requests_by_category(self, tmp_path: Path) -> None:
        """Attempting to mutate requests_by_category raises TypeError."""
        log_dir = tmp_path / "session" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS + "\n")

        summary = get_vscode_summary(tmp_path)
        with pytest.raises(TypeError):
            summary.requests_by_category["x"] = 1  # type: ignore[index]

    def test_mutation_raises_requests_by_date(self, tmp_path: Path) -> None:
        """Attempting to mutate requests_by_date raises TypeError."""
        log_dir = tmp_path / "session" / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_LOG_OPUS + "\n")

        summary = get_vscode_summary(tmp_path)
        with pytest.raises(TypeError):
            summary.requests_by_date["x"] = 1  # type: ignore[index]

    def test_build_vscode_summary_returns_immutable_fields(self) -> None:
        """build_vscode_summary also wraps dict fields in MappingProxyType."""
        summary = build_vscode_summary(
            [
                VSCodeRequest(
                    timestamp=datetime(2026, 3, 13, 22, 10, 24),
                    request_id="abc",
                    model="gpt-4o",
                    duration_ms=500,
                    category="panel",
                ),
            ],
            log_files_parsed=1,
            log_files_found=1,
        )
        with pytest.raises(TypeError):
            summary.requests_by_model["x"] = 1  # type: ignore[index]
        with pytest.raises(TypeError):
            summary.duration_by_model["x"] = 1  # type: ignore[index]
        with pytest.raises(TypeError):
            summary.requests_by_category["x"] = 1  # type: ignore[index]
        with pytest.raises(TypeError):
            summary.requests_by_date["x"] = 1  # type: ignore[index]

    def test_constructor_wraps_plain_dicts(self) -> None:
        """Passing plain dicts to the constructor produces MappingProxyType."""
        import types

        mutable = {"gpt-4o": 3}
        summary = VSCodeLogSummary(
            requests_by_model=mutable,
            duration_by_model={"gpt-4o": 7000},
            requests_by_category={"panel": 4},
            requests_by_date={"2026-03-13": 3},
        )
        # Caller's mutable dict must not alias the frozen field.
        mutable["injected"] = 1
        assert "injected" not in summary.requests_by_model
        assert type(summary.requests_by_model) is types.MappingProxyType
        assert type(summary.duration_by_model) is types.MappingProxyType
        assert type(summary.requests_by_category) is types.MappingProxyType
        assert type(summary.requests_by_date) is types.MappingProxyType

    def test_default_constructor_produces_immutable_fields(self) -> None:
        """VSCodeLogSummary() with no args has immutable empty mappings."""
        import types

        summary = VSCodeLogSummary()
        assert type(summary.requests_by_model) is types.MappingProxyType
        assert type(summary.duration_by_model) is types.MappingProxyType
        assert type(summary.requests_by_category) is types.MappingProxyType
        assert type(summary.requests_by_date) is types.MappingProxyType
        with pytest.raises(TypeError):
            summary.requests_by_model["x"] = 1  # type: ignore[index]


# ---------------------------------------------------------------------------
# _merge_partial — timestamp edge cases and additive counters
# ---------------------------------------------------------------------------


class TestMergePartialTimestamps:
    """Verify _merge_partial timestamp logic and counter merging."""

    def test_empty_partial_leaves_accumulator_timestamps_unchanged(self) -> None:
        """Merging a partial with None timestamps must not reset the accumulator."""
        acc = _SummaryAccumulator(  # pyright: ignore[reportPrivateUsage]
            first_timestamp=datetime(2026, 3, 13, 10, 0, 0),
            last_timestamp=datetime(2026, 3, 14, 18, 0, 0),
        )
        empty_partial = VSCodeLogSummary()  # timestamps default to None
        _merge_partial(acc, empty_partial)  # pyright: ignore[reportPrivateUsage]
        assert acc.first_timestamp == datetime(2026, 3, 13, 10, 0, 0)
        assert acc.last_timestamp == datetime(2026, 3, 14, 18, 0, 0)

    def test_first_partial_into_fresh_accumulator_adopts_timestamps(self) -> None:
        """Merging the first partial into a fresh acc adopts its timestamps."""
        acc = _SummaryAccumulator()  # pyright: ignore[reportPrivateUsage]
        partial = VSCodeLogSummary(
            first_timestamp=datetime(2026, 3, 13, 9, 0, 0),
            last_timestamp=datetime(2026, 3, 13, 17, 0, 0),
        )
        _merge_partial(acc, partial)  # pyright: ignore[reportPrivateUsage]
        assert acc.first_timestamp == datetime(2026, 3, 13, 9, 0, 0)
        assert acc.last_timestamp == datetime(2026, 3, 13, 17, 0, 0)

    def test_older_partial_updates_first_timestamp(self) -> None:
        """A partial with an earlier first_timestamp should update the accumulator."""
        acc = _SummaryAccumulator(  # pyright: ignore[reportPrivateUsage]
            first_timestamp=datetime(2026, 3, 14, 10, 0, 0),
            last_timestamp=datetime(2026, 3, 14, 18, 0, 0),
        )
        older = VSCodeLogSummary(
            first_timestamp=datetime(2026, 3, 13, 8, 0, 0),
            last_timestamp=datetime(2026, 3, 13, 12, 0, 0),
        )
        _merge_partial(acc, older)  # pyright: ignore[reportPrivateUsage]
        assert acc.first_timestamp == datetime(2026, 3, 13, 8, 0, 0)
        # last_timestamp unchanged — the older partial's last is earlier.
        assert acc.last_timestamp == datetime(2026, 3, 14, 18, 0, 0)

    def test_newer_partial_does_not_update_first_timestamp(self) -> None:
        """A partial newer than the accumulator must not alter first_timestamp."""
        acc = _SummaryAccumulator(  # pyright: ignore[reportPrivateUsage]
            first_timestamp=datetime(2026, 3, 13, 8, 0, 0),
            last_timestamp=datetime(2026, 3, 14, 18, 0, 0),
        )
        newer = VSCodeLogSummary(
            first_timestamp=datetime(2026, 3, 15, 10, 0, 0),
            last_timestamp=datetime(2026, 3, 15, 20, 0, 0),
        )
        _merge_partial(acc, newer)  # pyright: ignore[reportPrivateUsage]
        assert acc.first_timestamp == datetime(2026, 3, 13, 8, 0, 0)
        # last_timestamp updated because the newer partial is later.
        assert acc.last_timestamp == datetime(2026, 3, 15, 20, 0, 0)

    def test_counters_are_additive_not_replaced(self) -> None:
        """Counter dicts must be summed, not overwritten, by _merge_partial."""
        acc = _SummaryAccumulator(  # pyright: ignore[reportPrivateUsage]
            total_requests=10,
            total_duration_ms=5000,
        )
        acc.requests_by_model["gpt-4o"] += 5
        acc.duration_by_model["gpt-4o"] += 3000
        acc.requests_by_category["panel"] += 4
        acc.requests_by_date["2026-03-13"] += 6

        partial = VSCodeLogSummary(
            total_requests=3,
            total_duration_ms=2000,
            requests_by_model={"gpt-4o": 2, "claude-sonnet-4": 1},
            duration_by_model={"gpt-4o": 1200, "claude-sonnet-4": 800},
            requests_by_category={"panel": 1, "inline": 2},
            requests_by_date={"2026-03-13": 1, "2026-03-14": 2},
        )
        _merge_partial(acc, partial)  # pyright: ignore[reportPrivateUsage]

        assert acc.total_requests == 13
        assert acc.total_duration_ms == 7000
        assert acc.requests_by_model["gpt-4o"] == 7
        assert acc.requests_by_model["claude-sonnet-4"] == 1
        assert acc.duration_by_model["gpt-4o"] == 4200
        assert acc.duration_by_model["claude-sonnet-4"] == 800
        assert acc.requests_by_category["panel"] == 5
        assert acc.requests_by_category["inline"] == 2
        assert acc.requests_by_date["2026-03-13"] == 7
        assert acc.requests_by_date["2026-03-14"] == 2


# ---------------------------------------------------------------------------
# _vscode_summary_cache — not populated on partial failure
# ---------------------------------------------------------------------------


class TestVscodeSummaryCacheNotPopulatedOnPartialFailure:
    """Verify the summary cache stays None after a partial parse failure."""

    def test_cache_stays_none_on_oserror(self) -> None:
        """When one file raises OSError, _vscode_summary_cache must remain None."""
        import copilot_usage.vscode_parser as _mod

        file_a = Path("/fake/log_a.log")
        file_b = Path("/fake/log_b.log")
        ok_req = VSCodeRequest(
            timestamp=datetime(2026, 3, 14, 12, 0, 0),
            request_id="b1",
            model="claude-sonnet-4",
            duration_ms=300,
            category="inline",
        )

        def _fake_parse(path: Path) -> list[VSCodeRequest]:
            if path == file_a:
                raise OSError("Permission denied")
            return [ok_req]

        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=[file_a, file_b],
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                side_effect=_fake_parse,
            ),
        ):
            summary = get_vscode_summary()

        assert summary.total_requests == 1
        assert summary.log_files_found == 2
        assert summary.log_files_parsed == 1
        assert _mod._vscode_summary_cache is None  # pyright: ignore[reportPrivateUsage]

    def test_cache_populated_on_full_success_after_failure(self) -> None:
        """After a failed call, a fully successful call should populate the cache."""
        import copilot_usage.vscode_parser as _mod

        file_a = Path("/fake/log_a.log")
        file_b = Path("/fake/log_b.log")
        ok_req = VSCodeRequest(
            timestamp=datetime(2026, 3, 14, 12, 0, 0),
            request_id="b1",
            model="claude-sonnet-4",
            duration_ms=300,
            category="inline",
        )

        def _fail_a(path: Path) -> list[VSCodeRequest]:
            if path == file_a:
                raise OSError("Permission denied")
            return [ok_req]

        # First call: partial failure — cache stays None.
        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=[file_a, file_b],
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                side_effect=_fail_a,
            ),
        ):
            get_vscode_summary()

        assert _mod._vscode_summary_cache is None  # pyright: ignore[reportPrivateUsage]

        # Second call: all files succeed — cache should be populated.
        with (
            patch(
                "copilot_usage.vscode_parser._cached_discover_vscode_logs",
                return_value=[file_a, file_b],
            ),
            patch(
                "copilot_usage.vscode_parser.parse_vscode_log",
                return_value=[ok_req],
            ),
        ):
            summary2 = get_vscode_summary()

        assert summary2.total_requests == 2
        assert _mod._vscode_summary_cache is not None  # pyright: ignore[reportPrivateUsage]


class TestVscodeDiscoveryCacheSkipsGlob:
    """Verify that _cached_discover_vscode_logs skips glob on repeated calls.

    Uses monkeypatching to spy on Path.glob and assert it is called
    exactly once when the root directory has not changed — matching the
    project's deterministic perf-test convention (no wall-clock timing).
    """

    def test_second_summary_call_skips_glob(self, tmp_path: Path) -> None:
        """Path.glob is not called on the second get_vscode_summary invocation."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        original_glob = Path.glob

        glob_call_count = 0

        def _counting_glob(
            self: Path,
            pattern: str,
        ) -> list[Path]:
            nonlocal glob_call_count
            glob_call_count += 1
            return list(original_glob(self, pattern))

        with patch.object(Path, "glob", _counting_glob):
            s1 = get_vscode_summary(tmp_path)
            assert glob_call_count == 1  # glob called on first invocation
            assert s1.total_requests == 1

            s2 = get_vscode_summary(tmp_path)
            assert glob_call_count == 1  # NOT called again — cached discovery
            assert s2.total_requests == 1

    def test_cache_invalidated_on_root_mtime_change(self, tmp_path: Path) -> None:
        """Changing the root directory's identity forces a re-glob."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        s1 = get_vscode_summary(tmp_path)
        assert s1.total_requests == 1

        # Tamper with the cached root_id to simulate a directory change.
        cached = _VSCODE_DISCOVERY_CACHE[tmp_path]
        _VSCODE_DISCOVERY_CACHE[tmp_path] = _VSCodeDiscoveryCache(
            root_id=(cached.root_id[0] + 1_000_000_000, cached.root_id[1]),
            child_ids=cached.child_ids,
            newest_child_path=cached.newest_child_path,
            newest_child_id=cached.newest_child_id,
            log_paths=cached.log_paths,
        )

        original_glob = Path.glob
        glob_call_count = 0

        def _counting_glob(
            self: Path,
            pattern: str,
        ) -> list[Path]:
            nonlocal glob_call_count
            glob_call_count += 1
            return list(original_glob(self, pattern))

        with patch.object(Path, "glob", _counting_glob):
            s2 = get_vscode_summary(tmp_path)
            assert glob_call_count == 1  # glob called again due to identity change

        assert s2.total_requests == 1

    def test_discovery_cache_populated(self, tmp_path: Path) -> None:
        """_VSCODE_DISCOVERY_CACHE is populated after first get_vscode_summary call."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        (log_dir / "GitHub Copilot Chat.log").write_text(_make_log_line(req_idx=0))

        assert tmp_path not in _VSCODE_DISCOVERY_CACHE
        get_vscode_summary(tmp_path)
        assert tmp_path in _VSCODE_DISCOVERY_CACHE
        cached = _VSCODE_DISCOVERY_CACHE[tmp_path]
        assert len(cached.log_paths) == 1
        assert cached.root_id == safe_file_identity(tmp_path)
        assert cached.child_ids == _scan_child_ids(tmp_path)

    def test_new_window_under_existing_session_triggers_rediscovery(
        self, tmp_path: Path
    ) -> None:
        """Adding a window dir under an existing session invalidates the cache.

        The discovery cache stores a sentinel (the most recently modified
        session directory).  When a new ``window*`` directory is created
        under that session, the session directory's mtime changes, causing
        the sentinel check to miss and trigger a full re-glob.
        """
        session_dir = tmp_path / "20260313T211400"
        log_dir = session_dir / "window1" / "exthost" / "GitHub.copilot-chat"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "GitHub Copilot Chat.log"
        log_file.write_text(_make_log_line(req_idx=0))

        original_glob = Path.glob
        glob_call_count = 0

        def _counting_glob(
            self: Path,
            pattern: str,
        ) -> list[Path]:
            nonlocal glob_call_count
            glob_call_count += 1
            return list(original_glob(self, pattern))

        with patch.object(Path, "glob", _counting_glob):
            s1 = get_vscode_summary(tmp_path)
            assert glob_call_count == 1
            assert s1.total_requests == 1

            # Create a new window directory under the same session.
            new_log_dir = session_dir / "window2" / "exthost" / "GitHub.copilot-chat"
            new_log_dir.mkdir(parents=True)
            (new_log_dir / "GitHub Copilot Chat.log").write_text(
                _make_log_line(req_idx=1)
            )

            s2 = get_vscode_summary(tmp_path)
            # Session dir mtime changed → sentinel miss → re-globbed
            assert glob_call_count == 2
            assert s2.total_requests == 2

    def test_non_directory_candidate_skipped(self, tmp_path: Path) -> None:
        """A file (not a directory) passed as base_path produces an empty summary."""
        file_path = tmp_path / "not-a-dir.txt"
        file_path.write_text("hello")

        summary = get_vscode_summary(file_path)
        assert summary.total_requests == 0
        assert file_path not in _VSCODE_DISCOVERY_CACHE


class TestScanChildIdsEdgeCases:
    """Cover error-handling paths in _scan_child_ids."""

    def test_non_directory_entries_skipped(self, tmp_path: Path) -> None:
        """Regular files under root are excluded from child_ids."""
        (tmp_path / "session_dir").mkdir()
        (tmp_path / "regular_file.txt").write_text("data")

        ids = _scan_child_ids(tmp_path)
        names = {name for name, _ in ids}
        assert "session_dir" in names
        assert "regular_file.txt" not in names

    def test_stat_failure_on_entry_skipped(self, tmp_path: Path) -> None:
        """An entry whose stat raises OSError is silently skipped."""
        from unittest.mock import MagicMock

        good_stat = os.stat(tmp_path)
        good_entry = MagicMock(spec=os.DirEntry)
        good_entry.name = "good_dir"
        good_entry.stat.return_value = good_stat

        bad_entry = MagicMock(spec=os.DirEntry)
        bad_entry.name = "bad_dir"
        bad_entry.stat.side_effect = OSError("simulated stat failure")

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=iter([good_entry, bad_entry]))
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("os.scandir", return_value=ctx):
            ids = _scan_child_ids(tmp_path)

        names = {name for name, _ in ids}
        assert "good_dir" in names
        assert "bad_dir" not in names

    def test_scandir_oserror_returns_empty(self, tmp_path: Path) -> None:
        """When os.scandir itself raises OSError, return empty frozenset."""
        missing = tmp_path / "nonexistent_path"
        ids = _scan_child_ids(missing)
        assert ids == frozenset()


class TestCachedDiscoverOsErrors:
    """Cover OSError paths in _cached_discover_vscode_logs."""

    def test_missing_candidate_skipped(self, tmp_path: Path) -> None:
        """A candidate whose stat raises OSError produces no results."""
        missing = tmp_path / "nonexistent_vscode_logs"
        result = _cached_discover_vscode_logs(missing)
        assert result == []
        assert missing not in _VSCODE_DISCOVERY_CACHE


class TestCachedDiscoverSkipsChildScanOnHit:
    """Verify _scan_child_ids is not called on root_id + sentinel cache hits.

    After a warm call populates _VSCODE_DISCOVERY_CACHE, a subsequent
    call with an unchanged root *and* unchanged sentinel child must
    short-circuit without invoking _scan_child_ids — avoiding
    O(n_children) stat syscalls on every steady-state invocation.
    """

    def test_cached_discover_skips_child_scan_on_root_id_hit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_scan_child_ids must not be called when root_id matches the cache."""
        log_dir = (
            tmp_path / "20260313T211400" / "window1" / "exthost" / "GitHub.copilot-chat"
        )
        log_dir.mkdir(parents=True)
        (log_dir / "GitHub Copilot Chat.log").write_text(_make_log_line(req_idx=0))

        # Warm the cache with the real implementation.
        _cached_discover_vscode_logs(tmp_path)
        assert tmp_path in _VSCODE_DISCOVERY_CACHE

        # Spy on _scan_child_ids for subsequent calls.
        import copilot_usage.vscode_parser as _mod

        scan_calls: list[Path] = []
        original = _mod._scan_child_ids  # pyright: ignore[reportPrivateUsage]

        def spy(root: Path) -> frozenset[tuple[str, tuple[int, int]]]:
            scan_calls.append(root)
            return original(root)

        monkeypatch.setattr(_mod, "_scan_child_ids", spy)
        _cached_discover_vscode_logs(tmp_path)
        assert scan_calls == [], "child scan must be skipped on root_id cache hit"


# ---------------------------------------------------------------------------
# Correctness-equivalence test for the optimised _update_vscode_summary loop
# ---------------------------------------------------------------------------


class TestUpdateVscodeSummaryLargeScale:
    """Verify _update_vscode_summary produces correct aggregations at scale.

    Builds a synthetic list of 10 000+ VSCodeRequest objects spanning
    multiple models, categories, and dates and asserts the accumulated
    result is bit-for-bit identical to a hand-computed reference.
    No wall-clock timing — only deterministic correctness checks.
    """

    @staticmethod
    def _build_requests(n: int = 10_000) -> list[VSCodeRequest]:
        """Build *n* synthetic requests across several models/categories/dates."""
        models = ["gpt-4o", "gpt-4o-mini", "claude-opus-4.6", "o3-mini"]
        categories = ["inline", "panel/editAgent", "copilotLanguageModelWrapper"]
        base = datetime(2026, 3, 1, 0, 0, 0)
        requests: list[VSCodeRequest] = []
        for i in range(n):
            ts = base.replace(
                day=1 + (i % 28),
                hour=i % 24,
                minute=i % 60,
                second=i % 60,
            )
            requests.append(
                VSCodeRequest(
                    timestamp=ts,
                    request_id=f"req{i:06d}",
                    model=models[i % len(models)],
                    duration_ms=50 + i,
                    category=categories[i % len(categories)],
                )
            )
        return requests

    def test_aggregation_matches_reference(self) -> None:
        """Accumulated totals match a manually computed reference."""
        requests = self._build_requests(10_500)
        acc = _SummaryAccumulator()
        _update_vscode_summary(acc, requests)

        assert acc.total_requests == 10_500

        # Total duration: sum(50 + i for i in range(10_500))
        expected_total_dur = sum(50 + i for i in range(10_500))
        assert acc.total_duration_ms == expected_total_dur

        # Per-model counts: 4 models cycled evenly → each gets 10_500 // 4
        # with remainder distributed to first models.
        models = ["gpt-4o", "gpt-4o-mini", "claude-opus-4.6", "o3-mini"]
        for idx, m in enumerate(models):
            expected_count = 10_500 // 4 + (1 if idx < 10_500 % 4 else 0)
            assert acc.requests_by_model[m] == expected_count

        # Per-model durations: sum(50 + i for i where i % 4 == model_index)
        for idx, m in enumerate(models):
            expected_dur = sum(50 + i for i in range(idx, 10_500, 4))
            assert acc.duration_by_model[m] == expected_dur

        # Per-category counts: 3 categories cycled evenly
        categories = ["inline", "panel/editAgent", "copilotLanguageModelWrapper"]
        for idx, c in enumerate(categories):
            expected_count = 10_500 // 3 + (1 if idx < 10_500 % 3 else 0)
            assert acc.requests_by_category[c] == expected_count

        # Per-date counts: compute the exact expected mapping from input
        # requests so we verify the full distribution, not just the total.
        expected_requests_by_date: dict[str, int] = {}
        for request in requests:
            date_key = request.timestamp.date().isoformat()
            expected_requests_by_date[date_key] = (
                expected_requests_by_date.get(date_key, 0) + 1
            )
        assert acc.requests_by_date == expected_requests_by_date

    def test_timestamp_bounds(self) -> None:
        """first_timestamp and last_timestamp are correct min/max."""
        requests = self._build_requests(10_000)
        acc = _SummaryAccumulator()
        _update_vscode_summary(acc, requests)

        expected_first = min(r.timestamp for r in requests)
        expected_last = max(r.timestamp for r in requests)
        assert acc.first_timestamp == expected_first
        assert acc.last_timestamp == expected_last

    def test_empty_input(self) -> None:
        """Passing an empty sequence leaves the accumulator unchanged."""
        acc = _SummaryAccumulator()
        _update_vscode_summary(acc, [])
        assert acc.total_requests == 0
        assert acc.total_duration_ms == 0
        assert acc.first_timestamp is None
        assert acc.last_timestamp is None
