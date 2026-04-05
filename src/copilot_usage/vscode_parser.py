"""Parser for VS Code Copilot Chat log files."""

import os
import re
import sys
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Final

from loguru import logger

from copilot_usage._fs_utils import (
    _safe_file_identity,  # pyright: ignore[reportPrivateUsage]
)

__all__: Final[list[str]] = [
    "VSCodeLogSummary",
    "VSCodeRequest",
    "build_vscode_summary",
    "discover_vscode_logs",
    "get_vscode_summary",
    "parse_vscode_log",
]

_CCREQ_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) \[info\] "
    r"ccreq:(\w+)\.copilotmd \| success \| "
    r"(\S+?)(?:\s*->\s*\S+)? \| "
    r"(\d+)ms \| "
    r"\[([^\]]+)\]"
)


@dataclass(frozen=True, slots=True)
class VSCodeRequest:
    """A single parsed VS Code Copilot Chat request."""

    timestamp: datetime
    request_id: str
    model: str
    duration_ms: int
    category: str


@dataclass(frozen=True, slots=True)
class VSCodeLogSummary:
    """Aggregated stats from VS Code Copilot Chat logs.

    ``first_timestamp`` and ``last_timestamp`` are derived from a per-request
    min/max scan, so input order does not matter.
    """

    total_requests: int = 0
    total_duration_ms: int = 0
    requests_by_model: dict[str, int] = field(default_factory=lambda: {})
    duration_by_model: dict[str, int] = field(default_factory=lambda: {})
    requests_by_category: dict[str, int] = field(default_factory=lambda: {})
    requests_by_date: dict[str, int] = field(default_factory=lambda: {})
    # Earliest timestamp seen across all requests.
    first_timestamp: datetime | None = None
    # Latest timestamp seen across all requests.
    last_timestamp: datetime | None = None
    log_files_parsed: int = 0
    log_files_found: int = 0


