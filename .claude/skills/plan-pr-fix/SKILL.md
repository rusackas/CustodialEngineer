---
name: plan-pr-fix
description: Investigate a PR that a prior attempt-fix session flagged as needs_human, and produce a concrete, reviewable fix plan. Does NOT execute — the human reviews/edits the plan, then approves, and you execute in a later turn using the same session.
worktree_required: true
max_turns: 60
---

# Plan a fix for a PR (then execute on approval)

You are partnered with `attempt-fix-pr`. That skill tries a small targeted
diff and bails to `needs_human` when the fix is out of scope for a
straight bot patch — cross-PR coordination, migration work, ambiguous
trade-offs. You pick up from there: investigate deeply, then **propose a
plan the human can approve, edit, or reject**.

You are `cd`'d into a git worktree at `worktree_path`, checked out on the
PR's branch (`pr.head_ref`). You have the PR and triage context, plus
the prior attempt-fix session's `needs_human` message (if any).

## This runs in two phases

**Phase 1 — Plan (your first turn).** Investigate the repo, read CI
logs, trace the breakage, and emit a plan as JSON (schema below). DO
NOT commit, push, force-push, post comments, or otherwise change remote
state in this phase. Local exploration only.

**Phase 2 — Execute (next turn, after the human approves).** The
human's follow-up message will start with `APPROVED PLAN:` followed by
the (possibly edited) plan. When you see that, treat the attached plan
as your ground truth and execute it exactly like `attempt-fix-pr` would
— edit the files, run the cheap local check, commit, push (or dry-run
if `dry_run == true`). Emit the standard attempt-fix-pr output schema.

If the human replies with anything else in phase 2 (a question, a
revision request not in the APPROVED PLAN format), answer briefly and
stay in the idle/planning mindset — do not execute.

### Phase-2-only resume mode

If the runtime context includes a non-empty `approved_plan` field
on its first turn (i.e., the bot is reviving an approved plan into
a fresh session because the original phase-1 session had already
closed when the human clicked Approve), **skip phase 1 entirely**.
Treat that field's contents as if it were the body of the human's
phase-2 follow-up message — execute it directly. Don't re-investigate,
don't emit another plan, don't ask for confirmation. The plan was
already reviewed; your job is just execution.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref}`
- `triage` — prior triage output, possibly with `notes.log_excerpt`
- `dry_run` — if true, in phase 2 do NOT push
- `worktree_path` — absolute path of the checked-out worktree
- `identity` — `{github_username}`: the human who will approve your plan
- prior `last_result` (if provided via `triage.notes` or the initial
  prompt) — the attempt-fix `needs_human` message explaining why it bailed
- `approved_plan` (optional) — present only when the dispatcher is
  reviving an approved plan because the original phase-1 session was
  already closed. When set, this is your phase-2 input directly (see
  "Phase-2-only resume mode" above).

## Phase 1 procedure — planning

### 1. Read what attempt-fix already figured out

Skim `triage.proposal` and the prior `last_result.message` (attempt-fix's
`needs_human` note). Don't re-investigate what's already diagnosed —
build on it. You're not a second opinion, you're the next step.

### 2. Look for cross-PR dependencies

The #1 reason attempt-fix bails is coordinated bumps — this PR can't
land without another PR landing at the same time (peer dep constraints,
monorepo package graph). Check:

```
gh pr list --repo {pr.owner}/{pr.name} --search "dependabot" --state open --json number,title,headRefName --limit 50
```

Identify PRs that must land together with this one, and name them
explicitly in your plan's `coordination` field.

### 3. Understand the migration surface

If the bump crosses majors, read the library's breaking-change list
(CHANGELOG / migration guide — use WebFetch when useful). Enumerate the
concrete changes the repo needs (removed APIs, renamed rules, config
shape changes). Each one becomes a step.

### 4. Find the touch points in this repo

For each breaking change, grep for call sites:

```
rg --fixed-strings "<removed-symbol>" -l
```

Name specific files and line numbers in the plan steps. Avoid vague
"update all usages" — if you can't enumerate the files, the plan isn't
ready yet.

### 5. Cheap local verification plan

For each step, note what ONE fast command (< ~30s) would verify it —
`tsc --noEmit`, a single jest file, lint on a single file. The human
needs to know how you'd check your own work.

### 6. Risks & decision points

Call out: anything a human might want to decide differently (e.g.,
"skip this rule vs rewrite usages"), anything that could snowball
(e.g., "touching this config ripples into eight packages"), anything
time-sensitive (release freeze, maintainer coordination).

### 7. Output the plan

Emit a single JSON object fenced as ```json ... ```:

```json
{
  "status": "plan",
  "summary": "one or two sentences: the shape of the fix, any coordination",
  "steps": [
    {
      "title": "short imperative title",
      "rationale": "why this step is needed",
      "files": ["path/to/file.ts"],
      "commands": ["git add ...", "npx jest path/to/file.test.ts --runInBand"],
      "verify": "tsc --noEmit on the touched file"
    }
  ],
  "coordination": [
    {
      "pr": 39046,
      "why": "this PR's eslint-plugin bump must land alongside ours (peer dep pins to same major)"
    }
  ],
  "risks": [
    "migrating .eslintrc.js v7 → v8 removes rules the team may want to re-enable manually"
  ],
  "dry_run_note": "in dry_run we'll skip the force-push"
}
```

- `status` MUST be `"plan"` in phase 1. Do not use `completed`,
  `needs_human`, etc. — those statuses are for the execute phase.
- `coordination` and `risks` can be empty arrays if genuinely none.
- Keep the plan tight — aim for 3-6 steps, not 20.

## Phase 2 procedure — execution (only after APPROVED PLAN)

When the user's follow-up message opens with `APPROVED PLAN:`, parse the
JSON plan from the body. That plan may differ from what you proposed —
the human can edit it. The edited plan is authoritative.

Then execute exactly like `attempt-fix-pr`:
- For each step, touch only the `files` listed.
- Run each step's `verify` command; if it fails, stop and bail to
  `needs_human` with what broke.
- Before committing, run `pre-commit run --files <all edited files>`
  so formatter auto-fixes land in the same commit. Re-stage anything
  rewritten; if a non-auto-fixable hook errors on your edits, fix it
  in place. Skip gracefully if pre-commit isn't installed in the worktree.
- Commit with a message referencing the library and the migration
  summary.
- `dry_run == true`: `git push --dry-run --force-with-lease origin HEAD:{pr.head_ref}`
- Otherwise: `git push --force-with-lease origin HEAD:{pr.head_ref}`

Emit the standard attempt-fix-pr output:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "one sentence",
  "files_changed": ["..."],
  "commit_sha": "abc1234",
  "pushed": true
}
```

## Guardrails

- Phase 1 is **read-only on the repo and remote**. No commits, no
  pushes, no `gh pr comment`, no `gh pr close`. Local greps and reads
  only. Calling `gh` for read-only queries (`gh pr view`, `gh run view`)
  is fine.
- Phase 2 follows attempt-fix's rules: touch at most ~50 files; never
  disable/skip tests; never push without `--force-with-lease`.
- If `identity.github_username` is set, treat the human with that
  handle as the approver — do not @-mention them in commit messages or
  comments, and don't refer to them in third person.
- If the plan would touch more than ~50 files or span more than ~12
  steps, that's probably still out of bot scope — return phase-1
  output with `status: "plan"` but mark `risks` clearly, so the human
  can decide whether to approve or take it manually.
