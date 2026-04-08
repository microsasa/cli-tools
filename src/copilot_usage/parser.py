"""Event parser for Copilot CLI session data.

Discovers session directories, parses ``events.jsonl`` files into typed
:class:`SessionEvent` objects, and builds per-session :class:`SessionSummary`
aggregates.
"""

import dataclasses
import os
from collections import OrderedDict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import BaseModel, ValidationError

__all__: Final[list[str]] = [
    "DEFAULT_SESSION_PATH",
    "build_session_summary",
    "discover_sessions",
    "get_all_sessions",
    "get_cached_events",
    "parse_events",
]

from copilot_usage._fs_utils import lru_insert, safe_file_identity
from copilot_usage.models import (
    CodeChanges,
    EventType,
    ModelMetrics,
    SessionEvent,
    SessionShutdownData,
    SessionSummary,
    TokenUsage,
    add_to_model_metrics,
    copy_model_metrics,
    session_sort_key,
)

DEFAULT_SESSION_PATH: Final[Path] = Path.home() / ".copilot" / "session-state"
_CONFIG_PATH: Final[Path] = Path.home() / ".copilot" / "config.json"


@dataclasses.dataclass(slots=True)
class _DiscoveryCache:
    """Cached directory listing for a session-state root.

    Stores the ``(st_mtime_ns, st_size)`` identity of the root directory
    at the time of the last full ``os.scandir`` sweep together with the
    ``(events_path, plan_path | None)`` pairs found.  When the root
    identity is unchanged on the next call, the inner per-session
    ``os.scandir`` calls are skipped entirely — only per-file ``stat``
    calls are issued to detect content changes.

    *probe_cursor* tracks where the next ``plan.md`` probe sweep should
    start within *no_plan_indices* so that every session is eventually
    probed across multiple cache-hit calls rather than always probing
    the first ``_MAX_PLAN_PROBES`` entries.

    *no_plan_indices* stores the indices (into *entries*) of sessions
    where ``plan_path`` is ``None``.  When empty, the probe scan is
    skipped entirely because there are no entries to probe.  The list
    is kept in sync at every mutation site so the probe walk is bounded
    by ``min(_MAX_PLAN_PROBES, len(no_plan_indices))`` — O(1) amortised
    regardless of the total number of sessions.
    """

    root_id: tuple[int, int] | None
    entries: list[tuple[Path, Path | None]]  # (events_path, plan_path)
    probe_cursor: int = 0
    no_plan_indices: list[int] = dataclasses.field(default_factory=lambda: [])


# Module-level discovery cache: root_path → _DiscoveryCache.
# Avoids redundant inner os.scandir calls when the root directory
# has not changed (no sessions added or removed).
_DISCOVERY_CACHE: dict[Path, _DiscoveryCache] = {}

# On cache hits, probe at most this many sessions without a cached
# plan.md for a newly-created file.  The probe window rotates via
# _DiscoveryCache.probe_cursor so every session is eventually checked.
_MAX_PLAN_PROBES: Final[int] = 5


@dataclasses.dataclass(frozen=True, slots=True)
class _CachedSession:
    """Cache entry pairing file identities with a built summary.

    Stores the ``(st_mtime_ns, st_size)`` identity of both
    ``events.jsonl`` and ``plan.md`` so that the session name is only
    re-read when ``plan.md`` actually changes on disk.  A ``None``
    identity means the file was absent or unreadable at discovery time.

    ``depends_on_config`` is ``True`` only when the session's model was
    sourced from ``~/.copilot/config.json`` (i.e. neither the events nor
    tool executions supplied a model).  When ``True``, ``config_model``
    records the value that was read so the cache can be invalidated on
    change — including the ``None → "gpt-…"`` transition.

    Resumed and completed sessions always have
    ``depends_on_config=False`` because their model comes from the
    shutdown event.
    """

    file_id: tuple[int, int] | None
    plan_id: tuple[int, int] | None
    config_model: str | None
    depends_on_config: bool
    summary: SessionSummary


# Module-level file-identity cache: events_path → _CachedSession.
# Avoids re-parsing unchanged files on every interactive refresh.
# Uses OrderedDict for LRU eviction: most-recently-used entries are at
# the back, least-recently-used at the front.
_MAX_CACHED_SESSIONS: Final[int] = 512
_SESSION_CACHE: OrderedDict[Path, _CachedSession] = OrderedDict()


def _insert_session_entry(
    events_path: Path,
    entry: _CachedSession,
) -> None:
    """Insert a session entry into ``_SESSION_CACHE`` with LRU eviction."""
    lru_insert(_SESSION_CACHE, events_path, entry, _MAX_CACHED_SESSIONS)


@dataclasses.dataclass(frozen=True, slots=True)
class _CachedEvents:
    """Cache entry pairing a file identity with parsed events."""

    file_id: tuple[int, int] | None
    events: tuple[SessionEvent, ...]


# Module-level parsed-events cache: events_path → _CachedEvents.
# Avoids re-parsing the raw event list on every detail-view render.
# Uses OrderedDict for LRU eviction: most-recently-used entries are at
# the back, least-recently-used at the front.
_MAX_CACHED_EVENTS: Final[int] = 32
_EVENTS_CACHE: OrderedDict[Path, _CachedEvents] = OrderedDict()

