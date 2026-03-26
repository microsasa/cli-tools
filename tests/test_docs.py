import re
from pathlib import Path

from copilot_usage.pricing import KNOWN_PRICING

_IMPL_MD = (
    Path(__file__).parents[1] / "src/copilot_usage/docs/implementation.md"
).read_text(encoding="utf-8")


def test_implementation_md_has_no_line_number_citations() -> None:
    matches = re.findall(r"\.py:\d+(?:-\d+)?", _IMPL_MD)
    assert not matches, f"Found stale line-number citations: {matches}"


def _parse_pricing_table(doc: str) -> dict[str, str]:
    """Return {model_name: tier_string} from the Markdown pricing table."""
    rows: dict[str, str] = {}
    for m in re.finditer(
        r"^\|\s*`([^`]+)`\s*\|[^|]+\|\s*(\w+)\s*\|",
        doc,
        re.MULTILINE,
    ):
        rows[m.group(1)] = m.group(2).lower()
    return rows


def test_pricing_table_matches_known_pricing() -> None:
    """Every model in KNOWN_PRICING must appear in the doc table with the
    correct tier string."""
    table = _parse_pricing_table(_IMPL_MD)
    for model_name, pricing in KNOWN_PRICING.items():
        assert model_name in table, (
            f"Model '{model_name}' from KNOWN_PRICING is missing "
            f"from the pricing table in implementation.md"
        )
        assert table[model_name] == pricing.tier.value, (
            f"Tier mismatch for '{model_name}': "
            f"doc says '{table[model_name]}', "
            f"pricing.py says '{pricing.tier.value}'"
        )


def test_tier_derivation_description_mentions_all_tiers() -> None:
    """The tier derivation sentence must mention all four tier names."""
    for tier_name in ("Premium", "Free", "Light", "Standard"):
        assert tier_name in _IMPL_MD, (
            f"Tier derivation description in implementation.md "
            f"does not mention '{tier_name}'"
        )
