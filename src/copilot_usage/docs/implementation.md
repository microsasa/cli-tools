# Implementation Details

> **Maintainers:** do not add file:line-number citations to this document — they go stale. Reference symbols by name only.

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

Every line in `events.jsonl` is a JSON object conforming to the `SessionEvent` model (in `models.py`):

| Field          | Type               | Description                                                  |
|----------------|--------------------|--------------------------------------------------------------|
| `type`         | `str`              | Event type identifier (e.g. `"session.start"`)               |
| `data`         | `dict[str, object]` | Event-specific payload — parsed on demand via typed `as_*()` accessors |
| `id`           | `str \| None`       | Event UUID                                                   |
| `timestamp`    | `datetime \| None`  | ISO 8601 timestamp                                           |
| `parentId`     | `str \| None`       | Links tool completions to their turn                         |
| `currentModel` | `str \| None`       | Top-level model field (present on shutdown events)           |

### Known event types

Defined in `EventType` enum (in `models.py`):

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

Typed dispatch uses the `as_*()` accessors on `SessionEvent` (e.g. `as_session_start()`, `as_assistant_message()`). Each accessor validates that `self.type` matches the expected `EventType` and returns the corresponding typed data model for known event types. Unknown event types may still validate as the base `SessionEvent` envelope, but production code skips them rather than automatically parsing them into `GenericEventData`. `GenericEventData(extra="allow")` remains available only for optional, best-effort payload validation when a caller explicitly chooses to use it.

### SessionSummary fields

`SessionSummary` (in `models.py`) is a computed aggregate — never parsed directly from JSON. Built by `build_session_summary()`.

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
| `code_changes`           | `CodeChanges \| None`        | Aggregated from all shutdown cycles: `linesAdded`/`linesRemoved` are summed, `filesModified` is de-duplicated across all cycles. `None` when no shutdown carried `codeChanges`. |
| `model_calls`            | `int`                       | Count of `assistant.turn_start` events across entire session                        |
| `user_messages`          | `int`                       | Count of `user.message` events across entire session                                |
| `is_active`              | `bool`                      | `True` if no shutdowns, or if events exist after last shutdown                      |
| `has_shutdown_metrics`   | `bool`                      | `True` when at least one shutdown event produced non-empty `modelMetrics`; set to `bool(merged_metrics)` after merging all shutdowns |
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

`build_session_summary()` (in `parser.py`) delegates to four focused helpers:

**`_first_pass(events)` → `_FirstPassResult`** — single pass collecting all shutdowns:
```python
all_shutdowns: list[tuple[int, SessionShutdownData]] = []
# ...
elif ev.type == EventType.SESSION_SHUTDOWN:
    # ... validate, extract data ...
    all_shutdowns.append((idx, data))
```
Each tuple stores `(event_index, shutdown_data)`. The model is resolved inline and stored on `_FirstPassResult.model`.

**`_build_completed_summary(fp, name, resume)` → `SessionSummary`** — sums across all shutdowns:
```python
for _idx, sd in fp.all_shutdowns:
    total_premium += sd.totalPremiumRequests
    total_api_duration += sd.totalApiDurationMs
    # ... merge model_metrics ...
```

### Model metrics merging

When two shutdowns reference the **same model**, their `ModelMetrics` are summed field-by-field. Accumulation is done **in-place** using `add_to_model_metrics()` and `copy_model_metrics()` (in `models.py`):

```python
for model_name, mm in sd.modelMetrics.items():
    if model_name in merged_metrics:
        add_to_model_metrics(merged_metrics[model_name], mm)
    else:
        merged_metrics[model_name] = copy_model_metrics(mm)
```

Each model is copied exactly once (on first encounter) and accumulated in-place thereafter, yielding O(M) `copy_model_metrics` calls regardless of the number of shutdown cycles K. When two shutdowns reference **different models**, separate entries are kept in the result dict.

### Model resolution for shutdowns

The model name for a shutdown is resolved in priority order (in `parser.py`):
1. `currentModel` from the event envelope (top-level field)
2. `currentModel` from the shutdown data payload
3. Inferred from `modelMetrics` keys — if one key, use it; if multiple, pick the one with highest `requests.count` (`_infer_model_from_metrics()` in `parser.py`)

