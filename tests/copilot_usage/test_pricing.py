"""Tests for copilot_usage.pricing."""

# pyright: reportPrivateUsage=false

from functools import lru_cache

import pytest
from loguru import logger
from pydantic import ValidationError

from copilot_usage import pricing
from copilot_usage.pricing import (
    KNOWN_PRICING,
    ModelPricing,
    PricingTier,
    _cached_lookup,
    _tier_from_multiplier,
    lookup_model_pricing,
)


@pytest.fixture(autouse=True)
def _clear_pricing_cache() -> None:
    """Reset the LRU cache between tests to prevent cross-test leakage."""
    _cached_lookup.cache_clear()


# ---------------------------------------------------------------------------
# ModelPricing basics
# ---------------------------------------------------------------------------


class TestModelPricing:
    def test_defaults(self) -> None:
        p = ModelPricing(model_name="test-model")
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD

    def test_explicit_values(self) -> None:
        p = ModelPricing(model_name="opus", multiplier=50.0, tier=PricingTier.PREMIUM)
        assert p.model_name == "opus"
        assert p.multiplier == 50.0
        assert p.tier == PricingTier.PREMIUM

    def test_model_pricing_is_immutable(self) -> None:
        """Assigning to a field on a frozen ModelPricing must raise ValidationError."""
        p = ModelPricing(model_name="test", multiplier=1.0)
        with pytest.raises(ValidationError):
            p.multiplier = 2.0

    def test_cache_isolation_via_frozen_model(self) -> None:
        """Exact-match lookup returns a frozen object — mutation is impossible,
        so the cache and KNOWN_PRICING registry cannot be corrupted."""
        first = lookup_model_pricing("claude-sonnet-4.6")
        assert first.multiplier == 1.0

        with pytest.raises(ValidationError):
            first.multiplier = 99.0

        second = lookup_model_pricing("claude-sonnet-4.6")
        assert second.multiplier == 1.0


# ---------------------------------------------------------------------------
# _tier_from_multiplier boundary tests (issue #328)
# ---------------------------------------------------------------------------


class TestTierFromMultiplier:
    @pytest.mark.parametrize(
        ("multiplier", "expected"),
        [
            (0.0, PricingTier.FREE),  # exact FREE boundary
            (0.001, PricingTier.LIGHT),  # smallest non-zero → LIGHT, not FREE
            (0.33, PricingTier.LIGHT),  # haiku value
            (0.5, PricingTier.LIGHT),  # middle of LIGHT range
            (0.999, PricingTier.LIGHT),  # just below STANDARD boundary
            (1.0, PricingTier.STANDARD),  # exact STANDARD lower boundary
            (1.5, PricingTier.STANDARD),  # middle of STANDARD range
            (2.999, PricingTier.STANDARD),  # just below PREMIUM boundary
            (3.0, PricingTier.PREMIUM),  # exact PREMIUM boundary (inclusive)
            (6.0, PricingTier.PREMIUM),  # above threshold
        ],
    )
    def test_tier_classification(
        self, multiplier: float, expected: PricingTier
    ) -> None:
        assert _tier_from_multiplier(multiplier) == expected


# ---------------------------------------------------------------------------
# KNOWN_PRICING registry
# ---------------------------------------------------------------------------


class TestKnownPricing:
    def test_registry_not_empty(self) -> None:
        assert len(KNOWN_PRICING) > 0

    @pytest.mark.parametrize(
        ("model", "expected_mult"),
        [
            ("claude-sonnet-4", 1.0),
            ("claude-opus-4.6", 3.0),
            ("claude-opus-4.6-1m", 6.0),
            ("claude-haiku-4.5", 0.33),
            ("gpt-5.1-codex-max", 1.0),
            ("gpt-4.1", 0.0),
            ("gpt-5-mini", 0.0),
            ("gpt-5.4-mini", 0.0),
            ("gpt-4o-mini", 0.0),
            ("gpt-4o-mini-2024-07-18", 0.0),
            ("copilot-nes-oct", 0.0),
            ("copilot-suggestions-himalia-001", 0.0),
            ("gemini-3-pro-preview", 1.0),
        ],
    )
    def test_known_multipliers(self, model: str, expected_mult: float) -> None:
        assert KNOWN_PRICING[model].multiplier == expected_mult

    @pytest.mark.parametrize(
        ("model", "expected_tier"),
        [
            ("claude-opus-4.5", PricingTier.PREMIUM),
            ("claude-sonnet-4.6", PricingTier.STANDARD),
            ("claude-haiku-4.5", PricingTier.LIGHT),
            ("gpt-5-mini", PricingTier.FREE),
            ("gpt-5.4-mini", PricingTier.FREE),
            ("gpt-4.1", PricingTier.FREE),
            ("gpt-4o-mini", PricingTier.FREE),
            ("copilot-nes-oct", PricingTier.FREE),
        ],
    )
    def test_known_tiers(self, model: str, expected_tier: PricingTier) -> None:
        assert KNOWN_PRICING[model].tier == expected_tier


