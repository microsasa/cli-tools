"""Tests for copilot_usage.pricing."""

import warnings

import pytest

from copilot_usage.pricing import (
    KNOWN_PRICING,
    ModelPricing,
    PricingTier,
    categorize_model,
    lookup_model_pricing,
)

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

    def test_unknown_model_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            p = lookup_model_pricing("totally-unknown-model-9000")
        assert p.multiplier == 1.0
        assert p.tier == PricingTier.STANDARD
        assert len(caught) == 1
        assert "Unknown model" in str(caught[0].message)

    def test_unknown_model_returns_name(self) -> None:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            p = lookup_model_pricing("mystery")
        assert p.model_name == "mystery"


# ---------------------------------------------------------------------------
# categorize_model
# ---------------------------------------------------------------------------


class TestCategorizeModel:
    def test_premium(self) -> None:
        assert categorize_model("claude-opus-4.6") == PricingTier.PREMIUM

    def test_standard(self) -> None:
        assert categorize_model("gpt-5.4") == PricingTier.STANDARD

    def test_light(self) -> None:
        assert categorize_model("claude-haiku-4.5") == PricingTier.LIGHT

    def test_free(self) -> None:
        assert categorize_model("gpt-5-mini") == PricingTier.FREE

    def test_free_gpt_4_1(self) -> None:
        assert categorize_model("gpt-4.1") == PricingTier.FREE


# ---------------------------------------------------------------------------
# Partial-match tie-breaking (Gap 1 — issue #258)
# ---------------------------------------------------------------------------


class TestPartialMatchTieBreaking:
    def test_multiple_partial_candidates_same_length_deterministic(self) -> None:
        """When several keys share the same overlap length, the first-inserted
        matching key in KNOWN_PRICING wins (because the loop uses strict ``>``).

        ``"gpt-5.1-cod"`` (11 chars) matches:
          - ``"gpt-5.1-codex"``      → match_len = min(13, 11) = 11
          - ``"gpt-5.1-codex-max"``  → match_len = min(17, 11) = 11
          - ``"gpt-5.1-codex-mini"`` → match_len = min(18, 11) = 11

        All three share the same overlap; the first one in iteration order
        (``gpt-5.1-codex``, multiplier 1.0) is selected.  Because
        ``gpt-5.1-codex-mini`` has a *different* multiplier/tier, we can
        verify the tiebreak did not pick a later candidate with divergent
        pricing.
        """
        p = lookup_model_pricing("gpt-5.1-cod")
        # Collect all candidates with the maximum overlap length
        candidates = [
            (k, v)
            for k, v in KNOWN_PRICING.items()
            if "gpt-5.1-cod".startswith(k) or k.startswith("gpt-5.1-cod")
        ]
        max_overlap = max(min(len(k), len("gpt-5.1-cod")) for k, _ in candidates)
        tied = [
            (k, v)
            for k, v in candidates
            if min(len(k), len("gpt-5.1-cod")) == max_overlap
        ]
        assert len(tied) > 1, "need multiple tied candidates to test tiebreak"
        # First-inserted key with max overlap wins (strict > comparison)
        first = tied[0][1]
        # At least one tied candidate must differ so the assertion is meaningful
        assert any(
            v.multiplier != first.multiplier or v.tier != first.tier
            for _, v in tied[1:]
        ), "tied candidates must have divergent pricing to make tiebreak observable"
        assert p.multiplier == first.multiplier
        assert p.tier == first.tier

    def test_partial_match_does_not_confuse_gpt5_mini_with_gpt5_standard(
        self,
    ) -> None:
        """``"gpt-5"`` partially matches many ``gpt-5.*`` keys, all with
        ``match_len=5``.  Verify the resolved tier is deterministic and
        matches the first-inserted candidate in ``KNOWN_PRICING``
        (strict ``>`` tiebreak).
        """
        p = lookup_model_pricing("gpt-5")
        candidates = [
            v
            for k, v in KNOWN_PRICING.items()
            if "gpt-5".startswith(k) or k.startswith("gpt-5")
        ]
        assert len(candidates) > 1, "expected multiple partial matches"
        # First match in iteration order wins
        expected = candidates[0]
        assert p.multiplier == expected.multiplier
        assert p.tier == expected.tier
