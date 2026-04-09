"""Pydantic v2 models for parsing Copilot CLI session events.

Each line in ~/.copilot/session-state/*/events.jsonl is a JSON event.
These models provide typed parsing for all known event types plus a
flexible fallback for unknown ones.
"""

import builtins
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Self

from pydantic import BaseModel, Field, field_validator, model_validator

__all__: Final[list[str]] = [
    "EPOCH",
    "CodeChanges",
    "EventType",
    "ModelMetrics",
    "RequestMetrics",
    "SessionContext",
    "SessionEvent",
    "SessionShutdownData",
    "SessionStartData",
    "SessionSummary",
    "AssistantMessageData",
    "ToolExecutionData",
    "ToolRequest",
    "ToolTelemetry",
    "TokenUsage",
    "UserMessageData",
    "GenericEventData",
    "add_to_model_metrics",
    "copy_model_metrics",
    "ensure_aware",
    "ensure_aware_opt",
    "has_active_period_stats",
    "merge_model_metrics",
    "session_sort_key",
    "shutdown_output_tokens",
    "total_output_tokens",
    "parse_token_int",
]

# ---------------------------------------------------------------------------
# Shared datetime utilities
# ---------------------------------------------------------------------------

# Aware datetime sentinel used as a sort-key fallback for sessions without a start_time.
EPOCH: Final[datetime] = datetime.min.replace(tzinfo=UTC)


def ensure_aware(dt: datetime) -> datetime:
    """Attach UTC timezone to a naive datetime.

    .. warning::
        Assumes the input is already expressed in UTC. No timezone
        conversion is performed — only the ``tzinfo`` flag is set.
        Passing a naive datetime in a non-UTC local timezone will
        produce a silently incorrect result.
    """
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def ensure_aware_opt(dt: datetime | None) -> datetime | None:
    """None-safe variant of :func:`ensure_aware`."""
    return ensure_aware(dt) if dt is not None else None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """Known Copilot CLI event types."""

    SESSION_START = "session.start"
    SESSION_SHUTDOWN = "session.shutdown"
    SESSION_RESUME = "session.resume"
    SESSION_ERROR = "session.error"
    SESSION_PLAN_CHANGED = "session.plan_changed"
    SESSION_WORKSPACE_FILE_CHANGED = "session.workspace_file_changed"
    ASSISTANT_MESSAGE = "assistant.message"
    ASSISTANT_TURN_START = "assistant.turn_start"
    ASSISTANT_TURN_END = "assistant.turn_end"
    TOOL_EXECUTION_START = "tool.execution_start"
    TOOL_EXECUTION_COMPLETE = "tool.execution_complete"
    USER_MESSAGE = "user.message"
    ABORT = "abort"


# ---------------------------------------------------------------------------
# Shared / nested models
# ---------------------------------------------------------------------------


class SessionContext(BaseModel):
    """Context attached to a session.start event."""

    cwd: str | None = None


class TokenUsage(BaseModel):
    """Token usage breakdown for a single model."""

    inputTokens: int = 0
    outputTokens: int = 0
    cacheReadTokens: int = 0
    cacheWriteTokens: int = 0


class RequestMetrics(BaseModel):
    """Request count and cost for a single model."""

    count: int = 0
    cost: int = 0


class ModelMetrics(BaseModel):
    """Combined request + usage metrics for one model."""

    requests: RequestMetrics = Field(default_factory=RequestMetrics)
    usage: TokenUsage = Field(default_factory=TokenUsage)


def add_to_model_metrics(target: ModelMetrics, source: ModelMetrics) -> None:
    """Add *source* fields into *target* in-place."""
    target.requests.count += source.requests.count
    target.requests.cost += source.requests.cost
    target.usage.inputTokens += source.usage.inputTokens
    target.usage.outputTokens += source.usage.outputTokens
    target.usage.cacheReadTokens += source.usage.cacheReadTokens
    target.usage.cacheWriteTokens += source.usage.cacheWriteTokens


