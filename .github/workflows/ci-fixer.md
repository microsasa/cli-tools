---
on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number to fix"
        required: true
        type: string

permissions:
  contents: read
  issues: read
  pull-requests: read
  actions: read

checkout:
  fetch: ["*"]
  fetch-depth: 0

engine:
  id: copilot
  model: claude-opus-4.6

tools:
  github:
    toolsets: [default]

network:
  allowed:
    - defaults
    - python

safe-outputs:
  noop:
    report-as-issue: false
  push-to-pull-request-branch:
    target: "*"
    labels: [aw]
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-labels:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-comment:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# CI Fixer

Fix CI failures on pull request #${{ github.event.inputs.pr_number }}.

## Instructions

Read and follow the coding standards in `.github/CODING_GUIDELINES.md` for all code changes.

1. First, check if PR #${{ github.event.inputs.pr_number }} has the label `aw-ci-fix-attempted`. If it does, add a comment saying "CI fix already attempted once — stopping to prevent loops. Manual intervention needed." and stop. Do NOT attempt another fix.

2. Add the label `aw-ci-fix-attempted` to the PR.

3. Read the PR to find the head branch and understand what the PR changes.

4. Look at the most recent CI workflow run for this PR. Read the logs for any failed jobs to understand what went wrong.

5. Check out the PR branch and read the relevant files.

6. Fix the issues found in the CI logs. Common failures include:
   - ruff lint errors (import ordering, unused imports, style issues)
   - ruff format violations
   - pyright type errors (missing annotations, type mismatches, reportPrivateUsage)
   - pytest failures (assertion errors, missing fixtures, import errors)
   - bandit security warnings

7. After making fixes, run the checks locally to verify: `uv sync && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest --cov --cov-fail-under=80 -v`

8. Push the fix commit to the PR branch with a clear commit message like "fix: resolve CI failures (ruff/pyright/pytest)".

If you cannot fix the issue after reasonable effort, add a comment to the PR explaining what failed and why manual intervention is needed.