# Persists config file identity between invocations so that the
# _read_config_model lru_cache is only cleared when the file changes.
_config_file_id: tuple[int, int] | None = None


@dataclasses.dataclass(slots=True)
class _SortedSessionsCache:
    """Cached sorted result from ``get_all_sessions``.

    Stores a fingerprint of the discovered session set (path + file identity
    pairs) so that the O(n log n) sort can be skipped when the session set
    is completely unchanged.  The *root* field records which resolved base
    path the cache was built for so that the cheap early-return fast path
    can avoid rebuilding the O(n) fingerprint frozenset.
    """

    root: Path
    fingerprint: frozenset[tuple[Path, tuple[int, int] | None]]
    summaries: list[SessionSummary]


def _build_fingerprint(
    discovered: list[tuple[Path, tuple[int, int] | None, tuple[int, int] | None]],
) -> frozenset[tuple[Path, tuple[int, int] | None]]:
    """Build a fingerprint frozenset from discovered session entries."""
    return frozenset((p, fid) for p, fid, _ in discovered)


_sorted_sessions_cache: _SortedSessionsCache | None = None


def _insert_events_entry(
    events_path: Path,
    file_id: tuple[int, int] | None,
    events: list[SessionEvent],
) -> None:
    """Insert parsed events into ``_EVENTS_CACHE`` with LRU eviction.

    If *events_path* already exists in the cache (stale file-id), the
    old entry is removed first.  Otherwise, when the cache is full the
    least-recently-used entry (front of the ``OrderedDict``) is evicted.

    The *events* list is converted to a ``tuple`` before storage so
    that callers cannot accidentally add, remove, or reorder entries
    in the cache.  This is **container-level** immutability only —
    individual ``SessionEvent`` objects remain mutable and must not
    be modified by callers.
    """
    lru_insert(
        _EVENTS_CACHE,
        events_path,
        _CachedEvents(file_id=file_id, events=tuple(events)),
        _MAX_CACHED_EVENTS,
    )


def get_cached_events(events_path: Path) -> tuple[SessionEvent, ...]:
    """Return parsed events, using cache when file identity is unchanged.

    Delegates to :func:`parse_events` on a cache miss and stores the
    result keyed by *events_path* with file-identity validation on
    lookup.  The cache is bounded to :data:`_MAX_CACHED_EVENTS`
    entries; the **least-recently used** entry is evicted when the
    limit is reached.

    The returned ``tuple`` prevents callers from adding, removing, or
    reordering cached entries (container-level immutability).  Individual
    ``SessionEvent`` objects are **not** deep-copied and must not be
    mutated.  Callers that need a mutable sequence should use
    ``list(get_cached_events(...))``.

    Raises:
        OSError: Propagated from :func:`parse_events` when the file
            cannot be opened or read.
    """
    file_id = safe_file_identity(events_path)
    cached = _EVENTS_CACHE.get(events_path)
    if cached is not None and cached.file_id == file_id:
        _EVENTS_CACHE.move_to_end(events_path)
        return cached.events
    events = parse_events(events_path)
    _insert_events_entry(events_path, file_id, events)
    return _EVENTS_CACHE[events_path].events


def _infer_model_from_metrics(metrics: dict[str, ModelMetrics]) -> str | None:
    """Pick a model name from *metrics* when ``currentModel`` is absent.

    If there is exactly one key, return it.  With multiple keys, return
    the one with the highest ``requests.count``.
    """
    if not metrics:
        return None
    if len(metrics) == 1:
        return next(iter(metrics))
    return max(metrics, key=lambda m: metrics[m].requests.count)


class _CopilotConfig(BaseModel):
    """Schema for ``~/.copilot/config.json``."""

    model: str | None = None


@lru_cache(maxsize=4)
def _read_config_model(config_path: Path | None = None) -> str | None:
    """Read the active model from ``~/.copilot/config.json``."""
    path = config_path or _CONFIG_PATH
    if not path.is_file():
        return None
    try:
        config = _CopilotConfig.model_validate_json(path.read_text(encoding="utf-8"))
        return config.model if config.model else None
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "Config file {} contains malformed JSON; model will be unavailable: {}",
            path,
            exc,
        )
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read config file {}: {}", path, exc)
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _extract_output_tokens(ev: SessionEvent) -> int | None:
    """Extract ``outputTokens`` from an ``assistant.message`` event via direct dict access.

    Mirrors the domain intent of :class:`AssistantMessageData`'s
    ``_sanitize_non_numeric_tokens`` field-validator: only positive numeric
    values contribute tokens. When ``AssistantMessageData.model_validate(...)``
    succeeds, both paths agree on whether a value contributes tokens; the
    representation differs for non-contributing values — this function returns
    ``None``, whereas the Pydantic model stores ``0``. Inputs rejected by
    model validation should likewise be treated as non-contributing when
    comparing behaviors.

    Specifically:

    - ``bool`` / ``str`` → ``None`` (invalid, not coerced)
    - zero or negative ``int`` / ``float`` → ``None`` (non-positive)
    - whole-number positive ``float`` → coerced to ``int``
    - non-whole ``float`` / other non-numeric → ``None``

    Callers are responsible for verifying ``ev.type`` before calling; this
    function reads only the ``outputTokens`` key from the event data dict.
    """
    raw = ev.data.get("outputTokens")
    if raw is None or isinstance(raw, (bool, str)):
        return None
    if isinstance(raw, float):
        if not raw.is_integer():
            return None
        tokens = int(raw)
    elif isinstance(raw, int):
        tokens = raw
    else:
        return None
    return tokens if tokens > 0 else None