---

## Active vs Historical Session Detection

### Three session states

| State               | Shutdowns? | Events after last shutdown? | `is_active` | `end_time`  |
|---------------------|------------|----------------------------|-------------|-------------|
| **Completed**       | ≥1         | No                         | `False`     | Last shutdown timestamp |
| **Resumed (active)**| ≥1         | Yes                        | `True`      | `None`      |
| **Pure active**     | 0          | N/A                        | `True`      | `None`      |

### Detection logic

After the first pass, `build_session_summary()` calls `_detect_resume(events, fp.all_shutdowns)` (in `parser.py`) which scans events after the last shutdown index:

```python
def _detect_resume(events, all_shutdowns):
    # ...
    last_shutdown_idx = all_shutdowns[-1][0]

    for i in range(last_shutdown_idx + 1, len(events)):
        ev = events[i]
        etype = ev.type
        if etype == EventType.ASSISTANT_MESSAGE:
            session_resumed = True
            # ... accumulate output tokens ...
        elif etype == EventType.USER_MESSAGE:
            session_resumed = True
            # ... count user messages ...
        elif etype == EventType.ASSISTANT_TURN_START:
            # ... count turn starts ...
        elif etype == EventType.SESSION_RESUME:
            session_resumed = True
            # ... capture resume timestamp ...
```

The helper includes a defensive guard for empty `all_shutdowns` (returns empty `_ResumeInfo`), making it safe to call independently. The `if/elif` chain short-circuits after the first match, reducing comparisons from 5 per event to 1 for the most common `ASSISTANT_MESSAGE` case.

The presence of **any** `session.resume`, `user.message`, or `assistant.message` event after the last shutdown triggers `session_resumed = True`.

### `last_resume_time`

Populated from the `timestamp` of the `session.resume` event after the last shutdown (in `parser.py`). Used by the report layer to calculate "Running Time" — showing duration since resume, not since original start.

### Resumed session summary construction

For resumed sessions (in `parser.py`):
- `end_time` is set to `None` (not the last shutdown timestamp)
- `model_metrics` contain the **merged shutdown data** (historical baseline)
- `active_model_calls`, `active_user_messages`, `active_output_tokens` contain **only** post-shutdown counts
- `model_calls` and `user_messages` are the **total** counts across the entire session

---

## Premium Request Tracking

### Where premium requests come from

The **only** source of premium request counts is `SessionShutdownData.totalPremiumRequests` (in `models.py`). This is a pre-computed value from the Copilot CLI — not something we calculate.

For sessions with multiple shutdowns, the total is summed: `total_premium += sd.totalPremiumRequests` (in `parser.py`).

### Active sessions show "—"

If a session has no shutdown data (pure active), `total_premium_requests` is `0`, and the report displays "—" (in `report.py`):

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

Defined in `_interactive_loop()` (in `cli.py`).

### Non-blocking input with `select()`

The loop uses `select.select()` on stdin (in `cli.py`) with a 500ms timeout:

```python
def _read_line_nonblocking(timeout: float = 0.5) -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    return None
```

This is **Unix only** — `select()` on stdin doesn't work on Windows. The 500ms timeout allows the main loop to check for file-change events between input polls.

### Fallback to blocking `input()`

If `select()` raises `ValueError` or `OSError` (e.g. stdin is piped, not a real TTY, or during testing), the loop falls back to blocking `input()` (in `cli.py`):

```python
except (ValueError, OSError):
    try:
        line = input().strip()
    except (EOFError, KeyboardInterrupt):
        break
```

### Watchdog file observer

A `watchdog.Observer` watches `~/.copilot/session-state/` recursively for **any** filesystem change — new session directories, lockfile creation/deletion, `events.jsonl` writes, etc. (in `cli.py`):

```python
observer = Observer()
observer.schedule(handler, str(session_path), recursive=True)
observer.daemon = True
observer.start()
```

The observer watches the session-state directory; if the directory doesn't exist at startup, no observer is created and auto-refresh is simply skipped.

### `_FileChangeHandler` with 2-second debounce

