---
name: ping-pr-reviewers
description: Post a polite @-mention comment on your own PR asking currently-requested reviewers for an update. Uses the human-edited `comment_body` verbatim — the triage skill drafts it and the human reviews/edits it in the modal before it lands here.
worktree_required: false
---

# Ping the reviewers on your own PR

You're posting a single top-level comment on one of the user's own
PRs that's been sitting waiting for a review. The body @-mentions the
reviewers who have already been requested (so they get a GitHub
ping) and politely asks for an update.

The caller is using the editable-comment modal pattern: the human
opens the modal with a pre-filled draft from
`triage.notes.ping_comment`, edits it, confirms, and the edited text
lands here as `comment_body`.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title}`.
- `comment_body` — the reviewed, human-edited text to post. REQUIRED.
  Non-empty.
- `dry_run` — if true, do not call the API; log the intended body.
- `identity.github_username` — you, the PR author. Do NOT @-mention
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
  "message": "one sentence: reviewer ping posted to #N",
  "comment_url": "https://github.com/...#issuecomment-123",
  "posted_body": "the body you posted (verbatim)"
}
```

## Guardrails

- NEVER rewrite `comment_body`. The human already approved it.
- NEVER @-mention `identity.github_username` (the user is the PR
  author; pinging yourself is noise).
- If the body still contains placeholder text like `{reviewer1}`
  or `@...`, return `status: "error"` — the human didn't fill it
  in.
