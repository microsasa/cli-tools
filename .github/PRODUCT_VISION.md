# Product Vision

## Goal

Surface **local Copilot CLI usage data instantly** — the token counts, premium
request costs, model breakdowns, and session activity that GitHub's online
dashboard either hides or reports with multi-day delays.

## Target Users

- **Individual developers** using GitHub Copilot in the terminal (Copilot CLI)
  who want real-time visibility into their premium-request consumption and
  per-model token usage.
- **Power users** running multiple concurrent sessions across projects who need
  a single pane of glass for all active and historical sessions.

## Core Principles

1. **Local-first** — all data comes from `~/.copilot/session-state/` on disk;
   no API calls, no authentication, no network dependency.
2. **Accurate over estimated** — use shutdown-event metrics (the source of
   truth) whenever available; fall back to per-message sums only for active
   sessions that haven't shut down yet.
3. **Zero-config** — works out of the box with sensible defaults; optional
   flags for date filtering, custom paths, and VS Code log parsing.
4. **Fast** — incremental parsing with LRU caching; never re-read data that
   hasn't changed.

## Non-Goals

- Replacing or duplicating GitHub's online billing dashboard.
- Enforcing usage quotas or budget alerts (may be a future tool).
- Supporting non-CLI Copilot surfaces (VS Code inline completions, JetBrains)
  beyond the read-only `vscode` sub-command.
