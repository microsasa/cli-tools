# CLI Tools

Monorepo for personal Python CLI utilities. Each tool is a separate package under `src/` with its own entry point.

## Tools

### copilot-usage

Parses local Copilot CLI session data to show token usage, premium requests, model breakdown, and raw event counts — the data GitHub's usage dashboard doesn't show you (or shows with multi-day delays).

**Why?** GitHub's usage page has significant delays in reporting CLI premium request consumption. Your local `~/.copilot/session-state/` files have the real data — this tool surfaces it instantly.

#### Installation

```bash
# From the repo (dev mode — no install needed)
cd ~/projects/cli-tools
uv run copilot-usage summary

# Global install (available everywhere)
uv tool install ~/projects/cli-tools
copilot-usage summary
```

#### Interactive Mode

Run `copilot-usage` with no subcommand to launch the interactive session:

```
$ copilot-usage
```

This shows the full summary dashboard with a numbered session list. From there:
- Enter a **session number** to drill into that session's detail view
- Press **c** to see the cost breakdown
- Press **r** to refresh data
- Press **q** to quit

Each sub-view has a "Press Enter to go back" prompt to return home.

The display auto-refreshes when session files change (2-second debounce).

#### Commands

##### `copilot-usage summary`

Show usage totals across all sessions with per-model and per-session breakdowns. Sessions table includes Model Calls (assistant.turn_start count) and User Msgs columns for raw event counts.

```
copilot-usage summary [--since DATE] [--until DATE] [--path PATH]
```

