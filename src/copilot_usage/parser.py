"""Event parser for Copilot CLI session data.

Discovers session directories, parses ``events.jsonl`` files into typed
:class:`SessionEvent` objects, and builds per-session :class:`SessionSummary`
aggregates.
"""

import dataclasses
import json
from collections import OrderedDict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ValidationError

__all__: Final[list[str]] = [
    "build_session_summary",
    "discover_sessions",
    "get_all_sessions",
    "get_cached_events",
    "parse_events",
]

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

_DEFAULT_BASE: Final[Path] = Path.home() / ".copilot" / "session-state"
_CONFIG_PATH: Final[Path] = Path.home() / ".copilot" / "config.json"


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
_SESSION_CACHE: dict[Path, _CachedSession] = {}


@dataclasses.dataclass(frozen=True, slots=True)
class _CachedEvents:
    """Cache entry pairing a file identity with parsed events."""

    file_id: tuple[int, int] | None
    events: list[SessionEvent]


# Module-level parsed-events cache: events_path → _CachedEvents.
# Avoids re-parsing the raw event list on every detail-view render.
# Uses OrderedDict for LRU eviction: most-recently-used entries are at
# the back, least-recently-used at the front.
_MAX_CACHED_EVENTS: Final[int] = 8
_EVENTS_CACHE: OrderedDict[Path, _CachedEvents] = OrderedDict()


def _insert_events_entry(
    events_path: Path,
    file_id: tuple[int, int] | None,
    events: list[SessionEvent],
) -> None:
    """Insert parsed events into ``_EVENTS_CACHE`` with LRU eviction.

    If *events_path* already exists in the cache (stale file-id), the
    old entry is removed first.  Otherwise, when the cache is full the
    least-recently-used entry (front of the ``OrderedDict``) is evicted.
    """
    if events_path in _EVENTS_CACHE:
        del _EVENTS_CACHE[events_path]
    elif len(_EVENTS_CACHE) >= _MAX_CACHED_EVENTS:
        _EVENTS_CACHE.popitem(last=False)  # evict LRU (front)
    _EVENTS_CACHE[events_path] = _CachedEvents(file_id=file_id, events=events)


def get_cached_events(events_path: Path) -> list[SessionEvent]:
    """Return parsed events, using cache when file identity is unchanged.

    Delegates to :func:`parse_events` on a cache miss and stores the
    result keyed by ``(events_path, file_identity)``.  The cache is
    bounded to :data:`_MAX_CACHED_EVENTS` entries; the **least-recently
    used** entry is evicted when the limit is reached.

    Raises:
        OSError: Propagated from :func:`parse_events` when the file
            cannot be opened or read.
    """
    file_id = _safe_file_identity(events_path)
    cached = _EVENTS_CACHE.get(events_path)
    if cached is not None and cached.file_id == file_id:
        _EVENTS_CACHE.move_to_end(events_path)
        return cached.events
    events = parse_events(events_path)
    _insert_events_entry(events_path, file_id, events)
    return events


_RESUME_INDICATOR_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.SESSION_RESUME,
        EventType.USER_MESSAGE,
        EventType.ASSISTANT_MESSAGE,
    }
)


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


@lru_cache(maxsize=4)
def _read_config_model(config_path: Path | None = None) -> str | None:
    """Read the active model from ``~/.copilot/config.json``."""
    path = config_path or _CONFIG_PATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        model = data.get("model")
        return model if isinstance(model, str) and model else None
    except json.JSONDecodeError as exc:
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


