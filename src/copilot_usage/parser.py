"""Event parser for Copilot CLI session data.

Discovers session directories, parses ``events.jsonl`` files into typed
:class:`SessionEvent` objects, and builds per-session :class:`SessionSummary`
aggregates.
"""

import json
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from copilot_usage.models import (
    EPOCH,
    CodeChanges,
    EventType,
    ModelMetrics,
    SessionEvent,
    SessionShutdownData,
    SessionSummary,
    TokenUsage,
    merge_model_metrics,
)

_DEFAULT_BASE: Path = Path.home() / ".copilot" / "session-state"
_CONFIG_PATH: Path = Path.home() / ".copilot" / "config.json"

_RESUME_INDICATOR_TYPES: frozenset[str] = frozenset(
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


def _read_config_model(config_path: Path | None = None) -> str | None:
    """Read the active model from ``~/.copilot/config.json``."""
    path = config_path or _CONFIG_PATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        model = data.get("model")
        return model if isinstance(model, str) else None
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _safe_mtime(path: Path) -> float:
    """Return *path*'s mtime, or ``0`` on any OS-level error (deleted, permission denied, etc.)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def discover_sessions(base_path: Path | None = None) -> list[Path]:
    """Find all session directories containing events.jsonl.

    Default *base_path*: ``~/.copilot/session-state/``

    Returns list of paths to ``events.jsonl`` files, sorted by
    modification time (newest first).

    Tolerates directories deleted between the glob and the stat call
    (TOCTOU race) by assigning mtime 0 to vanished paths.
    """
    root = base_path or _DEFAULT_BASE
    if not root.is_dir():
        return []
    return sorted(
        root.glob("*/events.jsonl"),
        key=_safe_mtime,
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_events(events_path: Path) -> list[SessionEvent]:
    """Parse an ``events.jsonl`` file into a list of :class:`SessionEvent`.

    Lines that fail JSON decoding or Pydantic validation are skipped with
    a warning.
    """
    events: list[SessionEvent] = []
    with events_path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("{}:{} — malformed JSON, skipping", events_path, lineno)
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
    return events


# ---------------------------------------------------------------------------
# Session name extraction
# ---------------------------------------------------------------------------


def _extract_session_name(session_dir: Path) -> str | None:
    """Try to read a session name from ``plan.md`` in *session_dir*."""
    plan = session_dir / "plan.md"
    if not plan.is_file():
        return None
    try:
        first_line = plan.read_text(encoding="utf-8").split("\n", maxsplit=1)[0]
        if first_line.startswith("# "):
            return first_line.removeprefix("# ").strip()
    except OSError as exc:
        logger.debug("Could not read session name from {}: {}", plan, exc)
    return None


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def build_session_summary(
    events: list[SessionEvent],
    *,
    session_dir: Path | None = None,
    config_path: Path | None = None,
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
    session_id = ""
    start_time = None
    end_time = None
    cwd: str | None = None
    model: str | None = None
    all_shutdowns: list[tuple[int, SessionShutdownData, str | None]] = []
    user_message_count = 0
    total_output_tokens = 0
    total_turn_starts = 0

    for idx, ev in enumerate(events):
        # -- session.start ------------------------------------------------
        if ev.type == EventType.SESSION_START:
            try:
                data = ev.as_session_start()
            except ValidationError:
                continue
            session_id = data.sessionId
            start_time = data.startTime
            cwd = data.context.cwd

        # -- session.shutdown ---------------------------------------------
        elif ev.type == EventType.SESSION_SHUTDOWN:
            try:
                data = ev.as_session_shutdown()
            except ValidationError:
                continue
            current_model = ev.currentModel or data.currentModel
            if not current_model and data.modelMetrics:
                current_model = _infer_model_from_metrics(data.modelMetrics)
            all_shutdowns.append((idx, data, current_model))
            end_time = ev.timestamp
            model = current_model

        # -- user.message -------------------------------------------------
        elif ev.type == EventType.USER_MESSAGE:
            user_message_count += 1

        # -- assistant.turn_start -----------------------------------------
        elif ev.type == EventType.ASSISTANT_TURN_START:
            total_turn_starts += 1

        # -- assistant.message --------------------------------------------
        elif ev.type == EventType.ASSISTANT_MESSAGE:
            raw_tokens = ev.data.get("outputTokens")
            if isinstance(raw_tokens, int):
                total_output_tokens += raw_tokens

    # Derive name
    name = _extract_session_name(session_dir) if session_dir else None

    # --- Detect resumed session (events after last shutdown) --------------
    session_resumed = False
    post_shutdown_output_tokens = 0
    post_shutdown_turn_starts = 0
    post_shutdown_user_messages = 0
    last_resume_time = None

    last_shutdown_idx = all_shutdowns[-1][0] if all_shutdowns else -1

    if all_shutdowns and last_shutdown_idx >= 0:
        for ev in events[last_shutdown_idx + 1 :]:
            if ev.type in _RESUME_INDICATOR_TYPES:
                session_resumed = True
            if ev.type == EventType.SESSION_RESUME and ev.timestamp is not None:
                last_resume_time = ev.timestamp
            if ev.type == EventType.ASSISTANT_MESSAGE:
                raw_tokens = ev.data.get("outputTokens")
                if isinstance(raw_tokens, int):
                    post_shutdown_output_tokens += raw_tokens
            if ev.type == EventType.ASSISTANT_TURN_START:
                post_shutdown_turn_starts += 1
            if ev.type == EventType.USER_MESSAGE:
                post_shutdown_user_messages += 1

    # --- completed or resumed session ------------------------------------
    if all_shutdowns:
        # Sum across ALL shutdown cycles
        total_premium = 0
        total_api_duration = 0
        merged_metrics: dict[str, ModelMetrics] = {}
        last_code_changes: CodeChanges | None = None

        for _idx, sd, _m in all_shutdowns:
            total_premium += sd.totalPremiumRequests
            total_api_duration += sd.totalApiDurationMs
            if sd.codeChanges is not None:
                last_code_changes = sd.codeChanges
            merged_metrics = merge_model_metrics(merged_metrics, sd.modelMetrics)

        return SessionSummary(
            session_id=session_id,
            start_time=start_time,
            end_time=None if session_resumed else end_time,
            name=name,
            cwd=cwd,
            model=model,
            total_premium_requests=total_premium,
            total_api_duration_ms=total_api_duration,
            model_metrics=merged_metrics,
            code_changes=last_code_changes,
            model_calls=total_turn_starts,
            user_messages=user_message_count,
            is_active=session_resumed,
            last_resume_time=last_resume_time,
            active_model_calls=post_shutdown_turn_starts,
            active_user_messages=post_shutdown_user_messages,
            active_output_tokens=post_shutdown_output_tokens,
        )

    # --- active session (no shutdown) ------------------------------------
    # Try to determine model from tool.execution_complete events
    for ev in events:
        if ev.type == EventType.TOOL_EXECUTION_COMPLETE:
            try:
                parsed = ev.as_tool_execution()
            except ValidationError:
                continue
            if parsed.model:
                model = parsed.model
                break

    # Fall back to ~/.copilot/config.json for active sessions
    if model is None:
        model = _read_config_model(config_path)

    active_metrics: dict[str, ModelMetrics] = {}
    if model and total_output_tokens:
        active_metrics[model] = ModelMetrics(
            usage=TokenUsage(outputTokens=total_output_tokens),
        )

    return SessionSummary(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        name=name,
        cwd=cwd,
        model=model,
        total_premium_requests=0,
        total_api_duration_ms=0,
        model_metrics=active_metrics,
        code_changes=None,
        model_calls=total_turn_starts,
        user_messages=user_message_count,
        is_active=True,
        active_model_calls=total_turn_starts,
        active_user_messages=user_message_count,
        active_output_tokens=total_output_tokens,
    )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def get_all_sessions(base_path: Path | None = None) -> list[SessionSummary]:
    """Discover → parse → build summary for every session.

    Returns list sorted by ``start_time`` (newest first).  Sessions
    without a ``start_time`` sort last.
    """
    paths = discover_sessions(base_path)
    summaries: list[SessionSummary] = []
    for events_path in paths:
        try:
            events = parse_events(events_path)
        except OSError as exc:
            logger.warning("Skipping vanished session {}: {}", events_path, exc)
            continue
        if not events:
            continue
        summary = build_session_summary(events, session_dir=events_path.parent)
        summary.events_path = events_path
        summaries.append(summary)

    def _sort_key(s: SessionSummary) -> datetime:
        return s.start_time if s.start_time is not None else EPOCH

    summaries.sort(key=_sort_key, reverse=True)
    return summaries
