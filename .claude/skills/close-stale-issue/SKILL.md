---
name: close-stale-issue
description: Close one open GitHub issue with a polite explanatory comment. Posts the comment first, then closes with `--reason not_planned`. Comment body comes from the human-edited modal.
worktree_required: false
---

# Close a stale issue

**Inputs**: `issue.{owner, name, number, title}`, optional
`comment_body` (the close comment — human-edited in the modal),
`identity.github_username`, `dry_run`.

## Procedure

1. Re-verify the issue is still open:

   ```
   gh issue view {issue.number} --repo {issue.owner}/{issue.name} \
     --json state,stateReason
   ```

   - If `state != "OPEN"` → `status: skipped`, message:
     "already closed (state={state}, reason={stateReason})".

2. **If `dry_run == true`**:
   - Print the comment body that *would* be posted.
   - Print the close command that *would* run.
   - Stop. Report `status: skipped_dry_run`.

3. **Otherwise**: post the comment FIRST, then close. Order matters
   — if the close happens first, the comment lands on a closed
   issue and the visual story is "got closed, then somebody
   commented on the corpse." Posting first means the comment
   contextualizes the close.

   If `comment_body` is provided (human-edited), use it verbatim:
   ```
   printf '%s' "$COMMENT_BODY" | \
     gh issue comment {issue.number} --repo {issue.owner}/{issue.name} --body-file -
   ```

   Otherwise compose a default:
   > Closing as stale — no recent activity. Reopen with fresh
   > context if this is still hitting you.

   Then close:
   ```
   gh issue close {issue.number} --repo {issue.owner}/{issue.name} \
     --reason "not planned"
   ```

4. Verify:
   ```
   gh issue view {issue.number} --repo {issue.owner}/{issue.name} \
     --json state,stateReason,closedAt
   ```

   - `state == "CLOSED"` → `status: completed`.
   - Anything else → `status: error`.

## Voice rules

- Don't @-mention `identity.github_username` — the comment is from
  them.
- Be friendly but decisive. The default "we'd rather close than
  leave forever" framing is correct.
- Always end with "reopen if [condition]" so the door isn't slammed.

## Output

```json
{
  "status": "completed | skipped | skipped_dry_run | error",
  "message": "one-sentence summary",
  "comment_posted": true,
  "closed_at": "ISO timestamp or null"
}
```

## Guardrails

- NEVER close with `--reason completed` for a stale-cleanup pass.
  That reads as "this was solved" which is misleading. Always
  `not planned`.
- If `comment_body` is empty/whitespace-only, fall back to the
  default — never close silently.
