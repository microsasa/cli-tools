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
Audit/Health Agent → creates issue (labeled code-health or test-audit)
  → Pipeline Orchestrator (15-min cron) picks up the issue:
    → No aw-labeled PR in flight? → dispatches Implementer (one at a time)
  → Implementer creates PR (lint-clean, non-draft, auto-merge, aw label)
    → CI runs + Copilot auto-reviews (parallel, via ruleset)
      → CI fails? → CI Fixer agent (1 retry, label guard)
      → Copilot has comments? → Review Responder addresses them (pushes fixes, resolves threads via GraphQL)
      → Copilot reviews (COMMENTED state) → Quality Gate evaluates quality + blast radius
        → LOW/MEDIUM impact → approves + adds quality-gate-approved label → auto-merge fires
        → HIGH impact → flags for human review (auto-merge stays blocked)
    → PR stalled? → Pipeline Orchestrator detects and fixes:
      → No Copilot review → requests one
      → Unresolved threads with responder replies → resolves them
      → Behind main → logs, skips (requires manual rebase)
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

> **⚠️ KNOWN BUG ([github/gh-aw#21098](https://github.com/github/gh-aw/issues/21098))**: Step 3 is unreachable for GitHub App actors. When a bot like `Copilot` triggers a workflow, step 2 calls `getCollaboratorPermissionLevel("Copilot")` which returns a 404 ("not a user"). This error causes `check_membership.cjs` to exit immediately via the `if (result.error)` branch — **before ever reaching the bot fallback in step 3**. The `bots:` field compiles correctly but the runtime never evaluates it.

### Allowing bot triggers (WORKAROUND)

Due to the upstream bug above, the `bots:` field alone is insufficient. The current workaround is `roles: all`, which tells the compiler to skip the permission check entirely (`check_membership.cjs` is not included in the `pre_activation` job):

```yaml
on:
  pull_request_review:
    types: [submitted]
  roles: all
  bots: [Copilot, copilot-pull-request-reviewer]   # keep for when upstream is fixed
```

This is overly permissive — any actor can trigger the workflow. Track removal via issue #74.

> **Actor identity note**: The event **actor** for Copilot reviews is `Copilot` (the GitHub App), NOT `copilot-pull-request-reviewer` (the review author login). `context.actor` returns `Copilot`. Keep both in the bots list for when the upstream bug is fixed.

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

> **Pitfall**: Copilot auto-reviews almost always submit as `COMMENTED`, not `APPROVED`. Any downstream workflow that triggers on `pull_request_review` and checks the review state must accept `COMMENTED` reviews from Copilot — not just `APPROVED`. The Quality Gate handles this correctly.

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
| Branch protection: dismiss stale | API | `false` (disabled for PR rescue rebase flow) |
| Branch protection: required status | API | `check` (strict: must be up to date) |
| Branch protection: enforce admins | API | `true` |
| Branch protection: conversation resolution | API | `true` (all review threads must be resolved) |
| Copilot auto-review | Ruleset API (see above) | Active, review on push |
| Actions: first-time contributor approval | GitHub UI (Settings → Actions → General) | TBD |

### Branch protection API

```bash
gh api repos/OWNER/REPO/branches/main/protection -X PUT --input - <<'EOF'
{
  "required_status_checks": { "strict": true, "contexts": ["check"] },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_conversation_resolution": true
}
EOF
```

### Admin merge workaround (solo repos)

With `enforce_admins: true` and 1 required approval, you can't merge your own PRs without an external approver. Workaround:

```bash
# 1. FIRST: Disable auto-merge on all other open PRs (CRITICAL — race condition, see #83)
for pr in $(gh pr list --state open --json number,autoMergeRequest --jq '.[] | select(.autoMergeRequest != null) | .number'); do
  gh pr merge --disable-auto "$pr"
done

# 2. Temporarily disable enforce_admins
gh api repos/OWNER/REPO/branches/main/protection/enforce_admins -X DELETE

# 3. Admin merge
gh pr merge <PR> --merge --admin --delete-branch

# 4. Re-enable enforce_admins
gh api repos/OWNER/REPO/branches/main/protection/enforce_admins -X POST

# 5. Re-enable auto-merge on those PRs
for pr in <saved list>; do
  gh pr merge --auto --merge "$pr"
done
```

> **Warning**: Skipping steps 1 and 5 allows any PR with auto-merge + green CI to merge without required approvals during the enforce_admins disable window. PR #69 merged with zero approvals due to this race condition (issue #83).

This is a known limitation for solo repos. Agent PRs don't need this — the quality gate approves them.

### Pipeline Orchestrator

The **Pipeline Orchestrator** (`.github/workflows/pipeline-orchestrator.md`) owns the full lifecycle of agent work — from issue to merged PR. It runs every 15 minutes and on every push to main.

**Issue dispatch** (one at a time):
- If no `aw`-labeled PR is currently in flight, it finds open issues labeled `code-health` or `test-audit` that don't have a PR yet
- Dispatches `issue-implementer` for the first eligible issue
- Only one at a time — avoids concurrent PRs fighting over main

**PR orchestration** (unstick what's in flight):
For each open `aw`-labeled PR with auto-merge enabled (excluding `aw-conflict`), sorted by progress (approved first), applies the first matching action:

1. **No Copilot review** → requests review from `@copilot`
2. **Unresolved threads** → queries real thread IDs via GraphQL, resolves threads where the responder (PAT owner) posted the last comment
3. **Behind main** → logs and skips (requires manual rebase)
4. **All clear** → auto-merge should handle it

The orchestrator is a pure reasoning agent — no git access, no `contents: write`. It uses safe-outputs (`dispatch-workflow`, `add-reviewer`, `resolve-pull-request-review-thread`, `add-comment`, `add-labels`) and bash for GraphQL queries.

Replaces the old `pr-rescue.yml` bash script which only handled rebasing and required repeated bug-fix cycles.

### Review Responder Thread ID Lookup

The Review Responder has `bash: ["gh:api:graphql"]` access so it can query real `PRRT_` thread IDs before resolving. Without this, the agent hallucinates thread IDs because the MCP server doesn't expose them (#114). The responder runs `gh api graphql` to fetch thread IDs upfront, then uses those real IDs in resolve calls.

This is a workaround until gh-aw upgrades their pinned MCP server (`github/gh-aw#21130`).

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

### 6. `pull_request_review` workflows run from the PR's head branch
The workflow definition comes from the **PR's head branch**, not the default branch. This was verified empirically on PR #119 — the `if:` condition added on that branch was active immediately without merging to main first. This contradicts common web search results and many documentation sources. **Never trust web search over empirical evidence.**

### 7. GitHub's `action_required` is separate from gh-aw's `pre_activation`
`action_required` means GitHub itself blocked the run (first-time contributor approval). No jobs run at all. `pre_activation` is gh-aw's role/bot check within the workflow.

### 8. `bots:` field is broken due to upstream bug (gh-aw#21098)
The `bots:` field compiles correctly into `GH_AW_ALLOWED_BOTS` in the lock file, but `check_membership.cjs` never evaluates it for GitHub App actors. The role check fails with a 404 error and the `error` branch exits before the bot fallback. Use `roles: all` as a workaround (see #74 to track removal).

### 9. Always use merge commits
Never squash merge — it loses commit history and the user gets angry. Set merge method preference explicitly.

### 10. Issues are specs
Issues describe WHAT to do, not HOW. The implementer agent reads the issue and decides the implementation.

### 11. `labels:` on `create-pull-request` config is broken (gh-aw runtime bug)
The `labels` field compiles into the lock file and the handler reads it, but the post-creation label API call fails non-deterministically with a node ID resolution error. Worse, the tool description tells the agent "Labels will be automatically added" — so the agent stops including labels in its own call. Do NOT use `labels:` config. Instead, instruct the agent to include labels in the `create_pull_request` call. Tracked in #108.

### 12. Review thread IDs are invalidated by pushes
Pushing code to a PR branch can invalidate GraphQL thread IDs. If the responder pushes before resolving threads, the resolve calls fail with stale node IDs. Always resolve threads BEFORE pushing.

### 13. MCP server doesn't expose thread IDs to agents (#114)
The GitHub MCP server (pinned by gh-aw) does not return `PRRT_` thread node IDs in its tool responses. Agents hallucinate plausible-looking IDs that fail at the GraphQL API. The `resolve_pull_request_review_thread` safe-output works fine — the problem is the agent doesn't know which ID to pass. Workaround: give the agent `bash: ["gh:api:graphql"]` so it can query real thread IDs directly. Tracked upstream in `github/gh-aw#21130`.

### 14. `push-to-pull-request-branch` safe-output can't force-push
The safe-output generates patches via `git format-patch` and applies them. It cannot do `git push --force-with-lease` after a rebase. If your workflow needs to rebase and force-push, it must either use a regular workflow (`.yml`) with `contents: write`, or use `strict: false` (not recommended). This is why the pipeline orchestrator delegates rebasing to humans instead of trying to do it.

### 15. Don't write complex bash in GitHub Actions — use gh-aw agents
Shell scripts under `set -euo pipefail` are fragile. Every API call needs `|| { warn; continue }` guards, every git command needs error handling, variable interpolation in GraphQL queries creates injection risks, and the bash gets longer with every bug fix. If the logic involves decisions and error recovery, an agent handles it better. The old `pr-rescue.yml` went through 4 rounds of Copilot review, a Gemini review, and an OpenAI Codex review — each finding new bugs. The orchestrator replacement is ~80 lines of natural language.

### 16. `gh api user` resolves the PAT owner identity at runtime
When agents post comments or replies using `GH_AW_WRITE_TOKEN` (a PAT), the comments appear as the PAT owner — not `github-actions[bot]`. Don't hardcode usernames. In a solo-developer repo, the PAT owner is the repository owner — use `$GITHUB_REPOSITORY_OWNER` to get the identity. Note: `gh api user` may not work in the agent sandbox because `GH_TOKEN` is not set for the agent's bash environment (it uses an installation token that returns 403 on `/user`).

### 17. The `if:` frontmatter field gates at the infrastructure level
Adding `if: "contains(github.event.pull_request.labels.*.name, 'aw')"` to a workflow's frontmatter compiles to a job-level `if:` on the activation job. When the condition is false, the workflow skips entirely at the GitHub Actions level — zero tokens burned, no agent activation. This is fundamentally different from checking labels in the agent prompt (which still activates the agent, burns compute, then noops).

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
- **Known bug**: Even if the above are correct, `check_membership.cjs` never reaches the bot check for GitHub App actors (see [gh-aw#21098](https://github.com/github/gh-aw/issues/21098)). Use `roles: all` as a workaround.

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
- PR #72: Added `Copilot` to bots list (correct actor name) — still didn't fix it
- Read actual `check_membership.cjs` source: the `error` branch from 404 exits BEFORE the bot fallback is ever reached
- **Three PRs merged to main (#64, #65, #72) based on guessing from logs. None fixed the problem.**
- Filed upstream bug: [github/gh-aw#21098](https://github.com/github/gh-aw/issues/21098)
- Workaround: `roles: all` skips `check_membership.cjs` entirely — tracked for removal in issue #74
- Issue #75 documents the full root cause and links all previous failed attempts

### 2026-03-15 — Pipeline working end-to-end + hardening

- PR #80: Quality Gate fix — accept COMMENTED reviews from Copilot (not just APPROVED). Quality gate was noop'ing on every Copilot review.
- PR #85: First fully autonomous merge! Issue #78 → implementer → PR → CI → Copilot review → quality gate approval → auto-merge. Zero human intervention.
- PR #87: Pipeline hardening — PR rescue workflow (rebase behind-main PRs), quality-gate `quality-gate-approved` label, safe admin merge procedure, `dismiss_stale_reviews: false`, `required_conversation_resolution: true`
- PR #69: Accidentally auto-merged with zero approvals during admin merge window (issue #83). Led to safe admin merge procedure.
- Filed upstream: [github/gh-aw#21103](https://github.com/github/gh-aw/issues/21103) — feature request for `merge-pull-request` safe-output

### 2026-03-15 — More churn from Copilot not thinking

- PR #97: "Fixing many bugs caused by Copilot CLI not thinking" — 6 bugs in pr-rescue.yml (missing git config, BLOCKED check too broad, single failure aborts loop, cancel-in-progress corruption, unguarded checkout, unguarded abort). Also added `labels: ["aw"]` to implementer config — which broke label application.
- PR #93: Created without `aw` label (agent non-determinism). Quality gate noop'd. Manually added label, but 2 unresolved threads from responder pushing before resolving. Closed.
- PR #104: Created without `aw` label — caused by PR #97's `labels: ["aw"]` config change. The gh-aw handler's post-creation label API call failed with node ID resolution error. The tool description told the agent "labels will be automatically added" so the agent stopped including them. Worse than before.
- PR #106: Got `aw` label (non-deterministic — same config as #104), approved by quality gate, but 3 unresolved threads blocked merge. Same responder ordering bug.
- PR #109: Reverts labels config, rewrites responder instructions with `***MUST***`/`***DOUBLE CHECK***` ordering enforcement.
- **Lesson reinforced**: NEVER add config without verifying the runtime behavior. Read the source code. The compiler accepting a field does not mean the handler implements it.

### 2026-03-16 — Label gate fix + pipeline orchestrator

- PR #119: Added `if:` frontmatter condition to review-responder and quality-gate — workflows now skip entirely when `aw` label is absent. Previously burned compute + tokens on every PR. (Issue #120)
- **Discovery**: `pull_request_review` events use workflow files from the PR's **head branch**, not the default branch. The `if:` condition was active immediately on PR #119 itself — no agent workflows fired. Contradicts common web search results — verified empirically by checking workflow runs. **Rule: never trust web search over empirical evidence.**
- Filed issue #120 for the label gate bug. Merged PR #119 using safe admin merge procedure.

#### The pr-rescue saga

The enhanced PR rescue (#116) went through three complete rewrites:

1. **Bash script attempt (PR #118, #121)**: 230 lines of bash under `set -euo pipefail`. Copilot review found 6 bugs (unguarded API calls, `git checkout` on fresh runner, pagination cap). Gemini review found 3 more (shell injection via branch names, `first:0` invalid in GraphQL, bot error replies). OpenAI Codex found a logic bug (thread resolution checked for `github-actions[bot]` but responder posts as PAT owner). Then I hardcoded the username instead of deriving it from the token. Then Copilot found the hardcode. Then I added a stray `--` to `git checkout -B`. Every fix introduced new bugs. PR #121 accumulated 7 fix commits across 4 rounds of review.

2. **gh-aw agent attempt (pr-rescue.md)**: Rewrote as a gh-aw agent to escape bash fragility. Compiled clean. Then on self-review discovered: no `bash:` tools but instructions reference `gh api graphql` and `git rebase`. Added tools. Then discovered `push-to-pull-request-branch` safe-output can't force-push after rebase — it only applies patches. The agent literally cannot do the core operation.

3. **Pipeline orchestrator (final)**: User proposed a fundamentally different approach — instead of one workflow doing everything, split into an orchestrator agent (reasoning + safe-outputs, no git) that handles everything EXCEPT rebasing. Rebasing either stays as a simple dedicated workflow or is left to humans. The orchestrator is ~80 lines of natural language, compiles clean, needs no `contents: write`.

- Added `bash: ["gh:api:graphql"]` to review-responder (#117) — fixes thread ID hallucination at the source. Agent now queries real `PRRT_` IDs via GraphQL before resolving.
- Closed PR #121 (bash attempt). Abandoned pr-rescue.md (gh-aw attempt). Created pipeline-orchestrator.md (final approach).
- Closed stale/noise issues: #94, #105 (auto-generated fallback issues from implementer), #115 (duplicate of #108), #120 (fixed in PR #119).
- **Lessons learned**: (1) Complex bash in Actions is a bug factory. (2) gh-aw safe-outputs have limitations (no force-push). (3) Split reasoning from operations — agents reason, workflows operate. (4) Never hardcode values that can be derived at runtime. (5) Every round of review found bugs the previous round missed — self-review is not enough.

</details>
