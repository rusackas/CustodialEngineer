---
name: approve-pr-ci
description: Approve workflow runs on a first-time-contributor PR that GitHub has gated behind the "Approve and run workflow" maintainer click. One short flow — list the waiting runs on the PR's head SHA, POST the approve endpoint for each.
worktree_required: false
---

# Approve a PR's pending CI runs

**Inputs**: `pr.{owner,name,number}`, `dry_run`.

GitHub gates first-time-contributor PRs (and some `pull_request_target`
flows) behind a manual "Approve and run workflow" click. Until the
click happens, the workflow runs sit in `action_required` /
`waiting` and never start. This skill performs the click via API.

## Procedure

1. Fetch the PR's head SHA:
   ```
   gh pr view {pr.number} --repo {pr.owner}/{pr.name} --json headRefOid
   ```

2. List workflow runs on that SHA awaiting approval. The states we
   care about are `action_required` (first-contributor gate
   post-creation) and `waiting` (deployment / environment approval):
   ```
   gh api "repos/{pr.owner}/{pr.name}/actions/runs?head_sha=<sha>&per_page=50" \
     --jq '.workflow_runs[] | select(.status == "waiting" or .conclusion == "action_required") | {id, name, status, conclusion}'
   ```

3. **If dry_run**: list the run IDs that *would* be approved, then
   stop. Report `status: skipped_dry_run`.

4. **Otherwise**: for each gated run, POST the approval endpoint:
   ```
   gh api -X POST "repos/{pr.owner}/{pr.name}/actions/runs/<id>/approve"
   ```

   Don't fail the whole skill on one bad approval — collect errors
   and continue, then report what succeeded vs. failed.

5. Report how many were approved (or would be).

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "Approved N waiting workflow runs on <sha>.",
  "notes": "list of run IDs that were (or would be) approved",
  "errors": "list of {id, error} for any that failed — omit when empty"
}
```

## Guardrails

- Only acts on workflow runs whose `status == waiting` or whose
  `conclusion == action_required`. Don't approve anything else.
- If no waiting runs exist, return `status: completed` with a
  message saying so. (Idempotent — clicking the button twice is
  fine.)
- If `gh pr view --json headRefOid` errors, bail with `status: error`
  and the underlying message; we can't approve runs without the SHA.
