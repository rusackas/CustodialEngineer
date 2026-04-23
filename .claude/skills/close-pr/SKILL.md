---
name: close-pr
description: Close a GitHub PR using `gh pr close`, optionally leaving a comment explaining why.
worktree_required: false
---

# Close a PR

**Inputs** (from runtime context): `pr.owner`, `pr.name`, `pr.number`,
optional `comment_body` (the human-reviewed text to post as the
closing comment — prefer this over `reason`), optional `reason`
(legacy fallback), `identity.github_username` (the human operating
this bot), and `dry_run` (boolean).

## Voice guardrails (read before composing any comment)

- `identity.github_username` names the human operating this bot. NEVER
  @-mention them, NEVER refer to them in third person ("as @username
  noted"), NEVER write attribution like "posted on @username's behalf".
  The comment is from them — write in first person if you write at all.
- Dependabot and other bots (`dependabot[bot]`, `coderabbitai[bot]`,
  `dosu`, `sonar`, etc.) are not people. Don't thank them, don't say
  "great work", don't address them conversationally. Bot-directed
  text is strictly operational (e.g., `@dependabot rebase`).
- If `comment_body` is provided, post it verbatim — the human already
  approved the wording. Do not rewrite, reformat, or append.

## Procedure

1. Decide the comment text:
   - If `comment_body` is present and non-empty, that's your comment
     (the human already approved it — do NOT rewrite it).
   - Else if `reason` is present, use it.
   - Else skip the comment.
2. If `dry_run` is true: print the exact `gh pr close` (and optional
   `gh pr comment`) commands you *would* have run, then stop. Report
   `status: skipped_dry_run`.
3. Otherwise:
   - Post the comment first if one is set:
     ```
     gh pr comment {pr.number} --repo {pr.owner}/{pr.name} --body-file -
     ```
     (pipe the body via stdin so multiline / special chars survive)
   - Then close:
     ```
     gh pr close {pr.number} --repo {pr.owner}/{pr.name}
     ```
4. Confirm closure with `gh pr view {pr.number} --repo {pr.owner}/{pr.name} --json state`.
   - If `state == CLOSED`: report `status: completed`.
   - Otherwise: report `status: error` with the observed state.

## Output

Emit a single fenced JSON block:

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "short one-sentence summary",
  "notes": "optional extra detail"
}
```
