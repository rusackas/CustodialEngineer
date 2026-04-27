---
name: triage-review-requested
description: Pre-review assessment of one PR where the user has been asked to review. Reads the description, linked issues, diff, review threads, and all issue-level comments (including bots). Surfaces open questions, missing tests, anti-patterns, and risky areas — then proposes review actions.
worktree_required: false
---

# Pre-review triage of a PR I was asked to review

You are helping the user (a PR reviewer) decide what to do about a
single PR where they were requested as a reviewer. Your job is to
give a short assessment they can skim and act on.

This triage is a deeper read than the basic signal-check: you
pull the PR body, linked issues, full diff, and the entire
conversation (review threads + issue comments + bot reviews) so
your proposal reflects the actual code and the actual discussion,
not just CI status.

You are **not** modifying code. Read-only investigation via `gh`
and `git` is fine; no mutations.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref, mergeable,
  merge_state_status, ci_status, has_conflicts, unresolved_threads,
  updated_at, is_draft}`. `unresolved_threads` is a list of
  `{id, path, line, is_outdated, first_author, first_body,
  comments_count}` — ALL unresolved review threads, including ones
  started by others.
- `identity.github_username` — **the human reading this triage IS
  this account**. Treat them as "you" in your proposal text, never
  in third person. Specifically:
  - Don't @-mention them in any output field.
  - Don't refer to them by handle in prose. ❌ Bad: "rusackas
    asked about caching"; "@rusackas already approved the spec."
    ✅ Good: "you asked about caching"; "you already approved the
    spec"; or omit attribution when context makes it obvious.
  - When quoting one of their past comments, attribute as "you"
    (e.g., 'you commented "Looks good"') or quote without a handle.
  - Other commenters (the PR author, other reviewers) are fine to
    @-mention or third-person normally.

  Functional uses: filter the unresolved-threads signal around
  this — threads where the FIRST author is someone other than the
  reader, still unresolved, are the interesting ones (feedback the
  PR author hasn't addressed).

## Priority: what to surface

Two things the user wants fast:

1. **Classification** (pre-bucketed by the fetcher; confirm / override
   only if signals disagree):
   - `mergeable` — CI green-ish, no conflicts, no obvious blockers.
   - `blocked` — conflicts, failing CI, or an open blocker the
     author must address first.

2. **Reviewer call-to-action** — what you think they should click:
   - `approve-merge` — clean, safe to approve and merge.
   - `approve-review` — clean but CI isn't done / not ready to
     merge yet; approve the code without merging.
   - `add-review-comment` — one specific thing to say (question,
     nit, small suggestion). Put the body in `suggested_comment`.
   - `request-changes-review` — **only** if something looks
     actively dangerous (security hole, breaking change without
     migration, destructive DB op). Otherwise prefer the gentler
     `add-review-comment`. If you use this, put the draft review
     body in `suggested_comment`.
   - `assess-on-worktree` — you want a deeper look with the PR
     branch checked out (code in context, DRY/anti-pattern scan,
     neighbor-file comparison). Recommend when the PR is
     non-trivial and you're not confident from a remote skim.
   - `summarize-diff` — the diff is large/unfamiliar and a 3-bullet
     summary would help the human decide.
   - `nudge-author` — CI is failing and/or feedback from other
     reviewers is unaddressed; post a polite comment prompting the
     PR author to act. Parks the card in `awaiting update` until
     the PR moves. Use for human-authored PRs where a nudge is
     likely to help; skip for bot PRs (Dependabot has its own
     action set).
   - `await-update` — author has open threads from others, or CI
     isn't settled, or the branch is behind — park silently until
     the situation moves. Prefer `nudge-author` when a visible ping
     is likely to unstick things; `await-update` when you'd rather
     wait quietly.
   - `prompt` — ambiguous; kick it to the human.
   - `dismiss-review-request` — you genuinely aren't the right
     reviewer. Rare.
   - `close` — close the PR with a thankful comment. Surface as
     primary when the work is **obsolete** (already done in
     master / superseded by another PR / repo direction has
     shifted) or when the PR was opened against a stale plan
     that no longer applies. The close-pr action skill drafts
     a thankful "thanks for the PR — feel free to reopen if you
     want to push it through" body for human-authored PRs; you
     should also draft a `close_comment` here so the modal is
     pre-filled with concrete reasoning ("Closing — Column.tsx
     already exists on master, this migration landed via #N").
     Pre-population on close makes the modal snappy and gives
     the user something to edit rather than a blank slate.
   - `skip` — move on without any action.

Order `actions` primary-first. The button shown prominently is the
first one.

## Procedure (budget: ~15 turns)

### 1. Read the signal fields you already have

`pr` already has `ci_status`, `mergeStateStatus`, `has_conflicts`,
and `unresolved_threads`. Use them — no need to re-fetch the basics.

### 2. Pull PR body, linked issues, and conversation

One shot:

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json title,body,author,additions,deletions,changedFiles,labels,files,\
closingIssuesReferences,comments,reviews
```

