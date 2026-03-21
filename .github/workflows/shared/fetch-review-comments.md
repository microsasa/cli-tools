---
steps:
  - name: Fetch PR review comments
    env:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      PR_NUMBER: ${{ inputs.pr_number }}
    run: |
      mkdir -p /tmp/gh-aw/review-data

      OWNER="${GITHUB_REPOSITORY_OWNER}"
      REPO="${GITHUB_REPOSITORY#*/}"

      echo "Fetching review comments for PR #${PR_NUMBER}..."

      # Fetch review comment threads via GraphQL (includes resolution status)
      gh api graphql -f query='
        query($owner: String!, $repo: String!, $pr: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) {
              title
              body
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  isOutdated
                  comments(first: 100) {
                    nodes {
                      id
                      databaseId
                      body
                      path
                      line
                      author { login }
                      createdAt
                    }
                  }
                }
              }
            }
          }
        }' -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" \
        > /tmp/gh-aw/review-data/threads.json

      # Extract unresolved threads for easy agent consumption
      if ! jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .comments = .comments.nodes]' \
        /tmp/gh-aw/review-data/threads.json \
        > /tmp/gh-aw/review-data/unresolved-threads.json; then
        echo "Error: Failed to extract unresolved review threads from GraphQL response" >&2
        exit 1
      fi

      UNRESOLVED=$(jq 'length' /tmp/gh-aw/review-data/unresolved-threads.json)
      echo "Found ${UNRESOLVED} unresolved thread(s)"
      echo "Data saved to /tmp/gh-aw/review-data/"
---

<!--
## Fetch PR Review Comments

Shared component that pre-fetches PR review comment threads before the agent runs.
The agent reads `/tmp/gh-aw/review-data/unresolved-threads.json` instead of using MCP tools.

### Why

The GitHub MCP `pull_request_read` tool intermittently returns empty arrays `[]` inside
the gh-aw agent sandbox. This has been observed consistently in our repo — the tool
never reliably returns review comment data. Pre-fetching via `gh api` in a workflow step
(which has GITHUB_TOKEN) bypasses the MCP entirely.

### Output Files

- `/tmp/gh-aw/review-data/threads.json` — Full GraphQL response with all review threads
- `/tmp/gh-aw/review-data/unresolved-threads.json` — Filtered to unresolved threads only

### Usage

Import in your workflow:

```yaml
imports:
  - shared/fetch-review-comments.md
```

Then tell the agent to read the pre-fetched data:

```markdown
Read the unresolved review threads from `/tmp/gh-aw/review-data/unresolved-threads.json`.
```
-->
