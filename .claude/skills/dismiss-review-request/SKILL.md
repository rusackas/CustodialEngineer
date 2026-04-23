---
name: dismiss-review-request
description: Remove the user from a PR's requested-reviewers list via the GitHub API. The PR stops showing up in the review-requested queue.
worktree_required: false
---

# Dismiss a review request (stop being asked to review this PR)

You're telling GitHub the user is no longer a requested reviewer on
this PR. This does NOT dismiss a review that's already been
submitted — only the *request*. Common reason: the PR got into the
user's queue but someone else is the right reviewer, or it's been
reassigned, or it's a duplicate.

No code changes, no comments, no worktree. One API call.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title}`.
- `identity.github_username` — you, the reviewer being removed.
  REQUIRED — the API call targets this handle.
- `dry_run` — if true, do not call the API; log what you would do.

## Procedure

1. **Validate** that `identity.github_username` is set. If not,
   bail with `status: "error"`.

2. **DELETE the request** via the REST endpoint. The body is
   JSON with a `reviewers` array containing the user's handle:

   ```
   gh api \
     --method DELETE \
     -H "Accept: application/vnd.github+json" \
     /repos/{pr.owner}/{pr.name}/pulls/{pr.number}/requested_reviewers \
     -f "reviewers[]={identity.github_username}"
   ```

   In `dry_run`: log the command, do NOT execute.

3. **Do not comment on the PR.** The dismissal is silent — that's
   the point. If the user wanted to leave a note, they'd have used
   `add-review-comment` instead.

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one sentence: review request removed for {identity.github_username} on #N"
}
```

## Guardrails

- NEVER remove any reviewer other than `identity.github_username`.
  Other reviewers stay on the PR.
- NEVER post a comment explaining the dismissal. Silent dismissal
  is intentional — comments generate notifications the author
  probably doesn't need.
- If the API responds that the user isn't currently a requested
  reviewer (404 / no-op), treat that as success with message
  "was not a requested reviewer".
