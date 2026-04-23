---
name: update-pr-lockfile
description: Regenerate the Superset frontend lockfile inside a worktree by deleting it and running a full `npm install`, commit the result, and force-push.
worktree_required: true
---

# Update a PR's lockfile

You are already `cd`'d into a worktree on the PR's branch. This skill
handles Superset's JS monorepo specifically — the maintainer flow is a
full lockfile regeneration, not a targeted one, because `npm install`
walks the workspaces config in `superset-frontend/package.json` and
reconciles the whole tree in a single pass. Piecemeal regen tends to
leave peer-dep drift.

**Inputs**: `pr.{owner,name,number,head_ref}`, `dry_run`.

## Procedure

1. Confirm this is a JS-ecosystem PR (the common case for Dependabot
   here). Peek at the diff:
   ```
   gh pr diff {pr.number} --repo {pr.owner}/{pr.name} --name-only
   ```
   - If the changed files include `superset-frontend/package.json`
     and/or `superset-frontend/package-lock.json`, proceed.
   - If not (e.g. only Python / docs / poetry.lock changed), report
     `status: needs_human` — this skill does not cover those cases.

2. Regenerate the lockfile from scratch:
   ```
   cd superset-frontend
   rm package-lock.json
   npm install
   ```
   `npm install` reads the workspaces config and regenerates the full
   monorepo lockfile. Do NOT pass `--package-lock-only` — a real
   install is what catches peer-dep / workspace resolution issues.

3. Sanity-check the diff. Back at the repo root:
   ```
   git status --porcelain
   ```
   The only expected dirty file is `superset-frontend/package-lock.json`.
   If anything else changed (e.g. `package.json` got rewritten, a
   `node_modules` symlink crept in), report `status: needs_human` and
   list the stray paths.

4. Commit:
   ```
   git add superset-frontend/package-lock.json
   git commit -m "chore(deps): regenerate package-lock.json"
   ```

5. Push:
   - `dry_run`: `git push --dry-run --force-with-lease origin HEAD:{pr.head_ref}`
   - Otherwise: `git push --force-with-lease origin HEAD:{pr.head_ref}`

## Output

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one-sentence summary",
  "notes": "anything noteworthy — e.g. how long npm install took, or why we bailed"
}
```

## Guardrails

- NEVER edit `package.json` yourself; the regen should be purely
  mechanical. If `npm install` rewrites `package.json`, bail.
- NEVER commit `node_modules` or any other stray artifact.
- `--force-with-lease` (not `--force`) — so a concurrent push from
  another contributor doesn't get stomped.
