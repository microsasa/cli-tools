import re
from pathlib import Path


def test_implementation_md_has_no_line_number_citations() -> None:
    doc = (
        Path(__file__).parents[1] / "src/copilot_usage/docs/implementation.md"
    ).read_text()
    matches = re.findall(r"\.py:\d", doc)
    assert not matches, f"Found stale line-number citations: {matches}"
