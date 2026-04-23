---
name: attempt-fix-pr
description: Attempt a minimal, targeted fix for a Dependabot PR whose triage identified a real code breakage caused by the dependency bump (e.g., breaking API change that consumers must adapt to). Commits inside the worktree and force-pushes when dry_run is false.
worktree_required: true
max_turns: 80
---

# Attempt fix for a Dependabot PR

You are already `cd`'d into a git worktree at `worktree_path`, checked out
on the PR's branch (`pr.head_ref`). The triage run has already identified
a **real code breakage** — typically a breaking API change in the bumped
dependency that consumer code (tests, app code, types) must now adapt to.

You are NOT doing a broad rewrite. You are doing the **smallest
targeted diff** that unblocks the bump.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref}`
- `triage` — prior triage output:
  `{ proposal, source, notes: { classification, failing_check, log_excerpt, run_url } }`
- `dry_run` — if true, DO NOT push; log the command you would have run.
- `worktree_path` — absolute path of the checked-out worktree.

## Procedure

### 1. Read the triage

Re-read `triage.proposal` and `triage.notes.log_excerpt`. These name the
failing check and the concrete symptom (e.g., "9 tests fail with
TestingLibraryElementError"). Treat the triage as your ground truth for
WHAT to fix.

### 2. Understand the dependency change

Look at what the PR is bumping and skim the changelog / release notes:

```
gh pr diff {pr.number} --repo {pr.owner}/{pr.name} | head -200
```

If the bump is cross-major (e.g., 1.x → 2.x), check the library's
release notes / CHANGELOG for breaking changes that match the symptom.
You have `WebFetch` available for vendor changelogs when useful.

### 3. Pull the failing log once more if needed

If the log excerpt in `triage.notes.log_excerpt` is not enough to locate
the exact source lines:

```
gh run view <run-id> --repo {pr.owner}/{pr.name} --log-failed | tail -300
```

Grep the repo for the offending symbols / imports / role names to find
call sites:

```
rg --fixed-strings "getByRole('link'" -l
```

### 4. Apply the minimal fix

Edit **only** the files that the failure signature points at. For
common patterns:

- **Breaking API rename** (e.g., `role='link'` → `role='button'` in
  rendered output): update the call sites / assertions.
- **Removed export**: switch to the new name or re-export.
- **Type-only breakage**: adjust the type import / generics.
- **Snapshot drift from intentional output change**: regenerate the
  affected snapshots (`--u` only on the specific test file).
- **TypeScript project-reference errors (TS6305: "Output file has
  not been built from source")** in apache/superset: the referenced
  `.d.ts` files come from a separate build step. Run
  `npm run plugins:build` from inside `superset-frontend/` (it
  builds the plugin/preset packages that other packages reference).
  If that resolves the missing `.d.ts` files, continue; if other TS
  errors remain, treat them as real breakage and fix the call sites.

### 5. Verify locally if cheap (< ~30s)

If the project has a cheap single-file check, run it on just the file(s)
you touched — e.g., `pytest path/to/file_test.py::Case -x`,
`npx jest path/to/file.test.tsx`, `npx tsc --noEmit -p <tsconfig>` on a
small surface. Do NOT run the full test suite.

### 5b. Run pre-commit on the files you touched

Many repos enforce pre-commit in CI. Your edits may have introduced
formatter drift (wrong quote style, missing trailing newline, import
order) that CI will flag even if the code is logically correct. Before
committing, run pre-commit on just the files you modified:

```
pre-commit run --files <files you edited>
```

If hooks rewrote anything (black, ruff-format, prettier,
trailing-whitespace), re-stage those files and re-run until clean. If
a non-auto-fixable hook (e.g., a type-checker or a strict lint rule)
flags something in your edit, fix it — it's part of "adapting to the
breaking change." If pre-commit isn't installed in the worktree, skip
this step rather than blocking on installing it.

### 6. Commit

```
git status --porcelain
```

Make sure only the files you intentionally modified are dirty. Then:

```
git add <files you changed>
git commit -m "fix(deps): adapt to <lib> breaking change"
```

The commit message body should name the library, the breaking change in
one line, and which files were updated.

### 7. Push (or dry-run)

- `dry_run == true`:
  `git push --dry-run --force-with-lease origin HEAD:{pr.head_ref}`
- Otherwise:
  `git push --force-with-lease origin HEAD:{pr.head_ref}`

## Guardrails

- Touch at most ~50 files. If the breakage spans more, return
  `status: "needs_human"` with a list of affected paths. This is a
  soft ceiling — genuinely mechanical bulk changes (codemod-style
  renames, import path updates) are fine up to the cap; if you're
  hand-authoring unique logic across that many files, you're probably
  out of bot scope regardless of the count.
- NEVER disable / skip tests to make them pass. Fix them, or punt.
- NEVER modify unrelated files (`package.json`, other lockfiles,
  configs outside the failure scope).
- NEVER run `git push` without `--force-with-lease` (Dependabot branches
  are rewritten frequently).
- Budget is the `max_turns` cap in frontmatter. If you're past 2/3 of
  it and still not green, bail to needs_human rather than thrash.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one sentence: what was broken, what you did",
  "files_changed": ["superset-frontend/src/.../FilterScope.test.tsx"],
  "commit_sha": "abc1234",
  "pushed": true
}
```

- `status: "skipped_dry_run"` when you made a commit locally but did not
  push because `dry_run == true`.
- `status: "needs_human"` when you determined the fix is out of scope
  (too broad, ambiguous, requires product decisions).
- `status: "error"` when a command failed unrecoverably.
