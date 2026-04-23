---
name: triage-dependabot-pr
description: Triage a single open Dependabot PR. Branches on CI status — passing PRs get an approve-merge proposal (after verifying mergeStateStatus is clean); failing PRs get a root-cause analysis from the failing-check logs.
worktree_required: false
---

# Triage a Dependabot PR

You are triaging **one** open Dependabot PR. It might be passing CI
(candidate for approve-and-merge) or failing CI (needs fix / rebase /
close). Your job is to look at real data — not just metadata — and
produce a concrete, specific proposal.

## Inputs

The caller provides `{ pr: { owner, name, number, url, title, head_ref,
mergeable, updated_at, is_draft, ci_status } }`. `ci_status` is one of
`passing`, `failing`, `pending` — already computed from the check
rollup.

## Procedure

### 1. Always fetch deeper PR metadata first

```
gh pr view <number> --repo <owner>/<name> \
  --json number,title,url,mergeable,mergeStateStatus,statusCheckRollup,reviewDecision,reviews,comments,updatedAt,headRefName,isDraft,body
```

Note `mergeStateStatus`:
- `CLEAN` — ready to merge (all required checks passed, not behind base).
- `BLOCKED` — required reviews/checks missing.
- `BEHIND` — base branch has moved; needs a rebase.
- `DIRTY` — merge conflicts (should also show `mergeable: CONFLICTING`).
- `UNSTABLE` — non-required checks failing but mergeable.
- `UNKNOWN` — GitHub hasn't computed it yet; treat as "not confirmed".

`mergeable: MERGEABLE` alone is NOT enough to recommend approve-merge.
You need `mergeStateStatus: CLEAN` too.

### 1b. Scan review signals (critical — always do this)

Even a CI-green, CLEAN PR can have **human or bot reviewer concerns**
on the thread — and those should block approve-merge. Read:

- `reviewDecision` — if `CHANGES_REQUESTED`, always propose `prompt`
  (not approve-merge), and summarize what was requested.
- `reviews[].state` — any `CHANGES_REQUESTED` that hasn't been
  resolved by a later `APPROVED` from the same reviewer counts.
- `comments` — scan the last ~10 thread comments for:
  - Maintainer comments like "don't merge", "hold", "blocked on X",
    "needs follow-up", "revert this if merged".
  - Bot reviewer comments (dosu / dosu-bot, coderabbitai[bot],
    sonar, etc.) flagging concerns, unresolved TODOs, regressions,
    or questions the author hasn't responded to.
  - Discussion about breaking changes or intentional pins that this
    bump conflicts with.

If ANY of the above looks blocking, propose `prompt` (so a human
reviews) and reference the concern in the proposal. Don't propose
approve-merge.

Benign bot output (pure summaries, "LGTM", link previews, changelog
diffs) is fine to ignore.

### 2. Branch on `ci_status`

#### If `ci_status == "passing"` …

- `mergeStateStatus == CLEAN` and not draft → propose
  **`approve-merge`** (primary). Optionally include `close` if the
  bump looks clearly unwanted (e.g., major version of a library the
  repo has pinned on purpose — skim the diff / title).
- `mergeStateStatus == BEHIND` → propose `rebase` or
  `dependabot-rebase` (primary). Don't recommend approve-merge; the
  branch has to catch up first.
- `mergeStateStatus == BLOCKED` → propose `prompt` so a human can
  decide (usually means required reviewers or branch protection).
  Don't approve-merge.
- `mergeStateStatus == UNSTABLE` → note which non-required checks
  are red in the excerpt. Usually safe to `approve-merge`, but say so
  explicitly in the proposal.
- Draft / WIP → propose `prompt` only.

Classification: `approve-merge`, `rebase-needed`, `blocked`,
`unstable`, `draft`.

#### If `ci_status == "pending"` …

Checks are still in flight. Propose `prompt` (secondary: `retrigger-ci`,
`close`). Classification: `pending`. Don't read logs yet — they're
incomplete.

#### If `ci_status == "failing"` …

Follow the failing-PR flow below.

### 3. Failing-PR flow (ci_status == "failing")

Handle these cheap early-exits before reading logs:

