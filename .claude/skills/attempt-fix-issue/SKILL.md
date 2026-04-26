---
name: attempt-fix-issue
description: Try to implement a fix for one open issue from scratch. Worktree is checked out on a fresh `ce/issue-{N}` branch off default. On success, push and open a PR with `Closes #N` so the link auto-resolves. Bails to needs_human if the issue isn't actionable from code alone.
worktree_required: true
---

# Attempt a fix for an issue

**Inputs**: `issue.{owner, name, number, title, body, labels,
comments, author_login, comments_count}`, `worktree_path` (the
repo checked out on `ce/issue-{N}`, branched off the default),
`identity.github_username`, `dry_run`.

The user clicked "attempt-fix-issue" because the issue looks
solvable from the code. Your job: read the issue, investigate the
codebase, implement the fix in the worktree, push the branch, open
a PR with `Closes #N`. If the issue isn't a real bug-shaped problem
or the fix is unclear, bail with `status: needs_human` — don't
push speculative changes.

## Procedure (budget: ~30 turns)

### 1. Sanity-check the issue is actionable

A well-shaped bug report will have one or more of:
- A clear description of incorrect behavior + expected behavior.
- A repro (steps, code snippet, screenshot).
- A version / environment that pins the surface area.

If the issue is:
- Empty or one-line vague ("it's broken") → `status: needs_human`,
  message: "issue body lacks repro / specifics; need clarification
  before a fix is safe."
- A feature request, not a bug → `status: needs_human`, message:
  "feature request, not a bug — needs design discussion, not a
  speculative implementation."
- Already discussed at length without a clear consensus on the
  fix approach → `status: needs_human`, message: "no consensus on
  approach in the comment thread; would push a fix the maintainers
  may reject."

### 2. Reproduce or isolate

If the bug has a reproducible code path:
- Find the relevant module (grep / read).
- Trace the flow from the user-visible symptom back to the code
  that's wrong.
- If you can write a failing test that captures the bug, do it
  (in the test file alongside existing tests for the module).
  This keeps you honest about whether your fix actually addresses
  the report.

If you can't reproduce or isolate within ~10 turns:
- `status: needs_human`, message: "couldn't isolate the failing
  code path from the report; need a maintainer-pointed
  reproduction."

### 3. Implement the fix

- Smallest scope that fixes the bug. Don't refactor surrounding
  code, don't fix adjacent unrelated issues.
- Add/update tests that would have caught this. If the codebase
  has a test for the module, add a test case there. If not, write
  one (and accept that may surface a "no test infrastructure for
  this area" need_human bail).
- Match the codebase's existing style and patterns. Don't
  introduce new dependencies, new abstractions, or new file-layout
  conventions unless the issue specifically asks for them.

### 4. Run the local checks

Run pre-commit / linters / formatters that the repo configures
(typically `pre-commit run --files <changed>`, `npm run lint`,
`npx tsc --noEmit`, `pytest <touched-tests>` — read the repo's
contributor docs to know which apply).

If checks fail in ways your changes caused, fix them. If checks
fail in ways unrelated to your changes (pre-existing flakes,
infra issues), note the failure in your report but don't get
distracted.

### 5. Commit and push

Use a focused commit message:
```
Fix <one-line summary> (#{issue.number})

<2-4 line context: what was wrong, how the fix addresses it>
```

Push the branch:
```
git push -u origin HEAD
```

(The worktree is on `ce/issue-{N}`; HEAD pushes that branch up.)

### 6. Open the PR

```
gh pr create \
  --repo {issue.owner}/{issue.name} \
  --base <default-branch> \
  --head ce/issue-{issue.number} \
  --title "Fix: <issue.title>" \
  --body "$(cat <<'EOF'
Closes #{issue.number}

## Summary
<2-3 bullet points of what the fix does>

## Test plan
- <how to verify, ideally pointing at the new test>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Capture the PR URL from the command output.

### 7. **If `dry_run == true`**

Stop before pushing. Print the diff that *would* be committed and
the PR description that *would* be created. Report
`status: skipped_dry_run` with the diff in `notes`.

## Output

```json
{
  "status": "completed | needs_human | skipped_dry_run | error",
  "message": "one-sentence summary",
  "pr_url": "https://github.com/.../pull/N or null",
  "pr_number": 1234,
  "files_changed": ["path/one.py", "path/two.test.py"],
  "needs_human_reason": "when status is needs_human, why",
  "notes": "optional dump of the diff for dry_run"
}
```

## Guardrails

- **Don't push without tests** that would have caught the bug,
  unless the issue is explicitly asking for a non-functional change
  (docs typo, comment fix, etc.) where tests don't apply.
- **Don't touch unrelated files.** A PR that fixes one bug and
  reformats 30 unrelated files is a maintainer headache.
- **Don't speculate on unclear bugs.** `needs_human` is the right
  answer when the report is too vague — pushing the wrong fix to
  an issue with an audience makes the situation worse.
- Don't include "Refs #N" or "See #N" in the PR body. Use
  `Closes #N` so GitHub auto-closes the issue when the PR merges.
- Don't open the PR as draft. The maintainer can convert it later
  if they want — opening as ready signals "this is the proposed
  fix; please review."
