# Pipeline Orchestrator (gh-aw Agent) — Postmortem

## Summary

We attempted to build a gh-aw agent ("Pipeline Orchestrator") to shepherd agent PRs through the full CI/CD lifecycle: dispatch implementers for issues, request Copilot reviews, resolve unresolved threads, detect CI failures. After extensive development, testing, and debugging, the approach was abandoned. The agent takes 7-10+ minutes per run for deterministic logic that should execute in seconds as bash.

## Timeline

### Phase 1: PR Rescue as bash (PR #87, #97)

**What**: Original `pr-rescue.yml` — bash script triggered on push to main. Found `aw`-labeled PRs behind main, rebased them.

**PRs**: #87 (initial), #97 (6 bug fixes: missing git identity, BLOCKED check too broad, single failure aborts loop, cancel-in-progress corruption, unguarded checkout/abort)

**Issues fixed**: #86, #98, #99, #100, #101, #102

**Outcome**: Worked but only handled rebasing. PRs still got stuck on unresolved threads and missing reviews.

### Phase 2: Enhanced rescue as bash (PR #118, #121)

**What**: Expanded `pr-rescue.yml` to handle three rescue modes: request Copilot review, resolve addressed threads, rebase behind main. 230 lines of bash under `set -euo pipefail`.

**Issue**: #116

**Review rounds**:
1. **Copilot** found 6 bugs: unguarded API calls, `git checkout` fails on fresh runner, pagination cap at 50
2. **Gemini 3 Pro** found 3 more: shell injection via branch names starting with `-`, `first:0` invalid in GraphQL, bot error replies auto-resolved
3. **OpenAI Codex** found 1 critical logic bug: thread resolution checked for `github-actions[bot]` but the responder posts as the PAT owner (microsasa)
4. Then: hardcoded username instead of deriving from token → Copilot caught it. Added stray `--` to `git checkout -B` → Copilot caught it.

**PRs**: #118 (closed), #121 (closed after 7 fix commits across 4 review rounds)

**Outcome**: Every fix introduced new bugs. 13 bugs found total in 230 lines of bash.

### Phase 3: gh-aw agent attempt (pr-rescue.md)

**What**: Rewrote rescue as a gh-aw agent to escape bash fragility. Natural language instructions instead of shell scripting.

**Discovery 1**: Added `bash: ["gh:api:graphql"]` to tools → compiler switched from `--allow-all-tools` to a specific allowlist → broke CI commands (uv, ruff, pyright, pytest). Lesson: adding ANY explicit bash tools restricts the allowlist.

**Discovery 2**: `push-to-pull-request-branch` safe-output generates patches via `git format-patch` — it cannot do `git push --force-with-lease` after a rebase. The agent literally cannot perform the core rebase operation.

**Outcome**: Abandoned. The gh-aw safe-output model doesn't support force-push.

### Phase 4: Pipeline Orchestrator (PR #130, merged)

**What**: User proposed splitting responsibilities — an orchestrator agent handles everything EXCEPT rebasing. Pure reasoning agent with no git access.

**Design**:
- Issue dispatch: find `code-health`/`test-audit` issues with no PR, dispatch implementer (one at a time, only if no aw PR in flight)
- PR orchestration: request reviews, resolve threads, detect CI failures
- Rebase: log and skip (requires manual intervention)

**Changes in PR #130**:
- New `pipeline-orchestrator.md` (gh-aw agent, Opus model)
- Removed `dispatch-workflow` from code-health and test-analysis (centralized in orchestrator)
- Moved CI fixer dispatch from `ci.yml` to orchestrator
- Updated review-responder with GraphQL thread ID lookup instructions
- Deleted `pr-rescue.yml`
- Extensive documentation updates

**Review rounds**: 3 rounds across Codex, Gemini, Opus. Found and fixed:
- Double dispatch race (code-health/test-analysis + orchestrator both dispatching)
- `${{ }}` expressions don't expand in agent prompts (runtime-import)
- `gh api user` returns 403 with installation token
- GraphQL OWNER/REPO as string literals (need -f variables)
- Agent can't check in-progress workflow runs without `actions` toolset
- Adding `bash:` config broke `--allow-all-tools` on review-responder

