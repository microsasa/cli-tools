# `GH_AW_WRITE_TOKEN` rotation runbook

The autonomous pipeline writes to GitHub (labels, comments, reviews, branch
pushes, workflow dispatches) using a single fine-grained personal access token
stored as the repository secret **`GH_AW_WRITE_TOKEN`**. Expiration is set to
30 days as a deliberate forced-rotation control. GitHub emails a reminder
seven days before expiry; if the rotation is missed, every agentic workflow
will fail with HTTP 401 starting on the expiry day.

This document captures the exact token configuration so rotation is mechanical
and cannot introduce over-grants by accident.

## Rotation procedure

1. Go to **GitHub → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens**.
2. Click **Generate new token** (or regenerate the existing one). Use the
   settings in [Token specification](#token-specification) below.
3. Copy the token value immediately — GitHub will not show it again.
4. Go to the repository: **Settings → Secrets and variables → Actions**.
5. Edit the existing secret named `GH_AW_WRITE_TOKEN` and paste the new value.
   Do **not** create a second secret with a different name; every workflow
   references this exact name.
6. Trigger any scheduled workflow once (for example, run
   `pipeline-orchestrator` manually via `gh workflow run
   pipeline-orchestrator.yml`) to confirm the new token works. A successful
   run logs no `401` errors and produces the usual output.
7. If rotation happens **before** the old token expires, revoke the old token
   on GitHub after confirming the new one works.

## Token specification

Reproduce these settings exactly on every rotation.

### Repository access

- **Only select repositories** → `microsasa/cli-tools` (one repository).
- Not "All repositories". Not "Public repositories".

> Note: the GitHub UI has a known quirk where, on reload, the radio button
> visual state shows "All repositories" even when the underlying
> select-list has a restricted entry. The **select-list contents** are the
> source of truth, not the radio button. If `cli-tools` is the only entry in
> that list, scope is correct.

### Repository permissions

Set exactly these; leave everything else as **No access**.

| Permission       | Access  | Why |
|------------------|---------|-----|
| Actions          | Read and write | `pipeline-orchestrator` dispatches agent workflows via `gh workflow run`. Dispatch requires Actions: write. |
| Contents         | Read and write | `push-to-pull-request-branch`, `create-pull-request`, and the orchestrator's authenticated checkout. |
| Issues           | Read and write | `create-issue`, `add-comment` on issues/PRs (PR comments are issue comments), `add-labels`. |
| Pull requests    | Read and write | `submit-pull-request-review`, `close-pull-request`, `reply-to-pull-request-review-comment`, `create-pull-request` (co-requires Contents: write), auto-merge. |
| Metadata         | Read-only (auto) | Mandatory, granted automatically by GitHub. |

### Account permissions

All **No access**. No account-level capability is needed.

### Expiration

**30 days.** Do not extend without a security review. Short expiration is a
deliberate control — the token replaces revocation/rotation infrastructure we
do not have.

## Consumers of this token

If the list below drifts, audit the token scope again before rotating.

| File | Mechanism | Why it needs the token |
|------|-----------|------------------------|
| `.github/workflows/ci-fixer.md` | `safe-outputs` | push branch, add labels, comment |
| `.github/workflows/code-health.md` | `safe-outputs` | create issue |
| `.github/workflows/feature-planner.md` | `safe-outputs` | create issue |
| `.github/workflows/issue-implementer.md` | `safe-outputs` | create PR (with auto-merge), push branch |
| `.github/workflows/perf-agent-improver.md` | `safe-outputs` | create issue |
| `.github/workflows/perf-analysis.md` | `safe-outputs` | create issue |
| `.github/workflows/quality-gate.md` | `safe-outputs` | submit review, close PR, comment, labels |
| `.github/workflows/review-responder.md` | `safe-outputs` | push branch, reply to review comment, labels |
| `.github/workflows/test-analysis.md` | `safe-outputs` | create issue |
| `.github/workflows/pipeline-orchestrator.yml` | direct `env.GH_TOKEN` + checkout `token:` | dispatch workflows, edit PR labels/comments |

To regenerate this list, run:

```bash
grep -l GH_AW_WRITE_TOKEN .github/workflows/*.md .github/workflows/*.yml
```

## Related

- Security audit [#92](https://github.com/microsasa/cli-tools/issues/92) —
  finding H1 (shared write token, no rotation policy documented) is tracked
  here.
- The alternative architecture — a GitHub App with short-lived minted tokens
  per workflow run — is more secure but is deferred work. `gh-aw` supports
  `github-app:` configuration in frontmatter; migrating would remove the need
  for a long-lived PAT entirely.
