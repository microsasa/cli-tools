"""Verify wheel packaging excludes developer-only docs."""

import shutil
import subprocess
import zipfile
from pathlib import Path


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
