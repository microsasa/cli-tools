"""Tests for copilot_usage._fs_utils — shared filesystem helpers."""

# pyright: reportPrivateUsage=false

import os
from pathlib import Path

import pytest

from copilot_usage._fs_utils import _safe_file_identity


class TestSafeFileIdentity:
    """Covers normal file, missing path, OSError, and between-call changes."""

    def test_returns_mtime_ns_and_size_for_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("content")
        result = _safe_file_identity(f)
        assert result is not None
        mtime_ns, size = result
        assert mtime_ns > 0
        assert size == len(b"content")

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _safe_file_identity(tmp_path / "ghost.jsonl") is None

    def test_returns_none_for_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("")

        def _raise_perm(self: Path, **kwargs: object) -> object:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "stat", _raise_perm)
        assert _safe_file_identity(f) is None

    def test_returns_none_for_generic_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("")

        def _raise_os(self: Path, **kwargs: object) -> object:
            raise OSError("I/O error")

        monkeypatch.setattr(Path, "stat", _raise_os)
        assert _safe_file_identity(f) is None

    def test_identity_changes_when_file_is_modified(self, tmp_path: Path) -> None:
        f = tmp_path / "data.txt"
        f.write_text("v1")

        id_before = _safe_file_identity(f)
        assert id_before is not None

        # Keep file size constant; force an mtime_ns change via os.utime.
        # Bump by ≥1s and loop until stat() confirms the change, because some
        # filesystems truncate mtimes to 1-second (or coarser) resolution.
        original_mtime_ns = id_before[0]
        for delta_ns in (1_000_000_000, 2_000_000_000, 5_000_000_000):
            new_mtime_ns = original_mtime_ns + delta_ns
            os.utime(f, ns=(new_mtime_ns, new_mtime_ns))
            if f.stat().st_mtime_ns != original_mtime_ns:
                break

        id_after = _safe_file_identity(f)
        assert id_after is not None
        assert id_after[0] != original_mtime_ns
