"""Tests for copilot_usage.models — Pydantic v2 event parsing."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from copilot_usage.models import (
    EPOCH,
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
    ToolRequest,
    UserMessageData,
    add_to_model_metrics,
    copy_model_metrics,
    ensure_aware,
    ensure_aware_opt,
    has_active_period_stats,
    merge_model_metrics,
    parse_token_int,
    shutdown_output_tokens,
    total_output_tokens,
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
    assert EventType.ASSISTANT_MESSAGE == "assistant.message"
    assert EventType.TOOL_EXECUTION_COMPLETE == "tool.execution_complete"
    assert EventType.USER_MESSAGE == "user.message"
    assert EventType.ABORT == "abort"


# ---------------------------------------------------------------------------
# Leaf models
# ---------------------------------------------------------------------------


def test_token_usage() -> None:
    t = TokenUsage()
    assert t.inputTokens == 0
    assert t.outputTokens == 0
    assert t.cacheReadTokens == 0
    assert t.cacheWriteTokens == 0

    t2 = TokenUsage(inputTokens=100, outputTokens=50)
    assert t2.inputTokens == 100
    assert t2.outputTokens == 50
    assert t2.cacheReadTokens == 0
    assert t2.cacheWriteTokens == 0


def test_request_metrics() -> None:
    r = RequestMetrics()
    assert r.count == 0
    assert r.cost == 0

    r2 = RequestMetrics(count=10, cost=5)
    assert r2.count == 10
    assert r2.cost == 5


def test_model_metrics() -> None:
    m = ModelMetrics()
    assert m.requests.count == 0
    assert m.usage.inputTokens == 0
    assert m.usage.outputTokens == 0

    m2 = ModelMetrics(
        requests=RequestMetrics(count=3, cost=10),
        usage=TokenUsage(inputTokens=500, outputTokens=200),
    )
    assert m2.requests.count == 3
    assert m2.requests.cost == 10
    assert m2.usage.inputTokens == 500
    assert m2.usage.outputTokens == 200


def test_code_changes() -> None:
    c = CodeChanges()
    assert c.linesAdded == 0
    assert c.linesRemoved == 0
    assert c.filesModified == []

    c2 = CodeChanges(linesAdded=10, linesRemoved=2, filesModified=["a.py"])
    assert c2.linesAdded == 10
    assert c2.linesRemoved == 2
    assert c2.filesModified == ["a.py"]


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


def test_assistant_message_data_tool_requests_default() -> None:
    """toolRequests defaults to an empty list when not supplied."""
    d = AssistantMessageData()
    assert d.toolRequests == []


def test_assistant_message_data_tool_requests_populated() -> None:
    """toolRequests is populated correctly when provided."""
    d = AssistantMessageData(toolRequests=[ToolRequest(name="bash", toolCallId="t1")])
    assert len(d.toolRequests) == 1
    assert d.toolRequests[0].name == "bash"
    assert d.toolRequests[0].toolCallId == "t1"


class TestSanitizeNonNumericTokens:
    """Validator maps bool/str/non-positive outputTokens to 0."""

    def test_bool_true_maps_to_zero(self) -> None:
        d = AssistantMessageData.model_validate({"outputTokens": True})
        assert d.outputTokens == 0

    def test_bool_false_maps_to_zero(self) -> None:
        d = AssistantMessageData.model_validate({"outputTokens": False})
        assert d.outputTokens == 0

    def test_numeric_string_maps_to_zero(self) -> None:
        d = AssistantMessageData.model_validate({"outputTokens": "100"})
        assert d.outputTokens == 0

    def test_non_numeric_string_maps_to_zero(self) -> None:
        d = AssistantMessageData.model_validate({"outputTokens": "abc"})
        assert d.outputTokens == 0

    def test_valid_int_passes_through(self) -> None:
        d = AssistantMessageData.model_validate({"outputTokens": 373})
        assert d.outputTokens == 373

    def test_zero_int_maps_to_zero(self) -> None:
        d = AssistantMessageData.model_validate({"outputTokens": 0})
        assert d.outputTokens == 0

    def test_zero_float_maps_to_zero(self) -> None:
        """Non-positive float token counts also map to ``0``."""
        d = AssistantMessageData.model_validate({"outputTokens": 0.0})
        assert d.outputTokens == 0

    def test_negative_int_maps_to_zero(self) -> None:
        """Negative token counts are meaningless and map to ``0``."""
        d = AssistantMessageData.model_validate({"outputTokens": -1})
        assert d.outputTokens == 0

    def test_negative_float_maps_to_zero(self) -> None:
        """Negative float token counts also map to ``0``."""
        d = AssistantMessageData.model_validate({"outputTokens": -1.0})
        assert d.outputTokens == 0

    def test_large_negative_maps_to_zero(self) -> None:
        """Large negative values also map to ``0``."""
        d = AssistantMessageData.model_validate({"outputTokens": -100_000})
        assert d.outputTokens == 0

    def test_non_whole_float_maps_to_zero(self) -> None:
        """Non-integer float like ``1.5`` maps to ``0``, matching ``_extract_output_tokens``."""
        d = AssistantMessageData.model_validate({"outputTokens": 1.5})
        assert d.outputTokens == 0

    def test_non_whole_float_large_maps_to_zero(self) -> None:
        """Large non-integer float like ``2.3`` maps to ``0``."""
        d = AssistantMessageData.model_validate({"outputTokens": 2.3})
        assert d.outputTokens == 0

    def test_whole_positive_float_coerced_to_int(self) -> None:
        """Whole-number float like ``100.0`` is coerced to ``100``."""
        d = AssistantMessageData.model_validate({"outputTokens": 100.0})
        assert d.outputTokens == 100

    @pytest.mark.parametrize("raw", [float("inf"), float("-inf"), float("nan")])
    def test_special_float_maps_to_zero(self, raw: float) -> None:
        """IEEE 754 special floats on the Pydantic boundary must map to 0."""
        d = AssistantMessageData.model_validate({"outputTokens": raw})
        assert d.outputTokens == 0

    def test_negative_zero_float_maps_to_zero(self) -> None:
        """-0.0 on the Pydantic boundary must map to 0."""
        d = AssistantMessageData.model_validate({"outputTokens": -0.0})
        assert d.outputTokens == 0


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


def test_session_shutdown_data_ignores_session_start_time() -> None:
    """sessionStartTime was removed; Pydantic silently drops the extra field."""
    d = SessionShutdownData.model_validate(
        {"shutdownType": "routine", "sessionStartTime": 12345}
    )
    assert d.shutdownType == "routine"
    assert not hasattr(d, "sessionStartTime")


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
# SessionEvent envelope + as_*() typed accessors
# ---------------------------------------------------------------------------


def test_session_event_start() -> None:
    ev = SessionEvent.model_validate(RAW_SESSION_START)
    assert ev.type == "session.start"
    data = ev.as_session_start()
    assert isinstance(data, SessionStartData)


def test_session_event_shutdown() -> None:
    ev = SessionEvent.model_validate(RAW_SHUTDOWN)
    assert ev.currentModel == "claude-opus-4.6-1m"
    data = ev.as_session_shutdown()
    assert isinstance(data, SessionShutdownData)


def test_session_event_assistant_message() -> None:
    ev = SessionEvent.model_validate(RAW_ASSISTANT_MESSAGE)
    data = ev.as_assistant_message()
    assert isinstance(data, AssistantMessageData)


def test_session_event_tool_exec() -> None:
    ev = SessionEvent.model_validate(RAW_TOOL_EXEC)
    data = ev.as_tool_execution()
    assert isinstance(data, ToolExecutionData)


def test_session_event_user_message() -> None:
    ev = SessionEvent.model_validate(RAW_USER_MESSAGE)
    data = ev.as_user_message()
    assert isinstance(data, UserMessageData)


def test_session_event_unknown_type() -> None:
    raw = {"type": "some.future.event", "data": {"foo": "bar"}, "id": "x"}
    ev = SessionEvent.model_validate(raw)
    # Unknown types should not crash; as_*() methods raise ValueError on mismatch
    with pytest.raises(ValueError, match="Expected session.start"):
        ev.as_session_start()


def test_as_wrong_type_raises_value_error() -> None:
    """Calling an as_*() accessor on the wrong event type raises ValueError."""
    ev = SessionEvent(type="session.start", data={"sessionId": "s1", "version": 1})
    with pytest.raises(ValueError, match="Expected session.shutdown"):
        ev.as_session_shutdown()


# ---------------------------------------------------------------------------
# SessionSummary
# ---------------------------------------------------------------------------


def test_session_summary_defaults() -> None:
    s = SessionSummary(session_id="abc")
    assert s.session_id == "abc"
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


# ---------------------------------------------------------------------------
# parse_token_int
# ---------------------------------------------------------------------------


class TestParseTokenInt:
    """Direct unit tests for the public parse_token_int() function."""

    # --- invalid / non-contributing inputs ---

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_returns_none(self, value: bool) -> None:
        """Bool inputs (subclass of int) must be rejected."""
        assert parse_token_int(value) is None

    @pytest.mark.parametrize("value", ["42", "abc", ""])
    def test_str_returns_none(self, value: str) -> None:
        """String inputs must be rejected even if numeric."""
        assert parse_token_int(value) is None

    @pytest.mark.parametrize("value", [3.14, 0.5, -1.5, 1.999])
    def test_non_whole_float_returns_none(self, value: float) -> None:
        """Non-whole floats must be rejected."""
        assert parse_token_int(value) is None

    @pytest.mark.parametrize("value", [0, -1, -100_000])
    def test_zero_or_negative_int_returns_none(self, value: int) -> None:
        """Zero and negative ints must be rejected."""
        assert parse_token_int(value) is None

    @pytest.mark.parametrize("value", [0.0, -1.0, -100.0])
    def test_zero_or_negative_float_returns_none(self, value: float) -> None:
        """Zero and negative whole floats must be rejected."""
        assert parse_token_int(value) is None

    @pytest.mark.parametrize("raw", [float("inf"), float("-inf"), float("nan")])
    def test_special_float_returns_none(self, raw: float) -> None:
        """IEEE 754 special floats must be rejected, not coerced to int."""
        assert parse_token_int(raw) is None

    def test_negative_zero_float_returns_none(self) -> None:
        """-0.0 passes is_integer() but must not count as a positive token value."""
        assert parse_token_int(-0.0) is None

    @pytest.mark.parametrize("value", [None, {}, [], object()])
    def test_other_types_return_none(self, value: object) -> None:
        """Unsupported types must be rejected."""
        assert parse_token_int(value) is None

    # --- valid / contributing inputs ---

    def test_positive_int_returned_as_is(self) -> None:
        """Positive int should be returned unchanged."""
        assert parse_token_int(42) == 42

    def test_positive_whole_float_coerced_to_int(self) -> None:
        """Positive whole float should be coerced to int."""
        result = parse_token_int(1234.0)
        assert result == 1234
        assert isinstance(result, int)

    def test_large_positive_int(self) -> None:
        """Large positive ints should be returned unchanged."""
        assert parse_token_int(1_000_000) == 1_000_000

    def test_return_type_is_int_not_float(self) -> None:
        """Whole-number float coercion must produce an int, not a float."""
        result = parse_token_int(99.0)
        assert type(result) is int


# ---------------------------------------------------------------------------
# add_to_model_metrics
# ---------------------------------------------------------------------------


class TestAddToModelMetrics:
    """Unit tests for the add_to_model_metrics helper."""

    def test_all_fields_accumulated(self) -> None:
        # Assign distinct values per field so mis-mapped fields will fail the test.
        target_requests_kwargs = {
            name: idx + 1 for idx, name in enumerate(RequestMetrics.model_fields)
        }
        source_requests_kwargs = {
            name: (idx + 1) * 10 for idx, name in enumerate(RequestMetrics.model_fields)
        }
        target_usage_kwargs = {
            name: idx + 100 for idx, name in enumerate(TokenUsage.model_fields)
        }
        source_usage_kwargs = {
            name: (idx + 1) * 1000 for idx, name in enumerate(TokenUsage.model_fields)
        }

        target = ModelMetrics(
            requests=RequestMetrics(**target_requests_kwargs),
            usage=TokenUsage(**target_usage_kwargs),
        )
        source = ModelMetrics(
            requests=RequestMetrics(**source_requests_kwargs),
            usage=TokenUsage(**source_usage_kwargs),
        )

        add_to_model_metrics(target, source)

        expected_requests = {
            name: target_requests_kwargs[name] + source_requests_kwargs[name]
            for name in RequestMetrics.model_fields
        }
        expected_usage = {
            name: target_usage_kwargs[name] + source_usage_kwargs[name]
            for name in TokenUsage.model_fields
        }

        assert target.requests.model_dump() == expected_requests
        assert target.usage.model_dump() == expected_usage

    def test_zero_source_is_identity(self) -> None:
        target = ModelMetrics(
            requests=RequestMetrics(count=5, cost=3),
            usage=TokenUsage(
                inputTokens=100,
                outputTokens=50,
                cacheReadTokens=10,
                cacheWriteTokens=5,
            ),
        )
        before = target.model_dump()
        add_to_model_metrics(target, ModelMetrics())
        assert target.model_dump() == before

    def test_source_not_mutated(self) -> None:
        target = ModelMetrics(requests=RequestMetrics(count=1))
        source = ModelMetrics(requests=RequestMetrics(count=5))
        source_before = source.model_dump()
        add_to_model_metrics(target, source)
        assert source.model_dump() == source_before  # source unchanged

    def test_accumulates_incrementally(self) -> None:
        """Multiple sequential calls accumulate correctly."""
        target = ModelMetrics()
        for _ in range(3):
            add_to_model_metrics(
                target,
                ModelMetrics(
                    requests=RequestMetrics(count=2),
                    usage=TokenUsage(outputTokens=10),
                ),
            )
        assert target.requests.count == 6
        assert target.usage.outputTokens == 30


# ---------------------------------------------------------------------------
# copy_model_metrics
# ---------------------------------------------------------------------------


class TestCopyModelMetrics:
    """Unit tests for the copy_model_metrics helper."""

    def test_returns_equal_value(self) -> None:
        """All fields are faithfully copied, including any future additions."""
        # Build kwargs with non-default values for every field so newly-added
        # fields are automatically covered by the model_dump() comparison.
        req_kwargs = {
            name: idx + 1 for idx, name in enumerate(RequestMetrics.model_fields)
        }
        usage_kwargs = {
            name: (idx + 1) * 100 for idx, name in enumerate(TokenUsage.model_fields)
        }
        mm = ModelMetrics(
            requests=RequestMetrics(**req_kwargs),
            usage=TokenUsage(**usage_kwargs),
        )
        result = copy_model_metrics(mm)
        assert result.model_dump() == mm.model_dump()

    def test_requests_copy_is_independent(self) -> None:
        """Mutating the copy's requests must not affect the original."""
        mm = ModelMetrics(requests=RequestMetrics(count=5, cost=3))
        copy = copy_model_metrics(mm)
        copy.requests.count = 999
        copy.requests.cost = 888
        assert mm.requests.count == 5
        assert mm.requests.cost == 3

    def test_usage_copy_is_independent(self) -> None:
        """Mutating the copy's usage must not affect the original."""
        mm = ModelMetrics(
            usage=TokenUsage(
                inputTokens=100,
                outputTokens=50,
                cacheReadTokens=20,
                cacheWriteTokens=10,
            ),
        )
        copy = copy_model_metrics(mm)
        copy.usage.inputTokens = 999
        copy.usage.outputTokens = 888
        copy.usage.cacheReadTokens = 777
        copy.usage.cacheWriteTokens = 666
        assert mm.usage.inputTokens == 100
        assert mm.usage.outputTokens == 50
        assert mm.usage.cacheReadTokens == 20
        assert mm.usage.cacheWriteTokens == 10

    def test_original_is_independent_of_copy_mutations(self) -> None:
        """Mutating the original must not affect the copy."""
        mm = ModelMetrics(requests=RequestMetrics(count=5))
        copy = copy_model_metrics(mm)
        mm.requests.count = 999
        assert copy.requests.count == 5

    def test_defaults_copied_correctly(self) -> None:
        """Default (zero) values are preserved in the copy for all fields."""
        mm = ModelMetrics()
        copy = copy_model_metrics(mm)
        # Ensure *all* default values (including any future fields) are preserved
        assert copy.model_dump() == mm.model_dump()


