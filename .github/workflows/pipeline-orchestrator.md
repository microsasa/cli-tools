---
on:
  schedule:
    - cron: "*/15 * * * *"
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: pipeline-orchestrator
  cancel-in-progress: false

permissions:
  contents: read
  issues: read
  pull-requests: read
  actions: read

engine:
  id: copilot
  model: claude-opus-4.6

tools:
  github:
    toolsets: [default, actions]
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  bash:
    - "gh:api:graphql"

network:
  allowed:
    - defaults

safe-outputs:
  noop:
    report-as-issue: false
  dispatch-workflow:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    workflows: [issue-implementer, ci-fixer]
    max: 1
  add-reviewer:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    max: 3
  resolve-pull-request-review-thread:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    max: 10
  add-labels:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    max: 10
  remove-labels:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    max: 10
  add-comment:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    max: 5

---

# Pipeline Orchestrator

Own the full lifecycle of agent work: from issue to merged PR. Detect what needs attention and push it forward one step at a time.

## Context

This repository has an automated pipeline:
1. code-health or test-analysis creates issues (labeled `code-health` or `test-audit`)
2. issue-implementer creates a PR from the issue (labeled `aw`, auto-merge enabled)
3. Copilot auto-reviews the PR
4. review-responder addresses review comments and resolves threads
5. quality-gate approves if code quality is good and impact is low/medium
6. auto-merge fires when CI passes + approved + threads resolved

This orchestrator owns steps 2-6. It detects stalls and fixes them.

## Instructions

### Step 1: Find issues that need implementation

First, check if there are any open PRs with the `aw` label. If there are, skip issue dispatch entirely — only one agent PR should be in flight at a time to avoid merge conflicts.

Also check if any `issue-implementer` workflow runs are currently in progress (queued or running). If so, skip issue dispatch — an implementer is already working on something and will create a PR soon.

If there are NO open `aw`-labeled PRs AND no in-progress implementer runs, list open issues with the `code-health` or `test-audit` label. For each issue, check if there is already an open or recently merged PR that references it (look for PRs whose body contains "Closes #N" or "#N" where N is the issue number).

Dispatch the `issue-implementer` workflow for the **first** eligible issue only (one at a time). Add a comment on the issue: "Pipeline Orchestrator: dispatching issue-implementer."

If no issues need implementation, move on to Step 2.

### Step 2: Find stuck PRs

List all open PRs with the `aw` label that have auto-merge enabled. Exclude any PR labeled `aw-conflict` (merge conflicts, needs manual intervention).

If there are no issues to dispatch and no stuck PRs, stop with a noop.

Sort PRs by progress: approved PRs first (closest to merging), then unapproved.

### Step 3: Process each PR

For each PR (in sorted order), gather its state:
- `mergeStateStatus` (BEHIND, CLEAN, BLOCKED, etc.)
- `reviewDecision` (APPROVED, REVIEW_REQUIRED, etc.)
- Whether Copilot has submitted a review (look for reviews by author `copilot-pull-request-reviewer`)
- Whether the latest CI check run (`check` job) has failed
- Whether the PR has the `ci-fix-attempted` label

Then apply the **first matching** action and move to the next PR:

#### Action 1: Request Copilot review

If Copilot has not reviewed this PR yet, request a review from `@copilot` using the add-reviewer safe-output. Stop processing this PR — the pipeline will continue from here next cycle.

#### Action 2: Resolve unresolved threads

Query the PR's review threads using bash. Use `$GITHUB_MCP_SERVER_TOKEN` for authentication:
```
GH_TOKEN="$GITHUB_MCP_SERVER_TOKEN" gh api graphql -f query='query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(last: 1) {
            nodes { author { login } }
          }
        }
      }
    }
  }
}' -f owner="$GITHUB_REPOSITORY_OWNER" -f name="${GITHUB_REPOSITORY#*/}" -F pr=PR_NUMBER
```
Replace `PR_NUMBER` with the actual PR number.

For each unresolved thread, check the last comment's author. The review-responder posts replies using a PAT owned by the repository owner, so its comments appear as the value of `$GITHUB_REPOSITORY_OWNER`. Check this environment variable to determine the responder's identity.

If the last comment was posted by the responder (PAT owner) or by `github-actions[bot]`, resolve the thread using the resolve-pull-request-review-thread safe-output.

If any unresolved threads remain where the last commenter is someone else (human or Copilot reviewer), stop processing this PR — it needs attention.

#### Action 3: CI failure

If the latest CI `check` run has failed and the PR does NOT have the `ci-fix-attempted` label, dispatch the `ci-fixer` workflow with the PR number as input. The ci-fixer will read the logs, fix the issues, and push. Stop processing this PR — next cycle will check again.

If CI has failed but the PR already has `ci-fix-attempted`, skip — the fixer already tried once and manual intervention is needed.

#### Action 4: Behind main

If the PR is approved, all threads resolved, but `mergeStateStatus` is `BEHIND`:
- Log that the PR is approved and ready but needs a rebase to proceed.
- Do NOT attempt to rebase and do NOT comment on the PR — this requires manual intervention.
- Move to the next PR.

#### Action 5: All clear

If the PR is approved, threads resolved, and not behind main — auto-merge should handle it. Log this and move on.

### Step 4: Summary

After processing all PRs, output a brief summary of actions taken.

## Important rules

- Process PRs one at a time, in sorted order
- Apply only the FIRST matching action per PR, then move to the next
- If any API call fails for a PR, skip it and continue — one failure must not stop the entire run
- NEVER fabricate thread IDs — always use real IDs from the GraphQL response
- The `aw-conflict` label means merge conflicts exist — skip these PRs entirely
