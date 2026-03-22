#!/usr/bin/env bash
set -euo pipefail
REPO="${1:?Usage: $0 owner/repo}"
STASH="/tmp/hold-for-merge-prs.txt"

get_protection() {
  gh api "repos/${REPO}/branches/main/protection" --jq '{
    conversation_resolution: .required_conversation_resolution.enabled,
    enforce_admins: .enforce_admins.enabled,
    dismiss_stale_reviews: .required_pull_request_reviews.dismiss_stale_reviews,
    required_approvals: .required_pull_request_reviews.required_approving_review_count,
    require_code_owner_reviews: .required_pull_request_reviews.require_code_owner_reviews,
    require_last_push_approval: .required_pull_request_reviews.require_last_push_approval,
    status_checks_strict: .required_status_checks.strict,
    checks: [.required_status_checks.checks[].context],
    required_signatures: .required_signatures.enabled,
    required_linear_history: .required_linear_history.enabled,
    allow_force_pushes: .allow_force_pushes.enabled,
    allow_deletions: .allow_deletions.enabled,
    block_creations: .block_creations.enabled,
    lock_branch: .lock_branch.enabled,
    allow_fork_syncing: .allow_fork_syncing.enabled
  }'
}

BEFORE=$(get_protection)

echo "=== Disabling auto-merge on open PRs ==="
gh pr list --repo "${REPO}" --state open --json number,autoMergeRequest \
  --jq '.[] | select(.autoMergeRequest != null) | "\(.number) \(.autoMergeRequest.mergeMethod)"' > "${STASH}"
if [[ -s "${STASH}" ]]; then
  while read -r PR METHOD; do
    gh pr merge --repo "${REPO}" --disable-auto "$PR"
    echo "  disabled auto-merge on #${PR} (was ${METHOD})"
  done < "${STASH}"
else
  echo "  (none found)"
fi

echo ""
echo "=== Disabling enforce_admins ==="
gh api "repos/${REPO}/branches/main/protection/enforce_admins" -X DELETE > /dev/null

# Re-enable enforce_admins if anything below fails
trap 'echo "ERROR: re-enabling enforce_admins after failure"; gh api "repos/${REPO}/branches/main/protection/enforce_admins" -X POST > /dev/null 2>&1; exit 1' ERR

AFTER=$(get_protection)
echo ""
echo "${BEFORE}" | AFTER_JSON="${AFTER}" python3 -c "
import json, os, sys
before = json.load(sys.stdin)
after = json.loads(os.environ['AFTER_JSON'])
changing = {'enforce_admins'}
R, G, NC = '\033[0;31m', '\033[0;32m', '\033[0m'
h = ('Setting', 'Before', 'After', 'Status')
print(f'{h[0]:<35} {h[1]:<15} {h[2]:<15} {h[3]}')
print(f'{chr(45)*35} {chr(45)*15} {chr(45)*15} {chr(45)*20}')
ok = True
for k in before:
    b = str(before[k])
    a = str(after.get(k))
    if k in changing:
        if b == 'True' and a == 'False':
            print(f'{G}{k:<35} {b:<15} {a:<15} ✓ disabled{NC}')
        else:
            print(f'{R}{k:<35} {b:<15} {a:<15} ✗ should have changed{NC}')
            ok = False
    else:
        if a == b:
            print(f'{k:<35} {b:<15} {a:<15} unchanged')
        else:
            print(f'{R}{k:<35} {b:<15} {a:<15} ✗ changed unexpectedly!{NC}')
            ok = False
if not ok:
    print(f'\n{R}ERROR: Some settings are wrong. Do NOT merge.{NC}')
    sys.exit(1)
print(f'\n{G}All good. Merge your PR, then run release-from-merge.sh{NC}')
"