# ---------------------------------------------------------------------------
# merge_model_metrics
# ---------------------------------------------------------------------------


class TestMergeModelMetrics:
    """Unit tests for the merge_model_metrics helper."""

    def test_both_empty(self) -> None:
        assert merge_model_metrics({}, {}) == {}

    def test_empty_base(self) -> None:
        additional = {
            "model-a": ModelMetrics(
                requests=RequestMetrics(count=3, cost=2),
                usage=TokenUsage(inputTokens=100, outputTokens=50),
            )
        }
        result = merge_model_metrics({}, additional)
        assert "model-a" in result
        assert result["model-a"].requests.count == 3
        assert result["model-a"].usage.inputTokens == 100

    def test_empty_additional(self) -> None:
        base = {
            "model-a": ModelMetrics(
                requests=RequestMetrics(count=5, cost=3),
                usage=TokenUsage(outputTokens=200),
            )
        }
        result = merge_model_metrics(base, {})
        assert result["model-a"].requests.count == 5
        assert result["model-a"].usage.outputTokens == 200

    def test_overlapping_keys_accumulate(self) -> None:
        base = {
            "claude-sonnet-4": ModelMetrics(
                requests=RequestMetrics(count=3, cost=2),
                usage=TokenUsage(
                    inputTokens=100,
                    outputTokens=50,
                    cacheReadTokens=10,
                    cacheWriteTokens=5,
                ),
            )
        }
        additional = {
            "claude-sonnet-4": ModelMetrics(
                requests=RequestMetrics(count=7, cost=4),
                usage=TokenUsage(
                    inputTokens=200,
                    outputTokens=80,
                    cacheReadTokens=20,
                    cacheWriteTokens=15,
                ),
            )
        }
        result = merge_model_metrics(base, additional)
        m = result["claude-sonnet-4"]
        assert m.requests.count == 10
        assert m.requests.cost == 6
        assert m.usage.inputTokens == 300
        assert m.usage.outputTokens == 130
        assert m.usage.cacheReadTokens == 30
        assert m.usage.cacheWriteTokens == 20

    def test_disjoint_keys_kept_separate(self) -> None:
        base = {"model-a": ModelMetrics(usage=TokenUsage(outputTokens=100))}
        additional = {"model-b": ModelMetrics(usage=TokenUsage(outputTokens=200))}
        result = merge_model_metrics(base, additional)
        assert "model-a" in result and "model-b" in result
        assert result["model-a"].usage.outputTokens == 100
        assert result["model-b"].usage.outputTokens == 200

    def test_does_not_mutate_base(self) -> None:
        base = {
            "m1": ModelMetrics(
                requests=RequestMetrics(count=1, cost=1),
                usage=TokenUsage(inputTokens=10),
            )
        }
        additional = {
            "m1": ModelMetrics(
                requests=RequestMetrics(count=2, cost=2),
                usage=TokenUsage(inputTokens=20),
            )
        }
        merge_model_metrics(base, additional)
        # base must be unchanged
        assert base["m1"].requests.count == 1
        assert base["m1"].usage.inputTokens == 10

    def test_does_not_mutate_additional(self) -> None:
        base = {"m1": ModelMetrics(requests=RequestMetrics(count=1))}
        additional = {"m1": ModelMetrics(requests=RequestMetrics(count=5))}
        merge_model_metrics(base, additional)
        assert additional["m1"].requests.count == 5

    def test_no_deep_copy_regression(self) -> None:
        """merge_model_metrics must not use copy.deepcopy or model_copy."""
        base = {
            "m1": ModelMetrics(
                requests=RequestMetrics(count=10, cost=5),
                usage=TokenUsage(
                    inputTokens=100,
                    outputTokens=200,
                    cacheReadTokens=50,
                    cacheWriteTokens=10,
                ),
            )
        }
        additional = {
            "m1": ModelMetrics(
                requests=RequestMetrics(count=5, cost=2),
                usage=TokenUsage(
                    inputTokens=50,
                    outputTokens=100,
                    cacheReadTokens=25,
                    cacheWriteTokens=5,
                ),
            )
        }

        with (
            patch.object(
                ModelMetrics,
                "model_copy",
                side_effect=AssertionError(
                    "merge_model_metrics must not call model_copy"
                ),
            ),
            patch(
                "copy.deepcopy",
                side_effect=AssertionError(
                    "merge_model_metrics must not call copy.deepcopy"
                ),
            ),
        ):
            result = merge_model_metrics(base, additional)

        # Verify correctness of merge output
        assert result["m1"].requests.count == 15
        assert result["m1"].requests.cost == 7
        assert result["m1"].usage.inputTokens == 150
        assert result["m1"].usage.outputTokens == 300


