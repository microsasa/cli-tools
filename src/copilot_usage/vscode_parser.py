"""Parser for VS Code Copilot Chat log files."""

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Final

from loguru import logger

__all__: Final[list[str]] = [
    "CCREQ_RE",
    "VSCodeLogSummary",
    "VSCodeRequest",
    "build_vscode_summary",
    "discover_vscode_logs",
    "get_vscode_summary",
    "parse_vscode_log",
]

CCREQ_RE: Final[re.Pattern[str]] = re.compile(
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
    """Aggregated stats from VS Code Copilot Chat logs."""

    total_requests: int = 0
    total_duration_ms: int = 0
    requests_by_model: dict[str, int] = field(default_factory=lambda: {})
    duration_by_model: dict[str, int] = field(default_factory=lambda: {})
    requests_by_category: dict[str, int] = field(default_factory=lambda: {})
    requests_by_date: dict[str, int] = field(default_factory=lambda: {})
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    log_files_parsed: int = 0


def discover_vscode_logs(base_path: Path | None = None) -> list[Path]:
    """Find all VS Code Copilot Chat log files.

    By default, the base logs directory is:

    * On Windows: ``%APPDATA%/Code/logs`` (or ``~/AppData/Roaming/Code/logs`` if
      ``%APPDATA%`` is not set).
    * On macOS: ``~/Library/Application Support/Code/logs``.
    * On other platforms (e.g. Linux): ``~/.config/Code/logs``.
    """
    if base_path is None:
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                base_path = Path(appdata) / "Code" / "logs"
            else:
                base_path = Path.home() / "AppData" / "Roaming" / "Code" / "logs"
        elif sys.platform == "darwin":
            base_path = (
                Path.home() / "Library" / "Application Support" / "Code" / "logs"
            )
        else:
            base_path = Path.home() / ".config" / "Code" / "logs"

    if not base_path.is_dir():
        logger.debug("VS Code logs directory not found: {}", base_path)
        return []

    pattern = "*/window*/exthost/GitHub.copilot-chat/GitHub Copilot Chat.log"
    logs = sorted(base_path.glob(pattern))
    logger.debug("Discovered {} VS Code log file(s) under {}", len(logs), base_path)
    return logs


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
            m = CCREQ_RE.match(line)
            if m is None:
                continue
            ts_str, req_id, model, duration_str, category = m.groups()
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
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


def _update_vscode_summary(
    acc: _SummaryAccumulator, requests: list[VSCodeRequest]
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
    )


def build_vscode_summary(requests: list[VSCodeRequest]) -> VSCodeLogSummary:
    """Aggregate a list of parsed requests into a summary."""
    acc = _SummaryAccumulator()
    _update_vscode_summary(acc, requests)
    return _finalize_summary(acc)


def get_vscode_summary(base_path: Path | None = None) -> VSCodeLogSummary:
    """Discover, parse, and aggregate all VS Code Copilot Chat logs."""
    logs = discover_vscode_logs(base_path)
    acc = _SummaryAccumulator()
    for log_path in logs:
        try:
            result = parse_vscode_log(log_path)
        except OSError as exc:
            logger.warning("Could not read log file {}: {}", log_path, exc)
            continue
        _update_vscode_summary(acc, result)
        acc.log_files_parsed += 1
    return _finalize_summary(acc)
