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

Read `.github/copilot-instructions.md` and all referenced guidelines for context on the project's coding standards.

Read all files in the repository. Focus exclusively on performance — do not report code style, refactoring, or documentation issues (those belong to code-health).

Look for performance problems such as algorithmic inefficiency (O(n²) loops, repeated linear scans), redundant I/O, wasteful allocations, repeated computation, and import-time cost — but do not limit yourself to these categories. Any meaningful performance improvement is in scope.

For each finding, open an issue with: the specific file and function, what makes it slow, a concrete fix with expected improvement, and a testing requirement (benchmark or assertion that the optimized path is exercised). Prefix each issue title with `[aw][perf]` and label each issue with `aw` and `perf`.

## Duplicate and prior-work check

Before filing any issue, read **all** issues labeled `perf` — both open AND closed. Also check all other open issues in the repository, regardless of label, to avoid duplicating a problem already tracked under a different category. Do not re-file an issue that covers the same function and the same root cause as an existing issue, even if the code has changed since. If a previous issue was closed as won't-fix, respect that decision and do not re-file it. If a previous issue was closed as completed (fixed), do not re-file a variant of the same problem unless the fix was reverted or a genuinely new inefficiency was introduced.

## Materiality threshold

Before filing, estimate the absolute wall-clock time saved for realistic data sizes (e.g., 200 sessions, 50–100 log files, 1–3 models per shutdown cycle). **Do not file issues where the saving is under 1 ms.** Focus on improvements that eliminate I/O, reduce algorithmic complexity class, or save 10 ms+ on hot paths. Iterating a 2-entry dict twice instead of once, or replacing `LOAD_ATTR` with `LOAD_FAST` on a loop with <100 iterations, is not worth filing.

## Correctness-safety rule

If your proposed fix trades correctness for speed (e.g., skipping cache invalidation checks, removing defensive copies, deferring freshness checks), you **must** explicitly flag this tradeoff in the issue body under a `## Correctness risk` heading. Do not present a risky optimization as a clean win. If the only viable optimization carries a correctness risk, you may still file the issue — but the `## Correctness risk` section is mandatory and must explain what could go wrong.

## Quality over quota

It is better to file 0 issues than to fill your quota with marginal findings. Only file issues that you would confidently recommend to a senior engineer reviewing a production codebase. If nothing material is found, call `noop` and exit.

## Exclusions

Do not open issues for micro-optimizations that save nanoseconds. Do not open issues for things already caught by CI (ruff PERF rules). Do not open issues that require modifying protected files — the implementer agent cannot create PRs that touch these paths. Protected paths include: `.github/`, `pyproject.toml`, and `uv.lock`. If you find more issues than you can open, prioritize by impact: prefer findings in hot paths, high-frequency call sites, or code that scales with input size.
