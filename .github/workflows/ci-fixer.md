---
on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request number to fix"
        required: true
        type: number

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
    toolsets: [context, pull_requests]

network:
  allowed:
    - defaults

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

Read `.github/copilot-instructions.md` and follow all referenced guidelines for code changes.

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

7. After making fixes, run `make fix` to auto-fix lint and format issues, then run `make check` to verify all checks pass: `make fix && make check`

8. Push the fix commit to the PR branch with a clear commit message like "fix: resolve CI failures (ruff/pyright/pytest)".

If you cannot fix the issue after reasonable effort, add a comment to the PR explaining what failed and why manual intervention is needed.
