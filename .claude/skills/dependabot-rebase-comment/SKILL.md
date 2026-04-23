---
name: dependabot-rebase-comment
description: Post `@dependabot rebase` on a PR to ask Dependabot to rebase the branch itself.
worktree_required: false
---

# Ask Dependabot to rebase

**Inputs**: `pr.{owner,name,number}`, `dry_run`, `identity.github_username`
(the human operating this bot), optional `comment_body` (defaults to
`@dependabot rebase`; the human may have edited it to add context like
"please rebase after #12345 lands").

Dependabot is a bot — don't thank it, don't address it as a person,
keep the comment operational. If `comment_body` is provided, post it
verbatim.

## Procedure

1. Spot-check the PR is still open and still authored by Dependabot:
   ```
   gh pr view {pr.number} --repo {pr.owner}/{pr.name} --json state,author
   ```
   If not open or not a Dependabot PR, report `status: error`.

2. Body = `comment_body` if provided and non-empty, else
   `@dependabot rebase`. The body MUST still contain the literal
   `@dependabot rebase` trigger — if the user's edit dropped it, add
   it back on its own line at the top.

3. **If dry_run**: print the exact `gh pr comment` command you would
   have run, then stop. Report `status: skipped_dry_run`.

4. **Otherwise** (pipe stdin so multiline bodies survive):
   ```
   gh pr comment {pr.number} --repo {pr.owner}/{pr.name} --body-file -
   ```

## Output

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "one-sentence summary"
}
```
