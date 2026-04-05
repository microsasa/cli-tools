# Replicating the Autonomous Agent Pipeline

A step-by-step guide for building an autonomous code improvement pipeline
using GitHub Agentic Workflows (gh-aw). This guide is designed for both
humans and AI agents — each section includes the explanation (the why) and
a ready-to-use prompt (the how) that an Opus 4.6 agent can execute directly.

**What you'll build:** A pipeline where scheduled agents find issues in your
codebase (bugs, test gaps, performance problems), an implementer agent fixes
them, Copilot reviews the fix, a responder addresses review comments, and a
quality gate approves the PR for auto-merge. Zero human intervention for
low/medium-impact changes.

**Prerequisites:** A GitHub repository with code, CI, and a `GITHUB_TOKEN`.

---

## Phase 1: Foundation

### 1.1 Install gh-aw and initialize the repo

```bash
gh extension install github/gh-aw
gh aw init
```

This creates `.github/agents/agentic-workflows.agent.md` and configures
VS Code settings. Commit and push.

### 1.2 Create secrets

You need two secrets in your repo settings (Settings → Secrets → Actions):

| Secret | Purpose | Scopes needed |
|--------|---------|---------------|
| `COPILOT_GITHUB_TOKEN` | Authenticates the Copilot CLI agent inside the sandbox | PAT with `Copilot Requests` permission |
| `GH_AW_WRITE_TOKEN` | Authenticates safe-output operations (creating PRs, posting comments) | PAT with `repo`, `workflow` scopes |

These can be the same PAT if it has all scopes. Using separate tokens gives
better auditability.

### 1.3 Enable repo settings

```bash
# Enable auto-merge
gh api repos/OWNER/REPO -X PATCH -f allow_auto_merge=true

# Enable Copilot auto-review via ruleset
gh api repos/OWNER/REPO/rulesets -X POST --input - <<'EOF'
{
  "name": "Copilot Auto-Review",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] }
  },
  "rules": [
    {
      "type": "copilot_code_review",
      "parameters": { "review_on_push": true, "review_draft_pull_requests": false }
    }
  ]
}
EOF
```

Set branch protection (adjust to your needs):

```bash
gh api repos/OWNER/REPO/branches/main/protection -X PUT --input - <<'EOF'
{
  "required_status_checks": { "strict": true, "contexts": ["YOUR_CI_JOB_NAME"] },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_conversation_resolution": true
}
EOF
```

> **Critical:** The `contexts` value must be the **job name** in your CI
> workflow, not the workflow name. Getting this wrong permanently blocks PRs.
> See [commit 56bcc72](../../commit/56bcc72) for when we got this wrong.

### 1.4 Create coding guidelines

Create `.github/copilot-instructions.md` as a central hub that points to
your guidelines:

**Prompt for agent:**
```
Create .github/copilot-instructions.md that points to .github/CODING_GUIDELINES.md.
The instructions file should say "Read and follow .github/CODING_GUIDELINES.md for
all code changes" and include a repository layout section.

Then create .github/CODING_GUIDELINES.md with coding standards for this project.
Read the existing codebase to determine: language, linter/formatter config,
type checking settings, test framework, and any patterns used. Write guidelines
that match what's already in use.

Reference: https://github.com/microsasa/cli-tools/commit/0a425c6
```

---

## Phase 2: The Issue Implementer

The implementer is the workhorse — it reads an issue, writes the code, runs
tests, and opens a PR.

### 2.1 Create the implementer workflow

**Prompt for agent:**
```
Create .github/workflows/issue-implementer.md — a gh-aw agentic workflow that:

1. Triggers on workflow_dispatch with an issue_number input
2. Uses copilot engine with claude-opus-4.6 model
3. Has read permissions for contents, issues, pull-requests
4. Allows network access for defaults + your language ecosystem (e.g., python)
5. Safe outputs: create-pull-request (draft: false, auto-merge: true,
   protected-files: fallback-to-issue, labels: [aw]), push-to-pull-request-branch
6. Disable noop issue reporting (report-as-issue: false)

Instructions should tell the agent to:
- Read .github/copilot-instructions.md and follow all referenced guidelines
- Read the issue and implement the fix
- Run the project's CI check suite locally before committing
- Open a non-draft PR with "Closes #N" in the body

Then compile: gh aw compile issue-implementer

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/issue-implementer.md
```

