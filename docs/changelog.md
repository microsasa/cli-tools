# CLI Tools — Changelog

Append-only history of repo-level changes (CI, infra, shared config). Tool-specific changelogs live with each tool (e.g. `src/copilot_usage/docs/changelog.md`).

---

## fix: re-add labels config to implementer — 2026-03-21

**Problem**: The `labels: [aw]` config on `create-pull-request` was removed weeks ago due to a vague "node ID resolution error" that was never properly investigated. Without it, labeling depends on the agent including labels in its call — which is non-deterministic. Some PRs were created without the `aw` label.

**Investigation**: Read the gh-aw docs and source. The `labels` field is officially documented and supported — labels are applied via REST API after PR creation. The "node ID error" was likely misattributed (possibly from the `auto-merge` step which uses GraphQL node IDs, not from label application).

**Fix**: Re-added `labels: [aw]` to `issue-implementer.md` and recompiled lock file. Labels are now applied by infrastructure, not dependent on agent behavior. Note: recompiling also introduced SHA pinning for `gh-aw-actions/setup` (`@v0.60.0` → `@SHA # v0.60.0`) — this is a compiler behavior change expected in future recompilations.

Closes #108.

---

## fix: prevent duplicate implementer dispatches — 2026-03-21

**Problem**: The orchestrator dispatched a new implementer on every trigger without checking if one was already running. When multiple triggers fired in quick succession (e.g., two PRs merging back-to-back via auto-merge), multiple implementers were dispatched for different issues. GitHub's concurrency group (`cancel-in-progress` was `true`) then cancelled intermediate runs, leaving issues labeled `aw-dispatched` but never worked on. Observed: 4 implementer dispatches in 10 minutes for issues #155, #160, #161, #181 — one cancelled, issues orphaned.

**Root cause**: Three compounding issues: (1) the `push` trigger fired on every merge to main, causing rapid-fire orchestrator runs, (2) `cancel-in-progress: true` killed orchestrator runs mid-flight (potentially after labeling but before the implementer started), (3) no check for in-flight implementer runs.

