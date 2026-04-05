"""Verify wheel packaging and public API surface."""

import importlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

# Modules that declare __all__ and the expected public names.
_PUBLIC_MODULES: list[str] = [
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


def test_ccreq_re_not_in_vscode_parser_all() -> None:
    """``CCREQ_RE`` must not appear in ``vscode_parser.__all__``.

    Regression guard for issue #725: the compiled regex is an
    implementation detail and should not be part of the public API.
    """
    import copilot_usage.vscode_parser as vscode_mod

    dunder_all = vscode_mod.__all__
    assert "CCREQ_RE" not in dunder_all, "CCREQ_RE must not be in vscode_parser.__all__"