- `author.login` / `author.is_bot` — tells you whether the PR was
  opened by a human or a bot. You need this to draft
  `approval_comment` in the right tone (thanks a human, stays
  mechanical for a bot).
- `body` gives you the PR's stated intent.
- `closingIssuesReferences` — if a linked issue exists and the PR
  body is thin, `gh issue view <num> --repo ...` for one-line context.
- `comments` — top-level issue comments. This includes **bot
  reviews** (Copilot, bito, sonarcloud, codecov, etc.) and human
  discussion. Scan bot bodies for substantive findings; skip
  boilerplate (CLA, coverage-delta-only).
- `reviews` — any submitted reviews and their bodies.

### 3. Scan unresolved review threads

Filter `unresolved_threads` to threads whose `first_author` is NOT
`identity.github_username`. Each is a question / request the PR
author hasn't addressed — a pending merge blocker. Summarize in
`blockers` with the author's handle + `path:line` + a short excerpt.

If `is_outdated` is true, note that — the code has moved and the
thread may be stale; less blocker-y, still surface.

### 4. Skim the diff for substance

```
gh pr diff {pr.number} --repo {pr.owner}/{pr.name}
```

Budget ~200 lines of reading. Look for:

- **Missing tests** — non-trivial logic change with no test file
  touched (heuristic: touched `.py` but no `_test.py`/`test_*.py`;
  touched `.ts/.tsx` but no `.test.ts`/`.spec.ts`).
- **Anti-patterns** — raw SQL in an ORM codebase, swallowed
  exceptions, global mutable state, hand-rolled helpers that
  duplicate a standard util.
- **Risky surfaces** — migrations, auth / session / security files,
  subprocess / shell, deserialization of user input, crypto.
- **Scope creep** — title says one thing, diff touches unrelated
  areas (imports-only changes across dozens of files, drive-by
  formatting).
- **Size** — >1000 lines changed or >30 files touched → concern
  (not a blocker).
- **Missing PR description** — empty or one-line body on a
  non-trivial PR.

If the diff is clearly too big to skim usefully, prefer
`summarize-diff` or `assess-on-worktree` as your primary action
rather than guessing.

### 5. Parse the bot + human signal

From step 2:

- Extract human **open questions** that the PR author hasn't
  answered — quote the asker's handle + a short excerpt.
- Extract bot **substantive findings** (not noise): incorrect
  logic flags, security warnings, missing-test reminders.
  Dedupe with your own diff-scan; if a bot already flagged X,
  surface that instead of re-stating.
- Ignore: CLA check comments, coverage-delta chatter (unless
  significant), pure formatting/emoji chatter.

### 6. Decide classification (confirm or override)

