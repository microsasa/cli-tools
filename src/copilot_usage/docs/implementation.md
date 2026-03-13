# Implementation Details

Deep-dive into how `copilot-usage` works under the hood. For the high-level architecture and data flow diagram, see [architecture.md](architecture.md). This document is for developers maintaining the code.

---

## Session Data Model

### Storage location

Copilot CLI stores session data at:

```
~/.copilot/session-state/{uuid}/
├── events.jsonl      # Append-only event log (one JSON object per line)
└── plan.md           # Optional — session name extracted from first heading
```

The `{uuid}` is a full UUID assigned by the Copilot CLI at session creation.

### Event envelope

Every line in `events.jsonl` is a JSON object conforming to the `SessionEvent` model (`models.py:177–207`):

| Field          | Type               | Description                                                  |
|----------------|--------------------|--------------------------------------------------------------|
| `type`         | `str`              | Event type identifier (e.g. `"session.start"`)               |
| `data`         | `dict[str, object]` | Event-specific payload — parsed on demand via `parse_data()` |
| `id`           | `str \| None`       | Event UUID                                                   |
| `timestamp`    | `datetime \| None`  | ISO 8601 timestamp                                           |
| `parentId`     | `str \| None`       | Links tool completions to their turn                         |
| `currentModel` | `str \| None`       | Top-level model field (present on shutdown events)           |

### Known event types

Defined in `EventType` enum (`models.py:22–37`):

| Event type                        | Data class              | Key fields                                                         |
|-----------------------------------|-------------------------|--------------------------------------------------------------------|
| `session.start`                   | `SessionStartData`      | `sessionId`, `startTime`, `context.cwd`                            |
| `session.shutdown`                | `SessionShutdownData`   | `totalPremiumRequests`, `totalApiDurationMs`, `modelMetrics`, `codeChanges` |
| `session.resume`                  | `GenericEventData`      | No typed model — only `timestamp` is used                          |
| `session.error`                   | `GenericEventData`      | Catch-all                                                          |
| `session.plan_changed`            | `GenericEventData`      | Catch-all                                                          |
| `session.workspace_file_changed`  | `GenericEventData`      | Catch-all                                                          |
| `assistant.message`               | `AssistantMessageData`  | `outputTokens`, `content`, `toolRequests`                          |
| `assistant.turn_start`            | `GenericEventData`      | Counted for model calls — no typed payload needed                  |
| `assistant.turn_end`              | `GenericEventData`      | Catch-all                                                          |
| `tool.execution_start`            | `GenericEventData`      | Catch-all                                                          |
| `tool.execution_complete`         | `ToolExecutionData`     | `model`, `success`, `toolTelemetry.properties.tool_name`           |
| `user.message`                    | `UserMessageData`       | `content`, `attachments`                                           |
| `abort`                           | `GenericEventData`      | Catch-all                                                          |

Typed dispatch happens in `SessionEvent.parse_data()` (`models.py:193–207`) via `match`/`case`. Unknown types fall through to `GenericEventData(extra="allow")`, which accepts any JSON fields without validation errors.

### SessionSummary fields

`SessionSummary` (`models.py:229–256`) is a computed aggregate — never parsed directly from JSON. Built by `build_session_summary()`.

