---
on:
  pull_request_review:
    types: [submitted]

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
  submit-pull-request-review:
    max: 1
    footer: "if-body"
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
  add-comment:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Quality Gate

Evaluate pull request #${{ github.event.pull_request.number }} for autonomous merge eligibility.

## Instructions

This workflow runs when a review is submitted on a pull request.

1. First, check if the PR has the `aw` label. If it does NOT have the `aw` label, stop immediately — this workflow only evaluates agent-created PRs.

2. Check the review that triggered this workflow. This workflow should only proceed when:
   - The review is an APPROVAL
   - The reviewer is `copilot-pull-request-reviewer` (the Copilot reviewer bot)
   If the triggering review is not a Copilot approval, stop immediately.

3. Verify that CI checks are passing on the PR. If CI is still running or has failures, stop — do not evaluate until CI passes.

4. Evaluate the PR across these dimensions:

   **Code Quality (must be good to proceed):**
   - Are the changes well-structured and follow existing patterns?
   - Are tests included and meaningful (not just no-ops)?
   - Are there any obvious bugs, race conditions, or security issues?

   **Blast Radius / Impact Assessment:**
   - LOW: Test-only changes, documentation, dead code removal, renaming
   - MEDIUM: Refactoring with existing test coverage, adding new utility functions, fixing lint issues
   - HIGH: Changes to core business logic, API contracts, data models, dependency updates, security-sensitive code

5. Make your decision:
   - If code quality is good AND impact is LOW or MEDIUM: Submit an APPROVE review. Keep the review body empty (the footer setting will handle the rest).
   - If code quality is good but impact is HIGH: Add a comment to the PR explaining: what the high-impact areas are, why manual review is recommended, and what specifically a human reviewer should look at. Do NOT approve.
   - If code quality is poor: Add a comment explaining the quality concerns. Do NOT approve.

Be conservative — when in doubt about impact level, round up. It's better to flag something for human review than to auto-merge a risky change.
