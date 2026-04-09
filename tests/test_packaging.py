"""Verify wheel packaging and public API surface."""

import importlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

# Modules that declare __all__ and the expected public names.
_PUBLIC_MODULES: list[str] = [
    "copilot_usage._formatting",
    "copilot_usage._fs_utils",
    "copilot_usage.logging_config",
    "copilot_usage.models",
    "copilot_usage.parser",
    "copilot_usage.pricing",
    "copilot_usage.render_detail",
    "copilot_usage.report",
    "copilot_usage.vscode_parser",
    "copilot_usage.vscode_report",
]


@pytest.mark.parametrize("module_name", _PUBLIC_MODULES)
def test_all_names_importable(module_name: str) -> None:
    """Every name listed in a module's ``__all__`` must be importable at runtime."""
    mod = importlib.import_module(module_name)
    dunder_all: list[str] | None = getattr(mod, "__all__", None)  # noqa: B009
    assert dunder_all is not None, f"{module_name} is missing __all__"
    for name in dunder_all:
        assert hasattr(mod, name), (  # noqa: B009
            f"{module_name}.__all__ lists {name!r}, but it is not defined in the module"
        )


def test_wheel_excludes_docs(tmp_path: Path) -> None:
    """copilot_usage/docs/ must not be shipped in the wheel."""
    uv_executable = shutil.which("uv")
    assert uv_executable is not None, "'uv' executable not found in PATH"
    result = subprocess.run(  # noqa: S603
        [uv_executable, "build", "--wheel", "--out-dir", str(tmp_path)],
        capture_output=True,
        cwd=Path(__file__).parents[0].parent,
    )
    assert result.returncode == 0, result.stderr.decode()
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    docs = [n for n in names if "copilot_usage/docs/" in n]
    assert not docs, f"docs/ should not be in wheel, but found: {docs}"


def test_parse_data_and_event_data_removed_from_public_api() -> None:
    """``parse_data`` and ``EventData`` must not appear in the public API.

    Regression guard for issue #670: these were production dead-code that
    contradicted architecture docs.  The canonical dispatch API is the
    narrowly-typed ``as_*()`` accessors on ``SessionEvent``.
    """
    import copilot_usage.models as models_mod

    dunder_all = models_mod.__all__
    assert "EventData" not in dunder_all, "EventData must not be in models.__all__"
    assert not hasattr(models_mod, "EventData"), (  # noqa: B009
        "EventData type alias must not exist in models module"
    )
    assert not hasattr(models_mod.SessionEvent, "parse_data"), (  # noqa: B009
        "SessionEvent.parse_data() must not exist — use as_*() accessors"
    )


def test_format_helpers_not_in_report_all() -> None:
    """``format_duration`` and ``format_tokens`` must not appear in ``report.__all__``.

    Regression guard for issue #776: these formatting helpers belong to
    ``_formatting`` and should not be re-exported from ``report``.
    """
    import copilot_usage.report as report_mod

    dunder_all = report_mod.__all__
    assert "format_duration" not in dunder_all, (
        "format_duration must not be in report.__all__"
    )
    assert "format_tokens" not in dunder_all, (
        "format_tokens must not be in report.__all__"
    )


def test_ccreq_re_not_in_vscode_parser_all() -> None:
    """``CCREQ_RE`` must not appear in ``vscode_parser.__all__``.

    Regression guard for issue #725: the compiled regex is an
    implementation detail and should not be part of the public API.
    """
    import copilot_usage.vscode_parser as vscode_mod

    dunder_all = vscode_mod.__all__
    assert "CCREQ_RE" not in dunder_all, "CCREQ_RE must not be in vscode_parser.__all__"


def test_cli_does_not_import_vscode_modules_at_module_level() -> None:
    """``vscode_parser`` and ``vscode_report`` must be lazy-imported.

    Regression guard for issue #890: these modules are only needed by the
    ``vscode`` subcommand and should not be imported at ``cli`` module level
    to avoid loading ``re``, ``stat``, ``types``, and running ``re.compile``
    on every invocation.
    """
    import sys

    original_sys_modules = sys.modules.copy()
    try:
        # Purge any previously imported copilot_usage modules so we get a
        # clean import of cli.
        for mod_name in list(sys.modules):
            if mod_name == "copilot_usage" or mod_name.startswith("copilot_usage."):
                del sys.modules[mod_name]

        importlib.import_module("copilot_usage.cli")

        assert "copilot_usage.vscode_parser" not in sys.modules, (
            "vscode_parser must not be imported at cli module level"
        )
        assert "copilot_usage.vscode_report" not in sys.modules, (
            "vscode_report must not be imported at cli module level"
        )
    finally:
        sys.modules.clear()
        sys.modules.update(original_sys_modules)
