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
Scheduled Analysis Agents (code-health, test-analysis, perf-analysis)
  → create issues (labeled aw + domain label)

Feature Planner (every 3 hours)
  → reads PRODUCT_VISION.md + codebase
  → files one implementable feature issue (aw + auto-feature)

Pipeline Orchestrator (bash, every 30 min + event-driven)
  → finds oldest eligible aw issue → dispatches Implementer
  → monitors aw PRs: CI status, review threads, Copilot review
  → dispatches CI Fixer / Review Responder / Quality Gate as needed
  → rebases PRs behind main
  → labels stuck PRs for human intervention

Implementer → creates PR (lint-clean, non-draft, auto-merge, aw label)
  → CI runs + Copilot auto-reviews (parallel)
    → CI fails? → Orchestrator dispatches CI Fixer
    → Copilot has comments? → Orchestrator dispatches Review Responder
    → CI green + threads resolved + Copilot reviewed → Orchestrator dispatches Quality Gate
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
| Branch protection: dismiss stale | API | `true` (new pushes invalidate old approvals) |
| Branch protection: required status | API | `check` (strict: must be up to date) |
| Branch protection: enforce admins | API | `true` |
| Branch protection: conversation resolution | API | `true` (all review threads must be resolved) |
| Copilot auto-review | Ruleset API (see above) | Active, review on push |
| Actions: first-time contributor approval | GitHub UI (Settings → Actions → General) | TBD |

### Branch protection API

> **⚠️ WARNING**: The `PUT` API replaces the ENTIRE branch protection config. Omitting a field silently sets it to false/0. **Never use PUT directly** — use the dedicated sub-resource endpoints (e.g., `/enforce_admins`, `/required_pull_request_reviews`) or the safe admin merge scripts below.