def _full_scandir_discovery(
    root: Path,
    *,
    include_plan: bool,
) -> list[tuple[Path, Path | None]]:
    """Perform a full ``os.scandir`` sweep of *root* and its session subdirs.

    Returns ``(events_path, plan_path | None)`` pairs — one per session
    directory that contains an ``events.jsonl`` file.  Uses a two-variable
    linear scan per subdirectory with early exit once both target files are
    found.
    """
    entries: list[tuple[Path, Path | None]] = []
    with os.scandir(root) as session_entries:
        for session_entry in session_entries:
            try:
                is_session_dir = session_entry.is_dir(follow_symlinks=False)
            except OSError:
                is_session_dir = False
            if not is_session_dir:
                continue
            events_entry: os.DirEntry[str] | None = None
            plan_entry: os.DirEntry[str] | None = None
            try:
                with os.scandir(session_entry.path) as dir_entries:
                    for e in dir_entries:
                        name = e.name
                        if name == "events.jsonl":
                            events_entry = e
                            if not include_plan or plan_entry is not None:
                                break
                        elif include_plan and name == "plan.md":
                            plan_entry = e
                            if events_entry is not None:
                                break
            except OSError:
                continue
            if events_entry is None:
                continue
            events_path = Path(events_entry.path)
            plan_path = Path(plan_entry.path) if plan_entry is not None else None
            entries.append((events_path, plan_path))
    return entries