### 2.2 Test manually

```bash
# Create a simple issue
gh issue create --title "test: add a basic test" --body "Add a test for [function]" --label aw

# Trigger the implementer
gh workflow run "Issue Implementer" -f issue_number=N
```

Watch the Actions tab. The implementer should create a PR.

---

## Phase 3: Review and Fix Agents

### 3.1 Review Responder

Addresses Copilot's review comments on agent PRs.

**Key design decisions:**
- Trigger: `workflow_dispatch` (NOT `pull_request_review` — that causes loops)
- Must pre-fetch review comments because MCP tools return `[]` in the sandbox
- Uses a shared import for the pre-fetch step
- Loop prevention: `aw-review-response-attempted` label + round tracking

**Prompt for agent:**
```
Create two files:

1. .github/workflows/shared/fetch-review-comments.md — a shared component
   with a steps: block that runs gh api graphql to fetch all review threads
   for a PR, writes them to /tmp/gh-aw/review-data/unresolved-threads.json.
   Do NOT add a tools: block — that breaks the agent's tool permissions.

2. .github/workflows/review-responder.md — a gh-aw workflow that:
   - Triggers on workflow_dispatch with pr_number input
   - Imports shared/fetch-review-comments.md
   - Uses checkout with fetch: ["*"] and fetch-depth: 0
   - Safe outputs: push-to-pull-request-branch (target: "*"),
     reply-to-pull-request-review-comment (target: "*", max: 10),
     add-labels
   - Instructions: check for aw-review-response-attempted label (stop if
     present to prevent loops), add the label, read pre-fetched threads,
     fix each issue, reply to each thread, run CI, push once

Then compile both: gh aw compile

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/review-responder.md
Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/shared/fetch-review-comments.md
```

### 3.2 CI Fixer

Fixes CI failures on agent PRs.

**Prompt for agent:**
```
Create .github/workflows/ci-fixer.md — a gh-aw workflow that:

1. Triggers on workflow_dispatch with pr_number input
2. Uses checkout with fetch: ["*"] and fetch-depth: 0
3. Safe outputs: push-to-pull-request-branch (target: "*"),
   add-labels, add-comment
4. Loop prevention: check for aw-ci-fix-attempted label, add it, stop
   if already present
5. Instructions: read the PR, check CI logs for failures, fix the issues,
   run CI locally, push fixes

Then compile: gh aw compile ci-fixer

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/ci-fixer.md
```

### 3.3 Quality Gate

Evaluates PRs for autonomous merge eligibility.

**Prompt for agent:**
```
Create .github/workflows/quality-gate.md — a gh-aw workflow that:

1. Triggers on workflow_dispatch with pr_number input
2. Safe outputs: submit-pull-request-review (target: ${{ inputs.pr_number }},
   max: 1, footer: "always"), close-pull-request (target: ${{ inputs.pr_number }},
   max: 1), add-comment, add-labels (target: "*")
3. Instructions: verify aw label, verify CI passing, evaluate code quality
   and blast radius (LOW/MEDIUM/HIGH). LOW/MEDIUM → approve + add
   aw-quality-gate-approved label. HIGH → add comment explaining why human
   review is needed. Poor quality → close PR.

Note: submit-pull-request-review does NOT support target: "*" — use
target: ${{ inputs.pr_number }} instead.

Then compile: gh aw compile quality-gate

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/quality-gate.md
```

---

## Phase 4: The Pipeline Orchestrator

This is a regular GitHub Actions workflow (bash), NOT a gh-aw agent. It
runs in seconds, not minutes.

**Why bash, not an agent:** The orchestrator does deterministic if/else logic
(check CI status → dispatch responder). An agent took 7-10 minutes per run
for this same logic. See [docs/auto_pr_orchestrator_aw.md](auto_pr_orchestrator_aw.md)
for the full postmortem.

