"""Tests for copilot_usage._formatting — shared formatting utilities."""

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


class TestFormattingModuleImport:
    """Regression tests for issue #399 — no circular imports."""

    def test_import_formatting_directly(self) -> None:
        """Importing _formatting at module scope must not raise."""
        from copilot_usage._formatting import format_duration, format_tokens

        assert callable(format_tokens)
        assert callable(format_duration)

    def test_import_render_detail_then_report_modules(self) -> None:
        """Importing render_detail before report at module scope must succeed."""
        import copilot_usage.render_detail
        import copilot_usage.report

        assert callable(copilot_usage.report.render_session_detail)
        assert callable(copilot_usage.render_detail.render_session_detail)

    def test_import_report_then_render_detail_in_subprocess(self) -> None:
        """Importing report before render_detail in a fresh interpreter must succeed."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-c",
                    "import copilot_usage.report; "
                    "import copilot_usage.render_detail; "
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
            f"Importing report then render_detail failed:\n{result.stderr}"
        )

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

    def test_import_render_detail_then_report_in_subprocess(self) -> None:
        """Importing render_detail then report in a fresh interpreter must succeed."""
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
    """MAX_CONTENT_LEN must come from a single source."""

    def test_max_content_len_consistent(self) -> None:
        """_formatting.py and render_detail.py use the same MAX_CONTENT_LEN value."""
        from copilot_usage._formatting import MAX_CONTENT_LEN as formatting_val
        from copilot_usage.render_detail import MAX_CONTENT_LEN as detail_val

        assert formatting_val == detail_val

    def test_max_content_len_not_redefined(self) -> None:
        """report.py and render_detail.py must not locally assign MAX_CONTENT_LEN."""
        for module_name in ("copilot_usage.report", "copilot_usage.render_detail"):
            spec = importlib.util.find_spec(module_name)
            assert spec is not None and spec.origin is not None
            source = Path(spec.origin).read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id == "MAX_CONTENT_LEN"
                        ):
                            pytest.fail(
                                f"{module_name} redefines MAX_CONTENT_LEN "
                                f"at line {node.lineno}; "
                                "it must be imported from _formatting"
                            )
                elif (
                    (
                        isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "MAX_CONTENT_LEN"
                    )
                    or (
                        isinstance(node, ast.AugAssign)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "MAX_CONTENT_LEN"
                    )
                    or (
                        isinstance(node, ast.NamedExpr)
                        and node.target.id == "MAX_CONTENT_LEN"
                    )
                ):
                    pytest.fail(
                        f"{module_name} redefines MAX_CONTENT_LEN "
                        f"at line {node.lineno}; "
                        "it must be imported from _formatting"
                    )


class TestFormatDuration:
    """Tests for format_duration — millisecond to human-readable conversion."""

    def test_sub_second_shows_milliseconds(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(481) == "481ms"

    def test_zero_shows_zero_ms(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(0) == "0ms"

    def test_one_ms(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(1) == "1ms"

    def test_999_ms(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(999) == "999ms"

    def test_exactly_one_second(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(1000) == "1s"

    def test_seconds_truncates_ms(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(1500) == "1s 500ms"

    def test_minutes_and_seconds(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(389114) == "6m 29s 114ms"

    def test_hours_minutes_seconds(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(3661000) == "1h 1m 1s"

    def test_exact_minute(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(60000) == "1m"

    def test_negative_clamped_to_zero(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(-500) == "0ms"

    def test_negative_one_clamped_to_zero(self) -> None:
        from copilot_usage._formatting import format_duration

        assert format_duration(-1) == "0ms"


class TestFormatTimedelta:
    """Tests for format_timedelta — timedelta to human-readable conversion."""

    def test_sub_second_timedelta(self) -> None:
        from datetime import timedelta

        from copilot_usage._formatting import format_timedelta

        assert format_timedelta(timedelta(milliseconds=481)) == "481ms"

    def test_zero_timedelta(self) -> None:
        from datetime import timedelta

        from copilot_usage._formatting import format_timedelta

        assert format_timedelta(timedelta(0)) == "0ms"

    def test_seconds_timedelta(self) -> None:
        from datetime import timedelta

        from copilot_usage._formatting import format_timedelta

        assert format_timedelta(timedelta(seconds=5)) == "5s"

    def test_mixed_timedelta(self) -> None:
        from datetime import timedelta

        from copilot_usage._formatting import format_timedelta

        assert (
            format_timedelta(timedelta(hours=1, minutes=5, seconds=30)) == "1h 5m 30s"
        )


# ---------------------------------------------------------------------------
# format_tokens boundary values (issue #686)
# ---------------------------------------------------------------------------


class TestFormatTokensThresholds:
    """Boundary values at the K and M thresholds — off-by-one regression guard."""

    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (999, "999"),  # one below K threshold
            (1_000, "1.0K"),  # exact K threshold
            (999_999, "1000.0K"),  # one below M threshold
            (1_000_000, "1.0M"),  # exact M threshold
        ],
    )
    def test_format_tokens_thresholds(self, n: int, expected: str) -> None:
        from copilot_usage._formatting import format_tokens

        assert format_tokens(n) == expected
