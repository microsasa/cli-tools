---
on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: PR number to evaluate for merge eligibility
        required: true
        type: string

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
    toolsets: [default]

network: defaults

safe-outputs:
  noop:
    report-as-issue: false
  submit-pull-request-review:
    max: 1
    target: "*"
    footer: "always"
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  close-pull-request:
    max: 1
    target: "*"
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-comment:
    target: "*"
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-labels:
    target: "*"
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Quality Gate

Evaluate pull request #${{ inputs.pr_number }} for autonomous merge eligibility.

## Instructions

This workflow is dispatched by the pipeline orchestrator when a PR has CI green and all review threads resolved.

1. Fetch the PR details for PR #${{ inputs.pr_number }}. Verify it has the `aw` label. If not, stop immediately.

2. Verify that CI checks are passing on the PR. If CI is still running or has failures, stop — do not evaluate until CI passes.

3. Evaluate the PR across these dimensions:

   **Code Quality (must be good to proceed):**
   - Are the changes well-structured and follow existing patterns?
   - Are tests included and meaningful (not just no-ops)?
   - Are there any obvious bugs, race conditions, or security issues?

   **Blast Radius / Impact Assessment:**
   - LOW: Test-only changes, documentation, dead code removal, renaming
   - MEDIUM: Refactoring with existing test coverage, adding new utility functions, fixing lint issues
   - HIGH: Changes to core business logic, API contracts, data models, dependency updates, security-sensitive code

4. Make your decision:
   - If code quality is good AND impact is LOW or MEDIUM: Submit an APPROVE review with a brief summary of what was evaluated (e.g., "Low-impact test addition with good coverage. Auto-approving for merge."). Also add the label `aw-quality-gate-approved` to the PR — this label is used by the PR Rescue workflow to know which PRs are safe to re-approve after rebase. The PR has auto-merge enabled — your approval satisfies the required review and triggers automatic merge.
   - If code quality is good but impact is HIGH: Add a comment to the PR explaining: what the high-impact areas are, why manual review is recommended, and what specifically a human reviewer should look at. Do NOT approve — auto-merge will remain blocked until a human approves.
   - If code quality is poor: Close the PR with a comment explaining the quality concerns (what's wrong and what needs to be fixed). The source issue stays open so the implementer can retry. Do NOT approve.

Be conservative — when in doubt about impact level, round up. It's better to flag something for human review than to auto-merge a risky change.

Note: PRs created by the Issue Implementer have auto-merge enabled. Your APPROVE review is what triggers the merge. This is intentional — the pipeline is: Implementer creates PR → CI passes → Copilot reviews → Quality Gate evaluates and approves → GitHub auto-merges.
