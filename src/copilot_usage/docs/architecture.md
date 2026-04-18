# CLI Tools — Architecture

## Overview

Monorepo containing Python CLI utilities that share tooling, CI, and common dependencies. Each tool is a separate package under `src/` with its own Click entry point.

---

## copilot-usage

### Data Flow

```
~/.copilot/session-state/          src/copilot_usage/
┌─────────────────────────┐
│ {session-id}/           │        ┌──────────┐     ┌──────────┐     ┌──────────┐
│   events.jsonl ─────────┼───────▶│ parser   │────▶│ models   │────▶│ report   │───▶ terminal
│                         │        │          │     │          │     │          │
│ {session-id}/           │        │ discover │     │ Pydantic │     │ Rich     │
│   events.jsonl ─────────┼───────▶│ parse    │     │ validate │     │ tables   │
│                         │        │ summarize│     │          │     │ panels   │
│ ...                     │        └──────────┘     └──────────┘     └──────────┘
└─────────────────────────┘                                │
                                                    ┌──────┴──────┐
                                                    │ pricing     │
                                                    │             │
                                                    │ multipliers │
                                                    │ (live est.) │
                                                    └─────────────┘
```

### Components

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Click command group — routes commands to parser/report functions, handles CLI options, error display. Also contains the interactive loop (invoked when no subcommand is given) with watchdog-based auto-refresh (2-second debounce). |
| `parser.py` | Discovers sessions, reads events.jsonl line by line, builds SessionSummary per session via focused helpers: `_first_pass()` (extract identity/shutdowns/counters/post-shutdown resume data in a single pass), `_build_completed_summary()`, `_build_active_summary()`. |
| `models.py` | Pydantic v2 models for all event types + SessionSummary aggregate (includes model_calls and user_messages fields). Runtime validation at parse boundary. |
| `report.py` | Rich-formatted terminal output — summary tables (with Model Calls and User Msgs columns), live view, premium request breakdown. Shows raw counts and `~`-prefixed premium cost estimates for live/active sessions; historical post-shutdown views display exact API-provided numbers. |
| `render_detail.py` | Session detail rendering — extracted from report.py. Displays event timeline, per-event metadata, and session-level aggregates. |
| `_formatting.py` | Shared formatting utilities — `format_duration()` and `format_tokens()` with doctest-verified examples. Used by report.py and render_detail.py. |
| `_fs_utils.py` | Shared filesystem/caching utilities — `lru_insert` (LRU eviction for module-level `OrderedDict` caches) and `safe_file_identity` (returns `(mtime_ns, size)` for robust cache-invalidation; returns `None` on any `OSError`). Used by `parser.py` and `vscode_parser.py`. |
| `pricing.py` | Model pricing registry — multiplier lookup, tier categorization. Multipliers are used for `~`-prefixed cost estimates in live/active views (`render_live_sessions`, `render_cost_view`); historical post-shutdown views use exact API-provided numbers exclusively. |
| `logging_config.py` | Loguru setup — stderr warnings only, no file output. Uses a `_PatcherRecord` TypedDict to type-check the emoji-injection patcher without importing the unresolvable `loguru.Record` type at runtime. Called once from CLI entry point. |
| `vscode_parser.py` | VS Code Copilot Chat log parser — discovers log files per platform (macOS/Windows/Linux), parses `ccreq:` lines with regex, aggregates into `VSCodeLogSummary`. |
| `vscode_report.py` | Rich rendering for VS Code usage data — totals panel, per-model table, feature breakdown, daily activity. Accepts optional `target_console` for testing. |

### Event Processing Pipeline

1. **Discovery** — `discover_sessions()` scans `~/.copilot/session-state/*/events.jsonl`, returns paths sorted by modification time
2. **Parsing** — `_parse_events_from_offset()` reads each line as JSON in binary mode, creates `SessionEvent` objects via Pydantic validation. The production pipeline accesses this through `get_cached_events()`, which caches results and supports incremental byte-offset parsing for append-only file growth. The public `parse_events()` delegates to the same implementation with `include_partial_tail=True` for one-shot full-file reads. Malformed lines are skipped with a warning.
3. **Typed dispatch** — callers use the narrowly-typed `as_*()` accessors (`as_session_start()`, `as_assistant_message()`, etc.) on `SessionEvent` to get a validated payload for each known event type. Unknown event types still validate as `SessionEvent`, but normal processing ignores them unless a caller explicitly validates `data` with `GenericEventData`.
4. **Summarization** — `build_session_summary()` orchestrates focused helpers:
   - `_first_pass()`: single pass over events — extracts session metadata from `session.start`, counts raw events (model calls, user messages, output tokens), collects all shutdown data, and tracks rolling post-shutdown accumulators (reset on each shutdown) for resume detection
   - `_build_completed_summary()`: merges all shutdown cycles (metrics, premium requests, code changes) into a SessionSummary. Sets `is_active=True` if resumed.
   - `_build_active_summary()`: for sessions with no shutdowns — infers model from `tool.execution_complete` events or `~/.copilot/config.json`, builds synthetic metrics from output tokens
   - Two frozen dataclasses (`_FirstPassResult`, `_ResumeInfo`) carry state between helpers
5. **Rendering** — Report functions receive `SessionSummary` objects and render Rich output

### Key Design Decisions

**Pydantic at the boundary, not everywhere.** Raw JSON is validated into Pydantic models during parsing. After that, typed Python objects flow through the system — no re-validation needed internally.

**Shutdown event as source of truth.** The `session.shutdown` event contains pre-aggregated metrics (total tokens, premium requests, model breakdown). We use these directly instead of re-summing individual events — more accurate and faster.

**Resumed session detection.** Sessions can be shut down and resumed. The parser checks for events after the last `session.shutdown` to detect this. Resumed sessions get `is_active = True` with shutdown metrics preserved as historical data.

**Graceful degradation.** Unknown event types still validate as `SessionEvent`, but production code skips them. `GenericEventData(extra="allow")` remains available for optional best-effort payload validation when a caller explicitly chooses to use it. Missing fields get defaults. The tool never crashes on unexpected data.

### Testing Strategy

> For detailed implementation internals (shutdown aggregation, active detection, edge cases), see [implementation.md](implementation.md).

```
tests/
├── copilot_usage/              Unit tests — synthetic data, test functions in isolation
│   ├── test_models.py          Pydantic model creation and validation
│   ├── test_parser.py          Event parsing, session summary building, edge cases
│   ├── test_pricing.py         Pricing lookups, cost estimation
│   ├── test_report.py          Rich output & session-detail rendering
│   ├── test_formatting.py      Formatting helpers and string utilities
│   ├── test_logging_config.py  Loguru configuration
│   ├── test_cli.py             Click command invocation via CliRunner
│   ├── test_vscode_parser.py   VS Code log parsing, discovery, aggregation
│   └── test_vscode_report.py   VS Code report rendering
├── test_packaging.py           Wheel build test — verifies docs excluded from distribution
├── test_docs.py                Documentation tests
└── e2e/                        E2e tests — real CLI commands against fixture data
    ├── fixtures/               Anonymized events from real Copilot sessions
    └── test_e2e.py             Full pipeline: CLI → parser → models → report → output
```

- **Unit tests**: 99% coverage, test individual functions with synthetic data
- **Doctests**: `_formatting.py` functions have `>>>` examples executed via `--doctest-modules`
- **E2e tests**: Run actual CLI commands against anonymized fixture sessions, assert on output content
- Test counts grow regularly — run `make test` to see the current numbers
- Coverage is measured on unit tests only (e2e coverage would be misleading)