def _discover_with_identity(
    base_path: Path | None = None,
    *,
    include_plan: bool = True,
) -> tuple[bool, list[tuple[Path, tuple[int, int] | None, tuple[int, int] | None]]]:
    """Find session ``events.jsonl`` files paired with their file identities.

    Returns a ``(is_cache_hit, entries)`` tuple.  *is_cache_hit* is
    ``True`` when the root directory's identity was unchanged, no full
    rescan was necessary, **and** no cached ``events.jsonl`` was
    definitively deleted (``FileNotFoundError``).  If deletions are
    detected during a root-level cache hit, *is_cache_hit* is set to
    ``False`` so that callers (e.g. ``get_all_sessions``) still run
    their stale-prune scans.  *entries* is a list of
    ``(events_path, events_file_id, plan_file_id)`` tuples sorted by
    *events_file_id* (mtime descending, then size as tie-breaker).

    Copilot CLI session directories are typically append-only in normal
    operation: new session directories are created over time and existing
    ones are not usually modified structurally. A module-level
    ``_DISCOVERY_CACHE`` stores the root directory's
    ``(st_mtime_ns, st_size)`` identity alongside the discovered
    ``(events_path, plan_path)`` pairs. When the root identity is
    unchanged, inner per-session ``os.scandir`` calls are skipped entirely
    — only per-file ``stat`` calls are issued. When the root identity has
    changed (for example, because a session was added, removed, or
    otherwise changed on disk), a full rescan is performed.

    If a cached ``events.jsonl`` is definitively deleted
    (``FileNotFoundError``), the entry is excluded from the result and
    pruned from the cache.  Transient errors (e.g. ``PermissionError``)
    also exclude the entry from the current result but leave it in the
    cache so it can reappear once readable again.

    When *include_plan* is ``True`` (default) and ``plan.md`` is present,
    its file identity is computed via :func:`safe_file_identity`.  If a
    previously-present ``plan.md`` becomes unreadable, the cached path is
    cleared to avoid repeated failing syscalls.  On cache hits, up to
    ``_MAX_PLAN_PROBES`` entries with no cached ``plan.md`` are probed
    for a newly-created file so that session names appear without a full
    rescan; the probe window rotates via a per-root cursor stored in
    ``_DiscoveryCache.probe_cursor`` so every session is eventually
    checked across successive cache-hit calls.  The scan walks the
    explicit ``no_plan_indices`` list rather than iterating all entries,
    so it is bounded by ``min(_MAX_PLAN_PROBES, len(no_plan_indices))``
    regardless of ``n_sessions``.  When all cached entries already have
    a ``plan.md`` (``no_plan_indices`` is empty), the probe scan is
    skipped entirely.
    When *include_plan* is ``False``, the ``plan_file_id`` element is
    always ``None`` — useful for callers that only need event ordering.
    """
    root = (base_path or DEFAULT_SESSION_PATH).resolve()

    root_id = safe_file_identity(root)
    if root_id is None:
        return False, []
    cached = _DISCOVERY_CACHE.get(root)

    if cached is not None and cached.root_id is not None and cached.root_id == root_id:
        entries = cached.entries
        is_cache_hit = True
    else:
        try:
            entries = _full_scandir_discovery(root, include_plan=True)
        except OSError:
            return False, []
        no_plan_idx = [i for i, (_, pp) in enumerate(entries) if pp is None]
        _DISCOVERY_CACHE[root] = _DiscoveryCache(
            root_id=root_id, entries=entries, no_plan_indices=no_plan_idx
        )
        is_cache_hit = False

    # On cache hits, select which entries to probe for newly-created
    # plan.md, rotating from the stored cursor so every session is
    # eventually checked across successive cache-hit calls.
    # The scan walks *no_plan_indices* directly, so it is bounded by
    # min(_MAX_PLAN_PROBES, len(no_plan_indices)) — O(1) amortised
    # regardless of the total number of sessions.
    probe_indices: frozenset[int] = frozenset()
    current_cache = _DISCOVERY_CACHE.get(root)
    if (
        is_cache_hit
        and include_plan
        and current_cache is not None
        and len(current_cache.no_plan_indices) > 0
    ):
        k = len(current_cache.no_plan_indices)
        start = current_cache.probe_cursor % k
        count = min(_MAX_PLAN_PROBES, k)
        probe_indices = frozenset(
            current_cache.no_plan_indices[(start + i) % k] for i in range(count)
        )
        current_cache.probe_cursor = (start + count) % k

    result: list[tuple[Path, tuple[int, int] | None, tuple[int, int] | None]] = []
    definitively_gone: list[Path] = []
    for idx, (events_path, plan_path) in enumerate(entries):
        # Distinguish permanent deletion from transient errors so only
        # truly-gone files are pruned from the cache.
        try:
            st = events_path.stat()
            events_id: tuple[int, int] = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            definitively_gone.append(events_path)
            continue
        except OSError:
            # Transient error (e.g. PermissionError) — skip from result
            # but keep in cache so the entry reappears once readable.
            continue

        plan_id: tuple[int, int] | None = None
        if include_plan:
            if plan_path is not None:
                plan_id = safe_file_identity(plan_path)
                if plan_id is None:
                    # Plan deleted or unreadable — clear cached path to
                    # avoid repeated failing syscalls on cache hits.
                    entries[idx] = (events_path, None)
                    if current_cache is not None:
                        current_cache.no_plan_indices.append(idx)
            elif idx in probe_indices:
                # Probe for newly-created plan.md not yet in cache.
                # Bounded to _MAX_PLAN_PROBES per call; cursor rotates
                # so every session is eventually checked.
                candidate = events_path.parent / "plan.md"
                plan_id = safe_file_identity(candidate)
                if plan_id is not None:
                    entries[idx] = (events_path, candidate)
                    if current_cache is not None:
                        try:
                            current_cache.no_plan_indices.remove(idx)
                        except ValueError:
                            current_cache.no_plan_indices = [
                                i
                                for i, (_, cached_plan_path) in enumerate(entries)
                                if cached_plan_path is None
                            ]

        result.append((events_path, events_id, plan_id))

    # Prune only definitively-deleted entries (FileNotFoundError) from
    # the cache.  Entries that failed with a transient OSError are kept
    # so they can reappear once readable again.
    if definitively_gone:
        gone_set = frozenset(definitively_gone)
        current = _DISCOVERY_CACHE.get(root)
        if current is not None:
            current.entries = [
                (ep, pp) for ep, pp in current.entries if ep not in gone_set
            ]
            # Rebuild no_plan_indices since entry indices shifted.
            current.no_plan_indices = [
                i for i, (_, pp) in enumerate(current.entries) if pp is None
            ]
        # A cached events.jsonl was deleted without changing the root
        # directory identity.  Signal a non-hit so callers still run
        # their stale-prune scans on _SESSION_CACHE / _EVENTS_CACHE.
        is_cache_hit = False

    result.sort(key=lambda t: t[1] if t[1] is not None else (0, 0), reverse=True)
    return is_cache_hit, result


def discover_sessions(base_path: Path | None = None) -> list[Path]:
    """Find all session directories containing events.jsonl.

    Default *base_path*: ``~/.copilot/session-state/``

    Returns list of paths to ``events.jsonl`` files, sorted by file
    identity (newest first).

    Sessions whose ``events.jsonl`` has been deleted since the last
    directory scan are silently skipped and pruned from the discovery
    cache; transiently unreadable sessions are skipped but retained in
    the cache.
    """
    _, entries = _discover_with_identity(base_path, include_plan=False)
    return [p for p, _eid, _pid in entries]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_events(events_path: Path) -> list[SessionEvent]:
    """Parse an ``events.jsonl`` file into a list of :class:`SessionEvent`.

    Uses ``SessionEvent.model_validate_json`` (Rust-based parser) for each
    line, bypassing the intermediate ``dict`` allocation of
    ``json.loads`` + ``model_validate``.

    Lines that fail JSON decoding or Pydantic validation are skipped with
    a warning.

    If a UTF-8 decode error occurs while reading the file, parsing stops
    early and the events parsed so far are returned (a partial session).

    Raises:
        OSError: If the file cannot be opened or read (e.g., deleted
            between discovery and parsing, or I/O error while streaming).
            UnicodeDecodeError is caught internally; callers only need to
            handle OSError.
    """
    events: list[SessionEvent] = []
    try:
        with events_path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line or line.isspace():
                    continue
                try:
                    events.append(SessionEvent.model_validate_json(line))
                except ValidationError as exc:
                    errors = exc.errors(include_url=False)
                    if errors and errors[0].get("type") == "json_invalid":
                        logger.warning(
                            "{}:{} — malformed JSON, skipping",
                            events_path,
                            lineno,
                        )
                    else:
                        logger.warning(
                            "{}:{} — validation error ({}), skipping",
                            events_path,
                            lineno,
                            exc.error_count(),
                        )
    except UnicodeDecodeError as exc:
        logger.warning(
            "{} — UTF-8 decode error while reading; returning {} parsed events so far (partial session): {}",
            events_path,
            len(events),
            exc,
        )
    return events