# ---------------------------------------------------------------------------
# Typed accessor methods (as_*)
# ---------------------------------------------------------------------------


class TestAsSessionStart:
    """Tests for SessionEvent.as_session_start()."""

    def test_happy_path(self) -> None:
        ev = SessionEvent.model_validate(RAW_SESSION_START)
        data = ev.as_session_start()
        assert isinstance(data, SessionStartData)
        assert data.sessionId == "0faecbdf-b889-4bca-a51a-5254f5488cb6"

    def test_wrong_event_type_raises(self) -> None:
        ev = SessionEvent.model_validate(RAW_SHUTDOWN)
        with pytest.raises(ValueError, match="session.start"):
            ev.as_session_start()

    def test_invalid_data_raises_validation_error(self) -> None:
        ev = SessionEvent(type=EventType.SESSION_START, data={})
        with pytest.raises(ValidationError):
            ev.as_session_start()


class TestAsSessionShutdown:
    """Tests for SessionEvent.as_session_shutdown()."""

    def test_happy_path(self) -> None:
        ev = SessionEvent.model_validate(RAW_SHUTDOWN)
        data = ev.as_session_shutdown()
        assert isinstance(data, SessionShutdownData)
        assert data.totalPremiumRequests == 24

    def test_wrong_event_type_raises(self) -> None:
        ev = SessionEvent.model_validate(RAW_SESSION_START)
        with pytest.raises(ValueError, match="session.shutdown"):
            ev.as_session_shutdown()

    def test_invalid_data_raises_validation_error(self) -> None:
        ev = SessionEvent(
            type=EventType.SESSION_SHUTDOWN,
            data={"totalPremiumRequests": "not-an-int"},
        )
        with pytest.raises(ValidationError):
            ev.as_session_shutdown()


