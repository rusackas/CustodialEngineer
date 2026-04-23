---
name: pr-review-request-changes
description: Submit a formal "Request changes" review on a PR with a human-reviewed body. Uses `comment_body` verbatim — the triage skill drafted it and the human edited/approved it before it reached here.
worktree_required: false
---

# Submit a "request changes" review on a PR

This is the formal GitHub review flow — blocks merge until dismissed
or re-reviewed. Use sparingly; for gentler feedback prefer
`add-pr-review-comment`. Because a "request changes" review is a
stronger signal to the author, the human is expected to have edited
the draft body in the modal before this skill runs.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title}`.
- `comment_body` — the reviewed, human-edited review body. REQUIRED.
  Non-empty.
- `dry_run` — if true, do not call the API; log the intended body.
- `identity.github_username` — you, the reviewer. Do NOT @-mention
  yourself; first-person voice only.

## Procedure

1. **Validate**. Empty `comment_body` → `status: "error"`.

2. **Submit the review** via `gh pr review`, piping the body through
   stdin so multiline content survives:

   ```
   printf '%s' "$COMMENT_BODY" | gh pr review {pr.number} \
     --repo {pr.owner}/{pr.name} --request-changes --body-file -
   ```

   In `dry_run`: log the command + body, do NOT execute.

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one sentence: changes requested on #N",
  "review_url": "optional — the review html_url if gh prints one",
  "posted_body": "the body submitted (verbatim)"
}
```

## Guardrails

- NEVER rewrite `comment_body` — the human approved it.
- NEVER submit an approve or comment review via this skill —
  request-changes only.
- If the CLI errors because you've already reviewed this PR, bubble
  the error up with `status: "error"`; don't retry automatically.
