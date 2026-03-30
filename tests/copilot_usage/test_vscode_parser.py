"""Tests for copilot_usage.vscode_parser and the vscode CLI subcommand."""

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
        # Ensure the constructed line still matches the CCREQ_RE regex; otherwise
        # this test would no longer exercise the ValueError timestamp branch.
        assert CCREQ_RE.match(bad_line) is not None
        good_line = _LOG_OPUS  # a valid known-good line
        log_file = tmp_path / "test.log"
        log_file.write_text(f"{bad_line}\n{good_line}", encoding="utf-8")
        result = parse_vscode_log(log_file)
        assert len(result) == 1  # bad line skipped
        assert result[0].model == "claude-opus-4.6"

    def test_all_lines_invalid_timestamp_returns_empty_list(
        self, tmp_path: Path
    ) -> None:
        """All lines match CCREQ_RE but have invalid timestamps → returns [], not None."""
        bad_ts = "9999-99-99 99:99:99.000"
        bad_line = (
            f"{bad_ts} [info] ccreq:abc123.copilotmd"
            " | success | claude-sonnet-4 | 100ms | [panel]"
        )
        assert CCREQ_RE.match(bad_line) is not None  # regex matches
        log_file = tmp_path / "all_bad.log"
        log_file.write_text(f"{bad_line}\n{bad_line}\n", encoding="utf-8")
        result = parse_vscode_log(log_file)
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
        """Per-file incremental processing: requests are aggregated per file."""
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
                "copilot_usage.vscode_parser.discover_vscode_logs",
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
                "copilot_usage.vscode_parser.discover_vscode_logs",
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

    def test_finalized_summary_uses_plain_dict(self) -> None:
        """_finalize_summary converts defaultdict to plain dict."""
        summary = build_vscode_summary(_make_bulk_requests())
        assert type(summary.requests_by_model) is dict
        assert type(summary.duration_by_model) is dict
        assert type(summary.requests_by_category) is dict
        assert type(summary.requests_by_date) is dict
