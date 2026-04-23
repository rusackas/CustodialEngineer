---
name: dependabot-recreate-comment
description: Post `@dependabot recreate` on a stale PR so Dependabot rebuilds it against the latest base and the latest target version.
worktree_required: false
---

# Ask Dependabot to recreate

Use this for PRs that have drifted far from master, where a plain rebase
is unlikely to help — `recreate` gives you a clean PR against current
master and picks up any newer target version.

**Inputs**: `pr.{owner,name,number}`, `dry_run`, `identity.github_username`
(the human operating this bot), optional `comment_body` (defaults to
`@dependabot recreate`; the human may have edited it to add context).

Dependabot is a bot — don't thank it, don't address it as a person,
keep the comment operational. If `comment_body` is provided, post it
verbatim.

## Procedure

1. Verify the PR is still open and Dependabot-authored
   (`gh pr view ... --json state,author`).

2. Body = `comment_body` if provided and non-empty, else
   `@dependabot recreate`. The literal `@dependabot recreate` trigger
   must remain in the body — if the user's edit dropped it, add it
   back on its own line at the top.

3. **If dry_run**: print the command, stop. `status: skipped_dry_run`.

4. **Otherwise** (stdin so multiline survives):
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