# ---------------------------------------------------------------------------
# Session name extraction
# ---------------------------------------------------------------------------


def _extract_session_name(
    session_dir: Path,
    *,
    plan_exists: bool | None = None,
) -> str | None:
    """Try to read a session name from ``plan.md`` in *session_dir*.

    When *plan_exists* is supplied, the file-existence check is skipped:
    ``True`` means the file is known to exist; ``False`` means it was
    absent or unreadable at discovery time.  When ``None`` (default),
    the function falls back to a filesystem ``is_file()`` check.
    """
    plan = session_dir / "plan.md"
    exists = plan_exists if plan_exists is not None else plan.is_file()
    if not exists:
        return None
    try:
        with plan.open(encoding="utf-8") as fh:
            first_line = fh.readline().rstrip("\n")
        if first_line.startswith("# "):
            return first_line.removeprefix("# ").strip() or None
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read session name from {}: {}", plan, exc)
    return None


# ---------------------------------------------------------------------------
# Summary builder — internal data carriers
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _FirstPassResult:
    """Accumulated state from a single pass over the event list."""

    session_id: str
    start_time: datetime | None
    end_time: datetime | None
    cwd: str | None
    model: str | None
    all_shutdowns: tuple[tuple[int, SessionShutdownData], ...]
    user_message_count: int
    total_output_tokens: int
    total_turn_starts: int
    tool_model: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class _ResumeInfo:
    """Results of scanning for post-shutdown activity."""

    session_resumed: bool
    post_shutdown_output_tokens: int
    post_shutdown_turn_starts: int
    post_shutdown_user_messages: int
    last_resume_time: datetime | None


# ---------------------------------------------------------------------------
# Summary builder — helpers
# ---------------------------------------------------------------------------

# O(1) pre-filter for _first_pass: skip events whose type is not checked.
# TOOL_EXECUTION_COMPLETE is handled separately *before* this filter so that,
# once tool_model is resolved, each remaining tool event costs only one string
# comparison + one None-check instead of traversing the full elif chain.
_FIRST_PASS_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        EventType.SESSION_START,
        EventType.SESSION_SHUTDOWN,
        EventType.USER_MESSAGE,
        EventType.ASSISTANT_TURN_START,
        EventType.ASSISTANT_MESSAGE,
    }
)


def _first_pass(events: list[SessionEvent]) -> _FirstPassResult:
    """Iterate *events* once, extracting identity, shutdown data, and counters."""
    session_id = ""
    start_time = None
    end_time = None
    cwd: str | None = None
    model: str | None = None
    seen_session_start = False
    _shutdowns: list[tuple[int, SessionShutdownData]] = []
    user_message_count = 0
    total_output_tokens = 0
    total_turn_starts = 0
    tool_model: str | None = None

    for idx, ev in enumerate(events):
        etype = ev.type
        # Fast path: once tool_model is found, skip all tool-complete events
        # with a single None-check instead of traversing the full elif chain.
        if etype == EventType.TOOL_EXECUTION_COMPLETE:
            if tool_model is None:
                m = ev.data.get("model")
                if isinstance(m, str) and m:
                    tool_model = m
            continue
        if etype not in _FIRST_PASS_EVENT_TYPES:
            continue
        if etype == EventType.SESSION_START:
            try:
                data = ev.as_session_start()
            except ValidationError as exc:
                logger.debug(
                    "event {} — could not parse {} event ({}), skipping",
                    idx,
                    ev.type,
                    exc.error_count(),
                )
                continue
            if not seen_session_start:
                seen_session_start = True
                session_id = data.sessionId
                start_time = data.startTime
                cwd = data.context.cwd

        elif etype == EventType.SESSION_SHUTDOWN:
            try:
                data = ev.as_session_shutdown()
            except ValidationError as exc:
                logger.debug(
                    "event {} — could not parse {} event ({}), skipping",
                    idx,
                    ev.type,
                    exc.error_count(),
                )
                continue
            current_model = ev.currentModel or data.currentModel
            if not current_model and data.modelMetrics:
                current_model = _infer_model_from_metrics(data.modelMetrics)
            _shutdowns.append((idx, data))
            end_time = ev.timestamp
            model = current_model

        elif etype == EventType.USER_MESSAGE:
            user_message_count += 1

        elif etype == EventType.ASSISTANT_TURN_START:
            total_turn_starts += 1

        elif etype == EventType.ASSISTANT_MESSAGE:
            if (tokens := _extract_output_tokens(ev)) is not None:
                total_output_tokens += tokens

    return _FirstPassResult(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        cwd=cwd,
        model=model,
        all_shutdowns=tuple(_shutdowns),
        user_message_count=user_message_count,
        total_output_tokens=total_output_tokens,
        total_turn_starts=total_turn_starts,
        tool_model=tool_model,
    )