| Field                    | Type                        | How it's populated                                                                  |
|--------------------------|-----------------------------|-------------------------------------------------------------------------------------|
| `session_id`             | `str`                       | From `session.start` → `data.sessionId`                                             |
| `start_time`             | `datetime \| None`           | From `session.start` → `data.startTime`                                             |
| `end_time`               | `datetime \| None`           | Timestamp of last `session.shutdown`; `None` if resumed                              |
| `name`                   | `str \| None`                | Extracted from `plan.md` first heading (see Session Name Resolution)                |
| `cwd`                    | `str \| None`                | From `session.start` → `data.context.cwd`                                           |
| `model`                  | `str \| None`                | Last model seen in shutdowns, or inferred (see below)                               |
| `total_premium_requests` | `int`                       | Sum of `totalPremiumRequests` across all shutdown events                             |
| `total_api_duration_ms`  | `int`                       | Sum of `totalApiDurationMs` across all shutdown events                               |
| `model_metrics`          | `dict[str, ModelMetrics]`   | Merged from all shutdown events (same model → sum values)                           |
| `code_changes`           | `CodeChanges \| None`        | From the last shutdown event that has it                                             |
| `model_calls`            | `int`                       | Count of `assistant.turn_start` events across entire session                        |
| `user_messages`          | `int`                       | Count of `user.message` events across entire session                                |
| `is_active`              | `bool`                      | `True` if no shutdowns, or if events exist after last shutdown                      |
| `last_resume_time`       | `datetime \| None`           | Timestamp of `session.resume` event (if any, after last shutdown)                   |
| `events_path`            | `Path \| None`               | Set by `get_all_sessions()` after building — not from events                        |
| `active_model_calls`     | `int`                       | `assistant.turn_start` count after last shutdown (resumed sessions only)            |
| `active_user_messages`   | `int`                       | `user.message` count after last shutdown (resumed sessions only)                    |
| `active_output_tokens`   | `int`                       | Sum of `outputTokens` from `assistant.message` events after last shutdown           |

---

## Shutdown Event Processing

This is the most critical logic in the codebase. Getting it wrong means incorrect premium request counts and token totals.

### Key insight: shutdown events are NOT cumulative

Each `session.shutdown` event represents the metrics for **one lifecycle** (start → shutdown). If a session is resumed and shut down again, you get two separate shutdown events with independent metric snapshots. To get the session's total, you must **sum across all shutdowns**.

### The code path

In `build_session_summary()` (`parser.py:151–361`):

**Phase 1 — Collect all shutdowns** (`parser.py:207–218`):
```python
all_shutdowns: list[tuple[int, SessionShutdownData, str | None]] = []
# ...
elif ev.type == EventType.SESSION_SHUTDOWN:
    # ... validate, extract data ...
    all_shutdowns.append((idx, data, current_model))
```
Each tuple stores `(event_index, shutdown_data, resolved_model)`.

**Phase 2 — Sum across all shutdowns** (`parser.py:268–301`):
```python
for _idx, sd, _m in all_shutdowns:
    total_premium += sd.totalPremiumRequests
    total_api_duration += sd.totalApiDurationMs
    # ... merge model_metrics ...
```

### Model metrics merging

When two shutdowns reference the **same model**, their `ModelMetrics` are summed field-by-field (`parser.py:281–298`):

```python
if model_name in merged_metrics:
    existing = merged_metrics[model_name]
    merged_metrics[model_name] = ModelMetrics(
        requests=RequestMetrics(
            count=existing.requests.count + metrics.requests.count,
            cost=existing.requests.cost + metrics.requests.cost,
        ),
        usage=TokenUsage(
            inputTokens=existing.usage.inputTokens + metrics.usage.inputTokens,
            outputTokens=existing.usage.outputTokens + metrics.usage.outputTokens,
            cacheReadTokens=existing.usage.cacheReadTokens + metrics.usage.cacheReadTokens,
            cacheWriteTokens=existing.usage.cacheWriteTokens + metrics.usage.cacheWriteTokens,
        ),
    )
```

When they reference **different models**, separate entries are kept in the `merged_metrics` dict.

### Model resolution for shutdowns

The model name for a shutdown is resolved in priority order (`parser.py:213–216`):
1. `currentModel` from the event envelope (top-level field)
2. `currentModel` from the shutdown data payload
3. Inferred from `modelMetrics` keys — if one key, use it; if multiple, pick the one with highest `requests.count` (`_infer_model_from_metrics()`, `parser.py:33–43`)

---

## Active vs Historical Session Detection

### Three session states

