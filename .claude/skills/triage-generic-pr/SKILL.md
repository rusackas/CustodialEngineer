---
name: triage-generic-pr
description: Default triager for user-defined queues. Reads PR title/body/labels/comments and proposes one primary action plus safe fallbacks. Doesn't assume the user is the author or a requested reviewer — works for any "watching this PR" queue (stale-PR triage, repo-wide oversight, label-scoped buckets, etc.).
worktree_required: false
---

# Generic PR triage

You are triaging **one** PR for an unspecified observer relationship —
the user might be the author, a requested reviewer, a maintainer
watching the queue, or just someone who set up a filter that surfaced
this PR. **Do not assume you know which.** Your job is to give the
human a one- or two-sentence read on the PR's state plus a primary
action they can click without thinking.

This skill is the fallback when no bespoke triager (`triage-my-pr`,
`triage-review-requested`, `triage-dependabot-pr`) fits the queue. It
errs on the side of safe, low-commitment actions.

You are **not** modifying code. Read-only investigation via `gh` is
fine; no mutations.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref, mergeable,
  merge_state_status, ci_status, has_conflicts, unresolved_threads,
  updated_at, is_draft, maintainer_can_modify, is_cross_repository,
  needs_ci_approval, review_decision, author_login}`.
  - `unresolved_threads` is a list of
    `{id, path, line, is_outdated, first_author, first_body,
    comments_count}` — every still-open review thread.
  - `maintainer_can_modify` + `is_cross_repository` together tell you
    whether we can push commits back to the PR's head branch.
    **Push-allowed** = `not is_cross_repository OR
    maintainer_can_modify`. If push isn't allowed, **don't propose**
    `rebase` / `fix-precommit` / `attempt-fix` as primary — they'll
    bail to `needs_human`. Recommend `nudge-author` instead so the
    PR author does the push.
  - `needs_ci_approval` is set when GitHub flagged a CheckRun as
    `WAITING` or `ACTION_REQUIRED` — the first-time-contributor
    "Approve and run workflow" gate. When true, `approve-ci` is the
    obvious primary; CI can't even start until that click.
  - `review_decision` is GitHub's verdict on whether branch
    protection is satisfied: `null` on repos that don't require
    reviews, `REVIEW_REQUIRED` / `CHANGES_REQUESTED` when an
    external approver is needed, `APPROVED` when greenlit. Combine
    with `author_login` to decide whether self-merge is feasible
    (see ladder #6 below).
- `identity.github_username` — the human running the tool. Use this
  to:
  - phrase the proposal sensibly when relevant (don't @-mention
    them);
  - decide whether unresolved threads are likely "feedback from
    others" vs. their own;
  - **detect self-authored PRs**: when `author_login ==
    identity.github_username` and `review_decision` is
    `REVIEW_REQUIRED` / `CHANGES_REQUESTED`, branch protection
    requires another reviewer — don't propose `approve-merge`,
    GitHub will block it.

## Priority: what to surface

Two things the user wants fast:

1. **State summary** — what is this PR's situation in one beat?
   Examples: "stale and behind master", "green CI, ready to merge",
   "open question from a reviewer pending".
2. **One primary action** — the click that's most likely correct. If
   you're not sure, propose `prompt` and let the human decide.

## Procedure (budget: ~10 turns — keep it light)

### 1. Read the signal fields you already have

`pr` already has `ci_status`, `mergeStateStatus`, `has_conflicts`,
`unresolved_threads`, and `updated_at`. These are usually enough.
Only fetch more when the signals don't yield a clear primary.

### 2. Pull lightweight PR context

One shot:

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json title,body,author,labels,createdAt,updatedAt,additions,deletions,changedFiles,comments
```

- `author.login` / `author.is_bot` — affects the action set:
  - **Bot author** (Dependabot, renovate, copilot-pull-request-reviewer,
    etc.): skip `nudge-author`. Bots have their own action paths
    (rebase / recreate / close).
  - **Human author**: `nudge-author` is on the table when the PR has
    a real blocker the author needs to act on.
- `labels` — useful classification hints (`stale`, `wip`, `blocked`,
  `needs:rebase`).
- `body` — surface "no description" as a concern; otherwise skim
  the first paragraph for stated intent.
- `comments` — top-level discussion. Bot reviews (Copilot, sonar,
  codecov) can surface real findings; humans can have asked
  questions the author hasn't answered.

Skip `gh pr diff` unless you genuinely need it to decide. The
generic triager doesn't do deep code review — that's `assess-on-
worktree`.

### 3. Pick a primary action

Use this priority ladder. First match wins.

0. **CI awaiting approval** (`needs_ci_approval == true`):
   - `approve-ci` primary. One click runs the workflow gates that
     are blocking everything else. Skip the rest of the ladder until
     CI has actually run — most signals below are unreliable while
     the gate is up. Add `prompt` and `skip` as fallbacks.