def _detect_resume(
    events: list[SessionEvent],
    all_shutdowns: tuple[tuple[int, SessionShutdownData], ...],
) -> _ResumeInfo:
    """Scan events after the last shutdown for resume indicators."""
    if not all_shutdowns:
        return _ResumeInfo(
            session_resumed=False,
            post_shutdown_output_tokens=0,
            post_shutdown_turn_starts=0,
            post_shutdown_user_messages=0,
            last_resume_time=None,
        )

    last_shutdown_idx = all_shutdowns[-1][0]
    session_resumed = False
    post_shutdown_output_tokens = 0
    post_shutdown_turn_starts = 0
    post_shutdown_user_messages = 0
    last_resume_time = None

    for i in range(last_shutdown_idx + 1, len(events)):
        ev = events[i]
        etype = ev.type
        if etype == EventType.ASSISTANT_MESSAGE:
            session_resumed = True
            if (tokens := _extract_output_tokens(ev)) is not None:
                post_shutdown_output_tokens += tokens
        elif etype == EventType.USER_MESSAGE:
            session_resumed = True
            post_shutdown_user_messages += 1
        elif etype == EventType.ASSISTANT_TURN_START:
            post_shutdown_turn_starts += 1
        elif etype == EventType.SESSION_RESUME:
            session_resumed = True
            if ev.timestamp is not None:
                last_resume_time = ev.timestamp

    return _ResumeInfo(
        session_resumed=session_resumed,
        post_shutdown_output_tokens=post_shutdown_output_tokens,
        post_shutdown_turn_starts=post_shutdown_turn_starts,
        post_shutdown_user_messages=post_shutdown_user_messages,
        last_resume_time=last_resume_time,
    )


def _build_completed_summary(
    fp: _FirstPassResult,
    name: str | None,
    resume: _ResumeInfo,
    events: list[SessionEvent],
    *,
    events_path: Path | None = None,
) -> SessionSummary:
    """Build a :class:`SessionSummary` for a session that has shutdown data."""
    total_premium = 0
    total_api_duration = 0
    merged_metrics: dict[str, ModelMetrics] = {}

    # Aggregate CodeChanges across all shutdown cycles instead of keeping
    # only the last.  Lines are summed; files are de-duplicated via a set.
    total_lines_added = 0
    total_lines_removed = 0
    all_files_modified: set[str] = set()
    has_code_changes = False

    shutdown_cycles: list[tuple[datetime | None, SessionShutdownData]] = []

    for idx, sd in fp.all_shutdowns:
        total_premium += sd.totalPremiumRequests
        total_api_duration += sd.totalApiDurationMs
        if sd.codeChanges is not None:
            has_code_changes = True
            total_lines_added += sd.codeChanges.linesAdded
            total_lines_removed += sd.codeChanges.linesRemoved
            all_files_modified.update(sd.codeChanges.filesModified)
        for model_name, mm in sd.modelMetrics.items():
            if model_name in merged_metrics:
                add_to_model_metrics(merged_metrics[model_name], mm)
            else:
                merged_metrics[model_name] = copy_model_metrics(mm)
        shutdown_cycles.append(
            (events[idx].timestamp if idx < len(events) else None, sd)
        )

    return SessionSummary(
        session_id=fp.session_id,
        start_time=fp.start_time,
        end_time=None if resume.session_resumed else fp.end_time,
        name=name,
        cwd=fp.cwd,
        model=fp.model,
        total_premium_requests=total_premium,
        total_api_duration_ms=total_api_duration,
        model_metrics=merged_metrics,
        code_changes=(
            CodeChanges(
                linesAdded=total_lines_added,
                linesRemoved=total_lines_removed,
                filesModified=sorted(all_files_modified),
            )
            if has_code_changes
            else None
        ),
        model_calls=fp.total_turn_starts,
        user_messages=fp.user_message_count,
        is_active=resume.session_resumed,
        has_shutdown_metrics=bool(merged_metrics),
        last_resume_time=resume.last_resume_time,
        active_model_calls=resume.post_shutdown_turn_starts,
        active_user_messages=resume.post_shutdown_user_messages,
        active_output_tokens=resume.post_shutdown_output_tokens,
        shutdown_cycles=shutdown_cycles,
        events_path=events_path,
    )


