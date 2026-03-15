# GitHub Agentic Workflows — Lessons Learned

A reference guide based on our experience building an autonomous agent pipeline with gh-aw (GitHub Agentic Workflows). Use this to avoid the same mistakes and get up and running faster.

---

<details>
<summary>Overview</summary>

gh-aw lets you write AI-powered GitHub Actions workflows in markdown with YAML frontmatter. Each `.md` file compiles to a `.lock.yml` file that runs as a standard GitHub Actions workflow. The AI agent runs inside a sandboxed environment with a firewall controlling network access.

**Key commands:**
```bash
gh aw init                    # Initialize repo for gh-aw
gh aw compile                 # Compile all .md → .lock.yml
gh aw compile workflow-name   # Compile a specific workflow
gh aw fix --write             # Auto-fix deprecated fields
```

</details>

---

<details>
<summary>Architecture</summary>

Our autonomous pipeline:

```
Audit/Health Agent → creates issue (max 2) → dispatches Implementer
  → Implementer creates PR (lint-clean, non-draft, auto-merge, aw label)
    → CI runs + Copilot auto-reviews (parallel, via ruleset)
      → CI fails? → CI Fixer agent (1 retry, label guard)
      → Copilot has comments? → Review Responder addresses them (1 attempt, label guard)
      → Copilot approves → Quality Gate evaluates quality + blast radius
        → LOW/MEDIUM impact → approves → auto-merge fires
        → HIGH impact → flags for human review
```

</details>

---

<details>
<summary>Workflow File Format</summary>

```yaml
---
on:
  pull_request_review:
    types: [submitted]
  bots: [Copilot, copilot-pull-request-reviewer]    # MUST be under on:, NOT top-level

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
    - python              # ecosystem identifier — covers pypi, conda, astral.sh

safe-outputs:
  noop:
    report-as-issue: false
  create-pull-request:
    github-token: ${{ secrets.GH_AW_WRITE_TOKEN }}
    draft: false          # IMPORTANT: defaults to true!
    auto-merge: true
---

# Workflow Title

Natural language instructions for the agent.
```

### Critical: Field placement matters

| Field | Correct placement | What happens if wrong |
|---|---|---|
| `bots:` | Under `on:` | Top-level compiles silently but is **ignored** — no `GH_AW_ALLOWED_BOTS` in lock file |
| `roles:` | Under `on:` (as `on.roles:`) | Top-level `roles:` is deprecated — use `on.roles:` |
| `draft:` | Under `safe-outputs.create-pull-request:` | N/A |

</details>

---

<details>
<summary>Compilation</summary>

After creating or modifying any `.md` workflow, you MUST compile:

```bash
gh aw compile                    # All workflows
gh aw compile workflow-name      # Specific workflow (without .md)
```

**Always verify the lock file changed as expected.** The compiler can silently accept invalid field placements (like top-level `bots:`) without emitting the expected output. Check with:

```bash
grep 'GH_AW_ALLOWED_BOTS\|GH_AW_REQUIRED_ROLES' .github/workflows/your-workflow.lock.yml
```

**Ecosystem identifiers** are preferred over individual domain names:
```yaml
# ❌ Verbose — compiler warns
network:
  allowed:
    - defaults
    - "pypi.org"
    - "conda.anaconda.org"
    - "astral.sh"

# ✅ Clean — no warnings
network:
  allowed:
    - defaults
    - python
```

</details>

---

<details>
<summary>Safe Outputs</summary>

Safe outputs are the structured way for agents to interact with GitHub. Key ones we use:

