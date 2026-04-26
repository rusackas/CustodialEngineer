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

### 1. Find the PR author so the comment voice is right

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json author,state,isDraft
```

- `state != "OPEN"` → `status: skipped`, message: "already closed."
- Capture `author.login` and `author.is_bot`.

### 2. Decide the comment text

Closing someone else's open PR without a word is rude. **For
non-self, non-bot PRs, a comment is REQUIRED** — never silently
close human contributions.

Priority:

1. **`comment_body` provided** — the human already approved it.
   Post verbatim, no rewrites.

2. **No `comment_body`, non-self PR (human author)** — compose a
   thankful default. Open with `@{author.login}`. Acknowledge the
   contribution. Keep it short. Always end with "feel free to
   reopen if you want to push this through" so the door isn't
   slammed. Examples:

   > @alice — thanks for the PR. Closing for now since this hasn't
   > moved in a while. Feel free to reopen if you want to push it
   > through.

   > @bob — thanks for opening this. Going to close for now — the
   > approach we'd want has shifted since this was opened
   > (<one-sentence pointer if obvious from triage notes>). Reopen
   > or open a fresh PR any time.

3. **No `comment_body`, bot author** (`dependabot[bot]`,
   `renovate[bot]`, etc.) — bots aren't people. Skip the comment;
   the close itself is the signal. Don't compose a thank-you.

4. **`reason` provided as a legacy fallback** — use it as the
   comment body verbatim.

5. **Self-authored PR** (author.login == identity.github_username) —
   comment optional. The user is closing their own work; an
   explanation isn't owed to anyone.

### 3. If `dry_run == true`

Print the exact comment + close commands. Stop. Report
`status: skipped_dry_run`.

### 4. Otherwise: comment first, then close

Order matters — the comment lands ON the open PR (a normal-
looking interaction), then the close generates its own timeline
event. Closing first means the comment lands on a corpse and the
visual story reads as "got closed, then somebody talked over it."

```
printf '%s' "$COMMENT_BODY" | \
  gh pr comment {pr.number} --repo {pr.owner}/{pr.name} --body-file -

gh pr close {pr.number} --repo {pr.owner}/{pr.name}
```

### 5. Verify

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} --json state
```

- `state == CLOSED` → `status: completed`.
- Otherwise → `status: error`.

## Output

Emit a single fenced JSON block:

```json
{
  "status": "completed | skipped_dry_run | error",
  "message": "short one-sentence summary",
  "notes": "optional extra detail"
}
```