| State               | Shutdowns? | Events after last shutdown? | `is_active` | `end_time`  |
|---------------------|------------|----------------------------|-------------|-------------|
| **Completed**       | ≥1         | No                         | `False`     | Last shutdown timestamp |
| **Resumed (active)**| ≥1         | Yes                        | `True`      | `None`      |
| **Pure active**     | 0          | N/A                        | `True`      | `None`      |

### Detection logic

After collecting all shutdowns, `build_session_summary()` scans events after the last shutdown index (`parser.py:238–265`):

```python
_RESUME_INDICATOR_TYPES: set[str] = {
    EventType.SESSION_RESUME,
    EventType.USER_MESSAGE,
    EventType.ASSISTANT_MESSAGE,
}

last_shutdown_idx = all_shutdowns[-1][0] if all_shutdowns else -1

if all_shutdowns and last_shutdown_idx >= 0:
    for ev in events[last_shutdown_idx + 1:]:
        if ev.type in _RESUME_INDICATOR_TYPES:
            session_resumed = True
        # ... count post-shutdown tokens, messages, model calls ...
```

The presence of **any** `session.resume`, `user.message`, or `assistant.message` event after the last shutdown triggers `session_resumed = True`.

### `last_resume_time`

Populated from the `timestamp` of the `session.resume` event after the last shutdown (`parser.py:256–257`). Used by the report layer to calculate "Running Time" — showing duration since resume, not since original start.

### Resumed session summary construction

For resumed sessions (`parser.py:302–320`):
- `end_time` is set to `None` (not the last shutdown timestamp)
- `model_metrics` contain the **merged shutdown data** (historical baseline)
- `active_model_calls`, `active_user_messages`, `active_output_tokens` contain **only** post-shutdown counts
- `model_calls` and `user_messages` are the **total** counts across the entire session

---

## Premium Request Tracking

### Where premium requests come from

The **only** source of premium request counts is `SessionShutdownData.totalPremiumRequests` (`models.py:115`). This is a pre-computed value from the Copilot CLI — not something we calculate.

For sessions with multiple shutdowns, the total is summed: `total_premium += sd.totalPremiumRequests` (`parser.py:276`).

### Active sessions show "—"

If a session has no shutdown data (pure active), `total_premium_requests` is `0`, and the report displays "—" (`report.py:677–680`):

```python
if s.total_premium_requests > 0:
    pr_display = str(s.total_premium_requests)
else:
    pr_display = "—"
```

### Why estimation was removed

Early versions attempted to estimate premium requests using `multiplier × request_count`. This was removed (see changelog: "refactor: remove multiplier estimation") because:

1. **Multipliers don't map 1:1 to API calls.** A single `assistant.turn_start` may result in multiple API calls (tool use loops, retries), or a single API call may be counted as multiple premium requests at the billing layer.
2. **Shutdown data is authoritative.** The `totalPremiumRequests` field reflects actual billing, making estimation redundant for completed sessions.
3. **Active sessions have no reliable estimate.** Without shutdown data, any number would be misleading.

### Total includes resumed sessions

The grand total premium requests across all sessions includes resumed sessions that have shutdown data — their `total_premium_requests` reflects the sum of all their shutdown cycles.

---

## Interactive Loop Architecture

Defined in `_interactive_loop()` (`cli.py:155–250`).

### Non-blocking input with `select()`

The loop uses `select.select()` on stdin (`cli.py:103–108`) with a 500ms timeout:

```python
def _read_line_nonblocking(timeout: float = 0.5) -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    return None
```

This is **Unix only** — `select()` on stdin doesn't work on Windows. The 500ms timeout allows the main loop to check for file-change events between input polls.

### Fallback to blocking `input()`

If `select()` raises `ValueError` or `OSError` (e.g. stdin is piped, not a real TTY, or during testing), the loop falls back to blocking `input()` (`cli.py:193–199`):

```python
except (ValueError, OSError):
    try:
        line = input().strip()
    except (EOFError, KeyboardInterrupt):
        break
```

### Watchdog file observer