`_FileChangeHandler` (in `cli.py`) triggers on any filesystem event in the session-state tree and enforces a 2-second debounce using `time.monotonic()`:

```python
def dispatch(self, event):
    now = time.monotonic()
    if now - self._last_trigger > 2.0:
        self._last_trigger = now
        self._change_event.set()
```

Each trigger causes a full `get_all_sessions()` re-read, picking up new sessions, closed sessions, and updated event data. The debounce prevents rapid redraws during high-frequency event writes (e.g. tool execution loops producing many events per second). Manual refresh (`r`) is still available as a fallback.

### View state machine

The interactive loop maintains a simple view state (in `cli.py`):

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

Defined in `render_cost_view()` (in `report.py`).

### Per-model rows within sessions

Each session's `model_metrics` dict is iterated to produce one table row per model (in `report.py`):

```python
for model_name in sorted(s.model_metrics):
    mm = s.model_metrics[model_name]
    table.add_row(name, model_name, str(mm.requests.count), ...)
    name = ""                   # blank after first row
    model_calls_display = ""    # shown only on first model row
```

The session name and model calls are shown **only on the first model row** — subsequent model rows for the same session have blank session/model-calls columns to avoid visual repetition.

### "↳ Since last shutdown" rows

For active sessions **with shutdown metrics** (i.e., sessions where `has_shutdown_metrics=True`) **and meaningful active-period stats** (`has_active_period_stats(s)` returns `True`), an extra row is appended (in `report.py`). The `has_active_period_stats` guard prevents a misleadingly attributed row for sessions that completed a shutdown cycle but have no post-shutdown activity (i.e., `last_resume_time` is `None` and all active counters — `active_model_calls`, `active_user_messages`, `active_output_tokens` — are `0`). The Premium Cost column uses `_estimate_premium_cost()` to show a `~`-prefixed estimate based on the model multiplier, while the Requests column shows `N/A` (no shutdown data for requests). When the model cannot be determined (e.g., no tool events and missing/invalid `~/.copilot/config.json`), `_estimate_premium_cost()` returns `"—"` instead of an estimate. Pure-active sessions (never shut down) do **not** get this sub-row because there is no shutdown baseline to compare against:

```python
if s.is_active and s.has_shutdown_metrics and has_active_period_stats(s):
    cost_stats = _effective_stats(s)
    est = _estimate_premium_cost(s.model, cost_stats.model_calls)
    table.add_row(
        "  ↳ Since last shutdown",
        s.model or "—",
        "N/A",                         # Requests — no shutdown data
        est,                           # Premium Cost — "~N" estimate
        str(cost_stats.model_calls),
        format_tokens(cost_stats.output_tokens),
    )
```

The Requests column shows `N/A` because there's no shutdown data for the active period. The Premium Cost column shows an estimate (e.g. `~3`) derived from `_estimate_premium_cost()`, which multiplies the model's pricing multiplier by the number of model calls. If the model is `None`, the Premium Cost column shows `"—"` instead.

### Historical vs active sections in full summary

`render_full_summary()` (in `report.py`) renders two distinct sections:

1. **Historical Data** (`_render_historical_section_from` in `report.py`): Sessions with shutdown data. Includes sessions where `total_premium_requests > 0`, OR sessions that are **not** active, OR sessions that have `has_shutdown_metrics` (indicating non-empty shutdown model_metrics). The list is pre-partitioned by `render_full_summary` in a single pass.
2. **Active Sessions** (`_render_active_section_from` in `report.py`): Sessions where `is_active == True`. Shows `active_model_calls`, `active_user_messages`, `active_output_tokens`, and running time. The list is pre-partitioned by `render_full_summary` in a single pass.

Resumed sessions appear in **both** sections — historical section for their shutdown data, active section for their post-shutdown activity.

### Grand total row

After all session rows, a section divider and grand total row is added (in `report.py`). Grand totals accumulate `requests.count`, `requests.cost`, `model_calls`, and `output_tokens` from both shutdown metrics and active periods.

---

## Edge Cases & Error Handling

### Corrupt/malformed JSON lines

`parse_events()` (in `parser.py`) handles two failure modes per line:

