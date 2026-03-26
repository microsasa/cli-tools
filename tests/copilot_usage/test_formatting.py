"""Tests for copilot_usage._formatting — shared formatting utilities."""

# pyright: reportPrivateUsage=false

import subprocess
import sys

import pytest


class TestFormattingModuleImport:
    """Regression tests for issue #399 — no circular imports."""

    def test_import_formatting_directly(self) -> None:
        """Importing _formatting at module scope must not raise."""
        from copilot_usage._formatting import format_duration, format_tokens

        assert callable(format_tokens)
        assert callable(format_duration)

    def test_import_both_report_and_render_detail(self) -> None:
        """Importing both report and render_detail in either order must succeed."""
        import copilot_usage.render_detail
        import copilot_usage.report

        assert callable(copilot_usage.report.render_session_detail)
        assert callable(copilot_usage.render_detail.render_session_detail)

    def test_import_render_detail_then_report(self) -> None:
        """Importing render_detail before report must not raise."""
        from copilot_usage.render_detail import render_session_detail
        from copilot_usage.report import format_tokens

        assert callable(render_session_detail)
        assert callable(format_tokens)

    def test_import_formatting_in_subprocess(self) -> None:
        """Importing _formatting in a fresh interpreter confirms no cycle."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-c",
                    "from copilot_usage._formatting import format_tokens, format_duration; "
                    "assert callable(format_tokens); assert callable(format_duration)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "Subprocess import of 'copilot_usage._formatting' timed out; "
                "possible circular import regression."
            )
        assert result.returncode == 0, f"Importing _formatting failed:\n{result.stderr}"

    def test_import_both_modules_in_subprocess(self) -> None:
        """Importing render_detail and report in a fresh interpreter must succeed."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-c",
                    "import copilot_usage.render_detail; "
                    "import copilot_usage.report; "
                    "assert callable(copilot_usage.report.format_tokens); "
                    "assert callable(copilot_usage.render_detail.render_session_detail)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "Subprocess import timed out; possible circular import regression."
            )
        assert result.returncode == 0, (
            f"Importing both modules failed:\n{result.stderr}"
        )


class TestMaxContentLenSingleDefinition:
    """_MAX_CONTENT_LEN must come from a single source."""

    def test_max_content_len_consistent(self) -> None:
        """Both report.py and render_detail.py use the same _MAX_CONTENT_LEN value."""
        from copilot_usage._formatting import _MAX_CONTENT_LEN as formatting_val
        from copilot_usage.render_detail import _MAX_CONTENT_LEN as detail_val
        from copilot_usage.report import _MAX_CONTENT_LEN as report_val

        assert formatting_val == report_val == detail_val == 80

    def test_max_content_len_is_same_object(self) -> None:
        """All modules reference the exact same constant from _formatting."""
        from copilot_usage._formatting import _MAX_CONTENT_LEN as formatting_val
        from copilot_usage.render_detail import _MAX_CONTENT_LEN as detail_val
        from copilot_usage.report import _MAX_CONTENT_LEN as report_val

        assert report_val is formatting_val
        assert detail_val is formatting_val