### Phase 5: Orchestrator in production (22+ runs over ~22 hours)

**What happened overnight**: Orchestrator ran 22 times on 15-min cron. Every run either:
- Reported `missing_data` (couldn't authenticate `gh api graphql` to get thread IDs)
- Reported `missing_tool` (same root cause)
- Added redundant comments
- Requested reviews that already existed
- Noop'd

**Root cause**: `GH_AW_GITHUB_MCP_SERVER_TOKEN` secret wasn't set up. The fallback chain `GH_AW_GITHUB_MCP_SERVER_TOKEN || GH_AW_GITHUB_TOKEN || GITHUB_TOKEN` resolved to `GITHUB_TOKEN` (installation token) which can't do GraphQL. Even after adding the secret:
- `gh` CLI in the sandbox uses `GH_TOKEN` or `GITHUB_TOKEN`, not `GITHUB_MCP_SERVER_TOKEN`
- Need to prefix commands with `GH_TOKEN="$GITHUB_MCP_SERVER_TOKEN"`
- Even with correct auth, the agent made wrong decisions (re-requested reviews, noop'd instead of resolving threads)

**Performance**: Each run took 7-10+ minutes. Opus inference for if/else logic. Batched GraphQL query (#131) didn't help because the bottleneck is LLM round-trips, not API calls.

## Key Learnings

### When to use gh-aw agents
- Tasks requiring **judgment**: code review, implementation, debugging, test writing
- Tasks with **ambiguous input**: understanding issue specs, interpreting review comments
- Tasks where **error recovery** benefits from reasoning

### When NOT to use gh-aw agents
- **Deterministic orchestration**: if/else on API state, dispatching workflows
- **Simple data transformations**: query GraphQL, filter results, call API
- **Anything on a tight cron loop**: 10-minute inference for 2-second logic is wasteful

### Technical findings
1. Adding `bash:` to tools config switches compiler from `--allow-all-tools` to specific allowlist — breaks existing capabilities
2. `push-to-pull-request-branch` can't force-push (patches only, no rebase support)
3. `${{ }}` GitHub Actions expressions don't expand in agent prompts — use env vars or plain placeholders
4. `gh api user` returns 403 with installation tokens — use `$GITHUB_REPOSITORY_OWNER` for solo repos
5. `GITHUB_MCP_SERVER_TOKEN` is available in sandbox but `gh` CLI doesn't use it — need `GH_TOKEN=` prefix
6. Agent instructions via `{{#runtime-import}}` load from PR's head branch, not main — fixes only apply to new/rebased PRs
7. Concurrency group with `cancel-in-progress: false` queues runs — cron + dispatch can stack up

## What to do instead

Rewrite as a regular GitHub Action (`pipeline-orchestrator.yml`) with bash:
- Batched GraphQL query gets all PR state in one call
- Deterministic if/else logic in bash
- Auth via `GH_AW_GITHUB_MCP_SERVER_TOKEN` for reads, `GH_AW_WRITE_TOKEN` for writes
- Runs in seconds, not minutes
- See issue #135

## Related issues
- #116 — Enhanced PR rescue (closed, superseded)
- #117 — Responder thread ID fix (open, runtime-import limitation)
- #114 — MCP server thread ID bug (open, upstream tracking)
- #129 — Orchestrator spec (closed, implemented then abandoned)
- #131 — Batched GraphQL query (open, incorporate into yml)
- #135 — Rewrite as yml (open)
- #136 — Remove orchestrator (open)

## Related PRs
- #87 — Original pr-rescue.yml
- #97 — 6 bug fixes for pr-rescue.yml
- #109 — Responder resolve-before-push fix
- #118 — Enhanced rescue attempt 1 (closed)
- #119 — Label gate fix (merged)
- #121 — Enhanced rescue attempt 2 (closed)
- #130 — Pipeline orchestrator (merged, to be reverted)
- #134 — Auth fix attempt (closed)

## Upstream issues
- github/gh-aw#21130 — MCP server thread ID exposure (open, no response)
- github/gh-aw#21098 — Bot activation bug (closed, fixed)
- github/gh-aw#21103 — merge-pull-request safe-output request (open)
