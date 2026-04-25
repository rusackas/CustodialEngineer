---
name: label-stale-issue
description: Add a `stale` label to one open issue as a 30-day warning before close. Creates the label on the repo if it doesn't exist yet.
worktree_required: false
---

# Label an issue as stale

**Inputs**: `issue.{owner, name, number}`, `dry_run`.

## Procedure

1. Check whether the `stale` label exists on this repo:

   ```
   gh label list --repo {issue.owner}/{issue.name} \
     --search stale --json name --jq '.[].name' | grep -ix '^stale$'
   ```

2. If it doesn't exist, create it (idempotent — the `||` guards
   against a race):

   ```
   gh label create stale --repo {issue.owner}/{issue.name} \
     --color fbca04 \
     --description "No recent activity — auto-closes in 30 days unless reopened." \
     || true
   ```

3. **If `dry_run == true`**: print what *would* be applied; stop.
   Report `status: skipped_dry_run`.

4. **Otherwise**: add the label.

   ```
   gh issue edit {issue.number} --repo {issue.owner}/{issue.name} \
     --add-label stale
   ```

5. Verify:

   ```
   gh issue view {issue.number} --repo {issue.owner}/{issue.name} \
     --json labels --jq '.labels[].name'
   ```

   - `stale` is in the list → `status: completed`.
   - Otherwise → `status: error`.

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one-sentence summary",
  "label_existed": true
}
```

## Guardrails

- Don't post a comment from this skill. The label is the signal;
  the comment-on-stale flow is `nudge-issue-author`'s job.
- Idempotent — re-running is fine. If the label is already there,
  `gh issue edit --add-label` is a no-op.