| Safe output | What it does | Key options |
|---|---|---|
| `create-pull-request` | Opens a PR | `draft: false` (default true!), `auto-merge: true`, `protected-files: fallback-to-issue` |
| `push-to-pull-request-branch` | Pushes commits to PR branch | — |
| `create-issue` | Creates issues | `max: 2` |
| `dispatch-workflow` | Triggers other workflows | `workflows: [name]`, `max: 3` |
| `submit-pull-request-review` | Approves/rejects PRs | `footer: "always"/"none"/"if-body"` |
| `reply-to-pull-request-review-comment` | Replies in review threads | `max: 10` |
| `resolve-pull-request-review-thread` | Resolves review threads | `max: 10` |
| `add-labels` | Adds labels to issues/PRs | — |
| `add-comment` | Adds a comment | — |
| `noop` | No-op reporting | `report-as-issue: false` to disable noise |

### Gotcha: `create-pull-request` defaults to draft

```yaml
safe-outputs:
  create-pull-request:
    draft: false           # Without this, all agent PRs are drafts
    auto-merge: true       # Enable auto-merge when checks pass
```

Draft PRs **cannot** be auto-reviewed by Copilot and **cannot** be auto-merged. Always set `draft: false` for autonomous pipelines.

### Gotcha: noop issue spam

By default, agents post to a tracking issue every time they run but decide no action is needed. Disable with:

```yaml
safe-outputs:
  noop:
    report-as-issue: false
```

</details>

---

<details>
<summary>Network Access</summary>

Agents run inside a firewall sandbox. By default (`network: defaults`) they can only reach GitHub APIs. If agents need to install packages (e.g., `uv sync`), they need explicit network access.

```yaml
network:
  allowed:
    - defaults
    - python    # pypi.org, astral.sh, conda.anaconda.org, repo.anaconda.com
```

**Without this, `uv sync`, `pip install`, etc. will fail silently or with firewall blocked errors.** The PR body will show a warning listing blocked domains.

</details>

---

<details>
<summary>Triggers and Activation</summary>

### The pre_activation gate

Every compiled workflow has a `pre_activation` job that checks if the triggering actor has permission to run the workflow. It uses:

- `GH_AW_REQUIRED_ROLES` — default: `admin,maintainer,write`
- `GH_AW_ALLOWED_BOTS` — from `on.bots:` field

The `check_membership.cjs` script (in `github/gh-aw`) works as follows:
1. Check if event is "safe" (schedule, merge_group, workflow_dispatch with write role) → auto-approve
2. Check actor's repo permission against `GH_AW_REQUIRED_ROLES` → approve if match
3. **Fallback**: Check if actor is in `GH_AW_ALLOWED_BOTS` AND bot is active/installed on repo → approve as `authorized_bot`

### Allowing bot triggers

For workflows triggered by bot events (e.g., Copilot reviewer submitting a review):

```yaml
on:
  pull_request_review:
    types: [submitted]
  bots: [Copilot, copilot-pull-request-reviewer]   # MUST be under on:
```

This compiles to `GH_AW_ALLOWED_BOTS: Copilot,copilot-pull-request-reviewer` in the lock file, which `check_membership.cjs` checks as a fallback.

> **IMPORTANT**: The event **actor** for Copilot reviews is `Copilot` (the GitHub App), NOT `copilot-pull-request-reviewer` (the review author login). `check_membership.cjs` matches `context.actor` against the bots list, so `Copilot` is the identity that matters. Include both for safety.

**DO NOT use `roles: all` just to allow bots.** It opens the workflow to any actor. Use `bots:` instead.

### GitHub's `action_required` gate

Separate from gh-aw's `pre_activation`, GitHub Actions itself has an approval gate for first-time contributors. When a bot (like `copilot-pull-request-reviewer[bot]`) triggers a workflow for the first time, GitHub may pause the run with `action_required` status — no jobs run at all.

**Fix**: In repo Settings → Actions → General → "Fork pull request workflows from outside collaborators", adjust the approval requirement. (TODO: determine exact setting needed)

### Concurrency

The implementer uses a concurrency group:
```yaml
concurrency:
  group: "gh-aw-${{ github.workflow }}"
```

This means only one implementer runs at a time. If audit creates 2 issues and dispatches 2 implementers, the second waits for the first to complete.

</details>

---

<details>
<summary>Copilot Integration</summary>