1. **JSON decode failure**: `json.JSONDecodeError` → logged via `loguru.warning`, line skipped
2. **Pydantic validation failure**: `ValidationError` → logged with error count, line skipped

Valid lines in the same file are still processed. A file with 99 valid lines and 1 corrupt line produces 99 events.

### Empty sessions

A session directory with just a `session.start` event (and nothing else) produces a valid `SessionSummary` with `is_active=True`, `model_calls=0`, `user_messages=0`, `total_premium_requests=0`.

Sessions where `parse_events()` returns an empty list (no valid events at all) are skipped entirely by `get_all_sessions()` (in `parser.py`).

### TOCTOU races

Two levels of protection against files disappearing between discovery and read:

1. **Discovery**: `safe_file_identity()` (in `_fs_utils.py`) returns `None` instead of crashing when a file vanishes between directory listing (via `os.scandir()`) and `stat()`.
2. **Parsing**: `get_all_sessions()` (in `parser.py`) catches `FileNotFoundError` and `OSError` during `parse_events()` and skips the session with a warning.

### Unknown event types

Events with types not in `EventType` still parse successfully — `SessionEvent.type` is `str`, not the enum. Unknown event types are simply skipped by production code, which only has branches for known types.

In `_first_pass()` (in `parser.py`), unknown types are simply ignored — the loop only has branches for known types, with no `else` clause needed.

### Unknown models in pricing

`lookup_model_pricing()` (in `pricing.py`) has a three-tier resolution:
1. **Exact match** in `KNOWN_PRICING`
2. **Partial match** — `model_name.startswith(key)` or `key.startswith(model_name)`, longest match wins
3. **Fallback** — returns 1× standard multiplier, emits `UserWarning`

---

## Session Name Resolution

Implemented in `_extract_session_name()` (in `parser.py`).

### Resolution order

1. **Primary**: Read `plan.md` from the session directory. If it exists and the first line starts with `# `, extract the heading text after `# `.
2. **Fallback**: The report layer uses `s.name or s.session_id[:12] or "(no id)"` — showing the session name if available, then the first 12 characters of the session UUID, then the literal `"(no id)"` when the session ID is also empty.

### Plan probe mechanism

Not every session has a `plan.md` at discovery time — it may be created later during a session. To handle this without rescanning the entire session-state directory, the discovery cache implements a **plan probe** mechanism:

- `_DiscoveryCache` maintains a `no_plan_indices` list — the indices (into the cached `entries` list) of sessions where no `plan.md` has been found yet.
- On each **cache hit** (i.e., the root directory identity is unchanged so no full rescan is triggered), up to `_MAX_PLAN_PROBES` (currently 5) entries from `no_plan_indices` are checked for a newly-created `plan.md`.
- A `probe_cursor` tracks where the next sweep should start within `no_plan_indices`, rotating forward by the probe count each time. This ensures every session is eventually probed across successive cache-hit refresh cycles rather than always checking the same first few entries.
- When a probed session's `plan.md` is found, that index is removed from `no_plan_indices` and will not be re-checked.
- When `no_plan_indices` is empty (all sessions have a cached `plan.md` path), the probe step is skipped entirely.

This design keeps probe cost bounded at `O(min(_MAX_PLAN_PROBES, len(no_plan_indices)))` per call — constant amortised regardless of total session count.

### How it's called

`build_session_summary()` calls `_extract_session_name(session_dir)` when `session_dir` is provided (in `parser.py`). The `session_dir` parameter is passed by `get_all_sessions()` as `events_path.parent` (in `parser.py`).

---

## Model Multiplier Reference

From `pricing.py` — `_RAW_MULTIPLIERS` dict:

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
| `gpt-5.4-mini`         | 0×         | Free     |
| `gpt-5-mini`           | 0×         | Free     |
| `gpt-4.1`              | 0×         | Free     |
| `gpt-4o-mini`          | 0×         | Free     |
| `gpt-4o-mini-2024-07-18` | 0×      | Free     |
| `copilot-nes-oct`      | 0×         | Free     |
| `copilot-suggestions-himalia-001` | 0× | Free  |
| `gemini-3-pro-preview` | 1×         | Standard |

Tier is derived from the multiplier (in `pricing.py`): ≥3.0 → Premium, = 0.0 → Free, < 1.0 → Light, otherwise Standard.

