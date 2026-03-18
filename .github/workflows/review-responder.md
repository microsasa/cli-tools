---
on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR number to address review comments on"
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
  push-to-pull-request-branch:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  reply-to-pull-request-review-comment:
    max: 10
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-labels:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Review Responder

Address review comments on pull request #${{ inputs.pr_number }}.

## Instructions

This workflow addresses unresolved review comments on a pull request.

1. Check if the PR already has the label `review-response-attempted`. If it does, add a comment to the PR saying "Review response already attempted — stopping to prevent loops. Manual intervention needed." and stop.

2. Add the label `review-response-attempted` to the PR.

3. Read the unresolved review comment threads on the PR (not just the latest review — get all unresolved threads). If there are more than 10 unresolved threads, address the first 10 and leave a summary comment on the PR noting how many remain for manual follow-up.

4. For each unresolved review comment thread (up to 10):
   a. Read the comment and understand what change is being requested
   b. Read the relevant file and surrounding code context
   c. Make the requested fix in the code
   d. Reply to the comment thread explaining what you changed

5. After addressing all comments, run the CI checks locally to make sure your fixes don't break anything: `uv sync && uv run ruff check --fix . && uv run ruff format . && uv run pyright && uv run pytest --cov --cov-fail-under=80 -v`

6. Push all changes in a single commit with message "fix: address review comments".

If a review comment requests a change that would be architecturally significant or you're unsure about, reply to the thread explaining your concern rather than making the change blindly.
