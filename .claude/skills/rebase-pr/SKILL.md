---
name: rebase-pr
description: Rebase a PR branch onto master inside a git worktree, resolve trivial conflicts, and force-push with --force-with-lease.
worktree_required: true
---

# Rebase a PR

You are already `cd`'d into a git worktree checked out at the PR's head
branch (`pr.head_ref`). `origin` points at the upstream repo.

**Inputs**: `pr.{owner,name,number,head_ref,push_remote,push_ref}`,
`dry_run`. `push_remote` is "origin" for in-repo PRs and a per-PR
fork remote (e.g. `pr-fork-39432`) for fork PRs — the dispatcher
sets it up before calling you. Always push using these, not a
hardcoded `origin`.

## Procedure

1. `git status --porcelain` — confirm clean worktree. Abort with
   `status: error` if dirty.

2. `git fetch origin master`.

3. `git rebase origin/master`.

4. **If conflicts:**
   - Inspect the conflicted files. For trivial cases (e.g. both sides
     bump the same dependency version — keep the PR's version; both
     sides touch separate lines of a lockfile — accept theirs then
     regenerate) attempt a resolution.
   - After resolution, `git add -A` and `git rebase --continue`.
   - If you hit a non-trivial conflict (application code, test logic,
     anything requiring judgment beyond a version bump), run
     `git rebase --abort` and report `status: needs_human` with a note
     naming the conflicting paths.

5. `git status --porcelain` again — must be clean. Run any relevant
   smoke check (e.g. `git log --oneline origin/master..HEAD` to verify
   commits look sane).

6. **Push:**
   - If `dry_run` is true:
     `git push --dry-run --force-with-lease {pr.push_remote} HEAD:{pr.push_ref}`
     Report `status: skipped_dry_run` with the dry-run output in `notes`.
   - Otherwise:
     `git push --force-with-lease {pr.push_remote} HEAD:{pr.push_ref}`
     On success report `status: completed`.

## Output

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one-sentence summary",
  "notes": "conflict paths, git output tail, or other useful context"
}
```
