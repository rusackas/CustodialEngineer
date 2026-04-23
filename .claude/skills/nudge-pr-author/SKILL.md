---
name: nudge-pr-author
description: Post a polite maintainer "nudge" comment on a PR you were asked to review, prompting the author to address CI failures or unresolved review feedback. Uses the human-edited `comment_body` verbatim — the triage skill drafts it and the human reviews/edits it in the modal before it lands here.
worktree_required: false
---

# Nudge the PR author with a top-level comment

You're posting a single top-level comment on a PR where the user is
a requested reviewer. The point of the nudge is to prompt the PR
author to do something: fix CI, respond to a question, or address
unresolved feedback from other reviewers. Tone is polite and
maintainer-voice; specifics come from the triage.

The caller is using the editable-comment modal pattern: the human
opens the modal with a pre-filled draft from
`triage.notes.nudge_comment`, edits it, confirms, and the edited
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
  "message": "one sentence: nudge posted to #N",
  "comment_url": "https://github.com/...#issuecomment-123",
  "posted_body": "the body you posted (verbatim)"
}
```

## Guardrails

- NEVER rewrite `comment_body`. The human already approved it.
- NEVER @-mention `identity.github_username`.
- If the PR author's or another reviewer's handle appears in the
  draft body, that's expected — the human put it there deliberately
  so GitHub pings them.
