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
  resolve-pull-request-review-thread:
    max: 10
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-labels:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Review Responder

Address review comments on pull request #${{ github.event.pull_request.number }}.

## Instructions

This workflow runs when a review is submitted on a pull request.

1. ***PUSH AS LAST STEP***: Do NOT push any code until all other steps are complete. All replies, thread resolutions, and CI checks must happen before any push.

2. First, check if the PR has the `aw` label. If it does NOT have the `aw` label, stop immediately — this workflow only handles agent-created PRs.

3. Check the review that triggered this workflow. If the review has no comments (e.g., a plain approval with no inline comments), stop — there is nothing to address.

4. Check if the PR already has the label `review-response-attempted`. If it does, add a comment to the PR saying "Review response already attempted — stopping to prevent loops. Manual intervention needed." and stop.

5. Add the label `review-response-attempted` to the PR.

6. ***CRITICAL***: Look up real thread IDs using bash before doing anything else with threads. Run `gh api graphql` to query this PR's review threads. Use the repository owner and name from the environment variables `$GITHUB_REPOSITORY_OWNER` and `${GITHUB_REPOSITORY#*/}`, and substitute the actual PR number (from the workflow trigger context) into the query. Example:
   ```
   gh api graphql -f query='query($owner: String!, $name: String!, $pr: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $pr) { reviewThreads(first: 100) { nodes { id isResolved comments(first: 1) { nodes { id body path line } } } } } } }' -f owner="$GITHUB_REPOSITORY_OWNER" -f name="${GITHUB_REPOSITORY#*/}" -F pr=PR_NUMBER
   ```
   Replace `PR_NUMBER` with the actual pull request number from the trigger event. Parse the JSON response and extract the `id` field (starts with `PRRT_`) for each unresolved thread. You MUST use these real IDs when resolving threads — NEVER fabricate or guess thread IDs.

7. Read the unresolved review comment threads on the PR (not just the latest review — get all unresolved threads). If there are more than 10 unresolved threads, address the first 10 and leave a summary comment on the PR noting how many remain for manual follow-up.

8. For each unresolved review comment thread (up to 10):
   a. Read the comment and understand what change is being requested
   b. Read the relevant file and surrounding code context
   c. Make the requested fix in the code (edit the file locally — do NOT push yet)
   d. Reply to the comment thread explaining what you changed
   e. Resolve the thread using the real thread ID from step 6

9. ***MUST***: Reply to and resolve ALL threads BEFORE pushing any code. Pushing code invalidates thread IDs and makes them unresolvable. Do NOT emit a push_to_pull_request_branch safe-output until all reply and resolve safe-outputs have been emitted.

10. After addressing all comments, run the CI checks locally to make sure your fixes don't break anything: `uv sync && uv run ruff check --fix . && uv run ruff format . && uv run pyright && uv run pytest --cov --cov-fail-under=80 -v`

11. ***DOUBLE CHECK***: Before you finish, verify that your safe-output calls are in the correct order: all reply_to_pull_request_review_comment and resolve_pull_request_review_thread calls MUST come BEFORE any push_to_pull_request_branch call. If you emitted them in the wrong order, you cannot fix it — the threads will fail to resolve and the PR will be stuck.

If a review comment requests a change that would be architecturally significant or you're unsure about, reply to the thread explaining your concern rather than making the change blindly. You MUST still resolve the thread after replying — an unresolved thread blocks the PR from merging regardless of whether you made the change.

***MUST***: Every thread you addressed or decided not to address MUST be replied to and resolved. No unresolved threads should remain after your run — unresolved threads block the PR from merging.

***MUST***: Push all changes in a single commit with message "fix: address review comments". This is the ONLY push in this workflow — it comes after all replies, resolutions, and CI checks.
