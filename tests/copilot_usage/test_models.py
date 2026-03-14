"""Tests for copilot_usage.models — Pydantic v2 event parsing."""

import json
from datetime import UTC, datetime

from copilot_usage.models import (
    AssistantMessageData,
    CodeChanges,
    EventType,
    ModelMetrics,
    RequestMetrics,
    SessionEvent,
    SessionShutdownData,
    SessionStartData,
    SessionSummary,
    TokenUsage,
    ToolExecutionData,
    UserMessageData,
)

# ---------------------------------------------------------------------------
# Raw JSON fixtures (from real events.jsonl files)
# ---------------------------------------------------------------------------

RAW_SESSION_START = json.loads(
    '{"type":"session.start","data":{"sessionId":"0faecbdf-b889-4bca-a51a-5254f5488cb6",'
    '"version":1,"producer":"copilot-agent","copilotVersion":"1.0.2",'
    '"startTime":"2026-03-07T15:15:20.265Z","context":{"cwd":"/Users/sasa"}},'
    '"id":"7283e3ac-5608-4a28-a37b-32b744733314",'
    '"timestamp":"2026-03-07T15:15:20.267Z","parentId":null}'
)

RAW_ASSISTANT_MESSAGE = json.loads(
    '{"type":"assistant.message","data":{"messageId":"dca91a42",'
    '"content":"some content","toolRequests":[],'
    '"interactionId":"c0c803cf","reasoningOpaque":"...",'
    '"reasoningText":"...","outputTokens":373},'
    '"id":"161d0d5a","timestamp":"2026-03-07T15:23:45.175Z",'
    '"parentId":"d03b9461"}'
)

RAW_SHUTDOWN = json.loads(
    '{"type":"session.shutdown","data":{"shutdownType":"routine",'
    '"totalPremiumRequests":24,"totalApiDurationMs":389114,'
    '"sessionStartTime":1772896520265,'
    '"codeChanges":{"linesAdded":134,"linesRemoved":2,'
    '"filesModified":["/Users/sasa/test_github_models.sh"]},'
    '"modelMetrics":{"claude-opus-4.6-1m":{"requests":{"count":53,"cost":24},'
    '"usage":{"inputTokens":1627935,"outputTokens":16655,'
    '"cacheReadTokens":1424086,"cacheWriteTokens":0}}}},'
    '"currentModel":"claude-opus-4.6-1m"}'
)

RAW_TOOL_EXEC = json.loads(
    '{"type":"tool.execution_complete","data":{"toolCallId":"toolu_xxx",'
    '"model":"claude-opus-4.6-1m","interactionId":"c0c803cf","success":true,'
    '"toolTelemetry":{"properties":{"outcome":"answered"}}},'
    '"id":"xxx","timestamp":"2026-03-07T15:23:45.175Z","parentId":"yyy"}'
)

RAW_USER_MESSAGE = json.loads(
    '{"type":"user.message","data":{"content":"hey there",'
    '"transformedContent":"...","attachments":[],"interactionId":"c0c803cf"},'
    '"id":"d6648885","timestamp":"2026-03-07T15:23:35.661Z",'
    '"parentId":"f09411f5"}'
)


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


def test_event_type_values() -> None:
    assert EventType.SESSION_START == "session.start"
    assert EventType.SESSION_SHUTDOWN == "session.shutdown"
    assert EventType.USER_MESSAGE == "user.message"


# ---------------------------------------------------------------------------
# Leaf models
# ---------------------------------------------------------------------------


def test_token_usage() -> None:
    t = TokenUsage(inputTokens=100, outputTokens=50)
    assert t.cacheReadTokens == 0
    assert t.cacheWriteTokens == 0


def test_request_metrics() -> None:
    r = RequestMetrics(count=10, cost=5)
    assert r.count == 10


def test_model_metrics() -> None:
    m = ModelMetrics()
    assert m.requests.count == 0
    assert m.usage.inputTokens == 0


def test_code_changes() -> None:
    c = CodeChanges(linesAdded=10, linesRemoved=2, filesModified=["a.py"])
    assert len(c.filesModified) == 1


# ---------------------------------------------------------------------------
# Event data payloads
# ---------------------------------------------------------------------------