class TestAsAssistantMessage:
    """Tests for SessionEvent.as_assistant_message()."""

    def test_happy_path(self) -> None:
        ev = SessionEvent.model_validate(RAW_ASSISTANT_MESSAGE)
        data = ev.as_assistant_message()
        assert isinstance(data, AssistantMessageData)
        assert data.outputTokens == 373

    def test_wrong_event_type_raises(self) -> None:
        ev = SessionEvent.model_validate(RAW_USER_MESSAGE)
        with pytest.raises(ValueError, match="assistant.message"):
            ev.as_assistant_message()

    def test_invalid_data_raises_validation_error(self) -> None:
        ev = SessionEvent(
            type=EventType.ASSISTANT_MESSAGE,
            data={"outputTokens": [1, 2, 3]},
        )
        with pytest.raises(ValidationError):
            ev.as_assistant_message()


class TestAsUserMessage:
    """Tests for SessionEvent.as_user_message()."""

    def test_happy_path(self) -> None:
        ev = SessionEvent.model_validate(RAW_USER_MESSAGE)
        data = ev.as_user_message()
        assert isinstance(data, UserMessageData)
        assert data.content == "hey there"

    def test_wrong_event_type_raises(self) -> None:
        ev = SessionEvent.model_validate(RAW_ASSISTANT_MESSAGE)
        with pytest.raises(ValueError, match="user.message"):
            ev.as_user_message()

    def test_invalid_data_raises_validation_error(self) -> None:
        ev = SessionEvent(
            type=EventType.USER_MESSAGE,
            data={"attachments": 99},
        )
        with pytest.raises(ValidationError):
            ev.as_user_message()


