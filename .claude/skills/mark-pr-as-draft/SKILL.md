---
name: mark-pr-as-draft
description: Convert an open PR to draft and post a comment naming the outstanding work + warning that the PR may be closed in a future triage sweep if it doesn't move. Soft-warning sibling to label-as-stale. Parks the card.
worktree_required: false
---

# Mark PR as draft (warn-and-park)

**Inputs**: `pr.{owner, name, number, title}`, optional
`comment_body` (the warning comment — human-edited in the modal),
`triage.proposal` and `triage.notes` (so you can recap what's
outstanding), `identity.github_username`, `dry_run`.

The intent: a maintainer wants to keep the PR alive but signal "you
need to move this." Demoting to draft hides it from the
review-requested feeds and frees reviewers from feeling
responsible; the comment explains the conditions for re-opening as
ready and what would close it in a future sweep.

## Procedure

### 1. Re-verify the PR is open and not already a draft

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json state,isDraft
```

- `state != "OPEN"` → `status: skipped`, message: "PR isn't open."
- `isDraft == true` → `status: skipped`, message: "already a draft.
  Consider close-as-stale or nudge-author instead."

### 2. **If `dry_run == true`**

Print the comment that *would* be posted + the convert command.
Stop. Report `status: skipped_dry_run`.

### 3. Post the warning comment FIRST

Order matters — convert-to-draft generates a state-change event
on the timeline; posting the comment first means the explanation
sits ABOVE that event in the thread, so the author reading
top-down sees "here's what's needed → and the PR was demoted to
draft" instead of "PR was demoted (?) → here's an explanation
buried below."

If `comment_body` is provided (human-edited), use it verbatim:

```
printf '%s' "$COMMENT_BODY" | \
  gh pr comment {pr.number} --repo {pr.owner}/{pr.name} \
    --body-file -
```

Otherwise compose a default:

> @{author} — converting to draft for now. To bring this back to
> ready: <recap from triage.proposal / triage.notes — failing CI
> by name, unresolved threads with @asker + path:line, etc.>.
>
> If there's no movement in a future triage sweep, this may get
> closed. Reopen as ready any time once the items above are
> addressed.

(In the default body, write the recap as concrete bullets — quote
failing check names, asker handles, file:line excerpts. Vague
nudges read as dismissive.)

### 4. Convert to draft

```
gh pr ready {pr.number} --repo {pr.owner}/{pr.name} --undo
```

(`--undo` is gh's flag for "make it a draft again." Don't ask
why it's named that way.)

### 5. Verify

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} --json isDraft
```

- `isDraft == true` → `status: completed`.
- Anything else → `status: error` (race / API hiccup; the comment
  may have landed without the conversion).

## Voice rules

- The comment is from `identity.github_username` — never @-mention
  them.
- ALWAYS open with `@{author}` so the PR author gets pinged.
- Tone: friendly, specific, low-pressure. "Converting to draft for
  now" beats "marking this as stale." The warning that future
  sweeps may close it is real but stated as a possibility, not a
  threat.
- Recap should be CONCRETE — quote failing check names, asker
  handles, specific file:line. Don't say "address the comments"
  when you can name them.
- Always end with how to bring it back ("Reopen as ready any time
  once …").

## Output

```json
{
  "status": "completed | skipped | skipped_dry_run | error",
  "message": "one-sentence summary",
  "comment_posted": true,
  "is_draft_now": true
}
```

## Guardrails

- If `comment_body` is empty/whitespace-only, fall back to the
  default — never demote silently.
- One PR at a time — don't post multiple comments per click.
- Don't add a `stale` or `wip` label here; demoting to draft is
  itself the signal.
