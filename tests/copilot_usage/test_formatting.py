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