def copy_model_metrics(mm: ModelMetrics) -> ModelMetrics:
    """Create an independent copy of *mm* via explicit construction.

    Builds new ``ModelMetrics``/``RequestMetrics``/``TokenUsage`` instances
    instead of using Pydantic's ``model_copy(deep=True)`` which delegates to
    ``copy.deepcopy`` and is significantly slower for simple int fields.
    """
    return ModelMetrics(
        requests=RequestMetrics(count=mm.requests.count, cost=mm.requests.cost),
        usage=TokenUsage(
            inputTokens=mm.usage.inputTokens,
            outputTokens=mm.usage.outputTokens,
            cacheReadTokens=mm.usage.cacheReadTokens,
            cacheWriteTokens=mm.usage.cacheWriteTokens,
        ),
    )


def merge_model_metrics(
    base: dict[str, ModelMetrics],
    additional: dict[str, ModelMetrics],
) -> dict[str, ModelMetrics]:
    """Return a new dict merging *additional* into *base* without mutation."""
    result = {name: copy_model_metrics(mm) for name, mm in base.items()}
    for name, mm in additional.items():
        if name in result:
            add_to_model_metrics(result[name], mm)
        else:
            result[name] = copy_model_metrics(mm)
    return result


class CodeChanges(BaseModel):
    """Code‐change stats from a session.shutdown event."""

    linesAdded: int = 0
    linesRemoved: int = 0
    filesModified: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Event data payloads
# ---------------------------------------------------------------------------


class SessionStartData(BaseModel):
    """Payload for ``session.start`` events."""

    sessionId: str
    version: int = 1
    producer: str = ""
    copilotVersion: str = ""
    startTime: datetime | None = None
    context: SessionContext = Field(default_factory=SessionContext)


class ToolRequest(BaseModel):
    """A single tool-call request embedded in an assistant message.

    Maps the well-defined structure observed in ``events.jsonl``::

        {"toolCallId": "toolu_…", "name": "ask_user",
         "arguments": {…}, "type": "function"}

    ``arguments`` is kept as ``dict[str, object]`` because the shape varies
    per tool.
    """

    toolCallId: str = ""
    name: str = ""
    arguments: dict[str, object] = Field(default_factory=dict)
    type: str = ""


def parse_token_int(raw: object) -> int | None:
    """Parse a raw ``outputTokens`` value into a positive ``int``, or ``None``.

    Centralises the token-validation rules shared by
    :meth:`AssistantMessageData._sanitize_non_numeric_tokens` (Pydantic
    boundary) and :func:`~copilot_usage.parser._extract_output_tokens`
    (parser fast path).

    Rules:

    - ``bool`` / ``str`` → ``None`` (invalid, not coerced)
    - non-whole ``float`` → ``None``
    - zero or negative ``int`` / ``float`` → ``None``
    - positive whole-number ``float`` → coerced to ``int``
    - positive ``int`` → returned as-is
    - any other type → ``None``
    """
    if isinstance(raw, (bool, str)):
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


class AssistantMessageData(BaseModel):
    """Payload for ``assistant.message`` events."""

    messageId: str = ""
    content: str = ""
    outputTokens: int = 0
    interactionId: str = ""

    @field_validator("outputTokens", mode="before")
    @classmethod
    def _sanitize_non_numeric_tokens(cls, v: object) -> object:
        """Map non-positive, non-numeric, and non-whole-float token counts to ``0``.

        Delegates to :func:`parse_token_int` for the actual validation
        logic.  ``None`` (JSON ``null``) and types the helper recognises
        (``bool``, ``str``, ``int``, ``float``) are mapped to ``0`` when
        they don't represent a positive whole-number count, so that
        Pydantic's downstream ``int`` coercion always succeeds.  Unknown
        types are passed through so that Pydantic can raise its own
        ``ValidationError``.
        """
        if v is None:
            return 0
        if not isinstance(v, (bool, str, int, float)):
            return v
        result = parse_token_int(v)
        return result if result is not None else 0

    reasoningText: str | None = None
    reasoningOpaque: str | None = None
    toolRequests: list[ToolRequest] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType] - Pydantic infers the generic at runtime


