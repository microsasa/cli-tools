# CLI Tools — Plan

Monorepo for all Python CLI utilities under `microsasa`. Each tool is a separate package with its own entry point, sharing dev tooling, CI, and common dependencies.

Repo: `microsasa/cli-tools` (private)
Location: `~/projects/cli-tools/`
Standards: follows `microsasa/project-standards`

---

## Tools

### copilot-usage (shipped)

**Problem**: GitHub's usage dashboard has significant delays (or gaps) in reporting Copilot CLI premium request consumption. The local `~/.copilot/session-state/*/events.jsonl` files contain rich, accurate usage data that isn't being surfaced anywhere.

**Solution**: CLI tool that parses local session data and presents a usage dashboard in the terminal.

**Data sources** — each session in `~/.copilot/session-state/{session-id}/` contains:
- **`events.jsonl`** — every event logged during the session:
  - `session.start` — session ID, version, start time, cwd
  - `assistant.message` — per-message `outputTokens` and model info
  - `session.shutdown` — `totalPremiumRequests`, `totalApiDurationMs`, `modelMetrics` (per-model `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, request count & cost), `codeChanges`, `currentModel`
  - `tool.execution_complete` — model used per tool call
  - `user.message` — user prompts
**Commands**:
- `copilot-usage` — launches Rich interactive mode with numbered session list, cost view, and watchdog-based auto-refresh (2-second debounce)
- `copilot-usage session <id>` — per-turn token breakdown, tools used, API call timeline, code changes (static CLI output)

**Interactive mode**:
The main interface. Launches a Rich-based interactive loop in the terminal:
- **Home view**: summary dashboard with numbered session list showing premium requests, model calls, user messages, output tokens, and status
- **Session detail**: enter a session number to drill into per-turn breakdown
- **Cost view**: press `c` to see premium request breakdown per session, per model
- **Manual refresh**: press `r` to reload session data
- **Auto-refresh**: monitors `events.jsonl` files for changes and auto-refreshes the current view (2-second debounce). This provides the live-updating dashboard experience.
- **Quit**: press `q` to exit

**Data philosophy**:
- **Historical data** (completed shutdown cycles): exact numbers from shutdown events — premium requests, model metrics, input/output/cache tokens, API duration. Never estimated. A session can have multiple shutdown cycles (shutdown → resume → shutdown).
- **Active session data** (since last shutdown or session start): event counts from events.jsonl — model calls, user messages, output tokens. Premium requests are NOT available in events.jsonl between shutdowns; estimation approaches TBD.
- Reports clearly separate historical and active data — never mix exact and estimated numbers.

**Decided against**:
- Active session premium request estimation — multipliers don't map 1:1 to API calls, producing unreliable numbers. Show "N/A" instead.

### Future tool ideas
- **repo-health** — audit any repo against project-standards (missing Makefile targets, wrong ruff rules, missing py.typed, etc.)
- **session-manager** — list/search/clean up Copilot CLI sessions in `~/.copilot/session-state/`
- **docker-status** — pretty overview of all OrbStack containers, ports, health, resource usage
- **env-check** — verify dev environment matches standards (uv version, Python, pnpm, Docker, VS Code extensions)

---

## Project Structure

```
cli-tools/
├── .editorconfig
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── ci.yml
│       ├── codeql.yml
│       └── dependency-review.yml
├── Makefile
├── pyproject.toml
├── README.md
├── docs/
│   └── changelog.md                # Repo-level (CI, infra)
├── src/
│   └── copilot_usage/
│       ├── __init__.py
│       ├── py.typed
│       ├── cli.py
│       ├── parser.py
│       ├── models.py
│       ├── report.py
│       ├── pricing.py
│       ├── logging_config.py
│       └── docs/                   # Tool-specific docs
│           ├── plan.md             # This file
│           ├── architecture.md
│           ├── changelog.md
│           └── implementation.md
└── tests/
    ├── copilot_usage/              # Unit tests
    │   ├── test_cli.py
    │   ├── test_parser.py
    │   ├── test_models.py
    │   ├── test_pricing.py
    │   └── test_report.py
    └── e2e/                        # E2e tests with anonymized fixtures
        ├── fixtures/
        └── test_e2e.py
```

When adding a new tool, add a new package under `src/` and test dirs under `tests/`:
```
src/
├── copilot_usage/                  # existing
└── repo_health/                    # new tool
    ├── __init__.py
    ├── py.typed
    ├── cli.py
    └── ...
tests/
├── copilot_usage/                  # existing unit tests
├── repo_health/                    # new tool unit tests
└── e2e/                            # e2e tests for all tools
```

Each tool gets its own entry point in `pyproject.toml`:
```toml
[project.scripts]
copilot-usage = "copilot_usage.cli:main"
repo-health = "repo_health.cli:main"
```

---

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Repo structure | Monorepo for all CLI tools | Shared tooling config, one CI pipeline, less boilerplate |
| Python tooling | uv + pyproject.toml | Per project-standards |
| Type checking | pyright (strict mode) | No duck typing, per project-standards |
| Linting + formatting | ruff (13 rule groups) | Per project-standards |
| Security scanning | bandit | Per project-standards |
| Coverage threshold | 80% minimum (unit tests) | Per project-standards |
| CI | GitHub Actions PR gate | Per project-standards |
| Data validation | Pydantic v2 | Runtime + static type safety |
| CLI framework | Click | Cleaner than argparse |
| Terminal output | Rich | Beautiful tables, colors |
| Local dev workflow | Makefile (pretty output) | Per project-standards |
| Commit convention | Conventional commits | Per project-standards |
| Testing | Unit tests (coverage) + e2e tests (fixture data) | Unit for logic, e2e for full CLI pipeline |
| E2e fixtures | Anonymized real data, ~23KB | Real event structure, no sensitive content |