Options:
- `--since` — show sessions starting after this date (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`)
- `--until` — show sessions starting before this date
- `--path` — custom session-state directory (default: `~/.copilot/session-state/`)

Example:
```
$ copilot-usage summary

Copilot Usage Summary   (2026-03-07  →  2026-03-08)

╭──────────────────────────────────────────── Totals ─────────────────────────────────────────────╮
│ 0 premium requests   2337 model calls   647 user messages   2.2M output tokens                 │
│ 6h 47m 42s API duration   3 sessions                                                           │
╰────────────────────────────────────────────────────────────────────────────────────────────────╯

                                    Per-Model Breakdown
┌────────────────────┬──────────┬──────────────┬──────────────┬───────────────┬────────────┬─────────────┐
│ Model              │ Requests │ Premium Cost │ Input Tokens │ Output Tokens │ Cache Read │ Cache Write │
├────────────────────┼──────────┼──────────────┼──────────────┼───────────────┼────────────┼─────────────┤
│ claude-haiku-4.5   │       99 │            0 │         2.9M │         93.7K │       2.4M │        1.2M │
│ claude-opus-4.6    │      622 │            0 │        31.2M │          1.1M │      30.0M │        5.3M │
│ claude-opus-4.6-1m │     1420 │         1986 │       294.7M │          1.0M │     278.6M │       12.1M │
└────────────────────┴──────────┴──────────────┴──────────────┴───────────────┴────────────┴─────────────┘

                                              Sessions
┌─────────────────────┬──────────────────┬─────────┬─────────────┬───────────┬───────────────┬───────────┐
│ Name                │ Model            │ Premium │ Model Calls │ User Msgs │ Output Tokens │ Status    │
├─────────────────────┼──────────────────┼─────────┼─────────────┼───────────┼───────────────┼───────────┤
│ Copilot CLI Usage   │ claude-opus-4.6… │     288 │         539 │       200 │        468.0K │ Active 🟢 │
│ Tracker — Plan      │                  │         │             │           │               │           │
│ Stock Market        │ claude-opus-4.6… │     504 │         967 │       169 │        483.8K │ Active 🟢 │
│ Tracker             │                  │         │             │           │               │           │
│ ShapeShifter        │ claude-opus-4.6  │    1194 │         831 │       278 │          1.2M │ Active 🟢 │
└─────────────────────┴──────────────────┴─────────┴─────────────┴───────────┴───────────────┴───────────┘
```

##### `copilot-usage cost`

Show premium request costs from shutdown data (raw counts, no estimation).

```
copilot-usage cost [--since DATE] [--until DATE] [--path PATH]
```

Uses `render_cost_view` to show a per-session, per-model breakdown with 6 columns: Session, Model, Requests, Premium Cost, Model Calls, and Output Tokens. Resumed sessions include a "↳ Since last shutdown" row with active-period stats.

Example:
```
$ copilot-usage cost

💰 Cost Breakdown
┌──────────────────────────┬────────────────────┬──────────┬──────────────┬─────────────┬───────────────┐
│ Session                  │ Model              │ Requests │ Premium Cost │ Model Calls │ Output Tokens │
├──────────────────────────┼────────────────────┼──────────┼──────────────┼─────────────┼───────────────┤
│ Session Alpha            │ claude-opus-4.6-1m │      235 │          288 │           3 │         93.6K │
│ Session Beta             │ claude-opus-4.6-1m │      592 │          504 │           3 │        207.3K │
│ Session Gamma            │ claude-opus-4.6    │        8 │           10 │           2 │           400 │
│   ↳ Since last shutdown  │ claude-opus-4.6    │      N/A │           ~3 │           1 │           150 │
├──────────────────────────┼────────────────────┼──────────┼──────────────┼─────────────┼───────────────┤
│ Grand Total              │                    │      835 │          802 │           8 │        301.5K │
└──────────────────────────┴────────────────────┴──────────┴──────────────┴─────────────┴───────────────┘
```

##### `copilot-usage live`

Show currently active (running) Copilot sessions with real-time stats.

```
copilot-usage live [--path PATH]
```

Example:
```
$ copilot-usage live

                                            🟢 Active Copilot Sessions
┌─────────────┬───────────────────────────────┬────────────────────┬──────────┬──────────┬───────────┬───────────────┬─────────────┐
│ Session ID  │ Name                          │ Model              │  Running │ Messages │ Est. Cost │ Output Tokens │ CWD         │
├─────────────┼───────────────────────────────┼────────────────────┼──────────┼──────────┼───────────┼───────────────┼─────────────┤
│ 🟢 b5df8a34 │ Copilot CLI Usage Tracker —   │ claude-opus-4.6-1m │ 126h 21m │      200 │    ~3234  │        468.6K │ /Users/you  │
│             │ Plan                          │                    │          │          │           │               │             │
│ 🟢 0faecbdf │ Stock Market Tracker          │ claude-opus-4.6-1m │ 136h 17m │      169 │    ~5802  │        483.8K │ /Users/you  │
│ 🟢 4a547040 │ ShapeShifter                  │ claude-opus-4.6    │ 150h 33m │      278 │    ~2493  │          1.2M │ /Users/you  │
└─────────────┴───────────────────────────────┴────────────────────┴──────────┴──────────┴───────────┴───────────────┴─────────────┘
```

##### `copilot-usage session`

Show detailed per-turn breakdown for a specific session. Accepts a session ID prefix (first 8 chars is enough).

```
copilot-usage session SESSION_ID [--path PATH]
```

Example:
```
$ copilot-usage session b5df8a34
```

##### `copilot-usage vscode`

Show usage stats from local VS Code Copilot Chat logs. Parses `ccreq:` lines recording API requests with model name, latency, and feature category.

```
copilot-usage vscode [--vscode-logs PATH]
```

Output includes request totals, per-model breakdown with pricing tier, feature category percentages, and daily activity (last 14 days). Note: token counts are not available from local logs — only request counts and durations.

#### How It Works

Copilot CLI stores session data locally in `~/.copilot/session-state/{session-id}/`. Each session directory contains an `events.jsonl` file with structured events:

- **`session.start`** — session ID, start time, working directory
- **`assistant.message`** — per-message output tokens and model info
- **`session.shutdown`** — the goldmine: `totalPremiumRequests`, `modelMetrics` (per-model input/output/cache tokens, request counts), `codeChanges`
- **`tool.execution_complete`** — model used per tool call

For active sessions (no shutdown event yet), the tool sums individual message tokens to build a running total. For resumed sessions (activity after a shutdown), it merges the shutdown baseline with post-resume tokens.

#### Model Pricing (reference)

GitHub Copilot charges different premium-request multipliers per model. This tool reports raw counts (model calls, user messages, exact premium requests from shutdown data) — not estimated billing. The multiplier table below is provided for reference only:

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

## Development

**Prerequisites:** [uv](https://docs.astral.sh/uv/) (v0.10+)

```bash
git clone git@github.com:microsasa/cli-tools.git
cd cli-tools
uv sync
```

### Make Targets

| Command | Description |
|---|---|
| `make check` | Run all checks (lint + typecheck + security + tests) |
| `make test` | Unit tests (80% overall coverage enforced) + e2e tests |
| `make test-unit` | Unit tests only with verbose output |
| `make test-e2e` | E2E tests only |
| `make lint` | Ruff lint + format check |
| `make fix` | Auto-fix lint/formatting issues |

Add `V=1` for verbose output: `make check V=1`

### Project Structure

```
cli-tools/
├── src/
│   └── copilot_usage/
│       ├── cli.py              # Click commands + interactive loop + watchdog auto-refresh
│       ├── models.py           # Pydantic data models
│       ├── parser.py           # events.jsonl parsing
│       ├── pricing.py          # Model cost multipliers
│       ├── logging_config.py   # Loguru configuration
│       ├── report.py           # Rich terminal output
│       └── docs/               # Developer docs
│           ├── architecture.md
│           ├── changelog.md
│           ├── implementation.md
│           └── plan.md
├── tests/
│   ├── copilot_usage/          # Unit tests
│   └── e2e/                    # End-to-end tests with fixtures
├── docs/
│   └── changelog.md            # Top-level changelog
├── Makefile
└── pyproject.toml
```

### Stack

- **Python 3.12+** with pyright strict mode
- **Click** — CLI framework
- **Rich** — terminal tables and formatting
- **Pydantic v2** — runtime-validated data models
- **Ruff** — linting + formatting
- **Bandit** — security scanning
- **pytest + pytest-cov** — testing (80% overall coverage minimum)
- **diff-cover** — PRs must have ≥90% coverage on new/changed lines

## Future Tools

- **repo-health** — audit any repo against project-standards (missing Makefile targets, wrong ruff rules, etc.)
- **session-manager** — list/search/clean up Copilot CLI sessions in `~/.copilot/session-state/`
- **docker-status** — pretty overview of all OrbStack containers, ports, health, resource usage
- **env-check** — verify dev environment matches standards (uv version, Python, pnpm, Docker, VS Code extensions)