class SessionShutdownData(BaseModel):
    """Payload for ``session.shutdown`` events."""

    shutdownType: str = ""
    totalPremiumRequests: int = 0
    totalApiDurationMs: int = 0
    codeChanges: CodeChanges | None = None
    modelMetrics: dict[str, ModelMetrics] = Field(default_factory=dict)
    currentModel: str | None = None


class ToolTelemetry(BaseModel):
    """Telemetry attached to tool execution events."""

    properties: dict[str, str] = Field(default_factory=dict)


class ToolExecutionData(BaseModel):
    """Payload for ``tool.execution_complete`` events."""

    toolCallId: str = ""
    model: str | None = None
    interactionId: str | None = None
    success: bool = False
    toolTelemetry: ToolTelemetry | None = None


class UserMessageData(BaseModel):
    """Payload for ``user.message`` events."""

    content: str = ""
    transformedContent: str | None = None
    attachments: list[str] = Field(default_factory=list)
    interactionId: str | None = None


# ---------------------------------------------------------------------------
# Generic / fallback data (for events we don't model in detail)
# ---------------------------------------------------------------------------


class GenericEventData(BaseModel, extra="allow"):
    """Catch‐all payload for event types not yet modeled explicitly."""


# ---------------------------------------------------------------------------
# Typed event helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


class SessionEvent(BaseModel):
    """A single event from an ``events.jsonl`` file.

    ``data`` is kept as a generic dict-like object; callers use the
    typed ``as_*()`` accessors (``as_session_start()``,
    ``as_assistant_message()``, etc.) to get a validated, narrowly-typed
    payload for each known event type.
    """

    type: str
    data: dict[str, object] = Field(default_factory=dict)
    id: str | None = None
    timestamp: datetime | None = None
    parentId: str | None = None
    # session.shutdown has currentModel at the top level
    currentModel: str | None = None

    def _as[T: BaseModel](
        self, expected_type: EventType, model_cls: builtins.type[T]
    ) -> T:
        """Validate event type and return parsed data.

        Raises:
            ValueError: If ``self.type`` does not match *expected_type*.
            pydantic.ValidationError: If the ``data`` payload is malformed.
        """
        if self.type != expected_type:
            raise ValueError(f"Expected {expected_type}, got {self.type}")
        return model_cls.model_validate(self.data)

    def as_session_start(self) -> SessionStartData:
        """Return typed data.

        Raises:
            ValueError: If the event type is not ``session.start``.
            pydantic.ValidationError: If the underlying ``data`` payload is malformed.
        """
        return self._as(EventType.SESSION_START, SessionStartData)

    def as_session_shutdown(self) -> SessionShutdownData:
        """Return typed data.

        Raises:
            ValueError: If the event type is not ``session.shutdown``.
            pydantic.ValidationError: If the underlying ``data`` payload is malformed.
        """
        return self._as(EventType.SESSION_SHUTDOWN, SessionShutdownData)

    def as_assistant_message(self) -> AssistantMessageData:
        """Return typed data.

        Raises:
            ValueError: If the event type is not ``assistant.message``.
            pydantic.ValidationError: If the underlying ``data`` payload is malformed.
        """
        return self._as(EventType.ASSISTANT_MESSAGE, AssistantMessageData)

    def as_user_message(self) -> UserMessageData:
        """Return typed data.

        Raises:
            ValueError: If the event type is not ``user.message``.
            pydantic.ValidationError: If the underlying ``data`` payload is malformed.
        """
        return self._as(EventType.USER_MESSAGE, UserMessageData)

    def as_tool_execution(self) -> ToolExecutionData:
        """Return typed data.

        Raises:
            ValueError: If the event type is not ``tool.execution_complete``.
            pydantic.ValidationError: If the underlying ``data`` payload is malformed.
        """
        return self._as(EventType.TOOL_EXECUTION_COMPLETE, ToolExecutionData)


