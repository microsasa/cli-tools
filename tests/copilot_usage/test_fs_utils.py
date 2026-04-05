"""Tests for copilot_usage._fs_utils — shared filesystem helpers."""

import os
from collections import OrderedDict
from pathlib import Path

import pytest

from copilot_usage._fs_utils import lru_insert, safe_file_identity


class TestSafeFileIdentity:
    """Covers normal file, missing path, OSError, and between-call changes."""

    def test_returns_mtime_ns_and_size_for_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("content")
        result = safe_file_identity(f)
        assert result is not None
        mtime_ns, size = result
        assert mtime_ns > 0
        assert size == len(b"content")

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert safe_file_identity(tmp_path / "ghost.jsonl") is None

    def test_returns_none_for_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("")

        def _raise_perm(self: Path, **kwargs: object) -> object:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "stat", _raise_perm)
        assert safe_file_identity(f) is None

    def test_returns_none_for_generic_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "events.jsonl"
        f.write_text("")

        def _raise_os(self: Path, **kwargs: object) -> object:
            raise OSError("I/O error")

        monkeypatch.setattr(Path, "stat", _raise_os)
        assert safe_file_identity(f) is None

    def test_identity_changes_when_file_is_modified(self, tmp_path: Path) -> None:
        f = tmp_path / "data.txt"
        f.write_text("v1")

        id_before = safe_file_identity(f)
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

        id_after = safe_file_identity(f)
        assert id_after is not None
        assert id_after[0] != original_mtime_ns


class TestLruInsert:
    """Covers insert into empty cache, eviction when full, and stale replacement."""

    def test_insert_into_empty_cache(self) -> None:
        cache: OrderedDict[str, int] = OrderedDict()
        lru_insert(cache, "a", 1, max_size=3)
        assert dict(cache) == {"a": 1}

    def test_eviction_when_full(self) -> None:
        cache: OrderedDict[str, int] = OrderedDict()
        lru_insert(cache, "a", 1, max_size=2)
        lru_insert(cache, "b", 2, max_size=2)
        lru_insert(cache, "c", 3, max_size=2)
        # "a" (LRU) should have been evicted
        assert "a" not in cache
        assert list(cache.keys()) == ["b", "c"]

    def test_replacement_of_stale_entry(self) -> None:
        cache: OrderedDict[str, int] = OrderedDict()
        lru_insert(cache, "a", 1, max_size=2)
        lru_insert(cache, "b", 2, max_size=2)
        # Replace "a" with updated value — should NOT evict "b"
        lru_insert(cache, "a", 10, max_size=2)
        assert cache["a"] == 10
        assert "b" in cache
        # "a" should now be at the end (most recently used)
        assert list(cache.keys()) == ["b", "a"]

    def test_max_size_one(self) -> None:
        cache: OrderedDict[str, int] = OrderedDict()
        lru_insert(cache, "x", 1, max_size=1)
        lru_insert(cache, "y", 2, max_size=1)
        assert dict(cache) == {"y": 2}

    @pytest.mark.parametrize("bad_size", [0, -1, -100])
    def test_raises_on_invalid_max_size(self, bad_size: int) -> None:
        cache: OrderedDict[str, int] = OrderedDict()
        with pytest.raises(ValueError, match="max_size must be >= 1"):
            lru_insert(cache, "k", 1, max_size=bad_size)
