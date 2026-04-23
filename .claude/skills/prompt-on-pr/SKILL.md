---
name: prompt-on-pr
description: Execute a free-form user instruction on a PR, with the branch checked out in a worktree and prior triage context available.
worktree_required: true
---

# Free-form PR action driven by a user instruction

The user clicked "prompt" on a card and typed an instruction telling
you what they want done with this PR. Execute it.

## Inputs (runtime context)

- `pr.{owner,name,number,url,title,head_ref}` — PR metadata.
- `triage` — prior triage output, if any:
  - `triage.proposal` — the one-sentence proposal from the triage session.
  - `triage.notes` — failing check, log excerpt, classification.
- `instruction` — **the user's free-form request. This is your primary directive.**
- `dry_run` — when true, do not make any external mutations (no
  `gh pr comment`, `gh pr close`, `git push`, `@dependabot` comments).
  Just report what you *would* do.
- `worktree_path` — you are already `cd`d into this directory. The PR's
  branch is checked out here. You may inspect the code, run tests,
  etc. locally.

## Procedure

1. Read `instruction` carefully. Treat it as what a maintainer would
   say out loud to a teammate: sometimes it's a request for a comment,
   sometimes a close-with-explanation, sometimes a question ("check
   whether X is the real issue here"), sometimes a tell ("re-run only
   the frontend checks").

2. Use the PR metadata and the prior `triage` output as context — don't
   re-diagnose from scratch unless the user is asking you to.

3. Do only what the instruction asks. If it's ambiguous, pick the most
   conservative reasonable interpretation and note it in the output.

4. Mutations you may perform when `dry_run == false`:
   - `gh pr comment <n> --repo <o>/<r> --body "..."`
   - `gh pr close <n> --repo <o>/<r> [--comment "..."]`
   - `gh pr ready <n>` / `gh pr edit <n>` for labels, reviewers, etc.
   - `@dependabot rebase` / `@dependabot recreate` via `gh pr comment`

5. Mutations you must NOT perform from this skill (use the typed
   actions instead):
   - `git push` / `git push --force` — use `rebase` or
     `update-lockfile` for branch changes.
   - `gh pr merge` — humans merge, not the bot.

6. If the instruction asks for something outside the scope above
   (e.g., "ship a fix"), stop and emit `status: needs_human` with a
   note explaining why.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one-sentence summary of what you did (or would have done)",
  "notes": "longer explanation if helpful — e.g., the comment body, the reason for needs_human"
}
```
