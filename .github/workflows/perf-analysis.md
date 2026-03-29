---
# Performance analysis — every 6 hours
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

# Performance Analysis

Analyze the codebase for performance problems and open issues for anything worth optimizing.

## Instructions

Read `.github/CODING_GUIDELINES.md` for context on the project's coding standards.

Read all files in the repository. Read all open issues in the repository. Focus exclusively on performance — do not report code style, refactoring, or documentation issues (those belong to code-health).

Look for performance problems such as algorithmic inefficiency (O(n²) loops, repeated linear scans), redundant I/O, wasteful allocations, repeated computation, and import-time cost — but do not limit yourself to these categories. Any meaningful performance improvement is in scope.

For each finding, open an issue with: the specific file and function, what makes it slow, a concrete fix with expected improvement, and a testing requirement (benchmark or assertion that the optimized path is exercised). Prefix each issue title with `[aw][perf]` and label each issue with `aw` and `perf`.

Do not open issues for micro-optimizations that save nanoseconds. Do not open issues for things already caught by CI (ruff PERF rules). Do not open issues for things that already have an open issue. If nothing worth optimizing is found, do not create any issues.
