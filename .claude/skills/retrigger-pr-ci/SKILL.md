---
name: retrigger-pr-ci
description: Rerun failed workflow runs on a PR's head SHA.
worktree_required: false
---

# Retrigger a PR's failing CI runs

**Inputs**: `pr.{owner,name,number}`, `dry_run`.

## Procedure

1. Fetch the PR's head SHA:
   ```
   gh pr view {pr.number} --repo {pr.owner}/{pr.name} --json headRefOid
   ```

2. List workflow runs for that commit:
   ```
   gh run list --repo {pr.owner}/{pr.name} --commit <sha> \
     --json databaseId,name,status,conclusion,workflowName --limit 50
   ```

3. Identify runs with `conclusion` in `failure`, `cancelled`, `timed_out`,
   or `startup_failure`. These are the candidates.

4. **If dry_run**: list the candidate run IDs/names that *would* be
   rerun, then stop. Report `status: skipped_dry_run`.

5. **Otherwise**: for each failed run, rerun only the failed jobs:
   ```
   gh run rerun <databaseId> --failed --repo {pr.owner}/{pr.name}
   ```

6. Report how many were rerun.

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one-sentence summary",
  "notes": "list of run IDs that were (or would be) rerun"
}
```
