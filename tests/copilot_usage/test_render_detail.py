"""Tests for copilot_usage.render_detail — private helper coverage (issue #470)."""

# pyright: reportPrivateUsage=false

import io

import pytest
from rich.console import Console

from copilot_usage.models import (
    CodeChanges,
    ToolExecutionData,
    ToolTelemetry,
)
from copilot_usage.render_detail import _extract_tool_name, _render_code_changes

# ---------------------------------------------------------------------------
# _extract_tool_name — all branches
# ---------------------------------------------------------------------------


class TestExtractToolName:
    """Parametrized test covering every branch of _extract_tool_name."""

    @pytest.mark.parametrize(
        ("telemetry", "expected"),
        [
            pytest.param(None, "", id="telemetry-none"),
            pytest.param(ToolTelemetry(properties={}), "", id="properties-empty"),
            pytest.param(
                ToolTelemetry(properties={"outcome": "done"}), "", id="key-absent"
            ),
            pytest.param(
                ToolTelemetry(properties={"tool_name": "read_file"}),
                "read_file",
                id="key-present",
            ),
        ],
    )
    def test_extract_tool_name(
        self, telemetry: ToolTelemetry | None, expected: str
    ) -> None:
        data = ToolExecutionData(
            toolCallId="x", model="m", interactionId="i", toolTelemetry=telemetry
        )
        assert _extract_tool_name(data) == expected


# ---------------------------------------------------------------------------
# _render_code_changes — all branches
# ---------------------------------------------------------------------------


class TestRenderCodeChanges:
    """Tests for _render_code_changes covering None, all-zero, and with-data."""

    def test_none_produces_no_output(self) -> None:
        """code_changes=None → returns immediately without printing."""
        console = Console(file=io.StringIO(), force_terminal=True)
        _render_code_changes(None, target_console=console)
        assert console.file.getvalue() == ""  # type: ignore[union-attr]

    def test_all_zero_produces_no_output(self) -> None:
        """All fields zero/empty → returns without printing."""
        console = Console(file=io.StringIO(), force_terminal=True)
        changes = CodeChanges(linesAdded=0, linesRemoved=0, filesModified=[])
        _render_code_changes(changes, target_console=console)
        assert console.file.getvalue() == ""  # type: ignore[union-attr]

    def test_with_data_shows_table(self) -> None:
        """Non-zero code changes → renders a table with stats."""
        console = Console(file=io.StringIO(), force_terminal=True)
        changes = CodeChanges(linesAdded=10, linesRemoved=2, filesModified=["a.py"])
        _render_code_changes(changes, target_console=console)
        output = console.file.getvalue()  # type: ignore[union-attr]
        assert "Files modified" in output
        assert "+10" in output
        assert "-2" in output