def _safe_int_tokens(raw: object) -> int | None:
    """Return *raw* as non-negative int if it is a genuine integer (not bool), else None."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def _safe_file_identity(path: Path) -> tuple[int, int] | None:
    """Return ``(st_mtime_ns, st_size)`` for *path*, or ``None`` on any OS error.

    Uses nanosecond-precision mtime paired with file size for robust
    change detection — avoids the float-rounding and coarse-resolution
    issues of ``st_mtime``.  Returning ``None`` (rather than a sentinel
    tuple like ``(0, 0)``) makes it impossible for an absent-file marker
    to collide with a legitimate file identity.
    """
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _discover_with_identity(
    base_path: Path | None = None,
    *,
    include_plan: bool = True,
) -> list[tuple[Path, tuple[int, int] | None, tuple[int, int] | None]]:
    """Find session ``events.jsonl`` files paired with their file identities.

    Returns ``(events_path, events_file_id, plan_file_id)`` tuples sorted
    by *events_file_id* (mtime descending, then size as tie-breaker).

    When *include_plan* is ``True`` (default) both identities are computed
    in a single directory scan, so callers pay zero extra stat calls for
    ``plan.md``.  When ``False``, the ``plan_file_id`` element is always
    ``None`` and the ``plan.md`` stat is skipped entirely — useful for
    callers that only need event ordering.
    """
    root = base_path or _DEFAULT_BASE
    if not root.is_dir():
        return []
    result: list[tuple[Path, tuple[int, int] | None, tuple[int, int] | None]] = []
    for p in root.glob("*/events.jsonl"):
        events_id = _safe_file_identity(p)
        plan_id = _safe_file_identity(p.parent / "plan.md") if include_plan else None
        result.append((p, events_id, plan_id))
    result.sort(key=lambda t: t[1] if t[1] is not None else (0, 0), reverse=True)
    return result


def discover_sessions(base_path: Path | None = None) -> list[Path]:
    """Find all session directories containing events.jsonl.

    Default *base_path*: ``~/.copilot/session-state/``

    Returns list of paths to ``events.jsonl`` files, sorted by file
    identity (newest first).

    Tolerates directories deleted between the glob and the stat call
    (TOCTOU race) by returning a zero identity for vanished paths.
    """
    return [
        p for p, _eid, _pid in _discover_with_identity(base_path, include_plan=False)
    ]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_events(events_path: Path) -> list[SessionEvent]:
    """Parse an ``events.jsonl`` file into a list of :class:`SessionEvent`.

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
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "{}:{} — malformed JSON, skipping", events_path, lineno
                    )
                    continue
                try:
                    events.append(SessionEvent.model_validate(raw))
                except ValidationError as exc:
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
        if ev.type == EventType.SESSION_START:
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

        elif ev.type == EventType.SESSION_SHUTDOWN:
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

        elif ev.type == EventType.USER_MESSAGE:
            user_message_count += 1

        elif ev.type == EventType.ASSISTANT_TURN_START:
            total_turn_starts += 1

        elif ev.type == EventType.ASSISTANT_MESSAGE:
            if (tokens := _safe_int_tokens(ev.data.get("outputTokens"))) is not None:
                total_output_tokens += tokens

        elif ev.type == EventType.TOOL_EXECUTION_COMPLETE and tool_model is None:
            try:
                parsed = ev.as_tool_execution()
                if parsed.model:
                    tool_model = parsed.model
            except ValidationError as exc:
                logger.debug(
                    "event {} — could not parse {} event ({}), skipping",
                    idx,
                    ev.type,
                    exc.error_count(),
                )

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
        if ev.type in _RESUME_INDICATOR_TYPES:
            session_resumed = True
        if ev.type == EventType.SESSION_RESUME and ev.timestamp is not None:
            last_resume_time = ev.timestamp
        if (
            ev.type == EventType.ASSISTANT_MESSAGE
            and (tokens := _safe_int_tokens(ev.data.get("outputTokens"))) is not None
        ):
            post_shutdown_output_tokens += tokens
        if ev.type == EventType.ASSISTANT_TURN_START:
            post_shutdown_turn_starts += 1
        if ev.type == EventType.USER_MESSAGE:
            post_shutdown_user_messages += 1

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
        # idx is always a valid index: _first_pass populates it via
        # enumerate(events) over the same list we receive here.
        shutdown_cycles.append((events[idx].timestamp, sd))

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

    The ``_read_config_model`` cache is cleared at the start of each
    invocation so that interactive callers (e.g. ``_interactive_loop``)
    pick up config-file edits between refreshes while still avoiding
    redundant reads *within* a single invocation.
    """
    _read_config_model.cache_clear()
    # Pass None explicitly to match the lru_cache key used by
    # build_session_summary (which passes config_path=None by default).
    current_config_model = _read_config_model(None)
    discovered = _discover_with_identity(base_path)
    summaries: list[SessionSummary] = []
    # Deferred _EVENTS_CACHE insertions.  _discover_with_identity returns
    # sessions newest-first; inserting in that order would place the
    # newest entries at the *front* of the OrderedDict where they would
    # be evicted first by popitem(last=False).  We collect them here and
    # insert in reversed (oldest→newest) order after the loop so that
    # the newest sessions end up at the back (MRU) and eviction drops
    # the oldest (LRU) entries.
    #
    # Only the newest _MAX_CACHED_EVENTS entries are retained to avoid a
    # temporary memory spike when many sessions are cache-misses.
    deferred_events: list[tuple[Path, tuple[int, int] | None, list[SessionEvent]]] = []
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
                _SESSION_CACHE[events_path] = _CachedSession(
                    file_id,
                    plan_id,
                    cached.config_model,
                    cached.depends_on_config,
                    summary,
                )
            else:
                summary = cached.summary
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
        _SESSION_CACHE[events_path] = _CachedSession(
            file_id,
            plan_id,
            current_config_model if meta.used_config_fallback else None,
            meta.used_config_fallback,
            summary,
        )
        summaries.append(summary)

    # Populate _EVENTS_CACHE in oldest→newest order so that the newest
    # sessions sit at the back (MRU) and eviction drops the oldest.
    for ep, fid, evts in reversed(deferred_events):
        _insert_events_entry(ep, fid, evts)

    # Prune stale cache entries for sessions no longer on disk.
    discovered_paths = {p for p, _, _ in discovered}
    stale = [p for p in _SESSION_CACHE if p not in discovered_paths]
    for p in stale:
        del _SESSION_CACHE[p]

    summaries.sort(key=session_sort_key, reverse=True)
    return summaries