**Fix (PR #190)**:
1. Removed `push` trigger — 5-minute cron covers post-merge dispatch without the rapid-fire problem.
2. Changed `cancel-in-progress` to `false` — orchestrator runs queue instead of cancelling, so each run sees state left by the previous one.
3. Added in-flight implementer check before dispatching — queries `gh run list` for `in_progress`/`queued`/`waiting` implementer runs and skips dispatch if any exist.
4. Fail-safe: if the `gh run list` API call errors, defaults to "1" (assume something is running) rather than "0" (dispatch anyway).

**Review finding**: `gh run list --status` only accepts a single value — passing it multiple times only uses the last one. Fixed by filtering client-side with `--json databaseId,status --jq '[.[] | select(.status == "in_progress" or .status == "queued" or .status == "waiting")] | length'`. Caught by Copilot code reviewer.

**Lesson**: `cancel-in-progress: true` is dangerous for workflows that modify external state (labels, dispatches) before completing. Use `false` when the workflow has side effects that must complete atomically.

Fixes #164.

---

## fix: pre-fetch review comments via GraphQL — 2026-03-20/21

**Problem**: The MCP `pull_request_read` tool returns empty `[]` for review comments inside the gh-aw agent sandbox. This was confirmed across multiple responder runs — the tool never reliably returns review comment data. The responder couldn't find comments to address.

**Root cause**: Unknown upstream issue with MCP tool behavior in gh-aw sandbox. The GitHub GraphQL API works fine via `gh api graphql` in workflow steps.

**Fix (PR #186)**:
1. Created `.github/workflows/shared/fetch-review-comments.md` — a shared import that runs `gh api graphql` BEFORE the agent starts, writing unresolved review threads to `/tmp/gh-aw/review-data/unresolved-threads.json`.
2. Updated `review-responder.md` to import the shared step and read from the pre-fetched file instead of using MCP tools.
3. Added `databaseId` to GraphQL query so the agent can use `reply_to_pull_request_review_comment` with the correct comment ID.
4. Bumped `comments(first: 10)` to `comments(first: 100)` — proper pagination tracked in issue #185.
5. jq error handling: fail loudly on parse errors instead of silently writing `[]`.

**Critical discovery — `tools:` block in shared imports**: The initial shared import included a `tools: bash:` block listing allowed shell commands. This caused `gh aw compile` to switch from `--allow-all-tools` to a restricted `--allow-tool` list in the lock file. The agent could only run commands explicitly listed (cat, grep, jq, etc.) — everything else got "Permission denied." This broke `uv sync`, `python3 --version`, `pip install`, `curl`, even `git fetch`. Only the responder was affected because only it imported the shared file. **Fix**: removed the `tools:` block entirely from the shared import. This is the same class of bug as pitfall #13 in agentic-workflows.md.

**Tested**: Responder successfully found and addressed review comments on PRs #172 and #177. Both PRs subsequently passed quality gate and auto-merged — first end-to-end test of the pre-fetch pattern.

Closes #180. Related: #183 (astral.sh — not needed), #184 (audit — astral.sh not needed).

---

## fix: quality gate label/approval desync — 2026-03-21

**Problem**: The orchestrator checks for the `aw-quality-gate-approved` label to decide whether to dispatch the quality gate. But branch protection has `dismiss_stale_reviews: true` — when a new commit is pushed (e.g., by the responder or ci-fixer), GitHub automatically dismisses all existing approvals including the quality gate's APPROVE review. The label persists even though the approval is gone, so the orchestrator sees the label and skips the quality gate dispatch. PR stays BLOCKED with no valid approval.

**Observed on PR #177**: Quality gate approved → responder pushed fix commit → GitHub dismissed approval → orchestrator saw label → skipped quality gate → PR stuck.

**Fix**: Manually removed stale labels. Filed issue #187 to fix the orchestrator to check actual review state, not just labels. Proposed solution: agents remove `aw-quality-gate-approved` when they push, AND orchestrator verifies a non-dismissed approval exists.

---

## fix: cron schedule skipped — missing from orchestrator if: condition — 2026-03-20

**Problem**: PR #174 enabled a 5-minute cron on the orchestrator but didn't add `schedule` to the job's `if:` condition. Cron fired correctly but the job was immediately skipped every time.

**Root cause**: Copilot CLI added the trigger to `on:` without checking the job-level `if:` gate. This is the same class of bug as adding a `workflow_dispatch` trigger without updating event-specific conditions.

**Fix**: Add `github.event_name == 'schedule'` to the `if:` condition. Fixes #175.

---

## fix: quality gate dispatch + review approval + cron — 2026-03-19/20

**Problem**: The quality gate workflow triggered on `pull_request_review: submitted`, but the gh-aw pre-activation job filters out bot-submitted reviews. Since the autonomous pipeline has no human reviewers, the quality gate never fired — clean PRs (green CI, no review comments) sat open indefinitely. Example: PR #167 was stuck for 30+ minutes.

**Fix (PRs #169, #170, this PR)**:
1. Switched quality gate trigger from `pull_request_review` to `workflow_dispatch` with `pr_number` input.
2. Added Step 4 to orchestrator: when CI green + 0 open threads → dispatch quality gate.
3. Added `aw-quality-gate-evaluated` label to prevent re-dispatch loops for HIGH impact PRs.
4. Fixed `submit_pull_request_review` safe output: `target: "*"` doesn't work because the tool schema has no `pull_request_number` field. Per gh-aw docs, the correct approach for `workflow_dispatch` is `target: ${{ inputs.pr_number }}`. Labels use `target: "*"` (different handler, resolves via `item_number` from agent output).
5. Enabled 5-minute cron on orchestrator. Public repo — Actions minutes are unlimited. Cron catches new `aw`-labeled issues when the pipeline is idle and no event-driven triggers are firing. Closes #135.

**Lesson**: Lock files are auto-generated — always edit the `.md` and run `gh aw compile`. Manually editing the lock file breaks the frontmatter hash validation (PR #170 was an emergency fix for this).

**Lesson**: Each safe output handler resolves PR context differently. `submit_pull_request_review` needs an explicit target number. `add_labels` needs `target: "*"` with `item_number` in agent output. Read the gh-aw docs for each handler before configuring.

---

## revert: undo orchestrator v3, responder changes, and trigger disable — 2026-03-18

**Problem**: Three PRs were merged to main without adequate testing, creating cascading failures:
- PR #144 (orchestrator v3 + docs + daily test-analysis): Added cron trigger, issue dispatch, and review loop management — none tested end-to-end before merge.
- PR #147 (responder revert): Attempted to restore responder to working version but was merged untested.
- PR #150 (disable triggers): Panic fix to stop orchestrator loops, but disabled v1/v2 triggers that were working fine.

The responder was never able to address review threads in production. Investigation revealed:
1. The responder agent CAN read threads and fix code inside the sandbox.
2. The safe output handlers (`reply-to-pull-request-review-comment`, `push-to-pull-request-branch`) fail outside `pull_request_review` context because they default to `target: "triggering"` which requires PR event data.
3. The `pull_request_review` trigger caused infinite loops — every review from any actor (Copilot, quality gate, humans) fired the responder.

**Fix**: Reverted all three PRs to restore main to post-v2 state (orchestrator v1 thread resolution + v2 auto-rebase, both tested and proven). Responder fix being developed on `fix/responder-v2` branch with controlled testing.

**Key discovery**: Setting `target: "*"` on safe outputs + switching to `workflow_dispatch` trigger allows the responder to work from any context. Successfully tested on PR #152 — responder read thread, fixed code, replied, pushed, and orchestrator resolved the thread automatically.

**Lessons**:
- Never merge untested workflow changes to main.
- The safe output `target` config is critical — `"triggering"` only works with event-based triggers, `"*"` works with `workflow_dispatch`.
- Don't over-specify agent instructions — the original simple responder worked; adding GraphQL queries and ordering constraints broke it.
- Copilot CLI churn (lying about what worked, pushing to wrong branches, merging without permission) was the root cause of most failures.

---

## chore: remove pipeline orchestrator agent — 2026-03-17

**Problem**: The gh-aw orchestrator agent (PR #130) took 7-10 minutes per run for deterministic if/else logic. Over 22+ overnight runs it failed to resolve a single thread — auth failures, wrong action ordering, redundant review requests. Burned significant Opus inference tokens with no results.

**Fix**: Removed `pipeline-orchestrator.md` + `.lock.yml` (PR #137). Added full postmortem at `docs/auto_pr_orchestrator_aw.md`. Will be replaced by a regular GitHub Action (bash) in #135.

**Temporary gaps**: Issue dispatch (implementer) and CI fixer dispatch are inactive until #135. Review-responder and quality-gate continue working.

---

## feat: pipeline orchestrator + review-responder thread ID fix — 2026-03-16

**Problem 1**: Agent PRs get stuck at multiple stages (no Copilot review, unresolved threads, behind main). The old `pr-rescue.yml` bash script only handled rebasing and was brittle — 4 rounds of review across 3 AI models found a combined 13 bugs in 230 lines of bash. (Issues #116, #90)

**Fix 1**: New **pipeline orchestrator** (`pipeline-orchestrator.md`) — gh-aw agent that owns the full lifecycle. Dispatches implementer for eligible issues (one at a time, only if no aw PR in flight). Unsticks stuck PRs: requests Copilot reviews, resolves addressed threads via GraphQL. Pure reasoning agent with no git access. Replaces `pr-rescue.yml`. (Closes #116, #90. Refs #129)

**Problem 2**: Review-responder hallucinates thread IDs because the MCP server doesn't expose `PRRT_` node IDs. All `resolve_pull_request_review_thread` calls fail silently. PRs stay stuck with unresolved threads. (Issues #114, #117)

**Fix 2**: Updated review-responder instructions to query real thread IDs via `gh api graphql` before resolving. No `bash:` tool config added — the responder already has `--allow-all-tools` (adding `bash:` would restrict the allowlist and break CI commands). Instruction-only change. (Closes #117. Refs #114)

**Also**: Moved CI fixer dispatch from `ci.yml` into the orchestrator — all dispatch decisions now centralized in one agent.

---

## fix: gate agent workflows on aw label — 2026-03-16

**Problem**: Agent workflows (review-responder, quality-gate) fired on every `pull_request_review` event, including human-authored PRs. The `aw` label check was only in the agent prompt — a soft guard that still burned compute and inference tokens before noop'ing. Discovered on PR #118. (Issue #120)

**Fix**: Added `if: "contains(github.event.pull_request.labels.*.name, 'aw')"` to both workflow frontmatters. gh-aw compiles this to a job-level `if:` on the activation job — workflow skips entirely at the GitHub Actions level when the label is absent. Zero tokens burned. (PR #119)

**Finding**: `pull_request_review` events use the workflow file from the PR's **head branch**, not the default branch. The `if:` condition was active immediately on the PR itself — no need to merge first.

---

## fix: revert labels config + strengthen responder resolve-before-push — 2026-03-15

**Problem 1**: PR #97 added `labels: ["aw"]` to `create-pull-request` config. This broke label application — the gh-aw handler's post-creation label API call fails non-deterministically with a node ID resolution error, and the tool description tells the agent "labels will be automatically added" so the agent stops including them. PR #104 was created without the `aw` label. (Issue #107)

**Fix 1**: Removed `labels: ["aw"]` from config. Reverted to instruction-based labeling which worked for PRs #61-91. Upstream bug tracked in #108.

**Problem 2**: Review Responder pushed code before resolving threads, invalidating thread IDs. With `required_conversation_resolution: true`, unresolved threads block merge. PRs #91 and #106 were stuck. (Issue #95)

**Fix 2**: Rewrote responder instructions with explicit ordering enforcement: `***PUSH AS LAST STEP***` at step 1, `***MUST***` resolve before push, `***DOUBLE CHECK***` verify ordering, final `***MUST***` all threads resolved. (PR #109, closes #95, #107)

---

## feat: PR Rescue workflow + quality-gate label marker — 2026-03-15

**Problem**: When multiple agent PRs are open and one merges, the others fall behind main. With `strict: true` + `dismiss_stale_reviews: true`, rebasing dismisses the approval and no mechanism re-approves — PRs get stuck forever.

**Fix**:
- New `pr-rescue.yml` workflow: triggers on push to main, finds stuck agent PRs (behind main, `aw` + `quality-gate-approved` labels), rebases them, waits for CI, re-approves. (Issue #86)
- Quality Gate now adds `quality-gate-approved` label on approval (marker for rescue workflow). Added `add-labels` safe-output.
- Documented safe admin merge procedure (disable auto-merge on other PRs first). (Issue #83)
- Documented PR Rescue workflow in agentic-workflows.md.

---

## fix: Quality Gate trigger condition — accept COMMENTED reviews from Copilot — 2026-03-15

**Problem**: Quality Gate instructions required the triggering review to be an APPROVAL from Copilot. But Copilot auto-reviews almost always submit as `COMMENTED` (not `APPROVED`), so the Quality Gate would see the COMMENTED state and stop immediately (noop). This meant the Quality Gate never actually evaluated or approved agent PRs, and auto-merge stayed blocked.

**Fix**: Updated Quality Gate instructions to accept both COMMENTED and APPROVED reviews from Copilot. Added documentation about the auto-merge flow: Quality Gate approval is what triggers GitHub auto-merge on agent PRs. (PR #80, closes #81)

---

## fix: Copilot actor name mismatch in bots list — 2026-03-15

**Problem**: `check_membership.cjs` matches `context.actor` (`Copilot`) against `GH_AW_ALLOWED_BOTS`, but bots list only had `copilot-pull-request-reviewer` (the reviewer login). Actor name mismatch → `activated = false` → agent jobs skipped.

**Fix**: Added both `Copilot` and `copilot-pull-request-reviewer` to `bots:` in review-responder and quality-gate workflows. (PR #72, closes #73)

**Note**: This fix alone was insufficient — see the `roles: all` workaround entry below. The bot check is never reached due to upstream bug [github/gh-aw#21098](https://github.com/github/gh-aw/issues/21098).

---

## workaround: roles: all for bot activation — 2026-03-15

**Problem**: Agent workflows (review-responder, quality-gate) never activate when triggered by Copilot reviews. Root cause is an upstream bug in gh-aw's `check_membership.cjs` ([github/gh-aw#21098](https://github.com/github/gh-aw/issues/21098)) — the `error` branch exits before the bot allowlist fallback is evaluated. Three previous PRs (#64, #65, #72) failed to fix this.

**Workaround**: Added `roles: all` to skip the permission check entirely. Overly permissive but the only option until the upstream bug is fixed. (PR #76, closes #75, tracked for removal in #74)

---

## ci: enable free GitHub security features — 2026-03-13

**Plan**: Enable all free GitHub security features for the repository.

**Done**:
- CodeQL code scanning on PRs to main + weekly Monday schedule
- Dependency review action to block PRs introducing vulnerable dependencies
- Dependabot alerts enabled
- Dependabot automated security updates enabled

---

## feat: copilot-usage CLI tool — 2026-03-13

**Plan**: Build a CLI tool to parse local Copilot session data for token usage, premium requests, and cost data.

**Done**:
- Full tool delivered — see `src/copilot_usage/docs/changelog.md` for details
- PR #1 merged to main
