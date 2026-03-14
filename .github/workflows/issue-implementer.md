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

network: defaults

safe-outputs:
  create-pull-request:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    protected-files: fallback-to-issue
  push-to-pull-request-branch:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Issue Implementer

Read the issue specified by the input, understand the problem, implement the solution, and open a PR.

## Instructions

Read all files in the repository. Read issue #${{ github.event.inputs.issue_number }} to understand what needs to be fixed. Implement the fix following the spec in the issue, including any testing requirements.

Open a pull request with the fix. The PR title should reference the issue number. Include tests as specified in the issue.
