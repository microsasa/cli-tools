import re
from pathlib import Path

from copilot_usage.pricing import KNOWN_PRICING

_ARCH_MD = (
    Path(__file__).parents[1] / "src/copilot_usage/docs/architecture.md"
).read_text(encoding="utf-8")

_IMPL_MD = (
    Path(__file__).parents[1] / "src/copilot_usage/docs/implementation.md"
).read_text(encoding="utf-8")

_README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


def test_implementation_md_has_no_line_number_citations() -> None:
    matches = re.findall(r"\.py:\d+(?:-\d+)?", _IMPL_MD)
    assert not matches, f"Found stale line-number citations: {matches}"


def _parse_pricing_table(
    doc: str,
    section_heading: str = r"Model Multiplier Reference|Model Pricing",
) -> dict[str, str]:
    """Return {model_name: tier_string} from the Markdown pricing table."""
    # Restrict parsing to the matching section to avoid accidentally picking
    # up rows from unrelated tables earlier in the doc.
    section_match = re.search(
        rf"^#+\s+(?:{section_heading})\b.*$",
        doc,
        re.MULTILINE,
    )
    if section_match is not None:
        doc = doc[section_match.end() :]

    rows: dict[str, str] = {}
    for m in re.finditer(
        # Match model rows with known tier values only, to avoid false positives.
        r"^\|\s*`([^`]+)`\s*\|[^|]+\|\s*(premium|standard|light|free)\s*\|",
        doc,
        re.MULTILINE | re.IGNORECASE,
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


def test_readme_pricing_table_lists_all_known_models() -> None:
    """Every model in KNOWN_PRICING must appear explicitly in README.md's
    pricing table and use the correct tier string."""
    table = _parse_pricing_table(_README)
    for model_name, pricing in KNOWN_PRICING.items():
        assert model_name in table, (
            f"Model '{model_name}' from KNOWN_PRICING is missing "
            f"from the pricing table in README.md"
        )
        assert table[model_name] == pricing.tier.value, (
            f"Tier mismatch for '{model_name}' in README.md: "
            f"doc says '{table[model_name]}', "
            f"pricing.py says '{pricing.tier.value}'"
        )


def test_tier_derivation_description_mentions_all_tiers() -> None:
    """The tier derivation sentence must mention all four tier names."""
    match = re.search(
        r"^(Tier is derived from the multiplier.+)$",
        _IMPL_MD,
        re.MULTILINE,
    )
    assert match, (
        "Could not find the 'Tier is derived from the multiplier...' "
        "sentence in implementation.md"
    )
    tier_sentence = match.group(1)
    for tier_name in ("Premium", "Free", "Light", "Standard"):
        assert tier_name in tier_sentence, (
            f"Tier derivation description in implementation.md "
            f"does not mention '{tier_name}'"
        )


def test_since_last_shutdown_documents_premium_cost_estimate() -> None:
    """The '↳ Since last shutdown' section must not claim 'N/A' for premium cost.

    The actual code uses ``_estimate_premium_cost()`` to produce a '~N' estimate
    in the Premium Cost column.  This test prevents future drift on that detail.
    """
    # Extract the section starting from the "↳ Since last shutdown" heading/rows
    # up to the next heading (## or deeper).
    match = re.search(
        r"(^##+[^\n]*↳ Since last shutdown[^\n]*\n.*?)(?=^##+|\Z)",
        _IMPL_MD,
        re.MULTILINE | re.DOTALL,
    )
    assert match, (
        "Could not find '### ↳ Since last shutdown' section in implementation.md"
    )
    section = match.group(1)
    # The docs should describe the actual implementation detail:
    # `_estimate_premium_cost()` is used to compute a '~N' Premium Cost.
    assert "_estimate_premium_cost" in section, (
        "The '↳ Since last shutdown' section in implementation.md should "
        "mention '_estimate_premium_cost' — the Premium Cost column is "
        "NOT 'N/A', it's an estimate."
    )
    # Guard against regressions to the old 'Premium Cost: N/A' wording.
    # Match patterns where N/A is directly attributed to Premium Cost
    # (e.g. "Premium Cost | N/A", "# Premium Cost — N/A") but not lines
    # where N/A refers to a different column mentioned on the same line.
    assert not re.search(
        r"Premium Cost\s*(?:[\|:—=]|is|shows)\s*[`'\"]?N/A",
        section,
    ), (
        "The '↳ Since last shutdown' section in implementation.md must not "
        "claim 'N/A' for Premium Cost."
    )
    # The code snippet must include the has_active_period_stats guard that
    # suppresses the row when there is no meaningful post-shutdown activity.
    assert re.search(
        r"^\s*if\b[^\n]*\bhas_active_period_stats\s*\(\s*s\s*\)\s*:",
        section,
        re.MULTILINE,
    ), (
        "The '↳ Since last shutdown' section in implementation.md must "
        "include the 'if ... has_active_period_stats(s):' guard — the row "
        "is suppressed when all active counters are 0 and "
        "last_resume_time is None."
    )


def test_components_table_lists_all_modules() -> None:
    """Every .py module in src/copilot_usage/ (excluding __init__.py and the
    docs/ subdirectory) must appear in the ### Components table in
    architecture.md."""
    pkg_dir = Path(__file__).parents[1] / "src" / "copilot_usage"
    on_disk = {p.name for p in pkg_dir.glob("*.py") if p.name != "__init__.py"}
    components_section_match = re.search(
        r"^###\s+Components\b.*?(?=^#{1,6}\s+|\Z)",
        _ARCH_MD,
        re.MULTILINE | re.DOTALL,
    )
    assert components_section_match, (
        "Could not find the '### Components' section in architecture.md"
    )
    components_section = components_section_match.group(0)
    # Extract backtick-quoted module names from rows in the Components table only.
    in_table = set(
        re.findall(r"^\|\s*`([^`]+\.py)`\s*\|", components_section, re.MULTILINE)
    )
    missing = on_disk - in_table
    assert not missing, (
        f"Modules missing from the ### Components table in "
        f"architecture.md: {sorted(missing)}"
    )


def test_build_completed_summary_signature_mentions_events() -> None:
    """The _build_completed_summary signature snippet must include the
    'events' parameter that was added for shutdown_cycles."""
    match = re.search(
        r"`_build_completed_summary\(([^)]*)\)`",
        _IMPL_MD,
    )
    assert match, (
        "Could not find '_build_completed_summary(...)' signature in implementation.md"
    )
    sig = match.group(1)
    assert "events" in sig, (
        "_build_completed_summary signature in implementation.md must "
        "include 'events' — the parameter is used to look up timestamps "
        "for shutdown_cycles"
    )


def test_all_shutdowns_type_is_tuple_not_list() -> None:
    """all_shutdowns must be documented as a tuple (matching the frozen
    _FirstPassResult dataclass), not a list."""
    assert "all_shutdowns: tuple" in _IMPL_MD, (
        "implementation.md must show 'all_shutdowns: tuple[...]' — "
        "_FirstPassResult is frozen so the field is an immutable tuple"
    )
    assert "all_shutdowns: list" not in _IMPL_MD, (
        "implementation.md still shows 'all_shutdowns: list[...]' — "
        "_FirstPassResult uses tuple, not list"
    )


def test_shutdown_loop_uses_idx_not_underscore_idx() -> None:
    """The shutdown loop snippet must use 'idx' (consumed for timestamp
    lookup), not '_idx' (which implies the variable is unused)."""
    assert "_idx" not in _IMPL_MD, (
        "implementation.md contains '_idx' — the index variable is used "
        "to look up event timestamps via events[idx].timestamp, so it "
        "must be spelled 'idx' (not '_idx')"
    )


def test_architecture_first_pass_mentions_resume_detection() -> None:
    """The _first_pass() description in architecture.md must mention
    post-shutdown resume data tracking."""
    match = re.search(
        r"^\s*[-*]\s+`_first_pass\(\)`:.*?(?=^\s*[-*]\s+`|\Z)",
        _ARCH_MD,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "Could not find the '_first_pass()' description in architecture.md"
    description = match.group(0)
    assert "resume" in description.lower(), (
        "_first_pass() description in architecture.md must mention resume detection"
    )