A `watchdog.Observer` watches `~/.copilot/session-state/` recursively for **any** filesystem change — new session directories, lockfile creation/deletion, `events.jsonl` writes, etc. (`cli.py:129–141`):

```python
observer = Observer()
observer.schedule(handler, str(session_path), recursive=True)
observer.daemon = True
observer.start()
```

The observer is optional — if `watchdog` is not installed, the import fails silently and `observer` stays `None` (`cli.py:131`).

### `_FileChangeHandler` with 2-second debounce

`_FileChangeHandler` (`cli.py:111–123`) triggers on any filesystem event in the session-state tree and enforces a 2-second debounce using `time.monotonic()`:

```python
def dispatch(self, event):
    now = time.monotonic()
    if now - self._last_trigger > 2.0:
        self._last_trigger = now
        self._change_event.set()
```

Each trigger causes a full `get_all_sessions()` re-read, picking up new sessions, closed sessions, and updated event data. The debounce prevents rapid redraws during high-frequency event writes (e.g. tool execution loops producing many events per second). Manual refresh (`r`) is still available as a fallback.

### View state machine

The interactive loop maintains a simple view state (`cli.py:166–168`):

```
view: str = "home" | "detail" | "cost"
```

Transitions:
- **home → detail**: User enters a session number
- **home → cost**: User enters `c`
- **detail → home**: User presses Enter
- **cost → home**: User presses Enter
- **home → home**: User enters `r` (refresh)

On any view transition, `get_all_sessions()` is re-called to pick up new data. Auto-refresh via watchdog also triggers re-render of the current view.

---

## Cost View Rendering

Defined in `render_cost_view()` (`report.py:898–982`).

### Per-model rows within sessions

Each session's `model_metrics` dict is iterated to produce one table row per model (`report.py:931–947`):

```python
for model_name in sorted(s.model_metrics):
    mm = s.model_metrics[model_name]
    table.add_row(name, model_name, str(mm.requests.count), ...)
    name = ""                   # blank after first row
    model_calls_display = ""    # shown only on first model row
```

The session name and model calls are shown **only on the first model row** — subsequent model rows for the same session have blank session/model-calls columns to avoid visual repetition.

### "↳ Since last shutdown" rows

For active (resumed) sessions, an extra row is appended (`report.py:960–970`):

```python
if s.is_active:
    table.add_row(
        "  ↳ Since last shutdown",
        s.model or "—",
        "N/A",          # no premium requests available
        "N/A",          # no premium cost available
        str(s.active_model_calls),
        format_tokens(s.active_output_tokens),
    )
```

Premium columns show `N/A` because there's no shutdown data for the active period.

### Historical vs active sections in full summary

`render_full_summary()` (`report.py:871–891`) renders two distinct sections:

1. **Historical Data** (`_render_historical_section`, `report.py:725–822`): Sessions with shutdown data. Includes sessions where `total_premium_requests > 0` OR sessions that have `model_metrics` and are **not** active.
2. **Active Sessions** (`_render_active_section`, `report.py:825–868`): Sessions where `is_active == True`. Shows `active_model_calls`, `active_user_messages`, `active_output_tokens`, and running time.

Resumed sessions appear in **both** sections — historical section for their shutdown data, active section for their post-shutdown activity.

### Grand total row

After all session rows, a section divider and grand total row is added (`report.py:972–980`). Grand totals accumulate `requests.count`, `requests.cost`, `model_calls`, and `output_tokens` from both shutdown metrics and active periods.

---

## Edge Cases & Error Handling

### Corrupt/malformed JSON lines

`parse_events()` (`parser.py:98–124`) handles two failure modes per line:

1. **JSON decode failure**: `json.JSONDecodeError` → logged via `loguru.warning`, line skipped
2. **Pydantic validation failure**: `ValidationError` → logged with error count, line skipped

Valid lines in the same file are still processed. A file with 99 valid lines and 1 corrupt line produces 99 events.

### Empty sessions

A session directory with just a `session.start` event (and nothing else) produces a valid `SessionSummary` with `is_active=True`, `model_calls=0`, `user_messages=0`, `total_premium_requests=0`.

