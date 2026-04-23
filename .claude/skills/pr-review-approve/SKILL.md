---
name: pr-review-approve
description: Submit a formal "Approve" review on a PR (no merge). Uses `comment_body` verbatim when provided — the human edited it in the modal before it reached here. For approve-and-merge-in-one-step use the `approve-merge` action instead.
worktree_required: false
---

# Submit an "approve" review on a PR (without merging)

Use this when the code looks good but you don't want to merge yet —
CI isn't done, you want to wait for another reviewer, or the PR
author has said "hold off". This records your approval officially so
the PR is unblocked from the "needs review" state.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title}`.
- `comment_body` — OPTIONAL approval body. May be empty (plain
  approve with no note).
- `dry_run` — if true, do not call the API; log the intended body.
- `identity.github_username` — you, the reviewer. Don't @-mention
  yourself.

## Procedure

1. **Submit the review**. If `comment_body` is non-empty, pipe it
   through stdin:

   ```
   printf '%s' "$COMMENT_BODY" | gh pr review {pr.number} \
     --repo {pr.owner}/{pr.name} --approve --body-file -
   ```

   If empty, approve without a body:

   ```
   gh pr review {pr.number} --repo {pr.owner}/{pr.name} --approve
   ```

   In `dry_run`: log the command + body, do NOT execute.

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one sentence: approved #N",
  "review_url": "optional — the review html_url if gh prints one",
  "posted_body": "the body submitted, or empty string"
}
```

## Guardrails

- NEVER rewrite `comment_body` when present — the human approved it.
- Approve review is LESS destructive than request-changes, but still
  a public signal. In `dry_run` log clearly so the user can spot it.
- This skill does NOT merge. For approve-and-merge see `approve-merge`.
