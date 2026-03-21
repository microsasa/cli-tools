---
on:
  workflow_dispatch:
    inputs:
      issue_number:
        description: "Issue number to fix"
        required: true
        type: string

permissions:
  contents: read
  issues: read
  pull-requests: read

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
  create-pull-request:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    protected-files: fallback-to-issue
    labels: [aw]
    auto-merge: true
    draft: false
  push-to-pull-request-branch:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Issue Implementer

Read the issue specified by the input, understand the problem, implement the solution, and open a PR.

## Instructions

Read all files in the repository. Read issue #${{ github.event.inputs.issue_number }} to understand what needs to be fixed. Implement the fix following the spec in the issue, including any testing requirements.

Before committing, run the full CI check suite locally:

```
uv sync && uv run ruff check --fix . && uv run ruff format . && uv run pyright && uv run pytest --cov --cov-fail-under=80 -v
```

Fix any lint or type errors found by ruff/pyright before committing. Iterate until all checks pass cleanly.

Open a pull request with the fix. The PR title should reference the issue number. Include tests as specified in the issue. The PR must NOT be a draft — open it as a regular PR ready for review. Add the `aw` label to the PR.