- **`mergeable == "CONFLICTING"` or `mergeStateStatus == "DIRTY"`** →
  propose `dependabot-rebase` first (or `dependabot-recreate` if the
  bot has already refused a rebase — watch for "looks like this
  branch was modified" in dependabot's comments). Manual `rebase` is
  the fallback. If the PR has been conflicting for >5d, also include
  `close`. Do NOT read logs — the rebase has to happen first.
  **Also include `attempt-fix`** when reviews/comments flag a
  specific code or test breakage that won't be resolved by a rebase
  (e.g., "this bump breaks X.test", "same failure as #N"). Order:
  `[dependabot-rebase|dependabot-recreate, attempt-fix, close,
  prompt]`. Classification: `conflict`.
- **Stale** (updated >7d ago, still red) → propose
  `dependabot-recreate`; include `close` if the bump looks
  superseded. Classification: `stale`.
- **Draft / WIP** → propose `prompt` only. Classification: `draft`.

**Precommit-only failure** — if the failing checks are *entirely*
pre-commit jobs (e.g., `pre-commit (current)`, `pre-commit (next)`,
`pre-commit (previous)`) and every other check is green, classify as
`precommit-drift` and propose `fix-precommit` (primary), with
`attempt-fix` as a secondary fallback in case the drift turns out to
be more than formatter output. You can skim the log to confirm it's a
formatter/linter hook (black, ruff, prettier, trailing-whitespace,
eslint, etc.) — if the "failure" is a genuine code error surfaced via
pre-commit's mypy/pyright hooks, downgrade to `attempt-fix`.
Classification: `precommit-drift`.

Otherwise read the ACTUAL failing logs. Pick up to 2 of the most
informative failing checks (prefer test/build jobs over lint):

```
gh run view <run-id> --repo <owner>/<name> --log-failed | tail -200
```

Classify:

- **Lockfile drift** — `lockfile needs update`, `YN0028`,
  `EBADDEP`, `npm ERR! Missing:`, `poetry.lock is not consistent`,
  `pnpm-lock.yaml is not up to date`, `Cargo.lock would need
  updating`. → `update-lockfile` (primary), `dependabot-rebase`.
- **Infra / flake** — `Error: The operation was canceled`, runner
  shutdown, 502/503 from package registries, DNS failures, timeouts
  before any user code ran. → `retrigger-ci`.
- **Real code breakage** — type errors, failing assertions, import
  errors for a removed API, deprecation errors, snapshot diffs.
  → `attempt-fix` (primary: spin up a worktree + try a minimal
  adapter fix), then `prompt` (human steers), include `close` if the
  bump looks unwanted.
- **Unclear / mixed signals** → `prompt` and note the ambiguity.

Always capture a **3–10 line log excerpt** showing the actual failure,
not boilerplate.

## Output

Return a **single** fenced JSON object:

```json
{
  "proposal": "One sentence. Specific and concrete. If recommending approve-merge, say WHY (e.g., 'CI green, mergeStateStatus CLEAN, no required reviewers pending').",
  "actions": ["approve-merge", "close"],
  "notes": {
    "classification": "approve-merge | rebase-needed | blocked | unstable | draft | pending | conflict | stale | precommit-drift | lockfile-drift | flake | real-breakage | unknown",
    "merge_state_status": "CLEAN | BLOCKED | BEHIND | DIRTY | UNSTABLE | UNKNOWN",
    "failing_check": "optional — only for log-reading classifications",
    "run_url": "optional",
    "log_excerpt": "required for lockfile-drift/flake/real-breakage/unknown",
    "close_comment": "required if `close` is in actions — the draft comment a human will edit before posting on close"
  }
}
```

### Rules for `close_comment`

Populate this whenever `close` appears in `actions`. Write it as a
short, respectful comment from a maintainer explaining *why* we're
closing — the human will review/edit before anything gets posted.

- 1–3 sentences, plain prose (no "TL;DR", no emoji).
- Name the concrete reason: superseded by #N, bump intentionally
  declined (pinned on purpose), supersedes itself (author already
  pushed the fix direct), or unresolvable breakage (reference the
  failure briefly).
- If you don't know the reason with confidence, say so: "Closing
  because …; happy to reopen if that's wrong." Never invent a PR
  number or a reason.
- Thank Dependabot when the bump itself was fine but we just don't
  want it right now ("Thanks, but pinning at 1.4.0 on purpose …").

### Rules for `proposal`

- Always specific. Name the mechanism.
  - Good: "CI green, mergeStateStatus CLEAN, no required reviewers — approve-merge."
  - Good: "yarn.lock missing entry for @foo/bar@3.2.0 — regenerate the lockfile."
  - Bad: "CI is passing, ready to merge." (too vague — include the state check)
  - Bad: "CI is failing, probably lockfile or flake." (no root cause)

### Rules for `actions`

Valid ids only: `approve-merge`, `rebase`, `dependabot-rebase`,
`dependabot-recreate`, `update-lockfile`, `attempt-fix`,
`fix-precommit`, `retrigger-ci`, `close`, `prompt`. Order from most to
least recommended. Never empty; never more than 5.

### Rules for `notes`

Always include `classification` and `merge_state_status`. Include
`log_excerpt` whenever you actually read a log.

## Budget

Under ~8 turns. For passing PRs, one `gh pr view` call usually
suffices — no log reading needed. For failing PRs, at most 2 log
views, each bounded (`| tail -200`).