class TestAsToolExecution:
    """Tests for SessionEvent.as_tool_execution()."""

    def test_happy_path(self) -> None:
        ev = SessionEvent.model_validate(RAW_TOOL_EXEC)
        data = ev.as_tool_execution()
        assert isinstance(data, ToolExecutionData)
        assert data.success is True
        assert data.model == "claude-opus-4.6-1m"

    def test_wrong_event_type_raises(self) -> None:
        ev = SessionEvent.model_validate(RAW_SESSION_START)
        with pytest.raises(ValueError, match="tool.execution_complete"):
            ev.as_tool_execution()

    def test_invalid_data_raises_validation_error(self) -> None:
        ev = SessionEvent(
            type=EventType.TOOL_EXECUTION_COMPLETE,
            data={"success": "maybe"},
        )
        with pytest.raises(ValidationError):
            ev.as_tool_execution()


# ---------------------------------------------------------------------------
# Shared datetime utilities (EPOCH, ensure_aware, ensure_aware_opt)
# ---------------------------------------------------------------------------


class TestEpochSentinel:
    """Tests for the EPOCH constant."""

    def test_is_aware(self) -> None:
        assert EPOCH.tzinfo is not None

    def test_is_utc(self) -> None:
        assert EPOCH.tzinfo == UTC

    def test_is_datetime_min(self) -> None:
        assert EPOCH.replace(tzinfo=None) == datetime.min


