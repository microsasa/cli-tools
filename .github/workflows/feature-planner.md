---
# Feature planner — every 3 hours
on:
  schedule: every 3 hours
  workflow_dispatch:

permissions:
  contents: read
  issues: read
  pull-requests: read

engine: copilot

tools:
  github:
    toolsets: [default]

network: defaults

safe-outputs:
  noop:
    report-as-issue: false
  create-issue:
    max: 1
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}

---

# Feature Planner

Read the product vision, compare it against the current codebase, and file one implementable issue that moves the project closer to the vision.

## Instructions

Read `.github/copilot-instructions.md` and all referenced guidelines.

Read `.github/PRODUCT_VISION.md` — this is the target state for the project. If the file is empty or contains only whitespace, stop and do not create any issues.

Read all files in the repository. This is the current state.

Read all open issues in the repository. Check if any open issue has the `auto-feature` label. If so, stop — there is already a feature step in progress. Do not create another one.

Compare the current codebase against the vision. Identify one small, concrete step that would move the project closer to the vision. The step must be:

- **Implementable in a single PR** by an agent — no multi-PR epics, no "design a system" issues.
- **Specific enough to code** — include the files to create or modify, the behavior to add, and how to verify it works.
- **Non-conflicting** — do not propose changes that would contradict or duplicate any open issue.
- **Incremental** — prefer the smallest useful step over an ambitious leap. Each step should leave the codebase in a working, releasable state.

Open an issue with: what the step achieves toward the vision, what specific changes are needed, and a testing requirement. Prefix the title with `[aw][feature]` and label the issue with `aw` and `auto-feature`.

If the codebase already matches the vision, do not create any issues.