def _default_log_candidates() -> list[Path]:
    """Return candidate VS Code log directories for both Stable and Insiders."""
    code_dirs: list[str] = ["Code", "Code - Insiders"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return [root / d / "logs" for d in code_dirs]
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
        return [root / d / "logs" for d in code_dirs]
    # Linux and other platforms
    root = Path.home() / ".config"
    return [root / d / "logs" for d in code_dirs]


def discover_vscode_logs(base_path: Path | None = None) -> list[Path]:
    """Find all VS Code Copilot Chat log files.

    When *base_path* is ``None``, both the **stable** and **Insiders** log
    directories are searched:

    * On Windows: ``%APPDATA%/Code/logs`` and ``%APPDATA%/Code - Insiders/logs``
      (falls back to ``~/AppData/Roaming/…`` when ``%APPDATA%`` is unset).
    * On macOS: ``~/Library/Application Support/Code/logs`` and
      ``~/Library/Application Support/Code - Insiders/logs``.
    * On other platforms (e.g. Linux): ``~/.config/Code/logs`` and
      ``~/.config/Code - Insiders/logs``.

    If only one variant exists on disk the behaviour is identical to before.
    When both exist, files from both are returned and sorted together.
    """
    pattern = "*/window*/exthost/GitHub.copilot-chat/GitHub Copilot Chat.log"

    if base_path is not None:
        if not base_path.is_dir():
            logger.debug("VS Code logs directory not found: {}", base_path)
            return []
        logs = sorted(base_path.glob(pattern))
        logger.debug("Discovered {} VS Code log file(s) under {}", len(logs), base_path)
        return logs

    candidates = _default_log_candidates()
    all_logs: list[Path] = []
    for candidate in candidates:
        if not candidate.is_dir():
            logger.debug("VS Code logs directory not found: {}", candidate)
            continue
        all_logs.extend(candidate.glob(pattern))
    all_logs.sort()
    logger.debug(
        "Discovered {} VS Code log file(s) across {} candidate(s)",
        len(all_logs),
        len(candidates),
    )
    return all_logs


def parse_vscode_log(log_path: Path) -> list[VSCodeRequest]:
    """Parse a single VS Code Copilot Chat log file into request objects.

    Returns a list of parsed requests (possibly empty when no lines match).
    Unlike incremental parsing via :func:`_parse_vscode_log_from_offset`,
    this performs a complete one-shot read and includes the final line even
    when it is not newline-terminated.

    Raises:
        OSError: If the file cannot be opened or read.
    """
    requests, _ = _parse_vscode_log_from_offset(log_path, 0, include_partial_tail=True)
    return requests


def _parse_vscode_log_from_offset(
    log_path: Path,
    offset: int,
    *,
    include_partial_tail: bool = False,
) -> tuple[list[VSCodeRequest], int]:
    """Parse VS Code Copilot Chat log starting at *offset* bytes.

    Returns ``(requests, end_offset)`` where *end_offset* is the byte
    position at the end of the last **complete** (newline-terminated)
    line read.  Partial lines at EOF are intentionally excluded so that
    the next incremental call can re-read them once the writer finishes
    the line.

    When *include_partial_tail* is ``True`` (used by :func:`parse_vscode_log`
    for one-shot full parsing), a final non-newline-terminated line is
    **included** in the results to preserve text-mode semantics where
    every line — including the last — is processed.

    Raises:
        OSError: If the file cannot be opened or read.
    """
    requests: list[VSCodeRequest] = []
    safe_end: int = offset
    with log_path.open("rb") as fb:
        if offset > 0:
            fb.seek(offset)
        for raw_line in fb:
            is_complete = raw_line.endswith(b"\n")
            if not is_complete and not include_partial_tail:
                # Partial line at EOF — stop advancing so the next
                # incremental call re-reads this line once complete.
                break
            safe_end += len(raw_line)
            # Fast pre-filter: only ~1–5% of lines contain "ccreq:"
            if b"ccreq:" not in raw_line:
                continue
            # Decode with replacement to mirror parse_vscode_log behaviour.
            line = raw_line.decode("utf-8", errors="replace")
            m = _CCREQ_RE.match(line)
            if m is None:
                continue
            ts_str, req_id, model, duration_str, category = m.groups()
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            requests.append(
                VSCodeRequest(
                    timestamp=ts,
                    request_id=req_id,
                    model=model,
                    duration_ms=int(duration_str),
                    category=category,
                )
            )
    logger.debug(
        "Parsed {} request(s) from {} (offset {}→{})",
        len(requests),
        log_path,
        offset,
        safe_end,
    )
    return requests, safe_end


# ---------------------------------------------------------------------------
# Module-level parsed-requests cache (mirrors parser._EVENTS_CACHE).
# Uses OrderedDict for LRU eviction: most-recently-used entries are at
# the back, least-recently-used at the front.
#
# Cache value layout: (file_id, end_offset, requests_tuple)
#   file_id    – (st_mtime_ns, st_size) or None when stat() fails
#   end_offset – byte position of the last parsed line
#   requests   – immutable tuple of parsed VSCodeRequest objects
# ---------------------------------------------------------------------------

_MAX_CACHED_VSCODE_LOGS: Final[int] = 64
_VSCODE_LOG_CACHE: OrderedDict[
    Path, tuple[tuple[int, int] | None, int, tuple[VSCodeRequest, ...]]
] = OrderedDict()


def _update_vscode_cache(
    log_path: Path,
    file_id: tuple[int, int] | None,
    end_offset: int,
    requests: tuple[VSCodeRequest, ...],
) -> None:
    """Insert or replace a cache entry with LRU eviction."""
    if log_path in _VSCODE_LOG_CACHE:
        del _VSCODE_LOG_CACHE[log_path]
    elif len(_VSCODE_LOG_CACHE) >= _MAX_CACHED_VSCODE_LOGS:
        _VSCODE_LOG_CACHE.popitem(last=False)  # evict LRU (front)
    _VSCODE_LOG_CACHE[log_path] = (file_id, end_offset, requests)


def _get_cached_vscode_requests(log_path: Path) -> tuple[VSCodeRequest, ...]:
    """Return parsed requests, incrementally parsing only new content.

    On the first call for a given *log_path*, delegates to
    :func:`_parse_vscode_log_from_offset` (offset 0) and stores the
    result together with the byte offset reached.  Subsequent calls
    detect whether the file has **grown** (append-only) by comparing
    the new ``st_size`` against the cached size — if so, only the
    bytes after the stored offset are parsed and appended to the
    existing result.

    When the file is **replaced** (new size < cached size) or
    ``st_size`` cannot be determined, a full re-parse is performed.

    The cache is bounded to :data:`_MAX_CACHED_VSCODE_LOGS` entries;
    the **least-recently used** entry is evicted when the limit is
    reached.

    Raises:
        OSError: Propagated from :func:`_parse_vscode_log_from_offset`
            when the file cannot be opened or read.
    """
    new_id = _safe_file_identity(log_path)
    cached = _VSCODE_LOG_CACHE.get(log_path)

    if cached is not None:
        old_id, end_offset, old_requests = cached

        # Exact match: file unchanged — return cached result.
        if old_id == new_id:
            _VSCODE_LOG_CACHE.move_to_end(log_path)
            return old_requests

        # Incremental path: file grew (append-only) beyond the cached
        # resume point. Compare against ``end_offset`` because that is the
        # position we will seek to when resuming parsing.
        if new_id is not None and old_id is not None and new_id[1] >= end_offset:
            new_reqs, new_end = _parse_vscode_log_from_offset(log_path, end_offset)
            combined = old_requests + tuple(new_reqs)
            _update_vscode_cache(log_path, new_id, new_end, combined)
            return combined

    # Full parse: first call or file was truncated/replaced.
    requests, end_offset = _parse_vscode_log_from_offset(log_path, 0)
    result = tuple(requests)
    _update_vscode_cache(log_path, new_id, end_offset, result)
    return result


@dataclass(slots=True)
class _SummaryAccumulator:
    """Mutable accumulator for incremental VSCodeLogSummary construction."""

    total_requests: int = 0
    total_duration_ms: int = 0
    requests_by_model: defaultdict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    duration_by_model: defaultdict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    requests_by_category: defaultdict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    requests_by_date: defaultdict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    log_files_parsed: int = 0
    log_files_found: int = 0


def _update_vscode_summary(
    acc: _SummaryAccumulator, requests: Sequence[VSCodeRequest]
) -> None:
    """Merge *requests* into *acc* in-place, then discard."""
    last_date_key: str = ""
    last_date_val: date | None = None

    for req in requests:
        acc.total_requests += 1
        acc.total_duration_ms += req.duration_ms

        acc.requests_by_model[req.model] += 1
        acc.duration_by_model[req.model] += req.duration_ms
        acc.requests_by_category[req.category] += 1

        ts_date = req.timestamp.date()
        if last_date_val is None or ts_date != last_date_val:
            last_date_key = req.timestamp.strftime("%Y-%m-%d")
            last_date_val = ts_date
        acc.requests_by_date[last_date_key] += 1

        # Timestamp bounds: full min/max scan so callers (especially
        # build_vscode_summary) need not pre-sort their input.
        if acc.first_timestamp is None or req.timestamp < acc.first_timestamp:
            acc.first_timestamp = req.timestamp
        if acc.last_timestamp is None or req.timestamp > acc.last_timestamp:
            acc.last_timestamp = req.timestamp


def _finalize_summary(acc: _SummaryAccumulator) -> VSCodeLogSummary:
    """Convert a mutable accumulator into a frozen ``VSCodeLogSummary``."""
    return VSCodeLogSummary(
        total_requests=acc.total_requests,
        total_duration_ms=acc.total_duration_ms,
        requests_by_model=dict(acc.requests_by_model),
        duration_by_model=dict(acc.duration_by_model),
        requests_by_category=dict(acc.requests_by_category),
        requests_by_date=dict(acc.requests_by_date),
        first_timestamp=acc.first_timestamp,
        last_timestamp=acc.last_timestamp,
        log_files_parsed=acc.log_files_parsed,
        log_files_found=acc.log_files_found,
    )


def build_vscode_summary(
    requests: list[VSCodeRequest],
    *,
    log_files_parsed: int = 0,
    log_files_found: int = 0,
) -> VSCodeLogSummary:
    """Aggregate a list of parsed requests into a summary.

    *requests* may be in any order.  Timestamp bounds are derived from a
    per-request min/max scan so unsorted inputs are handled correctly.
    """
    acc = _SummaryAccumulator(
        log_files_parsed=log_files_parsed,
        log_files_found=log_files_found,
    )
    _update_vscode_summary(acc, requests)
    return _finalize_summary(acc)


def get_vscode_summary(base_path: Path | None = None) -> VSCodeLogSummary:
    """Discover, parse, and aggregate all VS Code Copilot Chat logs.

    Uses :func:`_get_cached_vscode_requests` so that unchanged log files
    are not re-parsed on repeated invocations.
    """
    logs = discover_vscode_logs(base_path)
    acc = _SummaryAccumulator(log_files_found=len(logs))
    for log_path in logs:
        try:
            result = _get_cached_vscode_requests(log_path)
        except OSError as exc:
            logger.warning("Could not read log file {}: {}", log_path, exc)
            continue
        _update_vscode_summary(acc, result)
        acc.log_files_parsed += 1
    return _finalize_summary(acc)
