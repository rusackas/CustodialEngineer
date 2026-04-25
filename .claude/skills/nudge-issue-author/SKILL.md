---
name: nudge-issue-author
description: Post a polite ping comment on an open issue to revive the thread — typically asking the reporter to confirm a still-relevant problem or supply missing repro info. Body comes from the human-edited modal.
worktree_required: false
---

# Nudge the issue author

**Inputs**: `issue.{owner, name, number, title, author_login}`,
optional `comment_body` (the nudge body — human-edited in the modal),
`identity.github_username`, `dry_run`.

## Procedure

1. Re-verify the issue is still open:

   ```
   gh issue view {issue.number} --repo {issue.owner}/{issue.name} \
     --json state,closed
   ```

   - If `state != "OPEN"` → `status: skipped`, message: "already
     closed; nothing to nudge."

2. **If `dry_run == true`**: print the comment that *would* be
   posted. Stop. Report `status: skipped_dry_run`.

3. **Otherwise**: post the comment.

   If `comment_body` is provided, use it verbatim:
   ```
   printf '%s' "$COMMENT_BODY" | \
     gh issue comment {issue.number} --repo {issue.owner}/{issue.name} \
       --body-file -
   ```

   Otherwise compose a default:
   > @{author_login} — circling back here. Is this still hitting
   > you? If so, could you share a repro / config / fresh details?
   > Happy to dig in once we have something concrete.

4. Verify the comment landed:
   ```
   gh issue view {issue.number} --repo {issue.owner}/{issue.name} \
     --json comments --jq '.comments[-1].author.login'
   ```

   The last comment's author should match `identity.github_username`.
   - Match → `status: completed`.
   - Otherwise → `status: error` (race / API hiccup).

## Voice rules

- The nudge is from `identity.github_username` — never @-mention
  them.
- ALWAYS open with `@{author_login}` so the reporter gets pinged.
  Without the @, the comment is invisible to them.
- Friendly + low-pressure. "Circling back / no rush / still
  hitting you?" — never accusatory.
- Be specific about what you want from them. "Could you share a
  repro?" beats "any update?"
- End with a low-friction next step ("happy to dig in once …" /
  "let me know either way").

## Output

```json
{
  "status": "completed | skipped | skipped_dry_run | error",
  "message": "one-sentence summary",
  "comment_url": "https://github.com/.../issuecomment-... or null"
}
```

## Guardrails

- If `comment_body` is empty/whitespace-only, fall back to the
  default. Never post an empty comment.
- One nudge at a time — don't post multiple comments per click.
