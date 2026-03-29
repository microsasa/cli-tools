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

imports:
  - shared/fetch-review-comments.md

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
  reply-to-pull-request-review-comment:
    target: "*"
    max: 10
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-labels:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Review Responder

Address review comments on pull request #${{ inputs.pr_number }}.

## Instructions

Read `.github/copilot-instructions.md` and follow all referenced guidelines for code changes.

This workflow addresses unresolved review comments on a pull request.

1. Check if the PR already has the label `aw-review-response-attempted`. If it does, add a comment to the PR saying "Review response already attempted — stopping to prevent loops. Manual intervention needed." and stop.

2. Add the label `aw-review-response-attempted` to the PR.

3. Read the pre-fetched unresolved review threads from the file `/tmp/gh-aw/review-data/unresolved-threads.json`. This file was populated before you started by a workflow step that queried the GitHub GraphQL API. Each thread contains an `id`, `comments` array (with `databaseId`, `body`, `path`, `line`, `author`), and resolution status. If the file is empty or contains `[]`, there are no unresolved threads — stop and report via noop. If there are more than 10 unresolved threads, address the first 10 and leave a summary comment on the PR noting how many remain for manual follow-up.

4. For each unresolved review comment thread (up to 10):
   a. Read the comment and understand what change is being requested
   b. Read the relevant file and surrounding code context
   c. Make the requested fix in the code
   d. Reply to the comment thread using `reply_to_pull_request_review_comment` with the comment's `databaseId` as the `comment_id`

5. After addressing all comments, run the CI checks locally to make sure your fixes don't break anything: `uv sync && uv run ruff check --fix . && uv run ruff format . && uv run pyright && uv run pytest --cov --cov-fail-under=80 -v`

6. If CI checks fail, fix the issues and re-run until they pass. Do not push broken code.

7. Push all changes in a single commit with message "fix: address review comments".

If a review comment requests a change that would be architecturally significant or you're unsure about, reply to the thread explaining your concern rather than making the change blindly.