def _build_active_summary(
    fp: _FirstPassResult,
    name: str | None,
    config_path: Path | None,
    *,
    events_path: Path | None = None,
) -> SessionSummary:
    """Build a :class:`SessionSummary` for a session with no shutdown data."""
    model = fp.model or fp.tool_model

    # Fall back to ~/.copilot/config.json for active sessions
    if model is None:
        model = _read_config_model(config_path)

    active_metrics: dict[str, ModelMetrics] = {}
    if model and fp.total_output_tokens:
        active_metrics[model] = ModelMetrics(
            usage=TokenUsage(outputTokens=fp.total_output_tokens),
        )

    return SessionSummary(
        session_id=fp.session_id,
        start_time=fp.start_time,
        end_time=fp.end_time,
        name=name,
        cwd=fp.cwd,
        model=model,
        total_premium_requests=0,
        total_api_duration_ms=0,
        model_metrics=active_metrics,
        code_changes=None,
        model_calls=fp.total_turn_starts,
        user_messages=fp.user_message_count,
        is_active=True,
        active_model_calls=fp.total_turn_starts,
        active_user_messages=fp.user_message_count,
        active_output_tokens=fp.total_output_tokens,
        events_path=events_path,
    )


# ---------------------------------------------------------------------------
# Summary builder — public API
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _BuildMeta:
    """Internal result from :func:`_build_session_summary_with_meta`.

    Carries the summary together with a flag indicating whether the
    model was resolved from the config file (as opposed to events).
    """

    summary: SessionSummary
    used_config_fallback: bool


def _build_session_summary_with_meta(
    events: list[SessionEvent],
    *,
    session_dir: Path | None = None,
    config_path: Path | None = None,
    events_path: Path | None = None,
    plan_exists: bool | None = None,
) -> _BuildMeta:
    """Build a summary and report whether the config fallback was used."""
    fp = _first_pass(events)
    name = (
        _extract_session_name(session_dir, plan_exists=plan_exists)
        if session_dir
        else None
    )

    if fp.all_shutdowns:
        resume = _detect_resume(events, fp.all_shutdowns)
        return _BuildMeta(
            _build_completed_summary(fp, name, resume, events, events_path=events_path),
            used_config_fallback=False,
        )

    used_config = fp.model is None and fp.tool_model is None
    return _BuildMeta(
        _build_active_summary(fp, name, config_path, events_path=events_path),
        used_config_fallback=used_config,
    )