class TestEnsureAware:
    """Tests for ensure_aware (non-None variant)."""

    def test_naive_gets_utc(self) -> None:
        naive = datetime(2026, 1, 15, 12, 0, 0)
        result = ensure_aware(naive)
        assert result.tzinfo == UTC
        assert result.replace(tzinfo=None) == naive

    def test_already_aware_unchanged(self) -> None:
        aware = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = ensure_aware(aware)
        assert result is aware

    def test_preserves_values(self) -> None:
        naive = datetime(2026, 6, 15, 8, 30, 45, 123456)
        result = ensure_aware(naive)
        assert result.year == 2026
        assert result.month == 6
        assert result.microsecond == 123456
        assert result.tzinfo == UTC


class TestEnsureAwareOpt:
    """Tests for ensure_aware_opt (None-safe variant)."""

    def test_none_returns_none(self) -> None:
        assert ensure_aware_opt(None) is None

    def test_naive_gets_utc(self) -> None:
        naive = datetime(2026, 1, 15, 12, 0, 0)
        result = ensure_aware_opt(naive)
        assert result is not None
        assert result.tzinfo == UTC

    def test_already_aware_unchanged(self) -> None:
        aware = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = ensure_aware_opt(aware)
        assert result is aware


# ---------------------------------------------------------------------------
# Issue #446 — Cleanup 2: session_sort_key
# ---------------------------------------------------------------------------


