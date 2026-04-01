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