def build_session_summary(
    events: list[SessionEvent],
    *,
    session_dir: Path | None = None,
    config_path: Path | None = None,
    events_path: Path | None = None,
) -> SessionSummary:
    """Build a :class:`SessionSummary` from parsed events.

    Reports raw facts only — no estimation or multiplier-based
    calculations.

    For **completed** sessions (``session.shutdown`` as last meaningful
    event):
      * Uses shutdown data directly (totalPremiumRequests, modelMetrics, …).

    For **resumed** sessions (events after the last
    ``session.shutdown``):
      * Uses the shutdown's modelMetrics as a baseline.
      * Adds ``outputTokens`` from post-shutdown ``assistant.message``
        events.
      * Sets ``is_active = True``.

    For **active** sessions (no shutdown at all):
      * Sums ``outputTokens`` from individual ``assistant.message``
        events.
      * Reads model from ``~/.copilot/config.json`` when not found in
        events.
      * Sets ``is_active = True``.

    If *session_dir* is given the session name is extracted from
    ``plan.md`` when present.
    """
    return _build_session_summary_with_meta(
        events,
        session_dir=session_dir,
        config_path=config_path,
        events_path=events_path,
    ).summary


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def get_all_sessions(base_path: Path | None = None) -> list[SessionSummary]:
    """Discover → parse → build summary for every session.

    Returns list sorted by ``start_time`` (newest first).  Sessions
    without a ``start_time`` sort last.

    Uses a module-level file-identity cache (``_SESSION_CACHE``) so
    that unchanged files are not re-parsed on subsequent calls — only
    files whose ``(st_mtime_ns, st_size)`` has changed since the last
    invocation are re-read.  Cached summaries have their ``name``
    refreshed from ``plan.md`` only when ``plan.md``'s own file
    identity has changed, avoiding redundant file reads.

    The ``_read_config_model`` cache is only cleared when the config
    file's ``(st_mtime_ns, st_size)`` identity changes, so interactive
    callers (e.g. ``_interactive_loop``) pick up config-file edits
    between refreshes while avoiding a redundant file read + JSON parse
    on every invocation.
    """
    global _config_file_id
    current_id = safe_file_identity(_CONFIG_PATH)
    if current_id != _config_file_id:
        _config_file_id = current_id
        _read_config_model.cache_clear()
    # Pass None explicitly to match the lru_cache key used by
    # build_session_summary (which passes config_path=None by default).
    current_config_model = _read_config_model(None)
    is_cache_hit, discovered = _discover_with_identity(base_path)
    summaries: list[SessionSummary] = []
    # Deferred cache insertions.  _discover_with_identity returns sessions
    # newest-first; inserting or promoting in that order would leave the
    # oldest session at MRU and the newest at LRU (wrong).  We collect
    # cache writes here and apply them in reversed (oldest→newest) order
    # after the loop so that the newest sessions end up at the back (MRU)
    # and eviction drops the oldest (LRU) entries.
    #
    # Only the newest _MAX_CACHED_EVENTS entries are retained for
    # _EVENTS_CACHE to avoid a temporary memory spike when many sessions
    # are cache-misses.
    deferred_events: list[tuple[Path, tuple[int, int] | None, list[SessionEvent]]] = []
    deferred_sessions: list[tuple[Path, _CachedSession]] = []
    cache_hit_paths: list[Path] = []
    for events_path, file_id, plan_id in discovered:
        cached = _SESSION_CACHE.get(events_path)
        # Config changes only invalidate cached entries that declared a
        # dependency on the config and were parsed with a different
        # config model. Entries that do not depend on config remain valid
        # across config changes.
        config_is_stale = (
            cached is not None
            and cached.depends_on_config
            and cached.config_model != current_config_model
        )
        if cached is not None and cached.file_id == file_id and not config_is_stale:
            if plan_id != cached.plan_id:
                fresh_name = _extract_session_name(events_path.parent)
                summary = cached.summary.model_copy(update={"name": fresh_name})
                deferred_sessions.append(
                    (
                        events_path,
                        _CachedSession(
                            file_id,
                            plan_id,
                            cached.config_model,
                            cached.depends_on_config,
                            summary,
                        ),
                    )
                )
            else:
                summary = cached.summary
                cache_hit_paths.append(events_path)
            summaries.append(summary)
            continue
        try:
            events = parse_events(events_path)
        except OSError as exc:
            logger.warning("Skipping unreadable session {}: {}", events_path, exc)
            continue
        if not events:
            continue
        if len(deferred_events) < _MAX_CACHED_EVENTS:
            deferred_events.append((events_path, file_id, events))
        meta = _build_session_summary_with_meta(
            events,
            session_dir=events_path.parent,
            events_path=events_path,
            plan_exists=plan_id is not None,
        )
        summary = meta.summary
        deferred_sessions.append(
            (
                events_path,
                _CachedSession(
                    file_id,
                    plan_id,
                    current_config_model if meta.used_config_fallback else None,
                    meta.used_config_fallback,
                    summary,
                ),
            )
        )
        summaries.append(summary)

    # Populate _SESSION_CACHE in oldest→newest order so that the newest
    # sessions sit at the back (MRU) and eviction drops the oldest.
    for ep, entry in reversed(deferred_sessions):
        _insert_session_entry(ep, entry)

    # Promote unchanged cache hits in oldest→newest order so that the
    # newest sessions end up at the MRU position.
    for ep in reversed(cache_hit_paths):
        if ep in _SESSION_CACHE:
            _SESSION_CACHE.move_to_end(ep)

    # Populate _EVENTS_CACHE in oldest→newest order so that the newest
    # sessions sit at the back (MRU) and eviction drops the oldest.
    for ep, fid, evts in reversed(deferred_events):
        _insert_events_entry(ep, fid, evts)

    # Prune stale cache entries for sessions no longer on disk.
    # Only remove entries rooted under the *current* base_path so that
    # callers using multiple roots in the same process don't evict each
    # other's cached entries.
    # Skipped on discovery cache hits — when the root directory is
    # unchanged *and* no cached events.jsonl was deleted, no sessions
    # can have been added or removed, making the O(cache_size) scan
    # unnecessary.  _discover_with_identity flips is_cache_hit to
    # False when it prunes definitively-deleted entries, ensuring the
    # scan still runs when needed.
    global _sorted_sessions_cache

    resolved_root = (base_path or DEFAULT_SESSION_PATH).resolve()

    if not is_cache_hit:
        discovered_paths = {p for p, _, _ in discovered}
        stale = [
            p
            for p in _SESSION_CACHE
            if p not in discovered_paths and p.is_relative_to(resolved_root)
        ]
        for p in stale:
            del _SESSION_CACHE[p]

        stale_events = [
            p
            for p in _EVENTS_CACHE
            if p not in discovered_paths and p.is_relative_to(resolved_root)
        ]
        for p in stale_events:
            del _EVENTS_CACHE[p]

    # Cheap fast path: when the discovery cache hits, no sessions were
    # re-parsed, no discovered sessions were skipped, and the cache was
    # built for the same root, the sorted order cannot have changed.
    # Return immediately without building the O(n) fingerprint
    # frozenset.
    if (
        is_cache_hit
        and not deferred_sessions
        and len(summaries) == len(discovered)
        and _sorted_sessions_cache is not None
        and _sorted_sessions_cache.root == resolved_root
    ):
        return list(_sorted_sessions_cache.summaries)

    # Fingerprint fallback: when the discovery cache missed but the
    # discovered session set is identical, skip the O(n log n) sort.
    current_fingerprint = _build_fingerprint(discovered)
    if (
        _sorted_sessions_cache is not None
        and _sorted_sessions_cache.fingerprint == current_fingerprint
        and not deferred_sessions
    ):
        return list(_sorted_sessions_cache.summaries)

    summaries.sort(key=session_sort_key, reverse=True)
    _sorted_sessions_cache = _SortedSessionsCache(
        resolved_root, current_fingerprint, list(summaries)
    )
    return summaries
