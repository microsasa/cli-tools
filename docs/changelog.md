# CLI Tools — Changelog

Append-only history of repo-level changes (CI, infra, shared config). Tool-specific changelogs live with each tool (e.g. `src/copilot_usage/docs/changelog.md`).

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