### Requesting Copilot review manually

```bash
gh pr edit <PR> --add-reviewer @copilot    # Requires gh CLI v2.88+
```

**Cannot self-approve PRs.** The `@copilot` syntax with the `@` prefix is required.

### Auto-review via ruleset

```bash
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
      "parameters": {
        "review_on_push": true,
        "review_draft_pull_requests": false
      }
    }
  ]
}
EOF
```

**Note**: The rule type is `copilot_code_review`, not `copilot_review`. The `parameters` field names are `review_on_push` (not `review_new_pushes`).

### Copilot review behavior

- **APPROVED**: Only when Copilot has zero concerns on a code-changing PR
- **COMMENTED**: When Copilot has inline comments, or on workflow-only PRs
- **Draft PRs**: Copilot does NOT review draft PRs (even if manually requested)
- **Reviewer identity**: `copilot-pull-request-reviewer[bot]` (login: `copilot-pull-request-reviewer`)
- **Event actor**: `Copilot` (the GitHub App identity — this is what `context.actor` returns and what `check_membership.cjs` matches against)

### Addressing Copilot review comments (GraphQL)

```bash
# Get thread IDs
gh api graphql -f query='query {
  repository(owner: "OWNER", name: "REPO") {
    pullRequest(number: N) {
      reviewThreads(first: 20) {
        nodes { id comments(first: 1) { nodes { id body path } } }
      }
    }
  }
}'

# Reply to a thread
gh api graphql -f query='mutation {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: "PRRT_...", body: "Fixed — ..."
  }) { comment { id } }
}'

# Resolve a thread
gh api graphql -f query='mutation {
  resolveReviewThread(input: {threadId: "PRRT_..."}) {
    thread { isResolved }
  }
}'
```

</details>

---

<details>
<summary>Repo Settings</summary>

Settings required for the autonomous pipeline:

| Setting | How to set | Value |
|---|---|---|
| Auto-merge | `gh api repos/OWNER/REPO -X PATCH -f allow_auto_merge=true` | `true` |
| Branch protection: required reviews | API (see below) | 1 approving review |
| Branch protection: dismiss stale | API | `true` |
| Branch protection: required status | API | `check` |
| Branch protection: enforce admins | API | `true` |
| Copilot auto-review | Ruleset API (see above) | Active, review on push |
| Actions: first-time contributor approval | GitHub UI (Settings → Actions → General) | TBD |

### Branch protection API

```bash
gh api repos/OWNER/REPO/branches/main/protection -X PUT --input - <<'EOF'
{
  "required_status_checks": { "strict": true, "contexts": ["check"] },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "required_approving_review_count": 1
  },
  "restrictions": null
}
EOF
```

### Admin merge workaround (solo repos)

With `enforce_admins: true` and 1 required approval, you can't merge your own PRs without an external approver. Workaround:

```bash
# Temporarily disable enforce_admins
gh api repos/OWNER/REPO/branches/main/protection/enforce_admins -X DELETE

# Admin merge
gh pr merge <PR> --merge --admin --delete-branch

# Re-enable
gh api repos/OWNER/REPO/branches/main/protection/enforce_admins -X POST
```

This is a known limitation for solo repos. Agent PRs don't need this — the quality gate approves them.

</details>

---

<details>
<summary>Common Pitfalls</summary>

### 1. `bots:` must be under `on:`, not top-level
The compiler accepts top-level `bots:` without error but ignores it. Always put it under `on:`.

### 2. `create-pull-request` defaults to `draft: true`
Agent PRs will be drafts unless you explicitly set `draft: false`. Drafts can't be auto-reviewed or auto-merged.

### 3. Agents can't install packages without network access
`network: defaults` only allows GitHub APIs. Add ecosystem identifiers (e.g., `python`) for package registries.

### 4. Lock files are auto-generated — don't edit them
All changes go in the `.md` file. Run `gh aw compile` to regenerate. Copilot may comment on lock file issues — reply that they're auto-generated.