> **📌 Canonical settings**: See the [Steady-State Branch Protection Settings](#steady-state-branch-protection-settings) table for the authoritative values. The snippet below is an example only and may be stale.

```bash
# EXAMPLE ONLY — see canonical settings table for current values
gh api repos/OWNER/REPO/branches/main/protection -X PUT --input - <<'EOF'
{
  "required_status_checks": { "strict": true, "contexts": ["check"] },
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

### Admin merge workaround (solo repos)

With `enforce_admins: true` and 1 required approval, agent PRs get approved by the quality gate and auto-merge. For human PRs in a solo repo (no second reviewer), use the safe admin merge scripts:

```bash
# 1. Hold — disables enforce_admins, pauses auto-merge, shows verification table
scripts/hold-for-merge.sh owner/repo

# 2. Merge your PR manually
gh pr merge <PR> --merge --admin --delete-branch

# 3. Release — re-enables enforce_admins, restores auto-merge (preserving merge method), shows verification table
scripts/release-from-merge.sh owner/repo
```

> **🚫 Do NOT perform safe admin merges manually.** The scripts exist because manual API calls broke branch protection twice (see bug #261). Always use the scripts.

> **Warning**: Skipping steps 1 and 5 allows any PR with auto-merge + green CI to merge without required approvals during the enforce_admins disable window. PR #69 merged with zero approvals due to this race condition (issue #83).

This is a known limitation for solo repos. Agent PRs don't need this — the quality gate approves them.

### Pipeline Orchestrator (bash-based)

The gh-aw agent orchestrator was removed (PR #137, see `docs/auto_pr_orchestrator_aw.md`). Replaced by a regular GitHub Action (`pipeline-orchestrator.yml`) that runs in seconds, not minutes.

**Current state on main (v1 + v2 + v3)**:
- **v1 — Thread resolution**: Triggered by `workflow_run` after Review Responder completes. Queries review threads via GraphQL, resolves threads where the last commenter is not `copilot-pull-request-reviewer` (meaning someone addressed it). Tested on PR #113 — resolved 2 threads in 3 seconds.
- **v2 — Auto-rebase**: Triggered by `push: branches: [main]`. Detects PRs behind main via `mergeStateStatus: BEHIND`, rebases onto `origin/main`, force-pushes with lease. On conflict: adds `aw-needs-rebase` label. Tested on PR #113 — rebased and auto-merge fired in 7 seconds.
- **v3 — Full orchestration** (PR #163): Issue dispatch (finds issues labeled `aw`, excluding `aw-dispatched`, `agentic-workflows`, and `aw-protected-files`, then dispatches implementer), 30-minute cron safety net, review loop management (tracks responder rounds via `aw-review-response-N` labels, marks stuck after 3 retries), CI fixer dispatch. Initially merged in PR #144 then reverted due to untested loops — reworked and re-merged in PR #163 after sandbox testing.

See issue #135 for the full roadmap.

### Review Responder — Current Status

The review-responder agent can read threads, fix code, and reply. However, the safe output handlers require proper context configuration:

- `target: "triggering"` (default) only works with `pull_request_review` triggers — fails with `workflow_dispatch`
- `target: "*"` works with `workflow_dispatch` — the agent includes the PR number in its messages
- The `pull_request_review` trigger caused infinite loops because it fires on ANY review submission (Copilot, quality gate, humans), not just Copilot reviews

**Fixed** (PR #163): Switched to `workflow_dispatch` trigger with `target: "*"` on safe outputs. Orchestrator dispatches the responder when needed. Successfully tested on PR #152, merged in PR #163.

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

### 11. ~~`labels:` on `create-pull-request` config is broken~~ (resolved)
~~The `labels` field compiles into the lock file and the handler reads it, but the post-creation label API call fails non-deterministically with a node ID resolution error.~~ **Update (2026-03-21)**: Investigation found that `labels` is officially documented and supported in gh-aw. The "node ID resolution error" was never properly investigated and may have been misattributed to the auto-merge step (which does use node IDs). Re-enabled `labels: [aw]` on the implementer. The `labels` field applies labels via REST API after PR creation — use it for reliable labeling instead of depending on agent instructions. Closed #108.

### 12. Review thread IDs are invalidated by pushes
Pushing code to a PR branch can invalidate GraphQL thread IDs. If the responder pushes before resolving threads, the resolve calls fail with stale node IDs. Always resolve threads BEFORE pushing.

### 13. MCP server doesn't expose thread IDs to agents (#114)
The GitHub MCP server (pinned by gh-aw) does not return `PRRT_` thread node IDs in its tool responses. Agents hallucinate plausible-looking IDs that fail at the GraphQL API. The `resolve_pull_request_review_thread` safe-output works fine — the problem is the agent doesn't know which ID to pass. Workaround: instruct the agent to query real thread IDs via `gh api graphql` (the agent already has `--allow-all-tools` when no explicit `bash:` config is set). Do NOT add `bash:` to the tools config — that causes the compiler to switch from `--allow-all-tools` to a restricted allowlist, breaking CI commands. Tracked upstream in `github/gh-aw#21130`.

### 14. `push-to-pull-request-branch` safe-output can't force-push
The safe-output generates patches via `git format-patch` and applies them. It cannot do `git push --force-with-lease` after a rebase. If your workflow needs to rebase and force-push, use a regular workflow (`.yml`) with `contents: write`.

### 15. Use gh-aw agents for judgment, regular workflows for orchestration
The pr-rescue bash script had many bugs, which initially suggested agents would be better. But the orchestrator agent (Opus) took 7-10 minutes per run for if/else logic and made wrong decisions (re-requested existing reviews, noop'd instead of resolving threads). **Agents are great for tasks requiring judgment** (code review, implementation, quality evaluation). **Regular bash workflows are better for deterministic orchestration** (check state, dispatch, resolve threads). See `docs/auto_pr_orchestrator_aw.md` for the full postmortem.

### 16. `gh api user` resolves the PAT owner identity at runtime
When agents post comments or replies using `GH_AW_WRITE_TOKEN` (a PAT), the comments appear as the PAT owner — not `github-actions[bot]`. Don't hardcode usernames. In a solo-developer repo, the PAT owner is the repository owner — use `$GITHUB_REPOSITORY_OWNER` to get the identity. Note: `gh api user` may not work in the agent sandbox because `GH_TOKEN` is not set for the agent's bash environment (it uses an installation token that returns 403 on `/user`).

### 17. The `if:` frontmatter field gates at the infrastructure level
Adding `if: "contains(github.event.pull_request.labels.*.name, 'aw')"` to a workflow's frontmatter compiles to a job-level `if:` on the activation job. When the condition is false, the workflow skips entirely at the GitHub Actions level — zero tokens burned, no agent activation. This is fundamentally different from checking labels in the agent prompt (which still activates the agent, burns compute, then noops).

### 18. Safe output `target` config determines PR context resolution
Safe outputs like `reply-to-pull-request-review-comment` and `push-to-pull-request-branch` default to `target: "triggering"`, which looks up the PR from `github.event.pull_request`. This only works with event-based triggers (`pull_request_review`, `pull_request`). With `workflow_dispatch`, there is no PR in the event context and safe outputs fail with "not running in a pull request context." Fix: set `target: "*"` for handlers whose schema includes a PR/issue number field (like `add-labels` with `item_number`, `reply-to-pull-request-review-comment` with `comment_id`). For handlers without a number field (like `submit-pull-request-review`), use `target: ${{ inputs.pr_number }}` to pass the PR number directly. Also requires `checkout: { fetch: ["*"], fetch-depth: 0 }` for push operations. Use `labels: [aw]` to restrict which PRs can receive pushes.

### 19. `pull_request_review` trigger fires on ALL review submissions
The `pull_request_review` trigger fires when ANY actor submits a review — not just the intended reviewer. Combined with `roles: all` (workaround for gh-aw#21098), this means Copilot reviews, quality gate approvals, and human comments ALL trigger the workflow. This caused infinite loops: responder fires → pushes → Copilot reviews → responder fires again. Fix: use `workflow_dispatch` instead and have the orchestrator decide when to run the responder.

### 20. Don't over-specify agent instructions
The responder originally worked with simple instructions: "Read the unresolved review comment threads" and "Reply to the comment thread." Adding explicit `gh api graphql` queries, ordering constraints, and MCP avoidance notes broke the agent's ability to discover threads. The agent is capable of figuring out how to read threads on its own — telling it exactly which API to use interfered with that.

### 21. Safe output `target` values differ by handler type
Not all safe output handlers resolve `target` the same way. `submit-pull-request-review` with `target: "*"` fails because its tool schema has no `pull_request_number` field — the agent can't specify which PR to review. Use `target: ${{ inputs.pr_number }}` instead (per gh-aw docs). Meanwhile, `add-labels` works with `target: "*"` because its schema has `item_number`. When using `workflow_dispatch`, check each handler's schema to pick the right `target` value. Don't assume one value works for all.

### 22. Adding a trigger to `on:` requires updating the job `if:` condition
If a job has an `if:` condition that gates on `github.event_name`, adding a new trigger to `on:` is not enough — the `if:` must also include the new event name. The orchestrator had this bug twice: first when switching quality gate to `workflow_dispatch`, then when adding `schedule`. The cron fired correctly but the job was skipped because `'schedule'` wasn't in the `if:` condition. **Always check for `event_name` gates when adding triggers.**
### 23. `tools:` block in shared imports restricts the entire agent's tool allowlist
Adding a `tools:` block (e.g., `tools: bash: [cat, grep, jq]`) to a shared import causes `gh aw compile` to switch from `--allow-all-tools` to a restricted `--allow-tool shell(...)` list in the compiled lock file. This affects the ENTIRE agent, not just the shared import's step. The agent gets "Permission denied" on any command not in the explicit list — including `uv`, `python3`, `pip`, `curl`, and `git fetch`. Since only the importing workflow is affected, the bug manifests as one agent failing while others work fine — it looks non-deterministic but is actually a consistent config issue. **Fix**: never add `tools:` blocks to shared imports. The pre-fetch step runs as a regular workflow step (not agent shell), so it doesn't need tool permissions.

### 24. `gh run list --status` only accepts a single value
The `--status` flag is type `string`, not array. Passing `--status=in_progress --status=queued` only uses the **last** value. To filter for multiple statuses, skip `--status` entirely and filter client-side with `--json databaseId,status --jq '[.[] | select(.status == "in_progress" or .status == "queued")]'`.

### 25. `cancel-in-progress: true` is dangerous for workflows with side effects
If a workflow modifies external state (labels, dispatches) and `cancel-in-progress: true` kills it mid-flight, the side effects may be partially applied. Example: orchestrator labels an issue `aw-dispatched` then gets cancelled before dispatching the implementer — the issue is orphaned. Use `cancel-in-progress: false` for workflows with non-atomic side effects.

### 26. Quality gate approval label desyncs from actual approval state
Branch protection's `dismiss_stale_reviews: true` dismisses the quality gate's APPROVE review when new code is pushed, but the `aw-quality-gate-approved` label persists. The orchestrator sees the label and skips re-dispatching the quality gate, leaving the PR stuck in BLOCKED state. Labels are hints, not source of truth — always verify actual review state.

### 27. Shared imports use `imports:` + `steps:` pattern
To pre-fetch data before the agent runs, create a shared `.md` file with a `steps:` block in the frontmatter. The importing workflow uses `imports: [shared/filename.md]`. The steps run as regular workflow steps (with full `gh` CLI access and `GITHUB_TOKEN`), writing data to `/tmp/gh-aw/` for the agent to read. This bypasses MCP tool limitations. Based on the pattern from `github/gh-aw`'s own `copilot-pr-data-fetch.md`.

### 28. Don't dispatch quality gate until Copilot has reviewed the current commit
Copilot code review runs asynchronously. If the orchestrator dispatches the quality gate as soon as CI passes and threads are resolved, it can approve before Copilot reviews. Then Copilot's comments arrive after merge — too late. Always verify a Copilot review exists on the current head commit AND no Copilot review is in-progress before dispatching the quality gate. Use `latestReviews` in GraphQL to check review state and `gh run list` to check for active review runs.

### 29. Check actual review approval state, not labels
Labels like `aw-quality-gate-approved` persist even after `dismiss_stale_reviews` invalidates the approval. Never use labels as the source of truth for review state. Query `latestReviews` and verify the APPROVE review is on the current head commit and not dismissed. Labels are hints for humans, not gates for automation.

### 30. Copilot code review always submits as COMMENTED
GitHub Copilot code review never submits APPROVED or CHANGES_REQUESTED — always COMMENTED. This means: (1) Copilot reviews don't count toward required approvals, (2) they're never dismissed by `dismiss_stale_reviews`, (3) when checking if Copilot has reviewed, filter by author login, not by review state. The quality gate approval (from a PAT) is separate from Copilot's review — don't conflate them.

### 31. Quality gate approval author is the PAT owner, not github-actions
The quality gate uses `GH_AW_WRITE_TOKEN` (a PAT) for `submit-pull-request-review`. The approval appears as the PAT owner (e.g., the repo owner), not as `github-actions[bot]`. Use `${{ github.repository_owner }}` to derive the identity at runtime — never hardcode usernames. This same mistake was made three times in this project (thread resolution, approval detection, changelog docs) before being codified as a rule.

### 32. Don't dispatch quality gate and rebase in the same orchestrator run
If the orchestrator dispatches the quality gate (slow — agent takes minutes) and then rebases (fast — git push in seconds) in the same run, the quality gate may evaluate pre-rebase code. The rebase push also dismisses any approval via `dismiss_stale_reviews`. Fix: check `mergeStateStatus` before dispatching quality gate — if `BEHIND` or `DIRTY`, skip and let the rebase step handle it. The next orchestrator cycle dispatches quality gate on the rebased commit.

### 33. Proactively request Copilot review — don't just wait
Copilot code review does not reliably auto-trigger on every push — observed on PR #210 where the responder pushed but Copilot never reviewed for 14+ minutes. Use `gh pr edit "$PR" --add-reviewer @copilot` (requires gh v2.88+) to proactively request a review. Safe to call repeatedly — if a review is already in progress, the request is ignored. Don't just wait and hope.

### 34. Check for in-flight workflows before dispatching
Before dispatching any agent workflow (implementer, quality gate, responder), check if one is already running. Use `gh run list --workflow=NAME --json databaseId,status | jq '[.[] | select(.status == "in_progress" or .status == "queued" or .status == "waiting")] | length'`. Without this check, multiple orchestrator runs from rapid-fire `workflow_run` triggers will dispatch duplicates, wasting compute and inference tokens. This bug occurred for both the implementer (#164) and quality gate (#213).

### 35. MCP gateway SSE connections die after 5 minutes of idle (#707)
The MCP gateway's HTTP transport drops connections after exactly 5 minutes of no MCP traffic. Confirmed via gateway logs showing `duration=5m0.8s (empty body)` on **every single run**. Successful runs happen to call safe-output tools before the 5-minute mark. Runs where the agent spends >5 minutes on local work (reading, editing, running tests) before calling safe-outputs fail silently — no PR created. Gateway config says `idleTimeout=30m0s` but the timeout is in the SSE transport layer, not the gateway pool. gh-aw v0.65.7 added a `sandbox.mcp.keepalive-interval` frontmatter option to send periodic pings, but as of v0.66.1 the gateway binary (v0.2.12) rejects the field during schema validation. No fix available yet — affects ~15-25% of implementer and responder runs.

### 36. Copilot code review reads copilot-instructions.md from the PR branch, not main
Empirically verified on PRs #478 and #479. The instructions file existed only in the PR branch — Copilot flagged violations citing the guidelines file by name. GitHub docs say "Copilot uses the custom instructions in the base branch" — this is wrong. PR branch is the source. This means new instructions take effect immediately on the PR that adds them.

### 37. Copilot code review drip-feeds comments (~2 per review pass)
Copilot leaves a limited number of comments per review — observed to be ~2. When the responder fixes those and pushes, Copilot re-reviews and finds more. This creates cycles where the responder is dispatched 3+ times, each time getting 2 new comments. PR #514 had 13 comments across 6 rounds — the responder exhausted its 3-attempt limit and the PR got stuck. There is no configurable setting to increase the per-review comment limit. Tracked in #520.

### 38. copilot-instructions.md should be the single hub — don't hardcode guideline paths
All agent workflows should read `.github/copilot-instructions.md` which points to guideline files. Don't hardcode `.github/CODING_GUIDELINES.md` directly in each workflow — that creates coupling where every new instruction file requires editing all agents. The instructions file is the indirection layer. Bug #486 was filed and fixed in PR #488 after this was done wrong initially.

### 39. Dependabot PRs for gh-aw must not be merged directly
Dependabot PRs that modify generated lock files should be closed. Instead: upgrade `gh aw` CLI, run `gh aw compile` to regenerate lock files, update `copilot-setup-steps.yml` SHA and version pin, and create a single PR with all changes. Use `@dependabot rebase` to rebase Dependabot branches if needed. See PR #525 for the pattern.

### 40. TYPE_CHECKING is banned — use runtime imports
`from typing import TYPE_CHECKING` and `if TYPE_CHECKING:` guards are banned per `.github/CODING_GUIDELINES.md`. Every import used in an annotation must be a real runtime import. For type-stub-only symbols like `loguru.Record`, use a runtime `import loguru` with a string annotation `"loguru.Record"` — Python never evaluates it at runtime but pyright resolves it via the stub. Do not use `from __future__ import annotations` as a workaround — it defers evaluation but breaks `typing.get_type_hints()` for stub-only types.

### 41. Protected files fall back to issues — apply patches manually
When the implementer modifies a protected file (e.g., `ci.yml`), the `protected-files: fallback-to-issue` config creates a fallback issue with the patch attached instead of a PR. The patch must be applied manually: `gh run download <RUN_ID> -n agent-artifacts`, `git am --3way <patch>`, push and create PR. See PR #483 for the pattern. The orchestrator skips issues with the `aw-protected-files` label.

### 42. COPILOT_GITHUB_TOKEN is a separate secret from GH_AW_WRITE_TOKEN
`COPILOT_GITHUB_TOKEN` authenticates the Copilot CLI agent — needed by implementer, responder, ci-fixer, quality-gate, code-health, test-analysis, perf-analysis, feature-planner. `GH_AW_WRITE_TOKEN` authenticates safe-output operations (creating PRs, posting comments). They can be the same PAT but both must have valid credentials. When `COPILOT_GITHUB_TOKEN` expires, agents fail with `Error: Authentication failed` but CI, Copilot code review, and the orchestrator keep working (they use `GITHUB_TOKEN` or `GH_AW_WRITE_TOKEN`). Check agent artifacts for the real error — don't guess "transient API outage."

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
<summary>Branch Protection & Safe Admin Merge</summary>

### Steady-state branch protection

These are the correct settings for `main` on `microsasa/cli-tools`. They provide maximum protection while allowing the autonomous agent pipeline to function.

To verify current settings match:
```bash
gh api repos/microsasa/cli-tools/branches/main/protection | python3 -m json.tool
```

| Setting | Value | Rationale |
|---------|-------|-----------|
| `required_status_checks.strict` | `true` | PR must be up-to-date with main before merge. Prevents merging stale branches that haven't been tested against latest main. |
| `required_status_checks.checks` | `["check"]` | The CI workflow (`ci.yml`) has a job named `check`. This is the **job name**, not the workflow name `CI`. GitHub matches against job names. Getting this wrong causes PRs to be permanently blocked (see incident below). |
| `required_approving_review_count` | `1` | The quality gate submits an APPROVE review on agent PRs, satisfying this requirement. For human PRs in a solo repo, use the safe admin merge scripts to bypass. This is the core protection that prevents unreviewed code from merging. |
| `dismiss_stale_reviews` | `true` | New pushes invalidate old approvals. Forces the quality gate to re-approve after any code changes (e.g., responder fixes). Without this, a push after quality gate approval could merge unreviewed code. |
| `require_code_owner_reviews` | `false` | No CODEOWNERS file. Enabling would block the agent since no code owner can approve. |
| `require_last_push_approval` | `false` | Requires someone OTHER than the last pusher to approve. Currently the implementer (`github-actions[bot]`) and quality gate (PAT owner) are different actors, but we leave this disabled as a safety margin in case token usage changes. |
| `required_conversation_resolution` | `true` | All review threads must be resolved before merge. Ensures Copilot review comments and human feedback are addressed. |
| `enforce_admins` | `true` | Branch protection rules apply to admins too. Prevents accidental direct pushes to main. Must be temporarily disabled for safe admin merge. |
| `required_signatures` | `false` | The agent cannot GPG-sign commits. Enabling would block the pipeline. |
| `required_linear_history` | `false` | Would force squash/rebase only. We want agents to use squash merge but humans should have the choice of merge commits. |
| `allow_force_pushes` | `false` | Never allow force push to main. |
| `allow_deletions` | `false` | Never allow deleting main. |
| `block_creations` | `false` | No need to block branch creation. |
| `lock_branch` | `false` | Main is not read-only. |
| `allow_fork_syncing` | `false` | Not needed. |

### Why `require_last_push_approval` is disabled

The implementer agent pushes code via the default `GITHUB_TOKEN`, which appears as `github-actions[bot]`. The quality gate submits approvals via `GH_AW_WRITE_TOKEN` (a PAT), which appears as the PAT owner (the repo owner). These are currently different actors, so `require_last_push_approval` would technically work today. However, we leave it disabled as a conservative choice — if the token setup ever changes (e.g., both steps use the same token), enabling this setting would silently block the entire agent pipeline with no obvious error.

### Why the status check is `check` not `CI`

The CI workflow file is named `CI` (`name: CI` in `ci.yml`), but the job inside is named `check`. GitHub branch protection matches against **job names**, not workflow names. Setting the required check to `CI` causes every PR to be permanently BLOCKED because no check run with that name ever appears.

This was the root cause of a 9-hour incident where the quality gate ran 100+ times on PR #246 without it ever merging.

### Safe admin merge

When you need to merge a PR to main bypassing the normal pipeline (e.g., hotfixes, reverts, PRs the agent can't handle):

```bash
# 1. Hold — disables enforce_admins, pauses auto-merge on open PRs
scripts/hold-for-merge.sh microsasa/cli-tools

# 2. Merge your PR
gh pr merge <number> --squash   # or --merge, your choice

# 3. Release — re-enables enforce_admins, restores auto-merge
scripts/release-from-merge.sh microsasa/cli-tools
```

Both scripts:
- Read all settings from the GitHub API before AND after making changes (never from memory)
- Display a single table comparing Before → After for every protection setting
- Color-coded: green = correct, red = something went wrong
- Exit with error code 1 if any setting is wrong after the change
- The only setting that changes is `enforce_admins` (`true` → `false` → `true`)

The scripts exist because Copilot CLI repeatedly broke branch protection by manually reconstructing settings from memory during admin merges, causing:
- `required_conversation_resolution` silently disabled (PRs #172, #189, #193 merged with unresolved threads)
- Wrong status check name `CI` instead of `check` (PR #246 stuck for 9 hours, 100+ quality gate runs)
- `dismiss_stale_reviews` and `required_approving_review_count` left wrong (PRs #252, #255, #256 merged without quality gate review)

**Copilot CLI is not allowed to modify branch protection settings directly. Only through these scripts.**

### Incident: 2026-03-22 — Branch protection broken, 3 PRs merged without review

**Root cause**: During safe admin merge of PR #245, Copilot CLI disabled the entire branch protection config and reconstructed it with the wrong required status check name (`CI` instead of `check`). This caused:

1. PR #246 permanently BLOCKED — the `CI` check never appeared, so auto-merge couldn't fire
2. Orchestrator dispatched quality gate every 5 minutes — 100+ approval reviews accumulated
3. When Copilot CLI fixed the check name, it didn't fix the review settings (`dismiss_stale_reviews: false`, `required_approving_review_count: 0` — should have been `1`)
4. Copilot CLI then tested hold/release scripts against live settings while the orchestrator was still running
5. Three PRs (#252, #255, #256) auto-merged without any quality gate review
6. All three had to be reverted

**Resolution**: Reverted PRs #252, #255, #256. Reopened linked issues (#239, #242, #243). Created `scripts/hold-for-merge.sh` and `scripts/release-from-merge.sh` to make admin merge deterministic. Stripped Copilot CLI of permission to modify branch protection directly.

</details>

---

<details>
<summary>Our Agent Inventory</summary>

| Agent | Trigger | Purpose | Safe outputs |
|---|---|---|---|
| `test-analysis.md` | schedule (every 6h) / manual | Find test coverage gaps | `create-issue` (max 2) |
| `code-health.md` | schedule (every 6h) / manual | Find refactoring/cleanup opportunities (excludes perf) | `create-issue` (max 2) |
| `perf-analysis.md` | schedule (every 6h) / manual | Find performance problems | `create-issue` (max 2) |
| `feature-planner.md` | schedule (every 3h) / manual | File one feature step toward PRODUCT_VISION.md | `create-issue` (max 1) |
| `issue-implementer.md` | `workflow_dispatch` (issue number) | Implement fix from issue spec, open PR | `create-pull-request` (draft: false, auto-merge), `push-to-pull-request-branch` |
| `ci-fixer.md` | `workflow_dispatch` (PR number) | Fix CI failures on agent PRs | `push-to-pull-request-branch`, `add-labels`, `add-comment` |
| `review-responder.md` | `workflow_dispatch` (PR number) | Address review comments | `push-to-pull-request-branch`, `reply-to-pull-request-review-comment`, `add-labels` |
| `quality-gate.md` | `workflow_dispatch` (PR number) | Evaluate quality + blast radius, approve or close | `submit-pull-request-review`, `close-pull-request`, `add-comment`, `add-labels` |
| `pipeline-orchestrator.yml` | `workflow_run` / `pull_request_review` / `workflow_dispatch` / `schedule` | Dispatch agents, resolve threads, rebase PRs, label stuck PRs | N/A (bash, not gh-aw) |

### Loop prevention

- **CI Fixer**: `aw-ci-fix-attempted` label. Max 1 retry.
- **Review Responder**: `aw-review-response-attempted` label + round tracking (`aw-review-response-1`, `-2`, `-3`). Max 3 attempts. If all fail, orchestrator adds `aw-pr-stuck:review`.
- **All agents**: Only act on PRs with the `aw` label.
- **Feature Planner**: Won't file if any open issue has `auto-feature` label.
- **Scheduled analysis**: Prioritization instructions (code-health: severity, test-analysis: risk, perf-analysis: impact) when findings exceed `max: 2` cap.

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

- Updated review-responder instructions to query real `PRRT_` thread IDs via `gh api graphql` before resolving (#117). No `bash:` tool config needed — `--allow-all-tools` is granted by default when no explicit `bash:` is set. Adding `bash:` would restrict the allowlist and break CI commands (uv, ruff, pyright, pytest). Instruction-only fix.
- Moved CI fixer dispatch from `ci.yml` into the orchestrator — all dispatch decisions (implementer + ci-fixer) now centralized.
- Closed PR #121 (bash attempt). Abandoned pr-rescue.md (gh-aw attempt). Created pipeline-orchestrator.md (final approach).
- Closed stale/noise issues: #94, #105 (auto-generated fallback issues from implementer), #115 (duplicate of #108), #120 (fixed in PR #119).
- **Lessons learned**: (1) Complex bash in Actions is a bug factory. (2) gh-aw safe-outputs have limitations (no force-push). (3) Split reasoning from operations — agents reason, workflows operate. (4) Never hardcode values that can be derived at runtime. (5) Every round of review found bugs the previous round missed — self-review is not enough.

### 2026-03-17 — Pipeline orchestrator removed

- The gh-aw orchestrator agent (PR #130) ran 22+ times overnight on a 15-min cron. Every run either reported `missing_data` (auth failure on GraphQL), re-requested reviews that already existed, or noop'd. Never resolved a single thread. Each run took 7-10 minutes of Opus inference for deterministic if/else logic.
- Root causes: `GH_AW_GITHUB_MCP_SERVER_TOKEN` secret wasn't set up (fixed but didn't help), `gh` CLI in sandbox uses `GH_TOKEN`/`GITHUB_TOKEN` not `GITHUB_MCP_SERVER_TOKEN`, agent made wrong action ordering decisions despite explicit instructions.
- PR #137: Removed `pipeline-orchestrator.md` + `.lock.yml`. Added postmortem at `docs/auto_pr_orchestrator_aw.md`.
- Issue #135: Rewrite orchestrator as a regular GitHub Action (bash). Same logic, runs in seconds.
- Issue #136: Cleanup tracking issue.
- Removed dispatch references from code-health and test-analysis prompts.
- **Key lesson**: gh-aw agents are for judgment (code review, implementation). Deterministic orchestration (check state → dispatch → resolve) should be regular bash workflows.

### 2026-03-17 — Bash pipeline orchestrator v1+v2 + quality gate close

- PR #140: Quality gate can now close poor-quality PRs (`close-pull-request` safe-output). Aligned gh-aw version to v0.60.0 in `copilot-setup-steps.yml`.
- PRs #125, #124, #123: Dependabot bumps (checkout v6, setup-uv v7, codeql-action v4). Rebased and merged sequentially.
- PR #141: Pipeline orchestrator v1 — bash GitHub Action resolves addressed review threads via GraphQL `resolveReviewThread`. Triggered by `workflow_run` after responder completes. Tested on PR #113: resolved 2 threads in 3 seconds.
- PR #142: Pipeline orchestrator v2 — auto-rebase. Detects PRs behind main via `mergeStateStatus: BEHIND`, rebases and force-pushes. Tested on PR #113: rebased and auto-merge fired in 7 seconds.
- PR #143: Fix for git fetch not creating remote tracking refs during rebase.
- PR #113: First end-to-end orchestrator success — rebased a stuck PR, CI passed, auto-merge fired.
- Issues closed: #89 (quality gate close), #88 (gh-aw outdated), #117 (thread resolution), #66 (code quality cleanups via PR #113).
- **Key insight**: Bash orchestrator in 7 seconds vs gh-aw agent in 7-10 minutes. Same logic, 60x faster.

### 2026-03-19/20 — Quality gate dispatch fix + first fully autonomous PR cycle

- PR #163: Merged orchestrator v3 + responder fix + ci-fixer fix + label renames. All tested on sandbox PRs before merge.
- PR #162: **First fully autonomous PR merge.** Issue #60 → implementer created PR → Copilot reviewed (4 comments) → responder addressed all 4 → threads auto-resolved → quality gate approved → auto-merge. Zero human intervention.
- PR #166: Second autonomous merge (issue #126). Responder addressed 1 comment, quality gate approved.
- PR #167: Exposed the quality gate happy-path bug. CI green, Copilot review clean (0 comments), but quality gate never fired because `pull_request_review` trigger has a bot filter that blocks Copilot-submitted reviews.
- PRs #169, #170: Fixed quality gate — switched to `workflow_dispatch`, orchestrator dispatches it. PR #170 was emergency fix after manually editing the lock file instead of running `gh aw compile` (broke frontmatter hash).
- Discovered `submit_pull_request_review` safe output doesn't support `target: "*"` — no `pull_request_number` field in tool schema. Fix: use `target: ${{ inputs.pr_number }}` per gh-aw docs. `add_labels` uses `target: "*"` (different handler, has `item_number` field).
- PR #167 eventually merged after testing the fix from branch via `gh workflow run --ref`.
- Enabled 5-minute cron on orchestrator. Public repo — Actions minutes are unlimited. Cron catches new issues when pipeline is idle. Closes #135.
- Bug: cron trigger was added to `on:` but `schedule` was not added to the job `if:` condition — cron fired but job was skipped every time. Fixed in #175.

### 2026-03-17/18 — Orchestrator v3 attempt, responder investigation, revert

- PR #144: Merged orchestrator v3 (issue dispatch, cron, review loop) + docs + daily test-analysis. Not adequately tested before merge.
- PR #147: Attempted to fix responder by reverting to working version. Merged untested.
- PR #150: Panic-disabled all orchestrator triggers to stop loops. Disabled v1/v2 triggers that were working.
- All three reverted after discovering cascading issues.
- **Responder investigation findings**:
  - The responder agent CAN read review threads and fix code inside the sandbox (confirmed by examining agent job logs).
  - The safe output handlers (`reply-to-pull-request-review-comment`, `push-to-pull-request-branch`) default to `target: "triggering"` which requires `pull_request_review` event context. With `workflow_dispatch`, no PR context exists and safe outputs fail.
  - Setting `target: "*"` in safe output config lets the agent specify the PR number in each message, enabling `workflow_dispatch`.
  - The `pull_request_review` trigger fires on ANY review submission (Copilot, quality gate, humans) — not just Copilot reviews. Combined with `roles: all` workaround, this caused infinite responder loops.
  - Successfully tested fix on PR #152: responder read thread via REST API, fixed code (renamed variable), replied to comment, pushed commit, orchestrator (v1) auto-resolved the thread.
- **Copilot CLI accountability**: Multiple failures caused by CLI agent: stating the responder had worked when it never had, pushing to implementer PR branches, merging without permission, creating branches and PRs without approval, not verifying changes before claiming they worked.
- **Key lessons**:
  - Never merge untested workflow changes to main — always test from branch first.
  - Safe output `target` config is critical: `"triggering"` for event triggers, `"*"` for `workflow_dispatch`.
  - Don't over-specify agent instructions — the simple original version worked; adding explicit API calls and ordering constraints broke it.
  - `workflow_dispatch` is the right trigger for the responder — the orchestrator decides when to run it, eliminating trigger loops.
  - Always verify claims by reading actual data (run logs, thread state) before proceeding.

### 2026-03-20/21 — Pre-fetch pattern, responder fix, duplicate dispatch fix

- **PR #186**: Fixed responder's inability to read review comments. MCP `pull_request_read` returns `[]` in gh-aw sandbox. Solution: shared import (`shared/fetch-review-comments.md`) runs `gh api graphql` BEFORE the agent starts, writes threads to `/tmp/gh-aw/review-data/unresolved-threads.json`.
- **Critical bug found and fixed**: The shared import initially included a `tools:` block with an allowlist of shell commands. This caused `gh aw compile` to switch from `--allow-all-tools` to `--allow-tool shell(cat) --allow-tool shell(grep) ...` in the lock file. The agent got "Permission denied" on everything not in the list (uv, python3, pip, curl, git fetch). Only the responder was affected because only it imported the shared file. Fix: removed the `tools:` block entirely. Same class of bug as pitfall #13.
- **Pre-fetch pattern tested end-to-end**: Responder found and addressed review comments on PRs #172 and #177. Both subsequently passed quality gate and auto-merged. First successful responder runs with both comment reading AND CI validation.
- **PR #190**: Fixed duplicate implementer dispatches. Orchestrator was dispatching multiple implementers in quick succession because: (1) `push` trigger fired on every merge, (2) `cancel-in-progress: true` killed runs mid-flight after labeling, (3) no check for in-flight implementer. Fix: removed `push` trigger, switched to `cancel-in-progress: false`, added in-flight check via `gh run list` with jq filter. Copilot review caught that `--status` only accepts a single value — fixed to client-side jq filter. Also changed API error fallback from "0" (fail open) to "1" (fail safe).
- **Quality gate label desync discovered**: `dismiss_stale_reviews: true` dismisses the quality gate's approval when new code is pushed, but the `aw-quality-gate-approved` label persists. Orchestrator sees label, skips quality gate, PR stays stuck. Filed issue #187.
- **Issues filed**: #183 (astral.sh blocked — not needed), #184 (audit workflows — not needed), #187 (quality gate label/approval desync).
- **Issues closed**: #180 (MCP empty comments), #164 (duplicate dispatches).
- **Key lessons**:
  - `tools:` blocks in shared imports affect the entire compiled agent, not just the import's step.
  - `cancel-in-progress: true` + side effects = orphaned state. Use `false` for workflows that label/dispatch.
  - `gh run list --status` is single-value — use jq for multi-status filtering.
  - Labels are hints, not source of truth — always verify actual state (review approval, run status).
  - When a bug looks non-deterministic (one agent fails, others don't), it's almost always a config difference — find it.

### 2026-03-28/29 — Coding guidelines, perf pipeline, copilot instructions

- **PR #482**: Created `.github/CODING_GUIDELINES.md` and `.github/copilot-instructions.md`. Verified empirically that Copilot code review reads instructions from the PR branch (not main) and follows links to guideline files. All agent workflows updated to reference guidelines. Removed `TYPE_CHECKING` from `logging_config.py`. Closes #110.
- **PR #485**: Added performance analysis pipeline (`perf-analysis.md`). Runs every 6 hours. Created `perf` label. Updated code-health to exclude performance problems (belong to perf-analysis).
- **PR #488**: Fixed agent indirection — all agents now read `copilot-instructions.md` (the hub) instead of hardcoding `CODING_GUIDELINES.md`. Added prioritization instructions to scheduled analysis workflows.
- **PR #483**: Applied agent's patch for protected-files fallback (#477). CI now excludes e2e from coverage measurement.
- **PR #525**: Upgraded gh-aw from v0.62.5 to v0.64.3. Dependabot PRs closed — proper upgrade path is `gh aw compile`.
- **Key lessons**:
  - `copilot-instructions.md` is the single hub for agent instructions — never hardcode paths to individual guideline files.
  - Copilot code review reads from the PR branch, not main — contradicts GitHub docs.
  - Copilot drip-feeds comments (~2 per review pass) — no configurable setting to change this.
  - Protected-files fallback creates an issue with a patch — human applies it manually.

### 2026-04-01 — Feature planner pipeline

- **PR #600**: Added feature planner pipeline (`feature-planner.md`). Runs every 3 hours. Reads `.github/PRODUCT_VISION.md`, compares against codebase, files one implementable feature issue. Empty vision = no-op. One feature in flight at a time (`auto-feature` label gate). Organic progression — code is ground truth.
- **COPILOT_GITHUB_TOKEN outage**: All agent workflows failed for ~12 hours due to expired/invalid token. CI, Copilot review, and orchestrator kept working (different tokens). Root cause was in the agent artifacts (`Error: Authentication failed`) — not a transient API outage.
- **Copilot CLI version change**: CLI upgraded from 1.0.12 to 1.0.14 between runs. Agents struggled more with `uv` installation but this was a pre-existing fragility, not a new bug.

### 2026-04-03/04 — MCP timeout investigation and gh-aw upgrade

- **Issue #707**: Discovered MCP gateway SSE connections die after exactly 5 minutes of idle on every run. Gateway logs: `duration=5m0.8s (empty body)`. Successful runs call safe-outputs before the 5-minute mark; failures spend >5 minutes on local work. Upstream issue gh-aw#20885 was closed March 14 but fix was incomplete.
- **PR #709**: Upgraded gh-aw to v0.66.1. Added `sandbox.mcp.keepalive-interval: 120` to all workflow frontmatters.
- **PR #717 (hotfix)**: Immediately reverted keepalive config — MCP Gateway v0.2.12 rejects `keepaliveInterval` during schema validation. All agent workflows broken at startup. The gh-aw compiler emits the field but the gateway binary doesn't support it yet. The 5-minute timeout remains unfixed (#707).
- **Issue #114 investigation**: PRRT_ thread ID bug still present upstream. MCP server PR #2245 (2-line fix) is open but unmerged. gh-aw smoke tests still skip "Resolve Review Thread." Our pre-fetch workaround remains necessary.
- **Key lessons**:
  - Always download and read agent artifacts (agent-stdio.log, mcp-gateway.log) — never guess from workflow conclusion alone.
  - The gateway config `idleTimeout=30m0s` is the pool timeout; the 5-minute kill is in the SSE transport layer.
  - `sandbox.mcp.keepalive-interval` in frontmatter is a heartbeat interval (pings every N seconds), not a timeout duration.
  - Test workflow changes with `workflow_dispatch` before merging — the keepalive schema rejection broke every agent workflow.
  - gh-aw releases are frequent (~2/day) — always check the latest version before upgrading.

</details>
