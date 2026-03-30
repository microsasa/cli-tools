"""Tests for copilot_usage.vscode_parser and the vscode CLI subcommand."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from copilot_usage.cli import main
from copilot_usage.vscode_parser import (
    CCREQ_RE,
    VSCodeRequest,
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


# ---------------------------------------------------------------------------
# CCREQ_RE regex
# ---------------------------------------------------------------------------


class TestCcreqRegex:
    def test_normal_line(self) -> None:
        m = CCREQ_RE.match(_LOG_OPUS)
        assert m is not None
        ts, req_id, model, dur, cat = m.groups()
        assert ts == "2026-03-13 22:10:24.523"
        assert req_id == "c0c8885e"
        assert model == "claude-opus-4.6"
        assert dur == "8003"
        assert cat == "panel/editAgent"

    def test_redirect_line(self) -> None:
        m = CCREQ_RE.match(_LOG_REDIRECT)
        assert m is not None
        _, _, model, dur, cat = m.groups()
        assert model == "gpt-4o-mini"
        assert dur == "481"
        assert cat == "copilotLanguageModelWrapper"

    def test_plain_model_line(self) -> None:
        m = CCREQ_RE.match(_LOG_GPT4O)
        assert m is not None
        _, _, model, dur, cat = m.groups()
        assert model == "gpt-4o-mini-2024-07-18"
        assert dur == "432"
        assert cat == "title"

    def test_noise_line_does_not_match(self) -> None:
        assert CCREQ_RE.match(_LOG_NOISE) is None

    def test_empty_line_does_not_match(self) -> None:
        assert CCREQ_RE.match("") is None


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
        assert requests is not None
        assert len(requests) == 3
        assert requests[0].model == "claude-opus-4.6"
        assert requests[0].duration_ms == 8003
        assert requests[1].model == "gpt-4o-mini"
        assert requests[2].model == "gpt-4o-mini-2024-07-18"

    def test_empty_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "empty.log"
        log_file.write_text("", encoding="utf-8")
        assert parse_vscode_log(log_file) == []

    def test_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_such.log"
        assert parse_vscode_log(missing) is None

    def test_invalid_timestamp_line_is_skipped(self, tmp_path: Path) -> None:
        """A regex-matching line with an unparseable timestamp is skipped."""
        bad_ts = "9999-99-99 99:99:99.000"  # impossible date triggers ValueError
        bad_line = (
            f"{bad_ts} [info] ccreq:abc123.copilotmd"
            " | success | claude-sonnet-4 | 100ms | [panel]"
        )
        good_line = _LOG_OPUS  # a valid known-good line
        log_file = tmp_path / "test.log"
        log_file.write_text(f"{bad_line}\n{good_line}", encoding="utf-8")
        result = parse_vscode_log(log_file)
        assert result is not None
        assert len(result) == 1  # bad line skipped
        assert result[0].model == "claude-opus-4.6"


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
        with patch.object(Path, "is_dir", return_value=False):
            result = discover_vscode_logs()
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
        assert "claude-opus-4.6" in summary.requests_by_model

    def test_no_logs(self, tmp_path: Path) -> None:
        summary = get_vscode_summary(tmp_path)
        assert summary.total_requests == 0
        assert summary.log_files_parsed == 0


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