**Note:** `pricing.py` multipliers are used **only for `~`-prefixed estimates** on live/active sessions (`render_live_sessions`, `render_cost_view`). Historical and shutdown-based views use exact API-provided premium request counts exclusively — the multipliers play no role there.

### Model resolution for active sessions

When no shutdown data exists, the model is resolved in `_build_active_summary()` (in `parser.py`):

1. Scan `tool.execution_complete` events for a `model` field
2. Fall back to `~/.copilot/config.json` → top-level `"model"` field (`_read_config_model()` in `parser.py`)

---

## VS Code Copilot Chat Logs

### Overview

The `vscode` subcommand parses VS Code Copilot Chat log files to show request counts, latency, and model usage. VS Code logs don't include token counts — only request-level data is available locally.

### Log Discovery

`discover_vscode_logs()` finds `GitHub Copilot Chat.log` files under platform-specific directories:

- **macOS:** `~/Library/Application Support/Code/logs/`
- **Windows:** `%APPDATA%/Code/logs/`
- **Linux:** `~/.config/Code/logs/`

It globs into date-stamped subdirectories (`*/window*/exthost/GitHub.copilot-chat/GitHub Copilot Chat.log`).

### Log Format

Each successful API request is logged as a `ccreq:` line:

```
2026-03-13 22:10:24.523 [info] ccreq:c0c8885e.copilotmd | success | claude-opus-4.6 | 8003ms | [panel/editAgent]
```

The `CCREQ_RE` regex in `vscode_parser.py` extracts: timestamp, request ID, model name, duration (ms), and feature category. Model redirects (e.g., `gpt-4o-mini -> gpt-4o-mini-2024-07-18`) are handled by capturing only the requested model name.

### Aggregation

`get_vscode_summary()` orchestrates: discover → parse each file → aggregate into `VSCodeLogSummary`. The summary tracks:

- Total requests, total API duration, date range
- Per-model request counts and durations
- Per-category (feature) request counts
- Daily request counts (last 14 days shown in report)
- Log files discovered vs successfully parsed

`parse_vscode_log()` raises `OSError` if a file can't be read. `get_vscode_summary()` catches it and skips unreadable files, so only successfully read files are counted in `log_files_parsed`.

### Caching

`vscode_parser.py` uses a four-layer caching architecture to avoid redundant I/O and parsing on repeated calls (e.g., during live-refresh). All caches rely on `(st_mtime_ns, st_size)`-based identities to detect changes on disk, though not every cache computes that identity via `safe_file_identity()`.

| Cache | Key | Purpose | Invalidation |
|---|---|---|---|
| `_VSCODE_DISCOVERY_CACHE` | Candidate root `Path` | Skips glob when the root directory identity and cached newest-child sentinel are unchanged | Replaced when `root_id` (root dir identity) changes or `newest_child_id` (the cached most-recently-modified child dir identity) changes; changes under older/non-sentinel child dirs may not invalidate until the root changes or the cache is cleared |
| `_VSCODE_LOG_CACHE` | Log file `Path` | Skips re-parsing a log file whose `(mtime_ns, size)` is unchanged | LRU eviction at `_MAX_CACHED_VSCODE_REQUESTS` (64); entry replaced when file identity changes |
| `_PER_FILE_SUMMARY_CACHE` | Log file `Path` | Caches per-file aggregation (`VSCodeLogSummary`) keyed by `Path`, validated by file identity | LRU eviction at `_MAX_CACHED_FILE_SUMMARIES` (256); entry replaced when file identity changes |
| `_vscode_summary_cache` | `frozenset[tuple[Path, file_id]]` | Full `VSCodeLogSummary` for the entire set of discovered files | Replaced when the combined identity set changes; only populated when all discovered logs were successfully parsed (transient read failures do not produce a stale cache) |

The two per-file caches (`_VSCODE_LOG_CACHE` and `_PER_FILE_SUMMARY_CACHE`) use `OrderedDict` with LRU insertion via the shared `lru_insert()` helper from `_fs_utils.py`. On a cache hit, entries are moved to the end via `move_to_end()` to maintain recency order.