def test_session_start_data() -> None:
    d = SessionStartData.model_validate(RAW_SESSION_START["data"])
    assert d.sessionId == "0faecbdf-b889-4bca-a51a-5254f5488cb6"
    assert d.copilotVersion == "1.0.2"
    assert d.context.cwd == "/Users/sasa"
    assert d.startTime is not None


def test_assistant_message_data() -> None:
    d = AssistantMessageData.model_validate(RAW_ASSISTANT_MESSAGE["data"])
    assert d.outputTokens == 373
    assert d.reasoningText == "..."


def test_session_shutdown_data() -> None:
    d = SessionShutdownData.model_validate(RAW_SHUTDOWN["data"])
    assert d.totalPremiumRequests == 24
    assert d.totalApiDurationMs == 389114
    assert d.codeChanges is not None
    assert d.codeChanges.linesAdded == 134
    assert "claude-opus-4.6-1m" in d.modelMetrics
    m = d.modelMetrics["claude-opus-4.6-1m"]
    assert m.requests.count == 53
    assert m.usage.inputTokens == 1627935


def test_tool_execution_data() -> None:
    d = ToolExecutionData.model_validate(RAW_TOOL_EXEC["data"])
    assert d.success is True
    assert d.model == "claude-opus-4.6-1m"
    assert d.toolTelemetry is not None
    assert d.toolTelemetry.properties["outcome"] == "answered"


def test_user_message_data() -> None:
    d = UserMessageData.model_validate(RAW_USER_MESSAGE["data"])
    assert d.content == "hey there"
    assert d.interactionId == "c0c803cf"


# ---------------------------------------------------------------------------
# SessionEvent envelope + parse_data()
# ---------------------------------------------------------------------------


def test_session_event_start() -> None:
    ev = SessionEvent.model_validate(RAW_SESSION_START)
    assert ev.type == "session.start"
    data = ev.parse_data()
    assert isinstance(data, SessionStartData)


def test_session_event_shutdown() -> None:
    ev = SessionEvent.model_validate(RAW_SHUTDOWN)
    assert ev.currentModel == "claude-opus-4.6-1m"
    data = ev.parse_data()
    assert isinstance(data, SessionShutdownData)


def test_session_event_assistant_message() -> None:
    ev = SessionEvent.model_validate(RAW_ASSISTANT_MESSAGE)
    data = ev.parse_data()
    assert isinstance(data, AssistantMessageData)


def test_session_event_tool_exec() -> None:
    ev = SessionEvent.model_validate(RAW_TOOL_EXEC)
    data = ev.parse_data()
    assert isinstance(data, ToolExecutionData)


def test_session_event_user_message() -> None:
    ev = SessionEvent.model_validate(RAW_USER_MESSAGE)
    data = ev.parse_data()
    assert isinstance(data, UserMessageData)


def test_session_event_unknown_type() -> None:
    raw = {"type": "some.future.event", "data": {"foo": "bar"}, "id": "x"}
    ev = SessionEvent.model_validate(raw)
    data = ev.parse_data()
    assert data is not None


# ---------------------------------------------------------------------------
# SessionSummary
# ---------------------------------------------------------------------------


def test_session_summary_defaults() -> None:
    s = SessionSummary(session_id="abc")
    assert s.is_active is False
    assert s.user_messages == 0
    assert s.model_calls == 0
    assert s.model_metrics == {}
    assert s.code_changes is None


def test_session_summary_full() -> None:
    s = SessionSummary(
        session_id="abc",
        start_time=datetime(2026, 3, 7, 15, 0, tzinfo=UTC),
        model="claude-opus-4.6-1m",
        total_premium_requests=24,
        total_api_duration_ms=389114,
        model_metrics={
            "claude-opus-4.6-1m": ModelMetrics(
                requests=RequestMetrics(count=53, cost=24),
                usage=TokenUsage(inputTokens=1627935, outputTokens=16655),
            )
        },
        code_changes=CodeChanges(linesAdded=134, linesRemoved=2),
        user_messages=10,
        model_calls=5,
        is_active=False,
    )
    assert s.total_premium_requests == 24
    assert s.model_metrics["claude-opus-4.6-1m"].usage.inputTokens == 1627935