# ---------------------------------------------------------------------------
# lookup_model_pricing
# ---------------------------------------------------------------------------


class TestLookupModelPricing:
    def test_exact_match(self) -> None:
        p = lookup_model_pricing("claude-sonnet-4.6")
        assert p.multiplier == 1.0
        assert p.model_name == "claude-sonnet-4.6"

    def test_partial_match_model_longer(self) -> None:
        """Model name is longer than any key — still matches the longest prefix."""
        p = lookup_model_pricing("claude-opus-4.6-1m")
        assert p.multiplier == 6.0

    def test_partial_match_model_shorter(self) -> None:
        """Model name is a prefix of a known key."""
        p = lookup_model_pricing("gemini-3-pro")
        assert p.multiplier == 1.0

    def test_unknown_model_logs_warning(self) -> None:
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("totally-unknown-model-9000")
        finally:
            logger.remove(sink_id)
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert any("Unknown model" in m for m in messages)

    def test_unknown_model_returns_name(self) -> None:
        p = lookup_model_pricing("mystery")
        assert p.model_name == "mystery"


# ---------------------------------------------------------------------------
# Partial-match tie-breaking (Gap 1 — issue #258)
# ---------------------------------------------------------------------------


class TestPartialMatchTieBreaking:
    def test_multiple_partial_candidates_same_length_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When several keys share the same overlap length, the lookup falls
        back to unknown-model pricing and logs a warning.

        ``"gpt-5.1-cod"`` (11 chars) matches:
          - ``"gpt-5.1-codex"``      → match_len = min(13, 11) = 11
          - ``"gpt-5.1-codex-max"``  → match_len = min(17, 11) = 11
          - ``"gpt-5.1-codex-mini"`` → match_len = min(18, 11) = 11

        All three share the same overlap — ambiguous, so fallback.
        """
        local_pricing = {
            "gpt-5.1-codex": ModelPricing(
                model_name="gpt-5.1-codex",
                multiplier=1.0,
                tier=PricingTier.STANDARD,
            ),
            "gpt-5.1-codex-max": ModelPricing(
                model_name="gpt-5.1-codex-max",
                multiplier=2.0,
                tier=PricingTier.PREMIUM,
            ),
            "gpt-5.1-codex-mini": ModelPricing(
                model_name="gpt-5.1-codex-mini",
                multiplier=0.5,
                tier=PricingTier.LIGHT,
            ),
        }
        monkeypatch.setattr("copilot_usage.pricing.KNOWN_PRICING", local_pricing)
        _cached_lookup.cache_clear()

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("gpt-5.1-cod")
        finally:
            logger.remove(sink_id)

        assert p.model_name == "gpt-5.1-cod"
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert any("Ambiguous partial match" in m for m in messages)

    def test_partial_match_tie_falls_back_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``"gpt-5"`` partially matches multiple ``gpt-5.*`` keys with the
        same overlap length. Ties fall back to unknown-model pricing.
        """
        local_pricing = {
            "gpt-5-mini": ModelPricing(
                model_name="gpt-5-mini",
                multiplier=0.0,
                tier=PricingTier.FREE,
            ),
            "gpt-5-pro": ModelPricing(
                model_name="gpt-5-pro",
                multiplier=1.0,
                tier=PricingTier.STANDARD,
            ),
        }
        monkeypatch.setattr("copilot_usage.pricing.KNOWN_PRICING", local_pricing)
        _cached_lookup.cache_clear()

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("gpt-5")
        finally:
            logger.remove(sink_id)

        assert p.model_name == "gpt-5"
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert any("Ambiguous partial match" in m for m in messages)


# ---------------------------------------------------------------------------
# Tie-breaking against the production registry (Gap 1 — issue #275)
# ---------------------------------------------------------------------------


class TestLookupModelPricingTieBreaking:
    """Tests for equal-length partial match resolution against the real registry.

    When multiple keys tie on overlap length, the lookup now falls back to
    unknown-model pricing (1.0×, STANDARD) and logs a warning.
    """

    def test_claude_opus_4_falls_back_on_tie(self) -> None:
        """``"claude-opus-4"`` (13 chars) matches ``claude-opus-4.5``,
        ``claude-opus-4.6``, and ``claude-opus-4.6-1m`` all at
        ``min_len=13``.  Ambiguous → falls back to unknown.
        """
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("claude-opus-4")
        finally:
            logger.remove(sink_id)

        assert p.model_name == "claude-opus-4"
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert any("Ambiguous partial match" in m for m in messages)

    def test_gpt_5_falls_back_on_tie(self) -> None:
        """``"gpt-5"`` (5 chars) matches ``gpt-5.4``, ``gpt-5.2``,
        ``gpt-5.1``, ``gpt-5-mini``, etc. all at ``min_len=5``.
        Ambiguous → falls back to unknown.
        """
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("gpt-5")
        finally:
            logger.remove(sink_id)

        assert p.model_name == "gpt-5"
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert any("Ambiguous partial match" in m for m in messages)


# ---------------------------------------------------------------------------
# Issue #355 — lookup_model_pricing empty/unknown string edge cases
# ---------------------------------------------------------------------------