**Prompt for agent:**
```
Create .github/workflows/pipeline-orchestrator.yml — a regular GitHub Actions
workflow (NOT gh-aw) that:

Triggers:
- workflow_run (after CI, Copilot review, responder, ci-fixer, quality-gate)
- pull_request_review (for aw-labeled PRs only)
- schedule (every 30 minutes)
- workflow_dispatch (with optional pr_number)

Concurrency: group pipeline-orchestrator, cancel-in-progress: false

Permissions: contents write, pull-requests write, issues write, actions write

The job should:
1. Find all open aw-labeled PRs (excluding aw-pr-stuck:* labeled ones)
2. Find the oldest eligible aw issue (has aw label, no aw-dispatched, no
   agentic-workflows, no aw-protected-files) and dispatch the implementer
   if no implementer is in flight
3. For each aw PR:
   a. Check CI status — if failed and no ci-fix-attempted, dispatch ci-fixer
   b. Check for unresolved review threads — resolve threads where the last
      commenter is not copilot-pull-request-reviewer, dispatch responder if
      unresolved threads remain
   c. Check for Copilot review on current commit — request one if missing
   d. If CI green + threads resolved + Copilot reviewed + no quality gate
      in flight → dispatch quality gate
   e. If PR is behind main → rebase
   f. If stuck after multiple attempts → label aw-pr-stuck:*

Use gh api graphql for all state queries. Check for in-flight workflows
before dispatching. Use GH_AW_WRITE_TOKEN for all API calls.

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/pipeline-orchestrator.yml
```

---

## Phase 5: Scheduled Analysis Agents

These create the issues that the implementer works on.

### 5.1 Code Health

**Prompt for agent:**
```
Create .github/workflows/code-health.md — scheduled every 6 hours:
- Read copilot-instructions.md and all referenced guidelines
- Read all files and open issues
- Find cleanup opportunities (refactoring, dead code, inconsistencies)
- Do NOT report performance problems (those belong to perf-analysis)
- File up to 2 issues, prefixed [aw][code health], labeled aw + code-health
- Prioritize by severity if findings exceed the cap

Then compile: gh aw compile code-health

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/code-health.md
```

### 5.2 Test Analysis

```
Create .github/workflows/test-analysis.md — scheduled every 6 hours:
- Find meaningful test gaps (untested paths, missing scenarios, weak assertions)
- File up to 2 issues, prefixed [aw][test audit], labeled aw + test-audit
- Prioritize by risk (core logic over edge cases)

Then compile: gh aw compile test-analysis
```

### 5.3 Performance Analysis

```
Create .github/workflows/perf-analysis.md — scheduled every 6 hours:
- Find performance problems (algorithmic inefficiency, redundant I/O, etc.)
- Do NOT limit categories — any meaningful improvement is in scope
- File up to 2 issues, prefixed [aw][perf], labeled aw + perf
- Prioritize by impact (hot paths over cold code)

Then compile: gh aw compile perf-analysis
```

### 5.4 Create required labels

```bash
gh label create aw --description "Created by agentic workflow" --color "7057ff"
gh label create code-health --description "Code cleanup and maintenance" --color "0e8a16"
gh label create test-audit --description "Test coverage improvement" --color "0e8a16"
gh label create perf --description "Performance improvement" --color "D4C5F9"
```

---

## Phase 6: Feature Planner (Optional)

An agent that reads a product vision document and files one implementable
feature issue per run — incrementally moving the codebase toward the vision.

**Prompt for agent:**
```
Create .github/PRODUCT_VISION.md — empty file (user fills it in later).

Create .github/workflows/feature-planner.md — scheduled every 3 hours:
- Read PRODUCT_VISION.md. If empty or whitespace-only, stop.
- Read all source files (current state) and all open issues
- Check if any open issue has the auto-feature label. If so, stop.
- File one small, implementable step toward the vision
- Prefix [aw][feature], label aw + auto-feature
- Max 1 issue per run

Then compile: gh aw compile feature-planner

Reference: https://github.com/microsasa/cli-tools/blob/main/.github/workflows/feature-planner.md
```

