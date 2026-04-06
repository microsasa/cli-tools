"""Parser for VS Code Copilot Chat log files."""

import os
import re
import sys
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Final, Literal

from loguru import logger

from copilot_usage._fs_utils import lru_insert, safe_file_identity

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

    Raises:
        OSError: If the file cannot be opened or read.
    """
    requests: list[VSCodeRequest] = []
    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            # Fast pre-filter: only ~1–5% of lines contain "ccreq:"
            if "ccreq:" not in line:
                continue
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
    logger.debug("Parsed {} request(s) from {}", len(requests), log_path)
    return requests


# ---------------------------------------------------------------------------
# Module-level parsed-requests cache (mirrors parser._EVENTS_CACHE).
# Uses OrderedDict for LRU eviction: most-recently-used entries are at
# the back, least-recently-used at the front.
# ---------------------------------------------------------------------------

_MAX_CACHED_VSCODE_LOGS: Final[int] = 64


@dataclass(frozen=True, slots=True)
class _CachedVSCodeLog:
    """Cache entry pairing a file identity with parsed VS Code requests."""

    file_id: tuple[int, int] | None
    requests: tuple[VSCodeRequest, ...]


_VSCODE_LOG_CACHE: OrderedDict[Path, _CachedVSCodeLog] = OrderedDict()


# ---------------------------------------------------------------------------
# Module-level summary cache: avoids O(total_requests) re-aggregation when
# no log file has changed.  Keyed by a frozenset of (path, file_id) tuples
# representing all discovered log files.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CachedVSCodeSummary:
    """Cache entry pairing a snapshot of file identities with the summary."""

    file_ids: frozenset[tuple[Path, tuple[int, int] | None]]
    summary: VSCodeLogSummary


_vscode_summary_cache: _CachedVSCodeSummary | None = None


# ---------------------------------------------------------------------------
# Per-file partial-summary cache: avoids O(total_requests) re-aggregation
# for unchanged files when the global summary cache is invalidated.
# Keyed by log file Path; each entry pairs a file identity with an
# already-aggregated VSCodeLogSummary for that single file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CachedFileSummary:
    """Cache entry pairing a file identity with an aggregated per-file summary."""

    file_id: tuple[int, int] | None
    partial: VSCodeLogSummary


class _BoundedFileSummaryCache(OrderedDict[Path, _CachedFileSummary]):
    """LRU-bounded cache for per-file summaries.

    This keeps the existing mapping-style API used by callers while ensuring
    entries for old log paths do not accumulate without bound.
    """

    def __init__(self, max_size: int) -> None:
        super().__init__()
        self._max_size = max_size

    def __setitem__(self, key: Path, value: _CachedFileSummary) -> None:
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        while len(self) > self._max_size:
            self.popitem(last=False)


_PER_FILE_SUMMARY_CACHE: OrderedDict[Path, _CachedFileSummary] = (
    _BoundedFileSummaryCache(_MAX_CACHED_VSCODE_LOGS)
)


_FILE_ID_UNSET: Final = "unset"


def _get_cached_vscode_requests(
    log_path: Path,
    file_id: tuple[int, int] | None | Literal["unset"] = _FILE_ID_UNSET,
) -> tuple[VSCodeRequest, ...]:
    """Return parsed requests, re-parsing only when ``(mtime_ns, size)`` changes.

    When *file_id* is omitted (or the sentinel ``"unset"``), the file
    identity is computed internally via :func:`safe_file_identity`.
    Callers that have already stat'd the file (e.g.
    :func:`get_vscode_summary`) can pass the pre-computed identity to
    avoid a redundant ``stat()`` call.

    On the first call for a given *log_path*, delegates to
    :func:`parse_vscode_log` and stores the result.  Subsequent calls
    return the cached tuple as long as the file identity is unchanged.
    The cache is bounded to :data:`_MAX_CACHED_VSCODE_LOGS` entries;
    the **least-recently used** entry is evicted when the limit is
    reached.

    The parsed list is converted to a ``tuple`` before storage so that
    callers cannot accidentally append, pop, or reorder entries in the
    cache — matching the container-level immutability pattern used by
    :func:`copilot_usage.parser.get_cached_events`.

    Raises:
        OSError: Propagated from :func:`parse_vscode_log` when the file
            cannot be opened or read.
    """
    resolved_id: tuple[int, int] | None = (
        safe_file_identity(log_path) if file_id == _FILE_ID_UNSET else file_id
    )
    cached = _VSCODE_LOG_CACHE.get(log_path)
    if cached is not None and cached.file_id == resolved_id:
        _VSCODE_LOG_CACHE.move_to_end(log_path)
        return cached.requests
    requests = tuple(parse_vscode_log(log_path))
    lru_insert(
        _VSCODE_LOG_CACHE,
        log_path,
        _CachedVSCodeLog(file_id=resolved_id, requests=requests),
        _MAX_CACHED_VSCODE_LOGS,
    )
    return requests


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


def _merge_partial(acc: _SummaryAccumulator, partial: VSCodeLogSummary) -> None:
    """Merge a pre-aggregated per-file summary into *acc* in-place.

    Unlike :func:`_update_vscode_summary`, which iterates every individual
    request, this function merges already-aggregated counters and dict
    entries in O(num_models + num_categories + num_dates) time —
    proportional to the number of distinct models, categories, and dates
    rather than the total request count.
    """
    acc.total_requests += partial.total_requests
    acc.total_duration_ms += partial.total_duration_ms

    for model, count in partial.requests_by_model.items():
        acc.requests_by_model[model] += count
    for model, dur in partial.duration_by_model.items():
        acc.duration_by_model[model] += dur
    for cat, count in partial.requests_by_category.items():
        acc.requests_by_category[cat] += count
    for date_key, count in partial.requests_by_date.items():
        acc.requests_by_date[date_key] += count

    if partial.first_timestamp is not None and (
        acc.first_timestamp is None or partial.first_timestamp < acc.first_timestamp
    ):
        acc.first_timestamp = partial.first_timestamp
    if partial.last_timestamp is not None and (
        acc.last_timestamp is None or partial.last_timestamp > acc.last_timestamp
    ):
        acc.last_timestamp = partial.last_timestamp


def _copy_summary(summary: VSCodeLogSummary) -> VSCodeLogSummary:
    """Return a shallow copy with independent dict fields.

    ``VSCodeLogSummary`` is a frozen dataclass, so scalar and datetime
    fields are already immutable.  The four ``dict[str, int]`` fields,
    however, are mutable containers — callers receiving a cached instance
    could inadvertently mutate the module-level cache.  This helper
    creates a new instance with copied dicts to preserve per-call
    isolation.
    """
    return replace(
        summary,
        requests_by_model=dict(summary.requests_by_model),
        duration_by_model=dict(summary.duration_by_model),
        requests_by_category=dict(summary.requests_by_category),
        requests_by_date=dict(summary.requests_by_date),
    )


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
    are not re-parsed on repeated invocations.  A module-level summary
    cache (:data:`_vscode_summary_cache`) avoids re-aggregating all
    requests when no log file has changed.

    When the summary cache is invalidated (a file changed or was added),
    a per-file partial-summary cache (:data:`_PER_FILE_SUMMARY_CACHE`)
    avoids re-iterating requests for unchanged files.  Only files whose
    ``(mtime_ns, size)`` identity differs are re-aggregated via
    :func:`_update_vscode_summary`; unchanged files contribute via an
    O(num_models + num_categories + num_dates) :func:`_merge_partial`
    instead of O(requests).
    """
    global _vscode_summary_cache  # noqa: PLW0603

    logs = discover_vscode_logs(base_path)
    log_ids: list[tuple[Path, tuple[int, int] | None]] = [
        (p, safe_file_identity(p)) for p in logs
    ]
    current_ids: frozenset[tuple[Path, tuple[int, int] | None]] = frozenset(log_ids)

    if (
        _vscode_summary_cache is not None
        and _vscode_summary_cache.file_ids == current_ids
    ):
        return _copy_summary(_vscode_summary_cache.summary)

    acc = _SummaryAccumulator(log_files_found=len(logs))
    for log_path, file_id in log_ids:
        cached_fs = _PER_FILE_SUMMARY_CACHE.get(log_path)
        if cached_fs is not None and cached_fs.file_id == file_id:
            _PER_FILE_SUMMARY_CACHE.move_to_end(log_path)
            _merge_partial(acc, cached_fs.partial)
        else:
            try:
                result = _get_cached_vscode_requests(log_path, file_id)
            except OSError as exc:
                logger.warning("Could not read log file {}: {}", log_path, exc)
                continue
            partial_acc = _SummaryAccumulator()
            _update_vscode_summary(partial_acc, result)
            partial_summary = _finalize_summary(partial_acc)
            _PER_FILE_SUMMARY_CACHE[log_path] = _CachedFileSummary(
                file_id, partial_summary
            )
            _merge_partial(acc, partial_summary)
        acc.log_files_parsed += 1
    summary = _finalize_summary(acc)

    # Only cache when every discovered log was successfully parsed;
    # transient read failures should not produce a permanently stale cache.
    if summary.log_files_parsed == summary.log_files_found:
        _vscode_summary_cache = _CachedVSCodeSummary(
            file_ids=current_ids, summary=_copy_summary(summary)
        )
    return summary
