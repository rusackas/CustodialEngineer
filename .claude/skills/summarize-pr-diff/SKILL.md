---
name: summarize-pr-diff
description: Read a PR's diff and emit a 3-bullet summary so the reviewer can decide what to do without reading every file. Stores the summary on the card without transitioning state.
worktree_required: false
---

# Summarize a PR's diff for the reviewer

You're helping the user (a reviewer) triage a PR by giving them a
fast read of what the diff actually does. They'll decide whether to
approve / comment / request changes themselves — you're just the
summarizer.

Read-only: no comments posted, no git mutations, no worktree.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title}`.
- `identity.github_username` — you, the reviewer (unused here;
  mentioned only so you know not to @-mention yourself if quoting).

## Procedure (budget: ~5 turns)

### 1. Get the file list and numbers

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json files,additions,deletions,changedFiles,title,body
```

Note the size. If > ~2000 lines, scope your reading — skim only the
highest-signal files (source, not lockfiles / snapshots).

### 2. Read the diff at a high level

```
gh pr diff {pr.number} --repo {pr.owner}/{pr.name}
```

Skim for:
- **What changed** (the functional delta — not the file list).
- **How it changed** (new abstraction? targeted fix? refactor? new
  feature?).
- **Risk surface** — migrations, concurrency, auth, serialization,
  destructive ops.

### 3. Emit the summary

Three bullets, each one sentence. First bullet is "what"; second is
"how / where"; third is "risk / testing". Don't repeat the PR title.

## Output

```json
{
  "status": "summary",
  "message": "one-line headline (use the PR title's shape but the diff's substance)",
  "bullets": [
    "Renames `foo_bar` → `foo_baz` across 12 call sites; no behavior change.",
    "Touches `superset/db_engine_specs/mysql.py` and a new migration file.",
    "Low risk — migration is reversible; unit test coverage on the MySQL engine looks adequate."
  ],
  "stats": {
    "additions": 0,
    "deletions": 0,
    "changed_files": 0
  }
}
```

- `status` MUST be `"summary"` — the caller's on_first_turn hook
  stashes the summary on the card instead of flipping state.
- Exactly 3 bullets, each ≤ 160 chars. No markdown inside bullets.

## Guardrails

- Read-only.
- Do NOT fabricate summaries for files you didn't look at. If the
  diff is too large to summarize responsibly, say so in the message
  and use 3 bullets that describe what IS known plus a "couldn't
  read $X" caveat.
