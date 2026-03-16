---
# Daily code health analysis
on:
  schedule: daily
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

Read all files in the repository. Read all open issues in the repository. Identify genuine cleanup opportunities — refactoring, dead code, inconsistencies, stale docs, dependency hygiene, or anything else that would make the codebase meaningfully better.

For each finding, open an issue with root cause analysis and a clear spec for resolving it. Each issue must include a testing requirement — regression tests for bugs, coverage for new functionality. Prefix each issue title with `[aw][code health]` and label each issue with `aw` and `code-health`. The pipeline orchestrator will pick up the issue and dispatch the implementer — do NOT dispatch it yourself.

Do not open issues for things already caught by CI (ruff, pyright, bandit). Do not open issues for things that already have an open issue. Do not open an issue that is just a nit — if there are many small nits that together form a meaningful cleanup, bundle them into one issue. If nothing worth fixing is found, do not create any issues.
