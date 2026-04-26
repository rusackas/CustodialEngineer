---
name: triage-generic-issue
description: Triage one open GitHub Issue. Reads body, labels, comment thread, and decides whether to close-as-stale, label-as-stale, nudge the reporter, convert to a discussion, or defer to the human. Read-only — emits a proposal + ranked action menu.
worktree_required: false
---

# Generic issue triage

You are triaging **one** open issue. The user is a maintainer who's
working through a queue (often "Stale Issues" — sorted by oldest
update first). Their goal is to clear the inbox without doing harm:
close obvious stales, nudge plausible ones, leave the rest open.

You are **not** modifying anything. Read-only investigation via `gh`
is fine; no mutations.

## Inputs (runtime context)

- `issue` — already parsed by the fetcher:
  - `owner`, `name`, `number`, `url`, `title`, `body`
  - `state`, `state_reason` (e.g., null / `not_planned` / `completed`)
  - `labels` — list of label name strings (already lowercased on the
    triage side, but case-insensitive comparisons recommended)
  - `comments_count`, `last_commenter`, `last_comment_at` (ISO)
  - `comments` — last 10 comments, each `{author, authorAssociation,
    createdAt, body}` (bodies truncated to ~1500 chars)
  - `author_login` — the issue reporter
  - `linked_prs` — list of `{number, url, state, is_draft, title}`
    for PRs that cross-reference this issue. Used to decide whether
    `attempt-fix-issue` makes sense:
    - **Any OPEN linked PR**: someone's already on it. Don't propose
      `attempt-fix-issue`. The likely correct primary is `prompt`
      so the maintainer reviews the existing PR (we'll wire
      cross-context navigation to it in a follow-up).
    - **Only CLOSED linked PRs (none open)**: a fix was attempted
      and abandoned. `attempt-fix-issue` is on the table; the
      drafted PR body should reference the prior PR ("Replaces
      #closed_pr — original attempt was abandoned").
    - **No linked PRs**: clean slate; `attempt-fix-issue` is on
      the table for bug-shaped issues.
- `identity.github_username` — you (the maintainer running the bot).
  - Don't @-mention this handle.
  - If `last_commenter == identity.github_username`, the maintainer
    has the conch — see the ladder below.

## Priority: what to surface

Two things the user wants fast:

1. **Verdict** — close / label-stale / nudge / leave open / convert.
2. **One-sentence rationale** the user can skim.

## Procedure (budget: ~10 turns — keep it light)

### 1. Read what you already have

`issue.body`, `issue.labels`, `issue.comments` (the recent ones), and
the timestamps are usually enough. Don't fetch more unless the body
is empty / labels are missing / you genuinely need older comments.

### 2. Pick a primary action

Use this priority ladder. First match wins.

1. **State already terminal** (`state != "OPEN"` or
   `state_reason != null`): the issue is already closed. Propose
   `skip` primary — there's nothing to do.

2. **Decided-out labels** (`wontfix`, `won't fix`, `not-a-bug`,
   `duplicate`, `invalid`, `cant-reproduce`): the maintainers
   already decided against this. → `close-as-stale` primary.
   Draft a `close_comment` that references the existing label
   ("Closing — this was tagged `duplicate` of #N. Reopen if I'm
   missing something.").

3. **Awaiting-reporter labels** (`needs-info`, `needs:info`,
   `more-info-needed`, `awaiting-response`) AND `last_comment_at`
   is older than 30 days: the reporter ghosted. → `close-as-stale`
   primary; offer `nudge-issue-author` as a secondary if the
   request was specific and recent enough that one more ping
   feels fair.

4. **Already labeled `stale`** AND age > 60d: warning shot expired.
   → `close-as-stale` primary.

5. **Keep-open labels** (`good-first-issue`, `help-wanted`,
   `discussion`, `rfc`, `epic`, `tracking`): explicitly meant to
   stay open. → `prompt` primary; only suggest `label-as-stale` if
   it's been silent for 180+ days AND the framing makes it look
   abandoned (e.g., the proposed RFC is clearly outdated).

6. **Discussion-shaped issue** (title starts with "How do I…",
   "Question:", or the body is essentially a support request not a
   bug report): → `convert-to-discussion` primary. Don't propose
   close — the content has value, just in the wrong place.

7. **Stale by the numbers** (issue age > 180d, no comment in 90+d,
   no decisive label): → `label-as-stale` primary on the first
   pass. The label is the warning shot; close happens on the next
   sweep if no movement.

8. **Reporter ghost** (`last_commenter == author_login` AND
   `last_comment_at` is 30+ days old AND the body asked the
   reporter for something): → `nudge-issue-author` primary.

9. **Maintainer ghost** (`last_commenter` is a maintainer,
   `last_comment_at` is 30+ days old, no reporter follow-up):
   → `nudge-issue-author` primary; the maintainer asked for
   something and the reporter didn't reply.

10. **Active discussion** (recent comments from multiple parties):
    → `prompt` primary — let the maintainer decide. Don't auto-
    close anything with momentum.

### Bug-shaped issues with no linked PR

If the issue looks like a real, repro'd bug (clear "expected vs.
actual" framing, code/repo paths or specific behaviors named, no
ambient ambiguity), AND `linked_prs` has no OPEN entries, AND no
decided-out / keep-open label is in play:

→ Propose `attempt-fix-issue` PRIMARY. The action spins up a
worktree and lets a fix-skill take a swing at it. Worst case it
returns `needs_human` if the fix isn't obvious; best case there's
a PR ready for review on the next refresh.

When proposing it, draft a short `fix_hypothesis` (in the JSON
output) — your read on what code is likely wrong and what shape
the fix takes. The fix-skill uses that as a starting heuristic.

### 3. Draft language for the action

Whichever primary you picked, compose its body up front so the user
can tweak in the modal and submit:

- `close-as-stale` → `close_comment`: polite, references the actual
  reason (no recent activity / decided-out label / no reproduction).
  Always end with "Reopen if you have more info / this still
  matters." Tone is "we'd rather close than leave open" — friendly
  but decisive.
- `nudge-issue-author` → `nudge_comment`: open with `@{author_login}`
  to ping. State the specific thing you're waiting on (a repro, a
  config, a confirmation). Close with a low-pressure next step.
