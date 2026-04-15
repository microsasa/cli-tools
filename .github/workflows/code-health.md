---
# Code health analysis — every 6 hours
on:
  schedule: every 6 hours
  workflow_dispatch:

permissions:
  contents: read
  issues: read
  pull-requests: read

engine: copilot

tools:
  github:
    toolsets: [default]

network: defaults

safe-outputs:
  noop:
    report-as-issue: false
  create-issue:
    max: 2
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Code Health Analysis

Analyze the entire codebase for cleanup opportunities and open issues for anything worth fixing.

## Instructions

Read `.github/copilot-instructions.md` and all referenced guidelines. Flag any violations of those standards in existing code as cleanup opportunities.

Read all files in the repository. Read all open issues in the repository. Identify genuine cleanup opportunities — refactoring, dead code, inconsistencies, stale docs, dependency hygiene, or anything else that would make the codebase meaningfully better.

For each finding, open an issue with root cause analysis and a clear spec for resolving it. Each issue must include a testing requirement — regression tests for bugs, coverage for new functionality. Prefix each issue title with `[aw][code health]` and label each issue with `aw` and `code-health`.

Do not open issues for things already caught by CI (ruff, pyright, bandit). Do not open issues for things that already have an open issue. Do not open an issue that is just a nit — if there are many small nits that together form a meaningful cleanup, bundle them into one issue. Do not open issues for performance problems (those belong to perf-analysis). Do not open issues that require modifying protected files — the implementer agent cannot create PRs that touch these paths. Protected paths include: `.github/`, `pyproject.toml`, and `uv.lock`. If you find more issues than you can open, prioritize by severity: prefer bugs and correctness issues over cosmetic cleanup. If nothing worth fixing is found, do not create any issues.