class TestLookupModelPricingEdgeCases:
    def test_empty_string_logs_and_returns_standard(self) -> None:
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("")
        finally:
            logger.remove(sink_id)
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert any("Empty model name" in m for m in messages)

    def test_whitespace_only_logs_and_returns_standard(self) -> None:
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("   ")
        finally:
            logger.remove(sink_id)
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert p.model_name == ""
        assert any("Empty model name" in m for m in messages)

    def test_single_char_unknown_logs_and_returns_standard(self) -> None:
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            p = lookup_model_pricing("x")
        finally:
            logger.remove(sink_id)
        assert p.multiplier == 1.0
        assert any("Unknown model" in m for m in messages)


# ---------------------------------------------------------------------------
# Case-insensitive and whitespace-tolerant lookup (issue #431)
# ---------------------------------------------------------------------------


class TestLookupModelPricingCaseNormalization:
    """lookup_model_pricing normalizes input with .lower().strip()."""

    def test_mixed_case_premium_model_resolves_correctly(self) -> None:
        """'Claude-Opus-4.6' resolves to the correct 3.0 multiplier."""
        p = lookup_model_pricing("Claude-Opus-4.6")
        assert p.multiplier == 3.0
        assert p.tier == PricingTier.PREMIUM

    def test_whitespace_padded_model_resolves_correctly(self) -> None:
        """Model name with trailing space resolves correctly."""
        p = lookup_model_pricing("claude-opus-4.6 ")
        assert p.multiplier == 3.0
        assert p.tier == PricingTier.PREMIUM

    def test_uppercase_free_model_resolves_correctly(self) -> None:
        """'GPT-5-mini' resolves to the FREE 0.0 multiplier."""
        p = lookup_model_pricing("GPT-5-mini")
        assert p.multiplier == 0.0
        assert p.tier == PricingTier.FREE

    def test_all_uppercase_model_resolves(self) -> None:
        """Fully uppercase model name resolves correctly."""
        p = lookup_model_pricing("CLAUDE-SONNET-4")
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD

    def test_leading_and_trailing_whitespace_stripped(self) -> None:
        """Leading and trailing whitespace is stripped before lookup."""
        p = lookup_model_pricing("  gpt-5.4-mini  ")
        assert p.multiplier == 0.0
        assert p.tier == PricingTier.FREE

    def test_mixed_case_partial_match(self) -> None:
        """Partial match works with mixed-case input."""
        p = lookup_model_pricing("Claude-Opus-4.6-1M-EXTRA")
        assert p.multiplier == 6.0
        assert p.tier == PricingTier.PREMIUM


# ---------------------------------------------------------------------------
# Caching behaviour (issue #493)
# ---------------------------------------------------------------------------


class TestLookupModelPricingCached:
    """Verify that repeated lookups hit the LRU cache instead of rescanning."""

    def test_lookup_model_pricing_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = 0
        original = pricing._cached_lookup.__wrapped__

        def counting_lookup(normalized: str) -> tuple[ModelPricing, bool]:
            nonlocal call_count
            call_count += 1
            return original(normalized)

        monkeypatch.setattr(
            pricing,
            "_cached_lookup",
            lru_cache(maxsize=256)(counting_lookup),
        )
        pricing._cached_lookup.cache_clear()

        for _ in range(50):
            pricing.lookup_model_pricing("claude-sonnet-4.6")

        assert call_count == 1, (
            "Should only scan KNOWN_PRICING once for a repeated model name"
        )

    def test_unknown_model_warning_log_emitted_on_every_call(self) -> None:
        """Warning log must fire on every call, not just the first (cache miss).

        This is the key regression: the old ``warnings.warn`` inside
        ``@lru_cache`` silently stopped firing after the first call.
        """
        first_messages: list[str] = []
        first_sink_id = logger.add(
            first_messages.append, level="WARNING", format="{message}"
        )
        try:
            lookup_model_pricing("unknown-xyz")
        finally:
            logger.remove(first_sink_id)

        second_messages: list[str] = []
        second_sink_id = logger.add(
            second_messages.append, level="WARNING", format="{message}"
        )
        try:
            lookup_model_pricing("unknown-xyz")
        finally:
            logger.remove(second_sink_id)

        assert any("Unknown model" in m for m in first_messages)
        assert any("Unknown model" in m for m in second_messages)


# ---------------------------------------------------------------------------
# Callers no longer use warnings (issue #695)
# ---------------------------------------------------------------------------


class TestCallersNoLongerSuppressWarnings:
    """Verify that report.py and vscode_report.py do not import warnings."""

    def test_report_does_not_import_warnings(self) -> None:
        from pathlib import Path

        import copilot_usage.report

        path = Path(copilot_usage.report.__file__)
        text = path.read_text()
        assert "import warnings" not in text

    def test_vscode_report_does_not_import_warnings(self) -> None:
        from pathlib import Path

        import copilot_usage.vscode_report

        path = Path(copilot_usage.vscode_report.__file__)
        text = path.read_text()
        assert "import warnings" not in text