```bash
gh label create auto-feature --description "Feature step toward product vision" --color "1D76DB"
```

---

## Known Issues and Workarounds

### MCP server doesn't expose review thread IDs

The GitHub MCP server strips `PRRT_` thread node IDs during response
minimization. Agents can reply to threads but can't resolve them via MCP.

**Workaround:** Pre-fetch threads via `gh api graphql` in a shared import
step that runs before the agent. Write to `/tmp/gh-aw/review-data/`.
See [shared/fetch-review-comments.md](../../.github/workflows/shared/fetch-review-comments.md).

**Upstream:** [github/github-mcp-server#2245](https://github.com/github/github-mcp-server/pull/2245) (open, unmerged).

### MCP gateway 5-minute idle timeout

SSE connections die after exactly 5 minutes of no MCP traffic. Agents that
spend >5 minutes on local work before calling safe-outputs fail silently.

**Status:** gh-aw v0.65.7 added `keepalive-interval` frontmatter option but
the gateway binary (v0.2.12) rejects it during schema validation. No fix
available as of v0.66.1.

**Mitigation:** Keep agent tasks small so safe-output calls happen within
5 minutes. The pipeline's retry mechanism (re-dispatch on failure) provides
some resilience.

### Copilot code review drip-feeds comments

Copilot leaves ~2 comments per review pass. After the responder fixes them,
Copilot finds more on re-review. This creates multi-round cycles that can
exhaust the responder's attempt limit. No configurable setting exists.

**Mitigation:** The responder gets 3 attempts. If it's still stuck, the
orchestrator labels the PR `aw-pr-stuck:review` for human intervention.

### Dependabot PRs for gh-aw

Never merge Dependabot PRs that modify `.lock.yml` files directly. Instead:
upgrade `gh aw` CLI, run `gh aw compile`, update `copilot-setup-steps.yml`,
and create a single PR. Use `@dependabot rebase` for non-lock-file PRs.

---

## Build Order Summary

| Phase | What | Time to implement |
|-------|------|-------------------|
| 1 | Foundation (gh-aw init, secrets, settings, guidelines) | 30 minutes |
| 2 | Issue Implementer | 15 minutes |
| 3 | Review Responder + CI Fixer + Quality Gate | 30 minutes |
| 4 | Pipeline Orchestrator | 1-2 hours (complex bash) |
| 5 | Scheduled Analysis Agents | 15 minutes |
| 6 | Feature Planner (optional) | 10 minutes |

Phases 1-3 give you the core loop. Phase 4 makes it autonomous. Phases 5-6
make it self-directing.

---

## Reference Implementation

This guide is based on the pipeline built at
[microsasa/cli-tools](https://github.com/microsasa/cli-tools). Key files:

| File | Purpose |
|------|---------|
| [pipeline-orchestrator.yml](../../.github/workflows/pipeline-orchestrator.yml) | Bash orchestrator |
| [issue-implementer.md](../../.github/workflows/issue-implementer.md) | Implementer agent |
| [review-responder.md](../../.github/workflows/review-responder.md) | Review responder |
| [ci-fixer.md](../../.github/workflows/ci-fixer.md) | CI fixer |
| [quality-gate.md](../../.github/workflows/quality-gate.md) | Quality gate |
| [code-health.md](../../.github/workflows/code-health.md) | Code health scanner |
| [test-analysis.md](../../.github/workflows/test-analysis.md) | Test gap finder |
| [perf-analysis.md](../../.github/workflows/perf-analysis.md) | Performance analyzer |
| [feature-planner.md](../../.github/workflows/feature-planner.md) | Feature planner |
| [shared/fetch-review-comments.md](../../.github/workflows/shared/fetch-review-comments.md) | Pre-fetch workaround |
| [CODING_GUIDELINES.md](../../.github/CODING_GUIDELINES.md) | Coding standards |
| [copilot-instructions.md](../../.github/copilot-instructions.md) | Copilot instruction hub |
| [agentic-workflows.md](agentic-workflows.md) | Lessons learned (42 pitfalls) |