class TestSessionSortKey:
    """Tests for the session_sort_key helper."""

    def test_importable_from_models(self) -> None:
        """session_sort_key is importable from copilot_usage.models."""
        from copilot_usage.models import session_sort_key as fn

        assert callable(fn)

    def test_returns_aware_start_time(self) -> None:
        """session_sort_key returns the aware start_time when set."""
        from copilot_usage.models import session_sort_key

        t = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        session = SessionSummary(session_id="s", start_time=t)
        assert session_sort_key(session) == t

    def test_naive_start_time_becomes_aware(self) -> None:
        """session_sort_key converts a naive start_time to aware."""
        from copilot_usage.models import session_sort_key

        naive = datetime(2026, 6, 15, 12, 0, 0)
        session = SessionSummary(session_id="s", start_time=naive)
        result = session_sort_key(session)
        assert result.tzinfo == UTC
        assert result.replace(tzinfo=None) == naive

    def test_none_start_time_returns_epoch(self) -> None:
        """session_sort_key returns EPOCH when start_time is None."""
        from copilot_usage.models import session_sort_key

        session = SessionSummary(session_id="s", start_time=None)
        assert session_sort_key(session) == EPOCH


# ---------------------------------------------------------------------------
# Issue #460 — Validate active_model_calls <= model_calls
# ---------------------------------------------------------------------------


class TestSessionSummaryCallCountInvariant:
    """Tests for the model_calls >= active_model_calls invariant."""

    def test_rejects_active_calls_exceeding_total(self) -> None:
        """SessionSummary must reject active_model_calls > model_calls."""
        with pytest.raises(ValidationError):
            SessionSummary(
                session_id="inv",
                model_calls=3,
                active_model_calls=5,
            )

    def test_accepts_active_calls_equal_to_total(self) -> None:
        """SessionSummary allows active_model_calls == model_calls."""
        s = SessionSummary(
            session_id="eq",
            model_calls=5,
            active_model_calls=5,
        )
        assert s.active_model_calls == s.model_calls

    def test_accepts_active_calls_less_than_total(self) -> None:
        """SessionSummary allows active_model_calls < model_calls."""
        s = SessionSummary(
            session_id="lt",
            model_calls=10,
            active_model_calls=3,
        )
        assert s.active_model_calls < s.model_calls

    def test_accepts_zero_calls(self) -> None:
        """SessionSummary allows both counts at zero (defaults)."""
        s = SessionSummary(session_id="zero")
        assert s.model_calls == 0
        assert s.active_model_calls == 0


class TestSessionSummaryUserMessageInvariant:
    """Tests for the user_messages >= active_user_messages invariant."""

    def test_rejects_active_messages_exceeding_total(self) -> None:
        """SessionSummary must reject active_user_messages > user_messages."""
        with pytest.raises(ValidationError):
            SessionSummary(
                session_id="inv",
                user_messages=3,
                active_user_messages=5,
            )

    def test_accepts_active_messages_equal_to_total(self) -> None:
        """SessionSummary allows active_user_messages == user_messages."""
        s = SessionSummary(
            session_id="eq",
            user_messages=5,
            active_user_messages=5,
        )
        assert s.active_user_messages == s.user_messages

    def test_accepts_active_messages_less_than_total(self) -> None:
        """SessionSummary allows active_user_messages < user_messages."""
        s = SessionSummary(
            session_id="lt",
            user_messages=10,
            active_user_messages=3,
        )
        assert s.active_user_messages < s.user_messages

    def test_accepts_zero_messages(self) -> None:
        """SessionSummary allows both counts at zero (defaults)."""
        s = SessionSummary(session_id="zero")
        assert s.user_messages == 0
        assert s.active_user_messages == 0


# ---------------------------------------------------------------------------
# shutdown_output_tokens
# ---------------------------------------------------------------------------


