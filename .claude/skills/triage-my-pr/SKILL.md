---
name: triage-my-pr
description: Triage one of the authenticated user's own open non-draft PRs. Branches on three signals — merge conflicts, failing CI, unresolved review threads — and proposes the next action in priority order.
worktree_required: false
---

# Triage my own PR

You are triaging **one** open, non-draft PR authored by the user
(`identity.github_username`). The caller has already confirmed the PR
needs attention because at least one of these is true:

- merge conflicts,
- failing CI,
- unresolved review threads.

Your job is to read signals (cheap first), propose a primary action,
and list plausible fallbacks. Fixes happen in downstream skills; you
just decide what to do next.

## Inputs (runtime context)

- `pr` — `{owner, name, number, url, title, head_ref, mergeable,
  merge_state_status, ci_status, has_conflicts, unresolved_threads}`.
  `unresolved_threads` is a list of `{id, path, line, is_outdated,
  first_author, first_body, comments_count}`.
- `identity.github_username` — used to phrase the proposal in first
  person ("I'll …") rather than third.

## Priority order

Act on whichever blocker will unblock the rest:

1. **Merge conflicts** (`has_conflicts` or `mergeable == CONFLICTING`
   or `merge_state_status == DIRTY`) → primary `rebase`. Conflicts
   block meaningful CI / review iteration, so resolve first.
2. **Failing CI** — pick the CI-fix variant from the failing-check
   signature. See below.
3. **Unresolved review threads** (only these are left red) → primary
   `address-comments`.

If all three apply, propose `rebase` primary with `attempt-fix` and
`address-comments` queued behind it so the human sees the plan.

## Choosing a CI-fix variant

Only read a failing check's log if the classification isn't obvious
from check names. Use this mapping:

- **Only `pre-commit` checks red, everything else green** →
  `fix-precommit` (primary). Auto-fixers usually close it.
- **Log mentions `lockfile`, `YN0028`, `EBADDEP`, `poetry.lock is not
  consistent`, `pnpm-lock.yaml is not up to date`** → `update-lockfile`.
- **Test / type failures that look like real code breakage** →
  `attempt-fix` (primary), `plan-fix` as a secondary when the
  migration surface is broad.
- **Clearly a flake / infra hiccup** (runner shutdown, 502/503 from
  registries, `The operation was canceled` with no user-code output)
  → `retrigger-ci`.

For unclear failures, propose `attempt-fix` primary with `prompt` as
fallback — the downstream skill can decide to bail.

## Review-thread assessment

If unresolved threads are the main (or only) issue, scan the
`first_body` previews to check they're genuinely actionable before
proposing `address-comments`:

- Clearly actionable ("rename X", "extract helper", "add test case")
  → `address-comments`.
- Architectural pushback / scope debate ("I don't think we should do
  this at all") → `prompt` so the user weighs in personally.
- Purely conversational (+1, thanks, link sharing) → note in proposal
  that these don't need code changes; still propose `address-comments`
  so the downstream skill can leave a reply closing them out.

Outdated threads (`is_outdated: true`) usually mean the code already
moved past the concern — include `address-comments` so the downstream
skill can reply+resolve.

## Output

Return a single JSON object fenced as ```json ... ```:

```json
{
  "proposal": "One sentence, first person. Name the specific blocker.",
  "actions": ["rebase", "attempt-fix", "address-comments", "prompt"],
  "notes": {
    "classification": "conflicts | failing-ci | unresolved-threads | mixed | unknown",
    "merge_state_status": "CLEAN | BLOCKED | BEHIND | DIRTY | UNSTABLE | UNKNOWN",
    "failing_check": "optional — name of the primary red check",
    "unresolved_thread_count": 0,
    "log_excerpt": "optional — only if you read a log"
  }
}
```

### Valid action ids

`rebase`, `attempt-fix`, `fix-precommit`, `update-lockfile`,
`plan-fix`, `retrigger-ci`, `address-comments`, `prompt`. Order most →
least recommended. Never empty; never more than 5.

### Rules for `proposal`

- First person: "I have 3 unresolved review threads on
  `superset/models.py` — I'll work through them."
- Name the mechanism when you can: "CI is red on `pre-commit (current)`
  only — running pre-commit with auto-fixes should close it."
- Do not @-mention `identity.github_username`; don't thank the user
  for their own PR.

## Budget

Under ~6 turns. Review-thread previews are in context — you don't
need to re-fetch them. Only read a CI log if the check names don't
classify it cleanly.
