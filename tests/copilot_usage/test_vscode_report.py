"""Tests for copilot_usage.vscode_report — rendering of VS Code summary."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import warnings
from datetime import datetime
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from copilot_usage.pricing import ModelPricing, PricingTier
from copilot_usage.vscode_parser import VSCodeLogSummary
from copilot_usage.vscode_report import _DAILY_ACTIVITY_LIMIT, render_vscode_summary


def _capture(summary: VSCodeLogSummary) -> str:
    """Render *summary* and return the plain-text output."""
    buf = StringIO()
    console = Console(file=buf, width=120, no_color=True)
    render_vscode_summary(summary, target_console=console)
    return buf.getvalue()


def _make_summary(
    *,
    total_requests: int = 0,
    total_duration_ms: int = 0,
    requests_by_model: dict[str, int] | None = None,
    duration_by_model: dict[str, int] | None = None,
    requests_by_category: dict[str, int] | None = None,
    requests_by_date: dict[str, int] | None = None,
    first_timestamp: datetime | None = None,
    last_timestamp: datetime | None = None,
    log_files_parsed: int = 1,
) -> VSCodeLogSummary:
    s = VSCodeLogSummary()
    s.total_requests = total_requests
    s.total_duration_ms = total_duration_ms
    s.requests_by_model = requests_by_model or {}
    s.duration_by_model = duration_by_model or {}
    s.requests_by_category = requests_by_category or {}
    s.requests_by_date = requests_by_date or {}
    s.first_timestamp = first_timestamp
    s.last_timestamp = last_timestamp
    s.log_files_parsed = log_files_parsed
    return s


# ---------------------------------------------------------------------------
# Totals panel
# ---------------------------------------------------------------------------


class TestRenderVscodeSummaryTotalsPanel:
    def test_date_range_with_both_timestamps(self) -> None:
        summary = _make_summary(
            total_requests=10,
            total_duration_ms=5000,
            first_timestamp=datetime(2026, 3, 13, 22, 10),
            last_timestamp=datetime(2026, 3, 14, 10, 0),
        )
        output = _capture(summary)
        assert "2026-03-13 22:10" in output
        assert "2026-03-14 10:00" in output
        assert "→" in output

    def test_date_range_without_timestamps(self) -> None:
        summary = _make_summary(total_requests=0)
        output = _capture(summary)
        assert "—" in output

    def test_request_count_rendered(self) -> None:
        summary = _make_summary(total_requests=42, total_duration_ms=120_000)
        output = _capture(summary)
        assert "42" in output

    def test_api_time_rendered(self) -> None:
        summary = _make_summary(total_requests=1, total_duration_ms=120_000)
        output = _capture(summary)
        # format_duration(120_000) produces "2m"
        assert "2m" in output

    def test_log_files_count_rendered(self) -> None:
        summary = _make_summary(log_files_parsed=3)
        output = _capture(summary)
        assert "3" in output


# ---------------------------------------------------------------------------
# Per-model breakdown table
# ---------------------------------------------------------------------------


class TestRenderVscodeSummaryPerModelTable:
    def test_per_model_table_rendered(self) -> None:
        summary = _make_summary(
            total_requests=5,
            total_duration_ms=10_000,
            requests_by_model={"claude-opus-4.6": 3, "gpt-4o-mini": 2},
            duration_by_model={"claude-opus-4.6": 9000, "gpt-4o-mini": 1000},
        )
        output = _capture(summary)
        assert "Per-Model Breakdown" in output
        assert "claude-opus-4.6" in output
        assert "gpt-4o-mini" in output

    def test_tier_column_uses_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = MagicMock(
            return_value=ModelPricing(
                model_name="claude-opus-4.6",
                multiplier=3.0,
                tier=PricingTier.PREMIUM,
            )
        )
        monkeypatch.setattr("copilot_usage.vscode_report.lookup_model_pricing", spy)
        summary = _make_summary(
            total_requests=1,
            requests_by_model={"claude-opus-4.6": 1},
            duration_by_model={"claude-opus-4.6": 500},
        )
        output = _capture(summary)
        spy.assert_called_once_with("claude-opus-4.6")
        assert "premium" in output

    def test_tier_lookup_suppresses_warnings(self) -> None:
        """Unknown models must not leak UserWarning to the caller."""
        summary = _make_summary(
            total_requests=1,
            requests_by_model={"totally-unknown-model-xyz": 1},
            duration_by_model={"totally-unknown-model-xyz": 100},
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _capture(summary)
        user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(user_warnings) == 0, f"Leaked warnings: {user_warnings}"

    def test_avg_ms_calculation(self) -> None:
        summary = _make_summary(
            total_requests=4,
            requests_by_model={"gpt-4o-mini": 4},
            duration_by_model={"gpt-4o-mini": 2000},
        )
        output = _capture(summary)
        # avg_ms = 2000 // 4 = 500
        assert "500ms" in output

    def test_avg_ms_division_by_zero_guard(self) -> None:
        """A model with count=0 in duration_by_model produces avg_ms=0."""
        summary = _make_summary(
            total_requests=0,
            requests_by_model={"some-model": 0},
            duration_by_model={"some-model": 100},
        )
        output = _capture(summary)
        assert "0ms" in output

    def test_total_duration_formatted(self) -> None:
        summary = _make_summary(
            total_requests=2,
            requests_by_model={"gpt-4o-mini": 2},
            duration_by_model={"gpt-4o-mini": 389_114},
        )
        output = _capture(summary)
        # format_duration(389_114) -> "6m 29s"
        assert "6m 29s" in output

    def test_empty_requests_by_model_table_absent(self) -> None:
        summary = _make_summary(total_requests=5, requests_by_model={})
        output = _capture(summary)
        assert "Per-Model Breakdown" not in output


# ---------------------------------------------------------------------------
# By-feature table
# ---------------------------------------------------------------------------


class TestRenderVscodeSummaryByFeatureTable:
    def test_by_feature_table_rendered(self) -> None:
        summary = _make_summary(
            total_requests=10,
            requests_by_category={"panel/editAgent": 7, "title": 3},
        )
        output = _capture(summary)
        assert "By Feature" in output
        assert "panel/editAgent" in output
        assert "title" in output

    def test_percentage_calculation(self) -> None:
        summary = _make_summary(
            total_requests=10,
            requests_by_category={"panel/editAgent": 7, "title": 3},
        )
        output = _capture(summary)
        # 7/10*100 = 70.0%
        assert "70.0%" in output
        # 3/10*100 = 30.0%
        assert "30.0%" in output

    def test_empty_requests_by_category_table_absent(self) -> None:
        summary = _make_summary(total_requests=5, requests_by_category={})
        output = _capture(summary)
        assert "By Feature" not in output


# ---------------------------------------------------------------------------
# Daily activity table
# ---------------------------------------------------------------------------


class TestRenderVscodeSummaryDailyActivity:
    def test_daily_activity_table_rendered(self) -> None:
        summary = _make_summary(
            total_requests=3,
            requests_by_date={"2026-03-13": 2, "2026-03-14": 1},
        )
        output = _capture(summary)
        assert "Daily Activity" in output
        assert "2026-03-13" in output
        assert "2026-03-14" in output

    def test_more_than_limit_shows_only_recent(self) -> None:
        dates = {f"2026-03-{d:02d}": d for d in range(1, 20)}
        summary = _make_summary(
            total_requests=sum(dates.values()),
            requests_by_date=dates,
        )
        output = _capture(summary)
        assert "Daily Activity" in output
        # Only the 14 most-recent dates should appear (March 6–19)
        for d in range(6, 20):
            assert f"2026-03-{d:02d}" in output
        # Oldest dates should be dropped (March 1–5)
        for d in range(1, 6):
            assert f"2026-03-{d:02d}" not in output

    def test_exactly_limit_all_rendered(self) -> None:
        dates = {f"2026-03-{d:02d}": 1 for d in range(1, _DAILY_ACTIVITY_LIMIT + 1)}
        summary = _make_summary(
            total_requests=_DAILY_ACTIVITY_LIMIT,
            requests_by_date=dates,
        )
        output = _capture(summary)
        for d in range(1, _DAILY_ACTIVITY_LIMIT + 1):
            assert f"2026-03-{d:02d}" in output

    def test_empty_requests_by_date_table_absent(self) -> None:
        summary = _make_summary(total_requests=5, requests_by_date={})
        output = _capture(summary)
        assert "Daily Activity" not in output
