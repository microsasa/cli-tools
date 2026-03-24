"""Model pricing data and premium-request cost estimation.

GitHub Copilot charges different premium-request multipliers depending on the
AI model used.  This module provides:

* A ``ModelPricing`` Pydantic model for per-model pricing metadata.
* A registry of known multipliers (easy to update in one place).
* Lookup helpers that handle exact matches, partial matches, and unknown models.
* A cost-estimation function that works with ``SessionSummary.model_metrics``.
"""

import warnings
from enum import StrEnum
from typing import Final

from pydantic import BaseModel

__all__: list[str] = [
    "ModelPricing",
    "PricingTier",
    "KNOWN_PRICING",
    "lookup_model_pricing",
    "categorize_model",
]


# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------


class PricingTier(StrEnum):
    """Broad pricing tiers for Copilot models."""

    PREMIUM = "premium"
    STANDARD = "standard"
    LIGHT = "light"
    FREE = "free"


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class ModelPricing(BaseModel):
    """Pricing metadata for a single AI model."""

    model_name: str
    multiplier: float = 1.0
    tier: PricingTier = PricingTier.STANDARD


# ---------------------------------------------------------------------------
# Known pricing registry — edit this dict to update multipliers.
# ---------------------------------------------------------------------------


def _tier_from_multiplier(m: float) -> PricingTier:
    """Map a numeric multiplier to the corresponding ``PricingTier``."""
    if m >= 3.0:
        return PricingTier.PREMIUM
    if m == 0.0:
        return PricingTier.FREE
    if m < 1.0:
        return PricingTier.LIGHT
    return PricingTier.STANDARD


_RAW_MULTIPLIERS: Final[dict[str, float]] = {
    # Claude -----------------------------------------------------------------
    "claude-sonnet-4.6": 1.0,
    "claude-sonnet-4.5": 1.0,
    "claude-sonnet-4": 1.0,
    "claude-opus-4.6": 3.0,
    "claude-opus-4.6-1m": 6.0,
    "claude-opus-4.5": 3.0,
    "claude-haiku-4.5": 0.33,
    # GPT --------------------------------------------------------------------
    "gpt-5.4": 1.0,
    "gpt-5.2": 1.0,
    "gpt-5.1": 1.0,
    "gpt-5.1-codex": 1.0,
    "gpt-5.2-codex": 1.0,
    "gpt-5.3-codex": 1.0,
    "gpt-5.1-codex-max": 1.0,
    "gpt-5.1-codex-mini": 0.33,
    "gpt-5.4-mini": 0.0,
    "gpt-5-mini": 0.0,
    "gpt-4.1": 0.0,
    # Gemini -----------------------------------------------------------------
    "gemini-3-pro-preview": 1.0,
}

KNOWN_PRICING: Final[dict[str, ModelPricing]] = {
    name: ModelPricing(
        model_name=name,
        multiplier=mult,
        tier=_tier_from_multiplier(mult),
    )
    for name, mult in _RAW_MULTIPLIERS.items()
}

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def lookup_model_pricing(model_name: str) -> ModelPricing:
    """Return ``ModelPricing`` for *model_name*.

    Resolution order:

    1. Exact match in ``KNOWN_PRICING``.
    2. Partial match — *model_name* starts with a known key, or a known key
       starts with *model_name*.
    3. Fallback — returns a 1× standard entry and emits a
       :class:`UserWarning`.
    """
    # 1. Exact
    if model_name in KNOWN_PRICING:
        return KNOWN_PRICING[model_name]

    # 2. Partial (longest matching key wins to avoid false positives)
    best: ModelPricing | None = None
    best_len = 0
    for key, pricing in KNOWN_PRICING.items():
        if model_name.startswith(key) or key.startswith(model_name):
            match_len = min(len(key), len(model_name))
            if match_len > best_len:
                best = pricing
                best_len = match_len

    if best is not None:
        return ModelPricing(
            model_name=model_name,
            multiplier=best.multiplier,
            tier=best.tier,
        )

    # 3. Unknown
    warnings.warn(
        f"Unknown model '{model_name}'; assuming 1× standard pricing.",
        UserWarning,
        stacklevel=2,
    )
    return ModelPricing(
        model_name=model_name, multiplier=1.0, tier=PricingTier.STANDARD
    )


def categorize_model(model_name: str) -> PricingTier:
    """Return the pricing tier for *model_name*."""
    return lookup_model_pricing(model_name).tier
