---
name: approve-and-merge-pr
description: Approve a Dependabot PR and enable GitHub auto-squash-merge. Refuses to proceed if the PR's mergeStateStatus is not CLEAN, so a misfired triage can't land a half-broken PR.
worktree_required: false
---

# Approve and auto-squash-merge a PR

**Inputs** (from runtime context): `pr.owner`, `pr.name`, `pr.number`,
optional `pr.title`, optional `comment_body` (the approval review body
— a human may have edited it), `identity.github_username` (the human
operating this bot), and `dry_run` (boolean).

## Voice guardrails (read before composing the approval body)

- `identity.github_username` names the human operating this bot. NEVER
  @-mention them, NEVER refer to them in third person, NEVER write
  attribution like "approved via repobot on @username's behalf". The
  review is from them — speak as them if you speak at all.
- Dependabot and other bots are not people. Don't thank them, don't
  praise them, don't address them conversationally. Approval text
  should be terse and operational.
- If `comment_body` is provided, post it verbatim — the human already
  approved the wording. Do not rewrite, reformat, or append.

## Procedure — you MUST do these steps in order

### 1. Re-verify the PR is actually safe to merge

Don't trust the triage. Two calls — `gh pr view` for most fields plus
a GraphQL query for `reviewThreads` (which isn't exposed on `gh pr
view --json`):

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json number,state,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,reviews,comments

gh api graphql -F pr={pr.number} -F owner={pr.owner} -F name={pr.name} \
  -f query='query($owner:String!,$name:String!,$pr:Int!){
    repository(owner:$owner,name:$name){
      pullRequest(number:$pr){
        reviewThreads(first:50){nodes{isResolved isOutdated
          comments(first:1){nodes{path line body author{login}}}}}
      }}}'
```

Hard bails with `status: needs_human` — these are always a no-go:
- `state != "OPEN"`  (already merged / closed).
- `isDraft == true`.
- `mergeable == "CONFLICTING"`.
- Any required check in `statusCheckRollup` has conclusion other than
  `SUCCESS`/`NEUTRAL`/`SKIPPED`.

`mergeStateStatus` gate — accept only one of:
- `"CLEAN"` — the ideal case.
- `"BLOCKED"` **iff** `reviewDecision == "REVIEW_REQUIRED"` AND every
  required check is green AND `mergeable != "CONFLICTING"`. This is
  the "waiting on a maintainer" state — it unblocks the moment we
  approve, which is exactly what this skill does. The component
  checks above guarantee BLOCKED isn't hiding a different failure.

Bail for anything else — `BEHIND`, `DIRTY`, `UNKNOWN`, `UNSTABLE`,
`HAS_HOOKS` — a human should look. (`UNSTABLE` = non-required checks
red; don't bypass.)

### 1b. Check reviewer signals (critical — always do this)

Even a CLEAN, CI-green PR can have reviewer concerns on the thread
that should block auto-merge. Bail with `status: needs_human` if:

- `reviewDecision == "CHANGES_REQUESTED"`.
- Any entry in `reviews[]` has `state == "CHANGES_REQUESTED"` that
  hasn't been superseded by a later `APPROVED` from the same author.
- Any entry in `reviewThreads[]` has `isResolved == false` AND
  `isOutdated == false`. Resolved threads are fine; outdated-on-
  removed-code threads are fine; live unresolved threads are not —
  a human needs to resolve or respond. When bailing for this, name
  the file/line in the message so the card is actionable.
- A recent comment (last ~10) from a maintainer includes blocking
  language like "don't merge", "hold", "wait", "blocked on", "needs
  follow-up", "revert this", "breaking change".
- A bot reviewer (dosu / dosu-bot, coderabbitai[bot], sonar, etc.)
  has flagged a concern, unresolved TODO, regression, or unanswered
  question. Pure summaries / "LGTM" / changelog diffs are fine to
  ignore.

When bailing, the `message` should name the concern specifically
(e.g., "dosu-bot flagged a potential regression in comment #6") so
the card is actionable.

The `message` in the output JSON should explain exactly which gate
failed — that's what makes the card actionable.

### 2. If `dry_run == true`

Print the exact commands you *would* have run and stop. Report
`status: skipped_dry_run`.

### 3. Otherwise approve and enable auto-merge

If `comment_body` was provided, pipe it through stdin so multiline /
special chars survive; otherwise use the default body below.

```
# with a human-provided body:
printf '%s' "$COMMENT_BODY" | \
  gh pr review {pr.number} --repo {pr.owner}/{pr.name} --approve --body-file -

# default:
gh pr review {pr.number} --repo {pr.owner}/{pr.name} --approve \
  --body "Dependabot version bump — CI green, mergeStateStatus CLEAN."

gh pr merge {pr.number} --repo {pr.owner}/{pr.name} --squash --auto
```

`--auto` tells GitHub to merge as soon as all required checks and
branch-protection rules are satisfied. If the PR is already CLEAN,
the merge typically happens within a few seconds.

### 4. Verify

```
gh pr view {pr.number} --repo {pr.owner}/{pr.name} \
  --json state,mergedAt,autoMergeRequest
```

- `state == MERGED` → `status: completed`, include `merged_at`.
- `autoMergeRequest` is non-null and `state == OPEN` →
  `status: completed`, `message: "auto-merge enabled; will land when
  branch protection is satisfied"`.
- Anything else → `status: error`, include the observed state.

## Output

Emit a single fenced JSON block:

```json
{
  "status": "completed | skipped_dry_run | needs_human | error",
  "message": "short one-sentence summary naming what happened or why we bailed",
  "merged_at": "ISO timestamp if the merge already landed, else null",
  "auto_merge_enabled": true,
  "merge_state_status": "CLEAN | BLOCKED | …"
}
```

## Guardrails

- NEVER force-merge (`--admin`), NEVER bypass checks.
- **Self-authored PRs**: when the PR's `author.login` equals
  `identity.github_username`, GitHub blocks the explicit `--approve`
  step (you can't approve your own work). Skip the `gh pr review
  --approve` call and go straight to `gh pr merge --squash --auto`.
  - If `reviewDecision == "REVIEW_REQUIRED"` or `"CHANGES_REQUESTED"`,
    bail with `status: needs_human` and a message like "self-authored
    PR but branch protection requires another reviewer — ask a
    collaborator to approve before retrying." The triage layer
    normally catches this earlier; we re-check at action time so a
    direct/manual click doesn't leak through.
  - If `reviewDecision` is `null` (no review requirement) or
    `APPROVED`, attempt the merge. GitHub will surface its own
    error if branch protection still blocks; pass it through as
    `status: needs_human` with the verbatim error.
- For all other PRs (someone else's work), the skill IS approving on
  behalf of `identity.github_username`. Voice rules above apply:
  don't @-mention the operator, don't write attribution like
  "approved via repobot."
- If in doubt, `status: needs_human` with a specific reason.
