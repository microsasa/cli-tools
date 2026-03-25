"""Pydantic v2 models for parsing Copilot CLI session events.

Each line in ~/.copilot/session-state/*/events.jsonl is a JSON event.
These models provide typed parsing for all known event types plus a
flexible fallback for unknown ones.
"""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

# Defensive alias for the built-in ``type`` so it remains usable inside
# classes (like SessionEvent) that may define a Pydantic field ``type``
# with a default/Field assignment in the future, which would shadow it.
_type = type

# ---------------------------------------------------------------------------
# Shared datetime utilities
# ---------------------------------------------------------------------------

# Aware datetime sentinel used as a sort-key fallback for sessions without a start_time.
EPOCH: datetime = datetime.min.replace(tzinfo=UTC)


def ensure_aware(dt: datetime) -> datetime:
    """Attach UTC timezone to a naive datetime."""
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


def merge_model_metrics(
    base: dict[str, ModelMetrics],
    additional: dict[str, ModelMetrics],
) -> dict[str, ModelMetrics]:
    """Return a new dict merging *additional* into *base* without mutation."""
    result = {name: mm.model_copy(deep=True) for name, mm in base.items()}
    for name, mm in additional.items():
        if name in result:
            existing = result[name]
            existing.requests.count += mm.requests.count
            existing.requests.cost += mm.requests.cost
            existing.usage.inputTokens += mm.usage.inputTokens
            existing.usage.outputTokens += mm.usage.outputTokens
            existing.usage.cacheReadTokens += mm.usage.cacheReadTokens
            existing.usage.cacheWriteTokens += mm.usage.cacheWriteTokens
        else:
            result[name] = mm.model_copy(deep=True)
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


class AssistantMessageData(BaseModel):
    """Payload for ``assistant.message`` events."""

    messageId: str = ""
    content: str = ""
    outputTokens: int = 0
    interactionId: str = ""
    reasoningText: str | None = None
    reasoningOpaque: str | None = None
    toolRequests: list[dict[str, object]] = Field(default_factory=lambda: [])


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


EventData = (
    SessionStartData
    | AssistantMessageData
    | SessionShutdownData
    | ToolExecutionData
    | UserMessageData
    | GenericEventData
)


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


class SessionEvent(BaseModel):
    """A single event from an ``events.jsonl`` file.

    ``data`` is kept as a generic dict-like object; callers can use the
    helper ``parse_data()`` method to get a typed payload when needed.
    """

    type: str
    data: dict[str, object] = Field(default_factory=dict)
    id: str | None = None
    timestamp: datetime | None = None
    parentId: str | None = None
    # session.shutdown has currentModel at the top level
    currentModel: str | None = None

    def parse_data(self) -> EventData:
        """Return a strongly-typed data payload based on ``self.type``."""
        match self.type:
            case EventType.SESSION_START:
                return SessionStartData.model_validate(self.data)
            case EventType.ASSISTANT_MESSAGE:
                return AssistantMessageData.model_validate(self.data)
            case EventType.SESSION_SHUTDOWN:
                return SessionShutdownData.model_validate(self.data)
            case EventType.TOOL_EXECUTION_COMPLETE:
                return ToolExecutionData.model_validate(self.data)
            case EventType.USER_MESSAGE:
                return UserMessageData.model_validate(self.data)
            case _:
                return GenericEventData.model_validate(self.data)

    def _as[T: BaseModel](self, expected_type: EventType, model_cls: _type[T]) -> T:
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

    # Post-shutdown activity (only populated for resumed/active sessions)
    active_model_calls: int = 0
    active_user_messages: int = 0
    active_output_tokens: int = 0
