---
if: "contains(github.event.pull_request.labels.*.name, 'aw')"
on:
  pull_request_review:
    types: [submitted]
  roles: all
  bots: [Copilot, copilot-pull-request-reviewer]

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

Address review comments on pull request #${{ github.event.pull_request.number }}.

## Instructions

This workflow runs when a review is submitted on a pull request.

1. First, check if the PR has the `aw` label. If it does NOT have the `aw` label, stop immediately — this workflow only handles agent-created PRs.

2. Check the review that triggered this workflow. If the review has no comments (e.g., a plain approval with no inline comments), check if there are any unresolved review threads on the PR. If the review has no comments AND there are no unresolved threads, stop — there is nothing to address. Do NOT add any labels when stopping here. If the review has no comments BUT there ARE unresolved threads from a previous review, continue to step 3 to address those threads.

3. Read the unresolved review comment threads on the PR (not just the latest review — get all unresolved threads). If there are no unresolved threads, stop — there is nothing to address. Do NOT add any labels when stopping here.

4. Check if the PR already has the label `review-response-attempted`. If it does, add a comment to the PR saying "Review response already attempted — stopping to prevent loops. Manual intervention needed." and stop.

5. Add the label `review-response-attempted` to the PR. Only add this label HERE — after confirming there are actual threads to address.

6. For each unresolved review comment thread (up to 10):
   a. Read the comment and understand what change is being requested
   b. Read the relevant file and surrounding code context
   c. Make the requested fix in the code (edit the file locally — do NOT push yet)
   d. Reply to the comment thread explaining what you changed

7. If there are more than 10 unresolved threads, leave a summary comment on the PR noting how many remain for manual follow-up.

8. After addressing all comments, run the CI checks locally to make sure your fixes don't break anything: `uv sync && uv run ruff check --fix . && uv run ruff format . && uv run pyright && uv run pytest --cov --cov-fail-under=80 -v`

9. Push all changes in a single commit with message "fix: address review comments". Reply to all threads BEFORE pushing — replies after a push will appear on outdated code.

If a review comment requests a change that would be architecturally significant or you're unsure about, reply to the thread explaining your concern rather than making the change blindly.

NOTE: Thread resolution is handled separately by the pipeline orchestrator after this workflow completes. Your job is to fix the code and reply to threads — do NOT attempt to resolve threads.
