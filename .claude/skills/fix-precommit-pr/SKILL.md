---
name: fix-precommit-pr
description: Fix a PR whose only failing check is pre-commit. Runs `pre-commit run` on the PR's changed files, commits the auto-fixes (black / ruff / prettier / trailing-whitespace / etc.), and force-pushes. Dedicated, narrow path — does NOT attempt code reasoning.
worktree_required: true
max_turns: 40
---

# Fix pre-commit drift on a PR

You're already `cd`'d into a git worktree at `worktree_path`, checked
out on the PR's branch (`pr.head_ref`). Triage identified that
**pre-commit is the only failing check**. That almost always means one
of:

- A formatter (black, ruff-format, prettier) is rewriting files the
  PR touched because the formatter config changed, or the PR's
  imports / whitespace drifted from the repo style.
- A linter hook (ruff, eslint) is flagging something cheaply fixable
  (unused imports, trailing whitespace, wrong quote style).
- Auto-generated artifacts (LICENSE headers, babel translations,
  generated type files) need to be regenerated.

Your job is **not** to reason about code — it's to run pre-commit the
same way CI would, accept its auto-fixes, and push. If pre-commit
flags something you can't fix by re-running with auto-fix (genuine
logic errors, complex lint warnings, hook infrastructure failure),
bail to `needs_human` with the offending excerpt.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref}`
- `triage` — prior triage output; `triage.notes.failing_check` usually
  names the pre-commit job (e.g., `"pre-commit (current)"`).
- `dry_run` — if true, DO NOT push.
- `worktree_path` — absolute path of the checked-out worktree.
- `pr.push_remote` — the git remote name to push to. For maintainer-
  authored PRs this is `"origin"`; for fork PRs where maintainer
  edits are enabled, this is a per-PR remote (e.g. `"pr-fork-39432"`)
  that the dispatcher already configured with the fork's URL.
- `pr.push_ref` — the branch name to push as (same as `pr.head_ref`
  in practice; always prefer this).

## Procedure

### 1. Figure out the changed files

Pre-commit in CI runs on the PR's diff, not the whole repo. Mirror
that:

```
git fetch origin master --depth=1 2>/dev/null || true
BASE=$(git merge-base HEAD origin/master 2>/dev/null || git merge-base HEAD origin/main)
git diff --name-only --diff-filter=ACMR "$BASE"...HEAD
```

Keep the list — you'll pass it to pre-commit with `--files`.

### 2. Reproduce CI's failure locally

```
pre-commit run --files <the changed files>
```

If pre-commit isn't installed in the worktree, install it first
(`pip install pre-commit` or `uv pip install pre-commit`) or
`pre-commit install --install-hooks` as needed. Expect the initial
hook environment setup to take a minute or two on a cold worktree.

Capture the failing hook names and a short excerpt. You'll need them
for the commit message and for your result JSON.

### 3. Let pre-commit auto-fix what it can

Many hooks rewrite files in place (black, ruff-format, prettier,
trailing-whitespace, end-of-file-fixer, mixed-line-ending). Just run
pre-commit again:

```
pre-commit run --files <the changed files>
```

Then:

```
git status --porcelain
```

If files got rewritten, stage only the ones that were already part of
this PR — don't drag in repo-wide cleanup that slipped in from a
hook operating outside its usual scope.

```
git add <the rewritten subset of the PR's changed files>
```

### 4. Re-run until clean, OR bail

Run pre-commit once more on the same file list. Acceptable outcomes:

- **All hooks pass.** Proceed to commit.
- **Same hook fails again with a non-auto-fixable error** (e.g., ruff
  flags an unused import it can't auto-remove, eslint error that
  needs human judgment). Bail: return `needs_human` with the hook
  name, the file, and the 5-line excerpt. Do NOT hand-edit code to
  satisfy a linter — that's attempt-fix's job.
- **A hook complains about a file that isn't in the PR's diff.**
  Something is wrong with the hook config; bail with `needs_human`.
- **Hook infrastructure failure** (network, missing tool, registry
  404). Bail with `status: "error"` and the error message.

### 5. Commit and push

```
git status --porcelain
git add <only the PR's files that were rewritten>
git commit -m "fix: apply pre-commit auto-fixes"
```

The commit body should name the hooks that rewrote files (e.g.,
`ruff-format reformatted 3 files; trailing-whitespace fixed 1 file`).

- `dry_run == true`:
  `git push --dry-run --force-with-lease {pr.push_remote} HEAD:{pr.push_ref}`
- Otherwise:
  `git push --force-with-lease {pr.push_remote} HEAD:{pr.push_ref}`

Use the `push_remote` / `push_ref` from context — don't hardcode
`origin`, because fork PRs push to a different remote configured by
the dispatcher.

## Guardrails

- NEVER hand-edit code to silence a lint warning. If pre-commit's
  auto-fix can't do it, bail.
- NEVER stage files outside the PR's diff, even if pre-commit
  modified them. Repos sometimes have hooks that reformat adjacent
  files; that's out of scope here.
- NEVER disable / skip hooks (no `--no-verify`, no editing
  `.pre-commit-config.yaml`, no `SKIP=...` env var) to make them pass.
- NEVER run `git push` without `--force-with-lease`.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one sentence: which hooks rewrote what",
  "hooks_fixed": ["ruff-format", "trailing-whitespace"],
  "files_changed": ["superset/x.py", "superset/y.py"],
  "commit_sha": "abc1234",
  "pushed": true
}
```

- `status: "skipped_dry_run"` when you committed locally but did not
  push because `dry_run == true`.
- `status: "needs_human"` when a hook fails in a way auto-fix can't
  resolve — include the hook name, file, and excerpt in `message`.
- `status: "error"` when a command failed unrecoverably.
