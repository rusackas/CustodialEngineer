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

2. Use the PR / issue metadata and the prior `triage` output as
   context — don't re-diagnose from scratch unless the user is asking
   you to.

3. Do only what the instruction asks. If it's ambiguous, pick the most
   conservative reasonable interpretation and note it in the output.

4. **Read-only operations are always allowed.** Use `gh pr view`,
   `gh pr diff`, `gh issue view`, `gh api` for queries, file reads,
   `git log` / `git diff`, and any local checks (running tests,
   linters, `pre-commit run`). Investigate freely.

5. **Public mutations are NEVER allowed from this skill.** Every
   typed action (close, add-review-comment, nudge-author, rebase,
   approve-merge, etc.) has a dedicated skill that runs the user
   through the comment-edit modal first. This skill exists for ad-
   hoc work and must not pre-empt that review step. Specifically,
   you must NOT run any of:
   - `gh pr comment` / `gh issue comment` — drafts a public
     comment. Use `add-review-comment` (PRs) or `nudge-issue-
     author` (issues) — both surface the body in the modal for
     review-and-edit before posting.
   - `gh pr close` / `gh issue close` — public state change. Use
     `close` (PRs) or `close-as-stale` (issues).
   - `gh pr ready` / `gh pr edit --add-label` / `gh pr edit --add-
     reviewer` / similar — public PR/issue mutations. Use
     `mark-as-draft`, `request-reviewers`, etc.
   - `gh pr merge` — humans merge, not the bot.
   - `git push` / `git push --force` — use `rebase` /
     `update-lockfile` / `attempt-fix` for branch changes.
   - Replying via `@dependabot rebase` or similar bot commands
     posted as comments — that's `dependabot-rebase`'s job, with
     the modal flow.

6. **When the instruction asks for a public mutation, draft it
   instead.** Compose the proposed body/action in your output,
   bail with `status: needs_human`, and point the user at the
   typed action that owns it. Example output for "post a follow-up
   confirming and close":

   ```json
   {
     "status": "needs_human",
     "message": "Drafted a close comment + verified the work has landed. Use the `close` action to review/edit/post — this skill doesn't post mutations directly.",
     "proposed_action": "close",
     "proposed_comment": "Closing — Column.tsx already exists on master via #N. Thanks for the PR; reopen if you want to push it through.",
     "notes": "Confirmed Column.tsx, Column.test.tsx, and index.ts all exist on master."
   }
   ```

   The `close` action's modal is pre-fillable from `proposed_
   comment` so the user gets a one-click "review and ship" path.

7. If the instruction is purely a question ("check whether X is the
   real issue"), answer in the output's `message` and `notes`. No
   bail needed — `status: completed` is correct since the question
   was answered without mutating anything.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one-sentence summary of what you did (or would have done)",
  "proposed_action": "Optional — the typed action id the user should run to land the proposed mutation (e.g., `close`, `add-review-comment`, `nudge-author`, `mark-as-draft`). Empty when no mutation was proposed.",
  "proposed_comment": "Optional — the drafted comment body the proposed action should pre-fill its modal with. Empty when no comment was drafted.",
  "notes": "longer explanation if helpful — investigation findings, the reasoning behind a proposed_action, the reason for needs_human"
}
```
