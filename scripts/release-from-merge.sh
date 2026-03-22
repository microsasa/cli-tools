#!/usr/bin/env bash
set -euo pipefail
REPO="${1:?Usage: $0 owner/repo}"
STASH="/tmp/hold-for-merge-prs.txt"

if [[ ! -f "${STASH}" ]]; then
  echo "ERROR: ${STASH} not found. Did hold-for-merge.sh run?" >&2
  exit 1
fi

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

echo "=== Re-enabling enforce_admins ==="
gh api "repos/${REPO}/branches/main/protection/enforce_admins" -X POST > /dev/null

echo ""
echo "=== Re-enabling auto-merge on saved PRs ==="
if [[ -s "${STASH}" ]]; then
  while read -r PR METHOD; do
    case "${METHOD}" in
      SQUASH) merge_flag="--squash" ;;
      REBASE) merge_flag="--rebase" ;;
      *)      merge_flag="--merge" ;;
    esac
    gh pr merge --repo "${REPO}" --auto "${merge_flag}" "$PR"
    echo "  enabled auto-merge on #${PR} (${METHOD})"
  done < "${STASH}"
else
  echo "  (none to restore)"
fi

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
for k in after:
    b = str(before.get(k))
    a = str(after[k])
    if k in changing:
        if b == 'False' and a == 'True':
            print(f'{G}{k:<35} {b:<15} {a:<15} ✓ re-enabled{NC}')
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
    print(f'\n{R}ERROR: Some settings are wrong. Fix manually.{NC}')
    sys.exit(1)
print(f'\n{G}All settings restored correctly.{NC}')
"

rm "${STASH}"
