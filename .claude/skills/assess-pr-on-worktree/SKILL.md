---
name: assess-pr-on-worktree
description: Deep PR review assessment with the branch checked out in a worktree. Reads the diff in context of the current codebase to spot missing tests, anti-patterns, duplication, and risky changes. Read-only; emits a structured report.
worktree_required: true
---

# Deep PR assessment on a checked-out worktree

You're helping the reviewer take a closer look at one PR. The branch
is already checked out in your working directory, so you can read
files in their post-change state alongside the rest of the codebase.
Your job is to produce a richer assessment than the lightweight
triage skill — one that actually looks at the code.

Read-only: no commits, no pushes, no comments. The reviewer will
decide what to act on.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref}`.
- `triage` — the prior triage output (`proposal`, `notes`,
  including `assessment`, `blockers`, `concerns`). Build on it;
  don't repeat it.
- `identity.github_username` — the reviewer. Never @-mention; speak
  first-person ("I…").
- `worktree_path` — you're already `cwd` here; the PR branch is
  checked out. `git status` should be clean.

## Procedure (budget: ~20 turns)

### 1. Orient yourself

```
git status
git log --oneline -5
git diff --stat origin/master...HEAD
```

Confirm you're on the PR branch (`pr.head_ref`). If not, bail out:
something's wrong with the worktree setup.

### 2. Pull the PR's context in one shot

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json body,labels,additions,deletions,changedFiles,files,\
closingIssuesReferences,comments,reviews
```

- `body` — PR description; gives intent.
- `closingIssuesReferences` — linked issues. If present, glance at
  the first one (`gh issue view <num>`) to understand the ask.
- `comments` — top-level issue comments; includes bot reviews
  (Copilot, sonar, bito, codecov, etc.) and human discussion.
- `reviews` — submitted reviews + their summary bodies.

### 3. Read the diff with the codebase in hand

```
git diff origin/master...HEAD
```

For each non-trivial changed file, open the file and look at
neighboring code. You're looking for:

- **Duplication** — did they add a helper next to an existing one
  that does the same thing? Grep for similar function names or
  patterns in the touched area.
- **Anti-patterns** — stale idioms, raw SQL where the ORM is used
  elsewhere, error handling that swallows exceptions, feature flags
  that don't match the repo's existing flag style.
- **Missing tests** — look at the file's neighbors: is there a
  `test_*.py` / `*.test.ts` / `*.spec.ts` for this module? Did the
  PR touch logic without touching the test file? Flag it.
- **Risky surfaces** — migrations, auth, session handling, crypto,
  eval/exec, subprocess/shell, serialization of user input, SQL
  string building.
- **Scope** — does the diff match the PR title? Flag unrelated
  changes (drive-by refactors, formatting-only file touches).

### 4. Read the conversation

From step 2's `comments` and `reviews`:

- **Human open questions**: quote the question (short, with
  author handle) — these are things the PR author hasn't answered.
- **Bot findings worth surfacing**: Copilot / bito / sonarcloud
  often catch legit issues. Dedupe with your own scan; if a bot
  already flagged something you'd mention, cite it instead of
  repeating. Skip boilerplate bot chatter (codecov % deltas unless
  significant, CLA checks, etc.).
- **Reviewer signals**: if another reviewer already requested
  changes or approved, that matters.

### 5. Produce the report

Keep each section tight. Bullets, not paragraphs. Group findings
by severity.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "status": "assessment",
  "proposal": "One or two first-person sentences — my overall take after reading the code.",
  "headline": "One-line verdict (e.g., 'Solid approach, two missing tests and one duplication concern').",
  "findings": {
    "blockers": [
      "Specific issue the PR author must address before merge; each one quotes path:line"
    ],
    "concerns": [
      "Things reviewer should flag but don't strictly block merge"
    ],
    "tests_needed": [
      "Logic at `path:line` has no accompanying test — suggest what to cover"
    ],
    "anti_patterns": [
      "`path:line` does X the hard way; existing helper `foo()` in `other.py:30` handles this"
    ],
    "open_questions": [
      "@alice asked about the caching strategy on `models.py:140`, still unanswered",
      "Copilot flagged potential null-deref at `utils.ts:22`, author hasn't addressed"
    ],
    "scope_creep": [
      "PR title is about X but touches unrelated Y"
    ],
    "risky_surfaces": [
      "Adds raw SQL in `queries.py:55` — rest of the module uses SQLAlchemy"
    ]
  },
  "suggested_actions": ["add-review-comment", "await-update", "approve-review"],
  "suggested_comment": "Optional draft comment the reviewer can edit in the modal. First person, no @-mentions of the reviewer. Empty string if no comment is called for."
}
```

- `status` MUST be `"assessment"` — the caller stashes the output
  on the card without flipping its state.
- Omit empty buckets (don't render empty arrays as noise).
- `findings.blockers` is the highest-signal section. If empty,
  say so in the headline.
- `suggested_actions` orders the reviewer's likely next clicks,
  primary first. Valid ids: `approve-merge`, `approve-review`,
  `add-review-comment`, `request-changes-review`, `await-update`,
  `dismiss-review-request`, `summarize-diff`, `prompt`, `skip`.

## Guardrails

- Read-only. No `git commit`, no `gh pr comment`, no mutations.
- Don't run the test suite — budget-expensive and out of scope.
  Flag missing tests; let the reviewer decide whether to run them.
- If the worktree isn't on `pr.head_ref` (step 1 mismatch), bail
  with `status: "error"` and the mismatch details.
- Never @-mention `identity.github_username`.
- If the diff is huge (> ~3000 lines), scope your reading: pick
  the 5–8 highest-signal files and say in `headline` which files
  you prioritized and why.
- Cite files as `path:line` in backticks so the UI renders them
  as code. Short excerpts are fine; don't paste whole functions.
