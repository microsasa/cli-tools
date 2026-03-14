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
  create-issue:
    max: 2
  create-agent-session:
    max: 2

---

# Code Health Analysis

Analyze the entire codebase for cleanup opportunities and open issues for anything worth fixing.

## Instructions

Read all files in the repository. Identify genuine cleanup opportunities — refactoring, dead code, inconsistencies, stale docs, dependency hygiene, or anything else that would make the codebase meaningfully better.

For each finding, open an issue with root cause analysis and a clear spec for resolving it. Each issue must include a testing requirement — regression tests for bugs, coverage for new functionality. Assign copilot to work on each issue.

Do not open issues for things already caught by CI (ruff, pyright, bandit). Do not open an issue that is just a nit — if there are many small nits that together form a meaningful cleanup, bundle them into one issue. If nothing worth fixing is found, do not create any issues.
