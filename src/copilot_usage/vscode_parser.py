"""Parser for VS Code Copilot Chat log files."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger

__all__ = [
    "CCREQ_RE",
    "VSCodeLogSummary",
    "VSCodeRequest",
    "build_vscode_summary",
    "discover_vscode_logs",
    "get_vscode_summary",
    "parse_vscode_log",
]

CCREQ_RE = re.compile(
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


@dataclass(slots=True)
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


def parse_vscode_log(log_path: Path) -> list[VSCodeRequest] | None:
    """Parse a single VS Code Copilot Chat log file into request objects.

    Returns a list of parsed requests, or ``None`` if the file could not be
    opened/read (so callers can distinguish "no matching lines" from "I/O
    failure").
    """
    requests: list[VSCodeRequest] = []
    try:
        with log_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
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
    except OSError as exc:
        logger.warning("Could not read log file {}: {}", log_path, exc)
        return None
    logger.debug("Parsed {} request(s) from {}", len(requests), log_path)
    return requests


def build_vscode_summary(requests: list[VSCodeRequest]) -> VSCodeLogSummary:
    """Aggregate a list of parsed requests into a summary."""
    summary = VSCodeLogSummary()
    for req in requests:
        summary.total_requests += 1
        summary.total_duration_ms += req.duration_ms

        summary.requests_by_model[req.model] = (
            summary.requests_by_model.get(req.model, 0) + 1
        )
        summary.duration_by_model[req.model] = (
            summary.duration_by_model.get(req.model, 0) + req.duration_ms
        )
        summary.requests_by_category[req.category] = (
            summary.requests_by_category.get(req.category, 0) + 1
        )

        date_key = req.timestamp.strftime("%Y-%m-%d")
        summary.requests_by_date[date_key] = (
            summary.requests_by_date.get(date_key, 0) + 1
        )

        if summary.first_timestamp is None or req.timestamp < summary.first_timestamp:
            summary.first_timestamp = req.timestamp
        if summary.last_timestamp is None or req.timestamp > summary.last_timestamp:
            summary.last_timestamp = req.timestamp

    return summary


def get_vscode_summary(base_path: Path | None = None) -> VSCodeLogSummary:
    """Discover, parse, and aggregate all VS Code Copilot Chat logs."""
    logs = discover_vscode_logs(base_path)
    all_requests: list[VSCodeRequest] = []
    parsed_count = 0
    for log_path in logs:
        result = parse_vscode_log(log_path)
        if result is not None:
            all_requests.extend(result)
            parsed_count += 1
    summary = build_vscode_summary(all_requests)
    summary.log_files_parsed = parsed_count
    return summary