Sessions where `parse_events()` returns an empty list (no valid events at all) are skipped entirely by `get_all_sessions()` (`parser.py:384`).

### TOCTOU races

Two levels of protection against files disappearing between discovery and read:

1. **Discovery**: `_safe_mtime()` (`parser.py:64–69`) returns `0.0` instead of crashing when a file vanishes between `glob()` and `stat()`.
2. **Parsing**: `get_all_sessions()` (`parser.py:377–385`) catches `FileNotFoundError` and `OSError` during `parse_events()` and skips the session with a warning.

### Unknown event types

Events with types not in `EventType` still parse successfully — `SessionEvent.type` is `str`, not the enum. `parse_data()` returns `GenericEventData(extra="allow")` for unknown types, accepting any fields.

In `build_session_summary()`, unknown types are simply ignored — the `for idx, ev in enumerate(events)` loop only has branches for known types, with no `else` clause needed.

### Unknown models in pricing

`lookup_model_pricing()` (`pricing.py:106–146`) has a three-tier resolution:
1. **Exact match** in `KNOWN_PRICING`
2. **Partial match** — `model_name.startswith(key)` or `key.startswith(model_name)`, longest match wins
3. **Fallback** — returns 1× standard multiplier, emits `UserWarning`

---

## Session Name Resolution

Implemented in `_extract_session_name()` (`parser.py:132–143`).

### Resolution order

1. **Primary**: Read `plan.md` from the session directory. If it exists and the first line starts with `# `, extract the heading text after `# `.
2. **Fallback**: The report layer uses `s.name or s.session_id[:12]` — showing the first 12 characters of the session UUID.

### How it's called

`build_session_summary()` calls `_extract_session_name(session_dir)` when `session_dir` is provided (`parser.py:235`). The `session_dir` parameter is passed by `get_all_sessions()` as `events_path.parent` (`parser.py:385`).

---

## Model Multiplier Reference

From `pricing.py:68–90` — `_RAW_MULTIPLIERS` dict:

| Model                  | Multiplier | Tier     |
|------------------------|------------|----------|
| `claude-sonnet-4.6`    | 1×         | Standard |
| `claude-sonnet-4.5`    | 1×         | Standard |
| `claude-sonnet-4`      | 1×         | Standard |
| `claude-opus-4.6`      | 3×         | Premium  |
| `claude-opus-4.6-1m`   | 6×         | Premium  |
| `claude-opus-4.5`      | 3×         | Premium  |
| `claude-haiku-4.5`     | 0.33×      | Light    |
| `gpt-5.4`              | 1×         | Standard |
| `gpt-5.2`              | 1×         | Standard |
| `gpt-5.1`              | 1×         | Standard |
| `gpt-5.1-codex`        | 1×         | Standard |
| `gpt-5.2-codex`        | 1×         | Standard |
| `gpt-5.3-codex`        | 1×         | Standard |
| `gpt-5.1-codex-max`    | 1×         | Standard |
| `gpt-5.1-codex-mini`   | 0.33×      | Light    |
| `gpt-5-mini`           | 0×         | Light    |
| `gpt-4.1`              | 0×         | Light    |
| `gemini-3-pro-preview` | 1×         | Standard |

Tier is derived from the multiplier (`pricing.py:60–65`): ≥3.0 → Premium, <1.0 → Light, otherwise Standard.

**Important:** `pricing.py` is **reference data only**. The multipliers are not used in any runtime calculations — premium request counts come exclusively from `session.shutdown` events. The pricing module exists for `categorize_model()` (tier lookup) and potential future use.

### Model resolution for active sessions

When no shutdown data exists, the model is resolved in `build_session_summary()` (`parser.py:322–339`):

1. Scan `tool.execution_complete` events for a `model` field (`parser.py:324–330`)
2. Fall back to `~/.copilot/config.json` → `data.model` field (`_read_config_model()`, `parser.py:46–56`)
