---
name: add-pr-review-comment
description: Post a top-level comment on a PR you've been asked to review. Uses the human-provided `comment_body` verbatim — the triage skill drafted it and the human reviewed/edited it in the modal before it landed here.
worktree_required: false
---

# Post a PR review comment (top-level)

You're posting a plain comment on a PR (not an inline code comment,
not a formal review). The caller is using the editable-comment modal
pattern: the human opens the modal with a pre-filled draft from
`triage.notes.suggested_comment`, edits it, confirms, and the edited
text lands here as `comment_body`.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title}`.
- `comment_body` — the reviewed, human-edited text to post. REQUIRED.
  Non-empty.
- `dry_run` — if true, do not call the API; log the intended body.
- `identity.github_username` — you, the reviewer. Do NOT @-mention
  yourself; first-person voice only.

## Procedure

1. **Validate**. If `comment_body` is missing or whitespace-only,
   return `status: "error"` with a message saying the modal
   produced an empty body.

2. **Post** via `gh pr comment`, piping the body through stdin to
   handle newlines / multiline content safely:

   ```
   printf '%s' "$COMMENT_BODY" | gh pr comment {pr.number} \
     --repo {pr.owner}/{pr.name} --body-file -
   ```

   In `dry_run`: log the command + body, do NOT execute.

3. **Capture the comment URL** if the command prints one; otherwise
   leave it null.

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one sentence: comment posted to #N",
  "comment_url": "https://github.com/...#issuecomment-123",
  "posted_body": "the body you posted (verbatim)"
}
```

## Guardrails

- NEVER rewrite `comment_body`. The human already approved it.
- NEVER @-mention `identity.github_username`.
- If the PR author's handle appears in the draft body, that's fine —
  the human put it there.