### 5. `dismiss_stale_reviews` only dismisses APPROVED reviews
`COMMENTED` reviews are NOT dismissed on new pushes. This means a Copilot `COMMENTED` review from before a rebase will persist.

### 6. `pull_request_review` workflows run from default branch
The workflow definition always comes from the default branch, not the PR branch. You cannot test workflow changes from a PR — they must be merged first.

### 7. GitHub's `action_required` is separate from gh-aw's `pre_activation`
`action_required` means GitHub itself blocked the run (first-time contributor approval). No jobs run at all. `pre_activation` is gh-aw's role/bot check within the workflow.

### 8. Copilot has TWO identities — actor vs reviewer
The `pull_request_review` event actor (`context.actor`) is `Copilot`, but the review author login is `copilot-pull-request-reviewer`. `check_membership.cjs` matches against `context.actor`, so the `bots:` list MUST include `Copilot`. If you only list `copilot-pull-request-reviewer`, `pre_activation` will pass (job succeeds) but `activated` output will be `false` and the agent job gets skipped.

### 9. Always use merge commits
Never squash merge — it loses commit history and the user gets angry. Set merge method preference explicitly.

### 9. Issues are specs
Issues describe WHAT to do, not HOW. The implementer agent reads the issue and decides the implementation.

</details>

---

<details>
<summary>Debugging</summary>

### Check if a workflow compiled correctly
```bash
grep 'GH_AW_ALLOWED_BOTS\|GH_AW_REQUIRED_ROLES\|pre_activation' .github/workflows/your-workflow.lock.yml
```

### Check why a workflow run shows `action_required`
No jobs ran → GitHub's first-time contributor approval gate. Check repo Actions settings.

### Check why a workflow run shows `action_required` with jobs
The `pre_activation` job ran but the actor failed the role/bot check. Check:
- Is `bots:` under `on:` in the `.md` file?
- Does the lock file contain `GH_AW_ALLOWED_BOTS`?
- Does `GH_AW_ALLOWED_BOTS` contain the correct **actor** name? (For Copilot, the actor is `Copilot`, not `copilot-pull-request-reviewer`)
- Is the bot installed/active on the repo?

### Check if Copilot reviewed
```bash
gh pr view <PR> --json reviews --jq '.reviews[] | {author: .author.login, state: .state}'
```

### Check agent workflow runs
```bash
gh run list --workflow=review-responder.lock.yml --limit 5
gh run list --workflow=quality-gate.lock.yml --limit 5
gh run list --workflow=issue-implementer.lock.yml --limit 5
```

### View CI failure logs for a PR
```bash
gh pr checks <PR>                                    # See which checks failed
gh run view <RUN_ID> --log-failed                    # View failed job logs
```

</details>

---

<details>
<summary>Our Agent Inventory</summary>

| Agent | Trigger | Purpose | Safe outputs |
|---|---|---|---|
| `test-analysis.md` | schedule (weekly) / manual | Find test coverage gaps | `create-issue` (max 2), `dispatch-workflow` (implementer) |
| `code-health.md` | schedule (daily) / manual | Find refactoring/cleanup opportunities | `create-issue` (max 2), `dispatch-workflow` (implementer) |
| `issue-implementer.md` | `workflow_dispatch` (issue number) | Implement fix from issue spec, open PR | `create-pull-request` (draft: false, auto-merge), `push-to-pull-request-branch` |
| `ci-fixer.md` | `workflow_dispatch` (PR number) | Fix CI failures on agent PRs | `push-to-pull-request-branch`, `add-labels`, `add-comment` |
| `review-responder.md` | `pull_request_review` | Address Copilot review comments | `push-to-pull-request-branch`, `reply-to-review-comment`, `resolve-thread`, `add-labels` |
| `quality-gate.md` | `pull_request_review` | Evaluate quality + blast radius, approve or block | `submit-pull-request-review`, `add-comment` |

### Loop prevention