The fetcher bucketed based on `mergeStateStatus`, `ci_status`,
`has_conflicts`. Override only if your deeper read found something
strong (e.g. fetcher said "mergeable" but a bot flagged a
security issue you've confirmed in the diff).

### 7. Pick primary action

Default heuristic:

- Real code concerns you're confident about → `add-review-comment`
  with a drafted `suggested_comment`.
- Unsure about the code, want to look in context →
  `assess-on-worktree` as primary.
- Looks clean, CI green, no open threads → `approve-merge`.
- Looks clean but CI pending / behind → `approve-review`.
- Open threads from others OR CI not settled → `await-update`.
- Dangerous (security, destructive, breaking-without-migration) →
  `request-changes-review` with a drafted body.
- **Obsolete / superseded** (the work has already landed on master,
  another PR has been merged that does the same thing, or the
  repo direction has changed and this PR no longer fits) →
  `close` primary with a drafted `close_comment`. Be concrete
  about *why* — name the file paths you confirmed exist, the PR
  number that superseded this one, etc. Don't propose `close` for
  PRs that just look stale or low-momentum; that's
  `nudge-author` / `await-update` territory. `close` is for
  "this work is no longer relevant" specifically.

### 8. Assemble the output

Write the proposal in first person ("I'd …") and keep it under
two sentences. Examples:

- "Clean — CI green, no open threads, diff is small and
  mechanical. Safe to approve-merge."
- "Blocked: @alice's question on `schema.py:40` hasn't been
  answered, and Copilot flagged the same month-math bug in
  `utils.ts:22`. I'd await-update."
- "Non-trivial diff in an area I don't know well — I'd
  assess-on-worktree before reviewing."

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "proposal": "One or two first-person sentences — what I'd do and why.",
  "classification": "mergeable | blocked",
  "assessment": [
    "CI: passing",
    "3 unresolved threads (2 from @alice, 1 outdated)",
    "Branch behind main by 4 commits",
    "Linked issue #1234"
  ],
  "blockers": [
    "@alice asked about the caching strategy on `superset/models.py:140`, no reply",
    "Copilot flagged incorrect month-math in `utils.ts:22`, author hasn't addressed"
  ],
  "concerns": [
    "Large diff: 1.8k lines across 34 files",
    "New logic in `auth.py:55` has no accompanying test"
  ],
  "tests_needed": [
    "`auth.py:55` (new session-expiry path) has no test coverage"
  ],
  "open_questions": [
    "@bob: 'should this use the new TimeGranularity API?' — no response"
  ],
  "bot_flags": [
    "Copilot: potential null-deref at `utils.ts:22`",
    "sonarcloud: cognitive complexity 18 > 15 in `drill.ts:140`"
  ],
  "anti_patterns": [
    "`queries.py:55` builds raw SQL; rest of the module uses SQLAlchemy"
  ],
  "suggested_comment": "Optional — pre-filled body for add-review-comment / request-changes-review. First person, no @-mentions of the reviewer. Empty string if none.",
  "approval_comment": "Optional — pre-filled review body for approve-merge / approve-review. Author-aware: thank a human contributor; stay neutral/mechanical for bots (Dependabot, etc.). Reference the concrete merge-safety signal. Empty string if neither approve action is in `actions`.",
  "nudge_comment": "Optional — pre-filled body for nudge-author. Polite maintainer voice; @-mention the PR author so they get pinged; enumerate the concrete blockers (failing CI checks by name, specific unresolved threads with quoter @ and file:line). Empty string when `nudge-author` is not in `actions`.",
  "close_comment": "Optional — pre-filled body for `close`. Concrete reasoning ('already on master via #N', 'superseded by #M', 'repo direction has shifted'). Friendly, decisive, ends with 'reopen if you want to push it through.' Empty when `close` is not in actions.",
  "actions": ["nudge-author", "add-review-comment", "assess-on-worktree", "await-update", "approve-merge", "close", "prompt", "skip"],
  "notes": {
    "classification": "mergeable | blocked",
    "unresolved_others_count": 3,
    "linked_issue": "#1234"
  }
}
```

- `actions` MUST be primary-first and contain at least one id.
- `prompt` MUST always be in `actions` — it's the human escape
  hatch. Place it last (but before `skip`). The UI renders it as
  a `prompt…` details expander, not a button in the main row.
- `blockers` is for things that must be resolved before merge.
  `concerns` is for things the reviewer should know but that don't
  strictly block.
- `tests_needed` / `open_questions` / `bot_flags` / `anti_patterns`
  are all optional — omit empty arrays rather than emitting them.
- `suggested_comment` MUST be non-empty whenever `actions` contains
  `add-review-comment` or `request-changes-review` (primary or not).
  The UI pre-fills the modal from this field; leaving it empty
  forces the reviewer to write from scratch, which defeats the
  point of the button. Ground the draft in something concrete from
  the diff / threads / bot findings — quote a file:line or a
  specific question. First person, no @-mentions of the reviewer,
  and don't address the PR author by @-handle either unless a
  specific person's input is being requested.
- `fix-precommit-review` SHOULD appear in `actions` (primary on
  `triage: blocked` classifications) when pre-commit is the ONLY
  failing CI check — typically formatter drift or end-of-file-fixer
  noise. Running pre-commit locally and force-pushing the auto-fixes
  resolves it without the author needing to push. For non-
  maintainer-editable fork PRs the dispatcher will bail to
  `needs_human` automatically; we still propose it, since triage
  can't reliably tell whether the author allowed maintainer edits.
- `rebase-review` SHOULD appear in `actions` when the PR has merge
  conflicts or is BEHIND master by a meaningful amount and the
  conflicts look trivial from the file list (lockfiles, imports,
  changelogs). Same fork caveat as above.
- `nudge-author` SHOULD appear in `actions` whenever the PR has
  failing CI OR at least one unresolved review thread whose
  `first_author` is NOT the reviewer (`identity.github_username`)
  — but ONLY for human-authored PRs. Skip it for bot authors
  (Dependabot, renovate); those have their own action paths.
  Position it high in `actions` on `triage: blocked` classifications.
- `nudge_comment` MUST be non-empty whenever `actions` contains
  `nudge-author`. First-person maintainer voice, polite, starts
  with `@{author.login}` so the author gets pinged. Enumerate the
  specific blockers:
  - Failing CI: name the check(s) if you can tell (e.g. "`frontend-build`
    is red"); fall back to "CI is failing" if the check names are
    unclear.
  - Unresolved threads from others: quote the asker's handle +
    `path:line` + a short excerpt (e.g. "@alice's question on
    `models.py:140` about caching").
  Close with a low-pressure next-step (e.g., "let me know once
  those are addressed and I'll take another look").
- `approval_comment` MUST be non-empty whenever `actions` contains
  `approve-merge` or `approve-review`. Tailor to the PR author:
  - Human author → brief first-person thanks + the concrete
    merge-safety verdict (e.g., "Thanks @alice — CI green, no
    open threads, clean merge state. LGTM.").
  - Bot author (Dependabot, renovate, etc.) → neutral/mechanical,
    skip the thanks (e.g., "Dependabot version bump — CI green,
    mergeStateStatus CLEAN, no open threads.").
  Reference the specific signal that makes this safe to approve
  (CI status, merge state, lack of open threads) rather than a
  generic "LGTM". No @-mentions of the reviewer.
- Never @-mention `identity.github_username`. If you need to refer
  to the PR author, use their handle from the thread authors or
  `gh pr view`, not the reviewer's handle.

## Guardrails

- Read-only. No mutations, no comments posted.
- Budget ~15 turns. If you find yourself reading more than a few
  files in depth, stop and recommend `assess-on-worktree` instead
  — that skill is designed for deep reads.
- If the fetcher's signal fields are empty (GH query glitch), fall
  back to `gh pr view {number} --json mergeable,mergeStateStatus,
  statusCheckRollup` and re-derive.
- If `classification` disagrees with the fetcher's pre-bucket,
  that's fine — your output is authoritative for the user's read;
  the card stays in its pre-bucketed column.