class TestShutdownOutputTokens:
    """Direct unit tests for shutdown_output_tokens()."""

    def test_sums_model_metrics_only(self) -> None:
        """Active tokens are excluded — only model_metrics outputTokens count."""
        session = SessionSummary(
            session_id="s1",
            model_metrics={
                "model-a": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                    requests=RequestMetrics(count=1),
                ),
            },
            active_output_tokens=999,
            model_calls=1,
        )
        assert shutdown_output_tokens(session) == 100

    def test_empty_metrics(self) -> None:
        """Empty model_metrics returns 0 regardless of active_output_tokens."""
        session = SessionSummary(
            session_id="s2",
            model_metrics={},
            active_output_tokens=50,
        )
        assert shutdown_output_tokens(session) == 0

    def test_multiple_models(self) -> None:
        """Output tokens from multiple models are summed."""
        session = SessionSummary(
            session_id="s3",
            model_metrics={
                "model-a": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                    requests=RequestMetrics(count=1),
                ),
                "model-b": ModelMetrics(
                    usage=TokenUsage(outputTokens=200),
                    requests=RequestMetrics(count=2),
                ),
            },
            model_calls=3,
        )
        assert shutdown_output_tokens(session) == 300


# ---------------------------------------------------------------------------
# total_output_tokens
# ---------------------------------------------------------------------------


class TestTotalOutputTokens:
    """Direct unit tests for total_output_tokens() — all four logical cases."""

    def test_case_a_resumed_with_shutdown_metrics(self) -> None:
        """Resumed session with shutdown metrics: baseline + active."""
        session = SessionSummary(
            session_id="case-a",
            model_metrics={
                "m": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                    requests=RequestMetrics(count=1),
                ),
            },
            has_shutdown_metrics=True,
            active_output_tokens=50,
            last_resume_time=datetime(2026, 1, 1, tzinfo=UTC),
            model_calls=2,
            active_model_calls=1,
        )
        assert total_output_tokens(session) == 150

    def test_case_b_active_no_shutdown_metrics(self) -> None:
        """Active-period stats True but no shutdown metrics: only baseline."""
        session = SessionSummary(
            session_id="case-b",
            model_metrics={
                "m": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                    requests=RequestMetrics(count=1),
                ),
            },
            has_shutdown_metrics=False,
            active_output_tokens=50,
            active_user_messages=1,
            user_messages=1,
            model_calls=1,
        )
        assert total_output_tokens(session) == 100

    def test_case_c_shutdown_no_active_stats(self) -> None:
        """Shutdown metrics but no active-period indicators: only baseline."""
        session = SessionSummary(
            session_id="case-c",
            model_metrics={
                "m": ModelMetrics(
                    usage=TokenUsage(outputTokens=100),
                    requests=RequestMetrics(count=1),
                ),
            },
            has_shutdown_metrics=True,
            active_output_tokens=0,
            last_resume_time=None,
            active_user_messages=0,
            active_model_calls=0,
            model_calls=1,
        )
        assert total_output_tokens(session) == 100

    def test_case_d_pure_active_empty_metrics(self) -> None:
        """Empty model_metrics: only active tokens (no double-count risk)."""
        session = SessionSummary(
            session_id="case-d",
            model_metrics={},
            has_shutdown_metrics=False,
            active_output_tokens=75,
        )
        assert total_output_tokens(session) == 75


# ---------------------------------------------------------------------------
# has_active_period_stats
# ---------------------------------------------------------------------------


class TestHasActivePeriodStats:
    """Direct unit tests for has_active_period_stats()."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("last_resume_time", datetime(2026, 1, 1, tzinfo=UTC)),
            ("active_user_messages", 1),
            ("active_output_tokens", 1),
            ("active_model_calls", 1),
        ],
        ids=[
            "last_resume_time",
            "active_user_messages",
            "active_output_tokens",
            "active_model_calls",
        ],
    )
    def test_each_condition_sufficient(self, field: str, value: object) -> None:
        """Each OR condition alone must be sufficient to return True."""
        kwargs: dict[str, object] = {
            "session_id": "test",
            "last_resume_time": None,
            "active_user_messages": 0,
            "active_output_tokens": 0,
            "active_model_calls": 0,
            field: value,
        }
        # active counters must be <= their totals
        if field == "active_model_calls":
            kwargs["model_calls"] = value
        if field == "active_user_messages":
            kwargs["user_messages"] = value
        session = SessionSummary(**kwargs)  # type: ignore[arg-type]
        assert has_active_period_stats(session) is True

    def test_all_zero_is_false(self) -> None:
        """All zero/None fields must return False."""
        session = SessionSummary(
            session_id="zero",
            last_resume_time=None,
            active_user_messages=0,
            active_output_tokens=0,
            active_model_calls=0,
        )
        assert has_active_period_stats(session) is False