- **CI Fixer**: Checks for `ci-fix-attempted` label. CI dispatch also checks `!contains(labels, 'ci-fix-attempted')`. Max 1 retry.
- **Review Responder**: Checks for `review-response-attempted` label. Max 1 attempt.
- **All agents**: Only act on PRs with the `aw` label.

</details>

---

<details>
<summary>History</summary>

> This section is append-only. New entries are added at the bottom.

### 2026-03-14 — Initial agent setup and validation

- Set up `test-analysis.md`, `code-health.md`, and `issue-implementer.md` agents
- Validated test-audit pipeline end-to-end: agent scan → issue creation (#43, #44) → implementer dispatch → PR creation (#45, #46)
- Implementer PRs had trivial CI failures (ruff import ordering, pyright suppressions) — fixed manually
- Discovered `gh pr edit --add-reviewer @copilot` requires gh CLI v2.88+ (upgraded from v2.87.3)
- Copilot doesn't review draft PRs — must mark ready first with `gh pr ready`
- Old REST API approach (`gh api .../requested_reviewers -f 'reviewers[]=copilot'`) silently accepts but doesn't work
- Copilot reviewed PR #46 with 3 timing-flakiness comments — addressed by widening time gaps (days vs minutes) and explicit `_last_trigger` setting
- All PRs merged with merge commits (user preference — never squash)

### 2026-03-14 — Code-health agent validation

- Triggered code-health agent — found 2 real issues (#47: duplicated ModelMetrics merge, #48: dead EventBase + naming nits)
- Both implementers dispatched and completed successfully
- PR #50 (nits) had CI failure — pyright issue with `default_factory=list` losing type info in strict mode. Reverted to typed lambda.
- PR #49 (merge refactor) — Copilot suggested `model_copy(deep=True)` + in-place mutation instead of manual reconstruction. Good suggestion, implemented.

### 2026-03-14/15 — Autonomous pipeline build

- Built 3 new agents: ci-fixer, review-responder, quality-gate
- Upgraded implementer: lint before push, non-draft, auto-merge, aw label, Python network access
- Updated CI to dispatch ci-fixer on failure for aw-labeled PRs
- Disabled noop issue reporting across all agents (was creating spam tracking issues)
- Copilot reviewed pipeline PR (#51) with 5 comments — addressed overflow handling, dispatch guard, footer mode
- Two lock.yml comments about pre_activation gate — replied that lock files are auto-generated

### 2026-03-15 — Pipeline activation debugging

- Discovered `create-pull-request` defaults to `draft: true` — PR #57 added `draft: false`
- Enabled auto-merge on repo, created Copilot auto-review ruleset, set branch protection to 1 required approval
- Triggered test-audit → implementer created PR #61 (non-draft, aw label, CI green first try!) — pipeline progress!
- But review-responder and quality-gate showed `action_required` — agents never ran
- First theory: `pre_activation` role check blocking Copilot bot → added `bots:` to frontmatter
- Mistake: Put `bots:` at top level (PR #64) — compiled silently but was ignored. Wasted merge.
- Fix: `bots:` must be under `on:` — PR #65 corrects this, lock file now has `GH_AW_ALLOWED_BOTS`
- Discovered the `action_required` is actually GitHub's own first-time contributor approval gate, not gh-aw's pre_activation
- Read `check_membership.test.cjs` source to understand the actual logic: role check → bot allowlist fallback → active check
- Two blockers remain: (1) GitHub Actions approval setting for bot actors, (2) PR #65 for correct `bots:` placement
- Lesson: stop guessing, read the source code before making changes
- After PR #65 merge: `pre_activation` passes (job succeeds) but `activated` output still `false` — agent jobs skipped
- Root cause: `context.actor` is `Copilot` (GitHub App identity) but bots list had `copilot-pull-request-reviewer` (reviewer login) — name mismatch
- Fix: PR #72 adds both `Copilot` and `copilot-pull-request-reviewer` to bots list

</details>