1. **Conflicts** (`has_conflicts` / `mergeable == CONFLICTING` /
   `merge_state_status == DIRTY`):
   - Bot author → `prompt` primary. The dispatcher's bot-specific
     skills (rebase / recreate) sit behind a button you don't pick
     for non-bot queues.
   - Human author, **push-allowed**: `rebase` primary. We can do
     the work without bothering the author.
   - Human author, **push not allowed** (cross-repo fork without
     maintainer edits): `nudge-author` primary; `await-update` as a
     gentler fallback. Draft a `nudge_comment` that names the
     conflict.

2. **Failing CI**:
   - Bot author → `prompt` primary.
   - Human author, **push-allowed**: pick the primary by failure
     shape, but **always include `attempt-fix`, `plan-fix`, and
     `fix-precommit` in the actions list** so the user can choose
     between an immediate-fix attempt vs. a planning pass vs. a
     formatter sweep without having to retriage. Only the primary
     order changes; all three stay on the menu.
     - If the failure looks like pre-commit / formatter drift
       (`pre-commit`, `lint`, `format` in the failing-check name) →
       `fix-precommit` primary, then `attempt-fix`, then `plan-fix`.
     - Lockfile-shaped failure (`YN0028`, `EBADDEP`,
       `lockfile not consistent`) → `update-lockfile` primary, then
       `attempt-fix`, then `plan-fix` (skip `fix-precommit` here —
       not the right tool).
     - Broad / structural failure (multi-leg matrix red, missing
       deps, import errors, refactor breakage) → `plan-fix` primary,
       then `attempt-fix`, then `fix-precommit`. Plan first when the
       blast radius warrants a strategy before patching.
     - Single-test or single-file failure → `attempt-fix` primary,
       then `plan-fix`, then `fix-precommit`. Direct patching is
       usually right when the surface is small.
   - Human author, **push not allowed**: `nudge-author` primary;
     mention the failing check by name in `nudge_comment` when you
     can tell from `gh pr view --json statusCheckRollup` (only
     fetch this if you want a precise nudge).