# ---------------------------------------------------------------------------
# Session summary (aggregated from all events in one session)
# ---------------------------------------------------------------------------


class SessionSummary(BaseModel):
    """Aggregated data across all events in a single session.

    Populated by a parser that walks the ``events.jsonl`` file; not
    parsed directly from JSON.
    """

    session_id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    name: str | None = None
    cwd: str | None = None
    model: str | None = None
    total_premium_requests: int = 0
    total_api_duration_ms: int = 0
    model_metrics: dict[str, ModelMetrics] = Field(default_factory=dict)
    code_changes: CodeChanges | None = None
    model_calls: int = 0
    user_messages: int = 0
    last_resume_time: datetime | None = None
    is_active: bool = False
    has_shutdown_metrics: bool = False
    events_path: Path | None = None

    # Per-cycle shutdown data: (timestamp, parsed shutdown payload).
    # Populated at build time so renderers never re-scan the event list.
    shutdown_cycles: list[tuple[datetime | None, SessionShutdownData]] = Field(  # pyright: ignore[reportUnknownVariableType] - Pydantic infers the generic at runtime
        default_factory=list
    )

    # Post-shutdown activity (only populated for resumed/active sessions)
    active_model_calls: int = 0
    active_user_messages: int = 0
    active_output_tokens: int = 0

    @model_validator(mode="after")
    def _check_active_counters(self) -> Self:
        if self.active_model_calls > self.model_calls:
            raise ValueError(
                f"active_model_calls ({self.active_model_calls}) must be <= "
                f"model_calls ({self.model_calls})"
            )
        if self.active_user_messages > self.user_messages:
            raise ValueError(
                f"active_user_messages ({self.active_user_messages}) must be <= "
                f"user_messages ({self.user_messages})"
            )
        return self


# ---------------------------------------------------------------------------
# Session-level computed helpers (depend only on SessionSummary fields)
# ---------------------------------------------------------------------------


def shutdown_output_tokens(session: SessionSummary) -> int:
    """Return shutdown-derived output tokens only (model_metrics baseline).

    This deliberately excludes ``active_output_tokens`` so that historical /
    shutdown-only views never include post-resume activity.
    """
    return sum(m.usage.outputTokens for m in session.model_metrics.values())


def total_output_tokens(session: SessionSummary) -> int:
    """Return total output tokens including post-resume active tokens.

    For resumed sessions whose ``has_shutdown_metrics`` flag is ``True``,
    the ``active_output_tokens`` field represents *additional* tokens
    produced after the last shutdown and must be added to the historical
    baseline.

    When ``model_metrics`` is empty the baseline is zero, so the active
    tokens are the only source and are included unconditionally.

    Pure-active sessions (no shutdown data) already mirror
    ``active_output_tokens`` inside ``model_metrics``, so adding them again
    would double-count.
    """
    baseline = shutdown_output_tokens(session)
    if (
        has_active_period_stats(session) and session.has_shutdown_metrics
    ) or not session.model_metrics:
        return baseline + session.active_output_tokens
    return baseline


def has_active_period_stats(session: SessionSummary) -> bool:
    """Return True when *session* has meaningful active-period stats.

    A session has active-period stats when it was resumed (``last_resume_time``
    is set) **or** any of its ``active_*`` counters are positive.  When this
    returns ``False`` callers should fall back to the session totals.
    """
    return (
        session.last_resume_time is not None
        or session.active_user_messages > 0
        or session.active_output_tokens > 0
        or session.active_model_calls > 0
    )


def session_sort_key(session: SessionSummary) -> datetime:
    """Return an aware start_time for sorting; use with reverse=True to place unknown start_time last.

    When ``session.start_time`` is ``None``, this returns the ``EPOCH`` sentinel
    (``datetime.min`` in UTC). This means that in an ascending sort, sessions
    without a start time will appear first; to have them appear last, callers
    should sort with ``reverse=True``.
    """
    return ensure_aware(session.start_time) if session.start_time is not None else EPOCH
