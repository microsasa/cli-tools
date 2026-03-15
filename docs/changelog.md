# CLI Tools — Changelog

Append-only history of repo-level changes (CI, infra, shared config). Tool-specific changelogs live with each tool (e.g. `src/copilot_usage/docs/changelog.md`).

---

## fix: Copilot actor name mismatch in bots list — 2026-03-15

**Problem**: `check_membership.cjs` matches `context.actor` (`Copilot`) against `GH_AW_ALLOWED_BOTS`, but bots list only had `copilot-pull-request-reviewer` (the reviewer login). Actor name mismatch → `activated = false` → agent jobs skipped.

**Fix**: Added both `Copilot` and `copilot-pull-request-reviewer` to `bots:` in review-responder and quality-gate workflows. (PR #72, closes #73)

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