3. **Unresolved threads from other authors** (any thread where
   `first_author` is not the PR author and not the current user):
   - Human PR author → `nudge-author` primary. Quote the asker in
     `nudge_comment` ("@alice asked about X on `path:line`, hasn't
     been answered").
   - Bot PR author → `await-update`.

4. **Stale PR** (no other blocker AND `age_days > 30`):
   - Human author → `nudge-author` primary; `close` as a second
     option; `prompt` to defer.
   - Bot author → `close` primary; `prompt` to defer.

5. **Draft PR**:
   - **Active draft** (someone is clearly still working — recent
     commits, recent comments, the author is engaged):
     `await-update` primary. Drafts mean "in progress"; don't
     nudge.
   - **Stale draft** (no commit / comment activity in 60+ days,
     OR the queue surfaced it via an oldest-first sort): the
     kind thing is `close` with a thankful "thanks for the PR,
     feel free to reopen if you want to push it through" body.
     The close-pr skill drafts the comment voice — your job is
     just to put `close` first in `actions`. Don't propose
     `nudge-author` on a stale draft; if the author had
     bandwidth, they'd have moved the PR.

6. **Clean** (CI green / unset, no conflicts, no open threads, not
   draft):
   - **Branch-protection feasibility check first.** If `author_login
     == identity.github_username` AND `review_decision in
     (REVIEW_REQUIRED, CHANGES_REQUESTED)` → branch protection
     requires another reviewer's approval; the bot can't self-approve
     past it. Propose `await-update` primary; once an external review
     lands the card auto-unparks and the next triage will see
     `review_decision == APPROVED` and propose merge cleanly.
   - Otherwise: `approve-merge` primary. The dispatcher re-verifies
     mergeStateStatus before actually merging, so this is safe to
     propose even when signals are slightly stale. Draft an
     `approval_comment` (see Output schema for tone rules).
   - Add `prompt` and `summarize-diff` as fallbacks for when the
     human wants a deeper read first.

If at any point you're confused or the signals contradict, default
to `prompt` primary. The human escape hatch is always correct.

### 4. Draft the proposal

One or two sentences, neutral tone, no "I'll" / "I'd" framing
(the user might be any of several roles). Examples:

- "Stale (~92d), human-authored, failing CI on `frontend-build`.
  Nudge the author or close."
- "Bot PR with merge conflicts — `dependabot-rebase` would be the
  natural action; left as `prompt` since this queue isn't a bot
  queue."
- "Clean — green CI, no open threads, no conflicts. Awaiting your
  call on what to do."
- "Draft PR, last touched 3d ago. Nothing to do; wait for the
  author to mark ready."

### 5. Assemble the output

Always include `prompt` and `skip` in `actions`. The available
universal actions:

- `approve-merge` — approve and merge a clean PR. Surface as
  primary when CI is green / unset, no conflicts, no unresolved
  threads, and the PR is not a draft. Requires `approval_comment`.
- `mark-as-draft` — soft-warning sibling to `nudge-author`:
  converts the PR to draft AND posts a "here's what's needed,
  may close in a future sweep" comment. Surface alongside
  `nudge-author` on non-draft PRs whose author isn't moving;
  it's the friendlier-to-reviewers escalation for "this isn't
  ready, stop pretending it is." Skip on bot-authored PRs.
- `approve-ci` — click the "Approve and run workflow" gate.
  Idempotent — safe even if no runs are pending. Surface only when
  `needs_ci_approval` is true.
- `rebase` — rebase the head branch on master and force-push.
  Surface only when `has_conflicts` AND push-allowed.
- `fix-precommit` — run pre-commit locally and push the auto-fixes.
  Surface only when CI failing AND push-allowed.
- `attempt-fix` — Claude tries to fix the failing CI in a worktree.
  Surface only when CI failing AND push-allowed.
- `update-lockfile` — regenerate and push the package manager's
  lockfile. Surface for lockfile-shaped failures.
- `nudge-author` — post a polite ping; parks the card in
  `awaiting update`. Requires a `nudge_comment`.
- `await-update` — park silently; auto-unparks when the PR moves.
- `close` — close the PR (offer only when you're confident it's
  obsolete or the queue's policy says close-on-stale).
- `summarize-diff` — large/unfamiliar diff; ask for a 3-bullet
  summary.
- `assess-on-worktree` — non-trivial change you'd want to see in
  context; deeper read.
- `add-review-comment` — one specific thing to say. Requires a
  `suggested_comment`. Use sparingly; this skill isn't a code
  reviewer.
- `prompt` — escape hatch (always include).
- `skip` — move on without acting (always include).

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "proposal": "One or two sentences describing the PR's state and recommending an action.",
  "classification": "ready | blocked | stale | draft | unclear",
  "assessment": [
    "CI: failing (frontend-build red)",
    "Last update 92d ago",
    "Author: @alice (human)",
    "Linked issue: #1234"
  ],
  "blockers": [
    "@bob's question on `models.py:140` is unanswered"
  ],
  "concerns": [
    "PR description is one line"
  ],
  "suggested_comment": "Optional — pre-filled body for add-review-comment. First person, concrete (quote a file:line or specific question). Empty string if add-review-comment is not in actions.",
  "approval_comment": "Optional — pre-filled review body for approve-merge. Reference the concrete merge-safety signal (CI green, no open threads, clean merge state). Author-aware: thank a human contributor briefly, stay neutral/mechanical for bot authors. Empty string when approve-merge is not in actions.",
  "nudge_comment": "Optional — pre-filled body for nudge-author. Polite maintainer voice; @-mention the PR author to ping; enumerate the concrete blockers (named CI checks, asker @ + path:line excerpt). Close with a low-pressure next step. Empty string when nudge-author is not in actions.",
  "actions": ["approve-merge", "summarize-diff", "prompt", "skip"],
  "notes": {
    "classification": "stale",
    "age_days": 92,
    "author_is_bot": false
  }
}
```

- `actions` MUST be primary-first and contain at least one entry.
- `prompt` MUST always be in `actions`. Place it second-to-last
  (right before `skip`).
- `skip` MUST always be in `actions`, last.
- `suggested_comment` MUST be non-empty whenever `actions` contains
  `add-review-comment`. Ground it in something concrete (file:line,
  specific question). First person; no @-mentions of the user.
- `approval_comment` MUST be non-empty whenever `actions` contains
  `approve-merge`. Reference the concrete merge-safety signal
  (CI status, merge state, lack of open threads) instead of a
  generic "LGTM". Tailor to the PR author:
  - Human author → brief first-person thanks + the merge-safety
    verdict (e.g., "Thanks @alice — CI green, no open threads,
    clean merge state. LGTM.").
  - Bot author (Dependabot, renovate, etc.) → neutral/mechanical,
    skip the thanks (e.g., "Dependabot version bump — CI green,
    mergeStateStatus CLEAN.").
  No @-mentions of `identity.github_username`.
- `nudge_comment` MUST be non-empty whenever `actions` contains
  `nudge-author`. Open with `@{author.login}` so the author gets
  pinged. Enumerate the actual blockers — name failing checks if you
  can tell, quote unresolved threads with `@asker` + `path:line` +
  short excerpt. Polite maintainer voice; close with a low-pressure
  next step ("let me know once those are addressed").
- Never @-mention `identity.github_username`.
- `concerns` and `blockers` are optional — omit empty arrays rather
  than emitting them.

## Guardrails

- Read-only. No mutations, no posted comments.
- Budget ~10 turns. If you find yourself deep-reading the diff or
  many files, stop and recommend `assess-on-worktree` instead.
- If the signal fields are empty (GH query glitch), fall back to
  `gh pr view {number} --json mergeable,mergeStateStatus,
  statusCheckRollup` and re-derive.
- When unsure, lean on `prompt` as primary. Forcing a decision the
  user would override is worse than asking.
