---
# Weekly test suite analysis
on:
  schedule: weekly on monday
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
  create-issue:
    max: 2
  create-agent-session:
    max: 2

---

# Test Suite Analysis

Analyze the test suite for coverage gaps and suggest new tests.

## Instructions

Read all files in the repository. Identify meaningful test gaps across unit tests, e2e tests, and integration tests — untested code paths, missing scenarios, weak assertions, or anything else that would improve confidence in the code.

For each area with gaps, open an issue with root cause analysis, repro steps where applicable, and a clear spec for what tests to add. Each issue must specify the expected behavior to assert and any regression scenarios to cover. Assign copilot to work on each issue.

Do not suggest trivial tests or tests that duplicate existing coverage. Do not open an issue that is just a nit — if there are many small gaps that together form a meaningful improvement, bundle them into one issue. If the test suite is already comprehensive, do not create any issues.