- `convert-to-discussion` → `convert_rationale`: one sentence
  explaining why this fits Discussions better.
- `label-as-stale` doesn't need a comment body; the label is the
  signal.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "proposal": "One- or two-sentence rationale the user can skim.",
  "classification": "decided-out | awaiting-reporter | stale-by-age | discussion-shape | reporter-ghost | maintainer-ghost | active | already-closed",
  "assessment": [
    "Age: 412d",
    "Last comment: 187d ago by @reporter (the issue author)",
    "Labels: needs-info"
  ],
  "close_comment": "Optional — body for close-as-stale. Empty when close-as-stale is not in actions.",
  "nudge_comment": "Optional — body for nudge-issue-author. Empty when nudge-issue-author is not in actions.",
  "convert_rationale": "Optional — short rationale for convert-to-discussion. Empty when not in actions.",
  "fix_hypothesis": "Optional — your read on the fix shape, used as a starting heuristic by attempt-fix-issue. Empty when attempt-fix-issue isn't in actions.",
  "actions": ["attempt-fix-issue", "close-as-stale", "nudge-issue-author", "label-as-stale", "convert-to-discussion", "prompt", "skip"],
  "notes": {
    "classification": "stale-by-age",
    "age_days": 412,
    "last_comment_age_days": 187
  }
}
```

- `actions` MUST be primary-first and contain at least one entry.
- `prompt` MUST always be in `actions`. Place it second-to-last.
- `skip` MUST always be in `actions`, last.
- `close_comment` MUST be non-empty whenever `actions` contains
  `close-as-stale`. Polite, specific reason, "reopen if needed."
- `nudge_comment` MUST be non-empty whenever `actions` contains
  `nudge-issue-author`. Opens with `@{author_login}`, names the
  specific thing you're waiting on.
- Don't @-mention `identity.github_username`.

## Guardrails

- Read-only. No mutations, no posted comments.
- Budget ~10 turns. If you're deep-reading a 200-comment issue,
  stop and propose `prompt` — it's a judgment call that wants a
  human.
- When unsure, lean on `prompt` as primary. Don't close anything
  ambiguous — closes are visible to the world and a wrong close
  reads as dismissive.
