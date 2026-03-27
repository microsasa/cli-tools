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

      # Paginate review comment threads via GraphQL (100 per page, 10-page cap)
      HAS_NEXT_PAGE="true"
      CURSOR=""
      PAGE_COUNT=0
      ALL_THREADS="[]"

      while [ "$HAS_NEXT_PAGE" = "true" ]; do
        if [ -n "$CURSOR" ]; then
          AFTER_ARG="-f cursor=$CURSOR"
        else
          AFTER_ARG=""
        fi

        RESULT=$(gh api graphql -f query='
          query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
            repository(owner: $owner, name: $repo) {
              pullRequest(number: $pr) {
                reviewThreads(first: 100, after: $cursor) {
                  pageInfo {
                    hasNextPage
                    endCursor
                  }
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
          }' -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" $AFTER_ARG)

        PAGE_THREADS=$(echo "$RESULT" | jq '[.data.repository.pullRequest.reviewThreads.nodes[]]')
        ALL_THREADS=$(echo "$ALL_THREADS" "$PAGE_THREADS" | jq -s 'add')

        HAS_NEXT_PAGE=$(echo "$RESULT" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage')
        CURSOR=$(echo "$RESULT" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor')

        PAGE_COUNT=$((PAGE_COUNT + 1))
        echo "  Page ${PAGE_COUNT}: fetched $(echo "$PAGE_THREADS" | jq 'length') thread(s)"

        if [ $PAGE_COUNT -ge 10 ] && [ "$HAS_NEXT_PAGE" = "true" ]; then
          echo "Warning: hit 10-page safety cap (1000+ threads). Stopping pagination."
          break
        fi
      done

      echo "Total threads fetched: $(echo "$ALL_THREADS" | jq 'length')"

      # Wrap in the same structure the old single-page response had
      echo "$ALL_THREADS" | jq '{data: {repository: {pullRequest: {reviewThreads: {nodes: .}}}}}' \
        > /tmp/gh-aw/review-data/threads.json

      # Extract unresolved threads for easy agent consumption
      if ! echo "$ALL_THREADS" | jq '[.[] | select(.isResolved == false) | .comments = .comments.nodes]' \
        > /tmp/gh-aw/review-data/unresolved-threads.json; then
        echo "Error: Failed to extract unresolved review threads" >&2
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

- `/tmp/gh-aw/review-data/threads.json` — All review threads (paginated, reconstructed wrapper)
- `/tmp/gh-aw/review-data/unresolved-threads.json` — Filtered to unresolved threads only (agent reads this)

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
