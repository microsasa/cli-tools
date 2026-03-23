# CLI Tools — Changelog

Append-only history of what was planned and delivered, PR by PR. Newest entries first.

---

## fix: code review fixes + implementation doc — 2026-03-13

**Plan**: Address code review findings and add detailed implementation documentation.

**Done**:
- Fixed loguru format strings (was using f-strings instead of `{}` placeholders)
- Fixed model_calls duplication in resumed sessions
- Fixed premium total consistency across views
- Fixed active-in-historical leak (active sessions no longer bleed into historical section)
- Fixed parent `--path` fallback propagation
- Fixed TOCTOU race in session discovery (`_safe_mtime()` + catch in `get_all_sessions()`)
- Removed Start Time column from sessions table
- Added `last_resume_time` for accurate Running duration display
- Created `docs/implementation.md` — deep-dive into internals (shutdown aggregation, active detection, edge cases)
- 13 new e2e tests (55 total), 327 total tests, 96% unit coverage

---

## feat: add interactive mode — 2026-03-13

**Plan**: Add an interactive session loop when `copilot-usage` is invoked without a subcommand. Replace the deleted Textual TUI with a simpler Rich + input() approach.

**Done**:
- Interactive loop in `cli.py` — summary view with numbered session list
- Session detail drill-down by number, cost view via `c`, refresh via `r`, quit via `q`
- "Press Enter to go back" navigation between views
- Updated report.py with `render_full_summary`, `render_cost_view`, `render_session_detail` accepting `target_console`
- Removed Textual dependency, kept Rich only
- 247 unit tests + 30 e2e tests passing, 98% coverage

---

## refactor: remove multiplier estimation from summary/cost, report raw event counts — 2026-03-13

**Plan**: Strip out multiplier-based premium request estimation from `SessionSummary` and the `cost`/`summary` commands. Report raw facts: model calls (assistant.turn_start count), user messages, output tokens, and exact premium requests from shutdown data only.

**Done**:
- Removed estimated_premium_requests from SessionSummary
- Added model_calls and user_messages fields
- Simplified cost command to raw data only
- Updated all tests and fixtures

**Note**: Active/live sessions still show estimated costs with a `~` prefix via `_estimate_premium_cost` in the `live` command's "Est. Cost" column. This estimation was intentionally kept for live sessions where exact premium data is not yet available from a shutdown event.

---

## build: switch to loguru, align with latest standards — 2026-03-13

**Plan**: Replace stdlib logging with loguru per project-standards §13. Update per-file test ignores to match §15.

**Done**:
- Replaced `import logging` with loguru in parser.py
- Added minimal CLI logging config (stderr warnings only, no file output)
- Updated pyproject.toml per-file-ignores: added S105, S106 for test credentials
- Pinned pydantic>=2,<3 per dependency pinning standard

---

## fix: address Copilot code review (10 issues) — 2026-03-13

**Plan**: Fix all 10 issues flagged by Copilot code review on PR #1.

**Done**:
- Pinned pydantic>=2,<3 (was unpinned)
- Anonymized remaining "microsasa" in fixture files
- Updated README pricing table to match corrected multipliers
- Fixed e2e test name mismatch
- Dynamic e2e pass count in Makefile (was hardcoded)
- Cache tokens now includes both read + write
- Added pluralization ("1 session" vs "N sessions")
- Cost command shows "mixed" for multi-model sessions
- Widened toolRequests type to dict[str, object]

---

## fix: detect resumed sessions + correct model pricing — 2026-03-12

**Plan**: Fix resumed session detection and correct model multipliers from actual GitHub data.

**Done**:
- Fixed build_session_summary to detect events after shutdown (resumed = active)
- Corrected all model multipliers: Opus 3x/6x (was 50x), Haiku 0.33x (was 0.25x), GPT-5-mini/GPT-4.1 0x (was 0.25x)
- Added model fallback inference from modelMetrics keys when currentModel missing
- E2e fixture for resumed sessions, corrupt sessions
- 244 tests total, 98% coverage

---

## docs: add project docs (architecture, changelog, updated plan) — 2026-03-12

**Plan**: Add architecture.md and changelog.md per project-standards §13. Update plan.md to be forward-looking only (remove completed phase checkboxes).

**Done**:
- Created docs/architecture.md with data flow diagram, component descriptions, pipeline, design decisions
- Created docs/changelog.md (this file) with backfilled history
- Rewrote docs/plan.md — removed completed phases and checkboxes, kept scope, decisions, future ideas

---

## docs: add comprehensive README — 2026-03-12

**Plan**: Create README with installation, usage examples, dev setup, and all 4 commands documented.

**Done**:
- README.md with real command output examples (anonymized)
- Installation instructions (dev mode + global install)
- Model pricing table, dev workflow, project structure

---

## fix: detect resumed sessions — 2026-03-12

**Plan**: Sessions that are resumed after shutdown were incorrectly showing as "Completed." Fix parser to detect post-shutdown events.

**Done**:
- Fixed `build_session_summary` to check for events after last `session.shutdown`
- Resumed sessions marked `is_active = True`, post-shutdown tokens merged
- New e2e fixture: `resumed-session/events.jsonl`
- 4 unit tests + 2 e2e tests

---

## build: separate unit and e2e test output — 2026-03-12

**Plan**: `make test` should show unit test coverage and e2e tests as separate lines.

**Done**:
- Split `test` target: unit tests with coverage, e2e tests with pass count
- Added `make test-unit` and `make test-e2e` targets
- Output: `✅ unit tests (93% coverage)` + `✅ e2e tests (15 passed)`

---

## test: add e2e tests with anonymized fixture data — 2026-03-12

**Plan**: Create e2e tests that run actual CLI commands against real (anonymized) session data.

**Done**:
- Extracted 55 events across 3 sessions from real data, anonymized content (~23KB)
- 13 e2e tests covering summary, session, cost, live commands
- Added `--path` option to `session` command (was missing)
- Fixture data preserves full event sequences (no gaps)

---

## feat: wire up CLI commands, add CI workflow — 2026-03-09

**Plan**: Replace stub CLI commands with real implementations. Create GitHub Actions CI.

**Done**:
- All 4 commands wired up: summary, session, cost, live
- `--since`, `--until`, `--path` options on all commands
- Session prefix matching (first 8 chars of UUID)
- Graceful error handling (no tracebacks)
- `.github/workflows/ci.yml` — PR gate per standards
- 13 CLI tests

---

## feat: build core features (models, parser, reports, pricing, live) — 2026-03-09

**Plan**: Build all core modules — Pydantic models, event parser, Rich reports, pricing data, live session tracking.

**Done**:
- `models.py` — 15 Pydantic models covering all event types + SessionSummary
- `parser.py` — session discovery, event parsing, summary building, active session handling
- `report.py` — summary tables, session detail with event timeline, live session view, cost breakdown
- `pricing.py` — 17 model multipliers, lookup with partial matching, cost estimation
- 178 unit tests, 93% coverage

---

## feat: initial project scaffold — 2026-03-09

**Plan**: Create cli-tools monorepo with copilot-usage CLI stub, full tooling per project-standards.

**Done**:
- `uv init --python 3.12`, dependencies (pydantic, click, rich) + dev deps
- pyproject.toml with full tool config (ruff 13 rules, pyright strict, pytest, coverage 80%)
- Pretty Makefile with emoji output and V=1 verbose
- src/copilot_usage/ package with Click CLI stub (4 commands)
- .editorconfig, .github/dependabot.yml, .gitignore, py.typed
- Pushed to microsasa/cli-tools (private), main = empty init, dev = work
- `make check` passing (94% coverage)
